"""REST API：所有后端能力通过 JSON 接口暴露给前端。

这是各模块的粘合层：图像管理、流水线 CRUD、运行、特征/检测/分割/风格、
批处理、结果对比、预设、历史。单图运算接口统一走 _run_op（带缓存）。
"""
import io
import json

from flask import Blueprint, jsonify, request, send_file
from PIL import Image, ImageChops

from . import config, pipeline as pipeline_engine
from .algorithms import detection, features, segmentation, style, util
from .batch import BatchManager, process_image
from .cache import ResultCache, make_key
from .dedup import DedupManager
from .history import HistoryManager
from .image_store import ImageStore
from .nodes import CATEGORIES, get_public_nodes
from .storage import JsonStore, now_iso
from . import storage as storage_mod

# ---------------------------------------------------------------------------
# 单例（模块导入即创建，app.py 入口先 ensure_dirs）
# ---------------------------------------------------------------------------
config.ensure_dirs()
image_store = ImageStore()
cache = ResultCache()
history = HistoryManager()
batch = BatchManager(image_store, cache, history)
dedup = DedupManager(image_store)
presets_store = JsonStore(config.PRESETS_JSON, [])

bp = Blueprint("api", __name__, url_prefix="/api")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _image_view(rec):
    """给前端用的图像记录视图（附加缩略图/文件 URL）。"""
    return {
        "id": rec["id"], "filename": rec["filename"], "format": rec["format"],
        "width": rec["width"], "height": rec["height"],
        "size_bytes": rec["size_bytes"], "created_at": rec["created_at"],
        "tags": rec.get("tags", []), "note": rec.get("note", ""),
        "annotations": rec.get("annotations", []),
        "thumbnail_url": f"/api/images/{rec['id']}/thumbnail",
        "file_url": f"/api/images/{rec['id']}/file",
    }


def _load_full_image(image_id):
    rec = image_store.get(image_id)
    if not rec:
        return None, None
    from PIL import Image
    path = image_store.file_path(image_id)
    if not path:
        return None, None
    return rec, Image.open(path)


def _run_op(image_id, op_name, params, func):
    """单图运算通用流程：载入 -> 缓存查询 -> 执行 -> 缓存结果。"""
    rec, img = _load_full_image(image_id)
    if not rec:
        return None, ({"error": "图像不存在"}, 404)
    work = util.downscale_to_max(util.ensure_rgb(img), config.MAX_DIM)
    key = make_key(rec["hash"], op_name, json.dumps(params, sort_keys=True))

    cached = cache.get(key)
    if cached:
        entry = cache.get_entry(cached) or {}
        return {"result_id": cached, "cache_hit": True, **entry.get("meta", {})}, None

    result = func(work, params)
    image_out = result.get("image", work)
    meta = {k: v for k, v in result.items() if k != "image"}
    result_id = cache.put(key, image_out, meta)
    return {"result_id": result_id, "cache_hit": False, **meta}, None


def _result_view(entry):
    return {
        "result_id": entry.get("result_id"),
        "key": entry.get("key"),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "size_bytes": entry.get("size_bytes"),
        "created_at": entry.get("created_at"),
        "meta": entry.get("meta", {}),
        "file_url": f"/api/results/{entry.get('result_id')}/file",
    }


def _pipeline_view(p):
    return {
        "id": p["id"], "name": p["name"], "nodes": p["nodes"],
        "version": p.get("version", 1), "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------
@bp.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "images": len(image_store.list_records()),
        "results": len(cache.list_results()),
        "history": len(history.list()),
        "pipelines": len(pipelines_store.read()),
    })


@bp.get("/config")
def get_config():
    return jsonify({
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "max_dim": config.MAX_DIM,
        "preview_dim": config.PREVIEW_DIM,
        "categories": CATEGORIES,
        "batch_workers": config.MAX_BATCH_WORKERS,
        "dedup_default_threshold": config.DEDUP_DEFAULT_THRESHOLD,
        "dedup_min_threshold": config.DEDUP_MIN_THRESHOLD,
        "dedup_max_threshold": config.DEDUP_MAX_THRESHOLD,
    })


@bp.get("/nodes")
def get_nodes():
    return jsonify({"nodes": get_public_nodes(), "categories": CATEGORIES})


@bp.get("/styles")
def get_styles():
    return jsonify({"styles": style.list_styles()})


@bp.get("/reconcile")
def reconcile():
    issues = image_store.reconcile()
    return jsonify({"issues": issues, "ok": not issues["orphan_meta"] and not issues["orphan_files"]})


