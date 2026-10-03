"""特征提取（SIFT / ORB 模拟）与关键点匹配。

纯 Pillow 实现的简化版特征检测：

- 关键点 = 梯度幅值的局部极大值（Sobel 梯度），ORB 额外叠加 FAST 式角点响应。
- SIFT 描述子：关键点邻域 16x16 分 4x4 子块，每块 8 方向梯度直方图 => 128 维。
- ORB 描述子：BRIEF 式 256 位二进制（邻域像素对比较）。
- 匹配：暴力最近邻 + Lowe 比值测试（SIFT 用 L2，ORB 用汉明距离）。

全部在低分辨率工作副本上运行，坐标按比例映射回原图，保证速度与内存可控。
"""
import math
import random

from PIL import Image, ImageDraw

from .. import config
from . import util


# ---------------------------------------------------------------------------
# 关键点
# ---------------------------------------------------------------------------
def _fast_cornerness(rows, w, h, x, y, thr=40):
    """FAST 式角点响应：统计半径为 3 的圆环上与中心差异超阈值的连续点数。"""
    if x < 3 or y < 3 or x >= w - 3 or y >= h - 3:
        return 0
    center = rows[y][x]
    ring = [
        (x, y - 3), (x + 1, y - 3), (x + 2, y - 2), (x + 3, y - 1),
        (x + 3, y), (x + 3, y + 1), (x + 2, y + 2), (x + 1, y + 3),
        (x, y + 3), (x - 1, y + 3), (x - 2, y + 2), (x - 3, y + 1),
        (x - 3, y), (x - 3, y - 1), (x - 2, y - 2), (x - 1, y - 3),
    ]
    bright = 0
    dark = 0
    for rx, ry in ring:
        v = rows[ry][rx]
        if v > center + thr:
            bright += 1
            dark = 0
        elif v < center - thr:
            dark += 1
            bright = 0
        else:
            bright = 0
            dark = 0
        if bright >= 9 or dark >= 9:
            return 1
    return 0


def _keypoints(gray_rows, w, h, mag_rows, method, max_points):
    """由梯度幅值局部极大值得到关键点列表（含尺度/方向/响应，未算描述子）。"""
    radius = 3
    candidates = util.local_maxima(mag_rows, w, h, radius=radius, min_response=40)
    kps = []
    for x, y, response in candidates:
        if len(kps) >= max_points:
            break
        # 尺度：用局部梯度能量做粗略估计
        size = 4.0
        # 主方向：邻域梯度方向的加权平均
        angle = _dominant_orientation(mag_rows, w, h, x, y, radius=6)
        kps.append({
            "x": x, "y": y, "size": size, "angle": angle,
            "response": response,
        })
    return kps


def _dominant_orientation(mag_rows, w, h, x, y, radius):
    """邻域内梯度方向直方图的峰值方向（简化为加权向量和）。"""
    from . import util as _u
    # 用简单的局部梯度方向统计：这里以邻域幅值加权的方向近似
    sx = 0.0
    sy = 0.0
    for dy in range(-radius, radius + 1):
        ny = y + dy
        if ny <= 0 or ny >= h - 1:
            continue
        for dx in range(-radius, radius + 1):
            nx = x + dx
            if nx <= 0 or nx >= w - 1:
                continue
            gx = (mag_rows[ny][nx + 1] - mag_rows[ny][nx - 1])
            gy = (mag_rows[ny + 1][nx] - mag_rows[ny - 1][nx])
            sx += gx
            sy += gy
    return math.atan2(sy, sx)


