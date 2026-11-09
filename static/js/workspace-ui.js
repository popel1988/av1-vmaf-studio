/* Progressive workspace UI. Business state and original controls belong to app/editor.js. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const t = (text) => window.I18N ? window.I18N.t(text) : text;
  const prefix = "video-studio.workspace.";
  const load = (key, fallback) => {
    try { const value = JSON.parse(localStorage.getItem(prefix + key)); return value == null ? fallback : value; }
    catch (_) { return fallback; }
  };
  const save = (key, value) => {
    try { localStorage.setItem(prefix + key, JSON.stringify(value)); } catch (_) { /* Storage is optional. */ }
  };
  const emit = (name, detail) => document.dispatchEvent(new CustomEvent("workspace:" + name, { detail }));
  const paths = {
    play: "m9 5 11 7-11 7Z", pause: "M8 5v14M16 5v14",
    stop: "M6 6h12v12H6Z", previous: "M5 5v14m14-14L8 12l11 7Z",
    next: "M19 5v14M5 5l11 7-11 7Z", left: "m15 5-7 7 7 7",
    right: "m9 5 7 7-7 7", up: "m5 14 7-7 7 7M12 7v14",
    down: "m5 10 7 7 7-7M12 3v14",
    film: "M3 4h18v16H3ZM7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4",
    chart: "M4 3v17h17M8 16v-5m5 5V6m5 10V9",
    audio: "m4 9 4 0 5-5v16l-5-5H4ZM17 8q5 4 0 8",
    mute: "m4 9 4 0 5-5v16l-5-5H4ZM17 9l5 6m0-6-5 6",
    cut: "M9 8 20 20M9 16 20 4M7 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm0 10a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
    search: "M16 16l5 5M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Z",
    folder: "M3 6h7l2 2h9v12H3Z", save: "M4 3h13l4 4v14H3V3ZM7 3v6h10V3M7 21v-8h10v8",
    settings: "M4 7h16M4 17h16M8 4v6m8 4v6",
    help: "M9 8a3 3 0 1 1 5 2c-2 1-2 2-2 3M12 17h.01M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z",
    star: "m12 3 3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1Z",
    spark: "m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3Z",
    list: "M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1",
    trash: "M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7",
    copy: "M8 8h13v13H8ZM16 8V3H3v13h5",
    undo: "m8 3-5 5 5 5M3 8h11a7 7 0 0 1 0 14",
    redo: "m16 3 5 5-5 5M21 8H10a7 7 0 0 0 0 14",
    loop: "m17 2 4 4-4 4M21 6H7a4 4 0 0 0-4 4m4 12-4-4 4-4M3 18h14a4 4 0 0 0 4-4",
    camera: "M3 7h5l2-3h4l2 3h5v14H3ZM12 10a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z",
    monitor: "M3 3h18v14H3ZM12 17v4M7 21h10",
    panel: "M3 3h18v18H3ZM9 3v18M5 7h2m-2 4h2",
    check: "m4 12 5 5L20 6", close: "m5 5 14 14M19 5 5 19",
    warning: "m12 3 10 18H2ZM12 9v5m0 3h.01",
    key: "M14 5a5 5 0 1 0 0 10 5 5 0 0 0 0-10ZM10 13l-8 8m3-3 3 3",
    link: "m9 15 6-6M8 17l-2 2a4 4 0 0 1-5-5l5-5a4 4 0 0 1 6 0m4-2 2-2a4 4 0 0 1 5 5l-5 5a4 4 0 0 1-6 0",
    file: "M5 2h9l5 5v15H5ZM14 2v6h5M8 13h8m-8 4h8",
    eject: "m4 15 8-11 8 11ZM4 20h16",
    clock: "M12 7v5l4 2M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z"
  };
  const glyphs = {
    "▶": "play", "►": "play", "◀": "left", "⏸": "pause", "⏹": "stop", "■": "stop",
    "⏮": "previous", "⏭": "next", "⏪": "previous", "⏩": "next", "‹": "left", "›": "right",
    "🎬": "film", "🎞": "film", "📺": "monitor", "🖼": "monitor", "🔊": "audio", "🎧": "audio",
    "🎙": "audio", "🔇": "mute", "✂": "cut", "★": "star", "✨": "spark", "🪄": "spark",
    "⚡": "spark", "🔍": "search", "🔎": "search", "📂": "folder", "📁": "folder", "💾": "save",
    "⚙": "settings", "🩺": "help", "📊": "chart", "≡": "list", "🗑": "trash", "⧉": "copy",
    "↩": "undo", "↪": "redo", "↺": "undo", "🔁": "loop", "⇄": "loop", "📸": "camera",
    "⬆": "up", "⬇": "down", "📥": "down", "📤": "up", "📋": "copy", "📄": "file",
    "🔑": "key", "🔗": "link", "✅": "check", "✓": "check", "❌": "close", "⚠": "warning",
    "⏏": "eject", "⬛": "stop", "💨": "spark", "🌊": "loop", "⌁": "loop", "💬": "list"
  };
  const glyphPattern = new RegExp("(" + Object.keys(glyphs).sort((a, b) => b.length - a.length)
    .map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\uFE0F?", "gu");
  const controls = "button, summary, label.btn, .card-idx, .ed-monitor-head > strong";
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "workspace-icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("data-no-i18n", "");
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", paths[name] || paths.help);
    svg.append(path);
    return svg;
  }
  function renderControl(control) {
    // Never replace controls or their descendants carrying application event handlers.
    const walker = document.createTreeWalker(control, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return node.parentElement.closest("svg, .workspace-control-text, [data-no-i18n], input, textarea, select")
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      }
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const raw = node.nodeValue;
      glyphPattern.lastIndex = 0;
      if (!glyphPattern.test(raw)) continue;
      glyphPattern.lastIndex = 0;
      const fragment = document.createDocumentFragment();
      let at = 0;
      for (const match of raw.matchAll(glyphPattern)) {
        if (match.index > at) fragment.append(document.createTextNode(raw.slice(at, match.index)));
        fragment.append(icon(glyphs[match[1]]));
        at = match.index + match[0].length;
      }
      fragment.append(document.createTextNode(raw.slice(at)));
      // Stable text wrapper allows i18n to translate labels independently of icons.
      Array.from(fragment.childNodes).forEach((child) => {
        if (child.nodeType !== Node.TEXT_NODE || !child.textContent.trim()) return;
        const text = document.createElement("span");
        text.className = "workspace-control-text";
        child.replaceWith(text);
        text.append(child);
      });
      if (!control.textContent.replace(glyphPattern, "").trim() && !control.getAttribute("aria-label")) {
        control.setAttribute("aria-label", control.title || t("Aktion"));
      }
      node.replaceWith(fragment);
    }
  }
  function renderIcons(root) {
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (root.matches(controls)) renderControl(root);
    root.querySelectorAll(controls).forEach(renderControl);
    const explicit = root.matches("[data-icon]") ? [root] : Array.from(root.querySelectorAll("[data-icon]"));
    explicit.forEach((el) => {
      if (!el.querySelector("svg")) el.append(icon(el.dataset.icon));
    });
  }
  function button(text, onClick) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "btn btn-ghost btn-sm";
    el.textContent = t(text);
    el.addEventListener("click", onClick);
    return el;
  }
  function disclosure(text, className) {
    const el = document.createElement("details");
    el.className = className;
    const summary = document.createElement("summary");
    summary.textContent = t(text);
    el.append(summary);
    return el;
  }
  function initNavigation() {
    const nav = $("nav");
    const groups = Array.from(nav.querySelectorAll("details[data-nav-group]"));
    groups.forEach((group) => {
      group.open = load("nav." + group.dataset.navGroup, group.open);
      group.addEventListener("toggle", () => {
        if (!document.body.classList.contains("workspace-sidebar-collapsed"))
          save("nav." + group.dataset.navGroup, group.open);
      });
    });
    let lastPage;
    function syncPage() {
      const active = nav.querySelector("[data-nav].active");
      if (!active || active.dataset.nav === lastPage) return;
      lastPage = active.dataset.nav;
      nav.querySelectorAll("[data-nav]").forEach((item) => {
        if (item === active) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
      });
      for (let parent = active.parentElement; parent && parent !== nav; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
      emit("pagechange", { page: lastPage });
    }
    const observer = new MutationObserver(syncPage);
    observer.observe(nav, { subtree: true, attributes: true, attributeFilter: ["class"] });
    syncPage();
    nav.querySelectorAll(".nav-item").forEach((item) => {
      item.title = item.textContent.trim();
      // Wrap only bare text; existing spans and the original button stay intact.
      Array.from(item.childNodes).filter((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim()).forEach((node) => {
        const label = document.createElement("span");
        label.className = "workspace-nav-label";
        node.replaceWith(label);
        label.append(node);
      });
    });
    const toggle = $("workspace-sidebar-toggle");
    function collapse(value) {
      const collapsed = Boolean(value);
      if (collapsed) groups.forEach((g) => { g.dataset.beforeCollapse = String(g.open); g.open = true; });
      else groups.forEach((g) => { if (g.dataset.beforeCollapse) g.open = g.dataset.beforeCollapse === "true"; });
      document.body.classList.toggle("workspace-sidebar-collapsed", collapsed);
      toggle.setAttribute("aria-expanded", String(!collapsed));
      toggle.title = t(collapsed ? "Navigation ausklappen" : "Navigation einklappen");
      toggle.setAttribute("aria-label", toggle.title);
      const text = toggle.querySelector(".workspace-sidebar-label");
      if (text) text.textContent = toggle.title;
      save("sidebarCollapsed", collapsed);
      emit("layoutchange", { sidebarCollapsed: collapsed });
    }
    toggle.addEventListener("click", () => collapse(!document.body.classList.contains("workspace-sidebar-collapsed")));
    collapse(load("sidebarCollapsed", false));
  }
  function initHardware() {
    const metrics = document.querySelector(".metrics");
    if (!metrics) return;
    const details = disclosure("Hardware-Details", "workspace-hardware");
    const summary = document.createElement("div");
    summary.className = "workspace-hardware-summary";
    summary.setAttribute("aria-label", t("Hardware-Auslastung"));
    const values = ["cpu-pct", "ram-pct"].map((id) => {
      const item = document.createElement("span");
      const label = document.createElement("span");
      label.textContent = id === "cpu-pct" ? "CPU " : "RAM ";
      const value = document.createElement("strong");
      item.append(label, value);
      summary.append(item);
      return { source: $(id), value };
    });
    const gpuValue = document.createElement("span");
    summary.append(gpuValue);
    function sync() {
      values.forEach(({ source, value }) => { if (source && value.textContent !== source.textContent) value.textContent = source.textContent; });
      const gpu = metrics.querySelector("#gpu-container .ring-pct");
      const text = gpu ? "GPU " + gpu.textContent : "";
      if (gpuValue.textContent !== text) gpuValue.textContent = text;
    }
    const children = Array.from(metrics.children);
    metrics.append(summary, details);
    children.forEach((child) => details.append(child));
    details.open = load("hardwareExpanded", false);
    details.addEventListener("toggle", () => {
      save("hardwareExpanded", details.open);
      // Charts may have been initialized while their container was collapsed.
      if (details.open) window.dispatchEvent(new Event("resize"));
    });
    new MutationObserver(sync).observe(details, { childList: true, subtree: true, characterData: true });
    sync();
  }
  function initEncoding() {
    const card = document.querySelector('.settings-card[data-page="encode"]');
    if (!card) return;
    const body = card.querySelector(".card-body");
    const bar = document.createElement("div");
    bar.className = "workspace-mode-bar";
    const label = document.createElement("strong");
    label.textContent = t("Bedienmodus");
    const simple = button("Einfach", () => setMode("simple"));
    const expert = button("Experte", () => setMode("expert"));
    simple.id = "workspace-encode-simple";
    expert.id = "workspace-encode-expert";
    const hint = document.createElement("p");
    hint.className = "hint workspace-mode-hint";
    hint.textContent = t("Alle Expertenwerte bleiben aktiv und werden unverändert übernommen.");
    const advancedIds = [
      "opt-denoise", "opt-deband", "opt-deinterlace", "opt-sharpen", "opt-grain",
      "opt-aq-strength", "opt-b-frames", "opt-nvenc-tune", "opt-keyint", "opt-film-grain",
      "opt-svt-look", "opt-name-pattern", "opt-max-bitrate", "opt-size-target", "opt-max-output-mb"
    ];
    advancedIds.forEach((id) => { const field = $(id)?.closest(".field"); if (field) field.classList.add("workspace-expert-only"); });
    // The options section contains dependent child panels; hide the section without touching values.
    const options = $("opt-two-pass")?.closest(".opt-section");
    if (options) options.classList.add("workspace-expert-only");
    let mode = load("encodingMode", "simple") === "expert" ? "expert" : "simple";
    function setMode(value) {
      mode = value;
      card.classList.toggle("workspace-simple", mode === "simple");
      simple.setAttribute("aria-pressed", String(mode === "simple"));
      expert.setAttribute("aria-pressed", String(mode === "expert"));
      hint.hidden = mode !== "simple";
      save("encodingMode", mode);
      emit("encodingmodechange", { mode });
    }
    bar.append(label, simple, expert);
    body.prepend(bar, hint);
    setMode(mode);
    const row = card.querySelector(".profile-row");
    if (row) {
      const more = disclosure("Profil-Aktionen", "workspace-overflow");
      const panel = document.createElement("div");
      panel.className = "workspace-overflow-panel";
      ["btn-profile-save", "btn-profile-delete"].forEach((id) => { if ($(id)) panel.append($(id)); });
      more.append(panel);
      row.append(more);
    }
  }
  function initEditor() {
    const layout = $("ed-layout");
    if (!layout) return;
    const card = layout.closest(".card");
    card.classList.add("workspace-editor");
    const tools = document.createElement("div");
    tools.className = "workspace-editor-tools";
    const programOnly = button("Nur Programm-Monitor", () => setProgramOnly(!layout.classList.contains("workspace-program-only")));
    programOnly.id = "workspace-program-only";
    programOnly.setAttribute("aria-controls", "ed-layout");
    const reset = button("Layout zurücksetzen", () => {
      applySplit(50); applyHeight(200); setProgramOnly(false);
    });
    tools.append(programOnly, reset);
    layout.before(tools);
    function setProgramOnly(value) {
      layout.classList.toggle("workspace-program-only", value);
      const source = layout.querySelector(".ed-preview");
      if (source) source.inert = value;
      programOnly.setAttribute("aria-pressed", String(value));
      save("editorProgramOnly", value);
      emit("editorlayoutchange", { programOnly: value });
      window.dispatchEvent(new Event("resize"));
    }
    const divider = document.createElement("div");
    divider.className = "workspace-monitor-divider";
    divider.tabIndex = 0;
    divider.setAttribute("role", "separator");
    divider.setAttribute("aria-orientation", "vertical");
    divider.setAttribute("aria-label", t("Monitoraufteilung"));
    divider.setAttribute("aria-valuemin", "25");
    divider.setAttribute("aria-valuemax", "75");
    divider.setAttribute("aria-controls", "ed-layout");
    layout.querySelector(".ed-preview").after(divider);
    let split = 50, height = 200;
    const clamp = (value, min, max, fallback) => Number.isFinite(Number(value)) ? Math.max(min, Math.min(max, Number(value))) : fallback;
    function applySplit(value) {
      split = clamp(value, 25, 75, 50);
      layout.style.setProperty("--workspace-source-share", split + "fr");
      layout.style.setProperty("--workspace-program-share", (100 - split) + "fr");
      divider.setAttribute("aria-valuenow", String(Math.round(split)));
      save("editorSplit", split);
    }
    const timeline = card.querySelector(".ed-tl-scroll");
    const heightHandle = document.createElement("div");
    heightHandle.className = "workspace-timeline-divider";
    heightHandle.tabIndex = 0;
    heightHandle.setAttribute("role", "separator");
    heightHandle.setAttribute("aria-orientation", "horizontal");
    heightHandle.setAttribute("aria-label", t("Timeline-Höhe"));
    heightHandle.setAttribute("aria-valuemin", "120");
    heightHandle.setAttribute("aria-valuemax", "600");
    heightHandle.setAttribute("aria-controls", "ed-tl-scroll");
    timeline.before(heightHandle);
    function applyHeight(value) {
      height = clamp(value, 120, 600, 200);
      timeline.style.height = height + "px";
      heightHandle.setAttribute("aria-valuenow", String(Math.round(height)));
      save("editorTimelineHeight", height);
    }
    function drag(handle, move) {
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        handle.setPointerCapture(event.pointerId);
        handle.classList.add("workspace-dragging");
        const start = { x: event.clientX, y: event.clientY, split, height };
        const onMove = (e) => move(e, start);
        const stop = () => {
          handle.removeEventListener("pointermove", onMove);
          handle.removeEventListener("pointerup", stop);
          handle.removeEventListener("pointercancel", stop);
          handle.removeEventListener("lostpointercapture", stop);
          handle.classList.remove("workspace-dragging");
          emit("editorlayoutchange", { split, timelineHeight: height });
          window.dispatchEvent(new Event("resize"));
        };
        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", stop);
        handle.addEventListener("pointercancel", stop);
        handle.addEventListener("lostpointercapture", stop);
      });
    }
    drag(divider, (e, start) => applySplit(start.split + (e.clientX - start.x) / layout.getBoundingClientRect().width * 100));
    drag(heightHandle, (e, start) => applyHeight(start.height + e.clientY - start.y));
    divider.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault(); e.stopPropagation();
      applySplit(e.key === "Home" ? 25 : e.key === "End" ? 75 : split + (e.key === "ArrowLeft" ? -2 : 2));
    });
    heightHandle.addEventListener("keydown", (e) => {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
      e.preventDefault(); e.stopPropagation();
      applyHeight(e.key === "Home" ? 120 : e.key === "End" ? 600 : height + (e.key === "ArrowUp" ? -20 : 20));
    });
    applySplit(load("editorSplit", 50));
    applyHeight(load("editorTimelineHeight", 200));
    setProgramOnly(Boolean(load("editorProgramOnly", false)));
    // Disclosure containers keep original button nodes, listeners and disabled states.
    card.querySelectorAll(".ed-oneclick-bar").forEach((bar, index) => {
      const more = disclosure(index ? "Aktionen für alle Clips" : "Schnittaktionen", "workspace-editor-actions");
      bar.before(more);
      more.append(bar);
    });
    const intro = card.querySelector(".card-body > p.hint");
    const help = disclosure("Kontexthilfe", "workspace-help");
    const context = document.createElement("p");
    context.className = "hint";
    context.id = "workspace-editor-context";
    context.textContent = t("Steuerelement fokussieren, um seine Funktion zu erklären.");
    if (intro) help.append(intro);
    help.append(context);
    tools.after(help);
    card.addEventListener("focusin", (event) => {
      const el = event.target.closest("button, input, select, [data-tip], [role=separator]");
      if (!el || help.contains(el)) return;
      const field = el.closest(".field");
      const label = field?.querySelector("label");
      const text = el.getAttribute("data-tip") || el.title || label?.getAttribute("data-tip") || label?.textContent || el.getAttribute("aria-label");
      if (text) context.textContent = t(text);
    });
    help.open = load("editorHelpExpanded", false);
    help.addEventListener("toggle", () => save("editorHelpExpanded", help.open));
  }
  function initDisclosures() {
    // A native disclosure is keyboard accessible. Escape and outside clicks dismiss overflow panels.
    document.addEventListener("click", (event) => {
      document.querySelectorAll(".workspace-overflow[open], .ed-tl-more[open]").forEach((el) => {
        if (!el.contains(event.target)) el.open = false;
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      document.querySelectorAll(".workspace-overflow[open], .ed-tl-more[open]").forEach((el) => {
        const focused = el.contains(document.activeElement);
        el.open = false;
        if (focused) el.querySelector("summary").focus();
      });
    });
  }
  function init() {
    initNavigation();
    initHardware();
    initEncoding();
    initEditor();
    initDisclosures();
    renderIcons(document.body);
    // Observe only changed controls/added subtrees, never rescan the document on telemetry updates.
    const pending = new Set();
    let scheduled = false;
    const observer = new MutationObserver((mutations) => {
      mutations.forEach((mu) => {
        const el = mu.target.nodeType === Node.ELEMENT_NODE ? mu.target : mu.target.parentElement;
        const control = el?.closest(controls);
        if (control && !el.closest("svg, .workspace-control-text")) pending.add(control);
        mu.addedNodes.forEach((node) => {
          if (node.nodeType === Node.ELEMENT_NODE && !node.matches("svg, .workspace-control-text") && !node.closest("svg"))
            pending.add(node);
        });
      });
      if (scheduled || !pending.size) return;
      scheduled = true;
      requestAnimationFrame(() => {
        const roots = Array.from(pending);
        pending.clear(); scheduled = false;
        roots.forEach((root) => { if (root.isConnected) renderIcons(root); });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    window.WorkspaceUI = Object.freeze({
      refresh: (root) => renderIcons(root || document.body),
      icon,
      emit,
      setEditorHelp: (text) => { if ($("workspace-editor-context")) $("workspace-editor-context").textContent = t(text); }
    });
    emit("ready", { version: 1 });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
