"""图像查重：感知指纹、候选粗筛、边缘对齐精验与分组。

需求是找出「同一张图的翻拍 / 裁剪 / 轻微调色副本」，同时不能把「色调接近
的不同图」误判成重复。单一特征做不到这一点：

- 只比颜色直方图：蓝天/夜景会成片误报；
- 只比感知哈希：裁剪后哈希整体错位，容易漏报。

因此采用两级流水线（全部纯 Pillow 实现，大图只走缩略图尺寸，内存与耗时可控）：

1. 指纹（每张图只算一次并缓存）
   - pHash：32x32 灰度 -> 二维 DCT，取左上 8x8 低频，按中位数二值化 => 64 位。
     对缩放、JPEG 压缩、轻微色彩调整稳健。
   - dHash：水平 64 位（9x8 邻列比较）+ 垂直 64 位（8x9 邻行比较），
     对亮度/对比度变化不敏感。
   - 多视口：pHash/dHash 都在「全图 / 中心 80% / 中心 64%」三个视口上各算
     一份。裁剪副本与原图的「中心视口 ↔ 全图」可直接在哈希阶段对上，
     不依赖昂贵的逐对精验。
   - color：RGB 每通道 4 bin 的 64 维归一化直方图，用于区分「只是色调像」。
   - sharpness：拉普拉斯方差（归一化灰度上），用于保留建议里的清晰度排序。
2. 粗筛：对三个视口的 pHash 做 8 bit 分段 LSH（8 个连续段 + 两组确定性
   置换各 8 段），dHash 做 16 个连续段；逐桶流式扫描，桶内用整数位掩码的
   bit_count 即时计算结构相似度并过闸口——不在内存里保存数百万候选对。
3. 精验：Pillow 边缘图上做零均值归一化互相关（ZNCC），双方 3x3 多尺度
   中心裁剪对齐（兜底翻拍取景范围变化），取最高相关。
   最终得分以结构哈希为主导（0.75），边缘（0.17）与颜色（0.08）只作
   「封顶加权」：二者的贡献都以结构相似度为上界，且结构相似度低于
   STRUCT_FLOOR 直接不判重。于是「只是色调/大轮廓接近的不同图」无法
   靠颜色或共享边缘翻过判定线，阈值（50-99）控制判定宽严。
"""
import math
from collections import OrderedDict

from PIL import Image, ImageFilter, ImageOps

from .. import config

# 相似度权重与规则：
# 结构哈希主导（多视口 pHash/dHash）；边缘相关与颜色都只作「封顶加权」——
# 以结构相似度为上界（只允许小段容差），两张只是共享天空/地平线或主色调
# 接近、实际内容不同的图，无法靠边缘/颜色翻过判定线。
# STRUCT_FLOOR 是硬下限：结构相似度低于它一律不判重（边缘再像也不行）。
W_STRUCT = 0.75
W_EDGE = 0.17
W_COLOR = 0.08
EDGE_BONUS_TOLERANCE = 5.0
COLOR_BONUS_TOLERANCE = 8.0
STRUCT_FLOOR = 76.0

PHASH_BITS = 64
DHASH_BITS = 128
BAND_BITS = 8
BAND_MASK = (1 << BAND_BITS) - 1

# pHash 的两组固定位置换（0..63 的重排，手工写死保证可复现），
# 让 LSH 分桶覆盖非连续的位组合
_PERM_1 = (57, 12, 41, 3, 33, 8, 50, 19, 60, 25, 2, 46, 14, 37, 54, 7,
           22, 31, 48, 10, 5, 43, 17, 59, 0, 28, 52, 35, 9, 63, 20, 45,
           27, 61, 1, 16, 40, 24, 56, 30, 6, 39, 49, 13, 58, 21, 36, 44,
           18, 53, 62, 4, 29, 47, 11, 34, 26, 55, 42, 23, 38, 32, 15, 51)
_PERM_2 = (9, 44, 23, 61, 2, 30, 17, 52, 36, 1, 48, 13, 58, 26, 7, 40,
           20, 55, 33, 4, 46, 11, 63, 28, 15, 50, 38, 6, 59, 22, 42, 0,
           31, 18, 54, 10, 25, 47, 3, 35, 57, 8, 29, 51, 14, 62, 39, 5,
           43, 21, 49, 27, 16, 34, 60, 24, 45, 12, 53, 37, 56, 42, 19, 32)

