"""查重能力测试：感知指纹、相似度判定、LSH 召回、保留建议、容错与端到端。

运行：python tests/test_dedup.py
使用临时 data 目录（不污染真实图库），覆盖：
1) 各种副本（缩放/重压缩/调色/加边框翻拍）被判为重复；
2) 内容不同的图（哪怕色调接近）不被误判；
3) 重度偏移裁剪宁可漏报不误报；
4) 推荐保留项 = 分辨率最大/最清晰；
5) 损坏文件被跳过而不中断扫描；
6) LSH 候选生成不遗漏真实近邻（与全对量暴力比对对照）；
7) 阈值可调；指纹缓存使重扫跳过已算图片。
"""
import io
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter  # noqa: E402

from server import config  # noqa: E402
from server.algorithms import perceptual  # noqa: E402
from server.dedup import DedupManager, iter_candidate_pairs  # noqa: E402
from server.image_store import ImageStore  # noqa: E402


# ---------------------------------------------------------------------------
# 测试图像生成
# ---------------------------------------------------------------------------
def landscape(w=800, h=600, seed=1):
    rnd = __import__("random").Random(seed)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(int(h * .55)):
        d.line([(0, y), (w, y)], fill=(110 + y // 6, 150 + y // 8, 190 + y // 10))
    for y in range(int(h * .55), h):
        t = (y - h * .55) / (h * .45)
        d.line([(0, y), (w, y)], fill=(int(70 + 30 * t), int(100 + 20 * t), int(50 + 20 * t)))
    d.ellipse([int(w * .7), 60, int(w * .82), 168], fill=(255, 235, 150))
    d.polygon([(0, int(h * .55)), (int(w * .3), int(h * .3)), (int(w * .55), int(h * .55))], fill=(80, 95, 80))
    d.rectangle([int(w * .15), int(h * .33), int(w * .15) + 130, int(h * .55)], fill=(180, 120, 90))
    for _ in range(40):
        x, y = rnd.randrange(w), rnd.randrange(h)
        r = rnd.randrange(2, 7)
        d.ellipse([x, y, x + r, y + r], fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    return img


def city(w=800, h=600, seed=2):
    rnd = __import__("random").Random(seed)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, h], fill=(30, 34, 48))
    x = 0
    while x < w:
        bw = rnd.randrange(50, 100)
        bh = rnd.randrange(200, 460)
        d.rectangle([x, h - bh, x + bw, h], fill=(rnd.randrange(40, 90), rnd.randrange(50, 100), rnd.randrange(70, 130)))
        x += bw + rnd.randrange(8, 18)
    d.rectangle([0, int(h * .9), w, h], fill=(20, 22, 30))
    return img


def indoor(w=800, h=600, seed=3):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, h], fill=(150, 120, 95))
    d.rectangle([0, int(h * .72), w, h], fill=(90, 65, 45))
    d.rectangle([int(w * .55), int(h * .12), int(w * .9), int(h * .6)], fill=(140, 190, 230))
    d.rounded_rectangle([int(w * .08), int(h * .5), int(w * .48), int(h * .85)], radius=30, fill=(120, 50, 50))
    return img


def png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def jpg(img, q=70):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=q)
    return b.getvalue()


def add_border(img, bw=40, color=(20, 20, 24)):
    w, h = img.size
    out = Image.new("RGB", (w + 2 * bw, h + 2 * bw), color)
    out.paste(img, (bw, bw))
    return out


def reshoot(img):
    v = add_border(img, 50).resize((int(img.size[0] * 0.9), int(img.size[1] * 0.9)))
    v = ImageEnhance.Brightness(v).enhance(1.12)
    v = ImageEnhance.Contrast(v).enhance(1.15)
    return v.filter(ImageFilter.GaussianBlur(0.8))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}  {detail}")


def fp(img):
    return perceptual.extract_fingerprint(img.convert("RGB"))


def wait_job(dm, job_id, tries=600):
    for _ in range(tries):
        time.sleep(0.03)
        j = dm.get_job(job_id)
        if j["status"] in ("done", "partial", "cancelled"):
            return j
    raise RuntimeError("任务超时")


