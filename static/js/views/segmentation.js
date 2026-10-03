/* 视图 5：图像分割。 */
window.Views = window.Views || {};
window.Views.segmentation = (function () {
  const C = window.Common;
  let imgId = null;

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div class="col">
            <div class="panel"><div class="panel-title">选择图像</div><div id="sg-gallery" style="max-height:420px;overflow:auto"></div></div>
            <div class="panel">
              <div class="panel-title">分割参数</div>
              <div class="field"><label>方法</label><select id="sg-method">
                <option value="threshold">阈值分割（Otsu/手动）</option>
                <option value="region">区域生长（自适应局部阈值）</option>
                <option value="color">颜色聚类</option>
              </select></div>
              <div class="field"><label>阈值（threshold 方法）</label><input type="range" id="sg-value" min="0" max="255" value="127"></div>
              <div class="field"><label>局部块大小（region 方法）</label><input type="range" id="sg-block" min="3" max="31" step="2" value="15"></div>
              <div class="field"><label>颜色数（color 方法）</label><input type="range" id="sg-colors" min="2" max="12" value="6"></div>
              <div class="field"><label>叠加透明度</label><input type="range" id="sg-alpha" min="0" max="1" step="0.05" value="0.45"></div>
              <button class="btn btn-primary" id="sg-run">执行分割</button>
            </div>
          </div>
          <div class="col">
            <div class="panel"><div class="panel-title">分割结果</div><div class="stage" id="sg-result"><span class="dim">选择图像并执行</span></div></div>
            <div class="panel"><div class="panel-title">区域统计</div><div id="sg-stats" class="keypoint-stats"><span class="dim">暂无</span></div></div>
          </div>
        </div>`;

      C.fetchImages().then((images) => {
        el.querySelector("#sg-gallery").innerHTML = C.galleryHTML(images);
        C.bindGallery(el.querySelector("#sg-gallery"), images, (id) => { imgId = id; });
      });

      el.querySelector("#sg-run").onclick = async () => {
        if (!imgId) { C.toast("请选择图像", "error"); return; }
        el.querySelector("#sg-result").innerHTML = `<div class="loading">分割中…</div>`;
        const r = await Api.post("/api/segment", {
          image_id: imgId, method: el.querySelector("#sg-method").value,
          value: Number(el.querySelector("#sg-value").value),
          block: Number(el.querySelector("#sg-block").value),
          colors: Number(el.querySelector("#sg-colors").value),
          alpha: Number(el.querySelector("#sg-alpha").value),
        });
        el.querySelector("#sg-result").innerHTML =
          `<img src="/api/results/${r.result_id}/file?t=${Date.now()}"><div class="caption">${r.region_count} 个区域 · 覆盖率 ${(r.coverage * 100).toFixed(1)}%</div>`;
        el.querySelector("#sg-stats").innerHTML =
          `<div style="margin-bottom:6px">区域数：<strong>${r.region_count}</strong> · 覆盖率：<strong>${(r.coverage * 100).toFixed(1)}%</strong></div>` +
          (r.regions || []).slice(0, 12).map((rg) =>
            `<div>▸ 区域 #${rg.id} <span class="dim">面积 ${rg.area}px · 主色 rgb(${rg.mean_color.map(Math.round).join(",")})</span></div>`).join("");
      };
    },
  };
})();
