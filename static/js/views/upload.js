/* 视图 1：图像上传与管理。 */
window.Views = window.Views || {};
window.Views.upload = (function () {
  const C = window.Common;
  let selectedId = null;

  async function load(el) {
    const grid = el.querySelector("#up-grid");
    const images = await C.fetchImages();
    grid.innerHTML = C.galleryHTML(images);
    C.bindGallery(grid, images, (id, rec) => {
      selectedId = id;
      renderDetail(el, rec);
    });
    if (selectedId) {
      const rec = images.find((i) => i.id === selectedId);
      if (rec) renderDetail(el, rec);
      else renderDetail(el, null);
    }
  }

  function renderDetail(el, rec) {
    const box = el.querySelector("#up-detail");
    if (!rec) {
      box.innerHTML = `<div class="panel"><div class="panel-title">图像详情</div>
        <div class="empty">选择左侧图像查看元数据</div></div>`;
      return;
    }
    const tags = (rec.tags || []).map((t) => `<span class="tag">${C.esc(t)}</span>`).join(" ") || "<span class='dim'>无</span>";
    const anns = (rec.annotations || []).length;
    box.innerHTML = `
      <div class="panel">
        <div class="panel-title">图像详情</div>
        <img src="${rec.file_url}" style="width:100%;border-radius:8px;margin-bottom:10px">
        <div class="keypoint-stats" style="line-height:1.9">
          <div><span class="dim">文件名</span> <strong>${C.esc(rec.filename)}</strong></div>
          <div><span class="dim">尺寸</span> ${rec.width} × ${rec.height}</div>
          <div><span class="dim">格式</span> ${C.esc(rec.format)} · <span class="dim">大小</span> ${C.fmtBytes(rec.size_bytes)}</div>
          <div><span class="dim">哈希</span> <span class="mono">${rec.id.slice(0, 16)}…</span></div>
          <div><span class="dim">上传时间</span> ${C.fmtDate(rec.created_at)}</div>
          <div><span class="dim">标签</span> ${tags}</div>
          <div><span class="dim">标注</span> ${anns} 条</div>
          ${rec.note ? `<div><span class="dim">备注</span> ${C.esc(rec.note)}</div>` : ""}
        </div>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn btn-sm" id="up-rename">重命名</button>
          <button class="btn btn-sm" id="up-tags">编辑标签</button>
          <button class="btn btn-sm" id="up-note">备注</button>
          <button class="btn btn-sm btn-danger" id="up-del">删除</button>
        </div>
      </div>`;
    box.querySelector("#up-del").onclick = async () => {
      if (!confirm("确认删除该图像？（历史结果仍保留）")) return;
      await Api.del(`/api/images/${rec.id}`);
      selectedId = null;
      C.toast("已删除", "success");
      await C.refreshImages();
      load(el);
    };
    box.querySelector("#up-rename").onclick = () => editField(el, rec, "filename", "重命名");
    box.querySelector("#up-tags").onclick = () => editField(el, rec, "tags", "标签（逗号分隔）");
    box.querySelector("#up-note").onclick = () => editField(el, rec, "note", "备注");
  }

  function editField(el, rec, field, label) {
    const m = C.modal(`<div class="field"><label>${label}</label>
      <input type="text" id="mf-val" value="${C.esc(field === "tags" ? (rec.tags || []).join(",") : rec[field] || "")}"></div>
      <div class="modal-actions"><button class="btn" id="mf-cancel">取消</button>
      <button class="btn btn-primary" id="mf-ok">保存</button></div>`, label);
    m.el.querySelector("#mf-cancel").onclick = m.close;
    m.el.querySelector("#mf-ok").onclick = async () => {
      let v = m.el.querySelector("#mf-val").value;
      const body = {};
      if (field === "tags") body.tags = v.split(",").map((s) => s.trim()).filter(Boolean);
      else body[field] = v;
      await Api.patch(`/api/images/${rec.id}`, body);
      m.close();
      C.toast("已保存", "success");
      await C.refreshImages();
      load(el);
    };
  }

  async function doUpload(files) {
    if (!files || !files.length) return;
    try {
      const r = await Api.upload(Array.from(files));
      if (r.saved.length) C.toast(`已上传 ${r.saved.length} 张`, "success");
      if (r.skipped.length) C.toast(`${r.skipped.length} 张被跳过（过大或格式问题）`, "error");
      await C.refreshImages();
      load(el);
    } catch (e) {
      C.toast("上传失败：" + e.message, "error");
    }
  }

  return {
    mount(el) {
      el.innerHTML = `
        <div class="row">
          <div class="col" style="flex:1;min-width:0">
            <div class="panel">
              <div class="panel-title">上传图像<span class="dim">支持 PNG/JPG/BMP/WebP，单张 ≤ 25MB，相同内容自动去重</span></div>
              <div id="up-drop" style="border:2px dashed var(--border-strong);border-radius:10px;padding:26px;text-align:center;color:var(--text-dim);cursor:pointer;transition:border-color .12s">
                <div style="font-size:26px">📥</div>
                <div>拖拽图片到此处，或点击选择文件</div>
              </div>
              <div class="toolbar" style="margin-top:12px;margin-bottom:0">
                <button class="btn btn-primary" id="up-btn">选择文件上传</button>
                <input type="file" id="up-file" multiple accept="image/*" hidden>
                <button class="btn" id="up-refresh">刷新</button>
              </div>
            </div>
            <div id="up-grid"></div>
          </div>
          <div class="col" style="width:320px;flex:none" id="up-detail"></div>
        </div>`;

      const drop = el.querySelector("#up-drop");
      const fileInput = el.querySelector("#up-file");
      el.querySelector("#up-btn").onclick = () => fileInput.click();
      fileInput.onchange = () => doUpload(fileInput.files);
      drop.onclick = () => fileInput.click();
      drop.ondragover = (e) => { e.preventDefault(); drop.style.borderColor = "var(--accent)"; };
      drop.ondragleave = () => { drop.style.borderColor = "var(--border-strong)"; };
      drop.ondrop = (e) => { e.preventDefault(); drop.style.borderColor = "var(--border-strong)"; doUpload(e.dataTransfer.files); };
      el.querySelector("#up-refresh").onclick = () => { C.invalidate("images"); load(el); };

      renderDetail(el, null);
      load(el);
    },
    refresh() { C.refreshImages().then(() => this.mounted && load(document.querySelector('.view[data-view="upload"]'))); },
  };
})();
