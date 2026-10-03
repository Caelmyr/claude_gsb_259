"""节点注册表：声明式定义所有流水线节点类型。

每个节点 = 类型标识 + 中文名 + 分类 + 参数 schema + 默认参数 + 输入约束 + 处理函数。
前端滤镜链编辑器通过 /api/nodes 拉取这份 schema 来生成节点面板与参数表单；
流水线引擎根据 min_inputs/max_inputs 做 DAG 校验，根据 defaults 补全参数。
"""
from .algorithms import color as _color
from .algorithms import detection as _detection
from .algorithms import features as _features
from .algorithms import filters as _filters
from .algorithms import geometry as _geometry
from .algorithms import segmentation as _segmentation
from .algorithms import style as _style


def _range(key, label, min_v, max_v, step=1, default=0, desc=""):
    return {"key": key, "label": label, "type": "range", "min": min_v, "max": max_v,
            "step": step, "default": default, "desc": desc}


def _select(key, label, options, default=None, desc=""):
    return {"key": key, "label": label, "type": "select", "options": options,
            "default": default if default is not None else options[0], "desc": desc}


def _bool(key, label, default=False, desc=""):
    return {"key": key, "label": label, "type": "bool", "default": default, "desc": desc}


def _number(key, label, default=0, desc=""):
    return {"key": key, "label": label, "type": "number", "default": default, "desc": desc}


def _pick_color(key, label, default="#000000", desc=""):
    return {"key": key, "label": label, "type": "color", "default": default, "desc": desc}


def _wrap(fn):
    """把返回 Image 或 dict 的算法函数包装成节点 handler(image, params, meta) -> (image, meta)。"""
    def handler(image, params, meta):
        out = fn(image, params)
        if isinstance(out, dict):
            img = out.get("image", image)
            extra = {k: v for k, v in out.items() if k != "image"}
            new_meta = dict(meta)
            new_meta.update(extra)
            return img, new_meta
        return out, meta
    return handler


def _node(ntype, label, category, schema, fn, default_overrides=None, desc="",
          min_inputs=0, max_inputs=1):
    defaults = {p["key"]: p.get("default", 0) for p in schema}
    if default_overrides:
        defaults.update(default_overrides)
    return {
        "type": ntype, "label": label, "category": category, "desc": desc,
        "schema": schema, "defaults": defaults, "min_inputs": min_inputs,
        "max_inputs": max_inputs, "handler": _wrap(fn),
    }


# ---------------------------------------------------------------------------
# 节点定义
# ---------------------------------------------------------------------------
NODES = {}

NODES["brightness"] = _node(
    "brightness", "亮度", "滤镜",
    [_range("amount", "亮度", -100, 100, 1, 0, "正数提亮，负数压暗")],
    _filters.brightness, desc="调整整体亮度")

NODES["contrast"] = _node(
    "contrast", "对比度", "滤镜",
    [_range("amount", "对比度", -100, 100, 1, 0)],
    _filters.contrast, desc="调整明暗对比")

NODES["saturation"] = _node(
    "saturation", "饱和度", "滤镜",
    [_range("amount", "饱和度", -100, 100, 1, 0)],
    _filters.saturation, desc="调整色彩饱和度")

NODES["blur"] = _node(
    "blur", "模糊", "滤镜",
    [_select("mode", "模式", ["gaussian", "box", "median"]),
     _range("radius", "半径", 0, 20, 0.5, 2)],
    _filters.blur, desc="高斯/均值/中值模糊")

NODES["sharpen"] = _node(
    "sharpen", "锐化", "滤镜",
    [_range("amount", "强度", 0, 100, 1, 30)],
    _filters.sharpen, desc="USM 锐化")

NODES["noise"] = _node(
    "noise", "噪点", "滤镜",
    [_select("mode", "模式", ["gaussian", "saltpepper"]),
     _range("amount", "强度", 0, 100, 1, 10)],
    _filters.noise, desc="添加高斯/椒盐噪声")

NODES["edges"] = _node(
    "edges", "边缘检测", "滤镜",
    [_select("method", "算子", ["sobel", "prewitt", "laplacian", "find"]),
     _range("strength", "强度", 0, 100, 1, 50)],
    _filters.edges, desc="Sobel/Prewitt/Laplacian 边缘")

NODES["emboss"] = _node(
    "emboss", "浮雕", "滤镜",
    [_range("strength", "强度", 0, 100, 1, 40)],
    _filters.emboss, desc="浮雕纹理")

NODES["grayscale"] = _node(
    "grayscale", "灰度化", "颜色",
    [_select("mode", "方式", ["luma", "average", "channel"]),
     _select("channel", "通道", ["r", "g", "b"])],
    _color.grayscale, desc="转灰度")

NODES["invert"] = _node(
    "invert", "反相", "颜色", [], _color.invert, desc="颜色取反")

