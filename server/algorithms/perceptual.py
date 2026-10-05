"""感知指纹：pHash（DCT 均值哈希）、dHash（梯度哈希）、颜色直方图、清晰度。

查重要识别「同一张图的翻拍 / 裁剪 / 轻微调色 / 缩放 / 重压缩」副本，
逐字节哈希（image_store 里的 SHA-256）无能为力，这里用对这类扰动鲁棒的
感知特征，多信号组合：

- pHash：小尺寸灰度 -> DCT -> 取低频 8×8 与中位数比较得 64bit。缩放、JPEG
  重压缩、亮度/对比度/轻微调色都几乎不改变它，是主判据。除全图外另算两个
  中心裁剪比例（80%、64%）的变体：翻拍照片/截图通常带边框、或被裁掉边缘，
  用「取双方最小距离」对齐，常见的加边框/裁边副本即可命中。
- dHash：相邻像素亮度差符号，对整体亮度变化不敏感，作为结构辅助判据，
  进一步压低「只是色调接近的不同图」的误判。同样有全图/中心两种。
- 颜色签名：8 级 RGB 联合直方图（512 维），Pearson 相关做「颜色门控」：
  结构像、颜色分布也说得过去才算疑似重复（阈值宽松，轻微调色仍通过）。
- sharpness：拉普拉斯方差（统一 128×128 小图上算，跨图可比），用于在组里
  挑「最清晰」的一张。

纯 Python（与本项目其余算法一致，不依赖 numpy）：DCT 用可分离形式，
输入只有 32×32，每张图约 3.3 万次乘加；指纹在 dedup 模块缓存，重扫不重算。

已知边界：只剩画面一小块的重度偏移裁剪没有稳定结构可对齐，任何全局哈希
都无法可靠区分它与「构图碰巧相似的不同图」；这类宁可漏报也不误报。
"""
import math

from PIL import Image, ImageFilter, ImageOps

from .. import config

# 中心裁剪变体（保留画面中心这么大的方形）
CROP_RATIOS = (0.8, 0.64)
# 补救姿态：画面上取若干个 GRID_RATIO 大小的方形窗口（中心 + 四向偏移），
# 用于对齐加了非对称边框的翻拍、截图。负例（不同图）在这些窗口上的最佳
# 距离仍 ≥ ~24 位，而真副本 ≤ ~8 位，间隔足够大。
GRID_RATIO = 0.68
GRID_POSES = (0.5, 0.0), (0.5, 1.0), (0.0, 0.5), (1.0, 0.5), (0.5, 0.5)

# ---------------------------------------------------------------------------
# DCT（DCT-II，可分离：先行变换再列变换）
# ---------------------------------------------------------------------------
_DCT_CACHE = {}  # n -> 基矩阵


def _dct_basis(n=32):
    m = [[0.0] * n for _ in range(n)]
    for k in range(n):
        scale = math.sqrt(1.0 / n) if k == 0 else math.sqrt(2.0 / n)
        for i in range(n):
            m[k][i] = scale * math.cos(math.pi * (2 * i + 1) * k / (2.0 * n))
    return m


def _dct2(rows, n=32):
    """对 n×n 浮点矩阵做二维 DCT-II，返回 n×n 结果。"""
    c = _DCT_CACHE.get(n)
    if c is None:
        c = _dct_basis(n)
        _DCT_CACHE[n] = c
    tmp = [[0.0] * n for _ in range(n)]
    for y in range(n):
        ry = rows[y]
        ty = tmp[y]
        for k in range(n):
            ck = c[k]
            s = 0.0
            for i in range(n):
                s += ry[i] * ck[i]
            ty[k] = s
    out = [[0.0] * n for _ in range(n)]
    for v in range(n):
        cv = c[v]
        ov = out[v]
        for u in range(n):
            s = 0.0
            for y in range(n):
                s += cv[y] * tmp[y][u]
            ov[u] = s
    return out


def _median(values):
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _bits_to_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | (1 if b else 0)
    return v


