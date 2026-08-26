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
import math
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, ffmpeg_utils as ff
from . import media_stream as ms
from .encoder import _TONEMAP_CHAIN

logger = logging.getLogger("vcompress.player_hls")

_SESSIONS: dict[str, "PlayerSession"] = {}
_LOCK = threading.RLock()
_ROOT = config.DATA_DIR / "player_sessions"
_MAX_IDLE_SEC = 3600
# Wie weit FFmpeg vor der Abspielposition encoden darf (0 = unbegrenzt).
_DEFAULT_LOOKAHEAD_SEC = 30.0
_LOOKAHEAD_CHOICES = (0, 15, 30, 60, 120)
# Mehr FFmpeg-Ausgabe in Container-Logs (warning|info|debug|error)
_PLAYER_FF_LOGLEVEL = (os.getenv("PLAYER_FF_LOGLEVEL") or "warning").strip().lower()


def _plog(sid: str, msg: str, *args, level: int = logging.INFO) -> None:
    logger.log(level, "Player[%s] " + msg, sid, *args)


def _hls_video_copy_ok(
    source_codec: str,
    client_codecs: Optional[list[str]] = None,
) -> bool:
    """Ob Video per HLS-Copy (fMP4) im Browser laufen kann.

    H.264 immer; HEVC/AV1 nur wenn der Client sie meldet – Auslieferung dann
    als fMP4 mit ``hvc1``/``av01`` (nicht MPEG-TS).
    """
    src = ff.normalize_video_codec(source_codec)
    if src == "h264":
        return True
    allowed = {c.lower() for c in (client_codecs or [])}
    if src == "hevc" and "hevc" in allowed:
        return True
    if src == "av1" and "av1" in allowed:
        return True
    return False


def _cmd_preview(cmd: list[str], limit: int = 500) -> str:
    s = " ".join(str(x) for x in cmd)
    return s if len(s) <= limit else s[:limit] + "…"


def _spawn_ffmpeg(cmd: list[str], sid: str, work_dir: Path) -> subprocess.Popen:
    """Startet FFmpeg; stderr → Datei + Container-Log (kein PIPE-Deadlock)."""
    log_path = work_dir / "ffmpeg.log"
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        log_f.close()
        raise

    def _drain() -> None:
        try:
            assert proc.stderr is not None
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace")
                try:
                    log_f.write(line)
                    log_f.flush()
                except Exception:
                    pass
                text = line.rstrip()
                if text:
                    _plog(sid, "ffmpeg: %s", text)
        except Exception as e:  # pragma: no cover
            _plog(sid, "stderr-drain: %s", e, level=logging.DEBUG)
        finally:
            try:
                log_f.close()
            except Exception:
                pass

    threading.Thread(target=_drain, name=f"ff-log-{sid}", daemon=True).start()
    return proc


def _tail_ffmpeg_log(work_dir: Path, n: int = 40) -> str:
    path = work_dir / "ffmpeg.log"
    try:
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""

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
    lookahead_sec: float = _DEFAULT_LOOKAHEAD_SEC
    window_end: float = 0.0          # ungenutzt (Kompat.); Puffer steuert der Client
    audio_copy: bool = False         # Ton nicht umcodieren (−c:a copy)
    warning: str = ""
    work_dir: Path = field(default_factory=Path)
    proc: Optional[subprocess.Popen] = None
    encode_paused: bool = False
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    error: str = ""
    debug_cmd: str = ""
    last_client_error: str = ""
    decision_summary: str = ""
    decision_reasons: list = field(default_factory=list)
    decision_strategy: str = ""

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
        running = bool(self.proc and self.proc.poll() is None)
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
            "lookahead_sec": self.lookahead_sec,
            "window_end": round(self.window_end, 3) if self.window_end else 0,
            "audio_copy": bool(self.audio_copy),
            "warning": self.warning,
            "audio": self.audio_index,
            "subtitle": self.subtitle_index,
            "start": round(self.start_sec, 3),
            "duration": round(self.duration, 3),
            "duration_human": ff.human_duration(self.duration) if self.duration else "",
            "playlist_url": playlist_url,
            "media_url": f"/api/media?path={self.rel}",
            "ready": ready,
            "running": running,
            "encode_paused": bool(self.encode_paused and running),
            "error": self.error,
            "audio_codec": self.audio_codec,
            "audio_mode": self.audio_play_mode(),
            "audio_mode_label": self.audio_play_label(),
            "playback_label": self.playback_label(),
            "debug_cmd": self.debug_cmd,
            "ffmpeg_log_tail": _tail_ffmpeg_log(self.work_dir) if self.work_dir else "",
            "last_client_error": self.last_client_error,
            "decision_summary": self.decision_summary,
            "decision_reasons": list(self.decision_reasons or []),
            "decision_strategy": self.decision_strategy,
        }

    def playback_label(self) -> str:
        """Kurzer Text für die UI: tatsächlich laufender Modus (nicht nur Auswahl)."""
        if self.mode == "direct":
            return "Direct-Play"
        if self.encoder in ("", "copy", "direct") and self.profile == "copy":
            if self.audio_index < 0:
                return "HLS · Remux (ohne Ton)"
            if self.audio_play_mode() == "transcode":
                return "HLS · Video-Copy + Ton→AAC"
            return "HLS · Remux (Stream-Copy)"
        if self.encoder in ("", "copy") and not self.codec:
            return "HLS · Stream-Copy"
        bits = ["HLS"]
        if self.profile:
            bits.append(self.profile)
        if self.height:
            bits.append(f"{self.height}p")
        elif self.encoder and self.encoder not in ("copy", "direct"):
            bits.append("Original-Auflösung")
        if self.platform and self.encoder and self.encoder not in ("copy", "direct"):
            bits.append(f"{self.platform}/{self.codec or '?'}/{self.encoder}")
        if self.burn_subs:
            bits.append("UT eingebrannt")
        return " · ".join(bits)

    def audio_play_mode(self) -> str:
        """Wie die Tonspur ausgeliefert wird: direct | copy | transcode | none."""
        if self.audio_index < 0:
            return "none"
        if self.mode == "direct":
            return "direct"
        ac = (self.audio_codec or "").lower()
        if self.audio_copy or ac.startswith(("aac", "mp4a", "mp3")):
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
            if self.audio_copy and not ac.lower().startswith(("aac", "mp4a", "mp3")):
                return f"Stream-Copy erzwungen ({ac})"
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


