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

import copy
import json
import logging
import re
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
from . import runtime
from .encoder import _TONEMAP_CHAIN

logger = logging.getLogger("vcompress.player_hls")

_SESSIONS: dict[str, "PlayerSession"] = {}
# client_token → sid, sobald die Session registriert ist (auch während des Starts).
_TOKENS: dict[str, str] = {}
# Tokens, deren Start noch nicht registriert ist und schon abgebrochen wurde.
_PENDING_CANCEL: set[str] = set()
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
            # Eigene Prozessgruppe: Tonemap-/OpenCL-Helfer sterben mit SIGKILL mit.
            start_new_session=True,
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


def _playlist_size(path: Path) -> int:
    """Stop/Idle-Cleanup darf zwischen Existenzprüfung und stat stattfinden."""
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


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
    look_summary: str = ""
    client_token: str = ""
    cancel: bool = False
    owner: str = "player"
    label: str = ""
    starting: bool = False
    finished_at: Optional[float] = None
    source: dict = field(default_factory=dict)
    requested: dict = field(default_factory=dict)
    effective: dict = field(default_factory=dict)
    fallback_reasons: list[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    metrics: runtime.ProcessMetrics = field(default_factory=runtime.ProcessMetrics, repr=False)

    @property
    def playlist(self) -> Path:
        return self.work_dir / "index.m3u8"

    def touch(self) -> None:
        with self.lock:
            self.last_access = time.time()

    def to_dict(self, *, include_log: bool = True) -> dict:
        with self.lock:
            result = self._snapshot()
        # Log-Dateien und Prozessmessung nicht unter dem Session-Lock lesen.
        result["ffmpeg_log_tail"] = _tail_ffmpeg_log(self.work_dir) if include_log else ""
        return result

    def _snapshot(self) -> dict:
        ready = True
        playlist_url = ""
        if self.mode == "hls":
            ready = _playlist_size(self.playlist) > 0
            playlist_url = f"/api/player/session/{self.id}/index.m3u8"
        running = runtime.process_running(self.proc)
        if self.cancel:
            state = "stopping" if running or self.starting else "finished"
        elif self.mode == "direct":
            state = "running"
        elif running:
            state = "paused" if self.encode_paused else "running"
        else:
            state = "running" if self.starting else "finished"
        if state == "finished":
            self.finished_at = self.finished_at or time.time()
        return {
            "id": self.id,
            "session_id": self.id,
            "kind": "player_hls",
            "owner": self.owner,
            "label": self.label or self.title,
            "state": state,
            "pid": getattr(self.proc, "pid", None),
            "process_running": running,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "last_access": self.last_access,
            "starting": self.starting,
            "can_stop": not self.cancel,
            "source": copy.deepcopy(self.source),
            "requested": copy.deepcopy(self.requested),
            "effective": copy.deepcopy(self.effective),
            "fallback": bool(self.fallback_reasons),
            "fallback_reasons": list(self.fallback_reasons),
            **self.metrics.sample(self.proc),
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
            "last_client_error": self.last_client_error,
            "decision_summary": self.decision_summary,
            "decision_reasons": list(self.decision_reasons or []),
            "decision_strategy": self.decision_strategy,
            "look_summary": self.look_summary,
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
    look: Optional[dict] = None,
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
    preview = _normalize_look(look)
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

    if _look_needs(preview, is_hdr):
        targets = _server_encode_targets(client)
        enc = resolve_encode(platform, codec or targets[0], client_codecs=client)
        prof = "1080p" if src_h >= 1440 else "original"
        h, br = _quality_params(prof, height, v_bitrate)
        return _result(
            strategy="transcode", profile=prof, mode="hls",
            need_encode=True, encode=enc, height=h, v_bitrate=br,
            will_xcode_audio=_audio_will_transcode(ac, want_audio_copy),
            summary=f"Transcode {prof}, Encode-Vorschau",
            extra_reasons=[
                "Encode-Vorschau: Tonemap und Bildfilter brauchen einen Transcode",
                f"Server: {enc['platform']}/{enc['codec']}/{enc['encoder']}"
                + (" · HDR→SDR" if is_hdr else ""),
            ],
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
    enc = resolve_encode(platform, codec or targets[0], client_codecs=client)
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
    look: Optional[dict] = None,
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
        look=look,
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
    from .encoder import jellyfin_filters
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
        "jellyfin_filters": sorted(jellyfin_filters()),
        "intel_uses_vaapi": bool(ff.intel_uses_vaapi()),
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
                with_video_reencode: bool = False,
                start_sec: float = 0.0) -> list[str]:
    """Ton-Args für HLS (Jellyfin-Modell: keine eigene PTS-Nullung).

    Zeitstempel bleiben über ``-copyts -start_at_zero`` an Video gekoppelt.
    ``asetpts`` / ``first_pts=0`` würden Ton unabhängig auf 0 setzen und
    nach Seek gegen Video-Copy laufen.
    Video-Transcode + Ton-Copy: ``noise``-BSF droppt Packets vor dem Seek
    (Jellyfin #16580), weil Copy den Decoder-Trim von ``accurate_seek`` umgeht.
    """
    if audio_index < 0:
        return ["-an"]
    args = ["-map", f"0:a:{int(audio_index)}?"]
    ac = (audio_codec or "").lower()
    if force_copy or _audio_hls_copy_ok(ac):
        args += ["-c:a", "copy"]
        if with_video_reencode and float(start_sec or 0) > 0:
            # ffmpeg 5+: drop='lt(pts*tb\,seek)' – Komma im Ausdruck escapen
            args += [
                "-bsf:a",
                f"noise=drop='lt(pts*tb\\,{float(start_sec):.3f})'",
            ]
        return args
    # Stereo-Downmix: DTS-X/TrueHD sonst mit Objektkanälen.
    # aresample=async gleicht Drift aus, ohne die Timeline auf 0 zu zwingen.
    af = (
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "aresample=async=1:min_hard_comp=0.100"
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
    Frames landen für Scale im System-RAM, danach hwupload zum Encoder.
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


_PLAYER_SDR_TAGS = (
    "setparams=range=limited:color_primaries=bt709"
    ":color_trc=bt709:colorspace=bt709"
)


def _player_sdr_8bit_filters(*, is_hdr: bool, target_codec: str) -> list[str]:
    """HDR/10-bit → 8-bit SDR für Browser-H.264 (NVENC kann kein 10-bit H.264).

    Feste CPU-Kette. Die Encode-Vorschau hängt ihre eigene Kette an und
    ruft das hier nicht auf.
    """
    out: list[str] = []
    want_h264 = (target_codec or "h264").lower() == "h264"
    if not want_h264:
        return out
    if is_hdr:
        out.append(_TONEMAP_CHAIN)  # endet bereits mit format=yuv420p
    else:
        # Auch SDR-10-bit (yuv420p10le) muss vor h264_nvenc auf 8-bit
        out.append("format=yuv420p")
    # Kennung, keine Helligkeitsumrechnung: sonst setzt der Encoder oft
    # vollen Bereich und der Browser hebt Schwarz an.
    out.append(_PLAYER_SDR_TAGS)
    return out


def _drop_cuda_hwaccel(cmd: list[str], pre: list[str]) -> list[str]:
    """``-hwaccel cuda`` nur einmal. Der Decode setzt es, die CUDA-Tonemap auch."""
    has_cuda = any(
        cmd[i] == "-hwaccel" and i + 1 < len(cmd) and cmd[i + 1] == "cuda"
        for i in range(len(cmd))
    )
    if not has_cuda:
        return pre
    out: list[str] = []
    i = 0
    while i < len(pre):
        if pre[i] == "-hwaccel" and i + 1 < len(pre) and pre[i + 1] == "cuda":
            i += 2
            continue
        out.append(pre[i])
        i += 1
    return out


def _normalize_look(raw) -> Optional[dict]:
    """Encode-Formular → Vorschau. Ohne ``apply`` bleibt die feste CPU-Kette."""
    if not isinstance(raw, dict) or not raw.get("apply"):
        return None
    from .encoder import (
        normalize_deband, normalize_picture_level, normalize_tonemap_algo,
        normalize_tonemap_desat, normalize_tonemap_engine, normalize_tonemap_peak,
    )
    platform = str(raw.get("platform") or "cpu").strip().lower() or "cpu"
    return {
        "apply": True,
        "tonemap": bool(raw.get("tonemap", True)),
        "engine": normalize_tonemap_engine(raw.get("tonemap_engine") or "cpu"),
        "algo": normalize_tonemap_algo(raw.get("tonemap_algo") or "hable"),
        "desat": normalize_tonemap_desat(raw.get("tonemap_desat", 0)),
        "peak": normalize_tonemap_peak(raw.get("tonemap_peak", 0)),
        "platform": platform,
        "deband": normalize_deband(raw.get("deband") or "off"),
        "denoise": normalize_picture_level(raw.get("denoise") or "off"),
        "sharpen": normalize_picture_level(raw.get("sharpen") or "off"),
        "grain": normalize_picture_level(raw.get("grain") or "off"),
    }


def _look_needs(look: Optional[dict], is_hdr: bool) -> bool:
    """Transcode, damit die Vorschau die Encode-Kurve und die Bildfilter zeigt."""
    if not look:
        return False
    if any(look.get(k) not in (None, "", "off") for k in ("deband", "denoise", "sharpen", "grain")):
        return True
    return bool(is_hdr and look.get("tonemap"))


def _build_video_filter(
    *,
    height: int,
    burn_sub_index: int,
    platform: str,
    encoder: str,
    target_codec: str = "h264",
    is_hdr: bool = False,
    look: Optional[dict] = None,
) -> tuple[list[str], list[str], str]:
    """Filter + ggf. Extra-Args vor -i. Rückgabe (pre_input, map/filter args, binary).

    ``binary`` leer = BtbN. Jellyfin nur, wenn die gewählte Tonemap-Engine
    dessen Filter braucht. Zeitstempel kommen von ``-copyts -start_at_zero``.
    """
    pre: list[str] = []
    binary = ""
    backend = ff.encoder_backend(platform) if platform != "cpu" else "cpu"
    need_hwupload = "vaapi" in encoder or "qsv" in encoder
    sdr = _player_sdr_8bit_filters(is_hdr=is_hdr, target_codec=target_codec)
    want_h264 = (target_codec or "h264").lower() == "h264"
    scale = f"scale=-2:{int(height)}" if height and height > 0 else ""

    def apply_look(base: list[str]) -> list[str]:
        nonlocal pre, binary
        picture = bool(look) and any(
            look.get(k) not in (None, "", "off")
            for k in ("deband", "denoise", "sharpen", "grain")
        )
        if not look or (not picture and not is_hdr):
            return base + sdr
        from .encoder import player_preview_setup
        chosen = bool(look.get("tonemap")) and is_hdr
        # H.264 für den Browser ist SDR. Behält der Encode HDR, bleibt die
        # Anzeige auf der CPU-Kurve. Die Bildfilter kommen danach.
        tone = chosen or (is_hdr and want_h264)
        b, extra, pieces = player_preview_setup(
            tone=tone,
            engine=look.get("engine") if chosen else "cpu",
            platform=look.get("platform") if chosen else "cpu",
            algo=look.get("algo") if chosen else "hable",
            desat=look.get("desat") if chosen else 0,
            peak=look.get("peak") if chosen else 0,
            deband=look.get("deband") or "off",
            denoise=look.get("denoise") or "off",
            sharpen=look.get("sharpen") or "off",
            grain=look.get("grain") or "off",
            encoder=encoder,
        )
        if b:
            binary = b
        if extra:
            pre = list(extra) + pre
        out = list(base)
        out.extend(pieces)
        joined = ",".join(out)
        if want_h264 and "format=yuv420p" not in joined:
            out.append("format=yuv420p")
        if want_h264:
            out.append(_PLAYER_SDR_TAGS)
        return out

    if burn_sub_index >= 0:
        chain = apply_look([scale] if scale else [])
        pix = ",".join(chain)
        if scale:
            fc = (f"[0:v:0]{scale}[vs];"
                  f"[vs][0:s:{int(burn_sub_index)}]overlay=format=auto")
            # scale steht schon in der Overlay-Kette; die Look-Filter danach.
            rest = [p for p in chain if p != scale]
            pix = ",".join(rest)
        else:
            fc = "[0:v:0][0:s:{0}]overlay=format=auto".format(int(burn_sub_index))
        if pix:
            fc += "," + pix
        if need_hwupload:
            fc += ",format=nv12,hwupload"
            if "qsv" in encoder:
                fc += "=extra_hw_frames=64"
            fc += "[vout]"
            if backend == "vaapi":
                pre += ["-vaapi_device", _vaapi_device()]
            elif backend == "qsv":
                pre += ["-init_hw_device", f"qsv=hw:{_vaapi_device()}",
                        "-filter_hw_device", "hw"]
        else:
            fc += "[vout]"
        return pre, ["-filter_complex", fc, "-map", "[vout]"], binary

    parts = apply_look([scale] if scale else [])
    if need_hwupload:
        if "format=yuv420p" not in ",".join(parts):
            parts.append("format=yuv420p")
        parts.append("format=nv12")
        if "qsv" in encoder:
            parts.append("hwupload=extra_hw_frames=64")
        else:
            parts.append("hwupload")
        vf = ",".join(parts)
        if backend == "vaapi":
            pre += ["-vaapi_device", _vaapi_device()]
        elif backend == "qsv":
            pre += ["-init_hw_device", f"qsv=hw:{_vaapi_device()}",
                    "-filter_hw_device", "hw"]
        return pre, ["-vf", vf, "-map", "0:v:0?"], binary

    if parts:
        return pre, ["-vf", ",".join(parts), "-map", "0:v:0?"], binary
    return pre, ["-map", "0:v:0?"], binary


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
    look: Optional[dict] = None,
) -> list[str]:
    """HLS als fMP4, Zeitstempel wie Jellyfin Dynamic HLS.

    Jellyfin (DynamicHlsController / EncodingHelper):
      ``-copyts -avoid_negative_ts disabled -start_at_zero``
      ``-hls_playlist_type event -hls_list_size 0`` (wachsende Playlist,
      keine ``delete_segments``)
      ``-hls_segment_options movflags=+frag_discont+skip_sidx``
      (AAC-Encoder-Delay in TFDT; kein SIDX-Rewrite an Open-GOP)
      Video-Copy: ``-tag:v hvc1``, Seek ``-ss`` vor ``-i``, bei fMP4
      ``-noaccurate_seek`` damit Ton denselben Keyframe trifft.
      Remux-Seek +0,5 s, damit ffmpeg den Keyframe am Ziel trifft, nicht
      den davor.

    Lookahead drosselt FFmpeg clientseitig (SIGSTOP), nicht per Sliding Window.
    """
    _ = lookahead_sec  # Drossel läuft in player.js, nicht über delete_segments
    playlist = out_dir / "index.m3u8"
    src = ff.normalize_video_codec(source_codec) or (codec or "").lower()
    will_xcode_a = (
        audio_index >= 0
        and not bool(audio_copy)
        and not _audio_hls_copy_ok(audio_codec)
    )
    # Remux: längere Segmente an GOP-Grenzen; Transcode: kürzer für Latenz
    hls_time = 6 if video_copy else 3
    cmd = [
        config.FFMPEG, "-hide_banner", "-nostdin",
        "-loglevel", _PLAYER_FF_LOGLEVEL or "warning",
        "-fflags", "+genpts",
        "-analyzeduration", "20000000",
        "-probesize", "20000000",
    ]

    pre: list[str] = []
    vmap: list[str] = []
    binary = ""
    if not video_copy:
        cmd += _hwaccel_decode_args(platform, source_codec)
        pre, vmap, binary = _build_video_filter(
            height=height, burn_sub_index=burn_sub_index,
            platform=platform, encoder=encoder,
            target_codec=codec or "h264",
            is_hdr=bool(is_hdr),
            look=look,
        )
        pre = _drop_cuda_hwaccel(cmd, pre)
        cmd += pre
        if binary:
            cmd[0] = binary

    if start_sec and start_sec > 0:
        ss = float(start_sec)
        if video_copy:
            # Jellyfin GetFastSeek: +0,5 s, sonst landet Copy auf dem
            # *vorherigen* Keyframe, wenn die Zielzeit selbst ein Keyframe ist.
            ss += 0.5
        cmd += ["-ss", f"{ss:.3f}"]
        # Copy kann nicht framegenau; ohne noaccurate_seek trimmt AAC auf
        # die exakte -ss-Zeit, Video startet am Keyframe (Desync).
        if video_copy:
            cmd += ["-noaccurate_seek"]
    cmd += ["-i", str(path)]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]

    if video_copy:
        cmd += ["-map", "0:v:0?", "-c:v", "copy"]
        # Browser/hls.js erwarten hvc1/av01 in fMP4, nicht hev1/Annex-B
        if src == "hevc":
            cmd += ["-tag:v", "hvc1"]
        elif src == "av1":
            cmd += ["-tag:v", "av01"]
        cmd += ["-start_at_zero"]
    else:
        cmd += vmap
        cmd += ["-c:v", encoder]
        cmd += _encoder_rate_args(encoder, codec, v_bitrate)
        # Keyframes an Segmentgrenzen (Jellyfin GetHlsVideoKeyFrameArguments)
        gop = max(24, int(hls_time * 24))
        cmd += ["-g", str(gop), "-keyint_min", str(gop)]
        if encoder in ("libx264", "libx265"):
            cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{hls_time})"]
        if encoder == "libx264":
            cmd += ["-sc_threshold", "0"]
        if (codec or "h264").lower() == "h264":
            cmd += ["-colorspace", "bt709", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-color_range", "tv"]
        else:
            cmd += ["-color_range", "tv"]
        cmd += ["-start_at_zero"]

    cmd += _audio_args(
        audio_index, audio_codec,
        force_copy=bool(audio_copy),
        with_video_reencode=not bool(video_copy),
        start_sec=float(start_sec or 0),
    )

    queue = "8192" if (video_copy and will_xcode_a) else "2048"
    cmd += [
        "-sn", "-dn",
        "-copyts",
        "-avoid_negative_ts", "disabled",
        "-max_muxing_queue_size", queue,
        "-f", "hls",
        "-max_delay", "5000000",
        "-hls_time", str(hls_time),
        "-hls_playlist_type", "event",
        "-hls_list_size", "0",
        "-hls_flags", "temp_file",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_options", "movflags=+frag_discont+skip_sidx",
        "-hls_segment_filename", str(out_dir / "seg_%05d.m4s"),
        str(playlist),
    ]
    return cmd


def _record_effective(sess: PlayerSession, cmd: Optional[list[str]] = None,
                      *, fallback_reason: str = "") -> None:
    """Ist-Verarbeitung aus dem gebauten Kommando, nicht aus der UI-Auswahl.

    Insbesondere kann player_preview_setup eine nicht verfügbare Engine und
    BT.2390 auf CPU/Hable auflösen. VAAPI hat keine wählbare Kurve.
    """
    args = cmd or []

    def arg(name: str, default: str = "") -> str:
        try:
            return args[args.index(name) + 1]
        except (ValueError, IndexError):
            return default

    graph = arg("-vf") + "," + arg("-filter_complex")
    engine, algo = None, None
    for token, candidate in (("tonemap_cuda=", "cuda"),
                             ("tonemap_opencl=", "opencl"),
                             ("tonemap_vaapi=", "vaapi"),
                             ("tonemapx=", "tonemapx"),
                             ("libplacebo=", "libplacebo"),
                             ("tonemap=tonemap=", "cpu")):
        if token in graph:
            engine = candidate
            break
    if engine and engine != "vaapi":
        match = re.search(r"(?:tonemap=|tonemapping=)([a-z0-9.]+)", graph)
        # CPU's first 'tonemap=' contains the filter option, not the curve.
        if engine == "cpu":
            match = re.search(r"tonemap=tonemap=([a-z0-9.]+)", graph)
        algo = match.group(1).replace("bt.2390", "bt2390") if match else None
    picture_tokens = {"deband": "gradfun=", "denoise": "hqdn3d=",
                      "sharpen": "unsharp=", "grain": "noise=alls="}
    look = sess.requested.get("look") or {}
    picture = {key: look.get(key, "off") if token in graph else "off"
               for key, token in picture_tokens.items()}
    video_mode = "direct" if sess.mode == "direct" else (
        "copy" if arg("-c:v", sess.encoder) == "copy" else "transcode")
    sub_mode = "none"
    if sess.subtitle_index >= 0:
        if "overlay=" in graph:
            sub_mode = "burn"
        elif ms.is_text_subtitle(str(sess.source.get("subtitle_codec") or "")):
            sub_mode = "external"
        else:
            sub_mode = "not_applied"
    reasons = list(sess.fallback_reasons)

    def reason(text: str) -> None:
        if text not in reasons:
            reasons.append(text)

    if fallback_reason:
        reason(fallback_reason)
    if video_mode == "transcode":
        selected_platform = sess.requested.get("platform")
        if selected_platform not in (None, "", "auto", sess.platform):
            reason(f"Encoder-Plattform {selected_platform} → {sess.platform}.")
        selected_codec = sess.requested.get("codec")
        if selected_codec and selected_codec != sess.codec:
            reason(f"Zielcodec {selected_codec} → {sess.codec}.")
    if look and sess.source.get("hdr") and look.get("tonemap"):
        if engine != look.get("engine"):
            reason(f"Tonemap-Engine {look.get('engine')} → {engine or 'nicht angewendet'}.")
        if engine != "vaapi" and algo != look.get("algo"):
            reason(f"Tonemap-Kurve {look.get('algo')} → {algo or 'nicht angewendet'}.")
    elif look and engine:
        reason(f"Browser-SDR benötigt Tonemap trotz Auswahl: {engine}/{algo or 'Treiberkurve'}.")
    for key, value in picture.items():
        if look.get(key, "off") != value:
            reason(f"Vorschaufilter {key} nicht angewendet ({video_mode}).")
    if sess.requested.get("burn_subs") and sub_mode != "burn":
        reason("Untertitel-Burn-in nicht angewendet.")
    actual_codec = sess.codec if video_mode == "transcode" else sess.source.get("codec", "")
    from .encoder import command_details
    observed_command = command_details(args) if args else None
    effective = {
        "command": observed_command,
        "mode": sess.mode, "profile": sess.profile, "video_mode": video_mode,
        "platform": sess.platform if video_mode == "transcode" else None,
        "codec": actual_codec, "encoder": sess.encoder,
        "audio_mode": sess.audio_play_mode(),
        "tonemap": {"active": bool(engine), "engine": engine, "algo": algo},
        "picture": picture,
        "preview_applied": bool(engine or any(v != "off" for v in picture.values())),
        "subtitle": {"index": sess.subtitle_index, "mode": sub_mode,
                     "codec": sess.source.get("subtitle_codec", "")},
        "binary": args[0] if args else None,
        "fallback": bool(reasons), "fallback_reasons": list(reasons),
    }
    bits = []
    if engine:
        bits.append(f"{engine.upper()} / {algo or 'Treiberkurve'}")
    bits.extend(f"{key} {value}" for key, value in picture.items() if value != "off")
    if reasons:
        bits.append("Fallback: " + " ".join(reasons))
    with sess.lock:
        sess.effective = effective
        sess.fallback_reasons = reasons
        sess.look_summary = " · ".join(bits)


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
    look: Optional[dict] = None,
    client_token: str = "",
    owner: str = "player",
    label: str = "",
) -> dict:
    """Session starten; owner/label sind optionale Runtime-Kontextmetadaten."""
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
    preview = _normalize_look(look)

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
        look=preview,
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
        client_token=_clean_token(client_token),
        owner=runtime.clean_owner(owner),
        label=str(label or target.name)[:200],
        starting=True,
        source={
            "path": rel,
            "codec": ff.normalize_video_codec(getattr(info, "codec", "") if info else ""),
            "hdr": bool(getattr(info, "is_hdr", False)) if info else False,
            "width": int(getattr(info, "width", 0) or 0) if info else 0,
            "height": int(getattr(info, "height", 0) or 0) if info else 0,
            "subtitle_codec": sub_codec,
        },
        requested={"profile": requested, "platform": platform, "codec": codec,
                   "subtitle": int(subtitle_index), "burn_subs": bool(burn_subs),
                   "look": copy.deepcopy(preview), "raw_look": copy.deepcopy(look),
                   "audio_index": int(audio_index), "audio_copy": want_audio_copy,
                   "height": int(height or 0), "v_bitrate": int(v_bitrate or 0),
                   "start_sec": start0, "lookahead_sec": la},
    )

    if _attach_session(sess):
        _finish_cancelled(sess)
        return {"error": "abgebrochen", "cancelled": True}

    source_vcodec = ff.normalize_video_codec(
        getattr(info, "codec", "") if info else ""
    )
    is_hdr = bool(getattr(info, "is_hdr", False)) if info else False

    if preview and str(preview.get("platform") or "") in ("auto", ""):
        preview = dict(preview)
        preview["platform"] = sess.platform or "cpu"

    if preview and sess.mode == "hls" and not use_video_copy:
        from .encoder import preview_look_summary
        sess.look_summary = preview_look_summary(preview, is_hdr=is_hdr)

    _plog(
        sid,
        "PLAN strategy=%s profile=%s summary=%s | %s",
        plan.get("strategy"), resolved, plan.get("summary"),
        " · ".join(plan.get("reasons") or []),
    )

    if sess.mode == "direct":
        _record_effective(sess)
        with sess.lock:
            sess.starting = False
        if sess.cancel or not _session_live(sess):
            _finish_cancelled(sess)
            return {"error": "abgebrochen", "cancelled": True}
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
            look=preview,
        )

    _record_effective(sess, cmd)
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

    if sess.cancel or not _session_live(sess):
        _finish_cancelled(sess)
        return {"error": "abgebrochen", "cancelled": True}

    err = _assign_proc(sess, cmd)
    if err == "abgebrochen" or sess.cancel or not _session_live(sess):
        _finish_cancelled(sess)
        return {"error": "abgebrochen", "cancelled": True}
    if err:
        _plog(sid, "FFmpeg-Start fehlgeschlagen: %s", err, level=logging.ERROR)
        _finish_cancelled(sess)
        return {"error": f"FFmpeg-Start fehlgeschlagen: {err}"}

    deadline = time.time() + (10.0 if need_encode else 6.0)
    while time.time() < deadline:
        if sess.cancel or not _session_live(sess):
            break
        playlist_size = _playlist_size(sess.playlist)
        if playlist_size > 0:
            _plog(sid, "Playlist bereit (%s bytes)", playlist_size)
            break
        proc = sess.proc
        if proc is not None and proc.poll() is not None:
            tail = _tail_ffmpeg_log(work)
            sess.error = (tail or "FFmpeg beendet")[-800:]
            _plog(
                sid,
                "FFmpeg exit=%s vor Playlist. Log:\n%s",
                proc.returncode, tail or "(leer)",
                level=logging.ERROR,
            )
            # Einmaliger Fallback CPU/H.264 bei echtem HW-Fehler.
            # Nicht, wenn Entladen den Prozess gerade beendet hat: sonst
            # bleibt ein unregistrierter CPU-Encode liegen.
            if (
                need_encode
                and sess.platform != "cpu"
                and not sess.playlist.exists()
                and _session_live(sess)
            ):
                _plog(sid, "HW-Fallback → CPU/libx264", level=logging.WARNING)
                _kill(sess)
                if sess.cancel or not _session_live(sess):
                    break
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
                # Nach einem HW-Fehler auch den GPU-Tonemap-Pfad verlassen.
                # Die Bildfilter bleiben erhalten, die SDR-Kurve ist CPU/Hable.
                if preview:
                    preview = dict(preview, engine="cpu", platform="cpu", algo="hable")
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
                    look=preview,
                )
                _record_effective(sess, cmd, fallback_reason="HW-Verarbeitung fehlgeschlagen → CPU/H.264, Tonemap CPU/Hable falls aktiv.")
                sess.debug_cmd = _cmd_preview(cmd, 800)
                err = _assign_proc(sess, cmd)
                if err == "abgebrochen" or sess.cancel or not _session_live(sess):
                    break
                if err:
                    sess.error = err
                    _plog(sid, "Fallback-Start fehlgeschlagen: %s", err, level=logging.ERROR)
                    break
                deadline = time.time() + 10.0
                continue
            break
        time.sleep(0.1)

    with sess.lock:
        sess.starting = False
    if sess.cancel or not _session_live(sess):
        _finish_cancelled(sess)
        return {"error": "abgebrochen", "cancelled": True}

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


