/* ==========================================================================
 *  HDR / Dolby Vision Labor (Analyzer, 0-Disk-Space Bitrate-Graph,
 *  Cross-Release DV-Copy, HDR10+ -> DV Dual-HDR & RPU-Tresor)
 * ========================================================================== */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const t = (s) => (window.I18N && typeof window.I18N.t === "function") ? window.I18N.t(s) : s;

  const PREF_KEY = "vc_dv_lab_prefs_v1";
  const DEFAULT_COLS = {
    origin: true,
    profile: true,
    cm_version: true,
    frames: true,
    fps: true,
    peak_nits: true,
    cll: true,
    mastering: false,
    scenes: false,
    size: true,
  };

  const dvl = {
    initialized: false,
    activeSubtab: "analyzer",
    activeAnTab: "dvdata",
    currentAnalysis: null,
    chart: null,
    zoomStartPct: 0,
    zoomEndPct: 100,
    vaultEntries: [],
    vaultQueue: new Map(),
    activeVaultIdForCopy: "",
    prefs: loadPrefs(),
  };

  function loadPrefs() {
    try {
      const raw = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
      return {
        scanLimit: raw.scanLimit != null ? String(raw.scanLimit) : "0",
        exportFmt: raw.exportFmt || "txt",
        autoSync: raw.autoSync !== false,
        adjustL5: raw.adjustL5 !== false,
        keepTemp: !!raw.keepTemp,
        saveVault: raw.saveVault !== false,
        vaultOrigRpu: !!raw.vaultOrigRpu,
        vaultConv81: raw.vaultConv81 !== false,
        cols: Object.assign({}, DEFAULT_COLS, raw.cols || {}),
      };
    } catch (_) {
      return {
        scanLimit: "0",
        exportFmt: "txt",
        autoSync: true,
        adjustL5: true,
        keepTemp: false,
        saveVault: true,
        vaultOrigRpu: false,
        vaultConv81: true,
        cols: Object.assign({}, DEFAULT_COLS),
      };
    }
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify(dvl.prefs));
    } catch (_) { /* ignore */ }
  }

  function fmtBytes(b) {
    const n = Number(b || 0);
    if (n <= 0) return "0 MB";
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(2) + " MB";
    return (n / 1024).toFixed(1) + " KB";
  }

  function fmtTimeSec(sec) {
    const s = Math.max(0, Math.floor(Number(sec || 0)));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
    return `${m}:${String(r).padStart(2, "0")}`;
  }

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str == null ? "" : String(str);
    return d.innerHTML;
  }

  function setStatus(id, msg, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = msg || "";
    el.className = "dvl-status" + (kind ? " dvl-status-" + kind : "");
    el.style.display = msg ? "" : "none";
  }

  function setScanProgress(visible, pct, msg) {
    const box = $("dvl-an-progress");
    const fill = $("dvl-an-progress-fill");
    const label = $("dvl-an-progress-label");
    if (!box) return;
    box.hidden = !visible;
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    if (fill) fill.style.width = p.toFixed(1) + "%";
    if (label) label.textContent = msg || "";
    const track = box.querySelector(".dvl-progress-track");
    if (track) track.setAttribute("aria-valuenow", String(Math.round(p)));
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function paintDvlZoom() {
    const x0 = $("dvl-zoom-start");
    const x1 = $("dvl-zoom-end");
    const fill = $("dvl-zoom-fill");
    const lab = $("dvl-zoom-label");
    let a = x0 ? Number(x0.value) : 0;
    let b = x1 ? Number(x1.value) : 100;
    if (!Number.isFinite(a)) a = 0;
    if (!Number.isFinite(b)) b = 100;
    const lo = Math.max(0, Math.min(a, b));
    const hi = Math.min(100, Math.max(a, b));
    if (fill) {
      fill.style.left = lo + "%";
      fill.style.width = Math.max(0, hi - lo) + "%";
    }
    const full = lo <= 0.05 && hi >= 99.95;
    if (lab) lab.textContent = full ? t("gesamt") : `${Math.round(lo)}–${Math.round(hi)} %`;
    return { a: lo, b: hi };
  }

  function clampDvlZoom(changed) {
    const x0 = $("dvl-zoom-start");
    const x1 = $("dvl-zoom-end");
    if (!x0 || !x1) return;
    const gap = 1;
    const a = Number(x0.value);
    const b = Number(x1.value);
    if (changed === "start" && a > b - gap) x0.value = String(Math.max(0, b - gap));
    if (changed === "end" && b < a + gap) x1.value = String(Math.min(100, a + gap));
    x0.classList.toggle("is-top", changed === "start");
    x1.classList.toggle("is-top", changed !== "start");
  }

  function applyDvlZoom() {
    const span = paintDvlZoom();
    dvl.zoomStartPct = span.a;
    dvl.zoomEndPct = span.b;
    renderBitrateChart();
  }

  async function apiJson(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
    return data;
  }

  function parentFolderOf(relPath) {
    const clean = String(relPath || "").trim().replace(/\\/g, "/");
    const idx = clean.lastIndexOf("/");
    return idx > 0 ? clean.slice(0, idx) : undefined;
  }

  function pickSingleVideo(title, memKey, currentVal, onChosen) {
    if (typeof window.openFilePickerModal !== "function") return;
    const startDir = currentVal ? parentFolderOf(currentVal) : undefined;
    const opts = {
      title: t(title),
      kind: "video",
      rememberKey: memKey,
      onPick: (f) => {
        const rel = (f && (f.rel || f.path || f.name)) || "";
        if (rel && onChosen) onChosen(rel, f.name || rel.split("/").pop());
      },
    };
    if (startDir !== undefined) opts.start = startDir;
    window.openFilePickerModal(opts);
  }

  async function collectFolderVideos(folderRel) {
    const found = [];
    async function walk(rel, depth) {
      if (depth > 6 || found.length >= 500) return;
      const data = await apiJson(`/api/browse?path=${encodeURIComponent(rel || "")}&kind=video`);
      if (!data || data.error) return;
      for (const f of (data.files || [])) {
        const p = f.rel || (rel ? `${rel}/${f.name}` : f.name);
        found.push({ path: p, name: f.name, size: f.size || 0, size_human: f.size_human || "" });
      }
      for (const dir of (data.dirs || [])) {
        const sub = dir.rel || (rel ? `${rel}/${dir.name}` : dir.name);
        await walk(sub, depth + 1);
      }
    }
    await walk(folderRel || "", 0);
    return found;
  }

  function switchSubtab(tab) {
    dvl.activeSubtab = tab;
    document.querySelectorAll("[data-dvl-subtab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.dvlSubtab === tab);
    });
    document.querySelectorAll("[data-dvl-panel]").forEach((panel) => {
      panel.style.display = panel.dataset.dvlPanel === tab ? "" : "none";
    });
    if (tab === "vault") loadVault();
    if (tab === "analyzer" && dvl.chart) {
      requestAnimationFrame(() => { try { dvl.chart.resize(); } catch (_) {} });
    }
  }

  function switchAnalyzerTab(tab) {
    dvl.activeAnTab = tab;
    document.querySelectorAll("[data-dvl-antab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.dvlAntab === tab);
    });
    document.querySelectorAll("[data-dvl-anpanel]").forEach((p) => {
      p.style.display = p.dataset.dvlAnpanel === tab ? "" : "none";
    });
    if (tab === "graph" && dvl.chart) {
      requestAnimationFrame(() => { try { dvl.chart.resize(); } catch (_) {} });
    }
  }

  // =========================================================================
  //  1. DV-ANALYZER & BITRATE-GRAPH
  // =========================================================================
  async function runQuickAnalyze(customPath) {
    const input = $("dvl-an-path");
    const path = (customPath != null ? customPath : (input ? input.value : "")).trim();
    if (!path) {
      setStatus("dvl-an-status", t("Bitte zuerst eine Videodatei auswählen."), "warn");
      return;
    }
    if (input) input.value = path;
    setStatus("dvl-an-status", t("Lese Dolby-Vision-Struktur, RPU & MediaInfo …"), "info");
    const data = await apiJson(`/api/dv-lab/analyze?path=${encodeURIComponent(path)}&sample_seconds=180`);
    if (!data.ok) {
      setStatus("dvl-an-status", data.error || t("Analyse fehlgeschlagen."), "err");
      return;
    }
    dvl.currentAnalysis = data;
    setStatus("dvl-an-status", "", "");
    renderAnalysisResult(data);
  }

  async function runBitrateScan() {
    const input = $("dvl-an-path");
    const path = (input ? input.value : "").trim();
    if (!path) {
      setStatus("dvl-an-status", t("Bitte zuerst eine Videodatei laden."), "warn");
      return;
    }
    const limitSec = parseFloat($("dvl-scan-limit")?.value || "0") || 0;
    dvl.prefs.scanLimit = String(limitSec);
    savePrefs();

    const btn = $("btn-dvl-bitrate-scan");
    if (btn) btn.disabled = true;
    setStatus("dvl-an-status", "", "");
    setScanProgress(true, 0, t("0-Disk-Space Bitrate-Scan startet …"));
    try {
      const started = await apiJson("/api/dv-lab/bitrate-scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, duration_limit: limitSec }),
      });
      if (!started.ok || !started.job_id) {
        setScanProgress(false, 0, "");
        setStatus("dvl-an-status", started.error || t("Bitrate-Scan fehlgeschlagen."), "err");
        return;
      }
      let data = null;
      for (;;) {
        const job = await apiJson("/api/dv-lab/bitrate-scan/" + encodeURIComponent(started.job_id));
        if (!job.ok) {
          setScanProgress(false, 0, "");
          setStatus("dvl-an-status", job.error || t("Bitrate-Scan fehlgeschlagen."), "err");
          return;
        }
        setScanProgress(true, job.percent, job.message || t("Bitrate-Scan läuft …"));
        if (job.status === "done" || job.status === "error") {
          data = job.result || { ok: false, error: job.message };
          break;
        }
        await sleep(700);
      }
      if (!data.ok) {
        setScanProgress(false, 0, "");
        setStatus("dvl-an-status", data.error || t("Bitrate-Scan fehlgeschlagen."), "err");
        return;
      }
      dvl.currentAnalysis = data;
      dvl.zoomStartPct = 0;
      dvl.zoomEndPct = 100;
      if ($("dvl-zoom-start")) $("dvl-zoom-start").value = "0";
      if ($("dvl-zoom-end")) $("dvl-zoom-end").value = "100";
      paintDvlZoom();
      setScanProgress(true, 100, t("Bitrate-Scan abgeschlossen."));
      setStatus("dvl-an-status", t("Bitrate-Scan abgeschlossen (0 Byte temporärer Festplattenplatz belegt)."), "ok");
      renderAnalysisResult(data);
      switchAnalyzerTab("graph");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderAnalysisResult(d) {
    const wrap = $("dvl-an-results");
    if (wrap) wrap.style.display = "";

    const elBadge = d.enhancement_layer_type === "FEL"
      ? `<span class="dvl-badge dvl-badge-fel">FEL (Full Enhancement Layer)</span>`
      : (d.enhancement_layer_type === "MEL"
        ? `<span class="dvl-badge dvl-badge-mel">MEL (Minimal Enhancement Layer)</span>`
        : esc(d.enhancement_layer_type || "None"));

    const kvRows = [
      ["Dolby Vision", d.dolby_vision ? `<span class="dvl-badge dvl-badge-ok">Yes</span>` : "No"],
      ["HDR Format", esc(d.hdr_format || "SDR")],
      ["Profile", esc(d.profile_label || d.profile || "—")],
      ["Level", d.level != null ? esc(d.level) : "—"],
      ["CM Version", esc(d.cm_version || "—")],
      ["Base Layer", d.base_layer ? "Yes" : "No"],
      ["Base Layer Type", esc(d.base_layer_type || "—")],
      ["Enhancement Layer", d.enhancement_layer ? "Yes" : "No"],
      ["Enhancement Layer Type", elBadge],
      ["RPU", d.rpu ? "Yes" : "No"],
      ["HDR10+ Koexistenz", d.hdr10_plus ? `<span class="dvl-badge dvl-badge-h10p">Yes (Dual-HDR)</span>` : "No"],
      ["Video Codec", esc(d.video_codec || "—")],
      ["Resolution", esc(d.resolution || "—")],
      ["RPU mastering display", esc(d.rpu_mastering_display || "—")],
      ["RPU content light level", esc(d.rpu_content_light_level || "—")],
    ];

    const scan = d.bitrate_scan;
    if (scan) {
      kvRows.push(
        ["BL Size", esc(scan.bl_size_human)],
        ["BL Average Bitrate", `${Number(scan.bl_avg_mbps).toFixed(2)} Mbps`],
        ["BL Max Bitrate", `${Number(scan.bl_max_mbps).toFixed(2)} Mbps`],
        ["EL Size", esc(scan.el_size_human)],
        ["EL Average Bitrate", `${Number(scan.el_avg_mbps).toFixed(2)} Mbps`],
        ["EL Max Bitrate", `${Number(scan.el_max_mbps).toFixed(2)} Mbps`],
        ["EL Share", `<strong>${Number(scan.el_share_pct).toFixed(2)} %</strong>`],
        ["RPU Size", esc(scan.rpu_size_human)],
        ["RPU Bitrate", `${Number(scan.rpu_avg_kbps).toFixed(2)} Kbps`],
      );
    }

    const grid = $("dvl-an-kv-grid");
    if (grid) {
      grid.innerHTML = kvRows.map(([k, v]) =>
        `<div class="dvl-kv-row"><span class="dvl-kv-key">${esc(k)}</span><span class="dvl-kv-val">${v}</span></div>`
      ).join("");
    }

    const miPre = $("dvl-mediainfo-pre");
    if (miPre) miPre.textContent = d.mediainfo_text || "";

    if (scan) {
      renderBitrateChart();
    } else {
      const hint = $("dvl-graph-empty");
      if (hint) hint.style.display = "";
    }
  }

  function renderBitrateChart() {
    const d = dvl.currentAnalysis;
    const scan = d && d.bitrate_scan;
    if (!scan || typeof window.Chart === "undefined") return;
    const emptyHint = $("dvl-graph-empty");
    if (emptyHint) emptyHint.style.display = "none";

    const totalPts = (scan.timestamps || []).length;
    if (!totalPts) return;

    const i0 = Math.max(0, Math.floor((dvl.zoomStartPct / 100) * (totalPts - 1)));
    const i1 = Math.max(i0 + 1, Math.ceil((dvl.zoomEndPct / 100) * totalPts));

    const sliceTs = scan.timestamps.slice(i0, i1);
    const sliceBl = (scan.bl_mbps_series || []).slice(i0, i1);
    const sliceEl = (scan.el_mbps_series || []).slice(i0, i1);
    const sliceRpu = (scan.rpu_kbps_series || []).slice(i0, i1);

    // Downsampling auf max. 450 Punkte für butterweiche Darstellung
    const step = Math.max(1, Math.floor(sliceTs.length / 450));
    const labels = [];
    const blData = [];
    const elData = [];
    const rpuData = [];
    for (let i = 0; i < sliceTs.length; i += step) {
      labels.push(fmtTimeSec(sliceTs[i]));
      let maxBl = 0, maxEl = 0, maxRpu = 0;
      for (let j = i; j < Math.min(sliceTs.length, i + step); j++) {
        if (sliceBl[j] > maxBl) maxBl = sliceBl[j];
        if (sliceEl[j] > maxEl) maxEl = sliceEl[j];
        if (sliceRpu[j] > maxRpu) maxRpu = sliceRpu[j];
      }
      blData.push(Number(maxBl.toFixed(2)));
      elData.push(Number(maxEl.toFixed(2)));
      rpuData.push(Number(maxRpu.toFixed(1)));
    }

    const canvas = $("dvl-bitrate-canvas");
    if (!canvas) return;
    if (dvl.chart) {
      dvl.chart.destroy();
      dvl.chart = null;
    }

    const datasets = [
      {
        label: `BL Bitrate (Ø ${scan.bl_avg_mbps} Mbps)`,
        data: blData,
        borderColor: "#58a6ff",
        backgroundColor: "rgba(88, 166, 255, 0.16)",
        fill: true,
        borderWidth: 1.6,
        pointRadius: 0,
        tension: 0.2,
        yAxisID: "yMbps",
      },
    ];
    if (scan.el_size_bytes > 0) {
      datasets.push({
        label: `EL Bitrate (${d.enhancement_layer_type || "EL"}, Ø ${scan.el_avg_mbps} Mbps · ${scan.el_share_pct}%)`,
        data: elData,
        borderColor: "#f0883e",
        backgroundColor: "rgba(240, 136, 62, 0.22)",
        fill: true,
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.2,
        yAxisID: "yMbps",
      });
    }
    if (scan.rpu_size_bytes > 0) {
      datasets.push({
        label: `RPU (Ø ${scan.rpu_avg_kbps} Kbps)`,
        data: rpuData,
        borderColor: "#bc8cff",
        borderDash: [4, 3],
        fill: false,
        borderWidth: 1.3,
        pointRadius: 0,
        tension: 0.15,
        yAxisID: "yKbps",
      });
    }

    dvl.chart = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: "#c9d1d9" } },
        },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 12 }, grid: { color: "rgba(255,255,255,0.05)" } },
          yMbps: {
            type: "linear",
            position: "left",
            beginAtZero: true,
            title: { display: true, text: "Video (Mbps)", color: "#58a6ff" },
            ticks: { color: "#8b949e" },
            grid: { color: "rgba(255,255,255,0.06)" },
          },
          yKbps: {
            type: "linear",
            position: "right",
            beginAtZero: true,
            title: { display: true, text: "RPU (Kbps)", color: "#bc8cff" },
            ticks: { color: "#8b949e" },
            grid: { drawOnChartArea: false },
          },
        },
      },
    });
  }

  async function exportReport(fmt) {
    if (!dvl.currentAnalysis) {
      setStatus("dvl-an-status", t("Bitte zuerst eine Datei analysieren."), "warn");
      return;
    }
    dvl.prefs.exportFmt = fmt;
    savePrefs();
    const res = await apiJson("/api/dv-lab/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ analysis: dvl.currentAnalysis, format: fmt }),
    });
    if (!res.ok) return;
    const blob = new Blob([res.content], { type: res.mime || "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = res.filename || `dv_report.${fmt}`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  function exportGraphPng() {
    if (!dvl.chart) return;
    const url = dvl.chart.toBase64Image("image/png", 1.0);
    const a = document.createElement("a");
    const stem = (dvl.currentAnalysis?.filename || "dv_bitrate").replace(/\.[^.]+$/, "");
    a.href = url;
    a.download = `${stem}_bitrate_graph.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // =========================================================================
  //  2. CROSS-RELEASE DV-COPY / HYBRID-INJECTOR
  // =========================================================================
  async function checkCompatibility() {
    const targetPath = ($("dvl-copy-target-path")?.value || "").trim();
    const donorPath = ($("dvl-copy-donor-path")?.value || "").trim();
    const vaultId = dvl.activeVaultIdForCopy || "";
    if (!targetPath) {
      setStatus("dvl-copy-status", t("Bitte ein Ziel-Release angeben."), "warn");
      return;
    }
    if (!donorPath && !vaultId) {
      setStatus("dvl-copy-status", t("Bitte eine DV/HDR10+-Spenderdatei oder eine RPU aus dem Tresor wählen."), "warn");
      return;
    }
    setStatus("dvl-copy-status", t("Prüfe Kompatibilität, Intro-Szenenschnitte & L5-Crop …"), "info");
    const res = await apiJson("/api/dv-lab/compatibility", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_path: targetPath,
        donor_path: vaultId ? "" : donorPath,
        vault_id: vaultId,
        auto_sync_intro: !!$("dvl-copy-autosync")?.checked,
      }),
    });
    if (!res.ok) {
      setStatus("dvl-copy-status", res.error || t("Prüfung fehlgeschlagen."), "err");
      return;
    }
    if ($("dvl-copy-offset")) {
      $("dvl-copy-offset").value = String(res.recommended_frame_offset || 0);
    }
    const box = $("dvl-copy-compat-box");
    if (box) {
      box.style.display = "";
      const warns = (res.warnings || []).map((w) => `<li>⚠ ${esc(w)}</li>`).join("");
      box.innerHTML = `
        <div class="dvl-compat-grid">
          <div><strong>Spender:</strong> ${esc(res.donor.name)} (${esc(res.donor.origin || "")} · ${res.donor.frames} Frames @ ${res.donor.fps} FPS)</div>
          <div><strong>Ziel:</strong> ${esc(res.target.name)} (${esc(res.target.hdr_type)} · ${res.target.frames} Frames @ ${res.target.fps} FPS · ${esc(res.target.resolution)})</div>
          <div><strong>Empfohlener Intro-Offset:</strong> <code>${res.recommended_frame_offset >= 0 ? "+" : ""}${res.recommended_frame_offset} Frames</code> (Methode: ${esc(res.sync_method)}, Sicherheit: ${Math.round((res.sync_confidence || 0) * 100)}%)</div>
          <div><strong>L5 Active-Area (Ränder):</strong> <code>L:${res.target.detected_l5_offsets.left} R:${res.target.detected_l5_offsets.right} O:${res.target.detected_l5_offsets.top} U:${res.target.detected_l5_offsets.bottom}</code></div>
        </div>
        ${warns ? `<ul class="dvl-warn-list">${warns}</ul>` : `<p class="hint" style="color:var(--ok,#3fb950);margin-top:6px">✓ Releases passen zusammen – bereit zum Kopieren.</p>`}
      `;
    }
    setStatus("dvl-copy-status", "", "");
  }

  async function runHybridTransfer() {
    const targetPath = ($("dvl-copy-target-path")?.value || "").trim();
    const donorPath = ($("dvl-copy-donor-path")?.value || "").trim();
    const vaultId = dvl.activeVaultIdForCopy || "";
    if (!targetPath || (!donorPath && !vaultId)) {
      setStatus("dvl-copy-status", t("Bitte Spender (oder Tresor-RPU) und Ziel-Release auswählen."), "warn");
      return;
    }
    const rawOff = ($("dvl-copy-offset")?.value || "").trim();
    const frameOffset = rawOff !== "" ? parseInt(rawOff, 10) : null;
    const autoSync = !!$("dvl-copy-autosync")?.checked;
    const adjustL5 = !!$("dvl-copy-l5")?.checked;
    const keepTemp = !!$("dvl-copy-keep-temp")?.checked;
    const saveVault = !!$("dvl-copy-save-vault")?.checked;
    const replaceTarget = !!$("dvl-copy-replace")?.checked;

    dvl.prefs.autoSync = autoSync;
    dvl.prefs.adjustL5 = adjustL5;
    dvl.prefs.keepTemp = keepTemp;
    dvl.prefs.saveVault = saveVault;
    savePrefs();

    const btn = $("btn-dvl-copy-run");
    if (btn) btn.disabled = true;
    setStatus("dvl-copy-status", t("Übertrage Dolby Vision RPU & muxe Container …"), "info");
    try {
      const res = await apiJson("/api/dv-lab/transfer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_path: targetPath,
          donor_path: vaultId ? "" : donorPath,
          vault_id: vaultId,
          frame_offset: Number.isFinite(frameOffset) ? frameOffset : null,
          auto_sync_intro: autoSync,
          adjust_l5_crop: adjustL5,
          keep_temp_files: keepTemp,
          save_to_vault: saveVault,
          replace_target: replaceTarget,
          output_suffix: ($("dvl-copy-suffix")?.value || ".DV").trim(),
        }),
      });
      if (!res.ok) {
        setStatus("dvl-copy-status", res.error || t("DV-Übertragung fehlgeschlagen."), "err");
        return;
      }
      setStatus(
        "dvl-copy-status",
        `✓ Fertig: ${res.output_name} (${res.hdr_result}, Offset ${res.applied_frame_offset >= 0 ? "+" : ""}${res.applied_frame_offset} Frames)`,
        "ok"
      );
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // =========================================================================
  //  3. HDR10+ -> DOLBY VISION (DUAL-HDR)
  // =========================================================================
  async function runDualHdrConversion() {
    const path = ($("dvl-dual-path")?.value || "").trim();
    if (!path) {
      setStatus("dvl-dual-status", t("Bitte eine HDR10+-Datei auswählen."), "warn");
      return;
    }
    const btn = $("btn-dvl-dual-run");
    if (btn) btn.disabled = true;
    setStatus("dvl-dual-status", t("Extrahiere HDR10+, generiere DV Profil 8.1 RPU & muxe Dual-HDR …"), "info");
    try {
      const res = await apiJson("/api/dv-lab/hdr10plus-to-dv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path,
          replace_source: !!$("dvl-dual-replace")?.checked,
          save_rpu_to_vault: !!$("dvl-dual-vault")?.checked,
          keep_temp_files: !!$("dvl-dual-keep-temp")?.checked,
        }),
      });
      if (!res.ok) {
        setStatus("dvl-dual-status", res.error || t("Konvertierung fehlgeschlagen."), "err");
        return;
      }
      setStatus(
        "dvl-dual-status",
        `✓ Dual-HDR erstellt: ${res.output_name} (${res.hdr_result})`,
        "ok"
      );
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // =========================================================================
  //  4. RPU-DATENBANK (TRESOR)
  // =========================================================================
  function renderVaultSourceChecklist() {
    const box = $("dvl-vault-source-list");
    if (!box) return;
    const items = Array.from(dvl.vaultQueue.values());
    if (!items.length) {
      box.innerHTML = `<p class="hint" style="margin:2px 0">${esc(t("Noch keine Filme ausgewählt – klicke oben auf „📂 Dateien wählen …“ oder „📁 Ordner hinzufügen …“."))}</p>`;
      return;
    }
    box.innerHTML = items.map((f) => {
      const sz = f.size_human || (f.size ? fmtBytes(f.size) : "");
      return `<div class="dvl-picked-item">
        <span class="dvl-picked-path" title="${esc(f.path)}">🎬 ${esc(f.path)} ${sz ? `<small class="hint">(${esc(sz)})</small>` : ""}</span>
        <button type="button" class="btn btn-ghost btn-sm" data-vault-rm="${esc(f.path)}" title="${esc(t("Aus Auswahl entfernen"))}">✕</button>
      </div>`;
    }).join("");
    box.querySelectorAll("[data-vault-rm]").forEach((btn) => {
      btn.addEventListener("click", () => {
        dvl.vaultQueue.delete(btn.dataset.vaultRm);
        renderVaultSourceChecklist();
      });
    });
  }

  async function runVaultBatchExtract() {
    const paths = Array.from(dvl.vaultQueue.keys());
    if (!paths.length) {
      setStatus("dvl-vault-status", t("Bitte zuerst über „📂 Dateien wählen …“ mindestens einen Film hinzufügen."), "warn");
      return;
    }
    const conv81 = !!$("dvl-vault-conv81")?.checked;
    const keepOrig = !!$("dvl-vault-keep-orig")?.checked;
    dvl.prefs.vaultConv81 = conv81;
    dvl.prefs.vaultOrigRpu = keepOrig;
    savePrefs();

    const btn = $("btn-dvl-vault-extract");
    if (btn) btn.disabled = true;
    setStatus("dvl-vault-status", `${t("Extrahiere RPUs aus")} ${paths.length} ${t("Datei(en)")} …`, "info");
    try {
      const res = await apiJson("/api/dv-lab/vault/extract", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paths,
          convert_to_81: conv81,
          keep_original_rpu: keepOrig,
        }),
      });
      if (!res.ok) {
        const firstErr = (res.results || []).find((r) => !r.ok)?.error || res.error;
        setStatus("dvl-vault-status", firstErr || t("Extraktion fehlgeschlagen."), "err");
      } else {
        setStatus(
          "dvl-vault-status",
          `✓ ${res.succeeded} von ${res.processed} RPU(s) im Tresor gespeichert.`,
          "ok"
        );
        dvl.vaultQueue.clear();
        renderVaultSourceChecklist();
      }
      await loadVault();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadVault() {
    const q = ($("dvl-vault-search")?.value || "").trim();
    const res = await apiJson(`/api/dv-lab/vault?query=${encodeURIComponent(q)}`);
    dvl.vaultEntries = res.entries || [];
    if ($("dvl-vault-dir-label") && res.vault_dir) {
      $("dvl-vault-dir-label").textContent = res.vault_dir;
    }
    renderVaultTable();
  }

  function renderVaultTable() {
    const wrap = $("dvl-vault-table-wrap");
    if (!wrap) return;
    if (!dvl.vaultEntries.length) {
      wrap.innerHTML = `<p class="hint">${esc(t("Noch keine RPUs in der Datenbank gespeichert."))}</p>`;
      return;
    }
    const c = dvl.prefs.cols;
    const headers = [
      `<th>${esc(t("Titel / Quelle"))}</th>`,
      c.origin ? `<th>${esc(t("Herkunft"))}</th>` : "",
      c.profile ? `<th>${esc(t("Profil"))}</th>` : "",
      c.cm_version ? `<th>CM</th>` : "",
      c.frames ? `<th>Frames</th>` : "",
      c.fps ? `<th>FPS</th>` : "",
      c.peak_nits ? `<th>Peak L1</th>` : "",
      c.cll ? `<th>MaxCLL / FALL</th>` : "",
      c.mastering ? `<th>Mastering</th>` : "",
      c.scenes ? `<th>${esc(t("Szenen"))}</th>` : "",
      c.size ? `<th>${esc(t("Größe"))}</th>` : "",
      `<th>${esc(t("Aktionen"))}</th>`,
    ].join("");

    const rows = dvl.vaultEntries.map((it) => {
      const originBadge = it.origin === "hdr10plus_converted"
        ? `<span class="dvl-badge dvl-badge-h10p">${esc(it.origin_label || "aus HDR10+")}</span>`
        : `<span class="dvl-badge dvl-badge-ok">${esc(it.origin_label || "Dolby Vision")}</span>`;
      const profTxt = `P${it.profile || 8}${it.el_type ? " (" + it.el_type + ")" : ""}`;
      const peakTxt = it.peak_l1_nits ? `${Math.round(it.peak_l1_nits)} nits` : "—";
      const cllTxt = (it.max_cll_nits || it.max_fall_nits)
        ? `${Math.round(it.max_cll_nits || 0)} / ${Math.round(it.max_fall_nits || 0)} nits`
        : "—";
      return `<tr>
        <td><strong>${esc(it.title)}</strong><br><small class="hint">${esc(it.source_name || "")}</small></td>
        ${c.origin ? `<td>${originBadge}</td>` : ""}
        ${c.profile ? `<td>${esc(profTxt)}</td>` : ""}
        ${c.cm_version ? `<td>${esc(it.cm_version || "—")}</td>` : ""}
        ${c.frames ? `<td>${it.frames || "—"}</td>` : ""}
        ${c.fps ? `<td>${it.fps || "—"}</td>` : ""}
        ${c.peak_nits ? `<td>${esc(peakTxt)}</td>` : ""}
        ${c.cll ? `<td>${esc(cllTxt)}</td>` : ""}
        ${c.mastering ? `<td>${esc(it.rpu_mastering_display || "—")}</td>` : ""}
        ${c.scenes ? `<td>${it.scene_cuts || "—"}</td>` : ""}
        ${c.size ? `<td>${fmtBytes(it.rpu_size_bytes)}</td>` : ""}
        <td class="dvl-actions-cell">
          <button type="button" class="btn btn-primary btn-sm" data-vault-use="${esc(it.id)}" data-vault-title="${esc(it.title)}">⇄ ${esc(t("In Film packen"))}</button>
          <a class="btn btn-ghost btn-sm" href="/api/dv-lab/vault/${ encodeURIComponent(it.id) }/download" download>⬇ .bin</a>
          <button type="button" class="btn btn-ghost btn-sm" data-vault-del="${esc(it.id)}">🗑</button>
        </td>
      </tr>`;
    }).join("");

    wrap.innerHTML = `<table class="dvl-table"><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table>`;

    wrap.querySelectorAll("[data-vault-use]").forEach((btn) => {
      btn.addEventListener("click", () => {
        dvl.activeVaultIdForCopy = btn.dataset.vaultUse;
        const banner = $("dvl-copy-vault-banner");
        if (banner) {
          banner.style.display = "";
          banner.innerHTML = `🗄 <strong>Tresor-RPU aktiv:</strong> ${esc(btn.dataset.vaultTitle)} `
            + `<button type="button" class="btn btn-ghost btn-sm" id="btn-dvl-clear-vault-use">✕ Aufheben</button>`;
          $("btn-dvl-clear-vault-use")?.addEventListener("click", () => {
            dvl.activeVaultIdForCopy = "";
            banner.style.display = "none";
          });
        }
        switchSubtab("copy");
      });
    });

    wrap.querySelectorAll("[data-vault-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await apiJson(`/api/dv-lab/vault/${encodeURIComponent(btn.dataset.vaultDel)}`, { method: "DELETE" });
        loadVault();
      });
    });
  }

  function initColumnToggles() {
    document.querySelectorAll("[data-dvl-col]").forEach((cb) => {
      const key = cb.dataset.dvlCol;
      cb.checked = !!dvl.prefs.cols[key];
      cb.addEventListener("change", () => {
        dvl.prefs.cols[key] = cb.checked;
        savePrefs();
        renderVaultTable();
      });
    });
  }

  function activateVaultRpuForCopy(vaultId, title, frames, profile) {
    dvl.activeVaultIdForCopy = vaultId || "";
    const banner = $("dvl-copy-vault-banner");
    if (banner && dvl.activeVaultIdForCopy) {
      banner.style.display = "";
      banner.innerHTML = `<span>💾 <strong>${esc(t("Tresor-RPU gewählt:"))}</strong> ${esc(title || vaultId)}${profile ? ` (P${esc(profile)}${frames ? `, ${esc(frames)} Frames` : ""})` : ""}</span>
        <button type="button" class="btn btn-ghost btn-sm" id="btn-dvl-clear-vault-sel">✕ ${esc(t("Aufheben"))}</button>`;
      $("btn-dvl-clear-vault-sel")?.addEventListener("click", () => {
        dvl.activeVaultIdForCopy = "";
        banner.style.display = "none";
      });
    } else if (banner) {
      banner.style.display = "none";
    }
    switchSubtab("copy");
  }

  function dvLabInit(preloadPath, opts) {
    opts = opts || {};
    if (!dvl.initialized) {
      dvl.initialized = true;

      document.querySelectorAll("[data-dvl-subtab]").forEach((btn) => {
        btn.addEventListener("click", () => switchSubtab(btn.dataset.dvlSubtab));
      });
      document.querySelectorAll("[data-dvl-antab]").forEach((btn) => {
        btn.addEventListener("click", () => switchAnalyzerTab(btn.dataset.dvlAntab));
      });

      // Gespeicherte Einstellungen anwenden
      if ($("dvl-scan-limit")) $("dvl-scan-limit").value = dvl.prefs.scanLimit;
      if ($("dvl-copy-autosync")) $("dvl-copy-autosync").checked = dvl.prefs.autoSync;
      if ($("dvl-copy-l5")) $("dvl-copy-l5").checked = dvl.prefs.adjustL5;
      if ($("dvl-copy-keep-temp")) $("dvl-copy-keep-temp").checked = dvl.prefs.keepTemp;
      if ($("dvl-copy-save-vault")) $("dvl-copy-save-vault").checked = dvl.prefs.saveVault;
      if ($("dvl-vault-conv81")) $("dvl-vault-conv81").checked = dvl.prefs.vaultConv81;
      if ($("dvl-vault-keep-orig")) $("dvl-vault-keep-orig").checked = dvl.prefs.vaultOrigRpu;

      // Modal-Ordner-/Dateiauswahl für alle 4 Subtabs
      $("btn-dvl-an-browse")?.addEventListener("click", () => {
        pickSingleVideo("Videodatei für Layer-Inspektor wählen", "dvlAnDir", $("dvl-an-path")?.value, (rel, name) => {
          if ($("dvl-an-path")) $("dvl-an-path").value = rel;
          if ($("dvl-dual-path") && !$("dvl-dual-path").value) $("dvl-dual-path").value = rel;
          if (window.StudioSourceContext && typeof window.StudioSourceContext.set === "function") {
            window.StudioSourceContext.set({ path: rel, name });
          }
          runQuickAnalyze(rel);
        });
      });
      $("btn-dvl-an-clear")?.addEventListener("click", () => {
        if ($("dvl-an-path")) $("dvl-an-path").value = "";
        if ($("dvl-an-results")) $("dvl-an-results").style.display = "none";
        setStatus("dvl-an-status", "", "");
      });
      $("dvl-an-path")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); runQuickAnalyze(); }
      });

      $("btn-dvl-donor-browse")?.addEventListener("click", () => {
        pickSingleVideo("Spender-Film (Dolby Vision / HDR10+) wählen", "dvlDonorDir", $("dvl-copy-donor-path")?.value, (rel) => {
          if ($("dvl-copy-donor-path")) $("dvl-copy-donor-path").value = rel;
          if (dvl.activeVaultIdForCopy) {
            dvl.activeVaultIdForCopy = "";
            if ($("dvl-copy-vault-banner")) $("dvl-copy-vault-banner").style.display = "none";
          }
        });
      });
      $("btn-dvl-donor-clear")?.addEventListener("click", () => {
        if ($("dvl-copy-donor-path")) $("dvl-copy-donor-path").value = "";
      });

      $("btn-dvl-target-browse")?.addEventListener("click", () => {
        pickSingleVideo("Ziel-Release (HEVC MKV / MP4) wählen", "dvlTargetDir", $("dvl-copy-target-path")?.value, (rel) => {
          if ($("dvl-copy-target-path")) $("dvl-copy-target-path").value = rel;
        });
      });
      $("btn-dvl-target-clear")?.addEventListener("click", () => {
        if ($("dvl-copy-target-path")) $("dvl-copy-target-path").value = "";
      });

      $("btn-dvl-dual-browse")?.addEventListener("click", () => {
        pickSingleVideo("HDR10+-Videodatei für Dual-HDR wählen", "dvlDualDir", $("dvl-dual-path")?.value, (rel) => {
          if ($("dvl-dual-path")) $("dvl-dual-path").value = rel;
        });
      });
      $("btn-dvl-dual-clear")?.addEventListener("click", () => {
        if ($("dvl-dual-path")) $("dvl-dual-path").value = "";
      });

      // RPU-Tresor: Mehrfach-Dateiauswahl & Ordner-Auswahl
      $("btn-dvl-vault-pick-files")?.addEventListener("click", () => {
        if (typeof window.openFilePickerModal !== "function") return;
        window.openFilePickerModal({
          title: t("Filme für RPU-Extraktion auswählen"),
          kind: "video",
          multi: true,
          rememberKey: "dvlVaultDir",
          onPickMany: (files) => {
            (files || []).forEach((f) => {
              const rel = f.rel || f.path || f.name;
              if (rel) {
                dvl.vaultQueue.set(rel, {
                  path: rel,
                  name: f.name || rel.split("/").pop(),
                  size: f.size || 0,
                  size_human: f.size_human || "",
                });
              }
            });
            renderVaultSourceChecklist();
          },
        });
      });

      $("btn-dvl-vault-pick-folder")?.addEventListener("click", () => {
        if (typeof window.openFolderPickerModal !== "function") return;
        window.openFolderPickerModal({
          title: t("Ordner für RPU-Extraktion hinzufügen"),
          rememberKey: "dvlVaultDir",
          onPick: async (folderRel) => {
            setStatus("dvl-vault-status", t("Durchsuche Ordner nach Videodateien …"), "info");
            const videos = await collectFolderVideos(folderRel || "");
            videos.forEach((v) => dvl.vaultQueue.set(v.path, v));
            renderVaultSourceChecklist();
            setStatus(
              "dvl-vault-status",
              videos.length ? `${videos.length} ${t("Videodatei(en) aus Ordner hinzugefügt.")}` : t("Keine Videodateien im gewählten Ordner gefunden."),
              videos.length ? "ok" : "warn"
            );
          },
        });
      });

      $("btn-dvl-vault-clear-sel")?.addEventListener("click", () => {
        dvl.vaultQueue.clear();
        renderVaultSourceChecklist();
      });

      $("btn-dvl-analyze")?.addEventListener("click", () => runQuickAnalyze());
      $("btn-dvl-bitrate-scan")?.addEventListener("click", runBitrateScan);

      document.querySelectorAll("[data-dvl-export]").forEach((btn) => {
        btn.addEventListener("click", () => exportReport(btn.dataset.dvlExport));
      });
      $("btn-dvl-export-png")?.addEventListener("click", exportGraphPng);

      const zs = $("dvl-zoom-start");
      const ze = $("dvl-zoom-end");
      zs?.addEventListener("input", () => {
        clampDvlZoom("start");
        applyDvlZoom();
      });
      ze?.addEventListener("input", () => {
        clampDvlZoom("end");
        applyDvlZoom();
      });
      paintDvlZoom();
      $("btn-dvl-zoom-reset")?.addEventListener("click", () => {
        if (zs) zs.value = "0";
        if (ze) ze.value = "100";
        applyDvlZoom();
      });

      $("btn-dvl-copy-check")?.addEventListener("click", checkCompatibility);
      $("btn-dvl-copy-run")?.addEventListener("click", runHybridTransfer);
      $("btn-dvl-dual-run")?.addEventListener("click", runDualHdrConversion);

      $("btn-dvl-vault-extract")?.addEventListener("click", runVaultBatchExtract);
      $("dvl-vault-search")?.addEventListener("input", () => loadVault());

      initColumnToggles();
      renderVaultSourceChecklist();
    }

    if (opts.vaultId) {
      activateVaultRpuForCopy(opts.vaultId, opts.title, opts.frames, opts.profile);
      return;
    }

    if (opts.subtab) {
      switchSubtab(opts.subtab);
    }

    // Aktive Studio-Quelle übernehmen, falls Feld noch leer oder explizit übergeben
    const ctxPath = preloadPath || (window.StudioSourceContext && window.StudioSourceContext.get()?.path) || "";
    if (ctxPath && !ctxPath.startsWith("upload:")) {
      if (preloadPath) {
        switchSubtab(opts.subtab || "analyzer");
        if (window.StudioSourceContext && typeof window.StudioSourceContext.set === "function") {
          window.StudioSourceContext.set({ path: preloadPath, name: opts.name || preloadPath.split("/").pop() });
        }
      }
      if ($("dvl-an-path") && (!dvl.currentAnalysis || preloadPath)) {
        $("dvl-an-path").value = ctxPath;
        runQuickAnalyze(ctxPath);
      }
      if ($("dvl-dual-path") && (!$("dvl-dual-path").value || preloadPath)) {
        $("dvl-dual-path").value = ctxPath;
      }
    }
  }

  window.dvLabInit = function (preloadPath, opts) {
    if (dvl.pendingOpen) {
      const p = dvl.pendingOpen;
      dvl.pendingOpen = null;
      return dvLabInit(p.path, p.opts);
    }
    return dvLabInit(preloadPath, opts);
  };
  window.openInDvLab = function (path, opts) {
    dvl.pendingOpen = { path: path || "", opts: opts || {} };
    if (typeof window.navTo === "function") window.navTo("dvlab");
    if (dvl.pendingOpen) {
      const p = dvl.pendingOpen;
      dvl.pendingOpen = null;
      dvLabInit(p.path, p.opts);
    }
  };
})();
