/* 视图 3：特征提取（SIFT/ORB 模拟）与关键点匹配。 */
window.Views = window.Views || {};
window.Views.features = (function () {
  const C = window.Common;
  let imgA = null, imgB = null;

  return {
    mount(el) {
      el.innerHTML = `
        <div class="row">
          <div class="col" style="flex:1;min-width:0">
            <div class="panel">
              <div class="panel-title">选择图像 A</div>
              <div id="fe-a"></div>
            </div>
            <div class="panel">
              <div class="panel-title">提取参数</div>
              <div class="select-row">
                <select id="fe-method"><option value="sift">SIFT（128 维描述子）</option><option value="orb">ORB（256 位二进制描述子）</option></select>
                <input type="number" id="fe-max" value="120" min="20" max="500" style="width:110px" title="最大关键点数">
                <button class="btn btn-primary" id="fe-run">提取关键点</button>
              </div>
            </div>
            <div class="panel">
              <div class="panel-title">提取结果</div>
              <div class="stage" id="fe-result"><span class="dim">选择图像并提取</span></div>
            </div>
          </div>
          <div class="col" style="flex:1;min-width:0">
            <div class="panel">
              <div class="panel-title">关键点信息</div>
              <div id="fe-stats" class="keypoint-stats"><span class="dim">尚未提取</span></div>
              <div id="fe-desc" class="desc-preview" style="margin-top:8px"></div>
            </div>
            <div class="panel">
              <div class="panel-title">双图匹配（选择图像 B）</div>
              <div id="fe-b" style="max-height:220px;overflow:auto"></div>
              <div class="toolbar" style="margin-top:8px;margin-bottom:0">
                <button class="btn" id="fe-match">匹配两图</button>
              </div>
            </div>
            <div class="panel">
              <div class="panel-title">匹配结果</div>
              <div class="stage" id="fe-match-result"><span class="dim">选择两图后匹配</span></div>
            </div>
          </div>
        </div>`;

      loadA(el);

      el.querySelector("#fe-run").onclick = async () => {
        if (!imgA) { C.toast("请选择图像 A", "error"); return; }
        const box = el.querySelector("#fe-result");
        box.innerHTML = `<div class="loading">提取中…</div>`;
        const r = await Api.post("/api/features", {
          image_id: imgA, method: el.querySelector("#fe-method").value,
          max_points: Number(el.querySelector("#fe-max").value),
        });
        box.innerHTML = `<img src="/api/results/${r.result_id}/file?t=${Date.now()}">
          <div class="caption">${r.method.toUpperCase()} · ${r.count} 个关键点</div>`;
        const stats = el.querySelector("#fe-stats");
        stats.innerHTML = `
          <div>算法：<strong>${C.esc(r.method.toUpperCase())}</strong></div>
          <div>关键点数量：<strong>${r.count}</strong></div>
          <div>描述子维度：<strong>${r.descriptor_dim}</strong></div>
          <div>坐标范围：${r.keypoints.length ? `(${r.keypoints[0].x}, ${r.keypoints[0].y}) …` : "-"}</div>`;
        if (r.sample_descriptor && r.sample_descriptor.length) {
          el.querySelector("#fe-desc").innerHTML =
            `<div style="color:var(--text-faint);margin-bottom:4px">首个描述子（前 ${Math.min(24, r.sample_descriptor.length)} 维）：</div>` +
            r.sample_descriptor.slice(0, 24).join(" ");
        }
      };

      el.querySelector("#fe-match").onclick = async () => {
        if (!imgA || !imgB) { C.toast("请分别选择图像 A 和 B", "error"); return; }
        const box = el.querySelector("#fe-match-result");
        box.innerHTML = `<div class="loading">匹配中…</div>`;
        const r = await Api.post("/api/features/match", {
          image_id_a: imgA, image_id_b: imgB,
          method: el.querySelector("#fe-method").value,
          max_matches: 40,
        });
        box.innerHTML = `<img src="/api/results/${r.result_id}/file?t=${Date.now()}">
          <div class="caption">匹配到 ${r.count} 对（A 图 ${r.count_a} 点 / B 图 ${r.count_b} 点）</div>`;
      };
    },

    refresh() { const el = document.querySelector('.view[data-view="features"]'); if (el && this.mounted) loadA(el), loadB(el); },
  };

  async function loadA(el) {
    const images = await C.fetchImages();
    el.querySelector("#fe-a").innerHTML = C.galleryHTML(images, { compact: true });
    C.bindGallery(el.querySelector("#fe-a"), images, (id) => { imgA = id; });
  }
  async function loadB(el) {
    const images = await C.fetchImages();
    el.querySelector("#fe-b").innerHTML = C.galleryHTML(images, { compact: true });
    C.bindGallery(el.querySelector("#fe-b"), images, (id) => { imgB = id; });
  }
})();
