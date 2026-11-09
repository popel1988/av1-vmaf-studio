/* Native file paths are issued by the trusted host, never inferred from File.name. */
(function () {
  "use strict";
  const seen = new Set();
  let capabilities = null;
  let api = null;
  let handling = Promise.resolve();
  let pollTimer;
  const translate = (s) => window.I18N ? window.I18N.t(s) : s;
  const nativeApi = () => window.pywebview?.api || null;
  function error(message) {
    const status = document.getElementById("desktop-status");
    if (status) status.textContent = message;
  }
  async function openEvent(detail) {
    if (!detail || !detail.id || seen.has(detail.id) || !Array.isArray(detail.paths)) return;
    seen.add(detail.id);
    const host = document.getElementById("desktop-file-list");
    for (const path of detail.paths) {
      if (typeof path !== "string") continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-ghost btn-sm";
      button.textContent = path.split(/[/\\]/).pop();
      button.title = path;
      button.addEventListener("click", () => window.StudioExport.openSource(path).catch((e) => error(e.message)));
      if (host) host.append(button);
    }
    // Keep all paths available, but do not silently replace an existing selection.
    if (detail.paths.length && !window.StudioExport.selection()) {
      await window.StudioExport.openSource(detail.paths[0]);
    }
    error(detail.paths.length + " Datei(en) ohne Upload geöffnet.");
  }
  function receive(detail) {
    handling = handling.then(() => openEvent(detail)).catch((e) => error(String(e.message || e)));
  }
  function mount(value) {
    capabilities = value;
    let panel = document.getElementById("desktop-tools");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "desktop-tools";
      panel.className = "workspace-desktop-tools";
      const title = document.createElement("strong");
      title.textContent = translate("Desktop-Funktionen");
      const select = document.createElement("button");
      select.type = "button";
      select.id = "desktop-select-files";
      select.className = "btn btn-ghost btn-sm";
      select.textContent = translate("Dateien öffnen");
      select.addEventListener("click", async () => {
        try {
          const result = await nativeApi().select_files();
          if (result.error) throw new Error(result.error);
          receive(result);
        } catch (e) { error(e.message); }
      });
      const register = document.createElement("button");
      register.type = "button";
      register.id = "desktop-register-open-with";
      register.className = "btn btn-ghost btn-sm";
      register.textContent = translate("Öffnen mit registrieren");
      register.addEventListener("click", async () => {
        try {
          const result = await nativeApi().register_open_with();
          error(result.cancelled ? "Registrierung abgebrochen." : result.ok ? "Öffnen mit registriert." : result.error);
        } catch (e) { error(e.message); }
      });
      const status = document.createElement("span");
      status.id = "desktop-status";
      status.className = "muted";
      const files = document.createElement("div");
      files.id = "desktop-file-list";
      panel.append(title, select, register, status, files);
      document.querySelector(".topbar").after(panel);
    }
    document.getElementById("desktop-select-files").hidden = !value.file_dialog || !nativeApi();
    document.getElementById("desktop-register-open-with").hidden = !value.open_with_registration || !nativeApi();
    error(value.native_host
      ? "Natives Fenster · Dateien aus dem Explorer hierher ziehen (ohne Upload)."
      : "Browser-Rückfallmodus · Explorer-Drop nicht verfügbar; Dateiöffnung über „Öffnen mit“ möglich.");
  }
  async function attachNative() {
    api = nativeApi();
    if (!api) return;
    if (pollTimer) clearTimeout(pollTimer);
    try {
      mount(await api.capabilities());
      (await api.take_pending()).forEach(receive);
    } catch (e) { error(e.message); }
  }
  async function poll() {
    if (nativeApi()) { await attachNative(); return; }
    try {
      const response = await fetch("/api/desktop/pending", {
        method: "POST", headers: { "X-Studio-Desktop": "1" },
      });
      if (!response.ok) return;
      const result = await response.json();
      if (result.capabilities) mount(result.capabilities);
      (result.events || []).forEach(receive);
      pollTimer = setTimeout(poll, 2000);
    } catch (_) { /* Regular web/Docker sessions do not expose a desktop bridge. */ }
  }
  window.addEventListener("studio:open-paths", (event) => receive(event.detail));
  window.addEventListener("studio:desktop-capabilities", (event) => mount(event.detail));
  window.addEventListener("studio:desktop-error", (event) => error(event.detail?.message || "Desktop-Fehler"));
  window.addEventListener("pywebviewready", attachNative);
  document.addEventListener("DOMContentLoaded", () => {
    if (nativeApi()) attachNative();
    else poll();
  });
  window.StudioDesktop = { receive, capabilities: () => capabilities };
})();