# 多视口：全图 + 两个中心裁剪（覆盖翻拍/裁剪副本与原图中心区对齐的情况）
CROP_SCALES = (1.0, 0.8, 0.64)
VIEW_FULL, VIEW_80, VIEW_64 = 0, 1, 2


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _gray(img, size):
    """统一 EXIF 旋转 + 灰度，缩放到 size x size。size 可为 (w, h)。"""
    g = ImageOps.exif_transpose(img)
    if g.mode != "L":
        g = ImageOps.grayscale(g)
    return g.resize(size, Image.Resampling.LANCZOS)


def _center_crop(img, scale):
    """按最短边居中取 scale 比例的方形区域（scale=1 时等价于方形全图）。"""
    if scale >= 1.0:
        return img
    w, h = img.size
    side = int(round(min(w, h) * scale))
    left, top = (w - side) // 2, (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _dct_matrix(n):
    """DCT-II 正交变换矩阵（标准正交基归一化）。"""
    mat = [[0.0] * n for _ in range(n)]
    scale0 = math.sqrt(1.0 / n)
    scale = math.sqrt(2.0 / n)
    for k in range(n):
        for i in range(n):
            mat[k][i] = (scale0 if k == 0 else scale) * math.cos(math.pi * (2 * i + 1) * k / (2 * n))
    return mat


def _dct2_lowfreq(gray, n, keep=8):
    """对 n x n 灰度做二维 DCT，只计算左上 keep x keep 低频块。

    两个维度都只需前 keep 个基函数：行变换算 keep 列、列变换只对 keep 行，
    相比完整 n x n DCT 省约 (n/keep)^2 倍运算。
    """
    px = list(gray.getdata())
    mean = sum(px) / len(px)
    rows = [[px[y * n + x] - mean for x in range(n)] for y in range(n)]
    mat = _DCT_CACHE[n]
    basis = mat[:keep]
    tmp = [[0.0] * keep for _ in range(n)]
    for y in range(n):
        row = rows[y]
        for k in range(keep):
            dk = basis[k]
            tmp[y][k] = sum(dk[i] * row[i] for i in range(n))
    out = []
    for v in range(keep):
        dv = basis[v]
        line = []
        for u in range(keep):
            s = 0.0
            for y in range(n):
                s += dv[y] * tmp[y][u]
            line.append(s)
        out.append(line)
    return out


_DCT_CACHE = {32: _dct_matrix(32)}


def _bits_to_int(bits):
    v = 0
    for i, b in enumerate(bits):
        if b:
            v |= 1 << i
    return v


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def phash_int(img):
    """64 位 DCT pHash（整数位掩码）。32x32 DCT 的左上 8x8 低频，交流中位数为阈。"""
    return _phash_gray(_gray(img, (32, 32)))


def _phash_gray(g32):
    block = _dct2_lowfreq(g32, 32, 8)
    vals = [block[v][u] for v in range(8) for u in range(8)]
    ac = sorted(vals[1:])
    median = ac[len(ac) // 2]
    return _bits_to_int([1 if v > median else 0 for v in vals])


def dhash_int(img):
    """128 位差值哈希：9x8 水平邻列比较(64) + 8x9 垂直邻行比较(64)。"""
    gh = _gray(img, (9, 8))
    gv = _gray(img, (8, 9))
    return _dhash_grays(gh, gv)


def _dhash_grays(gh, gv):
    """从 9x8 / 8x9 灰度计算 128 位差值哈希（水平 64 + 垂直 64）。"""
    ph = list(gh.getdata())
    bits = [1 if ph[y * 9 + x + 1] > ph[y * 9 + x] else 0 for y in range(8) for x in range(8)]
    pv = list(gv.getdata())
    bits += [1 if pv[(y + 1) * 8 + x] > pv[y * 8 + x] else 0 for y in range(8) for x in range(8)]
    return _bits_to_int(bits)


def color_hist(img, bins_per_channel=4):
    """RGB 各通道 4 bin 的归一化直方图（64 维 list[float]）。

    在 64x64 缩略图上用 Image.histogram 的 C 实现统计，避免逐像素 Python 循环。
    """
    rgb = ImageOps.exif_transpose(img)
    if rgb.mode != "RGB":
        rgb = rgb.convert("RGB")
    return _color_hist_rgb(rgb.resize((64, 64), Image.Resampling.BILINEAR), bins_per_channel)


def _color_hist_rgb(rgb64, bins_per_channel=4):
    hist = rgb64.histogram()
    step = 256 // bins_per_channel
    total = 64.0 * 64 * 3  # 三通道合计归一化到 1，使直方图交落在 0..1
    out = []
    for ch in range(3):
        base = ch * 256
        for b in range(bins_per_channel):
            out.append(round(sum(hist[base + b * step:base + (b + 1) * step]) / total, 5))
    return out


def sharpness(img):
    """拉普拉斯方差作为清晰度指标（越大细节越丰富）。"""
    g = ImageOps.exif_transpose(img)
    if g.mode != "L":
        g = ImageOps.grayscale(g)
    g.thumbnail((128, 128), Image.Resampling.BILINEAR)
    return _sharpness_gray(g)


def _sharpness_gray(gray):
    """在已灰度化的图（任意尺寸）上算拉普拉斯方差，内部缩到最长边 128。"""
    g = gray.copy()
    g.thumbnail((128, 128), Image.Resampling.BILINEAR)
    lap = g.filter(ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0),
                                      scale=1, offset=128))
    data = list(lap.getdata())
    n = len(data)
    mean = sum(data) / n
    var = sum((v - mean) ** 2 for v in data) / n
    return round(var, 2)


