"""算法通用工具：降采样、灰度矩阵、卷积、梯度、连通域等。

难点「图像算法效率 / 大图内存管理」在这里集中处理：

- 所有像素级算法都在「工作副本」上运行（downscale_to_max 把最长边压到上限），
  避免在大图上做 O(n^2) 的 Python 循环；坐标最后按比例映射回原图。
- 卷积/模糊/梯度走 Pillow 内置 C 实现（ImageFilter.Kernel / BoxBlur），
  只有统计、连通域这类必须逐像素的才用 Python，且限定在低分辨率副本上。
- gray_matrix 直接复用 Image.getdata 一次性取出行列，避免逐点 getpixel 调用开销。
"""
import math

from PIL import Image, ImageFilter, ImageOps

from .. import config


# ---------------------------------------------------------------------------
# 尺寸与降采样
# ---------------------------------------------------------------------------
def downscale_to_max(img: Image.Image, max_dim: int) -> Image.Image:
    """若最长边超过 max_dim，按比例缩小；否则原样返回。"""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_dim:
        return img
    scale = max_dim / float(longest)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return img.resize((nw, nh), Image.Resampling.BILINEAR)


def scale_ratio(orig_size, work_size):
    """返回 原图->工作副本 的缩放系数（原图坐标 = 工作坐标 * ratio）。"""
    return orig_size[0] / float(work_size[0]) if work_size[0] else 1.0


def to_grayscale(img: Image.Image) -> Image.Image:
    """RGB 转灰度（若已是 L 则复制）。"""
    if img.mode in ("L", "I;16", "I"):
        return img.convert("L")
    return ImageOps.grayscale(img)


def ensure_rgb(img: Image.Image) -> Image.Image:
    """统一成 RGB（PNG 透明底合成到白底）。"""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "PA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


# ---------------------------------------------------------------------------
# 像素矩阵（低分辨率下使用）
# ---------------------------------------------------------------------------
def gray_matrix(img: Image.Image):
    """返回 (width, height, rows)，rows 是 list[list[int]] 灰度值。"""
    g = to_grayscale(img)
    w, h = g.size
    px = list(g.getdata())
    rows = [px[y * w:(y + 1) * w] for y in range(h)]
    return w, h, rows


def rgb_matrix(img: Image.Image):
    """返回 (width, height, rows)，rows 是 list[list[(r,g,b)]]。"""
    rgb = ensure_rgb(img)
    w, h = rgb.size
    px = list(rgb.getdata())
    rows = [px[y * w:(y + 1) * w] for y in range(h)]
    return w, h, rows


# ---------------------------------------------------------------------------
# 卷积与梯度（C 实现）
# ---------------------------------------------------------------------------
def apply_kernel(img: Image.Image, kernel, scale=None, offset=0):
    """对图像应用一个 (3,3) 卷积核。kernel 为 9 个数的扁平列表。"""
    k = ImageFilter.Kernel((3, 3), list(kernel), scale=scale, offset=offset)
    return img.filter(k)


SOBEL_X = (-1, 0, 1, -2, 0, 2, -1, 0, 1)
SOBEL_Y = (-1, -2, -1, 0, 0, 0, 1, 2, 1)
PREWITT_X = (-1, 0, 1, -1, 0, 1, -1, 0, 1)
PREWITT_Y = (-1, -1, -1, 0, 0, 0, 1, 1, 1)
LAPLACIAN = (0, 1, 0, 1, -4, 1, 0, 1, 0)


def _signed_gradients(img: Image.Image, operator="sobel"):
    """用 offset=128 保留符号，返回 (gx, gy) 两张灰度图（128 为零点）。"""
    g = to_grayscale(img)
    kx = SOBEL_X if operator == "sobel" else PREWITT_X
    ky = SOBEL_Y if operator == "sobel" else PREWITT_Y
    gx = apply_kernel(g, kx, scale=1, offset=128)
    gy = apply_kernel(g, ky, scale=1, offset=128)
    return gx, gy


