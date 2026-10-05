"""图库查重任务：指纹缓存、后台扫描调度、进度与取消。

设计要点（对应「图库规模大、图大小和格式不一也要能稳定跑完」）：

- 指纹缓存 dedup.json：指纹只依赖图像内容（id 即内容哈希）与算法版本，
  扫描时按 id 直接复用，新图只需补算；算指纹走已生成的缩略图（220px），
  无论原图多大都不会爆内存。
- 扫描在后台线程运行，阶段与逐图进度原子写入 dedup_jobs.json，前端轮询；
  取消是协作式标志位，在每个检查点退出。
- 单张图损坏/无法解码不影响整体：记为 skipped 继续跑。
- 粗筛流式进行（见 algorithms.dedup.stream_candidates），精验候选对设有
  上限 DEDUP_MAX_CANDIDATES，极端图库也能收敛结束（status=partial）。
"""
import os
import threading
import time
import uuid

from PIL import Image, UnidentifiedImageError

from . import config
from .algorithms import dedup as algo
from .storage import JsonStore, now_iso

FP_VERSION = 3  # 指纹算法版本，升级后旧缓存自动失效重算（v3: 多视口哈希）


class FingerprintStore:
    """查重指纹缓存：{image_id: {v, ph, dh, color, sharpness}}。"""

    def __init__(self):
        self.store = JsonStore(config.DEDUP_JSON, {})

    def get_all(self):
        doc = self.store.read()
        return {k: v for k, v in doc.items() if v.get("v") == FP_VERSION}

    def put_many(self, items):
        if not items:
            return

        def _upd(doc):
            doc = dict(doc)
            doc.update(items)
            return doc
        self.store.update(_upd)


