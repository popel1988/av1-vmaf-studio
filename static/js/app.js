/* AV1 / VMAF Compression Studio – Frontend-Logik */
(() => {
  "use strict";

  const RING_CIRC = 2 * Math.PI * 52; // ~327
  const $ = (id) => document.getElementById(id);

  const state = {
    currentPath: "",
    selected: null, // { path, name, isBatch }
    vmafChart: null,
    lastVmafKey: null,
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

    const vmaf = $("opt-vmaf");
    const manual = $("manual-quality");
    const syncManual = () => manual.classList.toggle("disabled", vmaf.checked);
    vmaf.addEventListener("change", syncManual);
    syncManual();

    $("btn-enqueue").addEventListener("click", enqueue);
    $("btn-clear").addEventListener("click", async () => {
      await fetch("/api/queue/clear", { method: "POST" });
    });
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
      post_processing: $("opt-post").value,
      suffix: "_" + $("opt-codec").value,
    };
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

    $("global-status").textContent = q.status_message || (c.running ? "Verarbeitung läuft" : "Bereit");

    renderQueueTable(q.items, q.active_id);
    renderActiveProgress(q.items, q.active_id);
    renderVmaf(q.items, q.active_id);
  }

  function statusBadge(status) {
    const map = {
      "wartend": "badge-wait", "vmaf-test": "badge-run", "in arbeit": "badge-run",
      "fertig": "badge-done", "fehlgeschlagen": "badge-fail", "abgebrochen": "badge-fail",
    };
    return `<span class="badge ${map[status] || ""}">${status}</span>`;
  }

  function renderQueueTable(items, activeId) {
    const body = $("queue-body");
    if (!items.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">Warteschlange ist leer.</td></tr>';
      return;
    }
    body.innerHTML = items.map((it) => {
      const reso = it.info ? it.info.resolution : "—";
      const canCancel = it.status === "wartend" || it.id === activeId;
      const cancelBtn = canCancel
        ? `<button class="btn btn-ghost btn-sm" data-cancel="${it.id}">Abbrechen</button>` : "";
      const err = it.error ? `<div class="muted" style="font-size:11px">${escapeHtml(it.error.slice(0, 80))}</div>` : "";
      return `<tr>
        <td>${escapeHtml(it.title)}${err}</td>
        <td>${reso}</td>
        <td class="status-cell">${statusBadge(it.status)}</td>
        <td>${it.settings.quality}</td>
        <td class="good">${it.saved_human}</td>
        <td>${cancelBtn}</td>
      </tr>`;
    }).join("");
    body.querySelectorAll("[data-cancel]").forEach((b) => {
      b.addEventListener("click", () =>
        fetch(`/api/queue/${b.dataset.cancel}/cancel`, { method: "POST" }));
    });
  }

  function renderActiveProgress(items, activeId) {
    const card = $("progress-card");
    const active = items.find((i) => i.id === activeId);
    if (!active || !active.progress || active.status === "vmaf-test") {
      if (active && active.status === "vmaf-test") {
        card.style.display = "";
        $("progress-title").textContent = `VMAF-Test: ${active.title}`;
        $("bar-fill").style.width = "100%";
        $("bar-pct").textContent = "…";
        $("progress-msg").textContent = "Test-Encodes & VMAF-Vergleich laufen …";
        return;
      }
      card.style.display = "none";
      return;
    }
    const p = active.progress;
    card.style.display = "";
    $("progress-title").textContent = active.title;
    $("bar-fill").style.width = `${p.percent || 0}%`;
    $("bar-pct").textContent = `${Math.round(p.percent || 0)}%`;
    $("st-fps").textContent = `${p.fps || 0} fps`;
    $("st-bitrate").textContent = p.bitrate || "—";
    $("st-eta").textContent = p.eta_human || "—";
    $("st-size").textContent = p.current_human || "—";
    $("st-saved").textContent = p.saved_human || "—";
    $("st-speed").textContent = p.speed || "—";
  }

  /* ----------------------------------------------------------- VMAF CHART */
  function renderVmaf(items, activeId) {
    // Zeige VMAF des aktiven Items, sonst des letzten mit Ergebnis.
    let target = items.find((i) => i.id === activeId && i.vmaf);
    if (!target) target = [...items].reverse().find((i) => i.vmaf && i.vmaf.results.length);
    const card = $("vmaf-card");
    if (!target || !target.vmaf || !target.vmaf.results.length) {
      card.style.display = "none";
      return;
    }
    const key = target.id + ":" + target.vmaf.results.length + ":" + target.vmaf.recommended_quality;
    card.style.display = "";
    $("vmaf-model-badge").textContent = "Modell: " + target.vmaf.model;
    if (key === state.lastVmafKey) return;
    state.lastVmafKey = key;
    drawChart(target.vmaf);
    fillVmafTable(target.vmaf);
  }

  function fillVmafTable(vmaf) {
    const body = $("vmaf-table").querySelector("tbody");
    body.innerHTML = vmaf.results.map((r) => `
      <tr class="${r.recommended ? "row-recommended" : ""}">
        <td>${r.quality}</td>
        <td>${r.vmaf.toFixed(2)}</td>
        <td>${r.predicted_human}</td>
        <td class="good">${r.savings_percent}%</td>
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
    const labels = vmaf.results.map((r) => "Q" + r.quality);
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

  /* ----------------------------------------------------------------- UTIL */
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ------------------------------------------------------------------ INIT */
  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initSettings();
    loadDir("");
    connectWs();
  });
})();
