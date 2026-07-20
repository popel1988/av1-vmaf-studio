/* AV1 / VMAF Compression Studio – Frontend-Logik */
(() => {
  "use strict";

  const RING_CIRC = 2 * Math.PI * 52; // ~327
  const $ = (id) => document.getElementById(id);

  const state = {
    currentPath: "",
    selected: null,
    vmafChart: null,
    lastVmafKey: null,
    awaitingItemId: null,
    audioTracks: [],   // ausgewählte Audio-Indizes der aktuellen Datei
    viewSession: null, // aktiver Archiv-Vergleich (null = Live-Ansicht)
    lastItems: [],     // letzter Queue-Stand (für Rückkehr aus Archiv-Ansicht)
    lastActiveId: null,
    currentPage: "encode",
    hasArchive: false, // es existieren archivierte VMAF-Vergleiche
    shotScene: null,   // aktuell gewählte Szene in der Screenshot-Galerie
    superBatch: null,  // aktive Super-Tool-Stapelkennung
    vmafSource: null,  // Quelle des aktuell gezeigten VMAF-Vergleichs (für „→ Encoding")
    browseData: null,  // zuletzt geladener Ordnerinhalt (für Live-Filter)
    libRows: [],       // Bibliotheks-Treffer (für Sortierung/Filter/Gruppierung)
    libStats: null,    // Dashboard-Statistik des letzten Scans
    libSort: { key: "est_saved_bytes", dir: "desc" },
    remuxLoaded: false, // Remux-Seite initialisiert
    remuxSel: null,     // { path, name } der Remux-Quelle
    remuxInfo: null,    // ffprobe-Info der Remux-Quelle
    remuxExt: [],       // hinzugefügte externe Spuren
    remuxExtPath: "",   // aktueller Ordner im externen Datei-Picker
    remuxAtt: [],       // hinzugefügte Attachments (Fonts/Cover)
    remuxChapters: null, // geladene/bearbeitete Kapitel (null = unverändert)
    remuxMerge: [],     // Dateien für "Zusammenführen"
    splitRanges: [],    // Bereiche für Ausschnitt-Export
    remuxPick: null,    // aktueller Modus des Datei-Pickers (ext|att|merge|chapters)
    outPick: null,      // Zielordner-Picker (prefix/root/path)
  };

  // Absoluten Pfad in einen relativen Medienpfad umwandeln.
  function inputRelPath(abs) {
    if (!abs) return "";
    const p = String(abs).replace(/\\/g, "/");
    const roots = (window.APP_CONFIG && window.APP_CONFIG.mediaRoots) || [];
    const multi = !!(window.APP_CONFIG && window.APP_CONFIG.multiMedia);
    for (const r of roots) {
      const base = String(r.path || "").replace(/\\/g, "/").replace(/\/+$/, "");
      if (base && (p === base || p.startsWith(base + "/"))) {
        const sub = p.slice(base.length).replace(/^\/+/, "");
        return multi ? (sub ? `${r.name}/${sub}` : r.name) : sub;
      }
    }
    return p.replace(/^\/+/, "");
  }

  /* --------------------------------------------------------- NAVIGATION */
  // data-page kann mehrere (leerzeichengetrennte) Seiten listen (z. B.
  // "encode vmaf" für die geteilte Quellenauswahl).
  function pagesOf(el) {
    return (el.dataset.page || "").split(/\s+/).filter(Boolean);
  }

  function showCard(el, hasContent) {
    if (!el) return;
    el.dataset.hasContent = hasContent ? "1" : "";
    el.style.display = (hasContent && pagesOf(el).includes(state.currentPage)) ? "" : "none";
  }

  function applyPageVisibility() {
    document.querySelectorAll("[data-page]").forEach((el) => {
      const onPage = pagesOf(el).includes(state.currentPage);
      if (el.id === "vmaf-card" || el.id === "progress-card") {
        el.style.display = (onPage && el.dataset.hasContent === "1") ? "" : "none";
      } else {
        el.style.display = onPage ? "" : "none";
      }
    });
  }

  function navTo(page) {
    // Beim Verlassen der A/B-Seite die Wiedergabe stoppen, damit im Hintergrund
    // kein Ton/Video weiterläuft.
    if (state.currentPage === "abcompare" && page !== "abcompare") pauseAbVideos();
    state.currentPage = page;
    localStorage.setItem("page", page);
    const nav = $("nav");
    if (nav) nav.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.nav === page));
    applyPageVisibility();
    if (page === "stats") loadStats();
    if (page === "supertool") pollSuperStatus();
    if (page === "audio" && !state.audioLoaded) { state.audioLoaded = true; auLoadDir(""); }
    if (page === "remux" && !state.remuxLoaded) { state.remuxLoaded = true; remuxInit(); }
    if (page === "diag" && !state.diagLoaded) loadDiagnostics();
  }

  function initNav() {
    const nav = $("nav");
    if (!nav) return;
    nav.querySelectorAll(".nav-item").forEach((b) =>
      b.addEventListener("click", () => navTo(b.dataset.nav)));
    navTo(localStorage.getItem("page") || "encode");
  }

  /* --------------------------------------------------------------- THEME */
  function initTheme() {
    const saved = localStorage.getItem("theme") || "anthracite";
    document.documentElement.setAttribute("data-theme", saved);
    $("theme-select").value = saved;
    $("theme-select").addEventListener("change", (e) => {
      const t = e.target.value;
      document.documentElement.setAttribute("data-theme", t);
      localStorage.setItem("theme", t);
      if (state.vmafChart) restyleChart();
    });
    initDensity();
  }

  function initDensity() {
    const compact = localStorage.getItem("density") === "compact";
    document.documentElement.setAttribute("data-density", compact ? "compact" : "comfortable");
    const cb = $("density-compact");
    if (cb) {
      cb.checked = compact;
      cb.addEventListener("change", () => {
        const on = cb.checked;
        document.documentElement.setAttribute("data-density", on ? "compact" : "comfortable");
        localStorage.setItem("density", on ? "compact" : "comfortable");
      });
    }
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* ------------------------------------------------------------- BROWSER */
  // Wiederverwendbarer Ordner-Browser: Breadcrumb, rekursive Suche und
  // Zurück/Vor-Navigation mit gemerkter Scroll-Position. Wird von Encoding,
  // Super-Tool, Audio-Optimierung, Remux und den Datei-Pickern genutzt.
  //
  // opts: { listId, crumbId, kind, showFiles, recursive, playFile,
  //         pickFile(f), onNavigate(data, path), rootLabel, searchPlaceholder,
  //         browseUrl(path), searchUrl(path, q), playRoot }
  function makeFolderBrowser(opts) {
    const listEl = $(opts.listId);
    if (!listEl) return null;
    const crumbEl = opts.crumbId ? $(opts.crumbId) : null;
    const kind = opts.kind || "video";
    const showFiles = opts.showFiles !== false;
    const browseUrl = opts.browseUrl ||
      ((p) => `/api/browse?path=${encodeURIComponent(p)}&kind=${kind}`);
    const searchUrl = opts.searchUrl ||
      ((p, q) => `/api/search?path=${encodeURIComponent(p)}&q=${encodeURIComponent(q)}&kind=${kind}`);
    // Rekursive Suche nur, wenn eine Such-URL existiert (Standard: /api/search).
    const allowRecursive = showFiles && opts.recursive !== false && opts.searchUrl !== null;
    const S = { path: "", data: null, hist: [], hidx: -1, scroll: {}, timer: null };

    // --- Toolbar (Zurück/Vor · Suche · Unterordner · Zähler) ---
    const bar = document.createElement("div");
    bar.className = "browser-search-row browser-nav";
    const back = navBtn("◀", "Zurück");
    const fwd = navBtn("▶", "Vor");
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = opts.searchPlaceholder || "Im Ordner suchen … (Name)";
    search.autocomplete = "off";
    const recWrap = document.createElement("label");
    recWrap.className = "check browser-recursive";
    recWrap.title = "Auch alle Unterordner durchsuchen";
    const rec = document.createElement("input");
    rec.type = "checkbox";
    const recTxt = document.createElement("span");
    recTxt.textContent = "Unterordner";
    recWrap.append(rec, recTxt);
    const count = document.createElement("span");
    count.className = "browser-count muted";
    bar.append(back, fwd, search);
    if (allowRecursive) bar.append(recWrap);
    bar.append(count);
    const anchor = crumbEl || listEl;
    anchor.parentNode.insertBefore(bar, anchor);

    function navBtn(txt, title) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-ghost btn-sm browser-navbtn";
      b.textContent = txt;
      b.title = title;
      return b;
    }
    function syncNav() {
      back.disabled = S.hidx <= 0;
      fwd.disabled = S.hidx >= S.hist.length - 1;
    }

    async function navigate(path, o) {
      o = o || {};
      if (S.data) S.scroll[S.path] = listEl.scrollTop;  // Position merken
      search.value = "";
      rec.checked = false;
      listEl.innerHTML = '<div class="browser-loading">Lade Verzeichnis …</div>';
      let data;
      try {
        data = await (await fetch(browseUrl(path))).json();
      } catch (e) {
        listEl.innerHTML = `<div class="browser-loading">Fehler: ${escapeHtml(String(e))}</div>`;
        return;
      }
      if (data.error) { listEl.innerHTML = `<div class="browser-loading">${escapeHtml(data.error)}</div>`; return; }
      S.path = (data.path != null && data.path !== "") ? data.path : "";
      S.data = data;
      if (o.push !== false) {
        S.hist = S.hist.slice(0, S.hidx + 1);
        if (S.hist[S.hidx] !== S.path) { S.hist.push(S.path); S.hidx = S.hist.length - 1; }
      }
      syncNav();
      renderCrumb(data);
      renderList();
      listEl.scrollTop = o.restore ? (S.scroll[S.path] || 0) : 0;
      if (opts.onNavigate) opts.onNavigate(data, S.path);
    }

    function go(path) { return navigate(path, { push: true }); }
    back.addEventListener("click", () => {
      if (S.hidx > 0) { S.hidx--; navigate(S.hist[S.hidx], { push: false, restore: true }); }
    });
    fwd.addEventListener("click", () => {
      if (S.hidx < S.hist.length - 1) { S.hidx++; navigate(S.hist[S.hidx], { push: false, restore: true }); }
    });

    function renderCrumb(data) {
      if (!crumbEl) return;
      crumbEl.innerHTML = "";
      const root = document.createElement("a");
      root.textContent = opts.rootLabel ||
        ((window.APP_CONFIG && APP_CONFIG.multiMedia) ? "Medien" : "/media");
      root.onclick = () => go("");
      crumbEl.appendChild(root);
      if (data.path) {
        let acc = "";
        data.path.split("/").forEach((p) => {
          acc = acc ? `${acc}/${p}` : p;
          const sep = document.createElement("span"); sep.textContent = " / "; crumbEl.appendChild(sep);
          const a = document.createElement("a"); a.textContent = p;
          const t = acc; a.onclick = () => go(t); crumbEl.appendChild(a);
        });
      }
    }

    function renderList() {
      const data = S.data;
      if (!data) return;
      listEl.innerHTML = "";
      const q = search.value.trim().toLowerCase();
      const match = (n) => !q || n.toLowerCase().includes(q);
      const dirs = (data.dirs || []).filter((d) => match(d.name));
      const files = showFiles ? (data.files || []).filter((f) => match(f.name)) : [];
      if (!data.roots && !data.is_root && !q) {
        listEl.appendChild(makeRow("dir", "..", "", () => go(data.parent || ""), null));
      }
      dirs.forEach((d) => listEl.appendChild(makeRow("dir", d.name, "", () => go(d.rel), null)));
      files.forEach((f) => listEl.appendChild(makeRow(
        "file", f.name, f.size_human, null,
        opts.pickFile ? () => opts.pickFile(f) : null,
        opts.playFile ? f.rel : null, opts.playRoot || "media")));
      if (!dirs.length && !files.length) {
        listEl.innerHTML = q
          ? '<div class="browser-loading">Keine Treffer in diesem Ordner.</div>'
          : ((!showFiles && (data.files || []).length)
              ? `<div class="browser-loading">${(data.files || []).length} Datei(en) hier · keine Unterordner</div>`
              : '<div class="browser-loading">Leerer Ordner.</div>');
      }
      setCount(files.length, dirs.length, q, data);
    }

    function setCount(files, dirs, q, data) {
      const parts = [];
      parts.push(q ? `${dirs}/${(data.dirs || []).length} Ordner` : `${dirs} Ordner`);
      if (showFiles) parts.push(q ? `${files}/${(data.files || []).length} Dateien` : `${files} Dateien`);
      count.textContent = parts.join(" · ");
    }

    async function runRecursive(q) {
      listEl.innerHTML = '<div class="browser-loading">Suche in Unterordnern …</div>';
      try {
        const data = await (await fetch(searchUrl(S.path, q))).json();
        if (data.error) { listEl.innerHTML = `<div class="browser-loading">${escapeHtml(data.error)}</div>`; return; }
        listEl.innerHTML = "";
        (data.files || []).forEach((f) => {
          const label = f.folder ? `${f.name}  ·  ${f.folder}/` : f.name;
          listEl.appendChild(makeRow("file", label, f.size_human, null,
            opts.pickFile ? () => opts.pickFile(f) : null,
            opts.playFile ? f.rel : null, opts.playRoot || "media"));
        });
        if (!(data.files || []).length) listEl.innerHTML = '<div class="browser-loading">Keine Treffer.</div>';
        count.textContent = `${(data.files || []).length} Treffer${data.truncated ? " (begrenzt)" : ""}`;
      } catch (e) {
        listEl.innerHTML = `<div class="browser-loading">Fehler: ${escapeHtml(String(e))}</div>`;
      }
    }

    function onSearch() {
      const q = search.value.trim();
      if (allowRecursive && rec.checked && q) {
        clearTimeout(S.timer);
        S.timer = setTimeout(() => runRecursive(q), 250);
      } else {
        renderList();
      }
    }
    search.addEventListener("input", onSearch);
    rec.addEventListener("change", onSearch);

    syncNav();
    return { go, current: () => S.path, refresh: () => navigate(S.path, { push: false, restore: true }) };
  }

  // Instanzen der wiederverwendbaren Browser (einmalig erzeugt).
  let mainBrowser = null;
  function loadDir(path) { return mainBrowser ? mainBrowser.go(path) : undefined; }

  function makeRow(type, name, size, onOpen, onPick, playRel, playRoot) {
    const row = document.createElement("div");
    row.className = "row-item";
    const icon = type === "dir" ? "📁" : "🎬";
    row.innerHTML = `
      <span class="row-icon ${type}">${icon}</span>
      <span class="row-name">${escapeHtml(name)}</span>
      <span class="row-size">${size}</span>`;
    if (onOpen) row.addEventListener("click", onOpen);
    if (playRel) {
      const play = document.createElement("button");
      play.className = "row-play";
      play.title = "Im Browser abspielen";
      play.textContent = "▶";
      play.addEventListener("click", (e) => {
        e.stopPropagation();
        openPlayer(playRoot || "media", playRel, name);
      });
      row.appendChild(play);
    }
    if (onPick) {
      const btn = document.createElement("button");
      btn.className = "row-pick";
      btn.textContent = "Auswählen";
      btn.addEventListener("click", (e) => { e.stopPropagation(); onPick(); });
      row.appendChild(btn);
      row.addEventListener("click", onPick);
    }
    return row;
  }

  // Modal-Ordnerauswahl (nur Ordner). onPick(relPath) erhält den Zielordner.
  function openFolderPickerModal(opts) {
    opts = opts || {};
    openModal(opts.title || "Ordner wählen",
      '<div class="breadcrumb" id="fp-crumb"></div>' +
      '<div class="browser browser-sm" id="fp-browser"><div class="browser-loading">Lade …</div></div>' +
      '<div class="lib-actions" style="margin-top:10px">' +
      '<button class="btn btn-primary btn-sm" id="fp-choose">Diesen Ordner wählen</button>' +
      '<span id="fp-sel" class="muted"></span></div>');
    let current = "";
    const picker = makeFolderBrowser({
      listId: "fp-browser", crumbId: "fp-crumb",
      kind: opts.kind || "video", showFiles: false,
      searchPlaceholder: "Unterordner filtern …",
      onNavigate: (data, p) => {
        current = p;
        const sel = $("fp-sel");
        if (sel) sel.textContent = p ? `Auswahl: /${p}` : "Auswahl: (Wurzel/gesamt)";
      },
    });
    const choose = $("fp-choose");
    if (choose) choose.addEventListener("click", () => {
      closeModal();
      if (opts.onPick) opts.onPick(current);
    });
    if (picker) picker.go(opts.start || "");
  }

  // Modal-Dateiauswahl (Video). onPick({rel,name}).
  function openFilePickerModal(opts) {
    opts = opts || {};
    openModal(opts.title || "Datei wählen",
      '<div class="breadcrumb" id="fp-crumb"></div>' +
      '<div class="browser browser-sm" id="fp-browser"><div class="browser-loading">Lade …</div></div>');
    const picker = makeFolderBrowser({
      listId: "fp-browser", crumbId: "fp-crumb",
      kind: "video", showFiles: true,
      rootLabel: opts.rootLabel || "Medien",
      playRoot: "media",
      searchPlaceholder: "Im Ordner suchen … (Name)",
      pickFile: (f) => { closeModal(); if (opts.onPick) opts.onPick(f); },
    });
    if (picker) picker.go("");
  }

  // Platzhalter-<div> in ein Mehrfach-Auswahl-Dropdown umwandeln.
  // options: [{value,label}] · returns { getValues, setValues }.
  function makeMultiSelect(container, options, cfg) {
    cfg = cfg || {};
    if (!container) return null;
    const chosen = new Set(cfg.initial || []);
    let open = false;
    container.classList.add("multiselect");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "multiselect-btn";
    const panel = document.createElement("div");
    panel.className = "multiselect-panel";
    panel.style.display = "none";
    const getValues = () => options.map((o) => o.value).filter((v) => chosen.has(v));
    const syncLabel = () => {
      const labels = options.filter((o) => chosen.has(o.value)).map((o) => o.label);
      if (!labels.length) { btn.textContent = cfg.placeholder || "Alle"; btn.classList.remove("has-sel"); }
      else { btn.textContent = labels.length <= 2 ? labels.join(", ") : `${labels.length} gewählt`; btn.classList.add("has-sel"); }
    };
    options.forEach((o) => {
      const lab = document.createElement("label");
      lab.className = "multiselect-opt check";
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.value = o.value; cb.checked = chosen.has(o.value);
      cb.addEventListener("change", () => {
        if (cb.checked) chosen.add(o.value); else chosen.delete(o.value);
        syncLabel();
        if (cfg.onChange) cfg.onChange(getValues());
      });
      const sp = document.createElement("span"); sp.textContent = o.label;
      lab.append(cb, sp);
      panel.appendChild(lab);
    });
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      open = !open; panel.style.display = open ? "" : "none";
    });
    document.addEventListener("click", (e) => {
      if (open && !container.contains(e.target)) { open = false; panel.style.display = "none"; }
    });
    container.append(btn, panel);
    syncLabel();
    return {
      getValues,
      setValues: (vals) => {
        chosen.clear();
        (vals || []).forEach((v) => chosen.add(v));
        panel.querySelectorAll("input").forEach((cb) => { cb.checked = chosen.has(cb.value); });
        syncLabel();
      },
    };
  }

  function enableActionButtons() {
    ["btn-enqueue", "btn-vmaf-start", "btn-clear-selection"].forEach((id) => {
      const b = $(id);
      if (b) b.disabled = false;
    });
  }

  // Auswahl (Datei/Ordner) auf der Quellen-Karte aufheben.
  function clearSelection() {
    state.selected = null;
    state.currentInfo = null;
    const badge = $("selection-badge");
    if (badge) badge.textContent = "Nichts ausgewählt";
    const info = $("selected-info");
    if (info) info.innerHTML = "";
    document.querySelectorAll("#browser .row-item.selected").forEach((r) => r.classList.remove("selected"));
    ["btn-enqueue", "btn-vmaf-start", "btn-clear-selection"].forEach((id) => {
      const b = $(id);
      if (b) b.disabled = true;
    });
  }

  async function selectFile(f) {
    state.selected = { path: f.rel, name: f.name, isBatch: false };
    $("selection-badge").textContent = "Datei ausgewählt";
    enableActionButtons();
    $("selected-info").innerHTML = `<strong>${escapeHtml(f.name)}</strong> · analysiere …`;
    document.querySelectorAll(".row-item.selected").forEach((r) => r.classList.remove("selected"));
    try {
      const res = await fetch(`/api/probe?path=${encodeURIComponent(f.rel)}`);
      const info = await res.json();
      if (info.error) {
        $("selected-info").innerHTML =
          `<strong>${escapeHtml(f.name)}</strong> · <span class="bad">${escapeHtml(info.error)}</span>`;
        return;
      }
      renderFileDetails(f.name, info);
      const hdrField = $("hdr-field");
      if (hdrField) hdrField.style.display = info.is_hdr ? "" : "none";
      applyDolbyVision(info);
      refreshSizeTargetHint();
    } catch (e) {
      $("selected-info").innerHTML = `<span class="bad">Analyse-Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  async function refreshSizeTargetHint() {
    const hint = $("opt-size-target-hint");
    const inp = $("opt-size-target");
    if (!hint || !inp) return;
    const mb = parseFloat(inp.value) || 0;
    if (mb <= 0 || !state.selected || state.selected.isBatch) {
      hint.textContent = "";
      return;
    }
    try {
      const d = await (await fetch("/api/size-target/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: state.selected.path,
          size_target_mb: mb,
          audio_tracks: gatherAudioTracks(),
          audio_mode: $("opt-audio-mode") ? $("opt-audio-mode").value : "copy",
        }),
      })).json();
      hint.textContent = d.message ? (" · " + d.message) : "";
      if (d.ok === false) hint.classList.add("bad"); else hint.classList.remove("bad");
    } catch (e) {
      hint.textContent = "";
    }
  }

  function chip(label, value, cls) {
    return `<div class="chip ${cls || ""}"><span class="chip-k">${label}</span><span class="chip-v">${value}</span></div>`;
  }

  // Dolby Vision: Bei einer neuen Datei die DV-Auswahl neu bewerten (Defaults
  // wieder zulassen, bis der Nutzer bewusst umschaltet).
  function applyDolbyVision(info) {
    state.currentInfo = info || null;
    const dvSel = $("opt-dv-mode");
    if (dvSel) dvSel.dataset.userset = "";
    syncDvOption();
  }

  // Steuert HDR- vs. DV-Behandlung: Bei Dolby-Vision-Quellen erscheint die
  // DV-Auswahl (übernehmen / nur HDR10 / Tonemap), sonst die normale HDR-Wahl.
  // Ziel-Profil richtet sich nach dem Encode-Codec: HEVC -> 8.1, AV1 -> 10.1.
  function syncDvOption() {
    const hdrWrap = $("hdr-mode-wrap");
    const dvWrap = $("dv-mode-wrap");
    const dvSel = $("opt-dv-mode");
    const dvHint = $("dv-mode-hint");
    const info = state.currentInfo;
    const codec = $("opt-codec") ? $("opt-codec").value : "";
    const isDv = !!(info && info.dolby_vision);
    if (hdrWrap) hdrWrap.style.display = isDv ? "none" : "";
    if (dvWrap) dvWrap.style.display = isDv ? "" : "none";
    if (!isDv || !dvSel) return;

    const platform = $("opt-platform") ? $("opt-platform").value : "";
    const prof = info.dv_profile || 0;
    const codecLabel = codec === "av1" ? "AV1" : "HEVC";
    // Profil 5 bleibt bei „Übernehmen" unverändert Profil 5, sonst 8.1/10.1.
    const targetProfile = prof === 5 ? "Profil 5" : (codec === "av1" ? "10.1" : "8.1");
    // AV1-Dolby-Vision kann nur der CPU-Encoder (libsvtav1) einbetten – Hardware-
    // Encoder (NVENC/QSV/VAAPI) können weder einbetten noch nachträglich
    // injizieren (dovi_tool kann kein AV1). Dann ist nur HDR10 möglich.
    const av1NeedsCpu = codec === "av1" && platform && platform !== "cpu";

    if (!dvSel.dataset.userset) {
      // Standardwahl: DV übernehmen, außer AV1 ohne CPU (dann HDR10) bzw.
      // Profil 5 ohne HDR10-Fallback (dann Tonemap als sichere Wahl).
      if (av1NeedsCpu) dvSel.value = "hdr10";
      else dvSel.value = prof === 5 ? "tonemap" : "preserve";
    }

    if (dvHint) {
      const p = prof ? `Profil ${prof}` : "Dolby Vision";
      if (av1NeedsCpu) {
        dvHint.textContent = `${p} erkannt. AV1-Dolby-Vision kann nur der `
          + `CPU-Encoder (SVT-AV1) beim Encoden einbetten – mit `
          + `${platform.toUpperCase()} ist keine DV-Übernahme möglich, `
          + `„Übernehmen" fällt auf HDR10 zurück. Für echtes DV: Plattform „CPU" `
          + `wählen (Profil 10.1) oder Codec HEVC nutzen (Profil 8.1).`;
        return;
      }
      let conv = "";
      if (prof === 7) conv = ` Profil 7 wird zu ${targetProfile} konvertiert (Enhancement-Layer entfällt, HDR10-Basis bleibt).`;
      else if (prof === 5) conv = ' Bei „Übernehmen" bleibt es Profil 5 (unverändert) – das braucht einen DV-fähigen Player und hat keinen HDR10-Fallback. Ohne solchen Player ist Tone-Mapping die sichere Wahl (Default).';
      const fallback = prof === 5
        ? " Schlägt ein Schritt fehl, bleibt die (nur mit DV korrekt darstellbare) Basis erhalten."
        : " Schlägt ein Schritt fehl, bleibt die HDR10-Basis erhalten.";
      const how = codec === "av1"
        ? `libsvtav1 bettet die DV-RPU direkt beim Encoden ein → Ziel ${targetProfile} (${codecLabel}).`
        : `„Übernehmen" re-injiziert die RPU nach dem Encode (dovi_tool) → Ziel ${targetProfile} (${codecLabel}).`;
      dvHint.textContent = `${p} erkannt. ${how}${conv}${fallback}`;
    }
  }

  function renderFileDetails(name, info) {
    const chips = [];
    chips.push(chip("Auflösung", `${info.resolution}${info.megapixels ? " · " + info.megapixels + " MP" : ""}`));
    if (info.is_4k) chips.push(chip("Klasse", "4K / UHD", "accent"));
    chips.push(chip("Codec", info.codec.toUpperCase() + (info.profile ? " · " + info.profile : "")));
    chips.push(chip("Bit-Tiefe", info.bit_depth + " bit"));
    if (info.fps) chips.push(chip("FPS", info.fps));
    chips.push(chip("Dynamik", info.hdr_type, info.is_hdr ? "warn" : ""));
    if (info.dolby_vision) {
      chips.push(chip("Dolby Vision", info.dv_profile ? "Profil " + info.dv_profile : "ja", "accent"));
    }
    chips.push(chip("Größe", info.size_human));
    chips.push(chip("Dauer", info.duration_human));
    if (info.overall_bitrate) chips.push(chip("Gesamt-Bitrate", info.overall_bitrate_human));
    chips.push(chip("Video-Bitrate", info.video_bitrate_human));
    chips.push(chip("Pixelformat", info.pix_fmt));
    if (info.color_primaries) chips.push(chip("Farbraum", info.color_primaries));
    chips.push(chip("Container", (info.container || "—").split(",")[0]));

    let audio = "";
    if (info.audio && info.audio.length) {
      // Standard: alle Spuren behalten.
      state.audioTracks = info.audio.map((a, i) => (a.index != null ? a.index : i));
      audio = `<div class="track-block"><div class="track-title">Audiospuren (${info.audio.length}) · einzeln konfigurierbar</div>` +
        info.audio.map((a, i) => audioTrackRow(a, i)).join("") +
        `</div>`;
    }
    let subs = "";
    if (info.subtitles && info.subtitles.length) {
      subs = `<div class="track-block"><div class="track-title">Untertitel (${info.subtitles.length}) · einzeln wählbar</div>` +
        info.subtitles.map((s, i) => subtitleTrackRow(s, i)).join("") +
        `</div>`;
    }

    $("selected-info").innerHTML =
      `<div class="file-title">${escapeHtml(name)}</div>` +
      `<div class="chips">${chips.join("")}</div>${audio}${subs}`;
    wireAudioRows();
  }

  function subtitleTrackRow(s, i) {
    const idx = s.index != null ? s.index : i;
    const info = `${escapeHtml((s.language || "und").toUpperCase())} · ` +
      `${escapeHtml((s.codec || "?").toUpperCase())}` +
      `${s.title ? " · " + escapeHtml(s.title) : ""}`;
    return `<div class="track-sub" data-index="${idx}">
      <label class="check track-enable">
        <input type="checkbox" class="sub-track" value="${idx}" checked />
        <span>${info}</span>
      </label>
      <label class="check sub-flag"><input type="checkbox" class="sub-default" ${s.default ? "checked" : ""} /><span>Default</span></label>
      <label class="check sub-flag"><input type="checkbox" class="sub-forced" ${s.forced ? "checked" : ""} /><span>Forced</span></label>
    </div>`;
  }

  // Per-Spur-Untertitel: null bei fehlender Analyse (Batch), sonst Liste der
  // behaltenen Spuren mit Default/Forced-Flags.
  function gatherSubtitleTracks() {
    const rows = [...document.querySelectorAll(".track-sub")];
    if (!rows.length) return null;
    const list = [];
    for (const row of rows) {
      if (!row.querySelector(".sub-track").checked) continue;
      list.push({
        index: parseInt(row.dataset.index, 10),
        default: row.querySelector(".sub-default").checked,
        forced: row.querySelector(".sub-forced").checked,
      });
    }
    return list;
  }

  const AUDIO_CODEC_OPTS = [
    ["aac", "AAC"], ["opus", "Opus"], ["ac3", "AC3"], ["eac3", "E-AC3"], ["flac", "FLAC"],
  ];

  function audioTrackRow(a, i) {
    const idx = a.index != null ? a.index : i;
    const info = `${escapeHtml((a.language || "und").toUpperCase())} · ` +
      `${escapeHtml(a.codec.toUpperCase())} · ${a.channels}ch` +
      `${a.layout ? " (" + escapeHtml(a.layout) + ")" : ""} · ${a.bitrate_human}` +
      `${a.title ? " · " + escapeHtml(a.title) : ""}`;
    const codecOpts = AUDIO_CODEC_OPTS.map(([v, l]) =>
      `<option value="${v}">${l}</option>`).join("");
    return `<div class="track-audio" data-index="${idx}">
      <div class="track-audio-head">
        <label class="check track-enable">
          <input type="checkbox" class="audio-track" value="${idx}" checked />
          <span>${info}</span>
        </label>
        <select class="audio-t-mode select-sm">
          <option value="std">Standard</option>
          <option value="copy">Kopieren</option>
          <option value="encode">Neu codieren</option>
        </select>
      </div>
      <div class="audio-t-enc" style="display:none">
        <select class="audio-t-codec select-sm">${codecOpts}</select>
        <select class="audio-t-channels select-sm">
          <option value="0">Kanäle: Original</option>
          <option value="2">Stereo</option>
          <option value="1">Mono</option>
        </select>
        <input type="number" class="audio-t-bitrate" min="32" max="640" step="16" value="160" title="kbit/s" />
        <label class="check"><input type="checkbox" class="audio-t-norm" /><span>Normalisieren</span></label>
      </div>
    </div>`;
  }

  function wireAudioRows() {
    document.querySelectorAll(".track-audio").forEach((row) => {
      const mode = row.querySelector(".audio-t-mode");
      const enc = row.querySelector(".audio-t-enc");
      const enable = row.querySelector(".audio-track");
      const sync = () => {
        enc.style.display = (enable.checked && mode.value === "encode") ? "" : "none";
        mode.disabled = !enable.checked;
      };
      mode.addEventListener("change", sync);
      enable.addEventListener("change", sync);
      sync();
    });
  }

  // Per-Spur-Audio: liefert null bei fehlender Analyse (Batch), sonst eine
  // Liste der behaltenen Spuren mit aufgelösten Einstellungen.
  function gatherAudioTrackSettings() {
    const rows = [...document.querySelectorAll(".track-audio")];
    if (!rows.length) return null;
    const gMode = $("opt-audio-mode").value;
    const list = [];
    for (const row of rows) {
      if (!row.querySelector(".audio-track").checked) continue;
      const idx = parseInt(row.dataset.index, 10);
      const rawMode = row.querySelector(".audio-t-mode").value; // std|copy|encode
      let mode = rawMode === "std" ? (gMode === "encode" ? "encode" : "copy") : rawMode;
      const t = { index: idx, mode: mode };
      if (mode === "encode") {
        if (rawMode === "std") {
          t.codec = $("opt-audio-codec").value;
          t.bitrate = parseInt($("opt-audio-bitrate").value, 10);
          t.channels = parseInt($("opt-audio-channels").value, 10);
          t.normalize = $("opt-audio-normalize").checked;
        } else {
          t.codec = row.querySelector(".audio-t-codec").value;
          t.bitrate = parseInt(row.querySelector(".audio-t-bitrate").value, 10) || 160;
          t.channels = parseInt(row.querySelector(".audio-t-channels").value, 10);
          t.normalize = row.querySelector(".audio-t-norm").checked;
        }
      }
      list.push(t);
    }
    return list;
  }

  function selectFolder(path, isRoot) {
    const name = isRoot ? "/media (alle Unterordner)" : path.split("/").pop();
    state.selected = { path: path, name: name, isBatch: true };
    $("selection-badge").textContent = "Ordner ausgewählt (Batch)";
    enableActionButtons();
    $("selected-info").innerHTML =
      `<strong>${escapeHtml(name)}</strong> · Batch-Modus (VMAF-Test repräsentativ für die erste Datei)`;
  }

  /* ------------------------------------------------------------ SETTINGS */
  function initSettings() {
    const quality = $("opt-quality");
    quality.addEventListener("input", () => { $("quality-val").textContent = quality.value; });

    const fg = $("opt-film-grain");
    if (fg) fg.addEventListener("input", () => { $("film-grain-val").textContent = fg.value; });

    // Encode-Ratemodus: CQ-Slider vs. Bitrate-Feld.
    const rate = $("opt-rate-mode");
    const syncRate = () => {
      const cq = rate.value === "cq";
      $("enc-cq-field").style.display = cq ? "" : "none";
      $("enc-br-field").style.display = cq ? "none" : "";
    };
    rate.addEventListener("change", syncRate);
    syncRate();

    $("opt-platform").addEventListener("change", updateCodecAvailability);
    $("opt-codec").addEventListener("change", updateCodecAvailability);
    updateCodecAvailability();

    const dvSel = $("opt-dv-mode");
    if (dvSel) dvSel.addEventListener("change", () => {
      dvSel.dataset.userset = "1";  // bewusste Wahl nicht mehr automatisch überschreiben
    });

    const verifyCb = $("opt-verify-vmaf");
    if (verifyCb) {
      const syncVerify = () => {
        const cfg = $("verify-config");
        if (cfg) cfg.style.display = verifyCb.checked ? "" : "none";
      };
      verifyCb.addEventListener("change", syncVerify);
      syncVerify();
    }

    const chunkedCb = $("opt-chunked");
    if (chunkedCb) {
      const syncChunked = () => {
        const cfg = $("chunked-config");
        if (cfg) cfg.style.display = chunkedCb.checked ? "" : "none";
      };
      chunkedCb.addEventListener("change", syncChunked);
      syncChunked();
    }

    const audioMode = $("opt-audio-mode");
    const audioCodec = $("opt-audio-codec");
    const audioBr = $("opt-audio-bitrate");
    const syncAudio = () => {
      const encoding = audioMode.value === "encode";
      $("audio-encode-opts").classList.toggle("disabled", !encoding);
      // FLAC ist verlustfrei -> keine Bitratenwahl
      $("audio-bitrate-field").classList.toggle("disabled", audioCodec.value === "flac");
    };
    audioMode.addEventListener("change", syncAudio);
    audioCodec.addEventListener("change", syncAudio);
    audioBr.addEventListener("input", () => {
      $("audio-bitrate-val").textContent = audioBr.value;
    });
    syncAudio();

    $("btn-enqueue").addEventListener("click", enqueue);
    const clearSel = $("btn-clear-selection");
    if (clearSel) clearSel.addEventListener("click", clearSelection);
    $("btn-clear").addEventListener("click", async () => {
      await fetch("/api/queue/clear", { method: "POST" });
    });
    $("btn-pause").addEventListener("click", async () => {
      await fetch("/api/queue/pause", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused: !state.paused }),
      });
    });
    const skip = $("btn-skip-encode");
    if (skip) skip.addEventListener("click", () => {
      if (state.awaitingItemId) skipEncode(state.awaitingItemId);
    });
  }

  const COMPARE_LABELS = {
    "nvidia:av1": "AV1 (NVENC)", "nvidia:hevc": "HEVC (NVENC)", "nvidia:h264": "H.264 (NVENC)",
    "intel:av1": "AV1 (QSV)", "intel:hevc": "HEVC (QSV)", "intel:h264": "H.264 (QSV)",
    "amd:av1": "AV1 (VAAPI)", "amd:hevc": "HEVC (VAAPI)", "amd:h264": "H.264 (VAAPI)",
    "cpu:av1": "SVT-AV1 (CPU)", "cpu:hevc": "x265 (CPU)", "cpu:h264": "x264 (CPU)",
  };
  const CODEC_LABELS = { av1: "AV1", hevc: "HEVC / H.265", h264: "H.264" };

  // Vom Server gelieferte Liste tatsächlich verfügbarer Encoder-Kombinationen.
  function encoderMatrix() {
    return (window.APP_CONFIG && window.APP_CONFIG.encoders) || [];
  }
  function encoderInfo(platform, codec) {
    return encoderMatrix().find((e) => e.platform === platform && e.codec === codec);
  }
  function isEncoderAvailable(platform, codec) {
    const e = encoderInfo(platform, codec);
    const present = e ? !!e.available : true; // im FFmpeg-Build vorhanden?
    if (!present) return false;
    // working: true = HW-Test bestanden, false = HW kann das nicht, null/undef = ungetestet
    return (e && e.working === false) ? false : true;
  }

  // Beschriftungs-Suffix für nicht wählbare Codecs (unterscheidet Build vs. HW).
  function encUnavailReason(platform, codec) {
    const e = encoderInfo(platform, codec);
    if (e && e.available && e.working === false) return " — von der Hardware nicht unterstützt";
    return " — nicht verfügbar";
  }

  // Ergebnisse des echten Encoder-Tests laden und in die Matrix übernehmen,
  // dann alle Codec-Dropdowns/Vergleichslisten neu bewerten.
  async function loadCapabilities() {
    try {
      const d = await (await fetch("/api/capabilities")).json();
      const res = (d && d.results) || {};
      if (!Object.keys(res).length) return; // noch nicht getestet -> Build-Fallback
      encoderMatrix().forEach((e) => {
        if (Object.prototype.hasOwnProperty.call(res, e.value)) e.working = res[e.value];
      });
      if ($("opt-codec")) updateCodecAvailability();
      if ($("vt-codec")) vtUpdateCodecAvailability();
      if ($("st-codec")) stUpdateCodec();
      buildCompareOptions();
    } catch (e) { /* still: UI fällt auf Build-Verfügbarkeit zurück */ }
  }

  // Codec-Dropdown je nach gewählter Plattform kennzeichnen (nicht verfügbare
  // Codecs werden deaktiviert), damit klar ist, was die Plattform kann.
  function updateCodecAvailability() {
    const sel = $("opt-codec");
    const plat = $("opt-platform").value;
    if (!sel) return;
    let firstAvail = null;
    [...sel.options].forEach((opt) => {
      const ok = isEncoderAvailable(plat, opt.value);
      opt.disabled = !ok;
      opt.textContent = (CODEC_LABELS[opt.value] || opt.value.toUpperCase())
        + (ok ? "" : encUnavailReason(plat, opt.value));
      if (ok && firstAvail === null) firstAvail = opt.value;
    });
    // Falls der aktuell gewählte Codec auf dieser Plattform fehlt -> umschalten.
    if (sel.selectedOptions[0] && sel.selectedOptions[0].disabled && firstAvail) {
      sel.value = firstAvail;
    }
    const hint = $("codec-hint");
    if (hint) {
      const e = encoderInfo(plat, sel.value);
      hint.textContent = e ? `FFmpeg-Encoder: ${e.encoder}` : "";
    }
    syncDvOption();
  }

  function compareLabel(v, info) {
    if (info) return `${info.codec_label} · ${info.platform_label}`;
    return COMPARE_LABELS[v] || v;
  }

  // Zeigt ALLE verfügbaren Encoder-Kombinationen (Plattform × Codec) als
  // Vergleichsziele im VMAF-Tool an – außer dem gewählten Basis-Encoder.
  function buildCompareOptions() {
    const cont = $("vt-compare");
    if (!cont) return;
    const base = `${$("vt-platform").value}:${$("vt-codec").value}`;
    const prev = new Set(getCompareEncoders());
    const all = encoderMatrix().filter((e) =>
      isEncoderAvailable(e.platform, e.codec) && e.value !== base);
    if (!all.length) {
      cont.innerHTML = '<span class="empty">Keine weiteren Encoder verfügbar.</span>';
      return;
    }
    // Nach Art gruppieren: GPU-Encoder zuerst, dann CPU (Software).
    const groups = [
      { key: "gpu", title: "GPU / Hardware" },
      { key: "cpu", title: "CPU / Software" },
    ];
    cont.innerHTML = groups.map((g) => {
      const items = all.filter((e) => e.kind === g.key);
      if (!items.length) return "";
      return `<div class="cmp-group"><span class="cmp-title">${g.title}</span>` +
        items.map((e) =>
          `<label><input type="checkbox" class="compare-enc" value="${e.value}" ` +
          `${prev.has(e.value) ? "checked" : ""}/>` +
          `<span>${escapeHtml(compareLabel(e.value, e))}</span></label>`
        ).join("") + `</div>`;
    }).join("");
  }

  function getCompareEncoders() {
    return [...document.querySelectorAll(".compare-enc:checked")].map((b) => b.value);
  }

  function gatherAudioTracks() {
    const boxes = [...document.querySelectorAll(".audio-track")];
    if (!boxes.length) return [];               // Batch/kein Probe -> alle
    const sel = boxes.filter((b) => b.checked).map((b) => parseInt(b.value, 10));
    // Alle ausgewählt -> leer lassen (= alle, sauberes Mapping).
    return sel.length === boxes.length ? [] : sel;
  }

  // Gemeinsame Ausgabe-Optionen (Auflösung, HDR, Audio, Untertitel, Post),
  // von Encoding und VMAF-Tool geteilt.
  function gatherOutputCommon() {
    const res = $("opt-resolution").value;
    const perTrack = gatherAudioTrackSettings();
    const subTracks = gatherSubtitleTracks();
    return {
      target_height: res ? parseInt(res, 10) : null,
      hdr_mode: $("opt-hdr-mode") ? $("opt-hdr-mode").value : "tonemap",
      // DV-Behandlung nur mitsenden, wenn die DV-Auswahl aktiv (= DV-Quelle) ist.
      dv_mode: ($("dv-mode-wrap") && $("dv-mode-wrap").style.display !== "none"
                && $("opt-dv-mode")) ? $("opt-dv-mode").value : "",
      keep_subtitles: subTracks === null
        ? ($("opt-keep-subs") ? $("opt-keep-subs").checked : true) : true,
      subtitle_per_track: subTracks !== null,
      subtitle_track_settings: subTracks || [],
      keep_chapters: $("opt-keep-chapters") ? $("opt-keep-chapters").checked : true,
      keep_metadata: $("opt-keep-metadata") ? $("opt-keep-metadata").checked : true,
      denoise: $("opt-denoise") ? $("opt-denoise").value : "off",
      film_grain: $("opt-film-grain") ? parseInt($("opt-film-grain").value, 10) : 0,
      two_pass: $("opt-two-pass") ? $("opt-two-pass").checked : false,
      autocrop: $("opt-autocrop") ? $("opt-autocrop").checked : false,
      post_processing: $("opt-post").value,
      integrity_check: $("opt-integrity") ? $("opt-integrity").checked : true,
      safe_replace: $("opt-safe-replace") ? $("opt-safe-replace").checked : true,
      audio_mode: $("opt-audio-mode").value,
      audio_codec: $("opt-audio-codec").value,
      audio_bitrate: parseInt($("opt-audio-bitrate").value, 10),
      audio_channels: parseInt($("opt-audio-channels").value, 10),
      audio_normalize: $("opt-audio-normalize").checked,
      audio_tracks: gatherAudioTracks(),
      audio_per_track: perTrack !== null && $("opt-audio-mode").value !== "none",
      audio_track_settings: perTrack || [],
      container: $("opt-container") ? $("opt-container").value : "auto",
      name_pattern: $("opt-name-pattern") ? ($("opt-name-pattern").value.trim() || "{stem}{suffix}") : "{stem}{suffix}",
      on_duplicate: $("opt-on-duplicate") ? $("opt-on-duplicate").value : "ask",
      max_output_mb: $("opt-max-output-mb") ? (parseFloat($("opt-max-output-mb").value) || 0) : 0,
      max_video_bitrate_kbps: $("opt-max-bitrate") ? (parseInt($("opt-max-bitrate").value, 10) || 0) : 0,
      size_target_mb: $("opt-size-target") ? (parseFloat($("opt-size-target").value) || 0) : 0,
      ...outTargetVals("opt"),
    };
  }

  // Ausgabe-Ziel für ein Feld-Präfix (z. B. "opt", "remux").
  function outTargetVals(prefix) {
    const modeEl = $(prefix + "-out-mode");
    const subEl = $(prefix + "-out-subdir");
    const mode = modeEl ? modeEl.value : "default";
    return {
      out_mode: mode,
      out_subdir: (mode === "custom" && subEl) ? (subEl.value || "").trim() : "",
    };
  }

  function syncOutModeUI(prefix) {
    const modeEl = $(prefix + "-out-mode");
    if (!modeEl) return;
    const box = modeEl.closest("[data-out-target]");
    const custom = box ? box.querySelector("[data-out-custom]") : null;
    if (custom) custom.style.display = modeEl.value === "custom" ? "" : "none";
    const hint = box ? box.querySelector("[data-out-default-hint]") : null;
    if (hint) {
      const def = (window.APP_CONFIG && APP_CONFIG.defaultOutput) || "output";
      hint.style.display = modeEl.value === "default" ? "" : "none";
      hint.textContent = `Standard-Ausgabe: ${def} (Quellstruktur wird gespiegelt).`;
    }
  }

  function initOutTargets() {
    ["opt", "remux", "merge", "split", "st"].forEach((prefix) => {
      const modeEl = $(prefix + "-out-mode");
      if (modeEl) {
        modeEl.addEventListener("change", () => syncOutModeUI(prefix));
        syncOutModeUI(prefix);
      }
      const browse = $(prefix + "-out-browse");
      if (browse) browse.addEventListener("click", () => openOutPicker(prefix));
    });
  }

  function initMediaSettings() {
    const inp = $("cfg-default-output");
    if (!inp) return;
    const abs = $("cfg-default-output-abs");
    const badge = $("media-settings-badge");
    const apply = (d) => {
      inp.value = d.default_output || "output";
      if (abs) abs.textContent = d.default_output_abs
        ? `Absolut: ${d.default_output_abs}` : "";
      if (badge) badge.textContent = d.default_output || "output";
      const lbl = $("default-output-label");
      if (lbl) lbl.textContent = d.default_output || "output";
      if (window.APP_CONFIG) APP_CONFIG.defaultOutput = d.default_output || "output";
      ["opt", "remux", "merge", "split", "st"].forEach(syncOutModeUI);
    };
    fetch("/api/settings").then((r) => r.json()).then(apply).catch(() => {});
    const save = $("btn-media-settings-save");
    if (save) save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const d = await (await fetch("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ default_output: inp.value.trim() || "output" }),
        })).json();
        if (d.error) { alert(d.error); return; }
        apply(d);
      } finally { save.disabled = false; }
    });
    const browse = $("btn-default-out-browse");
    if (browse) browse.addEventListener("click", () => {
      openFolderPickerModal({
        title: "Standard-Ausgabeordner wählen",
        onPick: (folder) => { inp.value = folder || "output"; },
      });
    });
  }

  // Ordner im Medienbaum wählen → media-relativer Pfad in out-subdir.
  function openOutPicker(prefix) {
    state.outPick = { prefix, path: "" };
    openModal("Zielordner wählen",
      '<div class="breadcrumb" id="out-pick-crumb"></div>' +
      '<div class="browser browser-sm" id="out-pick-browser"><div class="browser-loading">Lade …</div></div>' +
      '<div class="lib-actions" style="margin-top:10px">' +
      '<button class="btn btn-primary btn-sm" id="out-pick-choose">Diesen Ordner wählen</button>' +
      '<span id="out-pick-sel" class="muted"></span></div>');
    const choose = $("out-pick-choose");
    if (choose) choose.addEventListener("click", () => {
      const sub = (state.outPick || {}).path || "";
      const el = $(prefix + "-out-subdir");
      if (el) el.value = sub;
      const modeEl = $(prefix + "-out-mode");
      if (modeEl) { modeEl.value = "custom"; syncOutModeUI(prefix); }
      closeModal();
    });
    outPickLoadDir("");
  }

  async function outPickLoadDir(path) {
    const el = $("out-pick-browser");
    if (!el) return;
    el.innerHTML = '<div class="browser-loading">Lade Verzeichnis …</div>';
    try {
      const data = await (await fetch(
        `/api/browse?path=${encodeURIComponent(path)}`)).json();
      if (data.error) { el.innerHTML = `<div class="browser-loading">${escapeHtml(data.error)}</div>`; return; }
      state.outPick.path = data.path || "";
      const sel = $("out-pick-sel");
      if (sel) sel.textContent = data.path ? `Ziel: ${data.path}` : "Ziel: (Medienwurzel)";
      const bc = $("out-pick-crumb");
      if (bc) {
        bc.innerHTML = "";
        const r = document.createElement("a");
        r.textContent = "Medien";
        r.onclick = () => outPickLoadDir("");
        bc.appendChild(r);
        if (data.path) {
          let acc = "";
          data.path.split("/").forEach((p) => {
            acc = acc ? `${acc}/${p}` : p;
            const sep = document.createElement("span"); sep.textContent = " / "; bc.appendChild(sep);
            const a = document.createElement("a"); a.textContent = p;
            const t = acc; a.onclick = () => outPickLoadDir(t); bc.appendChild(a);
          });
        }
      }
      el.innerHTML = "";
      if (data.parent !== null && data.parent !== undefined)
        el.appendChild(stDirRow("..", () => outPickLoadDir(data.parent || "")));
      (data.dirs || []).forEach((d) => el.appendChild(stDirRow(d.name, () => outPickLoadDir(d.rel))));
      if (!(data.dirs || []).length) {
        const hint = document.createElement("div");
        hint.className = "browser-loading";
        hint.textContent = data.exists ? "Keine Unterordner." : "Ordner wird beim Job angelegt.";
        el.appendChild(hint);
      }
    } catch (e) {
      el.innerHTML = `<div class="browser-loading">Fehler: ${escapeHtml(String(e))}</div>`;
    }
  }

  // Encoding-Seite: reines Encoden mit manuellem Wert (keine Test-Encodes).
  function gatherSettings() {
    const rateMode = $("opt-rate-mode").value;
    const quality = rateMode === "cq"
      ? parseInt($("opt-quality").value, 10)
      : parseInt($("opt-bitrate").value, 10);
    return {
      platform: $("opt-platform").value,
      codec: $("opt-codec").value,
      quality: quality,
      vmaf_check: false,
      workflow: "auto",
      rate_mode: rateMode,
      suffix: "_" + $("opt-codec").value,
      ...gatherOutputCommon(),
      anime: $("opt-anime") ? $("opt-anime").checked : false,
      verify_vmaf: $("opt-verify-vmaf") ? $("opt-verify-vmaf").checked : false,
      verify_min: $("opt-verify-min") ? parseFloat($("opt-verify-min").value) || 93 : 93,
      verify_retry: $("opt-verify-retry") ? $("opt-verify-retry").checked : false,
      chunked: $("opt-chunked") ? $("opt-chunked").checked : false,
      chunk_seconds: $("opt-chunk-seconds") ? parseInt($("opt-chunk-seconds").value, 10) || 60 : 60,
      chunk_cq_range: $("opt-chunk-range") ? parseInt($("opt-chunk-range").value, 10) || 6 : 6,
    };
  }

  async function approveEncode(itemId, resultIndex) {
    await fetch(`/api/queue/${itemId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result_index: resultIndex }),
    });
    state.awaitingItemId = null;
  }

  async function skipEncode(itemId) {
    await fetch(`/api/queue/${itemId}/skip`, { method: "POST" });
    state.awaitingItemId = null;
  }

  async function enqueue() {
    if (!state.selected) return;
    // Original ersetzen: ausdrückliche Bestätigung einholen.
    const post = $("opt-post") ? $("opt-post").value : "keep";
    if ((post === "replace" || post === "inplace") &&
        !window.confirm("Original ersetzen?\n\nDie Quelldatei wird nach erfolgreichem Encode durch die neue Datei ersetzt. " +
          "Bei aktiver \"sicherer Nachbehandlung\" nur, wenn die Ausgabe intakt ist und die Qualität stimmt.")) {
      return;
    }
    const settings = gatherSettings();
    const paths = [state.selected.path];
    const ok = await confirmDryRunOrDups(paths, settings);
    if (!ok) return;
    const btn = $("btn-enqueue");
    btn.disabled = true;
    const payload = {
      path: state.selected.path,
      is_batch: state.selected.isBatch,
      ...settings,
    };
    try {
      const res = await fetch("/api/enqueue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.error) {
        $("selected-info").innerHTML = `<span class="bad">${data.error}</span>`;
      } else {
        $("selected-info").innerHTML =
          `<span class="good">${data.added} Auftrag/Aufträge hinzugefügt.</span>`;
      }
    } catch (e) {
      $("selected-info").innerHTML = `<span class="bad">Fehler: ${e}</span>`;
    } finally {
      btn.disabled = false;
    }
  }

  /* ------------------------------------ Dry-Run / Duplikat-Vorschau */
  function tt(s) {
    return window.I18N ? I18N.t(s) : s;
  }

  async function fetchPreview(paths, settings, estimates) {
    const res = await fetch("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, settings: settings || {}, estimates: estimates || {} }),
    });
    return res.json();
  }

  /** Zeigt Dry-Run-Modal. resolve(true) = starten, false = abbrechen. */
  function showPreviewModal(preview) {
    const rows = (preview && (preview.items || preview.jobs)) || [];
    const dups = rows.filter((r) => r.duplicate).length;
    const table = rows.length ? `
      <div class="table-wrap preview-table-wrap">
        <table class="queue-table">
          <thead><tr>
            <th>${tt("Quelle")}</th><th>${tt("Ziel")}</th>
            <th>${tt("Schätzung")}</th><th>${tt("Flags")}</th>
          </tr></thead>
          <tbody>${rows.map((r) => {
            const flags = [];
            if (r.exists) flags.push(tt("existiert"));
            if (r.history_done) flags.push(tt("in Historie"));
            const est = r.est_output_bytes
              ? formatBytes(r.est_output_bytes)
              : (r.est_saved_bytes ? ("≈ −" + formatBytes(r.est_saved_bytes)) : "—");
            return `<tr>
              <td title="${escapeHtml(r.source)}">${escapeHtml(r.source_name || r.source_rel || "")}</td>
              <td title="${escapeHtml(r.output)}">${escapeHtml(r.output_name || r.output_rel || "")}</td>
              <td>${escapeHtml(est)}</td>
              <td class="${r.duplicate ? "warn" : ""}">${flags.map(escapeHtml).join(", ") || "—"}</td>
            </tr>`;
          }).join("")}</tbody>
        </table>
      </div>
      <p class="muted" style="margin-top:8px">${rows.length} ${tt("Datei(en)")}${dups ? ` · <span class="warn">${dups} ${tt("Duplikat(e)")}</span>` : ""}</p>`
      : `<p class="muted">${tt("Keine Vorschau.")}</p>`;
    return new Promise((resolve) => {
      let settled = false;
      openModal(tt("Dry-Run Vorschau"), `
        ${table}
        <div class="lib-actions" style="margin-top:12px">
          <button class="btn btn-primary" id="preview-go">${tt("Trotzdem starten")}</button>
          <button class="btn btn-ghost" id="preview-cancel">${tt("Abbrechen")}</button>
        </div>`);
      const done = (v) => {
        if (settled) return;
        settled = true;
        closeModal();
        resolve(v);
      };
      const go = $("preview-go");
      const cancel = $("preview-cancel");
      if (go) go.addEventListener("click", () => done(true));
      if (cancel) cancel.addEventListener("click", () => done(false));
      // Escape / X-Button: Abbrechen
      const m = $("app-modal");
      const onHide = () => {
        if (!settled && m && m.style.display === "none") done(false);
      };
      const obs = new MutationObserver(onHide);
      if (m) obs.observe(m, { attributes: true, attributeFilter: ["style"] });
    });
  }

  /** Bei on_duplicate=ask immer Preview; sonst nur bei Duplikaten warnen. */
  async function confirmDryRunOrDups(paths, settings, estimates, { forcePreview = false } = {}) {
    if (!paths || !paths.length) return false;
    let preview;
    try {
      preview = await fetchPreview(paths, settings, estimates);
    } catch (e) {
      return window.confirm(tt("Vorschau fehlgeschlagen. Trotzdem fortfahren?"));
    }
    if (preview.error) {
      alert(preview.error);
      return false;
    }
    const jobs = preview.items || preview.jobs || [];
    const dups = jobs.filter((j) => j.duplicate);
    const onDup = (settings.on_duplicate || "ask").toLowerCase();
    if (forcePreview || onDup === "ask") {
      return showPreviewModal(preview);
    }
    if (dups.length && onDup !== "overwrite" && onDup !== "skip") {
      return showPreviewModal(preview);
    }
    return true;
  }

  /* ----------------------------------------------------------- VMAF-TOOL */
  let vtPrevRateFamily = null;

  function initVmafTool() {
    if (!$("btn-vmaf-start")) return;
    const clip = $("vt-clip");
    if (clip) clip.addEventListener("input", () => { $("vt-clip-val").textContent = clip.value; });

    $("vt-rate-mode").addEventListener("change", () => vtUpdateTestHints(true));
    vtUpdateTestHints(false);

    $("vt-platform").addEventListener("change", () => {
      vtUpdateCodecAvailability();
      buildCompareOptions();
    });
    $("vt-codec").addEventListener("change", () => {
      vtUpdateCodecAvailability();
      buildCompareOptions();
    });
    vtUpdateCodecAvailability();
    buildCompareOptions();

    $("btn-vmaf-start").addEventListener("click", vtEnqueue);
  }

  function vtUpdateCodecAvailability() {
    const sel = $("vt-codec");
    const plat = $("vt-platform").value;
    if (!sel) return;
    let firstAvail = null;
    [...sel.options].forEach((opt) => {
      const ok = isEncoderAvailable(plat, opt.value);
      opt.disabled = !ok;
      opt.textContent = (CODEC_LABELS[opt.value] || opt.value.toUpperCase())
        + (ok ? "" : encUnavailReason(plat, opt.value));
      if (ok && firstAvail === null) firstAvail = opt.value;
    });
    if (sel.selectedOptions[0] && sel.selectedOptions[0].disabled && firstAvail) {
      sel.value = firstAvail;
    }
    const hint = $("vt-codec-hint");
    if (hint) {
      const e = encoderInfo(plat, sel.value);
      hint.textContent = e ? `FFmpeg-Encoder: ${e.encoder}` : "";
    }
  }

  function vtUpdateTestHints(refill) {
    const mode = $("vt-rate-mode").value;
    const inputs = document.querySelectorAll(".vt-test-val");
    const hint = $("vt-test-hint");
    const fam = mode === "cq" ? "cq" : "bitrate";
    if (mode === "cq") {
      hint.textContent = "CQ/QP: niedrig = hohe Qualität · hoch = kleinere Datei · leere Felder werden ignoriert";
      inputs.forEach((i) => { i.min = 1; i.max = 51; });
    } else {
      hint.textContent = "Bitrate in kbit/s (z. B. 8000, 6000, 4000, 2000) · leere Felder werden ignoriert";
      inputs.forEach((i) => { i.min = 500; i.max = 50000; });
    }
    if (refill && fam !== vtPrevRateFamily) {
      const defaults = mode === "cq" ? [20, 24, 28, 32] : [8000, 6000, 4000, 2000];
      inputs.forEach((inp, idx) => { inp.value = defaults[idx]; });
    }
    vtPrevRateFamily = fam;
  }

  function vtGatherTestValues() {
    return [...document.querySelectorAll(".vt-test-val")]
      .map((i) => parseInt(i.value, 10))
      .filter((v) => !isNaN(v) && v > 0)
      .slice(0, 4);
  }

  function vtGatherSettings() {
    return {
      platform: $("vt-platform").value,
      codec: $("vt-codec").value,
      vmaf_check: true,
      workflow: "compare_only",
      rate_mode: $("vt-rate-mode").value,
      compare_encoders: getCompareEncoders(),
      test_values: vtGatherTestValues(),
      clip_seconds: parseInt($("vt-clip").value, 10),
      samples: parseInt($("vt-samples").value, 10),
      generate_screenshots: $("vt-screenshots").checked,
      suffix: "_" + $("vt-codec").value,
      ...gatherOutputCommon(),
      anime: $("vt-anime") ? $("vt-anime").checked : false,
    };
  }

  async function vtEnqueue() {
    if (!state.selected) return;
    const btn = $("btn-vmaf-start");
    btn.disabled = true;
    // Exakte Quelle des Vergleichs merken (rel. Pfad ist hier garantiert korrekt),
    // damit „→ Encoding" später genau diese Datei übernimmt.
    state.vmafSource = {
      path: state.selected.path, name: state.selected.name,
      isBatch: state.selected.isBatch, info: state.currentInfo || null,
    };
    const payload = {
      path: state.selected.path,
      is_batch: state.selected.isBatch,
      ...vtGatherSettings(),
    };
    try {
      const res = await fetch("/api/enqueue", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      $("selected-info").innerHTML = data.error
        ? `<span class="bad">${escapeHtml(data.error)}</span>`
        : `<span class="good">VMAF-Vergleich gestartet (${data.added} Auftrag/Aufträge).</span>`;
    } catch (e) {
      $("selected-info").innerHTML = `<span class="bad">Fehler: ${e}</span>`;
    } finally {
      btn.disabled = false;
    }
  }

  // Gewinner (oder gewählte Zeile) ins Encoding übernehmen und dorthin wechseln.
  async function transferToEncode(r) {
    if (!r) return;
    // Archivierte Quelle nicht mehr vorhanden? Dann früh und deutlich abbrechen.
    if (state.vmafSource && state.vmafSource.available === false) {
      alert("Die Quelldatei dieses gespeicherten Vergleichs ist nicht mehr "
        + "verfügbar (verschoben/gelöscht). Bitte die Datei erneut im Encoding "
        + "auswählen.");
      return;
    }
    navTo("encode");
    // Die im Vergleich genutzte Quelle wieder korrekt auswählen (inkl. Re-Probe),
    // damit der folgende „Zur Warteschlange hinzufügen" GENAU diese Datei
    // encodiert – auch wenn zwischenzeitlich eine andere Datei angeklickt wurde.
    // Wir nutzen bewusst selectFile (wie ein echter Klick), das ist robuster als
    // den DOM manuell zu rekonstruieren.
    const src = state.vmafSource;
    if (src && src.path && !src.isBatch) {
      try {
        await selectFile({ rel: src.path, name: src.name, size_human: "" });
      } catch (e) {
        // Fallback: wenigstens die Auswahl setzen, damit Enqueue funktioniert.
        state.selected = { path: src.path, name: src.name, isBatch: false };
        enableActionButtons();
      }
    }
    // Encoder-Einstellungen des Gewinners NACH der Auswahl setzen (die Auswahl
    // kann HDR-/DV-Defaults verändern; die Gewinner-Werte haben Vorrang).
    const setSel = (id, val) => {
      const el = $(id);
      if (el && val != null) { el.value = String(val); el.dispatchEvent(new Event("change")); }
    };
    setSel("opt-platform", r.platform);
    setSel("opt-codec", r.codec);
    setSel("opt-rate-mode", r.rate_mode || "cq");
    if ((r.rate_mode || "cq") === "cq") {
      setSel("opt-quality", r.value);
      if ($("quality-val")) $("quality-val").textContent = r.value;
    } else {
      setSel("opt-bitrate", r.value);
    }
    // Anime-Modus aus dem VMAF-Tool übernehmen (VMAF-NEG + 10-bit).
    const vtAnime = $("vt-anime"), optAnime = $("opt-anime");
    if (vtAnime && optAnime) {
      optAnime.checked = vtAnime.checked;
      optAnime.dispatchEvent(new Event("change"));
    }
    updateCodecAvailability();
  }

  /* ------------------------------------------------------------ WEBSOCKET */
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => setConn(true);
    ws.onclose = () => { setConn(false); setTimeout(connectWs, 2500); };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      if (data.hardware) updateHardware(data.hardware);
      if (data.queue) updateQueue(data.queue);
    };
  }

  function setConn(online) {
    $("conn-dot").classList.toggle("online", online);
    $("conn-text").textContent = online ? "Live verbunden" : "Getrennt – erneuter Versuch …";
  }

  /* ------------------------------------------------------------ HARDWARE */
  function setRing(id, pct) {
    const el = $(id);
    if (!el) return;
    const off = RING_CIRC * (1 - Math.min(100, pct) / 100);
    el.style.strokeDashoffset = off;
    let color = cssVar("--good");
    if (pct >= 85) color = cssVar("--bad");
    else if (pct >= 60) color = cssVar("--warn");
    el.style.stroke = color;
  }

  function updateHardware(hw) {
    $("cpu-pct").textContent = `${Math.round(hw.cpu_percent)}%`;
    $("cpu-sub").textContent = `${hw.cpu_cores} Threads`;
    setRing("ring-cpu", hw.cpu_percent);

    const sub2 = $("cpu-sub2");
    if (sub2) {
      const parts = [];
      if (hw.cpu_temp != null) parts.push(`${Math.round(hw.cpu_temp)}°C`);
      if (hw.cpu_freq_mhz != null) parts.push(`${(hw.cpu_freq_mhz / 1000).toFixed(1)} GHz`);
      if (Array.isArray(hw.load_avg) && hw.load_avg.length) parts.push(`load ${hw.load_avg[0]}`);
      sub2.textContent = parts.join(" · ");
    }

    $("ram-pct").textContent = `${Math.round(hw.ram_percent)}%`;
    $("ram-sub").textContent = `${hw.ram_used_gb} / ${hw.ram_total_gb} GB`;
    setRing("ring-ram", hw.ram_percent);

    renderGpus(hw.gpus || []);

    if (hw.history) {
      drawSpark("spark-cpu", hw.history.cpu, cssVar("--accent") || "#39d");
      const gpuItem = $("spark-gpu-item");
      if (hw.history.has_gpu) {
        if (gpuItem) gpuItem.style.display = "";
        drawSpark("spark-gpu", (hw.history.gpu || []).map((v) => v == null ? 0 : v),
                  cssVar("--good") || "#4c8");
      } else if (gpuItem) {
        gpuItem.style.display = "none";
      }
    }
  }

  function drawSpark(id, data, color) {
    const cv = $(id);
    if (!cv || !Array.isArray(data) || !data.length) return;
    const ctx = cv.getContext("2d");
    const w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    const n = data.length;
    const x = (i) => (n <= 1 ? 0 : (i / (n - 1)) * w);
    const y = (v) => h - (Math.max(0, Math.min(100, v)) / 100) * (h - 2) - 1;
    // Fläche
    ctx.beginPath();
    ctx.moveTo(x(0), h);
    data.forEach((v, i) => ctx.lineTo(x(i), y(v)));
    ctx.lineTo(x(n - 1), h);
    ctx.closePath();
    ctx.globalAlpha = 0.15;
    ctx.fillStyle = color;
    ctx.fill();
    // Linie
    ctx.globalAlpha = 1;
    ctx.beginPath();
    data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    // aktueller Wert
    const last = Math.round(data[n - 1]);
    ctx.globalAlpha = 0.9;
    ctx.fillStyle = color;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(`${last}%`, w - 2, 10);
  }

  function renderGpus(gpus) {
    const cont = $("gpu-container");
    if (!gpus.length) {
      if (!cont.dataset.empty) {
        cont.innerHTML =
          '<div class="ring-card"><div class="ring-label"><span class="ring-name">GPU</span><span class="ring-sub">nicht erkannt</span></div></div>';
        cont.dataset.empty = "1";
      }
      return;
    }
    delete cont.dataset.empty;
    if (cont.children.length !== gpus.length) {
      cont.innerHTML = gpus.map((g, i) => `
        <div class="ring-card">
          <svg class="ring" viewBox="0 0 120 120">
            <circle class="ring-track" cx="60" cy="60" r="52"></circle>
            <circle class="ring-value" id="ring-gpu-${i}" cx="60" cy="60" r="52"></circle>
          </svg>
          <div class="ring-label">
            <span class="ring-pct" id="gpu-pct-${i}">—</span>
            <span class="ring-name">GPU · ${g.vendor.toUpperCase()}</span>
            <span class="ring-sub" id="gpu-sub-${i}"></span>
          </div>
        </div>`).join("");
    }
    gpus.forEach((g, i) => {
      const pct = g.util == null ? 0 : g.util;
      $(`gpu-pct-${i}`).textContent = g.util == null ? "—" : `${Math.round(g.util)}%`;
      setRing(`ring-gpu-${i}`, pct);
      let sub = g.name || "";
      if (g.mem_used != null && g.mem_total != null) {
        sub = `${Math.round(g.mem_used)}/${Math.round(g.mem_total)} MB`;
      }
      if (g.temperature != null) sub += ` · ${Math.round(g.temperature)}°C`;
      $(`gpu-sub-${i}`).textContent = sub;
    });
  }

  /* --------------------------------------------------------------- QUEUE */
  function updateQueue(q) {
    if (!q) return; // ohne Daten nichts tun – der WS-Poll aktualisiert gleich
    $("total-saved").textContent = q.total_saved_human;
    const c = q.counts;
    $("cnt-wait").textContent = `${c.waiting} wartend`;
    $("cnt-run").textContent = `${c.running} aktiv`;
    $("cnt-done").textContent = `${c.done} fertig`;
    $("cnt-fail").textContent = `${c.failed} fehlgeschlagen`;
    if ($("cnt-await")) $("cnt-await").textContent = `${c.awaiting || 0} Auswahl`;

    state.paused = !!q.paused;
    const pauseBtn = $("btn-pause");
    if (pauseBtn) {
      pauseBtn.textContent = state.paused ? "Fortsetzen" : "Pausieren";
      pauseBtn.classList.toggle("btn-primary", state.paused);
    }
    $("global-status").textContent = q.paused ? "Pausiert"
      : (q.gate_message && c.waiting ? `⏸ ${q.gate_message}`
        : (q.status_message || (c.running ? "Verarbeitung läuft" : "Bereit")));
    const etaEl = $("queue-eta");
    if (etaEl) {
      const eta = q.queue_eta_human || "—";
      etaEl.textContent = `ETA ${eta}`;
      etaEl.title = tt("Geschätzte Restzeit der Warteschlange");
    }

    const activeIds = q.active_ids || (q.active_id ? [q.active_id] : []);
    state.lastItems = q.items;
    state.lastActiveId = q.active_id;
    renderQueueTable(q.items, activeIds);
    renderActiveProgress(q.items, activeIds);
    renderVmaf(q.items, q.active_id);
  }

  function statusBadge(status) {
    const map = {
      "wartend": "badge-wait", "vmaf-test": "badge-run", "in arbeit": "badge-run",
      "auswahl": "badge-await", "fertig": "badge-done", "fehlgeschlagen": "badge-fail",
      "abgebrochen": "badge-fail",
    };
    return `<span class="badge ${map[status] || ""}">${status}</span>`;
  }

  const CODEC_SHORT = {
    "cpu:av1": "SVT-AV1", "cpu:hevc": "x265", "cpu:h264": "x264",
    "nvidia:av1": "AV1", "nvidia:hevc": "HEVC", "nvidia:h264": "H.264",
    "intel:av1": "AV1", "intel:hevc": "HEVC", "intel:h264": "H.264",
    "amd:av1": "AV1", "amd:hevc": "HEVC", "amd:h264": "H.264",
  };

  function codecName(s) {
    return CODEC_SHORT[`${s.platform}:${s.codec}`] || (s.codec || "").toUpperCase();
  }

  function settingsLabel(it) {
    const s = it.settings;
    let val;
    if (s.rate_mode === "bitrate") val = `${s.quality} kbit/s`;
    else if (s.rate_mode === "abr") val = `ABR ${s.quality}`;
    else val = `CQ ${s.quality}`;
    let verify = "";
    if (it.vmaf_verify != null) {
      const min = (s.verify_min != null) ? s.verify_min : 93;
      const ok = it.vmaf_verify >= min;
      const retry = it.verify_attempts > 1 ? ` ·${it.verify_attempts}×` : "";
      verify = ` <span class="vmaf-verify ${ok ? "vv-ok" : "vv-bad"}" `
        + `title="Gemessener VMAF der Ausgabe (Ziel ≥ ${min})">VMAF ${it.vmaf_verify.toFixed(1)}${retry}</span>`;
    }
    let extra = "";
    if (it.crop) {
      extra += ` <span class="codec-badge" title="Auto-Crop angewendet">✂ ${escapeHtml(it.crop)}</span>`;
    }
    if (it.integrity_ok === false) {
      extra += ` <span class="vmaf-verify vv-bad" title="${escapeHtml(it.integrity_msg || "Integritäts-Check fehlgeschlagen")}">⚠ Integrität</span>`;
    } else if (it.integrity_ok === true) {
      extra += ` <span class="vmaf-verify vv-ok" title="Integritäts-Check bestanden">✓ intakt</span>`;
    }
    if (it.caps_failed) {
      extra += ` <span class="vmaf-verify vv-bad" title="Größen-/Bitrate-Cap überschritten">⚠ Cap</span>`;
    }
    return `<span class="codec-badge">${escapeHtml(codecName(s))}</span> ${val}${verify}${extra}`;
  }

  function renderQueueTable(items, activeIds) {
    const body = $("queue-body");
    const active = new Set(activeIds || []);
    if (!items.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="7">Warteschlange ist leer.</td></tr>';
      return;
    }
    const DONE = ["fertig", "fehlgeschlagen", "abgebrochen"];
    body.innerHTML = items.map((it) => {
      const reso = it.info ? it.info.resolution : "—";
      const canCancel = ["wartend", "auswahl"].includes(it.status) || active.has(it.id);
      const cancelBtn = canCancel
        ? `<button class="btn btn-ghost btn-sm" data-cancel="${it.id}">Abbrechen</button>` : "";
      const requeueBtn = DONE.includes(it.status)
        ? `<button class="btn btn-ghost btn-sm" data-requeue="${it.id}" title="Erneut einreihen">Erneut</button>` : "";
      const moveBtns = it.status === "wartend"
        ? `<button class="btn btn-ghost btn-sm iconbtn" data-move="${it.id}" data-dir="-1" title="Nach oben">↑</button>` +
          `<button class="btn btn-ghost btn-sm iconbtn" data-move="${it.id}" data-dir="1" title="Nach unten">↓</button>` : "";
      const err = it.error
        ? `<div class="queue-err" title="${escapeHtml(it.error)}">${escapeHtml(it.error.slice(0, 200))}${it.error.length > 200 ? " …" : ""}</div>`
        : "";
      // Dauer: laufend (aktiv) oder final (abgeschlossen).
      const dur = (active.has(it.id) || DONE.includes(it.status)) ? (it.duration_human || "—") : "—";
      const finished = DONE.includes(it.status) && it.finished_at
        ? `<div class="muted" style="font-size:11px">${new Date(it.finished_at * 1000).toLocaleTimeString().slice(0,5)}</div>` : "";
      return `<tr class="queue-row" data-details="${it.id}" title="Details / ffprobe anzeigen">
        <td><span class="queue-title-link">${escapeHtml(it.title)}</span>${err}</td>
        <td>${reso}</td>
        <td class="status-cell">${statusBadge(it.status)}</td>
        <td>${settingsLabel(it)}</td>
        <td>${dur}${finished}</td>
        <td class="good">${it.saved_human}</td>
        <td class="row-actions">${moveBtns}${requeueBtn}${cancelBtn}</td>
      </tr>`;
    }).join("");
    body.querySelectorAll("[data-cancel]").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        fetch(`/api/queue/${b.dataset.cancel}/cancel`, { method: "POST" });
      });
    });
    body.querySelectorAll("[data-requeue]").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        requeueJob(b.dataset.requeue, false);
      });
    });
    body.querySelectorAll("[data-move]").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        fetch(`/api/queue/${b.dataset.move}/move?direction=${b.dataset.dir}`, { method: "POST" });
      });
    });
    body.querySelectorAll("tr.queue-row").forEach((tr) => {
      tr.addEventListener("click", () => openQueueDetails(tr.dataset.details));
    });
  }

  async function requeueJob(id, fromHistory) {
    if (!id) return;
    const url = fromHistory
      ? `/api/history/${encodeURIComponent(id)}/requeue`
      : `/api/queue/${encodeURIComponent(id)}/requeue`;
    try {
      const r = await fetch(url, { method: "POST" });
      const d = await r.json();
      if (d.error) alert(d.error);
    } catch (e) {
      alert(String(e));
    }
  }

  function renderActiveProgress(items, activeIds) {
    const card = $("progress-card");
    const list = $("progress-list");
    const active = new Set(activeIds || []);
    const jobs = items.filter((i) => active.has(i.id));
    if (!jobs.length) {
      showCard(card, false);
      list.innerHTML = "";
      return;
    }
    showCard(card, true);
    const enc = jobs.filter((j) => j.status !== "vmaf-test").length;
    const ana = jobs.length - enc;
    const parts = [];
    if (enc) parts.push(enc === 1 ? "1 Encode" : `${enc} Encodes`);
    if (ana) parts.push(ana === 1 ? "1 VMAF-Analyse" : `${ana} VMAF-Analysen`);
    $("progress-count").textContent = parts.join(" + ") || "—";
    list.innerHTML = jobs.map(progressBlock).join("");
  }

  const VMAF_PHASE = {
    reference: "Referenz-Clip", encode: "Test-Encode", vmaf: "VMAF-Vergleich",
  };

  function progressBlock(job) {
    const analyzing = job.status === "vmaf-test";
    const p = job.progress || {};
    const pct = p.percent != null ? p.percent : (analyzing ? 0 : 0);
    const stage = job.message || (analyzing ? "VMAF-Analyse läuft …" : "Encode");

    let stats;
    if (analyzing) {
      const phase = VMAF_PHASE[p.phase] || "Analyse";
      const step = p.steps ? `${p.step || 0}/${p.steps}` : "—";
      const fps = p.fps ? `${p.fps} fps` : "—";
      let vmafEta = "—";
      const stepN = parseInt(p.step, 10) || 0, stepsN = parseInt(p.steps, 10) || 0;
      if (stepsN > 0 && stepN > 0 && job.started_at) {
        const elapsed = Math.max(0, (Date.now() / 1000) - job.started_at);
        const per = elapsed / stepN;
        vmafEta = formatDuration(per * Math.max(0, stepsN - stepN));
      } else if (p.eta_human) {
        vmafEta = p.eta_human;
      }
      stats = `
        <div class="stat-grid">
          <div class="stat"><span class="stat-label">Phase</span><span class="stat-val">${escapeHtml(phase)}</span></div>
          <div class="stat"><span class="stat-label">Testpunkt</span><span class="stat-val">${step}</span></div>
          <div class="stat"><span class="stat-label">Encode-Speed</span><span class="stat-val">${fps}</span></div>
          <div class="stat"><span class="stat-label">ETA</span><span class="stat-val">${escapeHtml(vmafEta)}</span></div>
        </div>`;
    } else {
      stats = `
        <div class="stat-grid">
          <div class="stat"><span class="stat-label">Geschwindigkeit</span><span class="stat-val">${p.fps || 0} fps</span></div>
          <div class="stat"><span class="stat-label">Bitrate</span><span class="stat-val">${p.bitrate || "—"}</span></div>
          <div class="stat"><span class="stat-label">ETA</span><span class="stat-val">${p.eta_human || "—"}</span></div>
          <div class="stat"><span class="stat-label">Aktuelle Größe</span><span class="stat-val">${p.current_human || "—"}</span></div>
          <div class="stat"><span class="stat-label">Eingespart</span><span class="stat-val good">${p.saved_human || "—"}</span></div>
          <div class="stat"><span class="stat-label">Speed</span><span class="stat-val">${p.speed || "—"}</span></div>
        </div>`;
    }
    return `
      <div class="job-progress ${analyzing ? "analyzing" : ""}">
        <div class="job-progress-head">
          <span class="job-progress-title">${escapeHtml(job.title)}</span>
          <span class="job-progress-stage">${escapeHtml(stage)}</span>
        </div>
        <div class="big-progress">
          <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
          <div class="bar-pct">${Math.round(pct)}%</div>
        </div>
        ${stats}
      </div>`;
  }

  /* ----------------------------------------------------------- VMAF CHART */
  function renderVmaf(items, activeId) {
    if (state.viewSession) return; // Archiv-Ansicht nicht überschreiben
    let target = items.find((i) => i.id === activeId && i.vmaf);
    if (!target) target = [...items].reverse().find((i) => i.vmaf && i.vmaf.results && i.vmaf.results.length);
    const awaiting = items.find((i) => i.status === "auswahl" && i.vmaf);
    if (awaiting) target = awaiting;

    const card = $("vmaf-card");
    const actions = $("vmaf-actions");
    if (!target || !target.vmaf || !target.vmaf.results.length) {
      if (actions) actions.style.display = "none";
      // Gibt es archivierte Vergleiche, Karte + Dropdown sichtbar lassen, damit
      // ältere Analysen auch ohne aktuelle Analyse abrufbar sind.
      if (state.hasArchive) {
        showCard(card, true);
        if (state.lastVmafKey !== "__placeholder__") {
          showArchivePlaceholder();
          state.lastVmafKey = "__placeholder__";
        }
      } else {
        showCard(card, false);
        state.lastVmafKey = null;
      }
      return;
    }

    const vmaf = target.vmaf;
    // Quelle des Vergleichs merken, damit „→ Encoding" genau diese Datei
    // übernimmt – unabhängig davon, was zwischendurch im Browser angeklickt wurde.
    // Wurde die Quelle beim Start (vtEnqueue) schon exakt erfasst, NICHT mit dem
    // aus dem Absolutpfad abgeleiteten Pfad überschreiben.
    if (!(state.vmafSource && state.vmafSource.name === target.title && state.vmafSource.path)) {
      state.vmafSource = {
        path: inputRelPath(target.path), name: target.title,
        isBatch: false, info: target.info || null,
      };
    } else if (!state.vmafSource.info) {
      state.vmafSource.info = target.info || null;
    }
    const key = target.id + ":" + vmaf.results.length + ":" + vmaf.recommended_quality + ":" + target.status;
    showCard(card, true);
    $("vmaf-model-badge").textContent = `Modell: ${vmaf.model} · Clip: ${vmaf.clip_seconds || 30}s`;

    // Nur neu rendern, wenn sich wirklich etwas geändert hat – sonst flackert
    // der Graph bei jedem Queue-Poll (alle paar Sekunden).
    if (key === state.lastVmafKey) return;

    drawChart(vmaf);
    fillVmafTable(vmaf);
    state.shotScene = null; // bei neuer Analyse mit erster Szene starten
    renderScreenshots(vmaf);

    const showPick = target.status === "auswahl";
    if (actions) {
      actions.style.display = showPick ? "" : "none";
      if (showPick) {
        state.awaitingItemId = target.id;
        const btns = $("vmaf-pick-btns");
        btns.innerHTML = vmaf.results.map((r, idx) =>
          `<button class="btn btn-primary btn-sm btn-pick" data-idx="${idx}">
            ${escapeHtml(r.label || ("Q" + r.quality))} · VMAF ${r.vmaf.toFixed(1)}
          </button>`).join("");
        btns.querySelectorAll(".btn-pick").forEach((b) => {
          b.addEventListener("click", () => approveEncode(target.id, parseInt(b.dataset.idx, 10)));
        });
      }
    }
    state.lastVmafKey = key;
    refreshVmafHistory(); // neue Analyse ins Archiv-Dropdown aufnehmen
  }

  /* ------------------------------------------------ VMAF-VERLAUF (ARCHIV) */
  async function initVmafHistory() {
    const sel = $("vmaf-history");
    if (sel) {
      sel.addEventListener("change", () => {
        if (sel.value) showArchivedSession(sel.value);
        else showLiveVmaf();
      });
    }
    const back = $("btn-vmaf-live");
    if (back) back.addEventListener("click", showLiveVmaf);
    refreshVmafHistory();
  }

  async function refreshVmafHistory() {
    const sel = $("vmaf-history");
    if (!sel) return;
    try {
      const r = await fetch("/api/vmaf/sessions");
      const data = await r.json();
      const sessions = data.sessions || [];
      const cur = sel.value;
      sel.innerHTML = '<option value="">Aktuelle Analyse</option>' +
        sessions.map((s) => {
          const d = s.created ? new Date(s.created * 1000) : null;
          const when = d ? `${d.toLocaleDateString()} ${d.toLocaleTimeString().slice(0,5)}` : "";
          const codec = s.multi_codec ? " · Multi-Codec" : "";
          return `<option value="${escapeHtml(s.session)}">${escapeHtml(s.title)} — ${when}${codec}</option>`;
        }).join("");
      sel.value = cur; // Auswahl beibehalten, falls noch vorhanden
      // Karte auch ohne Live-Analyse zeigen, wenn es Archive gibt.
      const had = state.hasArchive;
      state.hasArchive = sessions.length > 0;
      if (state.hasArchive) showCard($("vmaf-card"), true);
      // Beim ersten Erkennen von Archiven ohne Live-Analyse Platzhalter zeigen.
      if (state.hasArchive && !had && !state.viewSession) {
        renderVmaf(state.lastItems || [], state.lastActiveId);
      }
    } catch (e) { /* still leise */ }
  }

  // Karte ohne aktuelle Analyse: Chart/Tabelle/Screenshots leeren und Hinweis,
  // dass oben im Dropdown ein früherer Vergleich gewählt werden kann.
  function showArchivePlaceholder() {
    if (state.vmafChart) { state.vmafChart.destroy(); state.vmafChart = null; }
    const tb = $("vmaf-table") && $("vmaf-table").querySelector("tbody");
    if (tb) tb.innerHTML = "";
    const sc = $("vmaf-screenshots"); if (sc) sc.innerHTML = "";
    const note = $("vmaf-archive-note"); if (note) note.style.display = "none";
    const badge = $("vmaf-model-badge");
    if (badge) badge.textContent = "Kein aktueller Vergleich – oben einen früheren auswählen";
  }

  async function showArchivedSession(name) {
    try {
      const r = await fetch(`/api/vmaf/session/${encodeURIComponent(name)}`);
      if (!r.ok) return;
      const data = await r.json();
      const vmaf = data.analysis;
      if (!vmaf || !vmaf.results) return;
      state.viewSession = name;
      // Quelle des archivierten Vergleichs übernehmen, damit „→ Encoding" auch
      // nach einem Neustart/Rebuild direkt diese Datei encodiert.
      const srcPath = data.source_path || "";
      const srcName = srcPath ? srcPath.replace(/^.*[\\/]/, "") : "";
      state.vmafSource = srcPath
        ? { path: srcPath, name: srcName, isBatch: false, info: null,
            available: data.source_available !== false }
        : null;
      const note = $("vmaf-archive-note");
      if (note) note.style.display = "";
      const srcInfo = $("vmaf-archive-src");
      if (srcInfo) {
        if (!srcPath) {
          srcInfo.textContent = "";
        } else if (data.source_available === false) {
          srcInfo.innerHTML = ` · Quelle nicht mehr verfügbar `
            + `(<span class="bad">${escapeHtml(srcName)}</span>) – „→ Encoding" `
            + `nicht möglich.`;
        } else {
          srcInfo.innerHTML = ` · Quelle: <span class="good">`
            + `${escapeHtml(srcName)}</span> – „→ Encoding" verfügbar.`;
        }
      }
      const actions = $("vmaf-actions");
      if (actions) actions.style.display = "none";
      showCard($("vmaf-card"), true);
      $("vmaf-model-badge").textContent =
        `Modell: ${vmaf.model} · Clip: ${vmaf.clip_seconds || 30}s`;
      drawChart(vmaf);
      fillVmafTable(vmaf);
      state.shotScene = null;
      renderScreenshots(vmaf);
    } catch (e) { /* ignorieren */ }
  }

  function showLiveVmaf() {
    state.viewSession = null;
    state.lastVmafKey = null; // Neuzeichnen der Live-Ansicht erzwingen
    const note = $("vmaf-archive-note");
    if (note) note.style.display = "none";
    const sel = $("vmaf-history");
    if (sel) sel.value = "";
    renderVmaf(state.lastItems || [], state.lastActiveId);
  }

  // Ergebnisse auf eine einheitliche Szenen-Screenshotliste normalisieren.
  // Ältere Sessions kennen nur screenshot_ref/enc (= Szene 0).
  function shotsOf(r) {
    if (Array.isArray(r.screenshots) && r.screenshots.length) return r.screenshots;
    if (r.screenshot_ref || r.screenshot_enc)
      return [{ scene: 0, ref: r.screenshot_ref, enc: r.screenshot_enc }];
    return [];
  }

  function renderScreenshots(vmaf) {
    const grid = $("vmaf-screenshots");
    if (!grid) return;
    const results = (vmaf.results || [])
      .map((r) => ({ r, shots: shotsOf(r) }))
      .filter((x) => x.shots.length);
    if (!results.length) { grid.innerHTML = ""; return; }

    // Verfügbare Szenen (Vereinigung) und aktuell gewählte Szene.
    const scenes = [...new Set(
      results.flatMap((x) => x.shots.map((s) => s.scene))
    )].sort((a, b) => a - b);
    if (state.shotScene == null || !scenes.includes(state.shotScene))
      state.shotScene = scenes[0];
    const sc = state.shotScene;

    // Kacheln der aktuellen Szene: eine Referenz + je Qualität ein Encode.
    let refSrc = "";
    results.forEach((x) => {
      const s = x.shots.find((s) => s.scene === sc);
      if (s && s.ref && !refSrc) refSrc = s.ref;
    });

    const tiles = [];
    if (refSrc)
      tiles.push({ src: refSrc, label: "Original", sub: `Szene ${sc + 1}`, ref: true });
    results.forEach((x) => {
      const s = x.shots.find((s) => s.scene === sc);
      if (s && s.enc) {
        // VMAF dieser konkreten Szene (nicht der Mittelwert), falls vorhanden.
        const sceneScore = (x.r.scene_scores || []).find((v) => v.scene === sc);
        const v = sceneScore ? sceneScore.vmaf : x.r.vmaf;
        tiles.push({
          src: s.enc,
          label: x.r.label || ("Q" + x.r.quality),
          sub: `VMAF ${v.toFixed(1)}`,
          recommended: x.r.recommended,
        });
      }
    });
    if (!tiles.length) { grid.innerHTML = ""; return; }

    const sceneTabs = scenes.length > 1
      ? `<div class="shot-scenes">${scenes.map((n) =>
          `<button class="shot-scene ${n === sc ? "active" : ""}" data-scene="${n}">Szene ${n + 1}</button>`
        ).join("")}</div>`
      : "";

    grid.innerHTML = `
      <div class="shot-toolbar">
        ${sceneTabs}
        <span class="shot-hint">Bilder ankreuzen und vergleichen – oder anklicken zum Vergrößern.</span>
        <button class="btn small" id="shot-compare" disabled>Auswahl vergleichen</button>
      </div>
      <div class="shot-gallery">
        ${tiles.map((t) => {
          const cap = `${t.label} · ${t.sub}`;
          return `
          <div class="shot-tile ${t.recommended ? "recommended" : ""} ${t.ref ? "is-ref" : ""}"
               data-src="${t.src}" data-cap="${escapeHtml(cap)}">
            <label class="shot-check" title="Für Vergleich auswählen">
              <input type="checkbox" ${t.ref ? "checked" : ""} />
            </label>
            <span class="shot-badge">${escapeHtml(t.label)}<small>${escapeHtml(t.sub)}</small></span>
            <img src="${t.src}" alt="${escapeHtml(t.label)}" loading="lazy" />
          </div>`;
        }).join("")}
      </div>`;

    grid.querySelectorAll(".shot-scene").forEach((b) =>
      b.addEventListener("click", () => {
        state.shotScene = +b.dataset.scene;
        renderScreenshots(vmaf);
      }));

    const cmpBtn = $("shot-compare");
    const selected = () => [...grid.querySelectorAll(".shot-tile")]
      .filter((t) => t.querySelector(".shot-check input").checked)
      .map((t) => ({ src: t.dataset.src, label: t.dataset.cap }));
    const updateCmp = () => {
      const n = selected().length;
      cmpBtn.disabled = n < 1;
      cmpBtn.textContent = n > 0 ? `Auswahl vergleichen (${n})` : "Auswahl vergleichen";
    };
    grid.querySelectorAll(".shot-check input").forEach((c) => {
      c.addEventListener("click", (e) => e.stopPropagation());
      c.addEventListener("change", updateCmp);
    });
    cmpBtn.addEventListener("click", () => {
      const items = selected();
      if (items.length) openGallery(items);
    });
    grid.querySelectorAll(".shot-tile img").forEach((img) =>
      img.addEventListener("click", () => {
        const tile = img.closest(".shot-tile");
        openGallery([{ src: tile.dataset.src, label: tile.dataset.cap }]);
      }));
    updateCmp();
  }

  // Öffnet beliebig viele Bilder nebeneinander (Referenz + gewählte Qualitäten).
  function openGallery(items) {
    let box = $("lightbox");
    if (!box) {
      box = document.createElement("div");
      box.id = "lightbox";
      box.className = "lightbox";
      box.innerHTML =
        '<div class="lightbox-grid"></div>' +
        '<div class="lightbox-hint">Klick oder Esc zum Schließen</div>';
      document.body.appendChild(box);
      box.addEventListener("click", closeLightbox);
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeLightbox();
      });
    }
    const gal = box.querySelector(".lightbox-grid");
    gal.dataset.count = Math.min(items.length, 6);
    gal.innerHTML = items.map((it) =>
      `<figure><figcaption>${escapeHtml(it.label || "")}</figcaption>` +
      `<img src="${it.src}" alt="" /></figure>`
    ).join("");
    box.style.display = "flex";
    requestAnimationFrame(() => box.classList.add("open"));
  }

  function closeLightbox() {
    const box = $("lightbox");
    if (!box) return;
    box.classList.remove("open");
    setTimeout(() => { box.style.display = "none"; }, 150);
  }

  function vmafCell(r) {
    let s = `${r.vmaf.toFixed(2)}`;
    // Mehrere Szenen: Mittelwert oben, Streuung (min–max) je Szene darunter.
    if (r.vmaf_min != null && r.vmaf_max != null) {
      const perScene = (r.scene_scores || [])
        .map((sc) => `Szene ${sc.scene + 1}: ${sc.vmaf.toFixed(1)}`).join("\n");
      s += `<br><span class="muted" title="${escapeHtml(perScene)}">`
        + `Ø · Szenen ${r.vmaf_min.toFixed(1)}–${r.vmaf_max.toFixed(1)}</span>`;
    }
    // Zusatzmetriken (falls gemessen): 1%-Low + harmon. Mittel, PSNR/SSIM.
    const extra = [];
    if (r.vmaf_1pct != null) extra.push(`1%-Low ${r.vmaf_1pct.toFixed(1)}`);
    if (r.vmaf_hmean != null) extra.push(`H-Ø ${r.vmaf_hmean.toFixed(1)}`);
    if (extra.length) {
      s += `<br><span class="muted" title="1%-Low = Mittel der schlechtesten 1 % Frames; `
        + `H-Ø = harmonisches Mittel">${extra.join(" · ")}</span>`;
    }
    const qual = [];
    if (r.psnr != null) qual.push(`PSNR ${r.psnr.toFixed(1)} dB`);
    if (r.ssim != null) qual.push(`SSIM ${r.ssim.toFixed(3)}`);
    if (qual.length) {
      s += `<br><span class="muted">${qual.join(" · ")}</span>`;
    }
    return s;
  }

  function fillVmafTable(vmaf) {
    const body = $("vmaf-table").querySelector("tbody");
    body.innerHTML = vmaf.results.map((r, idx) => `
      <tr class="${r.recommended ? "row-recommended" : ""}">
        <td>${escapeHtml(r.label || ("Q" + r.quality))}</td>
        <td>${vmafCell(r)}</td>
        <td>${r.predicted_human}</td>
        <td class="${r.savings_percent >= 0 ? "good" : "bad"}">${r.savings_percent}%</td>
        <td class="vmaf-row-actions">
          ${r.recommended ? '<span class="badge recommended">Empfohlen</span>' : ""}
          <button class="btn btn-ghost btn-sm" data-take="${idx}" title="Diese Einstellung ins Encoding übernehmen">→ Encoding</button>
        </td>
      </tr>`).join("");
    body.querySelectorAll("[data-take]").forEach((b) =>
      b.addEventListener("click", () =>
        transferToEncode(vmaf.results[parseInt(b.dataset.take, 10)])));
  }

  function chartColors() {
    return {
      accent: cssVar("--accent"),
      accent2: cssVar("--accent-2"),
      good: cssVar("--good"),
      text: cssVar("--text"),
      muted: cssVar("--text-muted"),
      grid: cssVar("--border"),
    };
  }

  const CHART_PALETTE = ["#4f9dff", "#22c55e", "#f59e0b", "#e879f9", "#f43f5e", "#14b8a6"];

  function drawChart(vmaf) {
    if (typeof Chart === "undefined") return;
    if (vmaf.multi_codec) return drawChartMultiCodec(vmaf);

    const ctx = $("vmaf-chart");
    const col = chartColors();
    const labels = vmaf.results.map((r) => r.label || ("Q" + r.quality));
    const scores = vmaf.results.map((r) => r.vmaf);
    const savings = vmaf.results.map((r) => r.savings_percent);
    const pointColors = vmaf.results.map((r) => (r.recommended ? col.good : col.accent));
    const pointRadius = vmaf.results.map((r) => (r.recommended ? 8 : 4));

    if (state.vmafChart) state.vmafChart.destroy();
    state.vmafChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "VMAF-Score", data: scores, yAxisID: "y",
            borderColor: col.accent, backgroundColor: "transparent",
            pointBackgroundColor: pointColors, pointRadius, pointHoverRadius: 9,
            tension: 0.3, borderWidth: 2.5,
          },
          {
            label: "Ersparnis %", data: savings, yAxisID: "y1",
            borderColor: col.accent2, backgroundColor: "transparent",
            borderDash: [5, 4], pointRadius: 3, tension: 0.3, borderWidth: 1.8,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: col.text, font: { size: 12 } } },
          annotation: {},
          tooltip: { callbacks: {
            afterBody: (ctxs) => {
              const i = ctxs[0].dataIndex;
              return vmaf.results[i].recommended ? "★ Empfohlener Sweet Spot" : "";
            },
          }},
        },
        scales: {
          x: { grid: { color: col.grid }, ticks: { color: col.muted } },
          y: {
            position: "left", title: { display: true, text: "VMAF", color: col.muted },
            suggestedMin: 80, suggestedMax: 100,
            grid: { color: col.grid }, ticks: { color: col.muted },
          },
          y1: {
            position: "right", title: { display: true, text: "Ersparnis %", color: col.muted },
            grid: { drawOnChartArea: false }, ticks: { color: col.muted },
          },
        },
      },
    });
  }

  // Mehrere Codecs: faire Achse = VMAF (y) vs. Ersparnis % (x). Je Codec eine
  // Kurve; weiter oben-rechts = besser (mehr Qualität bei mehr Ersparnis).
  function drawChartMultiCodec(vmaf) {
    const ctx = $("vmaf-chart");
    const col = chartColors();
    const groups = {};
    vmaf.results.forEach((r) => {
      const key = r.codec_disp || r.codec;
      (groups[key] = groups[key] || []).push(r);
    });

    const datasets = Object.keys(groups).map((name, gi) => {
      const color = CHART_PALETTE[gi % CHART_PALETTE.length];
      const pts = groups[name].slice().sort((a, b) => a.savings_percent - b.savings_percent);
      return {
        label: name,
        data: pts.map((r) => ({ x: r.savings_percent, y: r.vmaf, _r: r })),
        borderColor: color, backgroundColor: "transparent",
        pointBackgroundColor: pts.map((r) => (r.recommended ? col.good : color)),
        pointRadius: pts.map((r) => (r.recommended ? 8 : 4)),
        pointHoverRadius: 9, tension: 0.25, borderWidth: 2.4, showLine: true,
      };
    });

    if (state.vmafChart) state.vmafChart.destroy();
    state.vmafChart = new Chart(ctx, {
      type: "scatter",
      data: { datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: col.text, font: { size: 12 } } },
          tooltip: { callbacks: {
            label: (c) => {
              const r = c.raw._r;
              return `${c.dataset.label} ${r.label.split("·").pop().trim()}: `
                + `VMAF ${r.vmaf.toFixed(1)} · ${r.predicted_human} (${r.savings_percent}%)`
                + (r.recommended ? "  ★" : "");
            },
          }},
        },
        scales: {
          x: {
            title: { display: true, text: "Ersparnis %", color: col.muted },
            grid: { color: col.grid }, ticks: { color: col.muted },
          },
          y: {
            title: { display: true, text: "VMAF", color: col.muted },
            suggestedMin: 80, suggestedMax: 100,
            grid: { color: col.grid }, ticks: { color: col.muted },
          },
        },
      },
    });
  }

  function restyleChart() {
    state.lastVmafKey = null; // erzwingt Neuzeichnen mit neuen Theme-Farben
  }

  /* ---------------------------------------------------------- DATA BROWSER */
  const dataState = { root: "vmaf", path: "" };

  function initDataBrowser() {
    $("data-root").addEventListener("change", (e) => {
      dataState.root = e.target.value;
      dataState.path = "";
      loadDataDir();
    });
    $("btn-data-refresh").addEventListener("click", () => {
      loadDataDir();
      refreshStorageBadge();
    });
    $("btn-data-delete-all").addEventListener("click", deleteAllInDataRoot);
    $("btn-data-preview-close").addEventListener("click", () => {
      $("data-preview").style.display = "none";
    });
    loadDataDir();
    refreshStorageBadge();
  }

  async function refreshStorageBadge() {
    try {
      const p = await fetch("/api/config/paths").then((r) => r.json());
      const s = p.storage || {};
      const parts = ["vmaf", "previews", "work"].map((k) =>
        s[k] ? `${k}: ${s[k].size_human}` : "").filter(Boolean);
      $("data-storage-badge").textContent = parts.join(" · ") || "—";
    } catch (_) { /* ignore */ }
  }

  async function loadDataDir() {
    const browser = $("data-browser");
    browser.innerHTML = '<div class="browser-loading">Lade …</div>';
    try {
      const res = await fetch(
        `/api/data/browse?root=${encodeURIComponent(dataState.root)}&path=${encodeURIComponent(dataState.path)}`
      );
      const data = await res.json();
      if (data.error) {
        browser.innerHTML = `<div class="browser-loading">${escapeHtml(data.error)}</div>`;
        return;
      }
      renderDataBreadcrumb(data);
      renderDataBrowser(data);
    } catch (e) {
      browser.innerHTML = `<div class="browser-loading">Fehler: ${escapeHtml(String(e))}</div>`;
    }
  }

  function renderDataBreadcrumb(data) {
    const bc = $("data-breadcrumb");
    bc.innerHTML = "";
    const rootLink = document.createElement("a");
    rootLink.textContent = data.root_label;
    rootLink.onclick = () => { dataState.path = ""; loadDataDir(); };
    bc.appendChild(rootLink);
    if (data.path) {
      const parts = data.path.split("/");
      let acc = "";
      parts.forEach((p) => {
        acc = acc ? `${acc}/${p}` : p;
        const sep = document.createElement("span");
        sep.textContent = " / ";
        bc.appendChild(sep);
        const a = document.createElement("a");
        a.textContent = p;
        const target = acc;
        a.onclick = () => { dataState.path = target; loadDataDir(); };
        bc.appendChild(a);
      });
    }
    if (!data.is_root) {
      const info = document.createElement("span");
      info.textContent = ` · ${data.total_human}`;
      info.style.color = "var(--text-muted)";
      bc.appendChild(info);
    }
  }

  function renderDataBrowser(data) {
    const browser = $("data-browser");
    browser.innerHTML = "";
    if (!data.is_root) {
      browser.appendChild(dataRow({
        is_dir: true, name: "..", rel: data.parent || "", size_human: "",
      }, data, true));
    }
    data.dirs.forEach((d) => browser.appendChild(dataRow(d, data, true)));
    data.files.forEach((f) => browser.appendChild(dataRow(f, data, false)));
    if (!data.dirs.length && !data.files.length && data.is_root) {
      browser.innerHTML = '<div class="browser-loading">Ordner ist leer.</div>';
    }
  }

  function dataRow(item, data, isDir) {
    const row = document.createElement("div");
    row.className = "row-item";
    const icon = isDir ? "📁" : fileIcon(item);
    row.innerHTML = `
      <span class="row-icon">${icon}</span>
      <span class="row-name">${escapeHtml(item.name)}</span>
      <span class="row-size">${item.size_human || ""}</span>`;

    if (isDir && item.name !== "..") {
      row.addEventListener("click", () => {
        dataState.path = item.rel;
        loadDataDir();
      });
    } else if (!isDir) {
      row.addEventListener("click", () => openDataPreview(item, data.root));
      const open = document.createElement("span");
      open.className = "row-open";
      open.textContent = "Öffnen";
      row.appendChild(open);
    } else if (item.name === "..") {
      row.addEventListener("click", () => {
        dataState.path = data.parent || "";
        loadDataDir();
      });
    }

    if (item.name !== "..") {
      const del = document.createElement("button");
      del.className = "row-del";
      del.textContent = "Löschen";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteDataItem(data.root, item.rel, item.name);
      });
      row.appendChild(del);
    }
    return row;
  }

  function fileIcon(item) {
    if (item.preview_url) return "🖼️";
    if (item.kind === "json") return "📄";
    if (item.kind === "video") return "🎬";
    return "📎";
  }

  async function openDataPreview(item, root) {
    const panel = $("data-preview");
    const body = $("data-preview-body");
    $("data-preview-title").textContent = item.name;
    panel.style.display = "";

    if (item.preview_url) {
      body.innerHTML = `<img src="${item.preview_url}" alt="${escapeHtml(item.name)}" />`;
      return;
    }
    if (item.kind === "json") {
      const url = `/api/data/file?root=${encodeURIComponent(root)}&path=${encodeURIComponent(item.rel)}`;
      try {
        const text = await fetch(url).then((r) => r.text());
        body.innerHTML = `<pre>${escapeHtml(text.slice(0, 50000))}</pre>`;
      } catch (e) {
        body.innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
      }
      return;
    }
    if (item.kind === "video") {
      const url = `/api/data/file?root=${encodeURIComponent(root)}&path=${encodeURIComponent(item.rel)}`;
      body.innerHTML = `<video controls style="max-width:100%"><source src="${url}" /></video>
        <p class="hint muted">Test-Encode-Vorschau (nur Ausschnitt).</p>`;
      return;
    }
    body.innerHTML = `<p class="muted">Keine Vorschau für diesen Dateityp. Pfad: <code>${escapeHtml(item.rel)}</code></p>`;
  }

  async function deleteDataItem(root, rel, name) {
    if (!confirm(`„${name}" wirklich löschen?`)) return;
    const res = await fetch("/api/data/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, path: rel }),
    });
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    $("data-preview").style.display = "none";
    loadDataDir();
    refreshStorageBadge();
  }

  async function deleteAllInDataRoot() {
    const root = dataState.root;
    const label = $("data-root").selectedOptions[0].text;
    if (!confirm(`Gesamten Bereich „${label}" leeren? Alle Dateien werden unwiderruflich gelöscht.`)) return;
    const res = await fetch(`/api/data/delete-all?root=${encodeURIComponent(root)}`, { method: "POST" });
    const data = await res.json();
    if (data.error) alert(data.error);
    dataState.path = "";
    $("data-preview").style.display = "none";
    loadDataDir();
    refreshStorageBadge();
  }

  /* -------------------------------------------------- MODAL / PLAYER / INFO */
  function ensureModal() {
    let m = $("app-modal");
    if (m) return m;
    m = document.createElement("div");
    m.id = "app-modal";
    m.className = "app-modal";
    m.style.display = "none";
    m.innerHTML = `
      <div class="app-modal-backdrop"></div>
      <div class="app-modal-box">
        <div class="app-modal-head">
          <span id="app-modal-title" class="app-modal-title"></span>
          <button id="app-modal-close" class="btn btn-ghost btn-sm">Schließen</button>
        </div>
        <div id="app-modal-body" class="app-modal-body"></div>
      </div>`;
    document.body.appendChild(m);
    const close = () => closeModal();
    m.querySelector(".app-modal-backdrop").addEventListener("click", close);
    $("app-modal-close").addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && m.style.display !== "none") close();
    });
    return m;
  }

  function openModal(title, html) {
    const m = ensureModal();
    $("app-modal-title").textContent = title || "";
    $("app-modal-body").innerHTML = html || "";
    m.style.display = "";
  }

  function closeModal() {
    const m = $("app-modal");
    if (!m) return;
    // Laufende Videos stoppen, damit im Hintergrund kein Ton weiterläuft.
    m.querySelectorAll("video").forEach((v) => { try { v.pause(); } catch (e) {} });
    m.style.display = "none";
    $("app-modal-body").innerHTML = "";
  }

  function videoHtml(mediaUrl, trackUrl) {
    const track = trackUrl
      ? `<track kind="subtitles" src="${trackUrl}" srclang="und" label="Subs" default>`
      : "";
    return `<video class="modal-video" controls preload="metadata" src="${mediaUrl}">${track}</video>`;
  }

  async function openPlayer(root, rel, name) {
    openModal(name || "Wiedergabe", `<p class="muted">${tt("Lade Spuren …")}</p>`);
    let info = null;
    try {
      info = await (await fetch(`/api/probe?path=${encodeURIComponent(rel)}`)).json();
      if (info.error) info = null;
    } catch (e) { info = null; }
    const audio = (info && info.audio) || [];
    const subs = (info && info.subtitles) || [];
    const textSubs = subs.filter((s) => {
      const c = (s.codec || "").toLowerCase();
      return c && !/pgs|dvd_sub|dvb_sub|xsub|hdmv/.test(c);
    });
    const audioOpts = audio.length
      ? audio.map((a, i) => {
          const lab = [a.language || "und", a.codec || "", a.channels ? `${a.channels}ch` : "", a.title || ""]
            .filter(Boolean).join(" · ");
          return `<option value="${i}">${i}: ${escapeHtml(lab)}</option>`;
        }).join("")
      : `<option value="-1">${tt("Kein Ton")}</option>`;
    const subOpts = `<option value="-1">${tt("Keine Untertitel")}</option>` +
      textSubs.map((s) => {
        // Relativer Untertitel-Index für ffmpeg 0:s:N (Position in info.subtitles).
        const idx = subs.indexOf(s);
        const lab = [s.language || "und", s.codec || "", s.title || ""].filter(Boolean).join(" · ");
        return `<option value="${idx}">${escapeHtml(lab)}</option>`;
      }).join("");
    const imgNote = subs.length > textSubs.length
      ? `<p class="hint warn">${tt("Bild-Untertitel (PGS o. Ä.) können im Browser nicht eingeblendet werden.")}</p>`
      : "";
    openModal(name || "Wiedergabe", `
      <div class="player-controls field-row" style="margin-bottom:10px;align-items:flex-end">
        <div class="field" style="flex:1">
          <label>${tt("Tonspur")}</label>
          <select id="player-audio">${audioOpts}</select>
        </div>
        <div class="field" style="flex:1">
          <label>${tt("Untertitel")}</label>
          <select id="player-sub">${subOpts}</select>
        </div>
        <button type="button" class="btn btn-ghost btn-sm" id="player-reload">${tt("Neu laden")}</button>
      </div>
      <div id="player-wrap">${videoHtml(_playerStreamUrl(root, rel, audio.length ? 0 : -1), "")}</div>
      <p class="muted" style="margin-top:8px;font-size:12px">
        ${tt("Ton wird für die Wiedergabe als AAC umgewandelt. Der Video-Codec hängt vom Browser ab (AV1/HEVC).")}
      </p>
      ${imgNote}`);
    const reload = () => {
      const a = parseInt(($("player-audio") || {}).value, 10);
      const s = parseInt(($("player-sub") || {}).value, 10);
      const vUrl = _playerStreamUrl(root, rel, isNaN(a) ? 0 : a);
      const tUrl = (!isNaN(s) && s >= 0)
        ? `/api/media/vtt?root=${encodeURIComponent(root)}&path=${encodeURIComponent(rel)}&subtitle=${s}`
        : "";
      const wrap = $("player-wrap");
      if (wrap) wrap.innerHTML = videoHtml(vUrl, tUrl);
    };
    const btn = $("player-reload");
    if (btn) btn.addEventListener("click", reload);
    ["player-audio", "player-sub"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", reload);
    });
  }

  function _playerStreamUrl(root, rel, audioIdx) {
    return `/api/media/stream?root=${encodeURIComponent(root || "media")}`
      + `&path=${encodeURIComponent(rel)}&audio=${audioIdx}`;
  }

  // Direkt in den A/B-Vergleich springen und beide Videos laden.
  function openAbCompare(rootA, pathA, rootB, pathB) {
    closeModal();
    navTo("abcompare");
    const set = (id, val) => { const el = $(id); if (el && val != null) el.value = val; };
    set("ab-root-a", rootA); set("ab-path-a", pathA);
    set("ab-root-b", rootB); set("ab-path-b", pathB);
    const load = $("btn-ab-load");
    if (load) load.click();
  }

  // Kompakte ffprobe-Übersicht (Video-/Audio-/Untertitelspuren) als HTML.
  function infoTableHtml(info) {
    if (!info) return `<p class="muted">Keine Analyse verfügbar.</p>`;
    const rows = [];
    rows.push(`<tr><th>Container</th><td>${escapeHtml(info.container || "—")} · ${escapeHtml(info.resolution || "—")} · ${info.duration ? Math.round(info.duration) + "s" : "—"}</td></tr>`);
    const v = `${info.codec || "—"}${info.is_hdr ? " · HDR" : ""}${info.dolby_vision ? " · DV" + (info.dv_profile ? " " + info.dv_profile : "") : ""}`;
    const vbr = (info.video_bitrate_human && info.video_bitrate_human !== "—")
      ? ` · ${info.video_bitrate_human}`
      : (info.overall_bitrate_human && info.overall_bitrate_human !== "—" ? ` · ${info.overall_bitrate_human} gesamt` : "");
    rows.push(`<tr><th>Video</th><td>${escapeHtml(v + vbr)}</td></tr>`);
    (info.audio || []).forEach((a, i) => {
      const parts = [a.codec, a.language, a.channels ? a.channels + " ch" : null,
        (a.bitrate_human && a.bitrate_human !== "—") ? a.bitrate_human : null].filter(Boolean);
      rows.push(`<tr><th>Audio ${i + 1}</th><td>${escapeHtml(parts.join(" · ") || "—")}</td></tr>`);
    });
    (info.subtitles || []).forEach((s, i) => {
      const parts = [s.codec, s.language].filter(Boolean);
      rows.push(`<tr><th>Sub ${i + 1}</th><td>${escapeHtml(parts.join(" · ") || "—")}</td></tr>`);
    });
    return `<table class="info-table">${rows.join("")}</table>`;
  }

  async function openQueueDetails(id) {
    if (!id) return;
    openModal("Details", `<p class="muted">Lade …</p>`);
    let d;
    try {
      const r = await fetch(`/api/queue/${id}/details`);
      d = await r.json();
    } catch (e) {
      openModal("Details", `<p class="bad">Fehler: ${escapeHtml(String(e))}</p>`);
      return;
    }
    if (d.error) { openModal("Details", `<p class="bad">${escapeHtml(d.error)}</p>`); return; }

    const s = d.stats || {};
    const statChips = [
      ["Status", d.status || "—"],
      ["Dauer", s.duration_human || "—"],
      ["Ø Speed", s.speed_x != null ? s.speed_x + "×" : "—"],
      ["Ø FPS", s.avg_fps != null ? s.avg_fps : "—"],
      ["Original", s.original_human || "—"],
      ["Ausgabe", s.output_human || "—"],
      ["Eingespart", (s.saved_human || "—") + (s.savings_percent != null ? ` (${s.savings_percent}%)` : "")],
      ["VMAF", s.vmaf_verify != null ? Number(s.vmaf_verify).toFixed(1) : "—"],
    ].map(([k, v]) => `<div class="stat"><span class="stat-label">${k}</span><span class="stat-val">${escapeHtml(String(v))}</span></div>`).join("");

    const player = (d.output && d.output.media)
      ? videoHtml(d.output.media)
      : (d.source && d.source.media ? videoHtml(d.source.media) : `<p class="muted">Keine abspielbare Datei gefunden.</p>`);
    const playToggle = (d.source && d.source.media && d.output && d.output.media)
      ? `<div class="modal-tabs">
           <button class="btn btn-ghost btn-sm active" data-src="${escapeHtml(d.output.media)}">Ausgabe</button>
           <button class="btn btn-ghost btn-sm" data-src="${escapeHtml(d.source.media)}">Quelle</button>
         </div>` : "";

    // A/B-Direktvergleich (alt vs. neu), sobald beide Dateien vorhanden sind.
    const canAb = d.source && d.source.rel && d.source.exists && d.output && d.output.rel && d.output.exists;
    const abBtn = canAb
      ? `<button class="btn btn-primary btn-sm" id="modal-ab"
           data-a="${escapeHtml(d.source.rel)}" data-b="${escapeHtml(d.output.rel)}">
           🎞 Im A/B-Vergleich öffnen (alt vs. neu)</button>` : "";
    const requeueBtn = `<button class="btn btn-ghost btn-sm" id="modal-requeue">${tt("Erneut")}</button>` +
      `<button class="btn btn-ghost btn-sm" id="modal-requeue-edit">${tt("Erneut mit …")}</button>`;

    const html = `
      <div class="stat-grid modal-stats">${statChips}</div>
      <div class="modal-tabs" style="margin-bottom:8px">${abBtn || ""}${requeueBtn}</div>
      ${playToggle}
      <div id="modal-player">${player}</div>
      <div class="modal-cols">
        <div><h4>Quelle</h4>${infoTableHtml(d.source && d.source.info)}</div>
        <div><h4>Ausgabe</h4>${infoTableHtml(d.output && d.output.info)}</div>
      </div>
      <div id="modal-vmaf-hist" class="hint" style="margin-top:12px">${tt("Lade frühere Läufe …")}</div>`;
    openModal(escapeHtml(d.title || "Details"), html);

    const body = $("app-modal-body");
    body.querySelectorAll(".modal-tabs [data-src]").forEach((b) => {
      b.addEventListener("click", () => {
        body.querySelectorAll(".modal-tabs [data-src]").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        $("modal-player").innerHTML = videoHtml(b.dataset.src);
      });
    });
    const ab = $("modal-ab");
    if (ab) ab.addEventListener("click", () =>
      openAbCompare("media", ab.dataset.a, "media", ab.dataset.b));
    const rq = $("modal-requeue");
    if (rq) rq.addEventListener("click", () => requeueJob(id, !!d.from_history));
    const rqe = $("modal-requeue-edit");
    if (rqe) rqe.addEventListener("click", () => {
      if (d.settings) applyProfile(d.settings);
      closeModal();
      navTo("encode");
    });
    loadVmafBySource(d.path || "", "modal-vmaf-hist");
  }

  async function loadVmafBySource(path, elId) {
    const el = $(elId);
    if (!el) return;
    if (!path) { el.textContent = ""; return; }
    try {
      const d = await (await fetch(`/api/vmaf/by-source?path=${encodeURIComponent(path)}`)).json();
      const jobs = d.jobs || [];
      const sessions = d.sessions || [];
      if (!jobs.length && !sessions.length) {
        el.innerHTML = `<p class="muted">${tt("Keine früheren Läufe für diese Quelle.")}</p>`;
        return;
      }
      const jobRows = jobs.slice(0, 8).map((j) => {
        const when = j.finished ? new Date(j.finished * 1000).toLocaleString() : "—";
        const v = j.vmaf != null ? Number(j.vmaf).toFixed(1) : "—";
        return `<li>${escapeHtml(when)} · ${(j.codec || "").toUpperCase()} Q${j.quality || "—"} · VMAF ${v} · ${escapeHtml(j.status || "")}</li>`;
      }).join("");
      const sessRows = sessions.slice(0, 5).map((s) =>
        `<li>${escapeHtml(s.title || s.session)} · ${escapeHtml(s.recommended_label || "")}` +
        (s.recommended_vmaf != null ? ` (VMAF ${Number(s.recommended_vmaf).toFixed(1)})` : "") + `</li>`).join("");
      el.innerHTML = `<h4>${tt("Frühere Läufe")}</h4>` +
        (jobRows ? `<ul class="hist-list">${jobRows}</ul>` : "") +
        (sessRows ? `<h4 style="margin-top:8px">${tt("VMAF-Sessions")}</h4><ul class="hist-list">${sessRows}</ul>` : "");
    } catch (e) {
      el.innerHTML = `<span class="bad">${escapeHtml(String(e))}</span>`;
    }
  }

  /* ----------------------------------------------------------------- UTIL */
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function formatBytes(n) {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(1)} ${u[i]}`;
  }

  function formatDuration(sec) {
    sec = Math.round(Number(sec) || 0);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}h ${m}m` : (m ? `${m}m ${s}s` : `${s}s`);
  }

  /* -------------------------------------------------------------- PROFILE */
  function initProfiles() {
    const sel = $("opt-profile");
    if (!sel) return;
    refreshProfiles();
    sel.addEventListener("change", () => {
      const p = state.profiles && state.profiles.find((x) => x.name === sel.value);
      if (p) applyProfile(p.settings);
    });
    $("btn-profile-save").addEventListener("click", async () => {
      const name = prompt("Profilname:");
      if (!name) return;
      const r = await fetch("/api/profiles", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name, settings: gatherSettings() }),
      });
      const d = await r.json();
      state.profiles = d.profiles || [];
      renderProfileOptions(name);
    });
    $("btn-profile-delete").addEventListener("click", async () => {
      const name = sel.value;
      if (!name) return;
      const r = await fetch(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
      const d = await r.json();
      state.profiles = d.profiles || [];
      renderProfileOptions("");
    });
    document.querySelectorAll("#opt-preset-chips [data-preset]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const name = btn.dataset.preset;
        const p = (state.profiles || []).find((x) => x.name === name);
        if (!p) {
          refreshProfiles().then(() => {
            const p2 = (state.profiles || []).find((x) => x.name === name);
            if (p2) { applyProfile(p2.settings); renderProfileOptions(name); }
            else alert(tt("Preset nicht gefunden: ") + name);
          });
          return;
        }
        applyProfile(p.settings);
        renderProfileOptions(name);
      });
    });
  }

  async function refreshProfiles() {
    try {
      const r = await fetch("/api/profiles");
      const d = await r.json();
      state.profiles = d.profiles || [];
      renderProfileOptions($("opt-profile").value);
    } catch (e) { /* ignorieren */ }
  }

  function renderProfileOptions(selected) {
    const sel = $("opt-profile");
    if (!sel) return;
    sel.innerHTML = '<option value="">— kein Profil —</option>' +
      (state.profiles || []).map((p) =>
        `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join("");
    if (selected) sel.value = selected;
  }

  function applyProfile(s) {
    if (!s) return;
    const set = (id, val, ev) => {
      const el = $(id);
      if (!el || val === undefined || val === null) return;
      if (el.type === "checkbox") el.checked = !!val; else el.value = val;
      el.dispatchEvent(new Event(ev || (el.tagName === "SELECT" ? "change" : "input")));
    };
    set("opt-platform", s.platform, "change");
    set("opt-codec", s.codec, "change");
    set("opt-rate-mode", s.rate_mode, "change");
    if (s.rate_mode === "cq") set("opt-quality", s.quality);
    else set("opt-bitrate", s.quality);
    set("opt-resolution", s.target_height ? String(s.target_height) : "");
    set("opt-hdr-mode", s.hdr_mode, "change");
    if (s.dv_mode) {
      const dvSel = $("opt-dv-mode");
      if (dvSel) { dvSel.value = s.dv_mode; dvSel.dataset.userset = "1"; }
    }
    set("opt-keep-subs", s.keep_subtitles);
    set("opt-keep-chapters", s.keep_chapters);
    set("opt-keep-metadata", s.keep_metadata);
    set("opt-denoise", s.denoise, "change");
    set("opt-film-grain", s.film_grain);
    set("opt-two-pass", s.two_pass);
    set("opt-anime", s.anime);
    set("opt-autocrop", s.autocrop);
    set("opt-verify-vmaf", s.verify_vmaf, "change");
    set("opt-verify-min", s.verify_min);
    set("opt-verify-retry", s.verify_retry);
    set("opt-container", s.container, "change");
    set("opt-post", s.post_processing, "change");
    if (s.integrity_check !== undefined) set("opt-integrity", s.integrity_check);
    if (s.safe_replace !== undefined) set("opt-safe-replace", s.safe_replace);
    set("opt-audio-mode", s.audio_mode, "change");
    set("opt-audio-codec", s.audio_codec, "change");
    set("opt-audio-bitrate", s.audio_bitrate);
    set("opt-audio-channels", s.audio_channels);
    set("opt-audio-normalize", s.audio_normalize);
    if (s.name_pattern !== undefined) set("opt-name-pattern", s.name_pattern);
    if (s.on_duplicate !== undefined) set("opt-on-duplicate", s.on_duplicate);
    if (s.max_output_mb !== undefined) set("opt-max-output-mb", s.max_output_mb);
    if (s.max_video_bitrate_kbps !== undefined) set("opt-max-bitrate", s.max_video_bitrate_kbps);
    if (s.size_target_mb !== undefined) set("opt-size-target", s.size_target_mb);
    if (s.out_mode !== undefined) set("opt-out-mode", s.out_mode, "change");
    if (s.out_subdir !== undefined) set("opt-out-subdir", s.out_subdir);
    if (s.remux_only) {
      // Remux-Profil → Remux & Bearbeiten (nicht Super-Tool), Auswahl mitnehmen.
      applyRemuxProfile(s);
    }
  }

  function applyRemuxProfile(s) {
    navTo("remux");
    const set = (id, val, ev) => {
      const el = $(id);
      if (!el || val === undefined || val === null) return;
      if (el.type === "checkbox") el.checked = !!val; else el.value = val;
      el.dispatchEvent(new Event(ev || (el.tagName === "SELECT" ? "change" : "input")));
    };
    if (s.suffix !== undefined) set("remux-suffix", s.suffix);
    if (s.name_pattern !== undefined) set("remux-name-pattern", s.name_pattern);
    if (s.on_duplicate !== undefined) set("remux-on-duplicate", s.on_duplicate);
    if (s.container !== undefined) set("remux-container", s.container, "change");
    if (s.post_processing !== undefined) set("remux-post", s.post_processing, "change");
    if (s.integrity_check !== undefined) set("remux-integrity", s.integrity_check);
    if (s.safe_replace !== undefined) set("remux-safe", s.safe_replace);
    if (s.out_mode !== undefined) set("remux-out-mode", s.out_mode, "change");
    if (s.out_subdir !== undefined) set("remux-out-subdir", s.out_subdir);
    // Encode-/Browser-Auswahl in Remux übernehmen (Einzeldatei).
    const sel = state.selected;
    if (sel && !sel.isBatch && sel.path) {
      const already = state.remuxSel && state.remuxSel.path === sel.path;
      if (!already) remuxSelectFile({ rel: sel.path, name: sel.name || sel.path });
    }
  }

  /* ------------------------------------------------------------- STATISTIK */
  function initStats() {
    const btn = $("btn-stats-clear");
    if (btn) btn.addEventListener("click", async () => {
      if (!confirm("Gesamte Job-Historie löschen?")) return;
      await fetch("/api/stats/clear", { method: "POST" });
      loadStats();
    });
  }

  async function loadStats() {
    try {
      const r = await fetch("/api/stats");
      const d = await r.json();
      renderStats(d.stats || {}, d.recent || []);
    } catch (e) { /* ignorieren */ }
  }

  function renderStats(st, recent) {
    const grid = $("stat-grid");
    if (grid) {
      const cards = [
        ["Encodes fertig", st.count_done || 0],
        ["Gesamt eingespart", formatBytes(st.saved_bytes)],
        ["Ersparnis", `${st.saved_percent || 0}%`],
        ["Original → Ergebnis", `${formatBytes(st.original_bytes)} → ${formatBytes(st.output_bytes)}`],
        ["Ø VMAF", st.avg_vmaf != null ? st.avg_vmaf : "—"],
        ["Encode-Zeit gesamt", formatDuration(st.encode_seconds)],
        ["Fehlgeschlagen", st.count_failed || 0],
      ];
      grid.innerHTML = cards.map(([l, v]) =>
        `<div class="stat-box"><span class="stat-val">${escapeHtml(String(v))}</span><span class="stat-lbl">${escapeHtml(l)}</span></div>`).join("");
    }
    const codecs = $("stat-codecs");
    if (codecs) {
      codecs.innerHTML = (st.by_codec || []).map((c) =>
        `<span class="codec-chip">${escapeHtml((c.codec || "?").toUpperCase())}: ${c.count}× · ${formatBytes(c.saved_bytes)}</span>`).join("");
    }
    const body = $("stats-body");
    if (body) {
      body.innerHTML = recent.length ? recent.map((j) => {
        const when = j.finished ? new Date(j.finished * 1000).toLocaleString() : "—";
        const id = escapeHtml(j.id || "");
        return `
        <tr>
          <td><a href="#" class="stats-title" data-id="${id}" title="Details & Wiedergabe öffnen">${escapeHtml(j.title || "")}</a></td>
          <td>${escapeHtml((j.codec || "").toUpperCase())}</td>
          <td>${j.quality || "—"}</td>
          <td>${j.vmaf != null ? Number(j.vmaf).toFixed(1) : "—"}</td>
          <td>${formatBytes(j.original_size)}</td>
          <td>${formatBytes(j.output_size)}</td>
          <td class="${(j.saved_bytes || 0) >= 0 ? "good" : "bad"}">${formatBytes(j.saved_bytes)}</td>
          <td>${formatDuration(j.duration || 0)}</td>
          <td class="muted">${escapeHtml(when)}</td>
          <td>${escapeHtml(j.status || "")}</td>
          <td>
            <button class="btn btn-ghost btn-sm stats-play" data-id="${id}" title="Details & Wiedergabe öffnen">▶</button>
            <button class="btn btn-ghost btn-sm stats-requeue" data-id="${id}" title="Erneut einreihen">Erneut</button>
          </td>
        </tr>`; }).join("") :
        '<tr class="empty-row"><td colspan="11">Noch keine Jobs.</td></tr>';
      body.querySelectorAll(".stats-title, .stats-play").forEach((el) => {
        el.addEventListener("click", (e) => {
          e.preventDefault();
          const id = el.dataset.id;
          if (id) openQueueDetails(id);
        });
      });
      body.querySelectorAll(".stats-requeue").forEach((el) => {
        el.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          requeueJob(el.dataset.id, true);
        });
      });
    }
  }

  /* ------------------------------------------------------------ BIBLIOTHEK */
  let libPoll = null;
  function initLibrary() {
    const scanBtn = $("btn-lib-scan");
    if (!scanBtn) return;
    scanBtn.addEventListener("click", startLibraryScan);
    $("btn-lib-add").addEventListener("click", () => addLibrarySelection(false));
    const auto = $("btn-lib-add-auto");
    if (auto) auto.addEventListener("click", () => addLibrarySelection(true));
    const csv = $("btn-lib-csv");
    if (csv) csv.addEventListener("click", () => window.open("/api/library/export.csv", "_blank"));
    const all = $("lib-check-all");
    if (all) all.addEventListener("change", () => {
      document.querySelectorAll(".lib-check").forEach((c) => { c.checked = all.checked; });
    });
    const rs = $("lib-result-search");
    if (rs) rs.addEventListener("input", renderLibrary);
    const grp = $("lib-group");
    if (grp) grp.addEventListener("change", renderLibrary);
    initLibLibraries();
    // Sortierbare Spalten
    document.querySelectorAll(".lib-table th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        const cur = state.libSort || { key: "est_saved_bytes", dir: "desc" };
        state.libSort = (cur.key === key)
          ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
          : { key, dir: (key === "name" || key === "codec") ? "asc" : "desc" };
        renderLibrary();
      });
    });
    // Aktionen (Play / →Encode / →VMAF) delegiert.
    const body = $("lib-body");
    if (body) body.addEventListener("click", onLibAction);

    // Mehrfach-Auswahl-Dropdowns für Codec- und Dynamik-Filter.
    state.libCodecMulti = makeMultiSelect($("lib-codec-multi"), [
      { value: "h264", label: "H.264" },
      { value: "hevc", label: "HEVC/H.265" },
      { value: "av1", label: "AV1" },
      { value: "vp9", label: "VP9" },
      { value: "mpeg2video", label: "MPEG-2" },
      { value: "mpeg4", label: "MPEG-4" },
      { value: "vc1", label: "VC-1" },
    ], { placeholder: "Alle Codecs" });
    state.libDynMulti = makeMultiSelect($("lib-dynamic-multi"), [
      { value: "sdr", label: "SDR" },
      { value: "hdr", label: "HDR (ohne DV)" },
      { value: "dv", label: "Dolby Vision (alle)" },
      { value: "dv5", label: "DV Profil 5" },
      { value: "dv7", label: "DV Profil 7" },
      { value: "dv8", label: "DV Profil 8" },
    ], { placeholder: "Alle" });

    const cancel = $("btn-lib-cancel");
    if (cancel) cancel.addEventListener("click", cancelLibraryScan);
    const clear = $("btn-lib-clear");
    if (clear) clear.addEventListener("click", clearLibrary);

    libBuildFormats();
    loadLastLibrary();
  }

  function libBuildFormats() {
    const cont = $("lib-formats");
    if (!cont) return;
    const exts = (window.APP_CONFIG && window.APP_CONFIG.videoExtensions) || [];
    cont.innerHTML = exts.map((e) =>
      `<label><input type="checkbox" class="lib-fmt" value="${escapeHtml(e)}" /><span>${escapeHtml(e)}</span></label>`
    ).join("") || '<span class="empty">Keine Formate.</span>';
  }

  function libSelectedRoot() {
    const sel = $("lib-library");
    const id = sel ? sel.value : "";
    if (!id) return "";
    const lib = (state.libraries || []).find((l) => l.id === id);
    return lib ? (lib.path || "") : "";
  }

  function libFilters() {
    const codecs = state.libCodecMulti ? state.libCodecMulti.getValues() : [];
    const dyn = state.libDynMulti ? state.libDynMulti.getValues() : [];
    const codecMatch = $("lib-codec-match") ? $("lib-codec-match").value : "include";
    const f = {
      root: libSelectedRoot(),
      extensions: [...document.querySelectorAll(".lib-fmt:checked")].map((c) => c.value),
      name_contains: $("lib-name").value.trim(),
      name_exclude: ($("lib-exclude") ? $("lib-exclude").value : "")
        .split(",").map((s) => s.trim()).filter(Boolean),
      min_size_mb: parseFloat($("lib-min-size").value) || 0,
      min_bitrate_mbps: parseFloat($("lib-min-br").value) || 0,
      min_height: parseInt($("lib-min-h").value, 10) || 0,
      codecs_include: codecMatch === "exclude" ? [] : codecs,
      codecs_exclude: codecMatch === "exclude" ? codecs : [],
      target_codec: $("lib-target-codec") ? $("lib-target-codec").value : "av1",
      dynamic_filters: dyn,
      skip_optimized: $("lib-skip-optimized") ? $("lib-skip-optimized").checked : false,
      skip_processed: $("lib-skip-processed") ? $("lib-skip-processed").checked : false,
    };
    return f;
  }

  function initLibLibraries() {
    const sel = $("lib-library");
    if (!sel) return;
    refreshLibraries().then(() => {
      const saved = localStorage.getItem("libLibraryId") || "";
      if (saved && (state.libraries || []).some((l) => l.id === saved)) sel.value = saved;
      syncLibLibraryButtons();
    });
    sel.addEventListener("change", () => {
      localStorage.setItem("libLibraryId", sel.value || "");
      syncLibLibraryButtons();
    });
    const add = $("btn-lib-add-library");
    const edit = $("btn-lib-edit-library");
    const del = $("btn-lib-del-library");
    if (add) add.addEventListener("click", () => openLibraryEditor(null));
    if (edit) edit.addEventListener("click", () => {
      const lib = (state.libraries || []).find((l) => l.id === sel.value);
      if (lib) openLibraryEditor(lib);
    });
    if (del) del.addEventListener("click", async () => {
      const id = sel.value;
      if (!id) return;
      const lib = (state.libraries || []).find((l) => l.id === id);
      if (!lib || !confirm(tt("Unterbibliothek löschen?") + `\n${lib.name}`)) return;
      const r = await fetch(`/api/libraries/${encodeURIComponent(id)}`, { method: "DELETE" });
      const d = await r.json();
      if (d.error) { alert(d.error); return; }
      state.libraries = d.libraries || [];
      renderLibraryOptions("");
      syncLibLibraryButtons();
    });
  }

  async function refreshLibraries() {
    try {
      const d = await (await fetch("/api/libraries")).json();
      state.libraries = d.libraries || [];
      renderLibraryOptions(($("lib-library") || {}).value || "");
    } catch (e) {
      state.libraries = [];
    }
  }

  function renderLibraryOptions(selected) {
    const sel = $("lib-library");
    if (!sel) return;
    const cur = selected != null ? selected : sel.value;
    sel.innerHTML = `<option value="">${tt("— gesamter Medienbaum —")}</option>` +
      (state.libraries || []).map((l) =>
        `<option value="${escapeHtml(l.id)}">${escapeHtml(l.name)}` +
        (l.path ? ` (${escapeHtml(l.path)})` : "") + `</option>`).join("");
    if (cur) sel.value = cur;
    syncLibLibraryButtons();
  }

  function syncLibLibraryButtons() {
    const has = !!( $("lib-library") && $("lib-library").value );
    ["btn-lib-edit-library", "btn-lib-del-library"].forEach((id) => {
      const el = $(id); if (el) el.disabled = !has;
    });
  }

  function openLibraryEditor(existing) {
    const isEdit = !!(existing && existing.id);
    openModal(isEdit ? tt("Unterbibliothek bearbeiten") : tt("Unterbibliothek hinzufügen"), `
      <div class="field">
        <label>${tt("Name")}</label>
        <input type="text" id="lib-ed-name" value="${escapeHtml((existing && existing.name) || "")}"
               placeholder="${tt("z. B. Filme")}" style="width:100%" />
      </div>
      <div class="field" style="margin-top:10px">
        <label>${tt("Ordner (Medienpfad)")}</label>
        <div class="subdir-row">
          <input type="text" id="lib-ed-path" value="${escapeHtml((existing && existing.path) || "")}"
                 placeholder="${tt("leer = gesamter Baum")}" style="flex:1" />
          <button type="button" class="btn btn-ghost btn-sm" id="lib-ed-browse">${tt("Durchsuchen")}</button>
        </div>
        <p class="hint">${tt("Relativer Pfad unter /media, z. B. Filme oder Serien/Anime.")}</p>
      </div>
      <div class="lib-actions" style="margin-top:12px">
        <button class="btn btn-primary" id="lib-ed-save">${tt("Speichern")}</button>
        <button class="btn btn-ghost" id="lib-ed-cancel">${tt("Abbrechen")}</button>
        <span id="lib-ed-err" class="bad"></span>
      </div>`);
    const browse = $("lib-ed-browse");
    if (browse) browse.addEventListener("click", () => {
      const draft = {
        id: existing && existing.id,
        name: (($("lib-ed-name") || {}).value || ""),
        path: (($("lib-ed-path") || {}).value || ""),
      };
      openFolderPickerModal({
        title: tt("Ordner für Unterbibliothek"),
        start: draft.path || "",
        onPick: (rel) => openLibraryEditor({ ...draft, path: rel || "" }),
      });
    });
    const cancel = $("lib-ed-cancel");
    if (cancel) cancel.addEventListener("click", closeModal);
    const save = $("lib-ed-save");
    if (save) save.addEventListener("click", async () => {
      const name = ($("lib-ed-name") || {}).value || "";
      const path = ($("lib-ed-path") || {}).value || "";
      const errEl = $("lib-ed-err");
      try {
        let r;
        if (isEdit) {
          r = await fetch(`/api/libraries/${encodeURIComponent(existing.id)}`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim(), path: path.trim() }),
          });
        } else {
          r = await fetch("/api/libraries", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim(), path: path.trim() }),
          });
        }
        const d = await r.json();
        if (d.error) { if (errEl) errEl.textContent = d.error; return; }
        state.libraries = d.libraries || [];
        const pick = (d.library && d.library.id) || (existing && existing.id) || "";
        renderLibraryOptions(pick);
        localStorage.setItem("libLibraryId", pick || "");
        closeModal();
      } catch (e) {
        if (errEl) errEl.textContent = String(e);
      }
    });
  }

  function renderLibProjection(st) {
    const box = $("lib-projection");
    if (!box) return;
    const rows = st.matched || state.libRows || [];
    if (!rows.length) { box.style.display = "none"; return; }
    box.style.display = "";
    $("lib-proj-count").textContent = String(rows.length);
    $("lib-proj-size").textContent = st.total_size_human || formatBytes(st.total_size_bytes || 0);
    $("lib-proj-saved").textContent = st.total_saved_human || formatBytes(st.total_saved_bytes || 0);
    const pct = st.total_size_bytes
      ? Math.round((st.total_saved_bytes / st.total_size_bytes) * 100) : 0;
    $("lib-proj-pct").textContent = `${pct}%`;
  }

  function renderLibDashboard(stats, totalMatched) {
    const box = $("lib-dashboard");
    if (!box) return;
    if (!stats || !totalMatched) { box.style.display = "none"; return; }
    box.style.display = "";
    const bar = (label, count, total, cls) => {
      const pct = total ? Math.round((count / total) * 100) : 0;
      return `<div class="lib-bar"><span class="lib-bar-lbl">${escapeHtml(label)}</span>`
        + `<span class="lib-bar-track"><span class="lib-bar-fill ${cls || ""}" style="width:${pct}%"></span></span>`
        + `<span class="lib-bar-val">${count}</span></div>`;
    };
    const codecs = (stats.codec_distribution || []);
    $("lib-dash-codecs").innerHTML = codecs.map((c) =>
      bar((c.codec || "?").toUpperCase(), c.count, totalMatched)).join("") || "<span class='muted'>—</span>";
    $("lib-dash-dynamic").innerHTML =
      bar("SDR", stats.sdr_count || 0, totalMatched) +
      bar("HDR", stats.hdr_count || 0, totalMatched, "warn") +
      bar("Dolby Vision", stats.dv_count || 0, totalMatched, "accent");
    const hogs = stats.top_hogs || [];
    $("lib-dash-hogs").innerHTML = hogs.map((h) =>
      `<li title="${escapeHtml(h.path || "")}"><span class="hog-name">${escapeHtml(h.name || "")}</span>`
      + `<span class="hog-save good">${escapeHtml(h.est_saved_human || "—")}</span></li>`).join("")
      || "<li class='muted'>—</li>";
  }

  function libSetRunning(running) {
    const scan = $("btn-lib-scan"); if (scan) scan.disabled = running;
    const cancel = $("btn-lib-cancel"); if (cancel) cancel.disabled = !running;
    const clear = $("btn-lib-clear"); if (clear) clear.disabled = running;
  }

  async function startLibraryScan() {
    $("lib-scan-badge").textContent = "Scan läuft …";
    libSetRunning(true);
    await fetch("/api/library/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(libFilters()),
    });
    if (libPoll) clearInterval(libPoll);
    libPoll = setInterval(pollLibrary, 1200);
    pollLibrary();
  }

  async function cancelLibraryScan() {
    const b = $("btn-lib-cancel");
    if (b) { b.disabled = true; b.textContent = "Breche ab …"; }
    try { await fetch("/api/library/scan/cancel", { method: "POST" }); }
    catch (e) { /* ignorieren */ }
    setTimeout(() => { if (b) b.textContent = "Abbrechen"; }, 1500);
  }

  async function clearLibrary() {
    if (libPoll) return;  // während des Scans nicht leeren
    try { await fetch("/api/library/clear", { method: "POST" }); }
    catch (e) { /* ignorieren */ }
    state.libRows = [];
    state.libStats = null;
    renderLibrary();
    renderLibProjection({ matched: [] });
    renderLibDashboard(null, 0);
    ["btn-lib-add", "btn-lib-add-auto", "btn-lib-csv"].forEach((id) => {
      const el = $(id); if (el) el.disabled = true;
    });
    $("lib-progress").textContent = "";
    $("lib-scan-badge").textContent = "Bereit";
  }

  function applyLibState(st) {
    state.libRows = st.matched || [];
    state.libStats = st.stats || null;
    renderLibrary();
    renderLibProjection(st);
    renderLibDashboard(st.stats, state.libRows.length);
    const has = state.libRows.length > 0;
    ["btn-lib-add", "btn-lib-add-auto", "btn-lib-csv"].forEach((id) => {
      const b = $(id); if (b) b.disabled = !has;
    });
  }

  async function pollLibrary() {
    try {
      const r = await fetch("/api/library/scan");
      const st = await r.json();
      $("lib-progress").textContent =
        `${st.scanned}/${st.total} geprüft · ${st.matched.length} Treffer · ca. ${st.total_saved_human || "0 B"} einsparbar`;
      applyLibState(st);
      if (!st.running) {
        clearInterval(libPoll); libPoll = null;
        libSetRunning(false);
        $("lib-scan-badge").textContent = st.error ? "Fehler" : `${st.matched.length} Treffer`;
      }
    } catch (e) { /* ignorieren */ }
  }

  async function loadLastLibrary() {
    try {
      const r = await fetch("/api/library/last");
      const st = await r.json();
      if (st && (st.matched || []).length) {
        applyLibState(st);
        const when = st.generated_at ? new Date(st.generated_at * 1000).toLocaleString() : "";
        $("lib-scan-badge").textContent = `${st.matched.length} Treffer`;
        if (when) $("lib-progress").textContent = `Letzter Scan: ${when}`;
      }
    } catch (e) { /* kein Cache */ }
  }

  function libDynamicLabel(m) {
    if (m.dolby_vision) return "DV" + (m.dv_profile ? " P" + m.dv_profile : "");
    if (m.is_hdr) return (m.hdr_type || "HDR").replace(/ \(.*\)/, "");
    return "SDR";
  }

  function libViewRows() {
    const q = ($("lib-result-search") ? $("lib-result-search").value : "").trim().toLowerCase();
    let rows = (state.libRows || []).slice();
    if (q) rows = rows.filter((m) =>
      (m.name || "").toLowerCase().includes(q) || (m.folder || "").toLowerCase().includes(q));
    const s = state.libSort || { key: "est_saved_bytes", dir: "desc" };
    const numeric = ["height", "video_bitrate", "duration", "size_bytes", "est_saved_bytes"];
    rows.sort((a, b) => {
      let av = a[s.key], bv = b[s.key];
      if (numeric.includes(s.key)) { av = av || 0; bv = bv || 0; return s.dir === "asc" ? av - bv : bv - av; }
      av = String(av || "").toLowerCase(); bv = String(bv || "").toLowerCase();
      return s.dir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    return rows;
  }

  function libRowHtml(m) {
    const dyn = libDynamicLabel(m);
    const dynCls = m.dolby_vision ? "accent" : (m.is_hdr ? "warn" : "");
    const opt = m.already_optimized
      ? '<span class="lib-opt-badge">schon optimiert</span>'
      : `<span class="good">${escapeHtml(m.est_saved_human || "—")}</span>`;
    const sug = (m.suggest && m.suggest.label) ? escapeHtml(m.suggest.label) : "—";
    return `
      <tr>
        <td><input type="checkbox" class="lib-check" value="${escapeHtml(m.path)}" ${m.already_optimized ? "" : "checked"} /></td>
        <td title="${escapeHtml(m.path)}">${escapeHtml(m.name)}</td>
        <td>${escapeHtml((m.codec || "").toUpperCase())}</td>
        <td>${escapeHtml(m.resolution)}</td>
        <td><span class="dyn-badge ${dynCls}">${escapeHtml(dyn)}</span></td>
        <td>${escapeHtml(m.video_bitrate_human)}</td>
        <td>${escapeHtml(m.duration_human || "—")}</td>
        <td>${escapeHtml(m.size_human)}</td>
        <td>${opt}</td>
        <td class="lib-suggest">${sug}</td>
        <td class="lib-row-actions">
          <button class="lib-act" data-act="play" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Abspielen">▶</button>
          <button class="lib-act" data-act="encode" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Ins Encoding übernehmen">→E</button>
          <button class="lib-act" data-act="vmaf" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Ins VMAF-Tool übernehmen">→V</button>
        </td>
      </tr>`;
  }

  function renderLibrary() {
    const body = $("lib-body");
    if (!body) return;
    const rows = libViewRows();
    const cnt = $("lib-result-count");
    if (cnt) cnt.textContent = rows.length
      ? `${rows.length} von ${(state.libRows || []).length} angezeigt` : "";
    if (!rows.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="11">Keine Treffer.</td></tr>';
      return;
    }
    const grouped = $("lib-group") && $("lib-group").checked;
    if (!grouped) {
      body.innerHTML = rows.map(libRowHtml).join("");
      return;
    }
    // Nach Ordner gruppieren.
    const byFolder = {};
    rows.forEach((m) => {
      const k = m.folder || "(Wurzel)";
      (byFolder[k] = byFolder[k] || []).push(m);
    });
    body.innerHTML = Object.keys(byFolder).sort().map((folder) => {
      const items = byFolder[folder];
      const saved = items.reduce((a, m) => a + (m.est_saved_bytes || 0), 0);
      return `<tr class="lib-group-row"><td colspan="11">📁 ${escapeHtml(folder)} `
        + `<span class="muted">· ${items.length} Dateien · ca. ${escapeHtml(formatBytes(saved))} einsparbar</span></td></tr>`
        + items.map(libRowHtml).join("");
    }).join("");
  }

  function onLibAction(e) {
    const btn = e.target.closest(".lib-act");
    if (!btn) return;
    e.stopPropagation();
    const path = btn.dataset.path;
    const name = btn.dataset.name;
    const act = btn.dataset.act;
    if (act === "play") { openPlayer("media", path, name); return; }
    const row = (state.libRows || []).find((m) => m.path === path);
    libTransfer(path, name, act === "vmaf" ? "vmaf" : "encode", row ? row.suggest : null);
  }

  // Datei aus der Bibliothek in Encoding/VMAF-Tool übernehmen (inkl. Vorschlag).
  async function libTransfer(path, name, page, suggest) {
    navTo(page);
    await selectFile({ rel: path, name: name });
    if (page === "encode" && suggest) {
      const cSel = $("opt-codec");
      if (cSel && suggest.codec) { cSel.value = suggest.codec; cSel.dispatchEvent(new Event("change")); }
      if (suggest.dv_mode) {
        const dv = $("opt-dv-mode");
        if (dv) { dv.value = suggest.dv_mode; dv.dataset.userset = "1"; }
      } else if (suggest.hdr_mode) {
        const h = $("opt-hdr-mode");
        if (h) h.value = suggest.hdr_mode;
      }
    }
  }

  async function addLibrarySelection(auto) {
    const checked = [...document.querySelectorAll(".lib-check:checked")].map((c) => c.value);
    if (!checked.length) return;
    const base = gatherSettings();
    // Container-Wahl der Bibliothek hat Vorrang vor der Encode-Seite.
    const libContainer = $("lib-container") ? $("lib-container").value : "";
    if (libContainer) base.container = libContainer;
    const estimates = {};
    checked.forEach((p) => {
      const row = (state.libRows || []).find((m) => m.path === p);
      if (row) {
        estimates[p] = {
          est_saved_bytes: row.est_saved_bytes || 0,
          est_output_bytes: row.est_output_bytes || 0,
        };
      }
    });
    const go = await confirmDryRunOrDups(checked, base, estimates, { forcePreview: true });
    if (!go) return;
    const btnA = $("btn-lib-add"); const btnB = $("btn-lib-add-auto");
    if (btnA) btnA.disabled = true; if (btnB) btnB.disabled = true;
    let ok = 0;
    for (const p of checked) {
      let payload = { path: p, is_batch: false, ...base };
      if (auto) {
        const row = (state.libRows || []).find((m) => m.path === p);
        const sug = row && row.suggest;
        if (sug) {
          payload.codec = sug.codec;
          payload.suffix = "_" + sug.codec;
          if (sug.dv_mode) payload.dv_mode = sug.dv_mode;
          else if (sug.hdr_mode) payload.hdr_mode = sug.hdr_mode;
        }
      }
      try {
        const r = await fetch("/api/enqueue", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (r.ok) ok++;
      } catch (e) { /* weiter */ }
    }
    if (btnA) btnA.disabled = false; if (btnB) btnB.disabled = false;
    $("lib-progress").textContent = auto
      ? `${ok} mit Auto-Einstellungen hinzugefügt.`
      : `${ok} zur Warteschlange hinzugefügt.`;
  }

  /* ------------------------------------------------------------- SUPER-TOOL */
  let superScanPoll = null;
  let superStatusPoll = null;
  let stListTimer = null;

  const ST_MODE_HINTS = {
    target_vmaf: "Pro Datei werden Test-Encodes mit den CQ-Werten unten erstellt, per VMAF " +
      "gemessen und automatisch der effizienteste Wert mit VMAF ≥ Ziel gewählt. Genau, aber rechenintensiv.",
    representative: "Nur die erste Datei wird per VMAF getestet (Sweet-Spot ~93–95). Der ermittelte " +
      "Wert wird auf alle übrigen Dateien übertragen – schnell, aber weniger genau bei gemischtem Material.",
    fixed: "Alle Dateien werden mit exakt dem eingestellten CQ bzw. der Bitrate encodiert – ohne VMAF-Analyse.",
  };

  function initSuperTool() {
    if (!$("btn-st-scan")) return;
    stBuildFormats();
    stLoadDir("");

    const mode = $("st-mode");
    const syncMode = () => {
      const m = mode.value;
      $("st-target-field").style.display = m === "target_vmaf" ? "" : "none";
      $("st-quality-field").style.display = m === "fixed" ? "" : "none";
      const cfg = $("st-vmaf-config");
      if (cfg) cfg.style.display = m === "fixed" ? "none" : "";
      const h = $("st-mode-hint");
      if (h) h.textContent = ST_MODE_HINTS[m] || "";
    };
    mode.addEventListener("change", syncMode);
    syncMode();

    const target = $("st-target");
    if (target) target.addEventListener("input", () => { $("st-target-val").textContent = target.value; });
    const q = $("st-quality");
    if (q) q.addEventListener("input", () => { $("st-quality-val").textContent = q.value; });

    const rate = $("st-rate-mode");
    const syncRate = () => {
      const cq = rate.value === "cq";
      $("st-cq-field").style.display = cq ? "" : "none";
      $("st-br-field").style.display = cq ? "none" : "";
    };
    rate.addEventListener("change", syncRate);
    syncRate();

    // Ziel-VMAF/Repräsentativ: Test-Encodes wahlweise über CQ- oder Bitratenwerte.
    const vmafRate = $("st-vmaf-rate");
    if (vmafRate) {
      vmafRate.addEventListener("change", () => syncVmafRate(true));
      syncVmafRate(false);
    }

    $("st-platform").addEventListener("change", stUpdateCodec);
    $("st-codec").addEventListener("change", stUpdateCodec);
    stUpdateCodec();

    $("btn-st-scan").addEventListener("click", startSuperScan);
    $("btn-st-start").addEventListener("click", startSuperBatch);
    const stCancel = $("btn-st-cancel");
    if (stCancel) stCancel.addEventListener("click", cancelSuperScan);
    stInitTrackHandlers();
    stInitCommonHandlers();
    stInitLangWhitelist();

    // Remux-Schalter: Encode-Einstellungen aus-/einblenden.
    const remux = $("st-remux-only");
    if (remux) {
      const syncRemux = () => {
        const on = remux.checked;
        const enc = $("st-encode-only");
        if (enc) enc.style.display = on ? "none" : "";
        const rc = $("st-remux-container-field");
        if (rc) rc.style.display = on ? "" : "none";
        const am = $("st-audio-mode-field");
        if (am) am.style.display = on ? "none" : "";
        const sc = $("st-sidecar-field");
        if (sc) sc.style.display = on ? "" : "none";
        const scan = $("btn-st-scan");
        if (scan) scan.textContent = on ? "Scan – Spuren ermitteln" : "Scan – Codec/Bitrate ermitteln";
      };
      remux.addEventListener("change", syncRemux);
      syncRemux();
    }

    // Warn-Schwelle (viele Dateien) lokal persistieren.
    const warnEl = $("st-warn-count");
    if (warnEl) {
      const saved = localStorage.getItem("st-warn-count");
      if (saved !== null && saved !== "") warnEl.value = saved;
      warnEl.addEventListener("change", () => {
        localStorage.setItem("st-warn-count", warnEl.value);
      });
    }
    const all = $("st-check-all");
    if (all) all.addEventListener("change", () => {
      document.querySelectorAll(".st-check").forEach((c) => { c.checked = all.checked; });
    });

    // Live-Vorschau bei Änderung der günstigen Filter aktualisieren.
    ["st-name", "st-exclude", "st-min-size"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("input", stRefreshListDebounced);
    });
    const fmts = $("st-formats");
    if (fmts) fmts.addEventListener("change", stRefreshListDebounced);
  }

  function stUpdateCodec() {
    const sel = $("st-codec");
    const plat = $("st-platform").value;
    if (!sel) return;
    let firstAvail = null;
    [...sel.options].forEach((opt) => {
      const ok = isEncoderAvailable(plat, opt.value);
      opt.disabled = !ok;
      opt.textContent = (CODEC_LABELS[opt.value] || opt.value.toUpperCase())
        + (ok ? "" : encUnavailReason(plat, opt.value));
      if (ok && firstAvail === null) firstAvail = opt.value;
    });
    if (sel.selectedOptions[0] && sel.selectedOptions[0].disabled && firstAvail) sel.value = firstAvail;
    const hint = $("st-codec-hint");
    if (hint) {
      const e = encoderInfo(plat, sel.value);
      hint.textContent = e ? `FFmpeg-Encoder: ${e.encoder}` : "";
    }
  }

  function stBuildFormats() {
    const cont = $("st-formats");
    if (!cont) return;
    const exts = (window.APP_CONFIG && window.APP_CONFIG.videoExtensions) || [];
    cont.innerHTML = exts.map((e) =>
      `<label><input type="checkbox" class="st-fmt" value="${escapeHtml(e)}" /><span>${escapeHtml(e)}</span></label>`
    ).join("") || '<span class="empty">Keine Formate.</span>';
  }

  // Live-Vorschau: schnelle Dateiliste (ohne Probe) zum aktuellen Ordner + Filter.
  function stRefreshListDebounced() {
    if (stListTimer) clearTimeout(stListTimer);
    stListTimer = setTimeout(stRefreshList, 350);
  }

  async function stRefreshList() {
    const panel = $("st-file-panel");
    if (!panel) return;
    panel.innerHTML = '<div class="browser-loading">Lade …</div>';
    try {
      const d = await (await fetch("/api/supertool/list", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(stFilters()),
      })).json();
      renderQuickList(d);
    } catch (e) {
      panel.innerHTML = `<div class="browser-loading">Fehler: ${escapeHtml(String(e))}</div>`;
    }
  }

  function renderQuickList(d) {
    const panel = $("st-file-panel");
    const cnt = $("st-list-count");
    const files = (d && d.files) || [];
    if (cnt) cnt.textContent = (d && d.truncated) ? `${files.length}+` : String((d && d.count) || 0);
    if (!panel) return;
    if (d && d.error) { panel.innerHTML = `<div class="browser-loading">${escapeHtml(d.error)}</div>`; return; }
    if (!files.length) { panel.innerHTML = '<div class="browser-loading">Keine passenden Dateien.</div>'; return; }
    panel.innerHTML = files.map((f) =>
      `<div class="st-file-row" title="${escapeHtml(f.path)}">` +
      `<span class="row-name">🎬 ${escapeHtml(f.name)}</span>` +
      `<span class="row-size">${escapeHtml(f.size_human)}</span></div>`
    ).join("") + (d.truncated
      ? '<div class="browser-loading">… weitere ausgeblendet (Limit 1000)</div>' : "");
  }

  // Ordner-Browser des Super-Tools (der geöffnete Ordner ist zugleich der zu
  // scannende Ordner). Nutzt die gemeinsame Factory, zeigt nur Ordner.
  let stBrowser = null;
  function stLoadDir(path) {
    if (!stBrowser) {
      stBrowser = makeFolderBrowser({
        listId: "st-browser", crumbId: "st-breadcrumb", kind: "video",
        showFiles: false,
        searchPlaceholder: "Unterordner filtern …",
        onNavigate: (data, p) => {
          state.stPath = p;
          const hid = $("st-folder"); if (hid) hid.value = p;
          const info = $("st-folder-info");
          if (info) info.textContent = p
            ? `Aktuell: /${p} (inkl. Unterordner)`
            : "Aktuell: gesamter Eingabeordner (alle Unterordner)";
          stRefreshList();
        },
      });
    }
    return stBrowser ? stBrowser.go(path) : undefined;
  }

  function stDirRow(name, onOpen) {
    const row = document.createElement("div");
    row.className = "row-item";
    row.innerHTML =
      `<span class="row-icon dir">📁</span><span class="row-name">${escapeHtml(name)}</span>`;
    row.addEventListener("click", onOpen);
    return row;
  }

  function stFilters() {
    const codecMode = $("st-codec-mode").value;
    const f = {
      folder: $("st-folder").value.trim(),
      extensions: [...document.querySelectorAll(".st-fmt:checked")].map((c) => c.value),
      name_contains: $("st-name").value.trim(),
      name_exclude: $("st-exclude").value.split(",").map((s) => s.trim()).filter(Boolean),
      min_size_mb: parseFloat($("st-min-size").value) || 0,
      min_bitrate_mbps: parseFloat($("st-min-br").value) || 0,
      min_height: parseInt($("st-min-h").value, 10) || 0,
      codecs_include: [],
      codecs_exclude: [],
    };
    if (codecMode === "exclude-av1") f.codecs_exclude = ["av1"];
    else if (codecMode === "include-h264") f.codecs_include = ["h264"];
    else if (codecMode === "include-hevc") f.codecs_include = ["hevc"];
    return f;
  }

  async function startSuperScan() {
    $("st-scan-badge").textContent = "Scan läuft …";
    $("btn-st-start").disabled = true;
    stScanRunning(true);
    state.stTracks = {};
    await fetch("/api/supertool/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(stFilters()),
    });
    if (superScanPoll) clearInterval(superScanPoll);
    superScanPoll = setInterval(pollSuperScan, 1200);
    pollSuperScan();
  }

  function stScanRunning(running) {
    const scan = $("btn-st-scan");
    if (scan) scan.disabled = running;
    const cancel = $("btn-st-cancel");
    if (cancel) cancel.disabled = !running;
  }

  async function cancelSuperScan() {
    const cancel = $("btn-st-cancel");
    if (cancel) cancel.disabled = true;
    try {
      await fetch("/api/supertool/scan/cancel", { method: "POST" });
      $("st-progress").textContent = "Scan abgebrochen.";
    } catch (e) { /* ignorieren */ }
  }

  async function pollSuperScan() {
    try {
      const st = await (await fetch("/api/supertool/scan")).json();
      $("st-progress").textContent =
        `${st.scanned}/${st.total} geprüft · ${st.matched.length} Treffer`;
      renderSuperMatches(st.matched);
      if (!st.running) {
        clearInterval(superScanPoll); superScanPoll = null;
        stScanRunning(false);
        $("st-scan-badge").textContent = st.error ? "Fehler" : `${st.matched.length} Treffer`;
        $("btn-st-start").disabled = st.matched.length === 0;
        // Whitelist (falls schon gesetzt) direkt auf die neue Trefferliste anwenden.
        if (($("st-audio-langs") || {}).value) stApplyLangWhitelist("audio");
        if (($("st-sub-langs") || {}).value) stApplyLangWhitelist("subs");
      }
    } catch (e) { /* ignorieren */ }
  }

  function stAudioLabel(a) {
    const lang = (a.language || "und").toUpperCase();
    const codec = (a.codec || "?").toUpperCase();
    const ch = a.layout || (a.channels ? a.channels + "ch" : "");
    const br = (a.bitrate_human && a.bitrate_human !== "—") ? a.bitrate_human : "";
    return [lang, codec, ch, br].filter(Boolean).join(" · ") +
      (a.title ? ` – ${a.title}` : "");
  }

  function stSubLabel(s) {
    const lang = (s.language || "und").toUpperCase();
    const codec = (s.codec || "?").toUpperCase();
    const flags = [s.default ? "default" : "", s.forced ? "forced" : ""].filter(Boolean).join("/");
    return [lang, codec, flags].filter(Boolean).join(" · ") +
      (s.title ? ` – ${s.title}` : "");
  }

  // Mini-Dropdown mit Häkchen für Ton- oder Untertitelspuren einer Datei.
  function stTrackCell(kind, m, tracks) {
    if (!tracks || !tracks.length) return '<td class="muted">—</td>';
    const sel = stTrackSel(m.path, kind);
    const label = kind === "audio" ? stAudioLabel : stSubLabel;
    const opts = tracks.map((t) =>
      `<label class="check st-track-opt"><input type="checkbox" data-idx="${t.index}"` +
      `${sel.has(t.index) ? " checked" : ""} /><span>${escapeHtml(label(t))}</span></label>`
    ).join("");
    const btn = stTrackBtnText(sel.size, tracks.length);
    return `<td class="st-track-cell"><div class="st-track-dd" data-kind="${kind}"` +
      ` data-path="${escapeHtml(m.path)}"><button type="button" class="st-track-btn">` +
      `${btn}</button><div class="st-track-panel" hidden>${opts}</div></div></td>`;
  }

  function stTrackBtnText(sel, total) {
    if (sel >= total) return `${window.I18N ? I18N.t("Alle") : "Alle"} (${total})`;
    if (sel === 0) return window.I18N ? I18N.t("Keine") : "Keine";
    return `${sel}/${total}`;
  }

  // Aktuelle Auswahl (Set der Indizes) einer Datei/Spurart; initialisiert mit
  // allen vorhandenen Spuren beim ersten Zugriff.
  function stTrackSel(path, kind) {
    if (!state.stTracks) state.stTracks = {};
    if (!state.stTracks[path]) state.stTracks[path] = {};
    if (!state.stTracks[path][kind]) {
      const m = (state.stMatches || {})[path];
      const list = m ? (kind === "audio" ? m.audio : m.subtitles) || [] : [];
      state.stTracks[path][kind] = new Set(list.map((t) => t.index));
    }
    return state.stTracks[path][kind];
  }

  function renderSuperMatches(rows) {
    const body = $("st-body");
    if (!body) return;
    state.stRows = rows;
    state.stMatches = {};
    rows.forEach((m) => { state.stMatches[m.path] = m; });
    stRenderCommon(rows);
    body.innerHTML = rows.length ? rows.map((m) => {
      const dyn = libDynamicLabel(m);
      const dynCls = m.dolby_vision ? "accent" : (m.is_hdr ? "warn" : "");
      return `
      <tr>
        <td><input type="checkbox" class="st-check" value="${escapeHtml(m.path)}" checked /></td>
        <td title="${escapeHtml(m.path)}">${escapeHtml(m.name)}</td>
        <td>${escapeHtml((m.container || "—").toUpperCase())}</td>
        <td>${escapeHtml((m.codec || "").toUpperCase())}</td>
        <td>${escapeHtml(m.resolution)}</td>
        <td><span class="dyn-badge ${dynCls}">${escapeHtml(dyn)}</span></td>
        <td>${escapeHtml(m.video_bitrate_human)}</td>
        <td>${escapeHtml(m.size_human)}</td>
        ${stTrackCell("audio", m, m.audio)}
        ${stTrackCell("subs", m, m.subtitles)}
        <td class="lib-row-actions">
          <button class="lib-act" data-act="play" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Abspielen">▶</button>
          <button class="lib-act" data-act="encode" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Ins Encoding übernehmen">→E</button>
          <button class="lib-act" data-act="vmaf" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Ins VMAF-Tool übernehmen">→V</button>
          <button class="lib-act" data-act="remux" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Ins Remux übernehmen">→R</button>
        </td>
      </tr>`; }).join("") :
      '<tr class="empty-row"><td colspan="11">Keine Treffer.</td></tr>';
  }

  // Öffnet/schließt die Mini-Dropdowns und pflegt die Auswahl. Delegation auf
  // dem Tabellenkörper, da dieser bei jedem Scan-Poll neu gerendert wird.
  function stInitTrackHandlers() {
    const body = $("st-body");
    if (!body || body.dataset.trackWired) return;
    body.dataset.trackWired = "1";
    body.addEventListener("click", (e) => {
      const actBtn = e.target.closest(".lib-act");
      if (actBtn) { onStAction(e); return; }
      if (e.target.closest(".st-track-panel")) { e.stopPropagation(); return; }
      const btn = e.target.closest(".st-track-btn");
      if (!btn) return;
      e.stopPropagation();
      const panel = btn.nextElementSibling;
      const isOpen = !panel.hidden;
      stCloseTrackPanels();
      if (isOpen) return;
      // Fixed positionieren, damit das Panel nicht vom horizontal scrollenden
      // Tabellen-Container abgeschnitten wird.
      panel.hidden = false;
      const r = btn.getBoundingClientRect();
      panel.style.position = "fixed";
      panel.style.top = `${Math.round(r.bottom + 4)}px`;
      const w = panel.offsetWidth || 240;
      panel.style.left = `${Math.round(Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)))}px`;
      panel.style.right = "auto";
    });
    body.addEventListener("change", (e) => {
      const cb = e.target.closest(".st-track-opt input");
      if (!cb) return;
      const dd = cb.closest(".st-track-dd");
      const kind = dd.dataset.kind;
      const path = dd.dataset.path;
      const idx = parseInt(cb.dataset.idx, 10);
      const sel = stTrackSel(path, kind);
      if (cb.checked) sel.add(idx); else sel.delete(idx);
      const total = dd.querySelectorAll(".st-track-opt").length;
      dd.querySelector(".st-track-btn").textContent = stTrackBtnText(sel.size, total);
      stRenderCommon(state.stRows || []);
    });
    document.addEventListener("click", stCloseTrackPanels);
    window.addEventListener("scroll", stCloseTrackPanels, true);
  }

  async function onStAction(e) {
    const btn = e.target.closest(".lib-act");
    if (!btn) return;
    e.stopPropagation();
    const path = btn.dataset.path;
    const name = btn.dataset.name;
    const act = btn.dataset.act;
    if (act === "play") { openPlayer("media", path, name); return; }
    if (act === "remux" || (act === "encode" && $("st-remux-only") && $("st-remux-only").checked)) {
      navTo("remux");
      await remuxSelectFile({ rel: path, name });
      return;
    }
    await libTransfer(path, name, act === "vmaf" ? "vmaf" : "encode", null);
  }

  function stCloseTrackPanels() {
    document.querySelectorAll("#st-body .st-track-panel").forEach((p) => {
      p.hidden = true;
      p.style.position = "";
      p.style.top = "";
      p.style.left = "";
      p.style.right = "";
    });
  }

  // Spur-Signatur (Sprache/Codec/…) – für Schnittmenge über Dateien hinweg.
  // Ohne Titel, damit Serien mit leicht abweichenden Spur-Namen matchen;
  // Extra-Spuren einzelner Folgen (andere Sprache/Forced) fallen aus der Schnittmenge.
  function stOneTrackSig(kind, t) {
    const lang = (t.language || "und").toLowerCase();
    if (kind === "audio") {
      return `${lang}|${(t.codec || "").toLowerCase()}|${t.channels || ""}`;
    }
    return `${lang}|${(t.codec || "").toLowerCase()}|${t.forced ? "f" : ""}`;
  }

  function stTracksOf(m, kind) {
    return (kind === "audio" ? m.audio : m.subtitles) || [];
  }

  // Signaturen, die in jeder Datei mindestens einmal vorkommen.
  function stCommonSigs(rows, kind) {
    if (!rows || !rows.length) return [];
    let common = null;
    const order = [];
    const seen = new Set();
    rows.forEach((m) => {
      const set = new Set();
      stTracksOf(m, kind).forEach((t) => {
        const sig = stOneTrackSig(kind, t);
        set.add(sig);
        if (!seen.has(sig)) { seen.add(sig); order.push(sig); }
      });
      common = common === null ? set : new Set([...common].filter((s) => set.has(s)));
    });
    return order.filter((s) => common && common.has(s));
  }

  function stFindTrackBySig(m, kind, sig) {
    return stTracksOf(m, kind).find((t) => stOneTrackSig(kind, t) === sig) || null;
  }

  function stSigSelectedInAll(rows, kind, sig) {
    return rows.every((m) => {
      const matches = stTracksOf(m, kind).filter((t) => stOneTrackSig(kind, t) === sig);
      if (!matches.length) return false;
      const sel = stTrackSel(m.path, kind);
      return matches.every((t) => sel.has(t.index));
    });
  }

  function stApplySigToAll(rows, kind, sig, on) {
    rows.forEach((m) => {
      const sel = stTrackSel(m.path, kind);
      stTracksOf(m, kind).forEach((t) => {
        if (stOneTrackSig(kind, t) !== sig) return;
        if (on) sel.add(t.index); else sel.delete(t.index);
      });
    });
  }

  // Gemeinsame Spurauswahl über die Schnittmenge (nicht nur bei exakter Gleichheit).
  function stRenderCommon(rows) {
    const box = $("st-common-tracks");
    if (!box) return;
    if (!rows || rows.length < 2) { box.style.display = "none"; return; }

    const aSigs = stCommonSigs(rows, "audio");
    const sSigs = stCommonSigs(rows, "subs");
    if (!aSigs.length && !sSigs.length) { box.style.display = "none"; return; }

    const allExact = (() => {
      const sig0 = (m) =>
        stTracksOf(m, "audio").map((t) => stOneTrackSig("audio", t)).join(",") + "##" +
        stTracksOf(m, "subs").map((t) => stOneTrackSig("subs", t)).join(",");
      const s0 = sig0(rows[0]);
      return rows.every((m) => sig0(m) === s0);
    })();

    const chk = (kind, sig, on, label) =>
      `<label class="check st-common-opt"><input type="checkbox" data-kind="${kind}"` +
      ` data-sig="${escapeHtml(sig)}"${on ? " checked" : ""} /><span>${escapeHtml(label)}</span></label>`;
    const aBoxes = aSigs.map((sig) => {
      const t = stFindTrackBySig(rows[0], "audio", sig);
      return t ? chk("audio", sig, stSigSelectedInAll(rows, "audio", sig), stAudioLabel(t)) : "";
    }).join("");
    const sBoxes = sSigs.map((sig) => {
      const t = stFindTrackBySig(rows[0], "subs", sig);
      return t ? chk("subs", sig, stSigSelectedInAll(rows, "subs", sig), stSubLabel(t)) : "";
    }).join("");
    const tt = (x) => (window.I18N ? I18N.t(x) : x);
    const head = allExact
      ? tt("Alle Dateien haben dieselben Spuren – Auswahl für alle übernehmen:")
      : tt("Gemeinsame Spuren aller Dateien – Auswahl für alle übernehmen:");
    const hint = allExact ? "" :
      `<p class="hint">${tt("Sonder-Spuren einzelner Dateien bleiben unberührt und können pro Zeile angepasst werden.")}</p>`;
    box.innerHTML =
      `<div class="st-common-head">${head}</div>${hint}` +
      `<div class="st-common-grid">` +
      `<div class="st-common-col"><strong>${tt("Ton")}</strong>${aBoxes || '<span class="muted">—</span>'}</div>` +
      `<div class="st-common-col"><strong>${tt("Untertitel")}</strong>${sBoxes || '<span class="muted">—</span>'}</div>` +
      `</div>`;
    box.style.display = "";
  }

  function stInitCommonHandlers() {
    const box = $("st-common-tracks");
    if (!box || box.dataset.wired) return;
    box.dataset.wired = "1";
    box.addEventListener("change", (e) => {
      const cb = e.target.closest(".st-common-opt input");
      if (!cb) return;
      const kind = cb.dataset.kind;
      const sig = cb.dataset.sig;
      if (!sig) return;
      stApplySigToAll(state.stRows || [], kind, sig, cb.checked);
      stUpdateRowButtons();
    });
  }

  // Sprach-Whitelist (wie Backend) → kanonische 2-Buchstaben-Codes.
  const ST_LANG_TO_CANON = (() => {
    const aliases = {
      de: ["de", "deu", "ger", "german", "deutsch"],
      en: ["en", "eng", "english"],
      fr: ["fr", "fra", "fre", "french", "francais", "français"],
      es: ["es", "spa", "spanish", "espanol", "español", "castellano"],
      it: ["it", "ita", "italian", "italiano"],
      pt: ["pt", "por", "portuguese", "portugues", "português"],
      nl: ["nl", "nld", "dut", "dutch", "nederlands"],
      ru: ["ru", "rus", "russian"],
      ja: ["ja", "jpn", "japanese"],
      zh: ["zh", "zho", "chi", "chinese", "mandarin"],
      ko: ["ko", "kor", "korean"],
      pl: ["pl", "pol", "polish"],
      sv: ["sv", "swe", "swedish"],
      da: ["da", "dan", "danish"],
      no: ["no", "nor", "norwegian"],
      fi: ["fi", "fin", "finnish"],
      cs: ["cs", "cze", "ces", "czech"],
      hu: ["hu", "hun", "hungarian"],
      tr: ["tr", "tur", "turkish"],
      ar: ["ar", "ara", "arabic"],
      hi: ["hi", "hin", "hindi"],
      und: ["und", "undetermined", "unknown"],
    };
    const map = {};
    Object.keys(aliases).forEach((c) => aliases[c].forEach((f) => { map[f] = c; }));
    return map;
  })();

  function stCanonLang(s) {
    const t = String(s || "").trim().toLowerCase();
    return ST_LANG_TO_CANON[t] || t;
  }

  function stParseLangs(val) {
    return new Set(String(val || "").replace(/;/g, ",").split(",")
      .map((p) => stCanonLang(p)).filter(Boolean));
  }

  function stInitLangWhitelist() {
    [["st-audio-langs", "audio"], ["st-sub-langs", "subs"]].forEach(([id, kind]) => {
      const el = $(id);
      if (!el || el.dataset.wlWired) return;
      el.dataset.wlWired = "1";
      let t = null;
      el.addEventListener("input", () => {
        if (t) clearTimeout(t);
        t = setTimeout(() => stApplyLangWhitelist(kind), 200);
      });
    });
  }

  // Whitelist sofort auf die Spurauswahl in der Trefferliste anwenden.
  // kind: "audio" | "subs" – nur die betreffende Spurart wird angepasst.
  function stApplyLangWhitelist(kind) {
    const rows = state.stRows || [];
    if (!rows.length) return;
    const doAudio = kind === "audio";
    const doSubs = kind === "subs";
    if (!doAudio && !doSubs) return;
    const aLangs = stParseLangs($("st-audio-langs") ? $("st-audio-langs").value : "");
    const sLangs = stParseLangs($("st-sub-langs") ? $("st-sub-langs").value : "");
    rows.forEach((m) => {
      if (doAudio) {
        const aList = m.audio || [];
        const aSel = stTrackSel(m.path, "audio");
        aSel.clear();
        if (aLangs.size) {
          const picked = aList.filter((t) => aLangs.has(stCanonLang(t.language)));
          // Ohne Treffer alle behalten (kein Ton-Verlust, analog Backend).
          (picked.length ? picked : aList).forEach((t) => aSel.add(t.index));
        } else {
          aList.forEach((t) => aSel.add(t.index));
        }
      }
      if (doSubs) {
        const sList = m.subtitles || [];
        const sSel = stTrackSel(m.path, "subs");
        sSel.clear();
        if (sLangs.size) {
          sList.filter((t) => sLangs.has(stCanonLang(t.language)))
            .forEach((t) => sSel.add(t.index));
        } else {
          sList.forEach((t) => sSel.add(t.index));
        }
      }
    });
    stUpdateRowButtons();
    stRenderCommon(rows);
  }

  // Aktualisiert die Zeilen-Dropdowns (Button-Text + Häkchen) nach einer
  // gemeinsamen Auswahl, ohne die Tabelle komplett neu zu rendern.
  function stUpdateRowButtons() {
    document.querySelectorAll("#st-body .st-track-dd").forEach((dd) => {
      const kind = dd.dataset.kind;
      const path = dd.dataset.path;
      const sel = stTrackSel(path, kind);
      const total = dd.querySelectorAll(".st-track-opt").length;
      const btn = dd.querySelector(".st-track-btn");
      if (btn) btn.textContent = stTrackBtnText(sel.size, total);
      dd.querySelectorAll(".st-track-opt input").forEach((cb) => {
        cb.checked = sel.has(parseInt(cb.dataset.idx, 10));
      });
    });
  }

  // Passt Beschriftung, Grenzen und (optional) die Vorgabewerte des Test-Grids
  // an den gewählten Steuerungsmodus an (CQ vs. Bitrate).
  function syncVmafRate(resetValues) {
    const mode = $("st-vmaf-rate") ? $("st-vmaf-rate").value : "cq";
    const bitrate = mode === "abr" || mode === "bitrate";
    const lbl = $("st-test-label");
    if (lbl) lbl.textContent = bitrate ? "Test-Bitraten (kbit/s)" : "Test-CQ-Werte";
    const hint = $("st-test-hint");
    if (hint) hint.textContent = bitrate
      ? "Leere Felder werden ignoriert. Höhere Bitrate = höhere Qualität/größer."
      : "Leere Felder werden ignoriert. Niedriger CQ = höhere Qualität/größer.";
    const inputs = [...document.querySelectorAll("#st-test-grid .st-test-val")];
    if (resetValues) {
      const defs = bitrate ? [8000, 6000, 4000, 2000] : [20, 24, 28, 32];
      inputs.forEach((inp, i) => { inp.value = defs[i] != null ? defs[i] : ""; });
    }
    inputs.forEach((inp) => {
      inp.min = bitrate ? 500 : 1;
      inp.max = bitrate ? 50000 : 51;
      inp.step = bitrate ? 500 : 1;
    });
  }

  function stTestValues() {
    const vals = [...document.querySelectorAll("#st-test-grid .st-test-val")]
      .map((i) => parseInt(i.value, 10))
      .filter((v) => !isNaN(v) && v > 0);
    if (vals.length) return vals;
    const bitrate = $("st-vmaf-rate") &&
      ($("st-vmaf-rate").value === "abr" || $("st-vmaf-rate").value === "bitrate");
    return bitrate ? [8000, 6000, 4000, 2000] : [20, 24, 28, 32];
  }

  function stGatherSettings() {
    const mode = $("st-mode").value;
    const rateMode = $("st-rate-mode").value;
    const s = {
      platform: $("st-platform").value,
      codec: $("st-codec").value,
      suffix: "_" + $("st-codec").value,
      post_processing: $("st-post").value,
      audio_mode: $("st-audio-mode").value,
      rate_mode: "cq",
      anime: $("st-anime") ? $("st-anime").checked : false,
      dynamik: $("st-dynamik") ? $("st-dynamik").value : "auto",
      audio_languages: $("st-audio-langs") ? $("st-audio-langs").value.trim() : "",
      subtitle_languages: $("st-sub-langs") ? $("st-sub-langs").value.trim() : "",
      remux_only: $("st-remux-only") ? $("st-remux-only").checked : false,
      remux_container: $("st-remux-container") ? $("st-remux-container").value : "mkv",
      sidecar_attachments: $("st-sidecar-att") ? $("st-sidecar-att").checked : false,
      name_pattern: $("st-name-pattern") ? ($("st-name-pattern").value.trim() || "{stem}{suffix}") : "{stem}{suffix}",
      on_duplicate: $("st-on-duplicate") ? $("st-on-duplicate").value : "ask",
      max_output_mb: $("st-max-output-mb") ? (parseFloat($("st-max-output-mb").value) || 0) : 0,
      max_video_bitrate_kbps: $("st-max-bitrate") ? (parseInt($("st-max-bitrate").value, 10) || 0) : 0,
      size_target_mb: $("st-size-target") ? (parseFloat($("st-size-target").value) || 0) : 0,
      ...outTargetVals("st"),
    };
    if (mode === "target_vmaf" || mode === "representative") {
      // Test-Encode-Konfiguration für die VMAF-Analyse (CQ oder Bitrate).
      s.rate_mode = $("st-vmaf-rate") ? $("st-vmaf-rate").value : "cq";
      s.clip_seconds = parseInt($("st-clip").value, 10) || 20;
      s.samples = parseInt($("st-samples").value, 10) || 1;
      s.test_values = stTestValues();
      s.generate_screenshots = false; // Batch: keine Screenshot-Flut
      if (mode === "target_vmaf") s.target_vmaf = parseInt($("st-target").value, 10);
    } else if (mode === "fixed") {
      s.rate_mode = rateMode;
      s.quality = rateMode === "cq"
        ? parseInt($("st-quality").value, 10) : parseInt($("st-bitrate").value, 10);
    }
    return s;
  }

  // Sammelt Pro-Datei-Spurauswahl, aber nur wo der Nutzer vom Standard
  // (alle Spuren) abgewichen ist – sonst greift das globale Verhalten.
  function stCollectPerFile(paths) {
    const out = {};
    paths.forEach((p) => {
      const m = (state.stMatches || {})[p];
      if (!m) return;
      const entry = {};
      const audio = (m.audio || []).map((t) => t.index);
      const subs = (m.subtitles || []).map((t) => t.index);
      const selA = state.stTracks && state.stTracks[p] && state.stTracks[p].audio;
      const selS = state.stTracks && state.stTracks[p] && state.stTracks[p].subs;
      if (selA && audio.length && selA.size !== audio.length) {
        entry.audio_tracks = audio.filter((i) => selA.has(i));
      }
      if (selS && subs.length && selS.size !== subs.length) {
        entry.subtitle_tracks = subs.filter((i) => selS.has(i));
      }
      if (Object.keys(entry).length) out[p] = entry;
    });
    return out;
  }

  function stWarnThreshold() {
    const el = $("st-warn-count");
    const v = el ? parseInt(el.value, 10) : 40;
    return isNaN(v) ? 40 : Math.max(0, v);
  }

  // Warnungen vor dem Start: sehr viele Dateien bzw. sehr heterogene Qualität
  // (eine gemeinsame Einstellung passt dann evtl. nicht für alle).
  function stBatchWarnings(paths) {
    const tt = (s) => (window.I18N ? I18N.t(s) : s);
    const warns = [];
    const threshold = stWarnThreshold();
    if (threshold > 0 && paths.length >= threshold) {
      warns.push(`${paths.length} ${tt("Dateien ausgewählt – das kann sehr lange dauern und viel Speicher belegen.")}`);
    }
    const remux = $("st-remux-only") && $("st-remux-only").checked;
    const ms = paths.map((p) => (state.stMatches || {})[p]).filter(Boolean);
    if (!remux && ms.length >= 2) {
      const tier = (h) => (h <= 576 ? 0 : h <= 720 ? 1 : h <= 1080 ? 2 : h <= 1440 ? 3 : h <= 2160 ? 4 : 5);
      const tiers = new Set(ms.map((m) => tier(m.height || 0)));
      const bpps = ms.map((m) => {
        const [w, h] = String(m.resolution || "0x0").split("x").map((n) => parseInt(n, 10) || 0);
        return w && h && m.video_bitrate ? m.video_bitrate / (w * h) : 0;
      }).filter((v) => v > 0);
      const ratio = bpps.length >= 2 ? Math.max(...bpps) / Math.min(...bpps) : 1;
      const hdr = ms.some((m) => m.is_hdr);
      const sdr = ms.some((m) => !m.is_hdr);
      const dynamik = $("st-dynamik") ? $("st-dynamik").value : "auto";
      if (tiers.size >= 3 || ratio >= 4) {
        warns.push(tt("Die Auswahl enthält sehr unterschiedliche Qualitäten/Auflösungen. Eine einzige feste Einstellung liefert dann uneinheitliche Ergebnisse – „Ziel-VMAF (pro Datei)\" passt sich besser an."));
      }
      if (hdr && sdr && dynamik !== "auto") {
        warns.push(tt("HDR- und SDR-Dateien gemischt, aber die Dynamik ist fest eingestellt. „Automatisch je Datei\" behandelt jede Datei korrekt."));
      }
    }
    return warns;
  }

  async function startSuperBatch() {
    const paths = [...document.querySelectorAll(".st-check:checked")].map((c) => c.value);
    if (!paths.length) return;
    const warns = stBatchWarnings(paths);
    if (warns.length) {
      const msg = tt("Bitte prüfen, bevor der Stapel startet:") + "\n\n• " +
        warns.join("\n\n• ") + "\n\n" + tt("Trotzdem fortfahren?");
      if (!window.confirm(msg)) return;
    }
    const settings = stGatherSettings();
    const estimates = {};
    paths.forEach((p) => {
      const m = (state.stMatches || {})[p];
      if (m) {
        estimates[p] = {
          est_saved_bytes: m.est_saved_bytes || 0,
          est_output_bytes: m.est_output_bytes || 0,
        };
      }
    });
    // Dry-Run über Super-Tool-API (inkl. Remux-Plan), Fallback /api/preview.
    let preview;
    try {
      const dry = await (await fetch("/api/supertool/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paths, mode: $("st-mode").value, settings,
          per_file: stCollectPerFile(paths), dry_run: true,
        }),
      })).json();
      preview = dry.preview || dry;
    } catch (e) {
      preview = await fetchPreview(paths, settings, estimates);
    }
    if (!(await showPreviewModal(preview))) return;

    const btn = $("btn-st-start");
    btn.disabled = true;
    try {
      const res = await fetch("/api/supertool/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paths, mode: $("st-mode").value,
          settings, per_file: stCollectPerFile(paths),
        }),
      });
      const data = await res.json();
      if (data.error) {
        $("st-progress").textContent = data.error;
      } else {
        state.superBatch = data.group_id;
        $("st-progress").textContent = `${data.added} Datei(en) eingereiht.`;
        $("st-dash").style.display = "";
        pollSuperStatus();
      }
    } catch (e) {
      $("st-progress").textContent = "Fehler: " + e;
    } finally {
      btn.disabled = false;
    }
  }

  async function pollSuperStatus() {
    if (!state.superBatch || !$("st-dash-body")) return;
    try {
      const d = await (await fetch(
        `/api/supertool/status?batch_id=${encodeURIComponent(state.superBatch)}`)).json();
      renderSuperDash(d.items || []);
    } catch (e) { /* ignorieren */ }
    if (superStatusPoll) clearTimeout(superStatusPoll);
    if (state.currentPage === "supertool") superStatusPoll = setTimeout(pollSuperStatus, 2500);
  }

  function renderSuperDash(items) {
    const grid = $("st-dash-grid");
    if (grid) {
      const done = items.filter((i) => i.status === "fertig").length;
      const saved = items.reduce((a, i) => a + (i.saved_bytes > 0 ? i.saved_bytes : 0), 0);
      const failed = items.filter((i) => i.status === "fehlgeschlagen" || i.status === "abgebrochen").length;
      const cards = [
        ["Dateien", items.length], ["Fertig", done],
        ["Eingespart", formatBytes(saved)], ["Fehler", failed],
      ];
      grid.innerHTML = cards.map(([l, v]) =>
        `<div class="stat-box"><span class="stat-val">${escapeHtml(String(v))}</span><span class="stat-lbl">${escapeHtml(l)}</span></div>`).join("");
    }
    const body = $("st-dash-body");
    if (!body) return;
    body.innerHTML = items.length ? items.map((it) => `
      <tr>
        <td title="${escapeHtml(it.path)}">${escapeHtml(it.title)}</td>
        <td class="status-cell">${statusBadge(it.status)}</td>
        <td>${settingsLabel(it)}</td>
        <td>${it.duration_human || "—"}</td>
        <td class="good">${it.saved_human}</td>
      </tr>`).join("") :
      '<tr class="empty-row"><td colspan="5">Noch nichts.</td></tr>';
  }

  /* --------------------------------------------------- AUDIO-OPTIMIERUNG */
  let audioScanPoll = null;

  function initAudioOpt() {
    const scan = $("btn-audio-scan");
    if (scan) scan.addEventListener("click", audioStartScan);
    const start = $("btn-audio-start");
    if (start) start.addEventListener("click", audioStart);
    const all = $("audio-check-all");
    if (all) all.addEventListener("change", () => {
      document.querySelectorAll(".audio-pick").forEach((c) => { c.checked = all.checked; });
      audioSyncStart();
    });
  }

  function audioSettings() {
    return {
      audio_codec: $("audio-codec").value,
      audio_channels: parseInt($("audio-channels").value, 10) || 0,
      audio_bitrate: parseInt($("audio-bitrate").value, 10) || 0,
      scope: $("audio-scope").value,
      min_bitrate_kbps: parseInt($("audio-min-br").value, 10) || 700,
      audio_normalize: $("audio-normalize").checked,
      post_processing: $("audio-post").value,
    };
  }

  let auBrowser = null;
  function auLoadDir(path) {
    if (!auBrowser) {
      auBrowser = makeFolderBrowser({
        listId: "audio-browser", crumbId: "audio-breadcrumb", kind: "video",
        showFiles: false,
        searchPlaceholder: "Unterordner filtern …",
        onNavigate: (data, p) => {
          const fld = $("audio-folder"); if (fld) fld.value = p;
          const info = $("audio-folder-info");
          if (info) info.textContent = p ? `Aktuell: /${p} (inkl. Unterordner)`
            : "Aktuell: gesamter Eingabeordner (alle Unterordner)";
        },
      });
    }
    return auBrowser ? auBrowser.go(path) : undefined;
  }

  /* ---------------------------------------------- REMUX & BEARBEITEN */
  const RX_MP4_AUDIO_COPY = new Set(["aac", "ac3", "eac3", "mp3", "opus", "alac", "ac4"]);
  const RX_IMAGE_SUBS = new Set(["hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub", "pgs"]);
  const RX_EXT_IMAGE = new Set(["sup", "pgs", "idx", "sub"]);

  function remuxInit() {
    const cont = $("remux-container");
    if (cont) cont.addEventListener("change", () => { if (state.remuxInfo) remuxRenderEditor(); });
    const on = (id, fn) => { const b = $(id); if (b) b.addEventListener("click", fn); };
    on("btn-remux-add-ext", () => remuxOpenPicker("ext", "aux"));
    on("btn-remux-upload", () => { const inp = $("remux-upload-input"); if (inp) inp.click(); });
    const upIn = $("remux-upload-input");
    if (upIn) upIn.addEventListener("change", () => {
      const f = upIn.files && upIn.files[0];
      if (f) remuxUploadExternal(f);
      upIn.value = "";  // gleiche Datei erneut wählbar machen
    });
    on("btn-remux-add-att", () => remuxOpenPicker("att", "att"));
    on("btn-remux-sidecar", remuxAddSidecarAttachments);
    on("btn-remux-smart", remuxSmartDisposition);
    on("btn-remux-extract", remuxExtract);
    on("btn-remux-load-chapters", remuxLoadChapters);
    on("btn-remux-import-chapters", () => remuxOpenPicker("chapters", "aux"));
    on("btn-remux-start", remuxStart);
    on("btn-remux-clear", remuxClearSelection);
    on("btn-merge-add", () => remuxOpenPicker("merge", "aux"));
    on("btn-merge-start", remuxMergeStart);
    on("btn-merge-check", remuxMergeCheck);
    const unify = $("merge-unify");
    if (unify) unify.addEventListener("change", () => {
      const el = $("merge-encode-opts");
      if (el) el.style.display = unify.checked ? "" : "none";
    });
    on("btn-split-start", remuxSplitStart);
    on("btn-split-range-add", () => { state.splitRanges.push({ start: "", end: "", title: "" }); remuxRenderSplitRanges(); });
    on("btn-split-download", remuxCutDownload);
    on("btn-split-preview", remuxOpenPreview);
    const sm = $("split-mode");
    if (sm) sm.addEventListener("change", remuxSyncSplitMode);
    const chSel = $("split-range-chapter");
    if (chSel) chSel.addEventListener("change", () => {
      const idx = parseInt(chSel.value, 10);
      const chaps = state.remuxChapters || [];
      if (!isNaN(idx) && chaps[idx]) {
        const c = chaps[idx];
        state.splitRanges.push({ start: fmtClock(c.start), end: fmtClock(c.end), title: c.title || "" });
        remuxRenderSplitRanges();
      }
      chSel.value = "";
    });
    remuxSyncSplitMode();
    remuxLoadDir("");
  }

  // Zeigt je Split-Methode das passende Eingabefeld.
  function remuxSyncSplitMode() {
    const m = ($("split-mode") || {}).value || "chapters";
    const show = (id, on) => { const el = $(id); if (el) el.style.display = on ? "" : "none"; };
    show("split-dur-field", m === "duration");
    show("split-parts-field", m === "parts");
    show("split-times-field", m === "times");
    show("split-size-field", m === "size");
    show("split-range-field", m === "range");
  }

  function remuxRenderSplitRanges() {
    const body = $("split-range-body");
    if (!body) return;
    body.innerHTML = "";
    if (!state.splitRanges.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="4">Keine Bereiche.</td></tr>';
      return;
    }
    state.splitRanges.forEach((r, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><input type="text" class="rx-r-start" data-i="${i}" value="${escapeHtml(r.start || "")}" placeholder="00:00:00" style="width:90px"></td>
        <td><input type="text" class="rx-r-end" data-i="${i}" value="${escapeHtml(r.end || "")}" placeholder="00:10:00" style="width:90px"></td>
        <td><input type="text" class="rx-r-title" data-i="${i}" value="${escapeHtml(r.title || "")}"></td>
        <td><button class="btn btn-ghost btn-sm bad-btn rx-r-del" data-i="${i}">✕</button></td>`;
      body.appendChild(tr);
    });
    const sync = () => state.splitRanges.forEach((r, i) => {
      const g = (c) => document.querySelector(`.${c}[data-i="${i}"]`);
      if (g("rx-r-start")) r.start = g("rx-r-start").value.trim();
      if (g("rx-r-end")) r.end = g("rx-r-end").value.trim();
      if (g("rx-r-title")) r.title = g("rx-r-title").value.trim();
    });
    body.querySelectorAll("input").forEach((el) => el.addEventListener("input", sync));
    body.querySelectorAll(".rx-r-del").forEach((b) =>
      b.addEventListener("click", () => { sync(); state.splitRanges.splice(parseInt(b.dataset.i, 10), 1); remuxRenderSplitRanges(); }));
  }

  // Ersten gültigen Bereich verlustfrei schneiden und direkt herunterladen.
  async function remuxCutDownload() {
    if (!state.remuxSel) { $("split-download-info").innerHTML = '<span class="bad">Erst oben eine Quelle wählen.</span>'; return; }
    const r = (state.splitRanges || []).find((x) => x.start && x.end);
    if (!r) { $("split-download-info").innerHTML = '<span class="bad">Bereich mit Start und Ende angeben (oben „+ Bereich" oder Vorschau nutzen).</span>'; return; }
    const info = $("split-download-info");
    info.textContent = "Schneide … (kann bei großen Dateien einen Moment dauern)";
    try {
      const res = await fetch("/api/remux/cut", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: state.remuxSel.path, start: r.start, end: r.end,
          container: $("remux-container").value,
        }),
      });
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try { msg = (await res.json()).error || msg; } catch (e) {}
        info.innerHTML = `<span class="bad">${escapeHtml(msg)}</span>`;
        return;
      }
      const blob = await res.blob();
      let fname = "ausschnitt." + $("remux-container").value;
      const cd = res.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="?([^"]+)"?/);
      if (m) fname = m[1];
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = fname;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      info.innerHTML = `<span class="good">Download gestartet (${fname}).</span>`;
    } catch (e) {
      info.innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  // Leichtgewichtige Vorschau: Video-Player mit Marker-Buttons für Start/Ende.
  // Nutzt die native Zeitleiste des Players (keine Wellenform).
  function remuxOpenPreview() {
    if (!state.remuxSel) { $("split-download-info").innerHTML = '<span class="bad">Erst oben eine Quelle wählen.</span>'; return; }
    const url = `/api/media?root=media&path=${encodeURIComponent(state.remuxSel.path)}`;
    openModal("Vorschau & Ausschnitt-Marker",
      videoHtml(url) +
      '<div class="preview-marks">' +
      '<div class="field-row">' +
      '<div class="field"><label>Start</label><input type="text" id="pv-start" placeholder="00:00:00" style="width:110px"></div>' +
      '<div class="field"><label>Ende</label><input type="text" id="pv-end" placeholder="00:10:00" style="width:110px"></div>' +
      '</div>' +
      '<div class="lib-actions">' +
      '<button class="btn btn-ghost btn-sm" id="pv-set-start">⇥ Start = aktuelle Position</button>' +
      '<button class="btn btn-ghost btn-sm" id="pv-set-end">Ende = aktuelle Position ⇤</button>' +
      '<button class="btn btn-ghost btn-sm" id="pv-add-range">Als Bereich übernehmen</button>' +
      '<button class="btn btn-primary btn-sm" id="pv-download">Diesen Ausschnitt herunterladen</button>' +
      '</div>' +
      '<span id="pv-info" class="muted" style="font-size:12px"></span>' +
      '<p class="hint" style="margin-top:6px">Position im Player anspringen, dann Start/Ende setzen. ' +
      'Spielt der Browser den Codec (HEVC/AV1) nicht ab, funktionieren die Marker per manueller Zeiteingabe trotzdem.</p>' +
      '</div>');
    const vid = document.querySelector("#app-modal-body video");
    const cur = () => (vid && vid.currentTime) ? fmtClock(vid.currentTime) : "0:00:00";
    const on = (id, fn) => { const b = $(id); if (b) b.addEventListener("click", fn); };
    on("pv-set-start", () => { $("pv-start").value = cur(); });
    on("pv-set-end", () => { $("pv-end").value = cur(); });
    on("pv-add-range", () => {
      const s = $("pv-start").value.trim(), e = $("pv-end").value.trim();
      if (!s || !e) { $("pv-info").innerHTML = '<span class="bad">Start und Ende setzen.</span>'; return; }
      state.splitRanges.push({ start: s, end: e, title: "" });
      remuxRenderSplitRanges();
      $("pv-info").innerHTML = '<span class="good">Bereich übernommen.</span>';
    });
    on("pv-download", async () => {
      const s = $("pv-start").value.trim(), e = $("pv-end").value.trim();
      if (!s || !e) { $("pv-info").innerHTML = '<span class="bad">Start und Ende setzen.</span>'; return; }
      $("pv-info").textContent = "Schneide …";
      try {
        const res = await fetch("/api/remux/cut", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: state.remuxSel.path, start: s, end: e, container: $("remux-container").value }),
        });
        if (!res.ok) {
          let msg = `HTTP ${res.status}`; try { msg = (await res.json()).error || msg; } catch (er) {}
          $("pv-info").innerHTML = `<span class="bad">${escapeHtml(msg)}</span>`; return;
        }
        const blob = await res.blob();
        let fname = "ausschnitt." + $("remux-container").value;
        const cd = res.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename="?([^"]+)"?/);
        if (m) fname = m[1];
        const u = URL.createObjectURL(blob);
        const a = document.createElement("a"); a.href = u; a.download = fname;
        document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(u);
        $("pv-info").innerHTML = `<span class="good">Download gestartet (${fname}).</span>`;
      } catch (er) {
        $("pv-info").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(er))}</span>`;
      }
    });
  }

  let remuxBrowser = null;
  function remuxLoadDir(path) {
    if (!remuxBrowser) {
      remuxBrowser = makeFolderBrowser({
        listId: "remux-browser", crumbId: "remux-breadcrumb", kind: "video",
        showFiles: true, playFile: true, pickFile: remuxSelectFile,
        onNavigate: (data, p) => { state.currentRemuxPath = p; },
      });
    }
    return remuxBrowser ? remuxBrowser.go(path) : undefined;
  }

  // Remux-Quellenauswahl aufheben und Editor zurücksetzen.
  function remuxClearSelection() {
    state.remuxSel = null;
    state.remuxInfo = null;
    state.remuxExt = [];
    state.remuxAtt = [];
    state.remuxChapters = null;
    state.splitRanges = [];
    const ed = $("remux-editor"); if (ed) ed.style.display = "none";
    const badge = $("remux-badge"); if (badge) badge.textContent = "Keine Datei";
    const info = $("remux-start-info"); if (info) info.textContent = "";
    const cw = $("remux-chapters-wrap"); if (cw) cw.style.display = "none";
    document.querySelectorAll("#remux-browser .row-item.selected").forEach((r) => r.classList.remove("selected"));
    ["btn-remux-start", "btn-split-start", "btn-remux-clear"].forEach((id) => {
      const b = $(id); if (b) b.disabled = true;
    });
    remuxRenderSplitRanges();
  }

  async function remuxSelectFile(f) {
    state.remuxSel = { path: f.rel, name: f.name };
    state.remuxExt = [];
    state.remuxAtt = [];
    state.remuxChapters = null;
    $("remux-chapters-wrap").style.display = "none";
    $("remux-chapters-info").textContent = "";
    const splitBtn = $("btn-split-start");
    if (splitBtn) splitBtn.disabled = false;
    const clr = $("btn-remux-clear");
    if (clr) clr.disabled = false;
    $("remux-badge").textContent = `${f.name} · analysiere …`;
    document.querySelectorAll("#remux-browser .row-item.selected").forEach((r) => r.classList.remove("selected"));
    try {
      const info = await (await fetch(`/api/probe?path=${encodeURIComponent(f.rel)}`)).json();
      if (info.error) { $("remux-badge").textContent = info.error; return; }
      state.remuxInfo = info;
      $("remux-badge").textContent = f.name;
      $("remux-editor").style.display = "";
      remuxRenderEditor();
    } catch (e) {
      $("remux-badge").textContent = `Analyse-Fehler: ${e}`;
    }
  }

  function remuxOrderBtns() {
    return '<button class="btn btn-ghost btn-sm iconbtn rx-up" title="Nach oben">↑</button>' +
           '<button class="btn btn-ghost btn-sm iconbtn rx-down" title="Nach unten">↓</button>';
  }

  function remuxMoveRow(tr, dir) {
    if (!tr) return;
    if (dir < 0 && tr.previousElementSibling) {
      tr.parentNode.insertBefore(tr, tr.previousElementSibling);
    } else if (dir > 0 && tr.nextElementSibling) {
      tr.parentNode.insertBefore(tr.nextElementSibling, tr);
    }
  }

  function remuxAudioCodecSelect(cls, sel) {
    const opts = [["eac3", "E-AC3"], ["ac3", "AC3"], ["aac", "AAC"], ["opus", "Opus"], ["flac", "FLAC"]];
    return `<select class="${cls}">` +
      opts.map(([v, l]) => `<option value="${v}"${v === sel ? " selected" : ""}>${l}</option>`).join("") +
      `</select>`;
  }

  function remuxRenderEditor() {
    const info = state.remuxInfo || {};
    // Audio-Tabelle
    const ab = $("remux-audio-body");
    ab.innerHTML = "";
    (info.audio || []).forEach((a) => {
      const tr = document.createElement("tr");
      tr.dataset.aindex = a.index;
      tr.innerHTML = `
        <td><input type="checkbox" class="rx-a-keep" checked></td>
        <td>#${a.index} · ${escapeHtml(a.codec)} ${a.channels || "?"}ch · ${escapeHtml(a.language || "und")} · ${escapeHtml(a.bitrate_human || "—")}${a.title ? " · " + escapeHtml(a.title) : ""}</td>
        <td><input type="checkbox" class="rx-a-default"${a.default ? " checked" : ""}></td>
        <td><input type="checkbox" class="rx-a-forced"${a.forced ? " checked" : ""}></td>
        <td><input type="text" class="rx-a-lang" value="${escapeHtml(a.language || "")}" size="4"></td>
        <td><input type="text" class="rx-a-title" value="${escapeHtml(a.title || "")}"></td>
        <td class="rx-tc-cell">
          <label class="check"><input type="checkbox" class="rx-a-tc"><span>→</span></label>
          ${remuxAudioCodecSelect("rx-a-codec", "eac3")}
          <input type="number" class="rx-a-br" value="640" min="64" max="1536" step="64" style="width:70px">
        </td>
        <td class="row-actions">${remuxOrderBtns()}</td>`;
      ab.appendChild(tr);
    });
    if (!(info.audio || []).length) ab.innerHTML = '<tr class="empty-row"><td colspan="8">Keine Tonspuren.</td></tr>';

    // Untertitel-Tabelle
    const sb = $("remux-sub-body");
    sb.innerHTML = "";
    (info.subtitles || []).forEach((s) => {
      const tr = document.createElement("tr");
      tr.dataset.sindex = s.index;
      tr.innerHTML = `
        <td><input type="checkbox" class="rx-s-keep" checked></td>
        <td>#${s.index} · ${escapeHtml(s.codec)} · ${escapeHtml(s.language || "und")}${s.title ? " · " + escapeHtml(s.title) : ""}</td>
        <td><input type="checkbox" class="rx-s-default"${s.default ? " checked" : ""}></td>
        <td><input type="checkbox" class="rx-s-forced"${s.forced ? " checked" : ""}></td>
        <td><input type="text" class="rx-s-lang" value="${escapeHtml(s.language || "")}" size="4"></td>
        <td><input type="text" class="rx-s-title" value="${escapeHtml(s.title || "")}"></td>
        <td class="row-actions">${remuxOrderBtns()}</td>`;
      sb.appendChild(tr);
    });
    if (!(info.subtitles || []).length) sb.innerHTML = '<tr class="empty-row"><td colspan="7">Keine Untertitel.</td></tr>';

    ["rx-a-keep", "rx-s-keep", "rx-a-tc"].forEach((c) =>
      document.querySelectorAll(`#remux-editor .${c}`).forEach((el) =>
        el.addEventListener("change", remuxUpdateConflicts)));
    document.querySelectorAll("#remux-audio-body .rx-up, #remux-audio-body .rx-down, #remux-sub-body .rx-up, #remux-sub-body .rx-down")
      .forEach((b) => b.addEventListener("click", (e) => {
        e.stopPropagation();
        remuxMoveRow(b.closest("tr"), b.classList.contains("rx-up") ? -1 : 1);
      }));

    remuxRenderExternals();
    remuxUpdateConflicts();
    $("btn-remux-start").disabled = false;
  }

  // "Encoden"-Zelle einer externen Tonspur (Checkbox + Codec/Bitrate/Kanäle).
  // Untertitel können nicht in Audio umgewandelt werden -> Platzhalter.
  function remuxExtEncCell(e, i) {
    if (e.type === "subtitle") return '<td class="muted">—</td>';
    const codecs = [["eac3", "E-AC3"], ["ac3", "AC3"], ["aac", "AAC"], ["opus", "Opus"], ["flac", "FLAC"]];
    const codec = e.codec || "eac3";
    const ch = e.channels || 0;
    const chOpts = [[0, "orig"], [2, "2.0"], [6, "5.1"], [8, "7.1"]];
    const dis = e.transcode ? "" : " disabled";
    return `<td class="rx-e-enc-cell">
      <label class="check" title="Diese Spur beim Remux encodieren"><input type="checkbox" class="rx-e-tc" data-i="${i}"${e.transcode ? " checked" : ""}></label>
      <select class="rx-e-codec" data-i="${i}"${dis}>${codecs.map(([v, l]) => `<option value="${v}"${v === codec ? " selected" : ""}>${l}</option>`).join("")}</select>
      <input type="number" class="rx-e-br" data-i="${i}" value="${e.bitrate || 640}" style="width:60px" title="kbit/s"${dis}>
      <select class="rx-e-ch" data-i="${i}"${dis}>${chOpts.map(([v, l]) => `<option value="${v}"${v === ch ? " selected" : ""}>${l}</option>`).join("")}</select>
    </td>`;
  }

  function remuxRenderExternals() {
    const eb = $("remux-ext-body");
    eb.innerHTML = "";
    if (!state.remuxExt.length) {
      eb.innerHTML = '<tr class="empty-row"><td colspan="10">Keine externen Spuren.</td></tr>';
      return;
    }
    state.remuxExt.forEach((e, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(e.name)}${e.desc ? `<div class="muted" style="font-size:11px">${escapeHtml(e.desc)}</div>` : ""}</td>
        <td>${e.type === "subtitle" ? "Untertitel" : "Ton"}</td>
        <td><input type="text" class="rx-e-lang" data-i="${i}" value="${escapeHtml(e.language || "")}" size="4"></td>
        <td><input type="text" class="rx-e-title" data-i="${i}" value="${escapeHtml(e.title || "")}"></td>
        <td><input type="number" class="rx-e-delay" data-i="${i}" value="${e.delay || 0}" step="0.1" style="width:70px"></td>
        <td><input type="checkbox" class="rx-e-default" data-i="${i}"${e.default ? " checked" : ""}></td>
        <td><input type="checkbox" class="rx-e-forced" data-i="${i}"${e.forced ? " checked" : ""}></td>
        ${remuxExtEncCell(e, i)}
        <td class="row-actions"><button class="btn btn-ghost btn-sm iconbtn rx-e-up" data-i="${i}" title="Nach oben">↑</button><button class="btn btn-ghost btn-sm iconbtn rx-e-down" data-i="${i}" title="Nach unten">↓</button></td>
        <td><button class="btn btn-ghost btn-sm bad-btn rx-e-del" data-i="${i}">✕</button></td>`;
      eb.appendChild(tr);
    });
    const flush = () => { remuxSyncExternalInputs(); remuxRenderExternals(); remuxUpdateConflicts(); };
    document.querySelectorAll(".rx-e-del").forEach((b) =>
      b.addEventListener("click", () => { state.remuxExt.splice(parseInt(b.dataset.i, 10), 1); flush(); }));
    document.querySelectorAll(".rx-e-up").forEach((b) =>
      b.addEventListener("click", () => { remuxArrMove(state.remuxExt, parseInt(b.dataset.i, 10), -1); flush(); }));
    document.querySelectorAll(".rx-e-down").forEach((b) =>
      b.addEventListener("click", () => { remuxArrMove(state.remuxExt, parseInt(b.dataset.i, 10), 1); flush(); }));
    // Encoden-Checkbox schaltet Codec/Bitrate/Kanäle der Zeile frei.
    document.querySelectorAll(".rx-e-tc").forEach((b) =>
      b.addEventListener("change", () => {
        const i = b.dataset.i;
        ["rx-e-codec", "rx-e-br", "rx-e-ch"].forEach((c) => {
          const el = document.querySelector(`.${c}[data-i="${i}"]`);
          if (el) el.disabled = !b.checked;
        });
        remuxSyncExternalInputs();
      }));
  }

  // Vor dem Neu-Rendern die editierten Werte aus dem DOM in den State übernehmen,
  // damit Reihenfolge-Änderungen nichts überschreiben.
  function remuxSyncExternalInputs() {
    state.remuxExt.forEach((e, i) => {
      const g = (c) => document.querySelector(`.${c}[data-i="${i}"]`);
      if (g("rx-e-lang")) e.language = g("rx-e-lang").value.trim();
      if (g("rx-e-title")) e.title = g("rx-e-title").value.trim();
      if (g("rx-e-delay")) e.delay = parseFloat(g("rx-e-delay").value) || 0;
      if (g("rx-e-default")) e.default = g("rx-e-default").checked;
      if (g("rx-e-forced")) e.forced = g("rx-e-forced").checked;
      if (g("rx-e-tc")) e.transcode = g("rx-e-tc").checked;
      if (g("rx-e-codec")) e.codec = g("rx-e-codec").value;
      if (g("rx-e-br")) e.bitrate = parseInt(g("rx-e-br").value, 10) || 640;
      if (g("rx-e-ch")) e.channels = parseInt(g("rx-e-ch").value, 10) || 0;
    });
  }

  function remuxArrMove(arr, i, dir) {
    const j = i + dir;
    if (j < 0 || j >= arr.length) return;
    const t = arr[i]; arr[i] = arr[j]; arr[j] = t;
  }

  const RX_PICK_TITLE = {
    ext: "Externe Ton-/Untertiteldatei auswählen",
    att: "Attachment (Font/Cover) auswählen",
    merge: "Datei zum Zusammenführen auswählen",
    chapters: "Kapiteldatei (NFO/Text) auswählen",
  };

  function remuxOpenPicker(mode, kind) {
    state.remuxPick = { mode, kind: kind || "aux" };
    openModal(RX_PICK_TITLE[mode] || "Datei auswählen",
      '<div class="breadcrumb" id="remux-ext-breadcrumb"></div>' +
      '<div class="browser browser-sm" id="remux-ext-browser"><div class="browser-loading">Lade …</div></div>');
    // Frische Browser-Instanz fürs Modal (eigene History pro Öffnung).
    const picker = makeFolderBrowser({
      listId: "remux-ext-browser", crumbId: "remux-ext-breadcrumb",
      kind: kind || "aux", showFiles: true,
      searchPlaceholder: "Suchen … (Name)",
      pickFile: remuxPickChoose,
    });
    if (picker) picker.go("");
  }

  function remuxPickChoose(f) {
    const mode = (state.remuxPick || {}).mode;
    if (mode === "att") {
      state.remuxAtt.push({ path: f.rel, name: f.name });
      closeModal();
      remuxRenderAttachments();
    } else if (mode === "merge") {
      state.remuxMerge.push({ path: f.rel, name: f.name });
      closeModal();
      remuxRenderMerge();
    } else if (mode === "chapters") {
      closeModal();
      remuxImportChapters(f.rel || f.path);
    } else {
      remuxAddExternal(f);
    }
  }

  async function remuxSmartDisposition() {
    if (!state.remuxInfo) return;
    const audio = [];
    document.querySelectorAll("#remux-audio-body tr[data-aindex]").forEach((tr) => {
      const q = (c) => tr.querySelector("." + c);
      audio.push({
        index: parseInt(tr.dataset.aindex, 10),
        keep: q("rx-a-keep").checked,
        default: q("rx-a-default").checked,
        forced: q("rx-a-forced") ? q("rx-a-forced").checked : false,
        language: q("rx-a-lang").value.trim(),
        title: q("rx-a-title").value.trim(),
      });
    });
    const subs = [];
    document.querySelectorAll("#remux-sub-body tr[data-sindex]").forEach((tr) => {
      const q = (c) => tr.querySelector("." + c);
      subs.push({
        index: parseInt(tr.dataset.sindex, 10),
        keep: q("rx-s-keep").checked,
        default: q("rx-s-default").checked,
        forced: q("rx-s-forced").checked,
        language: q("rx-s-lang").value.trim(),
        title: q("rx-s-title").value.trim(),
      });
    });
    const prefer = [];
    const al = $("st-audio-langs");
    if (al && al.value.trim()) prefer.push(...al.value.split(/[,;\s]+/).filter(Boolean));
    try {
      const d = await (await fetch("/api/remux/smart-disposition", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audio, subtitles: subs, prefer_langs: prefer }),
      })).json();
      (d.audio || []).forEach((a) => {
        const tr = document.querySelector(`#remux-audio-body tr[data-aindex="${a.index}"]`);
        if (!tr) return;
        const def = tr.querySelector(".rx-a-default");
        if (def) def.checked = !!a.default;
      });
      (d.subtitles || []).forEach((s) => {
        const tr = document.querySelector(`#remux-sub-body tr[data-sindex="${s.index}"]`);
        if (!tr) return;
        const def = tr.querySelector(".rx-s-default");
        const fr = tr.querySelector(".rx-s-forced");
        if (def) def.checked = !!s.default;
        if (fr) fr.checked = !!s.forced;
      });
      $("remux-start-info").textContent = tt("Default/Forced intelligent gesetzt.");
    } catch (e) {
      $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(String(e))}</span>`;
    }
  }

  async function remuxAddSidecarAttachments() {
    if (!state.remuxSel) return;
    try {
      const d = await (await fetch(
        `/api/remux/sidecar-attachments?path=${encodeURIComponent(state.remuxSel.path)}`)).json();
      const list = d.attachments || [];
      if (!list.length) {
        $("remux-start-info").textContent = tt("Keine Sidecar-Attachments gefunden.");
        return;
      }
      const have = new Set(state.remuxAtt.map((a) => a.path));
      list.forEach((a) => {
        if (!have.has(a.path)) state.remuxAtt.push({ path: a.path, name: a.name });
      });
      remuxRenderAttachments();
      $("remux-start-info").textContent = `${list.length} ${tt("Sidecar-Attachment(s) hinzugefügt.")}`;
    } catch (e) {
      $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(String(e))}</span>`;
    }
  }

  function remuxApplyChapters(chapters) {
    state.remuxChapters = chapters || [];
    const body = $("remux-chapters-body");
    body.innerHTML = "";
    if (!state.remuxChapters.length) {
      $("remux-chapters-info").textContent = tt("Keine Kapitel vorhanden.");
      $("remux-chapters-wrap").style.display = "none";
      state.remuxChapters = null;
      return;
    }
    state.remuxChapters.forEach((c, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${i + 1}</td><td>${fmtClock(c.start)}</td>` +
        `<td><input type="text" class="rx-ch-title" data-i="${i}" value="${escapeHtml(c.title || "")}" style="width:100%"></td>`;
      body.appendChild(tr);
    });
    $("remux-chapters-wrap").style.display = "";
    $("remux-chapters-info").textContent = `${state.remuxChapters.length} ${tt("Kapitel – Titel bearbeitbar.")}`;
  }

  async function remuxImportChapters(path) {
    if (!path) return;
    $("remux-chapters-info").textContent = tt("Importiere Kapitel …");
    try {
      const d = await (await fetch("/api/remux/import-chapters", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      })).json();
      if (d.error) {
        $("remux-chapters-info").innerHTML = `<span class="bad">${escapeHtml(d.error)}</span>`;
        return;
      }
      remuxApplyChapters(d.chapters || []);
    } catch (e) {
      $("remux-chapters-info").innerHTML = `<span class="bad">${escapeHtml(String(e))}</span>`;
    }
  }

  // Aus einem probe-Ergebnis externe Spuren in state.remuxExt übernehmen.
  // refPath ist der Pfad, unter dem die Datei im Backend aufgelöst wird
  // (rel-Pfad eines Input-Roots oder "upload:<name>" für PC-Uploads).
  function remuxAddStreamsFromProbe(data, refPath, name) {
    const streams = [];
    (data.audio || []).forEach((a) => streams.push({
      type: "audio", stream: a.index, src_codec: a.codec,
      desc: `Ton #${a.index} · ${a.codec} ${a.channels || "?"}ch · ${a.bitrate_human || "—"}`,
      language: a.language, title: a.title,
    }));
    (data.subtitles || []).forEach((s) => streams.push({
      type: "subtitle", stream: s.index, src_codec: s.codec,
      desc: `UT #${s.index} · ${s.codec}`,
      language: s.language, title: s.title,
    }));
    if (!streams.length) return 0;
    streams.forEach((st) => state.remuxExt.push({
      path: refPath, name: name, type: st.type, stream: st.stream, desc: st.desc,
      src_codec: st.src_codec || "",
      language: (st.language && st.language !== "und") ? st.language : "",
      title: st.title || "", delay: 0, default: false, forced: false,
      transcode: false, codec: "eac3", bitrate: 640, channels: 0,
    }));
    return streams.length;
  }

  async function remuxAddExternal(f) {
    closeModal();
    $("remux-start-info").textContent = `Analysiere ${f.name} …`;
    try {
      const data = await (await fetch(`/api/remux/probe?path=${encodeURIComponent(f.rel)}`)).json();
      if (data.error) { $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(data.error)}</span>`; return; }
      const n = remuxAddStreamsFromProbe(data, f.rel, f.name);
      if (!n) {
        $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(f.name)}: keine Ton-/Untertitelspuren gefunden.</span>`;
        return;
      }
      $("remux-start-info").textContent = `${n} Spur(en) aus ${f.name} hinzugefügt.`;
    } catch (e) {
      $("remux-start-info").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
      return;
    }
    remuxRenderExternals();
    remuxUpdateConflicts();
  }

  async function remuxUploadExternal(file) {
    $("remux-start-info").textContent = `Lade ${file.name} hoch …`;
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const resp = await fetch("/api/remux/upload", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`;
        return;
      }
      const n = remuxAddStreamsFromProbe(data, data.path, data.name || file.name);
      if (!n) {
        $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(data.name || file.name)}: keine Ton-/Untertitelspuren gefunden.</span>`;
        return;
      }
      $("remux-start-info").textContent = `${n} Spur(en) aus ${data.name || file.name} (Upload) hinzugefügt.`;
    } catch (e) {
      $("remux-start-info").innerHTML = `<span class="bad">Upload-Fehler: ${escapeHtml(String(e))}</span>`;
      return;
    }
    remuxRenderExternals();
    remuxUpdateConflicts();
  }

  function remuxGatherSpec() {
    const audio = [];
    document.querySelectorAll("#remux-audio-body tr[data-aindex]").forEach((tr) => {
      const q = (c) => tr.querySelector("." + c);
      audio.push({
        index: parseInt(tr.dataset.aindex, 10),
        keep: q("rx-a-keep").checked,
        default: q("rx-a-default").checked,
        forced: q("rx-a-forced").checked,
        language: q("rx-a-lang").value.trim(),
        title: q("rx-a-title").value.trim(),
        transcode: q("rx-a-tc").checked,
        codec: q("rx-a-codec").value,
        bitrate: parseInt(q("rx-a-br").value, 10) || 640,
      });
    });
    const subtitles = [];
    document.querySelectorAll("#remux-sub-body tr[data-sindex]").forEach((tr) => {
      const q = (c) => tr.querySelector("." + c);
      subtitles.push({
        index: parseInt(tr.dataset.sindex, 10),
        keep: q("rx-s-keep").checked,
        default: q("rx-s-default").checked,
        forced: q("rx-s-forced").checked,
        language: q("rx-s-lang").value.trim(),
        title: q("rx-s-title").value.trim(),
      });
    });
    // Externe: Werte aus DOM aktualisieren
    remuxSyncExternalInputs();
    const spec = {
      container: $("remux-container").value,
      keep_chapters: $("remux-keep-chapters").checked,
      keep_metadata: $("remux-keep-metadata").checked,
      keep_attachments: $("remux-keep-att").checked,
      audio, subtitles,
      external: state.remuxExt.map((e) => ({ ...e })),
      add_attachments: state.remuxAtt.map((a) => ({ path: a.path })),
    };
    const ts = parseFloat($("remux-trim-start").value) || 0;
    const te = parseFloat($("remux-trim-end").value) || 0;
    if (ts > 0 || te > 0) spec.trim = { start: ts, end: te };
    // Kapitel nur mitsenden, wenn geladen/bearbeitet (sonst greift keep_chapters).
    if (state.remuxChapters) {
      spec.chapters = state.remuxChapters.map((c, i) => ({
        start: c.start, end: c.end,
        title: (document.querySelector(`.rx-ch-title[data-i="${i}"]`) || {}).value || c.title,
      }));
    }
    return spec;
  }

  function remuxRenderAttachments() {
    const box = $("remux-att-list");
    if (!state.remuxAtt.length) { box.style.display = "none"; box.innerHTML = ""; return; }
    box.style.display = "";
    box.innerHTML = "Attachments: " + state.remuxAtt.map((a, i) =>
      `${escapeHtml(a.name)} <a href="#" data-i="${i}" class="rx-att-del">✕</a>`).join(" · ");
    box.querySelectorAll(".rx-att-del").forEach((el) =>
      el.addEventListener("click", (e) => {
        e.preventDefault();
        state.remuxAtt.splice(parseInt(el.dataset.i, 10), 1);
        remuxRenderAttachments();
      }));
  }

  async function remuxLoadChapters() {
    if (!state.remuxSel) return;
    $("remux-chapters-info").textContent = "Lade Kapitel …";
    try {
      const data = await (await fetch(`/api/remux/chapters?path=${encodeURIComponent(state.remuxSel.path)}`)).json();
      remuxApplyChapters(data.chapters || []);
      // Kapitel auch als Bereichsvorlage für den Ausschnitt-Export anbieten.
      const chSel = $("split-range-chapter");
      if (chSel && state.remuxChapters) {
        chSel.innerHTML = '<option value="">Kapitel als Bereich …</option>' +
          state.remuxChapters.map((c, i) =>
            `<option value="${i}">${i + 1}. ${escapeHtml(c.title || fmtClock(c.start))}</option>`).join("");
      }
    } catch (e) {
      $("remux-chapters-info").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  function fmtClock(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }

  async function remuxExtract() {
    if (!state.remuxInfo) return;
    const tracks = [];
    document.querySelectorAll("#remux-audio-body tr[data-aindex]").forEach((tr) => {
      if (tr.querySelector(".rx-a-keep").checked)
        tracks.push({ type: "audio", index: parseInt(tr.dataset.aindex, 10) });
    });
    document.querySelectorAll("#remux-sub-body tr[data-sindex]").forEach((tr) => {
      if (tr.querySelector(".rx-s-keep").checked)
        tracks.push({ type: "subtitle", index: parseInt(tr.dataset.sindex, 10) });
    });
    if (!tracks.length) { $("remux-start-info").innerHTML = '<span class="bad">Keine (behaltene) Spur zum Extrahieren.</span>'; return; }
    $("remux-start-info").textContent = "Extrahiere …";
    try {
      const data = await (await fetch("/api/remux/extract", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.remuxSel.path, tracks, ...outTargetVals("remux") }),
      })).json();
      if (data.error) { $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(data.error)}</span>`; return; }
      const n = (data.extracted || []).length;
      const errs = (data.errors || []).length ? ` (${data.errors.length} Fehler)` : "";
      $("remux-start-info").innerHTML = `<span class="good">${n} Spur(en) in den Ausgabeordner extrahiert${errs}.</span>`;
    } catch (e) {
      $("remux-start-info").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  function remuxRenderMerge() {
    const body = $("remux-merge-body");
    body.innerHTML = "";
    if (!state.remuxMerge.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="4">Keine Dateien.</td></tr>';
      $("btn-merge-start").disabled = true;
      return;
    }
    state.remuxMerge.forEach((m, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${i + 1}</td><td>${escapeHtml(m.name)}</td>` +
        `<td class="row-actions"><button class="btn btn-ghost btn-sm iconbtn mg-up" data-i="${i}">↑</button><button class="btn btn-ghost btn-sm iconbtn mg-down" data-i="${i}">↓</button></td>` +
        `<td><button class="btn btn-ghost btn-sm bad-btn mg-del" data-i="${i}">✕</button></td>`;
      body.appendChild(tr);
    });
    $("btn-merge-start").disabled = state.remuxMerge.length < 2;
    body.querySelectorAll(".mg-del").forEach((b) =>
      b.addEventListener("click", () => { state.remuxMerge.splice(parseInt(b.dataset.i, 10), 1); remuxRenderMerge(); }));
    body.querySelectorAll(".mg-up").forEach((b) =>
      b.addEventListener("click", () => { remuxArrMove(state.remuxMerge, parseInt(b.dataset.i, 10), -1); remuxRenderMerge(); }));
    body.querySelectorAll(".mg-down").forEach((b) =>
      b.addEventListener("click", () => { remuxArrMove(state.remuxMerge, parseInt(b.dataset.i, 10), 1); remuxRenderMerge(); }));
  }

  async function remuxMergeCheck() {
    if (state.remuxMerge.length < 2) {
      $("merge-check-result").innerHTML = '<span class="bad">Mindestens zwei Dateien wählen.</span>';
      return;
    }
    $("merge-check-result").textContent = "Prüfe Kompatibilität …";
    try {
      const data = await (await fetch("/api/remux/concat/check", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: state.remuxMerge.map((m) => m.path) }),
      })).json();
      if (data.error) { $("merge-check-result").innerHTML = `<span class="bad">${escapeHtml(data.error)}</span>`; return; }
      if (data.compatible) {
        $("merge-check-result").innerHTML = '<span class="good">✓ Dateien sind kompatibel – verlustfreies Zusammenführen möglich.</span>';
      } else {
        $("merge-check-result").innerHTML = '<span class="bad">⚠ Unterschiede gefunden:</span><br>' +
          (data.warnings || []).map(escapeHtml).join("<br>") +
          '<br><span class="muted">Für ein einheitliches Ergebnis „neu encodieren" aktivieren.</span>';
      }
    } catch (e) {
      $("merge-check-result").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  async function remuxMergeStart() {
    if (state.remuxMerge.length < 2) return;
    const unify = $("merge-unify") && $("merge-unify").checked;
    $("merge-info").textContent = "Wird eingereiht …";
    try {
      const data = await (await fetch("/api/remux/concat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paths: state.remuxMerge.map((m) => m.path),
          container: $("merge-container").value,
          chapters_at_joins: $("merge-chapters") ? $("merge-chapters").checked : false,
          unify: !!unify,
          platform: $("merge-platform") ? $("merge-platform").value : "cpu",
          codec: $("merge-codec") ? $("merge-codec").value : "av1",
          cq: $("merge-cq") ? (parseInt($("merge-cq").value, 10) || 30) : 30,
          ...outTargetVals("merge"),
        }),
      })).json();
      $("merge-info").innerHTML = data.error
        ? `<span class="bad">${escapeHtml(data.error)}</span>`
        : `<span class="good">${unify ? "Zusammenführen (Re-Encode)" : "Zusammenführen"} eingereiht.</span>`;
    } catch (e) {
      $("merge-info").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  async function remuxSplitStart() {
    if (!state.remuxSel) { $("split-info").innerHTML = '<span class="bad">Erst oben eine Quelle wählen.</span>'; return; }
    const mode = $("split-mode").value;
    let value = 0;
    if (mode === "duration") value = parseFloat($("split-value").value) || 0;
    else if (mode === "parts") value = parseInt($("split-parts").value, 10) || 0;
    else if (mode === "size") value = parseFloat($("split-size").value) || 0;
    const times = mode === "times"
      ? ($("split-times").value || "").split(",").map((s) => s.trim()).filter(Boolean) : [];
    let ranges = [];
    if (mode === "range") {
      ranges = (state.splitRanges || []).filter((r) => r.start && r.end);
      if (!ranges.length) { $("split-info").innerHTML = '<span class="bad">Mindestens einen Bereich mit Start und Ende angeben.</span>'; return; }
    }
    $("split-info").textContent = "Wird eingereiht …";
    try {
      const data = await (await fetch("/api/remux/split", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: state.remuxSel.path,
          mode, value, times, ranges,
          container: $("remux-container").value,
          ...outTargetVals("split"),
        }),
      })).json();
      $("split-info").innerHTML = data.error
        ? `<span class="bad">${escapeHtml(data.error)}</span>`
        : `<span class="good">${mode === "range" ? "Ausschnitt-Export" : "Splitten"} eingereiht.</span>`;
    } catch (e) {
      $("split-info").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  function remuxCheckConflicts(spec) {
    if (spec.container !== "mp4") return [];
    const out = [];
    const info = state.remuxInfo || {};
    const aMap = {}; (info.audio || []).forEach((a) => { aMap[a.index] = a; });
    const sMap = {}; (info.subtitles || []).forEach((s) => { sMap[s.index] = s; });
    spec.audio.forEach((a) => {
      if (!a.keep || a.transcode) return;
      const codec = ((aMap[a.index] || {}).codec || "").toLowerCase();
      if (!RX_MP4_AUDIO_COPY.has(codec))
        out.push(`Tonspur #${a.index} (${codec}) ist in MP4 nicht kopierbar – MKV wählen oder „Transcode" aktivieren.`);
    });
    spec.subtitles.forEach((s) => {
      if (!s.keep) return;
      const codec = ((sMap[s.index] || {}).codec || "").toLowerCase();
      if (RX_IMAGE_SUBS.has(codec))
        out.push(`Untertitel #${s.index} (${codec}) ist ein Bild-Untertitel und in MP4 nicht möglich (MKV wählen).`);
    });
    spec.external.forEach((e) => {
      const suf = (e.name.split(".").pop() || "").toLowerCase();
      if (e.type === "subtitle" && RX_EXT_IMAGE.has(suf))
        out.push(`Externer Bild-Untertitel „${e.name}“ ist in MP4 nicht möglich (MKV wählen).`);
    });
    return out;
  }

  function remuxUpdateConflicts() {
    if (!state.remuxInfo) return;
    const box = $("remux-conflicts");
    const conflicts = remuxCheckConflicts(remuxGatherSpec());
    if (conflicts.length) {
      box.style.display = "";
      box.innerHTML = "⚠ " + conflicts.map(escapeHtml).join("<br>");
    } else {
      box.style.display = "none";
      box.innerHTML = "";
    }
  }

  async function remuxStart() {
    if (!state.remuxSel) return;
    const spec = remuxGatherSpec();
    const conflicts = remuxCheckConflicts(spec);
    if (conflicts.length) {
      $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(conflicts[0])}</span>`;
      return;
    }
    // Original ersetzen: nur nach ausdrücklicher Bestätigung.
    if ($("remux-post").value === "inplace" &&
        !window.confirm("Original ersetzen?\n\n\"" + state.remuxSel.name +
          "\" wird nach erfolgreichem Remux durch die neue Datei ersetzt. " +
          "Bei aktiver \"sicherer Nachbehandlung\" nur, wenn die Ausgabe intakt ist.")) {
      return;
    }
    const remuxSettings = {
      suffix: $("remux-suffix").value.trim() || "_remux",
      name_pattern: $("remux-name-pattern")
        ? ($("remux-name-pattern").value.trim() || "{stem}{suffix}") : "{stem}{suffix}",
      on_duplicate: $("remux-on-duplicate") ? $("remux-on-duplicate").value : "ask",
      container: spec.container,
      post_processing: $("remux-post").value,
      ...outTargetVals("remux"),
    };
    if (!(await confirmDryRunOrDups([state.remuxSel.path], remuxSettings))) return;
    const btn = $("btn-remux-start");
    btn.disabled = true;
    $("remux-start-info").textContent = "Wird eingereiht …";
    try {
      const res = await fetch("/api/remux/enqueue", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: state.remuxSel.path,
          spec,
          container: spec.container,
          post_processing: $("remux-post").value,
          integrity_check: $("remux-integrity").checked,
          safe_replace: $("remux-safe").checked,
          suffix: remuxSettings.suffix,
          name_pattern: remuxSettings.name_pattern,
          on_duplicate: remuxSettings.on_duplicate,
          ...outTargetVals("remux"),
        }),
      });
      const data = await res.json();
      $("remux-start-info").innerHTML = data.error
        ? `<span class="bad">${escapeHtml(data.error)}</span>`
        : `<span class="good">Remux-Auftrag eingereiht (Warteschlange).</span>`;
    } catch (e) {
      $("remux-start-info").innerHTML = `<span class="bad">Fehler: ${escapeHtml(String(e))}</span>`;
    } finally {
      btn.disabled = false;
    }
  }

  async function audioStartScan() {
    const info = $("audio-scan-info");
    if (info) info.textContent = "Scan gestartet …";
    $("audio-results").innerHTML = '<tr class="empty-row"><td colspan="5">Scanne …</td></tr>';
    await fetch("/api/audio/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder: $("audio-folder").value.trim(),
        settings: audioSettings(),
      }),
    });
    if (audioScanPoll) clearTimeout(audioScanPoll);
    audioPollScan();
  }

  async function audioPollScan() {
    try {
      const d = await (await fetch("/api/audio/scan")).json();
      renderAudioScan(d);
      if (d.running) { audioScanPoll = setTimeout(audioPollScan, 1500); }
    } catch (e) { /* ignore */ }
  }

  function renderAudioScan(d) {
    const info = $("audio-scan-info");
    if (info) info.textContent = d.running
      ? `Scanne … ${d.scanned}/${d.total}`
      : `${(d.matched || []).length} Treffer · ca. ${d.total_saved_human} einsparbar`;
    const badge = $("audio-saved-badge");
    if (badge) badge.textContent = d.total_saved_human || "—";
    const body = $("audio-results");
    const files = d.matched || [];
    if (!files.length) {
      body.innerHTML = `<tr class="empty-row"><td colspan="5">${d.running ? "Scanne …" : "Keine optimierbaren Dateien."}</td></tr>`;
      audioSyncStart();
      return;
    }
    body.innerHTML = files.map((f) => `
      <tr>
        <td><input type="checkbox" class="audio-pick" value="${escapeHtml(f.path)}" checked /></td>
        <td title="${escapeHtml(f.path)}">${escapeHtml(f.name)}</td>
        <td class="muted" style="font-size:12px">${escapeHtml((f.tracks || []).join(", "))}</td>
        <td>${escapeHtml(f.size_human)}</td>
        <td class="good">${escapeHtml(f.est_saved_human)}</td>
      </tr>`).join("");
    body.querySelectorAll(".audio-pick").forEach((c) =>
      c.addEventListener("change", audioSyncStart));
    audioSyncStart();
  }

  function audioSyncStart() {
    const picked = [...document.querySelectorAll(".audio-pick:checked")];
    const btn = $("btn-audio-start");
    if (btn) btn.disabled = picked.length === 0;
    const info = $("audio-start-info");
    if (info) info.textContent = picked.length ? `${picked.length} Datei(en) ausgewählt` : "";
  }

  async function audioStart() {
    const paths = [...document.querySelectorAll(".audio-pick:checked")].map((c) => c.value);
    if (!paths.length) return;
    const btn = $("btn-audio-start");
    if (btn) { btn.disabled = true; btn.textContent = "Wird eingereiht …"; }
    try {
      const r = await (await fetch("/api/audio/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, settings: audioSettings() }),
      })).json();
      const info = $("audio-start-info");
      if (info) info.textContent = r.error ? r.error : `${r.added} Job(s) in der Warteschlange.`;
    } finally {
      if (btn) { btn.textContent = "Auswahl optimieren"; }
      audioSyncStart();
    }
  }

  /* --------------------------------------------------- A/B-VERGLEICHSPLAYER */
  function initAbCompare() {
    const load = $("btn-ab-load");
    if (!load) return;
    const va = $("ab-video-a"), vb = $("ab-video-b");
    const mediaUrl = (root, path) =>
      `/api/media?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`;

    load.addEventListener("click", () => {
      const pa = $("ab-path-a").value.trim(), pb = $("ab-path-b").value.trim();
      const badge = $("ab-badge");
      if (pa) va.src = mediaUrl($("ab-root-a").value, pa);
      if (pb) vb.src = mediaUrl($("ab-root-b").value, pb);
      if (badge) badge.textContent = "Geladen";
      va.load(); vb.load();
    });

    const synced = () => $("ab-sync").checked;
    const offset = () => parseFloat($("ab-offset").value) || 0;

    // B exakt auf A (+ Versatz) ziehen. Wird beim Suchen und laufend genutzt.
    const alignB = (force) => {
      if (!synced()) return;
      const t = Math.max(0, va.currentTime + offset());
      // Kleine, unhörbare Abweichungen nicht ständig „nachziehen" (Ruckeln),
      // aber nach einem Sprung präzise ausrichten.
      if (force || Math.abs(vb.currentTime - t) > 0.05) vb.currentTime = t;
    };

    // A ist Master; B folgt (mit Versatz).
    va.addEventListener("play", () => { if (synced()) vb.play().catch(() => {}); });
    va.addEventListener("pause", () => { if (synced()) vb.pause(); });
    va.addEventListener("ratechange", () => { vb.playbackRate = va.playbackRate; });
    // Beim Springen: sofort grob (seeking) und nach Abschluss exakt (seeked).
    // Keyframe-Suche in unterschiedlichen Containern kann sonst leicht driften.
    va.addEventListener("seeking", () => alignB(true));
    va.addEventListener("seeked", () => alignB(true));
    va.addEventListener("timeupdate", () => {
      const seek = $("ab-seek"), time = $("ab-time");
      if (va.duration) {
        if (seek) seek.value = String(Math.round((va.currentTime / va.duration) * 1000));
        if (time) time.textContent = fmtClock(va.currentTime) + " / " + fmtClock(va.duration);
      }
      // Laufende Drift korrigieren (engere Toleranz für sauberere Sync).
      if (synced() && Math.abs((vb.currentTime - offset()) - va.currentTime) > 0.15) {
        alignB(true);
      }
    });

    $("ab-play").addEventListener("click", () => {
      if (va.paused) { va.play().catch(() => {}); } else { va.pause(); }
    });
    $("ab-seek").addEventListener("input", (e) => {
      if (va.duration) va.currentTime = (parseInt(e.target.value, 10) / 1000) * va.duration;
    });

    const browse = (which) => {
      openFilePickerModal({
        title: `Video ${which.toUpperCase()} wählen`,
        onPick: (f) => {
          $("ab-path-" + which).value = f.rel;
          const rootEl = $("ab-root-" + which);
          if (rootEl) rootEl.value = "media";
        },
      });
    };
    const ba = $("btn-ab-browse-a");
    if (ba) ba.addEventListener("click", () => browse("a"));
    const bb = $("btn-ab-browse-b");
    if (bb) bb.addEventListener("click", () => browse("b"));
  }

  // Beide A/B-Videos pausieren (z. B. beim Verlassen der Seite).
  function pauseAbVideos() {
    ["ab-video-a", "ab-video-b"].forEach((id) => {
      const v = $(id);
      if (v) { try { v.pause(); } catch (e) {} }
    });
  }

  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(sec / 60), s = sec % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  /* ------------------------------------------------------------- DIAGNOSE */
  function initDiagnostics() {
    const btn = $("btn-diag-run");
    if (btn) btn.addEventListener("click", () => loadDiagnostics(false));
    const deep = $("btn-diag-deep");
    if (deep) deep.addEventListener("click", () => loadDiagnostics(true));
  }

  const DIAG_ICON = { ok: "✓", warn: "!", fail: "✗" };
  const DIAG_LABEL = { ok: "OK", warn: "Warnung", fail: "Fehler" };

  async function loadDiagnostics(deep) {
    const report = $("diag-report");
    const badge = $("diag-badge");
    const prog = $("diag-progress");
    if (prog) prog.textContent = deep ? "Encoder-Funktionstest läuft (kann etwas dauern) …" : "Prüfe …";
    if (report) report.innerHTML = `<div class="browser-loading">${deep ? "Encoder werden real getestet …" : "Selbsttest läuft …"}</div>`;
    try {
      const d = await (await fetch("/api/diagnostics" + (deep ? "?deep=1" : ""))).json();
      state.diagLoaded = true;
      if (badge) {
        badge.textContent = DIAG_LABEL[d.overall] || "—";
        badge.className = "badge diag-" + (d.overall || "ok");
      }
      renderDiagnostics(d);
      // Der Funktionstest aktualisiert die echten Encoder-Fähigkeiten -> Dropdowns
      // in VMAF/Encoding sofort nachziehen.
      if (deep) loadCapabilities();
    } catch (e) {
      if (report) report.innerHTML = `<div class="browser-loading">Fehler: ${escapeHtml(String(e))}</div>`;
    } finally {
      if (prog) prog.textContent = "";
    }
  }

  function renderDiagnostics(d) {
    const report = $("diag-report");
    if (!report) return;
    const sections = (d && d.sections) || [];
    report.innerHTML = sections.map((sec) => `
      <div class="diag-section">
        <div class="diag-sec-head diag-${sec.status}">
          <span class="diag-dot diag-${sec.status}">${DIAG_ICON[sec.status] || "?"}</span>
          ${escapeHtml(sec.title)}
        </div>
        ${sec.checks.map((c) => `
          <div class="diag-row">
            <span class="diag-dot diag-${c.status}">${DIAG_ICON[c.status] || "?"}</span>
            <span class="diag-name">${escapeHtml(c.name)}</span>
            <span class="diag-detail">${escapeHtml(c.detail || "")}</span>
          </div>`).join("")}
      </div>`).join("");
  }

  /* ------------------------------------------------------------------ INIT */
  function initParallel() {
    const sel = $("opt-parallel");
    if (!sel) return;
    fetch("/api/config/parallel").then((r) => r.json()).then((cfg) => {
      if (cfg.value) sel.value = String(cfg.value);
      const cap = cfg.capacity || {};
      const gpus = (cap.gpus || []).map((g) => `${g.name} (${g.encoders}×)`).join(", ");
      sel.title = `Empfohlen: ${cap.suggested_parallel || 1} · `
        + (gpus ? `GPUs: ${gpus}` : `CPU-Threads: ${cap.cpu_threads || "?"}`);
    }).catch(() => {});
    sel.addEventListener("change", () => {
      fetch("/api/config/parallel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: parseInt(sel.value, 10) }),
      }).catch(() => {});
    });
  }

  /* ------------------------------------------------------- BENACHRICHTIGUNG */
  async function initNotify() {
    const badge = $("notify-badge");
    if (!$("btn-notify-save")) return;
    try {
      const d = await (await fetch("/api/notify")).json();
      $("ntf-discord").value = d.discord_url || "";
      $("ntf-tg-chat").value = d.telegram_chat || "";
      $("ntf-webhook").value = d.webhook_url || "";
      $("ntf-on-done").checked = !!d.on_done;
      $("ntf-on-failed").checked = !!d.on_failed;
      if ($("ntf-tg-token")) $("ntf-tg-token").placeholder =
        d.telegram_token_set ? "gesetzt – leer lassen zum Beibehalten" : "Bot-Token";
      const active = d.discord_url || d.webhook_url || d.telegram_token_set;
      if (badge) { badge.textContent = active ? "Aktiv" : "Aus"; }
    } catch (e) { /* ignorieren */ }

    $("btn-notify-save").addEventListener("click", async () => {
      const body = {
        discord_url: $("ntf-discord").value.trim(),
        telegram_token: $("ntf-tg-token").value.trim(),
        telegram_chat: $("ntf-tg-chat").value.trim(),
        webhook_url: $("ntf-webhook").value.trim(),
        on_done: $("ntf-on-done").checked,
        on_failed: $("ntf-on-failed").checked,
      };
      await fetch("/api/notify", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      $("ntf-tg-token").value = "";
      initNotify();
    });
    $("btn-notify-test").addEventListener("click", async () => {
      await fetch("/api/notify/test", { method: "POST" });
      const b = $("btn-notify-test");
      const t = b.textContent; b.textContent = "Gesendet ✓";
      setTimeout(() => { b.textContent = t; }, 2000);
    });
  }

  /* ---------------------------------------------------------- WATCH-ORDNER */
  async function initApiKeys() {
    if (!$("btn-apikey-new")) return;
    const url = $("arr-webhook-url");
    if (url) url.value = `${location.origin}/api/v1/webhook/arr`;
    const load = async () => {
      try {
        const d = await (await fetch("/api/apikeys")).json();
        const badge = $("api-badge");
        if (badge) badge.textContent = d.any ? "Geschützt" : "Offen";
        const list = $("apikey-list");
        const items = d.file_keys || [];
        list.innerHTML = (d.env_count
          ? `<div class="apikey-row"><span class="muted">${d.env_count} Schlüssel via Env (API_KEYS)</span></div>` : "")
          + (items.length ? items.map((k) =>
            `<div class="apikey-row"><code>${escapeHtml(k.masked)}</code>` +
            `<button class="btn btn-ghost btn-sm" data-revoke="${k.index}">Widerrufen</button></div>`).join("")
            : '<div class="apikey-row muted">Keine gespeicherten Schlüssel.</div>');
        list.querySelectorAll("[data-revoke]").forEach((b) =>
          b.addEventListener("click", async () => {
            await fetch("/api/apikeys/revoke", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ index: parseInt(b.dataset.revoke, 10) }),
            });
            load();
          }));
      } catch (e) { /* ignorieren */ }
    };
    await load();
    $("btn-apikey-new").addEventListener("click", async () => {
      const r = await (await fetch("/api/apikeys/generate", { method: "POST" })).json();
      const el = $("apikey-new");
      if (el && r.key) {
        el.style.display = "";
        el.innerHTML = `Neuer Schlüssel (nur jetzt sichtbar): <code>${escapeHtml(r.key)}</code>`;
      }
      load();
    });
  }

  async function initScheduler() {
    if (!$("btn-sched-save")) return;
    const load = async () => {
      try {
        const d = await (await fetch("/api/scheduler")).json();
        $("sched-enabled").checked = !!d.enabled;
        $("sched-window").checked = !!d.window_enabled;
        $("sched-start").value = d.start_hour;
        $("sched-end").value = d.end_hour;
        $("sched-throttle").checked = !!d.throttle_enabled;
        $("sched-maxcpu").value = d.max_cpu_percent;
        const badge = $("sched-badge");
        if (badge) badge.textContent = d.enabled ? (d.active_now ? "Aktiv" : "Wartet") : "Aus";
        const st = $("sched-status");
        if (st) st.textContent = d.enabled
          ? (d.active_now ? "Encodes sind aktuell freigegeben." : `Pausiert: ${d.reason || "—"}`)
          : "Zeitplan deaktiviert – Encodes laufen jederzeit.";
      } catch (e) { /* ignorieren */ }
    };
    await load();
    $("btn-sched-save").addEventListener("click", async () => {
      const b = $("btn-sched-save"); const t = b.textContent; b.textContent = "Gespeichert";
      await fetch("/api/scheduler", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: $("sched-enabled").checked,
          window_enabled: $("sched-window").checked,
          start_hour: parseInt($("sched-start").value, 10) || 0,
          end_hour: parseInt($("sched-end").value, 10) || 0,
          throttle_enabled: $("sched-throttle").checked,
          max_cpu_percent: parseInt($("sched-maxcpu").value, 10) || 85,
        }),
      });
      await load();
      setTimeout(() => { b.textContent = t; }, 1500);
    });
  }

  async function initWatch() {
    if (!$("btn-watch-save")) return;
    // Profile-Dropdown befüllen (teilt sich die Liste mit den Encode-Profilen).
    const fillProfiles = (sel) => {
      const cur = $("wf-profile").value;
      $("wf-profile").innerHTML = '<option value="">Standard-Einstellungen</option>' +
        (state.profiles || []).map((p) =>
          `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join("");
      $("wf-profile").value = sel || cur || "";
    };

    const load = async () => {
      try {
        if (!state.profiles) {
          try { state.profiles = (await (await fetch("/api/profiles")).json()).profiles || []; }
          catch (e) { state.profiles = []; }
        }
        const d = await (await fetch("/api/watch")).json();
        $("wf-enabled").checked = !!d.enabled;
        $("wf-folder").value = d.folder || "";
        $("wf-interval").value = d.interval_min || 15;
        $("wf-start").value = (d.active_start === null || d.active_start === undefined) ? "" : d.active_start;
        $("wf-end").value = (d.active_end === null || d.active_end === undefined) ? "" : d.active_end;
        fillProfiles(d.profile || "");
        const badge = $("watch-badge");
        if (badge) badge.textContent = d.enabled ? "Aktiv" : "Aus";
        const st = $("wf-status");
        if (st) {
          const last = d.last_run ? new Date(d.last_run * 1000).toLocaleString() : "noch nie";
          st.textContent = `Letzte Prüfung: ${last} · zuletzt hinzugefügt: ${d.last_added || 0} · bekannt: ${d.processed_count || 0}`;
        }
      } catch (e) { /* ignorieren */ }
    };
    await load();

    const parseHour = (v) => v.trim() === "" ? null : parseInt(v, 10);
    const save = async () => {
      await fetch("/api/watch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: $("wf-enabled").checked,
          folder: $("wf-folder").value.trim(),
          interval_min: parseInt($("wf-interval").value, 10) || 15,
          profile: $("wf-profile").value,
          active_start: parseHour($("wf-start").value),
          active_end: parseHour($("wf-end").value),
        }),
      });
    };
    const browse = $("btn-wf-browse");
    if (browse) browse.addEventListener("click", () => openFolderPickerModal({
      title: "Watch-Ordner wählen", kind: "video",
      start: ($("wf-folder").value || "").trim(),
      onPick: (p) => { $("wf-folder").value = p; },
    }));
    $("btn-watch-save").addEventListener("click", async () => { await save(); await load(); });
    $("btn-watch-scan").addEventListener("click", async () => {
      const b = $("btn-watch-scan"); const t = b.textContent; b.textContent = "Prüfe …";
      await save();
      const d = await (await fetch("/api/watch/scan", { method: "POST" })).json();
      b.textContent = `+${d.added || 0} eingereiht`;
      await load(); updateQueue();
      setTimeout(() => { b.textContent = t; }, 2500);
    });
  }

  function initGlobalSearch() {
    const btn = $("btn-global-search");
    if (!btn) return;
    btn.addEventListener("click", openGlobalSearch);
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openGlobalSearch();
      }
    });
  }

  function openGlobalSearch() {
    openModal(tt("Globale Suche"), `
      <div class="field">
        <input type="search" id="gs-q" placeholder="${tt("Dateiname …")}" autofocus style="width:100%" />
      </div>
      <div id="gs-results" class="table-wrap" style="margin-top:10px;max-height:420px;overflow:auto">
        <p class="muted">${tt("Tippen zum Suchen (Medienordner).")}</p>
      </div>`);
    const inp = $("gs-q");
    let timer = null;
    const run = () => {
      clearTimeout(timer);
      timer = setTimeout(() => globalSearchRun(inp.value.trim()), 280);
    };
    if (inp) {
      inp.addEventListener("input", run);
      inp.focus();
    }
  }

  async function globalSearchRun(q) {
    const box = $("gs-results");
    if (!box) return;
    if (!q || q.length < 2) {
      box.innerHTML = `<p class="muted">${tt("Mindestens 2 Zeichen.")}</p>`;
      return;
    }
    box.innerHTML = `<p class="muted">${tt("Suche …")}</p>`;
    try {
      const d = await (await fetch(
        `/api/search?q=${encodeURIComponent(q)}&kind=video&limit=80`)).json();
      const files = d.files || [];
      if (!files.length) {
        box.innerHTML = `<p class="muted">${tt("Keine Treffer.")}</p>`;
        return;
      }
      box.innerHTML = `<table class="queue-table"><thead><tr>
        <th>${tt("Datei")}</th><th>${tt("Ordner")}</th><th></th></tr></thead><tbody>` +
        files.map((f) => {
          const path = f.rel || f.path || "";
          const name = f.name || path;
          const folder = f.folder || (path.includes("/") ? path.replace(/\/[^/]+$/, "") : "") || "—";
          return `<tr>
            <td title="${escapeHtml(path)}">${escapeHtml(name)}</td>
            <td class="muted">${escapeHtml(folder)}</td>
            <td class="row-actions">
              <button class="btn btn-ghost btn-sm gs-act" data-act="play" data-path="${escapeHtml(path)}" data-name="${escapeHtml(name)}">▶</button>
              <button class="btn btn-ghost btn-sm gs-act" data-act="encode" data-path="${escapeHtml(path)}" data-name="${escapeHtml(name)}">→E</button>
              <button class="btn btn-ghost btn-sm gs-act" data-act="remux" data-path="${escapeHtml(path)}" data-name="${escapeHtml(name)}">→R</button>
              <button class="btn btn-ghost btn-sm gs-act" data-act="vmaf" data-path="${escapeHtml(path)}" data-name="${escapeHtml(name)}">→V</button>
            </td></tr>`;
        }).join("") + `</tbody></table>` +
        (d.truncated ? `<p class="muted" style="margin-top:6px">${tt("Ergebnisse gekürzt.")}</p>` : "");
      box.querySelectorAll(".gs-act").forEach((b) => {
        b.addEventListener("click", async () => {
          const act = b.dataset.act, path = b.dataset.path, name = b.dataset.name;
          closeModal();
          if (act === "play") { openPlayer("media", path, name); return; }
          if (act === "remux") {
            navTo("remux");
            await remuxSelectFile({ rel: path, name });
            return;
          }
          await libTransfer(path, name, act === "vmaf" ? "vmaf" : "encode", null);
        });
      });
    } catch (e) {
      box.innerHTML = `<p class="bad">${escapeHtml(String(e))}</p>`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initSettings();
    initVmafTool();
    initSuperTool();
    initDataBrowser();
    initParallel();
    initVmafHistory();
    initNav();
    initProfiles();
    initStats();
    initLibrary();
    initNotify();
    initWatch();
    initScheduler();
    initApiKeys();
    initAbCompare();
    initAudioOpt();
    initDiagnostics();
    initOutTargets();
    initMediaSettings();
    initGlobalSearch();
    const sizeT = $("opt-size-target");
    if (sizeT) sizeT.addEventListener("change", refreshSizeTargetHint);
    if (sizeT) sizeT.addEventListener("input", () => {
      clearTimeout(sizeT._hintTimer);
      sizeT._hintTimer = setTimeout(refreshSizeTargetHint, 400);
    });
    loadCapabilities();
    // Haupt-Browser (Encoding/Quellenauswahl): Dateien wählbar, abspielbar,
    // Batch-Button + Library-Ordner werden über onNavigate aktualisiert.
    mainBrowser = makeFolderBrowser({
      listId: "browser", crumbId: "breadcrumb", kind: "video",
      showFiles: true, playFile: true, pickFile: selectFile,
      onNavigate: (data) => {
        state.currentPath = data.path || "";
        const folderBtn = $("btn-select-folder");
        if (folderBtn) {
          folderBtn.disabled = !!data.roots;
          folderBtn.onclick = data.roots ? null : () => selectFolder(data.path, data.is_root);
        }
      },
    });
    loadDir("");
    connectWs();
    fetch("/api/config/paths").then((r) => r.json()).then((p) => {
      const el = $("data-dir");
      if (el) el.textContent = p.data_dir;
    }).catch(() => {});
  });
})();