def _browser_audio_direct_ok(codec: str) -> bool:
    """Ton für HTML5-Direct-Play (nicht HLS)."""
    return (codec or "").lower().startswith(
        ("aac", "mp3", "mp4a", "opus", "vorbis", "flac")
    )


def _server_encode_targets(client_codecs: Optional[list[str]]) -> list[str]:
    """Zielcodecs die Browser abspielen kann und Server encoden kann."""
    allowed = {c.lower() for c in (client_codecs or [])} or {"h264"}
    out = []
    for c in ("h264", "hevc", "av1"):
        if c not in allowed:
            continue
        if c == "h264" or any(_cap_ok(p, c) for p in ("nvidia", "intel", "amd", "cpu")):
            out.append(c)
    return out or ["h264"]


def plan_playback(
    info,
    *,
    profile: str = "auto",
    audio_index: int = 0,
    audio_codec: str = "",
    subtitle_index: int = -1,
    sub_codec: str = "",
    start_sec: float = 0.0,
    burn_subs: bool = False,
    client_direct_ok: bool = False,
    client_codecs: Optional[list[str]] = None,
    platform: str = "auto",
    codec: str = "h264",
    height: int = 0,
    v_bitrate: int = 0,
    audio_copy: bool = False,
) -> dict:
    """Datei × Browser × Server → bester Wiedergabepfad.

    Rückgabe u. a. strategy, profile, mode, use_video_copy, drop_audio,
    will_xcode_audio, need_encode, encode{platform,codec,encoder},
    height, v_bitrate, reasons[], summary, file{}, browser{}, server{}.
    """
    from . import capabilities as caps

    requested = (profile or "auto").lower()
    client = [c.lower() for c in (client_codecs or [])] or ["h264"]
    want_audio_copy = bool(audio_copy)
    want_burn = bool(burn_subs) and subtitle_index >= 0 and _is_image_sub(sub_codec)
    src_v = ff.normalize_video_codec(getattr(info, "codec", "") if info else "")
    src_h = int(getattr(info, "height", 0) or 0) if info else 0
    is_hdr = bool(getattr(info, "is_hdr", False)) if info else False
    container = (getattr(info, "container", "") or "") if info else ""
    ac = (audio_codec or "").lower()

    enc_map = caps.results_map()
    dec_map = caps.decode_results_map()
    server_h264 = [p for p in ("nvidia", "intel", "amd", "cpu") if _cap_ok(p, "h264") or p == "cpu"]

    file_info = {
        "container": container,
        "video": src_v or (getattr(info, "codec", "") if info else ""),
        "audio": ac or "-",
        "height": src_h,
        "hdr": is_hdr,
    }
    browser_info = {
        "codecs": client,
        "direct_ok_flag": bool(client_direct_ok),
        "video_ok": _hls_video_copy_ok(src_v or "", client),
        "audio_hls_ok": _audio_hls_copy_ok(ac) if ac else True,
        "audio_direct_ok": _browser_audio_direct_ok(ac) if ac else True,
    }
    server_info = {
        "capabilities_ready": bool(enc_map) and bool(dec_map),
        "encode_h264_platforms": server_h264,
        "best_h264": pick_auto_platform("h264"),
    }

    reasons: list[str] = [
        f"Datei: {(src_v or '?').upper()}"
        + (f" {src_h}p" if src_h else "")
        + (" HDR" if is_hdr else "")
        + (f" · Ton {(ac or '–').upper()}"),
        f"Browser: {', '.join(c.upper() for c in client)}"
        + (f" · Video={'ok' if browser_info['video_ok'] else 'nein'}"
           f" · Ton-HLS={'ok' if browser_info['audio_hls_ok'] else 'nein'}"),
        f"Server: Encode-H.264 → {server_info['best_h264']}"
        + (" · Capabilities bereit" if server_info["capabilities_ready"]
           else " · Capabilities unvollständig"),
    ]

    def _result(**kw) -> dict:
        base = {
            "requested": requested,
            "strategy": kw.get("strategy", "transcode"),
            "profile": kw.get("profile", "720p"),
            "mode": kw.get("mode", "hls"),
            "use_video_copy": bool(kw.get("use_video_copy", False)),
            "drop_audio": bool(kw.get("drop_audio", False)),
            "will_xcode_audio": bool(kw.get("will_xcode_audio", False)),
            "need_encode": bool(kw.get("need_encode", False)),
            "encode": kw.get("encode") or {
                "platform": "cpu", "codec": "h264", "encoder": "copy", "warnings": [],
            },
            "height": int(kw.get("height", 0) or 0),
            "v_bitrate": int(kw.get("v_bitrate", 0) or 0),
            "effective_audio_index": kw.get("effective_audio_index", audio_index),
            "reasons": reasons + list(kw.get("extra_reasons") or []),
            "summary": kw.get("summary", ""),
            "file": file_info,
            "browser": browser_info,
            "server": server_info,
        }
        if not base["summary"]:
            base["summary"] = base["strategy"]
        return base

    # --- Explizite Modi (kein Auto-Baum) ---------------------------------
    if requested != "auto":
        if requested == "direct":
            return _result(
                strategy="direct", profile="direct", mode="direct",
                summary="Direct-Play (manuell)",
                extra_reasons=["Auswahl: Direct-Play erzwungen"],
                effective_audio_index=audio_index,
            )
        if requested == "copy":
            drop = bool(ac and not _audio_hls_copy_ok(ac))
            return _result(
                strategy="remux_silent" if drop else "remux_copy",
                profile="copy", mode="hls", use_video_copy=True,
                drop_audio=drop,
                effective_audio_index=(-1 if drop else audio_index),
                summary=("Remux ohne Ton" if drop else "Remux Stream-Copy"),
                extra_reasons=[
                    "Auswahl: Remux",
                    (f"Ton {ac.upper()} nicht HLS-fähig → weglassen" if drop
                     else "Ton kopierbar"),
                ],
            )
        # Encode-Profile
        enc = resolve_encode(platform, codec, client_codecs=client)
        prof = requested if requested in _ENCODE_PROFILES else "custom"
        h, br = _quality_params(prof, height, v_bitrate)
        return _result(
            strategy="transcode", profile=prof, mode="hls",
            need_encode=True, encode=enc, height=h, v_bitrate=br,
            will_xcode_audio=_audio_will_transcode(ac, want_audio_copy),
            summary=f"Transcode {prof} → {enc['platform']}/{enc['codec']}",
            extra_reasons=[f"Auswahl: Profil {requested}"],
        )

    # --- Automatik -------------------------------------------------------
    if want_burn:
        enc = resolve_encode(platform, "h264", client_codecs=client)
        h, br = _quality_params("720p", 0, v_bitrate)
        return _result(
            strategy="transcode", profile="720p", mode="hls",
            need_encode=True, encode=enc, height=h, v_bitrate=br,
            will_xcode_audio=_audio_will_transcode(ac, want_audio_copy),
            summary="Burn-in → Transcode 720p",
            extra_reasons=["Bild-Untertitel → Video muss encodiert werden"],
        )

    # Direct-Play: Container+Codec Server-seitig + Browser-Flag + Ton/Video ok
    can_dp = bool(info) and can_direct_play(info)
    video_ok = browser_info["video_ok"]
    audio_direct = browser_info["audio_direct_ok"] or audio_index < 0 or not ac
    seek_blocks = bool(start_sec and start_sec > 0)
    if (
        can_dp and client_direct_ok and video_ok and audio_direct
        and not seek_blocks and not want_audio_copy
    ):
        # want_audio_copy mit exotischem Ton: Direct oft ohne Ton nutzlos → HLS
        return _result(
            strategy="direct", profile="direct", mode="direct",
            summary="Direct-Play",
            extra_reasons=[
                "Container/Codecs browserfähig",
                "Kein Ton-Transcode nötig → günstigster Pfad",
            ],
        )

    # Video-Copy möglich?
    if video_ok:
        if want_audio_copy and ac and not _audio_hls_copy_ok(ac):
            return _result(
                strategy="remux_silent", profile="copy", mode="hls",
                use_video_copy=True, drop_audio=True,
                effective_audio_index=-1,
                summary="Video-Copy, Ton weggelassen",
                extra_reasons=[
                    f"Browser kann {(src_v or '?').upper()} → kein Video-Encode",
                    f"Ton {ac.upper()} + „nicht umcodieren“ → Spur stumm",
                ],
            )
        if _audio_will_transcode(ac, want_audio_copy):
            return _result(
                strategy="remux_audio_xcode", profile="copy", mode="hls",
                use_video_copy=True, will_xcode_audio=True,
                summary="Video-Copy + Ton→AAC (fMP4)",
                extra_reasons=[
                    f"Browser kann {(src_v or '?').upper()} → Video-Copy als fMP4"
                    + ("/hvc1" if src_v == "hevc" else ""),
                    f"Ton {ac.upper()} → AAC (gemeinsame Timeline ab 0)",
                ],
            )
        # Video+Ton kopierbar, aber kein Direct (z. B. MKV) → Remux
        return _result(
            strategy="remux_copy", profile="copy", mode="hls",
            use_video_copy=True,
            summary="Remux Stream-Copy (fMP4)",
            extra_reasons=[
                "Video/Ton für HLS-fMP4 kopierbar",
                "Kein Direct-Play (Container/Seek) → Remux",
            ],
        )

    # Video nicht browserfähig → Transcode; Server wählt Encode-HW
    targets = _server_encode_targets(client)
    enc = resolve_encode(platform, targets[0], client_codecs=client)
    # 4K-Live-Vorschau: 1080p reicht meist
    prof = "1080p" if src_h >= 1440 else "original"
    h, br = _quality_params(prof, height, v_bitrate)
    return _result(
        strategy="transcode", profile=prof, mode="hls",
        need_encode=True, encode=enc, height=h, v_bitrate=br,
        will_xcode_audio=_audio_will_transcode(ac, want_audio_copy),
        summary=f"Transcode {prof} → {enc['platform']}/{enc['encoder']}",
        extra_reasons=[
            f"Browser kann {(src_v or '?').upper()} nicht → Encode nötig",
            f"Server: {enc['platform']}/{enc['codec']}/{enc['encoder']}"
            + (" · HDR→SDR" if is_hdr else ""),
        ],
    )


