/* 视图：查重清理。
 * 后台扫描全库 -> 按相似度分组 -> 推荐保留分辨率最大/最清晰的一张；
 * 可拖动阈值，扫描进度实时轮询，结果可逐组或一键清理。 */
window.Views = window.Views || {};
window.Views.dedup = (function () {
  const C = window.Common;
  let pollTimer = null;
  let currentJobId = null;
  let lastGroups = [];

  return {
    mount(el) {
      el.innerHTML = `
        <div class="panel">
          <div class="panel-title">查重扫描<span class="dim">按图像内容比对，识别翻拍 / 裁剪 / 调色 / 缩放副本</span></div>
          <div class="dedup-controls">
            <div class="dedup-thresh">
              <div class="field" style="margin:0;flex:1">
                <label>相似度阈值 <span class="hint">越高越严格，仅判定近乎相同的图；调低可纳入更多翻拍/裁边副本</span></label>
                <div class="range-row">
                  <input type="range" id="dd-thresh" min="50" max="99" step="1" value="90">
                  <span class="range-val" id="dd-thresh-val">90%</span>
                </div>
                <div class="dedup-presets">
                  <button class="btn btn-sm dd-preset" data-v="95">极严 95%</button>
                  <button class="btn btn-sm dd-preset" data-v="90">严格 90%</button>
                  <button class="btn btn-sm dd-preset" data-v="85">标准 85%</button>
                  <button class="btn btn-sm dd-preset" data-v="80">宽松 80%</button>
                </div>
              </div>
              <div class="dedup-actions">
                <button class="btn btn-primary" id="dd-scan">开始扫描</button>
                <label class="dd-force"><input type="checkbox" id="dd-force"> 重建指纹</label>
              </div>
            </div>
            <div class="hint" style="color:var(--text-faint);font-size:12px">
              指纹会缓存，新增图片后重扫很快；勾选「重建指纹」可强制重算全部图片。
            </div>
          </div>
          <div id="dd-progress" hidden>
            <div class="toolbar" style="margin:12px 0 6px">
              <span class="badge" id="dd-stage"></span>
              <span class="dim" id="dd-count"></span>
              <span style="flex:1"></span>
              <button class="btn btn-sm btn-danger" id="dd-cancel">取消扫描</button>
            </div>
            <div class="progress"><div class="progress-bar" id="dd-bar" style="width:0%"></div></div>
          </div>
        </div>

        <div class="panel" id="dd-summary-panel" hidden>
          <div class="toolbar" style="margin-bottom:10px">
            <div id="dd-summary"></div>
            <span style="flex:1"></span>
            <button class="btn btn-sm btn-danger" id="dd-cleanall">一键清理所有副本（保留推荐项）</button>
          </div>
        </div>

        <div id="dd-groups"></div>
        <div id="dd-history"></div>`;

      const thresh = el.querySelector("#dd-thresh");
      const threshVal = el.querySelector("#dd-thresh-val");
      thresh.addEventListener("input", () => { threshVal.textContent = thresh.value + "%"; });
      el.querySelectorAll(".dd-preset").forEach((b) => b.onclick = () => {
        thresh.value = b.dataset.v; threshVal.textContent = b.dataset.v + "%";
      });
      el.querySelector("#dd-scan").onclick = () => startScan(el);
      el.querySelector("#dd-cancel").onclick = () => cancelScan(el);
      el.querySelector("#dd-cleanall").onclick = () => cleanAll(el);

      loadHistory(el);
      // 默认载入最近一次已完成扫描的结果
      Api.get("/api/dedup/jobs").then((d) => {
        const done = d.jobs.find((j) => j.status === "done");
        if (done) loadResult(el, done.id);
        const running = d.jobs.find((j) => j.status === "queued" || j.status === "running");
        if (running) attachJob(el, running.id);
      }).catch(() => {});
    },

    refresh() {
      const el = document.querySelector('.view[data-view="dedup"]');
      if (el && this.mounted) loadHistory(el);
    },
  };

  // ---------------------------------------------------------------- 扫描控制
  async function startScan(el) {
    const threshold = Number(el.querySelector("#dd-thresh").value);
    const force = el.querySelector("#dd-force").checked;
    try {
      const r = await Api.post("/api/dedup/scan", { threshold, force_refresh: force });
      C.toast("扫描已开始", "success");
      attachJob(el, r.job_id);
    } catch (e) { C.toast(e.message || "启动失败", "error"); }
  }

  async function cancelScan(el) {
    if (!currentJobId) return;
    await Api.post(`/api/dedup/jobs/${currentJobId}/cancel`);
    C.toast("已请求取消", "success");
  }

  function attachJob(el, jobId) {
    currentJobId = jobId;
    el.querySelector("#dd-progress").hidden = false;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => pollJob(el, jobId), 700);
    pollJob(el, jobId);
  }

  async function pollJob(el, jobId) {
    let j;
    try { j = await Api.get(`/api/dedup/jobs/${jobId}`); } catch (e) { return; }
    const box = el.querySelector("#dd-progress");
    if (j.status === "queued" || j.status === "running") {
      box.hidden = false;
      el.querySelector("#dd-stage").textContent = j.stage || "排队中";
      const pct = j.total ? Math.round(j.done / j.total * 100) : 0;
      el.querySelector("#dd-bar").style.width = pct + "%";
      el.querySelector("#dd-count").textContent = `${j.done}/${j.total} 张` + (j.errors ? ` · ${j.errors} 张无法解析` : "");
      return;
    }
    // 结束
    clearInterval(pollTimer); pollTimer = null;
    box.hidden = true;
    if (j.status === "done") {
      C.toast("扫描完成", "success");
      loadResult(el, jobId);
    }
    loadHistory(el);
  }

  // ---------------------------------------------------------------- 结果
  async function loadResult(el, jobId) {
    currentJobId = jobId;
    let j;
    try { j = await Api.get(`/api/dedup/jobs/${jobId}?groups=1`); } catch (e) { return; }
    const s = j.summary || {};
    const res = j.result || { groups: [], errors: [] };
    lastGroups = res.groups || [];

    const panel = el.querySelector("#dd-summary-panel");
    panel.hidden = false;
    el.querySelector("#dd-summary").innerHTML = `
      <span class="badge green">扫描完成</span>
      <span class="dim">阈值 ${s.threshold}% · 扫描 ${s.fingerprinted}/${s.image_count} 张
      · 发现 <strong>${s.group_count}</strong> 组疑似重复，共 ${s.duplicate_files || 0} 个副本
      · 可回收约 <strong>${C.fmtBytes(s.waste_bytes)}</strong></span>`;

    const box = el.querySelector("#dd-groups");
    if (!lastGroups.length) {
      box.innerHTML = `<div class="panel"><div class="empty"><span class="big">🎉</span>
        当前阈值（${s.threshold}%）下未发现疑似重复图片<br>
        若怀疑有翻拍/裁剪副本，可适当调低阈值后重扫</div></div>`;
      return;
    }
    box.innerHTML = lastGroups.map((g) => groupHTML(g)).join("");
    bindGroupEvents(el);
    if (res.errors && res.errors.length) {
      box.insertAdjacentHTML("beforeend", `<div class="panel"><div class="panel-title">无法解析（${res.errors.length} 张，已跳过）</div>
        <div class="dim" style="font-size:12px">${res.errors.slice(0, 8).map((e) => C.esc(e.id.slice(0, 12)) + "…").join("、")}${res.errors.length > 8 ? " 等" : ""}</div></div>`);
    }
  }

  function levelBadge(sim) {
    if (sim >= 97) return `<span class="badge green">近乎相同 ${sim}%</span>`;
    if (sim >= 90) return `<span class="badge">高度相似 ${sim}%</span>`;
    return `<span class="badge amber">疑似 ${sim}%</span>`;
  }

  function groupHTML(g) {
    const cards = g.members.map((m) => `
      <div class="dd-card ${m.is_keeper ? "keeper" : ""}" data-gid="${C.esc(g.id)}" data-id="${C.esc(m.id)}">
        <div class="dd-check"><input type="checkbox" class="dd-pick" ${m.is_keeper ? "disabled" : "checked"}></div>
        <img class="thumb" src="${C.esc(m.thumbnail_url)}" loading="lazy">
        <div class="dd-card-body">
          <div class="dd-card-name" title="${C.esc(m.filename)}">${m.is_keeper ? "⭐ " : ""}${C.esc(m.filename)}</div>
          <div class="dd-card-meta">${m.width}×${m.height} · ${C.fmtBytes(m.size_bytes)} · 清晰 ${Math.round(m.sharpness)}</div>
          <div class="dd-card-sim">${m.is_keeper ? "<span class='dim'>推荐保留</span>" : levelBadge(m.similarity)}</div>
        </div>
      </div>`).join("");
    return `
      <div class="panel dd-group" data-gid="${C.esc(g.id)}">
        <div class="toolbar" style="margin-bottom:10px">
          <span class="dd-group-index badge"></span>
          ${levelBadge(g.min_similarity)}
          <span class="dim">建议保留：<strong>${C.esc(g.keeper_reason)}</strong></span>
          <span style="flex:1"></span>
          <span class="dim">本组可回收 ${C.fmtBytes(g.waste_bytes)}</span>
          <button class="btn btn-sm btn-danger dd-clean-group">删除勾选（${g.count - 1}）</button>
        </div>
        <div class="dd-grid">${cards}</div>
      </div>`;
  }

  // 组序号按当前 DOM 顺序动态标注（删除整组后自动连续）
  function renumberGroups(el) {
    el.querySelectorAll(".dd-group").forEach((gel, i) => {
      const b = gel.querySelector(".dd-group-index");
      if (b) b.textContent = `第 ${i + 1} 组 · ${gel.querySelectorAll(".dd-card").length} 张`;
    });
  }

  function bindGroupEvents(el) {
    el.querySelectorAll(".dd-clean-group").forEach((btn) => {
      btn.onclick = () => {
        const gel = btn.closest(".dd-group");
        const gid = gel.dataset.gid;
        const group = lastGroups.find((g) => g.id === gid);
        const toDelete = pickInGroup(el, gid);
        if (!group) return;
        if (!toDelete.length) { C.toast("没有勾选要删除的图片", "error"); return; }
        const keep = group.members.find((m) => m.is_keeper);
        if (!confirm(`确认删除该组勾选的 ${toDelete.length} 张副本？\n保留：${keep ? keep.filename : "（无）"}\n删除后可在「图像管理」相关记录中看到变化。`)) return;
        doDelete(el, toDelete, gid);
      };
    });
    // 勾选状态实时更新按钮计数
    el.querySelectorAll(".dd-group").forEach((gel) => {
      const gid = gel.dataset.gid;
      gel.querySelectorAll(".dd-pick").forEach((cb) => cb.addEventListener("change", () => {
        const n = pickInGroup(el, gid).length;
        gel.querySelector(".dd-clean-group").textContent = `删除勾选（${n}）`;
      }));
    });
    renumberGroups(el);
  }

  function pickInGroup(el, gid) {
    return Array.from(el.querySelectorAll(`.dd-group[data-gid="${CSS.escape(gid)}"] .dd-pick:checked`))
      .map((cb) => cb.closest(".dd-card").dataset.id);
  }

  async function doDelete(el, ids, gid) {
    try {
      const r = await Api.post("/api/images/bulk-delete", { ids });
      C.toast(`已删除 ${r.deleted.length} 张`, "success");
      C.refreshImages();
      // 同步内存结果，再从 DOM 移除已删卡片
      const group = lastGroups.find((g) => g.id === gid);
      if (group) group.members = group.members.filter((m) => !ids.includes(m.id));
      const gel = el.querySelector(`.dd-group[data-gid="${CSS.escape(gid)}"]`);
      if (gel) {
        ids.forEach((id) => gel.querySelector(`.dd-card[data-id="${id}"]`)?.remove());
        if (gel.querySelectorAll(".dd-card").length < 2) gel.remove();
      }
      lastGroups = lastGroups.filter((g) => g.members.length >= 2);
      renumberGroups(el);
      if (!el.querySelectorAll(".dd-group").length) {
        el.querySelector("#dd-summary-panel").hidden = true;
        el.querySelector("#dd-groups").innerHTML = `<div class="panel"><div class="empty">已清理完毕</div></div>`;
      }
    } catch (e) { C.toast(e.message || "删除失败", "error"); }
  }

  async function cleanAll(el) {
    const allIds = [];
    lastGroups.forEach((g) => allIds.push(...pickInGroup(el, g.id)));
    if (!allIds.length) { C.toast("没有可删除的副本", "error"); return; }
    if (!confirm(`确认删除全部 ${allIds.length} 张勾选副本？\n每组保留带 ⭐ 的推荐项。此操作不可撤销。`)) return;
    try {
      const r = await Api.post("/api/images/bulk-delete", { ids: allIds });
      C.toast(`已删除 ${r.deleted.length} 张副本`, "success");
      C.refreshImages();
      el.querySelector("#dd-summary-panel").hidden = true;
      el.querySelector("#dd-groups").innerHTML = `<div class="panel"><div class="empty"><span class="big">🎉</span>清理完成</div></div>`;
      lastGroups = [];
      loadHistory(el);
    } catch (e) { C.toast(e.message || "删除失败", "error"); }
  }

  // ---------------------------------------------------------------- 历史
  async function loadHistory(el) {
    let jobs = [];
    try { jobs = (await Api.get("/api/dedup/jobs")).jobs; } catch (e) { return; }
    const box = el.querySelector("#dd-history");
    if (!jobs.length) { box.innerHTML = ""; return; }
    const statusText = { queued: "排队中", running: "扫描中", done: "完成", partial: "部分失败", cancelled: "已取消" };
    const badge = { done: "green", running: "", queued: "", cancelled: "amber", partial: "amber" };
    box.innerHTML = `<div class="panel"><div class="panel-title">历史扫描</div>
      <div class="table-wrap"><table class="table">
      <thead><tr><th>时间</th><th>阈值</th><th>状态</th><th>发现</th><th>可回收</th><th></th></tr></thead>
      <tbody>${jobs.slice(0, 12).map((j) => {
        const s = j.summary || {};
        return `<tr>
          <td>${C.fmtDate(j.created_at)}</td>
          <td>${j.threshold}%</td>
          <td><span class="badge ${badge[j.status] || "red"}">${statusText[j.status] || j.status}</span>
              ${j.errors ? `<span class="dim"> ${j.errors} 张跳过</span>` : ""}</td>
          <td>${s.group_count != null ? `${s.group_count} 组 / ${s.duplicate_files} 副本` : "-"}</td>
          <td>${s.waste_bytes != null ? C.fmtBytes(s.waste_bytes) : "-"}</td>
          <td>${j.status === "done" ? `<button class="btn btn-sm dd-view" data-id="${j.id}">查看</button>` : ""}</td>
        </tr>`;
      }).join("")}</tbody></table></div></div>`;
    box.querySelectorAll(".dd-view").forEach((b) => b.onclick = () => loadResult(el, b.dataset.id));
  }
})();
