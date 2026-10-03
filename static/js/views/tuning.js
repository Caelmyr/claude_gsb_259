/* 视图 9：参数调优与预设（实时预览 + 预设管理）。 */
window.Views = window.Views || {};
window.Views.tuning = (function () {
  const C = window.Common;
  let imgId = null, nodeType = "brightness", nodeDef = null;

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div class="col">
            <div class="panel"><div class="panel-title">选择图像</div><div id="tu-gallery" style="max-height:280px;overflow:auto"></div></div>
            <div class="panel">
              <div class="panel-title">选择算法与参数</div>
              <div class="field"><label>算法节点</label><select id="tu-node"></select></div>
              <div id="tu-params" class="param-grid"></div>
              <div class="toolbar" style="margin-top:10px">
                <button class="btn" id="tu-save-preset">存为预设</button>
              </div>
            </div>
            <div class="panel">
              <div class="panel-title">我的预设</div>
              <div id="tu-presets"></div>
            </div>
          </div>
          <div class="col">
            <div class="panel"><div class="panel-title">实时预览<span class="dim">参数变化自动运行（节流）</span></div>
              <div class="stage" id="tu-preview"><span class="dim">选择图像与算法后显示</span></div></div>
          </div>
        </div>`;

      C.fetchImages().then((images) => {
        el.querySelector("#tu-gallery").innerHTML = C.galleryHTML(images);
        C.bindGallery(el.querySelector("#tu-gallery"), images, (id) => { imgId = id; runPreview(el); });
      });

      C.fetchNodes().then((nodes) => {
        C._nodesInfo = C._nodesInfo || {};
        nodes.forEach((n) => { C._nodesInfo[n.type] = n; });
        el.querySelector("#tu-node").innerHTML = nodes.map((n) =>
          `<option value="${n.type}">${C.esc(n.label)} — ${C.esc(n.category)}</option>`).join("");
        selectNode(el, nodeType);
      });

      el.querySelector("#tu-node").onchange = () => selectNode(el, el.querySelector("#tu-node").value);
      el.querySelector("#tu-save-preset").onclick = () => savePreset(el);
      loadPresets(el);
    },
  };

  function selectNode(el, type) {
    nodeType = type;
    nodeDef = (C._nodesInfo || {})[type];
    if (!nodeDef) return;
    const form = C.schemaForm(nodeDef.schema, nodeDef.defaults);
    const box = el.querySelector("#tu-params");
    box.innerHTML = form.html;
    form.bind(box, (vals) => {
      runPreview(el, vals);
    });
    runPreview(el, form.collect(box));
  }

  let previewTimer = null;
  function runPreview(el, vals) {
    if (!imgId || !nodeType) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const nodes = [{ id: "t1", type: nodeType, params: vals || {}, inputs: [] }];
      try {
        const r = await Api.post("/api/run", { image_id: imgId, nodes, pipeline_name: "调优预览" });
        el.querySelector("#tu-preview").innerHTML =
          `<img src="${r.file_url}?t=${Date.now()}"><div class="caption">${r.cache_hit ? "缓存命中" : "已计算"}</div>`;
      } catch (e) {
        el.querySelector("#tu-preview").innerHTML = `<div class="empty">${C.esc(e.message)}</div>`;
      }
    }, 350);
  }

  function currentParams(el) {
    const root = el.querySelector("#tu-params");
    const out = {};
    root.querySelectorAll("[data-key]").forEach((inp) => {
      const k = inp.dataset.key;
      if (inp.type === "range" || inp.type === "number") out[k] = Number(inp.value);
      else if (inp.type === "checkbox") out[k] = inp.checked;
      else out[k] = inp.value;
    });
    return out;
  }

  function savePreset(el) {
    const m = C.modal(`<div class="field"><label>预设名称</label><input type="text" id="pr-name" value="${C.esc((C._nodesInfo[nodeType]?.label || nodeType))} 预设"></div>
      <div class="modal-actions"><button class="btn" id="pr-cancel">取消</button><button class="btn btn-primary" id="pr-ok">保存</button></div>`, "保存预设");
    m.el.querySelector("#pr-cancel").onclick = m.close;
    m.el.querySelector("#pr-ok").onclick = async () => {
      const name = m.el.querySelector("#pr-name").value || "预设";
      await Api.post("/api/presets", { name, scope: "filter", node_type: nodeType, params: currentParams(el) });
      m.close();
      C.toast("已保存预设", "success");
      await C.refreshPresets();
      loadPresets(el);
    };
  }

  async function loadPresets(el) {
    const presets = await C.fetchPresets();
    const box = el.querySelector("#tu-presets");
    const filtered = presets.filter((p) => p.scope === "filter");
    if (!filtered.length) { box.innerHTML = `<span class="dim">暂无预设</span>`; return; }
    box.innerHTML = filtered.map((p) => `
      <div class="card" style="padding:10px;margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:6px">
          <strong style="font-size:13px">${C.esc(p.name)}</strong>
          <span class="badge">${C.esc(p.node_type || "")}</span>
          <span style="flex:1"></span>
          <button class="btn btn-sm" data-apply="${p.id}">应用</button>
          <button class="btn btn-sm btn-danger" data-del="${p.id}">删除</button>
        </div>
        <div class="mono" style="color:var(--text-faint);margin-top:4px">${C.esc(JSON.stringify(p.params))}</div>
      </div>`).join("");
    box.querySelectorAll("[data-apply]").forEach((b) => b.onclick = () => {
      const p = filtered.find((x) => x.id === b.dataset.apply);
      if (!p) return;
      el.querySelector("#tu-node").value = p.node_type;
      nodeType = p.node_type; nodeDef = C._nodesInfo[nodeType];
      const form = C.schemaForm(nodeDef.schema, p.params);
      const pb = el.querySelector("#tu-params");
      pb.innerHTML = form.html;
      form.bind(pb, (vals) => runPreview(el, vals));
      runPreview(el, p.params);
    });
    box.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
      await Api.del(`/api/presets/${b.dataset.del}`);
      C.toast("已删除", "success");
      await C.refreshPresets();
      loadPresets(el);
    });
  }
})();