def plan_for_path(
    rel: str,
    *,
    audio_index: int = 0,
    subtitle_index: int = -1,
    profile: str = "auto",
    burn_subs: bool = False,
    client_direct_ok: bool = False,
    client_codecs: Optional[list[str]] = None,
    platform: str = "auto",
    codec: str = "h264",
    height: int = 0,
    v_bitrate: int = 0,
    audio_copy: bool = False,
    start_sec: float = 0.0,
) -> dict:
    """Probe + plan_playback für die UI (ohne Session zu starten)."""
    target = config.resolve_input(rel)
    if target is None or not target.is_file():
        return {"error": "Datei nicht gefunden"}
    info, err = ff.probe_with_error(target)
    if err and not info:
        return {"error": f"Analyse fehlgeschlagen: {err}"}
    audio_codec = ""
    if info and info.audio and 0 <= audio_index < len(info.audio):
        audio_codec = str(info.audio[audio_index].get("codec") or "")
    sub_codec = ""
    if info and info.subtitles and 0 <= subtitle_index < len(info.subtitles):
        sub_codec = str(info.subtitles[subtitle_index].get("codec") or "")
    plan = plan_playback(
        info,
        profile=profile,
        audio_index=audio_index,
        audio_codec=audio_codec,
        subtitle_index=subtitle_index,
        sub_codec=sub_codec,
        start_sec=start_sec,
        burn_subs=burn_subs,
        client_direct_ok=client_direct_ok,
        client_codecs=client_codecs,
        platform=platform,
        codec=codec,
        height=height,
        v_bitrate=v_bitrate,
        audio_copy=audio_copy,
    )
    return {
        "plan": plan,
        "info": info.to_dict() if info else None,
    }