def phash_from_gray(gray, work=config.DEDUP_HASH_DIM, keep=8):
    """从一张灰度图计算 DCT-pHash（64bit 整数）。

    统一拉伸到 work×work（哈希要规整网格，比例无关的形变由调用方裁剪承担），
    取 DCT 左上 keep×keep 低频块与中位数比较。
    """
    n = keep * 4
    small = gray.resize((work, work), Image.Resampling.LANCZOS).resize((n, n), Image.Resampling.BILINEAR)
    px = list(small.getdata())
    rows = [list(px[y * n:(y + 1) * n]) for y in range(n)]
    dct = _dct2(rows, n)
    block = [dct[v][u] for v in range(keep) for u in range(keep)]
    med = _median(block)
    return _bits_to_int(v > med for v in block)


def dhash_from_gray(gray, size=9):
    """梯度哈希：size×size 灰度上相邻两列亮度差符号，返回 64bit。"""
    small = gray.resize((size, size), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    rows = [px[y * size:(y + 1) * size] for y in range(size)]
    bits = []
    for y in range(size):
        for x in range(size - 1):
            bits.append(rows[y][x] > rows[y][x + 1])
    return _bits_to_int(bits)


def center_square(gray, ratio):
    """取中心 ratio×ratio 的方形区域（对应翻拍裁边/截图/加边框）。"""
    w, h = gray.size
    side = int(min(w, h) * ratio)
    x0 = (w - side) // 2
    y0 = (h - side) // 2
    return gray.crop((x0, y0, x0 + side, y0 + side))


def grid_square(gray, fx, fy, ratio=GRID_RATIO):
    """取窗口中心位于画面 (fx,fy) 比例处、边长 ratio 的方形（自动夹在图内）。"""
    w, h = gray.size
    side = int(min(w, h) * ratio)
    cx, cy = fx * w, fy * h
    x0 = max(0, min(int(cx - side / 2), w - side))
    y0 = max(0, min(int(cy - side / 2), h - side))
    return gray.crop((x0, y0, x0 + side, y0 + side))


# ---------------------------------------------------------------------------
# 清晰度：拉普拉斯方差
# ---------------------------------------------------------------------------
def sharpness(gray):
    """拉普拉斯响应方差（越大越锐利）。统一 128×128 上计算，跨图可比。"""
    g = gray.resize((128, 128), Image.Resampling.BILINEAR)
    lap = g.filter(ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=128))
    data = list(lap.getdata())
    n = len(data)
    mean = sum(data) / n
    var = sum((v - mean) ** 2 for v in data) / n
    return round(var, 2)


# ---------------------------------------------------------------------------
# 颜色签名：6 级/通道 RGB 联合直方图（216 维，量化为整数节省缓存体积）
# ---------------------------------------------------------------------------
COLOR_LEVELS = 6
COLOR_BIN_SCALE = 10000  # 比例放大成整数，落盘指纹更小