# ---------------------------------------------------------------------------
# 图像
# ---------------------------------------------------------------------------
@bp.get("/images")
def list_images():
    return jsonify({"images": [_image_view(r) for r in image_store.list_records()]})


@bp.post("/images")
def upload_images():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "未收到文件"}), 400
    saved, skipped = [], []
    for f in files:
        data = f.read()
        if len(data) > config.MAX_UPLOAD_BYTES:
            skipped.append({"filename": f.filename, "reason": "超过大小限制"})
            continue
        if not data:
            continue
        try:
            rec = image_store.save_upload(data, f.filename or "upload")
            saved.append(_image_view(rec))
        except Exception as exc:  # noqa: BLE001
            skipped.append({"filename": f.filename, "reason": str(exc)})
    return jsonify({"saved": saved, "skipped": skipped})


@bp.get("/images/<image_id>")
def get_image(image_id):
    rec = image_store.get(image_id)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


@bp.patch("/images/<image_id>")
def patch_image(image_id):
    data = request.get_json(silent=True) or {}
    rec = image_store.update_meta(image_id, data)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


@bp.delete("/images/<image_id>")
def delete_image(image_id):
    if not image_store.delete(image_id):
        return jsonify({"error": "not found"}), 404
    dedup.forget(image_id)
    return jsonify({"ok": True})


@bp.post("/images/bulk-delete")
def bulk_delete_images():
    """批量删除（查重页「清理所选」用）。返回成功/失败/缺失清单。"""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "缺少 ids"}), 400
    deleted, missing = [], []
    for image_id in ids:
        if image_store.delete(image_id):
            dedup.forget(image_id)
            deleted.append(image_id)
        else:
            missing.append(image_id)
    return jsonify({"ok": True, "deleted": deleted, "missing": missing})


@bp.get("/images/<image_id>/file")
def image_file(image_id):
    path = image_store.file_path(image_id)
    if not path:
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="image/png" if path.endswith(".png") else "image/jpeg")


@bp.get("/images/<image_id>/thumbnail")
def image_thumbnail(image_id):
    path = image_store.thumbnail_path(image_id)
    if not path or not __import__("os").path.exists(path):
        # 回退到全图（缩略图缺失时）
        path = image_store.file_path(image_id)
        if not path:
            return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="image/jpeg")


@bp.post("/images/<image_id>/annotations")
def add_annotation(image_id):
    data = request.get_json(silent=True) or {}
    rec = image_store.add_annotation(image_id, data.get("box"), data.get("label", ""),
                                     data.get("color", "#ff5252"))
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


@bp.delete("/images/<image_id>/annotations/<int:index>")
def delete_annotation(image_id, index):
    rec = image_store.delete_annotation(image_id, index)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


# ---------------------------------------------------------------------------
# 流水线
# ---------------------------------------------------------------------------
pipelines_store = JsonStore(config.PIPELINES_JSON, {})


def _next_version(p):
    return int(p.get("version", 1)) + 1


@bp.get("/pipelines")
def list_pipelines():
    items = sorted(pipelines_store.read().values(), key=lambda p: p.get("updated_at", ""), reverse=True)
    return jsonify({"pipelines": [_pipeline_view(p) for p in items]})


