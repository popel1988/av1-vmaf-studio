/* Video-Editor: Timeline, Mehrfachquellen, Tastatur, HLS-Vorschau, Export → Queue */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const tt = (s) => (window.I18N && window.I18N.t ? window.I18N.t(s) : s);
  const PAGE = () => document.querySelector('[data-page="editor"]');

  const ed = {
    loaded: false,
    src: null,
    inSec: 0,
    outSec: 0,
    playhead: 0,
    segments: [],
    playTl: null,
    streamUrl: "",
    uploadDest: "upload",
    hist: [],
    histPos: -1,
    hls: null,
    sid: null,
    previewOffset: 0,
    previewMode: "none",
    keyframes: [],
    rates: [1, 1.5, 2, 4],
    rateIdx: 0,
    dragId: null,
    activeId: null,
    overlays: [],      // V2 B-Roll / PiP Overlays ({id, path, name, title, start, end, tl_start, position, size_pct, opacity, fade})
    activeV2Id: null,
    tlZoom: 1,
    cropBox: null,     // {x, y, w, h} in Quellpixeln für den visuellen Crop-Rahmen
    cropRatio: "free",
    loopRange: false,
    bgmPath: "",
    v2Url: "",
    assets: {},        // Pfad → { strip, duration } für Timeline-Vorschaubilder
    trackSel: {},      // Pfad → gewählte Tonspur-Indizes (für neue Clips)
    subSel: {},        // Pfad → gewählte Untertitel-Indizes
    inClip: false,     // Wiedergabe ist im aktiven Clip angekommen
    advancing: false,  // Clipwechsel läuft – Follow-Logik pausieren
    lastAdvance: 0,
    followTimer: null,
  };

  const TL_COLORS = ["#22d3ee", "#38bdf8", "#34d399", "#fbbf24", "#f87171", "#a78bfa"];

  function fmt(sec) {
    sec = Math.max(0, Number(sec) || 0);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const ms = Math.floor((sec % 1) * 100);
    const core = h > 0
      ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
      : `${m}:${String(s).padStart(2, "0")}`;
    return ms ? `${core}.${String(ms).padStart(2, "0")}` : core;
  }

  function round2(n) {
    return Math.round(Number(n) * 100) / 100;
  }

  /** Akzeptiert Sekunden ("90.5") oder Zeitformat ("1:30.5" / "01:02:03.40"). */
  function parseTimeStr(val, fallback) {
    if (val == null) return fallback || 0;
    const raw = String(val).trim().replace(",", ".");
    if (!raw) return fallback || 0;
    if (/^\d+(\.\d+)?$/.test(raw)) return Math.max(0, Number(raw));
    const parts = raw.split(":").map((p) => Number(p.trim()));
    if (parts.some((n) => !Number.isFinite(n) || n < 0)) return fallback || 0;
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return fallback || 0;
  }

  function syncMarkLabels() {
    const inEl = $("ed-in");
    const outEl = $("ed-out");
    const inFmt = $("ed-in-fmt");
    const outFmt = $("ed-out-fmt");
    if (inEl && inFmt) {
      const s = parseTimeStr(inEl.value, ed.inSec);
      inFmt.textContent = `(${fmt(s)})`;
    }
    if (outEl && outFmt) {
      const s = parseTimeStr(outEl.value, ed.outSec);
      outFmt.textContent = `(${fmt(s)})`;
    }
    updateKfDeltaBar();
  }

  function clipDur(s) {
    const raw = Math.max(0, s.end - s.start);
    const sp = Number(s.speed) > 0 ? Number(s.speed) : 1;
    return raw / sp;
  }

  function totalDur() {
    const xf = Math.max(0, Number(($("ed-crossfade") || {}).value) || 0);
    let acc = 0;
    ed.segments.forEach((s, i) => {
      acc += clipDur(s);
      if (i > 0 && xf > 0) acc -= Math.min(xf, clipDur(s) * 0.45, clipDur(ed.segments[i - 1]) * 0.45);
    });
    return Math.max(0, acc);
  }

  function uid() {
    return Math.random().toString(36).slice(2, 9);
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function setStatus(msg, isErr) {
    const el = $("ed-status");
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = isErr ? "var(--bad)" : "";
  }

  function isEditorVisible() {
    const p = PAGE();
    if (!p) return false;
    return p.style.display !== "none" && !p.hidden;
  }

  function detectClientCodecs() {
    const v = document.createElement("video");
    const out = ["h264"];
    if (v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"')
        || v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"')) out.push("hevc");
    if (v.canPlayType('video/mp4; codecs="av01.0.05M.08"')) out.push("av1");
    return out;
  }

  function clientDirectOk(info) {
    if (!info) return false;
    const v = document.createElement("video");
    const fmt = String(info.container || "").toLowerCase();
    const isMp4 = /\b(mp4|mov|m4v|isom)\b/.test(fmt);
    const isWebm = /\bwebm\b/.test(fmt) && !/\bmatroska\b/.test(fmt);
    if (!isMp4 && !isWebm) return false;
    const vc = (info.codec || "").toLowerCase();
    let mime = "";
    if (isMp4 && (/^(h264|avc)/.test(vc) || vc === "avc1")) mime = 'video/mp4; codecs="avc1.640028"';
    else if (isMp4 && /^(h265|hevc)/.test(vc)) mime = 'video/mp4; codecs="hvc1.1.6.L93.B0"';
    else if (/^(av1|av01)/.test(vc)) mime = isWebm ? 'video/webm; codecs="av01.0.05M.08"' : 'video/mp4; codecs="av01.0.05M.08"';
    else if (isWebm && /^vp9/.test(vc)) mime = 'video/webm; codecs="vp9"';
    else return false;
    if (!v.canPlayType(mime)) return false;
    const a = (info.audio || [])[0];
    if (!a) return true;
    const ac = (a.codec || "").toLowerCase();
    return /^(aac|mp3|mp4a)/.test(ac) || (ac === "opus" && !!v.canPlayType('audio/webm; codecs="opus"'));
  }

  function syncBadge() {
    const b = $("ed-badge");
    const n = ed.segments.length;
    const nv2 = ed.overlays.length;
    const v2Txt = nv2 ? ` + ${nv2} V2` : "";
    if (b) b.textContent = n ? `${n} Clip(s)${v2Txt} · ${fmt(totalDur())}` : tt("Keine Clips");
    const tot = $("ed-total");
    if (tot) tot.textContent = `${n} Clips${v2Txt} · ${fmt(totalDur())}`;
    ["ed-enqueue", "ed-play-tl", "ed-clear", "ed-preview-cut"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = n === 0;
    });
    const dl = $("ed-direct-dl");
    if (dl) dl.disabled = !ed.src && n === 0;
    const un = $("ed-undo");
    const re = $("ed-redo");
    if (un) un.disabled = ed.histPos < 1;
    if (re) re.disabled = ed.histPos >= ed.hist.length - 1;
    syncProgramMonitorInfo();
    updateSizeEstimate();
  }

  function updateSizeEstimate() {
    const el = $("ed-size-est");
    if (!el) return;
    if (!ed.segments.length) {
      el.textContent = "";
      return;
    }
    const mode = ($("ed-mode") && $("ed-mode").value) || "remux";
    let totalBytes = 0;
    ed.segments.forEach((s) => {
      const d = clipDur(s);
      if (mode === "encode") {
        const rm = ($("ed-rate-mode") && $("ed-rate-mode").value) || "cq";
        const vbr = rm === "cq" ? 4500 : (Number(($("ed-vbr") || {}).value) || 6000);
        const abr = Number(($("ed-abr") || {}).value) || 192;
        totalBytes += ((vbr + abr) * 1000 / 8) * d;
      } else {
        const a = s.path ? ed.assets[s.path] : null;
        const bps = (a && a.overall_bitrate) || (ed.src && ed.src.path === s.path && ed.src.overall_bitrate) || 12000000;
        totalBytes += (bps / 8) * (s.end - s.start);
      }
    });
    if (totalBytes <= 0) {
      el.textContent = "";
      return;
    }
    const mb = totalBytes / (1024 * 1024);
    el.textContent = mb >= 1024
      ? `≈ ${(mb / 1024).toFixed(2)} GB`
      : `≈ ${Math.max(1, Math.round(mb))} MB`;
  }

  function syncProgramMonitorInfo(srcTime) {
    const pInfo = $("ed-prog-info");
    const pv = $("ed-prog-video");
    const s = activeClip();
    if (!ed.segments.length) {
      if (pInfo) pInfo.textContent = tt("Noch keine Clips auf der Timeline.");
      if (pv && pv.currentSrc) {
        pv.pause();
        pv.removeAttribute("src");
        try { pv.load(); } catch (e) { /* ignore */ }
      }
      return;
    }
    const cur = s || ed.segments[0];
    const idx = ed.segments.indexOf(cur);
    if (pInfo) {
      pInfo.textContent = `Clip ${idx + 1}/${ed.segments.length}: ${clipLabel(cur)} · ${fmt(cur.start)} → ${fmt(cur.end)} (${fmt(clipDur(cur))}) · Gesamt ${fmt(totalDur())}`;
    }
    if (!pv || cur.kind !== "media" || !cur.path) return;
    const url = mediaUrl(cur.path, 0);
    if (pv.dataset.clipPath !== cur.path) {
      pv.dataset.clipPath = cur.path;
      pv.src = fileUrl(cur.path);
      pv.load();
    }
    if (srcTime != null && pv.readyState >= 1) {
      try { pv.currentTime = Math.max(0, Number(srcTime) || cur.start); } catch (e) { /* ignore */ }
    }
  }

  function snapshot() {
    return JSON.parse(JSON.stringify({
      segments: ed.segments,
      overlays: ed.overlays,
    }));
  }

  function pushHist() {
    ed.hist = ed.hist.slice(0, ed.histPos + 1);
    ed.hist.push(snapshot());
    if (ed.hist.length > 80) ed.hist.shift();
    ed.histPos = ed.hist.length - 1;
    syncBadge();
    saveAutoSession();
  }

  function saveAutoSession() {
    try {
      if (!ed.segments.length && !ed.overlays.length && !ed.src) return;
      localStorage.setItem("edAutoSaveV2", JSON.stringify({
        ts: Date.now(),
        segments: ed.segments,
        overlays: ed.overlays,
        src: ed.src && { path: ed.src.path, name: ed.src.name },
      }));
    } catch (e) { /* ignore */ }
  }

  function checkAutoSession() {
    const btn = $("ed-restore-autosave");
    if (!btn) return;
    try {
      const raw = JSON.parse(localStorage.getItem("edAutoSaveV2") || "null");
      if (raw && Array.isArray(raw.segments) && raw.segments.length > 0) {
        btn.style.display = "";
      }
    } catch (e) { /* ignore */ }
  }

  async function restoreAutoSession() {
    try {
      const raw = JSON.parse(localStorage.getItem("edAutoSaveV2") || "null");
      if (!raw || !Array.isArray(raw.segments) || !raw.segments.length) return;
      pushHist();
      ed.segments = raw.segments.map((s) => defaultClip(s));
      ed.overlays = Array.isArray(raw.overlays) ? raw.overlays.map((o) => defaultOverlay(o)) : [];
      if (ed.segments[0]) ed.activeId = ed.segments[0].id;
      renderSegList();
      if (raw.src && raw.src.path) await loadSource(raw.src.path, raw.src.name);
      const btn = $("ed-restore-autosave");
      if (btn) btn.style.display = "none";
      setStatus(`${ed.segments.length} ${tt("Clips aus letzter Sitzung wiederhergestellt.")}`);
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  function applyHist(i) {
    if (i < 0 || i >= ed.hist.length) return;
    ed.histPos = i;
    const snap = JSON.parse(JSON.stringify(ed.hist[i]));
    if (Array.isArray(snap)) {
      ed.segments = snap;
      ed.overlays = [];
    } else {
      ed.segments = snap.segments || [];
      ed.overlays = snap.overlays || [];
    }
    renderSegList();
  }

  function undo() {
    if (ed.histPos < 1) return;
    applyHist(ed.histPos - 1);
    setStatus(tt("Rückgängig"));
  }

  function redo() {
    if (ed.histPos >= ed.hist.length - 1) return;
    applyHist(ed.histPos + 1);
    setStatus(tt("Wiederholt"));
  }

  function readMarks() {
    const inEl = $("ed-in");
    const outEl = $("ed-out");
    return {
      start: inEl ? parseTimeStr(inEl.value, ed.inSec) : ed.inSec,
      end: outEl ? parseTimeStr(outEl.value, ed.outSec) : ed.outSec,
    };
  }

  function audioOpts() {
    const burn = !!($("ed-burn") && $("ed-burn").checked);
    const tracks = currentTrackSel();
    const subs = currentSubSel();
    return {
      audio_indexes: tracks.slice(),
      audio_index: tracks.length ? tracks[0] : -1,
      mute: tracks.length === 0,
      sub_indexes: subs.slice(),
      sub_index: subs.length ? subs[0] : -1,
      burn_subs: burn && subs.length > 0,
    };
  }

  /** Tonspuren der geladenen Quelle, die in den Export sollen. */
  function currentTrackSel() {
    const path = ed.src && ed.src.path;
    if (!path) return [];
    const total = (ed.src.audio || []).length;
    if (!total) return [];
    const clip = ed.segments.find((s) => s.kind === "media" && s.path === path);
    if (clip) {
      if (clip.mute) return [];
      const list = (clip.audio_indexes || []).filter((i) => i >= 0 && i < total);
      if (list.length) return list;
      const one = Number(clip.audio_index);
      return one >= 0 && one < total ? [one] : [];
    }
    if (ed.trackSel[path]) return ed.trackSel[path].filter((i) => i < total);
    // Vorgabe: alles behalten – wie beim Remux ohne Auswahl.
    return ed.src.audio.map((_, i) => i);
  }

  function currentSubSel() {
    const path = ed.src && ed.src.path;
    if (!path) return [];
    const total = (ed.src.subtitles || []).length;
    if (!total) return [];
    const clip = ed.segments.find((s) => s.kind === "media" && s.path === path);
    if (clip) {
      if (Array.isArray(clip.sub_indexes)) {
        return clip.sub_indexes.filter((i) => i >= 0 && i < total);
      }
      const one = Number(clip.sub_index);
      return one >= 0 && one < total ? [one] : [];
    }
    if (ed.subSel[path]) return ed.subSel[path].filter((i) => i < total);
    return ed.src.subtitles.map((_, i) => i);
  }

  function subLabel(s, i) {
    const bits = [s.language || "und", s.codec || ""];
    if (s.title) bits.push(s.title);
    if (s.forced) bits.push(tt("forced"));
    return `#${i} · ${bits.filter(Boolean).join(" · ")}`;
  }

  function trackLabel(a, i) {
    const br = a.bitrate_human && a.bitrate_human !== "—" ? a.bitrate_human : "";
    const bits = [a.language || "und", a.codec || "",
      a.channels ? `${a.channels}ch` : "", br];
    if (a.title) bits.push(a.title);
    return `#${i} · ${bits.filter(Boolean).join(" · ")}`;
  }

  function renderTracks() {
    const list = $("ed-tracks-list");
    const info = $("ed-tracks-info");
    if (!list) return;
    list.innerHTML = "";
    const tracks = (ed.src && ed.src.audio) || [];
    if (!tracks.length) {
      const p = document.createElement("div");
      p.className = "ed-tracks-empty";
      p.textContent = ed.src ? tt("Diese Quelle hat keine Tonspur.") : tt("Keine Quelle geladen.");
      list.appendChild(p);
      if (info) info.textContent = "";
      return;
    }
    const sel = currentTrackSel();
    tracks.forEach((a, i) => {
      const row = document.createElement("div");
      row.className = "ed-track-row" + (sel.indexOf(i) < 0 ? " off" : "");
      const lab = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = sel.indexOf(i) >= 0;
      cb.addEventListener("change", () => {
        const next = currentTrackSel().filter((x) => x !== i);
        if (cb.checked) next.push(i);
        next.sort((x, y) => x - y);
        applyTrackSelection(next);
      });
      const txt = document.createElement("span");
      txt.textContent = trackLabel(a, i);
      lab.appendChild(cb);
      lab.appendChild(txt);
      row.appendChild(lab);
      const listen = document.createElement("button");
      listen.type = "button";
      listen.className = "btn btn-ghost btn-sm ed-track-listen";
      listen.textContent = previewAudioIndex() === i ? "🔊" : "🎧";
      listen.title = tt("Diese Spur in der Vorschau hören");
      listen.addEventListener("click", () => setPreviewAudio(i));
      row.appendChild(listen);
      list.appendChild(row);
    });
    if (info) {
      info.textContent = sel.length
        ? `${sel.length}/${tracks.length} ${tt("Spuren")}`
        : tt("stumm");
    }
    renderSubs();
  }

  function renderSubs() {
    const list = $("ed-subs-list");
    const info = $("ed-subs-info");
    if (!list) return;
    list.innerHTML = "";
    const tracks = (ed.src && ed.src.subtitles) || [];
    if (!tracks.length) {
      const p = document.createElement("div");
      p.className = "ed-tracks-empty";
      p.textContent = ed.src
        ? tt("Diese Quelle hat keine Untertitel.")
        : tt("Keine Quelle geladen.");
      list.appendChild(p);
      if (info) info.textContent = "";
      return;
    }
    const sel = currentSubSel();
    tracks.forEach((s, i) => {
      const row = document.createElement("div");
      row.className = "ed-track-row" + (sel.indexOf(i) < 0 ? " off" : "");
      const lab = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = sel.indexOf(i) >= 0;
      cb.addEventListener("change", () => {
        const next = currentSubSel().filter((x) => x !== i);
        if (cb.checked) next.push(i);
        next.sort((x, y) => x - y);
        applySubSelection(next);
      });
      const txt = document.createElement("span");
      txt.textContent = subLabel(s, i);
      lab.appendChild(cb);
      lab.appendChild(txt);
      row.appendChild(lab);
      list.appendChild(row);
    });
    if (info) {
      info.textContent = sel.length
        ? `${sel.length}/${tracks.length}`
        : tt("keine");
    }
  }

  /** Auswahl auf alle Clips dieser Quelle anwenden (und für neue merken). */
  function applyTrackSelection(listSel) {
    const path = ed.src && ed.src.path;
    if (!path) return;
    ed.trackSel[path] = listSel.slice();
    const mine = ed.segments.filter((s) => s.kind === "media" && s.path === path);
    if (mine.length) {
      pushHist();
      mine.forEach((s) => {
        s.audio_indexes = listSel.slice();
        s.audio_index = listSel.length ? listSel[0] : -1;
        s.mute = listSel.length === 0;
      });
      renderSegList();
    }
    renderTracks();
  }

  function applySubSelection(listSel) {
    const path = ed.src && ed.src.path;
    if (!path) return;
    ed.subSel[path] = listSel.slice();
    const mine = ed.segments.filter((s) => s.kind === "media" && s.path === path);
    if (mine.length) {
      pushHist();
      mine.forEach((s) => {
        s.sub_indexes = listSel.slice();
        s.sub_index = listSel.length ? listSel[0] : -1;
      });
      renderSegList();
    }
    renderTracks();
  }

  function setPreviewAudio(idx) {
    const sel = $("ed-preview-audio");
    if (sel) sel.value = String(idx);
    renderTracks();
    seekPreview(currentPreviewTime(), { snap: false });
  }

  function defaultClip(extra) {
    const clip = Object.assign({
      id: uid(),
      kind: "media",
      path: "",
      name: "",
      start: 0,
      end: 3,
      title: "",
      audio_index: 0,
      audio_indexes: [0],
      mute: false,
      sub_index: -1,
      sub_indexes: [],
      burn_subs: false,
      fade_in: 0,
      fade_out: 0,
      speed: 1,
      crop: "",
      delogo: "",
      text_overlay: "",
      text_pos: "bottom",
      look_preset: "none",
      voice_enhance: false,
      reverse: false,
      scale: 0,
      rotate: 0,
      hflip: false,
      vflip: false,
      volume_db: 0,
      loudnorm: false,
    }, extra || {});
    // Projekte/Vorlagen von früher kennen nur eine einzelne Tonspur.
    if (!Array.isArray(clip.audio_indexes)) {
      const one = Number(clip.audio_index);
      clip.audio_indexes = clip.mute || !(one >= 0) ? [] : [one];
    }
    if (!Array.isArray(clip.sub_indexes)) {
      const one = Number(clip.sub_index);
      clip.sub_indexes = one >= 0 ? [one] : [];
    }
    if (!clip.audio_indexes.length && clip.kind === "media") clip.mute = true;
    return clip;
  }

  function defaultOverlay(extra) {
    return Object.assign({
      id: uid(),
      path: "",
      name: "",
      title: "B-Roll",
      start: 0,
      end: 5,
      tl_start: 0,
      position: "pip_br",
      size_pct: 30,
      opacity: 1,
      fade: 0.3,
    }, extra || {});
  }

  function snapTime(t) {
    const dur = (ed.src && ed.src.duration) || 0;
    let x = Math.max(0, Math.min(dur || t, Number(t) || 0));
    const pts = [];
    if ($("ed-snap-ch") && $("ed-snap-ch").checked && ed.src && ed.src.chapters) {
      ed.src.chapters.forEach((c) => pts.push(Number(c.start) || 0));
    }
    if ($("ed-snap-kf") && $("ed-snap-kf").checked) {
      ed.keyframes.forEach((k) => pts.push(k));
    }
    if (!pts.length) return round2(x);
    let best = x;
    let bestD = 0.4;
    pts.forEach((p) => {
      const d = Math.abs(p - x);
      if (d < bestD) { bestD = d; best = p; }
    });
    return round2(best);
  }

  function keepsForSource(path) {
    const raw = ed.segments
      .filter((s) => s.kind !== "black" && s.path === path)
      .map((s) => ({ start: s.start, end: s.end }))
      .filter((r) => r.end > r.start)
      .sort((a, b) => a.start - b.start);
    if (!raw.length) return [];
    const out = [Object.assign({}, raw[0])];
    for (let i = 1; i < raw.length; i++) {
      const last = out[out.length - 1];
      if (raw[i].start <= last.end + 0.001) last.end = Math.max(last.end, raw[i].end);
      else out.push(Object.assign({}, raw[i]));
    }
    return out;
  }

  function subtractRange(keeps, cutStart, cutEnd) {
    const cs = Math.min(cutStart, cutEnd);
    const ce = Math.max(cutStart, cutEnd);
    const out = [];
    keeps.forEach((k) => {
      if (ce <= k.start || cs >= k.end) {
        out.push({ start: k.start, end: k.end });
        return;
      }
      if (cs > k.start) out.push({ start: k.start, end: Math.min(cs, k.end) });
      if (ce < k.end) out.push({ start: Math.max(ce, k.start), end: k.end });
    });
    return out.filter((r) => r.end - r.start > 0.05);
  }

  function replaceSourceSegments(path, name, ranges) {
    const opts = audioOpts();
    const mine = (s) => s.path === path && s.kind !== "black";
    // Die neuen Clips bleiben dort, wo die Quelle schon in der Timeline lag.
    const firstIdx = ed.segments.findIndex(mine);
    const insertAt = firstIdx < 0
      ? Infinity
      : ed.segments.slice(0, firstIdx).filter((s) => !mine(s)).length;
    const others = ed.segments.filter((s) => !mine(s));
    const neu = ranges.map((r, i) => defaultClip({
      path, name,
      start: round2(r.start), end: round2(r.end),
      title: `Clip ${i + 1}`,
      audio_index: opts.audio_index, audio_indexes: opts.audio_indexes.slice(),
      mute: opts.mute,
      sub_index: opts.sub_index, sub_indexes: (opts.sub_indexes || []).slice(),
      burn_subs: opts.burn_subs,
    }));
    others.splice(Math.min(insertAt, others.length), 0, ...neu);
    ed.segments = others;
    return neu;
  }

  function clipsNeedEncode() {
    const xf = Number(($("ed-crossfade") || {}).value) || 0;
    if (xf > 0.01) return true;
    if (ed.overlays && ed.overlays.length > 0) return true;
    if (ed.bgmPath || (($("ed-bgm-path") && $("ed-bgm-path").value) || "").trim()) return true;
    const padMode = ($("ed-pad-mode") && $("ed-pad-mode").value) || "black";
    const aspMode = ($("ed-aspect-mode") && $("ed-aspect-mode").value) || "source";
    if (padMode !== "black" || aspMode !== "source") return true;
    // Copy kann nur eine Streamstruktur durchreichen.
    const sels = new Set(ed.segments.filter((s) => s.kind === "media")
      .map((s) => (s.audio_indexes || []).join(",")));
    if (sels.size > 1) return true;
    const subs = new Set(ed.segments.filter((s) => s.kind === "media")
      .map((s) => (s.sub_indexes || []).join(",")));
    if (subs.size > 1) return true;
    return ed.segments.some((s) => (
      s.kind === "black" || s.kind === "silence" || s.kind === "freeze"
      || Number(s.fade_in) > 0 || Number(s.fade_out) > 0
      || Math.abs((Number(s.speed) || 1) - 1) > 0.001
      || s.crop || s.delogo || s.text_overlay || s.reverse || Number(s.scale) > 0 || s.burn_subs
      || (s.look_preset && s.look_preset !== "none") || s.voice_enhance
      || Number(s.rotate) > 0 || s.hflip || s.vflip
      || Math.abs(Number(s.volume_db) || 0) > 0.05 || s.loudnorm
    ));
  }

  /** Cache-Eintrag einer Quelle ergänzen, ohne bekannte Werte zu verlieren. */
  function setAsset(path, patch) {
    ed.assets[path] = Object.assign(
      { strip: "", duration: 0 }, ed.assets[path] || {}, patch || {});
    return ed.assets[path];
  }

  /** Video-/Ton-Bitraten einer Quelle aus dem Probe-Cache, kurz gefasst. */
  function assetRates(path, clip) {
    const a = path ? ed.assets[path] : null;
    if (!a) return "";
    const bits = [];
    if (a.vbr) bits.push(`${tt("Video")} ${a.vbr}`);
    const tracks = a.audio || [];
    const sel = clip && Array.isArray(clip.audio_indexes)
      ? clip.audio_indexes
      : tracks.map((_, i) => i);
    const parts = sel.map((i) => {
      const t = tracks[i];
      if (!t) return "";
      const br = t.bitrate_human && t.bitrate_human !== "—" ? t.bitrate_human : "";
      return br ? `#${i} ${br}` : (t.codec ? `#${i} ${t.codec}` : "");
    }).filter(Boolean);
    if (parts.length) bits.push(`${tt("Ton")} ${parts.join(" · ")}`);
    else if (a.abr) bits.push(`${tt("Ton")} ${a.abr}`);
    const nsub = clip && Array.isArray(clip.sub_indexes) ? clip.sub_indexes.length
      : ((a.subtitles || []).length || 0);
    if (nsub) bits.push(`${nsub} UT`);
    return bits.join(" · ");
  }

  /** Bitraten aus einer Probe-Antwort für die Anzeige aufbereiten. */
  function rateInfo(data) {
    const audio = data.audio || [];
    const abr = audio
      .map((a) => a.bitrate_human)
      .filter((x) => x && x !== "—")
      .slice(0, 4)
      .join(" / ");
    return {
      vbr: data.video_bitrate_human || "",
      overall_bitrate: Number(data.overall_bitrate) || 0,
      abr,
      audio,
      subtitles: data.subtitles || [],
    };
  }

  function renderSegList() {
    const ul = $("ed-seg-list");
    if (!ul) return;
    ul.innerHTML = "";
    ed.segments.forEach((s, i) => {
      const li = document.createElement("li");
      li.className = "ed-seg-item";
      li.dataset.id = s.id;
      li.draggable = true;
      const dur = clipDur(s);
      const extras = [];
      const rates = assetRates(s.path, s);
      if (s.mute) extras.push(tt("stumm"));
      if (s.kind === "black") extras.push(tt("Schwarz"));
      if (s.kind === "freeze") extras.push(tt("Standbild"));
      if (Number(s.speed) && Number(s.speed) !== 1) extras.push(`${s.speed}×`);
      if (Number(s.fade_in) || Number(s.fade_out)) extras.push("Fade");
      if (s.crop) extras.push("Crop");
      if (s.delogo) extras.push("Blur");
      if (s.text_overlay) extras.push("Text");
      if (s.look_preset && s.look_preset !== "none") extras.push(`Look:${s.look_preset}`);
      if (s.voice_enhance) extras.push("🎙Dialog");
      if (s.reverse) extras.push("⏪");
      if (Number(s.rotate)) extras.push(`${s.rotate}°`);
      if (s.hflip || s.vflip) extras.push("Flip");
      if (Math.abs(Number(s.volume_db) || 0) > 0.05) extras.push(`${s.volume_db > 0 ? "+" : ""}${s.volume_db} dB`);
      if (s.loudnorm) extras.push("Loudnorm");
      li.innerHTML = `
        <div class="ed-seg-main">
          <strong>${i + 1}. ${escapeHtml(s.title || s.name)}</strong>
          <span class="muted">${escapeHtml(s.name || s.kind)} · ${fmt(s.start)} → ${fmt(s.end)} (${fmt(dur)})${extras.length ? " · " + extras.join(" · ") : ""}</span>
          ${rates ? `<span class="muted ed-seg-rates">${escapeHtml(rates)}</span>` : ""}
        </div>
        <div class="ed-seg-actions">
          <button type="button" class="btn btn-ghost btn-sm" data-act="up" title="Hoch">↑</button>
          <button type="button" class="btn btn-ghost btn-sm" data-act="down" title="Runter">↓</button>
          <button type="button" class="btn btn-ghost btn-sm" data-act="load" title="Quelle laden">↺</button>
          <button type="button" class="btn btn-ghost btn-sm bad-btn" data-act="del" title="Entfernen">×</button>
        </div>`;
      li.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => onSegAction(s.id, btn.dataset.act));
      });
      li.addEventListener("click", (ev) => {
        if (ev.target.closest("button, input, select")) return;
        ed.activeV2Id = null;
        setActive(s.id);
      });
      li.addEventListener("dragstart", (ev) => {
        ed.dragId = s.id;
        li.classList.add("dragging");
        ev.dataTransfer.effectAllowed = "move";
        try { ev.dataTransfer.setData("text/plain", s.id); } catch (e) { /* ignore */ }
      });
      li.addEventListener("dragend", () => li.classList.remove("dragging"));
      li.addEventListener("dragover", (ev) => { ev.preventDefault(); });
      li.addEventListener("drop", (ev) => {
        ev.preventDefault();
        moveClip(ed.dragId, s.id);
        ed.dragId = null;
      });
      ul.appendChild(li);
    });
    if (ed.activeId && !ed.segments.some((s) => s.id === ed.activeId)) ed.activeId = null;
    if (ed.activeV2Id && !ed.overlays.some((o) => o.id === ed.activeV2Id)) ed.activeV2Id = null;
    renderInspector();
    renderTimeline();
    renderSourceRuler();
    renderTracks();
    syncBadge();
    syncModeUI();
  }

  function renderInspector() {
    const box = $("ed-inspector");
    if (!box) return;
    const v2 = ed.overlays.find((o) => o.id === ed.activeV2Id) || null;
    if (v2) {
      const idx = ed.overlays.indexOf(v2);
      const dur = Math.max(0.05, Number(v2.end) - Number(v2.start));
      box.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <strong>V2 Overlay #${idx + 1}: ${escapeHtml(v2.title || v2.name)}</strong>
          <button type="button" class="btn btn-ghost btn-sm bad-btn" id="ed-insp-v2-del">Löschen</button>
        </div>
        <div class="muted" style="font-size:11.5px;margin-top:2px">${escapeHtml(v2.name || v2.path)} · Dauer ${fmt(dur)}</div>
        <div class="ed-insp-grid">
          <div class="full">
            <label>Titel</label>
            <input data-v2f="title" value="${escapeHtml(v2.title || "")}" />
          </div>
          <div>
            <label>Timeline-Start (V1)</label>
            <input data-v2f="tl_start" value="${fmt(v2.tl_start)}" title="Position auf der Haupt-Timeline" />
          </div>
          <div>
            <label>Layout / Position</label>
            <select data-v2f="position">
              <option value="pip_br"${v2.position === "pip_br" ? " selected" : ""}>PiP unten rechts</option>
              <option value="pip_tr"${v2.position === "pip_tr" ? " selected" : ""}>PiP oben rechts</option>
              <option value="pip_bl"${v2.position === "pip_bl" ? " selected" : ""}>PiP unten links</option>
              <option value="pip_tl"${v2.position === "pip_tl" ? " selected" : ""}>PiP oben links</option>
              <option value="side"${v2.position === "side" ? " selected" : ""}>Rechts mittig</option>
              <option value="full"${v2.position === "full" ? " selected" : ""}>Vollbild (B-Roll)</option>
            </select>
          </div>
          <div>
            <label>Quelle In</label>
            <input data-v2f="start" value="${fmt(v2.start)}" />
          </div>
          <div>
            <label>Quelle Out</label>
            <input data-v2f="end" value="${fmt(v2.end)}" />
          </div>
          <div>
            <label>Größe (% Breite)</label>
            <input type="number" min="15" max="100" step="5" data-v2f="size_pct" value="${v2.size_pct || 30}" />
          </div>
          <div>
            <label>Deckkraft (0.1–1.0)</label>
            <input type="number" min="0.1" max="1" step="0.05" data-v2f="opacity" value="${v2.opacity != null ? v2.opacity : 1}" />
          </div>
          <div>
            <label>Ein-/Ausblenden (s)</label>
            <input type="number" min="0" max="5" step="0.1" data-v2f="fade" value="${v2.fade || 0}" />
          </div>
          <div style="display:flex;align-items:flex-end">
            <button type="button" class="btn btn-ghost btn-sm" id="ed-insp-v2-load">Quelle in Vorschau</button>
          </div>
        </div>`;
      const delBtn = $("ed-insp-v2-del");
      if (delBtn) delBtn.addEventListener("click", () => {
        pushHist();
        ed.overlays = ed.overlays.filter((x) => x.id !== v2.id);
        ed.activeV2Id = null;
        renderSegList();
      });
      const loadBtn = $("ed-insp-v2-load");
      if (loadBtn) loadBtn.addEventListener("click", () => {
        loadSource(v2.path, v2.name, { inSec: v2.start, outSec: v2.end, seek: v2.start });
      });
      box.querySelectorAll("[data-v2f]").forEach((inp) => {
        inp.addEventListener("change", () => {
          pushHist();
          const f = inp.dataset.v2f;
          if (f === "title" || f === "position") v2[f] = String(inp.value);
          else if (f === "tl_start" || f === "start" || f === "end") v2[f] = round2(parseTimeStr(inp.value, v2[f]));
          else v2[f] = Number(inp.value);
          if (v2.end <= v2.start) v2.end = round2(v2.start + 0.5);
          renderSegList();
          syncV2Preview();
        });
      });
      return;
    }

    const s = activeClip();
    if (!s) {
      box.innerHTML = `<div class="muted" style="font-size:12px">Klicke auf einen Clip (V1) oder ein B-Roll-Overlay (V2), um alle Details zu bearbeiten.</div>`;
      return;
    }
    const idx = ed.segments.indexOf(s);
    const isMedia = s.kind === "media";
    box.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <strong>Clip #${idx + 1}: ${escapeHtml(clipLabel(s))} (${fmt(clipDur(s))})</strong>
        <button type="button" class="btn btn-ghost btn-sm" id="ed-insp-apply-all" title="Filter/Tempo/Skalierung/Lautstärke auf alle Clips anwenden">Auf alle Clips</button>
      </div>
      <div class="ed-insp-grid">
        <div class="full">
          <label>Titel (Kapitelname)</label>
          <input data-f="title" value="${escapeHtml(s.title || "")}" placeholder="${tt("Titel")}" />
        </div>
        <div>
          <label>In (Zeit / s)</label>
          <input data-f="start" value="${fmt(s.start)}" />
        </div>
        <div>
          <label>Out (Zeit / s)</label>
          <input data-f="end" value="${fmt(s.end)}" />
        </div>
        <div>
          <label>Fade-In (s)</label>
          <input type="number" step="0.1" min="0" max="15" data-f="fade_in" value="${s.fade_in || 0}" />
        </div>
        <div>
          <label>Fade-Out (s)</label>
          <input type="number" step="0.1" min="0" max="15" data-f="fade_out" value="${s.fade_out || 0}" />
        </div>
        <div>
          <label>Tempo (0.25–4×)</label>
          <input type="number" step="0.05" min="0.25" max="4" data-f="speed" value="${s.speed || 1}" />
        </div>
        <div>
          <label>Zielhöhe px (0 = Orig.)</label>
          <input type="number" step="2" min="0" max="2160" data-f="scale" value="${s.scale || 0}" />
        </div>
        ${isMedia ? `
        <div>
          <label>Drehen</label>
          <select data-f="rotate">
            <option value="0"${!s.rotate ? " selected" : ""}>0° (Original)</option>
            <option value="90"${Number(s.rotate) === 90 ? " selected" : ""}>90° rechts</option>
            <option value="180"${Number(s.rotate) === 180 ? " selected" : ""}>180°</option>
            <option value="270"${Number(s.rotate) === 270 ? " selected" : ""}>90° links (270°)</option>
          </select>
        </div>
        <div>
          <label>Spiegeln</label>
          <div style="display:flex;gap:8px;padding-top:4px">
            <label class="check" style="margin:0"><input type="checkbox" data-f="hflip"${s.hflip ? " checked" : ""} /><span>Horizontal</span></label>
            <label class="check" style="margin:0"><input type="checkbox" data-f="vflip"${s.vflip ? " checked" : ""} /><span>Vertikal</span></label>
          </div>
        </div>
        <div>
          <label>Lautstärke (dB)</label>
          <input type="number" step="0.5" min="-30" max="24" data-f="volume_db" value="${s.volume_db || 0}" />
        </div>
        <div style="display:flex;align-items:flex-end;gap:10px;padding-bottom:4px;flex-wrap:wrap">
          <label class="check" style="margin:0"><input type="checkbox" data-f="loudnorm"${s.loudnorm ? " checked" : ""} /><span>Loudnorm</span></label>
          <label class="check" style="margin:0" title="Entbrummen &amp; Sprache hervorheben (highpass + afftdn)"><input type="checkbox" data-f="voice_enhance"${s.voice_enhance ? " checked" : ""} /><span>🎙 Dialog+</span></label>
          <label class="check" style="margin:0" title="Spielt den Clip rückwärts ab (bis 60s)"><input type="checkbox" data-f="reverse"${s.reverse ? " checked" : ""} /><span>⏪ Rückwärts</span></label>
        </div>
        <div>
          <label>Bild-Look / Korrektur</label>
          <select data-f="look_preset">
            <option value="none"${(!s.look_preset || s.look_preset === "none") ? " selected" : ""}>Original (Kein Filter)</option>
            <option value="night"${s.look_preset === "night" ? " selected" : ""}>☀ Nachtszene aufhellen</option>
            <option value="vivid"${s.look_preset === "vivid" ? " selected" : ""}>🎨 Kräftigere Farben (Vivid)</option>
            <option value="clean"${s.look_preset === "clean" ? " selected" : ""}>✨ Entrauschen + Schärfen</option>
            <option value="cinema"${s.look_preset === "cinema" ? " selected" : ""}>🎬 Kino-Kontrast</option>
            <option value="bw"${s.look_preset === "bw" ? " selected" : ""}>🎞 Schwarz-Weiß</option>
          </select>
        </div>
        <div>
          <label>Text-Einblendung (optional)</label>
          <input data-f="text_overlay" value="${escapeHtml(s.text_overlay || "")}" placeholder="z. B. Kapitel 1 / Ort" />
        </div>
        <div>
          <label>Text-Position</label>
          <select data-f="text_pos">
            <option value="bottom"${(s.text_pos || "bottom") === "bottom" ? " selected" : ""}>Unten mittig (Bauchbinde)</option>
            <option value="top"${s.text_pos === "top" ? " selected" : ""}>Oben mittig</option>
            <option value="center"${s.text_pos === "center" ? " selected" : ""}>Zentriert</option>
            <option value="bl"${s.text_pos === "bl" ? " selected" : ""}>Unten links</option>
            <option value="br"${s.text_pos === "br" ? " selected" : ""}>Unten rechts</option>
            <option value="tl"${s.text_pos === "tl" ? " selected" : ""}>Oben links</option>
            <option value="tr"${s.text_pos === "tr" ? " selected" : ""}>Oben rechts</option>
          </select>
        </div>
        <div>
          <label>Crop (w:h:x:y)</label>
          <div style="display:flex;gap:4px">
            <input data-f="crop" value="${escapeHtml(s.crop || "")}" placeholder="3840:1600:0:280" style="flex:1" />
            <button type="button" class="btn btn-ghost btn-sm" id="ed-insp-crop-vis" title="Im Bild aufziehen">✂</button>
            <button type="button" class="btn btn-ghost btn-sm" id="ed-insp-crop-auto" title="Schwarze Ränder automatisch erkennen">Auto</button>
          </div>
        </div>
        <div>
          <label>Unkenntlich / Blur (w:h:x:y)</label>
          <div style="display:flex;gap:4px">
            <input data-f="delogo" value="${escapeHtml(s.delogo || "")}" placeholder="240:80:1600:40" style="flex:1" />
            <button type="button" class="btn btn-ghost btn-sm" id="ed-insp-blur-vis" title="Im Bild Rahmen aufziehen und auf 'Bereich unkenntlich' klicken">💨</button>
          </div>
        </div>` : ""}
      </div>`;

    const applyAll = $("ed-insp-apply-all");
    if (applyAll) applyAll.addEventListener("click", () => {
      pushHist();
      ed.segments.forEach((other) => {
        if (other.id === s.id || other.kind !== "media") return;
        other.speed = s.speed;
        other.scale = s.scale;
        other.crop = s.crop;
        other.rotate = s.rotate;
        other.hflip = s.hflip;
        other.vflip = s.vflip;
        other.volume_db = s.volume_db;
        other.loudnorm = s.loudnorm;
        other.look_preset = s.look_preset;
        other.voice_enhance = s.voice_enhance;
      });
      renderSegList();
      setStatus(tt("Effekte auf alle Clips übertragen."));
    });
    const visBtn = $("ed-insp-crop-vis");
    if (visBtn) visBtn.addEventListener("click", toggleCropOverlay);
    const blurVisBtn = $("ed-insp-blur-vis");
    if (blurVisBtn) blurVisBtn.addEventListener("click", toggleCropOverlay);
    const autoBtn = $("ed-insp-crop-auto");
    if (autoBtn) autoBtn.addEventListener("click", runAutoCrop);

    box.querySelectorAll("[data-f]").forEach((inp) => {
      inp.addEventListener("change", () => {
        pushHist();
        const f = inp.dataset.f;
        const rec = ed.segments.find((x) => x.id === s.id);
        if (!rec) return;
        if (inp.type === "checkbox") rec[f] = !!inp.checked;
        else if (f === "title" || f === "crop" || f === "delogo" || f === "text_overlay" || f === "text_pos" || f === "look_preset") rec[f] = String(inp.value).trim();
        else if (f === "start" || f === "end") rec[f] = round2(parseTimeStr(inp.value, rec[f]));
        else rec[f] = Number(inp.value);
        if (rec.end <= rec.start) rec.end = round2(rec.start + 0.1);
        if ((f === "start" || f === "end") && ed.src && ed.src.path === rec.path) {
          if ($("ed-in")) $("ed-in").value = fmt(rec.start);
          if ($("ed-out")) $("ed-out").value = fmt(rec.end);
          syncMarkLabels();
        }
        renderSegList();
      });
    });
  }

  /** Timeline-Länge ohne Überblendung – Basis für Blockbreiten und Playhead. */
  function rawTotal() {
    return ed.segments.reduce((a, s) => a + clipDur(s), 0);
  }

  /** Startzeit eines Clips auf der Timeline. */
  function tlOffset(idx) {
    let acc = 0;
    for (let i = 0; i < idx && i < ed.segments.length; i++) acc += clipDur(ed.segments[i]);
    return acc;
  }

  function activeClip() {
    return ed.segments.find((s) => s.id === ed.activeId) || null;
  }

  function clipLabel(s) {
    return s.title || s.name || (s.kind === "black" ? tt("Schwarz") : "Clip");
  }

  function applyTimelineZoom() {
    const canvas = $("ed-tl-canvas");
    const zEl = $("ed-tl-zoom");
    const zVal = $("ed-tl-zoom-val");
    const z = Math.max(1, Math.min(16, Number(ed.tlZoom) || 1));
    ed.tlZoom = z;
    if (zEl && Number(zEl.value) !== z) zEl.value = String(z);
    if (zVal) zVal.textContent = `${z}×`;
    if (canvas) canvas.style.width = `${z * 100}%`;
  }

  function renderTimeline() {
    applyTimelineZoom();
    const track = $("ed-timeline-track");
    if (!track) return;
    track.innerHTML = "";
    const tot = rawTotal();
    renderV2Track(tot);
    if (!ed.segments.length || tot <= 0) {
      const empty = document.createElement("div");
      empty.className = "ed-tl-empty";
      empty.textContent = tt("Noch keine Clips – Bereich behalten, ganze Datei oder + Quellen");
      track.appendChild(empty);
      renderTlRuler();
      renderTlPlayhead();
      return;
    }
    ed.segments.forEach((s, i) => {
      const dur = clipDur(s);
      const block = document.createElement("div");
      block.className = "ed-tl-block";
      block.dataset.id = s.id;
      block.draggable = true;
      block.style.width = `${Math.max(1.2 / (ed.tlZoom || 1), (dur / tot) * 100)}%`;
      block.style.setProperty("--tl-color", TL_COLORS[i % TL_COLORS.length]);
      if (s.kind !== "media") block.style.background = "#111827";
      applyStripBackground(block, s);
      const extras = [];
      if (Number(s.speed) && Number(s.speed) !== 1) extras.push(`${s.speed}×`);
      if (Number(s.fade_in) || Number(s.fade_out)) extras.push("Fade");
      if (s.mute) extras.push(tt("stumm"));
      if (s.crop) extras.push("Crop");
      const rates = assetRates(s.path, s);
      block.innerHTML = `
        <span class="ed-tl-idx">${i + 1}</span>
        <span class="ed-tl-name">${escapeHtml(clipLabel(s))}</span>
        <span class="ed-tl-meta">${fmt(dur)}${extras.length ? " · " + extras.join(" · ") : ""}</span>`;
      block.title = `${i + 1}. ${clipLabel(s)}\n${fmt(s.start)} → ${fmt(s.end)} (${fmt(dur)})`
        + (rates ? `\n${rates}` : "")
        + `\n${tt("Ränder ziehen = trimmen, Ecken oben = Fade, Rechtsklick = Menü")}`;
      addBlockHandles(block, s);
      wireBlockDrag(block, s);
      block.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        ed.activeV2Id = null;
        ed.activeId = s.id;
        markActive();
        renderInspector();
        openClipMenu(ev.clientX, ev.clientY, s.id);
      });
      if ((dur / tot) * 100 * (ed.tlZoom || 1) < 7) block.classList.add("narrow");
      track.appendChild(block);
    });
    renderTlRuler();
    markActive();
    renderTlPlayhead();
    loadStripsForClips();
  }

  function renderV2Track(tot) {
    const v2El = $("ed-timeline-v2");
    if (!v2El) return;
    v2El.innerHTML = "";
    if (!ed.overlays.length || tot <= 0) {
      v2El.title = tt("Spur V2 leer – mit '+ B-Roll (V2)' ein Bild-im-Bild oder Schnittbild auflegen");
      return;
    }
    ed.overlays.forEach((ov, idx) => {
      const dur = Math.max(0.1, Number(ov.end) - Number(ov.start));
      const leftPct = Math.max(0, Math.min(99, ((Number(ov.tl_start) || 0) / tot) * 100));
      const widthPct = Math.max(1.2, Math.min(100 - leftPct, (dur / tot) * 100));
      const b = document.createElement("div");
      b.className = "ed-v2-block" + (ov.id === ed.activeV2Id ? " active" : "");
      b.dataset.v2id = ov.id;
      b.style.left = `${leftPct}%`;
      b.style.width = `${widthPct}%`;
      b.innerHTML = `<strong>V2.${idx + 1}</strong> <span>${escapeHtml(ov.title || ov.name)} (${fmt(dur)})</span>`;
      b.title = `V2: ${ov.title || ov.name}\nStart @ ${fmt(ov.tl_start)} · Dauer ${fmt(dur)}\nZiehen = verschieben, Ränder = Länge ändern`;
      ["left", "right"].forEach((side) => {
        const edge = document.createElement("div");
        edge.className = `ed-v2-edge ${side}`;
        edge.addEventListener("mousedown", (ev) => beginV2Drag(ev, b, ov, side === "left" ? "trim-in" : "trim-out"));
        b.appendChild(edge);
      });
      b.addEventListener("mousedown", (ev) => {
        if (ev.target.closest(".ed-v2-edge")) return;
        beginV2Drag(ev, b, ov, "move");
      });
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        ed.activeV2Id = ov.id;
        markActive();
        renderInspector();
      });
      v2El.appendChild(b);
    });
  }

  function beginV2Drag(ev, block, ov, mode) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    ev.stopPropagation();
    const v2El = $("ed-timeline-v2");
    const tot = rawTotal();
    if (!v2El || !(tot > 0)) return;
    const rect = v2El.getBoundingClientRect();
    if (!(rect.width > 0)) return;
    const secPerPx = tot / rect.width;
    const startX = ev.clientX;
    const orig = { tl_start: Number(ov.tl_start) || 0, start: Number(ov.start) || 0, end: Number(ov.end) || 0 };
    const maxDur = srcDuration(ov.path) || 3600;
    let moved = false;
    ed.activeV2Id = ov.id;
    markActive();

    const onMove = (e) => {
      const dt = (e.clientX - startX) * secPerPx;
      if (!moved && Math.abs(e.clientX - startX) < 2) return;
      if (!moved) { moved = true; pushHist(); }
      if (mode === "move") {
        ov.tl_start = round2(Math.max(0, Math.min(tot - 0.1, orig.tl_start + dt)));
      } else if (mode === "trim-in") {
        const nextStart = round2(Math.max(0, Math.min(orig.end - 0.1, orig.start + dt)));
        const delta = nextStart - orig.start;
        ov.start = nextStart;
        ov.tl_start = round2(Math.max(0, orig.tl_start + delta));
      } else if (mode === "trim-out") {
        ov.end = round2(Math.max(orig.start + 0.1, Math.min(maxDur, orig.end + dt)));
      }
      const dur = Math.max(0.1, ov.end - ov.start);
      block.style.left = `${Math.max(0, Math.min(99, (ov.tl_start / tot) * 100))}%`;
      block.style.width = `${Math.max(1.2, (dur / tot) * 100)}%`;
      setStatus(`V2 ${ov.title || ov.name}: @ ${fmt(ov.tl_start)} (${fmt(dur)})`);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      if (moved) {
        renderSegList();
        syncV2Preview();
      } else {
        renderInspector();
      }
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  /** Passenden Ausschnitt des Filmstreifens als Blockhintergrund setzen. */
  function applyStripBackground(block, s) {
    const a = ed.assets[s.path];
    if (!a || !a.strip || !(a.duration > 0) || s.kind !== "media") return;
    const raw = Math.max(0.05, s.end - s.start);
    const d = a.duration;
    if (d / raw > 60) return;   // sehr kurzer Ausschnitt: Bild wäre nur Pixelbrei
    block.style.backgroundImage = `url(${a.strip})`;
    if (d > raw + 0.05) {
      block.style.backgroundSize = `${(d / raw) * 100}% 100%`;
      block.style.backgroundPosition = `${(s.start / (d - raw)) * 100}% 0`;
    } else {
      block.style.backgroundSize = "100% 100%";
      block.style.backgroundPosition = "0 0";
    }
  }

  /** Bekannte Länge der Quelldatei (für Trimm-Grenzen). */
  function srcDuration(path) {
    if (ed.src && ed.src.path === path && ed.src.duration > 0) return ed.src.duration;
    const a = ed.assets[path];
    return a && a.duration > 0 ? a.duration : 0;
  }

  /** Trimm-Kanten, Fade-Rampen mit Griffen und Tempo-Badge in den Block legen. */
  function addBlockHandles(block, s) {
    const dur = clipDur(s);
    const fi = Math.min(Number(s.fade_in) || 0, dur * 0.45);
    const fo = Math.min(Number(s.fade_out) || 0, dur * 0.45);

    const rampIn = document.createElement("div");
    rampIn.className = "ed-tl-ramp in";
    rampIn.style.width = `${dur > 0 ? (fi / dur) * 100 : 0}%`;
    const rampOut = document.createElement("div");
    rampOut.className = "ed-tl-ramp out";
    rampOut.style.width = `${dur > 0 ? (fo / dur) * 100 : 0}%`;
    block.append(rampIn, rampOut);

    ["left", "right"].forEach((side) => {
      const edge = document.createElement("div");
      edge.className = `ed-tl-edge ${side}`;
      edge.title = side === "left" ? tt("Anfang trimmen") : tt("Ende trimmen");
      edge.addEventListener("mousedown", (ev) => {
        beginBlockDrag(ev, block, s, side === "left" ? "trim-in" : "trim-out");
      });
      block.appendChild(edge);
    });

    [["in", fi], ["out", fo]].forEach(([side, val]) => {
      const grip = document.createElement("div");
      grip.className = `ed-tl-grip ${side}${val > 0.02 ? " set" : ""}`;
      grip.title = side === "in"
        ? `${tt("Fade-In")}: ${val.toFixed(1)} s`
        : `${tt("Fade-Out")}: ${val.toFixed(1)} s`;
      grip.addEventListener("mousedown", (ev) => {
        beginBlockDrag(ev, block, s, side === "in" ? "fade-in" : "fade-out");
      });
      block.appendChild(grip);
    });

    const badge = document.createElement("div");
    badge.className = "ed-tl-speed";
    badge.textContent = `${Number(s.speed) || 1}×`;
    badge.title = tt("Tempo ändern");
    badge.addEventListener("mousedown", (ev) => ev.stopPropagation());
    badge.addEventListener("click", (ev) => {
      ev.stopPropagation();
      ed.activeId = s.id;
      markActive();
      openClipMenu(ev.clientX, ev.clientY, s.id);
    });
    block.appendChild(badge);
  }

  /**
   * Trimmen und Fades direkt mit der Maus. Die Zeitskala wird beim Start
   * eingefroren, damit der Clip beim Ziehen nicht unter dem Zeiger wegläuft.
   */
  function beginBlockDrag(ev, block, s, mode) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    ev.stopPropagation();
    const tl = $("ed-timeline");
    if (!tl) return;
    const rect = tl.getBoundingClientRect();
    const tot = rawTotal();
    if (!(tot > 0) || !(rect.width > 0)) return;
    const secPerPx = tot / rect.width;
    const speed = Number(s.speed) > 0 ? Number(s.speed) : 1;
    const startX = ev.clientX;
    const orig = {
      start: s.start, end: s.end,
      fade_in: Number(s.fade_in) || 0, fade_out: Number(s.fade_out) || 0,
    };
    // Generierte Clips (Schwarz) sind frei dehnbar, Mediendateien enden bei ihrer Länge.
    const maxDur = s.kind === "media" ? srcDuration(s.path) : 3600;
    const wasDraggable = block.draggable;
    block.draggable = false;          // HTML5-Sortieren nicht gleichzeitig starten
    let moved = false;

    const onMove = (e) => {
      const dOut = (e.clientX - startX) * secPerPx;
      if (!moved && Math.abs(e.clientX - startX) < 2) return;
      if (!moved) { moved = true; pushHist(); }
      if (mode === "trim-in") {
        s.start = round2(Math.max(0, Math.min(orig.start + dOut * speed, s.end - 0.1)));
      } else if (mode === "trim-out") {
        const cap = maxDur > 0 ? maxDur : orig.end;
        s.end = round2(Math.max(s.start + 0.1, Math.min(orig.end + dOut * speed, cap)));
      } else {
        const lim = clipDur(s) * 0.45;
        if (mode === "fade-in") s.fade_in = round2(Math.max(0, Math.min(orig.fade_in + dOut, lim)));
        else s.fade_out = round2(Math.max(0, Math.min(orig.fade_out - dOut, lim)));
      }
      // Nur den gezogenen Block live nachziehen; exakt wird beim Loslassen gerendert.
      const dur = clipDur(s);
      block.style.width = `${Math.max(1.2, (dur / tot) * 100)}%`;
      const rIn = block.querySelector(".ed-tl-ramp.in");
      const rOut = block.querySelector(".ed-tl-ramp.out");
      if (rIn) rIn.style.width = `${dur > 0 ? (Math.min(s.fade_in || 0, dur * 0.45) / dur) * 100 : 0}%`;
      if (rOut) rOut.style.width = `${dur > 0 ? (Math.min(s.fade_out || 0, dur * 0.45) / dur) * 100 : 0}%`;
      const meta = block.querySelector(".ed-tl-meta");
      if (meta) meta.textContent = fmt(dur);
      setStatus(mode.indexOf("fade") === 0
        ? `${clipLabel(s)} · ${tt("Fade")} ${(mode === "fade-in" ? s.fade_in : s.fade_out).toFixed(1)} s`
        : `${clipLabel(s)} · ${fmt(s.start)} → ${fmt(s.end)} (${fmt(dur)})`);
    };

    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      block.draggable = wasDraggable;
      if (!moved) return;
      renderSegList();
      if ((mode === "trim-in" || mode === "trim-out") && ed.src && ed.src.path === s.path) {
        if ($("ed-in")) $("ed-in").value = String(round2(s.start));
        if ($("ed-out")) $("ed-out").value = String(round2(s.end));
      }
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  function closeClipMenu() {
    const el = document.querySelector(".ed-ctx");
    if (el) el.remove();
    document.removeEventListener("mousedown", onDocDownForMenu, true);
    document.removeEventListener("keydown", onKeyForMenu, true);
  }

  function onDocDownForMenu(ev) {
    if (!ev.target.closest(".ed-ctx")) closeClipMenu();
  }

  function onKeyForMenu(ev) {
    if (ev.key === "Escape") closeClipMenu();
  }

  /** Rechtsklick-Menü eines Clips: Schnitt, Tempo, Ton, Fades, Löschen. */
  function openClipMenu(x, y, id) {
    closeClipMenu();
    const s = ed.segments.find((c) => c.id === id);
    if (!s) return;
    ed.activeId = s.id;      // alle Menüpunkte arbeiten auf dem aktiven Clip
    markActive();
    const idx = ed.segments.indexOf(s);
    const menu = document.createElement("div");
    menu.className = "ed-ctx";

    const head = document.createElement("div");
    head.className = "ed-ctx-head";
    head.textContent = `${idx + 1}. ${clipLabel(s)} · ${fmt(clipDur(s))}`;
    menu.appendChild(head);

    const item = (label, fn, cls) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      if (cls) b.className = cls;
      b.addEventListener("click", () => { closeClipMenu(); fn(); });
      menu.appendChild(b);
      return b;
    };

    const sameSrc = s.kind === "media" && ed.src && ed.src.path === s.path;
    const canSplit = sameSrc && ed.playhead > s.start + 0.05 && ed.playhead < s.end - 0.05;
    const split = item(`✂ ${tt("Am Playhead teilen")}`, splitAtPlayhead);
    split.disabled = !canSplit;
    if (!canSplit) split.style.opacity = ".45";
    item(`⧉ ${tt("Duplizieren")}`, duplicateActive);
    if (s.kind === "media") {
      item(s.mute ? `🔊 ${tt("Ton an")}` : `🔇 ${tt("Stumm")}`, () => {
        pushHist();
        s.mute = !s.mute;
        if (s.mute) s.audio_indexes = [];
        else if (!(s.audio_indexes || []).length) {
          s.audio_indexes = [Math.max(0, Number(s.audio_index) || 0)];
          s.audio_index = s.audio_indexes[0];
        }
        renderSegList();
      });
    }
    if ((Number(s.fade_in) || 0) > 0 || (Number(s.fade_out) || 0) > 0) {
      item(`⌁ ${tt("Fades zurücksetzen")}`, () => {
        pushHist();
        s.fade_in = 0;
        s.fade_out = 0;
        renderSegList();
      });
    } else {
      item(`⌁ ${tt("Fade 1 s ein und aus")}`, () => {
        pushHist();
        const lim = clipDur(s) * 0.45;
        s.fade_in = round2(Math.min(1, lim));
        s.fade_out = round2(Math.min(1, lim));
        renderSegList();
      });
    }
    if (s.kind === "media" && s.path) {
      item(`↺ ${tt("Quelle in Vorschau laden")}`, () => setActive(s.id));
    }

    const speedLab = document.createElement("div");
    speedLab.className = "ed-ctx-label";
    speedLab.textContent = tt("Tempo");
    menu.appendChild(speedLab);
    const row = document.createElement("div");
    row.className = "ed-ctx-row";
    [0.5, 0.75, 1, 1.5, 2].forEach((v) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = `${v}×`;
      if (Math.abs((Number(s.speed) || 1) - v) < 0.001) b.className = "on";
      b.addEventListener("click", () => {
        closeClipMenu();
        pushHist();
        s.speed = v;
        renderSegList();
        setStatus(`${clipLabel(s)} · ${tt("Tempo")} ${v}×`);
      });
      row.appendChild(b);
    });
    menu.appendChild(row);

    item(`🗑 ${tt("Löschen")}`, () => { ed.activeId = s.id; deleteActive(); }, "bad");

    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    document.body.appendChild(menu);
    const r = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(6, Math.min(x, window.innerWidth - r.width - 6))}px`;
    menu.style.top = `${Math.max(6, Math.min(y, window.innerHeight - r.height - 6))}px`;
    document.addEventListener("mousedown", onDocDownForMenu, true);
    document.addEventListener("keydown", onKeyForMenu, true);
  }

  /** Ziehen im Zeitlineal: Playhead folgt, gesucht wird erst beim Loslassen. */
  function beginRulerScrub(ev) {
    const tl = $("ed-timeline");
    const ruler = $("ed-tl-ruler");
    if (!tl || !ruler || !ed.segments.length) return;
    ev.preventDefault();
    const rect = tl.getBoundingClientRect();
    const tot = rawTotal();
    if (!(tot > 0)) return;
    const head = $("ed-tl-playhead");
    const lab = $("ed-tl-time");
    const at = (x) => Math.max(0, Math.min(1, (x - rect.left) / rect.width));
    const show = (ratio) => {
      if (head) {
        head.hidden = false;
        head.style.left = `${ratio * 100}%`;
      }
      if (lab) lab.textContent = `${fmt(ratio * tot)} / ${fmt(totalDur())}`;
    };
    show(at(ev.clientX));
    const onMove = (e) => show(at(e.clientX));
    const onUp = (e) => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      timelineSeek(at(e.clientX));
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  function wireBlockDrag(block, s) {
    block.addEventListener("dragstart", (ev) => {
      ed.dragId = s.id;
      block.classList.add("dragging");
      ev.dataTransfer.effectAllowed = "move";
      // Ohne Nutzdaten startet Firefox das Ziehen nicht.
      try { ev.dataTransfer.setData("text/plain", s.id); } catch (e) { /* ignore */ }
    });
    block.addEventListener("dragend", () => {
      block.classList.remove("dragging");
      const t = $("ed-timeline-track");
      if (t) t.querySelectorAll(".drop-target").forEach((e) => e.classList.remove("drop-target"));
    });
    block.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      if (ed.dragId && ed.dragId !== s.id) block.classList.add("drop-target");
    });
    block.addEventListener("dragleave", () => block.classList.remove("drop-target"));
    block.addEventListener("drop", (ev) => {
      ev.preventDefault();
      block.classList.remove("drop-target");
      moveClip(ed.dragId, s.id);
      ed.dragId = null;
    });
  }

  function moveClip(fromId, toId) {
    if (!fromId || fromId === toId) return;
    const from = ed.segments.findIndex((x) => x.id === fromId);
    const to = ed.segments.findIndex((x) => x.id === toId);
    if (from < 0 || to < 0) return;
    pushHist();
    const [m] = ed.segments.splice(from, 1);
    ed.segments.splice(to, 0, m);
    renderSegList();
    flashClip(m.id);
  }

  function renderTlRuler() {
    const el = $("ed-tl-ruler");
    if (!el) return;
    el.innerHTML = "";
    const tot = rawTotal();
    if (tot <= 0) return;
    const maxTicks = Math.round(12 * (ed.tlZoom || 1));
    const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const step = steps.find((s) => tot / s <= maxTicks) || 3600;
    for (let t = 0; t <= tot + 0.001; t += step) {
      const tick = document.createElement("span");
      tick.className = "ed-tl-tick";
      tick.style.left = `${(t / tot) * 100}%`;
      tick.textContent = fmt(t);
      el.appendChild(tick);
    }
  }

  function currentTimelineTime() {
    const tot = rawTotal();
    const s = activeClip();
    if (s && s.kind === "media" && ed.src && ed.src.path === s.path && tot > 0) {
      const cur = ed.playhead;
      if (cur >= s.start - 0.25 && cur <= s.end + 0.25) {
        const sp = Number(s.speed) > 0 ? Number(s.speed) : 1;
        const idx = ed.segments.indexOf(s);
        return tlOffset(idx) + Math.max(0, Math.min(clipDur(s), (cur - s.start) / sp));
      }
    }
    return -1;
  }

  /** Playhead auf der Gesamt-Timeline, solange die Vorschau im aktiven Clip liegt. */
  function renderTlPlayhead() {
    const head = $("ed-tl-playhead");
    const lab = $("ed-tl-time");
    const tot = rawTotal();
    const t = currentTimelineTime();
    if (head) {
      head.hidden = t < 0;
      if (t >= 0 && tot > 0) head.style.left = `calc(32px + (100% - 32px) * ${(t / tot).toFixed(5)})`;
    }
    if (lab) lab.textContent = `${t >= 0 ? fmt(t) : "–"} / ${fmt(totalDur())}`;
    syncV2Preview(t);
  }

  function syncV2Preview(tlTime) {
    const v2 = $("ed-video-v2");
    if (!v2) return;
    const t = tlTime != null ? tlTime : currentTimelineTime();
    if (t < 0 || !ed.overlays.length) {
      v2.hidden = true;
      if (!v2.paused) v2.pause();
      return;
    }
    const activeOv = ed.overlays.find((o) => {
      const dur = Math.max(0.05, Number(o.end) - Number(o.start));
      return t >= Number(o.tl_start) && t <= Number(o.tl_start) + dur;
    });
    if (!activeOv) {
      v2.hidden = true;
      if (!v2.paused) v2.pause();
      return;
    }
    const pos = activeOv.position || "pip_br";
    const pct = Math.max(15, Math.min(100, Number(activeOv.size_pct) || 30));
    v2.style.width = pos === "full" ? "100%" : `${pct}%`;
    v2.style.height = pos === "full" ? "100%" : "auto";
    v2.style.opacity = String(activeOv.opacity != null ? activeOv.opacity : 1);
    v2.style.top = (pos === "pip_tr" || pos === "pip_tl") ? "3.5%" : (pos === "full" ? "0" : (pos === "side" ? "50%" : "auto"));
    v2.style.bottom = (pos === "pip_br" || pos === "pip_bl") ? "3.5%" : "auto";
    v2.style.left = (pos === "pip_bl" || pos === "pip_tl") ? "2.5%" : (pos === "full" ? "0" : "auto");
    v2.style.right = (pos === "pip_br" || pos === "pip_tr" || pos === "side") ? "2.5%" : "auto";
    v2.style.transform = pos === "side" ? "translateY(-50%)" : "none";
    const url = fileUrl(activeOv.path);
    if (ed.v2Url !== url) {
      ed.v2Url = url;
      v2.src = url;
    }
    const target = Number(activeOv.start) + (t - Number(activeOv.tl_start));
    if (Math.abs((v2.currentTime || 0) - target) > 0.35 && v2.readyState >= 1) {
      try { v2.currentTime = target; } catch (e) { /* ignore */ }
    }
    v2.hidden = false;
    const pv = $("ed-prog-video") || $("ed-video");
    if (pv && !pv.paused && v2.paused) v2.play().catch(() => {});
    else if (pv && pv.paused && !v2.paused) v2.pause();
  }

  function markActive() {
    const track = $("ed-timeline-track");
    if (track) {
      track.querySelectorAll(".ed-tl-block").forEach((b) => {
        b.classList.toggle("active", !ed.activeV2Id && b.dataset.id === ed.activeId);
      });
    }
    const v2Track = $("ed-timeline-v2");
    if (v2Track) {
      v2Track.querySelectorAll(".ed-v2-block").forEach((b) => {
        b.classList.toggle("active", b.dataset.v2id === ed.activeV2Id);
      });
    }
    const ul = $("ed-seg-list");
    if (ul) {
      ul.querySelectorAll(".ed-seg-item").forEach((li) => {
        li.classList.toggle("active", !ed.activeV2Id && li.dataset.id === ed.activeId);
      });
    }
    renderSourceRuler();
    syncTools();
  }

  function flashClip(id) {
    const track = $("ed-timeline-track");
    if (!track) return;
    const el = track.querySelector(`.ed-tl-block[data-id="${id}"]`);
    if (!el) return;
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1900);
  }

  function syncTools() {
    const s = activeClip();
    const idx = s ? ed.segments.indexOf(s) : -1;
    const isMedia = !!(s && s.kind === "media");
    const sameSrc = !!(isMedia && ed.src && ed.src.path === s.path);
    const inside = sameSrc && ed.playhead > s.start + 0.05 && ed.playhead < s.end - 0.05;
    const set = (id, on) => { const el = $(id); if (el) el.disabled = !on; };
    set("ed-tl-split", inside);
    set("ed-tl-trim-in", sameSrc);
    set("ed-tl-trim-out", sameSrc);
    set("ed-tl-left", idx > 0);
    set("ed-tl-right", idx >= 0 && idx < ed.segments.length - 1);
    set("ed-tl-dup", !!s);
    set("ed-tl-del", !!s);
    const lab = $("ed-tl-sel");
    if (lab) {
      lab.textContent = s
        ? `${tt("Clip")} ${idx + 1}/${ed.segments.length}: ${clipLabel(s)} · ${fmt(clipDur(s))}`
        : tt("Kein Clip gewählt");
    }
  }

  /** Clip auswählen; lädt bei Bedarf die zugehörige Quelle in die Vorschau. */
  async function setActive(id, opts) {
    opts = opts || {};
    const s = ed.segments.find((x) => x.id === id) || null;
    ed.activeId = s ? s.id : null;
    markActive();
    renderTlPlayhead();
    if (!s || s.kind !== "media" || !s.path) return;
    const srcTime = opts.srcTime != null ? opts.srcTime : s.start;
    if (!ed.src || ed.src.path !== s.path) {
      await loadSource(s.path, s.name, { inSec: s.start, outSec: s.end, seek: srcTime });
    } else {
      if ($("ed-in")) $("ed-in").value = fmt(s.start);
      if ($("ed-out")) $("ed-out").value = fmt(s.end);
      syncMarkLabels();
      // Die Zielzeit ist berechnet – hier darf nichts einrasten.
      if (opts.seek !== false) seekSoft(srcTime, { snap: false });
      renderSourceRuler();
    }
    markActive();
    renderInspector();
    syncProgramMonitorInfo(srcTime);
  }

  /** Klick in die Timeline: Clip wählen und an die passende Quellzeit springen. */
  function timelineSeek(ratio) {
    const tot = rawTotal();
    if (tot <= 0) return;
    const t = Math.max(0, Math.min(tot, ratio * tot));
    let acc = 0;
    for (let i = 0; i < ed.segments.length; i++) {
      const s = ed.segments[i];
      const d = clipDur(s);
      if (t < acc + d || i === ed.segments.length - 1) {
        const sp = Number(s.speed) > 0 ? Number(s.speed) : 1;
        setActive(s.id, { srcTime: s.start + (t - acc) * sp });
        return;
      }
      acc += d;
    }
  }

  /** Filmstreifen der Clip-Quellen nachladen (einmal pro Datei). */
  function loadStripsForClips() {
    const paths = [];
    ed.segments.forEach((s) => {
      const a = ed.assets[s.path];
      if (s.kind === "media" && s.path && !(a && a.tried) && paths.indexOf(s.path) < 0) {
        paths.push(s.path);
      }
    });
    paths.forEach(async (p) => {
      const prev = ed.assets[p] || { strip: "", duration: 0 };
      // tried verhindert Doppelanfragen bei jedem Neuzeichnen.
      setAsset(p, { tried: true });
      try {
        const d = await (await fetch(`/api/editor/preview-assets?path=${encodeURIComponent(p)}`)).json();
        setAsset(p, {
          strip: d.filmstrip || "",
          duration: Number(d.duration) || prev.duration || 0,
          tried: true,
        });
        if (ed.assets[p].strip) renderTimeline();
      } catch (e) { /* ohne Bild weiter */ }
    });
  }

  function renderSourceRuler() {
    const keepsEl = $("ed-src-keeps");
    const selEl = $("ed-src-sel");
    const head = $("ed-src-playhead");
    const chEl = $("ed-src-chaps");
    if (!keepsEl || !selEl || !ed.src || !(ed.src.duration > 0)) {
      if (keepsEl) keepsEl.innerHTML = "";
      if (selEl) selEl.style.width = "0";
      if (head) head.style.left = "0";
      if (chEl) chEl.innerHTML = "";
      return;
    }
    const dur = ed.src.duration;
    // Jeder Clip einzeln: so sieht man Schnittkanten und den aktiven Clip.
    const clips = ed.segments.filter((s) => s.kind === "media" && s.path === ed.src.path);
    keepsEl.innerHTML = "";
    if (clips.length) {
      clips.forEach((c) => {
        const d = document.createElement("div");
        d.className = "ed-src-keep" + (c.id === ed.activeId ? " active" : "");
        d.style.left = `${(c.start / dur) * 100}%`;
        d.style.width = `${Math.max(0.3, ((c.end - c.start) / dur) * 100)}%`;
        d.title = `${ed.segments.indexOf(c) + 1}. ${clipLabel(c)} · ${fmt(c.start)} – ${fmt(c.end)}`;
        keepsEl.appendChild(d);
      });
    } else {
      const d = document.createElement("div");
      d.className = "ed-src-keep ed-src-keep-empty";
      d.style.left = "0";
      d.style.width = "100%";
      d.title = tt("Noch keine Clips – ganze Datei oder Bereiche behalten/entfernen");
      keepsEl.appendChild(d);
    }
    if (chEl) {
      chEl.innerHTML = "";
      (ed.src.chapters || []).forEach((c) => {
        const m = document.createElement("div");
        m.className = "ed-src-chap";
        m.style.left = `${((Number(c.start) || 0) / dur) * 100}%`;
        m.title = c.title || fmt(c.start);
        chEl.appendChild(m);
      });
    }
    const { start, end } = readMarks();
    const a = Math.max(0, Math.min(start, end));
    const b = Math.min(dur, Math.max(start, end));
    selEl.style.left = `${(a / dur) * 100}%`;
    selEl.style.width = `${Math.max(0, ((b - a) / dur) * 100)}%`;
    if (head) head.style.left = `${(Math.min(dur, Math.max(0, ed.playhead)) / dur) * 100}%`;
  }

  function onSegAction(id, act) {
    const idx = ed.segments.findIndex((s) => s.id === id);
    if (idx < 0) return;
    ed.activeId = id;
    if (act === "del") {
      deleteActive();
      return;
    }
    if (act === "up") { moveActive(-1); return; }
    if (act === "down") { moveActive(1); return; }
    if (act === "load") {
      setActive(id);
      return;
    }
    renderSegList();
  }

  /** Welche Tonspur die Vorschau hörbar machen soll (-1 = stumm). */
  function previewAudioIndex() {
    const sel = $("ed-preview-audio");
    const n = sel ? parseInt(sel.value, 10) : 0;
    return Number.isFinite(n) ? n : 0;
  }

  function mediaUrl(path, startSec) {
    const audio = previewAudioIndex();
    let u = `/api/media/stream?root=media&owner=editor&path=${encodeURIComponent(path)}&audio=${audio}`;
    if (startSec && startSec > 0) u += `&start=${encodeURIComponent(String(startSec))}`;
    return u;
  }

  function fileUrl(path) {
    return `/api/media?root=media&path=${encodeURIComponent(path)}`;
  }

  /** Originaldatei direkt ins <video>: exakte Zeiten, kein Transcode, kein A/V-Versatz. */
  function canPlayFile() {
    if (!ed.src || !clientDirectOk(ed.src)) return false;
    // Eine andere als die erste Tonspur kann nur der Server heraussuchen.
    return previewAudioIndex() <= 0;
  }

  function seekFileTo(v, t) {
    const target = Math.max(0, Number(t) || 0);
    const apply = () => {
      try { v.currentTime = target; } catch (e) { /* noch nicht bereit */ }
    };
    if (v.readyState >= 1) apply();
    else v.addEventListener("loadedmetadata", apply, { once: true });
  }

  async function stopPreviewSession() {
    if (ed.hls) {
      try { ed.hls.destroy(); } catch (e) { /* ignore */ }
      ed.hls = null;
    }
    if (ed.sid) {
      const sid = ed.sid;
      ed.sid = null;
      try { await fetch(`/api/player/session/${sid}`, { method: "DELETE" }); } catch (e) { /* ignore */ }
    }
    ed.previewMode = "none";
  }

  function stopVideo() {
    const v = $("ed-video");
    stopPreviewSession();
    if (!v) return;
    v.pause();
    v.removeAttribute("src");
    try { v.load(); } catch (e) { /* ignore */ }
    ed.streamUrl = "";
  }

  function playHls(url) {
    const v = $("ed-video");
    if (!v) return;
    if (ed.hls) {
      try { ed.hls.destroy(); } catch (e) { /* ignore */ }
      ed.hls = null;
    }
    if (window.Hls && window.Hls.isSupported()) {
      ed.hls = new window.Hls({
        enableWorker: true, lowLatencyMode: false, startPosition: 0,
        maxBufferLength: 20, liveMaxLatencyDurationCount: Infinity,
        maxLiveSyncPlaybackRate: 1,
      });
      ed.hls.loadSource(url);
      ed.hls.attachMedia(v);
      ed.hls.on(window.Hls.Events.MANIFEST_PARSED, () => { v.play().catch(() => {}); });
    } else {
      v.src = url;
      v.play().catch(() => {});
    }
  }

  async function startPreview(sec) {
    const v = $("ed-video");
    if (!v || !ed.src || !ed.src.path) return;
    const start = Math.max(0, Number(sec) || 0);
    ed.playhead = start;
    ed.previewOffset = start;
    await stopPreviewSession();
    if (canPlayFile()) {
      // Ganze Datei laden, im Browser springen: Zeitachse = echte Quellzeit.
      ed.previewMode = "file";
      ed.previewOffset = 0;
      v.muted = previewAudioIndex() < 0;
      const url = fileUrl(ed.src.path);
      if (ed.streamUrl !== url || !v.currentSrc) {
        ed.streamUrl = url;
        v.src = url;
        v.load();
      }
      seekFileTo(v, start);
      return;
    }
    v.muted = false;
    if (clientDirectOk(ed.src)) {
      ed.previewMode = "direct";
      ed.streamUrl = mediaUrl(ed.src.path, start);
      v.src = ed.streamUrl;
      v.load();
      return;
    }
    try {
      const r = await fetch("/api/player/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: ed.src.path,
          owner: "editor",
          audio: previewAudioIndex(),
          subtitle: audioOpts().sub_index,
          start,
          profile: "auto",
          burn_subs: audioOpts().burn_subs,
          client_direct_ok: false,
          client_codecs: detectClientCodecs(),
          lookahead_sec: 30,
        }),
      });
      const d = await r.json();
      const sess = d.session || {};
      if (!r.ok || d.error) throw new Error(d.error || "Vorschau fehlgeschlagen");
      ed.sid = sess.id;
      ed.previewMode = sess.mode || "hls";
      if (sess.mode === "direct") {
        v.src = sess.media_url || sess.playlist_url;
        v.load();
      } else {
        const url = sess.playlist_url || `/api/player/session/${ed.sid}/index.m3u8`;
        let tries = 0;
        while (tries < 40) {
          const st = await (await fetch(`/api/player/session/${ed.sid}`)).json();
          if (st.session && st.session.ready) break;
          if (st.session && st.session.error) throw new Error(st.session.error);
          await new Promise((res) => setTimeout(res, 150));
          tries += 1;
        }
        playHls(url);
      }
    } catch (e) {
      setStatus(String(e.message || e), true);
      ed.previewMode = "direct";
      v.src = mediaUrl(ed.src.path, start);
      v.load();
    }
  }

  async function loadPreviewAssets(path) {
    const strip = $("ed-src-strip");
    const wave = $("ed-wave");
    if (strip) strip.style.backgroundImage = "";
    if (wave) { wave.hidden = true; wave.style.backgroundImage = ""; }
    try {
      const d = await (await fetch(`/api/editor/preview-assets?path=${encodeURIComponent(path)}`)).json();
      if (d.filmstrip && strip) strip.style.backgroundImage = `url(${d.filmstrip})`;
      if (d.waveform && wave) {
        wave.hidden = false;
        wave.style.backgroundImage = `url(${d.waveform})`;
      }
      const prev = ed.assets[path] || { strip: "", duration: 0 };
      setAsset(path, {
        strip: d.filmstrip || "",
        duration: Number(d.duration) || prev.duration || 0,
        tried: true,
      });
      if (!prev.strip && d.filmstrip) renderTimeline();
    } catch (e) { /* ignore */ }
  }

  async function loadKeyframes(path) {
    ed.keyframes = [];
    try {
      const d = await (await fetch(`/api/editor/keyframes?path=${encodeURIComponent(path)}`)).json();
      ed.keyframes = Array.isArray(d.times) ? d.times : [];
    } catch (e) { /* ignore */ }
  }

  async function loadSource(path, name, opts) {
    opts = opts || {};
    setStatus(tt("Lade Quelle …"));
    // Beim automatischen Clipwechsel läuft die Timeline-Vorschau weiter.
    if (!ed.advancing) stopTlPreview();
    stopVideo();
    let data;
    try {
      const r = await fetch(`/api/editor/probe?path=${encodeURIComponent(path)}`);
      data = await r.json();
      if (!r.ok || data.error) throw new Error(data.error || "Probe fehlgeschlagen");
    } catch (e) {
      setStatus(String(e.message || e), true);
      return;
    }
    const srcDur = Number(data.duration) || 0;
    const knownAsset = ed.assets[path] || { strip: "", duration: 0 };
    setAsset(path, Object.assign(
      { duration: srcDur || knownAsset.duration }, rateInfo(data)));
    ed.src = {
      path,
      name: name || data.name || path,
      duration: srcDur,
      audio: data.audio || [],
      subtitles: data.subtitles || [],
      chapters: data.chapters || [],
      codec: data.codec || "",
      container: data.container || "",
      fps: Number(data.fps) || 25,
      width: data.width || 0,
      height: data.height || 0,
      overall_bitrate: Number(data.overall_bitrate) || 0,
    };
    const aSel = $("ed-preview-audio");
    if (aSel) {
      aSel.innerHTML = "";
      const none = document.createElement("option");
      none.value = "-1";
      none.textContent = tt("Kein Ton");
      ed.src.audio.forEach((a, i) => {
        const o = document.createElement("option");
        o.value = String(i);
        o.textContent = trackLabel(a, i);
        aSel.appendChild(o);
      });
      aSel.appendChild(none);
      aSel.value = ed.src.audio.length ? "0" : "-1";
    }
    ed.inSec = opts.inSec != null ? opts.inSec : 0;
    ed.outSec = opts.outSec != null ? opts.outSec : ed.src.duration;
    ed.playhead = opts.seek != null ? opts.seek : ed.inSec;
    if ($("ed-in")) $("ed-in").value = fmt(ed.inSec);
    if ($("ed-out")) $("ed-out").value = fmt(ed.outSec);
    syncMarkLabels();
    renderChapterChips();
    const info = $("ed-src-info");
    if (info) {
      info.textContent = `${ed.src.name} · ${fmt(ed.src.duration)}`
        + (data.size_human ? ` · ${data.size_human}` : "")
        + (ed.src.codec ? ` · ${String(ed.src.codec).toUpperCase()}` : "")
        + (data.video_bitrate_human ? ` · ${data.video_bitrate_human} ${tt("Video")}` : "")
        + ((ed.src.chapters || []).length ? ` · ${ed.src.chapters.length} ${tt("Kapitel")}` : "");
      info.title = path.startsWith("upload:") ? `${path} (Upload)` : path;
    }
    const unload = $("ed-unload");
    if (unload) unload.disabled = false;
    renderTracks();
    seekPreview(ed.playhead, { snap: false });
    renderSourceRuler();
    setStatus("");
    loadPreviewAssets(path);
    loadKeyframes(path);
  }

  function afterSoftSeek(t) {
    ed.playhead = t;
    updateTimeLabel(t, ed.src.duration);
    const seek = $("ed-seek");
    if (seek && ed.src.duration > 0) {
      seek.value = String(Math.round((t / ed.src.duration) * 1000));
    }
    renderSourceRuler();
  }

  /** Quelle aus der Vorschau nehmen – die Timeline bleibt unangetastet. */
  function unloadSource() {
    stopTlPreview();
    stopFollowTimer();
    stopVideo();
    ed.src = null;
    ed.inSec = 0;
    ed.outSec = 0;
    ed.playhead = 0;
    ed.keyframes = [];
    ed.previewOffset = 0;
    ed.inClip = false;
    ["ed-in", "ed-out", "ed-seek"].forEach((id) => {
      const el = $(id);
      if (el) el.value = "0";
    });
    const time = $("ed-time");
    if (time) time.textContent = "0:00 / 0:00";
    const info = $("ed-src-info");
    if (info) {
      info.textContent = tt("Keine Quelle geladen.");
      info.title = "";
    }
    const unload = $("ed-unload");
    if (unload) unload.disabled = true;
    const strip = $("ed-src-strip");
    if (strip) strip.style.backgroundImage = "";
    const wave = $("ed-wave");
    if (wave) { wave.hidden = true; wave.style.backgroundImage = ""; }
    const aSel = $("ed-preview-audio");
    if (aSel) aSel.innerHTML = `<option value="-1">${tt("Kein Ton")}</option>`;
    renderTracks();
    renderChapterChips();
    syncMarkLabels();
    renderSourceRuler();
    renderTlPlayhead();
    syncTools();
  }

  /** Kompletter Neuanfang: Timeline leeren und Quelle entladen. */
  function resetAll() {
    if ((ed.segments.length || ed.overlays.length)
        && !window.confirm(tt("Timeline leeren und Quelle entladen?"))) return;
    pushHist();
    ed.segments = [];
    ed.overlays = [];
    ed.activeId = null;
    ed.activeV2Id = null;
    ed.trackSel = {};
    ed.subSel = {};
    ed.assets = {};
    unloadSource();
    renderSegList();
    setStatus(tt("Editor zurückgesetzt."));
  }

  function seekSoft(sec, opts) {
    if (!ed.src) return;
    const raw = Math.max(0, Math.min(ed.src.duration || 0, Number(sec) || 0));
    const noSnap = !!(opts && opts.snap === false);
    const t = noSnap ? Math.round(raw * 1000) / 1000 : snapTime(raw);
    const v = $("ed-video");
    if (v && ed.previewMode === "file" && v.currentSrc) {
      // Ganze Datei im Player: jeder Punkt ist direkt erreichbar.
      seekFileTo(v, t);
      afterSoftSeek(t);
      return;
    }
    const rel = t - ed.previewOffset;
    if (v && ed.previewMode !== "none" && rel >= -0.05 && rel < 20) {
      try { v.currentTime = Math.max(0, rel); } catch (e) { seekPreview(t, opts); return; }
      afterSoftSeek(t);
      return;
    }
    seekPreview(t, opts);
  }

  function seekPreview(sec, opts) {
    const raw = Math.max(0, Number(sec) || 0);
    const start = opts && opts.snap === false
      ? Math.round(raw * 1000) / 1000
      : snapTime(raw);
    ed.playhead = start;
    const seek = $("ed-seek");
    if (seek && ed.src && ed.src.duration > 0) {
      seek.value = String(Math.round((start / ed.src.duration) * 1000));
    }
    updateTimeLabel(start, ed.src ? ed.src.duration : 0);
    renderSourceRuler();
    startPreview(start);
  }

  function updateTimeLabel(cur, dur) {
    const el = $("ed-time");
    if (el) el.textContent = `${fmt(cur)} / ${fmt(dur || 0)}`;
    ed.playhead = cur;
    const head = $("ed-src-playhead");
    if (head && dur > 0) {
      head.style.left = `${(Math.min(dur, Math.max(0, cur)) / dur) * 100}%`;
    }
    renderTlPlayhead();
    syncTools();
  }

  function currentPreviewTime() {
    if (!ed.src) return 0;
    if (ed.previewMode === "file") {
      const v = $("ed-video");
      if (v && v.currentSrc) return v.currentTime || 0;
    }
    if (ed.previewMode === "hls" || ed.previewMode === "direct") {
      const v = $("ed-video");
      const t = ed.previewOffset + ((v && v.currentTime) || 0);
      if (t > 0.05) return t;
    }
    const seek = $("ed-seek");
    if (seek && ed.src.duration > 0) {
      return (Number(seek.value) / 1000) * ed.src.duration;
    }
    return ed.playhead || 0;
  }

  function setMark(which) {
    if (!ed.src) return;
    const t = snapTime(currentPreviewTime());
    if (which === "in") {
      ed.inSec = t;
      if ($("ed-in")) $("ed-in").value = fmt(t);
    } else {
      ed.outSec = t;
      if ($("ed-out")) $("ed-out").value = fmt(t);
    }
    syncMarkLabels();
    renderSourceRuler();
  }

  function addSegmentFromMarks() {
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const { start, end } = readMarks();
    if (!(end > start)) {
      setStatus(tt("Out muss nach In liegen."), true);
      return;
    }
    const opts = audioOpts();
    pushHist();
    const clip = defaultClip({
      path: ed.src.path, name: ed.src.name,
      start: round2(start), end: round2(end),
      title: `Clip ${ed.segments.length + 1}`,
      audio_index: opts.audio_index, audio_indexes: opts.audio_indexes.slice(),
      mute: opts.mute,
      sub_index: opts.sub_index, sub_indexes: (opts.sub_indexes || []).slice(),
      burn_subs: opts.burn_subs,
    });
    ed.segments.push(clip);
    ed.activeId = clip.id;
    renderSegList();
    flashClip(clip.id);
    setStatus(tt("Bereich behalten – du kannst weitere Bereiche markieren."));
  }

  function keepWholeFile() {
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    if ($("ed-in")) $("ed-in").value = "0";
    if ($("ed-out")) $("ed-out").value = String(round2(ed.src.duration));
    pushHist();
    const neu = replaceSourceSegments(ed.src.path, ed.src.name, [{ start: 0, end: ed.src.duration }]);
    if (neu[0]) ed.activeId = neu[0].id;
    renderSegList();
    if (neu[0]) flashClip(neu[0].id);
    setStatus(tt("Ganze Datei als Clip übernommen."));
  }

  function appendWholeSource() {
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const opts = audioOpts();
    pushHist();
    ed.segments.push(defaultClip({
      path: ed.src.path, name: ed.src.name,
      start: 0, end: round2(ed.src.duration),
      title: ed.src.name,
      audio_index: opts.audio_index, audio_indexes: opts.audio_indexes.slice(),
      mute: opts.mute,
      sub_index: opts.sub_index, sub_indexes: (opts.sub_indexes || []).slice(),
      burn_subs: opts.burn_subs,
    }));
    renderSegList();
    setStatus(tt("Quelle an Timeline angehängt."));
  }

  /** Mehrere Dateien in der gewählten Reihenfolge als ganze Clips anhängen. */
  async function appendSources(files) {
    const list = (files || []).filter((f) => f && f.rel);
    if (!list.length) return;
    const needFirst = !ed.src;
    pushHist();
    const failed = [];
    let added = 0;
    for (let i = 0; i < list.length; i++) {
      const f = list[i];
      setStatus(`${tt("Analysiere")} ${i + 1}/${list.length}: ${f.name}`);
      let data = null;
      try {
        const r = await fetch(`/api/editor/probe?path=${encodeURIComponent(f.rel)}`);
        data = await r.json();
        if (!r.ok || data.error || !data.has_video) data = null;
      } catch (e) {
        data = null;
      }
      if (!data) {
        failed.push(f.name);
        continue;
      }
      const name = f.name || data.name || f.rel;
      const dur = Number(data.duration) || 0;
      const known = ed.assets[f.rel] || { strip: "", duration: 0 };
      setAsset(f.rel, Object.assign(
        { duration: dur || known.duration }, rateInfo(data)));
      ed.segments.push(defaultClip({
        path: f.rel,
        name,
        start: 0,
        end: round2(Number(data.duration) || 0),
        title: name,
        audio_indexes: (data.audio || []).map((_, i) => i),
        audio_index: (data.audio || []).length ? 0 : -1,
        mute: !(data.audio || []).length,
        sub_indexes: (data.subtitles || []).map((_, i) => i),
        sub_index: (data.subtitles || []).length ? 0 : -1,
      }));
      added += 1;
    }
    renderSegList();
    if (needFirst && added) {
      const first = ed.segments.find((s) => s.kind === "media" && s.path);
      if (first) await setActive(first.id);
    }
    const msg = `${added} ${tt("Clip(s) angehängt.")}`;
    if (failed.length) {
      setStatus(`${msg} ${tt("Übersprungen")}: ${failed.join(", ")}`, true);
    } else {
      setStatus(msg);
    }
  }

  function cutOutRange() {
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const { start, end } = readMarks();
    if (!(end > start)) {
      setStatus(tt("Out muss nach In liegen."), true);
      return;
    }
    let keeps = keepsForSource(ed.src.path);
    if (!keeps.length) keeps = [{ start: 0, end: ed.src.duration }];
    const next = subtractRange(keeps, start, end);
    if (!next.length) {
      setStatus(tt("Nach dem Entfernen bleibt nichts übrig."), true);
      return;
    }
    pushHist();
    const neu = replaceSourceSegments(ed.src.path, ed.src.name, next);
    if (neu[0]) ed.activeId = neu[0].id;
    renderSegList();
    neu.forEach((c) => flashClip(c.id));
    setStatus(tt("Bereich entfernt.") + ` ${next.length} ` + tt("Clip(s) verbleiben – weitere Bereiche kannst du erneut entfernen."));
  }

  function splitAtPlayhead() {
    if (!ed.src) return;
    const t = snapTime(currentPreviewTime());
    const fits = (s) => (
      s && s.kind === "media" && s.path === ed.src.path
      && t > s.start + 0.05 && t < s.end - 0.05
    );
    const act = activeClip();
    const idx = fits(act) ? ed.segments.indexOf(act) : ed.segments.findIndex(fits);
    if (idx < 0) {
      setStatus(tt("Playhead liegt in keinem Clip dieser Quelle."), true);
      return;
    }
    pushHist();
    const s = ed.segments[idx];
    const base = clipLabel(s).replace(/ [ab]$/, "");
    const right = defaultClip(Object.assign({}, s, {
      id: uid(), start: round2(t), title: `${base} b`,
    }));
    s.end = round2(t);
    s.title = `${base} a`;
    ed.segments.splice(idx + 1, 0, right);
    ed.activeId = right.id;
    renderSegList();
    flashClip(right.id);
    setStatus(`${tt("Clip geteilt bei")} ${fmt(t)}`);
  }

  function deleteActive() {
    const s = activeClip();
    if (!s) return;
    const idx = ed.segments.indexOf(s);
    pushHist();
    ed.segments.splice(idx, 1);
    const next = ed.segments[Math.min(idx, ed.segments.length - 1)];
    ed.activeId = next ? next.id : null;
    renderSegList();
    setStatus(`${clipLabel(s)} ${tt("gelöscht.")}`);
  }

  function moveActive(dir) {
    const s = activeClip();
    if (!s) return;
    const idx = ed.segments.indexOf(s);
    const to = idx + dir;
    if (to < 0 || to >= ed.segments.length) return;
    pushHist();
    ed.segments.splice(idx, 1);
    ed.segments.splice(to, 0, s);
    renderSegList();
    flashClip(s.id);
  }

  function duplicateActive() {
    const s = activeClip();
    if (!s) return;
    pushHist();
    const copy = defaultClip(Object.assign({}, s, {
      id: uid(), title: `${clipLabel(s)} (2)`,
    }));
    ed.segments.splice(ed.segments.indexOf(s) + 1, 0, copy);
    ed.activeId = copy.id;
    renderSegList();
    flashClip(copy.id);
    setStatus(tt("Clip dupliziert."));
  }

  /** Clip-Grenze des aktiven Clips auf die aktuelle Vorschauposition ziehen. */
  function trimActive(which) {
    const s = activeClip();
    if (!s || s.kind !== "media" || !ed.src || ed.src.path !== s.path) return;
    const t = snapTime(currentPreviewTime());
    if (which === "in") {
      if (t >= s.end - 0.05) {
        setStatus(tt("Anfang muss vor dem Ende liegen."), true);
        return;
      }
      pushHist();
      s.start = round2(t);
    } else {
      if (t <= s.start + 0.05) {
        setStatus(tt("Ende muss nach dem Anfang liegen."), true);
        return;
      }
      pushHist();
      s.end = round2(t);
    }
    if ($("ed-in")) $("ed-in").value = String(round2(s.start));
    if ($("ed-out")) $("ed-out").value = String(round2(s.end));
    renderSegList();
    flashClip(s.id);
    setStatus(`${clipLabel(s)}: ${fmt(s.start)} → ${fmt(s.end)}`);
  }

  function addBlack(sec) {
    pushHist();
    ed.segments.push(defaultClip({
      kind: "black", name: tt("Schwarz"), title: `${tt("Schwarz")} ${sec}s`,
      start: 0, end: Number(sec) || 3, mute: true,
      audio_index: -1, audio_indexes: [],
    }));
    renderSegList();
    setStatus(tt("Schwarzclip eingefügt (Encode)."));
  }

  function insertTemplate(which) {
    let rec = null;
    try { rec = JSON.parse(localStorage.getItem(which === "intro" ? "edIntro" : "edOutro") || "null"); } catch (e) { rec = null; }
    if (!rec || !rec.path) {
      setStatus(tt("Zuerst aktuelle Quelle als Intro/Outro merken."), true);
      return;
    }
    pushHist();
    const clip = defaultClip({
      path: rec.path, name: rec.name, start: 0, end: rec.duration || rec.end || 0,
      title: which === "intro" ? "Intro" : "Outro",
    });
    if (which === "intro") ed.segments.unshift(clip);
    else ed.segments.push(clip);
    renderSegList();
  }

  function rememberTemplate(which) {
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const rec = { path: ed.src.path, name: ed.src.name, duration: ed.src.duration };
    try { localStorage.setItem(which === "intro" ? "edIntro" : "edOutro", JSON.stringify(rec)); } catch (e) { /* ignore */ }
    setStatus(which === "intro" ? tt("Intro gespeichert.") : tt("Outro gespeichert."));
  }

  function saveProject() {
    const blob = new Blob([JSON.stringify({
      v: 2,
      segments: ed.segments,
      overlays: ed.overlays,
      src: ed.src && { path: ed.src.path, name: ed.src.name },
    }, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "editor-projekt.json";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function loadProjectFile(file) {
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!data || !Array.isArray(data.segments)) throw new Error("Ungültiges Projekt");
      pushHist();
      ed.segments = data.segments.map((s) => defaultClip(s));
      ed.overlays = Array.isArray(data.overlays) ? data.overlays.map((o) => defaultOverlay(o)) : [];
      renderSegList();
      if (data.src && data.src.path) await loadSource(data.src.path, data.src.name);
      setStatus(tt("Projekt geladen."));
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  function stopTlPreview() {
    if (ed.playTl && ed.playTl.timer) clearTimeout(ed.playTl.timer);
    ed.playTl = null;
    const btn = $("ed-play-tl");
    if (btn) btn.textContent = `▶ ${tt("Timeline abspielen")}`;
    const pv = $("ed-prog-video");
    if (pv && !pv.paused) pv.pause();
  }

  function followEnabled() {
    const el = $("ed-follow");
    return el ? !!el.checked : true;
  }

  /** Clip anspielen: Quelle laden falls nötig, an die Clipzeit springen, im Programm-Monitor + Quell-Monitor starten. */
  async function playClip(s, opts) {
    opts = opts || {};
    const v = $("ed-video");
    const pv = $("ed-prog-video");
    if (!v || !s) return;
    ed.inClip = false;
    ed.advancing = true;
    try {
      if (s.kind !== "media" || !s.path) {
        // Schwarzclip hat keine Quelle – Bild aus und die Dauer abwarten.
        ed.activeId = s.id;
        markActive();
        v.pause();
        if (pv) { pv.pause(); pv.removeAttribute("src"); pv.dataset.clipPath = ""; }
        v.removeAttribute("src");
        ed.streamUrl = "";
        if (ed.playTl) {
          ed.playTl.timer = setTimeout(() => {
            if (ed.playTl) gotoNextClip();
          }, Math.max(0.2, clipDur(s)) * 1000);
        }
        return;
      }
      const t = opts.srcTime != null ? opts.srcTime : s.start;
      await setActive(s.id, { srcTime: t });
      if (pv) {
        if (pv.dataset.clipPath !== s.path) {
          pv.dataset.clipPath = s.path;
          pv.src = fileUrl(s.path);
          pv.load();
        }
        seekFileTo(pv, t);
        pv.muted = false;
        v.muted = true; // Ton kommt aus dem Programm-Monitor, kein Echo
        pv.play().catch(() => {});
      }
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    } finally {
      ed.lastAdvance = Date.now();
      ed.advancing = false;
    }
  }

  /** Am Clipende weiter: nächster Clip oder Ende der Timeline. */
  async function gotoNextClip() {
    if (ed.advancing) return;
    const s = activeClip();
    const idx = s ? ed.segments.indexOf(s) : -1;
    const next = idx >= 0 ? ed.segments[idx + 1] : null;
    const wasTl = !!ed.playTl;
    if (!next) {
      const v = $("ed-video");
      if (v) v.pause();
      stopTlPreview();
      if (wasTl || s) setStatus(tt("Ende der Timeline erreicht."));
      return;
    }
    await playClip(next);
    setStatus(`${tt("Clip")} ${ed.segments.indexOf(next) + 1}/${ed.segments.length}: `
      + clipLabel(next));
  }

  // timeupdate feuert nur ~4×/s – zu grob für saubere Schnittkanten.
  function startFollowTimer() {
    if (ed.followTimer) return;
    ed.followTimer = setInterval(tlFollowTick, 60);
  }

  function stopFollowTimer() {
    if (ed.followTimer) clearInterval(ed.followTimer);
    ed.followTimer = null;
  }

  /** Läuft während der Wiedergabe: hält sie innerhalb des aktiven Clips. */
  function tlFollowTick() {
    if (ed.advancing) return;
    if (!ed.playTl && !followEnabled()) return;
    const s = activeClip();
    if (!s || s.kind !== "media" || !ed.src || ed.src.path !== s.path) return;
    const v = $("ed-video");
    if (!v || v.paused || v.ended) return;
    const t = currentPreviewTime();
    if (t < s.start - 0.05) { ed.inClip = false; return; }
    if (t < s.end - 0.04) { ed.inClip = true; return; }
    // Weit hinter dem Clip: der Nutzer sichtet frei, nicht automatisch springen.
    if (t > s.end + 1.5) { ed.inClip = false; return; }
    if (!ed.inClip) return;
    if (Date.now() - (ed.lastAdvance || 0) < 250) return;
    gotoNextClip();
  }

  async function playTimeline() {
    if (!ed.segments.length) return;
    const v = $("ed-video");
    if (!v) return;
    if (ed.playTl) {
      stopTlPreview();
      v.pause();
      v.muted = previewAudioIndex() < 0;
      setStatus(tt("Timeline-Vorschau gestoppt."));
      return;
    }
    ed.playTl = { timer: null };
    const btn = $("ed-play-tl");
    if (btn) btn.textContent = `⏸ ${tt("Timeline stoppen")}`;
    const act = activeClip();
    const first = act || ed.segments[0];
    const t = currentPreviewTime();
    const inside = !!(act && act.kind === "media" && ed.src && ed.src.path === act.path
      && t > act.start + 0.05 && t < act.end - 0.1);
    setStatus(tt("Timeline-Vorschau läuft im Programm-Monitor – Schnitte werden übersprungen."));
    await playClip(first, inside ? { srcTime: t } : {});
  }

  function currentUploadDest() {
    const sel = $("ed-upload-dest");
    return sel ? (sel.value || "upload") : (ed.uploadDest || "upload");
  }

  function rememberUploadDest(val) {
    ed.uploadDest = val || "upload";
    try { localStorage.setItem("edUploadDest", ed.uploadDest); } catch (e) { /* ignore */ }
  }

  function ensureDestOption(value, label) {
    const sel = $("ed-upload-dest");
    if (!sel || !value) return;
    if ([...sel.options].some((o) => o.value === value)) { sel.value = value; return; }
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label || value;
    sel.appendChild(o);
    sel.value = value;
  }

  async function fillUploadDestOptions() {
    const sel = $("ed-upload-dest");
    if (!sel) return;
    const saved = (() => {
      try { return localStorage.getItem("edUploadDest") || "upload"; } catch (e) { return "upload"; }
    })();
    sel.innerHTML = "";
    const add = (value, label) => {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = label;
      sel.appendChild(o);
    };
    add("upload", tt("Standard-Upload (/data/uploads)"));
    const defOut = (window.APP_CONFIG && window.APP_CONFIG.defaultOutput) || "output";
    if (defOut) add(defOut, `${tt("Standard-Ausgabe")}: ${defOut}`);
    try {
      const libs = await (await fetch("/api/libraries")).json();
      (libs.libraries || []).forEach((lib) => {
        if (lib.path) add(lib.path, `${tt("Unterbibliothek")}: ${lib.name || lib.path}`);
      });
    } catch (e) { /* ignore */ }
    if (saved && saved !== "upload" && ![...sel.options].some((o) => o.value === saved)) {
      add(saved, `${tt("Ordner")}: ${saved}`);
    }
    if ([...sel.options].some((o) => o.value === saved)) sel.value = saved;
    else sel.value = "upload";
    ed.uploadDest = sel.value;
  }

  async function uploadFile(file) {
    if (!file) return;
    const dest = currentUploadDest();
    rememberUploadDest(dest);
    const st = $("ed-upload-status");
    if (st) st.textContent = `${tt("Upload")} → ${dest === "upload" ? "uploads" : dest} … ${file.name}`;
    setStatus(tt("Upload läuft …"));
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("dest", dest);
    try {
      const r = await fetch("/api/editor/upload", { method: "POST", body: fd });
      const data = await r.json();
      if (!r.ok || data.error) throw new Error(data.error || "Upload fehlgeschlagen");
      if (st) {
        st.textContent = `${tt("Hochgeladen")}: ${data.name}`
          + (data.dest && data.dest !== "upload" ? ` → ${data.dest}` : " → uploads");
      }
      await loadSource(data.path, data.name);
      setStatus(tt("Upload bereit – Bereiche behalten oder entfernen."));
    } catch (e) {
      if (st) st.textContent = "";
      setStatus(String(e.message || e), true);
    }
  }

  function syncModeUI() {
    const need = clipsNeedEncode();
    const modeEl = $("ed-mode");
    if (need && modeEl && (modeEl.value === "remux" || modeEl.value === "smart")) {
      modeEl.value = "encode";
    }
    const mode = (modeEl && modeEl.value) || "remux";
    const enc = $("ed-encode-opts");
    if (enc) enc.style.display = mode === "encode" ? "" : "none";
    const xf = Number(($("ed-crossfade") || {}).value) || 0;
    const tf = $("ed-transition-field");
    if (tf) tf.style.display = xf > 0.01 ? "" : "none";
    const hint = $("ed-mode-hint");
    if (hint) {
      if (mode === "encode") {
        hint.textContent = tt("Encode schneidet framegenau, unterstützt Fades, B-Roll (V2) sowie Filter und behält 10-Bit/HDR bei.");
      } else if (mode === "smart") {
        hint.textContent = tt("Smart Cut encodiert nur wenige Sekunden an angeschnittenen GOP-Rändern und kopiert den Hauptteil verlustfrei.");
      } else {
        hint.textContent = tt("Remux kopiert Streams ohne Neucodierung. Schnitte liegen am nächsten Keyframe.");
      }
    }
    updateKfDeltaBar();
    syncRateUI();
    syncAudioUI(mode);
    syncNameUI();
  }

  function syncRateUI() {
    const mode = ($("ed-rate-mode") && $("ed-rate-mode").value) || "cq";
    const br = mode === "bitrate" || mode === "abr";
    const cqf = $("ed-cq-field");
    const vbf = $("ed-vbr-field");
    if (cqf) cqf.style.display = br ? "none" : "";
    if (vbf) vbf.style.display = br ? "" : "none";
  }

  function audioCopyPossible() {
    if (Number(($("ed-crossfade") || {}).value) > 0.01) return false;
    return !ed.segments.some((s) => (
      s.kind === "black" || s.kind === "silence"
      || Number(s.fade_in) > 0 || Number(s.fade_out) > 0
      || Math.abs((Number(s.speed) || 1) - 1) > 0.001
      || Math.abs(Number(s.volume_db) || 0) > 0.05 || s.loudnorm
    ));
  }

  function syncAudioUI(mode) {
    const sel = $("ed-acodec");
    if (!sel) return;
    const encMode = (mode || ($("ed-mode") && $("ed-mode").value) || "remux") === "encode";
    const copyOk = !encMode || audioCopyPossible();
    const copyOpt = [...sel.options].find((o) => o.value === "copy");
    if (copyOpt) copyOpt.disabled = encMode && !copyOk;
    if (encMode && sel.value === "copy" && !copyOk) sel.value = "flac";
    if (!encMode) sel.value = "copy";
    const copy = sel.value === "copy";
    const abrf = $("ed-abr-field");
    const chf = $("ed-achannels-field");
    if (abrf) abrf.style.display = copy || sel.value === "flac" ? "none" : "";
    if (chf) chf.style.display = copy ? "none" : "";
    const hint = $("ed-audio-hint");
    if (!hint) return;
    if (!encMode || copy) {
      hint.textContent = tt("Angehakte Ton- und Untertitelspuren werden unverändert übernommen (Copy).");
    } else if (sel.value === "flac") {
      hint.textContent = tt("Video wird neu encodiert, Ton verlustlos als FLAC. Untertitel bleiben Copy.");
    } else {
      hint.textContent = tt("Video und Ton werden neu encodiert. Untertitel bleiben Copy, sofern gewählt.");
    }
  }

  function nameModeCustom() {
    return ($("ed-name-mode") && $("ed-name-mode").value) === "custom";
  }

  function syncNameUI() {
    const custom = nameModeCustom();
    const sr = $("ed-name-suffix-row");
    const cr = $("ed-name-custom-row");
    if (sr) sr.style.display = custom ? "none" : "";
    if (cr) cr.style.display = custom ? "" : "none";
    const prev = $("ed-name-preview");
    if (!prev) return;
    const ext = "." + (($("ed-container") && $("ed-container").value) || "mkv");
    if (custom) {
      const raw = (($("ed-out-name") && $("ed-out-name").value) || "").trim();
      prev.textContent = raw
        ? `${tt("Ausgabedatei")}: ${raw}${ext}`
        : tt("Eigener Name ist leer – es gilt Quellname + Suffix.");
      return;
    }
    const first = ed.segments.find((s) => s.path);
    const base = first ? String(first.path).split("/").pop().replace(/\.[^.]+$/, "")
      : tt("Quelle");
    const sfx = (($("ed-suffix") && $("ed-suffix").value) || "_edit");
    prev.textContent = `${tt("Ausgabedatei")}: ${base}${sfx}${ext}`;
  }

  async function enqueue() {
    if (!ed.segments.length) return;
    const mode = ($("ed-mode") && $("ed-mode").value) || "remux";
    const hdrMode = ($("ed-hdr-mode") && $("ed-hdr-mode").value) || "keep";
    const exportSplit = (($("ed-export-split") && $("ed-export-split").value) || "merge") === "split";
    const bgmPath = (($("ed-bgm-path") && $("ed-bgm-path").value) || ed.bgmPath || "").trim();
    const payload = {
      segments: ed.segments.map((s) => ({
        path: s.path, kind: s.kind || "media",
        start: s.start, end: s.end, title: s.title,
        audio_index: s.audio_index,
        audio_indexes: Array.isArray(s.audio_indexes) ? s.audio_indexes : undefined,
        mute: !!s.mute,
        sub_index: s.sub_index,
        sub_indexes: Array.isArray(s.sub_indexes) ? s.sub_indexes : undefined,
        burn_subs: !!s.burn_subs,
        fade_in: Number(s.fade_in) || 0, fade_out: Number(s.fade_out) || 0,
        speed: Number(s.speed) || 1, crop: s.crop || "",
        delogo: s.delogo || "",
        text_overlay: s.text_overlay || "",
        text_pos: s.text_pos || "bottom",
        look_preset: s.look_preset || "none",
        voice_enhance: !!s.voice_enhance,
        reverse: !!s.reverse,
        scale: Number(s.scale) || 0,
        rotate: Number(s.rotate) || 0,
        hflip: !!s.hflip,
        vflip: !!s.vflip,
        volume_db: Number(s.volume_db) || 0,
        loudnorm: !!s.loudnorm,
      })),
      overlays: ed.overlays.map((o) => ({
        path: o.path,
        title: o.title || o.name || "B-Roll",
        start: Number(o.start) || 0,
        end: Number(o.end) || 5,
        tl_start: Number(o.tl_start) || 0,
        position: o.position || "pip_br",
        size_pct: Number(o.size_pct) || 30,
        opacity: o.opacity != null ? Number(o.opacity) : 1,
        fade: Number(o.fade) || 0,
      })),
      mode,
      export_split: exportSplit,
      bgm_path: bgmPath,
      bgm_volume_db: Number(($("ed-bgm-vol") || {}).value || -12),
      bgm_ducking: !($("ed-bgm-duck") && !$("ed-bgm-duck").checked),
      pad_mode: ($("ed-pad-mode") && $("ed-pad-mode").value) || "black",
      aspect_mode: ($("ed-aspect-mode") && $("ed-aspect-mode").value) || "source",
      container: ($("ed-container") && $("ed-container").value) || "mkv",
      suffix: ($("ed-suffix") && $("ed-suffix").value) || "_edit",
      name_mode: nameModeCustom() ? "custom" : "suffix",
      out_name: (($("ed-out-name") && $("ed-out-name").value) || "").trim(),
      chapters_from_cuts: !!($("ed-chapters") && $("ed-chapters").checked),
      force_remux: !!($("ed-force") && $("ed-force").checked),
      platform: ($("ed-platform") && $("ed-platform").value) || "cpu",
      codec: ($("ed-codec") && $("ed-codec").value) || "av1",
      encoder_speed: ($("ed-enc-speed") && $("ed-enc-speed").value) || "balanced",
      b_frames: ($("ed-b-frames") && $("ed-b-frames").value) || "auto",
      nvenc_tune: ($("ed-nvenc-tune") && $("ed-nvenc-tune").value) || "auto",
      aq_strength: ($("ed-aq-strength") && parseInt($("ed-aq-strength").value, 10)) || 8,
      keyint_sec: ($("ed-keyint") && parseInt($("ed-keyint").value, 10)) || 0,
      cq: ($("ed-cq") && parseInt($("ed-cq").value, 10)) || 30,
      rate_mode: ($("ed-rate-mode") && $("ed-rate-mode").value) || "cq",
      v_bitrate: ($("ed-vbr") && parseInt($("ed-vbr").value, 10)) || 0,
      audio_codec: ($("ed-acodec") && $("ed-acodec").value) || "aac",
      audio_bitrate: ($("ed-abr") && parseInt($("ed-abr").value, 10)) || 192,
      audio_channels: ($("ed-achannels") && parseInt($("ed-achannels").value, 10)) || 0,
      out_mode: ($("ed-out-mode") && $("ed-out-mode").value) || "default",
      out_subdir: ($("ed-out-subdir") && $("ed-out-subdir").value) || "",
      crossfade: Number(($("ed-crossfade") || {}).value) || 0,
      transition: ($("ed-transition") && $("ed-transition").value) || "fade",
      tonemap: hdrMode === "tonemap",
      preserve_hdr: hdrMode !== "tonemap",
      post_processing: "keep",
    };
    if ((mode === "remux" || mode === "smart") && !payload.force_remux) {
      try {
        const chk = await (await fetch("/api/editor/check", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ segments: payload.segments }),
        })).json();
        if (chk.error) throw new Error(chk.error);
        if (chk.compatible === false) {
          const wrap = $("ed-force-wrap");
          if (wrap) wrap.style.display = "";
          if (chk.needs_encode) {
            if ($("ed-mode")) $("ed-mode").value = "encode";
            syncModeUI();
            setStatus(tt("Encode nötig (Fades/Tempo/Schwarz/UT). Modus umgestellt – erneut einreihen."), true);
            return;
          }
          setStatus(
            tt("Quellen nicht kompatibel für Remux.") + " "
            + (chk.warnings || []).join("; ")
            + " — " + tt("Encode wählen oder Remux erzwingen."),
            true,
          );
          return;
        }
      } catch (e) {
        setStatus(String(e.message || e), true);
        return;
      }
    }
    setStatus(tt("Reihe ein …"));
    try {
      const r = await fetch("/api/editor/enqueue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok || data.error) throw new Error(data.error || "Enqueue fehlgeschlagen");
      setStatus(`${tt("In Warteschlange")}: ${data.id} · ${fmt(data.duration || totalDur())}`);
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  function stepFrame(dir) {
    if (!ed.src) return;
    const fps = ed.src.fps > 1 ? ed.src.fps : 25;
    seekSoft(currentPreviewTime() + dir / fps, { snap: false });
  }

  /** Feinsprung in Sekunden (Buttons ±0,2 s) – ohne Einrasten. */
  function jumpBy(sec) {
    if (!ed.src) return;
    seekSoft(currentPreviewTime() + Number(sec || 0), { snap: false });
  }

  function renderChapterChips() {
    const bar = $("ed-chap-bar");
    const chips = $("ed-chap-chips");
    if (!bar || !chips) return;
    const chaps = (ed.src && ed.src.chapters) || [];
    if (!chaps.length) {
      bar.hidden = true;
      chips.innerHTML = "";
      return;
    }
    bar.hidden = false;
    chips.innerHTML = "";
    chaps.forEach((c, idx) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ed-chap-chip";
      const start = Number(c.start) || 0;
      const end = Number(c.end) || (chaps[idx + 1] ? Number(chaps[idx + 1].start) : ed.src.duration) || start;
      b.textContent = `${idx + 1}. ${c.title || tt("Kapitel")} (${fmt(start)})`;
      b.title = `${fmt(start)} → ${fmt(end)} · Klick = Bereich setzen`;
      b.addEventListener("click", () => {
        ed.inSec = round2(start);
        ed.outSec = round2(end);
        if ($("ed-in")) $("ed-in").value = fmt(ed.inSec);
        if ($("ed-out")) $("ed-out").value = fmt(ed.outSec);
        syncMarkLabels();
        seekSoft(ed.inSec, { snap: false });
        renderSourceRuler();
        setStatus(`${tt("Kapitel gewählt")}: ${c.title || idx + 1} (${fmt(ed.inSec)} → ${fmt(ed.outSec)})`);
      });
      chips.appendChild(b);
    });
  }

  function chaptersToClips() {
    if (!ed.src || !(ed.src.chapters || []).length) return;
    const chaps = ed.src.chapters;
    const opts = audioOpts();
    pushHist();
    const neu = chaps.map((c, idx) => {
      const start = round2(Number(c.start) || 0);
      const end = round2(Number(c.end) || (chaps[idx + 1] ? Number(chaps[idx + 1].start) : ed.src.duration) || start);
      return defaultClip({
        path: ed.src.path,
        name: ed.src.name,
        start,
        end: Math.max(start + 0.1, end),
        title: c.title || `Kapitel ${idx + 1}`,
        audio_index: opts.audio_index,
        audio_indexes: opts.audio_indexes.slice(),
        mute: opts.mute,
        sub_index: opts.sub_index,
        sub_indexes: (opts.sub_indexes || []).slice(),
        burn_subs: opts.burn_subs,
      });
    });
    ed.segments = neu;
    if (neu[0]) ed.activeId = neu[0].id;
    renderSegList();
    setStatus(`${neu.length} ${tt("Kapitel als Clips angelegt.")}`);
  }

  function beginSourceRulerDrag(ev) {
    if (ev.button !== 0 || !ed.src || !(ed.src.duration > 0)) return;
    const ruler = $("ed-src-ruler");
    if (!ruler) return;
    ev.preventDefault();
    const grip = ev.target.closest("[data-sel-grip]");
    const mode = grip ? grip.dataset.selGrip : "scrub";
    const rect = ruler.getBoundingClientRect();
    if (!(rect.width > 0)) return;
    const timeAt = (clientX) => {
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return snapTime(ratio * ed.src.duration);
    };
    const apply = (clientX, finalSeek) => {
      const t = timeAt(clientX);
      const cur = readMarks();
      if (mode === "in") {
        const nextIn = Math.min(t, Math.max(0, cur.end - 0.1));
        ed.inSec = round2(nextIn);
        if ($("ed-in")) $("ed-in").value = fmt(ed.inSec);
        syncMarkLabels();
        renderSourceRuler();
        if (finalSeek) seekSoft(ed.inSec, { snap: false });
      } else if (mode === "out") {
        const nextOut = Math.max(t, cur.start + 0.1);
        ed.outSec = round2(nextOut);
        if ($("ed-out")) $("ed-out").value = fmt(ed.outSec);
        syncMarkLabels();
        renderSourceRuler();
        if (finalSeek) seekSoft(ed.outSec, { snap: false });
      } else {
        updateTimeLabel(t, ed.src.duration);
        renderSourceRuler();
        if (finalSeek) seekPreview(t, { snap: false });
        else if (ed.previewMode === "file") seekSoft(t, { snap: false });
      }
    };
    apply(ev.clientX, false);
    const onMove = (e) => apply(e.clientX, false);
    const onUp = (e) => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      apply(e.clientX, true);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  function nearestKf(t) {
    if (!ed.keyframes || !ed.keyframes.length) return null;
    let best = ed.keyframes[0];
    let bestD = Math.abs(best - t);
    for (let i = 1; i < ed.keyframes.length; i++) {
      const d = Math.abs(ed.keyframes[i] - t);
      if (d < bestD) { bestD = d; best = ed.keyframes[i]; }
    }
    return { snapped: best, delta: best - t };
  }

  function updateKfDeltaBar() {
    const bar = $("ed-kf-bar");
    const txt = $("ed-kf-delta");
    if (!bar || !txt) return;
    const mode = ($("ed-mode") && $("ed-mode").value) || "remux";
    if (mode !== "remux" || !ed.src || !ed.keyframes || !ed.keyframes.length) {
      bar.hidden = true;
      return;
    }
    const { start, end } = readMarks();
    const kIn = nearestKf(start);
    const kOut = nearestKf(end);
    if (!kIn || !kOut || (Math.abs(kIn.delta) < 0.04 && Math.abs(kOut.delta) < 0.04)) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    const fmtD = (d) => `${d >= 0 ? "+" : ""}${d.toFixed(2)}s`;
    txt.textContent = `Keyframe-Abstand im Remux: In ${fmt(kIn.snapped)} (${fmtD(kIn.delta)}) · Out ${fmt(kOut.snapped)} (${fmtD(kOut.delta)})`;
  }

  function roundToKeyframes() {
    if (!ed.src || !ed.keyframes || !ed.keyframes.length) return;
    const { start, end } = readMarks();
    const kIn = nearestKf(start);
    const kOut = nearestKf(end);
    if (!kIn || !kOut) return;
    ed.inSec = round2(kIn.snapped);
    ed.outSec = round2(Math.max(ed.inSec + 0.1, kOut.snapped));
    if ($("ed-in")) $("ed-in").value = fmt(ed.inSec);
    if ($("ed-out")) $("ed-out").value = fmt(ed.outSec);
    const act = activeClip();
    if (act && act.kind === "media" && act.path === ed.src.path) {
      pushHist();
      act.start = ed.inSec;
      act.end = ed.outSec;
      renderSegList();
    } else {
      syncMarkLabels();
      renderSourceRuler();
    }
    setStatus(`${tt("Auf Keyframes gerundet")}: ${fmt(ed.inSec)} → ${fmt(ed.outSec)}`);
  }

  function videoContentRect() {
    const stage = $("ed-stage");
    if (!stage) return null;
    const sr = stage.getBoundingClientRect();
    const vw = (ed.src && ed.src.width) || 1920;
    const vh = (ed.src && ed.src.height) || 1080;
    const scale = Math.min(sr.width / vw, sr.height / vh);
    const dw = vw * scale;
    const dh = vh * scale;
    return {
      left: (sr.width - dw) / 2,
      top: (sr.height - dh) / 2,
      width: dw,
      height: dh,
      vw,
      vh,
      scale,
      stageRect: sr,
    };
  }

  function parseCropSpec(str, vw, vh) {
    const parts = String(str || "").split(":").map((x) => parseInt(x, 10));
    if (parts.length === 4 && parts.every((n) => Number.isFinite(n) && n >= 0) && parts[0] > 0 && parts[1] > 0) {
      return { w: Math.min(vw, parts[0]), h: Math.min(vh, parts[1]), x: Math.min(vw - 2, parts[2]), y: Math.min(vh - 2, parts[3]) };
    }
    return { x: Math.round(vw * 0.05), y: Math.round(vh * 0.05), w: Math.round(vw * 0.9), h: Math.round(vh * 0.9) };
  }

  function renderCropBox() {
    const ov = $("ed-crop-overlay");
    const box = $("ed-crop-box");
    const lab = $("ed-crop-label");
    if (!ov || !box || ov.hidden || !ed.cropBox) return;
    const vr = videoContentRect();
    if (!vr) return;
    const c = ed.cropBox;
    box.style.left = `${vr.left + c.x * vr.scale}px`;
    box.style.top = `${vr.top + c.y * vr.scale}px`;
    box.style.width = `${Math.max(16, c.w * vr.scale)}px`;
    box.style.height = `${Math.max(16, c.h * vr.scale)}px`;
    if (lab) lab.textContent = `${c.w}×${c.h} @ ${c.x},${c.y}`;
  }

  function toggleCropOverlay() {
    const ov = $("ed-crop-overlay");
    const bar = $("ed-crop-bar");
    if (!ov || !bar) return;
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const show = ov.hidden;
    ov.hidden = !show;
    bar.hidden = !show;
    if (show) {
      const vw = ed.src.width || 1920;
      const vh = ed.src.height || 1080;
      const act = activeClip();
      ed.cropBox = parseCropSpec(act && act.crop ? act.crop : "", vw, vh);
      renderCropBox();
    }
  }

  function applyCropRatio(ratioStr) {
    ed.cropRatio = ratioStr || "free";
    if (!ed.src || !ed.cropBox || ed.cropRatio === "free") return;
    const vw = ed.src.width || 1920;
    const vh = ed.src.height || 1080;
    const [rw, rh] = ed.cropRatio.split(":").map(Number);
    if (!(rw > 0 && rh > 0)) return;
    const target = rw / rh;
    let w = vw;
    let h = Math.round(w / target / 2) * 2;
    if (h > vh) {
      h = vh;
      w = Math.round(h * target / 2) * 2;
    }
    ed.cropBox = {
      w,
      h,
      x: Math.round((vw - w) / 4) * 2,
      y: Math.round((vh - h) / 4) * 2,
    };
    renderCropBox();
  }

  function applyVisualCrop() {
    if (!ed.cropBox || !ed.src) return;
    const c = ed.cropBox;
    const spec = `${c.w}:${c.h}:${c.x}:${c.y}`;
    let act = activeClip();
    pushHist();
    if (!act) {
      addSegmentFromMarks();
      act = activeClip();
    }
    if (act) {
      act.crop = spec;
      renderSegList();
      setStatus(`${tt("Crop übernommen")}: ${spec}`);
    }
    const ov = $("ed-crop-overlay");
    const bar = $("ed-crop-bar");
    if (ov) ov.hidden = true;
    if (bar) bar.hidden = true;
  }

  async function runAutoCrop() {
    const path = (activeClip() && activeClip().path) || (ed.src && ed.src.path);
    if (!path) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    setStatus(tt("Erkenne schwarze Balken (cropdetect) …"));
    try {
      const r = await fetch(`/api/editor/autocrop?path=${encodeURIComponent(path)}`);
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || "Auto-Crop fehlgeschlagen");
      if (!d.crop) {
        setStatus(tt("Keine schwarzen Ränder erkannt."));
        return;
      }
      if (ed.src) {
        ed.cropBox = parseCropSpec(d.crop, d.width || ed.src.width || 1920, d.height || ed.src.height || 1080);
        renderCropBox();
      }
      const act = activeClip();
      if (act && act.kind === "media") {
        pushHist();
        act.crop = d.crop;
        renderSegList();
      }
      setStatus(`${tt("Auto-Crop erkannt")}: ${d.crop}`);
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  function wireCropDrag() {
    const box = $("ed-crop-box");
    if (!box) return;
    box.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0 || !ed.cropBox) return;
      ev.preventDefault();
      ev.stopPropagation();
      const vr = videoContentRect();
      if (!vr || !(vr.scale > 0)) return;
      const handle = ev.target.closest("[data-h]");
      const mode = handle ? handle.dataset.h : "move";
      const startX = ev.clientX;
      const startY = ev.clientY;
      const orig = Object.assign({}, ed.cropBox);
      const onMove = (e) => {
        const dx = Math.round((e.clientX - startX) / vr.scale / 2) * 2;
        const dy = Math.round((e.clientY - startY) / vr.scale / 2) * 2;
        const c = Object.assign({}, orig);
        if (mode === "move") {
          c.x = Math.max(0, Math.min(vr.vw - c.w, orig.x + dx));
          c.y = Math.max(0, Math.min(vr.vh - c.h, orig.y + dy));
        } else {
          if (mode.indexOf("w") >= 0) {
            const nx = Math.max(0, Math.min(orig.x + orig.w - 32, orig.x + dx));
            c.w = orig.w + (orig.x - nx);
            c.x = nx;
          }
          if (mode.indexOf("e") >= 0) {
            c.w = Math.max(32, Math.min(vr.vw - orig.x, orig.w + dx));
          }
          if (mode.indexOf("n") >= 0) {
            const ny = Math.max(0, Math.min(orig.y + orig.h - 32, orig.y + dy));
            c.h = orig.h + (orig.y - ny);
            c.y = ny;
          }
          if (mode.indexOf("s") >= 0) {
            c.h = Math.max(32, Math.min(vr.vh - orig.y, orig.h + dy));
          }
        }
        ed.cropBox = c;
        renderCropBox();
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  async function runAutoDetect(kind) {
    if (!ed.src || !ed.src.path) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const { start, end } = readMarks();
    const labelMap = { black: "Schwarzbilder", silence: "Sprechpausen/Stille", scene: "Szenenwechsel" };
    setStatus(`${tt("Analysiere")} ${labelMap[kind] || kind} …`);
    try {
      const r = await fetch("/api/editor/detect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: ed.src.path,
          kind,
          start: start > 0 ? start : 0,
          end: end > start ? end : ed.src.duration,
          min_duration: kind === "scene" ? 1.5 : 0.5,
        }),
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || "Erkennung fehlgeschlagen");
      const keeps = d.keeps || [];
      if (keeps.length <= 1 && !(d.gaps || []).length && !(d.scenes || []).length) {
        setStatus(`Keine ${labelMap[kind] || "Schnitte"} im gewählten Bereich gefunden.`);
        return;
      }
      pushHist();
      const opts = audioOpts();
      const neu = keeps.map((k, idx) => defaultClip({
        path: ed.src.path,
        name: ed.src.name,
        start: round2(k.start),
        end: round2(k.end),
        title: k.title || `Clip ${idx + 1}`,
        audio_index: opts.audio_index,
        audio_indexes: opts.audio_indexes.slice(),
        mute: opts.mute,
        sub_index: opts.sub_index,
        sub_indexes: (opts.sub_indexes || []).slice(),
        burn_subs: opts.burn_subs,
      }));
      ed.segments = neu;
      if (neu[0]) ed.activeId = neu[0].id;
      renderSegList();
      setStatus(`${neu.length} Clips aus ${labelMap[kind] || kind}-Erkennung angelegt.`);
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  function addBrollV2() {
    const placeOverlay = (path, name, start, end) => {
      pushHist();
      const tlNow = Math.max(0, currentTimelineTime());
      const ov = defaultOverlay({
        path,
        name,
        title: name || "B-Roll",
        start: round2(start),
        end: round2(Math.max(start + 0.5, end)),
        tl_start: round2(tlNow),
        position: "pip_br",
        size_pct: 30,
        opacity: 1,
        fade: 0.3,
      });
      ed.overlays.push(ov);
      ed.activeV2Id = ov.id;
      renderSegList();
      syncV2Preview();
      setStatus(`B-Roll '${ov.title}' auf Spur V2 bei ${fmt(ov.tl_start)} eingefügt.`);
    };
    if (ed.src && ed.src.path) {
      const { start, end } = readMarks();
      const dur = end > start ? Math.min(end - start, 15) : Math.min(ed.src.duration || 5, 8);
      placeOverlay(ed.src.path, ed.src.name, start, start + dur);
      return;
    }
    if (typeof window.openFilePickerModal === "function") {
      window.openFilePickerModal({
        title: tt("B-Roll / PiP Video für Spur V2 wählen"),
        rememberKey: "edPickDir",
        onPick: async (f) => {
          try {
            const r = await fetch(`/api/editor/probe?path=${encodeURIComponent(f.rel)}`);
            const d = await r.json();
            const dur = Math.min(Number(d.duration) || 8, 12);
            placeOverlay(f.rel, f.name, 0, dur);
          } catch (e) {
            placeOverlay(f.rel, f.name, 0, 5);
          }
        },
      });
    }
  }

  async function previewTransitionCut() {
    if (!ed.segments.length) return;
    const act = activeClip();
    let cutIdx = act ? ed.segments.indexOf(act) : 0;
    if (cutIdx >= ed.segments.length - 1 && ed.segments.length >= 2) cutIdx = ed.segments.length - 2;
    const xf = Number(($("ed-crossfade") || {}).value) || 0;
    const trans = ($("ed-transition") && $("ed-transition").value) || "fade";
    setStatus(tt("Rendere 5-Sekunden-Übergangsvorschau …"));
    try {
      const r = await fetch("/api/editor/preview-transition", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          segments: ed.segments,
          overlays: ed.overlays,
          cut_index: Math.max(0, cutIdx),
          crossfade: xf,
          transition: trans,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.error || "Übergangsvorschau fehlgeschlagen");
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const pv = $("ed-prog-video") || $("ed-video");
      if (pv) {
        pv.dataset.clipPath = "";
        pv.src = url;
        pv.muted = false;
        pv.play().catch(() => {});
      }
      setStatus(tt("Übergangsvorschau wird im Programm-Monitor abgespielt."));
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  /** 1-Klick: Zum vorherigen/nächsten Keyframe in der Quelle springen. */
  function jumpKeyframe(dir) {
    if (!ed.src || !ed.keyframes || !ed.keyframes.length) {
      setStatus(tt("Keine Keyframes geladen."), true);
      return;
    }
    const cur = currentPreviewTime();
    let target = null;
    if (dir < 0) {
      for (let i = ed.keyframes.length - 1; i >= 0; i--) {
        if (ed.keyframes[i] < cur - 0.08) { target = ed.keyframes[i]; break; }
      }
      if (target == null) target = ed.keyframes[0];
    } else {
      for (let i = 0; i < ed.keyframes.length; i++) {
        if (ed.keyframes[i] > cur + 0.08) { target = ed.keyframes[i]; break; }
      }
      if (target == null) target = ed.keyframes[ed.keyframes.length - 1];
    }
    seekSoft(target, { snap: false });
    setStatus(`${tt("Keyframe")}: ${fmt(target)}`);
  }

  /** 1-Klick: Aktuellen Video-Frame als PNG herunterladen. */
  function takeSnapshot() {
    if (!ed.src || !ed.src.path) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const t = Math.max(0, currentPreviewTime());
    const url = `/api/editor/snapshot?path=${encodeURIComponent(ed.src.path)}&t=${encodeURIComponent(t.toFixed(3))}`;
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setStatus(`${tt("Einzelbild gespeichert")} @ ${fmt(t)}`);
  }

  /** 1-Klick: Alles vor dem Playhead wegschneiden (Ripple Trim Start). */
  function cutBeforePlayhead() {
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const t = snapTime(currentPreviewTime());
    if (t <= 0.05 || t >= ed.src.duration - 0.05) return;
    ed.inSec = round2(t);
    if ($("ed-in")) $("ed-in").value = fmt(ed.inSec);
    syncMarkLabels();
    let keeps = keepsForSource(ed.src.path);
    if (!keeps.length) keeps = [{ start: 0, end: ed.src.duration }];
    const next = subtractRange(keeps, 0, t);
    if (!next.length) return;
    pushHist();
    const neu = replaceSourceSegments(ed.src.path, ed.src.name, next);
    if (neu[0]) ed.activeId = neu[0].id;
    renderSegList();
    setStatus(`${tt("Anfang bis")} ${fmt(t)} ${tt("weggeschnitten.")}`);
  }

  /** 1-Klick: Alles ab dem Playhead bis zum Dateiende wegschneiden (Ripple Trim End). */
  function cutAfterPlayhead() {
    if (!ed.src) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const t = snapTime(currentPreviewTime());
    if (t <= 0.05 || t >= ed.src.duration - 0.05) return;
    ed.outSec = round2(t);
    if ($("ed-out")) $("ed-out").value = fmt(ed.outSec);
    syncMarkLabels();
    let keeps = keepsForSource(ed.src.path);
    if (!keeps.length) keeps = [{ start: 0, end: ed.src.duration }];
    const next = subtractRange(keeps, t, ed.src.duration);
    if (!next.length) return;
    pushHist();
    const neu = replaceSourceSegments(ed.src.path, ed.src.name, next);
    if (neu[0]) ed.activeId = neu[0].id;
    renderSegList();
    setStatus(`${tt("Ab")} ${fmt(t)} ${tt("bis Ende weggeschnitten.")}`);
  }

  /** 1-Klick-Aktionen für alle Clips der Timeline. */
  function applyOneClickAll(action) {
    if (!ed.segments.length) return;
    pushHist();
    if (action === "fades") {
      const anyHas = ed.segments.some((s) => (Number(s.fade_in) || 0) > 0 || (Number(s.fade_out) || 0) > 0);
      ed.segments.forEach((s) => {
        if (anyHas) {
          s.fade_in = 0;
          s.fade_out = 0;
        } else {
          const lim = clipDur(s) * 0.45;
          s.fade_in = round2(Math.min(1.0, lim));
          s.fade_out = round2(Math.min(1.0, lim));
        }
      });
      renderSegList();
      setStatus(anyHas ? tt("Fades bei allen Clips entfernt.") : tt("1s Ein-/Ausblendung für alle Clips aktiviert."));
    } else if (action === "xfade") {
      const xfEl = $("ed-crossfade");
      if (xfEl) {
        const cur = Number(xfEl.value) || 0;
        xfEl.value = cur > 0 ? "0" : "1.0";
        syncBadge();
        syncModeUI();
        setStatus(cur > 0 ? tt("Überblendung deaktiviert.") : tt("1s weiche Überblendung zwischen allen Clips aktiviert."));
      }
    } else if (action === "loudnorm") {
      const allOn = ed.segments.filter((s) => s.kind === "media").every((s) => s.loudnorm);
      ed.segments.forEach((s) => {
        if (s.kind === "media") s.loudnorm = !allOn;
      });
      renderSegList();
      setStatus(!allOn ? tt("Loudnorm (EBU R128) für alle Clips aktiviert.") : tt("Loudnorm für alle Clips ausgeschaltet."));
    } else if (action === "voice") {
      const allOn = ed.segments.filter((s) => s.kind === "media").every((s) => s.voice_enhance);
      ed.segments.forEach((s) => {
        if (s.kind === "media") s.voice_enhance = !allOn;
      });
      renderSegList();
      setStatus(!allOn ? tt("Sprachverbesserung & Entbrummen für alle Clips aktiviert.") : tt("Sprachverbesserung ausgeschaltet."));
    } else if (action === "highlight") {
      ed.segments.forEach((s) => {
        if (s.kind !== "media") return;
        const dur = s.end - s.start;
        if (dur > 4.5) {
          const mid = (s.start + s.end) / 2;
          s.start = round2(mid - 2.0);
          s.end = round2(mid + 2.0);
        }
      });
      renderSegList();
      setStatus(tt("Alle Clips auf ihre mittleren 4 Sekunden (Highlight-Reel) gekürzt."));
    } else if (action === "kf") {
      if (!ed.keyframes || !ed.keyframes.length) {
        setStatus(tt("Keine Keyframes für die aktuelle Quelle geladen."), true);
        return;
      }
      ed.segments.forEach((s) => {
        if (s.kind === "media" && ed.src && s.path === ed.src.path) {
          const kIn = nearestKf(s.start);
          const kOut = nearestKf(s.end);
          if (kIn && kOut) {
            s.start = round2(kIn.snapped);
            s.end = round2(Math.max(s.start + 0.1, kOut.snapped));
          }
        }
      });
      renderSegList();
      setStatus(tt("Alle Clips der Quelle auf Keyframes gerundet."));
    } else if (action === "reverse") {
      ed.segments.reverse();
      renderSegList();
      setStatus(tt("Clip-Reihenfolge umgekehrt."));
    }
  }

  function applyVisualBlur() {
    if (!ed.cropBox || !ed.src) return;
    const c = ed.cropBox;
    const spec = `${c.w}:${c.h}:${c.x}:${c.y}`;
    let act = activeClip();
    pushHist();
    if (!act) {
      addSegmentFromMarks();
      act = activeClip();
    }
    if (act) {
      act.delogo = spec;
      renderSegList();
      setStatus(`${tt("Bereich unkenntlich gesetzt (Blur/Delogo)")}: ${spec}`);
    }
    const ov = $("ed-crop-overlay");
    const bar = $("ed-crop-bar");
    if (ov) ov.hidden = true;
    if (bar) bar.hidden = true;
  }

  function insertFreezeFrame(sec) {
    if (!ed.src || !ed.src.path) {
      setStatus(tt("Zuerst eine Quelle laden."), true);
      return;
    }
    const dur = Math.max(0.5, Number(sec) || 3);
    const t = snapTime(currentPreviewTime());
    pushHist();
    const freezeClip = defaultClip({
      kind: "freeze",
      path: ed.src.path,
      name: ed.src.name,
      title: `Standbild ${dur}s @ ${fmt(t)}`,
      start: round2(t),
      end: round2(t + dur),
      mute: true,
      audio_index: -1,
      audio_indexes: [],
    });
    const act = activeClip();
    if (act && act.kind === "media" && act.path === ed.src.path && t > act.start + 0.05 && t < act.end - 0.05) {
      const idx = ed.segments.indexOf(act);
      const base = clipLabel(act).replace(/ [ab]$/, "");
      const right = defaultClip(Object.assign({}, act, {
        id: uid(), start: round2(t), title: `${base} b`,
      }));
      act.end = round2(t);
      act.title = `${base} a`;
      ed.segments.splice(idx + 1, 0, freezeClip, right);
    } else {
      ed.segments.push(freezeClip);
    }
    ed.activeId = freezeClip.id;
    renderSegList();
    flashClip(freezeClip.id);
    setStatus(`${tt("Standbild")} (${dur}s) @ ${fmt(t)} ${tt("eingefügt.")}`);
  }

  function copyYoutubeTimestamps() {
    if (!ed.segments.length) return;
    const xf = Math.max(0, Number(($("ed-crossfade") || {}).value) || 0);
    let acc = 0;
    const lines = ed.segments.map((s, i) => {
      const dur = clipDur(s);
      if (i > 0 && xf > 0) acc = Math.max(0, acc - Math.min(xf, dur * 0.45, clipDur(ed.segments[i - 1]) * 0.45));
      const h = Math.floor(acc / 3600);
      const m = Math.floor((acc % 3600) / 60);
      const sec = Math.floor(acc % 60);
      const stamp = h > 0
        ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
        : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
      acc += dur;
      return `${stamp} ${clipLabel(s)}`;
    });
    const text = lines.join("\n");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => {
        setStatus(tt("YouTube-Kapitel in die Zwischenablage kopiert."));
      }).catch(() => {
        setStatus(text);
      });
    } else {
      setStatus(text);
    }
  }

  function exportCsvCuts() {
    if (!ed.segments.length) return;
    const lines = ed.segments.map((s) => {
      const safeTitle = String(clipLabel(s) || "").replace(/"/g, '""');
      return `${Number(s.start).toFixed(3)},${Number(s.end).toFixed(3)},"${safeTitle}"`;
    });
    const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "schnittliste.csv";
    a.click();
    URL.revokeObjectURL(a.href);
    setStatus(tt("CSV-Schnittliste exportiert."));
  }

  async function importCsvCuts(file) {
    if (!file) return;
    if (!ed.src || !ed.src.path) {
      setStatus(tt("Zuerst eine Quelle laden, für die die CSV-Schnitte gelten."), true);
      return;
    }
    try {
      const raw = await file.text();
      const opts = audioOpts();
      const neu = [];
      raw.split(/\r?\n/).forEach((line, idx) => {
        const clean = line.trim();
        if (!clean || clean.startsWith("#")) return;
        const m = clean.match(/^([^,;]+)[,;]([^,;]+)(?:[,;](.*))?$/);
        if (!m) return;
        const st = parseTimeStr(m[1], 0);
        const en = parseTimeStr(m[2], ed.src.duration);
        const title = (m[3] || "").replace(/^"|"$/g, "").trim() || `Clip ${idx + 1}`;
        if (en > st) {
          neu.push(defaultClip({
            path: ed.src.path,
            name: ed.src.name,
            start: round2(st),
            end: round2(en),
            title,
            audio_index: opts.audio_index,
            audio_indexes: opts.audio_indexes.slice(),
            mute: opts.mute,
            sub_index: opts.sub_index,
            sub_indexes: (opts.sub_indexes || []).slice(),
            burn_subs: opts.burn_subs,
          }));
        }
      });
      if (!neu.length) throw new Error("Keine gültigen Schnittzeilen in der CSV gefunden.");
      pushHist();
      ed.segments = neu;
      ed.activeId = neu[0].id;
      renderSegList();
      setStatus(`${neu.length} ${tt("Clips aus CSV importiert.")}`);
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  async function directDownloadClip() {
    const act = activeClip();
    const path = (act && act.kind === "media" && act.path) || (ed.src && ed.src.path) || "";
    if (!path) {
      setStatus(tt("Zuerst eine Quelle oder einen Clip wählen."), true);
      return;
    }
    const marks = readMarks();
    const start = act ? act.start : marks.start;
    const end = act ? act.end : marks.end;
    const container = ($("ed-container") && $("ed-container").value) || "mkv";
    setStatus(tt("Schneide Direkt-Download …"));
    try {
      const r = await fetch("/api/remux/cut", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path,
          start: String(start),
          end: String(end),
          container,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.error || "Direkt-Download fehlgeschlagen");
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const base = String(path).split("/").pop().replace(/\.[^.]+$/, "");
      a.download = `${base}_${Math.round(start)}-${Math.round(end)}.${container}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus(tt("Direkt-Download gestartet."));
    } catch (e) {
      setStatus(String(e.message || e), true);
    }
  }

  function applySizePreset() {
    const sel = $("ed-size-preset");
    if (!sel) return;
    const mb = Number(sel.value) || 0;
    if (mb <= 0) return;
    const dur = Math.max(1, totalDur());
    if ($("ed-mode")) $("ed-mode").value = "encode";
    if ($("ed-rate-mode")) $("ed-rate-mode").value = "abr";
    const abr = Number(($("ed-abr") || {}).value) || 128;
    if ($("ed-acodec") && $("ed-acodec").value === "copy") $("ed-acodec").value = "opus";
    const totalKbps = Math.floor((mb * 8192 * 0.94) / dur);
    const vbr = Math.max(200, totalKbps - abr);
    if ($("ed-vbr")) $("ed-vbr").value = String(vbr);
    syncModeUI();
    updateSizeEstimate();
    setStatus(`${tt("Zielgröße")} ${mb} MB → Video-Bitrate automatisch auf ${vbr} kbit/s gesetzt.`);
  }

  function stepActiveClip(dir) {
    if (!ed.segments.length) return;
    const act = activeClip();
    const idx = act ? ed.segments.indexOf(act) : 0;
    const next = ed.segments[(idx + dir + ed.segments.length) % ed.segments.length];
    if (next) setActive(next.id);
  }

  function onKey(ev) {
    if (!isEditorVisible()) return;
    const tag = (ev.target && ev.target.tagName) || "";
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) {
      if (ev.key === "Escape") ev.target.blur();
      return;
    }
    const k = ev.key;
    if ((ev.ctrlKey || ev.metaKey) && k.toLowerCase() === "z") {
      ev.preventDefault();
      if (ev.shiftKey) redo(); else undo();
      return;
    }
    if ((ev.ctrlKey || ev.metaKey) && k.toLowerCase() === "y") {
      ev.preventDefault();
      redo();
      return;
    }
    if (k === "i" || k === "I") { ev.preventDefault(); setMark("in"); }
    else if (k === "o" || k === "O") { ev.preventDefault(); setMark("out"); }
    else if (k === "k" || k === "K") {
      ev.preventDefault();
      const v = $("ed-video");
      if (v) { v.pause(); ed.rateIdx = 0; v.playbackRate = 1; }
    } else if (k === "l" || k === "L") {
      ev.preventDefault();
      const v = $("ed-video");
      if (!v || !ed.src) return;
      if (v.paused) {
        if (!v.src && ed.previewMode === "none") startPreview(ed.playhead);
        v.play().catch(() => {});
        ed.rateIdx = 0;
        v.playbackRate = 1;
      } else {
        ed.rateIdx = Math.min(ed.rates.length - 1, ed.rateIdx + 1);
        v.playbackRate = ed.rates[ed.rateIdx];
      }
    } else if (k === "j" || k === "J") {
      ev.preventDefault();
      seekSoft(currentPreviewTime() - 5);
    } else if (k === "s" || k === "S") { ev.preventDefault(); splitAtPlayhead(); }
    else if (k === "Delete" || k === "Backspace") {
      if (!activeClip()) return;
      ev.preventDefault();
      deleteActive();
    } else if (k === "ArrowUp" || k === "ArrowDown") {
      if (!ed.segments.length) return;
      ev.preventDefault();
      const act = activeClip();
      const idx = act ? ed.segments.indexOf(act) : -1;
      const step = k === "ArrowUp" ? -1 : 1;
      const next = ed.segments[(idx + step + ed.segments.length) % ed.segments.length];
      if (next) setActive(next.id);
    }
    else if (k === "ArrowLeft") { ev.preventDefault(); stepFrame(ev.shiftKey ? -Math.round(ed.src && ed.src.fps || 25) : -1); }
    else if (k === "ArrowRight") { ev.preventDefault(); stepFrame(ev.shiftKey ? Math.round(ed.src && ed.src.fps || 25) : 1); }
    else if (k === " ") {
      const v = $("ed-video");
      if (v && ed.src) {
        ev.preventDefault();
        if (v.paused) v.play().catch(() => {});
        else v.pause();
      }
    }
  }

  function wire() {
    fillUploadDestOptions();
    pushHist();

    const browse = $("ed-browse");
    if (browse) {
      browse.addEventListener("click", () => {
        if (typeof window.openFilePickerModal !== "function") {
          setStatus(tt("Dateiauswahl nicht verfügbar."), true);
          return;
        }
        window.openFilePickerModal({
          title: tt("Video für Editor wählen"),
          rememberKey: "edPickDir",
          onPick: (f) => loadSource(f.rel, f.name),
        });
      });
    }
    const addSrc = $("ed-add-source");
    if (addSrc) {
      addSrc.addEventListener("click", () => {
        if (typeof window.openFilePickerModal !== "function") return;
        window.openFilePickerModal({
          title: tt("Quellen anhängen – mehrere anhaken, Reihenfolge = Timeline"),
          multi: true,
          rememberKey: "edPickDir",
          onPickMany: (files) => appendSources(files),
          onPick: async (f) => {
            await loadSource(f.rel, f.name);
            appendWholeSource();
          },
        });
      });
    }

    const destBrowse = $("ed-upload-dest-browse");
    if (destBrowse) {
      destBrowse.addEventListener("click", () => {
        if (typeof window.openFolderPickerModal !== "function") {
          setStatus(tt("Ordnerauswahl nicht verfügbar."), true);
          return;
        }
        window.openFolderPickerModal({
          title: tt("Upload-Zielordner wählen"),
          start: currentUploadDest() === "upload" ? "" : currentUploadDest(),
          onPick: (folder) => {
            const rel = folder || "";
            if (!rel) {
              ensureDestOption("upload", tt("Standard-Upload (/data/uploads)"));
              rememberUploadDest("upload");
              return;
            }
            ensureDestOption(rel, `${tt("Ordner")}: ${rel}`);
            rememberUploadDest(rel);
          },
        });
      });
    }
    const destSel = $("ed-upload-dest");
    if (destSel) destSel.addEventListener("change", () => rememberUploadDest(destSel.value));

    const up = $("ed-upload");
    if (up) up.addEventListener("change", () => {
      const f = up.files && up.files[0];
      up.value = "";
      if (f) uploadFile(f);
    });

    const markIn = $("ed-mark-in");
    if (markIn) markIn.addEventListener("click", () => setMark("in"));
    const markOut = $("ed-mark-out");
    if (markOut) markOut.addEventListener("click", () => setMark("out"));
    ["ed-in", "ed-out"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => {
        const cur = id === "ed-in" ? ed.inSec : ed.outSec;
        const t = snapTime(parseTimeStr(el.value, cur));
        if (id === "ed-in") ed.inSec = t;
        else ed.outSec = t;
        el.value = fmt(t);
        syncMarkLabels();
        renderSourceRuler();
      });
    });

    if ($("ed-keep-range")) $("ed-keep-range").addEventListener("click", addSegmentFromMarks);
    if ($("ed-cut-range")) $("ed-cut-range").addEventListener("click", cutOutRange);
    if ($("ed-keep-all")) $("ed-keep-all").addEventListener("click", keepWholeFile);
    if ($("ed-add-v2")) $("ed-add-v2").addEventListener("click", addBrollV2);
    if ($("ed-split")) $("ed-split").addEventListener("click", splitAtPlayhead);
    if ($("ed-cut-before")) $("ed-cut-before").addEventListener("click", cutBeforePlayhead);
    if ($("ed-cut-after")) $("ed-cut-after").addEventListener("click", cutAfterPlayhead);
    if ($("ed-kf-prev")) $("ed-kf-prev").addEventListener("click", () => jumpKeyframe(-1));
    if ($("ed-kf-next")) $("ed-kf-next").addEventListener("click", () => jumpKeyframe(1));
    if ($("ed-snapshot")) $("ed-snapshot").addEventListener("click", takeSnapshot);
    if ($("ed-freeze-insert")) $("ed-freeze-insert").addEventListener("click", () => insertFreezeFrame(3));
    if ($("ed-loop-toggle")) $("ed-loop-toggle").addEventListener("click", () => {
      ed.loopRange = !ed.loopRange;
      const btn = $("ed-loop-toggle");
      if (btn) btn.classList.toggle("btn-primary", ed.loopRange);
      setStatus(ed.loopRange ? tt("Bereichs-Loop (In ↔ Out) aktiviert.") : tt("Bereichs-Loop deaktiviert."));
    });
    if ($("ed-prog-prev-clip")) $("ed-prog-prev-clip").addEventListener("click", () => stepActiveClip(-1));
    if ($("ed-prog-next-clip")) $("ed-prog-next-clip").addEventListener("click", () => stepActiveClip(1));
    if ($("ed-1c-fades")) $("ed-1c-fades").addEventListener("click", () => applyOneClickAll("fades"));
    if ($("ed-1c-xfade")) $("ed-1c-xfade").addEventListener("click", () => applyOneClickAll("xfade"));
    if ($("ed-1c-loudnorm")) $("ed-1c-loudnorm").addEventListener("click", () => applyOneClickAll("loudnorm"));
    if ($("ed-1c-voice")) $("ed-1c-voice").addEventListener("click", () => applyOneClickAll("voice"));
    if ($("ed-1c-highlight")) $("ed-1c-highlight").addEventListener("click", () => applyOneClickAll("highlight"));
    if ($("ed-1c-kf")) $("ed-1c-kf").addEventListener("click", () => applyOneClickAll("kf"));
    if ($("ed-1c-reverse")) $("ed-1c-reverse").addEventListener("click", () => applyOneClickAll("reverse"));
    if ($("ed-restore-autosave")) $("ed-restore-autosave").addEventListener("click", restoreAutoSession);
    if ($("ed-size-preset")) $("ed-size-preset").addEventListener("change", applySizePreset);
    if ($("ed-aspect-mode")) $("ed-aspect-mode").addEventListener("change", syncModeUI);
    if ($("ed-pad-mode")) $("ed-pad-mode").addEventListener("change", syncModeUI);
    if ($("ed-black")) $("ed-black").addEventListener("click", () => addBlack(3));
    if ($("ed-chaps-to-clips")) $("ed-chaps-to-clips").addEventListener("click", chaptersToClips);
    if ($("ed-kf-round")) $("ed-kf-round").addEventListener("click", roundToKeyframes);
    if ($("ed-crop-toggle")) $("ed-crop-toggle").addEventListener("click", toggleCropOverlay);
    if ($("ed-crop-auto")) $("ed-crop-auto").addEventListener("click", runAutoCrop);
    if ($("ed-crop-apply")) $("ed-crop-apply").addEventListener("click", applyVisualCrop);
    if ($("ed-blur-apply")) $("ed-blur-apply").addEventListener("click", applyVisualBlur);
    if ($("ed-crop-reset")) $("ed-crop-reset").addEventListener("click", () => {
      const act = activeClip();
      if (act) { pushHist(); act.crop = ""; act.delogo = ""; renderSegList(); }
      const ov = $("ed-crop-overlay");
      const bar = $("ed-crop-bar");
      if (ov) ov.hidden = true;
      if (bar) bar.hidden = true;
      setStatus(tt("Crop/Blur zurückgesetzt."));
    });
    if ($("ed-crop-close")) $("ed-crop-close").addEventListener("click", () => {
      const ov = $("ed-crop-overlay");
      const bar = $("ed-crop-bar");
      if (ov) ov.hidden = true;
      if (bar) bar.hidden = true;
    });
    document.querySelectorAll("[data-crop-ratio]").forEach((btn) => {
      btn.addEventListener("click", () => applyCropRatio(btn.dataset.cropRatio));
    });
    wireCropDrag();
    if ($("ed-det-black")) $("ed-det-black").addEventListener("click", () => runAutoDetect("black"));
    if ($("ed-det-silence")) $("ed-det-silence").addEventListener("click", () => runAutoDetect("silence"));
    if ($("ed-det-scene")) $("ed-det-scene").addEventListener("click", () => runAutoDetect("scene"));
    if ($("ed-det-autocrop")) $("ed-det-autocrop").addEventListener("click", runAutoCrop);
    if ($("ed-preview-cut")) $("ed-preview-cut").addEventListener("click", previewTransitionCut);
    if ($("ed-tl-zoom")) $("ed-tl-zoom").addEventListener("input", () => {
      ed.tlZoom = Number($("ed-tl-zoom").value) || 1;
      renderTimeline();
    });
    if ($("ed-tl-zoom-fit")) $("ed-tl-zoom-fit").addEventListener("click", () => {
      ed.tlZoom = 1;
      renderTimeline();
    });
    const tlScroll = $("ed-tl-scroll");
    if (tlScroll) {
      tlScroll.addEventListener("wheel", (ev) => {
        if (!ev.ctrlKey) return;
        ev.preventDefault();
        const delta = ev.deltaY < 0 ? 0.5 : -0.5;
        ed.tlZoom = Math.max(1, Math.min(16, (Number(ed.tlZoom) || 1) + delta));
        renderTimeline();
      }, { passive: false });
    }
    if ($("ed-set-intro")) $("ed-set-intro").addEventListener("click", () => rememberTemplate("intro"));
    if ($("ed-ins-intro")) $("ed-ins-intro").addEventListener("click", () => insertTemplate("intro"));
    if ($("ed-set-outro")) $("ed-set-outro").addEventListener("click", () => rememberTemplate("outro"));
    if ($("ed-ins-outro")) $("ed-ins-outro").addEventListener("click", () => insertTemplate("outro"));
    if ($("ed-save-proj")) $("ed-save-proj").addEventListener("click", saveProject);
    if ($("ed-load-proj")) $("ed-load-proj").addEventListener("click", () => $("ed-proj-file") && $("ed-proj-file").click());
    if ($("ed-proj-file")) $("ed-proj-file").addEventListener("change", () => {
      const f = $("ed-proj-file").files && $("ed-proj-file").files[0];
      $("ed-proj-file").value = "";
      if (f) loadProjectFile(f);
    });
    if ($("ed-copy-yt")) $("ed-copy-yt").addEventListener("click", copyYoutubeTimestamps);
    if ($("ed-export-csv")) $("ed-export-csv").addEventListener("click", exportCsvCuts);
    if ($("ed-import-csv")) $("ed-import-csv").addEventListener("click", () => $("ed-csv-file") && $("ed-csv-file").click());
    if ($("ed-csv-file")) $("ed-csv-file").addEventListener("change", () => {
      const f = $("ed-csv-file").files && $("ed-csv-file").files[0];
      $("ed-csv-file").value = "";
      if (f) importCsvCuts(f);
    });
    if ($("ed-bgm-browse")) $("ed-bgm-browse").addEventListener("click", () => {
      if (typeof window.openFilePickerModal !== "function") return;
      window.openFilePickerModal({
        title: tt("Audiodatei für Hintergrundmusik (A2) wählen"),
        kind: "aux",
        rememberKey: "edPickDir",
        onPick: (f) => {
          ed.bgmPath = f.rel;
          if ($("ed-bgm-path")) $("ed-bgm-path").value = f.rel;
          if ($("ed-bgm-opts")) $("ed-bgm-opts").style.display = "";
          syncModeUI();
          setStatus(`${tt("Hintergrundmusik gesetzt")}: ${f.name}`);
        },
      });
    });
    if ($("ed-bgm-clear")) $("ed-bgm-clear").addEventListener("click", () => {
      ed.bgmPath = "";
      if ($("ed-bgm-path")) $("ed-bgm-path").value = "";
      if ($("ed-bgm-opts")) $("ed-bgm-opts").style.display = "none";
      syncModeUI();
      setStatus(tt("Hintergrundmusik entfernt."));
    });
    if ($("ed-direct-dl")) $("ed-direct-dl").addEventListener("click", directDownloadClip);
    if ($("ed-unload")) $("ed-unload").addEventListener("click", () => {
      if (!ed.src) return;
      unloadSource();
      setStatus(tt("Quelle entladen."));
    });
    if ($("ed-reset")) $("ed-reset").addEventListener("click", resetAll);
    if ($("ed-undo")) $("ed-undo").addEventListener("click", undo);
    if ($("ed-redo")) $("ed-redo").addEventListener("click", redo);
    if ($("ed-frame-back")) $("ed-frame-back").addEventListener("click", () => stepFrame(-1));
    if ($("ed-frame-fwd")) $("ed-frame-fwd").addEventListener("click", () => stepFrame(1));
    if ($("ed-jump-back")) $("ed-jump-back").addEventListener("click", () => jumpBy(-0.2));
    if ($("ed-jump-fwd")) $("ed-jump-fwd").addEventListener("click", () => jumpBy(0.2));
    if ($("ed-snap-kf")) $("ed-snap-kf").addEventListener("change", () => {
      if ($("ed-snap-kf").checked && ed.src) loadKeyframes(ed.src.path);
    });

    if ($("ed-preview-audio")) $("ed-preview-audio").addEventListener("change", () => {
      renderTracks();
      seekPreview(currentPreviewTime(), { snap: false });
    });
    if ($("ed-tracks-all")) $("ed-tracks-all").addEventListener("click", () => {
      const n = (ed.src && ed.src.audio && ed.src.audio.length) || 0;
      applyTrackSelection(Array.from({ length: n }, (_, i) => i));
    });
    if ($("ed-tracks-none")) $("ed-tracks-none").addEventListener("click", () => applyTrackSelection([]));
    if ($("ed-subs-all")) $("ed-subs-all").addEventListener("click", () => {
      const n = (ed.src && ed.src.subtitles && ed.src.subtitles.length) || 0;
      applySubSelection(Array.from({ length: n }, (_, i) => i));
    });
    if ($("ed-subs-none")) $("ed-subs-none").addEventListener("click", () => applySubSelection([]));

    if ($("ed-tl-split")) $("ed-tl-split").addEventListener("click", splitAtPlayhead);
    if ($("ed-tl-del")) $("ed-tl-del").addEventListener("click", deleteActive);
    if ($("ed-tl-left")) $("ed-tl-left").addEventListener("click", () => moveActive(-1));
    if ($("ed-tl-right")) $("ed-tl-right").addEventListener("click", () => moveActive(1));
    if ($("ed-tl-dup")) $("ed-tl-dup").addEventListener("click", duplicateActive);
    if ($("ed-tl-trim-in")) $("ed-tl-trim-in").addEventListener("click", () => trimActive("in"));
    if ($("ed-tl-trim-out")) $("ed-tl-trim-out").addEventListener("click", () => trimActive("out"));

    const tl = $("ed-timeline");
    if (tl) {
      tl.addEventListener("click", (ev) => {
        if (!ed.segments.length) return;
        if (ev.target.closest(".ed-tl-edge, .ed-tl-grip, .ed-tl-speed")) return;
        const rect = tl.getBoundingClientRect();
        timelineSeek((ev.clientX - rect.left) / rect.width);
      });
      tl.addEventListener("dragover", (ev) => ev.preventDefault());
    }
    const tlRuler = $("ed-tl-ruler");
    if (tlRuler) tlRuler.addEventListener("mousedown", beginRulerScrub);
    window.addEventListener("resize", closeClipMenu);

    document.querySelectorAll(".ed-tl-more").forEach((more) => {
      more.addEventListener("click", (ev) => {
        if (ev.target.closest(".ed-tl-more-panel button")) more.open = false;
      });
      document.addEventListener("click", (ev) => {
        if (more.open && !more.contains(ev.target)) more.open = false;
      });
    });
    if ($("ed-crossfade")) $("ed-crossfade").addEventListener("input", () => { syncBadge(); syncModeUI(); });

    const play = $("ed-play");
    if (play) play.addEventListener("click", () => {
      const v = $("ed-video");
      if (!v || !ed.src) return;
      if (v.paused) {
        if (ed.previewMode === "none") startPreview(ed.playhead || ed.inSec || 0);
        v.play().catch(() => {});
        play.textContent = "⏸";
      } else {
        v.pause();
        play.textContent = "▶";
      }
    });

    const seek = $("ed-seek");
    if (seek) {
      seek.addEventListener("input", () => {
        if (!ed.src || !ed.src.duration) return;
        const t = (Number(seek.value) / 1000) * ed.src.duration;
        updateTimeLabel(t, ed.src.duration);
      });
      seek.addEventListener("change", () => {
        if (!ed.src || !ed.src.duration) return;
        seekPreview((Number(seek.value) / 1000) * ed.src.duration);
      });
    }

    const ruler = $("ed-src-ruler");
    if (ruler) {
      ruler.addEventListener("mousedown", beginSourceRulerDrag);
    }

    const v = $("ed-video");
    if (v) {
      v.addEventListener("timeupdate", () => {
        if (!ed.src) return;
        const t = ed.previewOffset + (v.currentTime || 0);
        if (ed.loopRange && !ed.playTl) {
          const marks = readMarks();
          if (marks.end > marks.start + 0.2 && t >= marks.end) {
            seekSoft(marks.start, { snap: false });
            return;
          }
        }
        updateTimeLabel(t, ed.src.duration);
        const seekEl = $("ed-seek");
        if (seekEl && ed.src.duration > 0) {
          seekEl.value = String(Math.round((t / ed.src.duration) * 1000));
        }
        tlFollowTick();
      });
      v.addEventListener("ended", () => {
        // Dateiende mitten in der Timeline: trotzdem zum nächsten Clip.
        if (ed.playTl || (followEnabled() && ed.inClip)) gotoNextClip();
      });
      v.addEventListener("pause", () => {
        const p = $("ed-play");
        if (p) p.textContent = "▶";
        stopFollowTimer();
      });
      v.addEventListener("play", () => {
        const p = $("ed-play");
        if (p) p.textContent = "⏸";
        startFollowTimer();
      });
    }

    if ($("ed-clear")) $("ed-clear").addEventListener("click", () => {
      pushHist();
      ed.segments = [];
      ed.overlays = [];
      ed.activeId = null;
      ed.activeV2Id = null;
      stopTlPreview();
      renderSegList();
      setStatus("");
    });
    if ($("ed-play-tl")) $("ed-play-tl").addEventListener("click", playTimeline);
    if ($("ed-mode")) $("ed-mode").addEventListener("change", syncModeUI);
    if ($("ed-rate-mode")) $("ed-rate-mode").addEventListener("change", syncRateUI);
    if ($("ed-acodec")) $("ed-acodec").addEventListener("change", () => syncAudioUI());
    if ($("ed-name-mode")) $("ed-name-mode").addEventListener("change", syncNameUI);
    ["ed-suffix", "ed-out-name"].forEach((id) => {
      if ($(id)) $(id).addEventListener("input", syncNameUI);
    });
    if ($("ed-container")) $("ed-container").addEventListener("change", syncNameUI);
    if ($("ed-enqueue")) $("ed-enqueue").addEventListener("click", enqueue);
    document.addEventListener("keydown", onKey);
    syncModeUI();
    syncBadge();
    renderTimeline();
    renderSourceRuler();
    renderTracks();
    const unload = $("ed-unload");
    if (unload) unload.disabled = true;
    checkAutoSession();
  }

  window.editorOpenSource = async function editorOpenSource(path, name) {
    if (!ed.loaded) window.editorInit();
    await loadSource(path, name || path);
    if (!ed.segments.length && ed.src && ed.src.duration > 0) {
      keepWholeFile();
    }
  };

  window.editorInit = function editorInit() {
    if (ed.loaded) return;
    ed.loaded = true;
    wire();
  };

  document.addEventListener("DOMContentLoaded", () => {
    if (localStorage.getItem("page") === "editor") window.editorInit();
  });
})();
