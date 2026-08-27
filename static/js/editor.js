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
  };

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
    const aSel = $("ed-audio");
    const sSel = $("ed-sub");
    const mute = !!($("ed-mute") && $("ed-mute").checked);
    const burn = !!($("ed-burn") && $("ed-burn").checked);
    const aidx = aSel ? parseInt(aSel.value, 10) : 0;
    const sidx = sSel ? parseInt(sSel.value, 10) : -1;
    return {
      audio_index: Number.isFinite(aidx) ? aidx : 0,
      mute: mute || aidx < 0,
      sub_index: Number.isFinite(sidx) ? sidx : -1,
      burn_subs: burn && sidx >= 0,
    };
  }

  function defaultClip(extra) {
    return Object.assign({
      id: uid(),
      kind: "media",
      path: "",
      name: "",
      start: 0,
      end: 3,
      title: "",
      audio_index: 0,
      mute: false,
      sub_index: -1,
      burn_subs: false,
      fade_in: 0,
      fade_out: 0,
      speed: 1,
      crop: "",
      scale: 0,
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
    const others = ed.segments.filter((s) => s.path !== path || s.kind === "black");
    const neu = ranges.map((r, i) => defaultClip({
      path, name,
      start: round2(r.start), end: round2(r.end),
      title: `Clip ${others.length + i + 1}`,
      audio_index: opts.audio_index, mute: opts.mute,
      sub_index: opts.sub_index, burn_subs: opts.burn_subs,
    }));
    ed.segments = others.concat(neu);
  }

  function clipsNeedEncode() {
    const xf = Number(($("ed-crossfade") || {}).value) || 0;
    if (xf > 0.01) return true;
    return ed.segments.some((s) => (
      s.kind === "black" || s.kind === "silence"
      || Number(s.fade_in) > 0 || Number(s.fade_out) > 0
      || Math.abs((Number(s.speed) || 1) - 1) > 0.001
      || s.crop || Number(s.scale) > 0 || s.burn_subs
    ));
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
      if (s.mute) extras.push(tt("stumm"));
      if (s.kind === "black") extras.push(tt("Schwarz"));
      if (Number(s.speed) && Number(s.speed) !== 1) extras.push(`${s.speed}×`);
      if (Number(s.fade_in) || Number(s.fade_out)) extras.push("Fade");
      li.innerHTML = `
        <div class="ed-seg-main">
          <strong>${i + 1}. ${escapeHtml(s.title || s.name)}</strong>
          <span class="muted">${escapeHtml(s.name || s.kind)} · ${fmt(s.start)} → ${fmt(s.end)} (${fmt(dur)})${extras.length ? " · " + extras.join(" · ") : ""}</span>
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
          syncBadge();
        });
      });
      li.addEventListener("dragstart", (ev) => {
        ed.dragId = s.id;
        li.classList.add("dragging");
        ev.dataTransfer.effectAllowed = "move";
      });
      li.addEventListener("dragend", () => li.classList.remove("dragging"));
      li.addEventListener("dragover", (ev) => { ev.preventDefault(); });
      li.addEventListener("drop", (ev) => {
        ev.preventDefault();
        if (!ed.dragId || ed.dragId === s.id) return;
        const from = ed.segments.findIndex((x) => x.id === ed.dragId);
        const to = ed.segments.findIndex((x) => x.id === s.id);
        if (from < 0 || to < 0) return;
        pushHist();
        const [m] = ed.segments.splice(from, 1);
        ed.segments.splice(to, 0, m);
        ed.dragId = null;
        renderSegList();
      });
      ul.appendChild(li);
    });
    renderTimelineBar();
    renderSourceRuler();
    syncBadge();
    syncModeUI();
  }

  function renderTimelineBar() {
    const track = $("ed-timeline-track");
    if (!track) return;
    track.innerHTML = "";
    const tot = totalDur() || 1;
    const colors = ["#22d3ee", "#38bdf8", "#34d399", "#fbbf24", "#f87171", "#a78bfa"];
    ed.segments.forEach((s, i) => {
      const w = (clipDur(s) / tot) * 100;
      const block = document.createElement("div");
      block.className = "ed-tl-block";
      block.style.width = Math.max(1.5, w) + "%";
      block.style.background = colors[i % colors.length];
      block.title = `${s.title || s.name}: ${fmt(clipDur(s))}`;
      block.textContent = String(i + 1);
      track.appendChild(block);
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
    const keeps = keepsForSource(ed.src.path);
    keepsEl.innerHTML = "";
    if (keeps.length) {
      keeps.forEach((k) => {
        const d = document.createElement("div");
        d.className = "ed-src-keep";
        d.style.left = `${(k.start / dur) * 100}%`;
        d.style.width = `${((k.end - k.start) / dur) * 100}%`;
        d.title = `${fmt(k.start)} – ${fmt(k.end)}`;
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
    if (act === "del") {
      pushHist();
      ed.segments.splice(idx, 1);
    } else if (act === "up" && idx > 0) {
      pushHist();
      const t = ed.segments[idx - 1];
      ed.segments[idx - 1] = ed.segments[idx];
      ed.segments[idx] = t;
    } else if (act === "down" && idx < ed.segments.length - 1) {
      pushHist();
      const t = ed.segments[idx + 1];
      ed.segments[idx + 1] = ed.segments[idx];
      ed.segments[idx] = t;
    } else if (act === "load") {
      const s = ed.segments[idx];
      if (s.kind === "black" || !s.path) return;
      loadSource(s.path, s.name, { inSec: s.start, outSec: s.end });
      return;
    }
    renderSegList();
  }

  function mediaUrl(path, startSec) {
    const audio = ($("ed-audio") && $("ed-audio").value) || "0";
    let u = `/api/media/stream?root=media&path=${encodeURIComponent(path)}&audio=${audio}`;
    if (startSec && startSec > 0) u += `&start=${encodeURIComponent(String(startSec))}`;
    return u;
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
          audio: audioOpts().audio_index,
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
    stopTlPreview();
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
    ed.src = {
      path,
      name: name || data.name || path,
      duration: Number(data.duration) || 0,
      audio: data.audio || [],
      subtitles: data.subtitles || [],
      chapters: data.chapters || [],
      codec: data.codec || "",
      container: data.container || "",
      fps: Number(data.fps) || 25,
      width: data.width || 0,
      height: data.height || 0,
    };
    const pathEl = $("ed-path");
    if (pathEl) {
      pathEl.value = ed.src.name
        + (path.startsWith("upload:") ? " (Upload)" : ` · ${path}`);
    }
    const aSel = $("ed-audio");
    if (aSel) {
      aSel.innerHTML = "";
      if (!ed.src.audio.length) {
        aSel.innerHTML = `<option value="-1">${tt("Kein Ton")}</option>`;
      } else {
        ed.src.audio.forEach((a, i) => {
          const lab = [a.language || "und", a.codec || "", a.channels ? `${a.channels}ch` : ""]
            .filter(Boolean).join(" · ");
          const o = document.createElement("option");
          o.value = String(i);
          o.textContent = `#${i}: ${lab}`;
          aSel.appendChild(o);
        });
      }
    }
    const sSel = $("ed-sub");
    if (sSel) {
      sSel.innerHTML = `<option value="-1">${tt("Keine")}</option>`;
      (ed.src.subtitles || []).forEach((s, i) => {
        const lab = [s.language || "und", s.codec || s.title || ""].filter(Boolean).join(" · ");
        const o = document.createElement("option");
        o.value = String(i);
        o.textContent = `#${i}: ${lab}`;
        sSel.appendChild(o);
      });
    }
    ed.inSec = opts.inSec != null ? opts.inSec : 0;
    ed.outSec = opts.outSec != null ? opts.outSec : ed.src.duration;
    ed.playhead = ed.inSec;
    if ($("ed-in")) $("ed-in").value = String(round2(ed.inSec));
    if ($("ed-out")) $("ed-out").value = String(round2(ed.outSec));
    const info = $("ed-src-info");
    if (info) {
      info.textContent = `${ed.src.name} · ${fmt(ed.src.duration)}`
        + (data.size_human ? ` · ${data.size_human}` : "")
        + (ed.src.codec ? ` · ${String(ed.src.codec).toUpperCase()}` : "")
        + ((ed.src.chapters || []).length ? ` · ${ed.src.chapters.length} ${tt("Kapitel")}` : "");
    }
    seekPreview(ed.inSec);
    renderSourceRuler();
    setStatus("");
    loadPreviewAssets(path);
    if ($("ed-snap-kf") && $("ed-snap-kf").checked) loadKeyframes(path);
  }

  function seekSoft(sec) {
    if (!ed.src) return;
    const t = snapTime(Math.max(0, Math.min(ed.src.duration || 0, Number(sec) || 0)));
    const v = $("ed-video");
    const rel = t - ed.previewOffset;
    if (v && ed.previewMode !== "none" && rel >= -0.05 && rel < 20) {
      try { v.currentTime = Math.max(0, rel); } catch (e) { seekPreview(t); return; }
      ed.playhead = t;
      updateTimeLabel(t, ed.src.duration);
      const seek = $("ed-seek");
      if (seek && ed.src.duration > 0) {
        seek.value = String(Math.round((t / ed.src.duration) * 1000));
      }
      renderSourceRuler();
      return;
    }
    seekPreview(t);
  }

  function seekPreview(sec) {
    const start = snapTime(Math.max(0, Number(sec) || 0));
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
  }

  function currentPreviewTime() {
    if (!ed.src) return 0;
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
    ed.segments.push(defaultClip({
      path: ed.src.path, name: ed.src.name,
      start: round2(start), end: round2(end),
      title: `Clip ${ed.segments.length + 1}`,
      audio_index: opts.audio_index, mute: opts.mute,
      sub_index: opts.sub_index, burn_subs: opts.burn_subs,
    }));
    renderSegList();
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
    replaceSourceSegments(ed.src.path, ed.src.name, [{ start: 0, end: ed.src.duration }]);
    renderSegList();
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
      audio_index: opts.audio_index, mute: opts.mute,
      sub_index: opts.sub_index, burn_subs: opts.burn_subs,
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
      ed.segments.push(defaultClip({
        path: f.rel,
        name,
        start: 0,
        end: round2(Number(data.duration) || 0),
        title: name,
      }));
      added += 1;
    }
    renderSegList();
    if (needFirst && added) {
      const first = ed.segments.find((s) => s.kind === "media" && s.path);
      if (first) await loadSource(first.path, first.name);
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
    replaceSourceSegments(ed.src.path, ed.src.name, next);
    renderSegList();
    setStatus(tt("Bereich entfernt.") + ` ${next.length} ` + tt("Clip(s) verbleiben – weitere Bereiche kannst du erneut entfernen."));
  }

  function splitAtPlayhead() {
    if (!ed.src) return;
    const t = snapTime(currentPreviewTime());
    const idx = ed.segments.findIndex((s) => (
      s.path === ed.src.path && s.kind !== "black" && t > s.start + 0.05 && t < s.end - 0.05
    ));
    if (idx < 0) {
      setStatus(tt("Playhead liegt in keinem Clip dieser Quelle."), true);
      return;
    }
    pushHist();
    const s = ed.segments[idx];
    const right = defaultClip(Object.assign({}, s, { id: uid(), start: round2(t), title: `${s.title || "Clip"} b` }));
    s.end = round2(t);
    s.title = `${s.title || "Clip"} a`;
    ed.segments.splice(idx + 1, 0, right);
    renderSegList();
    setStatus(tt("Clip geteilt."));
  }

  function addBlack(sec) {
    pushHist();
    ed.segments.push(defaultClip({
      kind: "black", name: tt("Schwarz"), title: `${tt("Schwarz")} ${sec}s`,
      start: 0, end: Number(sec) || 3, mute: true, audio_index: -1,
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
  }

  async function playTimeline() {
    if (!ed.segments.length) return;
    stopTlPreview();
    const v = $("ed-video");
    if (!v) return;
    let i = 0;
    const run = async () => {
      if (i >= ed.segments.length) {
        stopTlPreview();
        setStatus(tt("Timeline-Vorschau fertig."));
        return;
      }
      const s = ed.segments[i];
      const dur = Math.max(0.2, clipDur(s));
      setStatus(`${tt("Vorschau")} ${i + 1}/${ed.segments.length}: ${s.title || s.name}`);
      if (s.kind === "black" || !s.path) {
        v.removeAttribute("src");
        v.load();
      } else {
        await loadSource(s.path, s.name, { inSec: s.start, outSec: s.end });
        const p = v.play();
        if (p && p.catch) p.catch(() => {});
      }
      ed.playTl = {
        timer: setTimeout(() => { i += 1; run(); }, Math.min(dur, 120) * 1000),
      };
    };
    run();
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
  }

  async function enqueue() {
    if (!ed.segments.length) return;
    const mode = ($("ed-mode") && $("ed-mode").value) || "remux";
    const payload = {
      segments: ed.segments.map((s) => ({
        path: s.path, kind: s.kind || "media",
        start: s.start, end: s.end, title: s.title,
        audio_index: s.audio_index, mute: !!s.mute,
        sub_index: s.sub_index, burn_subs: !!s.burn_subs,
        fade_in: Number(s.fade_in) || 0, fade_out: Number(s.fade_out) || 0,
        speed: Number(s.speed) || 1, crop: s.crop || "", scale: Number(s.scale) || 0,
      })),
      mode,
      container: ($("ed-container") && $("ed-container").value) || "mkv",
      suffix: ($("ed-suffix") && $("ed-suffix").value) || "_edit",
      chapters_from_cuts: !!($("ed-chapters") && $("ed-chapters").checked),
      force_remux: !!($("ed-force") && $("ed-force").checked),
      platform: ($("ed-platform") && $("ed-platform").value) || "cpu",
      codec: ($("ed-codec") && $("ed-codec").value) || "av1",
      cq: ($("ed-cq") && parseInt($("ed-cq").value, 10)) || 30,
      audio_codec: ($("ed-acodec") && $("ed-acodec").value) || "aac",
      audio_bitrate: ($("ed-abr") && parseInt($("ed-abr").value, 10)) || 192,
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
    seekSoft(currentPreviewTime() + dir / fps);
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
    if ($("ed-undo")) $("ed-undo").addEventListener("click", undo);
    if ($("ed-redo")) $("ed-redo").addEventListener("click", redo);
    if ($("ed-frame-back")) $("ed-frame-back").addEventListener("click", () => stepFrame(-1));
    if ($("ed-frame-fwd")) $("ed-frame-fwd").addEventListener("click", () => stepFrame(1));
    if ($("ed-snap-kf")) $("ed-snap-kf").addEventListener("change", () => {
      if ($("ed-snap-kf").checked && ed.src) loadKeyframes(ed.src.path);
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
        if (!ed.src || ed.playTl) return;
        const t = ed.previewOffset + (v.currentTime || 0);
        updateTimeLabel(t, ed.src.duration);
        const seekEl = $("ed-seek");
        if (seekEl && ed.src.duration > 0) {
          seekEl.value = String(Math.round((t / ed.src.duration) * 1000));
        }
      });
      v.addEventListener("pause", () => {
        const p = $("ed-play");
        if (p && !ed.playTl) p.textContent = "▶";
      });
    }

    if ($("ed-clear")) $("ed-clear").addEventListener("click", () => {
      pushHist();
      ed.segments = [];
      stopTlPreview();
      renderSegList();
      setStatus("");
    });
    if ($("ed-play-tl")) $("ed-play-tl").addEventListener("click", playTimeline);
    if ($("ed-mode")) $("ed-mode").addEventListener("change", syncModeUI);
    if ($("ed-enqueue")) $("ed-enqueue").addEventListener("click", enqueue);
    document.addEventListener("keydown", onKey);
    syncModeUI();
    syncBadge();
    renderSourceRuler();
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