@bp.post("/pipelines")
def create_pipeline():
    data = request.get_json(silent=True) or {}
    pid = __import__("uuid").uuid4().hex
    rec = {
        "id": pid, "name": data.get("name", "未命名流水线"),
        "nodes": data.get("nodes", []),
        "version": 1, "versions": [],
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    errors = pipeline_engine.validate(rec["nodes"])
    rec["valid"] = not errors
    def _upd(doc):
        doc = dict(doc)
        doc[pid] = rec
        return doc
    pipelines_store.update(_upd)
    return jsonify({**_pipeline_view(rec), "valid": rec["valid"], "errors": errors})


@bp.get("/pipelines/<pid>")
def get_pipeline(pid):
    p = pipelines_store.read().get(pid)
    if not p:
        return jsonify({"error": "not found"}), 404
    return jsonify(_pipeline_view(p))


@bp.put("/pipelines/<pid>")
def update_pipeline(pid):
    data = request.get_json(silent=True) or {}
    errors = pipeline_engine.validate(data.get("nodes", []))

    def _upd(doc):
        doc = dict(doc)
        p = doc.get(pid)
        if not p:
            return doc
        p = dict(p)
        # 保留旧版本快照
        versions = list(p.get("versions", []))
        versions.insert(0, {"version": p.get("version", 1), "nodes": p.get("nodes", []),
                            "updated_at": p.get("updated_at")})
        p["nodes"] = data.get("nodes", p["nodes"])
        p["name"] = data.get("name", p["name"])
        p["version"] = _next_version(p)
        p["versions"] = versions[:config.PIPELINE_MAX_VERSIONS]
        p["updated_at"] = now_iso()
        p["valid"] = not errors
        doc[pid] = p
        return doc

    pipelines_store.update(_upd)
    p = pipelines_store.read().get(pid)
    if not p:
        return jsonify({"error": "not found"}), 404
    return jsonify({**_pipeline_view(p), "valid": not errors, "errors": errors})


@bp.delete("/pipelines/<pid>")
def delete_pipeline(pid):
    def _upd(doc):
        doc = dict(doc)
        doc.pop(pid, None)
        return doc
    pipelines_store.update(_upd)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 运行 / 结果
# ---------------------------------------------------------------------------
@bp.post("/run")
def run_pipeline():
    data = request.get_json(silent=True) or {}
    image_id = data.get("image_id")
    nodes = data.get("nodes")
    pipeline_id = data.get("pipeline_id")
    pipeline_name = data.get("pipeline_name")

    if not nodes and pipeline_id:
        p = pipelines_store.read().get(pipeline_id)
        if p:
            nodes = p.get("nodes", [])
            pipeline_name = p.get("name")
    if image_id is None or nodes is None:
        return jsonify({"error": "缺少 image_id 或 nodes"}), 400

    res = process_image(image_store, cache, history, image_id, nodes,
                        pipeline_id=pipeline_id, pipeline_name=pipeline_name)
    if res["error"]:
        return jsonify({"error": res["error"], "history_id": res["history_id"]}), 200
    entry = cache.get_entry(res["result_id"]) or {}
    return jsonify({
        "result_id": res["result_id"],
        "cache_hit": res["cache_hit"],
        "history_id": res["history_id"],
        "file_url": f"/api/results/{res['result_id']}/file",
        "meta": entry.get("meta", {}),
        "node_results": (res["exec_result"] or {}).get("node_results", []),
    })


@bp.get("/results")
def list_results():
    return jsonify({"results": [_result_view(e) for e in cache.list_results()]})


@bp.get("/results/<result_id>")
def get_result(result_id):
    entry = cache.get_entry(result_id)
    if not entry:
        return jsonify({"error": "not found"}), 404
    return jsonify(_result_view(entry))


@bp.get("/results/<result_id>/file")
def result_file(result_id):
    path = cache.result_path(result_id)
    if not path:
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="image/png")


# ---------------------------------------------------------------------------
# 特征 / 检测 / 分割 / 风格
# ---------------------------------------------------------------------------
@bp.post("/features")
def run_features():
    data = request.get_json(silent=True) or {}
    params = {"method": data.get("method", "sift"),
              "max_points": data.get("max_points", 120)}
    res, err = _run_op(data.get("image_id"), "features", params, features.extract_keypoints)
    if err:
        return err[0], err[1]
    return jsonify(res)


@bp.post("/features/match")
def run_match():
    data = request.get_json(silent=True) or {}
    a = data.get("image_id_a")
    b = data.get("image_id_b")
    params = {"method": data.get("method", "sift"),
              "max_points": data.get("max_points", 120),
              "max_matches": data.get("max_matches", 40)}
    rec_a, img_a = _load_full_image(a)
    rec_b, img_b = _load_full_image(b)
    if not rec_a or not rec_b:
        return jsonify({"error": "图像不存在"}), 404
    wa = util.downscale_to_max(util.ensure_rgb(img_a), config.MAX_DIM)
    wb = util.downscale_to_max(util.ensure_rgb(img_b), config.MAX_DIM)
    key = make_key(rec_a["hash"], rec_b["hash"], "match", json.dumps(params, sort_keys=True))
    cached = cache.get(key)
    if cached:
        entry = cache.get_entry(cached) or {}
        return jsonify({"result_id": cached, "cache_hit": True, **entry.get("meta", {})})
    result = features.match(wa, wb, params)
    image_out = result.get("image", wa)
    meta = {k: v for k, v in result.items() if k != "image"}
    result_id = cache.put(key, image_out, meta)
    return jsonify({"result_id": result_id, "cache_hit": False, **meta})


@bp.post("/detect")
def run_detect():
    data = request.get_json(silent=True) or {}
    params = {"method": data.get("method", "saliency"),
              "max_boxes": data.get("max_boxes", 20),
              "threshold": data.get("threshold") if data.get("auto", True) is False else None,
              "min_size": data.get("min_size", 0.02)}
    res, err = _run_op(data.get("image_id"), "detect", params, detection.detect)
    if err:
        return err[0], err[1]
    return jsonify(res)