def color_signature(img_rgb, levels=COLOR_LEVELS):
    """粗粒度 RGB 联合直方图。返回整数列表（各 bin 的占比 *1e4）。"""
    small = img_rgb.resize((48, 48), Image.Resampling.BILINEAR)
    bins = levels ** 3
    hist = [0] * bins
    step = 256 // levels
    total = 0
    for r, g, b in small.getdata():
        idx = (min(r // step, levels - 1) * levels + min(g // step, levels - 1)) * levels + min(b // step, levels - 1)
        hist[idx] += 1
        total += 1
    scale = COLOR_BIN_SCALE / max(total, 1)
    return [int(v * scale) for v in hist]


def histogram_corr(a, b):
    """两个等长计数/比例向量的 Pearson 相关系数（-1..1）。"""
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va < 1e-12 or vb < 1e-12:
        return 1.0 if va < 1e-12 and vb < 1e-12 else 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


# ---------------------------------------------------------------------------
# 顶层：从 PIL 图提取指纹
# ---------------------------------------------------------------------------
def extract_fingerprint(img):
    """提取查重指纹 dict。调用方负责 EXIF 旋转、模式统一与大图降采样。"""
    rgb = ImageOps.exif_transpose(img)
    if rgb.mode != "RGB":
        rgb = rgb.convert("RGB")
    gray = ImageOps.grayscale(rgb)
    fp = {
        "phash": phash_from_gray(gray),
        "phash_center": phash_from_gray(center_square(gray, CROP_RATIOS[0])),
        "phash_center_2": phash_from_gray(center_square(gray, CROP_RATIOS[1])),
        "phash_grid": [phash_from_gray(grid_square(gray, fx, fy)) for fx, fy in GRID_POSES],
        "dhash": dhash_from_gray(gray),
        "dhash_center": dhash_from_gray(center_square(gray, CROP_RATIOS[0])),
        "sharpness": sharpness(gray),
        "color": color_signature(rgb),
    }
    return fp


def hamming(a, b):
    return (a ^ b).bit_count()


# 所有标量 pHash 变体（LSH 建表与成对比较都遍历它）
PHASH_KEYS = ("phash", "phash_center", "phash_center_2")


# ---------------------------------------------------------------------------
# 成对相似度
# ---------------------------------------------------------------------------
# 主判据用全图 pHash/dHash；中心/网格姿态仅作「加边框/轻微裁边」的补救通道。
PHASH_WEIGHT = 0.6
DHASH_WEIGHT = 0.4
# 补救姿态：裁剪 pHash 必须足够近才采信（否则不同图会借裁剪姿态撞车）
CENTER_RESCUE_PHASH_BITS = 6
GRID_RESCUE_PHASH_BITS = 10
# 结构高度一致时免颜色门控的门槛（主姿态汉明位数）
NEAR_PHASH_BITS = 3
NEAR_DHASH_BITS = 6
# 非近同图时，颜色相关低于该值则拒绝（防不同图结构撞车）
COLOR_GATE = 0.5


def _pose_dist(dp_bits, dd_bits):
    return PHASH_WEIGHT * (dp_bits / 64.0) + DHASH_WEIGHT * (dd_bits / 64.0)


def pair_similarity(fa, fb):
    """两张图的相似度（0..100）与判定细节。多通道取「最像」的一个：

    - 主通道：全图 pHash + 全图 dHash。缩放/重压缩/调色/翻拍几乎 0 距离。
    - 中心通道：中心 80%/64% pHash + 中心 dHash，pHash ≤ 6 位才采信，
      覆盖对称裁边。
    - 网格通道：5 个偏移窗口的 pHash 最佳值 ≤ 10 位，配合中心 dHash，
      覆盖非对称边框的翻拍/截图。
    """
    dp_full = hamming(fa["phash"], fb["phash"])
    dd_full = hamming(fa["dhash"], fb["dhash"])
    dist = _pose_dist(dp_full, dd_full)
    dp_best, dd_best = float(dp_full), float(dd_full)
    rescued = False

    def try_rescue(dp_bits, dd_bits, limit):
        nonlocal dist, dp_best, dd_best, rescued
        if dp_bits <= limit:
            d = _pose_dist(dp_bits, dd_bits)
            if d < dist:
                dist, dp_best, dd_best = d, float(dp_bits), float(dd_bits)
                rescued = True

    # 中心 80% / 64%
    dd_center = hamming(fa["dhash_center"], fb["dhash_center"])
    try_rescue(hamming(fa["phash_center"], fb["phash_center"]), dd_center, CENTER_RESCUE_PHASH_BITS)
    try_rescue(hamming(fa["phash_center_2"], fb["phash_center_2"]), dd_center, CENTER_RESCUE_PHASH_BITS)
    # 偏移窗口（双方窗口按相同比例取，同位比较即可对齐）
    grid_best = min(hamming(a, b) for a, b in zip(fa["phash_grid"], fb["phash_grid"]))
    try_rescue(grid_best, dd_center, GRID_RESCUE_PHASH_BITS)

    score = max(0.0, min(100.0, (1.0 - dist) * 100.0))
    color_corr = histogram_corr(fa["color"], fb["color"])
    # 高置信：主通道本身就近；或补救通道且结构确实很近
    near_exact = (dp_full <= NEAR_PHASH_BITS and dd_full <= NEAR_DHASH_BITS) or (
        rescued and dp_best <= 6 and dd_best <= 14)
    return {
        "score": round(score, 2),
        "distance": round(dist, 4),
        "color_corr": round(color_corr, 3),
        "near_exact": bool(near_exact),
        "rescued": bool(rescued),
    }


def passes_threshold(sim, threshold):
    """相似度达到阈值，且（近同图 或 颜色门控通过）。"""
    if sim["score"] < threshold:
        return False
    return sim["near_exact"] or sim["color_corr"] >= COLOR_GATE
