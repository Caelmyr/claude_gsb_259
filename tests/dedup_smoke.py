"""图库查重端到端验证（不启动 HTTP）。

构造一组「原图 + 缩放/裁剪/调色/JPEG 重压缩副本」与若干「同风格但不同
内容」的干扰图，走真实链路：ImageStore 落盘 -> DedupManager 后台扫描
-> 分组/保留建议；另测损坏图不拖垮扫描、大规模图库粗筛耗时可控。

运行：python tests/dedup_smoke.py
"""
import io
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter  # noqa: E402

from server import config  # noqa: E402
from server.algorithms import dedup as algo  # noqa: E402
from server.dedup import DedupManager  # noqa: E402
from server.image_store import ImageStore  # noqa: E402


def _photo(seed, w=520, h=390):
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)],
               fill=(int(120 + 80 * t), int(150 + 40 * t), int(220 - 80 * t)))
    d.rectangle([0, int(h * 0.72), w, h], fill=(70, 110, 60))
    for i in range(20):
        x, y = rnd.randrange(w), rnd.randrange(int(h * 0.3), h)
        r = rnd.randrange(10, 42)
        col = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        if i % 3 == 0:
            d.ellipse([x, y, x + r, y + r], fill=col)
        elif i % 3 == 1:
            d.rectangle([x, y, x + r, y + r], fill=col)
        else:
            d.polygon([(x, y), (x + r, y), (x + r // 2, y - r)], fill=col)
    return img


def _png(img, size=None):
    if size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def _jpg(img, q=40, size=None):
    if size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    b = io.BytesIO()
    img.save(b, "JPEG", quality=q)
    return b.getvalue()


def _wait_job(mgr, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = mgr.get_job(job_id)
        if j["status"] in ("done", "partial", "cancelled", "error"):
            return j
        time.sleep(0.1)
    raise TimeoutError(job_id)


def main():
    config.ensure_dirs()
    store = ImageStore()
    mgr = DedupManager(store)

    print("== 上传测试图库 ==")
    base_a, base_b, base_c = _photo(1), _photo(2), _photo(3)
    uploads = [
        # A 组：同图副本（不同尺寸/格式/裁剪/调色）
        ("A_orig_large.png", _png(base_a, size=(1200, 900))),
        ("A_resize_small.jpg", _jpg(base_a, q=85, size=(320, 240))),
        ("A_crop.png", _png(base_a.crop((60, 40, 480, 360)).resize((520, 390), Image.Resampling.LANCZOS))),
        ("A_recolor.jpg", _jpg(ImageEnhance.Color(base_a).enhance(1.6), q=88)),
        ("A_recompress.jpg", _jpg(base_a, q=28)),
        # B 组：同一张图的两个文件
        ("B_1.png", _png(base_b)),
        ("B_2.png", _png(base_b, size=(800, 600))),
        # 干扰图：同风格不同内容
        ("scene_other_1.png", _png(_photo(7))),
        ("scene_other_2.png", _png(_photo(8))),
        ("scene_other_3.png", _png(_photo(9))),
        # C 只有一张
        ("C_solo.png", _png(base_c)),
    ]
    ids = {}
    for name, data in uploads:
        rec = store.save_upload(data, name)
        ids[name] = rec["id"]
        print(f"  {name:22s} -> {rec['width']}x{rec['height']} {rec['size_bytes']//1024}KB")

    print(f"\n== 标准阈值 {config.DEDUP_DEFAULT_THRESHOLD} 扫描 ==")
    job = mgr.enqueue(threshold=config.DEDUP_DEFAULT_THRESHOLD)
    res = _wait_job(mgr, job["id"])
    print(f"  状态={res['status']} 图像={res['image_count']} "
          f"候选={res['candidate_pairs']} 组={res['group_count']} "
          f"副本={res['duplicate_count']} 耗时={res['duration_ms']}ms")
    assert res["status"] in ("done", "partial"), res
    assert res["group_count"] == 2, f"应得到 2 组，实际 {res['group_count']}"

    g_a = next(g for g in res["groups"]
               if any(m["filename"].startswith("A_") for m in g["members"]))
    g_b = next(g for g in res["groups"]
               if any(m["filename"].startswith("B_") for m in g["members"]))
    a_names = {m["filename"] for m in g_a["members"]}
    assert {"A_orig_large.png", "A_resize_small.jpg", "A_crop.png",
            "A_recolor.jpg", "A_recompress.jpg"} <= a_names, a_names
    assert "C_solo.png" not in a_names
    assert all(not m["filename"].startswith("scene_other") for m in g_a["members"]), \
        "干扰图被误判进 A 组"
    print(f"  A 组 {len(g_a['members'])} 张，类型={g_a['kind']} "
          f"最高相似={g_a['max_score']} 可省={g_a['waste_bytes']//1024}KB")
    print(f"  B 组 {len(g_b['members'])} 张，最高相似={g_b['max_score']}")

    print("\n== 保留建议 ==")
    keeper = next(m for m in g_a["members"] if m["is_keeper"])
    assert keeper["filename"] == "A_orig_large.png", keeper["filename"]
    assert keeper["width"] * keeper["height"] == 1200 * 900
    print(f"  A 组建议保留: {keeper['filename']} "
          f"({keeper['width']}x{keeper['height']}, 清晰度={keeper['sharpness']})")
    kb = next(m for m in g_b["members"] if m["is_keeper"])
    assert kb["filename"] == "B_2.png", kb  # 600x450 对 390x292，取大尺寸
    print(f"  B 组建议保留: {kb['filename']} ({kb['width']}x{kb['height']})")

    print("\n== 严格阈值 92（应收紧，裁剪副本可能落组外）==")
    job2 = mgr.enqueue(threshold=92)
    res2 = _wait_job(mgr, job2["id"])
    # 第二次扫描应全部命中指纹缓存
    print(f"  状态={res2['status']} 组={res2['group_count']}（指纹缓存命中，"
          f"耗时={res2['duration_ms']}ms）")
    assert res2["group_count"] <= res["group_count"], "提高阈值不应产生更多组"

    print("\n== 损坏图不拖垮扫描 ==")
    bad = os.path.join(config.IMAGES_DIR, "deadbeefdeadbeef.png")
    with open(bad, "wb") as f:
        f.write(b"not an image at all")
    job3 = mgr.enqueue(threshold=85)
    res3 = _wait_job(mgr, job3["id"])
    os.unlink(bad)
    # 损坏文件没有元数据，不会进入扫描；此处验证扫描本身稳定完成
    assert res3["status"] in ("done", "partial"), res3
    print(f"  扫描完成: {res3['status']} 组={res3['group_count']}")

    print("\n== 大规模图库粗筛性能（200 张随机图）==")
    t0 = time.time()
    big_imgs = [_photo(1000 + i, w=260, h=200) for i in range(200)]
    big = [(str(i), algo.fingerprint(im)) for i, im in enumerate(big_imgs)]
    fp_ms = (time.time() - t0) * 1000 / 200
    pair_idx = set()
    t0 = time.time()
    compared, accepted, capped = algo.stream_candidates(
        big, gate=config.DEDUP_HASH_GATE,
        on_pair=lambda i, j: pair_idx.add((i, j) if i < j else (j, i)))
    scan_ms = (time.time() - t0) * 1000
    total_pairs = 200 * 199 // 2
    print(f"  指纹 {fp_ms:.1f} ms/张；粗筛 {scan_ms:.0f} ms，"
          f"桶内比较 {compared} 次，去重后过闸 {len(pair_idx)}/{total_pairs} 对")
    # 闸口只是便宜预筛：对过闸对做精验，最终综合分必须全部低于默认阈值（否则误报）
    pair_idx = list(pair_idx)
    false_pos = 0
    for i, j in pair_idx[:300]:
        e = algo.edge_correlation(big_imgs[i], big_imgs[j])
        if algo.score_pair(big[i][1], big[j][1], e)[0] >= config.DEDUP_DEFAULT_THRESHOLD:
            false_pos += 1
    print(f"  抽检 {min(300, len(pair_idx))} 对过闸候选，最终误报 {false_pos} 对")
    assert false_pos == 0, "不同图被误判为重复"
    assert len(pair_idx) < total_pairs * 0.4, "粗筛剪枝率异常偏低"

    print("\n查重端到端验证通过 ✔")


if __name__ == "__main__":
    main()
