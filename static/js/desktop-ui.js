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

  const ACTION_LABELS = {
    none: "Nach Abschluss: Nichts tun",
    sleep: "Nach Abschluss: Energie sparen",
    hibernate: "Nach Abschluss: Ruhezustand",
    shutdown: "Nach Abschluss: Herunterfahren",
  };

  function error(message) {
    const status = document.getElementById("desktop-status");
    if (status) status.textContent = message;
  }

  async function selectFolderAction() {
    const hostApi = nativeApi();
    if (!hostApi || typeof hostApi.select_folder !== "function") return null;
    const btn = document.getElementById("desktop-select-folder");
    if (btn) btn.disabled = true;
    try {
      const result = await hostApi.select_folder();
      if (result.error || !result.ok) {
        throw new Error(result.error || "Ordner konnte nicht hinzugefügt werden.");
      }
      if (!result.cancelled) {
        error("Medienordner gespeichert: " + result.path);
        renderFolders(result.folders || []);
      }
      return result;
    } catch (e) {
      error(e.message || String(e));
      return null;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderFolders(folders) {
    const cont = document.getElementById("desktop-folder-list");
    if (!cont) return;
    cont.innerHTML = "";
    if (!Array.isArray(folders) || !folders.length) {
      cont.hidden = true;
      return;
    }
    cont.hidden = false;
    const label = document.createElement("span");
    label.className = "muted";
    label.style.fontSize = "12px";
    label.textContent = translate("Eigene Quellen:");
    cont.append(label);

    for (const entry of folders) {
      const folderPath = typeof entry === "string" ? entry : entry?.path;
      if (!folderPath) continue;
      const chip = document.createElement("span");
      chip.className = "desktop-folder-chip";
      const name = document.createElement("span");
      name.textContent = folderPath.replace(/[\\/]+$/, "").split(/[/\\]/).pop() || folderPath;
      name.title = folderPath;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "×";
      removeBtn.title = translate("Medienordner aus Liste entfernen") + ": " + folderPath;
      removeBtn.addEventListener("click", async () => {
        try {
          const res = await nativeApi()?.remove_folder?.(folderPath);
          if (res?.error || !res?.ok) throw new Error(res?.error || "Entfernen fehlgeschlagen.");
          renderFolders(res.folders || []);
          error("Medienordner entfernt: " + folderPath);
        } catch (e) {
          error(e.message || String(e));
        }
      });
      chip.append(name, removeBtn);
      cont.append(chip);
    }
  }

  function applyPowerState(state) {
    if (!state || typeof state !== "object") return;
    const sleepChk = document.getElementById("desktop-prevent-sleep");
    if (sleepChk && typeof state.prevent_sleep === "boolean") {
      sleepChk.checked = state.prevent_sleep;
    }
    const postSel = document.getElementById("desktop-post-action");
    if (postSel && state.post_queue_action) {
      postSel.value = state.post_queue_action;
    }
    const banner = document.getElementById("desktop-power-banner");
    const bannerTxt = document.getElementById("desktop-power-banner-text");
    if (banner && bannerTxt) {
      const cd = state.countdown;
      if (cd && cd.action && cd.action !== "none") {
        const actName = (ACTION_LABELS[cd.action] || cd.action).replace(/^Nach Abschluss:\s*/, "");
        bannerTxt.textContent = `⚡ Warteschlange abgeschlossen – ${actName} in ${cd.seconds} s …`;
        banner.hidden = false;
      } else {
        banner.hidden = true;
      }
    }
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

      const folder = document.createElement("button");
      folder.type = "button";
      folder.id = "desktop-select-folder";
      folder.className = "btn btn-ghost btn-sm";
      folder.textContent = translate("Ordner / Laufwerk hinzufügen");
      folder.title = translate("Ordner oder Laufwerk dauerhaft als Medienquelle hinzufügen");
      folder.addEventListener("click", () => { selectFolderAction(); });

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

      const powerWrap = document.createElement("div");
      powerWrap.id = "desktop-power-controls";
      powerWrap.className = "desktop-power-wrap";

      const sleepLabel = document.createElement("label");
      sleepLabel.style.display = "inline-flex";
      sleepLabel.style.alignItems = "center";
      sleepLabel.style.gap = "5px";
      sleepLabel.title = translate("Verhindert den Windows-Energiesparmodus während aktiver Aufträge");
      const sleepChk = document.createElement("input");
      sleepChk.type = "checkbox";
      sleepChk.id = "desktop-prevent-sleep";
      sleepChk.checked = true;
      sleepChk.addEventListener("change", async () => {
        try {
          const res = await nativeApi()?.set_prevent_sleep?.(sleepChk.checked);
          applyPowerState(res);
        } catch (e) { error(e.message); }
      });
      const sleepSpan = document.createElement("span");
      sleepSpan.textContent = translate("Standby verhindern");
      sleepLabel.append(sleepChk, sleepSpan);

      const postSel = document.createElement("select");
      postSel.id = "desktop-post-action";
      postSel.title = translate("Aktion automatisch ausführen, sobald alle Aufträge der Warteschlange fertig sind");
      for (const [val, text] of Object.entries(ACTION_LABELS)) {
        const opt = document.createElement("option");
        opt.value = val;
        opt.textContent = translate(text);
        postSel.append(opt);
      }
      postSel.addEventListener("change", async () => {
        try {
          const res = await nativeApi()?.set_post_queue_action?.(postSel.value);
          applyPowerState(res);
        } catch (e) { error(e.message); }
      });
      powerWrap.append(sleepLabel, postSel);

      const banner = document.createElement("div");
      banner.id = "desktop-power-banner";
      banner.className = "desktop-power-banner";
      banner.hidden = true;
      const bannerTxt = document.createElement("strong");
      bannerTxt.id = "desktop-power-banner-text";
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn btn-sm";
      cancelBtn.textContent = translate("Aktion abbrechen");
      cancelBtn.addEventListener("click", async () => {
        try {
          const res = await nativeApi()?.cancel_post_queue_action?.();
          applyPowerState(res);
          error("Geplante Energieaktion abgebrochen.");
        } catch (e) { error(e.message); }
      });
      banner.append(bannerTxt, cancelBtn);

      const folders = document.createElement("div");
      folders.id = "desktop-folder-list";
      folders.hidden = true;

      const files = document.createElement("div");
      files.id = "desktop-file-list";

      panel.append(title, select, folder, register, status, powerWrap, banner, folders, files);
      document.querySelector(".topbar").after(panel);
    }

    const hasHost = !!nativeApi();
    document.getElementById("desktop-select-files").hidden = !value.file_dialog || !hasHost;
    document.getElementById("desktop-select-folder").hidden = !value.folder_dialog || !hasHost;
    document.getElementById("desktop-register-open-with").hidden = !value.open_with_registration || !hasHost;
    const pwr = document.getElementById("desktop-power-controls");
    if (pwr) pwr.hidden = !(value.sleep_prevention || value.post_queue_actions) || !hasHost;

    document.querySelectorAll(".browser-add-root").forEach((btn) => {
      btn.hidden = !(value.folder_dialog && hasHost);
    });

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
      if (typeof api.list_folders === "function") {
        const fRes = await api.list_folders();
        if (fRes?.folders) renderFolders(fRes.folders);
      }
      if (typeof api.get_power_settings === "function") {
        applyPowerState(await api.get_power_settings());
      }
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
  window.addEventListener("studio:desktop-power", (event) => applyPowerState(event.detail));
  window.addEventListener("studio:media-roots-changed", (event) => {
    if (Array.isArray(event.detail?.folders)) renderFolders(event.detail.folders);
  });
  window.addEventListener("pywebviewready", attachNative);
  document.addEventListener("DOMContentLoaded", () => {
    if (nativeApi()) attachNative();
    else poll();
  });
  window.StudioDesktop = {
    receive,
    capabilities: () => capabilities,
    canSelectFolder: () => Boolean(capabilities?.folder_dialog && nativeApi()?.select_folder),
    selectFolder: selectFolderAction,
  };
})();