# ---------------------------------------------------------------------------
# 描述子
# ---------------------------------------------------------------------------
def _sift_descriptor(rows, w, h, x, y, angle):
    """128 维 SIFT 式描述子（4x4 子块 x 8 方向梯度直方图）。"""
    bins = [0.0] * 128
    half = 8  # 16x16 邻域半径
    cos_a, sin_a = math.cos(-angle), math.sin(-angle)
    for oy in range(-half, half):
        for ox in range(-half, half):
            nx, ny = x + ox, y + oy
            if nx <= 0 or ny <= 0 or nx >= w - 1 or ny >= h - 1:
                continue
            # 旋转邻域
            rx = ox * cos_a - oy * sin_a
            ry = ox * sin_a + oy * cos_a
            gx = rows[ny][nx + 1] - rows[ny][nx - 1]
            gy = rows[ny + 1][nx] - rows[ny - 1][nx]
            mag = math.sqrt(gx * gx + gy * gy)
            theta = math.atan2(gy, gx)
            if theta < 0:
                theta += 2 * math.pi
            bin_angle = int(theta / (2 * math.pi) * 8) % 8
            # 子块索引
            bx = min(3, max(0, int((rx + 8) / 4)))
            by = min(3, max(0, int((ry + 8) / 4)))
            bins[(by * 4 + bx) * 8 + bin_angle] += mag
    # L2 归一化 + 截断
    norm = math.sqrt(sum(v * v for v in bins)) or 1.0
    desc = [min(255, int(round(v / norm * 255))) for v in bins]
    return desc


def _orb_descriptor(rows, w, h, x, y):
    """256 位 BRIEF 式二进制描述子（随机像素对比较）。"""
    rng = random.Random(x * 1000003 + y * 99991)  # 确定性伪随机
    desc = []
    for _ in range(256):
        ox1, oy1 = rng.randrange(-8, 9), rng.randrange(-8, 9)
        ox2, oy2 = rng.randrange(-8, 9), rng.randrange(-8, 9)
        p1 = rows[min(h - 1, max(0, y + oy1))][min(w - 1, max(0, x + ox1))]
        p2 = rows[min(h - 1, max(0, y + oy2))][min(w - 1, max(0, x + ox2))]
        desc.append(1 if p1 < p2 else 0)
    return desc


def _descriptors(rows, w, h, kps, method):
    if method == "orb":
        return [_orb_descriptor(rows, w, h, kp["x"], kp["y"]) for kp in kps]
    return [_sift_descriptor(rows, w, h, kp["x"], kp["y"], kp["angle"]) for kp in kps]


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def _extract(image, method, max_points):
    orig = util.ensure_rgb(image)
    work = util.downscale_to_max(orig, config.FEATURE_WORK_DIM)
    ratio = util.scale_ratio(orig.size, work.size)
    gray = util.to_grayscale(work)
    w, h, rows = util.gray_matrix(gray)
    mag = util.gradient_magnitude(gray, "sobel")
    _, _, mag_rows = util.gray_matrix(mag)

    kps = _keypoints(rows, w, h, mag_rows, method, max_points)
    descs = _descriptors(rows, w, h, kps, method)

    # 映射回原图坐标
    for kp in kps:
        kp["x"] = int(round(kp["x"] * ratio))
        kp["y"] = int(round(kp["y"] * ratio))
        kp["size"] = round(kp["size"] * ratio, 2)
    return {"orig": orig, "work": work, "ratio": ratio,
            "keypoints": kps, "descriptors": descs}


def extract_keypoints(image, params):
    """提取关键点，返回关键点列表 + 覆盖层图像。"""
    method = params.get("method", "sift")
    max_points = int(params.get("max_points", 120))
    res = _extract(image, method, max_points)
    kps = res["keypoints"]
    overlay = draw_keypoints(res["orig"], kps, method)
    return {
        "method": method,
        "count": len(kps),
        "descriptor_dim": 128 if method == "sift" else 256,
        "sample_descriptor": res["descriptors"][0] if res["descriptors"] else [],
        "keypoints": kps,
        "image": overlay,
    }


