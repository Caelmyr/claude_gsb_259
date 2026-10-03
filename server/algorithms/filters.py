"""基础滤镜算法：亮度、对比度、饱和度、模糊、锐化、噪点、边缘。

简单调参类操作直接使用 Pillow 内置 C 实现（ImageEnhance / ImageFilter / ImageOps），
效率最高；边缘检测这类需要梯度幅值的则在低分辨率副本上自算。
"""
import random

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from . import util


def brightness(image, params):
    """亮度调整，amount ∈ [-100, 100]。"""
    amount = float(params.get("amount", 0))
    return ImageEnhance.Brightness(image).enhance(1.0 + amount / 100.0)


def contrast(image, params):
    """对比度调整，amount ∈ [-100, 100]。"""
    amount = float(params.get("amount", 0))
    return ImageEnhance.Contrast(image).enhance(1.0 + amount / 100.0)


def saturation(image, params):
    """饱和度调整，amount ∈ [-100, 100]。"""
    amount = float(params.get("amount", 0))
    rgb = util.ensure_rgb(image)
    return ImageEnhance.Color(rgb).enhance(1.0 + amount / 100.0)


def blur(image, params):
    """模糊。mode: gaussian / box / median。"""
    mode = params.get("mode", "gaussian")
    radius = float(params.get("radius", 2))
    radius = max(0.1, radius)
    if mode == "box":
        return image.filter(ImageFilter.BoxBlur(radius))
    if mode == "median":
        size = int(round(radius)) | 1  # 取奇数
        return image.filter(ImageFilter.MedianFilter(size=max(3, size)))
    return image.filter(ImageFilter.GaussianBlur(radius))


def sharpen(image, params):
    """锐化。amount ∈ [0, 100] 映射到 unsharp 强度。"""
    amount = float(params.get("amount", 30))
    factor = 0.3 + (amount / 100.0) * 4.0
    return image.filter(ImageFilter.UnsharpMask(radius=2, percent=int(factor * 100), threshold=2))


def noise(image, params):
    """加噪。mode: gaussian / saltpepper。"""
    mode = params.get("mode", "gaussian")
    amount = float(params.get("amount", 10)) / 100.0
    rgb = util.ensure_rgb(image)
    if mode == "saltpepper":
        w, h = rgb.size
        n = int(w * h * amount * 0.05)
        px = rgb.load()
        for _ in range(n):
            x, y = random.randrange(w), random.randrange(h)
            px[x, y] = (255, 255, 255) if random.random() < 0.5 else (0, 0, 0)
        return rgb
    # 高斯噪点：生成噪声图再按强度叠加
    sigma = int(max(1, amount * 80))
    noise_img = Image.effect_noise(rgb.size, sigma).convert("RGB")
    return Image.blend(rgb, noise_img, min(0.6, amount * 0.6))


def edges(image, params):
    """边缘检测。method: sobel / prewitt / laplacian / find。"""
    method = params.get("method", "sobel")
    strength = float(params.get("strength", 50)) / 100.0
    # 在低分辨率副本上算梯度，再放大回原尺寸，保证速度
    work = util.downscale_to_max(util.to_grayscale(image), 900)
    if method in ("sobel", "prewitt", "laplacian"):
        mag = util.gradient_magnitude(work, method)
    else:
        mag = work.filter(ImageFilter.FIND_EDGES)
    if strength < 0.999:
        mag = ImageEnhance.Contrast(mag).enhance(0.4 + strength * 1.6)
    mag = mag.resize(image.size, Image.Resampling.BILINEAR)
    if image.mode == "RGB":
        # 边缘图叠加回原图，做成描边效果
        return Image.blend(util.to_grayscale(image).convert("RGB"), mag.convert("RGB"), 0.5)
    return mag


def emboss(image, params):
    """浮雕。strength ∈ [0, 100]。"""
    strength = float(params.get("strength", 40)) / 100.0
    base = image.filter(ImageFilter.EMBOSS)
    return ImageEnhance.Contrast(base).enhance(0.5 + strength)
