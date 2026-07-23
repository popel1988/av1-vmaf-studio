/* Einfacher Video-Editor: Timeline, Mehrfach-Schnitte, Upload-Ziel, Export → Queue */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const tt = (s) => (window.I18N && window.I18N.t ? window.I18N.t(s) : s);

  const ed = {
    loaded: false,
    src: null,       // { path, name, duration, audio, … }
    inSec: 0,
    outSec: 0,
    playhead: 0,
    segments: [],    // { id, path, name, start, end, title, audio_index, mute }
    playTl: null,
    streamUrl: "",
    uploadDest: "upload",
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

  function totalDur() {
    return ed.segments.reduce((a, s) => a + Math.max(0, s.end - s.start), 0);
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

  function syncBadge() {
    const b = $("ed-badge");
    const n = ed.segments.length;
    if (b) b.textContent = n ? `${n} Clip(s) · ${fmt(totalDur())}` : "Keine Clips";
    const tot = $("ed-total");
    if (tot) tot.textContent = `${n} Clips · ${fmt(totalDur())}`;
    const enq = $("ed-enqueue");
    const playTl = $("ed-play-tl");
    const clear = $("ed-clear");
    if (enq) enq.disabled = n === 0;
    if (playTl) playTl.disabled = n === 0;
    if (clear) clear.disabled = n === 0;
  }

  function readMarks() {
    const inEl = $("ed-in");
    const outEl = $("ed-out");
    const start = inEl ? Number(inEl.value) : ed.inSec;
    const end = outEl ? Number(outEl.value) : ed.outSec;
    return { start, end };
  }

  function audioOpts() {
    const aSel = $("ed-audio");
    const mute = !!($("ed-mute") && $("ed-mute").checked);
    const aidx = aSel ? parseInt(aSel.value, 10) : 0;
    return {
      audio_index: Number.isFinite(aidx) ? aidx : 0,
      mute: mute || aidx < 0,
    };
  }

  /** Intervalle dieser Quelle aus der Timeline (sortiert, gemerged). */
  function keepsForSource(path) {
    const raw = ed.segments
      .filter((s) => s.path === path)
      .map((s) => ({ start: s.start, end: s.end }))
      .filter((r) => r.end > r.start)
      .sort((a, b) => a.start - b.start);
    if (!raw.length) return [];
    const out = [Object.assign({}, raw[0])];
    for (let i = 1; i < raw.length; i++) {
      const last = out[out.length - 1];
      if (raw[i].start <= last.end + 0.001) {
        last.end = Math.max(last.end, raw[i].end);
      } else {
        out.push(Object.assign({}, raw[i]));
      }
    }
    return out;
  }

  /** [start,end] aus einer Intervall-Liste entfernen. */
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
    const others = ed.segments.filter((s) => s.path !== path);
    const neu = ranges.map((r, i) => ({
      id: uid(),
      path,
      name,
      start: round2(r.start),
      end: round2(r.end),
      title: `Clip ${others.length + i + 1}`,
      audio_index: opts.audio_index,
      mute: opts.mute,
    }));
    ed.segments = others.concat(neu);
    // Titel neu durchnummerieren
    ed.segments.forEach((s, i) => { s.title = `Clip ${i + 1}`; });
  }

  function renderSegList() {
    const ul = $("ed-seg-list");
    if (!ul) return;
    ul.innerHTML = "";
    ed.segments.forEach((s, i) => {
      const li = document.createElement("li");
      li.className = "ed-seg-item";
      li.dataset.id = s.id;
      const dur = Math.max(0, s.end - s.start);
      li.innerHTML = `
        <div class="ed-seg-main">
          <strong>${i + 1}. ${escapeHtml(s.title || s.name)}</strong>
          <span class="muted">${escapeHtml(s.name)} · ${fmt(s.start)} → ${fmt(s.end)} (${fmt(dur)})${s.mute ? " · stumm" : ""}</span>
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
      ul.appendChild(li);
    });
    renderTimelineBar();
    renderSourceRuler();
    syncBadge();
  }

  function renderTimelineBar() {
    const track = $("ed-timeline-track");
    if (!track) return;
    track.innerHTML = "";
    const tot = totalDur() || 1;
    const colors = ["#22d3ee", "#38bdf8", "#34d399", "#fbbf24", "#f87171", "#a78bfa"];
    ed.segments.forEach((s, i) => {
      const w = ((s.end - s.start) / tot) * 100;
      const block = document.createElement("div");
      block.className = "ed-tl-block";
      block.style.width = Math.max(1.5, w) + "%";
      block.style.background = colors[i % colors.length];
      block.title = `${s.title || s.name}: ${fmt(s.end - s.start)}`;
      block.textContent = String(i + 1);
      track.appendChild(block);
    });
  }

  function renderSourceRuler() {
    const keepsEl = $("ed-src-keeps");
    const selEl = $("ed-src-sel");
    const head = $("ed-src-playhead");
    if (!keepsEl || !selEl || !ed.src || !(ed.src.duration > 0)) {
      if (keepsEl) keepsEl.innerHTML = "";
      if (selEl) selEl.style.width = "0";
      if (head) head.style.left = "0";
      return;
    }
    const dur = ed.src.duration;
    const keeps = keepsForSource(ed.src.path);
    // Ohne Clips: ganze Quelle als „noch nicht geschnitten“ andeuten
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
      ed.segments.splice(idx, 1);
      ed.segments.forEach((s, i) => { s.title = `Clip ${i + 1}`; });
    } else if (act === "up" && idx > 0) {
      const t = ed.segments[idx - 1];
      ed.segments[idx - 1] = ed.segments[idx];
      ed.segments[idx] = t;
    } else if (act === "down" && idx < ed.segments.length - 1) {
      const t = ed.segments[idx + 1];
      ed.segments[idx + 1] = ed.segments[idx];
      ed.segments[idx] = t;
    } else if (act === "load") {
      loadSource(ed.segments[idx].path, ed.segments[idx].name, {
        inSec: ed.segments[idx].start,
        outSec: ed.segments[idx].end,
      });
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

  function stopVideo() {
    const v = $("ed-video");
    if (!v) return;
    v.pause();
    v.removeAttribute("src");
    v.load();
    ed.streamUrl = "";
  }

  async function loadSource(path, name, opts) {
    opts = opts || {};
    setStatus(tt("Lade Quelle …"));
    stopTlPreview();
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
    ed.inSec = opts.inSec != null ? opts.inSec : 0;
    ed.outSec = opts.outSec != null ? opts.outSec : ed.src.duration;
    ed.playhead = ed.inSec;
    const inEl = $("ed-in");
    const outEl = $("ed-out");
    if (inEl) inEl.value = String(round2(ed.inSec));
    if (outEl) outEl.value = String(round2(ed.outSec));
    const info = $("ed-src-info");
    if (info) {
      info.textContent = `${ed.src.name} · ${fmt(ed.src.duration)}`
        + (data.size_human ? ` · ${data.size_human}` : "");
    }
    seekPreview(ed.inSec);
    renderSourceRuler();
    setStatus("");
  }

  function seekPreview(sec) {
    const v = $("ed-video");
    if (!v || !ed.src) return;
    const start = Math.max(0, Number(sec) || 0);
    ed.playhead = start;
    ed.streamUrl = mediaUrl(ed.src.path, start);
    v.src = ed.streamUrl;
    v.load();
    const seek = $("ed-seek");
    if (seek && ed.src.duration > 0) {
      seek.value = String(Math.round((start / ed.src.duration) * 1000));
    }
    updateTimeLabel(start, ed.src.duration);
    renderSourceRuler();
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
    const seek = $("ed-seek");
    if (seek && ed.src.duration > 0) {
      return (Number(seek.value) / 1000) * ed.src.duration;
    }
    return ed.playhead || 0;
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
    ed.segments.push({
      id: uid(),
      path: ed.src.path,
      name: ed.src.name,
      start: round2(start),
      end: round2(end),
      title: `Clip ${ed.segments.length + 1}`,
      audio_index: opts.audio_index,
      mute: opts.mute,
    });
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
    // Ersetzt ggf. vorhandene Clips dieser Quelle durch einen Voll-Clip
    replaceSourceSegments(ed.src.path, ed.src.name, [{ start: 0, end: ed.src.duration }]);
    renderSegList();
    setStatus(tt("Ganze Datei als Clip übernommen."));
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
    if (!keeps.length) {
      // Noch nichts auf der Timeline → von der ganzen Datei ausschneiden
      keeps = [{ start: 0, end: ed.src.duration }];
    }
    const next = subtractRange(keeps, start, end);
    if (!next.length) {
      setStatus(tt("Nach dem Entfernen bleibt nichts übrig."), true);
      return;
    }
    replaceSourceSegments(ed.src.path, ed.src.name, next);
    renderSegList();
    setStatus(
      tt("Bereich entfernt.") + ` ${next.length} `
      + tt("Clip(s) verbleiben – weitere Bereiche kannst du erneut entfernen."),
    );
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
    const run = () => {
      if (i >= ed.segments.length) {
        stopTlPreview();
        setStatus(tt("Timeline-Vorschau fertig."));
        return;
      }
      const s = ed.segments[i];
      const dur = Math.max(0.2, s.end - s.start);
      setStatus(`${tt("Vorschau")} ${i + 1}/${ed.segments.length}: ${s.title || s.name}`);
      v.src = `/api/media/stream?root=media&path=${encodeURIComponent(s.path)}`
        + `&audio=${s.mute ? -1 : (s.audio_index || 0)}&start=${s.start}`;
      v.load();
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
      ed.playTl = {
        timer: setTimeout(() => {
          i += 1;
          run();
        }, Math.min(dur, 120) * 1000),
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
    if ([...sel.options].some((o) => o.value === value)) {
      sel.value = value;
      return;
    }
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
    // Gespeicherten Custom-Ordner wiederherstellen
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
    if (st) {
      st.textContent = `${tt("Upload")} → ${dest === "upload" ? "uploads" : dest} … ${file.name}`;
    }
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
    const mode = ($("ed-mode") && $("ed-mode").value) || "remux";
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
        path: s.path,
        start: s.start,
        end: s.end,
        title: s.title,
        audio_index: s.audio_index,
        mute: !!s.mute,
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

  function wire() {
    fillUploadDestOptions();

    const browse = $("ed-browse");
    if (browse) {
      browse.addEventListener("click", () => {
        if (typeof window.openFilePickerModal !== "function") {
          setStatus(tt("Dateiauswahl nicht verfügbar."), true);
          return;
        }
        window.openFilePickerModal({
          title: tt("Video für Editor wählen"),
          onPick: (f) => loadSource(f.rel, f.name),
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
    if (destSel) {
      destSel.addEventListener("change", () => rememberUploadDest(destSel.value));
    }

    const up = $("ed-upload");
    if (up) up.addEventListener("change", () => {
      const f = up.files && up.files[0];
      up.value = "";
      if (f) uploadFile(f);
    });

    const markIn = $("ed-mark-in");
    if (markIn) markIn.addEventListener("click", () => {
      const t = currentPreviewTime();
      ed.inSec = t;
      if ($("ed-in")) $("ed-in").value = String(round2(t));
      renderSourceRuler();
    });
    const markOut = $("ed-mark-out");
    if (markOut) markOut.addEventListener("click", () => {
      const t = currentPreviewTime();
      ed.outSec = t;
      if ($("ed-out")) $("ed-out").value = String(round2(t));
      renderSourceRuler();
    });
    ["ed-in", "ed-out"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("input", () => renderSourceRuler());
    });

    const keepBtn = $("ed-keep-range");
    if (keepBtn) keepBtn.addEventListener("click", addSegmentFromMarks);
    const cutBtn = $("ed-cut-range");
    if (cutBtn) cutBtn.addEventListener("click", cutOutRange);
    const allBtn = $("ed-keep-all");
    if (allBtn) allBtn.addEventListener("click", keepWholeFile);

    const play = $("ed-play");
    if (play) play.addEventListener("click", () => {
      const v = $("ed-video");
      if (!v || !ed.src) return;
      if (v.paused) {
        if (!v.src) seekPreview(ed.inSec || 0);
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
        const t = (Number(seek.value) / 1000) * ed.src.duration;
        seekPreview(t);
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
        const seekEl = $("ed-seek");
        const base = seekEl && ed.src.duration
          ? (Number(seekEl.value) / 1000) * ed.src.duration
          : ed.inSec;
        updateTimeLabel(base + (v.currentTime || 0), ed.src.duration);
      });
      v.addEventListener("pause", () => {
        const p = $("ed-play");
        if (p && !ed.playTl) p.textContent = "▶";
      });
    }

    const clear = $("ed-clear");
    if (clear) clear.addEventListener("click", () => {
      ed.segments = [];
      stopTlPreview();
      renderSegList();
      setStatus("");
    });
    const playTl = $("ed-play-tl");
    if (playTl) playTl.addEventListener("click", playTimeline);
    const mode = $("ed-mode");
    if (mode) mode.addEventListener("change", syncModeUI);
    const enq = $("ed-enqueue");
    if (enq) enq.addEventListener("click", enqueue);
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
    if (localStorage.getItem("page") === "editor") {
      window.editorInit();
    }
  });
})();