def _clean_token(token: str) -> str:
    raw = (token or "").strip()
    if not raw or len(raw) > 64:
        return ""
    if not all(c.isalnum() or c in "-_" for c in raw):
        return ""
    return raw


def _signal_tree(pid: int, sig: int) -> bool:
    """Signal ausschließlich an die eigene FFmpeg-Prozessgruppe."""
    if not pid or sig is None:
        return False
    if os.name == "posix":
        try:
            os.killpg(pid, sig)
            return True
        except OSError:
            pass
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def _suspend_or_resume_tree(pid: int, suspend: bool) -> bool:
    """Pausiert oder weckt FFmpeg inkl. Unterprozessen (POSIX: SIGSTOP/SIGCONT, Windows: psutil)."""
    if not pid or pid <= 0:
        return False
    if os.name == "posix":
        sig = getattr(signal, "SIGSTOP" if suspend else "SIGCONT", None)
        return _signal_tree(pid, sig) if sig is not None else False
    try:
        import psutil
        parent = psutil.Process(pid)
        procs = [parent] + parent.children(recursive=True)
        for p in procs:
            try:
                if suspend:
                    p.suspend()
                else:
                    p.resume()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return True
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _proc_state(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = raw.rsplit(")", 1)
    if len(tail) < 2:
        return ""
    parts = tail[1].split()
    return parts[0] if parts else ""


def _release_token(sess: PlayerSession) -> None:
    with _LOCK:
        token = sess.client_token
        if token and _TOKENS.get(token) == sess.id:
            _TOKENS.pop(token, None)


def _drop_session(sess: PlayerSession) -> None:
    with _LOCK:
        if _SESSIONS.get(sess.id) is sess:
            _SESSIONS.pop(sess.id, None)
        _release_token(sess)


def _session_live(sess: PlayerSession) -> bool:
    if sess.cancel:
        return False
    with _LOCK:
        return _SESSIONS.get(sess.id) is sess


def _attach_session(sess: PlayerSession) -> bool:
    """Registriert die Session. True = der Client hat den Start schon abgebrochen."""
    token = sess.client_token
    with _LOCK:
        if token and token in _PENDING_CANCEL:
            _PENDING_CANCEL.discard(token)
            sess.cancel = True
        _SESSIONS[sess.id] = sess
        if token:
            _TOKENS[token] = sess.id
    return bool(sess.cancel)


def _assign_proc(sess: PlayerSession, cmd: list[str]) -> Optional[str]:
    """Startet FFmpeg. ``abgebrochen``, eine Fehlermeldung, oder None."""
    if sess.cancel or not _session_live(sess):
        return "abgebrochen"
    try:
        proc = _spawn_ffmpeg(cmd, sess.id, sess.work_dir)
    except OSError as e:
        return str(e)
    with sess.lock:
        sess.proc = proc
        sess.finished_at = None
    sess.metrics.sample(proc)
    if sess.cancel or not _session_live(sess):
        _kill(sess)
        return "abgebrochen"
    return None


def _remove_stopped_files(sess: PlayerSession) -> None:
    with sess.lock:
        removable = sess.cancel and not sess.starting and not runtime.process_running(sess.proc)
    if removable:
        shutil.rmtree(sess.work_dir, ignore_errors=True)


def _finish_cancelled(sess: PlayerSession) -> None:
    with sess.lock:
        sess.cancel = True
        sess.starting = False
    _release_token(sess)
    _kill(sess)
    _remove_stopped_files(sess)


def get_session(sid: str) -> Optional[PlayerSession]:
    with _LOCK:
        sess = _SESSIONS.get(sid)
    if sess:
        sess.touch()
    return sess


def cancel_client_token(token: str) -> bool:
    """Start abbrechen, auch solange ``start_session`` noch keine Antwort geliefert hat."""
    token = _clean_token(token)
    if not token:
        return False
    with _LOCK:
        sid = _TOKENS.get(token)
        if not sid:
            if len(_PENDING_CANCEL) > 200:
                _PENDING_CANCEL.clear()
            _PENDING_CANCEL.add(token)
            return True
        sess = _SESSIONS.get(sid)
        if sess:
            sess.cancel = True
    return stop_session(sid)


def stop_session(sid: str, *, owner: Optional[str] = None) -> bool:
    """Stop nur dieser Session; bis zum echten Prozessende bleibt sie sichtbar.

    Optionales owner schränkt den Stop zusätzlich auf den erwarteten Kontext ein.
    True bedeutet angenommen, nicht notwendigerweise bereits beendet (state).
    """
    with _LOCK:
        sess = _SESSIONS.get(sid)
    if not sess:
        return runtime.stop_session(sid, owner=owner)
    if owner is not None and sess.owner != owner:
        return False
    with sess.lock:
        sess.cancel = True
    _release_token(sess)
    _kill(sess)
    _remove_stopped_files(sess)
    _plog(sid, "Stop angefordert (Prozess läuft=%s)", runtime.process_running(sess.proc))
    return True


def status(sid: str) -> Optional[dict]:
    """Status ohne Idle-Touch; None für unbekannte Session (auch Stream-SIDs)."""
    with _LOCK:
        sess = _SESSIONS.get(sid)
    if sess:
        return sess.to_dict()
    return next((row for row in runtime.list_runtime_sessions() if row["id"] == sid), None)


def list_runtime_sessions(*, owner: Optional[str] = None,
                          include_finished: bool = True) -> list[dict]:
    """Leichte Snapshots ohne Log-Reads und ohne Idle-Lebenszeit zu verlängern."""
    with _LOCK:
        sessions = list(_SESSIONS.values())
    rows = []
    for sess in sessions:
        if owner is not None and sess.owner != owner:
            continue
        row = sess.to_dict(include_log=False)
        if include_finished or row["state"] != "finished":
            rows.append(row)
    rows.extend(runtime.list_runtime_sessions(owner=owner, include_finished=include_finished))
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)