@bp.post("/segment")
def run_segment():
    data = request.get_json(silent=True) or {}
    params = {"method": data.get("method", "threshold"),
              "value": data.get("value"), "block": data.get("block", 15),
              "colors": data.get("colors", 6), "alpha": data.get("alpha", 0.45)}
    res, err = _run_op(data.get("image_id"), "segment", params, segmentation.segment)
    if err:
        return err[0], err[1]
    return jsonify(res)


@bp.post("/style")
def run_style():
    data = request.get_json(silent=True) or {}
    params = {"style": data.get("style", "oil"), "strength": data.get("strength", 100)}
    res, err = _run_op(data.get("image_id"), "style", params, style.apply)
    if err:
        return err[0], err[1]
    return jsonify(res)


# ---------------------------------------------------------------------------
# 对比 / 差异
# ---------------------------------------------------------------------------
@bp.post("/compare/diff")
def compare_diff():
    data = request.get_json(silent=True) or {}
    rec, img_a = _load_full_image(data.get("image_id"))
    result_id = data.get("result_id")
    img_b = cache.result_image(result_id)
    if not rec or img_b is None:
        return jsonify({"error": "图像或结果不存在"}), 404

    # 对齐到同一尺寸（以结果尺寸为准）
    img_a = util.ensure_rgb(img_a).resize(img_b.size, Image.Resampling.LANCZOS)
    diff = ImageChops.difference(img_a, img_b).convert("L")
    hist = diff.histogram()
    total = sum(hist)
    mse = sum(i * c for i, c in enumerate(hist)) / max(total, 1)
    import math
    rmse = math.sqrt(mse)
    psnr = 100.0 if mse < 1e-9 else 20 * math.log10(255.0 / max(rmse, 1e-6))
    changed = sum(c for i, c in enumerate(hist) if i > 8) / max(total, 1)

    # 热力图：差异放大 + 伪彩色
    heat = diff.point(lambda v: util.clamp(v * 4))
    heat_rgb = colorize_heat(heat)
    result_id = cache.put(make_key(rec["hash"], result_id, "diff"), heat_rgb)
    return jsonify({
        "result_id": result_id,
        "file_url": f"/api/results/{result_id}/file",
        "metrics": {"mse": round(mse, 2), "rmse": round(rmse, 2),
                    "psnr": round(psnr, 2), "changed_ratio": round(changed, 4)},
    })


def colorize_heat(gray):
    """把差异灰度映射为热力伪彩色。"""
    lut = []
    for i in range(256):
        t = i / 255.0
        if t < 0.5:
            r = int(t * 2 * 255)
            g = 0
            b = int((0.5 - t) * 2 * 255)
        else:
            r = 255
            g = int((t - 0.5) * 2 * 255)
            b = 0
        lut.append((r, g, b))
    from PIL import Image
    try:
        return gray.point(lut, "RGB")
    except Exception:
        out = Image.new("RGB", gray.size)
        out.putdata([lut[v] for v in gray.getdata()])
        return out


# ---------------------------------------------------------------------------
# 批处理
# ---------------------------------------------------------------------------
@bp.get("/batch")
def list_batch():
    return jsonify({"jobs": batch.list_jobs()})


@bp.post("/batch")
def create_batch():
    data = request.get_json(silent=True) or {}
    nodes = data.get("nodes")
    pipeline_id = data.get("pipeline_id")
    pipeline_name = data.get("pipeline_name")
    if not nodes and pipeline_id:
        p = pipelines_store.read().get(pipeline_id)
        if p:
            nodes = p.get("nodes", [])
            pipeline_name = p.get("name")
    image_ids = data.get("image_ids", [])
    if not nodes or not image_ids:
        return jsonify({"error": "缺少 nodes 或 image_ids"}), 400
    job = batch.enqueue(nodes, image_ids, pipeline_id=pipeline_id, pipeline_name=pipeline_name)
    return jsonify({"job_id": job["id"]})


@bp.get("/batch/<job_id>")
def get_batch(job_id):
    job = batch.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@bp.post("/batch/<job_id>/cancel")
