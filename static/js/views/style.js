/* 视图 6：风格迁移（模拟）。 */
window.Views = window.Views || {};
window.Views.style = (function () {
  const C = window.Common;
  let imgId = null, currentStyle = "oil";

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div class="col">
            <div class="panel"><div class="panel-title">选择图像</div><div id="st-gallery" style="max-height:360px;overflow:auto"></div></div>
            <div class="panel">
              <div class="panel-title">风格预设</div>
              <div id="st-styles" style="display:grid;grid-template-columns:1fr 1fr;gap:8px"></div>
              <div class="field" style="margin-top:12px"><label>强度</label>
                <div class="range-row"><input type="range" id="st-strength" min="0" max="100" value="100"><span class="range-val">100</span></div>
              </div>
              <button class="btn btn-primary" id="st-run">应用风格</button>
            </div>
          </div>
          <div class="col">
            <div class="panel"><div class="panel-title">风格化结果</div><div class="stage" id="st-result"><span class="dim">选择图像与风格</span></div></div>
          </div>
        </div>`;

      C.fetchImages().then((images) => {
        el.querySelector("#st-gallery").innerHTML = C.galleryHTML(images);
        C.bindGallery(el.querySelector("#st-gallery"), images, (id) => { imgId = id; });
      });

      C.fetchStyles().then((styles) => {
        const box = el.querySelector("#st-styles");
        box.innerHTML = styles.map((s) => `
          <div class="card" data-style="${s.name}" style="padding:10px;cursor:pointer">
            <div style="font-weight:600;font-size:13px">${C.esc(s.name)}</div>
            <div style="font-size:11px;color:var(--text-faint);margin-top:2px">${C.esc(s.description)}</div>
          </div>`).join("");
        box.querySelectorAll(".card").forEach((c) => c.addEventListener("click", () => {
          box.querySelectorAll(".card").forEach((x) => x.classList.remove("selected"));
          c.classList.add("selected");
          currentStyle = c.dataset.style;
        }));
      });

      el.querySelector("#st-strength").addEventListener("input", (e) => {
        el.querySelector("#st-strength").nextElementSibling.textContent = e.target.value;
      });

      el.querySelector("#st-run").onclick = async () => {
        if (!imgId) { C.toast("请选择图像", "error"); return; }
        el.querySelector("#st-result").innerHTML = `<div class="loading">风格化中…</div>`;
        const r = await Api.post("/api/style", {
          image_id: imgId, style: currentStyle,
          strength: Number(el.querySelector("#st-strength").value),
        });
        el.querySelector("#st-result").innerHTML =
          `<img src="/api/results/${r.result_id}/file?t=${Date.now()}"><div class="caption">${C.esc(r.style)} · ${C.esc(r.description || "")}</div>`;
      };
    },
  };
})();
