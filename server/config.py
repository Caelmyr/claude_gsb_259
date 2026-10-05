"""全局配置：目录路径、限制常量与目录初始化。

所有运行时数据（图像文件、结果、缩略图、缓存、JSON 元数据）都落在 data/ 下，
图像文件与 JSON 元数据分离存放，元数据只保存指针信息（哈希、尺寸、时间戳等）。
"""
import os

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

IMAGES_DIR = os.path.join(DATA_DIR, "images")        # 上传的原图（按内容哈希命名）
RESULTS_DIR = os.path.join(DATA_DIR, "results")      # 处理结果图
THUMBS_DIR = os.path.join(DATA_DIR, "thumbnails")    # 预览缩略图
CACHE_DIR = os.path.join(DATA_DIR, "cache")          # 结果缓存（与 results 统一）
META_DIR = os.path.join(DATA_DIR, "metadata")        # JSON 元数据

IMAGES_JSON = os.path.join(META_DIR, "images.json")
PIPELINES_JSON = os.path.join(META_DIR, "pipelines.json")
HISTORY_JSON = os.path.join(META_DIR, "history.json")
PRESETS_JSON = os.path.join(META_DIR, "presets.json")
QUEUE_JSON = os.path.join(META_DIR, "queue.json")
CACHE_JSON = os.path.join(META_DIR, "cache.json")
DEDUP_JSON = os.path.join(META_DIR, "dedup.json")        # 查重指纹缓存（哈希/直方图/清晰度）
DEDUP_JOBS_JSON = os.path.join(META_DIR, "dedup_jobs.json")

# ---------------------------------------------------------------------------
# 限制与默认值
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = 25                # 单文件上传上限（MB）
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

MAX_DIM = 1600                    # 算法工作副本的最长边（超出则先降采样）
PREVIEW_DIM = 900                 # 前端展示/下载的完整预览尺寸
THUMB_DIM = 220                   # 缩略图最长边
FEATURE_WORK_DIM = 360            # 特征提取/检测/分割的工作分辨率（加速）

MAX_BATCH_WORKERS = 2             # 批处理线程池大小（CPU 密集，控制内存）
CACHE_MAX_BYTES = 256 * 1024 * 1024
CACHE_MAX_ENTRIES = 400
PIPELINE_MAX_VERSIONS = 20        # 每条流水线保留的版本快照数
HISTORY_MAX_ENTRIES = 500         # 历史记录上限（超出裁掉最旧）

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}

# ---------------------------------------------------------------------------
# 查重
# ---------------------------------------------------------------------------
DEDUP_DEFAULT_THRESHOLD = 86        # 相似度阈值默认值（0-100，越高越严格）
DEDUP_MIN_THRESHOLD = 50
DEDUP_MAX_THRESHOLD = 99
DEDUP_HASH_GATE = 0.70              # 内部粗筛闸口：综合结构哈希相似度低于此值不做精验
DEDUP_MAX_CANDIDATES = 200000       # 单次扫描精验候选对上限（防极端图库耗时失控）
DEDUP_EDGE_DIM = 32                 # 边缘互相关工作尺寸
DEDUP_MAX_JOBS = 20                 # 查重任务历史保留数

# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
_ALL_DIRS = [
    DATA_DIR, IMAGES_DIR, RESULTS_DIR, THUMBS_DIR, CACHE_DIR, META_DIR,
]


def ensure_dirs():
    """幂等地创建所有运行时目录。"""
    for d in _ALL_DIRS:
        os.makedirs(d, exist_ok=True)


def human_bytes(n):
    """把字节数转成可读字符串。"""
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n / 1024.0:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
