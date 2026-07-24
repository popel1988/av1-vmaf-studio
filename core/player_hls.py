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

logger = logging.getLogger("vcompress.player_hls")

_SESSIONS: dict[str, "PlayerSession"] = {}
_LOCK = threading.RLock()
_ROOT = config.DATA_DIR / "player_sessions"
_MAX_IDLE_SEC = 3600
# Wie weit FFmpeg vor der Abspielposition encoden darf (0 = unbegrenzt).
_DEFAULT_LOOKAHEAD_SEC = 30.0
_LOOKAHEAD_CHOICES = (0, 15, 30, 60, 120)

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
        }

    def playback_label(self) -> str:
        """Kurzer Text für die UI: tatsächlich laufender Modus (nicht nur Auswahl)."""
        if self.mode == "direct":
            return "Direct-Play"
        if self.encoder in ("", "copy", "direct") and self.profile == "copy":
            return "HLS · Original-Video + Ton→AAC"
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
                reset_pts: bool = True) -> list[str]:
    """Ton-Args.

    ``reset_pts`` nur zusammen mit Video-Re-Encode (``setpts``) – sonst läuft
    der Ton bei Video-Copy (Auto→HLS) asynchron vor/nach dem Bild.
    """
    if audio_index < 0:
        return ["-an"]
    args = ["-map", f"0:a:{int(audio_index)}?"]
    ac = (audio_codec or "").lower()
    if force_copy or ac.startswith(("aac", "mp4a", "mp3")):
        # Copy: keine PTS-Filter möglich ohne Decode – Mux-Flags gleichen grob aus.
        args += ["-c:a", "copy"]
    elif reset_pts:
        # Stereo-AAC + gemeinsame Null-Timeline mit Video (setpts/asetpts).
        args += [
            "-c:a", "aac", "-ac", "2", "-b:a", "192k", "-ar", "48000",
            "-profile:a", "aac_low",
            "-af", "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
        ]
    else:
        # Video bleibt Copy → Original-PTS behalten, nur leicht syncen.
        args += [
            "-c:a", "aac", "-ac", "2", "-b:a", "192k", "-ar", "48000",
            "-profile:a", "aac_low",
            "-af", "aresample=async=1",
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


def _build_video_filter(
    *,
    height: int,
    burn_sub_index: int,
    platform: str,
    encoder: str,
) -> tuple[list[str], list[str]]:
    """Filter + ggf. Extra-Args vor -i. Rückgabe (pre_input, map/filter args).

    ``setpts=PTS-STARTPTS`` setzt die Video-Timeline nach Seek auf 0 – analog
    zu ``asetpts`` beim Ton. Seek bleibt vor ``-i`` (schnell); kein langsames
    Decode-from-start.
    """
    pre: list[str] = []
    backend = ff.encoder_backend(platform) if platform != "cpu" else "cpu"
    need_hwupload = "vaapi" in encoder or "qsv" in encoder

    scale = f"scale=-2:{int(height)}" if height and height > 0 else ""
    # setpts immer vor hwupload (nur Systemspeicher-Frames)
    pts = "setpts=PTS-STARTPTS"

    if burn_sub_index >= 0:
        # Overlay immer auf CPU, danach gemeinsame Timeline, ggf. hwupload
        if scale:
            fc = (f"[0:v:0]{scale}[vs];"
                  f"[vs][0:s:{int(burn_sub_index)}]overlay=format=auto,{pts}")
        else:
            fc = f"[0:v:0][0:s:{int(burn_sub_index)}]overlay=format=auto,{pts}"
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
        parts.append(pts)
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

    # NVENC / CPU: scale (optional) + einheitliche Timeline
    parts = []
    if scale:
        parts.append(scale)
    parts.append(pts)
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
) -> list[str]:
    """HLS-Command. Läuft durchgehend; Vorlauf drosselt der Client per Pause.

    Timestamps: bei Video-Re-Encode ``setpts`` + bei Ton-Re-Encode ``asetpts``
    (beide auf 0). Seek bleibt vor ``-i`` (schnell). Video-Copy kann PTS nicht
    filtern – dort nur Mux-Normalisierung.
    """
    playlist = out_dir / "index.m3u8"
    cmd = [
        config.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-fflags", "+genpts",
    ]

    pre: list[str] = []
    vmap: list[str] = []
    if not video_copy:
        # HW-Decode nur wenn Capabilities den Quellcodec freigeben.
        # Ohne -hwaccel_output_format*: Filter (setpts/scale) bleiben auf CPU,
        # danach hwupload zum jeweiligen Encoder.
        cmd += _hwaccel_decode_args(platform, source_codec)
        pre, vmap = _build_video_filter(
            height=height, burn_sub_index=burn_sub_index,
            platform=platform, encoder=encoder,
        )
        cmd += pre

    # Seek vor -i: schnell (Keyframe). A/V danach über setpts/asetpts bzw.
    # start_at_zero auf eine gemeinsame Ausgabe-Timeline.
    if start_sec and start_sec > 0:
        cmd += ["-ss", f"{float(start_sec):.3f}"]
    cmd += ["-i", str(path)]

    if video_copy:
        cmd += ["-map", "0:v:0?", "-c:v", "copy"]
    else:
        cmd += vmap
        cmd += ["-c:v", encoder]
        cmd += _encoder_rate_args(encoder, codec, v_bitrate)

    cmd += _audio_args(
        audio_index, audio_codec,
        force_copy=bool(audio_copy),
        reset_pts=not bool(video_copy),
    )

    la = _normalize_lookahead(lookahead_sec)
    hls_time = 2
    if la > 0:
        list_size = max(6, int(math.ceil(la / hls_time)) + 4)
    else:
        list_size = 30
    hls_flags = "independent_segments+omit_endlist+delete_segments+temp_file"

    cmd += [
        "-sn", "-dn",
        "-avoid_negative_ts", "make_zero",
        "-start_at_zero",
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-max_interleave_delta", "0",
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
    # Ohne Ton-Transcode zählt inkompatibler Ton nicht als HLS-Zwang für auto
    force_hls = bool(start_sec and start_sec > 0) or (
        (not want_audio_copy) and audio_index >= 0 and audio_codec
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

    la = _normalize_lookahead(lookahead_sec)
    start0 = max(0.0, float(start_sec or 0))

    warn_parts = list(enc_info.get("warnings") or [])
    if want_audio_copy and audio_codec and not (audio_codec or "").lower().startswith(
            ("aac", "mp4a", "mp3")):
        warn_parts.append(
            f"Ton-Copy ({audio_codec}): Browser kann die Spur ggf. nicht abspielen."
        )
    warn = "; ".join(warn_parts)
    sess = PlayerSession(
        id=sid,
        path=target,
        rel=rel,
        audio_index=int(audio_index),
        subtitle_index=int(subtitle_index),
        start_sec=start0,
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
        lookahead_sec=la,
        window_end=0.0,
        audio_copy=want_audio_copy,
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

    source_vcodec = ff.normalize_video_codec(
        getattr(info, "codec", "") if info else ""
    )

    if resolved == "copy" and not want_burn:
        cmd = _build_hls_cmd(
            target, work,
            audio_index=sess.audio_index,
            start_sec=sess.start_sec,
            audio_codec=audio_codec,
            platform="cpu", encoder="copy", codec="h264",
            height=0, v_bitrate=0, burn_sub_index=-1, video_copy=True,
            lookahead_sec=la,
            audio_copy=want_audio_copy,
            source_codec=source_vcodec,
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
            lookahead_sec=la,
            audio_copy=want_audio_copy,
            source_codec=source_vcodec,
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
                    lookahead_sec=la,
                    audio_copy=want_audio_copy,
                    source_codec=source_vcodec,
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