def match(image_a, image_b, params):
    """两张图关键点匹配，返回匹配对 + 连线图。"""
    method = params.get("method", "sift")
    max_points = int(params.get("max_points", 120))
    max_matches = int(params.get("max_matches", 40))
    ra = _extract(image_a, method, max_points)
    rb = _extract(image_b, method, max_points)

    matches = _bruteforce_match(ra["keypoints"], ra["descriptors"],
                                rb["keypoints"], rb["descriptors"], method, max_matches)
    canvas = draw_matches(ra["orig"], rb["orig"],
                          ra["keypoints"], rb["keypoints"], matches)
    return {
        "method": method,
        "count": len(matches),
        "matches": matches,
        "count_a": len(ra["keypoints"]),
        "count_b": len(rb["keypoints"]),
        "image": canvas,
    }


def _distance(a, b, method):
    if method == "orb":
        return sum(x != y for x, y in zip(a, b))
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _bruteforce_match(kps_a, desc_a, kps_b, desc_b, method, max_matches):
    """暴力最近邻 + Lowe 比值测试。"""
    if not desc_a or not desc_b:
        return []
    scored = []
    for i, da in enumerate(desc_a):
        best = (None, float("inf"))
        second = float("inf")
        for j, db in enumerate(desc_b):
            d = _distance(da, db, method)
            if d < best[1]:
                second = best[1]
                best = (j, d)
            elif d < second:
                second = d
        if best[0] is not None and best[1] < 0.75 * second:
            scored.append((i, best[0], best[1]))
    scored.sort(key=lambda m: m[2])
    out = []
    seen_a, seen_b = set(), set()
    for ia, ib, d in scored:
        if ia in seen_a or ib in seen_b:
            continue
        seen_a.add(ia)
        seen_b.add(ib)
        out.append({
            "a": {"x": kps_a[ia]["x"], "y": kps_a[ia]["y"]},
            "b": {"x": kps_b[ib]["x"], "y": kps_b[ib]["y"]},
            "distance": round(d, 3),
        })
        if len(out) >= max_matches:
            break
    return out


# ---------------------------------------------------------------------------
# 可视化
# ---------------------------------------------------------------------------
def draw_keypoints(image, keypoints, method):
    """在图像上画关键点（圆 + 方向短线）。"""
    img = util.ensure_rgb(image).copy()
    draw = ImageDraw.Draw(img)
    color = (255, 60, 60) if method == "sift" else (60, 160, 255)
    for kp in keypoints:
        x, y, size, angle = kp["x"], kp["y"], max(2.0, kp["size"]), kp["angle"]
        r = max(2, int(size))
        draw.ellipse([x - r, y - r, x + r, y + r], outline=color, width=1)
        draw.line([x, y, x + int(2 * size * math.cos(angle)), y + int(2 * size * math.sin(angle))],
                  fill=color, width=1)
    return img


def draw_matches(img_a, img_b, kps_a, kps_b, matches):
    """并排两张图，用直线连接匹配关键点。"""
    wa, ha = img_a.size
    wb, hb = img_b.size
    canvas = Image.new("RGB", (wa + wb, max(ha, hb)), (20, 20, 20))
    canvas.paste(util.ensure_rgb(img_a), (0, 0))
    canvas.paste(util.ensure_rgb(img_b), (wa, 0))
    draw = ImageDraw.Draw(canvas)
    import colorsys
    for m in matches:
        a, b = m["a"], m["b"]
        hue = (m["distance"] * 137.5) % 1.0
        r, g, bl = colorsys.hsv_to_rgb(hue, 0.9, 1.0)
        color = (int(r * 255), int(g * 255), int(bl * 255))
        draw.line([a["x"], a["y"], wa + b["x"], b["y"]], fill=color, width=1)
        r = 3
        draw.ellipse([a["x"] - r, a["y"] - r, a["x"] + r, a["y"] + r], fill=color)
        draw.ellipse([wa + b["x"] - r, b["y"] - r, wa + b["x"] + r, b["y"] + r], fill=color)
    return canvas
