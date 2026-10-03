/* API 客户端：统一的 fetch 封装。 */
window.Api = (function () {
  async function request(method, url, body, isForm) {
    const opts = { method, headers: {} };
    if (isForm) {
      opts.body = body;
    } else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    const ct = resp.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await resp.json() : await resp.blob();
    if (!resp.ok) {
      const msg = (data && data.error) ? data.error : ("HTTP " + resp.status);
      throw new Error(msg);
    }
    return data;
  }
  return {
    get: (u) => request("GET", u),
    post: (u, b) => request("POST", u, b),
    put: (u, b) => request("PUT", u, b),
    patch: (u, b) => request("PATCH", u, b),
    del: (u) => request("DELETE", u),
    upload(files) {
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      return request("POST", "/api/images", fd, true);
    },
  };
})();