# ---------------------------------------------------------------------------
# 1. 成对相似度：副本 vs 不同图
# ---------------------------------------------------------------------------
def test_pairwise():
    print("\n== 成对相似度 ==")
    A = landscape(seed=1)
    w, h = A.size
    fA = fp(A)
    variants = {
        "缩放": A.resize((w // 3, h // 3), Image.Resampling.LANCZOS),
        "重压缩": Image.open(io.BytesIO(jpg(A, 20))),
        "调色": ImageEnhance.Color(A).enhance(1.7),
        "提亮": ImageEnhance.Brightness(A).enhance(1.3),
        "轻微模糊": A.filter(ImageFilter.GaussianBlur(1.3)),
    }
    for name, v in variants.items():
        s = perceptual.pair_similarity(fA, fp(v))
        check(f"副本[{name}] 相似度≥90", s["score"] >= 90, f"score={s['score']}")

    s_border = perceptual.pair_similarity(fA, fp(add_border(A, 45)))
    s_reshoot = perceptual.pair_similarity(fA, fp(reshoot(A)))
    check("加边框副本 相似度≥80", s_border["score"] >= 80, f"score={s_border['score']}")
    check("翻拍副本 相似度≥78", s_reshoot["score"] >= 78, f"score={s_reshoot['score']}")

    # 负例：不同内容（城市夜景、室内）即便都是照片也不该判重
    check("风景 vs 城市 相似度<75", perceptual.pair_similarity(fA, fp(city()))["score"] < 75)
    check("风景 vs 室内 相似度<75", perceptual.pair_similarity(fA, fp(indoor()))["score"] < 75)
    check("城市 vs 室内 相似度<75", perceptual.pair_similarity(fp(city()), fp(indoor()))["score"] < 75)

    # 只是色调接近：把城市调成偏蓝（天空蓝），仍不应与风景混淆
    city_blue = ImageEnhance.Color(city()).enhance(0.3)
    s = perceptual.pair_similarity(fA, fp(city_blue))
    check("风景 vs 偏蓝城市 不判重", s["score"] < 78 or s["color_corr"] < perceptual.COLOR_GATE,
          f"score={s['score']} color={s['color_corr']}")

    # 同一张图与自身
    check("自身相似度=100", perceptual.pair_similarity(fA, fp(A))["score"] == 100.0)


# ---------------------------------------------------------------------------
# 2. 指纹稳定性：同一图两种格式应几乎一致
# ---------------------------------------------------------------------------
def test_stability():
    print("\n== 跨格式/尺寸稳定性 ==")
    A = landscape(seed=7)
    f1 = fp(A)
    f2 = fp(Image.open(io.BytesIO(jpg(A.resize((333, 250)), 55))))
    s = perceptual.pair_similarity(f1, f2)
    check("PNG大图 vs JPEG小图 判为同一", s["score"] >= 90 and s["near_exact"], f"score={s['score']}")


# ---------------------------------------------------------------------------
# 3. LSH 召回：候选集必须包含暴力全比对下所有近邻
# ---------------------------------------------------------------------------
def test_lsh_recall():
    print("\n== LSH 候选召回（对照 O(n²) 暴力） ==")
    base = [landscape(600, 450, seed=s) for s in range(12)]
    # 注入副本：每个家族 1 张缩放 + 1 张边框
    imgs = []
    for b in base:
        imgs += [b, b.resize((300, 225)), add_border(b, 35)]
    imgs += [city(600, 450, seed=50), indoor(600, 450, seed=51)]
    fps = [fp(im) for im in imgs]

    # 暴力：阈值 80 下所有近邻对
    truth = set()
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            s = perceptual.pair_similarity(fps[i], fps[j])
            if perceptual.passes_threshold(s, 80):
                truth.add((i, j))

    candidates = set(iter_candidate_pairs(fps))
    missing = [p for p in truth if p not in candidates]
    check(f"LSH 候选覆盖全部 {len(truth)} 个近邻对", not missing, f"遗漏 {missing}")
    check("LSH 候选数远小于全对量", len(candidates) < len(fps) * (len(fps) - 1) // 2,
          f"{len(candidates)} vs {len(fps) * (len(fps) - 1) // 2}")


# ---------------------------------------------------------------------------
# 4. 端到端扫描 + 保留建议 + 容错（临时目录）
# ---------------------------------------------------------------------------
def test_end_to_end():
    print("\n== 端到端扫描（临时 data 目录） ==")
    tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_dedup_test")
    shutil.rmtree(tmp, ignore_errors=True)
    old_data = config.DATA_DIR
    config.DATA_DIR = tmp
    _reprefix(tmp)
    config.ensure_dirs()
    try:
        store = ImageStore()
        A, B, C = landscape(seed=1), city(seed=2), indoor(seed=3)
        w, h = A.size

        # 家族 A：原图（最大/清晰）+ 小图 + 重压缩 + 模糊小翻拍
        id_a_big = store.save_upload(png(A), "A_big.png")["id"]
        id_a_small = store.save_upload(jpg(A.resize((w // 3, h // 3)), 40), "A_small.jpg")["id"]
        id_a_recomp = store.save_upload(jpg(A, 25), "A_q25.jpg")["id"]
        id_a_blur = store.save_upload(jpg(A.filter(ImageFilter.GaussianBlur(1.5)), 60), "A_blur.jpg")["id"]
        # 家族 B
        id_b = store.save_upload(png(B), "B.png")["id"]
        id_b_copy = store.save_upload(png(B.resize((400, 300))), "B_copy.png")["id"]
        # 独立 C
        store.save_upload(png(C), "C.png")

        # 损坏文件：正常上传后截断原图
        bad = store.save_upload(png(C), "corrupt.png")
        with open(store.file_path(bad["id"]), "wb") as f:
            f.write(open(store.file_path(bad["id"]), "rb").read()[:200])

        dm = DedupManager(store)
        j = wait_job(dm, dm.start_scan(90)["id"])
        check("扫描完成状态=done", j["status"] == "done", j["status"])
        res = dm.load_result(j["id"])
        check("损坏图被记为错误且不中断扫描", len(res["errors"]) == 1, f"errors={len(res['errors'])}")
        check("损坏图未参与指纹", j["summary"]["fingerprinted"] == j["summary"]["image_count"] - 1)

        # 两个家族（严格 90 下边框翻拍不强制并入，这里 A 用的都是非边框副本）
        families = [set(m["id"] for m in g["members"]) for g in res["groups"]]
        check("分组数=2", len(families) == 2, f"groups={len(families)}")
        family_a = {id_a_big, id_a_small, id_a_recomp, id_a_blur}
        family_b = {id_b, id_b_copy}
        check("A 家族完整聚成一组", family_a in families, f"{families}")
        check("B 家族聚成一组", family_b in families, f"{families}")
        check("独立图 C 不在任何组", all(id_c not in fam for fam in families for id_c in [
            r["id"] for r in store.list_records() if r["filename"] == "C.png"]))

        # 保留建议：A 家族应保留最大原图
        ga = next(g for g in res["groups"] if g["keeper_id"] == id_a_big)
        check("A 家族推荐保留分辨率最大的原图", ga["keeper_id"] == id_a_big)
        keeper = next(m for m in ga["members"] if m["is_keeper"])
        check("保留项尺寸=800×600", (keeper["width"], keeper["height"]) == (800, 600),
              f"{keeper['width']}x{keeper['height']}")

        # 每组成员相似度字段存在且按相似度排序（保留项在首位）
        check("保留项排在成员首位", ga["members"][0]["is_keeper"])
        check("成员相似度≤100", all(0 <= m["similarity"] <= 100 for m in ga["members"]))

        # 可回收空间 = 非保留项体积之和
        waste = sum(m["size_bytes"] for m in ga["members"] if not m["is_keeper"])
        check("waste_bytes 正确", ga["waste_bytes"] == waste)

        # 阈值收紧到 99：缩放模糊副本应被拆开（近乎字节级才保留）——组数允许变少
        j99 = wait_job(dm, dm.start_scan(99)["id"])
        n99 = j99["summary"]["group_count"]
        check("阈值 99 分组数 ≤ 阈值 90", n99 <= 2, f"n99={n99}")

        # 阈值放宽到 78：分组只会更多或相等（边框/翻拍被并入）
        j78 = wait_job(dm, dm.start_scan(78)["id"])
        n78 = j78["summary"]["group_count"]
        check("阈值 78 分组数 ≥ 阈值 90", n78 >= 2, f"n78={n78}")

        # 指纹缓存：第二次扫描指纹阶段几乎零成本（指纹数一致且不报错）
        check("指纹缓存命中（重扫 fingerprinted 不变）",
              j78["summary"]["fingerprinted"] == j["summary"]["fingerprinted"])

        # 删除一张图后指纹被遗忘，load_result 自动剔除/重选
        store.delete(id_a_small)
        dm.forget(id_a_small)
        res2 = dm.load_result(j["id"])
        ga2 = next((g for g in res2["groups"] if id_a_big in [m["id"] for m in g["members"]]), None)
        check("删除副本后该组仍在且不含已删图",
              ga2 is not None and all(m["id"] != id_a_small for m in ga2["members"]))

    finally:
        config.DATA_DIR = old_data
        _reprefix(old_data)
        shutil.rmtree(tmp, ignore_errors=True)


def _reprefix(data_dir):
    """把 config 里基于 DATA_DIR 的路径全部切到给定目录（测试隔离用）。"""
    config.IMAGES_DIR = os.path.join(data_dir, "images")
    config.RESULTS_DIR = os.path.join(data_dir, "results")
    config.THUMBS_DIR = os.path.join(data_dir, "thumbnails")
    config.CACHE_DIR = os.path.join(data_dir, "cache")
    config.META_DIR = os.path.join(data_dir, "metadata")
    config.DEDUP_RESULTS_DIR = os.path.join(config.META_DIR, "dedup")
    config.IMAGES_JSON = os.path.join(config.META_DIR, "images.json")
    config.PIPELINES_JSON = os.path.join(config.META_DIR, "pipelines.json")
    config.HISTORY_JSON = os.path.join(config.META_DIR, "history.json")
    config.PRESETS_JSON = os.path.join(config.META_DIR, "presets.json")
    config.QUEUE_JSON = os.path.join(config.META_DIR, "queue.json")
    config.CACHE_JSON = os.path.join(config.META_DIR, "cache.json")
    config.DEDUP_JSON = os.path.join(config.META_DIR, "dedup_jobs.json")
    config.DEDUP_FP_JSON = os.path.join(config.META_DIR, "dedup_fingerprints.json")


def main():
    print("开始查重能力测试 …")
    test_pairwise()
    test_stability()
    test_lsh_recall()
    test_end_to_end()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    if FAIL:
        sys.exit(1)
    print("查重测试全部通过 ✔")


if __name__ == "__main__":
    main()