def pause_encode(sid: str) -> dict:
    """FFmpeg per SIGSTOP anhalten (bei Pause im Player) – stoppt CPU/GPU-Last."""
    sess = get_session(sid)
    if not sess:
        return {"ok": False, "error": "Session nicht gefunden"}
    with sess.lock:
        if sess.mode != "hls":
            return {"ok": True, "encode_paused": False, "skipped": True}
        if sess.cancel or not runtime.process_running(sess.proc):
            return {"ok": True, "encode_paused": False, "running": False}
        if sess.encode_paused:
            return {"ok": True, "encode_paused": True}
        if not _suspend_or_resume_tree(int(sess.proc.pid), True):
            return {"ok": False, "encode_paused": False, "error": "Prozess konnte nicht pausiert werden"}
        sess.encode_paused = True
        return {"ok": True, "encode_paused": True}


def resume_encode(sid: str) -> dict:
    """FFmpeg nach Pause fortsetzen (SIGCONT / psutil.resume)."""
    sess = get_session(sid)
    if not sess:
        return {"ok": False, "error": "Session nicht gefunden"}
    with sess.lock:
        if sess.mode != "hls":
            return {"ok": True, "encode_paused": False, "skipped": True}
        if sess.cancel or not runtime.process_running(sess.proc):
            return {"ok": True, "encode_paused": False, "running": False}
        if not sess.encode_paused:
            return {"ok": True, "encode_paused": False}
        if not _suspend_or_resume_tree(int(sess.proc.pid), False):
            return {"ok": False, "encode_paused": True, "error": "Prozess konnte nicht fortgesetzt werden"}
        sess.encode_paused = False
        return {"ok": True, "encode_paused": False}


