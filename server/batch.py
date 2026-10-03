"""批处理队列与调度。

难点之一「批处理调度」：线程池（CPU 密集，限制 worker 数防内存爆炸）+ 任务队列。

- 每个任务 = 一条流水线 + 一组图像；逐张处理，每完成一张原子更新一次进度到 queue.json，
  前端可实时看到进度，也可随时取消（协作式标志位）。
- 每张图都走结果缓存（process_image），重复组合不重复算。
- 每张图完成后同时写入处理历史，历史页与批量页共享同一份结果。
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from . import config
from . import pipeline as pipeline_engine
from .algorithms import util
from .cache import make_key
from .storage import JsonStore, now_iso


def load_working_image(image_store, image_id):
    """载入图像并降采样到工作分辨率（大图内存管理入口）。"""
    rec = image_store.get(image_id)
    if not rec:
        raise FileNotFoundError(f"图像不存在：{image_id}")
    path = image_store.file_path(image_id)
    img = Image.open(path)
    img = util.ensure_rgb(img)
    return img, util.downscale_to_max(img, config.MAX_DIM), rec


def process_image(image_store, cache, history, image_id, nodes,
                  pipeline_id=None, pipeline_name=None):
    """对单张图执行流水线（带缓存），并记录历史。

    返回 {result_id, cache_hit, error, exec_result, history_id}。
    """
    t0 = time.time()
    try:
        _, work, rec = load_working_image(image_store, image_id)
    except Exception as exc:  # noqa: BLE001
        return {"result_id": None, "cache_hit": False,
                "error": f"载入图像失败: {exc}", "exec_result": None, "history_id": None}

    key = make_key(rec["hash"], pipeline_engine.canonical_key(nodes))
    cached = cache.get(key)
    if cached:
        entry = _record_history(history, image_store, image_id, nodes, pipeline_id,
                                pipeline_name, cached, True, None, None, t0)
        return {"result_id": cached, "cache_hit": True, "error": None,
                "exec_result": None, "history_id": entry["id"]}

    exec_result = pipeline_engine.execute(work, nodes)
    if exec_result.get("error"):
        entry = _record_history(history, image_store, image_id, nodes, pipeline_id,
                                pipeline_name, None, False, exec_result["error"],
                                exec_result.get("node_results"), t0)
        return {"result_id": None, "cache_hit": False, "error": exec_result["error"],
                "exec_result": exec_result, "history_id": entry["id"]}

    result_id = cache.put(key, exec_result["image"], exec_result["meta"])
    entry = _record_history(history, image_store, image_id, nodes, pipeline_id,
                            pipeline_name, result_id, False, None,
                            exec_result.get("node_results"), t0)
    return {"result_id": result_id, "cache_hit": False, "error": None,
            "exec_result": exec_result, "history_id": entry["id"]}


def _record_history(history, image_store, image_id, nodes, pipeline_id, pipeline_name,
                    result_id, cache_hit, error, node_results, t0):
    rec = image_store.get(image_id)
    return history.add({
        "image_id": image_id,
        "image_name": rec.get("filename", "") if rec else "",
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "pipeline_snapshot": {"nodes": nodes},
        "node_count": len(nodes),
        "result_id": result_id,
        "cache_hit": cache_hit,
        "status": "error" if error else "ok",
        "error": error,
        "node_results": node_results,
        "duration_ms": int((time.time() - t0) * 1000),
    })


class BatchManager:
    def __init__(self, image_store, cache, history):
        self.images = image_store
        self.cache = cache
        self.history = history
        self.queue = JsonStore(config.QUEUE_JSON, {"jobs": []})
        self.executor = ThreadPoolExecutor(max_workers=config.MAX_BATCH_WORKERS)
        self._started = set()

    # ------------------------------------------------------------------ 队列
    def _read_jobs(self):
        return self.queue.read().get("jobs", [])

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
        return self.queue.update(_upd)

    def enqueue(self, nodes, image_ids, pipeline_id=None, pipeline_name=None):
        job = {
            "id": uuid.uuid4().hex,
            "pipeline_id": pipeline_id,
            "pipeline_name": pipeline_name,
            "pipeline_snapshot": {"nodes": nodes},
            "image_ids": list(image_ids),
            "total": len(image_ids),
            "done": 0,
            "status": "queued",
            "created_at": now_iso(),
            "finished_at": None,
            "results": {},
        }

        def _upd(doc):
            doc = dict(doc)
            doc["jobs"] = [job] + doc.get("jobs", [])[:99]
            return doc
        self.queue.update(_upd)
        self.executor.submit(self._run_job, job["id"])
        return job

    def _run_job(self, job_id):
        self._update_job(job_id, lambda j: j.update({"status": "running"}) or j)
        jobs = self._read_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job:
            return
        nodes = job["pipeline_snapshot"]["nodes"]

        for image_id in job["image_ids"]:
            current = next((j for j in self._read_jobs() if j["id"] == job_id), None)
            if not current or current.get("status") == "cancelled":
                break
            res = process_image(self.images, self.cache, self.history, image_id, nodes,
                                pipeline_id=job.get("pipeline_id"),
                                pipeline_name=job.get("pipeline_name"))

            def _done(j):
                j["results"][image_id] = {
                    "result_id": res["result_id"], "cache_hit": res["cache_hit"],
                    "status": "error" if res["error"] else "ok", "error": res["error"],
                }
                j["done"] = len(j["results"])
                return j
            self._update_job(job_id, _done)

        # 收尾：若非取消则标记完成
        def _finish(j):
            if j.get("status") != "cancelled":
                errors = sum(1 for r in j["results"].values() if r["status"] == "error")
                j["status"] = "done" if errors == 0 else "partial"
                j["finished_at"] = now_iso()
            return j
        self._update_job(job_id, _finish)

    def cancel(self, job_id):
        def _upd(j):
            if j.get("status") in ("queued", "running"):
                j["status"] = "cancelled"
                j["finished_at"] = now_iso()
            return j
        self._update_job(job_id, _upd)
        return self.get_job(job_id)

    def list_jobs(self):
        return self._read_jobs()

    def get_job(self, job_id):
        for j in self._read_jobs():
            if j["id"] == job_id:
                return j
        return None
