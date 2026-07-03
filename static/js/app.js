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
  };

  /* --------------------------------------------------------- NAVIGATION */
  function showCard(el, hasContent) {
    if (!el) return;
    el.dataset.hasContent = hasContent ? "1" : "";
    el.style.display = (hasContent && el.dataset.page === state.currentPage) ? "" : "none";
  }

  function applyPageVisibility() {
    document.querySelectorAll("[data-page]").forEach((el) => {
      const onPage = el.dataset.page === state.currentPage;
      if (el.id === "vmaf-card" || el.id === "progress-card") {
        el.style.display = (onPage && el.dataset.hasContent === "1") ? "" : "none";
      } else {
        el.style.display = onPage ? "" : "none";
      }
    });
  }

  function initNav() {
    const nav = $("nav");
    if (!nav) return;
    const go = (page) => {
      state.currentPage = page;
      localStorage.setItem("page", page);
      nav.querySelectorAll(".nav-item").forEach((b) =>
        b.classList.toggle("active", b.dataset.nav === page));
      applyPageVisibility();
      if (page === "stats") loadStats();
    };
    nav.querySelectorAll(".nav-item").forEach((b) =>
      b.addEventListener("click", () => go(b.dataset.nav)));
    go(localStorage.getItem("page") || "encode");
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
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* ------------------------------------------------------------- BROWSER */
  async function loadDir(path) {
    state.currentPath = path;
    const browser = $("browser");
    browser.innerHTML = '<div class="browser-loading">Lade Verzeichnis …</div>';
    try {
      const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
      const data = await res.json();
      if (data.error) {
        browser.innerHTML = `<div class="browser-loading">${data.error}</div>`;
        return;
      }
      renderBreadcrumb(data);
      renderBrowser(data);
      // "Diesen Ordner als Batch" bezieht sich auf das aktuelle Verzeichnis
      const folderBtn = $("btn-select-folder");
      folderBtn.disabled = false;
      folderBtn.onclick = () => selectFolder(data.path, data.is_root);
    } catch (e) {
      browser.innerHTML = `<div class="browser-loading">Fehler: ${e}</div>`;
    }
  }

  function renderBreadcrumb(data) {
    const bc = $("breadcrumb");
    bc.innerHTML = "";
    const root = document.createElement("a");
    root.textContent = "/media/input";
    root.onclick = () => loadDir("");
    bc.appendChild(root);
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
        a.onclick = () => loadDir(target);
        bc.appendChild(a);
      });
    }
  }

  function renderBrowser(data) {
    const browser = $("browser");
    browser.innerHTML = "";

    if (!data.is_root) {
      browser.appendChild(makeRow("dir", "..", "", () => loadDir(data.parent || ""), null));
    }
    data.dirs.forEach((d) => {
      browser.appendChild(
        makeRow("dir", d.name, "", () => loadDir(d.rel), null)
      );
    });
    data.files.forEach((f) => {
      browser.appendChild(
        makeRow("file", f.name, f.size_human, null, () => selectFile(f))
      );
    });
    if (!data.dirs.length && !data.files.length) {
      browser.innerHTML = '<div class="browser-loading">Leerer Ordner.</div>';
    }
  }

  function makeRow(type, name, size, onOpen, onPick) {
    const row = document.createElement("div");
    row.className = "row-item";
    const icon = type === "dir" ? "📁" : "🎬";
    row.innerHTML = `
      <span class="row-icon ${type}">${icon}</span>
      <span class="row-name">${escapeHtml(name)}</span>
      <span class="row-size">${size}</span>`;
    if (onOpen) row.addEventListener("click", onOpen);
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

  async function selectFile(f) {
    state.selected = { path: f.rel, name: f.name, isBatch: false };
    $("selection-badge").textContent = "Datei ausgewählt";
    $("btn-enqueue").disabled = false;
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
    } catch (e) {
      $("selected-info").innerHTML = `<span class="bad">Analyse-Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  function chip(label, value, cls) {
    return `<div class="chip ${cls || ""}"><span class="chip-k">${label}</span><span class="chip-v">${value}</span></div>`;
  }

  // Dolby Vision: Hinweis anzeigen und bei Profilen ohne HDR10-Basis (z. B. 5)
  // automatisch Tone-Mapping voreinstellen, damit die Farben stimmen.
  function applyDolbyVision(info) {
    const dvNote = $("dv-note");
    const modeSel = $("opt-hdr-mode");
    if (!info.dolby_vision) {
      if (dvNote) dvNote.style.display = "none";
      return;
    }
    const prof = info.dv_profile || 0;
    // Profil 8.1 hat eine HDR10-kompatible Basis -> „beibehalten" ist ok.
    const hasHdr10Base = prof === 8 || prof === 0;
    if (modeSel && !hasHdr10Base) modeSel.value = "tonemap";
    if (dvNote) {
      dvNote.style.display = "";
      const p = prof ? `Profil ${prof}` : "Dolby Vision";
      dvNote.textContent = hasHdr10Base
        ? `${p} erkannt: Die dynamische DV-Schicht (RPU) kann beim Re-Encode nicht übernommen werden. Die HDR10-Basis bleibt bei „HDR beibehalten" erhalten.`
        : `${p} erkannt: keine HDR10-kompatible Basis – daher ist Tone-Mapping voreingestellt. „HDR beibehalten" würde hier zu Farbfehlern führen. Die DV-Schicht (RPU) kann ohnehin nicht übernommen werden.`;
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
      subs = `<div class="track-block"><div class="track-title">Untertitel (${info.subtitles.length})</div>` +
        info.subtitles.map((s) =>
          `<div class="track-row"><span class="track-lang">${escapeHtml((s.language || "und").toUpperCase())}</span>` +
          `<span>${escapeHtml(s.codec.toUpperCase())}${s.forced ? " · forced" : ""}${s.default ? " · default" : ""}</span>` +
          `${s.title ? `<span class="track-extra">${escapeHtml(s.title)}</span>` : ""}</div>`).join("") +
        `</div>`;
    }

    $("selected-info").innerHTML =
      `<div class="file-title">${escapeHtml(name)}</div>` +
      `<div class="chips">${chips.join("")}</div>${audio}${subs}`;
    wireAudioRows();
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
    const name = isRoot ? "/media/input (alle Unterordner)" : path.split("/").pop();
    state.selected = { path: path, name: name, isBatch: true };
    $("selection-badge").textContent = "Ordner ausgewählt (Batch)";
    $("btn-enqueue").disabled = false;
    $("selected-info").innerHTML =
      `<strong>${escapeHtml(name)}</strong> · Batch-Modus (VMAF-Test repräsentativ für die erste Datei)`;
  }

  /* ------------------------------------------------------------ SETTINGS */
  function initSettings() {
    const quality = $("opt-quality");
    quality.addEventListener("input", () => { $("quality-val").textContent = quality.value; });

    const clip = $("opt-clip");
    clip.addEventListener("input", () => { $("clip-val").textContent = clip.value; });

    const fg = $("opt-film-grain");
    if (fg) fg.addEventListener("input", () => { $("film-grain-val").textContent = fg.value; });

    const vmaf = $("opt-vmaf");
    const manual = $("manual-quality");
    const vmafOpts = $("vmaf-options");
    const syncVmaf = () => {
      const on = vmaf.checked;
      manual.classList.toggle("disabled", on);
      vmafOpts.classList.toggle("disabled", !on);
      $("btn-enqueue").textContent = on && $("opt-workflow").value === "compare_only"
        ? "VMAF-Vergleich starten" : "Zur Warteschlange hinzufügen";
    };
    vmaf.addEventListener("change", syncVmaf);
    $("opt-workflow").addEventListener("change", syncVmaf);
    syncVmaf();

    $("opt-rate-mode").addEventListener("change", () => updateTestValueHints(true));
    updateTestValueHints(false);

    $("opt-platform").addEventListener("change", () => {
      updateCodecAvailability();
      buildCompareOptions();
    });
    $("opt-codec").addEventListener("change", () => {
      updateCodecAvailability();
      buildCompareOptions();
    });
    updateCodecAvailability();
    buildCompareOptions();

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
    $("btn-clear").addEventListener("click", async () => {
      await fetch("/api/queue/clear", { method: "POST" });
    });
    $("btn-pause").addEventListener("click", async () => {
      await fetch("/api/queue/pause", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused: !state.paused }),
      });
    });
    $("btn-skip-encode").addEventListener("click", () => {
      if (state.awaitingItemId) skipEncode(state.awaitingItemId);
    });
  }

  let prevRateFamily = null;
  const rateFamily = (mode) => (mode === "cq" ? "cq" : "bitrate");

  function updateTestValueHints(refill) {
    const mode = $("opt-rate-mode").value;
    const inputs = document.querySelectorAll(".test-val");
    const hint = $("test-values-hint");
    const fam = rateFamily(mode);
    if (mode === "cq") {
      hint.textContent = "CQ/QP: niedrig = hohe Qualität · hoch = kleinere Datei · leere Felder werden ignoriert";
      inputs.forEach((i) => { i.min = 1; i.max = 51; });
    } else {
      hint.textContent = "Bitrate in kbit/s (z. B. 8000, 6000, 4000, 2000) · leere Felder werden ignoriert";
      inputs.forEach((i) => { i.min = 500; i.max = 50000; });
    }
    // Beim Wechsel der Wert-Familie (CQ <-> Bitrate) sinnvolle Defaults setzen.
    // Zwischen Bitrate und ABR bleiben die eingegebenen Werte erhalten.
    if (refill && fam !== prevRateFamily) {
      const defaults = mode === "cq" ? [20, 24, 28, 32] : [8000, 6000, 4000, 2000];
      inputs.forEach((inp, idx) => { inp.value = defaults[idx]; });
    }
    prevRateFamily = fam;
  }

  function gatherTestValues() {
    return [...document.querySelectorAll(".test-val")]
      .map((i) => parseInt(i.value, 10))
      .filter((v) => !isNaN(v) && v > 0)
      .slice(0, 4);
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
    return e ? !!e.available : true; // unbekannt -> nicht blockieren
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
        + (ok ? "" : " — nicht verfügbar");
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
  }

  function compareLabel(v, info) {
    if (info) return `${info.codec_label} · ${info.platform_label}`;
    return COMPARE_LABELS[v] || v;
  }

  // Zeigt ALLE verfügbaren Encoder-Kombinationen (Plattform × Codec) als
  // Vergleichsziele an – außer dem aktuell gewählten Basis-Encoder. So ist auf
  // einen Blick sichtbar, was zur Verfügung steht.
  function buildCompareOptions() {
    const cont = $("compare-encoders");
    if (!cont) return;
    const base = `${$("opt-platform").value}:${$("opt-codec").value}`;
    const prev = new Set(getCompareEncoders());
    const all = encoderMatrix().filter((e) => e.available && e.value !== base);
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

  function gatherSettings() {
    const res = $("opt-resolution").value;
    const perTrack = gatherAudioTrackSettings();
    return {
      platform: $("opt-platform").value,
      codec: $("opt-codec").value,
      quality: parseInt($("opt-quality").value, 10),
      target_height: res ? parseInt(res, 10) : null,
      hdr_mode: $("opt-hdr-mode") ? $("opt-hdr-mode").value : "tonemap",
      keep_subtitles: $("opt-keep-subs") ? $("opt-keep-subs").checked : true,
      keep_chapters: $("opt-keep-chapters") ? $("opt-keep-chapters").checked : true,
      keep_metadata: $("opt-keep-metadata") ? $("opt-keep-metadata").checked : true,
      denoise: $("opt-denoise") ? $("opt-denoise").value : "off",
      film_grain: $("opt-film-grain") ? parseInt($("opt-film-grain").value, 10) : 0,
      two_pass: $("opt-two-pass") ? $("opt-two-pass").checked : false,
      vmaf_check: $("opt-vmaf").checked,
      workflow: $("opt-workflow").value,
      rate_mode: $("opt-rate-mode").value,
      compare_encoders: getCompareEncoders(),
      test_values: gatherTestValues(),
      clip_seconds: parseInt($("opt-clip").value, 10),
      samples: $("opt-samples") ? parseInt($("opt-samples").value, 10) : 1,
      vmaf_engine: $("opt-vmaf-engine") ? $("opt-vmaf-engine").value : "auto",
      generate_screenshots: $("opt-screenshots").checked,
      post_processing: $("opt-post").value,
      suffix: "_" + $("opt-codec").value,
      audio_mode: $("opt-audio-mode").value,
      audio_codec: $("opt-audio-codec").value,
      audio_bitrate: parseInt($("opt-audio-bitrate").value, 10),
      audio_channels: parseInt($("opt-audio-channels").value, 10),
      audio_normalize: $("opt-audio-normalize").checked,
      audio_tracks: gatherAudioTracks(),
      audio_per_track: perTrack !== null && $("opt-audio-mode").value !== "none",
      audio_track_settings: perTrack || [],
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
    const btn = $("btn-enqueue");
    btn.disabled = true;
    const payload = {
      path: state.selected.path,
      is_batch: state.selected.isBatch,
      ...gatherSettings(),
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
      : (q.status_message || (c.running ? "Verarbeitung läuft" : "Bereit"));

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
    return `<span class="codec-badge">${escapeHtml(codecName(s))}</span> ${val}`;
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
      const moveBtns = it.status === "wartend"
        ? `<button class="btn btn-ghost btn-sm iconbtn" data-move="${it.id}" data-dir="-1" title="Nach oben">↑</button>` +
          `<button class="btn btn-ghost btn-sm iconbtn" data-move="${it.id}" data-dir="1" title="Nach unten">↓</button>` : "";
      const err = it.error ? `<div class="muted" style="font-size:11px">${escapeHtml(it.error.slice(0, 80))}</div>` : "";
      // Dauer: laufend (aktiv) oder final (abgeschlossen).
      const dur = (active.has(it.id) || DONE.includes(it.status)) ? (it.duration_human || "—") : "—";
      const finished = DONE.includes(it.status) && it.finished_at
        ? `<div class="muted" style="font-size:11px">${new Date(it.finished_at * 1000).toLocaleTimeString().slice(0,5)}</div>` : "";
      return `<tr>
        <td>${escapeHtml(it.title)}${err}</td>
        <td>${reso}</td>
        <td class="status-cell">${statusBadge(it.status)}</td>
        <td>${settingsLabel(it)}</td>
        <td>${dur}${finished}</td>
        <td class="good">${it.saved_human}</td>
        <td class="row-actions">${moveBtns}${cancelBtn}</td>
      </tr>`;
    }).join("");
    body.querySelectorAll("[data-cancel]").forEach((b) => {
      b.addEventListener("click", () =>
        fetch(`/api/queue/${b.dataset.cancel}/cancel`, { method: "POST" }));
    });
    body.querySelectorAll("[data-move]").forEach((b) => {
      b.addEventListener("click", () =>
        fetch(`/api/queue/${b.dataset.move}/move?direction=${b.dataset.dir}`, { method: "POST" }));
    });
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
      stats = `
        <div class="stat-grid">
          <div class="stat"><span class="stat-label">Phase</span><span class="stat-val">${escapeHtml(phase)}</span></div>
          <div class="stat"><span class="stat-label">Testpunkt</span><span class="stat-val">${step}</span></div>
          <div class="stat"><span class="stat-label">Encode-Speed</span><span class="stat-val">${fps}</span></div>
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
    const reBtn = $("btn-vmaf-reanalyze");
    if (reBtn) reBtn.addEventListener("click", async () => {
      if (!state.viewSession) return;
      const engine = $("vmaf-re-engine") ? $("vmaf-re-engine").value : "cpu";
      reBtn.disabled = true;
      const orig = reBtn.textContent;
      reBtn.textContent = "Startet …";
      try {
        const r = await fetch("/api/vmaf/reanalyze", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session: state.viewSession, vmaf_engine: engine }),
        });
        const d = await r.json();
        if (!r.ok) {
          reBtn.textContent = d.error || "Fehler";
        } else {
          reBtn.textContent = "Läuft – siehe Warteschlange";
          showLiveVmaf();
          updateQueue();
        }
      } catch (e) {
        reBtn.textContent = "Fehler";
      }
      setTimeout(() => { reBtn.disabled = false; reBtn.textContent = orig; }, 3000);
    });
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
      const note = $("vmaf-archive-note");
      if (note) note.style.display = "";
      const reBox = $("vmaf-reanalyze");
      if (reBox) reBox.style.display = data.source_available ? "" : "none";
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
    // Primärwert plus – falls „Beide" gerechnet wurde – GPU-Wert & Differenz.
    let s = `${r.vmaf.toFixed(2)}`;
    if (r.engine === "gpu") s += ' <span class="engine-tag gpu">GPU</span>';
    if (r.vmaf_gpu != null) {
      const d = r.vmaf_delta != null ? r.vmaf_delta : (r.vmaf - r.vmaf_gpu);
      const sign = d >= 0 ? "+" : "";
      s = `${r.vmaf.toFixed(2)} <span class="engine-tag cpu">CPU</span>`
        + `<br><span class="muted">${r.vmaf_gpu.toFixed(2)} <span class="engine-tag gpu">GPU</span>`
        + ` · Δ ${sign}${d.toFixed(2)}</span>`;
    }
    // Mehrere Szenen: Mittelwert oben, Streuung (min–max) je Szene darunter.
    if (r.vmaf_min != null && r.vmaf_max != null) {
      const perScene = (r.scene_scores || [])
        .map((sc) => `Szene ${sc.scene + 1}: ${sc.vmaf.toFixed(1)}`).join("\n");
      s += `<br><span class="muted" title="${escapeHtml(perScene)}">`
        + `Ø · Szenen ${r.vmaf_min.toFixed(1)}–${r.vmaf_max.toFixed(1)}</span>`;
    }
    return s;
  }

  function fillVmafTable(vmaf) {
    const body = $("vmaf-table").querySelector("tbody");
    body.innerHTML = vmaf.results.map((r) => `
      <tr class="${r.recommended ? "row-recommended" : ""}">
        <td>${escapeHtml(r.label || ("Q" + r.quality))}</td>
        <td>${vmafCell(r)}</td>
        <td>${r.predicted_human}</td>
        <td class="${r.savings_percent >= 0 ? "good" : "bad"}">${r.savings_percent}%</td>
        <td>${r.recommended ? '<span class="badge recommended">Empfohlen</span>' : ""}</td>
      </tr>`).join("");
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
    set("opt-quality", s.quality);
    set("opt-resolution", s.target_height ? String(s.target_height) : "");
    set("opt-hdr-mode", s.hdr_mode, "change");
    set("opt-keep-subs", s.keep_subtitles);
    set("opt-keep-chapters", s.keep_chapters);
    set("opt-keep-metadata", s.keep_metadata);
    set("opt-denoise", s.denoise, "change");
    set("opt-film-grain", s.film_grain);
    set("opt-two-pass", s.two_pass);
    set("opt-vmaf", s.vmaf_check);
    set("opt-workflow", s.workflow, "change");
    set("opt-clip", s.clip_seconds);
    set("opt-samples", s.samples, "change");
    set("opt-vmaf-engine", s.vmaf_engine, "change");
    set("opt-screenshots", s.generate_screenshots);
    set("opt-post", s.post_processing, "change");
    set("opt-audio-mode", s.audio_mode, "change");
    set("opt-audio-codec", s.audio_codec, "change");
    set("opt-audio-bitrate", s.audio_bitrate);
    set("opt-audio-channels", s.audio_channels);
    set("opt-audio-normalize", s.audio_normalize);
    if (Array.isArray(s.test_values)) {
      const inputs = [...document.querySelectorAll(".test-val")];
      s.test_values.forEach((v, i) => { if (inputs[i]) inputs[i].value = v; });
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
        return `
        <tr>
          <td>${escapeHtml(j.title || "")}</td>
          <td>${escapeHtml((j.codec || "").toUpperCase())}</td>
          <td>${j.quality || "—"}</td>
          <td>${j.vmaf != null ? Number(j.vmaf).toFixed(1) : "—"}</td>
          <td>${formatBytes(j.original_size)}</td>
          <td>${formatBytes(j.output_size)}</td>
          <td class="${(j.saved_bytes || 0) >= 0 ? "good" : "bad"}">${formatBytes(j.saved_bytes)}</td>
          <td>${formatDuration(j.duration || 0)}</td>
          <td class="muted">${escapeHtml(when)}</td>
          <td>${escapeHtml(j.status || "")}</td>
        </tr>`; }).join("") :
        '<tr class="empty-row"><td colspan="10">Noch keine Jobs.</td></tr>';
    }
  }

  /* ------------------------------------------------------------ BIBLIOTHEK */
  let libPoll = null;
  function initLibrary() {
    const scanBtn = $("btn-lib-scan");
    if (!scanBtn) return;
    scanBtn.addEventListener("click", startLibraryScan);
    $("btn-lib-add").addEventListener("click", addLibrarySelection);
    const all = $("lib-check-all");
    if (all) all.addEventListener("change", () => {
      document.querySelectorAll(".lib-check").forEach((c) => { c.checked = all.checked; });
    });
  }

  function libFilters() {
    const mode = $("lib-codec-mode").value;
    const f = {
      root: state.currentPath || "",
      name_contains: $("lib-name").value.trim(),
      min_size_mb: parseFloat($("lib-min-size").value) || 0,
      min_bitrate_mbps: parseFloat($("lib-min-br").value) || 0,
      min_height: parseInt($("lib-min-h").value, 10) || 0,
      codecs_include: [],
      codecs_exclude: [],
    };
    if (mode === "exclude-av1") f.codecs_exclude = ["av1"];
    else if (mode === "include-h264") f.codecs_include = ["h264"];
    else if (mode === "include-hevc") f.codecs_include = ["hevc"];
    return f;
  }

  async function startLibraryScan() {
    $("lib-scan-badge").textContent = "Scan läuft …";
    await fetch("/api/library/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(libFilters()),
    });
    if (libPoll) clearInterval(libPoll);
    libPoll = setInterval(pollLibrary, 1200);
    pollLibrary();
  }

  async function pollLibrary() {
    try {
      const r = await fetch("/api/library/scan");
      const st = await r.json();
      $("lib-progress").textContent =
        `${st.scanned}/${st.total} geprüft · ${st.matched.length} Treffer`;
      renderLibrary(st.matched);
      if (!st.running) {
        clearInterval(libPoll); libPoll = null;
        $("lib-scan-badge").textContent = st.error ? "Fehler" : `${st.matched.length} Treffer`;
        $("btn-lib-add").disabled = st.matched.length === 0;
      }
    } catch (e) { /* ignorieren */ }
  }

  function renderLibrary(rows) {
    const body = $("lib-body");
    if (!body) return;
    body.innerHTML = rows.length ? rows.map((m) => `
      <tr>
        <td><input type="checkbox" class="lib-check" value="${escapeHtml(m.path)}" /></td>
        <td title="${escapeHtml(m.path)}">${escapeHtml(m.name)}</td>
        <td>${escapeHtml((m.codec || "").toUpperCase())}</td>
        <td>${escapeHtml(m.resolution)}</td>
        <td>${escapeHtml(m.video_bitrate_human)}</td>
        <td>${escapeHtml(m.size_human)}</td>
        <td>${escapeHtml(m.hdr_type)}</td>
      </tr>`).join("") :
      '<tr class="empty-row"><td colspan="7">Keine Treffer.</td></tr>';
  }

  async function addLibrarySelection() {
    const paths = [...document.querySelectorAll(".lib-check:checked")].map((c) => c.value);
    if (!paths.length) return;
    const btn = $("btn-lib-add");
    btn.disabled = true;
    const base = gatherSettings();
    let ok = 0;
    for (const p of paths) {
      try {
        const r = await fetch("/api/enqueue", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: p, is_batch: false, ...base }),
        });
        if (r.ok) ok++;
      } catch (e) { /* weiter */ }
    }
    btn.disabled = false;
    $("lib-progress").textContent = `${ok} zur Warteschlange hinzugefügt.`;
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

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initSettings();
    initDataBrowser();
    initParallel();
    initVmafHistory();
    initNav();
    initProfiles();
    initStats();
    initLibrary();
    initNotify();
    initWatch();
    loadDir("");
    connectWs();
    fetch("/api/config/paths").then((r) => r.json()).then((p) => {
      const el = $("data-dir");
      if (el) el.textContent = p.data_dir;
    }).catch(() => {});
  });
})();
