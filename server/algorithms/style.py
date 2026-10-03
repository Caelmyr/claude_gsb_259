"""风格迁移（模拟）。

用经典风格化手段的组合来「模拟」风格迁移，无需训练模型：

- 色彩迁移（Reinhard）：把源图各通道的均值/方差对齐到目标风格的均值/方差。
- 纹理/笔触：浮雕卷积、边缘强化、色调分离、模糊（油画/水彩/蜡笔）。
- 明暗：暗角（vignette）、对比度、色偏（复古/赛博/黑银）。

每个风格是一个「构建函数 + 元信息」的注册项，前端可从 /api/styles 拉取预设列表。
"""
import math

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .. import config
from . import util


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _channel_transfer(img, tgt_mean, tgt_std):
    """Reinhard 色彩迁移：逐通道对齐均值/标准差到目标。"""
    rgb = util.ensure_rgb(img)
    src_stats = util.channel_stats(rgb)
    r, g, b = rgb.split()
    out_bands = []
    for i, band in enumerate((r, g, b)):
        sm, ss = src_stats[i]
        tm, ts = tgt_mean[i], tgt_std[i]
        if ss < 1e-6:
            ss = 1.0
        factor = ts / ss
        out_bands.append(band.point(lambda v, f=factor, m=sm, t=tm: util.clamp((v - m) * f + t)))
    return Image.merge("RGB", out_bands)


def _vignette(rgb, strength=0.5):
    """径向暗角。"""
    w, h = rgb.size
    cx, cy = w / 2.0, h / 2.0
    max_r = math.sqrt(cx * cx + cy * cy)
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    for r in range(int(max_r), 0, -1):
        alpha = int(255 * (1 - strength * (1 - r / max_r)))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=alpha)
    mask = mask.filter(ImageFilter.GaussianBlur(w / 30.0))
    black = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(rgb, black, mask)


def _sepia(rgb, amount=1.0):
    """复古棕褐色调。"""
    gray = util.to_grayscale(rgb)
    r = gray.point(lambda v: util.clamp(v * 1.12 * amount))
    g = gray.point(lambda v: util.clamp(v * 0.95 * amount))
    b = gray.point(lambda v: util.clamp(v * 0.72 * amount))
    return Image.merge("RGB", (r, g, b))


def _pencil(gray, strength=1.0):
    """铅笔/素描：边缘反相。"""
    edges = gray.filter(ImageFilter.FIND_EDGES)
    inverted = ImageOps.invert(edges)
    if strength < 0.999:
        inverted = ImageEnhance.Contrast(inverted).enhance(0.5 + strength)
    return inverted.convert("RGB")


def _emboss_overlay(img, strength=0.5):
    """浮雕纹理叠加。"""
    emb = img.filter(ImageFilter.EMBOSS).convert("RGB")
    return Image.blend(img, emb, strength)


def _posterize(img, levels):
    return ImageOps.posterize(img, levels)


# ---------------------------------------------------------------------------
# 风格注册表
# ---------------------------------------------------------------------------
STYLES = {}


def _register(name, description, builder):
    STYLES[name] = {"name": name, "description": description, "builder": builder}


def _build_oil(img, params):
    base = _channel_transfer(img, (150, 120, 80), (60, 55, 45))
    base = base.filter(ImageFilter.GaussianBlur(1.0))
    base = _emboss_overlay(base, 0.35)
    return ImageEnhance.Color(base).enhance(1.25)


def _build_watercolor(img, params):
    base = _channel_transfer(img, (210, 215, 225), (45, 45, 50))
    base = base.filter(ImageFilter.GaussianBlur(1.6))
    base = ImageEnhance.Brightness(base).enhance(1.08)
    return _posterize(base, 6)


def _build_sketch(img, params):
    gray = util.to_grayscale(img)
    return _pencil(gray, 0.9)


def _build_vintage(img, params):
    base = ImageEnhance.Contrast(img).enhance(0.9)
    base = ImageEnhance.Color(base).enhance(0.7)
    base = _sepia(base, 0.9)
    return _vignette(base, 0.45)


def _build_comic(img, params):
    base = _posterize(img, 5)
    base = ImageEnhance.Color(base).enhance(1.5)
    edges = util.to_grayscale(img).filter(ImageFilter.FIND_EDGES).point(lambda v: 0 if v < 60 else 255)
    base = Image.composite(Image.new("RGB", img.size, (0, 0, 0)), base, edges)
    return ImageEnhance.Contrast(base).enhance(1.15)


def _build_cyber(img, params):
    base = _channel_transfer(img, (60, 90, 160), (70, 70, 70))
    base = ImageEnhance.Color(base).enhance(1.6)
    base = ImageEnhance.Contrast(base).enhance(1.3)
    # 霓虹边缘辉光（蓝紫）
    edges = util.gradient_magnitude(util.downscale_to_max(img, 600), "sobel")
    edges = edges.resize(img.size, Image.Resampling.BILINEAR)
    glow = Image.merge("RGB", (
        edges.point(lambda v: v),
        edges.point(lambda v: v // 2),
        edges,
    ))
    return Image.blend(base, glow, 0.35)


def _build_crayon(img, params):
    base = _posterize(img, 7)
    base = ImageEnhance.Color(base).enhance(1.3)
    return _emboss_overlay(base, 0.45)


def _build_noir(img, params):
    gray = util.to_grayscale(img)
    base = ImageEnhance.Contrast(gray).enhance(1.5)
    return _vignette(base.convert("RGB"), 0.6)


def _build_autumn(img, params):
    base = _channel_transfer(img, (190, 150, 90), (60, 50, 40))
    base = ImageEnhance.Color(base).enhance(1.2)
    return _vignette(base, 0.3)


_register("oil", "油画：浓郁笔触、暖色调与浮雕纹理", _build_oil)
_register("watercolor", "水彩：柔和晕染、低饱和淡彩", _build_watercolor)
_register("sketch", "素描：铅笔线条勾勒", _build_sketch)
_register("vintage", "复古：棕褐怀旧 + 暗角", _build_vintage)
_register("comic", "漫画：色块化 + 粗描边", _build_comic)
_register("cyber", "赛博朋克：蓝紫霓虹辉光", _build_cyber)
_register("crayon", "蜡笔：色粉笔触与颗粒质感", _build_crayon)
_register("noir", "黑银：高反差黑白电影", _build_noir)
_register("autumn", "秋日：暖橙金黄的季节感", _build_autumn)


def list_styles():
    """返回前端可用的风格预设列表（不含 builder）。"""
    return [
        {"name": name, "description": s["description"]}
        for name, s in STYLES.items()
    ]


def apply(image, params):
    """应用风格迁移，返回 {image, style, description}。"""
    style_name = params.get("style", "oil")
    strength = float(params.get("strength", 100)) / 100.0
    style = STYLES.get(style_name, STYLES["oil"])

    work = util.downscale_to_max(util.ensure_rgb(image), config.PREVIEW_DIM)
    result = style["builder"](work, params)
    if strength < 0.999:
        result = Image.blend(util.ensure_rgb(work), result, strength)
    result = result.resize(image.size, Image.Resampling.LANCZOS)

    return {
        "style": style_name,
        "description": style["description"],
        "image": result,
    }
