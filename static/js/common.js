/* 通用工具：DOM 辅助、格式化、提示、图库、参数表单渲染。 */
window.Common = (function () {
  // ------------------------------------------------------------------ DOM
  function h(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ------------------------------------------------------------------ 格式化
  function fmtBytes(n) {
    if (n == null) return "-";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }
  function fmtDate(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fmtMs(ms) {
    if (ms == null) return "-";
    if (ms < 1000) return ms + " ms";
    return (ms / 1000).toFixed(2) + " s";
  }

  // ------------------------------------------------------------------ 提示
  function toast(msg, type) {
    const box = document.getElementById("toasts");
    const t = h(`<div class="toast ${type || ""}">${esc(msg)}</div>`);
    box.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, 2600);
    setTimeout(() => t.remove(), 3000);
  }

  // ------------------------------------------------------------------ 模态
  function modal(innerHTML, title) {
    const backdrop = document.getElementById("modalBackdrop");
    const m = h(`<div class="modal"><div class="modal-title">${esc(title || "")}</div>${innerHTML}</div>`);
    backdrop.innerHTML = "";
    backdrop.appendChild(m);
    backdrop.hidden = false;
    function close() { backdrop.hidden = true; backdrop.innerHTML = ""; }
    backdrop.onclick = (e) => { if (e.target === backdrop) close(); };
    return { el: m, close };
  }

  // ------------------------------------------------------------------ 节流
  function debounce(fn, ms) {
    let t = null;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  // ------------------------------------------------------------------ 数据缓存
  let _cache = {};
  function cached(key, fn) {
    if (_cache[key]) return Promise.resolve(_cache[key]);
    return fn().then((d) => { _cache[key] = d; return d; });
  }
  function invalidate(key) { delete _cache[key]; }

  const fetchImages = () => cached("images", () => Api.get("/api/images").then((d) => d.images));
  const fetchNodes = () => cached("nodes", () => Api.get("/api/nodes").then((d) => d.nodes));
  const fetchStyles = () => cached("styles", () => Api.get("/api/styles").then((d) => d.styles));
  const fetchPipelines = () => cached("pipelines", () => Api.get("/api/pipelines").then((d) => d.pipelines));
  const fetchPresets = () => cached("presets", () => Api.get("/api/presets").then((d) => d.presets));
  const refreshImages = () => { invalidate("images"); return fetchImages(); };
  const refreshPipelines = () => { invalidate("pipelines"); return fetchPipelines(); };
  const refreshPresets = () => { invalidate("presets"); return fetchPresets(); };

  // ------------------------------------------------------------------ 图库
  function galleryHTML(images, opts) {
    opts = opts || {};
    if (!images.length) {
      return `<div class="empty"><span class="big">🖼️</span>暂无图像<br>请先到「图像管理」上传，或拖拽图片到上传区</div>`;
    }
    return `<div class="grid">` + images.map((im) => `
      <div class="card" data-id="${esc(im.id)}">
        <img class="thumb" src="${esc(im.thumbnail_url)}" loading="lazy" draggable="false">
        <span class="card-badge">${im.width}×${im.height}</span>
        <div class="card-meta">
          <div class="card-name" title="${esc(im.filename)}">${esc(im.filename)}</div>
          <div class="card-dim">${fmtBytes(im.size_bytes)} · ${fmtDate(im.created_at)}</div>
        </div>
      </div>`).join("") + `</div>`;
  }

  /* 绑定图库点击选择。onSelect(id, record) 在点击时触发。 */
  function bindGallery(container, images, onSelect, selectedId) {
    container.addEventListener("click", (e) => {
      const card = e.target.closest(".card");
      if (!card) return;
      const id = card.dataset.id;
      container.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
      const rec = images.find((im) => im.id === id);
      if (onSelect) onSelect(id, rec);
    });
    if (selectedId) {
      const card = container.querySelector(`.card[data-id="${selectedId}"]`);
      if (card) card.classList.add("selected");
    }
  }

  /* 简单的下拉选择器（图像/流水线选择）。返回 {el, value()}。 */
  function selectBox(items, placeholder, valueFn, labelFn) {
    const opts = [`<option value="">${esc(placeholder)}</option>`]
      .concat(items.map((it) => `<option value="${esc(valueFn(it))}">${esc(labelFn(it))}</option>`))
      .join("");
    const el = h(`<select>${opts}</select>`);
    return { el, value: () => el.value };
  }

  // ------------------------------------------------------------------ 参数表单
  /* 依据节点 schema 渲染参数控件。返回 {html, collect, bind}。 */
  function schemaForm(schema, values) {
    values = values || {};
    const html = schema.map((p) => {
      const v = values[p.key] !== undefined ? values[p.key] : p.default;
      const hint = p.desc ? `<span class="hint">${esc(p.desc)}</span>` : "";
      if (p.type === "range") {
        return `<div class="field">
          <label>${esc(p.label)}${hint}</label>
          <div class="range-row">
            <input type="range" data-key="${p.key}" min="${p.min}" max="${p.max}" step="${p.step || 1}" value="${v}">
            <span class="range-val" data-val="${p.key}">${v}</span>
          </div></div>`;
      }
      if (p.type === "select") {
        return `<div class="field"><label>${esc(p.label)}${hint}</label>
          <select data-key="${p.key}">${p.options.map((o) => `<option value="${esc(o)}" ${String(o) === String(v) ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
      }
      if (p.type === "bool") {
        return `<div class="field"><label>${esc(p.label)}${hint}</label>
          <div><input type="checkbox" data-key="${p.key}" ${v ? "checked" : ""}></div></div>`;
      }
      if (p.type === "color") {
        return `<div class="field"><label>${esc(p.label)}${hint}</label>
          <input type="color" data-key="${p.key}" value="${esc(v)}"></div>`;
      }
      return `<div class="field"><label>${esc(p.label)}${hint}</label>
        <input type="number" data-key="${p.key}" value="${v}"></div>`;
    }).join("");

    function collect(root) {
      const out = {};
      root.querySelectorAll("[data-key]").forEach((inp) => {
        const k = inp.dataset.key;
        if (inp.type === "range" || inp.type === "number") out[k] = Number(inp.value);
        else if (inp.type === "checkbox") out[k] = inp.checked;
        else out[k] = inp.value;
      });
      return out;
    }
    function bind(root, onChange) {
      root.querySelectorAll("[data-key]").forEach((inp) => {
        const evt = inp.type === "range" ? "input" : (inp.type === "checkbox" ? "change" : "change");
        inp.addEventListener(evt, () => {
          if (inp.type === "range") {
            const v = root.querySelector(`[data-val="${inp.dataset.key}"]`);
            if (v) v.textContent = inp.value;
          }
          if (onChange) onChange(collect(root));
        });
      });
      return root;
    }
    return { html, collect, bind };
  }

  return {
    h, esc, fmtBytes, fmtDate, fmtMs, toast, modal, debounce,
    fetchImages, fetchNodes, fetchStyles, fetchPipelines, fetchPresets,
    refreshImages, refreshPipelines, refreshPresets, invalidate,
    galleryHTML, bindGallery, selectBox, schemaForm,
  };
})();