def fingerprint(img):
    """计算单张图的全部查重指纹（pHash/dHash 在三个视口上各算一份）。

    只做一次 EXIF/灰度转换与方形化，三个中心视口都从同一张方形灰度导出，
    尺寸缩放走 Pillow C 路径。
    """
    base = ImageOps.exif_transpose(img)
    rgb = base if base.mode == "RGB" else base.convert("RGB")
    gray = base if base.mode == "L" else ImageOps.grayscale(base)
    w, h = gray.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    sq = gray.crop((left, top, left + side, top + side))

    ph, dh = [], []
    for scale in CROP_SCALES:
        view_gray = sq if scale >= 1.0 else _center_crop(sq, scale)
        g32 = view_gray.resize((32, 32), Image.Resampling.LANCZOS)
        gh = view_gray.resize((9, 8), Image.Resampling.LANCZOS)
        gv = view_gray.resize((8, 9), Image.Resampling.LANCZOS)
        ph.append(_phash_gray(g32))
        dh.append(_dhash_grays(gh, gv))
    return {
        "ph": ph,
        "dh": dh,
        "color": _color_hist_rgb(rgb.resize((64, 64), Image.Resampling.BILINEAR)),
        "sharpness": _sharpness_gray(sq),
    }


# ---------------------------------------------------------------------------
# 结构 / 颜色相似度
# ---------------------------------------------------------------------------
def _ph_sim(x, y):
    return 1.0 - ((x ^ y).bit_count() / PHASH_BITS)


def _dh_sim(x, y):
    return 1.0 - ((x ^ y).bit_count() / DHASH_BITS)


def struct_similarity(a, b):
    """两图结构相似度（0..1）：三视口两两 9 组配对取最佳，pHash 0.6 + dHash 0.4。

    取 max 是关键：A 的中心视口与 B 的全图视口相似，即说明 B 是 A 中心
    区域的放大副本（裁剪/翻拍场景）。
    """
    best = 0.0
    for x, u in zip(a["ph"], a["dh"]):
        for y, v in zip(b["ph"], b["dh"]):
            sp = _ph_sim(x, y)
            # pHash 已明显劣于当前最优时，该视口对不必再算 dHash
            if 0.6 * sp + 0.4 <= best:
                continue
            val = 0.6 * sp + 0.4 * _dh_sim(u, v)
            if val > best:
                best = val
    return best


def histogram_intersection(ca, cb):
    """颜色直方图交（0..1），对整体调色比逐 bin 差更宽容。"""
    return sum(min(a, b) for a, b in zip(ca, cb))


# ---------------------------------------------------------------------------
# LSH 粗筛（流式，不保存全量候选对）
# ---------------------------------------------------------------------------
def _perm_band_value(bits, perm, band):
    """从 64 位整数按置换表抽取第 band 个 8-bit 段。"""
    v = 0
    base = band * BAND_BITS
    for k in range(BAND_BITS):
        if bits & (1 << perm[base + k]):
            v |= 1 << k
    return v


