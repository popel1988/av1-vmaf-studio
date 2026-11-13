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
    hdrCodec: "",
    extending: false,
    encodePaused: false,
    plan: null,
    starting: false,
    statsOpen: false,
    statsTimer: 0,
    sessMeta: null,
    audioDelayMs: 0,
    subDelayMs: 0,
    negSessionDelayMs: 0,
    negAudioTimer: 0,
    audioCtx: null,
    audioSrcNode: null,
    audioDelayNode: null,
  };

  const ENCODE_PROFILES = ["original", "1080p", "720p", "480p", "custom"];
  const PRESET_BR = { original: 8000, "1080p": 6000, "720p": 3500, "480p": 1500 };

  function prettyCodec(c) {
    const x = String(c || "").toLowerCase().trim();
    if (!x || x === "?" || x === "-") return "";
    if (x.startsWith("h264") || x.startsWith("avc")) return "H.264";
    if (x.startsWith("hevc") || x.startsWith("h265")) return "HEVC";
    if (x.startsWith("av1") || x.startsWith("av01")) return "AV1";
    if (x.startsWith("vp9")) return "VP9";
    if (x.startsWith("vp8")) return "VP8";
    if (x.startsWith("mpeg2") || x === "mpeg2video") return "MPEG-2";
    if (x.startsWith("mpeg4")) return "MPEG-4";
    return String(c).toUpperCase();
  }

  function fmtClock(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }

  function fmtClockPrecise(s) {
    const val = Math.max(0, Number(s) || 0);
    const whole = Math.floor(val);
    const ms = String(Math.min(999, Math.round((val - whole) * 1000))).padStart(3, "0");
    return `${fmtClock(whole)}.${ms}`;
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

  const PLAN_KIND = {
    direct: {
      cls: "fp-kind-direct", label: "Direct-Play",
      tip: "Datei geht unverändert in den Browser – kein Remux, kein Encode. Nur bei MP4/WebM mit passendem Codec.",
    },
    copy: {
      cls: "fp-kind-copy", label: "Remux",
      tip: "Video und Ton werden nur nach fMP4/HLS umgepackt, nicht neu encodiert. Günstig bei MKV, wenn der Browser den Codec kann.",
    },
    audio: {
      cls: "fp-kind-audio", label: "Remux · Ton→AAC",
      tip: "Video bleibt Copy (z. B. HEVC als hvc1). Nur der Ton wird nach Stereo-AAC gewandelt, damit HLS im Browser Ton hat.",
    },
    silent: {
      cls: "fp-kind-silent", label: "Remux · ohne Ton",
      tip: "Video-Copy ohne Tonspur: der Audiocodec ist für HLS ungeeignet und „Ton nicht umcodieren“ ist aktiv.",
    },
    transcode: {
      cls: "fp-kind-xcode", label: "Transcode",
      tip: "Video wird neu encodiert (Auflösung, Codec, ggf. HDR→SDR), weil der Browser die Quelle so nicht abspielen kann.",
    },
    idle: { cls: "fp-kind-idle", label: "Kein Video", tip: "" },
  };

  function planKind(plan, sess) {
    const strat = (plan && plan.strategy) || (sess && sess.decision_strategy) || "";
    if (strat === "direct" || (sess && sess.mode === "direct")) return "direct";
    if (strat === "remux_audio_xcode") return "audio";
    if (strat === "remux_silent") return "silent";
    if (strat === "remux_copy") return "copy";
    if (strat === "transcode") return "transcode";
    if (sess) {
      if (sess.mode === "direct") return "direct";
      if (sess.encoder === "copy" || sess.profile === "copy") {
        if (sess.audio_mode === "transcode") return "audio";
        if (sess.audio_mode === "none") return "silent";
        return "copy";
      }
      if (sess.encoder && sess.encoder !== "direct") return "transcode";
    }
    return "idle";
  }

  function renderPlan(plan, sess) {
    fp.plan = plan || null;
    const badge = $("fp-plan-badge");
    const facts = $("fp-plan-facts");
    const more = $("fp-plan-more");
    const reasonsEl = $("fp-plan-reasons");
    if (!badge || !facts) return;

    const kind = plan || sess ? planKind(plan, sess) : "idle";
    const meta = PLAN_KIND[kind] || PLAN_KIND.idle;
    badge.className = `fp-plan-badge ${meta.cls}`;
    badge.textContent = tt(meta.label);
    if (meta.tip) badge.setAttribute("data-tip", tt(meta.tip));
    else badge.removeAttribute("data-tip");

    const rows = [];
    const file = (plan && plan.file) || {};
    const browser = (plan && plan.browser) || {};
    const enc = (plan && plan.encode) || {};

    const srcCodec = prettyCodec(file.video || (fp.info && fp.info.codec) || "");
    const outCodec = prettyCodec((sess && sess.codec) || enc.codec || "");
    let codecLabel = srcCodec;
    if (plan && plan.need_encode && outCodec && outCodec !== srcCodec) {
      codecLabel = srcCodec ? `${srcCodec} → ${outCodec}` : outCodec;
    }
    const vBits = [
      codecLabel,
      file.height ? `${file.height}p` : "",
      file.hdr ? "HDR" : "",
    ].filter(Boolean);
    if (vBits.length) {
      let how = "";
      if (plan && plan.use_video_copy) how = ` · ${tt("Copy")}`;
      else if (plan && plan.need_encode) how = ` · ${tt("Encode")}`;
      rows.push([tt("Video"), vBits.join(" · ") + how]);
    }

    const ac = (file.audio || "").toUpperCase();
    if (ac && ac !== "-") {
      let how = "";
      if (plan && plan.drop_audio) how = ` · ${tt("weggelassen")}`;
      else if (plan && plan.will_xcode_audio) how = " → AAC";
      else if (ac) how = ` · ${tt("Copy")}`;
      rows.push([tt("Ton"), ac + how]);
    } else if (plan && plan.drop_audio) {
      rows.push([tt("Ton"), tt("Kein Ton")]);
    }

    const codecs = (browser.codecs || []).map((c) => String(c).toUpperCase());
    if (codecs.length) rows.push([tt("Browser"), codecs.join(", ")]);

    if (kind === "transcode") {
      const plat = (sess && sess.platform) || enc.platform || "";
      const codec = (sess && sess.codec) || enc.codec || "";
      const encoder = (sess && sess.encoder) || enc.encoder || "";
      const hw = [plat, codec && prettyCodec(codec), encoder].filter(Boolean).join(" / ");
      if (hw) rows.push([tt("Hardware"), hw]);
      const res = sess && sess.height
        ? `${sess.height}p${sess.v_bitrate ? ` · ${sess.v_bitrate} kbit/s` : ""}`
        : (plan && plan.height ? `${plan.height}p` : tt("Original"));
      if (res) rows.push([tt("Zielauflösung"), res]);
      if (sess && sess.look_summary) {
        rows.push([tt("Encode-Vorschau"), sess.look_summary]);
      }
    }

    const la = (sess && sess.lookahead_sec != null) ? sess.lookahead_sec : fp.lookahead;
    if (kind !== "direct" && kind !== "idle" && la != null) {
      rows.push([tt("Puffer"), Number(la) > 0 ? `${Math.round(la)} s` : tt("unbegrenzt")]);
    }

    const summary = (plan && plan.summary) || (sess && sess.decision_summary) || "";
    if (summary && !rows.length) {
      rows.push([tt("Modus"), summary]);
    }

    facts.innerHTML = rows.map(([k, v]) =>
      `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");

    const reasons = (plan && Array.isArray(plan.reasons) ? plan.reasons : null)
      || (sess && Array.isArray(sess.decision_reasons) ? sess.decision_reasons : []);
    if (more && reasonsEl) {
      if (reasons.length) {
        more.hidden = false;
        reasonsEl.innerHTML = reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("");
      } else {
        more.hidden = true;
        reasonsEl.innerHTML = "";
      }
    }
  }

  function setActiveMode(label) {
    const badge = $("fp-plan-badge");
    if (!badge || fp.plan || !label) return;
    badge.className = "fp-plan-badge fp-kind-idle";
    badge.textContent = label;
  }

  function setDecision(plan) {
    renderPlan(plan, null);
  }

  async function refreshPlan() {
    const path = ($("fp-path") || {}).value || fp.path;
    if (!path) {
      setDecision(null);
      return null;
    }
    const audio = parseInt(($("fp-audio") || {}).value, 10);
    const sub = parseInt(($("fp-sub") || {}).value, 10);
    const profile = (($("fp-profile") || {}).value) || "auto";
    const platform = (($("fp-platform") || {}).value) || "auto";
    const codec = (($("fp-codec") || {}).value) || "h264";
    const height = parseInt(($("fp-height") || {}).value, 10) || 0;
    const vBitrate = parseInt(($("fp-vbr") || {}).value, 10) || 0;
    const burn = !!($("fp-burn") && $("fp-burn").checked);
    const audioCopy = !!($("fp-audio-copy") && $("fp-audio-copy").checked);
    try {
      const d = await (await fetch("/api/player/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path,
          audio: isNaN(audio) ? 0 : audio,
          subtitle: isNaN(sub) ? -1 : sub,
          profile,
          burn_subs: burn,
          client_direct_ok: clientDirectOk(fp.info),
          platform,
          codec,
          height: profile === "custom" ? height : 0,
          v_bitrate: ENCODE_PROFILES.includes(profile) ? vBitrate : 0,
          client_codecs: detectClientCodecs(),
          audio_copy: audioCopy,
          audio_delay_ms: fp.audioDelayMs < 0 ? fp.audioDelayMs : 0,
          look: playerLook(),
        }),
      })).json();
      if (d.error) {
        setDecision({ summary: d.error, reasons: [] });
        return null;
      }
      setDecision(d.plan || null);
      return d.plan || null;
    } catch (e) {
      setDecision(null);
      return null;
    }
  }

  function absoluteTime() {
    const v = $("fp-video");
    const cur = v ? (v.currentTime || 0) : 0;
    const t = fp.startOffset + cur;
    if (t <= 0 && fp.lastPosition > 0 && (!v || !v.src)) {
      return fp.lastPosition;
    }
    return t;
  }

  function updateTimeUi() {
    const seek = $("fp-seek");
    const time = $("fp-time");
    const t = absoluteTime();
    if (t > 0) fp.lastPosition = t;
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
    if (fp.hdrCodec && !out.includes(fp.hdrCodec)) out.push(fp.hdrCodec);
    return out;
  }

  function evenSize(value, fallback) {
    const n = Math.round(Number(value) || fallback);
    return Math.max(2, n - (n % 2));
  }

  function hdrContentType(info) {
    const codec = String((info && info.codec) || "").toLowerCase();
    const height = Number(info && info.height) || 0;
    let family = "";
    let contentType = "";
    if (codec.startsWith("hev") || codec.includes("h265")) {
      family = "hevc";
      const level = height > 2160 ? "L183" : (height > 1080 ? "L153" : "L123");
      contentType = `video/mp4; codecs="hvc1.2.4.${level}.B0"`;
    } else if (codec.startsWith("av1") || codec.startsWith("av01")) {
      family = "av1";
      const level = height > 2160 ? "16" : (height > 1080 ? "13" : "08");
      contentType = `video/mp4; codecs="av01.0.${level}M.10"`;
    }
    return family ? { codec: family, contentType } : null;
  }

  function hdrTonemapChoice(info, supported) {
    if (!info || !info.is_hdr) return { on: false, codec: "", reason: "" };
    if (info.dolby_vision && Number(info.dv_profile) === 5) {
      return { on: true, codec: "", reason: "Dolby Vision Profil 5 hat keine HDR10-Basis. Tonemapping ist nötig." };
    }
    const spec = hdrContentType(info);
    if (!spec) {
      return { on: true, codec: "", reason: "Dieses HDR-Format kann der Browser nicht direkt abspielen. Tonemapping ist an." };
    }
    if (supported) {
      return { on: false, codec: spec.codec, reason: "Dieser Browser kann den HDR-Pfad abspielen. Tonemapping bleibt aus." };
    }
    return { on: true, codec: "", reason: "Dieser Browser kann den HDR-Pfad nicht abspielen. Tonemapping ist an." };
  }

  async function queryHdrPlayback(info) {
    if (!info || !info.is_hdr || (info.dolby_vision && Number(info.dv_profile) === 5)) {
      return hdrTonemapChoice(info, false);
    }
    const spec = hdrContentType(info);
    if (!spec) return hdrTonemapChoice(info, false);
    const transfer = String(info.color_transfer || "").toLowerCase();
    const video = {
      contentType: spec.contentType,
      width: evenSize(info.width, 1920),
      height: evenSize(info.height, 1080),
      bitrate: Math.max(1, Math.round(Number(info.video_bitrate || info.bit_rate) || 15000000)),
      framerate: Number(info.fps) > 0 ? Number(info.fps) : 24,
      hdrMetadataType: "smpteSt2086",
      colorGamut: "bt2020",
      transferFunction: transfer.includes("hlg") || transfer.includes("arib") ? "hlg" : "pq",
    };
    let supported = false;
    const fallback = () => {
      try {
        return !!document.createElement("video").canPlayType(spec.contentType);
      } catch (e) {
        return false;
      }
    };
    try {
      if (navigator.mediaCapabilities && navigator.mediaCapabilities.decodingInfo) {
        const result = await navigator.mediaCapabilities.decodingInfo({ type: "media-source", video });
        supported = !!(result && result.supported);
      } else {
        supported = fallback();
      }
    } catch (e) {
      supported = fallback();
    }
    return hdrTonemapChoice(info, supported);
  }

  function setTonemapReason(text) {
    const reason = $("fp-tonemap-reason");
    if (!reason) return;
    reason.hidden = !text;
    reason.textContent = text ? tt(text) : "";
  }

  async function applyHdrCheck(info) {
    const decision = await queryHdrPlayback(info);
    fp.hdrCodec = decision.codec || "";
    const box = $("fp-tonemap");
    if (box) box.checked = decision.on;
    setTonemapReason(decision.reason);
  }

  function enforceTonemapForH264() {
    const hdr = !!(fp.info && fp.info.is_hdr);
    const codec = (($("fp-codec") || {}).value) || "h264";
    const filters = ["fp-deband", "fp-denoise", "fp-sharpen", "fp-grain"].some((id) => {
      const el = $(id);
      return !!(el && el.value && el.value !== "off");
    });
    const burn = !!($("fp-burn") && $("fp-burn").checked);
    if (!(hdr && codec === "h264" && (filters || burn))) return;
    const box = $("fp-tonemap");
    if (box) box.checked = true;
    setTonemapReason("H.264 kann diese HDR-Quelle nicht tragen. Tonemapping ist für den Transcode nötig.");
  }

  function syncLookUi() {
    const hasFile = !!fp.info;
    const lookWrap = $("fp-look");
    if (lookWrap) lookWrap.hidden = !hasFile;
    const hdr = !!(fp.info && fp.info.is_hdr);
    const prof = (($("fp-profile") || {}).value) || "auto";
    const fixedCopy = prof === "direct" || prof === "copy";
    const showTone = hdr && !fixedCopy;
    const toggle = $("fp-tonemap-toggle");
    if (toggle) toggle.hidden = !showTone;
    const enabled = !!($("fp-tonemap") && $("fp-tonemap").checked);
    const box = $("fp-tonemap-opts");
    if (box) box.hidden = !(showTone && enabled);
    if (!showTone) setTonemapReason("");

    const rawPlat = (($("fp-platform") || {}).value) || "auto";
    const plat = rawPlat === "auto"
      ? ((fp.options && fp.options.transcode_platform) || "cpu")
      : rawPlat;
    const jfList = fp.options && Array.isArray(fp.options.jellyfin_filters)
      ? fp.options.jellyfin_filters
      : null;
    const hasJf = (f) => !jfList || jfList.includes(f);
    const intelVaapi = !(fp.options && fp.options.intel_uses_vaapi === false);
    const engine = $("fp-tonemap-engine");
    if (engine) {
      [...engine.options].forEach((opt) => {
        let ok = true;
        if (opt.value === "libplacebo") ok = plat === "cpu" || plat === "nvidia";
        else if (opt.value === "cuda") ok = plat === "nvidia" && hasJf("tonemap_cuda");
        else if (opt.value === "opencl") ok = hasJf("tonemap_opencl");
        else if (opt.value === "vaapi") {
          ok = (plat === "amd" || (plat === "intel" && intelVaapi)) && hasJf("tonemap_vaapi");
        } else if (opt.value === "tonemapx") ok = hasJf("tonemapx");
        opt.disabled = !ok;
        opt.hidden = !ok;
      });
      if (engine.selectedOptions[0] && engine.selectedOptions[0].disabled) {
        engine.value = "cpu";
      }
    }

    const engVal = engine ? engine.value : "cpu";
    const algo = $("fp-tonemap-algo");
    if (algo) {
      const vaapiEngine = engVal === "vaapi";
      algo.disabled = vaapiEngine;
      [...algo.options].forEach((opt) => {
        const ok = !(engVal === "cpu" && opt.value === "bt2390");
        opt.disabled = !ok;
        opt.hidden = !ok;
      });
      if (algo.selectedOptions[0] && algo.selectedOptions[0].disabled) {
        algo.value = "hable";
      }
    }

    const showPeak = showTone && engVal !== "libplacebo";
    document.querySelectorAll("#fp-tonemap-opts .tonemap-cpu-only").forEach((el) => {
      el.hidden = !showPeak;
    });
    const el = $("fp-tonemap-desat");
    const lab = $("fp-tonemap-desat-val");
    if (el && lab) {
      const n = parseFloat(el.value);
      lab.textContent = (Number.isFinite(n) ? n : 0).toFixed(1).replace(".", ",");
    }
    const hint = $("fp-look-hint");
    if (hint && hasFile) {
      if (fixedCopy) {
        hint.textContent = tt("Direct-Play und Remux zeigen die Datei unverändert. Stellst du unten einen Filter um, wechselt der Modus auf Automatisch und startet den Transcode an der aktuellen Stelle.");
      } else if (hdr) {
        hint.textContent = tt("HDR-Quelle erkannt: Tonemap-Engine, Kurve, Peak, Entsättigung und Bildfilter starten die Vorschau nach einer Änderung sofort an der aktuellen Stelle neu. CUDA, OpenCL, VAAPI und tonemapx laufen über Jellyfin-FFmpeg, CPU und libplacebo über BtbN.");
      } else {
        hint.textContent = tt("SDR-Quelle: Tonemap entfällt. Deband, Denoise, Schärfen und Korn starten die Vorschau nach einer Änderung sofort an der aktuellen Stelle neu.");
      }
    }
  }

  function playerLook() {
    enforceTonemapForH264();
    const hdr = !!(fp.info && fp.info.is_hdr);
    const tonemap = hdr && !!($("fp-tonemap") && $("fp-tonemap").checked);
    let desat = parseFloat(($("fp-tonemap-desat") || {}).value);
    if (!Number.isFinite(desat)) desat = 0;
    const peakEl = $("fp-tonemap-peak");
    let peak = 0;
    if (peakEl && String(peakEl.value).trim() !== "") {
      peak = parseFloat(peakEl.value);
      if (!Number.isFinite(peak) || peak < 0) peak = 0;
    }
    const level = (id) => {
      const el = $(id);
      return el ? el.value : "off";
    };
    return {
      apply: true,
      tonemap,
      platform: (($("fp-platform") || {}).value) || "auto",
      tonemap_engine: (($("fp-tonemap-engine") || {}).value) || "cpu",
      tonemap_algo: (($("fp-tonemap-algo") || {}).value) || "hable",
      tonemap_desat: Math.round(desat * 100) / 100,
      tonemap_peak: peak,
      deband: level("fp-deband"),
      denoise: level("fp-denoise"),
      sharpen: level("fp-sharpen"),
      grain: level("fp-grain"),
    };
  }

  function ensurePreviewMode() {
    const sel = $("fp-profile");
    if (!sel) return;
    if (sel.value === "direct" || sel.value === "copy") {
      sel.value = "auto";
      syncEncodeUi();
    }
  }

  function queueLookApply() {
    if (!fp.path) return;
    enforceTonemapForH264();
    syncLookUi();
    if (fp.lookTimer) clearTimeout(fp.lookTimer);
    fp.lookTimer = setTimeout(() => {
      fp.lookTimer = 0;
      if (fp.starting) {
        fp.lookAgain = true;
        return;
      }
      ensurePreviewMode();
      startSession(absoluteTime(), true);
    }, 450);
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
    const canTranscode = prof !== "direct" && prof !== "copy";
    ["fp-platform", "fp-codec"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = !canTranscode;
    });
    const hw = $("fp-height-wrap");
    const bw = $("fp-vbr-wrap");
    if (hw) hw.style.display = prof === "custom" ? "" : "none";
    if (bw) bw.style.display = encode ? "" : "none";
    // Preset gewechselt → Default-Bitrate vorschlagen (nur wenn gewünscht)
    if (opts && opts.presetBr && PRESET_BR[prof] && $("fp-vbr")) {
      $("fp-vbr").value = String(PRESET_BR[prof]);
    }
    syncLookUi();
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
    const ahead = bufferedAhead();
    const target = fp.lookahead;
    if (ahead >= target - 0.5 && !fp.encodePaused) {
      setEncodePaused(true);
    } else if (ahead < target * 0.4 && fp.encodePaused) {
      setEncodePaused(false);
    }
  }

  function platformCodecIds(platformId) {
    const platforms = (fp.options && fp.options.platforms) || [];
    const p = platforms.find((x) => x.id === platformId);
    if (platformId === "auto" || !p) return ["h264", "hevc", "av1"];
    const list = Array.isArray(p.codecs) ? p.codecs.slice() : [];
    return list.length ? list : (platformId === "cpu" ? ["h264"] : []);
  }

  function syncCodecOptionsForPlatform() {
    const codec = $("fp-codec");
    if (!codec || !fp.options) return;
    const client = detectClientCodecs();
    const platId = (($("fp-platform") || {}).value) || "auto";
    const allowed = new Set(platformCodecIds(platId));
    const cur = codec.value || "h264";
    const all = (fp.options.codecs) || [];
    codec.innerHTML = all.map((c) => {
      const hwOk = allowed.has(c.id);
      const brOk = client.includes(c.id);
      let lab = c.label;
      if (!hwOk) lab += ` (${tt("auf dieser HW nicht verfügbar")})`;
      else if (!brOk) lab += ` (${tt("Browser: Fallback H.264")})`;
      const dis = hwOk ? "" : " disabled";
      return `<option value="${c.id}"${dis}>${escapeHtml(lab)}</option>`;
    }).join("");
    if ([...codec.options].some((o) => o.value === cur && !o.disabled)) {
      codec.value = cur;
    } else {
      const fallback = ["h264", "hevc", "av1"].find((id) =>
        [...codec.options].some((o) => o.value === id && !o.disabled));
      if (fallback) codec.value = fallback;
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
        const note = (p.id !== "auto" && p.codecs && p.codecs.length)
          ? ` (${p.codecs.map((c) => c.toUpperCase()).join(", ")})`
          : "";
        return `<option value="${p.id}"${dis}>${escapeHtml(p.label + note)}</option>`;
      }).join("");
      if ([...plat.options].some((o) => o.value === cur && !o.disabled)) plat.value = cur;
    }

    syncCodecOptionsForPlatform();

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
        .map((p) => `${p.id}[${(p.codecs || []).join("/")}]`)
        .join(", ");
      hw.textContent = `${ready} · ${tt("Browser kann")}: ${client.join(", ").toUpperCase()}`
        + (plats ? ` · ${tt("Server")}: ${plats}` : "");
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
    const sidecars = ((info && info.sidecar_subtitles) || []).filter((sc) => !sc.bitmap);
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
      }).join("") +
      sidecars.map((sc, idx) => {
        const lab = ["Extern", sc.name || "Sidecar", sc.language || "und", sc.forced ? "Forced" : "", sc.sdh ? "SDH" : ""]
          .filter(Boolean).join(" · ");
        return `<option value="sidecar:${idx}" data-image="0" data-sidecar-path="${escapeHtml(sc.rel || sc.path || "")}">${escapeHtml(lab)}</option>`;
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
    const provenance = document.getElementById("fp-provenance");
    if (provenance) provenance.innerHTML = "";
    if (fp.sid) {
      try { await fetch(`/api/player/session/${fp.sid}`, { method: "DELETE" }); }
      catch (e) { /* ignore */ }
      fp.sid = null;
    }
    setBadge(tt("Bereit"));
    setActiveMode(fp.path
      ? tt("Gestoppt – erneut „Laden“ oder „Übernehmen“")
      : tt("Kein Video geladen"));
    const ov = $("fp-overlay");
    if (ov) ov.style.display = "";
  }

  async function unloadVideo() {
    await stopSession();
    fp.path = "";
    fp.info = null;
    syncLookUi();
    fp.chapters = [];
    fp.duration = 0;
    fp.startOffset = 0;
    fp.lastPosition = 0;
    fp.windowEnd = 0;
    fp.encodePaused = false;
    fp.mode = "hls";
    fp.audioDelayMs = 0;
    fp.subDelayMs = 0;
    fp.negSessionDelayMs = 0;
    if ($("fp-aud-delay")) $("fp-aud-delay").value = "0";
    if ($("fp-sub-delay")) $("fp-sub-delay").value = "0";
    if (fp.audioDelayNode) fp.audioDelayNode.delayTime.value = 0;
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
    setDecision(null);
    updateTimeUi();
    updateAudioModeHint(null);
    const play = $("fp-play");
    if (play) play.textContent = "▶";
  }

  function ensureWebAudioDelay() {
    const v = $("fp-video");
    if (!v) return null;
    if (fp.audioDelayNode) {
      if (fp.audioCtx && fp.audioCtx.state === "suspended") {
        fp.audioCtx.resume().catch(() => {});
      }
      return fp.audioDelayNode;
    }
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try {
      const ctx = new Ctx();
      const src = ctx.createMediaElementSource(v);
      const dly = ctx.createDelay(5.0);
      dly.delayTime.value = 0;
      src.connect(dly);
      dly.connect(ctx.destination);
      fp.audioCtx = ctx;
      fp.audioSrcNode = src;
      fp.audioDelayNode = dly;
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      return dly;
    } catch (e) {
      return null;
    }
  }

  function applyLiveAudioDelay(ms) {
    ms = Math.max(-15000, Math.min(5000, Math.round(Number(ms) || 0)));
    fp.audioDelayMs = ms;
    if ($("fp-aud-delay") && document.activeElement !== $("fp-aud-delay")) {
      $("fp-aud-delay").value = String(ms);
    }
    if (fp.negAudioTimer) {
      clearTimeout(fp.negAudioTimer);
      fp.negAudioTimer = 0;
    }
    if (ms > 0) {
      const dly = ensureWebAudioDelay();
      if (dly) dly.delayTime.value = Math.min(5.0, ms / 1000);
      if (fp.negSessionDelayMs < 0 && fp.path && fp.sid) {
        const v = $("fp-video");
        fp.negAudioTimer = setTimeout(() => {
          startSession(absoluteTime(), !(v && v.paused));
        }, 250);
      } else {
        setStatus(`Ton-Versatz live: +${ms} ms`);
      }
    } else if (ms === 0) {
      if (fp.audioDelayNode) fp.audioDelayNode.delayTime.value = 0;
      if (fp.negSessionDelayMs < 0 && fp.path && fp.sid) {
        const v = $("fp-video");
        fp.negAudioTimer = setTimeout(() => {
          startSession(absoluteTime(), !(v && v.paused));
        }, 250);
      } else {
        setStatus("Ton-Versatz: 0 ms");
      }
    } else {
      if (fp.audioDelayNode) fp.audioDelayNode.delayTime.value = 0;
      if (fp.path && fp.sid) {
        const v = $("fp-video");
        setStatus(`Ton-Versatz ${ms} ms wird per FFmpeg -itsoffset angewendet …`);
        fp.negAudioTimer = setTimeout(() => {
          startSession(absoluteTime(), !(v && v.paused));
        }, 450);
      }
    }
  }

  function applyLiveSubDelay(ms) {
    if (ms !== undefined) {
      fp.subDelayMs = Math.max(-15000, Math.min(15000, Math.round(Number(ms) || 0)));
      if ($("fp-sub-delay") && document.activeElement !== $("fp-sub-delay")) {
        $("fp-sub-delay").value = String(fp.subDelayMs);
      }
    }
    const v = $("fp-video");
    if (!v || !v.textTracks || !v.textTracks.length) return;
    const deltaSec = (fp.subDelayMs || 0) / 1000;
    for (let t = 0; t < v.textTracks.length; t += 1) {
      const ttObj = v.textTracks[t];
      const cues = ttObj && ttObj.cues;
      if (!cues) continue;
      for (let i = 0; i < cues.length; i += 1) {
        const c = cues[i];
        if (c._origStart === undefined) {
          c._origStart = c.startTime;
          c._origEnd = c.endTime;
        }
        const ns = Math.max(0, c._origStart + deltaSec);
        const ne = Math.max(ns + 0.05, c._origEnd + deltaSec);
        try {
          c.startTime = ns;
          c.endTime = ne;
        } catch (e) { /* ignore read-only cue edge case */ }
      }
    }
  }

  function attachSubtitles(path, subVal) {
    const v = $("fp-video");
    if (!v) return;
    [...v.querySelectorAll("track")].forEach((t) => t.remove());
    const raw = String(subVal != null ? subVal : (($("fp-sub") && $("fp-sub").value) || "-1"));
    if (raw === "-1" || raw === "") return;
    const opt = ($("fp-sub") || {}).selectedOptions;
    if (opt && opt[0] && opt[0].dataset.image === "1") return; // Burn-in / nicht als VTT
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = "Subtitles";
    track.srclang = "und";
    track.default = true;
    if (raw.startsWith("sidecar:")) {
      const scPath = (opt && opt[0] && opt[0].dataset.sidecarPath) || "";
      if (!scPath) return;
      track.src = `/api/media/vtt?path=${encodeURIComponent(path)}&sidecar=${encodeURIComponent(scPath)}`;
    } else {
      const subIdx = parseInt(raw, 10);
      if (isNaN(subIdx) || subIdx < 0) return;
      track.src = `/api/media/vtt?path=${encodeURIComponent(path)}&subtitle=${subIdx}`;
    }
    track.addEventListener("load", () => {
      if (v.textTracks && v.textTracks[0]) v.textTracks[0].mode = "showing";
      applyLiveSubDelay();
    });
    v.appendChild(track);
    setTimeout(() => {
      if (v.textTracks && v.textTracks[0]) v.textTracks[0].mode = "showing";
      applyLiveSubDelay();
    }, 250);
  }

  async function reportClientError(payload) {
    if (!fp.sid) return;
    try {
      const d = await (await fetch(`/api/player/session/${fp.sid}/client-error`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      })).json();
      if (d && d.ffmpeg_log_tail) {
        console.warn("[player] ffmpeg log:\n", d.ffmpeg_log_tail);
      }
      if (d && d.debug_cmd) {
        console.warn("[player] ffmpeg cmd:", d.debug_cmd);
      }
      if (d && d.session_error) {
        setStatus(
          `${tt("Wiedergabefehler")} – ${String(d.session_error).slice(0, 220)}`,
          true,
        );
      }
    } catch (e) { /* ignore */ }
  }

  function playDirect(url, autoplay = true) {
    const v = $("fp-video");
    if (!v) return;
    destroyPlayback();
    fp.mode = "direct";
    v.src = url;
    v.addEventListener("loadedmetadata", () => {
      if (fp.duration <= 0 && v.duration && isFinite(v.duration)) {
        fp.duration = v.duration;
      }
      if (autoplay !== false) v.play().catch(() => {});
      else v.pause();
      updateTimeUi();
    }, { once: true });
  }

  function playHls(url, autoplay = true) {
    const v = $("fp-video");
    if (!v) return;
    destroyPlayback();
    fp.mode = "hls";
    // EVENT-Playlist (Jellyfin): wächst vom Seek-Punkt, kein Sliding-Window.
    // Bei Segment 0 starten – nicht an die Live-Kante springen.
    const la = fp.lookahead > 0 ? fp.lookahead : 60;
    const buf = Math.max(12, Math.min(120, la + 8));
    if (window.Hls && window.Hls.isSupported()) {
      fp.hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        maxBufferLength: buf,
        maxMaxBufferLength: buf + 30,
        backBufferLength: Infinity,
        maxBufferHole: 0.5,
        startPosition: 0,
        liveSyncDurationCount: 3,
        liveMaxLatencyDurationCount: Infinity,
        maxLiveSyncPlaybackRate: 1,
        liveDurationInfinity: false,
      });
      fp.hls.loadSource(url);
      fp.hls.attachMedia(v);
      fp.hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        if (autoplay !== false) v.play().catch(() => {});
        else v.pause();
      });
      fp.hls.on(window.Hls.Events.ERROR, (_, data) => {
        if (!data) return;
        const details = String(data.details || "");
        if (details === "bufferStalledError" || details === "bufferNudgeOnStall"
            || details === "bufferSeekOverHole") {
          if (fp.encodePaused) setEncodePaused(false);
          return;
        }
        if (details === "fragLoadError" || details === "fragLoadTimeOut") {
          if (fp.encodePaused) setEncodePaused(false);
          if (data.fatal && fp.hls) {
            try { fp.hls.startLoad(); } catch (e) { /* ignore */ }
          }
          return;
        }
        if (data.fatal) {
          const errMsg = (data.error && data.error.message) || data.details || "fatal";
          setStatus(`${tt("Wiedergabefehler")} (${data.type || "?"}: ${errMsg})`, true);
          setBadge(tt("Fehler"));
          reportClientError({
            type: String(data.type || ""),
            details: String(data.details || ""),
            fatal: true,
            error: String(errMsg),
            url: url,
            note: "hls.js fatal",
          });
        }
      });
    } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = url;
      v.addEventListener("loadedmetadata", () => {
        if (autoplay !== false) v.play().catch(() => {});
        else v.pause();
      }, { once: true });
    } else {
      setStatus(tt("HLS wird von diesem Browser nicht unterstützt."), true);
    }
  }

  async function startSession(startSec, autoplay) {
    if (fp.starting) return;
    const path = ($("fp-path") || {}).value || fp.path;
    if (!path) {
      setStatus(tt("Bitte eine Datei wählen."), true);
      return;
    }
    fp.starting = true;
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

    const targetStart = Math.max(0, startSec || 0);
    fp.lastPosition = targetStart;
    fp.startOffset = targetStart;
    const directOk = clientDirectOk(fp.info);
    const rawSubVal = ($("fp-sub") && $("fp-sub").value) || "-1";
    const negAudMs = fp.audioDelayMs < 0 ? fp.audioDelayMs : 0;
    const body = {
      path,
      audio: isNaN(audio) ? 0 : audio,
      subtitle: isNaN(sub) ? -1 : sub,
      start: targetStart,
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
      audio_delay_ms: negAudMs,
      look: playerLook(),
      owner: "player",
    };
    try {
      await stopSession();
      const r = await fetch("/api/player/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.error) {
        setStatus(d.error, true);
        setBadge(tt("Fehler"));
        return;
      }
      if (d.options) {
      fillEncodeOptions(d.options);
      window.fpPlayerOptions = d.options;
    }
      const sess = d.session || {};
      fp.sid = sess.id;
      fp.startOffset = sess.mode === "direct" ? 0 : (sess.start != null ? sess.start : targetStart);
      fp.lastPosition = sess.start != null ? sess.start : targetStart;
      fp.duration = sess.duration || fp.duration || 0;
      fp.mode = sess.mode || "hls";
      fp.windowEnd = 0;
      fp.encodePaused = false;
      fp.negSessionDelayMs = negAudMs;
      if (fp.audioDelayMs > 0) {
        const dly = ensureWebAudioDelay();
        if (dly) dly.delayTime.value = Math.min(5.0, fp.audioDelayMs / 1000);
      } else if (fp.audioDelayNode) {
        fp.audioDelayNode.delayTime.value = 0;
      }
      if ($("fp-audio-copy") && sess.audio_copy != null) {
        $("fp-audio-copy").checked = !!sess.audio_copy;
      }
      if (d.info) {
        fp.info = d.info;
        syncLookUi();
        fillTracks(d.info);
        if ($("fp-audio") && sess.audio != null) $("fp-audio").value = String(sess.audio);
        if ($("fp-sub")) {
          if (rawSubVal.startsWith("sidecar:")) $("fp-sub").value = rawSubVal;
          else if (sess.subtitle != null) $("fp-sub").value = String(sess.subtitle);
        }
      }
      renderChapters(d.chapters || []);
      updateAudioModeHint(sess);
      setDirty(false);
      if (sess.error && !sess.ready && sess.mode === "hls") {
        setStatus(sess.error, true);
        setBadge(tt("Fehler"));
        return;
      }

      const provenance = document.getElementById("fp-provenance");
      if (provenance && window.StudioInsights) provenance.innerHTML = window.StudioInsights.previewHtml(sess);
      renderPlan(d.plan || {
        strategy: sess.decision_strategy,
        summary: sess.decision_summary,
        reasons: sess.decision_reasons || [],
      }, sess);

      if (sess.mode === "direct") {
        playDirect(sess.media_url || sess.playlist_url, autoplay);
        setBadge(tt("Direct-Play"));
        setStatus(fp.duration ? fmtClock(fp.duration) : "");
        if (rawSubVal !== "-1") attachSubtitles(path, rawSubVal);
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
        playHls(url, autoplay);
        const kind = planKind(d.plan, sess);
        const badgeLab = (PLAN_KIND[kind] && PLAN_KIND[kind].label) || sess.profile || tt("HLS");
        setBadge(tt(badgeLab));
        const extra = [];
        if (sess.look_summary) extra.push(tt("Encode-Vorschau") + ": " + sess.look_summary);
        if (sess.burn_subs) extra.push(tt("Bild-UT eingebrannt"));
        if (negAudMs < 0) extra.push(`Ton ${negAudMs} ms`);
        if (sess.warning && !(d.plan && d.plan.summary && sess.warning.indexOf(d.plan.summary) >= 0)) {
          extra.push(sess.warning);
        }
        setStatus(extra.join(" · "));
        if (rawSubVal !== "-1" && !sess.burn_subs) attachSubtitles(path, rawSubVal);
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
    } finally {
      fp.starting = false;
      if (fp.lookAgain) {
        fp.lookAgain = false;
        ensurePreviewMode();
        startSession(absoluteTime(), true);
      }
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
      await applyHdrCheck(info);
      syncLookUi();
      fp.duration = info.duration || 0;
      fp.lastPosition = 0;
      fp.path = path;
      fillTracks(info);
      updateTimeUi();
      await refreshPlan();
      await startSession(0, true);
    } catch (e) {
      setStatus(String(e), true);
    }
  }

  async function loadOptions() {
    try {
      const d = await (await fetch("/api/player/options")).json();
      window.fpPlayerOptions = d;
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
      v.addEventListener("progress", () => {
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

    function fpFps() {
      const f = Number(fp.info && fp.info.fps);
      return f > 1 && f <= 240 ? f : 24;
    }

    function stepFrame(dir) {
      if (!v || !fp.path) return;
      if (!v.paused) v.pause();
      const fps = fpFps();
      const dt = dir / fps;
      const curRel = v.currentTime || 0;
      // Frame-Index exakt runden, damit wiederholtes Tippen keinen Gleitkomma-Drift hat
      const curAbs = fp.startOffset + curRel;
      const nextFrame = Math.max(0, Math.round(curAbs * fps) + dir);
      const maxDur = fp.duration || (isFinite(v.duration) ? v.duration : 0) || Infinity;
      const targetAbs = Math.min(maxDur, nextFrame / fps);
      const targetRel = targetAbs - fp.startOffset;
      const showStepStatus = () => {
        setStatus(`${tt("Einzelbild")} #${nextFrame} · ${fmtClockPrecise(targetAbs)} (${fps.toFixed(3).replace(/\.?0+$/, "")} fps)`);
      };
      if (fp.mode === "direct") {
        try { v.currentTime = Math.max(0, targetRel); } catch (e) { /* ignore */ }
        updateTimeUi();
        renderStatsHud();
        showStepStatus();
        return;
      }
      // Wenn man nahe an das Ende des Puffers vorrückt, FFmpeg rechtzeitig aufwecken
      if (dir > 0 && fp.encodePaused && bufferedAhead() < 4) {
        setEncodePaused(false);
      }
      // Innerhalb des bereits gepufferten HLS-Bereichs direkt im Video-Element springen
      let inBuf = false;
      if (targetRel >= -0.005 && v.buffered) {
        for (let i = 0; i < v.buffered.length; i++) {
          if (targetRel >= v.buffered.start(i) - 0.02 && targetRel <= v.buffered.end(i) + 0.02) {
            inBuf = true;
            break;
          }
        }
      }
      if (inBuf) {
        try { v.currentTime = Math.max(0, targetRel); } catch (e) { /* ignore */ }
        updateTimeUi();
        renderStatsHud();
        showStepStatus();
      } else {
        startSession(Math.max(0, targetAbs), false).then(() => showStepStatus());
      }
    }

    function exportPngSnapshot() {
      if (!v || !v.videoWidth || !v.videoHeight) {
        setStatus(tt("Kein dekodiertes Videobild für PNG-Export vorhanden."), true);
        return;
      }
      try {
        const c = document.createElement("canvas");
        c.width = v.videoWidth;
        c.height = v.videoHeight;
        const ctx = c.getContext("2d");
        ctx.drawImage(v, 0, 0, c.width, c.height);
        const t = absoluteTime();
        const ms = String(Math.round((t % 1) * 1000)).padStart(3, "0");
        const stamp = fmtClock(t).replace(/:/g, "-") + "." + ms;
        const base = String(fp.path || "frame").split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
        c.toBlob((blob) => {
          if (!blob) return;
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = `${base}_${stamp}.png`;
          document.body.appendChild(a);
          a.click();
          setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
          setStatus(`${tt("PNG gespeichert")}: ${base}_${stamp}.png`);
        }, "image/png");
      } catch (e) {
        setStatus(`${tt("PNG-Export fehlgeschlagen")}: ${e}`, true);
      }
    }

    function renderStatsHud() {
      const hud = $("fp-stats-hud");
      if (!hud || !fp.statsOpen) return;
      const info = fp.info || {};
      const vw = (v && v.videoWidth) || 0;
      const vh = (v && v.videoHeight) || 0;
      const srcRes = info.width && info.height ? `${info.width}×${info.height}` : "—";
      const outRes = vw && vh ? `${vw}×${vh}` : "—";
      const q = v && typeof v.getVideoPlaybackQuality === "function" ? v.getVideoPlaybackQuality() : null;
      const totalF = q ? q.totalVideoFrames : (v && v.webkitDecodedFrameCount) || 0;
      const dropF = q ? q.droppedVideoFrames : (v && v.webkitDroppedFrameCount) || 0;
      const dropPct = totalF > 0 ? ((dropF / totalF) * 100).toFixed(2) : "0.00";
      const fps = fpFps();
      const t = absoluteTime();
      const estFrame = Math.round(t * fps);
      const buf = bufferedAhead().toFixed(1);
      const plan = fp.plan || {};
      const strat = plan.strategy || fp.mode || "—";
      const enc = (plan.encoder || (fp.mode === "direct" ? "direct" : "—"));
      const hdr = info.dolby_vision
        ? `Dolby Vision P${info.dv_profile ?? "?"}`
        : (info.is_hdr ? (info.hdr_type || "HDR10") : "SDR");
      const bw = fp.hls && fp.hls.bandwidthEstimate
        ? `${(fp.hls.bandwidthEstimate / 1e6).toFixed(1)} Mbit/s`
        : "—";
      const lines = [
        `Datei       : ${(fp.path || "—").split(/[\\/]/).pop()}`,
        `Pfad / Mode : ${strat} (Encoder: ${enc}${fp.encodePaused ? " · pausiert" : ""})`,
        `Auflösung   : Quelle ${srcRes} → Player ${outRes} @ ${fps.toFixed(3).replace(/\.?0+$/, "")} fps`,
        `Codec / HDR : ${prettyCodec(info.codec) || "—"} · ${info.pix_fmt || "—"} · ${hdr}`,
        `Position    : ${fmtClock(t)} (${t.toFixed(3)} s · ~Frame #${estFrame})`,
        `Frames      : ${dropF} dropped / ${totalF} decoded (${dropPct} %)`,
        `Puffer      : ${buf} s voraus (Ziel: ${fp.lookahead || "∞"} s) · HLS-BW: ${bw}`,
      ];
      hud.textContent = lines.join("\n");
    }

    function toggleStatsHud(force) {
      const hud = $("fp-stats-hud");
      const btn = $("fp-stats-toggle");
      fp.statsOpen = typeof force === "boolean" ? force : !fp.statsOpen;
      if (hud) hud.hidden = !fp.statsOpen;
      if (btn) btn.setAttribute("aria-pressed", fp.statsOpen ? "true" : "false");
      if (fp.statsTimer) { clearInterval(fp.statsTimer); fp.statsTimer = 0; }
      if (fp.statsOpen) {
        renderStatsHud();
        fp.statsTimer = setInterval(renderStatsHud, 400);
      }
    }

    const sbk = $("fp-step-back");
    const sfw = $("fp-step-fwd");
    if (sbk) sbk.addEventListener("click", () => stepFrame(-1));
    if (sfw) sfw.addEventListener("click", () => stepFrame(1));
    const snapBtn = $("fp-snapshot");
    if (snapBtn) snapBtn.addEventListener("click", exportPngSnapshot);
    const statsBtn = $("fp-stats-toggle");
    if (statsBtn) statsBtn.addEventListener("click", () => toggleStatsHud());
    const vmafSceneBtn = $("fp-use-vmaf-scene");
    if (vmafSceneBtn) vmafSceneBtn.addEventListener("click", () => {
      if (!fp.path) {
        setStatus(tt("Bitte zuerst ein Video laden."), true);
        return;
      }
      if (typeof window.addCustomVmafScene === "function") {
        const res = window.addCustomVmafScene(fp.path, absoluteTime(), fp.info);
        if (res && res.ok) {
          setStatus(`${tt("Als gezielte VMAF-Testszene übernommen")}: ${fmtClock(res.start)} (${res.length} s · ${res.count}/5)`);
        } else if (res && res.error) {
          setStatus(res.error, true);
        }
      }
    });

    document.addEventListener("keydown", (ev) => {
      const sec = document.querySelector('section[data-page="player"]');
      if (!sec || sec.hidden || sec.style.display === "none") return;
      const tag = (ev.target && ev.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || (ev.target && ev.target.isContentEditable)) return;
      if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
      if (ev.key === ",") { ev.preventDefault(); stepFrame(-1); }
      else if (ev.key === ".") { ev.preventDefault(); stepFrame(1); }
      else if (ev.key === "ArrowLeft") {
        ev.preventDefault();
        if (ev.shiftKey || (v && v.paused)) stepFrame(ev.shiftKey ? -10 : -1);
        else if (v) stepFrame(-Math.round(fpFps() * 5));
      } else if (ev.key === "ArrowRight") {
        ev.preventDefault();
        if (ev.shiftKey || (v && v.paused)) stepFrame(ev.shiftKey ? 10 : 1);
        else if (v) stepFrame(Math.round(fpFps() * 5));
      } else if (ev.key === " ") {
        if (tag !== "button" && v && fp.path) {
          ev.preventDefault();
          if (v.paused) v.play().catch(() => {});
          else v.pause();
        }
      }
      else if (ev.key === "i" || ev.key === "I") { ev.preventDefault(); toggleStatsHud(); }
      else if (ev.key === "s" || ev.key === "S") { ev.preventDefault(); exportPngSnapshot(); }
    });

    function fsElement() {
      return document.fullscreenElement || document.webkitFullscreenElement || null;
    }
    function syncFsButton() {
      const btn = $("fp-fs");
      if (!btn) return;
      const on = !!fsElement();
      btn.textContent = on ? "✕" : "⛶";
      btn.title = on ? tt("Vollbild beenden (Esc)") : tt("Vollbild (Esc zum Beenden)");
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    }
    async function toggleFullscreen() {
      const stage = document.querySelector(".fp-stage");
      const target = stage || v;
      if (!target) return;
      try {
        if (fsElement()) {
          if (document.exitFullscreen) await document.exitFullscreen();
          else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
        } else if (target.requestFullscreen) {
          await target.requestFullscreen();
        } else if (target.webkitRequestFullscreen) {
          target.webkitRequestFullscreen();
        } else if (v && v.webkitEnterFullscreen) {
          // iOS Safari: nur Video-Element
          v.webkitEnterFullscreen();
        }
      } catch (e) {
        setStatus(tt("Vollbild nicht verfügbar."), true);
      }
      syncFsButton();
    }
    const fsBtn = $("fp-fs");
    if (fsBtn) fsBtn.addEventListener("click", () => toggleFullscreen());
    document.addEventListener("fullscreenchange", syncFsButton);
    document.addEventListener("webkitfullscreenchange", syncFsButton);
    if (v) {
      v.addEventListener("dblclick", (e) => {
        e.preventDefault();
        toggleFullscreen();
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
        if (id === "fp-sub") {
          syncBurnHint();
          const opt = ($("fp-sub") || {}).selectedOptions;
          const isImg = opt && opt[0] && opt[0].dataset.image === "1";
          if (fp.path && fp.sid && !isImg && !($("fp-burn") && $("fp-burn").checked)) {
            attachSubtitles(fp.path, $("fp-sub").value);
            return;
          }
        }
        if (id === "fp-profile") syncEncodeUi({ presetBr: true });
        if (id === "fp-platform") {
          syncCodecOptionsForPlatform();
          syncLookUi();
        }
        if (id === "fp-audio" || id === "fp-profile" || id === "fp-audio-copy") {
          updateAudioModeHint(null);
        }
        if (!fp.path) return;
        if ((id === "fp-platform" || id === "fp-codec") && fp.sid) {
          queueLookApply();
          return;
        }
        setDirty(true);
        setStatus(tt("Änderungen noch nicht übernommen – „Übernehmen“ klicken."));
        refreshPlan();
      };
      el.addEventListener("change", onChange);
      if (id === "fp-height" || id === "fp-vbr") el.addEventListener("input", onChange);
    });

    ["fp-tonemap", "fp-tonemap-engine", "fp-tonemap-algo", "fp-tonemap-desat", "fp-tonemap-peak",
      "fp-deband", "fp-denoise", "fp-sharpen", "fp-grain"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", queueLookApply);
      if (id === "fp-tonemap-desat") {
        el.addEventListener("input", syncLookUi);
      }
      if (id === "fp-tonemap-peak") {
        el.addEventListener("input", queueLookApply);
      }
    });
    syncLookUi();

    try {
      const savedLa = localStorage.getItem("fpLookahead");
      if (savedLa && $("fp-lookahead")) $("fp-lookahead").value = savedLa;
    } catch (e) { /* ignore */ }

    const apply = $("fp-apply");
    if (apply) apply.addEventListener("click", () => {
      if (!fp.path) return;
      startSession(absoluteTime(), true);
    });

    // Live Audio- & Untertitel-Versatz + 1-Klick Remux-Übernahme
    const audInp = $("fp-aud-delay");
    if (audInp) {
      audInp.addEventListener("change", () => applyLiveAudioDelay(parseInt(audInp.value, 10) || 0));
    }
    if ($("fp-aud-delay-minus")) $("fp-aud-delay-minus").addEventListener("click", () => applyLiveAudioDelay((fp.audioDelayMs || 0) - 100));
    if ($("fp-aud-delay-plus")) $("fp-aud-delay-plus").addEventListener("click", () => applyLiveAudioDelay((fp.audioDelayMs || 0) + 100));
    if ($("fp-aud-delay-zero")) $("fp-aud-delay-zero").addEventListener("click", () => applyLiveAudioDelay(0));

    const subInp = $("fp-sub-delay");
    if (subInp) {
      subInp.addEventListener("input", () => {
        applyLiveSubDelay(parseInt(subInp.value, 10) || 0);
        setStatus(`Untertitel-Versatz live: ${fp.subDelayMs > 0 ? "+" : ""}${fp.subDelayMs} ms`);
      });
    }
    if ($("fp-sub-delay-minus")) $("fp-sub-delay-minus").addEventListener("click", () => {
      applyLiveSubDelay((fp.subDelayMs || 0) - 100);
      setStatus(`Untertitel-Versatz live: ${fp.subDelayMs > 0 ? "+" : ""}${fp.subDelayMs} ms`);
    });
    if ($("fp-sub-delay-plus")) $("fp-sub-delay-plus").addEventListener("click", () => {
      applyLiveSubDelay((fp.subDelayMs || 0) + 100);
      setStatus(`Untertitel-Versatz live: ${fp.subDelayMs > 0 ? "+" : ""}${fp.subDelayMs} ms`);
    });
    if ($("fp-sub-delay-zero")) $("fp-sub-delay-zero").addEventListener("click", () => {
      applyLiveSubDelay(0);
      setStatus("Untertitel-Versatz: 0 ms");
    });

    const syncRemuxBtn = $("fp-sync-to-remux");
    if (syncRemuxBtn) {
      syncRemuxBtn.addEventListener("click", async () => {
        if (!fp.path) {
          setStatus(tt("Bitte zuerst ein Video im Player laden."), true);
          return;
        }
        const aIdx = parseInt(($("fp-audio") || {}).value, 10);
        const rawSub = String(($("fp-sub") || {}).value || "-1");
        let sidecarSub = null;
        let sIdx = -1;
        if (rawSub.startsWith("sidecar:")) {
          const scIdx = parseInt(rawSub.split(":")[1], 10);
          const scList = ((fp.info && fp.info.sidecar_subtitles) || []).filter((sc) => !sc.bitmap);
          sidecarSub = scList[scIdx] || null;
        } else {
          sIdx = parseInt(rawSub, 10);
          if (isNaN(sIdx)) sIdx = -1;
        }
        if (typeof window.openRemuxWithSyncOffsets === "function") {
          await window.openRemuxWithSyncOffsets({
            path: fp.path,
            name: String(fp.path).split(/[\\/]/).pop(),
            audioIndex: isNaN(aIdx) ? -1 : aIdx,
            audioDelayMs: fp.audioDelayMs || 0,
            subtitleIndex: sIdx,
            subDelayMs: fp.subDelayMs || 0,
            sidecarSub,
          });
        }
      });
    }

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
        onPick: async (f) => {
          if ($("fp-path")) $("fp-path").value = f.rel;
          fp.path = f.rel || "";
          setDirty(false);
          setStatus(tt("Datei gewählt – Plane Wiedergabe …"));
          setActiveMode(tt("Datei gewählt – Laden ausstehend"));
          try {
            const info = await (await fetch(
              `/api/probe?path=${encodeURIComponent(f.rel)}`)).json();
            if (!info.error) {
              fp.info = info;
              syncLookUi();
              fp.duration = info.duration || 0;
              fillTracks(info);
              updateTimeUi();
              updateAudioModeHint(null);
              await refreshPlan();
              setStatus(tt("Datei gewählt – zum Starten „Laden“ klicken."));
            } else {
              setStatus(info.error, true);
            }
          } catch (e) {
            setStatus(tt("Datei gewählt – zum Starten „Laden“ klicken."));
          }
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
  window.fpDetectClientCodecs = detectClientCodecs;
  window.StudioPlayerHdr = { choice: hdrTonemapChoice, contentType: hdrContentType };

  function initFullPlayer() {
    if (!$("fp-video")) return;
    bindControls();
    loadOptions();
    updateTimeUi();
  }

  document.addEventListener("DOMContentLoaded", initFullPlayer);
})();
