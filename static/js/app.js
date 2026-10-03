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
    chartScene: null,  // null = Gesamtschnitt, sonst Szenenindex in der VMAF-Grafik
    vmafShown: null,   // zuletzt gezeichnete Analyse (für Szenen-Umschaltung)
    stVmafId: null,    // Super-Tool-Zeile für Vergleichsbilder
    superBatch: null,  // aktive Super-Tool-Stapelkennung
    vmafSource: null,  // Quelle des aktuell gezeigten VMAF-Vergleichs (für „→ Encoding")
    browseData: null,  // zuletzt geladener Ordnerinhalt (für Live-Filter)
    libRows: [],       // Bibliotheks-Treffer (für Sortierung/Filter/Gruppierung)
    libStats: null,    // Dashboard-Statistik des letzten Scans
    libSort: { key: "est_saved_bytes", dir: "desc" },
    libPage: 1,        // aktuelle Seite der Ergebnisliste
    libPageSize: 50,   // Einträge pro Seite
    libByRoot: {},     // gecachte Scans pro Root ("" = gesamter Medienbaum)
    libScanAll: [],    // aktueller Scan (Anzeige)
    libScanRoot: "",   // Root des aktuell angezeigten Scans
    libActiveScanRoot: "", // Root eines laufenden Scans (falls abweichend)
    libOpen: new Set(),    // aufgeklappte Datei-Details in der Liste
    libDetails: {},        // nachgeladene Ton/UT/NFO je Pfad
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
    if (state.currentPage === "player" && page !== "player"
        && typeof window.stopFullPlayer === "function") {
      window.stopFullPlayer();
    }
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
    if (page === "editor" && typeof window.editorInit === "function") window.editorInit();
    if (page === "diag" && !state.diagLoaded) loadDiagnostics();
    if ((page === "encode" || page === "vmaf") && state.bitrateData) drawBitrateChart(state.bitrateData);
  }
  window.navTo = navTo;

  function initNav() {
    const nav = $("nav");
    if (!nav) return;
    nav.querySelectorAll(".nav-item").forEach((b) =>
      b.addEventListener("click", () => navTo(b.dataset.nav)));
    navTo(localStorage.getItem("page") || "encode");
    document.addEventListener("click", (e) => {
      const a = e.target.closest("a.faq-jump");
      if (!a) return;
      e.preventDefault();
      const id = (a.getAttribute("href") || "").replace(/^#/, "");
      navTo("faq");
      setTimeout(() => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.tagName === "DETAILS") el.open = true;
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 40);
    });
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

  function colorWithAlpha(hex, a) {
    const h = (hex || "").trim();
    if (h[0] === "#" && (h.length === 7 || h.length === 4)) {
      const n = h.length === 4
        ? ("#" + h[1] + h[1] + h[2] + h[2] + h[3] + h[3]) : h;
      const r = parseInt(n.slice(1, 3), 16);
      const g = parseInt(n.slice(3, 5), 16);
      const b = parseInt(n.slice(5, 7), 16);
      return `rgba(${r},${g},${b},${a})`;
    }
    return hex;
  }

  /* ------------------------------------------------------------- BROWSER */
  // Wiederverwendbarer Ordner-Browser: Breadcrumb, rekursive Suche und
  // Zurück/Vor-Navigation mit gemerkter Scroll-Position. Wird von Encoding,
  // Super-Tool, Audio-Optimierung, Remux und den Datei-Pickern genutzt.
  //
  // opts: { listId, crumbId, kind, showFiles, recursive, playFile,
  //         pickFile(f), onNavigate(data, path), rootLabel, searchPlaceholder,
  //         browseUrl(path), searchUrl(path, q), playRoot,
  //         multiSelect, onSelectionChange(files) }
  function makeFolderBrowser(opts) {
    const listEl = $(opts.listId);
    if (!listEl) return null;
    const crumbEl = opts.crumbId ? $(opts.crumbId) : null;
    const kind = opts.kind || "video";
    const showFiles = opts.showFiles !== false;
    const multi = !!opts.multiSelect;
    const browseUrl = opts.browseUrl ||
      ((p) => `/api/browse?path=${encodeURIComponent(p)}&kind=${kind}`);
    const searchUrl = opts.searchUrl ||
      ((p, q) => `/api/search?path=${encodeURIComponent(p)}&q=${encodeURIComponent(q)}&kind=${kind}`);
    // Rekursive Suche nur, wenn eine Such-URL existiert (Standard: /api/search).
    const allowRecursive = showFiles && opts.recursive !== false && opts.searchUrl !== null;
    const S = {
      path: "", data: null, hist: [], hidx: -1, scroll: {}, timer: null,
      sel: new Map(),   // rel → Datei; Reihenfolge = Reihenfolge des Anhakens
      visible: [],      // aktuell gelistete Dateien (für „alle wählen")
      repaint: null,    // aktuelle Ansicht neu zeichnen (Ordner oder Suche)
    };

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
      if (data.error) {
        // Gemerkter Ordner verschwunden? Einmalig auf die Wurzel zurückfallen.
        if (opts.rootFallback && path && !o.noFallback) {
          return navigate("", { push: o.push, noFallback: true });
        }
        listEl.innerHTML = `<div class="browser-loading">${escapeHtml(data.error)}</div>`;
        return;
      }
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

    function selection() { return Array.from(S.sel.values()); }

    function selChanged() {
      if (opts.onSelectionChange) opts.onSelectionChange(selection());
    }

    function toggleSel(f, on) {
      if (on == null) on = !S.sel.has(f.rel);
      if (on) {
        S.sel.set(f.rel, {
          rel: f.rel, name: f.name, size_human: f.size_human || "", folder: f.folder || "",
        });
      } else {
        S.sel.delete(f.rel);
      }
      return on;
    }

    // Datei-Zeile: einfacher Klick wählt aus, im Multi-Modus hakt er an.
    function fileRow(f, label) {
      if (f.disc) {
        return makeRow("iso", label, f.size_human, () => go(f.rel), null);
      }
      // Titel in einem Abbild oder einer DVD: kein normaler Dateipfad, nur
      // auf der Remux-Seite wählbar, nicht abspielbar.
      const discTitle = f.bluray && f.bluray.source;
      if (discTitle && !opts.discPick) {
        return makeRow("file", label, f.size_human, null, null);
      }
      const playRel = discTitle ? null : (opts.playFile ? f.rel : null);
      if (!multi) {
        return makeRow("file", label, f.size_human, null,
          opts.pickFile ? () => opts.pickFile(f) : null,
          playRel, opts.playRoot || "media");
      }
      const row = makeRow("file", label, f.size_human, null, null,
        playRel, opts.playRoot || "media");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "row-sel";
      cb.title = "Zur Auswahl hinzufügen";
      const paint = (on) => {
        cb.checked = on;
        row.classList.toggle("selected", on);
      };
      paint(S.sel.has(f.rel));
      cb.addEventListener("click", (e) => {
        e.stopPropagation();
        paint(toggleSel(f, cb.checked));
        selChanged();
      });
      row.addEventListener("click", () => {
        paint(toggleSel(f));
        selChanged();
      });
      row.insertBefore(cb, row.firstChild);
      if (opts.pickFile) {
        const only = document.createElement("button");
        only.className = "row-pick";
        only.textContent = "Nur diese";
        only.title = "Diese Datei sofort übernehmen";
        only.addEventListener("click", (e) => { e.stopPropagation(); opts.pickFile(f); });
        row.appendChild(only);
      }
      return row;
    }

    function renderList() {
      const data = S.data;
      if (!data) return;
      S.repaint = renderList;
      listEl.innerHTML = "";
      const q = search.value.trim().toLowerCase();
      const match = (n) => !q || n.toLowerCase().includes(q);
      const dirs = (data.dirs || []).filter((d) => match(d.name));
      const files = showFiles ? (data.files || []).filter((f) => match(f.name)) : [];
      const titles = (!q && data.bluray && data.bluray.titles) ? data.bluray.titles : [];
      S.visible = files;
      if (!data.roots && !data.is_root && !q) {
        listEl.appendChild(makeRow("dir", "..", "", () => go(data.parent || ""), null));
      }
      if (titles.length && showFiles) {
        const isDvd = data.bluray.kind === "dvd";
        const head = document.createElement("div");
        head.className = "browser-section";
        head.textContent = tt(isDvd ? "DVD" : "Blu-ray");
        listEl.appendChild(head);
        const hint = document.createElement("div");
        hint.className = "browser-section-hint";
        const onlyTitles = data.bluray.iso || isDvd;
        hint.textContent = onlyTitles
          ? (opts.discPick
            ? tt(isDvd
              ? "Der längste Titel ist der Hauptfilm. Die übrigen Titel bleiben wählbar. Kapitel und Sprachen kommen von der Disc."
              : "Die längste Playlist ist der Hauptfilm. Die übrigen Titel bleiben wählbar.")
            : tt("Zum Remuxen unter Remux & Bearbeiten öffnen."))
          : tt("Die längste Playlist ist der Hauptfilm. Die übrigen Titel bleiben wählbar. Darunter liegen die einzelnen M2TS.");
        listEl.appendChild(hint);
        titles.forEach((t) => listEl.appendChild(fileRow(discTitleFile(t), discTitleLabel(t))));
      }
      dirs.forEach((d) => listEl.appendChild(makeRow("dir", d.name, "", () => go(d.rel), null)));
      files.forEach((f) => listEl.appendChild(fileRow(f, f.name)));
      if (!dirs.length && !files.length && !titles.length) {
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
        const paint = () => {
          listEl.innerHTML = "";
          (data.files || []).forEach((f) => {
            const label = f.folder ? `${f.name}  ·  ${f.folder}/` : f.name;
            listEl.appendChild(fileRow(f, label));
          });
          if (!(data.files || []).length) listEl.innerHTML = '<div class="browser-loading">Keine Treffer.</div>';
        };
        S.visible = data.files || [];
        S.repaint = paint;
        paint();
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
    return {
      go,
      current: () => S.path,
      refresh: () => navigate(S.path, { push: false, restore: true }),
      selection,
      selectVisible: (on) => {
        (S.visible || []).forEach((f) => toggleSel(f, on));
        if (S.repaint) S.repaint();
        selChanged();
      },
      clearSelection: () => {
        S.sel.clear();
        if (S.repaint) S.repaint();
        selChanged();
      },
    };
  }

  // Disc-Titel (Blu-ray-Playlist oder DVD-Titel) als Datei-Eintrag für den Browser.
  function discTitleLabel(t) {
    const role = tt(t.role === "main" ? "Hauptfilm" : "Weiterer Titel");
    const parts = [role, t.playlist, t.duration_human];
    if (t.dvd_title) {
      if (t.chapter_count) parts.push(`${t.chapter_count} ${tt("Kapitel")}`);
    } else {
      parts.push(`${(t.clips || []).length}×`);
    }
    return parts.join(" · ");
  }
  function discTitleFile(t) {
    return {
      rel: (t.clips || [])[0] || "", name: discTitleLabel(t),
      size_human: t.size_human || "", bluray: t,
    };
  }

  // Instanzen der wiederverwendbaren Browser (einmalig erzeugt).
  let mainBrowser = null;
  function loadDir(path) { return mainBrowser ? mainBrowser.go(path) : undefined; }

  function makeRow(type, name, size, onOpen, onPick, playRel, playRoot) {
    const row = document.createElement("div");
    row.className = "row-item";
    const icon = type === "dir" ? "📁" : type === "iso" ? "💿" : "🎬";
    row.innerHTML = `
      <span class="row-icon ${type}">${icon}</span>
      <span class="row-name">${escapeHtml(name)}</span>
      <span class="row-size">${size}</span>`;
    if (onOpen) row.addEventListener("click", onOpen);
    if (playRel) {
      const play = document.createElement("button");
      play.className = "row-play";
      play.title = "Im Player öffnen";
      play.textContent = "▶";
      play.addEventListener("click", (e) => {
        e.stopPropagation();
        playMedia(playRoot || "media", playRel, name);
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
    const memKey = opts.rememberKey === null ? "" : (opts.rememberKey || "fpickFolderDir");
    const picker = makeFolderBrowser({
      listId: "fp-browser", crumbId: "fp-crumb",
      kind: opts.kind || "video", showFiles: false,
      searchPlaceholder: "Unterordner filtern …",
      rootFallback: true,
      onNavigate: (data, p) => {
        current = p;
        rememberPickerDir(memKey, p);
        const sel = $("fp-sel");
        if (sel) sel.textContent = p ? `Auswahl: /${p}` : "Auswahl: (Wurzel/gesamt)";
      },
    });
    const choose = $("fp-choose");
    if (choose) choose.addEventListener("click", () => {
      closeModal();
      if (opts.onPick) opts.onPick(current);
    });
    if (picker) picker.go(opts.start || pickerDir(memKey));
  }

  // Zuletzt besuchter Ordner der Datei-/Ordner-Dialoge.
  function pickerDir(key) {
    if (!key) return "";
    try { return localStorage.getItem(key) || ""; } catch (e) { return ""; }
  }
  function rememberPickerDir(key, path) {
    if (!key) return;
    try { localStorage.setItem(key, path || ""); } catch (e) { /* ignore */ }
  }

  // Modal-Dateiauswahl (Video). onPick({rel,name}).
  // opts.multi + onPickMany([{rel,name}]) → Mehrfachauswahl in Anhak-Reihenfolge.
  // Der Dialog startet im zuletzt genutzten Ordner (opts.rememberKey, null = aus).
  function openFilePickerModal(opts) {
    opts = opts || {};
    const multi = !!opts.multi;
    const memKey = opts.rememberKey === null ? "" : (opts.rememberKey || "fpickDir");
    const footer = multi
      ? '<div class="lib-actions fpick-actions">'
        + '<button class="btn btn-primary btn-sm" id="fpick-ok" disabled>Übernehmen</button>'
        + '<button class="btn btn-ghost btn-sm" id="fpick-all">Alle im Ordner</button>'
        + '<button class="btn btn-ghost btn-sm" id="fpick-none">Auswahl leeren</button>'
        + '<span class="muted" id="fpick-count">Nichts gewählt</span></div>'
      : "";
    openModal(opts.title || "Datei wählen",
      '<div class="breadcrumb" id="fpick-crumb"></div>'
      + '<div class="browser browser-sm" id="fpick-browser"><div class="browser-loading">Lade …</div></div>'
      + footer);
    const picker = makeFolderBrowser({
      listId: "fpick-browser", crumbId: "fpick-crumb",
      kind: "video", showFiles: true,
      rootLabel: opts.rootLabel || "Medien",
      playRoot: "media",
      searchPlaceholder: "Im Ordner suchen … (Name)",
      multiSelect: multi,
      rootFallback: true,
      onNavigate: (data, p) => rememberPickerDir(memKey, p),
      onSelectionChange: (files) => {
        const ok = $("fpick-ok");
        const info = $("fpick-count");
        if (ok) ok.disabled = !files.length;
        if (info) {
          info.textContent = files.length
            ? `${files.length} gewählt · ${files.map((f) => f.name).join(", ")}`.slice(0, 160)
            : "Nichts gewählt";
        }
      },
      pickFile: (f) => { closeModal(); if (opts.onPick) opts.onPick(f); },
    });
    if (multi && picker) {
      const ok = $("fpick-ok");
      if (ok) ok.addEventListener("click", () => {
        const files = picker.selection();
        if (!files.length) return;
        closeModal();
        if (opts.onPickMany) opts.onPickMany(files);
        else if (opts.onPick) files.forEach((f) => opts.onPick(f));
      });
      const all = $("fpick-all");
      if (all) all.addEventListener("click", () => picker.selectVisible(true));
      const none = $("fpick-none");
      if (none) none.addEventListener("click", () => picker.clearSelection());
    }
    if (picker) picker.go(opts.start != null ? opts.start : pickerDir(memKey));
  }
  window.openFilePickerModal = openFilePickerModal;
  window.openFolderPickerModal = openFolderPickerModal;

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

  const BITRATE_ROLES = {
    peak: "schwer", high: "hoch", typical: "typisch", low: "niedrig", quiet: "ruhig",
  };

  function fmtClock(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const p = (n) => String(n).padStart(2, "0");
    return `${p(h)}:${p(m)}:${p(s)}`;
  }

  function sampleModeValue(id) {
    const el = $(id || "bitrate-sample-mode");
    return el && el.checked ? "bitrate" : "even";
  }

  function sceneMinPct() {
    const el = $("scene-min-pct");
    const n = el ? parseInt(el.value, 10) : 10;
    if (!n || n < 5) return 5;
    if (n > 60) return 60;
    return n;
  }

  function bitrateQuery() {
    const page = state.currentPage;
    if (page === "vmaf") {
      return {
        samples: parseInt(($("vt-samples") && $("vt-samples").value) || "1", 10) || 1,
        clip: parseInt(($("vt-clip") && $("vt-clip").value) || "30", 10) || 30,
      };
    }
    if (page === "supertool") {
      return {
        samples: parseInt(($("st-samples") && $("st-samples").value) || "1", 10) || 1,
        clip: parseInt(($("st-clip") && $("st-clip").value) || "20", 10) || 20,
      };
    }
    return {
      samples: parseInt(($("opt-vmaf-samples") && $("opt-vmaf-samples").value) || "1", 10) || 1,
      clip: parseInt(($("opt-vmaf-clip") && $("opt-vmaf-clip").value) || "20", 10) || 20,
    };
  }

  function resetBitrateView(show) {
    const panel = $("bitrate-panel");
    if (panel) panel.hidden = !show;
    state.bitrateData = null;
    state.bitrateFor = "";
    destroyNamedChart("bitrateChart");
    const wrap = $("bitrate-chart-wrap");
    if (wrap) wrap.hidden = true;
    const sum = $("bitrate-summary");
    if (sum) sum.textContent = "";
    const wins = $("bitrate-wins");
    if (wins) wins.innerHTML = "";
  }

  function setSampleWindowNote(vmaf) {
    const note = $("vmaf-sample-note");
    if (!note) return;
    const wins = (vmaf && vmaf.sample_windows) || [];
    if (!wins.length) {
      note.hidden = true;
      note.textContent = "";
      return;
    }
    const bits = wins.map((w) => {
      const br = Number(w.kbps) > 0 ? ` · ${(Number(w.kbps) / 1000).toFixed(1)} Mbit/s` : "";
      return `${BITRATE_ROLES[w.role] || w.role} ${fmtClock(w.start)}${br}`;
    });
    note.hidden = false;
    note.textContent = `Szenen nach Bitrate: ${bits.join(" · ")}`;
  }

  function drawBitrateChart(data) {
    const wrap = $("bitrate-chart-wrap");
    const ctx = $("bitrate-chart");
    destroyNamedChart("bitrateChart");
    const bins = (data && data.bins) || [];
    if (!wrap || !ctx || !bins.length || typeof Chart === "undefined") {
      if (wrap) wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    const windows = data.windows || [];
    const vmafMarks = state.currentPage === "vmaf";
    const labels = bins.map((b) => fmtClock(b.t));
    const mark = bins.map((b) => {
      const hit = windows.some((w) => b.t >= w.start && b.t < w.start + w.length);
      return hit ? b.kbps : null;
    });
    const col = chartColors();
    const opts = lineChartOptions(col, "kbit/s");
    opts.scales.x.ticks.autoSkip = true;
    opts.scales.x.ticks.maxTicksLimit = 8;
    const datasets = [
      {
        label: "Bitrate",
        data: bins.map((b) => b.kbps),
        borderColor: col.accent || "#22d3ee",
        backgroundColor: "transparent",
        pointRadius: 0,
        borderWidth: 1.4,
        tension: 0.15,
        spanGaps: true,
      },
    ];
    if (vmafMarks) {
      datasets.push({
        label: "Vorschlag",
        data: mark,
        borderColor: "#fbbf24",
        backgroundColor: "transparent",
        pointRadius: 0,
        borderWidth: 3,
        tension: 0,
        spanGaps: false,
      });
    }
    state.bitrateChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: opts,
    });
    const avg = Number(data.avg_kbps) / 1000;
    const peak = Number(data.peak_kbps) / 1000;
    const floor = Number(data.floor_kbps) / 1000;
    const sum = $("bitrate-summary");
    if (sum) {
      const floorBit = vmafMarks && floor > 0 ? ` · Untergrenze ${floor.toFixed(1)} Mbit/s` : "";
      sum.textContent = `Schnitt ${avg.toFixed(1)} Mbit/s · Spitze ${peak.toFixed(1)} Mbit/s · Faktor ${Number(data.peak_ratio).toFixed(1)}${floorBit}`;
    }
    const wins = $("bitrate-wins");
    if (wins) {
      wins.innerHTML = vmafMarks ? windows.map((w) => {
        const br = Number(w.kbps) > 0 ? ` · ${(Number(w.kbps) / 1000).toFixed(1)} Mbit/s` : "";
        return `<span class="bitrate-win">${escapeHtml((BITRATE_ROLES[w.role] || w.role) + " " + fmtClock(w.start) + br)}</span>`;
      }).join("") : "";
    }
  }

  async function loadBitrateCurve() {
    if (!state.selected || state.selected.isBatch) return;
    const btn = $("btn-bitrate");
    const sum = $("bitrate-summary");
    const quiet = !!state.bitrateData;
    if (btn && !quiet) btn.disabled = true;
    if (sum && !quiet) sum.textContent = "Bitrate wird gelesen …";
    const q = bitrateQuery();
    const path = state.selected.path;
    state.bitrateFor = path;
    state.bitrateSeq = (state.bitrateSeq || 0) + 1;
    const seq = state.bitrateSeq;
    try {
      const res = await fetch(
        `/api/bitrate?path=${encodeURIComponent(path)}&samples=${q.samples}&clip=${q.clip}&min_pct=${sceneMinPct()}`);
      const data = await res.json();
      if (state.bitrateFor !== path || seq !== state.bitrateSeq) return;
      if (data.error) {
        if (sum) sum.textContent = data.error;
        return;
      }
      state.bitrateData = data;
      drawBitrateChart(data);
    } catch (e) {
      if (seq === state.bitrateSeq && sum) sum.textContent = "Bitrate-Verlauf konnte nicht geladen werden.";
    } finally {
      if (btn && seq === state.bitrateSeq) btn.disabled = false;
    }
  }

  function initBitratePanel() {
    const btn = $("btn-bitrate");
    if (btn) btn.addEventListener("click", loadBitrateCurve);
    let timer = 0;
    const refresh = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (state.bitrateData && state.selected && !state.selected.isBatch) loadBitrateCurve();
      }, 400);
    };
    ["vt-samples", "vt-clip", "opt-vmaf-samples", "opt-vmaf-clip", "st-samples", "st-clip", "scene-min-pct"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", refresh);
      if (id.indexOf("clip") >= 0 || id === "scene-min-pct") el.addEventListener("input", refresh);
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
    resetBitrateView();
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
    resetBitrateView(true);
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
      refreshCqSuggestion();
    } catch (e) {
      $("selected-info").innerHTML = `<span class="bad">Analyse-Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  // Vorschlag aus der Historie: CQ, der bei ähnlichen Quellen auf demselben
  // Encoder das Ziel-VMAF gehalten hat – plus Laufzeit-Schätzung. Kein Testlauf.
  let _suggestSeq = 0;
  async function refreshCqSuggestion() {
    const box = $("cq-suggest");
    const wrap = $("cq-suggest-field");
    if (!box || !wrap) return;
    const hide = () => { wrap.style.display = "none"; };
    const sel = state.selected;
    if (!sel || sel.isBatch || !sel.path) { hide(); return; }
    const seq = ++_suggestSeq;
    const target = $("opt-vmaf-target") ? parseFloat($("opt-vmaf-target").value) || 94 : 94;
    let d;
    try {
      d = await (await fetch("/api/suggest", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: sel.path,
          platform: $("opt-platform").value,
          codec: $("opt-codec").value,
          rate_mode: $("opt-rate-mode") ? $("opt-rate-mode").value : "cq",
          target_vmaf: target,
          target_height: $("opt-resolution") && $("opt-resolution").value ? parseInt($("opt-resolution").value, 10) : 0,
          encoder_speed: $("opt-enc-speed") ? $("opt-enc-speed").value : "balanced",
          two_pass: $("opt-two-pass") ? $("opt-two-pass").checked : false,
        }),
      })).json();
    } catch (e) { d = null; }
    if (seq !== _suggestSeq) return;  // inzwischen andere Datei gewählt
    if (!d || (!d.suggestion && !d.eta)) { hide(); return; }
    const parts = [];
    const s = d.suggestion;
    if (s) {
      const basis = `${s.samples} ${tt("frühere Encodes")}`;
      if (s.confident) {
        parts.push(`${tt("Vorschlag aus der Historie")}: <strong>CQ ${s.quality}</strong> → VMAF ≈ ${s.vmaf_expected} (${tt("Ziel")} ${s.target_vmaf}, ${basis}) `
          + `<button class="btn btn-ghost btn-sm" type="button" id="cq-suggest-apply" data-q="${s.quality}">${tt("Übernehmen")}</button>`);
      } else {
        parts.push(`${tt("Historie")}: ${tt("bester bekannter Wert")} CQ ${s.quality} → VMAF ≈ ${s.vmaf_expected}, ${tt("Ziel")} ${s.target_vmaf} ${tt("wurde bisher nicht erreicht")} (${basis})`);
      }
    }
    if (d.eta && d.eta.human) {
      parts.push(`${tt("Laufzeit")} ≈ ${escapeHtml(d.eta.human)} (${d.eta.speed_x}× ${tt("Echtzeit")}${d.eta.exact ? "" : ", " + tt("andere Auflösung/Speed")})`);
    }
    box.innerHTML = parts.join(" · ");
    wrap.style.display = "";
    const apply = $("cq-suggest-apply");
    if (apply) apply.addEventListener("click", () => {
      const q = $("opt-quality");
      if (!q) return;
      q.value = apply.dataset.q;
      q.dispatchEvent(new Event("input"));
      if ($("opt-rate-mode") && $("opt-rate-mode").value !== "cq") {
        $("opt-rate-mode").value = "cq";
        $("opt-rate-mode").dispatchEvent(new Event("change"));
      }
    });
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

  function chip(label, value, cls, tip) {
    const t = tip ? ` data-tip="${escapeHtml(tip)}"` : "";
    return `<div class="chip ${cls || ""}"${t}><span class="chip-k">${label}</span><span class="chip-v">${value}</span></div>`;
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
    chips.push(chip("Dynamik", info.hdr_type, info.is_hdr ? "warn" : "", hdrChipTip(info)));
    if (info.dolby_vision) {
      chips.push(chip("Dolby Vision", info.dv_profile ? "Profil " + info.dv_profile : "ja", "accent",
        hdrChipTip(info)));
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
    resetBitrateView();
  }

  /* ------------------------------------------------------------ SETTINGS */
  function initSettings() {
    const quality = $("opt-quality");
    quality.addEventListener("input", () => { $("quality-val").textContent = quality.value; });

    const fg = $("opt-film-grain");
    if (fg) fg.addEventListener("input", () => { $("film-grain-val").textContent = fg.value; });
    const aq = $("opt-aq-strength");
    if (aq) aq.addEventListener("input", () => {
      const lab = $("aq-strength-val");
      if (lab) lab.textContent = aq.value;
    });
    [["ed-aq-strength", "ed-aq-val"], ["st-aq-strength", "st-aq-val"],
     ["vt-aq-strength", "vt-aq-val"], ["eb-aq-strength", "eb-aq-val"]].forEach(([id, labId]) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("input", () => {
        const lab = $(labId);
        if (lab) lab.textContent = el.value;
      });
    });

    // Encode-Ratemodus: CQ-Slider vs. Bitrate-Feld vs. Ziel-VMAF (Test-Encodes).
    const rate = $("opt-rate-mode");
    const syncRate = () => {
      const vmaf = rate.value === "vmaf";
      const cq = rate.value === "cq";
      $("enc-cq-field").style.display = cq ? "" : "none";
      $("enc-br-field").style.display = (rate.value === "bitrate" || rate.value === "abr") ? "" : "none";
      const vcfg = $("enc-vmaf-config");
      if (vcfg) vcfg.style.display = vmaf ? "" : "none";
      const chunkedLbl = $("opt-chunked") && $("opt-chunked").closest("label");
      if (chunkedLbl) chunkedLbl.style.display = vmaf ? "none" : "";
      if (vmaf && $("chunked-config")) $("chunked-config").style.display = "none";
    };
    rate.addEventListener("change", syncRate);
    syncRate();

    const vmafTarget = $("opt-vmaf-target");
    if (vmafTarget) {
      vmafTarget.addEventListener("input", () => {
        const el = $("opt-vmaf-target-val");
        if (el) el.textContent = vmafTarget.value;
      });
      vmafTarget.addEventListener("change", () => refreshCqSuggestion());
    }
    const vmafClip = $("opt-vmaf-clip");
    if (vmafClip) {
      vmafClip.addEventListener("input", () => {
        const el = $("opt-vmaf-clip-val");
        if (el) el.textContent = vmafClip.value;
      });
    }
    const encVmafRate = $("opt-vmaf-rate");
    if (encVmafRate) {
      encVmafRate.addEventListener("change", () => syncEncVmafRate(true));
      syncEncVmafRate(false);
    }

    $("opt-platform").addEventListener("change", updateCodecAvailability);
    $("opt-codec").addEventListener("change", updateCodecAvailability);
    updateCodecAvailability();

    // CQ-Vorschlag aus der Historie: bei Wechsel von Encoder/Höhe/Modus neu holen.
    ["opt-platform", "opt-codec", "opt-resolution", "opt-rate-mode", "opt-enc-speed"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => refreshCqSuggestion());
    });

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
    "cpu:vp9": "VP9 (CPU)",
  };
  const CODEC_LABELS = { av1: "AV1", hevc: "HEVC / H.265", h264: "H.264", vp9: "VP9 (nur CPU)" };

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
      if ($("eb-codec")) ebUpdateCodec();
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
      if (sel.value === "vp9") {
        hint.textContent += (hint.textContent ? " · " : "")
          + "Nur CPU. Üblicher CRF-Bereich 30–35, die Skala hier geht bis 51.";
      }
    }
    syncDvOption();
    fillJobSpeedSelect("opt-enc-speed", "opt-platform", "opt-codec");
    syncAqField("opt");
  }

  function compareLabel(v, info) {
    if (info) return `${info.codec_label} · ${info.platform_label}`;
    return COMPARE_LABELS[v] || v;
  }

  const VT_EXTRA_MAX = 6;
  let vtRowSeq = 0;

  function vtCloneOptions(fromId, toSelect) {
    const from = $(fromId);
    if (!from || !toSelect) return;
    toSelect.innerHTML = from.innerHTML;
  }

  function refreshVtAddButton() {
    const btn = $("vt-add-row");
    if (!btn) return;
    const n = document.querySelectorAll("#vt-extra-rows .vt-enc-row").length;
    btn.disabled = n >= VT_EXTRA_MAX;
    btn.title = n >= VT_EXTRA_MAX ? "Höchstens sechs zusätzliche Vergleiche." : "";
  }

  function renumberVtRows() {
    document.querySelectorAll("#vt-extra-rows .vt-enc-row").forEach((row, i) => {
      const title = row.querySelector(".vt-row-title");
      if (title) title.textContent = "Vergleich " + (i + 2);
    });
  }

  function syncExtraRow(prefix) {
    const sel = $(prefix + "-codec");
    const platEl = $(prefix + "-platform");
    if (!sel || !platEl) return;
    const plat = platEl.value;
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
    fillJobSpeedSelect(prefix + "-enc-speed", prefix + "-platform", prefix + "-codec");
    syncAqField(prefix);
  }

  function addVtRow() {
    const host = $("vt-extra-rows");
    if (!host) return;
    if (host.querySelectorAll(".vt-enc-row").length >= VT_EXTRA_MAX) return;
    const rows = [...host.querySelectorAll(".vt-enc-row")];
    const src = rows.length ? rows[rows.length - 1].dataset.prefix : "";
    const srcId = (suffix) => src ? (src + "-" + suffix) : ("vt-" + suffix);
    vtRowSeq += 1;
    const prefix = "vtx-" + vtRowSeq;
    const row = document.createElement("div");
    row.className = "vt-enc-row";
    row.dataset.prefix = prefix;
    row.innerHTML =
      `<div class="vt-row-head"><span class="vt-row-title"></span>` +
      `<button type="button" class="btn btn-ghost btn-sm vt-row-remove">Vergleich entfernen</button></div>` +
      `<div class="field"><label>GPU / Plattform</label><select id="${prefix}-platform"></select></div>` +
      `<div class="field"><label>Codec</label><select id="${prefix}-codec"></select></div>` +
      `<div class="field"><label>Encoder-Speed</label><select id="${prefix}-enc-speed"></select>` +
      `<p class="hint enc-speed-warn" style="display:none"></p></div>` +
      `<div class="field"><label>B-Frames (NVIDIA)</label><select id="${prefix}-b-frames"></select></div>` +
      `<div class="field"><label>Tune (NVIDIA)</label><select id="${prefix}-nvenc-tune"></select></div>` +
      `<div class="field" id="${prefix}-aq-field"><label>AQ-Stärke (NVIDIA): <strong id="${prefix}-aq-val">8</strong></label>` +
      `<input type="range" id="${prefix}-aq-strength" min="1" max="15" value="8" /></div>` +
      `<div class="field"><label>Keyframe-Abstand</label><select id="${prefix}-keyint"></select></div>` +
      `<label class="check vt-rate-toggle">` +
      `<input type="checkbox" id="${prefix}-rate-custom" />` +
      `<span data-tip="Steuerungsmodus, Testwerte und Zwei-Pass nur für diese Zeile. Clip-Anzahl und Länge bleiben gemeinsam.">Andere Steuerung</span></label>` +
      `<div class="vt-rate-extra" id="${prefix}-rate-extra">` +
      `<div class="field"><label>Steuerungsmodus</label><select id="${prefix}-rate-mode">` +
      `<option value="cq">CQ / QP / CRF (Qualitätszahl)</option>` +
      `<option value="bitrate">Festbitrate (CBR)</option>` +
      `<option value="abr">Average Bitrate (VBR-Ziel)</option></select></div>` +
      `<div class="field"><label>Testwerte (1–4 · Feld leeren = weniger Tests)</label>` +
      `<div class="test-values-grid">` +
      `<input type="number" class="vt-row-val" />` +
      `<input type="number" class="vt-row-val" />` +
      `<input type="number" class="vt-row-val" />` +
      `<input type="number" class="vt-row-val" /></div>` +
      `<p class="hint vt-row-hint"></p></div>` +
      `<label class="check vt-row-two"><input type="checkbox" id="${prefix}-two-pass" />` +
      `<span>Zwei-Pass für die Testclips</span></label></div>`;
    host.appendChild(row);
    vtCloneOptions("vt-platform", $(prefix + "-platform"));
    vtCloneOptions("vt-codec", $(prefix + "-codec"));
    vtCloneOptions("vt-b-frames", $(prefix + "-b-frames"));
    vtCloneOptions("vt-nvenc-tune", $(prefix + "-nvenc-tune"));
    vtCloneOptions("vt-keyint", $(prefix + "-keyint"));
    const copyVal = (from, to) => {
      const a = $(from), b = $(to);
      if (a && b) b.value = a.value;
    };
    copyVal(srcId("platform"), prefix + "-platform");
    copyVal(srcId("codec"), prefix + "-codec");
    copyVal(srcId("b-frames"), prefix + "-b-frames");
    copyVal(srcId("nvenc-tune"), prefix + "-nvenc-tune");
    copyVal(srcId("keyint"), prefix + "-keyint");
    const srcAq = $(srcId("aq-strength"));
    const dstAq = $(prefix + "-aq-strength");
    if (srcAq && dstAq) {
      dstAq.value = srcAq.value;
      const lab = $(prefix + "-aq-val");
      if (lab) lab.textContent = srcAq.value;
    }
    syncExtraRow(prefix);
    const srcSpeed = $(srcId("enc-speed"));
    if (srcSpeed) {
      fillJobSpeedSelect(prefix + "-enc-speed", prefix + "-platform", prefix + "-codec", srcSpeed.value);
    }
    fillRowRate(prefix);
    $(prefix + "-platform").addEventListener("change", () => syncExtraRow(prefix));
    $(prefix + "-codec").addEventListener("change", () => syncExtraRow(prefix));
    $(prefix + "-rate-custom").addEventListener("change", () => {
      const extra = $(prefix + "-rate-extra");
      if (extra) extra.classList.toggle("is-open", $(prefix + "-rate-custom").checked);
    });
    $(prefix + "-rate-mode").addEventListener("change", () => syncRowRate(prefix));
    $(prefix + "-enc-speed").addEventListener("change", () => refreshEncSpeedWarn($(prefix + "-enc-speed")));
    dstAq.addEventListener("input", () => {
      const lab = $(prefix + "-aq-val");
      if (lab) lab.textContent = dstAq.value;
    });
    row.querySelector(".vt-row-remove").addEventListener("click", () => {
      row.remove();
      renumberVtRows();
      refreshVtAddButton();
    });
    renumberVtRows();
    refreshVtAddButton();
    applyLegacyBFrames(row);
  }

  function gatherVtRows() {
    return [...document.querySelectorAll("#vt-extra-rows .vt-enc-row")].slice(0, VT_EXTRA_MAX).map((el) => {
      const p = el.dataset.prefix;
      const row = {
        platform: $(p + "-platform").value,
        codec: $(p + "-codec").value,
        encoder_speed: encoderSpeedValue(p + "-enc-speed"),
        b_frames: $(p + "-b-frames").value || "auto",
        nvenc_tune: ($(p + "-nvenc-tune") && $(p + "-nvenc-tune").value) || "auto",
        aq_strength: parseInt($(p + "-aq-strength").value, 10) || 8,
        keyint_sec: parseInt($(p + "-keyint").value, 10) || 0,
      };
      if (!($(p + "-rate-custom") && $(p + "-rate-custom").checked)) return row;
      const mode = $(p + "-rate-mode").value;
      const box = $(p + "-rate-extra");
      row.rate_mode = mode;
      row.test_values = box
        ? [...box.querySelectorAll(".vt-row-val")]
          .map((inp) => parseInt(inp.value, 10))
          .filter((v) => !isNaN(v) && v > 0)
          .slice(0, 4)
        : [];
      row.two_pass = (mode === "abr" || mode === "bitrate")
        && !!($(p + "-two-pass") && $(p + "-two-pass").checked);
      return row;
    });
  }

  function fillRowRate(prefix) {
    const modeEl = $(prefix + "-rate-mode");
    const baseMode = $("vt-rate-mode");
    if (modeEl && baseMode) modeEl.value = baseMode.value;
    const src = [...document.querySelectorAll("#vt-test-grid .vt-test-val")];
    const box = $(prefix + "-rate-extra");
    if (box) {
      [...box.querySelectorAll(".vt-row-val")].forEach((inp, i) => {
        inp.value = src[i] ? src[i].value : "";
      });
    }
    const two = $(prefix + "-two-pass");
    if (two && $("vt-two-pass")) two.checked = $("vt-two-pass").checked;
    syncRowRate(prefix);
  }

  function syncRowRate(prefix) {
    const modeEl = $(prefix + "-rate-mode");
    const box = $(prefix + "-rate-extra");
    if (!modeEl || !box) return;
    const mode = modeEl.value;
    const fam = mode === "cq" ? "cq" : "bitrate";
    const inputs = [...box.querySelectorAll(".vt-row-val")];
    const hint = box.querySelector(".vt-row-hint");
    const twoWrap = box.querySelector(".vt-row-two");
    if (mode === "cq") {
      if (hint) hint.textContent = tt("CQ/QP: niedrig = hohe Qualität · hoch = kleinere Datei · leere Felder werden ignoriert");
      inputs.forEach((inp) => { inp.min = 1; inp.max = 51; });
    } else {
      if (hint) hint.textContent = tt("Bitrate in kbit/s (z. B. 8000, 6000, 4000, 2000) · leere Felder werden ignoriert");
      inputs.forEach((inp) => { inp.min = 500; inp.max = 50000; });
    }
    if (twoWrap) twoWrap.hidden = fam !== "bitrate";
    if (box.dataset.fam && box.dataset.fam !== fam) {
      const defaults = mode === "cq" ? [20, 24, 28, 32] : [8000, 6000, 4000, 2000];
      inputs.forEach((inp, idx) => { inp.value = defaults[idx]; });
    }
    box.dataset.fam = fam;
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
      sharpen: $("opt-sharpen") ? $("opt-sharpen").value : "off",
      grain: $("opt-grain") ? $("opt-grain").value : "off",
      deinterlace: $("opt-deinterlace") ? $("opt-deinterlace").value : "auto",
      aq_strength: $("opt-aq-strength") ? parseInt($("opt-aq-strength").value, 10) : 8,
      b_frames: $("opt-b-frames") ? $("opt-b-frames").value : "auto",
      nvenc_tune: $("opt-nvenc-tune") ? $("opt-nvenc-tune").value : "auto",
      keyint_sec: $("opt-keyint") ? (parseInt($("opt-keyint").value, 10) || 0) : 0,
      film_grain: $("opt-film-grain") ? parseInt($("opt-film-grain").value, 10) : 0,
      two_pass: $("opt-two-pass") ? $("opt-two-pass").checked : false,
      mobile_copy: $("opt-mobile-copy") ? $("opt-mobile-copy").checked : false,
      mobile_height: $("opt-mobile-height") ? parseInt($("opt-mobile-height").value, 10) : 720,
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
    ["opt", "remux", "merge", "split", "st", "ed"].forEach((prefix) => {
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

  function vmafP1GapValue() {
    const n = Number(window.APP_CONFIG && APP_CONFIG.vmafP1Gap);
    return Number.isFinite(n) ? n : 6;
  }

  function vmafTargetSetting() {
    const slider = $("cfg-vmaf-target");
    const fromSlider = slider ? Number(slider.value) : NaN;
    if (fromSlider >= 80 && fromSlider <= 99) return Math.round(fromSlider);
    const n = Number(window.APP_CONFIG && APP_CONFIG.vmafTarget);
    if (Number.isFinite(n) && n >= 80) return Math.min(99, Math.round(n));
    return 93;
  }

  function syncJobTargetSliders(v) {
    ["opt-vmaf-target", "st-target"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      const prev = el.dataset.fromSetting;
      if (prev == null || prev === el.value) {
        el.value = String(v);
        el.dataset.fromSetting = String(v);
        const lab = $(id + "-val");
        if (lab) lab.textContent = String(v);
      }
    });
  }

  function applyVmafTarget(raw, persist) {
    let v = Number(raw);
    if (!Number.isFinite(v)) v = 93;
    v = Math.min(99, Math.max(80, Math.round(v)));
    const slider = $("cfg-vmaf-target");
    if (slider) slider.value = String(v);
    const valEl = $("cfg-vmaf-target-val");
    if (valEl) valEl.textContent = String(v);
    const badge = $("vmaf-target-badge");
    if (badge) badge.textContent = `Ziel ${v}`;
    if (persist && window.APP_CONFIG) APP_CONFIG.vmafTarget = v;
    if (persist) syncJobTargetSliders(v);
    const gap = $("cfg-p1-gap");
    if (gap) applyVmafP1Gap(gap.value, false);
  }

  function vmafMinSavingsValue() {
    const n = Number(window.APP_CONFIG && APP_CONFIG.vmafMinSavings);
    return Number.isFinite(n) ? n : 0;
  }

  function vmafP1Anchor() {
    const sel = document.getElementById("cfg-p1-anchor");
    if (sel && (sel.value === "target" || sel.value === "mean" || sel.value === "both")) return sel.value;
    const a = window.APP_CONFIG && APP_CONFIG.vmafP1Anchor;
    return a === "target" || a === "mean" || a === "both" ? a : "both";
  }

  function applyVmafP1Anchor(anchor, persist) {
    anchor = anchor === "target" || anchor === "mean" ? anchor : "both";
    const sel = $("cfg-p1-anchor");
    if (sel) sel.value = anchor;
    const badge = $("vmaf-anchor-badge");
    if (badge) {
      badge.textContent = anchor === "target" ? "nur Ziel"
        : (anchor === "mean" ? "nur Schnitt" : "Ziel + Schnitt");
    }
    const hint = $("cfg-p1-anchor-hint");
    if (hint) {
      hint.textContent = anchor === "target"
        ? "Nur der Boden unter dem Ziel. Der Abstand zum Filmschnitt zählt nicht."
        : (anchor === "mean"
          ? "Nur der Abstand unter dem Filmschnitt der Stufe. Ein fester Boden unter dem Ziel entfällt."
          : "Boden unter dem Ziel und höchstens der Abstand unter dem Filmschnitt der Stufe.");
    }
    if (persist && window.APP_CONFIG) APP_CONFIG.vmafP1Anchor = anchor;
    const slider = $("cfg-p1-gap");
    const live = slider ? Number(slider.value) : vmafP1GapValue();
    applyVmafP1Gap(Number.isFinite(live) ? live : vmafP1GapValue(), false);
  }

  function applyVmafP1Gap(gap, persist) {
    gap = Number(gap);
    if (!Number.isFinite(gap)) gap = 6;
    if (gap <= 0) gap = 0;
    else gap = Math.min(12, Math.max(1, Math.round(gap)));
    const slider = $("cfg-p1-gap");
    if (slider) slider.value = String(gap);
    const valEl = $("cfg-p1-gap-val");
    if (valEl) valEl.textContent = String(gap);
    const badge = $("vmaf-p1-badge");
    if (badge) badge.textContent = gap <= 0 ? "nur Mittel" : `${gap} Punkte`;
    const hint = $("cfg-p1-gap-hint");
    if (hint) {
      const anchor = vmafP1Anchor();
      if (gap <= 0) {
        hint.textContent = "Floor aus: Empfehlung nur nach Mittelwert, 1%-Low zählt nicht als Mindestwert.";
      } else if (anchor === "mean") {
        hint.textContent = `Höchstens ${gap} Punkte unter dem Filmschnitt der Stufe.`;
      } else if (anchor === "both") {
        const t = vmafTargetSetting();
        hint.textContent = `Bei Ziel ${t} liegt der Boden bei ${t - gap}. Zusätzlich höchstens ${gap} Punkte unter dem Filmschnitt.`;
      } else {
        const t = vmafTargetSetting();
        hint.textContent = `Bei Ziel ${t} muss das 1%-Low der schwächsten Szene ≥ ${t - gap} liegen.`;
      }
    }
    if (persist && window.APP_CONFIG) APP_CONFIG.vmafP1Gap = gap;
  }

  function applyVmafMinSavings(raw, persist) {
    let v = Number(raw);
    if (!Number.isFinite(v)) v = 0;
    if (v < 0) v = -1;
    else v = Math.min(30, Math.max(0, Math.round(v)));
    const slider = $("cfg-min-sav");
    if (slider) slider.value = String(v);
    const valEl = $("cfg-min-sav-val");
    if (valEl) valEl.textContent = v < 0 ? "aus" : `${v} %`;
    const badge = $("vmaf-sav-badge");
    if (badge) badge.textContent = v < 0 ? "Ersparnis aus" : `≥ ${v} %`;
    const hint = $("cfg-min-sav-hint");
    if (hint) {
      if (v < 0) {
        hint.textContent = "Kein Größenfilter: Empfehlung darf auch eine größere Datei vorschlagen.";
      } else if (v === 0) {
        hint.textContent = "Datei darf nicht wachsen. Ziel-VMAF und Super-Tool überspringen den Encode, wenn jede passende Stufe größer wäre.";
      } else {
        hint.textContent = `Mindestens ${v} % kleiner als die Quelle, sonst Quelle behalten.`;
      }
    }
    if (persist && window.APP_CONFIG) APP_CONFIG.vmafMinSavings = v;
  }

  function initVmafP1Gap() {
    const slider = $("cfg-p1-gap");
    const sav = $("cfg-min-sav");
    if (!slider) return;
    applyVmafP1Gap(vmafP1GapValue(), true);
    applyVmafMinSavings(vmafMinSavingsValue(), true);
    applyVmafP1Anchor(vmafP1Anchor(), true);
    applyVmafTarget((window.APP_CONFIG && APP_CONFIG.vmafTarget) || 93, true);
    slider.addEventListener("input", () => applyVmafP1Gap(slider.value, false));
    if (sav) sav.addEventListener("input", () => applyVmafMinSavings(sav.value, false));
    const anchorSel = $("cfg-p1-anchor");
    if (anchorSel) anchorSel.addEventListener("change", () => applyVmafP1Anchor(anchorSel.value, false));
    const target = $("cfg-vmaf-target");
    if (target) target.addEventListener("input", () => applyVmafTarget(target.value, false));
    document.querySelectorAll("[data-p1-gap]").forEach((btn) => {
      btn.addEventListener("click", () => applyVmafP1Gap(btn.getAttribute("data-p1-gap"), false));
    });
    document.querySelectorAll("[data-min-sav]").forEach((btn) => {
      btn.addEventListener("click", () => applyVmafMinSavings(btn.getAttribute("data-min-sav"), false));
    });
    fetch("/api/settings").then((r) => r.json()).then((d) => {
      if (d && d.vmaf_p1_gap != null) applyVmafP1Gap(d.vmaf_p1_gap, true);
      if (d && d.vmaf_p1_anchor) applyVmafP1Anchor(d.vmaf_p1_anchor, true);
      if (d && d.vmaf_target != null) applyVmafTarget(d.vmaf_target, true);
      if (d && d.vmaf_min_savings != null) applyVmafMinSavings(d.vmaf_min_savings, true);
      const keep = $("cfg-keep-vmaf-clips");
      if (keep && d && d.keep_vmaf_clips != null) keep.checked = !!d.keep_vmaf_clips;
    }).catch(() => {});
    const save = $("btn-p1-gap-save");
    if (save) save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const d = await (await fetch("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            vmaf_p1_gap: Number(slider.value),
            vmaf_p1_anchor: ($("cfg-p1-anchor") && $("cfg-p1-anchor").value) || vmafP1Anchor(),
            vmaf_target: target ? Number(target.value) : vmafTargetSetting(),
            vmaf_min_savings: sav ? Number(sav.value) : vmafMinSavingsValue(),
            keep_vmaf_clips: !!($("cfg-keep-vmaf-clips") && $("cfg-keep-vmaf-clips").checked),
          }),
        })).json();
        if (d.error) { alert(d.error); return; }
        applyVmafP1Gap(d.vmaf_p1_gap, true);
        if (d.vmaf_p1_anchor) applyVmafP1Anchor(d.vmaf_p1_anchor, true);
        if (d.vmaf_target != null) applyVmafTarget(d.vmaf_target, true);
        if (state.vmafShown) fillVmafTable(state.vmafShown);
        if (d.vmaf_min_savings != null) applyVmafMinSavings(d.vmaf_min_savings, true);
      } finally { save.disabled = false; }
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

  const ENC_SPEED_IDS = ["cfg-enc-speed", "opt-enc-speed", "vt-enc-speed", "st-enc-speed", "ed-enc-speed"];
  const ENC_SPEED_JOB_IDS = [
    ["opt-enc-speed", "opt-platform", "opt-codec"],
    ["vt-enc-speed", "vt-platform", "vt-codec"],
    ["st-enc-speed", "st-platform", "st-codec"],
    ["ed-enc-speed", "ed-platform", "ed-codec"],
  ];
  const ENC_SPEED_LABELS = {
    fastest: "Sehr schnell", fast: "Schnell", balanced: "Ausgewogen",
    slow: "Langsam", slowest: "Sehr langsam",
  };

  function aqMode(platform, codec) {
    if (platform === "nvidia") return "nv";
    if (platform === "cpu" && (codec === "h264" || codec === "hevc")) return "cpu";
    return "";
  }
  function syncAqField(prefix) {
    const plat = $(prefix + "-platform");
    const codec = $(prefix + "-codec");
    const field = $(prefix + "-aq-field");
    if (!plat || !codec || !field) return;
    const mode = aqMode(plat.value, codec.value);
    field.style.display = mode ? "" : "none";
    const label = prefix === "opt"
      ? document.querySelector("#aq-label")
      : field.querySelector("label");
    if (label) {
      const strong = label.querySelector("strong");
      const name = mode === "cpu" ? "AQ-Stärke (x264/x265)" : "AQ-Stärke (NVIDIA)";
      const val = strong ? strong.textContent : "8";
      label.innerHTML = "";
      label.append(document.createTextNode(name + ": "));
      const s = document.createElement("strong");
      s.id = prefix === "opt" ? "aq-strength-val" : prefix + "-aq-val";
      s.textContent = val;
      label.append(s);
    }
    const hint = $("aq-hint-text");
    if (hint && prefix === "opt") {
      hint.textContent = mode === "cpu"
        ? "8 ist die Encoder-Vorgabe, das entspricht Stärke 1,0. Höher schützt Flächen und dunkle Stellen stärker, feines Detail kann etwas nachgeben. Nur CPU-H.264 und CPU-HEVC."
        : "Spatial AQ verschiebt Bits innerhalb des Bildes. Himmel, Wände und Verläufe werden feiner quantisiert, damit sie weniger banden. In Detail darf die Quantisierung gröber sein. 8 ist die NVIDIA-Vorgabe. Höher schützt Flächen stärker.";
    }
  }
  function speedFamily(platform, codec) {
    const e = encoderInfo(platform, codec);
    const enc = (e && e.encoder) || "";
    if (enc === "libsvtav1") return "svt";
    if (enc === "libvpx-vp9") return "vp9";
    if (enc.indexOf("libx") === 0) return "x264";
    if (enc.indexOf("nvenc") >= 0) return "nvenc";
    if (enc.indexOf("qsv") >= 0) return "qsv";
    return "none";
  }
  function speedPresetsFor(platform, codec) {
    const cat = (window.APP_CONFIG && APP_CONFIG.speedPresets) || {};
    return cat[speedFamily(platform, codec)] || [];
  }
  function normalizeSpeedValue(v) {
    v = String(v || "").trim().toLowerCase();
    if (["fastest", "fast", "balanced", "slow", "slowest"].includes(v)) return v;
    const cat = (window.APP_CONFIG && APP_CONFIG.speedPresets) || {};
    for (const k of Object.keys(cat)) {
      if ((cat[k] || []).some((p) => p.value === v)) return v;
    }
    return "balanced";
  }
  function encoderSpeedValue(id) {
    const el = $(id);
    return normalizeSpeedValue(el ? el.value : "");
  }
  function nativeForAlias(platform, codec, speed) {
    const presets = speedPresetsFor(platform, codec);
    const v = normalizeSpeedValue(speed);
    if (!presets.length) return v;
    if (presets.some((p) => p.value === v)) return v;
    const hit = presets.find((p) => p.alias === v);
    if (hit) return hit.value;
    const bal = presets.find((p) => p.alias === "balanced");
    return (bal && bal.value) || presets[0].value;
  }
  function speedLabelFor(platform, codec, speed) {
    const p = speedPresetsFor(platform, codec).find((x) => x.value === speed);
    if (p) return p.label;
    return ENC_SPEED_LABELS[speed] || speed || "";
  }
  function fillJobSpeedSelect(selId, platformId, codecId, preferred) {
    const sel = $(selId);
    const platEl = $(platformId);
    const codecEl = $(codecId);
    if (!sel || !platEl || !codecEl) return;
    const presets = speedPresetsFor(platEl.value, codecEl.value);
    const want = nativeForAlias(platEl.value, codecEl.value, preferred || sel.value);
    if (!presets.length) {
      sel.innerHTML = '<option value="balanced">Kein Speed-Preset (VAAPI)</option>';
      sel.value = "balanced";
      refreshEncSpeedWarn(sel);
      return;
    }
    sel.innerHTML = presets.map((p) =>
      `<option value="${escapeHtml(p.value)}">${escapeHtml(p.label)}</option>`).join("");
    sel.value = presets.some((p) => p.value === want)
      ? want
      : nativeForAlias(platEl.value, codecEl.value, "balanced");
    refreshEncSpeedWarn(sel);
  }
  function refreshAllJobSpeedSelects(preferred) {
    ENC_SPEED_JOB_IDS.forEach(([sid, pid, cid]) => fillJobSpeedSelect(sid, pid, cid, preferred));
  }

  function encSpeedWarnText(v, platform, codec) {
    if (v === "fastest" || v === "fast") {
      return "Achtung: schnellere Stufe = weniger gründliche Suche. Die Datei wird oft größer bzw. die Bit-Effizienz schlechter. VMAF-Werte gelten nur für genau diese Stufe.";
    }
    if (v === "slow" || v === "slowest") {
      return "Achtung: deutlich länger, besonders AV1 auf der CPU. Sehr langsam kann bei Filmen viele Stunden dauern. Nicht mit hoher Parallelität kombinieren.";
    }
    const presets = speedPresetsFor(platform, codec);
    const idx = presets.findIndex((p) => p.value === v);
    if (idx < 0 || presets.length < 2) return "";
    const frac = idx / (presets.length - 1);
    if (frac <= 0.22) {
      return "Achtung: schnellere Stufe = weniger gründliche Suche. Die Datei wird oft größer bzw. die Bit-Effizienz schlechter. VMAF-Werte gelten nur für genau diese Stufe.";
    }
    if (frac >= 0.75) {
      return "Achtung: deutlich länger, besonders AV1 auf der CPU. Sehr langsam kann bei Filmen viele Stunden dauern. Nicht mit hoher Parallelität kombinieren.";
    }
    return "";
  }

  function refreshEncSpeedWarn(sel) {
    if (!sel) return;
    const warn = sel.parentElement && sel.parentElement.querySelector(".enc-speed-warn");
    if (!warn) return;
    let plat = "", cod = "";
    const row = ENC_SPEED_JOB_IDS.find((r) => r[0] === sel.id);
    if (row) {
      plat = ($(row[1]) && $(row[1]).value) || "";
      cod = ($(row[2]) && $(row[2]).value) || "";
    } else if (sel.id.endsWith("-enc-speed")) {
      const prefix = sel.id.slice(0, -"-enc-speed".length);
      plat = ($(prefix + "-platform") && $(prefix + "-platform").value) || "";
      cod = ($(prefix + "-codec") && $(prefix + "-codec").value) || "";
    }
    const t = encSpeedWarnText(sel.value, plat, cod);
    warn.textContent = t;
    warn.style.display = t ? "" : "none";
    warn.classList.toggle("warn", !!t);
  }

  function applyEncoderSpeed(speed) {
    const v = normalizeSpeedValue(speed);
    const cfg = $("cfg-enc-speed");
    if (cfg) {
      if (v && ![...cfg.options].some((o) => o.value === v)) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = v + " (vom Encoder-Test)";
        cfg.appendChild(o);
      }
      if ([...cfg.options].some((o) => o.value === v)) cfg.value = v;
      else if (["fastest", "fast", "balanced", "slow", "slowest"].includes(v)) cfg.value = v;
      refreshEncSpeedWarn(cfg);
    }
    refreshAllJobSpeedSelects(v);
    const badge = $("enc-speed-badge");
    if (badge) {
      const plat = $("opt-platform") && $("opt-platform").value;
      const codec = $("opt-codec") && $("opt-codec").value;
      badge.textContent = speedLabelFor(plat, codec, v) || ENC_SPEED_LABELS[v] || v;
    }
    if (window.APP_CONFIG) APP_CONFIG.encoderSpeed = v;
  }

  function applyLegacyBFrames(root) {
    if (!window.APP_CONFIG || APP_CONFIG.imageChannel !== "legacy") return;
    const scope = root && root.querySelectorAll ? root : document;
    const selects = scope.id && String(scope.id).endsWith("b-frames")
      ? [scope]
      : [...scope.querySelectorAll('select[id$="b-frames"]')];
    selects.forEach((sel) => {
      const opt = [...sel.options].find((o) => o.value === "deep");
      if (!opt) return;
      opt.disabled = true;
      opt.textContent = "Tief, 7 hierarchisch (Legacy: nur bis 4)";
      if (sel.value === "deep") sel.value = "medium";
    });
    const note = $("bf-legacy-note");
    if (note) note.hidden = false;
  }

  function initEncoderSpeed() {
    ENC_SPEED_IDS.forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", () => refreshEncSpeedWarn(el));
    });
    [["opt-platform", "opt-codec", "opt-enc-speed"],
     ["vt-platform", "vt-codec", "vt-enc-speed"],
     ["st-platform", "st-codec", "st-enc-speed"],
     ["ed-platform", "ed-codec", "ed-enc-speed"]].forEach(([p, c, s]) => {
      const pe = $(p); const ce = $(c);
      const prefix = p.slice(0, p.indexOf("-"));
      const refresh = () => {
        fillJobSpeedSelect(s, p, c);
        syncAqField(prefix);
      };
      if (pe) pe.addEventListener("change", refresh);
      if (ce) ce.addEventListener("change", refresh);
      syncAqField(prefix);
    });
    const saved = (window.APP_CONFIG && APP_CONFIG.encoderSpeed) || "balanced";
    applyEncoderSpeed(saved);
    applyLegacyBFrames();
    fetch("/api/settings").then((r) => r.json()).then((d) => {
      if (d && d.encoder_speed) applyEncoderSpeed(d.encoder_speed);
    }).catch(() => {});
    const save = $("btn-enc-speed-save");
    if (save) save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const speed = encoderSpeedValue("cfg-enc-speed");
        const d = await (await fetch("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ encoder_speed: speed }),
        })).json();
        if (d.error) { alert(d.error); return; }
        applyEncoderSpeed(d.encoder_speed || speed);
      } finally { save.disabled = false; }
    });
  }

  let ebExtra = [];
  let ebPoll = null;

  function ebSelectedClipIds() {
    return [...document.querySelectorAll(".eb-clip:checked")].map((el) => el.value);
  }
  function ebSelectedSpeeds() {
    return [...document.querySelectorAll(".eb-speed:checked")].map((el) => el.value);
  }
  function ebParseValues() {
    const raw = ($("eb-values") && $("eb-values").value) || "";
    return raw.split(/[,;\s]+/).map((s) => parseInt(s, 10)).filter((n) => n > 0).slice(0, 4);
  }
  function ebRateMode() {
    return ($("eb-rate") && $("eb-rate").value) || "cq";
  }
  function ebUpdateEstimate() {
    const el = $("eb-estimate");
    if (!el) return;
    const nClips = ebSelectedClipIds().length + ebExtra.length;
    const nSp = ebSelectedSpeeds().length;
    const nVal = ebParseValues().length || 0;
    const nSamp = parseInt(($("eb-samples") && $("eb-samples").value) || "3", 10) || 1;
    const n = nClips * nSp * nVal * nSamp;
    if (!n) {
      el.textContent = "Mindestens einen Clip und eine Speed-Stufe wählen.";
      return;
    }
    el.textContent = `Ungefähr ${n} Mini-Encodes (${nClips} Clips × ${nSp} Speed-Stufen × ${nVal} Qualitätswerte × ${nSamp} Szenen).`
      + (n >= 40
        ? " Das dauert – CPU-AV1 mit allen Stufen kann Stunden laufen."
        : " CPU-AV1 dauert deutlich länger als GPU.");
  }
  function ebUpdateCodec() {
    const sel = $("eb-codec");
    const platEl = $("eb-platform");
    if (!sel || !platEl) return;
    const plat = platEl.value;
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
    fillBenchSpeeds("default");
    syncAqField("eb");
  }
  function fillBenchSpeeds(mode) {
    const box = $("eb-speeds");
    if (!box) return;
    const plat = $("eb-platform") && $("eb-platform").value;
    const codec = $("eb-codec") && $("eb-codec").value;
    const presets = speedPresetsFor(plat, codec);
    const prev = new Set(ebSelectedSpeeds());
    const first = !box.querySelector(".eb-speed");
    if (!presets.length) {
      box.innerHTML = '<p class="hint">Dieser Encoder hat kein Speed-Preset (VAAPI). Der Test vergleicht dann nur CQ-/ABR-Werte auf einer Stufe.</p>';
      ebUpdateEstimate();
      return;
    }
    box.innerHTML = presets.map((p) => {
      let checked = "";
      if (mode === "all") checked = "checked";
      else if (mode === "default" || first) checked = p.bench_default ? "checked" : "";
      else checked = prev.has(p.value) ? "checked" : "";
      return `<label class="check"><input type="checkbox" class="eb-speed" value="${escapeHtml(p.value)}" ${checked}/><span>${escapeHtml(p.label)}</span></label>`;
    }).join("");
    box.querySelectorAll(".eb-speed").forEach((el) => {
      el.addEventListener("change", ebUpdateEstimate);
    });
    ebUpdateEstimate();
  }
  function ebRenderClips(clips) {
    const box = $("eb-clips");
    if (!box) return;
    const prev = new Set(ebSelectedClipIds());
    const first = !box.querySelector(".eb-clip");
    box.innerHTML = (clips || []).map((c) => {
      const checked = first
        ? (["anim", "live", "fps60", "uhd"].includes(c.id) ? "checked" : "")
        : (prev.has(c.id) ? "checked" : "");
      const br = c.video_bitrate_human ? ` · ${c.video_bitrate_human}` : "";
      const status = c.present
        ? `Geladen · ${c.human || ""}${br}`
        : `Nicht geladen · ca. ${c.approx_mb} MB`;
      const media = c.present && c.media
        ? `<span class="why">${escapeHtml(c.media)}</span>` : "";
      const lic = c.license || "";
      return `<label class="enc-bench-clip"${lic ? ` title="${escapeHtml(lic)}"` : ""}>
        <input type="checkbox" class="eb-clip" value="${escapeHtml(c.id)}" ${checked} />
        <span>
          <strong>${escapeHtml(c.title)}</strong>
          <span class="enc-bench-status${c.present ? " ok" : ""}">${escapeHtml(status)}</span>
          ${media}
          <span class="why">${escapeHtml(c.why || "")}</span>
          ${lic ? `<span class="why license">${escapeHtml(lic)}</span>` : ""}
        </span>
      </label>`;
    }).join("");
    box.querySelectorAll(".eb-clip").forEach((el) => {
      el.addEventListener("change", ebUpdateEstimate);
    });
    ebUpdateEstimate();
  }
  function ebRenderExtras() {
    const box = $("eb-extras");
    if (!box) return;
    if (!ebExtra.length) {
      box.innerHTML = '<p class="hint">Keine eigenen Dateien. Optional bis zu 4 Videos aus der Bibliothek – sinnvoll, wenn deine Quellen anders sind als die Demo-Clips.</p>';
      ebUpdateEstimate();
      return;
    }
    box.innerHTML = ebExtra.map((f, i) =>
      `<div class="enc-bench-extra"><span>
        ${escapeHtml(f.name)}
        ${f.media ? `<span class="why">${escapeHtml(f.media)}</span>` : ""}
      </span>
        <button type="button" class="btn btn-ghost btn-sm" data-eb-x="${i}">Entfernen</button>
      </div>`).join("");
    box.querySelectorAll("[data-eb-x]").forEach((btn) => {
      btn.addEventListener("click", () => {
        ebExtra.splice(parseInt(btn.getAttribute("data-eb-x"), 10), 1);
        ebRenderExtras();
      });
    });
    ebUpdateEstimate();
  }
  function ebCodecLabel(c) {
    const n = String(c || "").toLowerCase();
    return ({ h264: "H.264", avc: "H.264", hevc: "HEVC", h265: "HEVC",
              av1: "AV1", vp9: "VP9", vp8: "VP8" })[n]
      || (c ? String(c).toUpperCase() : "");
  }
  function ebMediaLine(d) {
    if (!d || d.error) return "";
    const br = d.video_bitrate_human || d.overall_bitrate_human || "";
    const fps = d.fps ? `${Math.round(Number(d.fps))} fps` : "";
    return [ebCodecLabel(d.codec), d.resolution, fps, br, d.duration_human]
      .filter(Boolean).join(" · ");
  }
  async function ebFillExtraMedia(item) {
    try {
      const d = await (await fetch(`/api/probe?path=${encodeURIComponent(item.rel)}`)).json();
      item.media = ebMediaLine(d);
    } catch (_) { item.media = ""; }
  }
  function ebFillValuesDefault() {
    const inp = $("eb-values");
    if (!inp) return;
    inp.value = ebRateMode() === "cq" ? "24, 28, 32" : "8000, 6000, 4000";
    inp.placeholder = ebRateMode() === "cq" ? "z. B. 24, 28, 32" : "z. B. 8000, 6000, 4000";
    ebUpdateEstimate();
  }
  function ebRenderProgress(d) {
    const msg = $("eb-msg");
    const fill = $("eb-bar-fill");
    const pct = $("eb-pct");
    if (msg) msg.textContent = d.error || d.message || "";
    const p = Math.max(0, Math.min(100, Number(d.percent) || 0));
    if (fill) fill.style.width = p + "%";
    if (pct) pct.textContent = p.toFixed(0) + " %";
    const running = !!d.running;
    const start = $("btn-eb-start");
    const cancel = $("btn-eb-cancel");
    const dl = $("btn-eb-download");
    if (start) start.disabled = running;
    if (dl) dl.disabled = running;
    if (cancel) cancel.disabled = !running;
    const clear = $("btn-eb-clear");
    if (clear) {
      const last = d.last || {};
      const has = !!(d.rows && d.rows.length) || !!(last.rows && last.rows.length);
      clear.disabled = running || !has;
    }
  }
  function ebRenderTable(rows, rec) {
    const wrap = $("eb-results");
    if (!wrap) return;
    if (!rows || !rows.length) { wrap.innerHTML = ""; return; }
    const recSpeed = rec && rec.speed;
    const body = rows.map((r) => {
      if (r.error) {
        return `<tr><td>${escapeHtml(r.clip || "")}</td><td colspan="7" class="bad">${escapeHtml(r.error)}</td></tr>`;
      }
      const recCls = recSpeed && r.speed === recSpeed ? " row-recommended" : "";
        const val = r.rate_mode === "cq" ? ("CQ " + r.value) : (r.value + " kbit/s");
        const ist = bitrateReport(r);
      const vmaf = r.vmaf != null ? Number(r.vmaf).toFixed(1) : "—";
      const low = r.vmaf_1pct != null ? Number(r.vmaf_1pct).toFixed(1) : "—";
      const spd = speedLabelFor(r.platform, r.codec, r.speed);
      const sav = r.savings_percent;
      const savTxt = sav == null || sav === "" ? "—" : `${Number(sav).toFixed(1)}%`;
      const savCls = sav == null || sav === "" ? "num" : (Number(sav) >= 0 ? "num good" : "num bad");
      const tip = r.predicted_human
        ? `title="${escapeHtml(`Prognose ${r.predicted_human}` + (r.source_human ? ` vs. Quelle ${r.source_human}` : ""))}"`
        : "";
      return `<tr class="${recCls}">
        <td>${escapeHtml(r.clip || "")}</td>
        <td>${escapeHtml(spd)}</td>
        <td class="num">${escapeHtml(String(val))}${ist ? `<div class="hint">${escapeHtml(ist)}</div>` : ""}</td>
        <td class="num">${vmaf}</td>
        <td class="num">${low}</td>
        <td class="num">${escapeHtml(r.size_human || "")}</td>
        <td class="${savCls}" ${tip}>${savTxt}</td>
        <td class="num">${r.seconds != null ? Number(r.seconds).toFixed(1) + " s" : ""}</td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `<div class="data-table-wrap"><table class="data-table enc-bench-table">
      <thead><tr>
        <th>Clip</th><th>Speed</th><th>Wert</th><th>VMAF</th><th>1%-Low</th><th>Größe</th><th>Ersparnis</th><th>Zeit</th>
      </tr></thead><tbody>${body}</tbody></table></div>`;
  }
  function ebRenderShots(rows, rec) {
    const grid = $("eb-screenshots");
    if (!grid) return;
    const usable = (rows || []).filter((r) => !r.error && shotsOf(r).length);
    const sig = usable.map((r) =>
      (r.clip || "") + ":" + (r.speed || "") + ":" + (r.value || "") + ":"
      + ((r.screenshots || []).length)).join("|");
    if (grid._shotKey === sig) return;
    grid._shotKey = sig;
    const recSpeed = rec && rec.speed;
    renderScreenshots({
      results: usable.map((r) => ({
        label: `${speedLabelFor(r.platform, r.codec, r.speed)} · `
          + (r.rate_mode === "cq" ? ("CQ " + r.value) : (r.value + " kbit/s")),
        vmaf: r.vmaf,
        video_kbps: r.video_kbps,
        video_bitrate_human: r.video_bitrate_human,
        recommended: !!(recSpeed && r.speed === recSpeed),
        screenshots: r.screenshots,
        screenshot_ref: r.screenshot_ref,
        screenshot_enc: r.screenshot_enc,
        scene_scores: r.scene_scores,
        shotGroup: r.clip,
      })),
    }, grid);
  }
  function ebRenderRec(rec) {
    const box = $("eb-rec");
    const apply = $("btn-eb-apply");
    if (!box) return;
    if (!rec || !rec.reason) {
      box.style.display = "none";
      box.textContent = "";
      if (apply) apply.style.display = "none";
      return;
    }
    box.style.display = "";
    box.textContent = rec.reason;
    if (apply) {
      apply.style.display = rec.speed ? "" : "none";
      apply.dataset.speed = rec.speed || "";
    }
  }
  function ebApplySnapshot(d) {
    if (!d) return;
    ebRenderClips(d.clips || []);
    ebRenderProgress(d);
    if (d.running) {
      ebRenderTable(d.rows || [], d.recommendation);
      ebRenderShots(d.rows || [], d.recommendation);
      ebRenderRec(d.recommendation);
    } else {
      const last = d.last || {};
      const rows = last.rows || d.rows || [];
      const rec = last.recommendation || d.recommendation;
      ebRenderTable(rows, rec);
      ebRenderShots(rows, rec);
      ebRenderRec(rec);
    }
  }
  function ebStopPoll() {
    if (ebPoll) { clearInterval(ebPoll); ebPoll = null; }
  }
  function ebEnsurePoll() {
    if (ebPoll) return;
    ebPoll = setInterval(ebRefresh, 1200);
  }
  async function ebRefresh() {
    try {
      const d = await (await fetch("/api/encoder-bench")).json();
      ebApplySnapshot(d);
      if (d.running) ebEnsurePoll();
      else ebStopPoll();
    } catch (e) { /* ignore */ }
  }
  async function ebPost(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  function initEncoderBench() {
    if (!$("eb-clips")) return;
    ebRenderExtras();
    const plat = $("eb-platform");
    if (plat) plat.addEventListener("change", ebUpdateCodec);
    const codec = $("eb-codec");
    if (codec) codec.addEventListener("change", ebUpdateCodec);
    ebUpdateCodec();
    const rate = $("eb-rate");
    if (rate) rate.addEventListener("change", ebFillValuesDefault);
    const vals = $("eb-values");
    if (vals) vals.addEventListener("input", ebUpdateEstimate);
    const samp = $("eb-samples");
    if (samp) samp.addEventListener("change", ebUpdateEstimate);
    const allBtn = $("btn-eb-speeds-all");
    if (allBtn) allBtn.addEventListener("click", () => fillBenchSpeeds("all"));
    const defBtn = $("btn-eb-speeds-default");
    if (defBtn) defBtn.addEventListener("click", () => fillBenchSpeeds("default"));
    const add = $("btn-eb-add");
    if (add) add.addEventListener("click", () => {
      if (ebExtra.length >= 4) { alert("Höchstens 4 eigene Dateien."); return; }
      openFilePickerModal({
        title: "Eigene Testdatei wählen",
        rememberKey: "encBenchDir",
        multi: true,
        onPickMany: async (files) => {
          const added = [];
          (files || []).forEach((f) => {
            if (ebExtra.length >= 4) return;
            if (ebExtra.some((x) => x.rel === f.rel)) return;
            const item = { rel: f.rel, name: f.name, media: "" };
            ebExtra.push(item);
            added.push(item);
          });
          ebRenderExtras();
          await Promise.all(added.map(ebFillExtraMedia));
          if (added.length) ebRenderExtras();
        },
        onPick: async (f) => {
          if (ebExtra.length >= 4) return;
          if (ebExtra.some((x) => x.rel === f.rel)) return;
          const item = { rel: f.rel, name: f.name, media: "" };
          ebExtra.push(item);
          ebRenderExtras();
          await ebFillExtraMedia(item);
          ebRenderExtras();
        },
      });
    });
    const dl = $("btn-eb-download");
    if (dl) dl.addEventListener("click", async () => {
      const ids = ebSelectedClipIds();
      if (!ids.length) { alert("Mindestens einen Referenzclip anhaken."); return; }
      try {
        await ebPost("/api/encoder-bench/download", { ids });
        ebEnsurePoll();
        ebRefresh();
      } catch (e) { alert(e.message || e); }
    });
    const start = $("btn-eb-start");
    if (start) start.addEventListener("click", async () => {
      const clip_ids = ebSelectedClipIds();
      const extra_paths = ebExtra.map((f) => f.rel);
      let speeds = ebSelectedSpeeds();
      const values = ebParseValues();
      if (!clip_ids.length && !extra_paths.length) {
        alert("Mindestens einen Clip laden oder eine eigene Datei wählen.");
        return;
      }
      const platform = ($("eb-platform") && $("eb-platform").value) || "cpu";
      const codec = ($("eb-codec") && $("eb-codec").value) || "av1";
      if (!speeds.length) {
        if (speedPresetsFor(platform, codec).length) {
          alert("Mindestens eine Speed-Stufe wählen.");
          return;
        }
        speeds = ["balanced"];
      }
      if (!values.length) { alert("Mindestens einen CQ- oder Bitrate-Wert angeben."); return; }
      try {
        await ebPost("/api/encoder-bench/start", {
          clip_ids, extra_paths, speeds,
          rate_mode: ebRateMode(),
          values,
          platform, codec,
          b_frames: ($("eb-b-frames") && $("eb-b-frames").value) || "auto",
          nvenc_tune: ($("eb-nvenc-tune") && $("eb-nvenc-tune").value) || "auto",
          aq_strength: ($("eb-aq-strength") && parseInt($("eb-aq-strength").value, 10)) || 8,
          keyint_sec: ($("eb-keyint") && parseInt($("eb-keyint").value, 10)) || 0,
          clip_seconds: parseInt(($("eb-seconds") && $("eb-seconds").value) || "12", 10) || 12,
          samples: parseInt(($("eb-samples") && $("eb-samples").value) || "3", 10) || 3,
          anime: !!($("eb-anime") && $("eb-anime").checked),
        });
        ebEnsurePoll();
        ebRefresh();
      } catch (e) { alert(e.message || e); }
    });
    const cancel = $("btn-eb-cancel");
    if (cancel) cancel.addEventListener("click", async () => {
      try { await ebPost("/api/encoder-bench/cancel", {}); ebRefresh(); }
      catch (e) { alert(e.message || e); }
    });
    const clearBtn = $("btn-eb-clear");
    if (clearBtn) clearBtn.addEventListener("click", async () => {
      try {
        await ebPost("/api/encoder-bench/clear", {});
        ebRenderTable([], null);
        ebRenderRec(null);
        ebRefresh();
      } catch (e) { alert(e.message || e); }
    });
    const apply = $("btn-eb-apply");
    if (apply) apply.addEventListener("click", async () => {
      const speed = apply.dataset.speed;
      if (!speed) return;
      apply.disabled = true;
      try {
        const d = await (await fetch("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ encoder_speed: speed }),
        })).json();
        if (d.error) { alert(d.error); return; }
        applyEncoderSpeed(d.encoder_speed || speed);
      } finally { apply.disabled = false; }
    });
    ebRefresh();
  }

  // Encoding-Seite: fester CQ/CBR/ABR oder Ziel-VMAF (Test-Encodes, dann auto).
  function gatherSettings() {
    const uiMode = $("opt-rate-mode").value;
    const vmaf = uiMode === "vmaf";
    const rateMode = vmaf
      ? ($("opt-vmaf-rate") ? $("opt-vmaf-rate").value : "cq")
      : uiMode;
    const quality = rateMode === "cq"
      ? parseInt($("opt-quality").value, 10)
      : parseInt($("opt-bitrate").value, 10);
    const out = {
      platform: $("opt-platform").value,
      codec: $("opt-codec").value,
      quality: quality,
      vmaf_check: vmaf,
      workflow: "auto",
      rate_mode: rateMode,
      suffix: "_" + $("opt-codec").value,
      encoder_speed: encoderSpeedValue("opt-enc-speed"),
      ...gatherOutputCommon(),
      anime: $("opt-anime") ? $("opt-anime").checked : false,
      verify_vmaf: $("opt-verify-vmaf") ? $("opt-verify-vmaf").checked : false,
      verify_min: $("opt-verify-min") ? parseFloat($("opt-verify-min").value) || 93 : 93,
      verify_retry: $("opt-verify-retry") ? $("opt-verify-retry").checked : false,
      chunked: vmaf ? false : ($("opt-chunked") ? $("opt-chunked").checked : false),
      chunk_seconds: $("opt-chunk-seconds") ? parseInt($("opt-chunk-seconds").value, 10) || 60 : 60,
      chunk_cq_range: $("opt-chunk-range") ? parseInt($("opt-chunk-range").value, 10) || 6 : 6,
      sample_mode: "even",
    };
    if (vmaf) {
      out.target_vmaf = $("opt-vmaf-target") ? parseInt($("opt-vmaf-target").value, 10) : 94;
      out.clip_seconds = $("opt-vmaf-clip") ? parseInt($("opt-vmaf-clip").value, 10) || 20 : 20;
      out.samples = $("opt-vmaf-samples") ? parseInt($("opt-vmaf-samples").value, 10) || 1 : 1;
      out.test_values = encTestValues();
      out.generate_screenshots = $("opt-vmaf-shots") ? $("opt-vmaf-shots").checked : false;
      if (rateMode !== "cq" && out.test_values[0]) out.quality = out.test_values[0];
    }
    return out;
  }

  function encTestValues() {
    const vals = [...document.querySelectorAll("#opt-vmaf-grid .opt-vmaf-val")]
      .map((i) => parseInt(i.value, 10))
      .filter((v) => !isNaN(v) && v > 0);
    if (vals.length) return vals;
    const bitrate = $("opt-vmaf-rate") &&
      ($("opt-vmaf-rate").value === "abr" || $("opt-vmaf-rate").value === "bitrate");
    return bitrate ? [8000, 6000, 4000, 2000] : [20, 24, 28, 32];
  }

  function syncEncVmafRate(resetValues) {
    const mode = $("opt-vmaf-rate") ? $("opt-vmaf-rate").value : "cq";
    const bitrate = mode === "abr" || mode === "bitrate";
    const lbl = $("opt-vmaf-label");
    if (lbl) lbl.textContent = bitrate ? "Test-Bitraten (kbit/s)" : "Test-CQ-Werte";
    const hint = $("opt-vmaf-hint");
    if (hint) hint.textContent = bitrate
      ? "Leere Felder werden ignoriert. Höhere Bitrate = höhere Qualität/größer."
      : "Leere Felder werden ignoriert. Niedriger CQ = höhere Qualität/größer.";
    const inputs = [...document.querySelectorAll("#opt-vmaf-grid .opt-vmaf-val")];
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

  function initTooltips() {
    let tip = document.getElementById("ui-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.id = "ui-tip";
      tip.className = "ui-tip";
      tip.setAttribute("role", "tooltip");
      tip.hidden = true;
      document.body.appendChild(tip);
    }
    let showTimer = 0;
    let current = null;

    const hide = () => {
      clearTimeout(showTimer);
      showTimer = 0;
      tip.hidden = true;
      tip.textContent = "";
      current = null;
    };
    const place = (el) => {
      const r = el.getBoundingClientRect();
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      let x = r.left + (r.width / 2) - (tw / 2);
      let y = r.bottom + 8;
      if (y + th > window.innerHeight - 8) y = r.top - th - 8;
      x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
      y = Math.max(8, y);
      tip.style.left = `${Math.round(x)}px`;
      tip.style.top = `${Math.round(y)}px`;
    };
    const show = (el) => {
      const raw = (el.getAttribute("data-tip") || "").trim();
      if (!raw) return;
      current = el;
      tip.textContent = tt(raw);
      tip.hidden = false;
      place(el);
    };

    document.addEventListener("pointerover", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (!el || el === current) return;
      clearTimeout(showTimer);
      showTimer = setTimeout(() => show(el), 260);
    });
    document.addEventListener("pointerout", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (!el) return;
      if (e.relatedTarget && el.contains(e.relatedTarget)) return;
      if (el === current || showTimer) hide();
    });
    document.addEventListener("focusin", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (el) { clearTimeout(showTimer); show(el); }
    });
    document.addEventListener("focusout", hide);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
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
    });
    $("vt-codec").addEventListener("change", () => {
      vtUpdateCodecAvailability();
    });
    vtUpdateCodecAvailability();
    const addRow = $("vt-add-row");
    if (addRow) addRow.addEventListener("click", addVtRow);
    refreshVtAddButton();

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
    fillJobSpeedSelect("vt-enc-speed", "vt-platform", "vt-codec");
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
      test_values: vtGatherTestValues(),
      clip_seconds: parseInt($("vt-clip").value, 10),
      samples: parseInt($("vt-samples").value, 10),
      sample_mode: sampleModeValue(),
      scene_min_pct: sceneMinPct(),
      two_pass: !!($("vt-two-pass") && $("vt-two-pass").checked
        && ($("vt-rate-mode").value === "abr" || $("vt-rate-mode").value === "bitrate")),
      generate_screenshots: $("vt-screenshots").checked,
      suffix: "_" + $("vt-codec").value,
      encoder_speed: encoderSpeedValue("vt-enc-speed"),
      ...gatherOutputCommon(),
      anime: $("vt-anime") ? $("vt-anime").checked : false,
      b_frames: ($("vt-b-frames") && $("vt-b-frames").value) || "auto",
      nvenc_tune: ($("vt-nvenc-tune") && $("vt-nvenc-tune").value) || "auto",
      aq_strength: $("vt-aq-strength") ? parseInt($("vt-aq-strength").value, 10) : 8,
      keyint_sec: $("vt-keyint") ? (parseInt($("vt-keyint").value, 10) || 0) : 0,
      compare_rows: gatherVtRows(),
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
    if (r.encoder_speed) setSel("opt-enc-speed", r.encoder_speed);
    setSel("opt-b-frames", r.b_frames || (($("vt-b-frames") && $("vt-b-frames").value) || "auto"));
    applyLegacyBFrames($("opt-b-frames"));
    setSel("opt-nvenc-tune", r.nvenc_tune || (($("vt-nvenc-tune") && $("vt-nvenc-tune").value) || "auto"));
    if (Object.prototype.hasOwnProperty.call(r, "keyint_sec")) {
      setSel("opt-keyint", r.keyint_sec);
    } else {
      setSel("opt-keyint", ($("vt-keyint") && $("vt-keyint").value) || "0");
    }
    const aq = Object.prototype.hasOwnProperty.call(r, "aq_strength") && r.aq_strength
      ? r.aq_strength
      : ($("vt-aq-strength") && $("vt-aq-strength").value);
    if (aq) {
      setSel("opt-aq-strength", aq);
      const lab = $("aq-strength-val");
      if (lab) lab.textContent = aq;
    }
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

  /** Job-Art aus Settings (encode | edit | copy | concat | split). */
  function jobKind(s) {
    if (!s) return "encode";
    if (s.remux_only || s.video_mode === "edit") return "edit";
    const m = s.video_mode || "encode";
    if (m === "copy" || m === "concat" || m === "split") return m;
    return "encode";
  }

  /** Kurzes Badge + Detail für Queue/Stats (kein Encode-CQ bei Remux). */
  function jobModeParts(s) {
    const kind = jobKind(s);
    if (kind === "edit") {
      const cont = (((s.edit_spec || {}).container) || s.container || "mkv");
      return { badge: "Remux", detail: String(cont).toUpperCase(), kind };
    }
    if (kind === "copy") return { badge: "Audio-Opt", detail: "Video-Copy", kind };
    if (kind === "concat") return { badge: "Merge", detail: "—", kind };
    if (kind === "split") return { badge: "Split", detail: "—", kind };
    let detail;
    if (s.rate_mode === "bitrate") detail = `${s.quality} kbit/s`;
    else if (s.rate_mode === "abr") detail = `ABR ${s.quality}`;
    else detail = `CQ ${s.quality}`;
    return { badge: codecName(s), detail, kind };
  }

  /** Historien-Zeile → Anzeige (settings_json oder gespeicherte codec/quality). */
  function histModeParts(j) {
    let s = null;
    const raw = j.settings_json;
    if (raw) {
      try { s = typeof raw === "string" ? JSON.parse(raw) : raw; } catch (_) { s = null; }
    }
    if (s && typeof s === "object") return jobModeParts(s);
    const c = String(j.codec || "").toLowerCase();
    if (c === "remux") return { badge: "Remux", detail: (j.rate_mode || "mkv").toUpperCase(), kind: "edit" };
    if (c === "audio-opt") return { badge: "Audio-Opt", detail: "Video-Copy", kind: "copy" };
    if (c === "concat") return { badge: "Merge", detail: "—", kind: "concat" };
    if (c === "split") return { badge: "Split", detail: "—", kind: "split" };
    // Legacy Encode-Zeile
    let detail = "—";
    if (j.quality) {
      if (j.rate_mode === "bitrate") detail = `${j.quality} kbit/s`;
      else if (j.rate_mode === "abr") detail = `ABR ${j.quality}`;
      else detail = `CQ ${j.quality}`;
    }
    return { badge: (j.codec || "?").toUpperCase(), detail, kind: "encode" };
  }

  function settingsLabel(it) {
    const s = it.settings || {};
    const { badge, detail, kind } = jobModeParts(s);
    let verify = "";
    if (kind === "encode" && it.vmaf_verify != null) {
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
    if (it.message && String(it.message).startsWith("Quelle behalten")) {
      extra += ` <span class="vmaf-verify vv-ok" title="${escapeHtml(it.message)}">Quelle behalten</span>`;
    }
    if (it.vmaf_warning) {
      extra += ` <span class="vmaf-verify vv-warn" title="${escapeHtml(it.vmaf_warning)}">⚠ 1%-Low</span>`;
    }
    const det = detail && detail !== "—" ? ` ${escapeHtml(detail)}` : "";
    return `<span class="codec-badge">${escapeHtml(badge)}</span>${det}${verify}${extra}`;
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
      const warn = it.vmaf_warning
        ? `<div class="queue-warn" title="${escapeHtml(it.vmaf_warning)}">${escapeHtml(it.vmaf_warning)}</div>`
        : "";
      const extraOut = (it.extra_outputs || []).length
        ? `<div class="muted" style="font-size:11px" title="${escapeHtml(it.extra_outputs.join("\n"))}">+ ${tt("Mobile-Fassung")}</div>`
        : (it.settings && it.settings.mobile_copy && !DONE.includes(it.status)
          ? `<div class="muted" style="font-size:11px">+ ${tt("Mobile-Fassung")} (${it.settings.mobile_height || 720}p)</div>` : "");
      // Dauer: laufend (aktiv) oder final (abgeschlossen); wartend → Schätzung
      // aus der Historie (Encode-Zeit ähnlicher Jobs, ohne VMAF-Analyse).
      let dur = (active.has(it.id) || DONE.includes(it.status)) ? (it.duration_human || "—") : "—";
      if (dur === "—" && it.eta_estimate && it.eta_estimate.human) {
        const e = it.eta_estimate;
        const tip = `${tt("Schätzung aus der Historie")}: ${e.speed_x}× ${tt("Echtzeit")} (${e.samples} ${tt("frühere Encodes")}${e.exact ? "" : ", " + tt("andere Auflösung/Speed")})${it.settings && it.settings.vmaf_check ? " · " + tt("ohne VMAF-Analyse") : ""}`;
        dur = `<span class="muted" title="${escapeHtml(tip)}">≈ ${escapeHtml(e.human)}</span>`;
      }
      const finished = DONE.includes(it.status) && it.finished_at
        ? `<div class="muted" style="font-size:11px">${new Date(it.finished_at * 1000).toLocaleTimeString().slice(0,5)}</div>` : "";
      return `<tr class="queue-row" data-details="${it.id}" title="Details / ffprobe anzeigen">
        <td><span class="queue-title-link">${escapeHtml(it.title)}</span>${err}${warn}${extraOut}</td>
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

  function showRequeueConflictModal(outputName) {
    return new Promise((resolve) => {
      const name = outputName || tt("die Ausgabedatei");
      openModal(tt("Erneut einreihen"), `
        <p>${tt("Die Ausgabedatei existiert bereits und wird bei „Erneut“ sofort überschrieben")}:
          <strong>${escapeHtml(name)}</strong></p>
        <p class="hint">${tt("Alternativ einen neuen Dateinamen mit Suffix (_remux2, …) verwenden.")}</p>
        <div class="lib-actions" style="margin-top:12px">
          <button class="btn btn-primary btn-sm" id="rq-overwrite">${tt("Überschreiben")}</button>
          <button class="btn btn-ghost btn-sm" id="rq-suffix">${tt("Neuer Name (_…2)")}</button>
          <button class="btn btn-ghost btn-sm" id="rq-cancel">${tt("Abbrechen")}</button>
        </div>`);
      let settled = false;
      const done = (v) => {
        if (settled) return;
        settled = true;
        closeModal();
        resolve(v);
      };
      const ow = $("rq-overwrite");
      const suf = $("rq-suffix");
      const can = $("rq-cancel");
      if (ow) ow.addEventListener("click", () => done("overwrite"));
      if (suf) suf.addEventListener("click", () => done("suffix"));
      if (can) can.addEventListener("click", () => done(null));
      const m = $("app-modal");
      const onHide = () => {
        if (!settled && m && m.style.display === "none") done(null);
      };
      const obs = new MutationObserver(onHide);
      if (m) obs.observe(m, { attributes: true, attributeFilter: ["style"] });
    });
  }

  async function requeueJob(id, fromHistory) {
    if (!id) return;
    let mode = "overwrite";
    try {
      const det = await (await fetch(`/api/queue/${encodeURIComponent(id)}/details`)).json();
      if (det && det.output && det.output.exists) {
        const choice = await showRequeueConflictModal(det.output.name || "");
        if (!choice) return;
        mode = choice;
      }
    } catch (_) { /* ohne Details: überschreiben */ }
    const url = fromHistory
      ? `/api/history/${encodeURIComponent(id)}/requeue`
      : `/api/queue/${encodeURIComponent(id)}/requeue`;
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      const d = await r.json();
      if (d.error) alert(d.error);
      else if (d.conflict_mode === "suffix" && d.output_name) {
        alert(tt("Erneut eingereiht als") + ": " + d.output_name);
      }
    } catch (e) {
      alert(String(e));
    }
  }

  async function reopenJobWithSettings(d) {
    const s = (d && d.settings) || {};
    const rel = (d.source && d.source.rel) || null;
    const name = (d.source && d.source.name)
      || (d.path ? String(d.path).split(/[/\\]/).pop() : "") || "Datei";
    const isRemux = !!(s.remux_only || s.video_mode === "edit");
    closeModal();
    if (isRemux) {
      navTo("remux");
      applyRemuxProfile(s);
      if (rel) {
        try {
          await remuxSelectFile({ rel, name });
          if (s.edit_spec) remuxApplyEditSpec(s.edit_spec);
        } catch (e) {
          $("remux-start-info").innerHTML =
            `<span class="bad">${escapeHtml(String(e))}</span>`;
        }
      }
      return;
    }
    navTo("encode");
    if (rel) {
      try {
        await selectFile({ rel, name, size_human: "" });
      } catch (_) {
        state.selected = { path: rel, name, isBatch: false };
        if (typeof enableActionButtons === "function") enableActionButtons();
      }
    }
    applyProfile(s);
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
      syncKeepSourceBanner(null);
      state.vmafSession = "";
      syncVmafRepick();
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
    state.vmafSession = vmaf.session || "";
    syncVmafRepick();
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
    const key = target.id + ":" + vmaf.results.length + ":" + vmaf.recommended_quality + ":" + target.status + ":" + (vmaf.keep_source ? "1" : "0") + ":" + (vmaf.pick_warning ? "w" : "");
    showCard(card, true);
    $("vmaf-model-badge").textContent = `Modell: ${vmaf.model} · Clip: ${vmaf.clip_seconds || 30}s`;
    setSampleWindowNote(vmaf);

    // Nur neu rendern, wenn sich wirklich etwas geändert hat – sonst flackert
    // der Graph bei jedem Queue-Poll (alle paar Sekunden).
    if (key === state.lastVmafKey) return;

    state.chartScene = null;
    showVmafChart(vmaf);
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
    const applyBtn = $("btn-vmaf-apply");
    if (applyBtn) applyBtn.addEventListener("click", () => {
      if (state.vmafArchiveData) applyArchivedVmaf(state.vmafArchiveData);
    });
    const csv = $("btn-vmaf-csv");
    if (csv) csv.addEventListener("click", exportVmafCsv);
    const repick = $("btn-vmaf-repick");
    if (repick) repick.addEventListener("click", repickVmaf);
    const nerd = $("btn-vmaf-nerd");
    if (nerd) nerd.addEventListener("click", () => {
      state.vmafNerd = !state.vmafNerd;
      nerd.classList.toggle("active", state.vmafNerd);
      state.nerdKey = "";
      refreshNerdFrames();
      if (!state.vmafNerd && state.vmafShown) drawFrameChart(state.vmafShown);
    });
    const nerdFloor = $("nerd-floor");
    if (nerdFloor) nerdFloor.addEventListener("input", applyNerdFloor);
    bindVmafZoom();
    refreshVmafHistory();
  }

  function syncVmafRepick() {
    const btn = $("btn-vmaf-repick");
    if (btn) btn.disabled = !state.vmafSession;
  }

  async function repickVmaf() {
    const session = state.vmafSession;
    if (!session) return;
    const btn = $("btn-vmaf-repick");
    if (btn) btn.disabled = true;
    try {
      const r = await fetch("/api/vmaf/repick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session }),
      });
      const data = await r.json();
      if (!r.ok || !data.analysis) {
        alert(data.error || "Konnte nicht neu einordnen.");
        return;
      }
      const vmaf = data.analysis;
      state.vmafSession = vmaf.session || session;
      if (state.viewSession) {
        state.chartScene = null;
        showVmafChart(vmaf);
        fillVmafTable(vmaf);
        state.shotScene = null;
        renderScreenshots(vmaf);
      } else {
        const items = state.lastItems || [];
        const hit = items.find((i) => i.vmaf && i.vmaf.session === session);
        if (hit) hit.vmaf = vmaf;
        state.lastVmafKey = null;
        renderVmaf(items, state.lastActiveId);
      }
    } catch (e) {
      alert("Konnte nicht neu einordnen.");
    } finally {
      syncVmafRepick();
    }
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
    destroyNamedChart("vmafGapChart");
    destroyNamedChart("vmafFrameChart");
    const gap = $("vmaf-gap-wrap"); if (gap) gap.hidden = true;
    const fr = $("vmaf-frame-wrap"); if (fr) fr.hidden = true;
    const curveHint = $("vmaf-curve-hint"); if (curveHint) curveHint.hidden = true;
    const tb = $("vmaf-table") && $("vmaf-table").querySelector("tbody");
    if (tb) tb.innerHTML = "";
    const sc = $("vmaf-screenshots"); if (sc) sc.innerHTML = "";
    const note = $("vmaf-archive-note"); if (note) note.style.display = "none";
    syncKeepSourceBanner(null);
    const badge = $("vmaf-model-badge");
    if (badge) badge.textContent = "Kein aktueller Vergleich – oben einen früheren auswählen";
    setSampleWindowNote(null);
  }

  async function showArchivedSession(name) {
    try {
      const r = await fetch(`/api/vmaf/session/${encodeURIComponent(name)}`);
      if (!r.ok) return;
      const data = await r.json();
      const vmaf = data.analysis;
      if (!vmaf || !vmaf.results) return;
      state.viewSession = name;
      state.vmafSession = data.session || name;
      syncVmafRepick();
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
      setSampleWindowNote(vmaf);
      state.chartScene = null;
      showVmafChart(vmaf);
      fillVmafTable(vmaf);
      state.shotScene = null;
      renderScreenshots(vmaf);
      state.vmafArchiveData = data;
      const applied = $("vmaf-archive-applied");
      if (applied) applied.textContent = "";
    } catch (e) { /* ignorieren */ }
  }

  function vmafRunGroups(vmaf) {
    const groups = [];
    (vmaf.results || []).forEach((r) => {
      const mode = r.rate_mode || vmaf.rate_mode || "cq";
      const key = [
        r.platform || "", r.codec || "", r.encoder_speed || "", r.b_frames || "",
        r.nvenc_tune || "", r.aq_strength || "", r.keyint_sec || "",
        mode, r.two_pass ? "1" : "0",
      ].join("|");
      let g = groups.find((x) => x.key === key);
      if (!g) {
        g = { key, r, mode, values: [] };
        groups.push(g);
      }
      const v = r.value != null ? r.value : r.quality;
      if (v != null && !g.values.includes(v)) g.values.push(Number(v));
    });
    return groups;
  }

  function applyArchivedVmaf(data) {
    const vmaf = (data && data.analysis) || {};
    const params = (data && data.params) || {};
    const groups = vmafRunGroups(vmaf);
    const baseGroup = groups.find((g) =>
      g.r.platform === vmaf.recommended_platform && g.r.codec === vmaf.recommended_codec
      && g.mode === (vmaf.rate_mode || g.mode))
      || groups.find((g) => g.mode === (vmaf.rate_mode || "cq"))
      || groups[0];
    const base = {
      platform: params.platform || (baseGroup && baseGroup.r.platform) || "",
      codec: params.codec || (baseGroup && baseGroup.r.codec) || "",
      encoder_speed: params.encoder_speed || (baseGroup && baseGroup.r.encoder_speed) || "",
      b_frames: params.b_frames || (baseGroup && baseGroup.r.b_frames) || "auto",
      nvenc_tune: params.nvenc_tune || (baseGroup && baseGroup.r.nvenc_tune) || "auto",
      aq_strength: params.aq_strength || (baseGroup && baseGroup.r.aq_strength) || 8,
      keyint_sec: params.keyint_sec != null
        ? params.keyint_sec
        : ((baseGroup && baseGroup.r.keyint_sec) || 0),
      rate_mode: params.rate_mode || vmaf.rate_mode || (baseGroup && baseGroup.mode) || "cq",
      test_values: (params.test_values && params.test_values.length)
        ? params.test_values
        : (baseGroup ? baseGroup.values : []),
      two_pass: params.two_pass != null
        ? !!params.two_pass
        : !!(baseGroup && (baseGroup.r.two_pass
          || String(baseGroup.r.encoder_args || "").includes("-multipass fullres")
          || /(^|\s)-pass(\s|$)/.test(baseGroup.r.encoder_args || ""))),
      clip_seconds: params.clip_seconds || vmaf.clip_seconds || 30,
      samples: params.samples
        || (vmaf.sample_starts && vmaf.sample_starts.length)
        || 1,
      anime: !!params.anime,
      screenshots: params.generate_screenshots !== false,
      sample_mode: params.sample_mode || "even",
      scene_min_pct: params.scene_min_pct || 10,
    };
    const setVal = (id, val) => {
      const el = $(id);
      if (!el || val == null || val === "") return;
      el.value = String(val);
    };
    setVal("vt-platform", base.platform);
    if (typeof vtUpdateCodecAvailability === "function") vtUpdateCodecAvailability();
    setVal("vt-codec", base.codec);
    if (typeof vtUpdateCodecAvailability === "function") vtUpdateCodecAvailability();
    fillJobSpeedSelect("vt-enc-speed", "vt-platform", "vt-codec", base.encoder_speed);
    setVal("vt-b-frames", base.b_frames);
    setVal("vt-nvenc-tune", base.nvenc_tune);
    if ($("vt-aq-strength")) {
      $("vt-aq-strength").value = String(base.aq_strength);
      const lab = $("vt-aq-val");
      if (lab) lab.textContent = String(base.aq_strength);
    }
    setVal("vt-keyint", base.keyint_sec);
    setVal("vt-rate-mode", base.rate_mode);
    vtUpdateTestHints(false);
    const tests = [...document.querySelectorAll("#vt-test-grid .vt-test-val")];
    tests.forEach((inp, i) => {
      inp.value = base.test_values[i] != null ? String(base.test_values[i]) : "";
    });
    const clip = $("vt-clip");
    if (clip) {
      clip.value = String(base.clip_seconds);
      const shown = $("vt-clip-val");
      if (shown) shown.textContent = clip.value;
    }
    setVal("vt-samples", base.samples);
    if ($("vt-two-pass")) $("vt-two-pass").checked = !!base.two_pass;
    if ($("vt-anime")) $("vt-anime").checked = !!base.anime;
    if ($("vt-screenshots")) $("vt-screenshots").checked = !!base.screenshots;
    const sampleBox = $("bitrate-sample-mode");
    if (sampleBox) sampleBox.checked = base.sample_mode === "bitrate";
    setVal("scene-min-pct", base.scene_min_pct);
    applyLegacyBFrames(document.getElementById("vt-enc-base"));

    document.querySelectorAll("#vt-extra-rows .vt-enc-row").forEach((el) => el.remove());
    refreshVtAddButton();
    const extras = Array.isArray(params.compare_rows)
      ? params.compare_rows
      : groups.filter((g) => g !== baseGroup).map((g) => {
        const ownRate = g.mode !== base.rate_mode
          || !!g.r.two_pass !== !!base.two_pass
          || JSON.stringify(g.values) !== JSON.stringify(base.test_values || []);
        return {
          platform: g.r.platform,
          codec: g.r.codec,
          encoder_speed: g.r.encoder_speed,
          b_frames: g.r.b_frames,
          nvenc_tune: g.r.nvenc_tune,
          aq_strength: g.r.aq_strength,
          keyint_sec: g.r.keyint_sec,
          rate_mode: ownRate ? g.mode : "",
          test_values: ownRate ? g.values : [],
          two_pass: ownRate ? !!g.r.two_pass : false,
        };
      });
    extras.slice(0, VT_EXTRA_MAX).forEach((row) => {
      addVtRow();
      const host = $("vt-extra-rows");
      const el = host && host.lastElementChild;
      const prefix = el && el.dataset.prefix;
      if (!prefix) return;
      setVal(prefix + "-platform", row.platform);
      syncExtraRow(prefix);
      setVal(prefix + "-codec", row.codec);
      syncExtraRow(prefix);
      fillJobSpeedSelect(prefix + "-enc-speed", prefix + "-platform", prefix + "-codec", row.encoder_speed);
      setVal(prefix + "-b-frames", row.b_frames || "auto");
      setVal(prefix + "-nvenc-tune", row.nvenc_tune || "auto");
      const aq = $(prefix + "-aq-strength");
      if (aq && row.aq_strength) {
        aq.value = String(row.aq_strength);
        const lab = $(prefix + "-aq-val");
        if (lab) lab.textContent = String(row.aq_strength);
      }
      setVal(prefix + "-keyint", row.keyint_sec != null ? row.keyint_sec : 0);
      if (row.rate_mode) {
        const box = $(prefix + "-rate-extra");
        const custom = $(prefix + "-rate-custom");
        if (box) box.dataset.fam = row.rate_mode === "cq" ? "cq" : "bitrate";
        setVal(prefix + "-rate-mode", row.rate_mode);
        syncRowRate(prefix);
        const inputs = box ? [...box.querySelectorAll(".vt-row-val")] : [];
        (row.test_values || []).slice(0, 4).forEach((v, i) => {
          if (inputs[i]) inputs[i].value = String(v);
        });
        const two = $(prefix + "-two-pass");
        if (two) two.checked = !!row.two_pass;
        if (custom) custom.checked = true;
        if (box) box.classList.add("is-open");
      }
      applyLegacyBFrames($(prefix + "-b-frames"));
    });
    const note = $("vmaf-archive-applied");
    if (note) note.textContent = " · " + tt("Einstellungen für einen neuen Vergleich übernommen.");
  }

  function showLiveVmaf() {
    state.viewSession = null;
    state.vmafArchiveData = null;
    state.lastVmafKey = null; // Neuzeichnen der Live-Ansicht erzwingen
    const note = $("vmaf-archive-note");
    if (note) note.style.display = "none";
    const applied = $("vmaf-archive-applied");
    if (applied) applied.textContent = "";
    const sel = $("vmaf-history");
    if (sel) sel.value = "";
    renderVmaf(state.lastItems || [], state.lastActiveId);
  }

  // Ergebnisse auf eine einheitliche Szenen-Screenshotliste normalisieren.
  // Ältere Sessions kennen nur screenshot_ref/enc (= Szene 0).
  function fmtKbps(kbps) {
    const n = Number(kbps);
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n >= 1000) return (n / 1000).toFixed(1) + " Mbit/s";
    return Math.round(n) + " kbit/s";
  }

  function measuredBitrateText(kbps, human) {
    const h = (human && String(human).trim()) || fmtKbps(kbps);
    return h && h !== "—" ? `${tt("Ist")} ${h}` : "";
  }

  // Zielbitrate und gemessene Bitrate nebeneinander, plus Abweichung in Prozent.
  // Bei CQ gibt es kein Bitrate-Ziel, dann bleibt die gemessene Bitrate.
  function bitrateReport(r, kbps) {
    if (!r) return "";
    const raw = kbps != null && kbps !== "" ? kbps : r.video_kbps;
    const measured = Number(raw);
    const ist = measured > 0 ? fmtKbps(measured) : "";
    const mode = r.rate_mode || "";
    const target = Number(r.value != null ? r.value : r.quality);
    if ((mode === "abr" || mode === "bitrate") && target > 0) {
      const parts = [`${tt("Ziel")} ${Math.round(target)} kbit/s`];
      if (ist) parts.push(`${tt("Ist")} ${ist}`);
      if (measured > 0) {
        const d = Math.round((measured - target) / target * 100);
        parts.push((d > 0 ? "+" : "") + d + " %");
      }
      return parts.join(" · ");
    }
    return ist ? `${tt("Ist")} ${ist}` : "";
  }

  function shotsOf(r) {
    if (Array.isArray(r.screenshots) && r.screenshots.length) return r.screenshots;
    if (r.screenshot_ref || r.screenshot_enc)
      return [{ scene: 0, ref: r.screenshot_ref, enc: r.screenshot_enc }];
    return [];
  }

  function renderScreenshots(vmaf, gridEl) {
    const grid = gridEl || $("vmaf-screenshots");
    if (!grid) return;
    const ui = grid._shotUi || (grid._shotUi = { scene: null, group: null });
    const all = (vmaf.results || [])
      .map((r) => ({ r, shots: shotsOf(r) }))
      .filter((x) => x.shots.length);
    if (!all.length) { grid.innerHTML = ""; return; }

    const groups = [...new Set(all.map((x) => x.r.shotGroup).filter(Boolean))];
    if (groups.length && (ui.group == null || !groups.includes(ui.group)))
      ui.group = groups[0];
    const results = groups.length
      ? all.filter((x) => x.r.shotGroup === ui.group)
      : all;

    const scenes = [...new Set(
      results.flatMap((x) => x.shots.map((s) => s.scene))
    )].sort((a, b) => a - b);
    if (ui.scene == null || !scenes.includes(ui.scene))
      ui.scene = scenes[0];
    const sc = ui.scene;
    if (grid === $("vmaf-screenshots")) state.shotScene = sc;

    let refSrc = "";
    results.forEach((x) => {
      const s = x.shots.find((sh) => sh.scene === sc);
      if (s && s.ref && !refSrc) refSrc = s.ref;
    });

    let sceneStart = 0;
    let sceneLen = 0;
    results.forEach((x) => {
      const s = (x.r.scene_scores || []).find((v) => v.scene === sc);
      if (!s || s.start == null) return;
      sceneStart = Number(s.start) || 0;
      sceneLen = Number(s.length) || sceneLen;
    });
    const tiles = [];
    if (refSrc)
      tiles.push({
        src: refSrc, label: "Original", sub: `Szene ${sc + 1}`, ref: true,
        start: sceneStart, len: sceneLen,
      });
    results.forEach((x) => {
      const s = x.shots.find((sh) => sh.scene === sc);
      if (s && s.enc) {
        const sceneScore = (x.r.scene_scores || []).find((v) => v.scene === sc);
        const v = sceneScore ? sceneScore.vmaf : x.r.vmaf;
        const bits = [`VMAF ${Number(v).toFixed(1)}`];
        if (sceneScore && sceneScore.p1 != null)
          bits.push(`1% ${Number(sceneScore.p1).toFixed(1)}`);
        if (sceneScore && sceneScore.xpsnr != null)
          bits.push(`XPSNR ${Number(sceneScore.xpsnr).toFixed(1)}`);
        const ist = bitrateReport(
          x.r, (s && s.kbps) || (sceneScore && sceneScore.kbps) || x.r.video_kbps);
        if (ist) bits.push(ist);
        tiles.push({
          src: s.enc,
          label: x.r.label || ("Q" + x.r.quality),
          sub: bits.join(" · "),
          recommended: x.r.recommended,
          clip: (grid === $("vmaf-screenshots") && s.clip) ? s.clip : "",
          start: sceneStart, len: sceneLen,
        });
      }
    });
    if (!tiles.length) { grid.innerHTML = ""; return; }

    const groupTabs = groups.length > 1
      ? `<div class="shot-scenes">${groups.map((g) =>
          `<button class="shot-scene ${g === ui.group ? "active" : ""}" data-group="${escapeHtml(g)}">${escapeHtml(g)}</button>`
        ).join("")}</div>`
      : "";
    const sceneTabs = scenes.length > 1
      ? `<div class="shot-scenes">${scenes.map((n) =>
          `<button class="shot-scene ${n === sc ? "active" : ""}" data-scene="${n}">Szene ${n + 1}</button>`
        ).join("")}</div>`
      : "";

    grid.innerHTML = `
      <div class="shot-toolbar">
        ${groupTabs}${sceneTabs}
        <span class="shot-hint">Bilder ankreuzen und vergleichen – oder anklicken zum Vergrößern.</span>
        <button class="btn small shot-compare" disabled>Auswahl vergleichen</button>
        ${grid === $("vmaf-screenshots") ? `<button class="btn small shot-ab" disabled title="Genau zwei auswählen: Original und ein Testclip, oder zwei Testclips.">Im A/B abspielen</button>` : ""}
      </div>
      <div class="shot-gallery">
        ${tiles.map((t) => {
          const cap = `${t.label} · ${t.sub}`;
          return `
          <div class="shot-tile ${t.recommended ? "recommended" : ""} ${t.ref ? "is-ref" : ""}"
               data-src="${t.src}" data-cap="${escapeHtml(cap)}"
               data-kind="${t.ref ? "ref" : "enc"}" data-clip="${escapeHtml(t.clip || "")}"
               data-start="${Number(t.start) || 0}" data-len="${Number(t.len) || 0}">
            <label class="shot-check" title="Für Vergleich auswählen">
              <input type="checkbox" ${t.ref ? "checked" : ""} />
            </label>
            <img src="${t.src}" alt="${escapeHtml(t.label)}" loading="lazy" />
            <span class="shot-badge">${escapeHtml(t.label)}<small>${escapeHtml(t.sub)}</small></span>
            ${t.clip ? `<button type="button" class="shot-play" data-clip="${escapeHtml(t.clip)}">${escapeHtml(tt("Abspielen"))}</button>` : ""}
          </div>`;
        }).join("")}
      </div>`;

    grid.querySelectorAll(".shot-scene[data-scene]").forEach((b) =>
      b.addEventListener("click", () => {
        ui.scene = +b.dataset.scene;
        if (grid === $("vmaf-screenshots")) {
          state.chartScene = ui.scene;
          showVmafChart(vmaf);
        }
        renderScreenshots(vmaf, grid);
      }));
    grid.querySelectorAll(".shot-scene[data-group]").forEach((b) =>
      b.addEventListener("click", () => {
        ui.group = b.dataset.group;
        ui.scene = null;
        renderScreenshots(vmaf, grid);
      }));

    const cmpBtn = grid.querySelector(".shot-compare");
    const abBtn = grid.querySelector(".shot-ab");
    const selected = () => [...grid.querySelectorAll(".shot-tile")]
      .filter((t) => t.querySelector(".shot-check input").checked)
      .map((t) => ({
        src: t.dataset.src,
        label: t.dataset.cap,
        kind: t.dataset.kind || "",
        clip: t.dataset.clip || "",
        start: Number(t.dataset.start) || 0,
        len: Number(t.dataset.len) || 0,
      }));
    const updateCmp = () => {
      const picks = selected();
      const n = picks.length;
      cmpBtn.disabled = n < 1;
      cmpBtn.textContent = n > 0 ? `Auswahl vergleichen (${n})` : "Auswahl vergleichen";
      if (!abBtn) return;
      const why = clipAbBlock(picks);
      abBtn.disabled = !!why;
      abBtn.title = why ? tt(why) : "";
    };
    grid.querySelectorAll(".shot-check input").forEach((c) => {
      c.addEventListener("click", (e) => e.stopPropagation());
      c.addEventListener("change", updateCmp);
    });
    cmpBtn.addEventListener("click", () => {
      const items = selected();
      if (items.length) openGallery(items);
    });
    if (abBtn) abBtn.addEventListener("click", () => {
      const picks = selected();
      if (!clipAbBlock(picks)) openClipAb(picks);
    });
    grid.querySelectorAll(".shot-tile img").forEach((img) =>
      img.addEventListener("click", () => {
        const tile = img.closest(".shot-tile");
        openGallery([{ src: tile.dataset.src, label: tile.dataset.cap }]);
      }));
    grid.querySelectorAll(".shot-play").forEach((btn) =>
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const session = state.vmafSession;
        const file = btn.dataset.clip;
        if (!session || !file) return;
        const tile = btn.closest(".shot-tile");
        const cap = (tile && tile.dataset.cap) || "Szene";
        const src = `/api/vmaf/clip?session=${encodeURIComponent(session)}`
          + `&file=${encodeURIComponent(file)}`;
        openModal(cap, `<video class="vmaf-clip-video" controls autoplay playsinline src="${src}"></video>`,
          { player: true });
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

  function vmafTargetLo(vmaf) {
    const stored = Number(vmaf && vmaf.target_lo);
    if (stored > 0) return stored;
    return vmafTargetSetting();
  }

  function vmafWorstP1(r) {
    let p1 = null;
    let scene = null;
    (r.scene_scores || []).forEach((sc) => {
      const v = Number(sc.p1);
      if (!Number.isFinite(v) || v <= 0) return;
      if (p1 == null || v < p1) {
        p1 = v;
        scene = sc.scene;
      }
    });
    if (p1 == null && r.vmaf_1pct != null && Number(r.vmaf_1pct) > 0) p1 = Number(r.vmaf_1pct);
    return { p1, scene };
  }

  function vmafResultMiss(r, lo, gap) {
    const miss = {};
    if (Number(r.vmaf) + 1e-9 < lo) miss.mean = true;
    if (gap > 0) {
      const worst = vmafWorstP1(r);
      if (worst.p1 != null) {
        const anchor = vmafP1Anchor();
        const limits = [];
        if (anchor === "both" || anchor === "target") limits.push(lo - gap);
        if (anchor === "both" || anchor === "mean") limits.push(Number(r.vmaf) - gap);
        const floor = limits.length ? Math.max.apply(null, limits) : null;
        if (floor != null && worst.p1 + 1e-9 < floor) {
          miss.p1 = worst.p1;
          miss.floor = floor;
          if (worst.scene != null) miss.scene = Number(worst.scene) + 1;
        }
      }
    }
    return (miss.mean || miss.p1 != null) ? miss : null;
  }

  function vmafSceneFloor(r, lo, gap) {
    if (!(gap > 0)) return null;
    const anchor = vmafP1Anchor();
    const limits = [];
    if (anchor === "both" || anchor === "target") limits.push(lo - gap);
    if (anchor === "both" || anchor === "mean") limits.push(Number(r.vmaf) - gap);
    return limits.length ? Math.max.apply(null, limits) : null;
  }

  function vmafSceneP1Miss(sc, floor) {
    const p1 = Number(sc && sc.p1);
    return floor != null && Number.isFinite(p1) && p1 > 0 && p1 + 1e-9 < floor;
  }

  function vmafMissText(r, miss, lo) {
    const bits = [];
    if (miss.mean) bits.push(`Mittel ${Number(r.vmaf).toFixed(1)} unter Ziel ${Math.round(lo)}`);
    if (miss.p1 != null) {
      const where = miss.scene ? `Szene ${miss.scene}, ` : "";
      bits.push(`${where}1%-Low ${Number(miss.p1).toFixed(1)} unter ${Math.round(miss.floor)}`);
    }
    return bits.join(", ");
  }

  function vmafSpread(r) {
    const mean = Number(r.vmaf);
    if (!Number.isFinite(mean)) return null;
    const worst = vmafWorstP1(r);
    const avg = r.vmaf_1pct != null && Number(r.vmaf_1pct) > 0 ? Number(r.vmaf_1pct) : null;
    const min = worst.p1 != null ? worst.p1 : avg;
    if (min == null) return null;
    const sceneCount = (r.scene_scores || []).filter((sc) => Number(sc.p1) > 0).length;
    return {
      avg,
      min,
      scene: worst.scene != null ? Number(worst.scene) + 1 : null,
      delta: mean - min,
      perScene: sceneCount > 0 && worst.p1 != null,
      showMin: sceneCount > 1 && avg != null && Math.abs(avg - min) >= 0.15,
    };
  }

  function vmafDeltaMark(delta) {
    const d = Math.abs(delta);
    const cls = d >= 6 ? "bad" : (d >= 4 ? "warn" : "");
    const num = d.toFixed(1);
    const text = delta >= 0 ? `Δ −${num}` : `Δ +${num}`;
    return { cls, text };
  }

  // XPSNR-Einordnung (dB): ≥ 40 sehr gut, ≥ 36 gut, ≥ 32 mäßig, darunter schwach.
  function xpsnrMark(v) {
    const x = Number(v);
    if (!Number.isFinite(x)) return { cls: "", text: "—", word: "" };
    const cls = x >= 40 ? "good" : (x >= 36 ? "" : (x >= 32 ? "warn" : "bad"));
    const word = x >= 40 ? "sehr gut" : (x >= 36 ? "gut" : (x >= 32 ? "mäßig" : "schwach"));
    return { cls, text: `${x.toFixed(1)} dB`, word };
  }

  // Schwächste Szene nach XPSNR-Minimum (einzelne Frames), falls gemessen.
  function xpsnrWeakScene(r) {
    let worst = null;
    (r.scene_scores || []).forEach((sc) => {
      if (sc && sc.xpsnr_min != null && Number(sc.xpsnr_min) > 0
          && (!worst || Number(sc.xpsnr_min) < worst.v)) {
        worst = { v: Number(sc.xpsnr_min), scene: sc.scene + 1 };
      }
    });
    return worst;
  }

  function xpsnrPill(r) {
    if (r.xpsnr == null) return "";
    const m = xpsnrMark(r.xpsnr);
    const weak = xpsnrWeakScene(r);
    const tipBits = [
      `XPSNR ${m.text} – ${m.word}. Wahrnehmungsgewichtetes PSNR (FFmpeg-Filter), unabhängig vom VMAF-Modell;`,
      "Gewichtung (4·Y + U + V) / 6. Grob: ab 36 dB gut, ab 40 dB sehr gut.",
    ];
    if (weak) tipBits.push(`Schwächster Frame ${weak.v.toFixed(1)} dB in Szene ${weak.scene}.`);
    let s = `<span class="metric-pill ${m.cls}" title="${escapeHtml(tipBits.join(" "))}">XPSNR ${m.text}</span>`;
    if (weak && weak.v < 32) {
      s += `<span class="metric-pill ${weak.v < 28 ? "bad" : "warn"}" title="${escapeHtml(
        `Schwächster Frame nach XPSNR: ${weak.v.toFixed(1)} dB in Szene ${weak.scene}.`)}">min ${weak.v.toFixed(1)} S${weak.scene}</span>`;
    }
    return s;
  }

  function vmafCell(r) {
    const spread = vmafSpread(r);
    let s = `${r.vmaf.toFixed(2)}`;
    if (spread) {
      const mark = vmafDeltaMark(spread.delta);
      const tip = spread.perScene
        ? (spread.scene
          ? `Mittel minus 1%-Low der schwächsten Szene. Szene ${spread.scene}: ${spread.min.toFixed(1)}.`
          : `Mittel minus 1%-Low der schwächsten Szene. ${spread.min.toFixed(1)}.`)
        : "Mittel minus 1%-Low. Pro Szene liegt kein 1%-Low vor.";
      s += `<span class="vmaf-delta ${mark.cls}" title="${escapeHtml(tip)}">${mark.text}</span>`;
    }
    // Mehrere Szenen: Mittelwert oben, Streuung (min–max) je Szene darunter.
    if (r.vmaf_min != null && r.vmaf_max != null) {
      const perScene = (r.scene_scores || [])
        .map((sc) => {
          const bits = [`Szene ${sc.scene + 1}: ${Number(sc.vmaf).toFixed(1)}`];
          if (sc.p1 != null) bits.push(`1%-Low ${Number(sc.p1).toFixed(1)}`);
          if (sc.hmean != null) bits.push(`H-Ø ${Number(sc.hmean).toFixed(1)}`);
          if (sc.xpsnr != null) {
            const mn = sc.xpsnr_min != null && Number(sc.xpsnr_min) > 0
              ? ` (min ${Number(sc.xpsnr_min).toFixed(1)})` : "";
            bits.push(`XPSNR ${Number(sc.xpsnr).toFixed(1)} dB${mn}`);
          }
          const d = Number(sc.vmaf) - r.vmaf;
          bits.push(`Δ ${d >= 0 ? "+" : ""}${d.toFixed(1)}`);
          return bits.join(" · ");
        });
      const lo = vmafTargetLo(state.vmafShown);
      const floor = vmafSceneFloor(r, lo, vmafP1GapValue());
      const visible = (r.scene_scores || []).map((sc) => {
        const p = sc.p1 != null ? ` / 1% ${Number(sc.p1).toFixed(1)}` : "";
        const text = `S${sc.scene + 1} ${Number(sc.vmaf).toFixed(1)}${p}`;
        if (!vmafSceneP1Miss(sc, floor)) return escapeHtml(text);
        const tip = `1%-Low ${Number(sc.p1).toFixed(1)} unter ${Math.round(floor)}`;
        return `<span class="vmaf-scene-miss" title="${escapeHtml(tip)}">${escapeHtml(text)}</span>`;
      }).join(" · ");
      s += `<br><span class="muted vmaf-scenes" title="${escapeHtml(perScene.join("\n"))}">`
        + `${visible || escapeHtml(`Ø · Szenen ${r.vmaf_min.toFixed(1)}–${r.vmaf_max.toFixed(1)}`)}</span>`;
    }
    // Zusatzmetriken (falls gemessen): 1%-Low + harmon. Mittel, PSNR/SSIM.
    const extra = [];
    if (spread && spread.avg != null) extra.push(`<span>Ø 1% ${spread.avg.toFixed(1)}</span>`);
    if (spread && spread.showMin) {
      const sc = spread.scene ? ` S${spread.scene}` : "";
      extra.push(`<span>min 1% ${spread.min.toFixed(1)}${sc}</span>`);
    } else if (r.vmaf_1pct != null && !(spread && spread.avg != null)) {
      extra.push(`<span>1%-Low ${r.vmaf_1pct.toFixed(1)}</span>`);
    }
    if (r.vmaf_hmean != null) extra.push(`<span>H-Ø ${r.vmaf_hmean.toFixed(1)}</span>`);
    if (r.vmaf_score != null && r.vmaf_1pct != null) extra.push(`<span>Score ${Number(r.vmaf_score).toFixed(1)}</span>`);
    if (extra.length) {
      const gap = vmafP1GapValue();
      const rec = gap <= 0
        ? "Empfehlung: nur Mittel ≥ Ziel (1%-Low-Floor aus)."
        : `Empfehlung: Mittel ≥ Ziel und das 1%-Low der schwächsten Szene nicht mehr als ${gap} darunter.`;
      s += `<br><span class="muted" title="Ø 1% = Schnitt der Szenen-1%-Lows; min 1% = schwächste Szene; `
        + `1%-Low je Szene = Mittel der schlechtesten 1 % Frames; `
        + `H-Ø = harmonisches Mittel; Score = 55 % Mittel + 35 % 1%-Low + 10 % H-Ø. `
        + `${escapeHtml(rec)}">${extra.join(" · ")}</span>`;
    }
    // Zweite Meinung: XPSNR als eingefärbte Pille (wie die VMAF-Δ-Marke),
    // PSNR/SSIM bleiben Beiwerk.
    const pill = xpsnrPill(r);
    const qual = [];
    if (r.psnr != null) qual.push(`PSNR ${r.psnr.toFixed(1)} dB`);
    if (r.ssim != null) qual.push(`SSIM ${r.ssim.toFixed(3)}`);
    if (pill || qual.length) {
      s += `<br>${pill}${qual.length
        ? `<span class="muted" title="${escapeHtml("PSNR/SSIM: klassische Signalmaße, nur zur Orientierung.")}">${pill ? " · " : ""}${qual.join(" · ")}</span>`
        : ""}`;
    }
    const lo = vmafTargetLo(state.vmafShown);
    const miss = vmafResultMiss(r, lo, vmafP1GapValue());
    if (miss) {
      s += `<br><span class="badge vmaf-miss" title="${escapeHtml(vmafMissText(r, miss, lo))}">Ziel verfehlt</span>`;
    }
    return s;
  }

  function syncKeepSourceBanner(vmaf) {
    const el = $("vmaf-keep-source");
    if (el) el.style.display = vmaf && vmaf.keep_source ? "" : "none";
    const warn = $("vmaf-pick-warning");
    if (!warn) return;
    if (vmaf && vmaf.pick_warning && !vmaf.keep_source) {
      warn.textContent = vmaf.pick_warning;
      warn.style.display = "";
    } else {
      warn.textContent = "";
      warn.style.display = "none";
    }
    const missBox = $("vmaf-target-miss");
    if (!missBox) return;
    const misses = [];
    const lo = vmafTargetLo(vmaf);
    const gap = vmafP1GapValue();
    if (vmaf && vmaf.results) {
      vmaf.results.forEach((r) => {
        const miss = vmafResultMiss(r, lo, gap);
        if (!miss) return;
        const label = r.label || ("Q" + r.quality);
        misses.push(`${label}: ${vmafMissText(r, miss, lo)}.`);
      });
    }
    if (!misses.length) {
      missBox.textContent = "";
      missBox.style.display = "none";
      return;
    }
    const lead = gap <= 0
      ? `Ziel nicht erreicht (Ziel ${Math.round(lo)}, nur Mittel).`
      : (vmafP1Anchor() === "mean"
        ? `Ziel nicht erreicht (Ziel ${Math.round(lo)}, schwächste Szene ≥ Filmschnitt−${Math.round(gap)}).`
        : (vmafP1Anchor() === "both"
          ? `Ziel nicht erreicht (Ziel ${Math.round(lo)}, schwächste Szene ≥ ${Math.round(lo - gap)} und ≥ Filmschnitt−${Math.round(gap)}).`
          : `Ziel nicht erreicht (Ziel ${Math.round(lo)}, schwächste Szene ≥ ${Math.round(lo - gap)}).`));
    missBox.innerHTML = `<span>${escapeHtml(lead)}</span> `
      + misses.map((line) => `<span>${escapeHtml(line)}</span>`).join(" ");
    missBox.style.display = "";
  }

  function exportVmafCsv() {
    const vmaf = state.vmafShown;
    const rows = (vmaf && vmaf.results) || [];
    if (!rows.length) return;
    const esc = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
    const num = (v, d) => (v == null || v === "" || Number.isNaN(Number(v)))
      ? "" : Number(v).toFixed(d);
    const line = (cells) => cells.map(esc).join(";");
    const rateName = (r) => {
      const m = (r && r.rate_mode) || vmaf.rate_mode || "";
      if (m === "abr") return "ABR";
      if (m === "bitrate") return "CBR";
      if (m === "cq") return "CQ";
      return m;
    };
    const twoName = (r) => {
      const args = (r && r.encoder_args) || "";
      const on = !!(r && r.two_pass)
        || args.includes("-multipass fullres")
        || /(^|\s)-pass(\s|$)/.test(args);
      return on ? "ja" : "nein";
    };
    const settingCells = (r) => [
      rateName(r),
      twoName(r),
      r.video_kbps ? Math.round(r.video_kbps) : "",
      r.recommended ? "ja" : "",
      r.encoder_speed || "",
      r.b_frames || "",
      r.nvenc_tune || "",
      r.aq_strength ? r.aq_strength : "",
      Object.prototype.hasOwnProperty.call(r, "keyint_sec") ? r.keyint_sec : "",
      r.encoder_args || "",
    ];
    const settingHead = [
      "Steuerungsmodus", "Zwei-Pass", "Ist kbit/s", "Empfohlen",
      "Speed", "B-Frames", "Tune", "AQ", "Keyframe s", "Encoder-Args",
    ];
    const scenes = vmafSceneList(vmaf);
    const session = state.vmafSession;
    const pull = (url) => fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    Promise.all([
      loadSceneBitrate(vmaf),
      session
        ? Promise.all(scenes.map((sc) =>
          pull(`/api/vmaf/frames?session=${encodeURIComponent(session)}&scene=${sc}`)))
        : Promise.resolve([]),
      session
        ? Promise.all(scenes.map((sc) =>
          pull(`/api/vmaf/source-bitrate?session=${encodeURIComponent(session)}&scene=${sc}`)))
        : Promise.resolve([]),
    ]).then(([, logs, sources]) => {
      const lines = [];
      lines.push(line([
        "Modell", vmaf.model || "",
        "Clip s", vmaf.clip_seconds || "",
        "Szenen", scenes.length,
      ]));
      lines.push("");
      lines.push(line([
        "Einstellung", "Plattform", "Codec", "Wert", "VMAF", "1%-Low",
        "H-Mittel", "PSNR", "XPSNR", "SSIM", "Ersparnis %", "Prognose Bytes",
        ...settingHead,
      ]));
      rows.forEach((r) => {
        lines.push(line([
          r.label || ("Q" + r.quality), r.platform || "", r.codec || "",
          r.value != null ? r.value : r.quality,
          num(r.vmaf, 2), num(r.vmaf_1pct, 2), num(r.vmaf_hmean, 2),
          num(r.psnr, 2), num(r.xpsnr, 2), num(r.ssim, 4), num(r.savings_percent, 1),
          r.predicted_size_bytes != null ? r.predicted_size_bytes : "",
          ...settingCells(r),
        ]));
      });
      lines.push("");
      lines.push(line([
        "Szene", "Start s", "Länge s", "Einstellung", "VMAF", "1%-Low", "H-Mittel",
        "PSNR", "XPSNR", "SSIM", "kbit/s",
        ...settingHead,
      ]));
      rows.forEach((r) => {
        (r.scene_scores || []).forEach((sc) => {
          lines.push(line([
            (sc.scene != null ? sc.scene + 1 : ""),
            num(sc.start, 3), num(sc.length, 3),
            r.label || ("Q" + r.quality),
            num(sc.vmaf, 2), num(sc.p1, 2), num(sc.hmean, 2),
            num(sc.psnr, 2), num(sc.xpsnr, 2), num(sc.ssim, 4),
            sc.kbps ? Math.round(sc.kbps) : "",
            ...settingCells(r),
          ]));
        });
      });
      const frameRows = [];
      (logs || []).forEach((pack, idx) => {
        if (!pack || !pack.series) return;
        const sceneNo = (scenes[idx] != null ? scenes[idx] : idx) + 1;
        const frameSec = Number(pack.series[0] && pack.series[0].frame_sec) || 0;
        pack.series.forEach((s) => {
          const sec = Number(s.frame_sec) || frameSec;
          (s.frames || []).forEach((f) => {
            const t = sec > 0 ? (Number(f.n) || 0) * sec : "";
            frameRows.push(line([
              s.label || "", sceneNo, f.n, num(t, 3),
              num(f.vmaf, 2), num(f.psnr, 2), num(f.ssim, 4),
            ]));
          });
        });
      });
      lines.push("");
      if (frameRows.length) {
        lines.push(line(["Einstellung", "Szene", "Frame", "Zeit s", "VMAF", "PSNR", "SSIM"]));
        frameRows.forEach((row) => lines.push(row));
      } else {
        lines.push(line([
          "Verlauf", "Einstellung", "Szene", "Abschnitt",
          "VMAF (Tiefstwert im Abschnitt)",
        ]));
        rows.forEach((r) => {
          (r.scene_scores || []).forEach((sc) => {
            (sc.frames || []).forEach((v, i) => {
              lines.push(line([
                "frame", r.label || ("Q" + r.quality),
                (sc.scene != null ? sc.scene + 1 : ""), i + 1, num(v, 2),
              ]));
            });
          });
        });
      }
      const brRows = [];
      rows.forEach((r) => {
        (r.scene_scores || []).forEach((sc) => {
          (sc.bitrate || []).forEach((b) => {
            brRows.push(line([
              r.label || ("Q" + r.quality),
              (sc.scene != null ? sc.scene + 1 : ""),
              b.n != null ? b.n : "",
              num(b.t, 4), num(b.kbps, 1),
            ]));
          });
        });
      });
      if (brRows.length) {
        lines.push("");
        lines.push(line(["Einstellung", "Szene", "Frame", "Zeit s", "kbit/s"]));
        brRows.forEach((row) => lines.push(row));
      }
      const srcRows = [];
      (sources || []).forEach((pack, idx) => {
        const bins = (pack && pack.bins) || [];
        const sceneNo = (scenes[idx] != null ? scenes[idx] : idx) + 1;
        bins.forEach((b) => {
          srcRows.push(line([
            sceneNo,
            b.n != null ? b.n : "",
            num(b.t, 4), num(b.kbps, 1),
          ]));
        });
      });
      if (srcRows.length) {
        lines.push("");
        lines.push(line(["Original-Bitrate"]));
        lines.push(line(["Szene", "Frame", "Zeit s", "kbit/s"]));
        srcRows.forEach((row) => lines.push(row));
      }
      const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      const stem = (state.vmafSource && state.vmafSource.name)
        ? state.vmafSource.name.replace(/\.[^.]+$/, "") : "vmaf";
      a.href = URL.createObjectURL(blob);
      a.download = stem + "-vmaf.csv";
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  function fillVmafTable(vmaf) {
    syncKeepSourceBanner(vmaf);
    const body = $("vmaf-table").querySelector("tbody");
    body.innerHTML = vmaf.results.map((r, idx) => `
      <tr class="${r.recommended ? "row-recommended" : ""} ${vmafResultMiss(r, vmafTargetLo(vmaf), vmafP1GapValue()) ? "row-target-miss" : ""}">
        <td>${escapeHtml(r.label || ("Q" + r.quality))}${bitrateReport(r) ? `<div class="hint">${escapeHtml(bitrateReport(r))}</div>` : ""}</td>
        <td>${vmafCell(r)}</td>
        <td>${r.predicted_human}</td>
        <td class="${r.savings_percent >= 0 ? "good" : "bad"}">${r.savings_percent}%</td>
        <td class="vmaf-row-actions">
          ${r.recommended ? (vmaf.pick_warning
            ? '<span class="badge recommended" title="' + escapeHtml(vmaf.pick_warning) + '">Empfohlen · Kompromiss</span>'
            : '<span class="badge recommended">Empfohlen</span>') : ""}
          <button class="btn btn-ghost btn-sm" data-take="${idx}" title="Diese Einstellung ins Encoding übernehmen">→ Encoding</button>
        </td>
      </tr>`).join("");
    body.querySelectorAll("[data-take]").forEach((b) =>
      b.addEventListener("click", () =>
        transferToEncode(vmaf.results[parseInt(b.dataset.take, 10)])));
  }

  function chartColors() {
    const accent = cssVar("--accent");
    const warn = cssVar("--warn") || "#fbbf24";
    return {
      accent,
      accent2: cssVar("--accent-2"),
      good: cssVar("--good"),
      warn,
      p1: warn,
      text: cssVar("--text"),
      muted: cssVar("--text-muted"),
      grid: cssVar("--border"),
      band: colorWithAlpha(accent, 0.18),
    };
  }

  const CHART_PALETTE = ["#4f9dff", "#22c55e", "#f59e0b", "#e879f9", "#f43f5e", "#14b8a6"];

  function vmafSceneList(vmaf) {
    const set = new Set();
    (vmaf.results || []).forEach((r) => {
      (r.scene_scores || []).forEach((s) => {
        if (s && s.scene != null) set.add(s.scene);
      });
    });
    return [...set].sort((a, b) => a - b);
  }

  function sceneEntry(r, scene) {
    return (r.scene_scores || []).find((s) => s.scene === scene) || null;
  }

  function sceneScoreOf(r, scene) {
    const sc = sceneEntry(r, scene);
    return sc && sc.vmaf != null ? sc.vmaf : null;
  }

  function destroyNamedChart(key) {
    if (state[key]) {
      state[key].destroy();
      state[key] = null;
    }
  }

  function showVmafChart(vmaf) {
    state.vmafShown = vmaf;
    const curveHint = $("vmaf-curve-hint");
    if (curveHint) curveHint.hidden = false;
    drawChart(vmaf);
    renderChartScenes(vmaf);
    drawGapChart(vmaf);
    drawFrameChart(vmaf);
    Promise.all([loadSceneBitrate(vmaf), loadSourceBitrate(vmaf)]).then(() => {
      if (state.vmafShown !== vmaf) return;
      drawFrameChart(vmaf);
      if (state.vmafNerd && state.nerdData) drawNerdFrames(state.nerdData);
    });
    refreshNerdFrames();
  }

  function lineChartOptions(col, yTitle) {
    return {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: col.text, font: { size: 11 } } },
      },
      scales: {
        x: { grid: { color: col.grid }, ticks: { color: col.muted, maxRotation: 0 } },
        y: {
          title: { display: true, text: yTitle, color: col.muted },
          grid: { color: col.grid }, ticks: { color: col.muted },
        },
      },
    };
  }

  function drawGapChart(vmaf) {
    const wrap = $("vmaf-gap-wrap");
    const ctx = $("vmaf-gap-chart");
    destroyNamedChart("vmafGapChart");
    const scenes = vmafSceneList(vmaf);
    const rows = vmaf.results || [];
    if (!wrap || !ctx || scenes.length < 2 || !rows.length) {
      if (wrap) wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    const col = chartColors();
    const labels = scenes.map((n) => `Szene ${n + 1}`);
    const datasets = rows.map((r, i) => {
      const color = r.recommended ? col.good : CHART_PALETTE[i % CHART_PALETTE.length];
      return {
        label: r.label || ("Q" + r.quality),
        data: scenes.map((n) => {
          const v = sceneScoreOf(r, n);
          return v == null ? null : Math.round((v - r.vmaf) * 100) / 100;
        }),
        borderColor: color,
        backgroundColor: "transparent",
        pointRadius: r.recommended ? 5 : 3,
        borderWidth: r.recommended ? 2.6 : 1.6,
        tension: 0.25,
        spanGaps: true,
      };
    });
    state.vmafGapChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: lineChartOptions(col, "Δ VMAF"),
    });
  }

  function sceneClipFile(r, scene) {
    const shot = shotsOf(r).find((sh) => sh.scene === scene && sh.clip);
    return shot ? shot.clip : "";
  }

  function bitrateIsFrame(sc) {
    const bins = sc && sc.bitrate;
    return !!(bins && bins.length && bins[0] && bins[0].n != null
      && sc.bitrate_align === "body");
  }

  function scoredSpan(clipSec, sc) {
    const stored = Number(sc && sc.bitrate_sec);
    if (stored > 0) return stored;
    const sec = Number(clipSec) || 0;
    if (!(sec >= 4)) return sec;
    const margin = Math.min(0.5, sec * 0.08);
    if (margin > 0 && sec > margin * 2 + 1) return sec - 2 * margin;
    return sec;
  }

  function bitratePointX(b) {
    if (b && b.n != null) return Number(b.t) || 0;
    return (Number(b && b.t) || 0) + 0.25;
  }

  function readVmafZoom() {
    const yminEl = $("vmaf-zoom-ymin");
    const ymaxEl = $("vmaf-zoom-ymax");
    const x0El = $("vmaf-zoom-x0");
    const x1El = $("vmaf-zoom-x1");
    const yMin = yminEl && yminEl.value !== "" ? Number(yminEl.value) : null;
    const yMax = ymaxEl && ymaxEl.value !== "" ? Number(ymaxEl.value) : null;
    let x0 = x0El ? Number(x0El.value) : 0;
    let x1 = x1El ? Number(x1El.value) : 100;
    if (!Number.isFinite(x0)) x0 = 0;
    if (!Number.isFinite(x1)) x1 = 100;
    x0 = Math.max(0, Math.min(100, x0));
    x1 = Math.max(0, Math.min(100, x1));
    if (x1 < x0) {
      const swap = x0;
      x0 = x1;
      x1 = swap;
    }
    if (x1 - x0 < 1) x1 = Math.min(100, x0 + 1);
    const hasYMin = yMin != null && Number.isFinite(yMin);
    const hasYMax = yMax != null && Number.isFinite(yMax);
    return {
      yMin, yMax, x0, x1,
      partialY: hasYMin || hasYMax,
      lockedX: x0 > 0.05 || x1 < 99.95,
    };
  }

  function datasetXExtent(datasets) {
    let lo = Infinity;
    let hi = -Infinity;
    (datasets || []).forEach((d) => {
      (d.data || []).forEach((p) => {
        if (p && typeof p.x === "number" && Number.isFinite(p.x)) {
          if (p.x < lo) lo = p.x;
          if (p.x > hi) hi = p.x;
        }
      });
    });
    return hi > lo ? [lo, hi] : null;
  }

  function paintVmafRange() {
    const x0 = $("vmaf-zoom-x0");
    const x1 = $("vmaf-zoom-x1");
    const fill = $("vmaf-zoom-fill");
    if (!x0 || !x1 || !fill) return;
    const a = Number(x0.value);
    const b = Number(x1.value);
    const lo = Math.min(a, b);
    const hi = Math.max(a, b);
    fill.style.left = lo + "%";
    fill.style.width = Math.max(0, hi - lo) + "%";
  }

  function clampVmafWindow(changed) {
    const x0 = $("vmaf-zoom-x0");
    const x1 = $("vmaf-zoom-x1");
    if (!x0 || !x1) return;
    const gap = 1;
    let a = Number(x0.value);
    let b = Number(x1.value);
    if (changed === "x0" && a > b - gap) x0.value = String(Math.max(0, b - gap));
    if (changed === "x1" && b < a + gap) x1.value = String(Math.min(100, a + gap));
    x0.classList.toggle("is-top", changed === "x0");
    x1.classList.toggle("is-top", changed !== "x0");
    paintVmafRange();
  }

  function syncVmafZoomLabel() {
    const lab = $("vmaf-zoom-xlabel");
    if (!lab) return;
    const z = readVmafZoom();
    lab.textContent = z.lockedX
      ? `${Math.round(z.x0)}–${Math.round(z.x1)} %`
      : tt("gesamt");
    paintVmafRange();
  }

  function applyZoomToChart(chart) {
    if (!chart || !chart.$zoom) return;
    const meta = chart.$zoom;
    const z = readVmafZoom();
    const y = chart.options.scales && chart.options.scales.y;
    if (y) {
      const lo = z.yMin != null && Number.isFinite(z.yMin)
        ? z.yMin : (meta.ySuggest ? meta.ySuggest[0] : 0);
      const hi = z.yMax != null && Number.isFinite(z.yMax)
        ? z.yMax : (meta.ySuggest ? meta.ySuggest[1] : 100);
      if (z.partialY && hi > lo) {
        y.min = lo;
        y.max = hi;
      } else {
        delete y.min;
        delete y.max;
        if (meta.ySuggest) {
          y.suggestedMin = meta.ySuggest[0];
          y.suggestedMax = meta.ySuggest[1];
        }
      }
    }
    const x = chart.options.scales && chart.options.scales.x;
    if (x && meta.xMode) {
      if (z.lockedX && meta.xMode === "linear" && meta.extent) {
        const span = meta.extent[1] - meta.extent[0];
        x.min = meta.extent[0] + span * z.x0 / 100;
        x.max = meta.extent[0] + span * z.x1 / 100;
      } else if (z.lockedX && meta.xMode === "index" && meta.labelCount > 1) {
        x.min = (meta.labelCount - 1) * z.x0 / 100;
        x.max = (meta.labelCount - 1) * z.x1 / 100;
      } else {
        delete x.min;
        delete x.max;
      }
    }
    chart.update("none");
  }

  function attachVmafZoom(chart, meta) {
    if (!chart) return;
    chart.$zoom = meta;
    applyZoomToChart(chart);
  }

  function refreshVmafZoom() {
    syncVmafZoomLabel();
    [state.vmafChart, state.vmafFrameChart, state.vmafNerdChart].forEach(applyZoomToChart);
  }

  function bindVmafZoom() {
    const ymin = $("vmaf-zoom-ymin");
    const ymax = $("vmaf-zoom-ymax");
    const x0 = $("vmaf-zoom-x0");
    const x1 = $("vmaf-zoom-x1");
    const reset = $("vmaf-zoom-reset");
    if (ymin) ymin.addEventListener("change", refreshVmafZoom);
    if (ymax) ymax.addEventListener("change", refreshVmafZoom);
    if (x0) x0.addEventListener("input", () => {
      clampVmafWindow("x0");
      refreshVmafZoom();
    });
    if (x1) x1.addEventListener("input", () => {
      clampVmafWindow("x1");
      refreshVmafZoom();
    });
    if (reset) reset.addEventListener("click", () => {
      if (ymin) ymin.value = "";
      if (ymax) ymax.value = "";
      if (x0) x0.value = "0";
      if (x1) x1.value = "100";
      paintVmafRange();
      refreshVmafZoom();
    });
    syncVmafZoomLabel();
  }

  function loadSceneBitrate(vmaf) {
    const session = state.vmafSession;
    if (!vmaf || !session) return Promise.resolve();
    const jobs = [];
    (vmaf.results || []).forEach((r) => {
      (r.scene_scores || []).forEach((sc) => {
        if (!sc || sc._brTried || bitrateIsFrame(sc)) return;
        const file = sceneClipFile(r, sc.scene);
        if (!file) {
          if (!Array.isArray(sc.bitrate)) sc.bitrate = [];
          sc._brTried = true;
          return;
        }
        sc._brTried = true;
        jobs.push(fetch(
          `/api/vmaf/clip-bitrate?session=${encodeURIComponent(session)}&file=${encodeURIComponent(file)}`)
          .then((res) => (res.ok ? res.json() : null))
          .then((data) => {
            if (data && data.bins && data.bins.length) {
              sc.bitrate = data.bins;
              if (data.scored_sec) sc.bitrate_sec = data.scored_sec;
              if (data.align) sc.bitrate_align = data.align;
            } else if (!Array.isArray(sc.bitrate)) {
              sc.bitrate = [];
            }
          })
          .catch(() => {
            if (!Array.isArray(sc.bitrate)) sc.bitrate = [];
          }));
      });
    });
    return Promise.all(jobs);
  }

  function sourceBinsFor(scene) {
    const pack = state.sourceBr;
    if (!pack || pack.session !== state.vmafSession || pack.scene !== scene) return [];
    return pack.bins || [];
  }

  function loadSourceBitrate(vmaf) {
    const session = state.vmafSession;
    const scenes = vmaf ? vmafSceneList(vmaf) : [];
    const scene = state.chartScene != null ? state.chartScene : (scenes[0] ?? null);
    if (!session || scene == null) return Promise.resolve();
    const key = session + ":" + scene;
    if (state.sourceBr && state.sourceBr.key === key) return Promise.resolve();
    return fetch(`/api/vmaf/source-bitrate?session=${encodeURIComponent(session)}&scene=${scene}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (state.vmafSession !== session) return;
        state.sourceBr = {
          key, session, scene,
          bins: (data && data.bins) || [],
        };
      })
      .catch(() => {
        if (state.vmafSession !== session) return;
        state.sourceBr = { key, session, scene, bins: [] };
      });
  }

  function originalBitrateDataset(bins, col, data) {
    return {
      label: tt("Original"),
      yAxisID: "y1",
      order: 3,
      data,
      borderColor: col.text,
      backgroundColor: "transparent",
      borderDash: [2, 2],
      pointRadius: 0,
      borderWidth: 1.6,
      tension: 0.15,
      spanGaps: true,
    };
  }

  function drawFrameChart(vmaf) {
    const wrap = $("vmaf-frame-wrap");
    const ctx = $("vmaf-frame-chart");
    const title = $("vmaf-frame-title");
    destroyNamedChart("vmafFrameChart");
    if (state.vmafNerd) {
      if (wrap) wrap.hidden = true;
      return;
    }
    const scenes = vmafSceneList(vmaf);
    const scene = state.chartScene != null
      ? state.chartScene
      : (scenes.length === 1 ? scenes[0] : null);
    const rows = (vmaf.results || []).filter((r) => {
      const sc = scene == null ? null : sceneEntry(r, scene);
      return sc && Array.isArray(sc.frames) && sc.frames.length > 1;
    });
    if (!wrap || !ctx || scene == null || !rows.length) {
      if (wrap) wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    const clipSec = Number(vmaf.clip_seconds) || 0;
    const srcBins = sourceBinsFor(scene);
    const hasBr = clipSec > 0 && rows.some((r) => {
      const sc = sceneEntry(r, scene);
      return sc && sc.bitrate && sc.bitrate.length;
    });
    const timed = hasBr || (clipSec > 0 && srcBins.length > 0);
    if (title) {
      title.textContent = timed
        ? `Verlauf Szene ${scene + 1} · VMAF und Bitrate der Testclips`
        : `Verlauf Szene ${scene + 1} · Tiefstwert je Abschnitt`;
    }
    const col = chartColors();
    const datasets = [];
    rows.forEach((r, i) => {
      const sc = sceneEntry(r, scene);
      const color = r.recommended ? col.good : CHART_PALETTE[i % CHART_PALETTE.length];
      const label = r.label || ("Q" + r.quality);
      const n = sc.frames.length;
      const span = scoredSpan(clipSec, sc);
      if (hasBr && sc.bitrate && sc.bitrate.length) {
        datasets.push({
          label: label + " · Bitrate",
          yAxisID: "y1",
          order: 2,
          data: sc.bitrate.map((b) => ({ x: bitratePointX(b), y: Number(b.kbps) })),
          borderColor: color,
          backgroundColor: "transparent",
          borderDash: [5, 4],
          pointRadius: 0,
          borderWidth: 1.3,
          tension: 0.15,
          spanGaps: true,
        });
      }
      datasets.push({
        label,
        yAxisID: "y",
        order: 0,
        data: timed
          ? sc.frames.map((v, idx) => ({ x: (idx + 0.5) / n * span, y: v }))
          : sc.frames,
        borderColor: color,
        backgroundColor: "transparent",
        pointRadius: 0,
        borderWidth: r.recommended ? 2.4 : 1.5,
        tension: 0.2,
        spanGaps: true,
      });
    });
    if (timed && srcBins.length) {
      datasets.push(originalBitrateDataset(
        srcBins, col,
        srcBins.map((b) => ({ x: bitratePointX(b), y: Number(b.kbps) })),
      ));
    }
    const opts = lineChartOptions(col, "VMAF");
    opts.scales.y.suggestedMin = 80;
    opts.scales.y.suggestedMax = 100;
    opts.plugins.legend.position = "bottom";
    opts.plugins.legend.labels.boxWidth = 12;
    opts.plugins.legend.labels.filter = (item) => !String(item.text || "").endsWith("· Bitrate");
    if (timed) {
      opts.scales.x.type = "linear";
      opts.scales.x.title = { display: true, text: "Sekunden", color: col.muted };
      opts.scales.x.ticks.autoSkip = true;
      opts.scales.x.ticks.maxTicksLimit = 8;
      opts.scales.y1 = {
        position: "right",
        title: { display: true, text: "kbit/s", color: col.muted },
        grid: { drawOnChartArea: false },
        ticks: { color: col.muted },
      };
      opts.interaction = { mode: "x", intersect: false };
      opts.plugins.tooltip = {
        mode: "x",
        intersect: false,
        filter: (item) => !(item.dataset && item.dataset.yAxisID === "y1"),
      };
      state.vmafFrameChart = new Chart(ctx, {
        type: "line",
        data: { datasets },
        options: opts,
      });
      attachVmafZoom(state.vmafFrameChart, {
        ySuggest: [80, 100],
        xMode: "linear",
        extent: datasetXExtent(datasets),
        labelCount: 0,
      });
      return;
    }
    const n = Math.max(...rows.map((r) => sceneEntry(r, scene).frames.length));
    opts.scales.x.ticks.autoSkip = false;
    state.vmafFrameChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: Array.from({ length: n }, (_, i) => {
          if (i === 0) return "Anfang";
          if (i === n - 1) return "Ende";
          if (i === Math.floor(n / 2)) return "Mitte";
          return "";
        }),
        datasets,
      },
      options: opts,
    });
    attachVmafZoom(state.vmafFrameChart, {
      ySuggest: [80, 100],
      xMode: "index",
      extent: null,
      labelCount: n,
    });
  }

  function refreshNerdFrames() {
    const wrap = $("vmaf-nerd");
    const btn = $("btn-vmaf-nerd");
    if (btn) btn.classList.toggle("active", !!state.vmafNerd);
    if (!wrap) return;
    if (!state.vmafNerd) {
      wrap.hidden = true;
      destroyNamedChart("vmafNerdChart");
      return;
    }
    const frame = $("vmaf-frame-wrap");
    if (frame) frame.hidden = true;
    destroyNamedChart("vmafFrameChart");
    wrap.hidden = false;
    const vmaf = state.vmafShown;
    const scenes = vmaf ? vmafSceneList(vmaf) : [];
    const scene = state.chartScene != null ? state.chartScene : (scenes[0] ?? 0);
    const session = state.vmafSession;
    const title = $("vmaf-nerd-title");
    const body = $("vmaf-nerd-body");
    if (title) title.textContent = `Daten für Nerds · Szene ${scene + 1} · jeder bewertete Frame`;
    if (!session) {
      destroyNamedChart("vmafNerdChart");
      if (body) body.innerHTML = `<p class="hint">Rohdaten gibt es erst bei einem gespeicherten Vergleich.</p>`;
      return;
    }
    const key = `${session}:${scene}`;
    if (state.nerdKey === key && state.nerdData) {
      drawNerdFrames(state.nerdData);
      return;
    }
    state.nerdKey = key;
    if (body) body.innerHTML = `<p class="hint">Frame-Log wird gelesen …</p>`;
    fetch(`/api/vmaf/frames?session=${encodeURIComponent(session)}&scene=${scene}`)
      .then((r) => r.json())
      .then((data) => {
        if (state.nerdKey !== key || !state.vmafNerd) return;
        state.nerdData = data;
        drawNerdFrames(data);
      })
      .catch(() => {
        if (body) body.innerHTML = `<p class="hint">Frame-Log konnte nicht geladen werden.</p>`;
      });
  }

  function nerdFloorValue() {
    const el = $("nerd-floor");
    let v = el ? Number(el.value) : 90;
    if (!Number.isFinite(v)) v = 90;
    return Math.min(99, Math.max(80, Math.round(v)));
  }

  function nerdDipText(frames, floor, frameSec) {
    const list = frames || [];
    const n = list.length;
    if (!n) return "";
    let under = 0;
    let longest = 0;
    let cur = 0;
    let startAt = 0;
    let at = 0;
    list.forEach((f) => {
      if (Number(f.vmaf) < floor) {
        under += 1;
        if (cur === 0) at = f.n;
        cur += 1;
        if (cur > longest) { longest = cur; startAt = at; }
      } else {
        cur = 0;
      }
    });
    const pct = (100 * under / n).toFixed(1);
    if (longest > 0) {
      let line = `${pct} % unter ${floor} · längster Einbruch ${longest} Frames ab Frame ${startAt}`;
      const sec = frameSec > 0 ? longest * frameSec : 0;
      if (sec) line += ` (~${sec.toFixed(2)} s)`;
      return line;
    }
    return `${pct} % unter ${floor} · kein Frame unter ${floor}`;
  }

  function applyNerdFloor() {
    const floor = nerdFloorValue();
    const lab = $("nerd-floor-val");
    if (lab) lab.textContent = String(floor);
    const slider = $("nerd-floor");
    if (slider && slider.value !== String(floor)) slider.value = String(floor);
    const series = (state.nerdData && state.nerdData.series) || [];
    document.querySelectorAll("#vmaf-nerd-body .vmaf-nerd-dip, #vmaf-nerd-body .vmaf-nerd-dip-short").forEach((el) => {
      const s = series[Number(el.getAttribute("data-nerd-i"))];
      if (!s) return;
      const full = nerdDipText(s.frames, floor, Number(s.frame_sec) || 0);
      el.textContent = el.classList.contains("vmaf-nerd-dip-short")
        ? full.split(" · ")[0]
        : full;
    });
    const chart = state.vmafNerdChart;
    if (!chart) return;
    const ds = (chart.data.datasets || []).find((d) => d.floorLine);
    if (!ds) return;
    ds.data = ds.data.map(() => floor);
    ds.label = tt("Schwelle " + floor);
    chart.update("none");
  }

  function nerdSharedLine(series) {
    const results = (state.vmafShown && state.vmafShown.results) || [];
    const rows = (series || []).map((s) =>
      results.find((r) => (r.label || "") === (s.label || ""))).filter(Boolean);
    if (!rows.length) return "";
    const same = (pick) => {
      const vals = rows.map(pick);
      return vals.every((v) => v === vals[0]) ? vals[0] : null;
    };
    const parts = [];
    const plat = same((r) => r.platform || "");
    if (plat === "nvidia") parts.push("NVIDIA");
    else if (plat === "cpu") parts.push("CPU");
    else if (plat) parts.push(plat);
    const sp = same((r) => r.encoder_speed || "");
    if (sp) parts.push(tt(speedLabelFor(rows[0].platform, rows[0].codec, sp) || sp));
    const allNvidia = rows.every((r) => (r.platform || "") === "nvidia");
    if (allNvidia) {
      const bf = same((r) => r.b_frames || "");
      const bfTags = {
        auto: "Automatisch", off: "Aus, Lookahead 32", short: "Kurz",
        medium: "Mittel", deep: "Tief",
      };
      if (bf) parts.push(tt(bfTags[bf] || bf));
      const tune = same((r) => r.nvenc_tune || "");
      const tuneTags = {
        auto: "Tune automatisch", off: "Tune aus", hq: "HQ", uhq: "UHQ",
      };
      if (tune) parts.push(tt(tuneTags[tune] || tune));
    }
    const aq = same((r) => (r.aq_strength ? String(r.aq_strength) : ""));
    if (aq && rows.every((r) => aqMode(r.platform, r.codec))) parts.push("AQ " + aq);
    const ki = same((r) => (
      r.keyint_sec == null || r.keyint_sec === "" ? null : String(r.keyint_sec)));
    if (ki != null) {
      const n = Number(ki);
      parts.push(n ? (n + " s") : tt("Keyframe automatisch"));
    }
    if (same((r) => (r.two_pass ? "1" : "0")) === "1") parts.push(tt("Zwei-Pass"));
    return parts.length ? (tt("Für alle") + ": " + parts.join(" · ")) : "";
  }

  function drawNerdFrames(data) {
    const body = $("vmaf-nerd-body");
    const ctx = $("vmaf-nerd-chart");
    destroyNamedChart("vmafNerdChart");
    const series = (data && data.series) || [];
    if (!series.length) {
      if (body) {
        body.innerHTML = `<p class="hint">Für diese Szene liegt kein Frame-Log im Archiv.</p>`;
      }
      return;
    }
    const col = chartColors();
    const n = Math.max(...series.map((s) => (s.frames || []).length));
    const labels = Array.from({ length: n }, (_, i) => String(i + 1));
    const nerdKeyOf = (s, i) => s.label || ("Serie " + (i + 1));
    const nerdOff = (key) => !!(state.nerdHidden && state.nerdHidden.has(key));
    let nerdHasOriginal = false;
    const datasets = series.map((s, i) => {
      const color = CHART_PALETTE[i % CHART_PALETTE.length];
      const key = nerdKeyOf(s, i);
      return {
        label: key,
        nerdKey: key,
        hidden: nerdOff(key),
        data: (s.frames || []).map((f) => f.vmaf),
        borderColor: color,
        backgroundColor: "transparent",
        pointRadius: 0,
        borderWidth: 1.4,
        tension: 0.05,
        spanGaps: true,
      };
    });
    const nerdScene = data && data.scene;
    if (state.vmafShown && nerdScene != null) {
      series.forEach((s, i) => {
        const result = (state.vmafShown.results || []).find(
          (r) => (r.label || "") === (s.label || ""));
        const sc = result && sceneEntry(result, nerdScene);
        if (!bitrateIsFrame(sc)) return;
        const byN = {};
        sc.bitrate.forEach((b) => { byN[b.n] = Number(b.kbps); });
        const color = CHART_PALETTE[i % CHART_PALETTE.length];
        const key = nerdKeyOf(s, i);
        datasets.push({
          label: key + " · Bitrate",
          nerdKey: key,
          hidden: nerdOff(key),
          yAxisID: "y1",
          data: (s.frames || []).map((f) => (byN[f.n] == null ? null : byN[f.n])),
          borderColor: color,
          backgroundColor: "transparent",
          borderDash: [5, 4],
          pointRadius: 0,
          borderWidth: 1.2,
          tension: 0.05,
          spanGaps: true,
        });
      });
      const srcBins = sourceBinsFor(nerdScene);
      const frameSec = Number(series[0] && series[0].frame_sec) || 0;
      if (srcBins.length && frameSec > 0) {
        let j = 0;
        const data = [];
        for (let i = 0; i < n; i++) {
          const t = i * frameSec;
          while (j + 1 < srcBins.length
            && Math.abs(Number(srcBins[j + 1].t) - t) <= Math.abs(Number(srcBins[j].t) - t)) {
            j += 1;
          }
          data.push(Number(srcBins[j].kbps));
        }
        const src = originalBitrateDataset(srcBins, col, data);
        src.nerdKey = "original";
        src.hidden = nerdOff("original");
        nerdHasOriginal = true;
        datasets.push(src);
      }
    }
    const floor = nerdFloorValue();
    datasets.push({
      label: tt("Schwelle " + floor),
      floorLine: true,
      data: Array.from({ length: Math.max(n, 1) }, () => floor),
      borderColor: col.warn || "#fbbf24",
      backgroundColor: "transparent",
      borderDash: [6, 4],
      borderWidth: 1.6,
      pointRadius: 0,
      pointHoverRadius: 0,
      tension: 0,
      order: -1,
    });
    if (ctx && typeof Chart !== "undefined") {
      const opts = lineChartOptions(col, "VMAF");
      opts.plugins.legend.display = false;
      opts.scales.y.suggestedMin = 70;
      opts.scales.y.suggestedMax = 100;
      if (datasets.some((d) => d.yAxisID === "y1")) {
        opts.scales.y1 = {
          position: "right",
          title: { display: true, text: "kbit/s", color: col.muted },
          grid: { drawOnChartArea: false },
          ticks: { color: col.muted },
        };
      }
      opts.scales.x.ticks.autoSkip = true;
      opts.scales.x.ticks.maxTicksLimit = 12;
      opts.plugins.tooltip = {
        filter: (item) => !(item.dataset && item.dataset.floorLine),
        callbacks: {
          title: (items) => {
            const i = items[0] ? items[0].dataIndex : 0;
            const fr = series[0] && series[0].frames && series[0].frames[i];
            return fr ? `Frame ${fr.n}` : `Frame ${i}`;
          },
        },
      };
      state.vmafNerdChart = new Chart(ctx, {
        type: "line",
        data: { labels, datasets },
        options: opts,
      });
      attachVmafZoom(state.vmafNerdChart, {
        ySuggest: [70, 100],
        xMode: "index",
        extent: null,
        labelCount: labels.length,
      });
    }
    if (!body) return;
    const legend = "σ ist die Streuung der Frame-VMAFs um den Schnitt dieser Szene. Klein heißt: die Qualität liegt eng beieinander, nicht dass die Filmszene ruhig ist. Liegt der Median über dem Schnitt, zieht ein schlechter Schwanz den Schnitt nach unten. Der längste Einbruch zählt aufeinanderfolgende Frames unter dem Regler. Die Sekunden sind Clip-Länge durch bewertete Frames.";
    const num = (v) => Number(v).toFixed(2);
    const cell = (v) => (v == null || v === "" ? "" : escapeHtml(num(v)));
    const rows = series.map((s, i) => {
      const key = nerdKeyOf(s, i);
      const color = CHART_PALETTE[i % CHART_PALETTE.length];
      const st = s.stats || {};
      const worst = (s.worst || []).map((f) => {
        const extra = [
          f.psnr != null ? `PSNR ${Number(f.psnr).toFixed(1)}` : "",
          f.ssim != null ? `SSIM ${Number(f.ssim).toFixed(3)}` : "",
        ].filter(Boolean).join(" · ");
        return `<li>Frame ${f.n} · VMAF ${Number(f.vmaf).toFixed(2)}`
          + (extra ? ` · ${extra}` : "") + `</li>`;
      }).join("");
      const dipFull = (s.frames || []).length
        ? nerdDipText(s.frames, floor, Number(s.frame_sec) || 0)
        : "";
      let psnr = "";
      if (st.psnr_mean != null) {
        const d = Number(st.psnr_delta);
        const mark = d >= 0 ? "−" : "+";
        psnr = `PSNR schwache 5 % ${num(st.psnr_weak)} · Schnitt ${num(st.psnr_mean)} · ${mark}${Math.abs(d).toFixed(2)} dB`;
      }
      const extraBits = [
        s.count != null ? `${s.count} Frames` : "",
        st.p5 != null ? `P5 ${num(st.p5)} · P95 ${num(st.p95)}` : "",
        psnr,
      ].filter(Boolean);
      const details = `<details><summary>${escapeHtml(tt("schwächste Frames"))}</summary>`
        + extraBits.map((l) => `<p class="hint">${escapeHtml(l)}</p>`).join("")
        + (dipFull
          ? `<p class="hint vmaf-nerd-dip" data-nerd-i="${i}">${escapeHtml(dipFull)}</p>`
          : "")
        + (worst ? `<ol class="vmaf-nerd-worst">${worst}</ol>` : "")
        + `</details>`;
      return `<tr>
        <td><input type="checkbox" class="nerd-show" data-nerd-key="${escapeHtml(key)}"${nerdOff(key) ? "" : " checked"}></td>
        <td class="nerd-name"><span class="nerd-swatch" style="background:${color}"></span>${escapeHtml(key)}${details}</td>
        <td>${cell(st.mean)}</td>
        <td>${cell(st.median)}</td>
        <td>${cell(st.stdev)}</td>
        <td>${cell(st.p1)}</td>
        <td>${cell(st.min)}</td>
        <td>${cell(st.max)}</td>
        <td class="vmaf-nerd-dip-short" data-nerd-i="${i}">${escapeHtml(dipFull.split(" · ")[0] || "")}</td>
      </tr>`;
    }).join("");
    const originalRow = nerdHasOriginal
      ? `<tr>
        <td><input type="checkbox" class="nerd-show" data-nerd-key="original"${nerdOff("original") ? "" : " checked"}></td>
        <td class="nerd-name" colspan="8"><span class="nerd-swatch" style="background:${col.text}"></span>${escapeHtml(tt("Original"))}</td>
      </tr>`
      : "";
    const shared = nerdSharedLine(series);
    body.innerHTML = (shared ? `<p class="vmaf-nerd-shared">${escapeHtml(shared)}</p>` : "")
      + `<p class="hint vmaf-nerd-legend" title="${escapeHtml(tt(legend))}">${escapeHtml(tt("Haken zeigt VMAF und Bitrate dieser Zeile im Graphen."))}</p>`
      + `<table class="data-table vmaf-nerd-table"><thead><tr>`
      + `<th></th><th>${escapeHtml(tt("Einstellung"))}</th>`
      + `<th>${escapeHtml(tt("Schnitt"))}</th><th>${escapeHtml(tt("Median"))}</th><th>σ</th>`
      + `<th>${escapeHtml(tt("1%-Low"))}</th><th>${escapeHtml(tt("Min"))}</th><th>${escapeHtml(tt("Max"))}</th>`
      + `<th title="${escapeHtml(tt(legend))}">${escapeHtml(tt("unter"))}</th>`
      + `</tr></thead><tbody>${rows}${originalRow}</tbody></table>`;
    body.querySelectorAll(".nerd-show").forEach((box) => {
      box.addEventListener("change", () => {
        const key = box.getAttribute("data-nerd-key");
        if (!state.nerdHidden) state.nerdHidden = new Set();
        if (box.checked) state.nerdHidden.delete(key);
        else state.nerdHidden.add(key);
        const chart = state.vmafNerdChart;
        if (!chart) return;
        (chart.data.datasets || []).forEach((d, idx) => {
          if (d.nerdKey !== key) return;
          chart.setDatasetVisibility(idx, box.checked);
        });
        chart.update("none");
      });
    });
    const floorLab = $("nerd-floor-val");
    if (floorLab) floorLab.textContent = String(floor);
  }

  function renderChartScenes(vmaf) {
    const bar = $("vmaf-chart-scenes");
    if (!bar) return;
    const scenes = vmafSceneList(vmaf);
    if (scenes.length < 2) {
      bar.innerHTML = "";
      return;
    }
    if (state.chartScene != null && !scenes.includes(state.chartScene))
      state.chartScene = null;
    const cur = state.chartScene;
    bar.innerHTML = `<button type="button" class="shot-scene ${cur == null ? "active" : ""}" data-chart-scene="">Gesamt</button>`
      + scenes.map((n) =>
        `<button type="button" class="shot-scene ${cur === n ? "active" : ""}" data-chart-scene="${n}">Szene ${n + 1}</button>`
      ).join("");
    bar.querySelectorAll("[data-chart-scene]").forEach((b) => {
      b.addEventListener("click", () => {
        const raw = b.getAttribute("data-chart-scene");
        state.chartScene = raw === "" ? null : +raw;
        showVmafChart(vmaf);
        if (state.chartScene != null) {
          const grid = $("vmaf-screenshots");
          if (grid && grid._shotUi) {
            grid._shotUi.scene = state.chartScene;
            renderScreenshots(vmaf, grid);
          }
        }
      });
    });
  }

  function resultSeriesName(r, results) {
    const base = r.codec_disp || r.codec || "";
    const same = (results || []).filter((x) =>
      (x.platform || "") === (r.platform || "") && (x.codec || "") === (r.codec || ""));
    const speeds = new Set(same.map((x) => x.encoder_speed || ""));
    const bfs = new Set(same.map((x) => x.b_frames || ""));
    const aqs = new Set(same.map((x) => String(x.aq_strength ?? "")));
    const kis = new Set(same.map((x) => String(x.keyint_sec ?? "")));
    const tunes = new Set(same.map((x) => x.nvenc_tune || ""));
    const modes = new Set(same.map((x) => x.rate_mode || "cq"));
    const twos = new Set(same.map((x) => (x.two_pass ? "1" : "0")));
    const tags = {
      auto: "Automatisch", off: "Aus", short: "Kurz", medium: "Mittel", deep: "Tief",
    };
    const modeTags = { cq: "CQ", bitrate: "CBR", abr: "ABR" };
    const tuneTags = {
      auto: "Tune automatisch", off: "Tune aus", hq: "HQ", uhq: "UHQ",
    };
    const extra = [];
    if (speeds.size > 1 && r.encoder_speed) extra.push(r.encoder_speed);
    if (bfs.size > 1 && r.b_frames) extra.push(tags[r.b_frames] || r.b_frames);
    if (tunes.size > 1 && r.nvenc_tune) extra.push(tt(tuneTags[r.nvenc_tune] || r.nvenc_tune));
    if (aqs.size > 1 && r.aq_strength) extra.push("AQ " + r.aq_strength);
    if (kis.size > 1) extra.push(r.keyint_sec ? (r.keyint_sec + " s") : "Keyframe automatisch");
    if (modes.size > 1) extra.push(modeTags[r.rate_mode] || r.rate_mode || "CQ");
    if (twos.size > 1 && r.two_pass) extra.push(tt("Zwei-Pass"));
    return extra.length ? `${base} · ${extra.join(" · ")}` : base;
  }

  function resultHasSettingVariants(results) {
    const rows = results || [];
    const keys = new Set(rows.map((r) =>
      [r.platform, r.codec, r.encoder_speed || "", r.b_frames || "",
       r.nvenc_tune || "", r.aq_strength || "", r.keyint_sec || "",
       r.rate_mode || "cq", r.two_pass ? "1" : "0"].join("|")));
    const pcs = new Set(rows.map((r) => [r.platform, r.codec].join("|")));
    return keys.size > pcs.size;
  }

  function drawChart(vmaf) {
    if (typeof Chart === "undefined") return;
    if (vmaf.multi_codec || resultHasSettingVariants(vmaf.results)) return drawChartMultiCodec(vmaf);

    const ctx = $("vmaf-chart");
    const col = chartColors();
    const rows = vmaf.results || [];
    const scene = state.chartScene;
    const sceneMode = scene != null;
    const labels = rows.map((r) => r.label || ("Q" + r.quality));
    const scores = rows.map((r) => (sceneMode ? sceneScoreOf(r, scene) : r.vmaf));
    const savings = rows.map((r) => r.savings_percent);
    const lows = rows.map((r) => {
      if (!sceneMode) return r.vmaf_1pct != null ? r.vmaf_1pct : null;
      const sc = sceneEntry(r, scene);
      return sc && sc.p1 != null ? sc.p1 : null;
    });
    const hasLow = lows.some((v) => v != null);
    const mins = rows.map((r) => (r.vmaf_min != null ? r.vmaf_min : null));
    const maxes = rows.map((r) => (r.vmaf_max != null ? r.vmaf_max : null));
    const hasBand = mins.some((v) => v != null) && maxes.some((v) => v != null);
    const pointColors = rows.map((r) => (r.recommended ? col.good : col.accent));
    const pointRadius = rows.map((r) => (r.recommended ? 8 : 4));

    const datasets = [];
    if (hasBand && !sceneMode) {
      datasets.push({
        label: "Szenen min", data: mins, yAxisID: "y",
        borderColor: "transparent", backgroundColor: "transparent",
        pointRadius: 0, pointHoverRadius: 0, tension: 0.3, borderWidth: 0,
        fill: false, spanGaps: true,
      });
      datasets.push({
        label: "Szenen (min–max)", data: maxes, yAxisID: "y",
        borderColor: "transparent", backgroundColor: col.band,
        pointRadius: 0, pointHoverRadius: 0, tension: 0.3, borderWidth: 0,
        fill: "-1", spanGaps: true,
      });
    }
    datasets.push({
      label: sceneMode ? `VMAF · Szene ${scene + 1}` : "VMAF-Mittel",
      data: scores, yAxisID: "y",
      borderColor: col.accent, backgroundColor: "transparent",
      pointBackgroundColor: pointColors, pointRadius, pointHoverRadius: 9,
      tension: 0.3, borderWidth: 2.5, fill: false, spanGaps: true,
    });
    if (hasLow) {
      datasets.push({
        label: sceneMode ? `1%-Low · Szene ${scene + 1}` : "1%-Low",
        data: lows, yAxisID: "y",
        borderColor: col.p1, backgroundColor: "transparent",
        borderDash: [5, 4], pointRadius: 3, pointHoverRadius: 6,
        tension: 0.3, borderWidth: 2, fill: false, spanGaps: true,
      });
    }
    datasets.push({
      label: "Ersparnis %", data: savings, yAxisID: "y1",
      borderColor: col.accent2, backgroundColor: "transparent",
      borderDash: [5, 4], pointRadius: 3, tension: 0.3, borderWidth: 1.8,
      fill: false,
    });

    if (state.vmafChart) state.vmafChart.destroy();
    const mainChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false, resizeDelay: 150,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            labels: {
              color: col.text, font: { size: 12 },
              filter: (item) => item.text !== "Szenen min",
            },
          },
          annotation: {},
          tooltip: { callbacks: {
            afterBody: (ctxs) => {
              const i = ctxs[0].dataIndex;
              const r = rows[i];
              if (!r) return "";
              const lines = [];
              const sc = sceneMode ? sceneEntry(r, scene) : null;
              if (sceneMode) {
                if (sc && sc.p1 != null) lines.push(`1%-Low ${Number(sc.p1).toFixed(1)}`);
                if (sc && sc.hmean != null) lines.push(`H-Ø ${Number(sc.hmean).toFixed(1)}`);
                if (sc && sc.xpsnr != null) {
                  const m = xpsnrMark(sc.xpsnr);
                  const mn = sc.xpsnr_min != null && Number(sc.xpsnr_min) > 0
                    ? `, min ${Number(sc.xpsnr_min).toFixed(1)}` : "";
                  lines.push(`XPSNR ${m.text} (${m.word}${mn})`);
                }
                if (sc && sc.psnr != null) lines.push(`PSNR ${Number(sc.psnr).toFixed(1)} dB`);
                if (sc && sc.ssim != null) lines.push(`SSIM ${Number(sc.ssim).toFixed(3)}`);
                if (sc && sc.vmaf != null)
                  lines.push(`Abstand zum Schnitt ${(Number(sc.vmaf) - r.vmaf).toFixed(1)}`);
                lines.push("Ersparnis = Schätzung für die ganze Datei");
              } else {
                if (r.vmaf_1pct != null) lines.push(`1%-Low ${Number(r.vmaf_1pct).toFixed(1)}`);
                if (r.vmaf_hmean != null) lines.push(`H-Ø ${Number(r.vmaf_hmean).toFixed(1)}`);
                if (r.vmaf_min != null && r.vmaf_max != null)
                  lines.push(`Szenen ${Number(r.vmaf_min).toFixed(1)}–${Number(r.vmaf_max).toFixed(1)}`);
                if (r.xpsnr != null) {
                  const m = xpsnrMark(r.xpsnr);
                  lines.push(`XPSNR ${m.text} (${m.word})`);
                }
              }
              if (r.recommended) lines.push("★ Empfohlener Sweet Spot");
              const bits = bitrateReport(r, sceneMode && sc ? sc.kbps : null);
              if (bits) lines.push(bits);
              return lines;
            },
          }},
        },
        scales: {
          x: { grid: { color: col.grid }, ticks: { color: col.muted } },
          y: {
            position: "left",
            title: {
              display: true,
              text: sceneMode ? `VMAF · Szene ${scene + 1}` : "VMAF",
              color: col.muted,
            },
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
    state.vmafChart = mainChart;
    attachVmafZoom(mainChart, { ySuggest: [80, 100], xMode: null });
  }

  // Mehrere Codecs: faire Achse = VMAF (y) vs. Ersparnis % (x). Je Codec eine
  // Kurve; weiter oben-rechts = besser (mehr Qualität bei mehr Ersparnis).
  function drawChartMultiCodec(vmaf) {
    const ctx = $("vmaf-chart");
    const col = chartColors();
    const scene = state.chartScene;
    const sceneMode = scene != null;
    const groups = {};
    vmaf.results.forEach((r) => {
      const key = resultSeriesName(r, vmaf.results);
      (groups[key] = groups[key] || []).push(r);
    });

    const yOf = (r) => (sceneMode ? sceneScoreOf(r, scene) : r.vmaf);
    const datasets = [];
    Object.keys(groups).forEach((name, gi) => {
      const color = CHART_PALETTE[gi % CHART_PALETTE.length];
      const pts = groups[name].slice().sort((a, b) => a.savings_percent - b.savings_percent);
      datasets.push({
        label: sceneMode ? `${name} · Szene ${scene + 1}` : name,
        data: pts.map((r) => ({ x: r.savings_percent, y: yOf(r), _r: r })),
        borderColor: color, backgroundColor: "transparent",
        pointBackgroundColor: pts.map((r) => (r.recommended ? col.good : color)),
        pointRadius: pts.map((r) => (r.recommended ? 8 : 4)),
        pointHoverRadius: 9, tension: 0.25, borderWidth: 2.4, showLine: true, spanGaps: true,
      });
      if (pts.some((r) => (sceneMode ? sceneEntry(r, scene) && sceneEntry(r, scene).p1 != null : r.vmaf_1pct != null))) {
        datasets.push({
          label: sceneMode ? `${name} · 1%-Low Szene ${scene + 1}` : `${name} · 1%-Low`,
          data: pts.map((r) => {
            const sc = sceneMode ? sceneEntry(r, scene) : null;
            const y = sceneMode ? (sc && sc.p1 != null ? sc.p1 : null) : (r.vmaf_1pct != null ? r.vmaf_1pct : null);
            return { x: r.savings_percent, y, _r: r };
          }),
          borderColor: color, backgroundColor: "transparent",
          borderDash: [5, 4], pointRadius: 3, pointHoverRadius: 6,
          tension: 0.25, borderWidth: 1.6, showLine: true, spanGaps: true,
        });
      }
    });

    if (state.vmafChart) state.vmafChart.destroy();
    const multiChart = new Chart(ctx, {
      type: "scatter",
      data: { datasets },
      options: {
        responsive: true, maintainAspectRatio: false, resizeDelay: 150,
        plugins: {
          legend: { labels: { color: col.text, font: { size: 12 } } },
          tooltip: { callbacks: {
            label: (c) => {
              const r = c.raw && c.raw._r;
              if (!r) return "";
              const extra = [];
              const sc = sceneMode ? sceneEntry(r, scene) : null;
              if (sceneMode && sc) {
                if (sc.p1 != null) extra.push(`1%-Low ${Number(sc.p1).toFixed(1)}`);
                if (sc.hmean != null) extra.push(`H-Ø ${Number(sc.hmean).toFixed(1)}`);
                if (sc.xpsnr != null) extra.push(`XPSNR ${Number(sc.xpsnr).toFixed(1)} dB`);
                if (sc.psnr != null) extra.push(`PSNR ${Number(sc.psnr).toFixed(1)}`);
                if (sc.vmaf != null) extra.push(`Δ ${(Number(sc.vmaf) - r.vmaf).toFixed(1)}`);
              } else {
                if (r.vmaf_1pct != null) extra.push(`1%-Low ${Number(r.vmaf_1pct).toFixed(1)}`);
                if (r.vmaf_min != null && r.vmaf_max != null)
                  extra.push(`Szenen ${Number(r.vmaf_min).toFixed(1)}–${Number(r.vmaf_max).toFixed(1)}`);
                if (r.xpsnr != null) extra.push(`XPSNR ${Number(r.xpsnr).toFixed(1)} dB`);
              }
              const v = sceneMode ? sceneScoreOf(r, scene) : r.vmaf;
              const bits = bitrateReport(r, sceneMode && sc ? sc.kbps : null);
              return `${c.dataset.label} ${String(r.label || "").split("·").pop().trim()}: `
                + `VMAF ${v != null ? Number(v).toFixed(1) : "—"} · ${r.predicted_human} (${r.savings_percent}%)`
                + (bits ? ` · ${bits}` : "")
                + (extra.length ? ` · ${extra.join(" · ")}` : "")
                + (sceneMode ? " · Ersparnis = ganze Datei" : "")
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
            title: {
              display: true,
              text: sceneMode ? `VMAF · Szene ${scene + 1}` : "VMAF",
              color: col.muted,
            },
            suggestedMin: 80, suggestedMax: 100,
            grid: { color: col.grid },             ticks: { color: col.muted },
          },
        },
      },
    });
    state.vmafChart = multiChart;
    attachVmafZoom(multiChart, { ySuggest: [80, 100], xMode: null });
  }

  function restyleChart() {
    state.lastVmafKey = null; // erzwingt Neuzeichnen mit neuen Theme-Farben
  }

  /* ---------------------------------------------------------- DATA BROWSER */
  const dataState = { root: "vmaf", path: "", selected: new Set() };

  function initDataBrowser() {
    $("data-root").addEventListener("change", (e) => {
      dataState.root = e.target.value;
      dataState.path = "";
      dataState.selected = new Set();
      loadDataDir();
    });
    $("btn-data-refresh").addEventListener("click", () => {
      loadDataDir();
      refreshStorageBadge();
    });
    $("btn-data-delete-all").addEventListener("click", deleteAllInDataRoot);
    const delSel = $("btn-data-delete-sel");
    if (delSel) delSel.addEventListener("click", deleteSelectedData);
    const selAll = $("data-select-all");
    if (selAll) selAll.addEventListener("change", () => {
      document.querySelectorAll("#data-browser .row-sel").forEach((box) => {
        box.checked = selAll.checked;
        const row = box.closest(".row-item");
        if (row) row.classList.toggle("selected", selAll.checked);
        const rel = box.dataset.rel;
        if (!rel) return;
        if (selAll.checked) dataState.selected.add(rel);
        else dataState.selected.delete(rel);
      });
      syncDataSelection();
    });
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
    dataState.selected = new Set();
    syncDataSelection();
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
    syncDataSelection();
  }

  function dataRow(item, data, isDir) {
    const row = document.createElement("div");
    row.className = "row-item";
    const icon = isDir ? "📁" : fileIcon(item);
    row.innerHTML = `
      <span class="row-icon">${icon}</span>
      <span class="row-name">${escapeHtml(item.name)}</span>
      <span class="row-size">${item.size_human || ""}</span>`;

    if (item.name !== "..") {
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "row-sel";
      box.dataset.rel = item.rel;
      box.addEventListener("click", (e) => e.stopPropagation());
      box.addEventListener("change", () => {
        if (box.checked) dataState.selected.add(item.rel);
        else dataState.selected.delete(item.rel);
        row.classList.toggle("selected", box.checked);
        syncDataSelection();
      });
      row.insertBefore(box, row.firstChild);
    }

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

  function syncDataSelection() {
    const n = dataState.selected.size;
    const btn = $("btn-data-delete-sel");
    if (btn) {
      btn.disabled = n === 0;
      btn.textContent = n ? `Auswahl löschen (${n})` : "Auswahl löschen";
    }
    const all = $("data-select-all");
    const boxes = [...document.querySelectorAll("#data-browser .row-sel")];
    if (all) {
      all.disabled = boxes.length === 0;
      all.checked = boxes.length > 0 && boxes.every((b) => b.checked);
      all.indeterminate = n > 0 && !all.checked;
    }
  }

  async function deleteSelectedData() {
    const paths = [...dataState.selected];
    if (!paths.length) return;
    const names = paths.map((rel) => rel.split("/").pop());
    const head = paths.length === 1
      ? `„${names[0]}" wirklich löschen?`
      : `${paths.length} Einträge löschen? Das kann nicht rückgängig gemacht werden.`;
    const list = paths.length > 1 ? "\n" + names.slice(0, 12).join("\n") : "";
    if (!confirm(tt(head) + list)) return;
    const res = await fetch("/api/data/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: dataState.root, paths }),
    });
    const data = await res.json();
    if (data.error && !data.deleted) {
      alert(data.error);
      return;
    }
    if (data.error) alert(data.error);
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

  function openModal(title, html, opts) {
    const m = ensureModal();
    const box = m.querySelector(".app-modal-box");
    if (box) {
      box.classList.toggle("app-modal-nfo", !!(opts && opts.nfo));
      box.classList.toggle("app-modal-player", !!(opts && opts.player));
    }
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

  /** Ob Browser Video+Ton vermutlich nativ abspielen kann (volle Dauer/Seek). */
  function playerCanNative(info, audioIdx) {
    if (!info) return false;
    // Rohdatei nur bei gängigen Browser-Containern — MKV/TS gehen über Live-Remux.
    const fmt = String(info.container || "").toLowerCase();
    const isMp4 = /\b(mp4|mov|m4v|isom|iso5|iso6)\b/.test(fmt);
    const isWebm = /\bwebm\b/.test(fmt) && !/\bmatroska\b/.test(fmt);
    if (!isMp4 && !isWebm) return false;
    const v = document.createElement("video");
    const vc = (info.codec || "").toLowerCase();
    let vMime = "";
    if (isMp4 && (/^(h264|avc)/.test(vc) || vc === "avc1")) vMime = 'video/mp4; codecs="avc1.640028"';
    else if (isMp4 && /^(h265|hevc|hev1|hvc1)/.test(vc)) vMime = 'video/mp4; codecs="hvc1.1.6.L93.B0"';
    else if (/^(av1|av01)/.test(vc)) {
      vMime = isWebm ? 'video/webm; codecs="av01.0.05M.08"' : 'video/mp4; codecs="av01.0.05M.08"';
    } else if (isWebm && /^vp9/.test(vc)) vMime = 'video/webm; codecs="vp9"';
    else return false;
    if (!v.canPlayType(vMime)) return false;
    if (audioIdx == null || audioIdx < 0) return true;
    const a = ((info.audio || [])[audioIdx]) || null;
    if (!a) return true;
    const ac = (a.codec || "").toLowerCase();
    // Nur klar browser-gängige Toncodecs → sonst Live-Remux (AAC).
    if (/^(aac|mp3|mp4a)/.test(ac)) return true;
    if (ac === "opus" && v.canPlayType('audio/webm; codecs="opus"')) return true;
    return false;
  }

  function _playerNativeUrl(root, rel) {
    return `/api/media?root=${encodeURIComponent(root || "media")}`
      + `&path=${encodeURIComponent(rel)}`;
  }

  function _playerStreamUrl(root, rel, audioIdx, startSec, audioCodec) {
    let u = `/api/media/stream?root=${encodeURIComponent(root || "media")}`
      + `&path=${encodeURIComponent(rel)}&audio=${audioIdx}`;
    if (startSec && startSec > 0) u += `&start=${encodeURIComponent(String(startSec))}`;
    if (audioCodec) u += `&acodec=${encodeURIComponent(audioCodec)}`;
    return u;
  }

  /** Bibliothek/Browser → Vollplayer; Fallback: Modal-Schnellplayer. */
  function playMedia(root, rel, name) {
    if (typeof window.openFullPlayer === "function") {
      window.openFullPlayer(rel, name);
      return;
    }
    openPlayer(root, rel, name);
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
    const knownDur = (info && info.duration) ? Number(info.duration) : 0;
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
      <div id="player-wrap"></div>
      <div class="player-stream-seek" id="player-seek-row" style="display:none">
        <input type="range" id="player-seek" min="0" max="1000" value="0" />
        <span class="muted" id="player-time">0:00 / 0:00</span>
      </div>
      <p class="muted" style="margin-top:8px;font-size:12px" id="player-mode-hint"></p>
      ${imgNote}`);

    const ctx = { startOffset: 0, useStream: true };

    const vttUrl = () => {
      const s = parseInt(($("player-sub") || {}).value, 10);
      return (!isNaN(s) && s >= 0)
        ? `/api/media/vtt?root=${encodeURIComponent(root)}&path=${encodeURIComponent(rel)}&subtitle=${s}`
        : "";
    };

    const bindStreamSeek = (video) => {
      const row = $("player-seek-row");
      const seek = $("player-seek");
      const time = $("player-time");
      if (!knownDur || !row || !seek) {
        if (row) row.style.display = "none";
        return;
      }
      row.style.display = "";
      let dragging = false;
      const tick = () => {
        if (dragging) return;
        const t = ctx.startOffset + (video.currentTime || 0);
        seek.value = String(Math.round(Math.min(1, Math.max(0, t / knownDur)) * 1000));
        if (time) time.textContent = fmtClock(t) + " / " + fmtClock(knownDur);
      };
      video.addEventListener("timeupdate", tick);
      video.addEventListener("loadedmetadata", tick);
      seek.oninput = () => {
        dragging = true;
        const t = (parseInt(seek.value, 10) / 1000) * knownDur;
        if (time) time.textContent = fmtClock(t) + " / " + fmtClock(knownDur);
      };
      // Erst beim Loslassen neu streamen (sonst FFmpeg-Sturm beim Ziehen).
      const commit = () => {
        if (!dragging) return;
        dragging = false;
        const t = (parseInt(seek.value, 10) / 1000) * knownDur;
        loadAt(t, true);
      };
      seek.addEventListener("change", commit);
      seek.addEventListener("pointerup", commit);
      tick();
    };

    const loadAt = (startSec, autoplay) => {
      const a = parseInt(($("player-audio") || {}).value, 10);
      const aIdx = isNaN(a) ? (audio.length ? 0 : -1) : a;
      const wrap = $("player-wrap");
      const hint = $("player-mode-hint");
      const seekRow = $("player-seek-row");
      if (!wrap) return;

      const native = playerCanNative(info, aIdx);
      ctx.useStream = !native;
      ctx.startOffset = native ? 0 : Math.max(0, startSec || 0);

      if (native) {
        if (seekRow) seekRow.style.display = "none";
        wrap.innerHTML = videoHtml(_playerNativeUrl(root, rel), vttUrl());
        if (hint) {
          hint.textContent = tt("Native Wiedergabe (volle Dauer & Suche). Tonspur wird vom Browser direkt gelesen.");
        }
        const va = wrap.querySelector("video");
        if (va && startSec > 0) {
          va.addEventListener("loadedmetadata", () => {
            try { va.currentTime = startSec; } catch (e) { /* ignore */ }
            if (autoplay) va.play().catch(() => {});
          }, { once: true });
        } else if (va && autoplay) {
          va.play().catch(() => {});
        }
        return;
      }

      const acodec = (aIdx >= 0 && audio[aIdx]) ? (audio[aIdx].codec || "") : "";
      const vUrl = _playerStreamUrl(root, rel, aIdx, ctx.startOffset, acodec);
      wrap.innerHTML = videoHtml(vUrl, vttUrl());
      if (hint) {
        hint.textContent = tt("Live-Remux (Ton → AAC): Dauer aus Analyse. Zum Springen den Schieberegler darunter nutzen (Neustart ab Position).");
      }
      const va = wrap.querySelector("video");
      if (va) {
        bindStreamSeek(va);
        if (autoplay) {
          va.addEventListener("loadeddata", () => va.play().catch(() => {}), { once: true });
        }
      }
    };

    const reload = () => loadAt(0, false);
    const btn = $("player-reload");
    if (btn) btn.addEventListener("click", reload);
    ["player-audio", "player-sub"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", reload);
    });
    loadAt(0, false);
  }

  // Direkt in den A/B-Vergleich springen und beide Videos laden.
  // opts.job: Warteschlangen-/Historien-ID → Liste der schwächsten VMAF-Stellen.
  function openAbCompare(rootA, pathA, rootB, pathB, opts) {
    closeModal();
    navTo("abcompare");
    const set = (id, val) => { const el = $(id); if (el && val != null) el.value = val; };
    set("ab-root-a", rootA); set("ab-path-a", pathA);
    set("ab-root-b", rootB); set("ab-path-b", pathB);
    state.abJob = (opts && opts.job) || "";
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
           🎞 ${tt("Vorher/Nachher im Vergleichsplayer")}</button>` : "";
    const requeueBtn = `<button class="btn btn-ghost btn-sm" id="modal-requeue">${tt("Erneut")}</button>` +
      `<button class="btn btn-ghost btn-sm" id="modal-requeue-edit">${tt("Erneut mit …")}</button>`;

    const html = `
      ${d.vmaf_warning ? `<div class="keep-source-note" style="margin:0 0 12px">${escapeHtml(d.vmaf_warning)}</div>` : ""}
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
      openAbCompare("media", ab.dataset.a, "media", ab.dataset.b, { job: id }));
    const rq = $("modal-requeue");
    if (rq) rq.addEventListener("click", () => requeueJob(id, !!d.from_history));
    const rqe = $("modal-requeue-edit");
    if (rqe) rqe.addEventListener("click", () => reopenJobWithSettings(d));
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
        const mode = histModeParts(j);
        const v = (mode.kind === "encode" && j.vmaf != null) ? Number(j.vmaf).toFixed(1) : "—";
        return `<li>${escapeHtml(when)} · ${escapeHtml(mode.badge)} ${escapeHtml(mode.detail)} · VMAF ${v} · ${escapeHtml(j.status || "")}</li>`;
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
    const useVmaf = !!s.vmaf_check && s.workflow !== "compare_only";
    if (useVmaf) {
      set("opt-rate-mode", "vmaf", "change");
      set("opt-vmaf-rate", s.rate_mode || "cq", "change");
      if (s.target_vmaf) set("opt-vmaf-target", s.target_vmaf);
      if ($("opt-vmaf-target-val") && $("opt-vmaf-target")) {
        $("opt-vmaf-target-val").textContent = $("opt-vmaf-target").value;
      }
      if (s.clip_seconds) set("opt-vmaf-clip", s.clip_seconds);
      if ($("opt-vmaf-clip-val") && $("opt-vmaf-clip")) {
        $("opt-vmaf-clip-val").textContent = $("opt-vmaf-clip").value;
      }
      if (s.samples) set("opt-vmaf-samples", String(s.samples));
      if (s.generate_screenshots !== undefined) set("opt-vmaf-shots", s.generate_screenshots);
      const grid = [...document.querySelectorAll("#opt-vmaf-grid .opt-vmaf-val")];
      const vals = Array.isArray(s.test_values) ? s.test_values : [];
      if (grid.length && vals.length) {
        grid.forEach((inp, i) => { inp.value = vals[i] != null ? vals[i] : ""; });
      }
      syncEncVmafRate(false);
    } else {
      set("opt-rate-mode", s.rate_mode, "change");
      if (s.rate_mode === "cq") set("opt-quality", s.quality);
      else set("opt-bitrate", s.quality);
    }
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
    set("opt-sharpen", s.sharpen || "off", "change");
    set("opt-grain", s.grain || "off", "change");
    set("opt-deinterlace", s.deinterlace || "auto", "change");
    if (s.aq_strength) {
      set("opt-aq-strength", s.aq_strength);
      const lab = $("aq-strength-val");
      if (lab) lab.textContent = String(s.aq_strength);
    }
    if (s.b_frames) set("opt-b-frames", s.b_frames);
    applyLegacyBFrames($("opt-b-frames"));
    if (s.nvenc_tune) set("opt-nvenc-tune", s.nvenc_tune);
    if (s.keyint_sec !== undefined && s.keyint_sec !== null) {
      set("opt-keyint", String(s.keyint_sec));
    }
    set("opt-film-grain", s.film_grain);
    set("opt-two-pass", s.two_pass);
    if (s.mobile_copy !== undefined) set("opt-mobile-copy", s.mobile_copy);
    if (s.mobile_height) set("opt-mobile-height", s.mobile_height);
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
    if (s.encoder_speed) set("opt-enc-speed", s.encoder_speed, "change");
    if (s.remux_only || s.video_mode === "edit") {
      // Remux-Profil → Remux & Bearbeiten (nicht Super-Tool), Auswahl mitnehmen.
      navTo("remux");
      applyRemuxProfile(s);
      const sel = state.selected;
      if (sel && !sel.isBatch && sel.path) {
        const already = state.remuxSel && state.remuxSel.path === sel.path;
        if (!already) remuxSelectFile({ rel: sel.path, name: sel.name || sel.path });
      }
    }
  }

  function applyRemuxProfile(s) {
    const set = (id, val, ev) => {
      const el = $(id);
      if (!el || val === undefined || val === null) return;
      if (el.type === "checkbox") el.checked = !!val; else el.value = val;
      el.dispatchEvent(new Event(ev || (el.tagName === "SELECT" ? "change" : "input")));
    };
    if (s.suffix !== undefined) set("remux-suffix", s.suffix);
    if (s.name_pattern !== undefined) set("remux-name-pattern", s.name_pattern);
    if (s.on_duplicate !== undefined) set("remux-on-duplicate", s.on_duplicate);
    const cont = (s.edit_spec && s.edit_spec.container) || s.container;
    if (cont && cont !== "auto") set("remux-container", cont, "change");
    if (s.post_processing !== undefined) set("remux-post", s.post_processing, "change");
    if (s.integrity_check !== undefined) set("remux-integrity", s.integrity_check);
    if (s.safe_replace !== undefined) set("remux-safe", s.safe_replace);
    if (s.out_mode !== undefined) set("remux-out-mode", s.out_mode, "change");
    if (s.out_subdir !== undefined) set("remux-out-subdir", s.out_subdir);
    const spec = s.edit_spec || {};
    if (spec.keep_chapters !== undefined) set("remux-keep-chapters", spec.keep_chapters);
    if (spec.keep_metadata !== undefined) set("remux-keep-metadata", spec.keep_metadata);
    if (spec.keep_attachments !== undefined) set("remux-keep-att", spec.keep_attachments);
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
      codecs.innerHTML = (st.by_codec || []).map((c) => {
        const label = ({
          remux: "Remux", "audio-opt": "Audio-Opt", concat: "Merge", split: "Split",
        })[(c.codec || "").toLowerCase()] || (c.codec || "?").toUpperCase();
        return `<span class="codec-chip">${escapeHtml(label)}: ${c.count}× · ${formatBytes(c.saved_bytes)}</span>`;
      }).join("");
    }
    const body = $("stats-body");
    if (body) {
      body.innerHTML = recent.length ? recent.map((j) => {
        const when = j.finished ? new Date(j.finished * 1000).toLocaleString() : "—";
        const id = escapeHtml(j.id || "");
        const mode = histModeParts(j);
        return `
        <tr>
          <td><a href="#" class="stats-title" data-id="${id}" title="Details & Wiedergabe öffnen">${escapeHtml(j.title || "")}</a></td>
          <td><span class="codec-badge">${escapeHtml(mode.badge)}</span></td>
          <td>${escapeHtml(mode.detail)}</td>
          <td>${mode.kind === "encode" && j.vmaf != null ? Number(j.vmaf).toFixed(1) : "—"}</td>
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

  function libBindLiveFilters() {
    const refresh = () => libRefreshView();
    ["lib-name", "lib-exclude", "lib-min-size", "lib-min-br", "lib-min-h",
      "lib-codec-match", "lib-target-codec", "lib-skip-optimized", "lib-skip-processed"]
      .forEach((id) => {
        const el = $(id);
        if (!el) return;
        el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", refresh);
      });
    const fmts = $("lib-formats");
    if (fmts) fmts.addEventListener("change", refresh);
  }

  function initLibrary() {
    const scanBtn = $("btn-lib-scan");
    if (!scanBtn) return;
    state.libScanAll = state.libScanAll || [];
    state.libScanRoot = state.libScanRoot || "";
    scanBtn.addEventListener("click", startLibraryScan);
    $("btn-lib-add").addEventListener("click", () => addLibrarySelection(false));
    const auto = $("btn-lib-add-auto");
    if (auto) auto.addEventListener("click", () => addLibrarySelection(true));
    const csv = $("btn-lib-csv");
    if (csv) csv.addEventListener("click", exportLibCsv);
    const all = $("lib-check-all");
    if (all) all.addEventListener("change", () => {
      document.querySelectorAll(".lib-check").forEach((c) => { c.checked = all.checked; });
    });
    const rs = $("lib-result-search");
    if (rs) rs.addEventListener("input", () => { state.libPage = 1; renderLibrary(); });
    const grp = $("lib-group");
    if (grp) grp.addEventListener("change", () => { state.libPage = 1; renderLibrary(); });
    initLibLibraries();
    document.querySelectorAll(".lib-table .sortable").forEach((th) => {
      th.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const key = th.dataset.sort;
        const cur = state.libSort || { key: "est_saved_bytes", dir: "desc" };
        const textKey = key === "name" || key === "codec" || key === "nfo_title" || key === "nfo_year";
        state.libSort = (cur.key === key)
          ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
          : { key, dir: textKey ? "asc" : "desc" };
        state.libPage = 1;
        renderLibrary();
      });
    });
    document.querySelectorAll(".lib-pager").forEach((pager) => {
      pager.addEventListener("click", (e) => {
        const b = e.target.closest("[data-lib-page]");
        if (!b) return;
        const p = parseInt(b.dataset.libPage, 10);
        if (!p || p === state.libPage) return;
        state.libPage = p;
        renderLibrary();
        const wrap = document.querySelector(".lib-table");
        if (wrap) wrap.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    });
    const body = $("lib-body");
    if (body) body.addEventListener("click", onLibAction);

    state.libCodecMulti = makeMultiSelect($("lib-codec-multi"), [
      { value: "h264", label: "H.264" },
      { value: "hevc", label: "HEVC/H.265" },
      { value: "av1", label: "AV1" },
      { value: "vp9", label: "VP9" },
      { value: "mpeg2video", label: "MPEG-2" },
      { value: "mpeg4", label: "MPEG-4" },
      { value: "vc1", label: "VC-1" },
    ], { placeholder: "Alle Codecs", onChange: () => libRefreshView() });
    state.libDynMulti = makeMultiSelect($("lib-dynamic-multi"), [
      { value: "sdr", label: "SDR" },
      { value: "hdr", label: "HDR (ohne DV)" },
      { value: "dv", label: "Dolby Vision (alle)" },
      { value: "dv5", label: "DV Profil 5" },
      { value: "dv7", label: "DV Profil 7" },
      { value: "dv8", label: "DV Profil 8" },
    ], { placeholder: "Alle", onChange: () => libRefreshView() });

    const cancel = $("btn-lib-cancel");
    if (cancel) cancel.addEventListener("click", cancelLibraryScan);
    const clear = $("btn-lib-clear");
    if (clear) clear.addEventListener("click", clearLibrary);

    libBuildFormats();
    libBindLiveFilters();
    loadLastLibrary();
  }

  function libBuildFormats() {
    const cont = $("lib-formats");
    if (!cont) return;
    const exts = (window.APP_CONFIG && window.APP_CONFIG.videoExtensions) || [];
    cont.innerHTML = exts.map((e) =>
      `<label data-ext="${escapeHtml(e)}">`
      + `<input type="checkbox" class="lib-fmt" value="${escapeHtml(e)}" />`
      + `<span class="fmt-name">${escapeHtml(e)}</span>`
      + `<span class="fmt-count" aria-hidden="true">–</span></label>`
    ).join("") || '<span class="empty">Keine Formate.</span>';
    libUpdateFormatCounts();
  }

  /** Anzahlen je Container aus dem aktuellen Scan in die Format-Kästen schreiben. */
  function libUpdateFormatCounts() {
    const cont = $("lib-formats");
    if (!cont) return;
    const counts = {};
    (state.libScanAll || []).forEach((m) => {
      let e = (m.ext || "").toLowerCase().replace(/^\./, "");
      if (!e && m.name) {
        const i = String(m.name).lastIndexOf(".");
        if (i >= 0) e = String(m.name).slice(i + 1).toLowerCase();
      }
      if (!e) return;
      counts[e] = (counts[e] || 0) + 1;
    });
    const hasScan = (state.libScanAll || []).length > 0;
    cont.querySelectorAll("label[data-ext]").forEach((lab) => {
      const ext = lab.dataset.ext || "";
      const n = counts[ext] || 0;
      const badge = lab.querySelector(".fmt-count");
      if (badge) badge.textContent = hasScan ? String(n) : "–";
      lab.classList.toggle("fmt-zero", hasScan && n === 0);
    });
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
    return {
      root: libSelectedRoot(),
      extensions: [...document.querySelectorAll(".lib-fmt:checked")].map((c) => c.value),
      name_contains: ($("lib-name") ? $("lib-name").value : "").trim(),
      name_exclude: ($("lib-exclude") ? $("lib-exclude").value : "")
        .split(",").map((s) => s.trim()).filter(Boolean),
      min_size_mb: parseFloat(($("lib-min-size") || {}).value) || 0,
      min_bitrate_mbps: parseFloat(($("lib-min-br") || {}).value) || 0,
      min_height: parseInt(($("lib-min-h") || {}).value, 10) || 0,
      codecs_include: codecMatch === "exclude" ? [] : codecs,
      codecs_exclude: codecMatch === "exclude" ? codecs : [],
      target_codec: $("lib-target-codec") ? $("lib-target-codec").value : "av1",
      dynamic_filters: dyn,
      skip_optimized: $("lib-skip-optimized") ? $("lib-skip-optimized").checked : false,
      skip_processed: $("lib-skip-processed") ? $("lib-skip-processed").checked : false,
    };
  }

  function libTargetBitrateKbps(height, isHdr, targetCodec) {
    let base = height <= 720 ? 2000 : height <= 1080 ? 4000 : height <= 1440 ? 7000 : 12000;
    if (isHdr) base = Math.floor(base * 1.5);
    if (targetCodec === "hevc") base = Math.floor(base * 1.25);
    return base;
  }

  function libProjectSavings(m, targetCodec) {
    const codec = (m.codec || "").toLowerCase();
    const srcBr = m.video_bitrate || 0;
    const targetBr = libTargetBitrateKbps(m.height || 0, !!m.is_hdr, targetCodec) * 1000;
    const efficient = ["av1", "libsvtav1", "av01"].includes(codec);
    const already = efficient || (srcBr > 0 && srcBr <= targetBr * 1.15);
    const dur = m.duration || 0;
    if (already || dur <= 0 || srcBr <= 0) {
      return { already_optimized: !!already || efficient, est_saved_bytes: 0 };
    }
    const srcVideo = Math.floor(srcBr / 8 * dur);
    const newVideo = Math.floor(targetBr / 8 * dur);
    const rest = Math.max(0, (m.size_bytes || 0) - srcVideo);
    const estNew = newVideo + rest;
    return { already_optimized: false, est_saved_bytes: Math.max(0, (m.size_bytes || 0) - estNew) };
  }

  function libSuggestEncode(m, targetCodec) {
    const codec = targetCodec === "hevc" ? "hevc" : "av1";
    let hdr_mode = "", dv_mode = "", label;
    if (m.dolby_vision) {
      const prof = m.dv_profile || 0;
      dv_mode = prof === 5 ? "tonemap" : "preserve";
    } else if (m.is_hdr) {
      hdr_mode = "preserve";
    } else {
      hdr_mode = "tonemap";
    }
    if (dv_mode === "preserve") label = `${codec.toUpperCase()} · DV übernehmen`;
    else if (dv_mode === "tonemap") label = `${codec.toUpperCase()} · DV → SDR (Tonemap)`;
    else if (hdr_mode === "preserve") label = `${codec.toUpperCase()} · HDR behalten`;
    else label = `${codec.toUpperCase()} · SDR`;
    return { codec, hdr_mode, dv_mode, label };
  }

  function libDynMatch(m, filters) {
    const active = (filters || []).filter(Boolean);
    if (!active.length) return true;
    return active.some((d) => {
      if (d === "sdr") return !m.is_hdr;
      if (d === "hdr") return !!m.is_hdr && !m.dolby_vision;
      if (d === "dv") return !!m.dolby_vision;
      if (d.startsWith("dv")) {
        const want = parseInt(d.slice(2), 10);
        return !!m.dolby_vision && (!want || (m.dv_profile || 0) === want);
      }
      return true;
    });
  }

  function libEnrichRow(m, targetCodec) {
    const proj = libProjectSavings(m, targetCodec);
    return {
      ...m,
      already_optimized: proj.already_optimized,
      est_saved_bytes: proj.est_saved_bytes,
      est_saved_human: formatBytes(proj.est_saved_bytes),
      suggest: libSuggestEncode(m, targetCodec),
    };
  }

  function libApplyLiveFilters(all) {
    const f = libFilters();
    const name = (f.name_contains || "").toLowerCase();
    const excl = (f.name_exclude || []).map((t) => t.toLowerCase());
    const minSize = (f.min_size_mb || 0) * 1024 * 1024;
    const minBr = (f.min_bitrate_mbps || 0) * 1e6;
    const minH = f.min_height || 0;
    const inc = (f.codecs_include || []).map((c) => c.toLowerCase());
    const exc = (f.codecs_exclude || []).map((c) => c.toLowerCase());
    const exts = (f.extensions || []).map((e) => String(e).toLowerCase().replace(/^\./, ""));
    const target = f.target_codec || "av1";
    return (all || []).map((m) => libEnrichRow(m, target)).filter((m) => {
      if (name && !(m.name || "").toLowerCase().includes(name)) return false;
      if (excl.length) {
        const pathLow = ((m.path || m.name || "")).toLowerCase();
        if (excl.some((t) => pathLow.includes(t))) return false;
      }
      if (minSize && (m.size_bytes || 0) < minSize) return false;
      if (minH && (m.height || 0) < minH) return false;
      if (minBr && (m.video_bitrate || 0) < minBr) return false;
      const codec = (m.codec || "").toLowerCase();
      if (inc.length && !inc.includes(codec)) return false;
      if (exc.length && exc.includes(codec)) return false;
      if (exts.length) {
        const ext = (m.ext || (m.name || "").split(".").pop() || "").toLowerCase();
        if (!exts.includes(ext)) return false;
      }
      if (!libDynMatch(m, f.dynamic_filters)) return false;
      if (f.skip_optimized && m.already_optimized) return false;
      if (f.skip_processed && m.processed) return false;
      return true;
    });
  }

  /** HDR-Modus-Label für Statistik (ohne Dolby Vision). */
  function libHdrModeKey(m) {
    if (!m || m.dolby_vision || !m.is_hdr) return "";
    const t = String(m.hdr_type || "").toLowerCase();
    if (t.includes("hdr10+") || t.includes("hdr10plus")) return "HDR10+";
    if (t.includes("hlg") || t.includes("arib")) return "HLG";
    if (t.includes("hdr10") || t.includes("(pq)") || t.includes("pq")) return "HDR10";
    if (t && t !== "sdr") {
      // z. B. älterer Cache „HDR10 (PQ)“ / unbekannte Variante
      const clean = String(m.hdr_type || "HDR").replace(/\s*\+.*$/, "").trim();
      return clean || "HDR";
    }
    return "HDR";
  }

  function libComputeStats(matched) {
    const byCodec = {};
    const byHdrMode = {};
    const byDvProfile = {};
    let hdr = 0, dv = 0, sdr = 0;
    (matched || []).forEach((m) => {
      const c = (m.codec || "?").toLowerCase();
      byCodec[c] = (byCodec[c] || 0) + 1;
      if (m.dolby_vision) {
        dv += 1;
        const p = Number(m.dv_profile) || 0;
        const key = p > 0 ? String(p) : "?";
        byDvProfile[key] = (byDvProfile[key] || 0) + 1;
      } else if (m.is_hdr) {
        hdr += 1;
        const mode = libHdrModeKey(m);
        if (mode) byHdrMode[mode] = (byHdrMode[mode] || 0) + 1;
      } else {
        sdr += 1;
      }
    });
    const codec_distribution = Object.keys(byCodec)
      .map((k) => ({ codec: k, count: byCodec[k] }))
      .sort((a, b) => b.count - a.count);
    const hdr_modes = Object.keys(byHdrMode)
      .map((k) => ({ mode: k, count: byHdrMode[k] }))
      .sort((a, b) => b.count - a.count || a.mode.localeCompare(b.mode));
    const dv_profiles = Object.keys(byDvProfile)
      .map((k) => ({ profile: k, count: byDvProfile[k] }))
      .sort((a, b) => {
        const na = parseInt(a.profile, 10), nb = parseInt(b.profile, 10);
        if (!isNaN(na) && !isNaN(nb)) return na - nb;
        return String(a.profile).localeCompare(String(b.profile));
      });
    const top_hogs = (matched || []).slice()
      .sort((a, b) => (b.est_saved_bytes || 0) - (a.est_saved_bytes || 0))
      .slice(0, 10)
      .map((h) => ({
        name: h.name, path: h.path, size_human: h.size_human,
        est_saved_human: h.est_saved_human, est_saved_bytes: h.est_saved_bytes || 0,
      }));
    return {
      codec_distribution, hdr_count: hdr, dv_count: dv, sdr_count: sdr,
      hdr_modes, dv_profiles, top_hogs,
    };
  }

  function libRefreshView() {
    const all = state.libScanAll || [];
    state.libRows = libApplyLiveFilters(all);
    state.libPage = 1;
    state.libStats = libComputeStats(state.libRows);
    const size = state.libRows.reduce((a, m) => a + (m.size_bytes || 0), 0);
    const saved = state.libRows.reduce((a, m) => a + (m.est_saved_bytes || 0), 0);
    renderLibrary();
    renderLibProjection({
      matched: state.libRows,
      total_size_bytes: size,
      total_saved_bytes: saved,
      total_size_human: formatBytes(size),
      total_saved_human: formatBytes(saved),
    });
    renderLibDashboard(state.libStats, state.libRows.length);
    libUpdateFormatCounts();
    const has = state.libRows.length > 0;
    ["btn-lib-add", "btn-lib-add-auto", "btn-lib-csv"].forEach((id) => {
      const b = $(id); if (b) b.disabled = !has;
    });
    libUpdateScanBadge();
  }

  function libNormRoot(root) {
    return String(root || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  }

  function libUpdateScanBadge(running) {
    const badge = $("lib-scan-badge");
    if (!badge) return;
    const rootNow = libNormRoot(libSelectedRoot());
    const active = libNormRoot(state.libActiveScanRoot || "");
    if (running || (state.libActiveScanRoot && active === rootNow)) {
      badge.textContent = "Scan läuft …";
      return;
    }
    if (state.libActiveScanRoot && active !== rootNow) {
      badge.textContent = "Scan läuft (andere Bibliothek)";
      return;
    }
    const cached = state.libByRoot[rootNow];
    if (!cached) { badge.textContent = "Noch nicht gescannt"; return; }
    const all = (state.libScanAll || []).length;
    const n = (state.libRows || []).length;
    badge.textContent = n === all ? `${n} Dateien` : `${n} / ${all} gefiltert`;
  }

  function exportLibCsv() {
    const rows = state.libRows || [];
    if (!rows.length) return;
    const esc = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
    const lines = [[
      "Pfad", "Ordner", "Codec", "Aufloesung", "Bitrate", "HDR/DV",
      "Dauer", "Groesse", "Einsparung", "Vorschlag",
    ].join(";")];
    rows.forEach((m) => {
      const dyn = m.dolby_vision ? ("DV" + (m.dv_profile ? " P" + m.dv_profile : ""))
        : (m.is_hdr ? (m.hdr_type || "HDR") : "SDR");
      lines.push([
        m.path, m.folder, m.codec, m.resolution, m.video_bitrate_human, dyn,
        m.duration_human, m.size_human, m.est_saved_human,
        (m.suggest && m.suggest.label) || "",
      ].map(esc).join(";"));
    });
    const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "bibliothek.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function initLibLibraries() {
    const sel = $("lib-library");
    if (!sel) return;
    refreshLibraries().then(() => {
      const saved = localStorage.getItem("libLibraryId") || "";
      if (saved && (state.libraries || []).some((l) => l.id === saved)) sel.value = saved;
      syncLibLibraryButtons();
      // Cache-Anzeige übernimmt loadLastLibrary (nach init); hier nur Buttons.
    });
    sel.addEventListener("change", () => {
      localStorage.setItem("libLibraryId", sel.value || "");
      syncLibLibraryButtons();
      showLibraryForSelection();
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
    const bar = (label, count, total, cls, nested, tip) => {
      if (!count) return "";
      const pct = total ? Math.round((count / total) * 100) : 0;
      const t = tip ? ` data-tip="${escapeHtml(tip)}"` : "";
      return `<div class="lib-bar${nested ? " nested" : ""}"${t}><span class="lib-bar-lbl">${escapeHtml(label)}</span>`
        + `<span class="lib-bar-track"><span class="lib-bar-fill ${cls || ""}" style="width:${pct}%"></span></span>`
        + `<span class="lib-bar-val">${count}</span></div>`;
    };
    const codecs = (stats.codec_distribution || []);
    $("lib-dash-codecs").innerHTML = codecs.map((c) =>
      bar((c.codec || "?").toUpperCase(), c.count, totalMatched)).join("") || "<span class='muted'>—</span>";

    let dyn = "";
    dyn += bar("SDR", stats.sdr_count || 0, totalMatched, "muted", false,
      tt("Standard Dynamic Range – klassisches SDR ohne HDR-Metadaten."));
    dyn += bar("HDR", stats.hdr_count || 0, totalMatched, "warn", false,
      tt("HDR ohne Dolby Vision (HDR10, HDR10+ oder HLG)."));
    (stats.hdr_modes || []).forEach((h) => {
      dyn += bar(h.mode, h.count, totalMatched, "warn", true, libHdrModeTip(h.mode));
    });
    dyn += bar("Dolby Vision", stats.dv_count || 0, totalMatched, "accent", false,
      tt("Dolby Vision (RPU). HEVC-Mitnahme auch per GPU (8.1). AV1-DV nur CPU/SVT, nicht NVIDIA."));
    (stats.dv_profiles || []).forEach((p) => {
      const lab = p.profile === "?" ? "Profil ?" : `Profil ${p.profile}`;
      dyn += bar(lab, p.count, totalMatched, "accent", true, libDvProfileTip(p.profile));
    });
    $("lib-dash-dynamic").innerHTML = dyn || "<span class='muted'>—</span>";

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
    libUpdateScanBadge(true);
    libSetRunning(true);
    // Nur Root scannen – Filter greifen live auf dem Ergebnis.
    await fetch("/api/library/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: libSelectedRoot() }),
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
    if (libPoll) return;
    const root = libNormRoot(libSelectedRoot());
    try {
      await fetch(`/api/library/clear?root=${encodeURIComponent(root)}`, { method: "POST" });
    } catch (e) { /* ignorieren */ }
    delete state.libByRoot[root];
    applyLibState({ matched: [], root, generated_at: 0 }, { persist: false });
    const prog = $("lib-progress");
    if (prog) prog.textContent = tt("Noch nicht gescannt – „Scannen“ starten.");
    libUpdateScanBadge();
  }

  function applyLibState(st, { persist = true } = {}) {
    const root = libNormRoot(st.root);
    const matched = st.matched || [];
    state.libScanAll = matched;
    state.libScanRoot = root;
    state.libScanAt = st.generated_at || 0;
    // Fertige/laufende Scans pro Root merken (auch 0 Treffer nach Scan)
    if (persist && (matched.length || st.generated_at || st.done || st.running)) {
      state.libByRoot[root] = {
        matched,
        root,
        generated_at: st.generated_at || 0,
        total_size_bytes: st.total_size_bytes || 0,
        total_saved_bytes: st.total_saved_bytes || 0,
        done: !!st.done,
      };
    }
    libRefreshView();
  }

  /** Cache der aktuell gewählten Bibliothek anzeigen (oder leere Liste). */
  function showLibraryForSelection() {
    const root = libNormRoot(libSelectedRoot());
    const cached = state.libByRoot[root];
    const prog = $("lib-progress");
    if (cached) {
      applyLibState(cached, { persist: false });
      const n = (cached.matched || []).length;
      const when = cached.generated_at
        ? new Date(cached.generated_at * 1000).toLocaleString() : "";
      if (prog) {
        prog.textContent = when
          ? `Gespeichert: ${when} · ${n} Dateien`
          : `${n} Dateien`;
      }
    } else {
      applyLibState({ matched: [], root, generated_at: 0 }, { persist: false });
      if (prog) prog.textContent = tt("Noch nicht gescannt – „Scannen“ starten.");
    }
    // Läuft ein Scan für genau diese Bibliothek → Live-Stand nachziehen
    if (libNormRoot(state.libActiveScanRoot) === root && libPoll) {
      pollLibrary();
    } else {
      libSetRunning(!!(state.libActiveScanRoot && libNormRoot(state.libActiveScanRoot) === root));
      libUpdateScanBadge();
    }
  }

  async function pollLibrary() {
    try {
      const r = await fetch("/api/library/scan");
      const st = await r.json();
      const scanRoot = libNormRoot(st.root);
      state.libActiveScanRoot = st.running ? scanRoot : "";
      const viewing = libNormRoot(libSelectedRoot());
      const prog = $("lib-progress");
      if (viewing === scanRoot && prog) {
        prog.textContent = st.running
          ? `${st.scanned}/${st.total} geprüft · ${st.matched.length} Dateien im Scan`
          : `${st.matched.length} Dateien gescannt` +
            (st.generated_at ? ` · ${new Date(st.generated_at * 1000).toLocaleString()}` : "");
      }
      if (viewing === scanRoot) {
        applyLibState(st);
      } else if (!st.running && !st.error) {
        // Fertig, aber andere Bibliothek ausgewählt → nur Cache füllen
        state.libByRoot[scanRoot] = {
          matched: st.matched || [],
          root: scanRoot,
          generated_at: st.generated_at || 0,
          total_size_bytes: st.total_size_bytes || 0,
          total_saved_bytes: st.total_saved_bytes || 0,
          done: true,
        };
      }
      if (!st.running) {
        clearInterval(libPoll); libPoll = null;
        state.libActiveScanRoot = "";
        libSetRunning(false);
        if (st.error && viewing === scanRoot) {
          const badge = $("lib-scan-badge");
          if (badge) badge.textContent = "Fehler";
        } else {
          libUpdateScanBadge();
        }
      } else {
        libSetRunning(viewing === scanRoot);
        libUpdateScanBadge(viewing === scanRoot);
      }
    } catch (e) { /* ignorieren */ }
  }

  async function loadLastLibrary() {
    try {
      const r = await fetch("/api/library/last");
      const st = await r.json();
      state.libByRoot = {};
      const by = (st && st.by_root) || {};
      Object.keys(by).forEach((k) => {
        const snap = by[k] || {};
        const root = libNormRoot(snap.root != null ? snap.root : k);
        state.libByRoot[root] = {
          matched: snap.matched || [],
          root,
          generated_at: snap.generated_at || 0,
          total_size_bytes: snap.total_size_bytes || 0,
          total_saved_bytes: snap.total_saved_bytes || 0,
        };
      });
      // v1-Kompatibilität: einzelner matched-Block ohne by_root
      if (!Object.keys(state.libByRoot).length && st && (st.matched || []).length) {
        const root = libNormRoot(st.root);
        state.libByRoot[root] = {
          matched: st.matched,
          root,
          generated_at: st.generated_at || 0,
          total_size_bytes: st.total_size_bytes || 0,
          total_saved_bytes: st.total_saved_bytes || 0,
        };
      }
      if (st && st.running && st.active_root != null) {
        state.libActiveScanRoot = libNormRoot(st.active_root);
        if (!libPoll) {
          libPoll = setInterval(pollLibrary, 1200);
          pollLibrary();
        }
      }
      showLibraryForSelection();
    } catch (e) {
      showLibraryForSelection();
    }
  }

  function libDynamicLabel(m) {
    if (m.dolby_vision) return "DV" + (m.dv_profile ? " P" + m.dv_profile : "");
    if (m.is_hdr) return libHdrModeKey(m) || (m.hdr_type || "HDR");
    return "SDR";
  }

  function hdrChipTip(m) {
    return libDynamicTip(m);
  }

  function libDynamicTip(m) {
    if (!m) return "";
    const sug = (m.suggest && m.suggest.label) ? m.suggest.label : "";
    const auto = sug ? ` ${tt("Auto-Vorschlag")}: ${sug}.` : "";
    if (m.dolby_vision) {
      const p = Number(m.dv_profile) || 0;
      if (p === 5) {
        return tt("Dolby Vision Profil 5: IPTPQc2 ohne HDR10-Fallback – nur mit DV-Player korrekt. Auto mappt nach SDR (sicher).") + auto;
      }
      if (p === 7) {
        return tt("Dolby Vision Profil 7 (Blu-ray, zwei Layer). Auto: HEVC → 8.1 (auch GPU). AV1 → 10.1 nur mit CPU/SVT, nicht mit NVIDIA.") + auto;
      }
      if (p === 8 || p === 10) {
        return tt("Dolby Vision Single-Layer mit HDR10-Fallback. Auto übernimmt die RPU. AV1-DV nur per CPU/SVT – NVIDIA fällt auf HDR10 zurück.") + auto;
      }
      return tt("Dolby Vision erkannt. Auto wählt Übernehmen oder Tone-Mapping je nach Profil. AV1-DV nur CPU/SVT, nicht NVIDIA.") + auto;
    }
    if (m.is_hdr) {
      const k = libHdrModeKey(m);
      if (k === "HDR10+") {
        return tt("HDR10+ (dynamische Metadaten). Der Plus-Layer geht beim Encode meist verloren; Auto behält HDR10 (10-bit).") + auto;
      }
      if (k === "HLG") {
        return tt("HLG-HDR (Broadcast). Auto behält HDR, kein Tone-Mapping nach SDR.") + auto;
      }
      return tt("HDR10/PQ (statische Metadaten). Auto behält 10-bit HDR, sofern der Encoder das kann.") + auto;
    }
    return tt("SDR – Standard Dynamic Range, typisch 8-bit. Kein HDR. Auto encodiert als SDR.") + auto;
  }

  function libSuggestTip(m) {
    const s = m && m.suggest;
    if (!s || !s.label) return tt("Kein Auto-Vorschlag (fehlende Analyse).");
    if (s.dv_mode === "tonemap") {
      return tt("Vorschlag für „Auswahl mit Auto-Einstellungen“: Ziel-Codec laut Projektion, Dolby Vision Profil 5 → SDR (Tone-Mapping), weil es keinen HDR10-Fallback gibt.");
    }
    if (s.dv_mode === "preserve") {
      return tt("Vorschlag für „Auswahl mit Auto-Einstellungen“: Ziel-Codec laut Projektion, Dolby-Vision-RPU übernehmen. HEVC: auch GPU (8.1). AV1: nur CPU/SVT (10.1), NVIDIA kann DV nicht mitnehmen.");
    }
    if (s.hdr_mode === "preserve") {
      return tt("Vorschlag für „Auswahl mit Auto-Einstellungen“: Ziel-Codec laut Projektion, HDR10/HLG als 10-bit behalten.");
    }
    return tt("Vorschlag für „Auswahl mit Auto-Einstellungen“: Ziel-Codec laut Projektion, Quelle ist SDR – keine HDR-Behandlung nötig.");
  }

  function libSavingsTip(m) {
    if (m.already_optimized) {
      return tt("Schon effizient: bereits AV1 oder Videobitrate nahe am Ziel für diese Auflösung/HDR. Eine Neuencodierung spart kaum Platz.");
    }
    const codec = (($("lib-target-codec") || {}).value || "av1").toUpperCase();
    return tt("Grobe Schätzung aus Dauer, aktueller Videobitrate und Ziel-Codec")
      + ` (${codec}). `
      + tt("Kein echter Probe-Encode – CQ, Film und Ton ändern die reale Größe.");
  }

  function libHdrModeTip(mode) {
    const k = String(mode || "");
    if (k === "HDR10+") return tt("HDR10+ – dynamische Metadaten, Plus-Layer überlebt einen Re-Encode selten.");
    if (k === "HLG") return tt("HLG – Broadcast-HDR, kompatibel mit SDR-Displays, Auto behält HDR.");
    if (k === "HDR10") return tt("HDR10 – statische MaxCLL/MaxFALL-Metadaten, Auto behält 10-bit.");
    return tt("HDR-Variante ohne Dolby Vision.");
  }

  function libDvProfileTip(profile) {
    const p = String(profile || "");
    if (p === "5") return tt("Profil 5: kein HDR10-Fallback. Ohne DV-fähigen Player falsch – Auto: Tone-Mapping.");
    if (p === "7") return tt("Profil 7: Blu-ray dual-layer. Auto: HEVC 8.1 (GPU möglich), AV1 10.1 nur CPU/SVT – nicht NVIDIA.");
    if (p === "8") return tt("Profil 8: Single-Layer + HDR10-Fallback. Auto übernimmt die RPU. AV1 nur CPU/SVT, nicht NVIDIA.");
    if (p === "10") return tt("Profil 10: DV in AV1. Übernehmen nur mit CPU/SVT – NVIDIA/NVENC kann die RPU nicht einbetten.");
    return tt("Dolby-Vision-Profil laut Datei-Metadaten.");
  }

  function libViewRows() {
    const q = ($("lib-result-search") ? $("lib-result-search").value : "").trim().toLowerCase();
    let rows = (state.libRows || []).slice();
    if (q) rows = rows.filter((m) =>
      (m.name || "").toLowerCase().includes(q)
      || (m.folder || "").toLowerCase().includes(q)
      || libNfoSearch(m).includes(q));
    const s = state.libSort || { key: "est_saved_bytes", dir: "desc" };
    const numeric = ["height", "video_bitrate", "duration", "size_bytes", "est_saved_bytes"];
    rows.sort((a, b) => {
      if (s.key === "nfo_year") {
        const ay = libNfoYear(a), by = libNfoYear(b);
        if (!ay !== !by) return ay ? -1 : 1;
        return s.dir === "asc" ? ay - by : by - ay;
      }
      let av, bv;
      if (s.key === "nfo_title" || s.key === "name") {
        av = libNfoTitleKey(a); bv = libNfoTitleKey(b);
      } else if (numeric.includes(s.key)) {
        av = a[s.key] || 0; bv = b[s.key] || 0;
        return s.dir === "asc" ? av - bv : bv - av;
      } else {
        av = String(a[s.key] || "").toLowerCase();
        bv = String(b[s.key] || "").toLowerCase();
      }
      return s.dir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    return rows;
  }

  function libRowHtml(m) {
    const dyn = libDynamicLabel(m);
    const dynCls = m.dolby_vision ? "accent" : (m.is_hdr ? "warn" : "");
    const opt = m.already_optimized
      ? `<span class="lib-opt-badge" data-tip="${escapeHtml(libSavingsTip(m))}">schon optimiert</span>`
      : `<span class="good" data-tip="${escapeHtml(libSavingsTip(m))}">${escapeHtml(m.est_saved_human || "—")}</span>`;
    const sug = (m.suggest && m.suggest.label) ? escapeHtml(m.suggest.label) : "—";
    const sugTip = escapeHtml(libSuggestTip(m));
    const open = state.libOpen && state.libOpen.has(m.path);
    const meta = libFileMetaLine(m);
    return `
      <tr class="lib-file-row${open ? " open" : ""}" data-path="${escapeHtml(m.path)}">
        <td><input type="checkbox" class="lib-check" value="${escapeHtml(m.path)}" ${m.already_optimized ? "" : "checked"} /></td>
        <td title="${escapeHtml(m.path)}">
          <div class="lib-name-cell">
            <button type="button" class="lib-toggle" data-path="${escapeHtml(m.path)}"
              title="${escapeHtml(tt("Ton, Untertitel und NFO anzeigen"))}"
              aria-expanded="${open ? "true" : "false"}">${open ? "▾" : "▸"}</button>
            <span class="lib-name-stack">
              <span class="lib-name">${escapeHtml(m.name)}</span>
              ${libNfoHeadline(m) ? `<span class="lib-nfo-line">${escapeHtml(libNfoHeadline(m))}</span>` : ""}
              ${meta ? `<span class="lib-meta-line">${meta}</span>` : ""}
            </span>
          </div>
        </td>
        <td>${escapeHtml((m.codec || "").toUpperCase())}</td>
        <td>${escapeHtml(m.resolution)}</td>
        <td><span class="dyn-badge ${dynCls}" data-tip="${escapeHtml(libDynamicTip(m))}">${escapeHtml(dyn)}</span></td>
        <td>${escapeHtml(m.video_bitrate_human)}</td>
        <td>${escapeHtml(m.duration_human || "—")}</td>
        <td>${escapeHtml(m.size_human)}</td>
        <td>${opt}</td>
        <td class="lib-suggest" data-tip="${sugTip}">${sug}</td>
        <td class="lib-row-actions">
          <button class="lib-act" data-act="play" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Abspielen">▶</button>
          <button class="lib-act" data-act="encode" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Ins Encoding übernehmen">→E</button>
          <button class="lib-act" data-act="vmaf" data-path="${escapeHtml(m.path)}" data-name="${escapeHtml(m.name)}" title="Ins VMAF-Tool übernehmen">→V</button>
        </td>
      </tr>
      ${open ? libDetailRowHtml(m) : ""}`;
  }

  function libRowHtmlSafe(m) {
    try {
      return libRowHtml(m);
    } catch (e) {
      return `<tr class="lib-file-row"><td colspan="11" class="bad">${escapeHtml((m && m.name) || "")}: ${escapeHtml(String(e))}</td></tr>`;
    }
  }

  function libNfoOf(m) {
    const extra = (state.libDetails && state.libDetails[m.path]) || {};
    return libPickNfo(extra.nfo, m && m.nfo) || {};
  }

  function libNfoTitleKey(m) {
    const n = libNfoOf(m);
    const title = String(n.title || n.showtitle || n.originaltitle || "").trim();
    return (title || (m && m.name) || "").toLowerCase();
  }

  function libNfoYear(m) {
    const raw = String((libNfoOf(m).year) || "").replace(/\D/g, "");
    const y = parseInt(raw.slice(0, 4), 10);
    return Number.isFinite(y) && y > 0 ? y : 0;
  }

  function libNfoHeadline(m) {
    const n = libNfoOf(m);
    const title = String(n.title || n.showtitle || n.originaltitle || "").trim();
    const year = libNfoYear(m);
    if (title && year) return `${title} (${year})`;
    if (title) return title;
    return year ? String(year) : "";
  }

  function libNfoSearch(m) {
    const n = libNfoOf(m);
    return [n.title, n.originaltitle, n.showtitle, n.year]
      .filter(Boolean).join(" ").toLowerCase();
  }

  function libFileMetaLine(m) {
    const d = libMergedDetails(m);
    const parts = [];
    if (Array.isArray(d.audio)) {
      parts.push(`${d.audio.length} ${tt("Ton")}`);
    }
    if (Array.isArray(d.subtitles) || (d.sidecars && d.sidecars.length)) {
      const n = (d.subtitles || []).length + (d.sidecars || []).length;
      parts.push(`${n} ${tt("UT")}`);
    }
    if (d.nfo) {
      parts.push(`<button type="button" class="lib-nfo-badge" data-path="${escapeHtml(m.path)}">NFO</button>`);
    }
    return parts.join(" · ");
  }

  function libMergedDetails(m) {
    const extra = (state.libDetails && state.libDetails[m.path]) || {};
    return {
      audio: extra.audio || m.audio,
      subtitles: extra.subtitles || m.subtitles,
      nfo: libPickNfo(extra.nfo, m.nfo),
      nfoLoading: !!extra.nfoLoading,
      sidecars: extra.sidecars || m.sidecars,
      container: extra.container || m.container,
      fps: extra.fps || m.fps,
      bit_depth: extra.bit_depth || m.bit_depth,
      profile: extra.profile || m.profile,
      error: extra.error,
      loading: extra.loading,
    };
  }

  function libTrackLabel(t, kind) {
    const lang = String(t.language || "und").toUpperCase();
    const codec = (t.codec || "?").toUpperCase();
    const bits = [lang, codec];
    if (kind === "audio") {
      if (t.layout) bits.push(t.layout);
      else if (t.channels) bits.push(t.channels + " ch");
      if (t.bitrate_human && t.bitrate_human !== "—") bits.push(t.bitrate_human);
    }
    const flags = [t.default ? tt("Standard") : "", t.forced ? tt("Forced") : ""].filter(Boolean);
    let s = bits.filter(Boolean).join(" · ");
    if (flags.length) s += ` (${flags.join(", ")})`;
    if (t.title) s += ` – ${t.title}`;
    return s;
  }

  function libDetailRowHtml(m) {
    const d = libMergedDetails(m);
    let body;
    const haveBody = Array.isArray(d.audio) || d.nfo || (d.sidecars && d.sidecars.length);
    if (d.error && !haveBody) {
      body = `<p class="bad">${escapeHtml(d.error)}</p>`;
    } else if (d.loading && !haveBody) {
      body = `<p class="muted">${tt("Lade …")}</p>`;
    } else {
      body = libDetailBodyHtml(d);
    }
    return `<tr class="lib-detail-row"><td colspan="11">${body}</td></tr>`;
  }

  function libDetailBodyHtml(d) {
    const audio = Array.isArray(d.audio) ? d.audio : [];
    const subs = Array.isArray(d.subtitles) ? d.subtitles : [];
    const sides = Array.isArray(d.sidecars) ? d.sidecars : [];
    const aList = audio.length
      ? `<ul class="lib-track-list">${audio.map((t) =>
          `<li>${escapeHtml(libTrackLabel(t, "audio"))}</li>`).join("")}</ul>`
      : `<p class="muted">${tt("Keine Tonspur")}</p>`;
    const sItems = subs.map((t) => `<li>${escapeHtml(libTrackLabel(t, "sub"))}</li>`);
    sides.forEach((s) => {
      sItems.push(`<li>${escapeHtml(s.name)} · ${tt("extern")}</li>`);
    });
    const sList = sItems.length
      ? `<ul class="lib-track-list">${sItems.join("")}</ul>`
      : `<p class="muted">${tt("Keine Untertitel")}</p>`;
    const tech = [];
    if (d.container) tech.push(String(d.container).split(",")[0]);
    if (d.profile) tech.push(d.profile);
    if (d.bit_depth) tech.push(d.bit_depth + " bit");
    if (d.fps) tech.push((Math.round(d.fps * 100) / 100) + " fps");
    const techLine = tech.length
      ? `<p class="lib-nfo-tech">${escapeHtml(tech.join(" · "))}</p>` : "";
    return `<div class="lib-detail">
      <div class="lib-detail-col">
        <strong>${tt("Tonspuren")} (${audio.length})</strong>
        ${aList}
      </div>
      <div class="lib-detail-col">
        <strong>${tt("Untertitel")} (${subs.length}${sides.length ? ` + ${sides.length}` : ""})</strong>
        ${sList}
      </div>
    </div>${techLine}`;
  }

  function libNfoIsStub(nfo) {
    if (!nfo || typeof nfo !== "object") return true;
    return !(nfo.title || nfo.plot || nfo.showtitle || nfo.year
      || nfo.originaltitle || nfo.excerpt);
  }

  function libPickNfo(a, b) {
    if (!libNfoIsStub(a)) return a;
    if (!libNfoIsStub(b)) return b;
    return a || b || null;
  }

  function libNfoList(v) {
    if (Array.isArray(v)) return v.filter(Boolean).map(String);
    if (v) return [String(v)];
    return [];
  }

  function libNfoHtml(nfo, tech, loading) {
    const techLine = tech && tech.length
      ? `<p class="lib-nfo-tech">${escapeHtml(tech.join(" · "))}</p>` : "";
    if (!nfo) {
      if (loading) return `${techLine}<p class="muted">${tt("Lade …")}</p>`;
      return `${techLine}<p class="muted">${tt("Keine .nfo im Ordner")}</p>`;
    }
    const files = libNfoList(nfo.files).join(", ") || (nfo.file || "");
    if (libNfoIsStub(nfo) && !nfo.excerpt) {
      return `${techLine}<p class="muted">${tt("NFO gefunden, Inhalt konnte nicht gelesen werden.")}`
        + (files ? ` (${escapeHtml(files)})` : "") + `</p>`;
    }
    const title = nfo.showtitle && nfo.title && nfo.showtitle !== nfo.title
      ? `${nfo.showtitle}: ${nfo.title}`
      : (nfo.title || nfo.showtitle || nfo.originaltitle || "");
    const ep = (nfo.season && nfo.episode)
      ? `S${String(nfo.season).padStart(2, "0")}E${String(nfo.episode).padStart(2, "0")}`
      : "";
    const head = [title, nfo.year ? `(${nfo.year})` : "", ep].filter(Boolean).join(" ");
    const bits = [];
    if (nfo.rating) bits.push(nfo.rating + (String(nfo.rating).includes("/") ? "" : "/10"));
    if (nfo.mpaa) bits.push(nfo.mpaa);
    if (nfo.runtime) bits.push(nfo.runtime + (String(nfo.runtime).match(/\d$/) ? " min" : ""));
    const genres = libNfoList(nfo.genres);
    if (genres.length) bits.push(genres.join(", "));
    if (nfo.studio) bits.push(nfo.studio);
    const plot = nfo.plot || nfo.excerpt || "";
    return `${techLine}
      ${head ? `<p class="lib-nfo-title">${escapeHtml(head)}</p>` : ""}
      ${nfo.tagline ? `<p class="lib-nfo-tag">${escapeHtml(nfo.tagline)}</p>` : ""}
      ${bits.length ? `<p class="lib-nfo-bits">${escapeHtml(bits.join(" · "))}</p>` : ""}
      ${plot ? `<p class="lib-nfo-plot">${escapeHtml(plot)}</p>` : ""}
      ${files ? `<p class="muted lib-nfo-file">${escapeHtml(files)}</p>` : ""}
      ${nfo.text ? `<pre class="lib-nfo-raw">${escapeHtml(nfo.text)}</pre>` : ""}`;
  }

  function libDataPath(el) {
    if (!el) return "";
    return (el.dataset && el.dataset.path) || el.getAttribute("data-path") || "";
  }

  async function libToggleDetails(path) {
    if (!path) return;
    if (!state.libOpen) state.libOpen = new Set();
    if (state.libOpen.has(path)) {
      state.libOpen.delete(path);
      renderLibrary();
      return;
    }
    state.libOpen.add(path);
    const row = (state.libScanAll || []).find((m) => m.path === path) || {};
    const cached = state.libDetails && state.libDetails[path];
    const haveTracks = Array.isArray(row.audio) || (cached && Array.isArray(cached.audio));
    const haveNfo = !libNfoIsStub(row.nfo) || (cached && !libNfoIsStub(cached.nfo));
    if (!haveTracks || !haveNfo) {
      if (!state.libDetails) state.libDetails = {};
      state.libDetails[path] = {
        ...(cached || {}),
        loading: !haveTracks,
        nfoLoading: !haveNfo,
      };
    }
    renderLibrary();
    if (haveTracks && haveNfo) return;
    await libEnsureDetails(path, { probe: !haveTracks });
    renderLibrary();
  }

  async function libEnsureDetails(path, opts) {
    if (!state.libDetails) state.libDetails = {};
    const probe = !!(opts && opts.probe);
    try {
      const r = await fetch(
        `/api/library/details?path=${encodeURIComponent(path)}&probe=${probe ? "1" : "0"}`);
      const d = await r.json();
      if (!r.ok || (d.error && !d.nfo && !d.audio)) {
        state.libDetails[path] = {
          ...(state.libDetails[path] || {}),
          error: d.error || tt("Analyse fehlgeschlagen"),
          loading: false,
          nfoLoading: false,
        };
        return;
      }
      const prev = state.libDetails[path] || {};
      state.libDetails[path] = { ...prev, ...d, loading: false, nfoLoading: false };
      const row = (state.libScanAll || []).find((m) => m.path === path);
      if (row) {
        if (Array.isArray(d.audio)) row.audio = d.audio;
        if (Array.isArray(d.subtitles)) row.subtitles = d.subtitles;
        if (d.nfo) row.nfo = d.nfo;
        if (d.sidecars) row.sidecars = d.sidecars;
        if (d.container) row.container = d.container;
        if (d.fps) row.fps = d.fps;
      }
    } catch (e) {
      state.libDetails[path] = {
        ...(state.libDetails[path] || {}),
        error: String(e), loading: false, nfoLoading: false,
      };
    }
  }

  function libRenderPager(totalRows) {
    const pagers = document.querySelectorAll(".lib-pager");
    if (!pagers.length) return;
    const size = state.libPageSize || 50;
    const pages = Math.max(1, Math.ceil(totalRows / size));
    let page = Math.max(1, Math.min(state.libPage || 1, pages));
    state.libPage = page;
    if (totalRows <= size) {
      pagers.forEach((pager) => { pager.style.display = "none"; pager.innerHTML = ""; });
      return;
    }
    const btn = (p, label, { active = false, disabled = false } = {}) =>
      `<button type="button" class="btn btn-ghost btn-sm lib-page-btn${active ? " active" : ""}"`
      + ` data-lib-page="${p}"${disabled || active ? " disabled" : ""}>${label}</button>`;
    const nums = new Set([1, pages, page - 2, page - 1, page, page + 1, page + 2]);
    const sorted = [...nums].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
    let html = btn(Math.max(1, page - 1), "‹", { disabled: page <= 1 });
    let prev = 0;
    sorted.forEach((n) => {
      if (prev && n > prev + 1) html += `<span class="lib-page-info">…</span>`;
      html += btn(n, String(n), { active: n === page });
      prev = n;
    });
    html += `<span class="lib-page-info">${page} / ${pages}</span>`;
    html += btn(Math.min(pages, page + 1), "›", { disabled: page >= pages });
    pagers.forEach((pager) => {
      pager.style.display = "";
      pager.innerHTML = html;
    });
  }

  function renderLibrary() {
    const body = $("lib-body");
    if (!body) return;
    const rows = libViewRows();
    const size = state.libPageSize || 50;
    const pages = Math.max(1, Math.ceil(rows.length / size));
    if ((state.libPage || 1) > pages) state.libPage = pages;
    const page = state.libPage || 1;
    const start = (page - 1) * size;
    const pageRows = rows.slice(start, start + size);

    const cnt = $("lib-result-count");
    const allN = (state.libScanAll || []).length;
    const filtN = (state.libRows || []).length;
    if (cnt) {
      if (!allN) cnt.textContent = "";
      else {
        const range = rows.length
          ? `${start + 1}–${Math.min(start + pageRows.length, rows.length)} von ${rows.length}`
          : "0";
        let extra = "";
        if (rows.length !== filtN) extra = ` · Suche in ${filtN}`;
        else if (filtN !== allN) extra = ` · Filter von ${allN}`;
        cnt.textContent = `${range}${extra}`;
      }
    }

    if (!allN) {
      body.innerHTML = '<tr class="empty-row"><td colspan="11">Noch kein Scan. Oben Bibliothek wählen und „Scan" starten.</td></tr>';
      libRenderPager(0);
      return;
    }
    if (!rows.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="11">Keine Treffer für die aktuellen Filter.</td></tr>';
      libRenderPager(0);
      return;
    }

    const grouped = $("lib-group") && $("lib-group").checked;
    if (!grouped) {
      body.innerHTML = pageRows.map(libRowHtmlSafe).join("");
    } else {
      const byFolder = {};
      pageRows.forEach((m) => {
        const k = m.folder || "(Wurzel)";
        (byFolder[k] = byFolder[k] || []).push(m);
      });
      body.innerHTML = Object.keys(byFolder).sort().map((folder) => {
        const items = byFolder[folder];
        const saved = items.reduce((a, m) => a + (m.est_saved_bytes || 0), 0);
        return `<tr class="lib-group-row"><td colspan="11">📁 ${escapeHtml(folder)} `
          + `<span class="muted">· ${items.length} Dateien · ca. ${escapeHtml(formatBytes(saved))} einsparbar</span></td></tr>`
          + items.map(libRowHtmlSafe).join("");
      }).join("");
    }
    libRenderPager(rows.length);
  }

  function libNfoTitle(nfo, fallback) {
    if (!nfo) return fallback || "NFO";
    const title = nfo.showtitle && nfo.title && nfo.showtitle !== nfo.title
      ? `${nfo.showtitle}: ${nfo.title}`
      : (nfo.title || nfo.showtitle || nfo.originaltitle || "");
    const ep = (nfo.season && nfo.episode)
      ? `S${String(nfo.season).padStart(2, "0")}E${String(nfo.episode).padStart(2, "0")}`
      : "";
    return [title, nfo.year ? `(${nfo.year})` : "", ep].filter(Boolean).join(" ")
      || fallback || "NFO";
  }

  async function libOpenNfo(path) {
    const row = (state.libScanAll || []).find((m) => m.path === path) || {};
    openModal(row.name || "NFO", `<p class="muted">${tt("Lade …")}</p>`, { nfo: true });
    await libEnsureDetails(path, { probe: false });
    const modal = $("app-modal");
    if (!modal || modal.style.display === "none") return;
    const nfo = libPickNfo((state.libDetails[path] || {}).nfo, row.nfo);
    $("app-modal-title").textContent = libNfoTitle(nfo, row.name || "NFO");
    $("app-modal-body").innerHTML = `<div class="lib-nfo-pop">${libNfoHtml(nfo, [], false)}</div>`;
  }

  function onLibAction(e) {
    const nfoBtn = e.target.closest(".lib-nfo-badge");
    if (nfoBtn) {
      e.preventDefault();
      e.stopPropagation();
      const path = libDataPath(nfoBtn) || libDataPath(nfoBtn.closest("tr.lib-file-row"));
      if (path) libOpenNfo(path);
      return;
    }
    const btn = e.target.closest(".lib-act");
    if (btn) {
      e.stopPropagation();
      const path = btn.dataset.path;
      const name = btn.dataset.name;
      const act = btn.dataset.act;
      if (act === "play") { playMedia("media", path, name); return; }
      const row = (state.libRows || []).find((m) => m.path === path);
      libTransfer(path, name, act === "vmaf" ? "vmaf" : "encode", row ? row.suggest : null);
      return;
    }
    if (e.target.closest("input, a, select, textarea")) return;
    const row = e.target.closest("tr.lib-file-row");
    if (!row || e.target.closest(".lib-row-actions")) return;
    e.preventDefault();
    e.stopPropagation();
    libToggleDetails(libDataPath(row));
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
    fillJobSpeedSelect("st-enc-speed", "st-platform", "st-codec");
    syncAqField("st");
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
        <td><span class="dyn-badge ${dynCls}" data-tip="${escapeHtml(libDynamicTip(m))}">${escapeHtml(dyn)}</span></td>
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
    if (act === "play") { playMedia("media", path, name); return; }
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
      encoder_speed: encoderSpeedValue("st-enc-speed"),
      aq_strength: $("st-aq-strength") ? parseInt($("st-aq-strength").value, 10) : 8,
      b_frames: ($("st-b-frames") && $("st-b-frames").value) || "auto",
      nvenc_tune: ($("st-nvenc-tune") && $("st-nvenc-tune").value) || "auto",
      keyint_sec: $("st-keyint") ? (parseInt($("st-keyint").value, 10) || 0) : 0,
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
      s.sample_mode = sampleModeValue("st-sample-mode");
      s.scene_min_pct = sceneMinPct();
      s.two_pass = !!($("st-two-pass") && $("st-two-pass").checked
        && (s.rate_mode === "abr" || s.rate_mode === "bitrate"));
      s.test_values = stTestValues();
      s.generate_screenshots = true;
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
      <tr class="st-dash-row${it.id === state.stVmafId ? " is-active" : ""}" data-id="${it.id}"
          title="${it.vmaf ? "Vergleichsbilder anzeigen" : escapeHtml(it.path)}">
        <td title="${escapeHtml(it.path)}">${escapeHtml(it.title)}${it.vmaf_warning ? `<div class="queue-warn">${escapeHtml(it.vmaf_warning)}</div>` : ""}</td>
        <td class="status-cell">${statusBadge(it.status)}</td>
        <td>${settingsLabel(it)}</td>
        <td>${it.duration_human || "—"}</td>
        <td class="good">${it.saved_human}</td>
      </tr>`).join("") :
      '<tr class="empty-row"><td colspan="5">Noch nichts.</td></tr>';
    body.querySelectorAll(".st-dash-row").forEach((tr) => {
      tr.addEventListener("click", () => {
        state.stVmafId = tr.dataset.id;
        body.querySelectorAll(".st-dash-row").forEach((r) =>
          r.classList.toggle("is-active", r.dataset.id === state.stVmafId));
        const it = items.find((i) => i.id === tr.dataset.id);
        const shots = $("st-screenshots");
        if (shots) shots._shotKey = "";
        renderScreenshots((it && it.vmaf) || { results: [] }, shots);
      });
    });
    const hasShots = items.some((i) => i.vmaf && (i.vmaf.results || []).some((r) =>
      (r.screenshots && r.screenshots.length) || r.screenshot_enc));
    const hint = $("st-shot-hint");
    if (hint) hint.style.display = hasShots ? "" : "none";
    const focus = items.find((i) => i.id === state.stVmafId && i.vmaf)
      || [...items].reverse().find((i) => i.vmaf && i.vmaf.results && i.vmaf.results.length);
    const shots = $("st-screenshots");
    const key = focus
      ? (focus.id + ":" + (focus.vmaf.results || []).length + ":"
        + ((focus.vmaf.results || []).some((r) => (r.screenshots || []).length) ? "s" : ""))
      : "";
    if (shots && shots._shotKey !== key) {
      shots._shotKey = key;
      if (focus && !state.stVmafId) state.stVmafId = focus.id;
      renderScreenshots(focus ? focus.vmaf : { results: [] }, shots);
    }
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
        showFiles: true, playFile: true, discPick: true, pickFile: remuxSelectFile,
        onNavigate: (data, p) => {
          state.currentRemuxPath = p;
          const main = ((data.bluray && data.bluray.titles) || []).find((t) => t.role === "main");
          if (main && !state.remuxSel) remuxSelectTitle(main);
        },
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

  function remuxSelectTitle(t) {
    return remuxSelectFile(discTitleFile(t));
  }

  async function remuxSelectFile(f) {
    // Titel aus ISO oder DVD-Ordner: Quelle ist das Abbild bzw. der Ordner,
    // der Titel wird über disc_clip analysiert.
    const iso = f.bluray && f.bluray.source;
    state.remuxSel = {
      path: iso || f.rel, name: f.name,
      bluray: f.bluray || null,
    };
    state.remuxExt = [];
    state.remuxAtt = [];
    state.remuxChapters = null;
    $("remux-chapters-wrap").style.display = "none";
    $("remux-chapters-info").textContent = "";
    const splitBtn = $("btn-split-start");
    if (splitBtn) splitBtn.disabled = !!iso;
    const clr = $("btn-remux-clear");
    if (clr) clr.disabled = false;
    $("remux-badge").textContent = `${f.name} · analysiere …`;
    document.querySelectorAll("#remux-browser .row-item.selected").forEach((r) => r.classList.remove("selected"));
    try {
      const probeUrl = iso
        ? `/api/probe?path=${encodeURIComponent(iso)}&disc_clip=${encodeURIComponent((f.bluray.clips || [])[0] || "")}`
        : `/api/probe?path=${encodeURIComponent(f.rel)}`;
      const info = await (await fetch(probeUrl)).json();
      if (info.error) { $("remux-badge").textContent = info.error; return; }
      state.remuxInfo = info;
      if (state.remuxSel.bluray && state.remuxSel.bluray.chapters
          && state.remuxSel.bluray.chapters.length) {
        state.remuxChapters = state.remuxSel.bluray.chapters.map((c) => ({ ...c }));
      }
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

  function remuxBindTrackRowEvents() {
    ["rx-a-keep", "rx-s-keep", "rx-a-tc"].forEach((c) =>
      document.querySelectorAll(`#remux-editor .${c}`).forEach((el) =>
        el.addEventListener("change", remuxUpdateConflicts)));
    document.querySelectorAll("#remux-audio-body .rx-up, #remux-audio-body .rx-down, #remux-sub-body .rx-up, #remux-sub-body .rx-down")
      .forEach((b) => b.addEventListener("click", (e) => {
        e.stopPropagation();
        remuxMoveRow(b.closest("tr"), b.classList.contains("rx-up") ? -1 : 1);
      }));
    document.querySelectorAll("#remux-audio-body .rx-ext-del, #remux-sub-body .rx-ext-del")
      .forEach((b) => b.addEventListener("click", () => {
        const i = parseInt(b.dataset.i, 10);
        if (Number.isNaN(i)) return;
        remuxSyncExternalInputs();
        state.remuxExt.splice(i, 1);
        remuxRenderEditor();
      }));
    document.querySelectorAll("#remux-audio-body .rx-a-tc[data-ext]").forEach((b) =>
      b.addEventListener("change", () => {
        const i = b.dataset.i;
        ["rx-a-codec", "rx-a-br"].forEach((c) => {
          const el = document.querySelector(
            `#remux-audio-body tr[data-ext-i="${i}"] .${c}`);
          if (el) el.disabled = !b.checked;
        });
      }));
  }

  function remuxAppendExternalRows() {
    const ab = $("remux-audio-body");
    const sb = $("remux-sub-body");
    if (ab) ab.querySelectorAll("tr.empty-row").forEach((r) => r.remove());
    if (sb) sb.querySelectorAll("tr.empty-row").forEach((r) => r.remove());
    state.remuxExt.forEach((e, i) => {
      const tr = document.createElement("tr");
      tr.dataset.extI = String(i);
      if (e.type === "subtitle") {
        tr.innerHTML = `
          <td><input type="checkbox" class="rx-s-keep" checked></td>
          <td title="${escapeHtml(e.path || "")}">
            <span class="muted">Extern</span> · ${escapeHtml(e.name || "?")}
            ${e.desc ? `<div class="muted" style="font-size:11px">${escapeHtml(e.desc)}</div>` : ""}
            <label class="muted" style="font-size:11px">Delay
              <input type="number" class="rx-e-delay" data-i="${i}" value="${e.delay || 0}" step="0.1" style="width:60px"> s
            </label>
          </td>
          <td><input type="checkbox" class="rx-s-default"${e.default ? " checked" : ""}></td>
          <td><input type="checkbox" class="rx-s-forced"${e.forced ? " checked" : ""}></td>
          <td><input type="text" class="rx-s-lang rx-e-lang" data-i="${i}" value="${escapeHtml(e.language || "")}" size="4"></td>
          <td><input type="text" class="rx-s-title rx-e-title" data-i="${i}" value="${escapeHtml(e.title || "")}"></td>
          <td class="row-actions">${remuxOrderBtns()}
            <button class="btn btn-ghost btn-sm bad-btn rx-ext-del" data-i="${i}" title="Entfernen">✕</button></td>`;
        if (sb) sb.appendChild(tr);
      } else {
        const dis = e.transcode ? "" : " disabled";
        const srcLine = [
          e.src_codec || "",
          e.channels ? `${e.channels}ch` : "",
          e.src_bitrate_human || "",
        ].filter(Boolean).join(" · ") || (e.desc || "");
        tr.innerHTML = `
          <td><input type="checkbox" class="rx-a-keep" checked></td>
          <td title="${escapeHtml(e.path || "")}">
            <span class="muted">Extern</span> · ${escapeHtml(e.name || "?")}
            ${srcLine ? `<div class="muted" style="font-size:11px">${escapeHtml(srcLine)}</div>` : ""}
            <label class="muted" style="font-size:11px">Delay
              <input type="number" class="rx-e-delay" data-i="${i}" value="${e.delay || 0}" step="0.1" style="width:60px"> s
            </label>
          </td>
          <td><input type="checkbox" class="rx-a-default"${e.default ? " checked" : ""}></td>
          <td><input type="checkbox" class="rx-a-forced"${e.forced ? " checked" : ""}></td>
          <td><input type="text" class="rx-a-lang rx-e-lang" data-i="${i}" value="${escapeHtml(e.language || "")}" size="4"></td>
          <td><input type="text" class="rx-a-title rx-e-title" data-i="${i}" value="${escapeHtml(e.title || "")}"></td>
          <td class="rx-tc-cell">
            <label class="check"><input type="checkbox" class="rx-a-tc" data-ext="1" data-i="${i}"${e.transcode ? " checked" : ""}><span>→</span></label>
            ${remuxAudioCodecSelect("rx-a-codec", e.codec || "eac3").replace("<select", `<select${dis}`)}
            <input type="number" class="rx-a-br" value="${e.bitrate || 640}" min="64" max="1536" step="64" style="width:70px"${dis}>
          </td>
          <td class="row-actions">${remuxOrderBtns()}
            <button class="btn btn-ghost btn-sm bad-btn rx-ext-del" data-i="${i}" title="Entfernen">✕</button></td>`;
        if (ab) ab.appendChild(tr);
      }
    });
    if (ab && !ab.children.length)
      ab.innerHTML = '<tr class="empty-row"><td colspan="8">Keine Tonspuren.</td></tr>';
    if (sb && !sb.children.length)
      sb.innerHTML = '<tr class="empty-row"><td colspan="7">Keine Untertitel.</td></tr>';
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

    remuxAppendExternalRows();
    remuxBindTrackRowEvents();
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
    // Externe Spuren liegen in den Ton-/UT-Tabellen oben (gemeinsame Reihenfolge).
    const eb = $("remux-ext-body");
    if (!eb) return;
    const n = state.remuxExt.length;
    eb.innerHTML = n
      ? `<tr class="empty-row"><td colspan="10">${n} ${tt("externe Spur(en) in den Tabellen oben – dort sortieren/entfernen.")}</td></tr>`
      : `<tr class="empty-row"><td colspan="10">${tt("Keine externen Spuren.")}</td></tr>`;
  }

  // Vor dem Neu-Rendern die editierten Werte aus dem DOM in den State übernehmen.
  function remuxSyncExternalInputs() {
    state.remuxExt.forEach((e, i) => {
      const tr = document.querySelector(
        `#remux-audio-body tr[data-ext-i="${i}"], #remux-sub-body tr[data-ext-i="${i}"]`);
      if (!tr) return;
      const delay = tr.querySelector(".rx-e-delay");
      if (delay) e.delay = parseFloat(delay.value) || 0;
      if (e.type === "subtitle") {
        const lang = tr.querySelector(".rx-s-lang");
        const title = tr.querySelector(".rx-s-title");
        const def = tr.querySelector(".rx-s-default");
        const forced = tr.querySelector(".rx-s-forced");
        if (lang) e.language = lang.value.trim();
        if (title) e.title = title.value.trim();
        if (def) e.default = def.checked;
        if (forced) e.forced = forced.checked;
      } else {
        const lang = tr.querySelector(".rx-a-lang");
        const title = tr.querySelector(".rx-a-title");
        const def = tr.querySelector(".rx-a-default");
        const forced = tr.querySelector(".rx-a-forced");
        const tc = tr.querySelector(".rx-a-tc");
        const codec = tr.querySelector(".rx-a-codec");
        const br = tr.querySelector(".rx-a-br");
        if (lang) e.language = lang.value.trim();
        if (title) e.title = title.value.trim();
        if (def) e.default = def.checked;
        if (forced) e.forced = forced.checked;
        if (tc) e.transcode = tc.checked;
        if (codec) e.codec = codec.value;
        if (br) e.bitrate = parseInt(br.value, 10) || 640;
      }
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

  function remuxConfirmDurationMatch(data, name) {
    const main = (state.remuxInfo && state.remuxInfo.duration) || 0;
    const ext = (data && data.duration) || 0;
    if (!main || !ext) return true;
    const diff = Math.abs(main - ext);
    const tol = Math.max(2.0, main * 0.02);
    if (diff <= tol) return true;
    const mainH = state.remuxInfo.duration_human
      || (typeof formatDuration === "function" ? formatDuration(main) : (Math.round(main) + "s"));
    const extH = data.duration_human
      || (typeof formatDuration === "function" ? formatDuration(ext) : (Math.round(ext) + "s"));
    return window.confirm(
      tt("Dauer weicht ab") + ":\n\n"
      + `„${name}“: ${extH}\n`
      + `${tt("Quelle")}: ${mainH}\n`
      + `${tt("Differenz")}: ${Math.round(diff)}s\n\n`
      + tt("Trotzdem hinzufügen? (Sync/Versatz ggf. mit Delay korrigieren)"));
  }

  // Aus einem probe-Ergebnis externe Spuren in state.remuxExt übernehmen.
  // refPath ist der Pfad, unter dem die Datei im Backend aufgelöst wird
  // (rel-Pfad eines Input-Roots oder "upload:<name>" für PC-Uploads).
  function remuxAddStreamsFromProbe(data, refPath, name) {
    const streams = [];
    (data.audio || []).forEach((a) => {
      const brH = a.bitrate_human || (a.bitrate ? `${Math.round(a.bitrate / 1000)} kbit/s` : "—");
      const ch = a.channels || "?";
      streams.push({
        type: "audio", stream: a.index, src_codec: a.codec,
        src_bitrate: a.bitrate || 0,
        src_bitrate_human: brH,
        channels: a.channels || 0,
        desc: `${a.codec || "?"} · ${ch}ch · ${brH}`,
        language: a.language, title: a.title,
      });
    });
    (data.subtitles || []).forEach((s) => streams.push({
      type: "subtitle", stream: s.index, src_codec: s.codec,
      desc: `${s.codec || "?"}`,
      language: s.language, title: s.title,
    }));
    if (!streams.length) return 0;
    streams.forEach((st) => state.remuxExt.push({
      path: refPath, name: name, type: st.type, stream: st.stream, desc: st.desc,
      src_codec: st.src_codec || "",
      src_bitrate: st.src_bitrate || 0,
      src_bitrate_human: st.src_bitrate_human || "",
      language: (st.language && st.language !== "und") ? st.language : "",
      title: st.title || "", delay: 0, default: false, forced: false,
      transcode: false, codec: "eac3", bitrate: 640,
      channels: st.channels || 0,
      duration: data.duration || 0,
    }));
    return streams.length;
  }

  async function remuxAddExternal(f) {
    closeModal();
    $("remux-start-info").textContent = `Analysiere ${f.name} …`;
    try {
      const data = await (await fetch(`/api/remux/probe?path=${encodeURIComponent(f.rel)}`)).json();
      if (data.error) { $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(data.error)}</span>`; return; }
      if (!remuxConfirmDurationMatch(data, f.name)) {
        $("remux-start-info").textContent = tt("Hinzufügen abgebrochen.");
        return;
      }
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
    remuxSyncExternalInputs();
    remuxRenderEditor();
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
      if (!remuxConfirmDurationMatch(data, data.name || file.name)) {
        $("remux-start-info").textContent = tt("Hinzufügen abgebrochen.");
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
    remuxSyncExternalInputs();
    remuxRenderEditor();
  }

  function remuxApplyEditSpec(spec) {
    if (!spec || !state.remuxInfo) return;
    const set = (id, val, ev) => {
      const el = $(id);
      if (!el || val === undefined || val === null) return;
      if (el.type === "checkbox") el.checked = !!val; else el.value = val;
      el.dispatchEvent(new Event(ev || (el.tagName === "SELECT" ? "change" : "input")));
    };
    if (spec.container) set("remux-container", spec.container, "change");
    if (spec.keep_chapters !== undefined) set("remux-keep-chapters", spec.keep_chapters);
    if (spec.keep_metadata !== undefined) set("remux-keep-metadata", spec.keep_metadata);
    if (spec.keep_attachments !== undefined) set("remux-keep-att", spec.keep_attachments);
    if (spec.trim) {
      if (spec.trim.start != null) set("remux-trim-start", spec.trim.start);
      if (spec.trim.end != null) set("remux-trim-end", spec.trim.end);
    }
    state.remuxExt = (spec.external || []).map((e) => ({
      path: e.path, name: e.name || e.path, type: e.type || "audio",
      stream: e.stream || 0, desc: e.desc || "",
      src_codec: e.src_codec || "",
      language: e.language || "", title: e.title || "",
      delay: e.delay || 0, default: !!e.default, forced: !!e.forced,
      transcode: !!e.transcode, codec: e.codec || "eac3",
      bitrate: e.bitrate || 640, channels: e.channels || 0,
      duration: e.duration || 0,
    }));
    remuxRenderEditor();

    const applyAudioRow = (tr, a) => {
      const q = (c) => tr.querySelector("." + c);
      if (q("rx-a-keep")) q("rx-a-keep").checked = a.keep !== false;
      if (q("rx-a-default")) q("rx-a-default").checked = !!a.default;
      if (q("rx-a-forced")) q("rx-a-forced").checked = !!a.forced;
      if (q("rx-a-lang") && a.language != null) q("rx-a-lang").value = a.language;
      if (q("rx-a-title") && a.title != null) q("rx-a-title").value = a.title;
      if (q("rx-a-tc")) q("rx-a-tc").checked = !!a.transcode;
      if (q("rx-a-codec") && a.codec) q("rx-a-codec").value = a.codec;
      if (q("rx-a-br") && a.bitrate) q("rx-a-br").value = a.bitrate;
      if (q("rx-a-tc")) q("rx-a-tc").dispatchEvent(new Event("change"));
    };
    const applySubRow = (tr, s) => {
      const q = (c) => tr.querySelector("." + c);
      if (q("rx-s-keep")) q("rx-s-keep").checked = s.keep !== false;
      if (q("rx-s-default")) q("rx-s-default").checked = !!s.default;
      if (q("rx-s-forced")) q("rx-s-forced").checked = !!s.forced;
      if (q("rx-s-lang") && s.language != null) q("rx-s-lang").value = s.language;
      if (q("rx-s-title") && s.title != null) q("rx-s-title").value = s.title;
    };

    (spec.audio || []).forEach((a) => {
      if (a.external || a.path) {
        const i = state.remuxExt.findIndex(
          (e) => e.path === a.path && Number(e.stream) === Number(a.stream || 0)
            && e.type !== "subtitle");
        const tr = i >= 0
          ? document.querySelector(`#remux-audio-body tr[data-ext-i="${i}"]`) : null;
        if (tr) applyAudioRow(tr, a);
        return;
      }
      const tr = document.querySelector(
        `#remux-audio-body tr[data-aindex="${a.index}"]`);
      if (tr) applyAudioRow(tr, a);
    });
    (spec.subtitles || []).forEach((s) => {
      if (s.external || s.path) {
        const i = state.remuxExt.findIndex(
          (e) => e.path === s.path && Number(e.stream) === Number(s.stream || 0)
            && e.type === "subtitle");
        const tr = i >= 0
          ? document.querySelector(`#remux-sub-body tr[data-ext-i="${i}"]`) : null;
        if (tr) applySubRow(tr, s);
        return;
      }
      const tr = document.querySelector(
        `#remux-sub-body tr[data-sindex="${s.index}"]`);
      if (tr) applySubRow(tr, s);
    });

    // DOM-Reihenfolge an gespeicherte Spec-Reihenfolge anpassen.
    const ab = $("remux-audio-body");
    if (ab && (spec.audio || []).length) {
      (spec.audio || []).forEach((a) => {
        let tr = null;
        if (a.external || a.path) {
          const i = state.remuxExt.findIndex(
            (e) => e.path === a.path && Number(e.stream) === Number(a.stream || 0)
              && e.type !== "subtitle");
          if (i >= 0) tr = ab.querySelector(`tr[data-ext-i="${i}"]`);
        } else {
          tr = ab.querySelector(`tr[data-aindex="${a.index}"]`);
        }
        if (tr) ab.appendChild(tr);
      });
    }
    const sb = $("remux-sub-body");
    if (sb && (spec.subtitles || []).length) {
      (spec.subtitles || []).forEach((s) => {
        let tr = null;
        if (s.external || s.path) {
          const i = state.remuxExt.findIndex(
            (e) => e.path === s.path && Number(e.stream) === Number(s.stream || 0)
              && e.type === "subtitle");
          if (i >= 0) tr = sb.querySelector(`tr[data-ext-i="${i}"]`);
        } else {
          tr = sb.querySelector(`tr[data-sindex="${s.index}"]`);
        }
        if (tr) sb.appendChild(tr);
      });
    }
    remuxUpdateConflicts();
  }

  function remuxGatherSpec() {
    remuxSyncExternalInputs();
    const audio = [];
    document.querySelectorAll("#remux-audio-body tr[data-aindex], #remux-audio-body tr[data-ext-i]").forEach((tr) => {
      if (tr.dataset.extI !== undefined) {
        const i = parseInt(tr.dataset.extI, 10);
        const e = state.remuxExt[i];
        if (!e) return;
        const q = (c) => tr.querySelector("." + c);
        audio.push({
          external: true,
          path: e.path,
          stream: e.stream,
          keep: q("rx-a-keep") ? q("rx-a-keep").checked : true,
          default: q("rx-a-default") ? q("rx-a-default").checked : !!e.default,
          forced: q("rx-a-forced") ? q("rx-a-forced").checked : !!e.forced,
          language: q("rx-a-lang") ? q("rx-a-lang").value.trim() : (e.language || ""),
          title: q("rx-a-title") ? q("rx-a-title").value.trim() : (e.title || ""),
          transcode: q("rx-a-tc") ? q("rx-a-tc").checked : !!e.transcode,
          codec: q("rx-a-codec") ? q("rx-a-codec").value : (e.codec || "eac3"),
          bitrate: q("rx-a-br") ? (parseInt(q("rx-a-br").value, 10) || 640) : (e.bitrate || 640),
          delay: e.delay || 0,
          src_codec: e.src_codec || "",
          name: e.name || "",
          type: "audio",
        });
        return;
      }
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
    document.querySelectorAll("#remux-sub-body tr[data-sindex], #remux-sub-body tr[data-ext-i]").forEach((tr) => {
      if (tr.dataset.extI !== undefined) {
        const i = parseInt(tr.dataset.extI, 10);
        const e = state.remuxExt[i];
        if (!e) return;
        const q = (c) => tr.querySelector("." + c);
        subtitles.push({
          external: true,
          path: e.path,
          stream: e.stream,
          keep: q("rx-s-keep") ? q("rx-s-keep").checked : true,
          default: q("rx-s-default") ? q("rx-s-default").checked : !!e.default,
          forced: q("rx-s-forced") ? q("rx-s-forced").checked : !!e.forced,
          language: q("rx-s-lang") ? q("rx-s-lang").value.trim() : (e.language || ""),
          title: q("rx-s-title") ? q("rx-s-title").value.trim() : (e.title || ""),
          delay: e.delay || 0,
          name: e.name || "",
          type: "subtitle",
        });
        return;
      }
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
    const disc = state.remuxSel && state.remuxSel.bluray;
    if (disc && disc.dvd_title) {
      spec.dvd_title = disc.dvd_title;
      spec.playlist_duration = disc.duration;
      spec.disc_size = disc.size || 0;
      spec.disc_role = disc.role || "";
    } else if (disc && disc.iso) {
      spec.playlist_clips = (disc.clips || []).slice();
      spec.playlist_duration = disc.duration;
      spec.disc_role = disc.role || "";
      spec.disc_playlist = disc.playlist || "";
    } else if (disc && (disc.clips || []).length > 1) {
      spec.playlist_clips = disc.clips.slice();
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
    if (state.remuxSel.bluray && state.remuxSel.bluray.source
        && $("remux-post").value === "inplace") {
      $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(tt(
        state.remuxSel.bluray.dvd_title
          ? "Eine DVD wird nicht ersetzt. Bitte einen Zielordner wählen."
          : "Ein ISO-Abbild wird nicht ersetzt. Bitte einen Zielordner wählen."))}</span>`;
      return;
    }
    if (state.remuxSel.bluray && (state.remuxSel.bluray.clips || []).length > 1
        && $("remux-post").value === "inplace") {
      $("remux-start-info").innerHTML = `<span class="bad">${escapeHtml(tt(
        "Eine Playlist aus mehreren M2TS ersetzt nicht die einzelne Datei. Bitte einen Zielordner wählen."))}</span>`;
      return;
    }
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

  function clipPlayUrl(file) {
    return `/api/vmaf/clip?session=${encodeURIComponent(state.vmafSession)}`
      + `&file=${encodeURIComponent(file)}`;
  }

  function clipAbBlock(picks) {
    if (!picks || picks.length !== 2) {
      return "Genau zwei auswählen: Original und ein Testclip, oder zwei Testclips.";
    }
    const refs = picks.filter((p) => p.kind === "ref");
    const encs = picks.filter((p) => p.kind !== "ref");
    if (refs.length === 1 && encs.length === 1) {
      const src = state.vmafSource;
      if (!src || src.available === false || !src.path) return "Quelle nicht mehr verfügbar";
      if (!encs[0].clip || !state.vmafSession || !(encs[0].len > 0)) return "Testclip fehlt";
      return "";
    }
    if (!refs.length && encs.length === 2) {
      if (!state.vmafSession || encs.some((p) => !p.clip)) return "Testclip fehlt";
      return "";
    }
    return "Genau zwei auswählen: Original und ein Testclip, oder zwei Testclips.";
  }

  async function openClipAb(picks) {
    if (!ab.sides.a || !ab.sides.b) return;
    const refs = picks.filter((p) => p.kind === "ref");
    const encs = picks.filter((p) => p.kind !== "ref");
    const sa = ab.sides.a;
    const sb = ab.sides.b;
    sa.clipUrl = "";
    sb.clipUrl = "";
    sa.path = "";
    sb.path = "";
    state.abJob = "";
    const off = $("ab-offset");
    if (off) off.value = "0";
    navTo("abcompare");
    abApplyMode();
    const badge = $("ab-badge");
    if (badge) badge.textContent = tt("Lädt …");
    const pathA = $("ab-path-a");
    const pathB = $("ab-path-b");
    let ok;
    if (refs.length === 1) {
      const enc = encs[0];
      const start = enc.start || refs[0].start || 0;
      const len = enc.len || refs[0].len || 0;
      ab.window = len > 0 ? { start, end: start + len } : null;
      sa.path = state.vmafSource.path;
      sa.root = "media";
      sa.offset = 0;
      sb.clipUrl = clipPlayUrl(enc.clip);
      sb.offset = start;
      if (pathA) pathA.value = state.vmafSource.path;
      if (pathB) pathB.value = "";
      abSetStatus(`${tt("Original")} · ${enc.label || ""}`);
      ok = await Promise.all([abStartSide(sa, start), abStartSide(sb, 0)]);
    } else {
      const len = encs[0].len || encs[1].len || 0;
      ab.window = len > 0 ? { start: 0, end: len } : null;
      sa.clipUrl = clipPlayUrl(encs[0].clip);
      sb.clipUrl = clipPlayUrl(encs[1].clip);
      sa.offset = 0;
      sb.offset = 0;
      if (pathA) pathA.value = "";
      if (pathB) pathB.value = "";
      abSetStatus(`${encs[0].label || "A"} · ${encs[1].label || "B"}`);
      ok = await Promise.all([abStartSide(sa, 0), abStartSide(sb, 0)]);
    }
    if (badge) badge.textContent = ok.every(Boolean) ? tt("Geladen") : tt("Fehler");
    const box = $("ab-weak");
    if (box) { box.style.display = "none"; box.innerHTML = ""; }
  }

  /* --------------------------------------------------- A/B-VERGLEICHSPLAYER */
  // Zwei Seiten (A = Quelle, B = Ausgabe). Jede Seite spielt direkt
  // (/api/media), wenn der Browser den Codec kann, sonst über eine HLS-Session
  // des Studio-Players (Server-Transcode). HLS-Sessions beginnen am Seek-Punkt
  // (EVENT-Playlist) – ein Sprung außerhalb des Puffers startet die Session neu.
  const ab = {
    sides: {},          // {a: side, b: side}
    mode: "side",       // side | wipe
    wipe: 50,           // Kante in %
    seeking: false,
    weak: null,         // Schwachstellen (vom Job)
    window: null,       // {start, end} in Filmzeit, solange ein Testclip das Ende setzt
  };

  function abMakeSide(key) {
    return {
      key, video: $("ab-video-" + key), path: "", root: "media", clipUrl: "",
      mode: "", sid: "", offset: 0, hls: null, duration: 0, ready: false,
    };
  }

  function abDestroy(side) {
    if (side.hls) { try { side.hls.destroy(); } catch (e) { /* ignore */ } side.hls = null; }
    if (side.sid) {
      fetch(`/api/player/session/${side.sid}`, { method: "DELETE" }).catch(() => {});
      side.sid = "";
    }
    try { side.video.pause(); side.video.removeAttribute("src"); side.video.load(); } catch (e) { /* ignore */ }
    side.ready = false;
  }

  // Filmzeit ↔ Elementzeit (HLS-Sessions beginnen bei `offset`).
  const abTime = (side) => side.video.currentTime + (side.offset || 0);
  const abSetTime = (side, t) => { side.video.currentTime = Math.max(0, t - (side.offset || 0)); };
  const abBufferedEnd = (side) => {
    const b = side.video.buffered;
    return b.length ? b.end(b.length - 1) + (side.offset || 0) : (side.offset || 0);
  };
  const abOffsetB = () => parseFloat(($("ab-offset") || {}).value) || 0;
  const abSynced = () => !$("ab-sync") || $("ab-sync").checked;
  const abSetStatus = (msg, bad) => {
    const el = $("ab-status");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("bad", !!bad);
  };

  async function abStartSide(side, startSec) {
    abDestroy(side);
    if (side.clipUrl) {
      side.mode = "direct";
      side.path = side.clipUrl;
      side.sid = "";
      side.video.src = side.clipUrl;
      side.video.load();
      side.ready = true;
      side.video.addEventListener("loadedmetadata", () => {
        const dur = Number(side.video.duration);
        if (Number.isFinite(dur) && dur > 0) side.duration = (side.offset || 0) + dur;
      }, { once: true });
      return true;
    }
    if (!side.path) return false;
    const forceHls = $("ab-playback") && $("ab-playback").value === "hls";
    const codecs = (typeof window.fpDetectClientCodecs === "function") ? window.fpDetectClientCodecs() : ["h264"];
    let d;
    try {
      d = await (await fetch("/api/player/session", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: side.path, audio: 0, subtitle: -1, start: Math.max(0, startSec || 0),
          profile: "auto", client_direct_ok: !forceHls, client_codecs: codecs,
          lookahead_sec: 30, audio_copy: false,
        }),
      })).json();
    } catch (e) {
      abSetStatus(`${side.key.toUpperCase()}: ${e}`, true);
      return false;
    }
    if (d.error) { abSetStatus(`${side.key.toUpperCase()}: ${d.error}`, true); return false; }
    const sess = d.session || {};
    side.sid = sess.id || "";
    side.mode = sess.mode || "hls";
    side.duration = sess.duration || (d.info && d.info.duration) || side.duration || 0;
    if (side.mode === "direct") {
      side.offset = 0;
      side.sid = "";  // Direct-Play hält keinen Encoder offen
      side.video.src = sess.media_url || sess.playlist_url
        || `/api/media?root=${encodeURIComponent(side.root)}&path=${encodeURIComponent(side.path)}`;
      side.video.load();
      if (startSec > 0) {
        side.video.addEventListener("loadedmetadata", () => { try { side.video.currentTime = startSec; } catch (e) { /* ignore */ } }, { once: true });
      }
      side.ready = true;
      return true;
    }
    // HLS: auf „ready“ warten, dann hls.js anhängen.
    side.offset = sess.start || 0;
    const url = sess.playlist_url || `/api/player/session/${side.sid}/index.m3u8`;
    for (let i = 0; i < 60; i++) {
      const st = await (await fetch(`/api/player/session/${side.sid}`)).json();
      if (st.session && st.session.ready) break;
      if (st.session && st.session.error) { abSetStatus(`${side.key.toUpperCase()}: ${st.session.error}`, true); return false; }
      await new Promise((r) => setTimeout(r, 150));
    }
    if (window.Hls && window.Hls.isSupported()) {
      side.hls = new window.Hls({
        enableWorker: true, lowLatencyMode: false, maxBufferLength: 60, maxMaxBufferLength: 90,
        backBufferLength: Infinity, startPosition: 0, liveDurationInfinity: false,
      });
      side.hls.loadSource(url);
      side.hls.attachMedia(side.video);
      side.hls.on(window.Hls.Events.ERROR, (_, data) => {
        if (data && data.fatal && side.hls) {
          const det = String(data.details || "");
          if (det.indexOf("Load") >= 0) { try { side.hls.startLoad(); } catch (e) { /* ignore */ } }
          else abSetStatus(`${side.key.toUpperCase()}: ${det}`, true);
        }
      });
    } else if (side.video.canPlayType("application/vnd.apple.mpegurl")) {
      side.video.src = url;
    } else {
      abSetStatus(tt("HLS wird von diesem Browser nicht unterstützt."), true);
      return false;
    }
    side.ready = true;
    return true;
  }

  // Beide Seiten auf Filmzeit t setzen. HLS außerhalb des Puffers → Session neu ab t.
  async function abSeekBoth(t, pauseAfter) {
    const sa = ab.sides.a, sb = ab.sides.b;
    if (!sa || !sb) return;
    const win = ab.window;
    if (win && win.end > win.start) {
      t = Math.max(win.start, Math.min(win.end - 0.05, t));
    }
    ab.seeking = true;
    const wasPaused = sa.video.paused;
    const jobs = [];
    const want = (side, tt_) => {
      if (!side.path) return;
      if (side.mode === "direct") { abSetTime(side, tt_); return; }
      const inWin = tt_ >= (side.offset || 0) - 0.01 && tt_ <= abBufferedEnd(side) + 20;
      if (side.ready && inWin) abSetTime(side, tt_);
      else jobs.push(abStartSide(side, Math.max(0, tt_)).then(() => { abSetTime(side, tt_); }));
    };
    want(sa, t);
    want(sb, t + abOffsetB());
    if (jobs.length) {
      abSetStatus(tt("Spule …"));
      await Promise.all(jobs);
      abSetStatus("");
    }
    ab.seeking = false;
    if (pauseAfter || wasPaused) { sa.video.pause(); sb.video.pause(); }
    else { sa.video.play().catch(() => {}); if (abSynced()) sb.video.play().catch(() => {}); }
  }

  function abApplyMode() {
    const wrap = $("ab-videos");
    const handle = $("ab-wipe-handle");
    if (!wrap) return;
    ab.mode = ($("ab-mode") || {}).value || "side";
    const wipe = ab.mode === "wipe";
    wrap.classList.toggle("ab-wipe", wipe);
    if (handle) handle.style.display = wipe ? "" : "none";
    wrap.style.setProperty("--ab-wipe", ab.wipe + "%");
    abApplyZoom();
  }

  // 1:1 und darüber: die Mitte füllt das Fenster in echten Pixeln (bzw. 2×/4×).
  // Der Maßstab gilt für beide Seiten, damit derselbe Ausschnitt vergleichbar bleibt.
  function abZoomFactor(video) {
    const sel = $("ab-zoom");
    const mode = sel ? sel.value : "fit";
    if (!mode || mode === "fit") return 1;
    const extra = parseFloat(mode) || 1;
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh || !video.clientWidth || !video.clientHeight) return 1;
    const fit = Math.min(video.clientWidth / vw, video.clientHeight / vh);
    if (!(fit > 0)) return 1;
    const dpr = window.devicePixelRatio || 1;
    return extra / (fit * dpr);
  }

  function abApplyZoom() {
    const sel = $("ab-zoom");
    const zoomed = !!(sel && sel.value && sel.value !== "fit");
    const wipe = ab.mode === "wipe";
    Object.values(ab.sides).forEach((s) => {
      const video = s && s.video;
      if (!video) return;
      video.controls = !wipe && !zoomed;
      if (!zoomed || !video.videoWidth) {
        if (!zoomed) video.style.transform = "";
        return;
      }
      video.style.transform = `scale(${abZoomFactor(video)})`;
    });
  }

  async function abLoadWeakSpots() {
    const box = $("ab-weak");
    if (!box) return;
    ab.weak = null;
    if (!state.abJob) { box.style.display = "none"; box.innerHTML = ""; return; }
    let d = null;
    try {
      d = await (await fetch(`/api/compare/weak-spots?job=${encodeURIComponent(state.abJob)}`)).json();
    } catch (e) { d = null; }
    if (!d || !(d.spots || []).length) { box.style.display = "none"; box.innerHTML = ""; return; }
    ab.weak = d;
    const chips = d.spots.map((sp, i) =>
      `<button class="btn btn-ghost btn-sm" data-ab-jump="${sp.time}" data-idx="${i}" `
      + `title="${tt("Szene")} ${sp.scene + 1}${sp.kind === "frame" ? ` · Frame ${sp.frame}` : ""}">`
      + `VMAF ${Number(sp.vmaf).toFixed(1)} · ${fmtClock(sp.time)}</button>`).join("");
    const scenes = (d.scenes || []).filter((s) => s.start != null).map((s) =>
      `<button class="btn btn-ghost btn-sm" data-ab-jump="${s.start}" title="${tt("Szenenanfang")}">`
      + `${tt("Szene")} ${s.scene + 1} · ${fmtClock(s.start)} · ${Number(s.vmaf).toFixed(1)}</button>`).join("");
    box.innerHTML = `
      <div class="muted" style="font-size:12px">${tt("Schwächste Stellen")} (${escapeHtml(d.label || "")}${d.vmaf != null ? `, Ø ${Number(d.vmaf).toFixed(1)}` : ""}) – ${tt("Klick springt hin und hält an")}:</div>
      <div class="ab-weak-list">${chips}</div>
      ${scenes ? `<div class="muted" style="font-size:12px;margin-top:8px">${tt("Testszenen")}:</div><div class="ab-weak-list">${scenes}</div>` : ""}`;
    box.style.display = "";
    box.querySelectorAll("[data-ab-jump]").forEach((b) => b.addEventListener("click", () => {
      box.querySelectorAll("[data-ab-jump]").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      abSeekBoth(parseFloat(b.dataset.abJump) || 0, true);
    }));
  }

  function initAbCompare() {
    const load = $("btn-ab-load");
    if (!load) return;
    ab.sides.a = abMakeSide("a");
    ab.sides.b = abMakeSide("b");
    const va = ab.sides.a.video, vb = ab.sides.b.video;

    load.addEventListener("click", async () => {
      const sa = ab.sides.a, sb = ab.sides.b;
      sa.clipUrl = "";
      sb.clipUrl = "";
      ab.window = null;
      sa.path = $("ab-path-a").value.trim(); sa.root = $("ab-root-a").value || "media";
      sb.path = $("ab-path-b").value.trim(); sb.root = $("ab-root-b").value || "media";
      const badge = $("ab-badge");
      if (badge) badge.textContent = tt("Lädt …");
      abSetStatus(tt("Starte Wiedergabe …"));
      abApplyMode();
      const ok = await Promise.all([abStartSide(sa, 0), abStartSide(sb, 0)]);
      if (badge) badge.textContent = ok.every(Boolean) ? tt("Geladen") : tt("Fehler");
      const modes = [sa, sb].filter((s) => s.path).map((s) => `${s.key.toUpperCase()}: ${s.mode === "direct" ? "Direct" : "HLS"}`);
      if (ok.every(Boolean)) abSetStatus(modes.join(" · "));
      abLoadWeakSpots();
    });

    // B exakt auf A (+ Versatz) ziehen. Wird beim Suchen und laufend genutzt.
    const alignB = (force) => {
      if (!abSynced() || ab.seeking) return;
      const sb = ab.sides.b;
      if (!sb.path || !sb.ready) return;
      const t = Math.max(0, abTime(ab.sides.a) + abOffsetB());
      if (force || Math.abs(abTime(sb) - t) > 0.05) {
        // Außerhalb des HLS-Fensters → Session neu statt ins Leere springen.
        if (sb.mode !== "direct" && (t < (sb.offset || 0) - 0.01 || t > abBufferedEnd(sb) + 20)) {
          abSeekBoth(abTime(ab.sides.a));
        } else {
          abSetTime(sb, t);
        }
      }
    };

    // A ist Master; B folgt (mit Versatz).
    va.addEventListener("play", () => { if (abSynced()) vb.play().catch(() => {}); });
    va.addEventListener("pause", () => { if (abSynced()) vb.pause(); });
    va.addEventListener("ratechange", () => { vb.playbackRate = va.playbackRate; });
    va.addEventListener("seeking", () => alignB(true));
    va.addEventListener("seeked", () => alignB(true));
    va.addEventListener("timeupdate", () => {
      const seek = $("ab-seek"), time = $("ab-time");
      const sa = ab.sides.a;
      const win = ab.window;
      const boxed = win && win.end > win.start;
      const origin = boxed ? win.start : 0;
      const dur = boxed ? (win.end - win.start) : (sa.duration || va.duration || 0);
      const cur = abTime(sa) - origin;
      if (dur) {
        if (seek && !ab.seeking) seek.value = String(Math.round((Math.max(0, cur) / dur) * 1000));
        if (time) time.textContent = fmtClock(Math.max(0, cur)) + " / " + fmtClock(dur);
      }
      if (boxed && abTime(sa) >= win.end - 0.05) {
        va.pause();
        vb.pause();
      }
      // Drift in Filmzeit, nicht gegen die Fensterzeit. Sonst liegt der
      // Testclip beim Original um den Szenenstart daneben und wird bei
      // jedem timeupdate neu gespult (Flackern).
      const sb = ab.sides.b;
      const targetB = abTime(sa) + abOffsetB();
      if (abSynced() && sb.ready && !sb.video.seeking && Math.abs(abTime(sb) - targetB) > 0.25) {
        alignB(false);
      }
    });

    $("ab-play").addEventListener("click", () => {
      if (va.paused) { va.play().catch(() => {}); } else { va.pause(); }
    });
    // Einzelbild vor/zurück (beide): Framedauer aus 24 fps angenommen, wenn unbekannt.
    const step = (dir) => {
      const fps = (ab.weak && ab.weak.fps) || 24;
      va.pause(); vb.pause();
      abSeekBoth(Math.max(0, abTime(ab.sides.a) + dir / fps), true);
    };
    const sbk = $("ab-step-back"), sfw = $("ab-step-fwd");
    if (sbk) sbk.addEventListener("click", () => step(-1));
    if (sfw) sfw.addEventListener("click", () => step(1));
    const seekEl = $("ab-seek");
    seekEl.addEventListener("input", () => { ab.seeking = true; });
    seekEl.addEventListener("change", (e) => {
      const win = ab.window;
      const boxed = win && win.end > win.start;
      const origin = boxed ? win.start : 0;
      const dur = boxed ? (win.end - win.start) : (ab.sides.a.duration || va.duration || 0);
      ab.seeking = false;
      if (dur) abSeekBoth(origin + (parseInt(e.target.value, 10) / 1000) * dur);
    });
    va.addEventListener("ended", () => vb.pause());
    vb.addEventListener("ended", () => va.pause());

    const modeEl = $("ab-mode");
    if (modeEl) modeEl.addEventListener("change", abApplyMode);
    const zoomEl = $("ab-zoom");
    if (zoomEl) zoomEl.addEventListener("change", abApplyZoom);
    [va, vb].forEach((video) => {
      video.addEventListener("loadedmetadata", abApplyZoom);
      video.addEventListener("resize", abApplyZoom);
    });
    if (typeof ResizeObserver !== "undefined") {
      const panes = $("ab-videos");
      if (panes) new ResizeObserver(() => abApplyZoom()).observe(panes);
    }
    const wrap = $("ab-videos");
    if (wrap) {
      let drag = false;
      const setWipe = (ev) => {
        const r = wrap.getBoundingClientRect();
        const x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
        ab.wipe = Math.max(0, Math.min(100, (x / r.width) * 100));
        wrap.style.setProperty("--ab-wipe", ab.wipe.toFixed(2) + "%");
      };
      wrap.addEventListener("mousedown", (ev) => { if (ab.mode !== "wipe") return; drag = true; setWipe(ev); ev.preventDefault(); });
      window.addEventListener("mousemove", (ev) => { if (drag) setWipe(ev); });
      window.addEventListener("mouseup", () => { drag = false; });
      wrap.addEventListener("touchstart", (ev) => { if (ab.mode === "wipe") setWipe(ev); }, { passive: true });
      wrap.addEventListener("touchmove", (ev) => { if (ab.mode === "wipe") setWipe(ev); }, { passive: true });
      // Doppelklick im Wipe: Play/Pause (die nativen Controls sind dort aus).
      wrap.addEventListener("dblclick", () => { if (ab.mode === "wipe") $("ab-play").click(); });
    }
    const pb = $("ab-playback");
    if (pb) pb.addEventListener("change", () => {
      if (ab.sides.a.clipUrl || ab.sides.b.clipUrl) return;
      if (ab.sides.a.path || ab.sides.b.path) load.click();
    });

    const browse = (which) => {
      openFilePickerModal({
        title: `Video ${which.toUpperCase()} wählen`,
        onPick: (f) => {
          $("ab-path-" + which).value = f.rel;
          const rootEl = $("ab-root-" + which);
          if (rootEl) rootEl.value = "media";
          state.abJob = "";  // manuelle Wahl → keine Job-Schwachstellen mehr
        },
      });
    };
    const ba = $("btn-ab-browse-a");
    if (ba) ba.addEventListener("click", () => browse("a"));
    const bb = $("btn-ab-browse-b");
    if (bb) bb.addEventListener("click", () => browse("b"));
  }

  // Beide A/B-Videos pausieren (z. B. beim Verlassen der Seite). HLS-Sessions
  // laufen serverseitig weiter, bis sie als idle aufgeräumt werden.
  function pauseAbVideos() {
    Object.values(ab.sides).forEach((s) => {
      if (s && s.video) { try { s.video.pause(); } catch (e) { /* ignore */ } }
    });
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
    if (prog) prog.textContent = deep ? "Encode-/Decode-Funktionstest läuft (kann etwas dauern) …" : "Prüfe …";
    if (report) report.innerHTML = `<div class="browser-loading">${deep ? "Encoder und Decoder werden real getestet …" : "Selbsttest läuft …"}</div>`;
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

  async function initMediaServers() {
    const badge = $("media-badge");
    if (!$("btn-media-save")) return;
    const markSecret = (id, on) => {
      const el = $(id);
      if (!el) return;
      el.dataset.set = on ? "1" : "";
      el.placeholder = on ? "gesetzt – leer lassen zum Beibehalten" : "API-Schlüssel";
    };
    try {
      const d = await (await fetch("/api/media-servers")).json();
      $("ms-jf-url").value = d.jellyfin_url || "";
      $("ms-sonarr-url").value = d.sonarr_url || "";
      $("ms-radarr-url").value = d.radarr_url || "";
      $("ms-path-from").value = d.path_from || "";
      $("ms-path-to").value = d.path_to || "";
      markSecret("ms-jf-token", d.jellyfin_token_set);
      markSecret("ms-sonarr-key", d.sonarr_key_set);
      markSecret("ms-radarr-key", d.radarr_key_set);
      const active = d.jellyfin_url || d.sonarr_url || d.radarr_url;
      if (badge) badge.textContent = active ? "Aktiv" : "Aus";
    } catch (e) { /* ignorieren */ }

    $("btn-media-save").addEventListener("click", async () => {
      const body = {
        jellyfin_url: $("ms-jf-url").value.trim(),
        jellyfin_token: $("ms-jf-token").value.trim(),
        sonarr_url: $("ms-sonarr-url").value.trim(),
        sonarr_key: $("ms-sonarr-key").value.trim(),
        radarr_url: $("ms-radarr-url").value.trim(),
        radarr_key: $("ms-radarr-key").value.trim(),
        path_from: $("ms-path-from").value.trim(),
        path_to: $("ms-path-to").value.trim(),
      };
      await fetch("/api/media-servers", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      ["ms-jf-token", "ms-sonarr-key", "ms-radarr-key"].forEach((id) => {
        const el = $(id);
        const typed = !!(el && el.value.trim());
        if (el) el.value = "";
        markSecret(id, typed || (el && el.dataset.set === "1"));
      });
      const msg = $("ms-msg");
      if (msg) msg.textContent = "Gespeichert.";
      if (badge) badge.textContent = (body.jellyfin_url || body.sonarr_url || body.radarr_url) ? "Aktiv" : "Aus";
    });
    $("btn-media-test").addEventListener("click", async () => {
      const msg = $("ms-msg");
      if (msg) msg.textContent = "Prüfe …";
      try {
        const d = await (await fetch("/api/media-servers/test", { method: "POST" })).json();
        const rows = d.results || {};
        const keys = Object.keys(rows);
        if (!keys.length) {
          if (msg) msg.textContent = "Nichts konfiguriert.";
          return;
        }
        if (msg) msg.textContent = keys.map((k) => {
          const r = rows[k];
          return `${k}: ${r.ok ? "ok" : "Fehler"} ${r.detail || ""}`.trim();
        }).join(" · ");
      } catch (e) {
        if (msg) msg.textContent = "Prüfung fehlgeschlagen.";
      }
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
          if (act === "play") { playMedia("media", path, name); return; }
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
    initTooltips();
    initTheme();
    initSettings();
    initVmafTool();
    initSuperTool();
    initDataBrowser();
    initParallel();
    initVmafHistory();
    initNav();
    initBitratePanel();
    initProfiles();
    initStats();
    initLibrary();
    initNotify();
    initMediaServers();
    initWatch();
    initScheduler();
    initApiKeys();
    initAbCompare();
    initAudioOpt();
    initDiagnostics();
    initOutTargets();
    initMediaSettings();
    initVmafP1Gap();
    initEncoderSpeed();
    initEncoderBench();
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
