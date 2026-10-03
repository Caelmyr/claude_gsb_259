"""几何变换：缩放、旋转、裁剪、翻转、边框。"""
from PIL import Image

from . import util


def resize(image, params):
    """缩放。mode: scale（倍率） / width（定宽） / height（定高）。"""
    mode = params.get("mode", "scale")
    w, h = image.size
    if mode == "width":
        target_w = int(params.get("width", w))
        ratio = target_w / float(w) if w else 1.0
        nh = max(1, int(round(h * ratio)))
        nw = max(1, target_w)
    elif mode == "height":
        target_h = int(params.get("height", h))
        ratio = target_h / float(h) if h else 1.0
        nw = max(1, int(round(w * ratio)))
        nh = max(1, target_h)
    else:
        scale = float(params.get("scale", 1.0))
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
    return image.resize((nw, nh), Image.Resampling.LANCZOS)


def rotate(image, params):
    """旋转。angle 度，expand 是否扩展画布。"""
    angle = float(params.get("angle", 0))
    expand = bool(params.get("expand", True))
    fill = tuple(params.get("fill", (255, 255, 255)))
    rgb = util.ensure_rgb(image)
    return rgb.rotate(angle, resample=Image.Resampling.BICUBIC, expand=expand, fillcolor=fill)


def crop(image, params):
    """裁剪。left/top/right/bottom ∈ [0,1] 相对坐标。"""
    w, h = image.size
    left = float(params.get("left", 0.0))
    top = float(params.get("top", 0.0))
    right = float(params.get("right", 1.0))
    bottom = float(params.get("bottom", 1.0))
    x0 = int(round(w * max(0.0, min(1.0, left))))
    y0 = int(round(h * max(0.0, min(1.0, top))))
    x1 = int(round(w * max(0.0, min(1.0, right))))
    y1 = int(round(h * max(0.0, min(1.0, bottom))))
    if x1 <= x0 or y1 <= y0:
        return image
    return image.crop((x0, y0, x1, y1))


def flip(image, params):
    """翻转。mode: horizontal / vertical。"""
    mode = params.get("mode", "horizontal")
    from PIL import Image as _Image
    if mode == "vertical":
        return image.transpose(_Image.Transpose.FLIP_TOP_BOTTOM)
    return image.transpose(_Image.Transpose.FLIP_LEFT_RIGHT)


def border(image, params):
    """加边框。width 像素，color 十六进制。"""
    width = int(params.get("width", 10))
    color = params.get("color", "#000000")
    from PIL import ImageOps
    return ImageOps.expand(util.ensure_rgb(image), border=max(0, width), fill=color)
