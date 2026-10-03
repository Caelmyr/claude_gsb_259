/* 视图 2：滤镜链编辑器（拖拽节点、连线、参数调节、运行、保存/加载）。 */
window.Views = window.Views || {};
window.Views.pipeline = (function () {
  const C = window.Common;
  const NODE_W = 176;
  const HEADER_H = 38;

  let nodes = [];          // {id, type, x, y, params, inputs:[]}
  let sel = null;          // 选中节点 id
  let idCounter = 1;
  let nodeEls = {};        // id -> DOM element
  let connecting = null;   // {from, x, y}

  // 参考元素
  let canvas, svg, paletteEl, inspectorEl, imageSel, runBtn, previewBox;

  function newId() { return "n" + (idCounter++); }

  // ------------------------------------------------------------------ 渲染
  function nodeLabel(type) {
    const nodesInfo = C._nodesInfo || {};
    return (nodesInfo[type] && nodesInfo[type].label) || type;
  }

  function renderPalette(nodeDefs) {
    const byCat = {};
    (nodeDefs || []).forEach((n) => { (byCat[n.category] = byCat[n.category] || []).push(n); });
    const cats = Object.keys(byCat);
    paletteEl.innerHTML = cats.map((cat) => `
      <div class="palette-cat">${C.esc(cat)}</div>
      ${byCat[cat].map((n) => `
        <div class="palette-node" draggable="true" data-type="${n.type}">
          ${C.esc(n.label)}
          <div class="pn-desc">${C.esc(n.desc || "")}</div>
        </div>`).join("")}
    `).join("");
    paletteEl.querySelectorAll(".palette-node").forEach((pn) => {
      pn.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", pn.dataset.type);
        e.dataTransfer.effectAllowed = "copy";
      });
    });
  }

  function renderNodes() {
    canvas.querySelectorAll(".node").forEach((n) => n.remove());
    nodeEls = {};
    nodes.forEach((n) => {
      const el = C.h(`
        <div class="node ${sel === n.id ? "selected" : ""}" data-id="${n.id}" style="left:${n.x}px;top:${n.y}px">
          <div class="node-header"><span class="dot"></span>${C.esc(nodeLabel(n.type))}
            <span style="flex:1"></span><span class="node-x" title="删除">×</span></div>
          <div class="node-body">${C.esc((C._nodesInfo[n.type] && C._nodesInfo[n.type].desc) || "")}</div>
          <div class="port in" data-id="${n.id}" data-port="in"></div>
          <div class="port out" data-id="${n.id}" data-port="out"></div>
        </div>`);
      canvas.appendChild(el);
      nodeEls[n.id] = el;
      bindNode(el, n);
    });
    redrawEdges();
  }

  function redrawEdges() {
    const paths = [];
    nodes.forEach((n) => {
      (n.inputs || []).forEach((srcId) => {
        const a = nodeEls[srcId], b = nodeEls[n.id];
        if (!a || !b) return;
        const x1 = a.offsetLeft + NODE_W, y1 = a.offsetTop + HEADER_H / 2;
        const x2 = b.offsetLeft, y2 = b.offsetTop + HEADER_H / 2;
        const dx = Math.max(28, Math.abs(x2 - x1) / 2);
        paths.push(`<path d="M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}"/>`);
      });
    });
    svg.innerHTML = paths.join("");
    // 连接中的临时线
    if (connecting) {
      const fromEl = nodeEls[connecting.from];
      const x1 = fromEl.offsetLeft + NODE_W, y1 = fromEl.offsetTop + HEADER_H / 2;
      const dx = Math.max(28, Math.abs(connecting.x - x1) / 2);
      svg.innerHTML += `<path d="M ${x1} ${y1} C ${x1 + dx} ${y1}, ${connecting.x - dx} ${connecting.y}, ${connecting.x} ${connecting.y}" style="stroke-dasharray:4 3"/>`;
    }
  }

  // ------------------------------------------------------------------ 交互
  function bindNode(el, n) {
    // 拖拽节点
    el.querySelector(".node-header").addEventListener("mousedown", (e) => {
      if (e.target.classList.contains("node-x")) return;
      select(n.id);
      const startX = e.clientX, startY = e.clientY;
      const origX = n.x, origY = n.y;
      function move(ev) {
        n.x = origX + (ev.clientX - startX);
        n.y = origY + (ev.clientY - startY);
        n.x = Math.max(0, n.x); n.y = Math.max(0, n.y);
        el.style.left = n.x + "px"; el.style.top = n.y + "px";
        redrawEdges();
      }
      function up() { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });

    el.querySelector(".node-x").addEventListener("click", () => deleteNode(n.id));

    // 端口连线
    el.querySelector(".port.out").addEventListener("mousedown", (e) => {
      e.stopPropagation();
      connecting = { from: n.id, x: e.clientX, y: e.clientY };
      const move = (ev) => {
        const rect = canvas.getBoundingClientRect();
        connecting.x = ev.clientX - rect.left; connecting.y = ev.clientY - rect.top;
        redrawEdges();
      };
      const up = () => { connecting = null; redrawEdges(); document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
    el.querySelector(".port.in").addEventListener("mouseup", (e) => {
      e.stopPropagation();
      if (connecting && connecting.from !== n.id) {
        n.inputs = [connecting.from];
        connecting = null;
        renderNodes();
        select(n.id);
      }
    });
  }

  function select(id) {
    sel = id;
    Object.values(nodeEls).forEach((el) => el.classList.toggle("selected", el.dataset.id === id));
    renderInspector();
  }

  function deleteNode(id) {
    nodes = nodes.filter((n) => n.id !== id);
    nodes.forEach((n) => { n.inputs = (n.inputs || []).filter((s) => s !== id); });
    if (sel === id) sel = null;
    renderNodes();
    renderInspector();
  }

  function addNode(type, x, y) {
    const def = (C._nodesInfo || {})[type];
    if (!def) return;
    nodes.push({ id: newId(), type, x, y, params: {}, inputs: [] });
    renderNodes();
    const last = nodes[nodes.length - 1];
    select(last.id);
  }

  // ------------------------------------------------------------------ 检查器
  function renderInspector() {
    const node = nodes.find((n) => n.id === sel);
    let paramHTML = `<div class="empty">未选择节点<br><span style="font-size:12px">从左侧拖入节点，或点击已有节点编辑参数</span></div>`;
    if (node) {
      const def = C._nodesInfo[node.type];
      const form = C.schemaForm(def.schema, node.params);
      paramHTML = `
        <div class="panel-title">${C.esc(nodeLabel(node.type))} <span class="dim">#${node.id}</span></div>
        <div class="param-grid">${form.html}</div>
        <div class="toolbar" style="margin-top:10px">
          <button class="btn btn-sm btn-danger" id="insp-del">删除节点</button>
          <button class="btn btn-sm" id="insp-clear">清空全部</button>
        </div>`;
      // 延迟绑定表单
      setTimeout(() => {
        const box = inspectorEl.querySelector(".param-grid");
        if (box) {
          form.bind(box, (vals) => { node.params = vals; });
        }
        inspectorEl.querySelector("#insp-del").onclick = () => deleteNode(node.id);
        inspectorEl.querySelector("#insp-clear").onclick = () => { nodes = []; sel = null; renderNodes(); renderInspector(); };
      }, 0);
    }
    inspectorEl.querySelector("#insp-node").innerHTML = paramHTML;
    renderRunSection();
  }

  function renderRunSection() {
    // 图像选择 + 运行 + 预览（保持在检查器底部，不随节点选择重建而丢失图片选择）
    if (!inspectorEl.querySelector("#insp-run")) {
      inspectorEl.insertAdjacentHTML("beforeend", `
        <div class="panel-title" style="margin-top:16px">运行预览</div>
        <div class="field"><label>选择输入图像</label><select id="insp-image"></select></div>
        <div class="toolbar">
          <button class="btn btn-primary" id="insp-run">▶ 运行流水线</button>
          <button class="btn" id="insp-save">保存</button>
        </div>
        <div class="field"><label>加载已保存流水线</label>
          <div class="select-row"><select id="insp-load"></select><button class="btn btn-sm" id="insp-load-btn">加载</button></div>
        </div>
        <div id="insp-preview" class="stage" style="margin-top:10px;min-height:120px"><span class="dim">运行后在此显示结果</span></div>`);
      inspectorEl.querySelector("#insp-run").onclick = runPipeline;
      inspectorEl.querySelector("#insp-save").onclick = savePipeline;
      inspectorEl.querySelector("#insp-load-btn").onclick = loadPipeline;
      loadImageOptions();
      loadPipelineOptions();
    }
  }

  async function loadImageOptions() {
    const sel = inspectorEl.querySelector("#insp-image");
    const images = await C.fetchImages();
    sel.innerHTML = `<option value="">— 选择图像 —</option>` +
      images.map((i) => `<option value="${i.id}">${C.esc(i.filename)} (${i.width}×${i.height})</option>`).join("");
  }

  async function loadPipelineOptions() {
    const sel = inspectorEl.querySelector("#insp-load");
    const ps = await C.fetchPipelines();
    sel.innerHTML = `<option value="">— 选择流水线 —</option>` +
      ps.map((p) => `<option value="${p.id}">${C.esc(p.name)}</option>`).join("");
  }

  async function runPipeline() {
    const imageId = inspectorEl.querySelector("#insp-image").value;
    if (!imageId) { C.toast("请先选择输入图像", "error"); return; }
    if (!nodes.length) { C.toast("流水线为空，请先添加节点", "error"); return; }
    const preview = inspectorEl.querySelector("#insp-preview");
    preview.innerHTML = `<div class="loading">运行中…</div>`;
    try {
      const r = await Api.post("/api/run", { image_id: imageId, nodes: nodes.map(strip), pipeline_name: "临时流水线" });
      preview.innerHTML = `
        <img src="${r.file_url}?t=${Date.now()}">
        <div class="caption">${r.cache_hit ? "缓存命中" : "已计算"} · ${(r.meta && r.meta.count != null) ? "对象 " + r.meta.count : ""}</div>`;
      // 标记失败节点
      const failed = (r.node_results || []).filter((n) => !n.ok);
      if (failed.length) {
        C.toast("有节点执行失败：" + failed.map((f) => f.node_id).join(", "), "error");
        Object.values(nodeEls).forEach((el) => el.classList.remove("error"));
        failed.forEach((f) => { if (nodeEls[f.node_id]) nodeEls[f.node_id].classList.add("error"); });
      }
    } catch (e) {
      preview.innerHTML = `<div class="empty">运行失败：${C.esc(e.message)}</div>`;
    }
  }

  function strip(n) { return { id: n.id, type: n.type, params: n.params, inputs: n.inputs, x: n.x, y: n.y }; }

  function savePipeline() {
    const m = C.modal(`<div class="field"><label>流水线名称</label><input type="text" id="sp-name" value="我的流水线"></div>
      <div class="modal-actions"><button class="btn" id="sp-cancel">取消</button><button class="btn btn-primary" id="sp-ok">保存</button></div>`, "保存流水线");
    m.el.querySelector("#sp-cancel").onclick = m.close;
    m.el.querySelector("#sp-ok").onclick = async () => {
      const name = m.el.querySelector("#sp-name").value || "未命名流水线";
      await Api.post("/api/pipelines", { name, nodes: nodes.map(strip) });
      m.close();
      C.toast("已保存流水线", "success");
      await C.refreshPipelines();
      loadPipelineOptions();
    };
  }

  async function loadPipeline() {
    const pid = inspectorEl.querySelector("#insp-load").value;
    if (!pid) return;
    const ps = await C.fetchPipelines();
    const p = ps.find((x) => x.id === pid);
    if (!p) return;
    nodes = (p.nodes || []).map((n, i) => ({
      id: newId(), type: n.type, params: n.params, inputs: n.inputs,
      x: n.x != null ? n.x : 20 + (i % 4) * 220, y: n.y != null ? n.y : 20 + Math.floor(i / 4) * 130,
    }));
    // 修正 inputs 引用（用映射表）
    const map = {};
    p.nodes.forEach((n, i) => { map[n.id] = nodes[i].id; });
    nodes.forEach((n) => { n.inputs = (n.inputs || []).map((s) => map[s]).filter(Boolean); });
    sel = null;
    renderNodes();
    renderInspector();
    C.toast("已加载流水线", "success");
  }

  // ------------------------------------------------------------------ 挂载
  return {
    mount(el) {
      el.innerHTML = `
        <div class="pipeline-layout">
          <div class="node-palette" id="pl-palette">
            <div class="panel-title">节点面板<span class="dim">拖入画布</span></div>
          </div>
          <div class="canvas-wrap" id="pl-canvas-wrap">
            <div class="canvas-hint">从左侧拖入节点 · 拖「输出」端口到另一节点的「输入」端口连线 · 拖节点头部移动</div>
            <div class="canvas" id="pl-canvas"></div>
          </div>
          <div class="pipeline-inspector" id="pl-inspector">
            <div id="insp-node"></div>
          </div>
        </div>`;

      paletteEl = el.querySelector("#pl-palette");
      canvas = el.querySelector("#pl-canvas");
      const wrap = el.querySelector("#pl-canvas-wrap");
      svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      canvas.appendChild(svg);
      inspectorEl = el.querySelector("#pl-inspector");

      // 画布 drop
      wrap.addEventListener("dragover", (e) => e.preventDefault());
      wrap.addEventListener("drop", (e) => {
        e.preventDefault();
        const type = e.dataTransfer.getData("text/plain");
        const rect = canvas.getBoundingClientRect();
        addNode(type, e.clientX - rect.left - NODE_W / 2, e.clientY - rect.top - HEADER_H / 2);
      });

      C.fetchNodes().then((defs) => {
        C._nodesInfo = {};
        defs.forEach((d) => { C._nodesInfo[d.type] = d; });
        renderPalette(defs);
      });

      renderNodes();
      renderInspector();
    },
    refresh() { loadImageOptions(); loadPipelineOptions(); },
  };
})();