def band_keys_for_view(ph, dh):
    """单个视口的全部 LSH 桶键（band_id, bucket_value）：pHash 24 段 + dHash 16 段。"""
    keys = []
    bid = 0
    for i in range(0, PHASH_BITS, BAND_BITS):
        keys.append((bid, (ph >> i) & BAND_MASK)); bid += 1
    for perm in (_PERM_1, _PERM_2):
        for band in range(PHASH_BITS // BAND_BITS):
            keys.append((bid, _perm_band_value(ph, perm, band))); bid += 1
    for i in range(0, DHASH_BITS, BAND_BITS):
        keys.append((bid, (dh >> i) & BAND_MASK)); bid += 1
    return keys


def stream_candidates(fps, gate, on_pair, should_continue=None, limit=None):
    """流式枚举共桶候选对并即时过结构闸口。

    fps: list[(id, fp)]；桶键为 (view, band_id, value)，不同视口分别成桶，
    任意「视口 × 分段」共桶即成为候选，随后用全部视口对的最佳结构相似度
    过闸。每发现一对 >= gate 的索引对回调 on_pair(i, j)。
    limit: 通过对上限，达到后提前结束（返回的第三项标记是否触顶）。
    全程只保留桶索引，不保存候选对。返回 (比较次数, 通过数, 是否触顶)。
    """
    buckets = {}
    seen = set()         # 已评估过的对（一对可能在多个 band 共桶，只算一次）
    compared = 0
    accepted = 0
    capped = False
    for idx, (_id, fp) in enumerate(fps):
        per_view = [band_keys_for_view(ph, dh) for ph, dh in zip(fp["ph"], fp["dh"])]
        hit_limit = limit is not None and accepted >= limit
        if not hit_limit:
            for view, keys in enumerate(per_view):
                for bid, val in keys:
                    bucket = buckets.setdefault((view, bid, val), [])
                    for other in bucket:
                        compared += 1
                        if compared & 0x3FFF == 0 and should_continue and not should_continue():
                            return compared, accepted, capped
                        key = (other, idx)
                        if key in seen:
                            continue
                        seen.add(key)
                        if struct_similarity(fps[other][1], fp) >= gate:
                            on_pair(other, idx)
                            accepted += 1
                            if limit is not None and accepted >= limit:
                                hit_limit = True
                                break
                    if hit_limit:
                        break
                if hit_limit:
                    break
        # 无论是否触顶，当前索引都要入桶（否则后续图无法与它共桶）
        for view, keys in enumerate(per_view):
            for bid, val in keys:
                buckets.setdefault((view, bid, val), []).append(idx)
        if hit_limit:
            capped = True
            if should_continue and not should_continue():
                break
    return compared, accepted, capped


# ---------------------------------------------------------------------------
# 边缘对齐精验
# ---------------------------------------------------------------------------
class _LRU:
    """最小 LRU 缓存（边缘向量复用，避免同一图被多次重载重算）。"""
    def __init__(self, capacity=512):
        self.capacity = capacity
        self.data = OrderedDict()

    def get(self, key):
        v = self.data.get(key)
        if v is not None:
            self.data.move_to_end(key)
        return v

    def put(self, key, value):
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.capacity:
            self.data.popitem(last=False)


def _edge_vectors(img, dim):
    """生成 3 个尺度（1.0 / 0.8 / 0.64）中心裁剪视图的归一化边缘向量。

    先统一裁成方形消除宽高比差异；中心裁剪模拟「翻拍时取景范围变小」，
    双方 3x3 组合即可覆盖 A 是 B 局部画面的对齐。
    """
    g = ImageOps.exif_transpose(img)
    if g.mode != "L":
        g = ImageOps.grayscale(g)
    w, h = g.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    g = g.crop((left, top, left + s, top + s)).resize((dim, dim), Image.Resampling.BILINEAR)
    g = g.filter(ImageFilter.GaussianBlur(0.8))  # 压掉 JPEG 块/传感器噪点，对齐更稳
    out = []
    for scale in (1.0, 0.8, 0.64):
        side = int(round(dim * scale))
        off = (dim - side) // 2
        view = g.crop((off, off, off + side, off + side)).resize((dim, dim), Image.Resampling.BILINEAR)
        edges = view.filter(ImageFilter.FIND_EDGES)
        vec = list(edges.getdata())
        n = len(vec)
        mean = sum(vec) / n
        z = [v - mean for v in vec]
        denom = math.sqrt(sum(v * v for v in z))
        out.append((z, denom))
    return out


def _zncc(za, da, zb, db):
    """零均值归一化互相关（-1..1，平坦无纹理返回 0）。"""
    if da < 1e-6 or db < 1e-6:
        return 0.0
    dot = 0
    for x, y in zip(za, zb):
        dot += x * y
    return dot / (da * db)


def edge_correlation(img_a, img_b, dim=None, cache=None, id_a=None, id_b=None):
    """两张图在多尺度中心裁剪下的最高边缘相关（裁剪对齐的关键）。"""
    dim = dim or config.DEDUP_EDGE_DIM

    def _vectors(key, img):
        if cache is not None and key is not None:
            hit = cache.get(key)
            if hit is not None:
                return hit
        vecs = _edge_vectors(img, dim)
        if cache is not None and key is not None:
            cache.put(key, vecs)
        return vecs

    va = _vectors(id_a, img_a)
    vb = _vectors(id_b, img_b)
    best = 0.0
    for za, da in va:
        for zb, db in vb:
            r = _zncc(za, da, zb, db)
            if r > best:
                best = r
    return max(0.0, best)


# ---------------------------------------------------------------------------
# 打分 / 分组
# ---------------------------------------------------------------------------
def score_pair(fp_a, fp_b, edge):
    """综合打分（0-100）。返回 (总分, 结构, 边缘, 颜色)。

    - 结构相似度低于 STRUCT_FLOOR 直接给 0：边缘/颜色无法把不同图拉过线；
    - 边缘、颜色分都以结构分为上界（仅小段容差），防止共享天空/地平线
      或主色调接近的图被误判；色调只给小幅加分。
    """
    struct = struct_similarity(fp_a, fp_b) * 100.0
    if struct < STRUCT_FLOOR:
        return (0.0, round(struct, 1), round(edge * 100.0, 1),
                round(histogram_intersection(fp_a["color"], fp_b["color"]) * 100.0, 1))
    color = histogram_intersection(fp_a["color"], fp_b["color"]) * 100.0
    edge_raw = edge * 100.0
    edge_used = min(edge_raw, struct + EDGE_BONUS_TOLERANCE)
    color_used = min(color, struct + COLOR_BONUS_TOLERANCE)
    total = W_STRUCT * struct + W_EDGE * edge_used + W_COLOR * color_used
    return (round(min(100.0, total), 1), round(struct, 1),
            round(edge_raw, 1), round(color, 1))


def kind_label(score, edge, color):
    """依据分数构成给出重复类型的中文标签。"""
    if score >= 97:
        return "同图副本"
    if edge >= 82:
        return "裁剪/翻拍"
    if color < 45:
        return "构图近似"
    return "调色/重压缩"


class _DSU:
    """并查集：把成对关系合并成相似组。"""
    def __init__(self, n):
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]:
            self.r[ra] += 1


