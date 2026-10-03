"""目标检测（模拟）与标注辅助。

无深度学习情况下的经典显著性检测替代：

- saliency：原图与高斯模糊之差 -> 显著区域
- color    ：HSV 饱和度通道作为「物体突出度」
- edge     ：梯度幅值密度 -> 边缘密集区域（纹理/物体）

对显著性图做 Otsu 阈值 -> 连通域 -> 外接框 -> 非极大值抑制（NMS），
给每个框打上基于主色的粗标签与置信度。坐标映射回原图。
"""
import colorsys
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .. import config
from . import util


def _saliency_map(img, method):
    """计算显著性图（灰度）。"""
    if method == "color":
        hsv = util.ensure_rgb(img).convert("HSV")
        return hsv.split()[1]  # S 通道
    if method == "edge":
        gray = util.downscale_to_max(util.to_grayscale(img), 500)
        mag = util.gradient_magnitude(gray, "sobel")
        return mag.resize(img.size, Image.Resampling.BILINEAR)
    # saliency（默认）：原图 vs 高斯模糊之差
    rgb = util.ensure_rgb(img)
    blur = rgb.filter(ImageFilter.GaussianBlur(max(3, min(rgb.size) // 24)))
    diff = ImageChops.difference(rgb, blur)
    return util.to_grayscale(diff)


def _otsu_threshold(hist):
    """灰度直方图 Otsu 阈值。"""
    total = sum(hist)
    if total == 0:
        return 127
    sum_all = sum(i * c for i, c in enumerate(hist))
    sum_bg = 0.0
    weight_bg = 0.0
    best_thr = 127
    best_var = -1.0
    for t in range(256):
        weight_bg += hist[t]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += t * hist[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        var = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var > best_var:
            best_var = var
            best_thr = t
    return best_thr


def _iou(a, b):
    """两个 [x0,y0,x1,y1] 框的 IoU。"""
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / float(area_a + area_b - inter)


def _nms(boxes, threshold=0.5):
    """按 score 降序做 NMS，boxes: [{box:[...], score}]。"""
    boxes = sorted(boxes, key=lambda b: b["score"], reverse=True)
    kept = []
    for b in boxes:
        if all(_iou(b["box"], k["box"]) < threshold for k in kept):
            kept.append(b)
    return kept


def _color_name(mean_rgb):
    r, g, b = mean_rgb
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if s < 0.15:
        return "白色" if v > 0.6 else "深色"
    hue = h * 360
    names = [
        (15, "红色"), (45, "橙色"), (70, "黄色"), (160, "绿色"),
        (200, "青色"), (260, "蓝色"), (300, "紫色"), (345, "粉色"), (360, "红色"),
    ]
    for th, name in names:
        if hue < th:
            return name
    return "彩色"


def detect(image, params):
    """目标检测模拟。返回 boxes + 覆盖层图像。"""
    method = params.get("method", "saliency")
    max_boxes = int(params.get("max_boxes", 20))
    thr = params.get("threshold", None)  # None => Otsu 自动
    min_size = float(params.get("min_size", 0.02))  # 相对原图的最小框占比

    orig = util.ensure_rgb(image)
    work = util.downscale_to_max(orig, config.FEATURE_WORK_DIM)
    ratio = util.scale_ratio(orig.size, work.size)

    sal = _saliency_map(work, method)
    w, h, sal_rows = util.gray_matrix(sal)

    hist = sal.histogram()
    otsu = _otsu_threshold(hist)
    thr_val = int(thr) if thr is not None else otsu

    # 二值化 + 连通域
    _, components = util.connected_components(sal_rows, w, h, threshold=thr_val)
    min_area = (w * h) * (min_size ** 2) * 0.25
    boxes = []
    for label, pts in components.items():
        x0, y0, x1, y1 = util.points_bbox(pts)
        if (x1 - x0) * (y1 - y0) < min_area:
            continue
        # 框内显著性均值作为置信度
        s = sum(sal_rows[py][px] for px, py in pts) / float(len(pts))
        boxes.append({"box": [x0, y0, x1, y1], "score": s})

    boxes = _nms(boxes)[:max_boxes]
    max_score = max((b["score"] for b in boxes), default=1.0) or 1.0

    result_boxes = []
    for b in boxes:
        x0, y0, x1, y1 = b["box"]
        score = min(1.0, b["score"] / float(max_score))
        # 主色标签
        crop = work.crop((x0, y0, x1, y1))
        mean_rgb = tuple(int(v) for v in _mean_color(crop))
        label = _color_name(mean_rgb)
        result_boxes.append({
            "x": int(round(x0 * ratio)), "y": int(round(y0 * ratio)),
            "w": int(round((x1 - x0) * ratio)), "h": int(round((y1 - y0) * ratio)),
            "score": round(score, 3),
            "label": label,
            "mean_color": mean_rgb,
        })

    overlay = draw_boxes(orig, result_boxes)
    return {
        "method": method,
        "threshold_used": thr_val,
        "count": len(result_boxes),
        "boxes": result_boxes,
        "image": overlay,
    }


def _mean_color(img):
    """返回图像的平均 RGB。"""
    rgb = util.ensure_rgb(img).resize((1, 1), Image.Resampling.BILINEAR)
    return rgb.getpixel((0, 0))


_PALETTE = [(255, 82, 82), (33, 150, 243), (255, 193, 7), (76, 175, 80),
            (156, 39, 176), (0, 188, 212), (255, 87, 34), (139, 195, 74)]


def draw_boxes(image, boxes):
    """画检测框 + 标签 + 置信度。"""
    img = util.ensure_rgb(image).copy()
    draw = ImageDraw.Draw(img)
    for i, b in enumerate(boxes):
        color = _PALETTE[i % len(_PALETTE)]
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        draw.rectangle([x, y, x + w, y + h], outline=color, width=max(2, min(img.size) // 200))
        label = f"{b.get('label', 'object')} {b.get('score', 0):.2f}"
        tw = len(label) * 7
        draw.rectangle([x, max(0, y - 18), x + tw + 6, y], fill=color)
        draw.text((x + 3, max(0, y - 16)), label, fill=(255, 255, 255))
    return img


def draw_annotations(image, annotations):
    """画人工标注（存储的 box/label/color）。"""
    img = util.ensure_rgb(image).copy()
    draw = ImageDraw.Draw(img)
    for ann in annotations:
        box = ann.get("box")
        if not box or len(box) != 4:
            continue
        x, y, w, h = [int(v) for v in box]
        color = ann.get("color", "#ff5252")
        draw.rectangle([x, y, x + w, y + h], outline=color, width=max(2, min(img.size) // 200))
        label = ann.get("label", "")
        if label:
            draw.rectangle([x, max(0, y - 16), x + len(label) * 7 + 6, y], fill=color)
            draw.text((x + 3, max(0, y - 14)), label, fill=(255, 255, 255))
    return img
