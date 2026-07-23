/* Vollwertiger Media-Player: Direct-Play + HLS, Profile, Kapitel, Burn-in */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const tt = (s) => (window.I18N && window.I18N.t ? window.I18N.t(s) : s);

  const IMAGE_SUB = /pgs|dvd_sub|dvb_sub|xsub|hdmv/;

  const fp = {
    sid: null,
    hls: null,
    info: null,
    chapters: [],
    duration: 0,
    startOffset: 0,
    path: "",
    seeking: false,
    mode: "hls",
    options: null,
    dirty: false,
    audioMode: "",
    windowEnd: 0,
    lookahead: 30,
    extending: false,
    encodePaused: false,
  };

  const ENCODE_PROFILES = ["original", "1080p", "720p", "480p", "custom"];
  const PRESET_BR = { original: 8000, "1080p": 6000, "720p": 3500, "480p": 1500 };

  function fmtClock(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }

  function setStatus(msg, bad) {
    const el = $("fp-status");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("bad", !!bad);
  }

  function setBadge(text) {
    const b = $("fp-badge");
    if (b) b.textContent = text;
  }

  function absoluteTime() {
    const v = $("fp-video");
    return fp.startOffset + (v ? (v.currentTime || 0) : 0);
  }

  function updateTimeUi() {
    const seek = $("fp-seek");
    const time = $("fp-time");
    const t = absoluteTime();
    const dur = fp.duration || 0;
    if (time) time.textContent = `${fmtClock(t)} / ${fmtClock(dur)}`;
    if (seek && dur > 0 && !fp.seeking) {
      seek.value = String(Math.round(Math.min(1, Math.max(0, t / dur)) * 1000));
    }
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
    if (/^(aac|mp3|mp4a)/.test(ac)) return true;
    if (ac === "opus" && v.canPlayType('audio/webm; codecs="opus"')) return true;
    return false;
  }

  function detectClientCodecs() {
    const v = document.createElement("video");
    const out = ["h264"];
    if (v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"')
        || v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"')) out.push("hevc");
    if (v.canPlayType('video/mp4; codecs="av01.0.05M.08"')
        || v.canPlayType('video/webm; codecs="av01.0.05M.08"')) out.push("av1");
    return out;
  }

  function setDirty(on) {
    fp.dirty = !!on;
    const btn = $("fp-apply");
    if (btn) {
      btn.disabled = !fp.dirty || !fp.path;
      btn.classList.toggle("btn-primary", fp.dirty);
      btn.classList.toggle("btn-ghost", !fp.dirty);
    }
  }

  function syncEncodeUi(opts) {
    const prof = (($("fp-profile") || {}).value) || "auto";
    const encode = ENCODE_PROFILES.includes(prof);
    ["fp-platform", "fp-codec"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = !encode;
    });
    const hw = $("fp-height-wrap");
    const bw = $("fp-vbr-wrap");
    if (hw) hw.style.display = prof === "custom" ? "" : "none";
    if (bw) bw.style.display = encode ? "" : "none";
    // Preset gewechselt → Default-Bitrate vorschlagen (nur wenn gewünscht)
    if (opts && opts.presetBr && PRESET_BR[prof] && $("fp-vbr")) {
      $("fp-vbr").value = String(PRESET_BR[prof]);
    }
  }

  function updateAudioModeHint(sess) {
    const el = $("fp-audio-mode");
    if (!el) return;
    if (sess && sess.audio_mode_label) {
      fp.audioMode = sess.audio_mode || "";
      el.textContent = sess.audio_mode_label;
      el.classList.toggle("fp-audio-copy", sess.audio_mode === "copy" || sess.audio_mode === "direct");
      el.classList.toggle("fp-audio-xcode", sess.audio_mode === "transcode");
      return;
    }
    // Vorschau anhand gewählter Spur + Modus (noch nicht übernommen)
    const aSel = $("fp-audio");
    const idx = aSel ? parseInt(aSel.value, 10) : -1;
    if (isNaN(idx) || idx < 0) {
      el.textContent = tt("Kein Ton");
      el.classList.remove("fp-audio-copy", "fp-audio-xcode");
      return;
    }
    const tracks = (fp.info && fp.info.audio) || [];
    const ac = String((tracks[idx] && tracks[idx].codec) || "").toLowerCase();
    const acUp = (ac || "?").toUpperCase();
    const forceCopy = !!($("fp-audio-copy") && $("fp-audio-copy").checked);
    const prof = (($("fp-profile") || {}).value) || "auto";
    if (prof === "direct" || (prof === "auto" && clientDirectOk(fp.info))) {
      el.textContent = `${tt("Direct-Play")} (${acUp})`;
      el.classList.add("fp-audio-copy");
      el.classList.remove("fp-audio-xcode");
      return;
    }
    if (forceCopy || /^(aac|mp3|mp4a)/.test(ac)) {
      el.textContent = forceCopy && !/^(aac|mp3|mp4a)/.test(ac)
        ? `${tt("Stream-Copy erzwungen")} (${acUp})`
        : `${tt("Stream-Copy")} (${acUp})`;
      el.classList.add("fp-audio-copy");
      el.classList.remove("fp-audio-xcode");
    } else {
      el.textContent = `${tt("Transcode → AAC")} (${acUp})`;
      el.classList.add("fp-audio-xcode");
      el.classList.remove("fp-audio-copy");
    }
  }

  function bufferedAhead() {
    const v = $("fp-video");
    if (!v) return 0;
    const t = v.currentTime || 0;
    let end = t;
    try {
      for (let i = 0; i < v.buffered.length; i++) {
        if (v.buffered.start(i) <= t + 0.75) {
          end = Math.max(end, v.buffered.end(i));
        }
      }
    } catch (e) { /* ignore */ }
    return Math.max(0, end - t);
  }

  /** Encode drosseln wenn Zielpuffer voll – ohne Session-Neustart. */
  function throttleEncodeByBuffer() {
    if (fp.mode !== "hls" || !fp.sid) return;
    if (!(fp.lookahead > 0)) return;
    const v = $("fp-video");
    if (!v || v.paused || fp.seeking) return;
    if ((v.currentTime || 0) < 1.5) return;
    const ahead = bufferedAhead();
    const target = fp.lookahead;
    if (ahead >= target - 0.5 && !fp.encodePaused) {
      setEncodePaused(true);
    } else if (ahead < target * 0.4 && fp.encodePaused) {
      setEncodePaused(false);
    }
  }

  function fillEncodeOptions(options) {
    fp.options = options;
    const client = detectClientCodecs();

    const sel = $("fp-profile");
    if (sel) {
      const cur = sel.value || "auto";
      const profiles = (options && options.profiles) || [];
      sel.innerHTML = profiles.map((p) =>
        `<option value="${p.id}">${escapeHtml(p.label)}</option>`).join("");
      if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    }

    const plat = $("fp-platform");
    if (plat) {
      const cur = plat.value || "auto";
      const platforms = (options && options.platforms) || [];
      plat.innerHTML = platforms.map((p) => {
        const dis = p.available === false ? " disabled" : "";
        return `<option value="${p.id}"${dis}>${escapeHtml(p.label)}</option>`;
      }).join("");
      if ([...plat.options].some((o) => o.value === cur && !o.disabled)) plat.value = cur;
    }

    const codec = $("fp-codec");
    if (codec) {
      const cur = codec.value || "h264";
      const codecs = (options && options.codecs) || [];
      codec.innerHTML = codecs.map((c) => {
        const ok = client.includes(c.id);
        const lab = ok ? c.label : `${c.label} (${tt("Browser: Fallback H.264")})`;
        return `<option value="${c.id}">${escapeHtml(lab)}</option>`;
      }).join("");
      if ([...codec.options].some((o) => o.value === cur)) codec.value = cur;
    }

    const la = $("fp-lookahead");
    if (la && options && options.lookahead_choices) {
      const cur = la.value || String(options.lookahead_default || 30);
      la.innerHTML = options.lookahead_choices.map((c) =>
        `<option value="${c.id}">${escapeHtml(c.label)}</option>`).join("");
      if ([...la.options].some((o) => o.value === cur)) la.value = cur;
    }

    const hw = $("fp-hw-info");
    if (hw && options) {
      const ready = options.capabilities_ready
        ? tt("Capabilities aus Diagnose")
        : tt("Encoder-Liste (Diagnose noch nicht gelaufen)");
      const plats = (options.platforms || [])
        .filter((p) => p.id !== "auto" && p.available)
        .map((p) => p.id)
        .join(", ");
      hw.textContent = `${ready} · ${tt("Browser kann")}: ${client.join(", ").toUpperCase()}`
        + (plats ? ` · ${tt("Server")}: ${plats}` : "")
        + (options.note ? ` · ${options.note}` : "");
    }
    syncEncodeUi({ presetBr: false });
  }

  async function setEncodePaused(paused) {
    if (!fp.sid || fp.mode !== "hls") return;
    const path = paused ? "pause" : "resume";
    try {
      const d = await (await fetch(`/api/player/session/${fp.sid}/${path}`, {
        method: "POST",
      })).json();
      fp.encodePaused = !!d.encode_paused;
    } catch (e) { /* ignore */ }
  }

  function fillProfiles(options) {
    fillEncodeOptions(options);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fillTracks(info) {
    const aSel = $("fp-audio");
    const sSel = $("fp-sub");
    if (!aSel || !sSel) return;
    const audio = (info && info.audio) || [];
    const subs = (info && info.subtitles) || [];
    aSel.innerHTML = audio.length
      ? audio.map((a, i) => {
          const lab = [a.language || "und", a.codec || "", a.channels ? `${a.channels}ch` : "",
            a.bitrate_human && a.bitrate_human !== "—" ? a.bitrate_human : "", a.title || ""]
            .filter(Boolean).join(" · ");
          return `<option value="${i}">${i}: ${escapeHtml(lab)}</option>`;
        }).join("")
      : `<option value="-1">${tt("Kein Ton")}</option>`;

    sSel.innerHTML = `<option value="-1">${tt("Keine Untertitel")}</option>` +
      subs.map((s, idx) => {
        const c = (s.codec || "").toLowerCase();
        const img = IMAGE_SUB.test(c);
        const lab = [s.language || "und", s.codec || "", img ? tt("Bild") : "", s.title || ""]
          .filter(Boolean).join(" · ");
        return `<option value="${idx}" data-image="${img ? "1" : "0"}">${escapeHtml(lab)}</option>`;
      }).join("");
    syncBurnHint();
    updateAudioModeHint(null);
  }

  function syncBurnHint() {
    const sSel = $("fp-sub");
    const burn = $("fp-burn");
    if (!sSel || !burn) return;
    const opt = sSel.selectedOptions[0];
    const isImg = opt && opt.dataset.image === "1";
    burn.disabled = !isImg;
    if (!isImg) burn.checked = false;
  }

  function renderChapters(chapters) {
    fp.chapters = chapters || [];
    const wrap = $("fp-chapters-wrap");
    const ul = $("fp-chapters");
    if (!wrap || !ul) return;
    if (!fp.chapters.length) {
      wrap.style.display = "none";
      ul.innerHTML = "";
      return;
    }
    wrap.style.display = "";
    ul.innerHTML = fp.chapters.map((c, i) =>
      `<li><button type="button" data-ch="${i}">`
      + `<span>${escapeHtml(c.title || ("Kapitel " + (i + 1)))}</span>`
      + `<span class="fp-ch-time">${fmtClock(c.start)}</span></button></li>`
    ).join("");
    ul.querySelectorAll("button[data-ch]").forEach((b) => {
      b.addEventListener("click", () => {
        const ch = fp.chapters[parseInt(b.dataset.ch, 10)];
        if (ch) startSession(ch.start, true);
      });
    });
  }

  function destroyPlayback() {
    const v = $("fp-video");
    if (fp.hls) {
      try { fp.hls.destroy(); } catch (e) { /* ignore */ }
      fp.hls = null;
    }
    if (v) {
      v.removeAttribute("src");
      v.load();
      [...v.querySelectorAll("track")].forEach((t) => t.remove());
    }
  }

  async function stopSession() {
    destroyPlayback();
    if (fp.sid) {
      try { await fetch(`/api/player/session/${fp.sid}`, { method: "DELETE" }); }
      catch (e) { /* ignore */ }
      fp.sid = null;
    }
    setBadge(tt("Bereit"));
    const ov = $("fp-overlay");
    if (ov) ov.style.display = "";
  }

  async function unloadVideo() {
    await stopSession();
    fp.path = "";
    fp.info = null;
    fp.chapters = [];
    fp.duration = 0;
    fp.startOffset = 0;
    fp.windowEnd = 0;
    fp.encodePaused = false;
    fp.mode = "hls";
    if ($("fp-path")) $("fp-path").value = "";
    const aSel = $("fp-audio");
    const sSel = $("fp-sub");
    if (aSel) aSel.innerHTML = `<option value="-1">—</option>`;
    if (sSel) sSel.innerHTML = `<option value="-1">${tt("Keine Untertitel")}</option>`;
    const burn = $("fp-burn");
    if (burn) { burn.checked = false; burn.disabled = true; }
    renderChapters([]);
    setDirty(false);
    setStatus(tt("Video entladen."));
    updateTimeUi();
    updateAudioModeHint(null);
    const play = $("fp-play");
    if (play) play.textContent = "▶";
  }

  function attachSubtitles(path, subIdx) {
    const v = $("fp-video");
    if (!v || subIdx < 0) return;
    const opt = ($("fp-sub") || {}).selectedOptions;
    if (opt && opt[0] && opt[0].dataset.image === "1") return; // Burn-in / nicht als VTT
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = "Subtitles";
    track.srclang = "und";
    track.default = true;
    track.src = `/api/media/vtt?path=${encodeURIComponent(path)}&subtitle=${subIdx}`;
    v.appendChild(track);
    setTimeout(() => {
      if (v.textTracks && v.textTracks[0]) v.textTracks[0].mode = "showing";
    }, 200);
  }

  function playDirect(url) {
    const v = $("fp-video");
    if (!v) return;
    destroyPlayback();
    fp.mode = "direct";
    v.src = url;
    v.addEventListener("loadedmetadata", () => {
      if (fp.duration <= 0 && v.duration && isFinite(v.duration)) {
        fp.duration = v.duration;
      }
      v.play().catch(() => {});
      updateTimeUi();
    }, { once: true });
  }

  function playHls(url) {
    const v = $("fp-video");
    if (!v) return;
    destroyPlayback();
    fp.mode = "hls";
    const buf = fp.lookahead > 0
      ? Math.max(12, Math.min(90, fp.lookahead + 8))
      : 60;
    if (window.Hls && window.Hls.isSupported()) {
      fp.hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        maxBufferLength: buf,
        maxMaxBufferLength: buf + 20,
        liveSyncDurationCount: 3,
        liveMaxLatencyDurationCount: 10,
        liveDurationInfinity: true,
      });
      fp.hls.loadSource(url);
      fp.hls.attachMedia(v);
      fp.hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        v.play().catch(() => {});
      });
      fp.hls.on(window.Hls.Events.ERROR, (_, data) => {
        // Bei Puffer-Unterlauf Encode wieder anwerfen (falls gedrosselt)
        if (data && (data.details === "bufferStalledError"
            || data.details === "bufferNudgeOnStall")) {
          if (fp.encodePaused) setEncodePaused(false);
          return;
        }
        if (data.fatal) {
          setStatus(tt("Wiedergabefehler – Session neu starten."), true);
          setBadge(tt("Fehler"));
        }
      });
    } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = url;
      v.addEventListener("loadedmetadata", () => v.play().catch(() => {}), { once: true });
    } else {
      setStatus(tt("HLS wird von diesem Browser nicht unterstützt."), true);
    }
  }

  async function startSession(startSec, autoplay) {
    const path = ($("fp-path") || {}).value || fp.path;
    if (!path) {
      setStatus(tt("Bitte eine Datei wählen."), true);
      return;
    }
    fp.path = path;
    const audio = parseInt(($("fp-audio") || {}).value, 10);
    const sub = parseInt(($("fp-sub") || {}).value, 10);
    const profile = (($("fp-profile") || {}).value) || "auto";
    const platform = (($("fp-platform") || {}).value) || "auto";
    const codec = (($("fp-codec") || {}).value) || "h264";
    const height = parseInt(($("fp-height") || {}).value, 10) || 0;
    const vBitrate = parseInt(($("fp-vbr") || {}).value, 10) || 0;
    const lookahead = parseInt(($("fp-lookahead") || {}).value, 10);
    fp.lookahead = Number.isFinite(lookahead) ? lookahead : 30;
    try { localStorage.setItem("fpLookahead", String(fp.lookahead)); } catch (e) { /* ignore */ }
    const burn = !!($("fp-burn") && $("fp-burn").checked);
    const audioCopy = !!($("fp-audio-copy") && $("fp-audio-copy").checked);
    setBadge(tt("Lädt …"));
    setStatus(tt("Starte Session …"));
    await stopSession();

    const directOk = clientDirectOk(fp.info);
    try {
      const r = await fetch("/api/player/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path,
          audio: isNaN(audio) ? 0 : audio,
          subtitle: isNaN(sub) ? -1 : sub,
          start: Math.max(0, startSec || 0),
          profile,
          burn_subs: burn,
          client_direct_ok: directOk,
          platform,
          codec,
          height: profile === "custom" ? height : 0,
          // Bitrate bei allen Transcode-Profilen mitschicken (überschreibt Preset-Default)
          v_bitrate: ENCODE_PROFILES.includes(profile) ? vBitrate : 0,
          client_codecs: detectClientCodecs(),
          lookahead_sec: fp.lookahead,
          audio_copy: audioCopy,
        }),
      });
      const d = await r.json();
      if (d.error) {
        setStatus(d.error, true);
        setBadge(tt("Fehler"));
        return;
      }
      if (d.options) fillEncodeOptions(d.options);
      const sess = d.session || {};
      fp.sid = sess.id;
      fp.startOffset = sess.mode === "direct" ? 0 : (sess.start || 0);
      fp.duration = sess.duration || fp.duration || 0;
      fp.mode = sess.mode || "hls";
      fp.windowEnd = 0;
      fp.encodePaused = false;
      if ($("fp-audio-copy") && sess.audio_copy != null) {
        $("fp-audio-copy").checked = !!sess.audio_copy;
      }
      if (d.info) {
        fp.info = d.info;
        fillTracks(d.info);
        if ($("fp-audio") && sess.audio != null) $("fp-audio").value = String(sess.audio);
        if ($("fp-sub") && sess.subtitle != null) $("fp-sub").value = String(sess.subtitle);
      }
      renderChapters(d.chapters || []);
      updateAudioModeHint(sess);
      setDirty(false);
      if (sess.error && !sess.ready && sess.mode === "hls") {
        setStatus(sess.error, true);
        setBadge(tt("Fehler"));
        return;
      }

      if (sess.mode === "direct") {
        playDirect(sess.media_url || sess.playlist_url);
        setBadge(tt("Direct-Play"));
        setStatus(tt("Direct-Play · keine Server-Umcodierung")
          + (fp.duration ? ` · ${fmtClock(fp.duration)}` : ""));
      } else {
        const url = sess.playlist_url || `/api/player/session/${fp.sid}/index.m3u8`;
        let tries = 0;
        const waitReady = async () => {
          tries += 1;
          const st = await (await fetch(`/api/player/session/${fp.sid}`)).json();
          if (st.session && st.session.ready) return true;
          if (st.session && st.session.error) {
            setStatus(st.session.error, true);
            return false;
          }
          if (tries > 50) return !!st.session;
          await new Promise((res) => setTimeout(res, 150));
          return waitReady();
        };
        await waitReady();
        playHls(url);
        const burnNote = sess.burn_subs ? ` · ${tt("Bild-UT eingebrannt")}` : "";
        const resNote = sess.encoder && sess.encoder !== "copy"
          ? (sess.height
            ? ` · ${sess.height}p`
            : ` · ${tt("Original-Auflösung")}`)
            + (sess.v_bitrate ? ` @ ${sess.v_bitrate}k` : "")
            + ` · ${sess.platform}/${sess.codec || ""}/${sess.encoder}`
          : "";
        const laNote = sess.lookahead_sec > 0
          ? ` · ${tt("Puffer")} ${Math.round(sess.lookahead_sec)}s`
          : ` · ${tt("Puffer unbegrenzt")}`;
        const aNote = sess.audio_copy ? ` · ${tt("Ton-Copy")}` : "";
        const warn = sess.warning ? ` · ⚠ ${sess.warning}` : "";
        setBadge(sess.profile || tt("HLS"));
        setStatus(
          `${tt("HLS")} · ${sess.profile || "copy"}${resNote}${burnNote}${laNote}${aNote}${warn}`
          + ` · ${tt("Pause hält Encode an")}`
          + (fp.duration ? ` · ${fmtClock(fp.duration)}` : ""),
        );
        if (!isNaN(sub) && sub >= 0 && !sess.burn_subs) attachSubtitles(path, sub);
      }

      const ov = $("fp-overlay");
      if (ov) ov.style.display = "none";
      updateTimeUi();
      if (autoplay === false) {
        const v = $("fp-video");
        if (v) v.pause();
      }
      // Direct-Play Seek in Datei
      if (sess.mode === "direct" && startSec > 0) {
        const v = $("fp-video");
        if (v) {
          v.addEventListener("loadedmetadata", () => {
            try { v.currentTime = startSec; } catch (e) { /* ignore */ }
          }, { once: true });
        }
      }
    } catch (e) {
      setStatus(String(e), true);
      setBadge(tt("Fehler"));
    }
  }

  async function loadFile() {
    const path = ($("fp-path") || {}).value || "";
    if (!path.trim()) {
      setStatus(tt("Bitte eine Datei wählen."), true);
      return;
    }
    setStatus(tt("Analysiere …"));
    try {
      const info = await (await fetch(`/api/probe?path=${encodeURIComponent(path)}`)).json();
      if (info.error) {
        setStatus(info.error, true);
        return;
      }
      fp.info = info;
      fp.duration = info.duration || 0;
      fp.path = path;
      fillTracks(info);
      updateTimeUi();
      await startSession(0, true);
    } catch (e) {
      setStatus(String(e), true);
    }
  }

  async function loadOptions() {
    try {
      const d = await (await fetch("/api/player/options")).json();
      fillEncodeOptions(d);
    } catch (e) {
      fillEncodeOptions(null);
    }
  }

  function bindControls() {
    const v = $("fp-video");
    const seek = $("fp-seek");
    const play = $("fp-play");
    const vol = $("fp-vol");

    if (v) {
      v.addEventListener("timeupdate", () => {
        updateTimeUi();
        if (!v.paused) throttleEncodeByBuffer();
      });
      v.addEventListener("play", () => {
        if (play) play.textContent = "⏸";
        // Nach User-Pause: Encode wieder an, Puffer-Drossel übernimmt danach
        setEncodePaused(false);
      });
      v.addEventListener("pause", () => {
        if (play) play.textContent = "▶";
        // Seek-Scrubben nicht als Pause werten
        if (!fp.seeking) setEncodePaused(true);
      });
      v.addEventListener("click", () => {
        if (v.paused) v.play().catch(() => {});
        else v.pause();
      });
    }
    if (play) {
      play.addEventListener("click", () => {
        if (!v) return;
        if (v.paused) v.play().catch(() => {});
        else v.pause();
      });
    }
    const big = $("fp-big-play");
    if (big) big.addEventListener("click", () => loadFile());

    if (seek) {
      seek.addEventListener("input", () => {
        fp.seeking = true;
        const dur = fp.duration || 0;
        const t = (parseInt(seek.value, 10) / 1000) * dur;
        const time = $("fp-time");
        if (time) time.textContent = `${fmtClock(t)} / ${fmtClock(dur)}`;
      });
      const commit = () => {
        if (!fp.seeking) return;
        fp.seeking = false;
        const dur = fp.duration || 0;
        const t = (parseInt(seek.value, 10) / 1000) * dur;
        if (fp.mode === "direct" && v && isFinite(v.duration)) {
          try { v.currentTime = t; } catch (e) { startSession(t, true); }
          return;
        }
        startSession(t, true);
      };
      seek.addEventListener("change", commit);
      seek.addEventListener("pointerup", commit);
    }
    if (vol && v) {
      v.volume = (parseInt(vol.value, 10) || 90) / 100;
      vol.addEventListener("input", () => {
        v.volume = (parseInt(vol.value, 10) || 0) / 100;
      });
    }

    // Einstellungen nur markieren – Session startet erst mit „Übernehmen“
    // (oder initialem Laden / Seek / Kapitel).
    ["fp-audio", "fp-sub", "fp-profile", "fp-platform", "fp-codec", "fp-burn",
      "fp-audio-copy", "fp-height", "fp-vbr", "fp-lookahead"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      const onChange = () => {
        if (id === "fp-sub") syncBurnHint();
        if (id === "fp-profile") syncEncodeUi({ presetBr: true });
        if (id === "fp-audio" || id === "fp-profile" || id === "fp-audio-copy") {
          updateAudioModeHint(null);
        }
        if (!fp.path) return;
        setDirty(true);
        setStatus(tt("Änderungen noch nicht übernommen – „Übernehmen“ klicken."));
      };
      el.addEventListener("change", onChange);
      if (id === "fp-height" || id === "fp-vbr") el.addEventListener("input", onChange);
    });

    try {
      const savedLa = localStorage.getItem("fpLookahead");
      if (savedLa && $("fp-lookahead")) $("fp-lookahead").value = savedLa;
    } catch (e) { /* ignore */ }

    const apply = $("fp-apply");
    if (apply) apply.addEventListener("click", () => {
      if (!fp.path) return;
      startSession(absoluteTime(), true);
    });

    const stop = $("fp-stop");
    if (stop) stop.addEventListener("click", async () => {
      await stopSession();
      setDirty(false);
      setStatus("");
      updateTimeUi();
      updateAudioModeHint(null);
    });

    const load = $("fp-load");
    if (load) load.addEventListener("click", loadFile);

    const unload = $("fp-unload");
    if (unload) unload.addEventListener("click", unloadVideo);

    const browse = $("fp-browse");
    if (browse) browse.addEventListener("click", () => {
      if (typeof window.openFilePickerModal !== "function") {
        setStatus(tt("Dateiauswahl nicht verfügbar."), true);
        return;
      }
      window.openFilePickerModal({
        title: tt("Video für Player wählen"),
        onPick: (f) => {
          if ($("fp-path")) $("fp-path").value = f.rel;
          loadFile();
        },
      });
    });
  }

  window.openFullPlayer = async function openFullPlayer(rel, name) {
    if (typeof window.navTo === "function") window.navTo("player");
    else {
      const btn = document.querySelector('[data-nav="player"]');
      if (btn) btn.click();
    }
    if ($("fp-path")) $("fp-path").value = rel || "";
    setStatus(name ? `${tt("Lade")} ${name} …` : tt("Lade …"));
    setTimeout(() => loadFile(), 50);
  };

  window.stopFullPlayer = stopSession;

  function initFullPlayer() {
    if (!$("fp-video")) return;
    bindControls();
    loadOptions();
    updateTimeUi();
  }

  document.addEventListener("DOMContentLoaded", initFullPlayer);
})();
