"""结果缓存与结果文件管理。

难点之一「结果缓存」：同一个（图像 + 流水线）组合不重复计算。

- 缓存键 = sha256(图像内容哈希 + 流水线规范化 JSON)，命中直接复用结果文件。
- 结果图落盘到 data/results/<result_id>.png（原子写），cache.json 记录键->结果映射。
- LRU 淘汰：超过条目数或字节数上限时，按最后访问时间踢掉最久未用的结果，
  同步删除其文件，保持 JSON 与文件一致。
- 特征/检测/分割/风格等单图接口也统一走这里，天然获得缓存能力。
"""
import hashlib
import json
import os
import time
import uuid

from PIL import Image

from . import config
from .storage import JsonStore, atomic_write_bytes, now_iso
from .algorithms import util


def make_key(*parts):
    """由若干字符串片段生成确定性缓存键。"""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class ResultCache:
    def __init__(self):
        self.store = JsonStore(config.CACHE_JSON, {})

    # ------------------------------------------------------------------ 读
    def get(self, key):
        entry = self.store.read().get(key)
        if not entry:
            return None
        path = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
        if not os.path.exists(path):
            return None
        self._touch(key)
        return entry.get("result_id")

    def _touch(self, key):
        def _upd(doc):
            doc = dict(doc)
            if key in doc:
                e = dict(doc[key])
                e["last_access"] = time.time()
                doc[key] = e
            return doc
        self.store.update(_upd)

    def get_entry(self, result_id):
        for entry in self.store.read().values():
            if entry.get("result_id") == result_id:
                return entry
        return None

    def result_path(self, result_id):
        entry = self.get_entry(result_id)
        if not entry:
            return None
        p = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
        return p if os.path.exists(p) else None

    def result_image(self, result_id):
        p = self.result_path(result_id)
        if not p:
            return None
        try:
            return Image.open(p)
        except Exception:
            return None

    def list_results(self):
        """按创建时间倒序返回结果条目列表。"""
        entries = list(self.store.read().values())
        entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        return entries

    # ------------------------------------------------------------------ 写
    def put(self, key, image, meta=None):
        """保存结果图并登记缓存，返回 result_id。"""
        result_id = uuid.uuid4().hex
        file_name = result_id + ".png"
        dest = os.path.join(config.RESULTS_DIR, file_name)

        rgb = util.ensure_rgb(image)
        # 原子写：先写临时文件再 rename
        tmp = dest + ".tmp"
        rgb.save(tmp, "PNG", optimize=True)
        os.replace(tmp, dest)

        entry = {
            "result_id": result_id,
            "key": key,
            "file": file_name,
            "size_bytes": os.path.getsize(dest),
            "width": rgb.size[0],
            "height": rgb.size[1],
            "meta": meta or {},
            "created_at": now_iso(),
            "last_access": time.time(),
        }

        def _upd(doc):
            doc = dict(doc)
            doc[key] = entry
            return doc

        self.store.update(_upd)
        self.evict_if_needed()
        return result_id

    # ------------------------------------------------------------------ 淘汰
    def evict_if_needed(self):
        entries = self.store.read()
        if not entries:
            return 0
        total_bytes = sum(e.get("size_bytes", 0) for e in entries.values())
        count = len(entries)
        if count <= config.CACHE_MAX_ENTRIES and total_bytes <= config.CACHE_MAX_BYTES:
            return 0

        # 按最后访问时间升序，优先淘汰最久未用
        order = sorted(entries.items(), key=lambda kv: kv[1].get("last_access", 0))
        removed = 0
        while order and (len(entries) > config.CACHE_MAX_ENTRIES
                         or total_bytes > config.CACHE_MAX_BYTES):
            key, entry = order.pop(0)
            path = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
            entries.pop(key, None)
            total_bytes -= entry.get("size_bytes", 0)
            removed += 1
        self.store.write(entries)
        return removed

    def delete_result(self, result_id):
        """按 result_id 删除结果（供历史删除联动）。"""
        entry = self.get_entry(result_id)
        if not entry:
            return False
        path = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass

        def _upd(doc):
            doc = dict(doc)
            for k, e in list(doc.items()):
                if e.get("result_id") == result_id:
                    doc.pop(k)
            return doc
        self.store.update(_upd)
        return True
