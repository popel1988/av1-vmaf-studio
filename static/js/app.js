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
  };

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
      if (info.is_hdr) $("opt-tonemap").checked = false;
    } catch (e) {
      $("selected-info").innerHTML = `<span class="bad">Analyse-Fehler: ${escapeHtml(String(e))}</span>`;
    }
  }

  function chip(label, value, cls) {
    return `<div class="chip ${cls || ""}"><span class="chip-k">${label}</span><span class="chip-v">${value}</span></div>`;
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
      audio = `<div class="track-block"><div class="track-title">Audiospuren (${info.audio.length})</div>` +
        info.audio.map((a) =>
          `<div class="track-row"><span class="track-lang">${escapeHtml((a.language || "und").toUpperCase())}</span>` +
          `<span>${escapeHtml(a.codec.toUpperCase())} · ${a.channels}ch${a.layout ? " (" + escapeHtml(a.layout) + ")" : ""} · ${a.bitrate_human}</span>` +
          `${a.title ? `<span class="track-extra">${escapeHtml(a.title)}</span>` : ""}</div>`).join("") +
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

  function gatherSettings() {
    const res = $("opt-resolution").value;
    return {
      platform: $("opt-platform").value,
      codec: $("opt-codec").value,
      quality: parseInt($("opt-quality").value, 10),
      target_height: res ? parseInt(res, 10) : null,
      tonemap: $("opt-tonemap").checked,
      vmaf_check: $("opt-vmaf").checked,
      workflow: $("opt-workflow").value,
      rate_mode: $("opt-rate-mode").value,
      test_values: gatherTestValues(),
      clip_seconds: parseInt($("opt-clip").value, 10),
      generate_screenshots: $("opt-screenshots").checked,
      post_processing: $("opt-post").value,
      suffix: "_" + $("opt-codec").value,
      audio_mode: $("opt-audio-mode").value,
      audio_codec: $("opt-audio-codec").value,
      audio_bitrate: parseInt($("opt-audio-bitrate").value, 10),
      audio_channels: parseInt($("opt-audio-channels").value, 10),
      audio_normalize: $("opt-audio-normalize").checked,
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

    $("ram-pct").textContent = `${Math.round(hw.ram_percent)}%`;
    $("ram-sub").textContent = `${hw.ram_used_gb} / ${hw.ram_total_gb} GB`;
    setRing("ring-ram", hw.ram_percent);

    renderGpus(hw.gpus || []);
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
    $("total-saved").textContent = q.total_saved_human;
    const c = q.counts;
    $("cnt-wait").textContent = `${c.waiting} wartend`;
    $("cnt-run").textContent = `${c.running} aktiv`;
    $("cnt-done").textContent = `${c.done} fertig`;
    $("cnt-fail").textContent = `${c.failed} fehlgeschlagen`;
    if ($("cnt-await")) $("cnt-await").textContent = `${c.awaiting || 0} Auswahl`;

    $("global-status").textContent = q.status_message || (c.running ? "Verarbeitung läuft" : "Bereit");

    const activeIds = q.active_ids || (q.active_id ? [q.active_id] : []);
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

  function settingsLabel(it) {
    const s = it.settings;
    if (s.rate_mode === "bitrate") return `${s.quality} kbit/s`;
    if (s.rate_mode === "abr") return `ABR ${s.quality}`;
    return s.quality;
  }

  function renderQueueTable(items, activeIds) {
    const body = $("queue-body");
    const active = new Set(activeIds || []);
    if (!items.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">Warteschlange ist leer.</td></tr>';
      return;
    }
    body.innerHTML = items.map((it) => {
      const reso = it.info ? it.info.resolution : "—";
      const canCancel = ["wartend", "auswahl"].includes(it.status) || active.has(it.id);
      const cancelBtn = canCancel
        ? `<button class="btn btn-ghost btn-sm" data-cancel="${it.id}">Abbrechen</button>` : "";
      const err = it.error ? `<div class="muted" style="font-size:11px">${escapeHtml(it.error.slice(0, 80))}</div>` : "";
      return `<tr>
        <td>${escapeHtml(it.title)}${err}</td>
        <td>${reso}</td>
        <td class="status-cell">${statusBadge(it.status)}</td>
        <td>${settingsLabel(it)}</td>
        <td class="good">${it.saved_human}</td>
        <td>${cancelBtn}</td>
      </tr>`;
    }).join("");
    body.querySelectorAll("[data-cancel]").forEach((b) => {
      b.addEventListener("click", () =>
        fetch(`/api/queue/${b.dataset.cancel}/cancel`, { method: "POST" }));
    });
  }

  function renderActiveProgress(items, activeIds) {
    const card = $("progress-card");
    const list = $("progress-list");
    const active = new Set(activeIds || []);
    const jobs = items.filter((i) => active.has(i.id));
    if (!jobs.length) {
      card.style.display = "none";
      list.innerHTML = "";
      return;
    }
    card.style.display = "";
    $("progress-count").textContent =
      jobs.length === 1 ? "1 Encode" : `${jobs.length} Encodes parallel`;
    list.innerHTML = jobs.map(progressBlock).join("");
  }

  function progressBlock(job) {
    const analyzing = job.status === "vmaf-test";
    const p = job.progress || {};
    const pct = analyzing ? 100 : (p.percent || 0);
    const stage = analyzing
      ? (job.message || "VMAF-Test läuft …")
      : (job.message || "Encode");
    const stats = analyzing ? "" : `
      <div class="stat-grid">
        <div class="stat"><span class="stat-label">Geschwindigkeit</span><span class="stat-val">${p.fps || 0} fps</span></div>
        <div class="stat"><span class="stat-label">Bitrate</span><span class="stat-val">${p.bitrate || "—"}</span></div>
        <div class="stat"><span class="stat-label">ETA</span><span class="stat-val">${p.eta_human || "—"}</span></div>
        <div class="stat"><span class="stat-label">Aktuelle Größe</span><span class="stat-val">${p.current_human || "—"}</span></div>
        <div class="stat"><span class="stat-label">Eingespart</span><span class="stat-val good">${p.saved_human || "—"}</span></div>
        <div class="stat"><span class="stat-label">Speed</span><span class="stat-val">${p.speed || "—"}</span></div>
      </div>`;
    return `
      <div class="job-progress ${analyzing ? "analyzing" : ""}">
        <div class="job-progress-head">
          <span class="job-progress-title">${escapeHtml(job.title)}</span>
          <span class="job-progress-stage">${escapeHtml(stage)}</span>
        </div>
        <div class="big-progress">
          <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
          <div class="bar-pct">${analyzing ? "…" : Math.round(pct) + "%"}</div>
        </div>
        ${stats}
      </div>`;
  }

  /* ----------------------------------------------------------- VMAF CHART */
  function renderVmaf(items, activeId) {
    let target = items.find((i) => i.id === activeId && i.vmaf);
    if (!target) target = [...items].reverse().find((i) => i.vmaf && i.vmaf.results && i.vmaf.results.length);
    const awaiting = items.find((i) => i.status === "auswahl" && i.vmaf);
    if (awaiting) target = awaiting;

    const card = $("vmaf-card");
    const actions = $("vmaf-actions");
    if (!target || !target.vmaf || !target.vmaf.results.length) {
      card.style.display = "none";
      if (actions) actions.style.display = "none";
      return;
    }

    const vmaf = target.vmaf;
    const key = target.id + ":" + vmaf.results.length + ":" + vmaf.recommended_quality + ":" + target.status;
    card.style.display = "";
    $("vmaf-model-badge").textContent = `Modell: ${vmaf.model} · Clip: ${vmaf.clip_seconds || 30}s`;

    drawChart(vmaf);
    fillVmafTable(vmaf);
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
  }

  function renderScreenshots(vmaf) {
    const grid = $("vmaf-screenshots");
    if (!grid) return;
    const withShots = vmaf.results.filter((r) => r.screenshot_ref && r.screenshot_enc);
    if (!withShots.length) { grid.innerHTML = ""; return; }

    grid.innerHTML = withShots.map((r) => `
      <div class="screenshot-card ${r.recommended ? "recommended" : ""}">
        <div class="screenshot-head">
          <span>${escapeHtml(r.label || ("Q" + r.quality))}</span>
          <span>VMAF ${r.vmaf.toFixed(1)}</span>
        </div>
        <div class="screenshot-pair">
          <div><div class="screenshot-cap">Original</div>
            <img src="${r.screenshot_ref}" alt="Referenz" loading="lazy" /></div>
          <div><div class="screenshot-cap">Encode</div>
            <img src="${r.screenshot_enc}" alt="Encode" loading="lazy" /></div>
        </div>
      </div>`).join("");
  }

  function fillVmafTable(vmaf) {
    const body = $("vmaf-table").querySelector("tbody");
    body.innerHTML = vmaf.results.map((r) => `
      <tr class="${r.recommended ? "row-recommended" : ""}">
        <td>${escapeHtml(r.label || ("Q" + r.quality))}</td>
        <td>${r.vmaf.toFixed(2)}</td>
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

  function drawChart(vmaf) {
    if (typeof Chart === "undefined") return;
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

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initSettings();
    initDataBrowser();
    initParallel();
    loadDir("");
    connectWs();
    fetch("/api/config/paths").then((r) => r.json()).then((p) => {
      const el = $("data-dir");
      if (el) el.textContent = p.data_dir;
    }).catch(() => {});
  });
})();