def gradient_magnitude(img: Image.Image, operator="sobel"):
    """计算梯度幅值（灰度，0-255）。operator: sobel / prewitt / laplacian。

    采用 offset=128 技巧保留梯度符号，再逐像素算 sqrt(dx^2 + dy^2)。
    该函数只应在低分辨率工作副本上调用（features/detection 内部）。
    """
    g = to_grayscale(img)
    if operator == "laplacian":
        lap = apply_kernel(g, LAPLACIAN, scale=1, offset=128)
        data = [clamp(abs(v - 128) * 3) for v in lap.getdata()]
        out = Image.new("L", g.size)
        out.putdata(data)
        return out
    gx, gy = _signed_gradients(g, operator)
    gxd, gyd = gx.getdata(), gy.getdata()
    data = [
        clamp(int(math.sqrt((dx - 128) ** 2 + (dy - 128) ** 2)))
        for dx, dy in zip(gxd, gyd)
    ]
    out = Image.new("L", g.size)
    out.putdata(data)
    return out


def gradient_orientation(img: Image.Image, operator="sobel"):
    """返回每个像素的梯度方向（弧度，-pi..pi）。仅用于低分辨率。"""
    g = to_grayscale(img)
    kx = SOBEL_X if operator == "sobel" else PREWITT_X
    ky = SOBEL_Y if operator == "sobel" else PREWITT_Y
    gx = apply_kernel(g, kx, scale=1, offset=128)
    gy = apply_kernel(g, ky, scale=1, offset=128)
    w, h = g.size
    gxd, gyd = gx.getdata(), gy.getdata()
    angles = [
        math.atan2(dy - 128, dx - 128)
        for dx, dy in zip(gxd, gyd)
    ]
    return w, h, angles


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def channel_stats(img: Image.Image):
    """返回每通道 (mean, std)。mode 不限，按通道数返回列表。"""
    if img.mode not in ("RGB", "L"):
        img = ensure_rgb(img)
    bands = img.split()
    stats = []
    for band in bands:
        hist = band.histogram()
        total = sum(hist)
        mean = sum(i * c for i, c in enumerate(hist)) / max(total, 1)
        var = sum(((i - mean) ** 2) * c for i, c in enumerate(hist)) / max(total, 1)
        stats.append((mean, math.sqrt(var)))
    return stats


def clamp(v, lo=0, hi=255):
    return int(max(lo, min(hi, v)))


# ---------------------------------------------------------------------------
# 连通域（二值掩码，低分辨率）
# ---------------------------------------------------------------------------
def connected_components(mask_rows, w, h, threshold=128):
    """在二值矩阵上做 4-连通域标记（BFS）。mask_rows 为 list[list[int]]。

    返回 (labels, components)，labels 是二维 int 列表（0 为背景），
    components 是 {label: [(x, y), ...]}。
    """
    labels = [[0] * w for _ in range(h)]
    components = {}
    label = 0
    for y in range(h):
        row = mask_rows[y]
        for x in range(w):
            if row[x] >= threshold and labels[y][x] == 0:
                label += 1
                stack = [(x, y)]
                labels[y][x] = label
                pts = []
                while stack:
                    cx, cy = stack.pop()
                    pts.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h:
                            if mask_rows[ny][nx] >= threshold and labels[ny][nx] == 0:
                                labels[ny][nx] = label
                                stack.append((nx, ny))
                components[label] = pts
    return labels, components


def points_bbox(points):
    """一组 (x, y) 点的外接框 [x0, y0, x1, y1]（含边界）。"""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


# ---------------------------------------------------------------------------
# 局部极大值（关键点/角点）
# ---------------------------------------------------------------------------
def local_maxima(rows, w, h, radius=3, min_response=0.0):
    """在灰度幅值矩阵里找局部极大值，返回 [(x, y, response), ...]（已按响应降序）。"""
    pts = []
    for y in range(radius, h - radius):
        row = rows[y]
        for x in range(radius, w - radius):
            v = row[x]
            if v < min_response:
                continue
            # 与邻域比较（含对角）
            best = True
            for dy in range(-radius, radius + 1):
                r = rows[y + dy]
                for dx in range(-radius, radius + 1):
                    if r[x + dx] > v:
                        best = False
                        break
                if not best:
                    break
            if best:
                pts.append((x, y, v))
    pts.sort(key=lambda p: p[2], reverse=True)
    return pts
