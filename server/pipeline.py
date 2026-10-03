"""流水线引擎：DAG 校验、拓扑排序、执行与结果组装。

难点之一「流水线引擎设计」的核心实现：

- 节点用 inputs 表达依赖边（单输入链式，支持扇出）。执行前做完整校验：
  类型存在性、id 唯一、输入引用存在、输入数量在 min/max 内、无环。
- Kahn 拓扑排序决定执行顺序；每个节点消费其唯一上游节点的输出「数据包」，
  数据包 = 图像 + meta（meta 携带关键点/检测框/分割区域等非图像数据，
  供检测->画框、分割->统计这类下游节点复用）。
- 每节点执行包裹 try/except，错误记录到该节点，前端可定位失败点。
- canonical_key() 生成与拓扑顺序无关的确定性哈希，供结果缓存使用。
"""
import json

from . import nodes as node_registry


class Packet:
    """节点间传递的数据包：图像 + 附加元数据。"""

    def __init__(self, image=None, meta=None):
        self.image = image
        self.meta = meta if meta is not None else {}


def _merge_params(node):
    spec = node_registry.get_node(node.get("type"))
    defaults = dict(spec["defaults"]) if spec else {}
    merged = dict(defaults)
    merged.update(node.get("params") or {})
    return merged


def validate(nodes):
    """返回错误列表；空列表表示合法。"""
    errors = []
    ids = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            errors.append("存在缺少 id 的节点")
            continue
        if nid in ids:
            errors.append(f"节点 id 重复：{nid}")
        ids.add(nid)
        spec = node_registry.get_node(n.get("type"))
        if spec is None:
            errors.append(f"未知节点类型：{n.get('type')}")
            continue
        ni = len(n.get("inputs") or [])
        if ni < spec["min_inputs"] or ni > spec["max_inputs"]:
            errors.append(f"节点 {nid} 输入数量 {ni} 超出允许范围 "
                          f"[{spec['min_inputs']}, {spec['max_inputs']}]")

    for n in nodes:
        for inp in (n.get("inputs") or []):
            if inp not in ids:
                errors.append(f"节点 {n.get('id')} 引用了不存在的输入 {inp}")

    if not errors:
        ordered, leftover = _topo(nodes)
        if leftover:
            errors.append("流水线存在环，无法执行")
    return errors


def _topo(nodes):
    """Kahn 拓扑排序。返回 (ordered_ids, remaining_ids)。"""
    indeg = {}
    children = {}
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        indeg[n["id"]] = len(n.get("inputs") or [])
        children.setdefault(n["id"], [])
    for n in nodes:
        for inp in (n.get("inputs") or []):
            children.setdefault(inp, []).append(n["id"])

    queue = [nid for nid, d in indeg.items() if d == 0]
    ordered = []
    while queue:
        nid = queue.pop(0)
        ordered.append(nid)
        for c in children.get(nid, []):
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    remaining = [nid for nid, d in indeg.items() if d > 0]
    return ordered, remaining


def topological_order(nodes):
    ordered, _ = _topo(nodes)
    return ordered


def canonical_key(nodes):
    """生成与布局/命名无关的确定性流水线指纹（供缓存命中判定）。"""
    ordered, _ = _topo(nodes)
    by_id = {n["id"]: n for n in nodes}
    seq = []
    for nid in ordered:
        n = by_id[nid]
        seq.append({"type": n["type"], "params": _merge_params(n)})
    return json.dumps(seq, sort_keys=True, separators=(",", ":"))


def execute(image, nodes, source_meta=None):
    """在给定图像上执行流水线。

    返回 dict：
      image          - 最终结果图像（主输出）
      meta           - 主输出的 meta
      node_results   - [{node_id, type, ok, error}] 逐节点状态
      output_node_id - 主输出节点（无节点时为 None）
      error          - 顶层错误（校验失败等）
    """
    errors = validate(nodes)
    if errors:
        return {"image": image, "meta": source_meta or {}, "node_results": [],
                "output_node_id": None, "error": "; ".join(errors)}

    ordered, _ = _topo(nodes)
    by_id = {n["id"]: n for n in nodes}
    packets = {"__source__": Packet(image, source_meta or {})}
    node_results = []

    for nid in ordered:
        node = by_id[nid]
        spec = node_registry.get_node(node["type"])
        inputs = node.get("inputs") or []
        input_packet = packets.get(inputs[0], packets["__source__"]) if inputs else packets["__source__"]
        params = _merge_params(node)
        try:
            out_img, out_meta = spec["handler"](input_packet.image, params, input_packet.meta)
            packets[nid] = Packet(out_img, out_meta)
            node_results.append({"node_id": nid, "type": node["type"], "ok": True, "error": None})
        except Exception as exc:  # noqa: BLE001 —— 记录但继续，让前端能看到失败节点
            packets[nid] = Packet(input_packet.image, input_packet.meta)
            node_results.append({"node_id": nid, "type": node["type"], "ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"})

    # 主输出 = 无下游消费者的节点中拓扑序最后一个；无节点则输出源图
    consumers = set()
    for n in nodes:
        for inp in (n.get("inputs") or []):
            consumers.add(inp)
    sinks = [nid for nid in ordered if nid not in consumers]
    output_node_id = sinks[-1] if sinks else (ordered[-1] if ordered else None)

    if output_node_id:
        out = packets[output_node_id]
    else:
        out = packets["__source__"]

    return {"image": out.image, "meta": out.meta, "node_results": node_results,
            "output_node_id": output_node_id, "error": None}