def resolve_encode(
    platform: str,
    codec: str,
    *,
    client_codecs: Optional[list[str]] = None,
) -> dict:
    """Plattform/Codec gegen Capabilities + Browser-Fähigkeit auflösen.

    Explizit gewählte Hardware wird nicht still auf eine andere GPU umgebogen
    (z. B. Intel+AV1 → NVIDIA). Stattdessen Codec-Fallback auf derselben
    Plattform, sonst CPU.

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

    want_auto = (platform or "auto").lower() == "auto"
    plat = pick_auto_platform(codec) if want_auto else (platform or "cpu").lower()
    if plat not in ("nvidia", "intel", "amd", "cpu"):
        plat = "cpu"
        warnings.append("Unbekannte Plattform – Fallback CPU.")

    if plat != "cpu" and not _cap_ok(plat, codec):
        if want_auto:
            alt = pick_auto_platform(codec)
            warnings.append(
                f"{plat}/{codec} laut Capabilities nicht verfügbar – nutze {alt}."
            )
            plat = alt
        else:
            # Gewählte HW beibehalten: Codec auf derselben Plattform senken.
            fb = next(
                (c for c in ("h264", "hevc", "av1")
                 if c in allowed and _cap_ok(plat, c)),
                None,
            )
            if fb:
                warnings.append(
                    f"{plat}/{codec} laut Capabilities nicht verfügbar – "
                    f"bleibe bei {plat}, nutze {fb}."
                )
                codec = fb
            else:
                warnings.append(
                    f"{plat} hat keinen nutzbaren Encoder – Fallback CPU/H.264."
                )
                plat, codec = "cpu", "h264"

    if plat == "cpu":
        enc = _CPU_ENC.get(codec, "libx264")
    else:
        enc = ff.encoder_name(plat, codec) or ""
        if not enc:
            warnings.append(f"Kein Encoder für {plat}/{codec} – Fallback CPU/H.264.")
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
    decode = caps.decode_results_map()
    auto_p = pick_auto_platform("h264")
    return {
        "profiles": [
            {"id": "auto", "label": "Automatisch (Direct-Play wenn möglich)"},
            {"id": "direct", "label": "Direct-Play (ohne Remux)"},
            {"id": "copy", "label": "Remux HLS (Video-Copy; Ton weglassen wenn nötig)"},
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
        "capabilities_ready": bool(results) and bool(decode),
        "lookahead_choices": [
            {"id": 15, "label": "15 s Puffer"},
            {"id": 30, "label": "30 s Puffer (empfohlen)"},
            {"id": 60, "label": "60 s Puffer"},
            {"id": 120, "label": "120 s Puffer"},
            {"id": 0, "label": "Unbegrenzt (kein Drosseln)"},
        ],
        "lookahead_default": int(_DEFAULT_LOOKAHEAD_SEC),
        "note": (
            "Für Live-Vorschau im Browser ist H.264 am sichersten. "
            "HEVC/AV1 nur, wenn der Browser sie meldet – sonst Fallback H.264. "
            "Encode-Vorlauf = Zielpuffer: FFmpeg läuft durchgehend und wird "
            "gedrosselt (Pause), sobald genug voraus liegt – ohne Neu-Start. "
            "Video: HW-Decode (CUDA/QSV/VAAPI) nur wenn der Funktionstest den "
            "Quellcodec freigibt, danach HW-Encode; sonst Software-Decode. "
            "Ton→AAC bleibt oft CPU. Optional Ton Stream-Copy."
        ),
    }


def _audio_hls_copy_ok(codec: str) -> bool:
    """Ton-Codec, der in HLS-fMP4 per Stream-Copy browserfähig ist."""
    return (codec or "").lower().startswith(("aac", "mp3", "mp4a"))


def _audio_will_transcode(codec: str, audio_copy: bool) -> bool:
    """True, wenn die Spur für HLS nach AAC umcodiert würde."""
    if audio_copy or not codec:
        return False
    return not _audio_hls_copy_ok(codec)


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


def _audio_args(audio_index: int, audio_codec: str,
                force_copy: bool = False,
                with_video_reencode: bool = False) -> list[str]:
    """Ton-Args für HLS.

    AAC-Timeline startet bei 0 (``first_pts`` + ``asetpts``). Video-Copy
    setzt PTS parallel per ``setts`` auf 0 – sonst läuft der kopierte
    Video-PTS weiter, während AAC bei 0 beginnt (Desync / Lücken).
    ``with_video_reencode`` bleibt für Aufrufer erhalten (Filter ist identisch).
    """
    if audio_index < 0:
        return ["-an"]
    args = ["-map", f"0:a:{int(audio_index)}?"]
    ac = (audio_codec or "").lower()
    if force_copy or _audio_hls_copy_ok(ac):
        args += ["-c:a", "copy"]
        return args
    # Stereo-Downmix vor aresample: DTS-X/TrueHD sonst mit Objektkanälen.
    af = (
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "aresample=async=1:first_pts=0:min_hard_comp=0.100,"
        "asetpts=PTS-STARTPTS"
    )
    args += [
        "-c:a", "aac", "-ac", "2", "-b:a", "192k", "-ar", "48000",
        "-profile:a", "aac_low",
        "-af", af,
    ]
    return args


def _vaapi_device() -> str:
    return getattr(config, "VAAPI_DEVICE", "/dev/dri/renderD128") or "/dev/dri/renderD128"


def _hwaccel_decode_args(platform: str, source_codec: str = "") -> list[str]:
    """HW-Decode passend zur Encode-Plattform (ohne GPU-Output-Format).

    Nur bei explizit positivem Decode-Funktionstest für den Quellcodec
    (kein Build-Optimistic-Fallback – der erzeugt oft A/V-Sprünge).
    Frames landen für Scale/setpts im System-RAM, danach hwupload zum Encoder.
    """
    plat = (platform or "").lower()
    if plat not in ("nvidia", "intel", "amd"):
        return []
    src = ff.normalize_video_codec(source_codec)
    if not src:
        return []
    from . import capabilities as caps
    decode_map = caps.decode_results_map()
    if not decode_map or not decode_map.get(f"{plat}:{src}"):
        return []
    backend = ff.encoder_backend(plat)
    if backend == "nvenc":
        return ["-hwaccel", "cuda"]
    if backend == "qsv":
        return ["-hwaccel", "qsv"]
    if backend == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_device", _vaapi_device()]
    return []


def _player_sdr_8bit_filters(*, is_hdr: bool, target_codec: str) -> list[str]:
    """HDR/10-bit → 8-bit SDR für Browser-H.264 (NVENC kann kein 10-bit H.264)."""
    out: list[str] = []
    want_h264 = (target_codec or "h264").lower() == "h264"
    if not want_h264:
        return out
    if is_hdr:
        out.append(_TONEMAP_CHAIN)  # endet bereits mit format=yuv420p
    else:
        # Auch SDR-10-bit (yuv420p10le) muss vor h264_nvenc auf 8-bit
        out.append("format=yuv420p")
    return out


def _build_video_filter(
    *,
    height: int,
    burn_sub_index: int,
    platform: str,
    encoder: str,
    target_codec: str = "h264",
    is_hdr: bool = False,
) -> tuple[list[str], list[str]]:
    """Filter + ggf. Extra-Args vor -i. Rückgabe (pre_input, map/filter args).

    ``setpts=PTS-STARTPTS`` setzt die Video-Timeline nach Seek auf 0 – analog
    zu ``asetpts`` beim Ton. Seek bleibt vor ``-i`` (schnell); kein langsames
    Decode-from-start. Bei H.264-Ziel: Tonemap/``yuv420p`` (10-bit-HDR-Quellen).
    """
    pre: list[str] = []
    backend = ff.encoder_backend(platform) if platform != "cpu" else "cpu"
    need_hwupload = "vaapi" in encoder or "qsv" in encoder
    sdr = _player_sdr_8bit_filters(is_hdr=is_hdr, target_codec=target_codec)

    scale = f"scale=-2:{int(height)}" if height and height > 0 else ""
    # setpts immer vor hwupload (nur Systemspeicher-Frames)
    pts = "setpts=PTS-STARTPTS"

    if burn_sub_index >= 0:
        # Overlay immer auf CPU, danach gemeinsame Timeline, ggf. hwupload
        pix = (",".join(sdr) + ",") if sdr else ""
        if scale:
            fc = (f"[0:v:0]{scale}[vs];"
                  f"[vs][0:s:{int(burn_sub_index)}]overlay=format=auto,{pix}{pts}")
        else:
            fc = (f"[0:v:0][0:s:{int(burn_sub_index)}]overlay=format=auto,"
                  f"{pix}{pts}")
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
    parts: list[str] = []
    if scale:
        parts.append(scale)
    parts.extend(sdr)
    parts.append(pts)
    if need_hwupload:
        # Nach SDR-8-bit: nv12-Upload
        if "format=yuv420p" not in ",".join(parts):
            parts.append("format=yuv420p")
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

    # NVENC / CPU
    return pre, ["-vf", ",".join(parts), "-map", "0:v:0?"]


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


def _normalize_lookahead(sec) -> float:
    try:
        v = float(sec)
    except (TypeError, ValueError):
        v = _DEFAULT_LOOKAHEAD_SEC
    if v <= 0:
        return 0.0
    # Auf bekannte Stufen snappen, sonst clampen
    if int(v) in _LOOKAHEAD_CHOICES:
        return float(int(v))
    return max(10.0, min(600.0, v))


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
    lookahead_sec: float = _DEFAULT_LOOKAHEAD_SEC,
    audio_copy: bool = False,
    source_codec: str = "",
    is_hdr: bool = False,
) -> list[str]:
    """HLS immer als fMP4 (hls.js-tauglich).

    Video-Copy + Ton→AAC: HEVC braucht ``-tag:v hvc1`` (nicht MPEG-TS).
    Copy ist instant, AAC nicht – ohne ``-readrate`` und große Mux-Queue
    spült der Muxer Video ohne Ton (Desync/Lücken). Video-PTS per ``setts``
    auf 0, analog zum AAC-``asetpts``.
    """
    playlist = out_dir / "index.m3u8"
    src = ff.normalize_video_codec(source_codec) or (codec or "").lower()
    will_xcode_a = (
        audio_index >= 0
        and not bool(audio_copy)
        and not _audio_hls_copy_ok(audio_codec)
    )
    cmd = [
        config.FFMPEG, "-hide_banner", "-nostdin",
        "-loglevel", _PLAYER_FF_LOGLEVEL or "warning",
        "-fflags", "+genpts",
        "-analyzeduration", "20000000",
        "-probesize", "20000000",
    ]

    pre: list[str] = []
    vmap: list[str] = []
    if not video_copy:
        cmd += _hwaccel_decode_args(platform, source_codec)
        pre, vmap = _build_video_filter(
            height=height, burn_sub_index=burn_sub_index,
            platform=platform, encoder=encoder,
            target_codec=codec or "h264",
            is_hdr=bool(is_hdr),
        )
        cmd += pre
    else:
        # Copy rast sonst in Echtzeit-Minuten voraus; AAC und hls.js kommen
        # nicht hinterher, delete_segments löscht den Anfang.
        cmd += ["-readrate", "1.5" if will_xcode_a else "3"]

    if start_sec and start_sec > 0:
        cmd += ["-ss", f"{float(start_sec):.3f}"]
        # Video-Copy sucht den vorherigen Keyframe; Standard ``accurate_seek``
        # dekodiert den Ton aber bis zur exakten -ss-Zeit. Beide Streams werden
        # danach auf PTS 0 gesetzt → Ton eilt dem Bild voraus.
        if video_copy:
            cmd += ["-noaccurate_seek", "-seek_timestamp", "1"]
    cmd += ["-i", str(path)]

    if video_copy:
        cmd += ["-map", "0:v:0?", "-c:v", "copy"]
        # Browser/hls.js erwarten hvc1/av01 in fMP4, nicht „hevc“/Annex-B-TS
        if src == "hevc":
            cmd += ["-tag:v", "hvc1"]
        elif src == "av1":
            cmd += ["-tag:v", "av01"]
        # Gemeinsame Timeline mit AAC (DTS-Offset bleibt erhalten)
        cmd += ["-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS"]
    else:
        cmd += vmap
        cmd += ["-c:v", encoder]
        cmd += _encoder_rate_args(encoder, codec, v_bitrate)

    cmd += _audio_args(
        audio_index, audio_codec,
        force_copy=bool(audio_copy),
        with_video_reencode=not bool(video_copy),
    )

    if video_copy and will_xcode_a:
        cmd += [
            "-max_muxing_queue_size", "8192",
            "-max_interleave_delta", "30000000",
        ]

    la = _normalize_lookahead(lookahead_sec)
    hls_time = 2
    if la > 0:
        # Mehr Segmente behalten, damit delete_segments nicht den Start frisst
        extra = 10 if video_copy else 4
        list_size = max(8, int(math.ceil(la / hls_time)) + extra)
    else:
        list_size = 30
    # Copy: keine independent_segments (Schnitt fällt nicht immer auf IDR)
    hls_flags = (
        "omit_endlist+delete_segments+temp_file"
        if video_copy else
        "independent_segments+omit_endlist+delete_segments+temp_file"
    )

    cmd += [
        "-sn", "-dn",
        "-avoid_negative_ts", "make_zero",
        "-start_at_zero",
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-f", "hls",
        "-hls_time", str(hls_time),
        "-hls_list_size", str(list_size),
        "-hls_flags", hls_flags,
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(out_dir / "seg_%05d.m4s"),
        str(playlist),
    ]
    return cmd


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
    lookahead_sec: float = _DEFAULT_LOOKAHEAD_SEC,
    audio_copy: bool = False,
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

    want_audio_copy = bool(audio_copy)
    want_burn = bool(burn_subs) and subtitle_index >= 0 and _is_image_sub(sub_codec)
    requested = (profile or "auto").lower()

    plan = plan_playback(
        info,
        profile=requested,
        audio_index=audio_index,
        audio_codec=audio_codec,
        subtitle_index=subtitle_index,
        sub_codec=sub_codec,
        start_sec=start_sec,
        burn_subs=want_burn,
        client_direct_ok=client_direct_ok,
        client_codecs=client_codecs,
        platform=platform,
        codec=codec,
        height=height,
        v_bitrate=v_bitrate,
        audio_copy=want_audio_copy,
    )

    resolved = plan["profile"]
    use_video_copy = bool(plan["use_video_copy"]) and not want_burn
    drop_audio = bool(plan["drop_audio"])
    will_xcode_audio = bool(plan["will_xcode_audio"]) and not drop_audio
    need_encode = bool(plan["need_encode"]) or want_burn
    enc_info = dict(plan.get("encode") or {})
    h = int(plan.get("height") or 0)
    br = int(plan.get("v_bitrate") or 0)
    effective_audio = int(plan.get("effective_audio_index", audio_index))
    if drop_audio:
        effective_audio = -1

    if want_burn and not need_encode:
        need_encode = True
        resolved = "original" if resolved in ("direct", "copy") else resolved
        enc_info = resolve_encode(
            platform, codec, client_codecs=client_codecs or ["h264"],
        )
        h, br = _quality_params(resolved if resolved in _ENCODE_PROFILES else "720p",
                                height, v_bitrate)
        use_video_copy = False

    sid = uuid.uuid4().hex[:12]
    work = _ensure_root() / sid
    work.mkdir(parents=True, exist_ok=True)

    la = _normalize_lookahead(lookahead_sec)
    start0 = max(0.0, float(start_sec or 0))

    warn_parts = list(enc_info.get("warnings") or [])
    if plan.get("summary"):
        warn_parts.append(str(plan["summary"]))
    warn = "; ".join(warn_parts)

    sess = PlayerSession(
        id=sid,
        path=target,
        rel=rel,
        audio_index=effective_audio,
        subtitle_index=int(subtitle_index),
        start_sec=start0,
        duration=duration,
        title=target.name,
        profile=resolved,
        platform=enc_info["platform"] if need_encode else "cpu",
        codec=enc_info["codec"] if need_encode else "",
        encoder=enc_info["encoder"] if need_encode else ("direct" if resolved == "direct" else "copy"),
        burn_subs=want_burn,
        audio_codec=audio_codec if not drop_audio else "",
        mode="direct" if plan["mode"] == "direct" else "hls",
        height=h,
        v_bitrate=br,
        lookahead_sec=la,
        window_end=0.0,
        audio_copy=want_audio_copy and not drop_audio,
        warning=warn,
        work_dir=work,
        decision_summary=str(plan.get("summary") or ""),
        decision_reasons=list(plan.get("reasons") or []),
        decision_strategy=str(plan.get("strategy") or ""),
    )

    source_vcodec = ff.normalize_video_codec(
        getattr(info, "codec", "") if info else ""
    )
    is_hdr = bool(getattr(info, "is_hdr", False)) if info else False

    _plog(
        sid,
        "PLAN strategy=%s profile=%s summary=%s | %s",
        plan.get("strategy"), resolved, plan.get("summary"),
        " · ".join(plan.get("reasons") or []),
    )

    if sess.mode == "direct":
        with _LOCK:
            _SESSIONS[sid] = sess
        return {
            "session": sess.to_dict(),
            "info": info.to_dict() if info else None,
            "chapters": chapters,
            "options": player_options(),
            "plan": plan,
        }

    if use_video_copy:
        cmd = _build_hls_cmd(
            target, work,
            audio_index=sess.audio_index,
            start_sec=sess.start_sec,
            audio_codec=audio_codec,
            platform="cpu", encoder="copy", codec="h264",
            height=0, v_bitrate=0, burn_sub_index=-1, video_copy=True,
            lookahead_sec=la,
            audio_copy=want_audio_copy and not drop_audio,
            source_codec=source_vcodec,
            is_hdr=is_hdr,
        )
        sess.encoder = "copy"
        sess.platform = "cpu"
        sess.codec = ""
    else:
        cmd = _build_hls_cmd(
            target, work,
            audio_index=sess.audio_index,
            start_sec=sess.start_sec,
            audio_codec=audio_codec if not drop_audio else "",
            platform=sess.platform,
            encoder=sess.encoder,
            codec=sess.codec or "h264",
            height=h,
            v_bitrate=br,
            burn_sub_index=subtitle_index if want_burn else -1,
            video_copy=False,
            lookahead_sec=la,
            audio_copy=want_audio_copy and not drop_audio,
            source_codec=source_vcodec,
            is_hdr=is_hdr,
        )

    sess.debug_cmd = _cmd_preview(cmd, 800)
    _plog(
        sid,
        "start profile=%s→%s mode=hls video_copy=%s audio_xcode=%s drop_audio=%s "
        "src=%s/%s audio=%s plat=%s enc=%s la=%s | %s",
        requested, resolved, use_video_copy, will_xcode_audio, drop_audio,
        getattr(info, "container", "") if info else "?",
        source_vcodec or (getattr(info, "codec", "?") if info else "?"),
        audio_codec or "-",
        sess.platform, sess.encoder, la, sess.debug_cmd,
    )

    try:
        sess.proc = _spawn_ffmpeg(cmd, sid, work)
    except OSError as e:
        _plog(sid, "FFmpeg-Start fehlgeschlagen: %s", e, level=logging.ERROR)
        shutil.rmtree(work, ignore_errors=True)
        return {"error": f"FFmpeg-Start fehlgeschlagen: {e}"}

    with _LOCK:
        _SESSIONS[sid] = sess

    deadline = time.time() + (10.0 if need_encode else 6.0)
    while time.time() < deadline:
        if sess.playlist.is_file() and sess.playlist.stat().st_size > 0:
            _plog(sid, "Playlist bereit (%s bytes)", sess.playlist.stat().st_size)
            break
        if sess.proc.poll() is not None:
            tail = _tail_ffmpeg_log(work)
            sess.error = (tail or "FFmpeg beendet")[-800:]
            _plog(
                sid,
                "FFmpeg exit=%s vor Playlist. Log:\n%s",
                sess.proc.returncode, tail or "(leer)",
                level=logging.ERROR,
            )
            # Einmaliger Fallback CPU/H.264 bei HW-Fehler
            if need_encode and sess.platform != "cpu" and not sess.playlist.exists():
                _plog(sid, "HW-Fallback → CPU/libx264", level=logging.WARNING)
                _kill(sess)
                for old in work.glob("*"):
                    try:
                        if old.name != "ffmpeg.log":
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
                    audio_codec=audio_codec if not drop_audio else "",
                    platform="cpu", encoder="libx264", codec="h264",
                    height=h, v_bitrate=br or 3500,
                    burn_sub_index=subtitle_index if want_burn else -1,
                    lookahead_sec=la,
                    audio_copy=want_audio_copy and not drop_audio,
                    source_codec=source_vcodec,
                    is_hdr=is_hdr,
                )
                sess.debug_cmd = _cmd_preview(cmd, 800)
                try:
                    sess.proc = _spawn_ffmpeg(cmd, sid, work)
                    deadline = time.time() + 10.0
                    continue
                except OSError as e2:
                    sess.error = str(e2)
                    _plog(sid, "Fallback-Start fehlgeschlagen: %s", e2, level=logging.ERROR)
            break
        time.sleep(0.1)

    if not sess.playlist.is_file() and not sess.error:
        sess.error = "Timeout: keine HLS-Playlist erzeugt"
        _plog(sid, "%s\n%s", sess.error, _tail_ffmpeg_log(work) or "(kein log)",
              level=logging.ERROR)

    return {
        "session": sess.to_dict(),
        "info": info.to_dict() if info else None,
        "chapters": chapters,
        "options": player_options(),
        "plan": plan,
    }


def log_client_error(sid: str, payload: dict) -> dict:
    """hls.js-/Browser-Fehler aus der UI in Container-Logs schreiben."""
    sess = get_session(sid)
    detail = {
        "type": payload.get("type"),
        "details": payload.get("details"),
        "fatal": payload.get("fatal"),
        "error": payload.get("error"),
        "url": payload.get("url"),
        "note": payload.get("note"),
    }
    msg = json.dumps(detail, ensure_ascii=False)[:1000]
    if sess:
        sess.last_client_error = msg
        tail = _tail_ffmpeg_log(sess.work_dir)
        _plog(
            sid,
            "CLIENT-ERROR %s | session mode=%s profile=%s enc=%s running=%s | ffmpeg-tail:\n%s",
            msg,
            sess.mode, sess.profile, sess.encoder,
            bool(sess.proc and sess.proc.poll() is None),
            tail or "(kein ffmpeg.log)",
            level=logging.WARNING,
        )
        return {
            "ok": True,
            "ffmpeg_log_tail": tail,
            "debug_cmd": sess.debug_cmd,
            "session_error": sess.error,
        }
    logger.warning("Player[%s] CLIENT-ERROR (unbekannte Session): %s", sid, msg)
    return {"ok": False, "error": "Session nicht gefunden"}


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


def pause_encode(sid: str) -> dict:
    """FFmpeg per SIGSTOP anhalten (bei Pause im Player) – stoppt CPU/GPU-Last."""
    sess = get_session(sid)
    if not sess:
        return {"ok": False, "error": "Session nicht gefunden"}
    if sess.mode != "hls":
        return {"ok": True, "encode_paused": False, "skipped": True}
    if not sess.proc or sess.proc.poll() is not None:
        return {"ok": True, "encode_paused": False, "running": False}
    if sess.encode_paused:
        return {"ok": True, "encode_paused": True}
    try:
        os.kill(sess.proc.pid, signal.SIGSTOP)
        sess.encode_paused = True
        return {"ok": True, "encode_paused": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def resume_encode(sid: str) -> dict:
    """FFmpeg nach Pause fortsetzen (SIGCONT)."""
    sess = get_session(sid)
    if not sess:
        return {"ok": False, "error": "Session nicht gefunden"}
    if sess.mode != "hls":
        return {"ok": True, "encode_paused": False, "skipped": True}
    if not sess.proc or sess.proc.poll() is not None:
        return {"ok": True, "encode_paused": False, "running": False}
    if not sess.encode_paused:
        return {"ok": True, "encode_paused": False}
    try:
        os.kill(sess.proc.pid, signal.SIGCONT)
        sess.encode_paused = False
        return {"ok": True, "encode_paused": False}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def _kill(sess: PlayerSession) -> None:
    if sess.proc and sess.proc.poll() is None:
        try:
            # Falls per SIGSTOP eingefroren: erst fortsetzen, dann beenden
            if sess.encode_paused:
                try:
                    os.kill(sess.proc.pid, signal.SIGCONT)
                except OSError:
                    pass
                sess.encode_paused = False
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
