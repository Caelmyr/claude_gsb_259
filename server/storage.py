"""原子写 JSON 与统一元数据存储。

难点之一「JSON 元数据与图像文件的一致性」在这里打下地基：

- 所有 JSON 落盘都走 atomic_write_json：先写同目录临时文件 -> flush + fsync ->
  os.replace 原子替换 -> fsync 目录，保证任何时刻磁盘上的 JSON 都是完整可读的，
  崩溃/中断不会留下半截文件。
- 每次写前把旧版本复制为 ``.bak``，读取时若主文件损坏自动回退到备份。
- JsonStore 内置 threading.RLock，多个线程（批处理 + API）并发写互不干扰。
"""
import copy
import json
import os
import shutil
import tempfile
import threading


def _fsync_dir(directory):
    """fsync 目录，确保重命名操作落盘（POSIX）。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write_json(path, obj):
    """把 obj 原子地写入 path（JSON）。失败时保证原文件不受影响。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    # 备份旧版本，供损坏回退使用
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)   # 原子替换
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path, data):
    """把字节流原子地写入 path（用于图像文件落盘）。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    """读取 JSON，主文件损坏时回退 .bak，仍失败则返回 default 的深拷贝。"""
    if default is None:
        default = {}
    for candidate in (path, path + ".bak"):
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            continue
    return copy.deepcopy(default)


class JsonStore:
    """带锁的 JSON 文档存储。用于 images/pipelines/history/presets/queue/cache。

    每个实例对应一个 JSON 文件，对外提供 read / write / update 三个接口。
    update(fn) 在锁内执行「读-改-写」，避免并发读改写丢更新。
    """

    def __init__(self, path, default=None):
        self.path = path
        self.default = default if default is not None else {}
        self.lock = threading.RLock()
        self._ensure()

    def _ensure(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            atomic_write_json(self.path, self.default)

    def read(self):
        with self.lock:
            return read_json(self.path, self.default)

    def write(self, obj):
        with self.lock:
            atomic_write_json(self.path, obj)
            return obj

    def update(self, fn):
        """读-改-写在锁内完成，返回新的文档对象。fn 接收并返回文档。"""
        with self.lock:
            current = read_json(self.path, self.default)
            new_obj = fn(current)
            atomic_write_json(self.path, new_obj)
            return new_obj


def now_iso():
    """返回 ISO8601 时间字符串。"""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")