def cancel_batch(job_id):
    job = batch.cancel(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------
def _preset_view(p):
    return {"id": p["id"], "name": p["name"], "scope": p["scope"],
            "node_type": p.get("node_type"), "params": p.get("params", {}),
            "created_at": p.get("created_at")}


@bp.get("/presets")
def list_presets():
    return jsonify({"presets": [_preset_view(p) for p in presets_store.read()]})


@bp.post("/presets")
def create_preset():
    data = request.get_json(silent=True) or {}
    pid = __import__("uuid").uuid4().hex
    p = {"id": pid, "name": data.get("name", "预设"),
         "scope": data.get("scope", "filter"), "node_type": data.get("node_type"),
         "params": data.get("params", {}), "created_at": now_iso()}
    presets_store.update(lambda doc: [p] + doc)
    return jsonify(_preset_view(p))


@bp.put("/presets/<pid>")
def update_preset(pid):
    data = request.get_json(silent=True) or {}

    def _upd(doc):
        for i, p in enumerate(doc):
            if p["id"] == pid:
                p = dict(p)
                p["name"] = data.get("name", p["name"])
                p["params"] = data.get("params", p["params"])
                doc[i] = p
                break
        return doc
    presets_store.update(_upd)
    for p in presets_store.read():
        if p["id"] == pid:
            return jsonify(_preset_view(p))
    return jsonify({"error": "not found"}), 404


@bp.delete("/presets/<pid>")
def delete_preset(pid):
    presets_store.update(lambda doc: [p for p in doc if p["id"] != pid])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------
@bp.get("/history")
def list_history():
    return jsonify({"history": history.list()})


@bp.get("/history/<history_id>")
def get_history(history_id):
    e = history.get(history_id)
    if not e:
        return jsonify({"error": "not found"}), 404
    return jsonify(e)


@bp.delete("/history/<history_id>")
def delete_history(history_id):
    e = history.get(history_id)
    if e and e.get("result_id"):
        cache.delete_result(e["result_id"])
    history.delete(history_id)
    return jsonify({"ok": True})


@bp.post("/history/<history_id>/restore")
def restore_history(history_id):
    snapshot = history.restore_snapshot(history_id)
    if snapshot is None:
        return jsonify({"error": "not found"}), 404
    pid = __import__("uuid").uuid4().hex
    e = history.get(history_id)
    rec = {
        "id": pid,
        "name": f"恢复自 {e.get('pipeline_name') or '历史'}",
        "nodes": snapshot.get("nodes", []),
        "version": 1, "versions": [],
        "created_at": now_iso(), "updated_at": now_iso(),
        "valid": True,
    }
    pipelines_store.update(lambda doc: {**doc, pid: rec})
    return jsonify(_pipeline_view(rec))


# ---------------------------------------------------------------------------
# 查重（相似图聚类）
# ---------------------------------------------------------------------------
def _dedup_job_view(j, include_groups=False):
    view = {
        "id": j["id"], "status": j.get("status"),
        "threshold": j.get("threshold"), "total": j.get("total", 0),
        "done": j.get("done", 0), "errors": j.get("errors", 0),
        "stage": j.get("stage", ""),
        "created_at": j.get("created_at"), "finished_at": j.get("finished_at"),
        "summary": j.get("summary"),
    }
    if include_groups:
        view["result"] = dedup.load_result(j["id"])
    return view


@bp.post("/dedup/scan")
def dedup_scan():
    data = request.get_json(silent=True) or {}
    try:
        threshold = float(data.get("threshold", config.DEDUP_DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return jsonify({"error": "阈值需为数字"}), 400
    threshold = max(config.DEDUP_MIN_THRESHOLD, min(config.DEDUP_MAX_THRESHOLD, threshold))
    force = bool(data.get("force_refresh", False))
    try:
        job = dedup.start_scan(threshold, force_refresh=force)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"job_id": job["id"]})


@bp.get("/dedup/jobs")
def dedup_list_jobs():
    jobs = [_dedup_job_view(j) for j in dedup._read_jobs()]
    return jsonify({"jobs": jobs})


@bp.get("/dedup/jobs/<job_id>")
def dedup_get_job(job_id):
    j = dedup.get_job(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    include_groups = request.args.get("groups") == "1" or j.get("status") == "done"
    return jsonify(_dedup_job_view(j, include_groups=include_groups))


@bp.post("/dedup/jobs/<job_id>/cancel")
def dedup_cancel_job(job_id):
    j = dedup.cancel(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    return jsonify(_dedup_job_view(j))


def init_app(app):
    """在应用启动时注册蓝图并做一次性一致性检查。"""
    app.register_blueprint(bp)
    issues = image_store.reconcile()
    if issues["orphan_files"] or issues["orphan_meta"]:
        app.logger.info("启动一致性检查发现孤儿：%s", issues)
    dedup.prune_fingerprints()
    return app
