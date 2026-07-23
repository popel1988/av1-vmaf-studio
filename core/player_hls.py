"""Vollwertiger Browser-Player: Direct-Play + HLS-Sessions.

Encoder-Pfade: CPU (libx264/x265/svt), NVENC, Intel QSV/VAAPI, AMD VAAPI.
Die UI wählt Plattform, Zielcodec, Qualitätsstufe oder freie Höhe/Bitrate.

Kompatibilität:
  - HLS-fMP4 im Browser: H.264 am zuverlässigsten.
  - HEVC/AV1 nur, wenn der Client ``client_codecs`` meldet – sonst Fallback
    auf H.264 (mit Hinweis), damit nichts „durcheinander“ abspielt.
  - Burn-in (PGS): Filter auf CPU, danach hwupload für QSV/VAAPI.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, ffmpeg_utils as ff
from . import media_stream as ms

logger = logging.getLogger("vcompress.player_hls")

_SESSIONS: dict[str, "PlayerSession"] = {}
_LOCK = threading.RLock()
_ROOT = config.DATA_DIR / "player_sessions"
_MAX_IDLE_SEC = 3600

_QUALITY = {
    "1080p": {"height": 1080, "v_bitrate": 6000},
    "720p": {"height": 720, "v_bitrate": 3500},
    "480p": {"height": 480, "v_bitrate": 1500},
}
# Encode ohne Skalierung (Originalhöhe); Bitrate kommt aus der UI / Default.
_ENCODE_PROFILES = set(_QUALITY) | {"custom", "original"}

_IMAGE_SUB = {
    "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
    "dvb_subtitle", "dvbsub", "xsub",
}

_CPU_ENC = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}


@dataclass
class PlayerSession:
    id: str
    path: Path
    rel: str
    audio_index: int
    subtitle_index: int
    start_sec: float
    duration: float
    title: str
    profile: str = "copy"
    platform: str = "cpu"
    codec: str = "h264"
    encoder: str = "copy"
    burn_subs: bool = False
    audio_codec: str = ""
    mode: str = "hls"
    height: int = 0
    v_bitrate: int = 0
    warning: str = ""
    work_dir: Path = field(default_factory=Path)
    proc: Optional[subprocess.Popen] = None
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    error: str = ""

    @property
    def playlist(self) -> Path:
        return self.work_dir / "index.m3u8"

    def touch(self) -> None:
        self.last_access = time.time()

    def to_dict(self) -> dict:
        ready = True
        playlist_url = ""
        if self.mode == "hls":
            ready = self.playlist.is_file() and self.playlist.stat().st_size > 0
            playlist_url = f"/api/player/session/{self.id}/index.m3u8"
        return {
            "id": self.id,
            "path": self.rel,
            "title": self.title,
            "mode": self.mode,
            "profile": self.profile,
            "platform": self.platform,
            "codec": self.codec,
            "encoder": self.encoder,
            "burn_subs": self.burn_subs,
            "height": self.height,
            "v_bitrate": self.v_bitrate,
            "warning": self.warning,
            "audio": self.audio_index,
            "subtitle": self.subtitle_index,
            "start": round(self.start_sec, 3),
            "duration": round(self.duration, 3),
            "duration_human": ff.human_duration(self.duration) if self.duration else "",
            "playlist_url": playlist_url,
            "media_url": f"/api/media?path={self.rel}",
            "ready": ready,
            "running": bool(self.proc and self.proc.poll() is None),
            "error": self.error,
            "audio_codec": self.audio_codec,
            "audio_mode": self.audio_play_mode(),
            "audio_mode_label": self.audio_play_label(),
        }

    def audio_play_mode(self) -> str:
        """Wie die Tonspur ausgeliefert wird: direct | copy | transcode | none."""
        if self.audio_index < 0:
            return "none"
        if self.mode == "direct":
            return "direct"
        ac = (self.audio_codec or "").lower()
        if ac.startswith(("aac", "mp4a", "mp3")):
            return "copy"
        return "transcode"

    def audio_play_label(self) -> str:
        mode = self.audio_play_mode()
        ac = (self.audio_codec or "").upper() or "?"
        if mode == "none":
            return "Kein Ton"
        if mode == "direct":
            return f"Direct-Play ({ac})"
        if mode == "copy":
            return f"Stream-Copy ({ac})"
        return f"Transcode → AAC ({ac})"


def _ensure_root() -> Path:
    _ROOT.mkdir(parents=True, exist_ok=True)
    return _ROOT


def _is_image_sub(codec: str) -> bool:
    return (codec or "").lower() in _IMAGE_SUB


def probe_chapters(path: Path) -> list[dict]:
    cmd = [
        config.FFPROBE, "-v", "error",
        "-show_chapters", "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return []
    chapters = []
    for i, ch in enumerate(data.get("chapters") or []):
        start = float(ch.get("start_time") or 0)
        end = float(ch.get("end_time") or 0)
        tags = ch.get("tags") or {}
        title = tags.get("title") or tags.get("TITLE") or f"Kapitel {i + 1}"
        chapters.append({
            "index": i,
            "start": round(start, 3),
            "end": round(end, 3),
            "title": str(title),
        })
    return chapters


def _cap_ok(platform: str, codec: str) -> bool:
    from . import capabilities as caps
    results = caps.results_map()
    key = f"{platform}:{codec}"
    if results:
        return bool(results.get(key))
    return ff.encoder_available(platform, codec)


def list_platforms() -> list[dict]:
    """Plattformen mit verfügbaren Codecs für die UI."""
    out = []
    for p in ("auto", "nvidia", "intel", "amd", "cpu"):
        if p == "auto":
            out.append({
                "id": "auto", "label": "Automatisch (beste HW)",
                "codecs": ["h264", "hevc", "av1"], "available": True,
            })
            continue
        codecs = [c for c in ("h264", "hevc", "av1") if _cap_ok(p, c)]
        out.append({
            "id": p,
            "label": {"nvidia": "NVIDIA NVENC", "intel": "Intel QSV/VAAPI",
                      "amd": "AMD VAAPI", "cpu": "CPU (x264/x265/SVT)"}[p],
            "codecs": codecs or (["h264"] if p == "cpu" else []),
            "available": bool(codecs) or p == "cpu",
            "encoders": {c: (ff.encoder_name(p, c) or _CPU_ENC.get(c, ""))
                         for c in ("h264", "hevc", "av1")
                         if _cap_ok(p, c) or (p == "cpu" and c in _CPU_ENC)},
        })
    return out


def pick_auto_platform(codec: str = "h264") -> str:
    for p in ("nvidia", "intel", "amd", "cpu"):
        if _cap_ok(p, codec) or (p == "cpu" and codec in _CPU_ENC):
            return p
    return "cpu"


def resolve_encode(
    platform: str,
    codec: str,
    *,
    client_codecs: Optional[list[str]] = None,
) -> dict:
    """Plattform/Codec gegen Capabilities + Browser-Fähigkeit auflösen.

    Rückgabe: platform, codec, encoder, warnings[].
    """
    warnings: list[str] = []
    codec = (codec or "h264").lower()
    if codec not in ("h264", "hevc", "av1"):
        codec = "h264"
        warnings.append("Unbekannter Codec – Fallback H.264.")

    # Browser: ohne Freigabe kein HEVC/AV1 in HLS
    allowed = {c.lower() for c in (client_codecs or [])} or {"h264"}
    if codec not in allowed:
        warnings.append(
            f"Browser spielt {codec.upper()} in HLS voraussichtlich nicht ab "
            f"- Fallback H.264 (Client erlaubt: {', '.join(sorted(allowed)) or 'h264'})."
        )
        codec = "h264"

    plat = (platform or "auto").lower()
    if plat == "auto":
        plat = pick_auto_platform(codec)
    if plat not in ("nvidia", "intel", "amd", "cpu"):
        plat = "cpu"
        warnings.append("Unbekannte Plattform – Fallback CPU.")

    if plat != "cpu" and not _cap_ok(plat, codec):
        # Codec auf dieser HW nicht verfügbar → andere HW oder CPU
        alt = pick_auto_platform(codec)
        warnings.append(
            f"{plat}/{codec} laut Capabilities nicht verfuegbar - nutze {alt}."
        )
        plat = alt

    if plat == "cpu":
        enc = _CPU_ENC.get(codec, "libx264")
    else:
        enc = ff.encoder_name(plat, codec) or ""
        if not enc:
            warnings.append(f"Kein Encoder fuer {plat}/{codec} - Fallback CPU/H.264.")
            plat, codec, enc = "cpu", "h264", "libx264"

    return {
        "platform": plat,
        "codec": codec,
        "encoder": enc,
        "warnings": warnings,
    }


def player_options() -> dict:
    from . import capabilities as caps
    results = caps.results_map()
    auto_p = pick_auto_platform("h264")
    return {
        "profiles": [
            {"id": "auto", "label": "Automatisch (Direct-Play wenn möglich)"},
            {"id": "direct", "label": "Direct-Play (ohne Remux)"},
            {"id": "copy", "label": "Original-Video + Ton→AAC (HLS)"},
            {"id": "original", "label": "Original-Auflösung (Transcode + Bitrate)"},
            {"id": "1080p", "label": "1080p (Transcode)"},
            {"id": "720p", "label": "720p (Transcode)"},
            {"id": "480p", "label": "480p (Transcode)"},
            {"id": "custom", "label": "Benutzerdefiniert (Höhe/Bitrate)"},
        ],
        "platforms": list_platforms(),
        "codecs": [
            {"id": "h264", "label": "H.264 (empfohlen, beste Browser-Kompatibilität)"},
            {"id": "hevc", "label": "HEVC/H.265 (nur wenn Browser kann)"},
            {"id": "av1", "label": "AV1 (nur wenn Browser kann)"},
        ],
        "transcode_platform": auto_p,
        "transcode_encoder": (
            ff.encoder_name(auto_p, "h264") if auto_p != "cpu" else "libx264"
        ),
        "capabilities_ready": bool(results),
        "note": (
            "Für Live-Vorschau im Browser ist H.264 am sichersten. "
            "HEVC/AV1 werden nur genutzt, wenn der Browser sie meldet – "
            "sonst automatischer Fallback auf H.264."
        ),
    }


def can_direct_play(info) -> bool:
    if not info:
        return False
    fmt = (info.container or "").lower()
    is_mp4 = any(x in fmt for x in ("mp4", "mov", "m4v", "isom"))
    is_webm = "webm" in fmt and "matroska" not in fmt
    if not (is_mp4 or is_webm):
        return False
    vc = (info.codec or "").lower()
    if is_mp4 and not any(vc.startswith(p) for p in ("h264", "avc", "hevc", "h265", "av1", "av01")):
        return False
    if is_webm and not any(vc.startswith(p) for p in ("vp8", "vp9", "av1", "av01")):
        return False
    if info.audio:
        ac = (info.audio[0].get("codec") or "").lower()
        if ac and not any(ac.startswith(p) for p in ("aac", "mp3", "mp4a", "opus", "vorbis", "flac")):
            return False
    return True


def _audio_args(audio_index: int, audio_codec: str) -> list[str]:
    if audio_index < 0:
        return ["-an"]
    args = ["-map", f"0:a:{int(audio_index)}?"]
    if (audio_codec or "").lower().startswith(("aac", "mp4a", "mp3")):
        args += ["-c:a", "copy"]
    else:
        args += [
            "-c:a", "aac", "-ac", "2", "-b:a", "192k",
            "-af", "aresample=async=1000:first_pts=0",
        ]
    return args


def _vaapi_device() -> str:
    return getattr(config, "VAAPI_DEVICE", "/dev/dri/renderD128") or "/dev/dri/renderD128"


def _build_video_filter(
    *,
    height: int,
    burn_sub_index: int,
    platform: str,
    encoder: str,
) -> tuple[list[str], list[str]]:
    """Filter + ggf. Extra-Args vor -i. Rückgabe (pre_input, map/filter args)."""
    pre: list[str] = []
    backend = ff.encoder_backend(platform) if platform != "cpu" else "cpu"
    need_hwupload = "vaapi" in encoder or "qsv" in encoder

    scale = f"scale=-2:{int(height)}" if height and height > 0 else ""

    if burn_sub_index >= 0:
        # Overlay immer auf CPU
        chain = "[0:v:0]"
        if scale:
            chain += f"{scale}[vs];[vs]"
        else:
            chain += ""
        if scale:
            fc = (f"[0:v:0]{scale}[vs];"
                  f"[vs][0:s:{int(burn_sub_index)}]overlay=format=auto")
        else:
            fc = f"[0:v:0][0:s:{int(burn_sub_index)}]overlay=format=auto"
        if need_hwupload:
            fc += ",format=nv12,hwupload"
            if "qsv" in encoder:
                fc += "=extra_hw_frames=64"
            fc += "[vout]"
            if backend == "vaapi":
                pre = ["-vaapi_device", _vaapi_device()]
            elif backend == "qsv":
                pre = ["-init_hw_device", f"qsv=hw:{_vaapi_device()}",
                       "-filter_hw_device", "hw"]
        else:
            fc += "[vout]"
        return pre, ["-filter_complex", fc, "-map", "[vout]"]

    # Ohne Burn-in
    if need_hwupload:
        parts = []
        if scale:
            parts.append(scale)
        parts.append("format=nv12")
        if "qsv" in encoder:
            parts.append("hwupload=extra_hw_frames=64")
        else:
            parts.append("hwupload")
        vf = ",".join(parts)
        if backend == "vaapi":
            pre = ["-vaapi_device", _vaapi_device()]
        elif backend == "qsv":
            pre = ["-init_hw_device", f"qsv=hw:{_vaapi_device()}",
                   "-filter_hw_device", "hw"]
        return pre, ["-vf", vf, "-map", "0:v:0?"]

    # NVENC / CPU: software scale
    if scale:
        return pre, ["-vf", scale, "-map", "0:v:0?"]
    return pre, ["-map", "0:v:0?"]


def _encoder_rate_args(encoder: str, codec: str, v_bitrate: int) -> list[str]:
    br = max(300, int(v_bitrate or 3500))
    if "nvenc" in encoder:
        return ["-preset", "p4", "-rc", "vbr",
                "-b:v", f"{br}k",
                "-maxrate", f"{int(br * 1.5)}k",
                "-bufsize", f"{int(br * 2)}k"]
    if "qsv" in encoder:
        return ["-global_quality", "23", "-b:v", f"{br}k",
                "-maxrate", f"{int(br * 1.5)}k"]
    if "vaapi" in encoder:
        return ["-b:v", f"{br}k", "-maxrate", f"{int(br * 1.5)}k"]
    if encoder == "libsvtav1":
        return ["-preset", "8", "-crf", "28", "-b:v", "0"]
    if encoder == "libx265":
        return ["-preset", "veryfast", "-crf", "23",
                "-maxrate", f"{br}k", "-bufsize", f"{int(br * 2)}k"]
    # libx264
    return ["-preset", "veryfast", "-crf", "23",
            "-maxrate", f"{br}k", "-bufsize", f"{int(br * 2)}k"]


def _build_hls_cmd(
    path: Path,
    out_dir: Path,
    *,
    audio_index: int,
    start_sec: float,
    audio_codec: str,
    platform: str,
    encoder: str,
    codec: str,
    height: int,
    v_bitrate: int,
    burn_sub_index: int = -1,
    video_copy: bool = False,
) -> list[str]:
    playlist = out_dir / "index.m3u8"
    cmd = [
        config.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-fflags", "+genpts",
    ]

    if video_copy:
        if start_sec and start_sec > 0:
            cmd += ["-ss", f"{float(start_sec):.3f}"]
        cmd += ["-i", str(path), "-map", "0:v:0?", "-c:v", "copy"]
    else:
        pre, vmap = _build_video_filter(
            height=height, burn_sub_index=burn_sub_index,
            platform=platform, encoder=encoder,
        )
        cmd += pre
        if start_sec and start_sec > 0:
            cmd += ["-ss", f"{float(start_sec):.3f}"]
        cmd += ["-i", str(path)]
        cmd += vmap
        cmd += ["-c:v", encoder]
        cmd += _encoder_rate_args(encoder, codec, v_bitrate)

    cmd += _audio_args(audio_index, audio_codec)
    cmd += [
        "-sn", "-dn",
        "-avoid_negative_ts", "make_zero",
        "-max_interleave_delta", "0",
        "-f", "hls",
        "-hls_time", "3",
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments+omit_endlist",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(out_dir / "seg_%05d.m4s"),
        str(playlist),
    ]
    return cmd


def _resolve_profile(profile: str, info, *, force_hls: bool, burn: bool) -> str:
    p = (profile or "auto").lower()
    if p == "auto":
        if burn:
            return "720p"
        if force_hls:
            return "copy"
        if can_direct_play(info):
            return "direct"
        return "copy"
    return p


def _quality_params(profile: str, height: int, v_bitrate: int) -> tuple[int, int]:
    """Höhe/Bitrate aus Preset, Original oder Custom.

    ``v_bitrate`` > 0 überschreibt den Preset-Default (UI-Eingabe).
    ``height == 0`` bei Profil ``original`` = keine Skalierung.
    """
    p = (profile or "").lower()
    if p == "original":
        h = 0
        br = int(v_bitrate) if v_bitrate and int(v_bitrate) > 0 else 8000
    elif p in _QUALITY:
        q = _QUALITY[p]
        h = int(q["height"])
        br = int(v_bitrate) if v_bitrate and int(v_bitrate) > 0 else int(q["v_bitrate"])
        h = max(144, min(2160, h))
    else:
        h = int(height or 720)
        br = int(v_bitrate or 3500)
        h = max(144, min(2160, h))
    br = max(300, min(50000, br))
    return h, br


def start_session(
    rel: str,
    audio_index: int = 0,
    subtitle_index: int = -1,
    start_sec: float = 0.0,
    profile: str = "auto",
    burn_subs: bool = False,
    client_direct_ok: bool = False,
    platform: str = "auto",
    codec: str = "h264",
    height: int = 0,
    v_bitrate: int = 0,
    client_codecs: Optional[list[str]] = None,
) -> dict:
    """Session starten (Direct-Play oder HLS)."""
    target = config.resolve_input(rel)
    if target is None or not target.is_file():
        return {"error": "Datei nicht gefunden"}

    info, err = ff.probe_with_error(target)
    duration = float(getattr(info, "duration", 0) or 0) if info else 0.0
    if err and not info:
        return {"error": f"Analyse fehlgeschlagen: {err}"}

    chapters = probe_chapters(target)
    audio_codec = ""
    if info and info.audio and 0 <= audio_index < len(info.audio):
        audio_codec = str(info.audio[audio_index].get("codec") or "")

    sub_codec = ""
    if info and info.subtitles and 0 <= subtitle_index < len(info.subtitles):
        sub_codec = str(info.subtitles[subtitle_index].get("codec") or "")

    want_burn = bool(burn_subs) and subtitle_index >= 0 and _is_image_sub(sub_codec)
    force_hls = bool(start_sec and start_sec > 0) or (
        audio_index >= 0 and audio_codec
        and not (audio_codec or "").lower().startswith(("aac", "mp3", "mp4a", "opus"))
    )
    resolved = _resolve_profile(
        profile, info,
        force_hls=force_hls if (profile or "auto").lower() == "auto" else False,
        burn=want_burn,
    )
    if (profile or "auto").lower() == "auto" and client_direct_ok and not want_burn and not force_hls:
        if can_direct_play(info):
            resolved = "direct"

    need_encode = resolved in _ENCODE_PROFILES or want_burn
    enc_info = {"platform": "cpu", "codec": "h264", "encoder": "copy", "warnings": []}
    h, br = 0, 0
    if need_encode:
        enc_info = resolve_encode(
            platform, codec, client_codecs=client_codecs or ["h264"],
        )
        if resolved not in _ENCODE_PROFILES:
            # z. B. copy/direct + Burn-in → Encode in Originalauflösung
            resolved = "original" if want_burn else "720p"
        h, br = _quality_params(resolved, height, v_bitrate)

    sid = uuid.uuid4().hex[:12]
    work = _ensure_root() / sid
    work.mkdir(parents=True, exist_ok=True)

    warn = "; ".join(enc_info.get("warnings") or [])
    sess = PlayerSession(
        id=sid,
        path=target,
        rel=rel,
        audio_index=int(audio_index),
        subtitle_index=int(subtitle_index),
        start_sec=max(0.0, float(start_sec or 0)),
        duration=duration,
        title=target.name,
        profile=resolved,
        platform=enc_info["platform"] if need_encode else "cpu",
        codec=enc_info["codec"] if need_encode else "",
        encoder=enc_info["encoder"] if need_encode else ("direct" if resolved == "direct" else "copy"),
        burn_subs=want_burn,
        audio_codec=audio_codec,
        mode="direct" if resolved == "direct" else "hls",
        height=h,
        v_bitrate=br,
        warning=warn,
        work_dir=work,
    )

    if sess.mode == "direct":
        with _LOCK:
            _SESSIONS[sid] = sess
        return {
            "session": sess.to_dict(),
            "info": info.to_dict() if info else None,
            "chapters": chapters,
            "options": player_options(),
        }

    if resolved == "copy" and not want_burn:
        cmd = _build_hls_cmd(
            target, work,
            audio_index=sess.audio_index,
            start_sec=sess.start_sec,
            audio_codec=audio_codec,
            platform="cpu", encoder="copy", codec="h264",
            height=0, v_bitrate=0, burn_sub_index=-1, video_copy=True,
        )
        sess.encoder = "copy"
        sess.platform = "cpu"
        sess.codec = ""
    else:
        cmd = _build_hls_cmd(
            target, work,
            audio_index=sess.audio_index,
            start_sec=sess.start_sec,
            audio_codec=audio_codec,
            platform=sess.platform,
            encoder=sess.encoder,
            codec=sess.codec or "h264",
            height=h,
            v_bitrate=br,
            burn_sub_index=subtitle_index if want_burn else -1,
            video_copy=False,
        )

    try:
        sess.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        shutil.rmtree(work, ignore_errors=True)
        return {"error": f"FFmpeg-Start fehlgeschlagen: {e}"}

    with _LOCK:
        _SESSIONS[sid] = sess

    deadline = time.time() + (10.0 if need_encode else 4.0)
    while time.time() < deadline:
        if sess.playlist.is_file() and sess.playlist.stat().st_size > 0:
            break
        if sess.proc.poll() is not None:
            err_b = b""
            try:
                if sess.proc.stderr:
                    err_b = sess.proc.stderr.read() or b""
            except Exception:
                pass
            sess.error = (err_b.decode("utf-8", "replace") or "FFmpeg beendet")[-500:]
            # Einmaliger Fallback CPU/H.264 bei HW-Fehler
            if need_encode and sess.platform != "cpu" and not sess.playlist.exists():
                logger.warning("Player-HW fehlgeschlagen (%s), Fallback CPU: %s",
                               sess.encoder, sess.error[-200:])
                _kill(sess)
                for old in work.glob("*"):
                    try:
                        old.unlink()
                    except OSError:
                        pass
                sess.platform, sess.codec, sess.encoder = "cpu", "h264", "libx264"
                sess.warning = (sess.warning + "; " if sess.warning else "") + \
                    "HW-Encode fehlgeschlagen - Fallback CPU/H.264."
                sess.error = ""
                cmd = _build_hls_cmd(
                    target, work,
                    audio_index=sess.audio_index,
                    start_sec=sess.start_sec,
                    audio_codec=audio_codec,
                    platform="cpu", encoder="libx264", codec="h264",
                    height=h, v_bitrate=br or 3500,
                    burn_sub_index=subtitle_index if want_burn else -1,
                )
                try:
                    sess.proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                    )
                    deadline = time.time() + 10.0
                    continue
                except OSError as e2:
                    sess.error = str(e2)
            break
        time.sleep(0.1)

    return {
        "session": sess.to_dict(),
        "info": info.to_dict() if info else None,
        "chapters": chapters,
        "options": player_options(),
    }


def get_session(sid: str) -> Optional[PlayerSession]:
    with _LOCK:
        sess = _SESSIONS.get(sid)
    if sess:
        sess.touch()
    return sess


def stop_session(sid: str) -> bool:
    with _LOCK:
        sess = _SESSIONS.pop(sid, None)
    if not sess:
        return False
    _kill(sess)
    shutil.rmtree(sess.work_dir, ignore_errors=True)
    return True


def _kill(sess: PlayerSession) -> None:
    if sess.proc and sess.proc.poll() is None:
        try:
            sess.proc.kill()
            sess.proc.wait(timeout=5)
        except Exception:
            pass


def cleanup_idle(max_idle: float = _MAX_IDLE_SEC) -> int:
    now = time.time()
    dead: list[str] = []
    with _LOCK:
        for sid, sess in list(_SESSIONS.items()):
            if now - sess.last_access > max_idle:
                dead.append(sid)
    n = 0
    for sid in dead:
        if stop_session(sid):
            n += 1
    return n


def cleanup_all() -> None:
    with _LOCK:
        ids = list(_SESSIONS.keys())
    for sid in ids:
        stop_session(sid)
    if _ROOT.exists():
        for child in _ROOT.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)


def resolve_session_file(sid: str, name: str) -> Optional[Path]:
    sess = get_session(sid)
    if not sess or sess.mode != "hls":
        return None
    safe = Path(name).name
    if not safe or safe != name.replace("\\", "/").split("/")[-1]:
        return None
    if not all(c.isalnum() or c in "._-%" for c in safe):
        return None
    target = (sess.work_dir / safe).resolve()
    try:
        target.relative_to(sess.work_dir.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target
