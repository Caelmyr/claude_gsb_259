"""查重：全库扫描 -> 感知指纹 -> 相似度聚类 -> 保留建议。

设计要点（对应需求里的「规模大、格式/大小不一也要能稳定跑完」）：

- 后台线程池扫描，逐张出进度（dedup_jobs.json），可随时取消，不阻塞请求。
- 指纹按 image_id 缓存在 dedup_fingerprints.json，只在新图/强制刷新时重算；
  解析失败的图记成 error 跳过，绝不中断整次扫描。
- 大图先完整解码验证（损坏/截断文件在此记 error 跳过，绝不拿旧缩略图冒充），
  再压到 DEDUP_THUMB_DIM 计算特征，内存恒定；同一时刻只允许一个扫描。
- 避免 O(n²)：对 64bit pHash 建 LSH 倒排桶（4×16bit 分块 + 汉明邻域探测），
  只对「至少一个块足够接近」的候选对做完整评分；对全图、两个中心裁剪、
  5 个偏移窗口 pHash 各建一遍表（连续/交错分块），保证加边框、非对称
  裁边的翻拍副本也能召回。
- 并查集聚类；推荐保留项：分辨率最高优先；同分辨率时无损/未重压缩格式
  优先，再比组内归一化的清晰度（拉普拉斯方差）与编码信息量。
- 分组结果写侧载文件 data/metadata/dedup/<job_id>.json，任务列表保持轻量。
"""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config
from .algorithms import perceptual
from .storage import JsonStore, atomic_write_json, now_iso, read_json

# 64bit 分 4 个 16bit 块（contiguous / 交错两种分块方式）
_BANDS = 4
_BAND_BITS = 16