NODES["threshold"] = _node(
    "threshold", "二值化", "颜色",
    [_select("mode", "模式", ["binary", "adaptive"]),
     _range("value", "阈值", 0, 255, 1, 127),
     _range("block", "局部块", 3, 31, 2, 15)],
    _color.threshold, desc="全局/自适应阈值")

NODES["histogram_equalize"] = _node(
    "histogram_equalize", "直方图均衡", "颜色",
    [_select("mode", "方式", ["luminance", "per_channel"])],
    _color.histogram_equalize, desc="增强对比度的直方图均衡")

NODES["color_balance"] = _node(
    "color_balance", "色彩平衡", "颜色",
    [_range("r", "红", -100, 100, 1, 0), _range("g", "绿", -100, 100, 1, 0),
     _range("b", "蓝", -100, 100, 1, 0)],
    _color.color_balance, desc="三通道增益")

NODES["posterize"] = _node(
    "posterize", "色调分离", "颜色",
    [_range("levels", "色阶数", 2, 8, 1, 4)],
    _color.posterize, desc="减少颜色层级")

NODES["false_color"] = _node(
    "false_color", "伪彩色", "颜色",
    [_select("palette", "色板", ["thermal", "rainbow", "ocean", "fire"])],
    _color.false_color, desc="灰度映射为彩色")

NODES["solarize"] = _node(
    "solarize", "曝光过度", "颜色",
    [_range("threshold", "阈值", 0, 255, 1, 128)],
    _color.solarize, desc="solarize 效果")

NODES["resize"] = _node(
    "resize", "缩放", "几何",
    [_select("mode", "方式", ["scale", "width", "height"]),
     _range("scale", "倍率", 0.1, 3.0, 0.05, 1.0),
     _number("width", 800), _number("height", 600)],
    _geometry.resize, desc="缩放图像")

NODES["rotate"] = _node(
    "rotate", "旋转", "几何",
    [_range("angle", "角度", -180, 180, 1, 0), _bool("expand", "扩展画布", True)],
    _geometry.rotate, desc="旋转图像")

NODES["crop"] = _node(
    "crop", "裁剪", "几何",
    [_range("left", "左", 0, 1, 0.01, 0), _range("top", "上", 0, 1, 0.01, 0),
     _range("right", "右", 0, 1, 0.01, 1), _range("bottom", "下", 0, 1, 0.01, 1)],
    _geometry.crop, desc="按比例裁剪")

NODES["flip"] = _node(
    "flip", "翻转", "几何",
    [_select("mode", "方向", ["horizontal", "vertical"])],
    _geometry.flip, desc="水平/垂直翻转")

NODES["border"] = _node(
    "border", "边框", "几何",
    [_range("width", "宽度", 0, 100, 1, 10), _pick_color("color", "颜色", "#000000")],
    _geometry.border, desc="加边框")

NODES["keypoints"] = _node(
    "keypoints", "特征提取", "计算机视觉",
    [_select("method", "算法", ["sift", "orb"]),
     _range("max_points", "最大点数", 20, 500, 10, 120)],
    _features.extract_keypoints, desc="SIFT/ORB 模拟关键点")

NODES["detect"] = _node(
    "detect", "目标检测", "计算机视觉",
    [_select("method", "方法", ["saliency", "color", "edge"]),
     _bool("auto", "自动阈值", True),
     _range("threshold", "阈值", 0, 255, 1, 128),
     _range("max_boxes", "最大框数", 1, 60, 1, 20)],
    _detection.detect, desc="显著性目标检测模拟")

NODES["segment"] = _node(
    "segment", "图像分割", "计算机视觉",
    [_select("method", "方法", ["threshold", "region", "color"]),
     _range("value", "阈值", 0, 255, 1, 127),
     _range("block", "局部块", 3, 31, 2, 15),
     _range("colors", "颜色数", 2, 12, 1, 6),
     _range("alpha", "叠加透明度", 0, 1, 0.05, 0.45)],
    _segmentation.segment, desc="阈值/区域/颜色分割")

NODES["style_transfer"] = _node(
    "style_transfer", "风格迁移", "计算机视觉",
    [_select("style", "风格", list(_style.STYLES.keys())),
     _range("strength", "强度", 0, 100, 1, 100)],
    _style.apply, desc="风格化（模拟）")


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------
CATEGORIES = ["滤镜", "颜色", "几何", "计算机视觉"]


def get_public_nodes():
    """返回不含 handler 的节点定义（供前端渲染面板）。"""
    out = []
    for ntype, node in NODES.items():
        out.append({
            "type": ntype, "label": node["label"], "category": node["category"],
            "desc": node["desc"], "schema": node["schema"], "defaults": node["defaults"],
            "min_inputs": node["min_inputs"], "max_inputs": node["max_inputs"],
        })
    # 按分类稳定排序
    order = {c: i for i, c in enumerate(CATEGORIES)}
    out.sort(key=lambda n: (order.get(n["category"], 99), n["type"]))
    return out


def get_node(ntype):
    return NODES.get(ntype)
