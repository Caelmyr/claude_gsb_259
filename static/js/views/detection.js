/* 视图 4：目标检测与标注（检测模拟 + 人工画框标注）。 */
window.Views = window.Views || {};
window.Views.detection = (function () {
  const C = window.Common;
  let imgId = null, imgRec = null;
  let annCanvas, annCtx, annImg = null, drawStart = null, annotations = [];

  return {
    mount(el) {
      el.innerHTML = `
        <div class="row">
          <div class="col" style="flex:1;min-width:0">
            <div class="panel"><div class="panel-title">选择图像</div><div id="dt-gallery"></div></div>
            <div class="panel">
              <div class="panel-title">检测参数</div>
              <div class="select-row" style="margin-bottom:10px">
                <select id="dt-method">
                  <option value="saliency">显著性检测</option><option value="color">颜色突出度</option><option value="edge">边缘密度</option>
                </select>
                <label style="font-size:12px;display:flex;align-items:center;gap:4px"><input type="checkbox" id="dt-auto" checked> 自动阈值</label>
              </div>
              <div class="field"><label>阈值</label><input type="range" id="dt-thr" min="0" max="255" value="128"></div>
              <div class="field"><label>最大框数</label><input type="range" id="dt-max" min="1" max="60" value="20"></div>
              <button class="btn btn-primary" id="dt-run">运行检测</button>
            </div>
            <div class="panel"><div class="panel-title">检测结果</div><div class="stage" id="dt-result"><span class="dim">选择图像并运行</span></div>
              <div id="dt-boxes" class="keypoint-stats" style="margin-top:10px"></div></div>
          </div>
          <div class="col" style="flex:1;min-width:0">
            <div class="panel">
              <div class="panel-title">人工标注<span class="dim">在图上拖拽画框</span></div>
              <div class="select-row" style="margin-bottom:10px">
                <input type="text" id="dt-label" placeholder="标签，如：行人" value="物体">
                <button class="btn btn-sm" id="dt-new-ann">新建标注</button>
              </div>
              <div class="annotate-stage" id="dt-canvas-box"><canvas id="dt-canvas"></canvas></div>
              <div class="keypoint-stats" style="margin-top:10px"><span class="dim">已标注：</span><span id="dt-ann-count">0</span></div>
            </div>
            <div class="panel"><div class="panel-title">标注列表</div><div id="dt-ann-list" class="keypoint-stats"><span class="dim">暂无标注</span></div></div>
          </div>
        </div>`;

      annCanvas = el.querySelector("#dt-canvas");
      annCtx = annCanvas.getContext("2d");

      C.fetchImages().then((images) => {
        el.querySelector("#dt-gallery").innerHTML = C.galleryHTML(images);
        C.bindGallery(el.querySelector("#dt-gallery"), images, (id, rec) => {
          imgId = id; imgRec = rec;
          loadAnnotateCanvas(el, rec);
          refreshAnnList(el, rec);
        });
      });

      el.querySelector("#dt-run").onclick = async () => {
        if (!imgId) { C.toast("请选择图像", "error"); return; }
        el.querySelector("#dt-result").innerHTML = `<div class="loading">检测中…</div>`;
        const r = await Api.post("/api/detect", {
          image_id: imgId, method: el.querySelector("#dt-method").value,
          auto: el.querySelector("#dt-auto").checked,
          threshold: Number(el.querySelector("#dt-thr").value),
          max_boxes: Number(el.querySelector("#dt-max").value),
        });
        el.querySelector("#dt-result").innerHTML =
          `<img src="/api/results/${r.result_id}/file?t=${Date.now()}"><div class="caption">检测到 ${r.count} 个目标</div>`;
        el.querySelector("#dt-boxes").innerHTML = r.boxes.length
          ? r.boxes.map((b) => `<div>▸ ${C.esc(b.label)} <span class="dim">置信 ${b.score} · ${b.w}×${b.h} @(${b.x},${b.y})</span></div>`).join("")
          : `<span class="dim">未检测到目标</span>`;
      };

      // 画框
      annCanvas.addEventListener("mousedown", (e) => {
        if (!annImg) return;
        drawStart = toCanvas(e);
      });
      annCanvas.addEventListener("mousemove", (e) => {
        if (!drawStart || !annImg) return;
        redrawCanvas();
        const p = toCanvas(e);
        annCtx.strokeStyle = "#ff5252"; annCtx.lineWidth = 2;
        annCtx.strokeRect(drawStart.x, drawStart.y, p.x - drawStart.x, p.y - drawStart.y);
      });
      annCanvas.addEventListener("mouseup", async (e) => {
        if (!drawStart || !annImg) return;
        const p = toCanvas(e);
        const box = [Math.min(drawStart.x, p.x), Math.min(drawStart.y, p.y),
                     Math.abs(p.x - drawStart.x), Math.abs(p.y - drawStart.y)];
        drawStart = null;
        if (box[2] < 5 || box[3] < 5) { redrawCanvas(); return; }
        const label = el.querySelector("#dt-label").value || "物体";
        const rec = await Api.post(`/api/images/${imgId}/annotations`, { box, label, color: "#ff5252" });
        C.invalidate("images");
        refreshAnnList(el, rec);
        C.toast("已添加标注", "success");
      });

      el.querySelector("#dt-new-ann").onclick = () => {
        el.querySelector("#dt-label").focus();
      };
    },

    refresh() { const el = document.querySelector('.view[data-view="detection"]'); if (el && this.mounted && imgRec) refreshAnnList(el, imgRec); },
  };

  function toCanvas(e) {
    const rect = annCanvas.getBoundingClientRect();
    return { x: (e.clientX - rect.left) * (annCanvas.width / rect.width),
             y: (e.clientY - rect.top) * (annCanvas.height / rect.height) };
  }

  function loadAnnotateCanvas(el, rec) {
    annImg = new Image();
    annImg.crossOrigin = "anonymous";
    annImg.onload = () => {
      annCanvas.width = annImg.naturalWidth;
      annCanvas.height = annImg.naturalHeight;
      annotations = rec.annotations || [];
      redrawCanvas();
    };
    annImg.src = rec.file_url;
  }

  function redrawCanvas() {
    if (!annImg) return;
    annCtx.clearRect(0, 0, annCanvas.width, annCanvas.height);
    annCtx.drawImage(annImg, 0, 0);
    annotations.forEach((a) => {
      const [x, y, w, h] = a.box;
      annCtx.strokeStyle = a.color || "#ff5252";
      annCtx.lineWidth = Math.max(2, annCanvas.width / 200);
      annCtx.strokeRect(x, y, w, h);
      annCtx.fillStyle = a.color || "#ff5252";
      annCtx.fillRect(x, y - 20, (a.label || "").length * 13 + 10, 20);
      annCtx.fillStyle = "#fff"; annCtx.font = "13px sans-serif";
      annCtx.fillText(a.label || "", x + 5, y - 5);
    });
  }

  function refreshAnnList(el, rec) {
    annotations = rec.annotations || [];
    el.querySelector("#dt-ann-count").textContent = annotations.length;
    el.querySelector("#dt-ann-list").innerHTML = annotations.length
      ? annotations.map((a, i) => `
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="width:12px;height:12px;border-radius:3px;background:${C.esc(a.color || '#ff5252')};flex:none"></span>
            <span>${C.esc(a.label || "未命名")} <span class="dim">[${a.box.map(Math.round).join(", ")}]</span></span>
            <span style="flex:1"></span>
            <button class="btn btn-sm btn-danger" data-i="${i}">删除</button>
          </div>`).join("")
      : `<span class="dim">暂无标注</span>`;
    el.querySelectorAll("#dt-ann-list [data-i]").forEach((btn) => {
      btn.onclick = async () => {
        const rec2 = await Api.del(`/api/images/${imgId}/annotations/${btn.dataset.i}`);
        C.invalidate("images");
        refreshAnnList(el, rec2);
        redrawCanvas();
      };
    });
    redrawCanvas();
  }
})();
