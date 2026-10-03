/* 应用外壳：侧边栏路由、视图注册与挂载、健康检查。 */
(function () {
  const NAV = [
    { name: "upload", ico: "🖼️", title: "图像管理" },
    { name: "pipeline", ico: "🔗", title: "滤镜链编辑器" },
    { name: "features", ico: "✨", title: "特征提取" },
    { name: "detection", ico: "🎯", title: "目标检测与标注" },
    { name: "segmentation", ico: "🧩", title: "图像分割" },
    { name: "style", ico: "🎨", title: "风格迁移" },
    { name: "batch", ico: "📦", title: "批量处理" },
    { name: "compare", ico: "⚖️", title: "结果对比" },
    { name: "tuning", ico: "🎚️", title: "参数调优与预设" },
    { name: "history", ico: "🕘", title: "历史记录与版本" },
  ];

  const Views = window.Views;
  const App = window.App = {
    current: null,
    show(name) {
      const view = Views[name];
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      const section = document.querySelector(`.view[data-view="${name}"]`);
      if (section) section.classList.add("active");
      document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === name));
      const item = NAV.find((n) => n.name === name);
      document.getElementById("pageTitle").textContent = item ? item.title : "";
      if (view) {
        if (!view.mounted) { view.mount(section); view.mounted = true; }
        else if (view.refresh) view.refresh();
      }
      App.current = name;
    },
  };

  function buildNav() {
    const nav = document.getElementById("nav");
    nav.innerHTML = NAV.map((n) =>
      `<div class="nav-item" data-view="${n.name}"><span class="nav-ico">${n.ico}</span>${n.title}</div>`
    ).join("");
    nav.addEventListener("click", (e) => {
      const it = e.target.closest(".nav-item");
      if (it) App.show(it.dataset.view);
    });
  }

  async function checkHealth() {
    const dot = document.getElementById("statusDot");
    const txt = document.getElementById("statusText");
    try {
      const d = await Api.get("/api/health");
      dot.classList.add("ok");
      txt.textContent = `就绪 · ${d.images} 张图 · ${d.results} 个结果`;
      document.getElementById("sidebarFoot").textContent =
        `图像 ${d.images} · 结果 ${d.results} · 历史 ${d.history} · 流水线 ${d.pipelines}`;
    } catch (e) {
      dot.classList.add("err");
      txt.textContent = "后端连接失败";
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    buildNav();
    checkHealth();
    App.show("upload");
  });
})();