# ---------------------------------------------------------------------------
# 并查集
# ---------------------------------------------------------------------------
class DSU:
    def __init__(self, n):
        self.p = list(range(n))
        self.w = [1] * n

    def find(self, x):
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.w[ra] < self.w[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        self.w[ra] += self.w[rb]


# ---------------------------------------------------------------------------
# LSH 候选对生成
# ---------------------------------------------------------------------------
def _band_value(hv, band, layout):
    """从 64bit 哈希里取第 band 个 16bit 块。layout 决定取位方式。"""
    v = 0
    for j in range(_BAND_BITS):
        if layout == "interleave":
            # 位交错：块 band 取第 band, band+4, band+8... 位
            bitpos = band + j * _BANDS
        else:
            bitpos = band * _BAND_BITS + j
        v = (v << 1) | ((hv >> (63 - bitpos)) & 1)
    return v


def _probes(bucket, radius=1):
    """一个 16bit 桶号及其汉明邻域（radius=1 时 17 个）。"""
    yield bucket
    if radius >= 1:
        for b in range(_BAND_BITS):
            yield bucket ^ (1 << b)


def _build_tables(index, fps, hash_fn, layout):
    """为一组指纹建 {band: {bucket: [indices]}} 倒排表。

    hash_fn(idx) 返回一个或多个 64bit 值（标量哈希给一个，网格姿态给一组）。
    """
    tables = [{} for _ in range(_BANDS)]
    for idx in index:
        values = hash_fn(idx)
        if not isinstance(values, (list, tuple)):
            values = [values]
        for hv in values:
            for band in range(_BANDS):
                bv = _band_value(hv, band, layout)
                tables[band].setdefault(bv, []).append(idx)
    return tables


def iter_candidate_pairs(fps):
    """从指纹列表生成需要完整比较的候选下标对 (i, j)（i < j）。

    对全图 / 中心80% / 中心64% / 5 个偏移窗口各扫一遍：全图用连续分块，
    裁剪姿态用交错分块。64bit 距离较小的对，必然至少有一个 16bit 块
    距离 ≤ 总距离的 1/4，相似度阈值在常见范围（≥ ~78）时不漏；桶内穷举，
    评分幂等，不保留全量 pair-set 以省内存。
    """
    n = len(fps)
    index = list(range(n))
    specs = (
        (lambda i: fps[i]["phash"], "contiguous", 1),
        (lambda i: fps[i]["phash_center"], "interleave", 1),
        (lambda i: fps[i]["phash_center_2"], "interleave", 1),
        (lambda i: fps[i]["phash_grid"], "interleave", 1),
    )
    for hash_fn, layout, radius in specs:
        tables = _build_tables(index, fps, hash_fn, layout)
        for band in range(_BANDS):
            table = tables[band]
            for bucket, members in table.items():
                if len(members) < 2:
                    continue
                neighbors = set()
                for bv in _probes(bucket, radius):
                    neighbors.update(table.get(bv, ()))
                members.sort()
                nbr = sorted(neighbors)
                for ia in members:
                    for jb in nbr:
                        if jb > ia:
                            yield ia, jb


# ---------------------------------------------------------------------------
# 保留建议
# ---------------------------------------------------------------------------
def _is_lossless(rec):
    ext = rec.get("ext", "").lower()
    if ext in (".png", ".bmp", ".tiff", ".tif"):
        return True
    fmt = (rec.get("format") or "").upper()
    return fmt in ("PNG", "BMP", "TIFF")


def choose_keeper(member_items):
    """member_items: [(rec, fp), ...]，返回推荐保留项下标。

    选「最清晰/尺寸最大」的一张，按下面优先级（任一档拉开差距即定）：
      1) 分辨率（像素总数）最大——信息总量最重要；
      2) 同分辨率时，无损/未重压缩格式（PNG/BMP/TIFF）优先于 JPEG 这类
         有压缩损失的格式；
      3) 同档格式时，综合 0.65*清晰度（拉普拉斯方差，组内归一化）
         + 0.35*编码信息量（每像素字节数，组内归一化）取胜。
    """
    if len(member_items) == 1:
        return 0
    pixels = [r["width"] * r["height"] for r, _ in member_items]
    top = max(pixels)
    cand = [i for i, p in enumerate(pixels) if p == top]
    if len(cand) == 1:
        return cand[0]

    # 同分辨率：无损格式优先
    lossless = [i for i in cand if _is_lossless(member_items[i][0])]
    if lossless and len(lossless) < len(cand):
        cand = lossless
    if len(cand) == 1:
        return cand[0]

    def metrics(i):
        rec, fp = member_items[i]
        return (fp.get("sharpness", 0.0),
                rec.get("size_bytes", 0) / max(rec["width"] * rec["height"], 1))

    sharps = [metrics(i)[0] for i in cand]
    bpps = [metrics(i)[1] for i in cand]

    def norm(v, arr):
        lo, hi = min(arr), max(arr)
        return 0.5 if hi - lo < 1e-9 else (v - lo) / (hi - lo)

    best_i, best_score = cand[0], -1.0
    for k, i in enumerate(cand):
        score = 0.65 * norm(sharps[k], sharps) + 0.35 * norm(bpps[k], bpps)
        if score > best_score:
            best_score, best_i = score, i
    return best_i


def _keeper_reason(rec, fp):
    bits = [f"分辨率最高 {rec['width']}×{rec['height']}", f"清晰度 {fp.get('sharpness', 0):.0f}"]
    if _is_lossless(rec):
        bits.insert(1, "无损格式")
    return "，".join(bits)


# ---------------------------------------------------------------------------
# 查重管理器
# ---------------------------------------------------------------------------
class DedupManager:
    def __init__(self, image_store):
        self.images = image_store
        self.jobs = JsonStore(config.DEDUP_JSON, {"jobs": []})
        self.fp_store = JsonStore(config.DEDUP_FP_JSON, {})
        self.executor = ThreadPoolExecutor(max_workers=config.DEDUP_WORKERS)
        self._scan_lock = threading.Lock()   # 同一时刻只允许一个全库扫描

    # ------------------------------------------------------------- 指纹
    def _fingerprint_one(self, rec, force=False, known=None):
        """返回 (image_id, fingerprint_or_None, error_or_None, from_cache)。
        不写缓存（扫描时批量落盘，避免逐张重写整个 JSON 的 O(n²) I/O）。
        known 为本次扫描开始时的全量缓存 dict，命中即直接返回。"""
        image_id = rec["id"]
        if not force and known is not None and image_id in known:
            return image_id, known[image_id], None, True
        try:
            img = self._load_for_hash(rec)
            try:
                fp = perceptual.extract_fingerprint(img)
            finally:
                img.close()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            return image_id, None, f"无法解析图像：{exc}", False
        except Exception as exc:  # noqa: BLE001 单图失败不拖垮整扫
            return image_id, None, f"指纹提取失败：{exc}", False
        return image_id, fp, None, False

    def _save_fingerprints(self, new_fps):
        """把一批新指纹合并落盘（一次原子写）。"""
        if not new_fps:
            return

        def _upd(doc):
            doc = dict(doc)
            doc.update(new_fps)
            return doc
        self.fp_store.update(_upd)

    def _load_for_hash(self, rec):
        """读图并统一成小尺寸 RGB。

        优先缩略图（小、快、内存恒定），但缩略图可能是原图损坏前生成的，
        因此优先验证原图能否完整解码：能解码就以原图为准；原图损坏则记失败，
        绝不拿旧缩略图冒充（否则会与历史状态不一致）。
        """
        orig = self.images.file_path(rec["id"])
        path = orig if (orig and os.path.exists(orig)) else self.images.thumbnail_path(rec["id"])
        if not path or not os.path.exists(path):
            raise OSError("图像文件缺失")
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img.load()  # 强制完整解码：截断/损坏文件在此抛异常
        if img.mode in ("RGBA", "LA", "PA"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # 原图可能很大，压到工作分辨率（缩略图本就 ≤ THUMB_DIM）
        if max(img.size) > config.DEDUP_THUMB_DIM:
            img.thumbnail((config.DEDUP_THUMB_DIM, config.DEDUP_THUMB_DIM), Image.Resampling.BILINEAR)
        return img

    def prune_fingerprints(self):
        """删除已不存在的图像的指纹（图片被删除时调用）。"""
        alive = {r["id"] for r in self.images.list_records()}

        def _upd(doc):
            return {k: v for k, v in doc.items() if k in alive}
        removed = len(self.fp_store.read()) - len(alive)
        if removed > 0:
            self.fp_store.update(_upd)

    def forget(self, image_id):
        """删除单张图的指纹缓存。"""
        def _upd(doc):
            if image_id in doc:
                doc = dict(doc)
                doc.pop(image_id, None)
            return doc
        self.fp_store.update(_upd)

    # ------------------------------------------------------------- 任务
    def _read_jobs(self):
        return self.jobs.read().get("jobs", [])

    def get_job(self, job_id):
        return next((j for j in self._read_jobs() if j["id"] == job_id), None)

    def _update_job(self, job_id, fn):
        def _upd(doc):
            doc = dict(doc)
            jobs = []
            for j in doc.get("jobs", []):
                j = dict(j)
                if j["id"] == job_id:
                    j = fn(j) or j
                jobs.append(j)
            doc["jobs"] = jobs
            return doc
        self.jobs.update(_upd)

    def start_scan(self, threshold, force_refresh=False):
        """启动一次全库扫描。已有扫描在跑时抛 RuntimeError（前端提示先等待/取消）。"""
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("已有扫描任务正在运行")
        job = {
            "id": uuid.uuid4().hex,
            "status": "queued",
            "threshold": threshold,
            "force_refresh": bool(force_refresh),
            "total": 0, "done": 0, "errors": 0,
            "stage": "queued",
            "created_at": now_iso(), "finished_at": None,
            "summary": None,
        }

        def _upd(doc):
            doc = dict(doc)
            doc["jobs"] = [job] + doc.get("jobs", [])[:config.DEDUP_MAX_JOBS - 1]
            return doc
        self.jobs.update(_upd)
        self.executor.submit(self._run_scan_safe, job["id"])
        return job

    def cancel(self, job_id):
        def _upd(j):
            if j.get("status") in ("queued", "running"):
                j["status"] = "cancelled"
                j["stage"] = "已取消"
                j["finished_at"] = now_iso()
            return j
        self._update_job(job_id, _upd)
        return self.get_job(job_id)

    def _is_cancelled(self, job_id):
        j = self.get_job(job_id)
        return not j or j.get("status") == "cancelled"

    # ------------------------------------------------------------- 扫描
    def _run_scan_safe(self, job_id):
        """在持锁状态下执行扫描，任何退出路径都释放锁。"""
        try:
            self._run_scan(job_id)
        except Exception as exc:  # noqa: BLE001 兜底：标记任务失败，不拖垮线程
            self._update_job(job_id, lambda j: j.update({
                "status": "partial", "stage": f"扫描异常：{exc}",
                "finished_at": now_iso()}) or j)
        finally:
            try:
                self._scan_lock.release()
            except RuntimeError:
                pass

    def _run_scan(self, job_id):
        records = self.images.list_records()
        records = list(reversed(records))  # 按上传时间正序处理，稳定
        total = len(records)
        self._update_job(job_id, lambda j: j.update(
            {"status": "running", "stage": "提取指纹", "total": total}) or j)

        fingerprints = {}
        new_fps = {}
        errors = []
        done = 0
        force = self.get_job(job_id).get("force_refresh")
        known = self.fp_store.read()  # 扫描开始时读一次缓存，避免逐张解析 JSON

        def _task(rec):
            return self._fingerprint_one(rec, force=force, known=known)

        # 指纹提取走线程池，边完成边更新进度；新指纹攒一批后统一落盘
        SAVE_EVERY = 25
        for image_id, fp, err, from_cache in self.executor.map(_task, records):
            if self._is_cancelled(job_id):
                self._save_fingerprints(new_fps)
                return
            if err:
                errors.append({"id": image_id, "error": err})
            else:
                fingerprints[image_id] = fp
                if not from_cache:
                    new_fps[image_id] = fp
            done += 1
            if done % SAVE_EVERY == 0:
                self._save_fingerprints(new_fps)
                new_fps = {}
            if done % 10 == 0 or done == total:
                self._update_job(job_id, lambda j, d=done, e=len(errors):
                                 j.update({"done": d, "errors": e}) or j)
        self._save_fingerprints(new_fps)

        if self._is_cancelled(job_id):
            return

        self._update_job(job_id, lambda j: j.update({"stage": "比对聚类"}) or j)
        threshold = self.get_job(job_id)["threshold"]
        groups, compared = self._cluster(records, fingerprints, threshold, job_id)
        if groups is None:
            return  # 被取消

        self._update_job(job_id, lambda j: j.update({"stage": "生成报告"}) or j)
        summary, group_docs = self._build_report(records, fingerprints, groups, threshold, compared)

        result_path = os.path.join(config.DEDUP_RESULTS_DIR, f"{job_id}.json")
        atomic_write_json(result_path, {"groups": group_docs, "errors": errors})

        def _finish(j):
            if j.get("status") != "cancelled":
                j["status"] = "done"
                j["stage"] = "完成"
                j["finished_at"] = now_iso()
                j["errors"] = len(errors)
                j["summary"] = summary
            return j
        self._update_job(job_id, _finish)

    def _cluster(self, records, fingerprints, threshold, job_id):
        """LSH 候选 -> 完整评分 -> 并查集聚类。

        返回 (groups, compared)；groups 是 {root: set(image_id)}。
        """
        ids = [r["id"] for r in records if r["id"] in fingerprints]
        fps = [fingerprints[i] for i in ids]
        pos = {iid: k for k, iid in enumerate(ids)}
        dsu = DSU(len(ids))
        pair_scores = {}      # (i,j) -> score，保留每条正向边的分数
        compared = 0

        for ia, ib in iter_candidate_pairs(fps):
            compared += 1
            if compared & 4095 == 0 and self._is_cancelled(job_id):
                return None, compared
            sim = perceptual.pair_similarity(fps[ia], fps[ib])
            if not perceptual.passes_threshold(sim, threshold):
                continue
            key = (ia, ib)
            old = pair_scores.get(key)
            if old is None or sim["score"] > old:
                pair_scores[key] = sim["score"]
            dsu.union(ia, ib)

        clusters = {}
        for k, iid in enumerate(ids):
            clusters.setdefault(dsu.find(k), set()).add(iid)
        groups = {root: members for root, members in clusters.items() if len(members) >= 2}
        return groups, compared

    def _build_report(self, records, fingerprints, groups, threshold, compared):
        """生成前端用的分组视图与汇总。"""
        rec_by_id = {r["id"]: r for r in records}
        group_docs = []
        total_waste = 0
        total_dup_files = 0

        for members in groups.values():
            pairs = []
            for iid in sorted(members):
                rec = rec_by_id.get(iid)
                fp = fingerprints.get(iid)
                if rec and fp:
                    pairs.append((rec, fp))
            if len(pairs) < 2:
                continue
            keeper_idx = choose_keeper(pairs)
            items = [{
                "id": rec["id"],
                "filename": rec.get("filename", ""),
                "width": rec["width"], "height": rec["height"],
                "size_bytes": rec.get("size_bytes", 0),
                "sharpness": fp.get("sharpness", 0.0),
                "thumbnail_url": f"/api/images/{rec['id']}/thumbnail",
                "file_url": f"/api/images/{rec['id']}/file",
                "created_at": rec.get("created_at", ""),
            } for rec, fp in pairs]
            keeper_id = items[keeper_idx]["id"]
            krec, kfp = pairs[keeper_idx]

            # 每个成员相对保留项的相似度，用于排序展示
            rel = {}
            for it in items:
                if it["id"] == keeper_id:
                    rel[it["id"]] = 100.0
                    continue
                a, b = fingerprints[keeper_id], fingerprints[it["id"]]
                rel[it["id"]] = perceptual.pair_similarity(a, b)["score"]
            items.sort(key=lambda it: (it["id"] != keeper_id, -rel[it["id"]]))

            group_min = min(rel.values()) if len(rel) > 1 else 100.0
            waste = sum(it["size_bytes"] for it in items if it["id"] != keeper_id)
            total_waste += waste
            total_dup_files += len(items) - 1

            for it in items:
                it["similarity"] = rel[it["id"]]
                it["is_keeper"] = it["id"] == keeper_id

            group_docs.append({
                "id": keeper_id + "_" + uuid.uuid4().hex[:6],
                "keeper_id": keeper_id,
                "keeper_reason": _keeper_reason(krec, kfp),
                "min_similarity": round(group_min, 1),
                "count": len(items),
                "waste_bytes": waste,
                "members": items,
            })

        # 组排序：副本数多、可回收空间大的在前
        group_docs.sort(key=lambda g: (g["count"], g["waste_bytes"]), reverse=True)
        summary = {
            "threshold": threshold,
            "image_count": len(records),
            "fingerprinted": len(fingerprints),
            "group_count": len(group_docs),
            "duplicate_files": total_dup_files,
            "waste_bytes": total_waste,
            "pairs_compared": compared,
        }
        return summary, group_docs

    # ------------------------------------------------------------- 结果读取
    def result_path(self, job_id):
        return os.path.join(config.DEDUP_RESULTS_DIR, f"{job_id}.json")

    def load_result(self, job_id):
        """读取分组结果；顺带剔除已被删除的图片、清理空组。"""
        path = self.result_path(job_id)
        data = read_json(path, None)
        if data is None:
            return None
        alive = {r["id"] for r in self.images.list_records()}
        groups = []
        for g in data.get("groups", []):
            members = [m for m in g["members"] if m["id"] in alive]
            if len(members) < 2:
                continue
            keeper_alive = g.get("keeper_id") in alive
            if not keeper_alive:
                # 原推荐项已删：按现有成员重新挑
                keeper = max(members, key=lambda m: (m["width"] * m["height"], m["size_bytes"], m["sharpness"]))
                g = dict(g)
                g["keeper_id"] = keeper["id"]
                g["keeper_reason"] = f"分辨率最高 {keeper['width']}×{keeper['height']}"
                for m in members:
                    m["is_keeper"] = m["id"] == keeper["id"]
            else:
                for m in members:
                    m["is_keeper"] = m["id"] == g["keeper_id"]
            g = dict(g)
            g["members"] = members
            g["count"] = len(members)
            g["waste_bytes"] = sum(m["size_bytes"] for m in members if m["id"] != g["keeper_id"])
            g["min_similarity"] = min(m.get("similarity", 0) for m in members)
            groups.append(g)
        return {
            "groups": groups,
            "errors": data.get("errors", []),
            "groups_count": len(groups),
        }
