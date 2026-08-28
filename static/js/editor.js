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
    if (b) b.textContent = n ? `${n} Clip(s) · ${fmt(totalDur())}` : tt("Keine Clips");
    const tot = $("ed-total");
    if (tot) tot.textContent = `${n} Clips · ${fmt(totalDur())}`;
    ["ed-enqueue", "ed-play-tl", "ed-clear"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = n === 0;
    });
    const un = $("ed-undo");
    const re = $("ed-redo");
    if (un) un.disabled = ed.histPos < 1;
    if (re) re.disabled = ed.histPos >= ed.hist.length - 1;
  }

  function snapshot() {
    return JSON.parse(JSON.stringify(ed.segments));
  }

  function pushHist() {
    ed.hist = ed.hist.slice(0, ed.histPos + 1);
    ed.hist.push(snapshot());
    if (ed.hist.length > 80) ed.hist.shift();
    ed.histPos = ed.hist.length - 1;
    syncBadge();
  }

  function applyHist(i) {
    if (i < 0 || i >= ed.hist.length) return;
    ed.histPos = i;
    ed.segments = JSON.parse(JSON.stringify(ed.hist[i]));
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
      start: inEl ? Number(inEl.value) : ed.inSec,
      end: outEl ? Number(outEl.value) : ed.outSec,
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
      scale: 0,
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
    // Copy kann nur eine Streamstruktur durchreichen.
    const sels = new Set(ed.segments.filter((s) => s.kind === "media")
      .map((s) => (s.audio_indexes || []).join(",")));
    if (sels.size > 1) return true;
    const subs = new Set(ed.segments.filter((s) => s.kind === "media")
      .map((s) => (s.sub_indexes || []).join(",")));
    if (subs.size > 1) return true;
    return ed.segments.some((s) => (
      s.kind === "black" || s.kind === "silence"
      || Number(s.fade_in) > 0 || Number(s.fade_out) > 0
      || Math.abs((Number(s.speed) || 1) - 1) > 0.001
      || s.crop || Number(s.scale) > 0 || s.burn_subs
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
      if (Number(s.speed) && Number(s.speed) !== 1) extras.push(`${s.speed}×`);
      if (Number(s.fade_in) || Number(s.fade_out)) extras.push("Fade");
      li.innerHTML = `
        <div class="ed-seg-main">
          <strong>${i + 1}. ${escapeHtml(s.title || s.name)}</strong>
          <span class="muted">${escapeHtml(s.name || s.kind)} · ${fmt(s.start)} → ${fmt(s.end)} (${fmt(dur)})${extras.length ? " · " + extras.join(" · ") : ""}</span>
          ${rates ? `<span class="muted ed-seg-rates">${escapeHtml(rates)}</span>` : ""}
          <div class="ed-seg-edit">
            <input class="ed-seg-title" data-f="title" value="${escapeHtml(s.title || "")}" placeholder="${tt("Titel")}" />
            <input type="number" step="0.01" data-f="start" value="${s.start}" title="In" />
            <input type="number" step="0.01" data-f="end" value="${s.end}" title="Out" />
            <input type="number" step="0.1" min="0" max="8" data-f="fade_in" value="${s.fade_in || 0}" title="Fade-In s" />
            <input type="number" step="0.1" min="0" max="8" data-f="fade_out" value="${s.fade_out || 0}" title="Fade-Out s" />
            <input type="number" step="0.05" min="0.25" max="4" data-f="speed" value="${s.speed || 1}" title="${tt("Tempo")}" />
            <input type="number" step="2" min="0" max="2160" data-f="scale" value="${s.scale || 0}" title="${tt("Höhe")} (0 = Original)" />
            <input data-f="crop" value="${escapeHtml(s.crop || "")}" placeholder="crop w:h:x:y" title="Crop" />
          </div>
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
      li.querySelectorAll("[data-f]").forEach((inp) => {
        inp.addEventListener("change", () => {
          pushHist();
          const f = inp.dataset.f;
          const rec = ed.segments.find((x) => x.id === s.id);
          if (!rec) return;
          if (f === "title" || f === "crop") rec[f] = String(inp.value);
          else rec[f] = Number(inp.value);
          if (f === "start" || f === "end") renderSourceRuler();
          renderTimeline();
          syncBadge();
        });
      });
      li.addEventListener("click", (ev) => {
        if (ev.target.closest("button, input, select")) return;
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
    renderTimeline();
    renderSourceRuler();
    renderTracks();
    syncBadge();
    syncModeUI();
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

  function renderTimeline() {
    const track = $("ed-timeline-track");
    if (!track) return;
    track.innerHTML = "";
    const tot = rawTotal();
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
      block.style.width = `${Math.max(1.2, (dur / tot) * 100)}%`;
      block.style.setProperty("--tl-color", TL_COLORS[i % TL_COLORS.length]);
      if (s.kind !== "media") block.style.background = "#111827";
      applyStripBackground(block, s);
      const extras = [];
      if (Number(s.speed) && Number(s.speed) !== 1) extras.push(`${s.speed}×`);
      if (Number(s.fade_in) || Number(s.fade_out)) extras.push("Fade");
      if (s.mute) extras.push(tt("stumm"));
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
        ed.activeId = s.id;
        markActive();
        openClipMenu(ev.clientX, ev.clientY, s.id);
      });
      if ((dur / tot) * 100 < 7) block.classList.add("narrow");
      track.appendChild(block);
    });
    renderTlRuler();
    markActive();
    renderTlPlayhead();
    loadStripsForClips();
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
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const step = steps.find((s) => tot / s <= 12) || 3600;
    for (let t = 0; t <= tot + 0.001; t += step) {
      const tick = document.createElement("span");
      tick.className = "ed-tl-tick";
      tick.style.left = `${(t / tot) * 100}%`;
      tick.textContent = fmt(t);
      el.appendChild(tick);
    }
  }

  /** Playhead auf der Gesamt-Timeline, solange die Vorschau im aktiven Clip liegt. */
  function renderTlPlayhead() {
    const head = $("ed-tl-playhead");
    const lab = $("ed-tl-time");
    const tot = rawTotal();
    const s = activeClip();
    let t = -1;
    if (s && s.kind === "media" && ed.src && ed.src.path === s.path && tot > 0) {
      const cur = ed.playhead;
      if (cur >= s.start - 0.25 && cur <= s.end + 0.25) {
        const sp = Number(s.speed) > 0 ? Number(s.speed) : 1;
        const idx = ed.segments.indexOf(s);
        t = tlOffset(idx) + Math.max(0, Math.min(clipDur(s), (cur - s.start) / sp));
      }
    }
    if (head) {
      head.hidden = t < 0;
      if (t >= 0) head.style.left = `${(t / tot) * 100}%`;
    }
    if (lab) lab.textContent = `${t >= 0 ? fmt(t) : "–"} / ${fmt(totalDur())}`;
  }

  function markActive() {
    const track = $("ed-timeline-track");
    if (track) {
      track.querySelectorAll(".ed-tl-block").forEach((b) => {
        b.classList.toggle("active", b.dataset.id === ed.activeId);
      });
    }
    const ul = $("ed-seg-list");
    if (ul) {
      ul.querySelectorAll(".ed-seg-item").forEach((li) => {
        li.classList.toggle("active", li.dataset.id === ed.activeId);
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
      if ($("ed-in")) $("ed-in").value = String(round2(s.start));
      if ($("ed-out")) $("ed-out").value = String(round2(s.end));
      // Die Zielzeit ist berechnet – hier darf nichts einrasten.
      if (opts.seek !== false) seekSoft(srcTime, { snap: false });
      renderSourceRuler();
    }
    markActive();
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
    let u = `/api/media/stream?root=media&path=${encodeURIComponent(path)}&audio=${audio}`;
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
    if ($("ed-in")) $("ed-in").value = String(round2(ed.inSec));
    if ($("ed-out")) $("ed-out").value = String(round2(ed.outSec));
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
    if ($("ed-snap-kf") && $("ed-snap-kf").checked) loadKeyframes(path);
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
    renderSourceRuler();
    renderTlPlayhead();
    syncTools();
  }

  /** Kompletter Neuanfang: Timeline leeren und Quelle entladen. */
  function resetAll() {
    if (ed.segments.length
        && !window.confirm(tt("Timeline leeren und Quelle entladen?"))) return;
    pushHist();
    ed.segments = [];
    ed.activeId = null;
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
      if ($("ed-in")) $("ed-in").value = String(round2(t));
    } else {
      ed.outSec = t;
      if ($("ed-out")) $("ed-out").value = String(round2(t));
    }
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
      v: 1, segments: ed.segments, src: ed.src && { path: ed.src.path, name: ed.src.name },
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
    if (btn) btn.textContent = `▶ ${tt("Vorschau")}`;
  }

  function followEnabled() {
    const el = $("ed-follow");
    return el ? !!el.checked : true;
  }

  /** Clip anspielen: Quelle laden falls nötig, an die Clipzeit springen, starten. */
  async function playClip(s, opts) {
    opts = opts || {};
    const v = $("ed-video");
    if (!v || !s) return;
    ed.inClip = false;
    ed.advancing = true;
    try {
      if (s.kind !== "media" || !s.path) {
        // Schwarzclip hat keine Quelle – Bild aus und die Dauer abwarten.
        ed.activeId = s.id;
        markActive();
        v.pause();
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
      setStatus(tt("Timeline-Vorschau gestoppt."));
      return;
    }
    ed.playTl = { timer: null };
    const btn = $("ed-play-tl");
    if (btn) btn.textContent = `⏸ ${tt("Vorschau")}`;
    const act = activeClip();
    const first = act || ed.segments[0];
    const t = currentPreviewTime();
    const inside = !!(act && act.kind === "media" && ed.src && ed.src.path === act.path
      && t > act.start + 0.05 && t < act.end - 0.1);
    setStatus(tt("Timeline-Vorschau läuft – Schnitte werden übersprungen."));
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
    if (need && modeEl && modeEl.value === "remux") {
      modeEl.value = "encode";
    }
    const mode = (modeEl && modeEl.value) || "remux";
    const enc = $("ed-encode-opts");
    if (enc) enc.style.display = mode === "encode" ? "" : "none";
    const hint = $("ed-mode-hint");
    if (hint) {
      hint.textContent = mode === "encode"
        ? tt("Encode schneidet framegenau und vereinheitlicht inkompatible Quellen.")
        : tt("Remux kopiert Streams ohne Neucodierung. Schnitte liegen am nächsten Keyframe.");
    }
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
        speed: Number(s.speed) || 1, crop: s.crop || "", scale: Number(s.scale) || 0,
      })),
      mode,
      container: ($("ed-container") && $("ed-container").value) || "mkv",
      suffix: ($("ed-suffix") && $("ed-suffix").value) || "_edit",
      name_mode: nameModeCustom() ? "custom" : "suffix",
      out_name: (($("ed-out-name") && $("ed-out-name").value) || "").trim(),
      chapters_from_cuts: !!($("ed-chapters") && $("ed-chapters").checked),
      force_remux: !!($("ed-force") && $("ed-force").checked),
      platform: ($("ed-platform") && $("ed-platform").value) || "cpu",
      codec: ($("ed-codec") && $("ed-codec").value) || "av1",
      cq: ($("ed-cq") && parseInt($("ed-cq").value, 10)) || 30,
      rate_mode: ($("ed-rate-mode") && $("ed-rate-mode").value) || "cq",
      v_bitrate: ($("ed-vbr") && parseInt($("ed-vbr").value, 10)) || 0,
      audio_codec: ($("ed-acodec") && $("ed-acodec").value) || "aac",
      audio_bitrate: ($("ed-abr") && parseInt($("ed-abr").value, 10)) || 192,
      audio_channels: ($("ed-achannels") && parseInt($("ed-achannels").value, 10)) || 0,
      out_mode: ($("ed-out-mode") && $("ed-out-mode").value) || "default",
      out_subdir: ($("ed-out-subdir") && $("ed-out-subdir").value) || "",
      crossfade: Number(($("ed-crossfade") || {}).value) || 0,
      post_processing: "keep",
    };
    if (mode === "remux" && !payload.force_remux) {
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
        if (id === "ed-in") el.value = String(snapTime(Number(el.value)));
        else el.value = String(snapTime(Number(el.value)));
        renderSourceRuler();
      });
    });

    if ($("ed-keep-range")) $("ed-keep-range").addEventListener("click", addSegmentFromMarks);
    if ($("ed-cut-range")) $("ed-cut-range").addEventListener("click", cutOutRange);
    if ($("ed-keep-all")) $("ed-keep-all").addEventListener("click", keepWholeFile);
    if ($("ed-split")) $("ed-split").addEventListener("click", splitAtPlayhead);
    if ($("ed-black")) $("ed-black").addEventListener("click", () => addBlack(3));
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

    const more = document.querySelector(".ed-tl-more");
    if (more) {
      more.addEventListener("click", (ev) => {
        if (ev.target.closest(".ed-tl-more-panel button")) more.open = false;
      });
      document.addEventListener("click", (ev) => {
        if (more.open && !more.contains(ev.target)) more.open = false;
      });
    }
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
      ruler.addEventListener("click", (ev) => {
        if (!ed.src || !ed.src.duration) return;
        const rect = ruler.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
        seekPreview(ratio * ed.src.duration);
      });
    }

    const v = $("ed-video");
    if (v) {
      v.addEventListener("timeupdate", () => {
        if (!ed.src) return;
        const t = ed.previewOffset + (v.currentTime || 0);
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
      ed.activeId = null;
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
  }

  window.editorInit = function editorInit() {
    if (ed.loaded) return;
    ed.loaded = true;
    wire();
  };

  document.addEventListener("DOMContentLoaded", () => {
    if (localStorage.getItem("page") === "editor") window.editorInit();
  });
})();
