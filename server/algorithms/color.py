"""颜色算法：灰度、反转、阈值、直方图均衡、通道平衡、色调分离、伪彩色。"""
from PIL import Image, ImageChops, ImageFilter, ImageOps

from . import util


def grayscale(image, params):
    """转灰度。mode: luma / average / channel。"""
    mode = params.get("mode", "luma")
    rgb = util.ensure_rgb(image)
    if mode == "average":
        r, g, b = rgb.split()
        avg = ImageChops.add(ImageChops.add(r, g), b).point(lambda v: v // 3)
        return avg.convert("L")
    if mode == "channel":
        ch = params.get("channel", "r")
        return rgb.split()[{"r": 0, "g": 1, "b": 2}.get(ch, 0)]
    return ImageOps.grayscale(rgb)


def invert(image, params):
    """反相。"""
    return ImageOps.invert(image)


def threshold(image, params):
    """二值化。mode: binary（全局）/ adaptive（局部均值阈值）。"""
    value = int(params.get("value", 127))
    mode = params.get("mode", "binary")
    gray = util.to_grayscale(image)
    if mode == "adaptive":
        block = int(params.get("block", 15))
        # 局部均值 = BoxBlur，再用原图减去局部均值，>0 判白
        local = gray.filter(ImageFilter.BoxBlur(block / 2.0))
        diff = ImageChops.subtract(gray, local)
        return diff.point(lambda v: 255 if v >= 0 else 0)
    return gray.point(lambda v: 255 if v >= value else 0)


def histogram_equalize(image, params):
    """直方图均衡。mode: luminance（仅亮度） / per_channel（分通道）。"""
    mode = params.get("mode", "luminance")
    rgb = util.ensure_rgb(image)
    if mode == "per_channel":
        r, g, b = rgb.split()
        return Image.merge("RGB", (ImageOps.equalize(r), ImageOps.equalize(g), ImageOps.equalize(b)))
    return ImageOps.equalize(rgb)


def color_balance(image, params):
    """通道增益。r/g/b ∈ [-100, 100]，映射为 0~2 倍增益。"""
    r_gain = 1.0 + float(params.get("r", 0)) / 100.0
    g_gain = 1.0 + float(params.get("g", 0)) / 100.0
    b_gain = 1.0 + float(params.get("b", 0)) / 100.0
    rgb = util.ensure_rgb(image)
    r, g, b = rgb.split()

    def _gain(band, factor):
        if abs(factor - 1.0) < 1e-6:
            return band
        return band.point(lambda v: util.clamp(v * factor))

    return Image.merge("RGB", (_gain(r, r_gain), _gain(g, g_gain), _gain(b, b_gain)))


def posterize(image, params):
    """色调分离，levels ∈ [2, 16]。"""
    levels = int(params.get("levels", 4))
    return ImageOps.posterize(util.ensure_rgb(image), max(2, min(8, levels)))


def _palette_lut(palette):
    """由一组颜色停靠点生成 256 项 RGB LUT。palette 是 [ (position, (r,g,b)), ... ]。"""
    lut = []
    stops = sorted(palette, key=lambda s: s[0])
    for i in range(256):
        pos = i / 255.0
        if pos <= stops[0][0]:
            lut.append(stops[0][1])
        elif pos >= stops[-1][0]:
            lut.append(stops[-1][1])
        else:
            for k in range(len(stops) - 1):
                p0, c0 = stops[k]
                p1, c1 = stops[k + 1]
                if p0 <= pos <= p1:
                    t = (pos - p0) / max(1e-6, p1 - p0)
                    lut.append(tuple(int(c0[j] + (c1[j] - c0[j]) * t) for j in range(3)))
                    break
    return lut


_FALSE_COLOR_PALETTES = {
    "thermal": [(0, (0, 0, 128)), (0.4, (255, 0, 0)), (0.7, (255, 255, 0)), (1, (255, 255, 255))],
    "rainbow": [(0, (75, 0, 130)), (0.33, (0, 0, 255)), (0.66, (0, 255, 0)), (1, (255, 0, 0))],
    "ocean": [(0, (0, 0, 0)), (0.5, (0, 64, 128)), (1, (0, 255, 255))],
    "fire": [(0, (0, 0, 0)), (0.5, (255, 0, 0)), (1, (255, 255, 0))],
}


def false_color(image, params):
    """伪彩色映射。palette ∈ thermal/rainbow/ocean/fire。"""
    palette = _FALSE_COLOR_PALETTES.get(params.get("palette", "thermal"), _FALSE_COLOR_PALETTES["thermal"])
    gray = util.to_grayscale(image)
    lut = _palette_lut(palette)
    try:
        return gray.point(lut, "RGB")
    except Exception:
        # 兜底：手动逐像素（低分辨率下）
        work = util.downscale_to_max(gray, 600)
        data = [lut[v] for v in work.getdata()]
        out = Image.new("RGB", work.size)
        out.putdata(data)
        return out.resize(gray.size, Image.Resampling.BILINEAR)


def solarize(image, params):
    """曝光过度（solarize）。threshold ∈ [0, 255]。"""
    th = int(params.get("threshold", 128))
    return ImageOps.solarize(util.ensure_rgb(image), th)