def build_groups(items, pair_scores, records):
    """把过阈的成对分数合并成分组。

    items       : 有序 id 列表（与扫描时索引一致）
    pair_scores : {(i, j): (score, struct, edge, color)}
    records     : {id: 图像记录（width/height/size_bytes/sharpness）}
    返回按「可节省空间」降序的组列表，每组带 keeper 建议。
    """
    dsu = _DSU(len(items))
    for (i, j) in pair_scores:
        dsu.union(i, j)

    members = {}
    for i, iid in enumerate(items):
        members.setdefault(dsu.find(i), []).append(iid)

    groups = []
    for ids in members.values():
        if len(ids) < 2:
            continue
        idset = set(ids)
        edges = [
            {"a": items[i], "b": items[j], "score": sc[0],
             "struct": sc[1], "edge": sc[2], "color": sc[3]}
            for (i, j), sc in pair_scores.items()
            if items[i] in idset and items[j] in idset
        ]
        edges.sort(key=lambda e: e["score"], reverse=True)
        keeper = pick_keeper(ids, records)
        avg = round(sum(e["score"] for e in edges) / max(len(edges), 1), 1)
        best = max((e["score"] for e in edges), default=0)
        total_bytes = sum(records[i].get("size_bytes", 0) for i in ids)
        waste = total_bytes - records[keeper].get("size_bytes", 0)
        groups.append({
            "members": ids,
            "edges": edges,
            "keeper_id": keeper,
            "avg_score": avg,
            "max_score": best,
            "waste_bytes": waste,
            "kind": kind_label(best,
                               max((e["edge"] for e in edges), default=0),
                               min((e["color"] for e in edges), default=100)),
        })
    groups.sort(key=lambda g: g["waste_bytes"], reverse=True)
    return groups


def pick_keeper(ids, records):
    """选组内建议保留的一张：分辨率主导；与最大分辨率相差 15% 以内取更清晰。"""
    max_pixels = max(records[i].get("width", 0) * records[i].get("height", 0) for i in ids)
    eligible = [i for i in ids
                if records[i].get("width", 0) * records[i].get("height", 0) >= 0.85 * max_pixels]
    return max(eligible, key=lambda i: _quality_tuple(i, records[i]))


def _quality_tuple(iid, rec):
    return (rec.get("width", 0) * rec.get("height", 0),
            round(rec.get("sharpness", 0.0) or 0.0, 1),
            rec.get("size_bytes", 0), iid)
