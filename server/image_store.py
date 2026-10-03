"""图像文件存储与元数据管理。

- 采用「内容寻址」：上传即计算 SHA-256，磁盘文件名 = 内容哈希 + 扩展名，
  相同内容的图自动去重，元数据里的指针永远稳定，不会因重命名而失联。
- 写序保证一致性：先把图像字节原子落盘，再原子更新 images.json；
  中途崩溃只会留下「孤儿文件」，reconcile() 能把它识别出来。
- 缩略图独立生成，前端列表/预览不拖全尺寸大图。
"""
import hashlib
import io
import os
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config
from .storage import JsonStore, atomic_write_bytes, now_iso


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extension_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in config.ALLOWED_EXTENSIONS:
        return ext
    return ".png"


class ImageStore:
    def __init__(self):
        self.meta = JsonStore(config.IMAGES_JSON, {})

    # ------------------------------------------------------------------ 读
    def list_records(self):
        """按创建时间倒序返回所有图像记录列表。"""
        recs = list(self.meta.read().values())
        recs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return recs

    def get(self, image_id):
        return self.meta.read().get(image_id)

    def file_path(self, image_id):
        rec = self.get(image_id)
        if not rec:
            return None
        return os.path.join(config.IMAGES_DIR, rec["stored_name"])

    def thumbnail_path(self, image_id):
        return os.path.join(config.THUMBS_DIR, f"{image_id}.jpg")

    # ------------------------------------------------------------------ 写
    def save_upload(self, data: bytes, filename: str):
        """保存一次上传。返回记录 dict。相同内容自动去重。"""
        image_id = _content_hash(data)
        existing = self.get(image_id)
        if existing is not None:
            return existing

        ext = _extension_for(filename)
        stored_name = image_id + ext
        dest = os.path.join(config.IMAGES_DIR, stored_name)

        # 1) 先落图像文件（原子）
        atomic_write_bytes(dest, data)

        # 2) 读取尺寸/格式
        try:
            img = Image.open(io.BytesIO(data))
            width, height = img.size
            fmt = (img.format or ext[1:].upper())
        except UnidentifiedImageError:
            # 落盘失败清理，向上抛
            try:
                os.unlink(dest)
            except OSError:
                pass
            raise ValueError("无法识别的图像格式")

        record = {
            "id": image_id,
            "filename": filename or stored_name,
            "stored_name": stored_name,
            "hash": image_id,
            "ext": ext,
            "format": fmt,
            "width": width,
            "height": height,
            "size_bytes": len(data),
            "created_at": now_iso(),
            "tags": [],
            "note": "",
            "annotations": [],
        }

        # 3) 再原子更新元数据
        def _add(doc):
            doc = dict(doc)
            doc[image_id] = record
            return doc

        self.meta.update(_add)
        self._make_thumbnail(image_id, data)
        return record

    def _make_thumbnail(self, image_id, data: bytes):
        """生成缩略图；失败不致命（保留空缩略图路径）。"""
        try:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((config.THUMB_DIM, config.THUMB_DIM), Image.Resampling.LANCZOS)
            tmp = self.thumbnail_path(image_id) + ".tmp"
            img.save(tmp, "JPEG", quality=82)
            os.replace(tmp, self.thumbnail_path(image_id))
        except Exception:
            try:
                os.unlink(self.thumbnail_path(image_id) + ".tmp")
            except OSError:
                pass

    def update_meta(self, image_id, fields):
        """更新 tags / note / filename 等轻量字段。"""
        def _upd(doc):
            doc = dict(doc)
            rec = doc.get(image_id)
            if rec:
                rec = dict(rec)
                for k in ("tags", "note", "filename"):
                    if k in fields:
                        rec[k] = fields[k]
                rec["updated_at"] = now_iso()
                doc[image_id] = rec
            return doc

        return self.meta.update(_upd).get(image_id)

    def add_annotation(self, image_id, box, label, color="#ff5252"):
        """给图像加一条人工标注（目标检测标注页）。"""
        def _upd(doc):
            doc = dict(doc)
            rec = doc.get(image_id)
            if rec:
                rec = dict(rec)
                rec.setdefault("annotations", []).append({
                    "box": box, "label": label, "color": color, "created_at": now_iso(),
                })
                doc[image_id] = rec
            return doc

        return self.meta.update(_upd).get(image_id)

    def delete_annotation(self, image_id, index):
        def _upd(doc):
            doc = dict(doc)
            rec = doc.get(image_id)
            if rec and "annotations" in rec:
                rec = dict(rec)
                anns = list(rec["annotations"])
                if 0 <= index < len(anns):
                    anns.pop(index)
                rec["annotations"] = anns
                doc[image_id] = rec
            return doc

        return self.meta.update(_upd).get(image_id)

    def delete(self, image_id):
        """删除图像及其缩略图（结果图保留，历史里仍能查看）。"""
        rec = self.get(image_id)
        if not rec:
            return False
        for p in (self.file_path(image_id), self.thumbnail_path(image_id)):
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

        def _upd(doc):
            doc = dict(doc)
            doc.pop(image_id, None)
            return doc

        self.meta.update(_upd)
        return True

    # -------------------------------------------------------------- 一致性
    def reconcile(self):
        """校验「JSON 元数据」与「图像文件」的一致性。

        返回 issues 字典：
          orphan_meta  - 元数据存在但文件缺失（悬空引用）
          orphan_files - 文件存在但元数据没有（孤儿文件，可回收）
        """
        records = self.meta.read()
        issues = {"orphan_meta": [], "orphan_files": []}

        for image_id, rec in records.items():
            p = os.path.join(config.IMAGES_DIR, rec.get("stored_name", ""))
            if not os.path.exists(p):
                issues["orphan_meta"].append({"id": image_id, "filename": rec.get("filename")})

        known_names = {rec.get("stored_name") for rec in records.values()}
        for fn in os.listdir(config.IMAGES_DIR):
            if fn.startswith(".tmp-"):
                continue
            if fn not in known_names:
                issues["orphan_files"].append(fn)

        return issues

    def clean_orphan_files(self):
        """删除无元数据引用的孤儿文件，返回删除数量。"""
        records = self.meta.read()
        known = {rec.get("stored_name") for rec in records.values()}
        removed = 0
        for fn in os.listdir(config.IMAGES_DIR):
            if fn.startswith(".tmp-"):
                continue
            if fn not in known:
                try:
                    os.unlink(os.path.join(config.IMAGES_DIR, fn))
                    removed += 1
                except OSError:
                    pass
        return removed
