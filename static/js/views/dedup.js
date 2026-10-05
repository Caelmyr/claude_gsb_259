/* 视图：图库查重。两级比对（结构哈希粗筛 + 边缘对齐精验），阈值可调，分组列出并给出保留建议。 */
window.Views = window.Views || {};
window.Views.dedup = (function () {
  const C = window.Common;
  let pollTimer = null;
  let currentJob = null;
  let threshold = 86;

  const PRESETS = [
    { name: "严格", value: 92, desc: "几乎只报同图副本（缩放/重压缩/调色）" },
    { name: "标准", value: 86, desc: "副本 + 多数裁剪/翻拍，误判少" },
    { name: "宽松", value: 78, desc: "尽量多找含取景偏移的翻拍，需人工复核" },
  ];
  const PHASE_TEXT = {
    queued: "排队中", fingerprint: "提取指纹", scan: "粗筛候选",
    verify: "边缘对齐精验", done: "完成", cancelled: "已取消",
  };

  return {
    mount(el) {
      el.innerHTML = `
        <div class="panel">
          <div class="panel-title">查重设置
            <span class="dim">先按感知哈希粗筛，再对候选对做边缘对齐精验；颜色仅作辅助，色调接近但构图不同的图不会误判</span>
          </div>
          <div class="row">
            <div class="col" style="max-width:420px">
              <div class="field" style="margin-bottom:6px">
                <label>相似度阈值 <span class="hint">越高越严格（只报更像的）</span></label>
                <div class="range-row">
                  <input type="range" id="dd-thr" min="50" max="99" step="1" value="${threshold}">
                  <span class="range-val" id="dd-thr-val">${threshold}</span>                </div>
              </div>
              <div class="toolbar" style="margin:4px 0 0">
                ${PRESETS.map((p) => `<button class="btn btn-sm dd-preset" data-v="${p.value}" title="${C.esc(p.desc)}">${p.name} ${p.value}</button>`).join("")}
              </div>
            </div>
            <div class="col" style="display:flex;align-items:flex-end;justify-content:flex-end;gap:8px;flex-wrap:wrap">
              <button class="btn btn-primary" id="dd-start">🔍 扫描整个图库</button>
              <button class="btn btn-danger" id="dd-cancel" style="display:none">取消扫描</button>
            </div>
          </div>
        </div>
        <div id="dd-progress"></div>
        <div id="dd-result"></div>`;

      const slider = el.querySelector("#dd-thr");
      slider.oninput = () => { threshold = Number(slider.value); el.querySelector("#dd-thr-val").textContent = threshold; };
      el.querySelectorAll(".dd-preset").forEach((b) => b.onclick = () => {
        threshold = Number(b.dataset.v);
        slider.value = threshold;
        el.querySelector("#dd-thr-val").textContent = threshold;
      });
      el.querySelector("#dd-start").onclick = () => startScan(el);
      el.querySelector("#dd-cancel").onclick = () => cancelScan(el);

      Api.get("/api/dedup/config").then((cfg) => {
        slider.min = cfg.min_threshold; slider.max = cfg.max_threshold;
        if (cfg.default_threshold) {
          threshold = cfg.default_threshold;
          slider.value = threshold; el.querySelector("#dd-thr-val").textContent = threshold;
        }
      }).catch(() => {});

      restoreLatest(el);
    },

    refresh() {
      const el = document.querySelector('.view[data-view="dedup"]');
      if (el && this.mounted) restoreLatest(el);
    },
  };

  async function restoreLatest(el) {
    try {
      const jobs = (await Api.get("/api/dedup/jobs")).jobs;
      if (jobs.length) renderJob(el, jobs[0]);
    } catch (e) { /* 忽略 */ }
  }

  async function startScan(el) {
    const btn = el.querySelector("#dd-start");
    btn.disabled = true;
    try {
      const r = await Api.post("/api/dedup/scan", { threshold });
      C.toast("查重任务已开始", "success");
      const job = await Api.get(`/api/dedup/jobs/${r.job_id}`);
      renderJob(el, job);
      schedulePoll(el, r.job_id);
    } catch (e) {
      C.toast(e.message || "启动失败", "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function cancelScan(el) {
    if (!currentJob) return;
    await Api.post(`/api/dedup/jobs/${currentJob.id}/cancel`).catch(() => {});
    C.toast("已请求取消", "success");
  }

  function schedulePoll(el, jobId) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const section = document.querySelector('.view[data-view="dedup"]');
      if (!section || !section.classList.contains("active")) { clearInterval(pollTimer); pollTimer = null; return; }
      try {
        const job = await Api.get(`/api/dedup/jobs/${jobId}`);
        renderJob(el, job);
        if (["done", "partial", "cancelled", "error"].includes(job.status)) {
          clearInterval(pollTimer); pollTimer = null;
        }
      } catch (e) { /* 轮询失败继续 */ }
    }, 1200);
  }

  // ------------------------------------------------------------------ 渲染
  function renderJob(el, job) {
    currentJob = job;
    const running = ["queued", "running"].includes(job.status);
    el.querySelector("#dd-cancel").style.display = running ? "" : "none";
    renderProgress(el, job, running);
    if (job.groups) renderResult(el, job);
  }

  function renderProgress(el, job, running) {
    const box = el.querySelector("#dd-progress");
    if (["queued", "running"].includes(job.status) || job.status === "cancelled") {
      const phase = PHASE_TEXT[job.phase] || job.phase;
      let pct = 0, txt = "";
      if (job.phase === "fingerprint") {
        pct = job.total ? job.done / job.total * 100 : 0;
        txt = `提取图像指纹 ${job.done}/${job.total}`;
      } else if (job.phase === "scan") {
        pct = 99; txt = "LSH 粗筛候选对…";
      } else if (job.phase === "verify") {
        const t = job.phase_total || 0;
        pct = t ? job.phase_done / t * 100 : 5;
        txt = `边缘对齐精验 ${job.phase_done}/${t}`;
      } else if (job.phase === "cancelled") {
        pct = 100; txt = "已取消";
      } else {
        txt = phase;
      }
      box.innerHTML = `<div class="panel">
        <div style="display:flex;justify-content:space-between;margin-bottom:8px">
          <span class="badge">${C.esc(phase)}</span><span class="dim">${C.esc(txt)}</span>
        </div>
        <div class="progress"><div class="progress-bar" style="width:${Math.max(3, pct).toFixed(0)}%"></div></div>
      </div>`;
      return;
    }
    if (job.status === "error") {
      box.innerHTML = `<div class="panel"><span class="badge red">扫描失败</span> ${C.esc(job.warning || "")}</div>`;
      return;
    }
    box.innerHTML = "";
  }

  function renderResult(el, job) {
    const box = el.querySelector("#dd-result");
    const groups = job.groups || [];
    const statusBadge = {
      done: `<span class="badge green">完成</span>`,
      partial: `<span class="badge amber">完成（部分图像跳过或达候选上限）</span>`,
      cancelled: `<span class="badge amber">已取消（显示已完成部分）</span>`,
    }[job.status] || "";
    const head = `
      <div class="panel">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          ${statusBadge}
          <span class="badge">阈值 ${job.threshold}</span>
          <span style="font-weight:650">发现 ${job.group_count} 组疑似重复 · ${job.duplicate_count} 张副本</span>
          <span class="dim">扫描 ${job.image_count} 张 · 候选 ${job.candidate_pairs} 对 · 耗时 ${C.fmtMs(job.duration_ms)}</span>
          <span style="flex:1"></span>
          <span class="badge green">预计可节省 ${C.fmtBytes(job.waste_bytes)}</span>
        </div>
        ${job.warning ? `<div class="hint" style="color:var(--amber);margin-top:8px">⚠ ${C.esc(job.warning)}</div>` : ""}
        ${(job.skipped || []).length ? `<div class="hint" style="color:var(--text-faint);margin-top:6px">${job.skipped.length} 张图像无法解码已跳过</div>` : ""}
      </div>`;

    if (!groups.length) {
      box.innerHTML = head + `<div class="panel"><div class="empty"><span class="big">✅</span>当前阈值下没有发现疑似重复的图</div></div>`;
      return;
    }

    box.innerHTML = head + groups.map((g, gi) => groupHTML(g, gi)).join("");
    box.querySelectorAll(".dd-del-one").forEach((b) => {
      b.onclick = () => deleteOne(el, job, b.dataset.group, b.dataset.id);
    });
    box.querySelectorAll(".dd-del-rest").forEach((b) => {
      b.onclick = () => deleteRest(el, job, Number(b.dataset.group));
    });
  }

  function groupHTML(g, gi) {
    return `<div class="panel dd-group" data-group="${gi}">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px">
        <span class="badge">${C.esc(g.kind)}</span>
        <span style="font-weight:650">第 ${gi + 1} 组 · ${g.members.length} 张</span>
        <span class="dim">最高相似 ${g.max_score} · 均值 ${g.avg_score}</span>
        <span style="flex:1"></span>
        <span class="badge green">可省 ${C.fmtBytes(g.waste_bytes)}</span>
        <button class="btn btn-sm btn-danger dd-del-rest" data-group="${gi}">删除其余副本</button>
      </div>
      <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(180px,1fr))">
        ${g.members.map((m) => `
          <div class="card ${m.is_keeper ? "selected" : ""}" style="cursor:default">
            <img class="thumb" src="${C.esc(m.thumbnail_url)}" loading="lazy" draggable="false"
                 onclick="window.open('${C.esc(m.file_url)}','_blank')" title="点击查看原图"
                 style="cursor:zoom-in">
            ${m.is_keeper ? `<span class="card-badge" style="background:rgba(53,199,120,.9)">⭐ 建议保留</span>`
                          : `<span class="card-badge">副本</span>`}
            <div class="card-meta">
              <div class="card-name" title="${C.esc(m.filename)}">${C.esc(m.filename)}</div>
              <div class="card-dim">${m.width}×${m.height} · ${C.fmtBytes(m.size_bytes)} · 清晰度 ${Math.round(m.sharpness)}</div>
              ${m.is_keeper ? "" : `<button class="btn btn-sm btn-danger dd-del-one" style="margin-top:6px;width:100%"
                 data-group="${gi}" data-id="${C.esc(m.id)}">删除这张</button>`}
            </div>
          </div>`).join("")}
      </div>
    </div>`;
  }

  // ------------------------------------------------------------------ 删除
  async function deleteOne(el, job, gi, id) {
    const g = job.groups[Number(gi)];
    const m = g.members.find((x) => x.id === id);
    if (!m || !confirm(`确定删除「${m.filename}」？\n保留建议中的图片不会受影响，删除不可恢复。`)) return;
    await Api.del(`/api/images/${id}`);
    C.toast("已删除", "success");
    C.refreshImages();
    g.members = g.members.filter((x) => x.id !== id);
    g.edges = (g.edges || []).filter((e) => e.a !== id && e.b !== id);
    if (g.members.length <= 1) {
      job.groups = job.groups.filter((_, k) => k !== Number(gi));
      job.group_count = job.groups.length;
      job.duplicate_count = job.groups.reduce((s, x) => s + x.members.length - 1, 0);
    }
    renderResult(el, job);
  }

  async function deleteRest(el, job, gi) {
    const g = job.groups[gi];
    const rest = g.members.filter((m) => !m.is_keeper);
    if (!rest.length) { C.toast("该组只剩建议保留的一张"); return; }
    if (!confirm(`将删除该组除建议保留图外的 ${rest.length} 张副本，确定吗？\n（建议先点开图片确认）`)) return;
    let fail = 0;
    for (const m of rest) {
      try { await Api.del(`/api/images/${m.id}`); } catch (e) { fail += 1; }
    }
    C.refreshImages();
    C.toast(fail ? `完成，${fail} 张删除失败` : `已删除 ${rest.length} 张副本`, fail ? "error" : "success");
    job.groups = job.groups.filter((_, k) => k !== gi);
    job.group_count = job.groups.length;
    job.duplicate_count = job.groups.reduce((s, x) => s + x.members.length - 1, 0);
    renderResult(el, job);
  }
})();
