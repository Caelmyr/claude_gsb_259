"""生成演示用测试图像（干净的一次性初始化）。

运行：python tests/make_fixtures.py
会在 data/ 下生成 4 张内容不同的演示图（渐变、几何图形、纹理、圆点阵列），
便于首次打开前端时立刻有图可用。不会产生历史/结果记录。
"""
import io
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from server import config  # noqa: E402
from server.image_store import ImageStore  # noqa: E402


def _gradient(w=640, h=480):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (int(x * 255 / w), int(y * 255 / h), int((x + y) * 255 / (w + h)))
    return img


def _shapes(w=640, h=480):
    img = Image.new("RGB", (w, h), (245, 245, 245))
    d = ImageDraw.Draw(img)
    rnd = random.Random(42)
    for i in range(16):
        x, y = rnd.randrange(w), rnd.randrange(h)
        r = rnd.randrange(30, 90)
        color = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        kind = i % 4
        if kind == 0:
            d.ellipse([x, y, x + r, y + r], fill=color)
        elif kind == 1:
            d.rectangle([x, y, x + r, y + r], fill=color)
        elif kind == 2:
            d.polygon([(x, y), (x + r, y), (x + r // 2, y + r)], fill=color)
        else:
            d.line([x, y, x + r, y + r], fill=color, width=6)
    return img


def _texture(w=640, h=480):
    return _gradient(w, h).filter(ImageFilter.EMBOSS).filter(ImageFilter.EDGE_ENHANCE)


def _dots(w=640, h=480):
    img = Image.new("RGB", (w, h), (20, 24, 32))
    d = ImageDraw.Draw(img)
    rnd = random.Random(7)
    for _ in range(220):
        x, y = rnd.randrange(w), rnd.randrange(h)
        r = rnd.randrange(3, 12)
        c = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        d.ellipse([x - r, y - r, x + r, y + r], fill=c)
    return img


def _bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main():
    config.ensure_dirs()
    store = ImageStore()
    fixtures = [
        ("gradient.png", _gradient()),
        ("shapes.png", _shapes()),
        ("texture.png", _texture()),
        ("dots.png", _dots()),
    ]
    created = 0
    for name, img in fixtures:
        rec = store.save_upload(_bytes(img), name)
        if rec.get("filename") == name:
            created += 1
        print(f"  {name} -> {rec['width']}x{rec['height']} (id={rec['id'][:12]}…)")
    print(f"\n已就绪 {created} 张演示图，启动 app.py 即可在浏览器中使用。")


if __name__ == "__main__":
    main()
