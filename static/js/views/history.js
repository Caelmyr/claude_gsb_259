/* 视图 10：历史记录与版本。 */
window.Views = window.Views || {};
window.Views.history = (function () {
  const C = window.Common;
  let current = null;

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div class="col" style="flex:1.6">
            <div class="panel">
              <div class="panel-title">处理历史<span class="dim">每条含当时的流水线快照（可恢复版本）</span></div>
              <div id="hi-list"></div>
            </div>
          </div>
          <div class="col">
            <div class="panel"><div class="panel-title">记录详情</div><div id="hi-detail"><div class="empty">选择左侧记录查看</div></div></div>
          </div>
        </div>`;
      load(el);
    },
    refresh() { const el = document.querySelector('.view[data-view="history"]'); if (el && this.mounted) load(el); },
  };

  async function load(el) {
    const r = await Api.get("/api/history");
    const list = r.history || [];
    const box = el.querySelector("#hi-list");
    if (!list.length) { box.innerHTML = `<div class="empty"><span class="big">🕘</span>暂无历史记录</div>`; return; }
    box.innerHTML = `<table class="table"><thead><tr>
      <th>时间</th><th>图像</th><th>流水线</th><th>节点</th><th>耗时</th><th>状态</th><th></th>
    </tr></thead><tbody>` + list.map((e) => `
      <tr data-id="${e.id}" style="cursor:pointer">
        <td class="mono">${C.fmtDate(e.created_at)}</td>
        <td title="${C.esc(e.image_name)}">${C.esc((e.image_name || "").slice(0, 18))}</td>
        <td>${C.esc(e.pipeline_name || "临时")}</td>
        <td>${e.node_count}</td>
        <td>${C.fmtMs(e.duration_ms)}</td>
        <td><span class="badge ${e.status === "ok" ? "green" : "red"}">${e.status === "ok" ? "成功" : "失败"}</span>${e.cache_hit ? ' <span class="badge">缓存</span>' : ""}</td>
        <td><button class="btn btn-sm" data-id="${e.id}">查看</button></td>
      </tr>`).join("") + `</tbody></table>`;

    box.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.onclick = () => {
        current = list.find((x) => x.id === tr.dataset.id);
        box.querySelectorAll("tr").forEach((x) => x.style.background = "");
        tr.style.background = "var(--bg-hover)";
        renderDetail(el, current);
      };
    });
    if (current) renderDetail(el, current);
  }

  function renderDetail(el, e) {
    const box = el.querySelector("#hi-detail");
    const nodes = (e.pipeline_snapshot && e.pipeline_snapshot.nodes) || [];
    const nodeResults = e.node_results || [];
    box.innerHTML = `
      <div class="keypoint-stats" style="line-height:1.9">
        <div><span class="dim">状态</span> <span class="badge ${e.status === "ok" ? "green" : "red"}">${e.status === "ok" ? "成功" : "失败"}</span></div>
        <div><span class="dim">图像</span> ${C.esc(e.image_name || "-")}</div>
        <div><span class="dim">流水线</span> ${C.esc(e.pipeline_name || "临时")}（${e.node_count} 节点）</div>
        <div><span class="dim">耗时</span> ${C.fmtMs(e.duration_ms)} · <span class="dim">缓存</span> ${e.cache_hit ? "命中" : "计算"}</div>
        ${e.error ? `<div><span class="dim">错误</span> ${C.esc(e.error)}</div>` : ""}
        <div><span class="dim">版本快照</span> ${nodes.map((n) => `<span class="badge">${C.esc(n.type)}</span>`).join(" ") || "无节点"}</div>
        ${nodeResults.length ? `<div><span class="dim">节点执行</span> ${nodeResults.map((n) => `${n.ok ? "✓" : "✗"}${n.node_id}`).join(" ")}</div>` : ""}
      </div>
      ${e.result_id ? `<img src="/api/results/${e.result_id}/file" style="width:100%;border-radius:8px;margin-top:10px">` : ""}
      <div class="toolbar" style="margin-top:12px">
        <button class="btn" id="hi-restore">恢复为流水线</button>
        <button class="btn btn-danger" id="hi-del">删除记录</button>
      </div>`;
    box.querySelector("#hi-restore").onclick = async () => {
      const p = await Api.post(`/api/history/${e.id}/restore`);
      C.toast("已恢复为流水线：" + p.name, "success");
      await C.refreshPipelines();
    };
    box.querySelector("#hi-del").onclick = async () => {
      if (!confirm("删除该历史记录（连同结果文件）？")) return;
      await Api.del(`/api/history/${e.id}`);
      current = null;
      C.toast("已删除", "success");
      load(document.querySelector('.view[data-view="history"]'));
    };
  }
})();