def _kill(sess: PlayerSession) -> None:
    """Beendet FFmpeg inklusive Helfer. Hängt nicht an Popen.poll() nach SIGSTOP."""
    with sess.lock:
        proc = sess.proc
        sess.encode_paused = False
    if proc is None or not runtime.process_running(proc):
        return
    pid = int(proc.pid or 0)
    if pid and runtime.process_running(proc):
        _suspend_or_resume_tree(pid, False)
        if hasattr(signal, "SIGKILL"):
            _signal_tree(pid, signal.SIGKILL)
        elif os.name == "nt":
            try:
                import psutil
                for ch in psutil.Process(pid).children(recursive=True):
                    try:
                        ch.kill()
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            proc.kill()
        except OSError:
            pass
    try:
        if proc.returncode is None:
            proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _plog(sess.id, "ffmpeg pid %s reagiert nicht auf SIGKILL", pid, level=logging.ERROR)
        if pid and hasattr(signal, "SIGKILL"):
            _signal_tree(pid, signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception as e:
        _plog(sess.id, "ffmpeg wait pid %s: %s", pid, e, level=logging.WARNING)
    state = _proc_state(pid) if pid else ""
    if runtime.process_running(proc):
        _plog(
            sess.id,
            "ffmpeg pid %s läuft nach Stop weiter (state=%s)",
            pid, state, level=logging.ERROR,
        )


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
            with _LOCK:
                sess = _SESSIONS.get(sid)
            if sess:
                with sess.lock:
                    finished = not sess.starting and not runtime.process_running(sess.proc)
                if finished:
                    _drop_session(sess)
                    n += 1
    return n + runtime.cleanup_idle(max_idle)


def cleanup_all() -> None:
    with _LOCK:
        ids = list(_SESSIONS.keys())
    for sid in ids:
        stop_session(sid)
    runtime.stop_all()
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