class DedupManager:
    def __init__(self, image_store):
        self.images = image_store
        self.fps = FingerprintStore()
        self.jobs = JsonStore(config.DEDUP_JOBS_JSON, {"jobs": []})
        self._cancel = set()
        self._running = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 任务
    def enqueue(self, threshold=None, image_ids=None):
        threshold = self._clamp_threshold(threshold)
        job = {
            "id": uuid.uuid4().hex,
            "status": "queued",
            "threshold": threshold,
            "scope_ids": list(image_ids) if image_ids else None,
            "phase": "queued",
            "total": 0, "done": 0,
            "phase_total": 0, "phase_done": 0,
            "created_at": now_iso(),
            "started_at": None, "finished_at": None,
            "image_count": 0, "group_count": 0,
            "duplicate_count": 0, "waste_bytes": 0,
            "candidate_pairs": 0, "compared_pairs": 0, "verified_pairs": 0,
            "skipped": [],
            "warning": None,
            "groups": None,
            "duration_ms": None,
        }

        def _upd(doc):
            doc = dict(doc)
            doc["jobs"] = [job] + doc.get("jobs", [])[:config.DEDUP_MAX_JOBS - 1]
            return doc
        self.jobs.update(_upd)
        t = threading.Thread(target=self._run, args=(job["id"],), daemon=True)
        t.start()
        return job

    def get_job(self, job_id):
        for j in self.jobs.read().get("jobs", []):
            if j["id"] == job_id:
                return j
        return None

    def latest_job(self):
        jobs = self.jobs.read().get("jobs", [])
        return jobs[0] if jobs else None

    def cancel(self, job_id):
        with self._lock:
            self._cancel.add(job_id)
        return self.get_job(job_id)

    @staticmethod
    def _clamp_threshold(t):
        try:
            t = float(t)
        except (TypeError, ValueError):
            t = config.DEDUP_DEFAULT_THRESHOLD
        return max(config.DEDUP_MIN_THRESHOLD,
                   min(config.DEDUP_MAX_THRESHOLD, int(round(t))))

    def _is_cancelled(self, job_id):
        with self._lock:
            return job_id in self._cancel

    def _update(self, job_id, fn):
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

    # ------------------------------------------------------------------ 扫描
    def _run(self, job_id):
        with self._lock:
            if job_id in self._running:
                return
            self._running.add(job_id)
        t0 = time.time()
        try:
            self._scan(job_id)
        except Exception as exc:  # noqa: BLE001  扫描整体兜底，任务不能无声消失
            self._update(job_id, lambda j: (
                j.update({"status": "error", "warning": f"扫描异常终止: {exc}",
                          "finished_at": now_iso()}) or j))
        finally:
            with self._lock:
                self._running.discard(job_id)
                self._cancel.discard(job_id)

    def _scan(self, job_id):
        t0 = time.time()
        job = self.get_job(job_id)
        threshold = job["threshold"] / 100.0

        self._update(job_id, lambda j: j.update({
            "status": "running", "phase": "fingerprint",
            "started_at": now_iso()}) or j)

        # 1) 确定范围与已有指纹
        records = self.images.list_records()
        records.sort(key=lambda r: r.get("created_at", ""))  # 稳定顺序，便于缓存索引
        scope = set(job.get("scope_ids") or [])
        if scope:
            records = [r for r in records if r["id"] in scope]
        by_id = {r["id"]: r for r in records}

        cached = self.fps.get_all()
        todo = [r for r in records if r["id"] not in cached or not _valid_fp(cached[r["id"]])]

        self._update(job_id, lambda j: j.update({
            "total": len(records), "done": len(records) - len(todo),
            "phase_total": len(records), "phase_done": len(records) - len(todo)}) or j)

        # 2) 补算指纹（失败的图跳过，不拖垮整库扫描）
        skipped = []
        fresh = {}
        for k, rec in enumerate(todo):
            if self._is_cancelled(job_id):
                self._finish_cancelled(job_id)
                return
            try:
                img = self._load_for_fp(rec)
                try:
                    fp = algo.fingerprint(img)
                finally:
                    img.close()
                fresh[rec["id"]] = {"v": FP_VERSION, **fp}
            except Exception as exc:  # noqa: BLE001
                skipped.append({"id": rec["id"], "filename": rec.get("filename"),
                                "reason": str(exc)[:120]})
            done = len(records) - len(todo) + k + 1
            if k % 3 == 0 or k == len(todo) - 1:
                self._update(job_id, lambda j, d=done: j.update({"done": d, "phase_done": d}) or j)
        self.fps.put_many(fresh)

        fps_all = cached
        fps_all.update(fresh)
        items = [(r["id"], fps_all[r["id"]]) for r in records if r["id"] in fps_all]
        id_to_rec = {iid: {**by_id[iid], "sharpness": fps_all[iid].get("sharpness", 0.0)}
                     for iid, _ in items}

        # 3) LSH 流式粗筛：只保留过结构闸口的对（达到候选上限提前结束）
        accepted = {}

        def _on_pair(i, j):
            a, b = items[i][1], items[j][1]
            accepted[(i, j)] = algo.struct_similarity(a, b)
        self._update(job_id, lambda j: j.update({
            "phase": "scan", "phase_total": 0, "phase_done": 0,
            "skipped": skipped}) or j)

        compared, cand, limit_hit = algo.stream_candidates(
            items, gate=config.DEDUP_HASH_GATE,
            on_pair=_on_pair, should_continue=lambda: not self._is_cancelled(job_id),
            limit=config.DEDUP_MAX_CANDIDATES)
        if self._is_cancelled(job_id):
            self._finish_cancelled(job_id)
            return

        # 4) 边缘精验 + 综合打分
        pair_scores = {}
        edge_cache = algo._LRU(capacity=256)
        loaded = {}
        cand_list = sorted(accepted.items(), key=lambda kv: kv[1], reverse=True)
        total_c = len(cand_list)

        self._update(job_id, lambda j: j.update({
            "phase": "verify", "phase_total": total_c, "phase_done": 0,
            "candidate_pairs": total_c, "compared_pairs": compared}) or j)

        for k, ((i, j), _struct) in enumerate(cand_list):
            if k % 25 == 0:
                if self._is_cancelled(job_id):
                    self._finish_cancelled(job_id)
                    return
                self._update(job_id, lambda j, d=k: j.update({"phase_done": d}) or j)
            id_a, id_b = items[i][0], items[j][0]
            try:
                img_a = self._get_image(id_a, loaded)
                img_b = self._get_image(id_b, loaded)
                edge = algo.edge_correlation(img_a, img_b, cache=edge_cache,
                                             id_a=id_a, id_b=id_b)
            except (UnidentifiedImageError, OSError) as exc:
                skipped.append({"id": id_b, "filename": by_id.get(id_b, {}).get("filename", ""),
                                "reason": f"与 {id_a[:8]}.. 精验时解码失败: {str(exc)[:80]}"})
                continue
            sc = algo.score_pair(items[i][1], items[j][1], edge)
            if sc[0] / 100.0 >= threshold:
                pair_scores[(i, j)] = sc
        for img in loaded.values():
            try:
                img.close()
            except Exception:  # noqa: BLE001
                pass

        # 5) 并查集分组
        groups = algo.build_groups([iid for iid, _ in items], pair_scores, id_to_rec)
        waste = sum(g["waste_bytes"] for g in groups)
        dup_count = sum(len(g["members"]) - 1 for g in groups)
        warning = None
        if limit_hit:
            warning = (f"候选对超过上限 {config.DEDUP_MAX_CANDIDATES}，"
                       "仅精验了最相似的部分对；建议提高阈值或分批扫描。")

        groups_view = self._groups_view(groups, id_to_rec)

        def _finish(j):
            j.update({
                "status": "partial" if (warning or skipped) else "done",
                "phase": "done", "done": j["total"], "phase_done": j.get("phase_total", 0),
                "finished_at": now_iso(),
                "image_count": len(items), "group_count": len(groups),
                "duplicate_count": dup_count, "waste_bytes": waste,
                "verified_pairs": total_c,
                "warning": warning, "skipped": skipped,
                "groups": groups_view,
                "duration_ms": int((time.time() - t0) * 1000),
            })
            return j
        self._update(job_id, _finish)

    # ------------------------------------------------------------------ 辅助
    def _load_for_fp(self, rec):
        """优先用缩略图算指纹（小且快）；缩略图缺失回退原图降采样。

        两种路径都立即解码进内存并关闭文件句柄，避免大图库扫描时
        累积打开的文件描述符。
        """
        tp = self.images.thumbnail_path(rec["id"])
        if os.path.exists(tp):
            with Image.open(tp) as im:
                im.load()
                return im.copy()
        with Image.open(self.images.file_path(rec["id"])) as im:
            im.load()
            im = im.copy()
        im.thumbnail((config.THUMB_DIM, config.THUMB_DIM), Image.Resampling.BILINEAR)
        return im

    def _get_image(self, image_id, loaded):
        img = loaded.get(image_id)
        if img is not None:
            return img
        rec = self.images.get(image_id)
        tp = self.images.thumbnail_path(image_id)
        path = tp if (rec and os.path.exists(tp)) else self.images.file_path(image_id)
        with Image.open(path) as opened:
            opened.load()  # 立即解码进内存，随后关闭文件句柄
            img = opened.copy()
        loaded[image_id] = img
        if len(loaded) > 64:  # 控制常驻内存的图像数
            old = next(iter(loaded))
            try:
                loaded[old].close()
            except Exception:  # noqa: BLE001
                pass
            loaded.pop(old, None)
        return img

    def _groups_view(self, groups, records):
        """补充前端需要的成员信息（缩略图/文件名/尺寸），edges 只保留前若干条。"""
        out = []
        for gi, g in enumerate(groups):
            members = []
            for mid in g["members"]:
                r = records[mid]
                members.append({
                    "id": mid,
                    "filename": r.get("filename", ""),
                    "width": r.get("width", 0), "height": r.get("height", 0),
                    "size_bytes": r.get("size_bytes", 0),
                    "sharpness": r.get("sharpness", 0.0),
                    "thumbnail_url": f"/api/images/{mid}/thumbnail",
                    "file_url": f"/api/images/{mid}/file",
                    "is_keeper": mid == g["keeper_id"],
                })
            members.sort(key=lambda m: (not m["is_keeper"], -m["width"] * m["height"]))
            out.append({
                "group_id": gi,
                "kind": g["kind"],
                "avg_score": g["avg_score"],
                "max_score": g["max_score"],
                "waste_bytes": g["waste_bytes"],
                "keeper_id": g["keeper_id"],
                "members": members,
                "edges": g["edges"][:24],
            })
        return out

    def _finish_cancelled(self, job_id):
        def _upd(j):
            j.update({
                "status": "cancelled", "phase": "cancelled",
                "finished_at": now_iso(),
            })
            started = _parse_ts(j.get("started_at"))
            j["duration_ms"] = int((time.time() - started) * 1000) if started else None
        self._update(job_id, _upd)


def _parse_ts(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _valid_fp(fp):
    return (isinstance(fp.get("ph"), list) and len(fp["ph"]) == 3
            and isinstance(fp.get("dh"), list) and len(fp["dh"]) == 3
            and bool(fp.get("color")))
