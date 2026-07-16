"""FFprobe-Abfragen und Mapping von Plattform/Codec auf FFmpeg-Flags."""
from __future__ import annotations

import functools
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("vcompress.ffprobe")

_ENCODER_LINE = re.compile(r"^\s*[VAS][.FSXBD]{5}\s+(\S+)")

# HDR-Transferfunktionen
HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "smptest2084"}


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    duration: float  # Sekunden
    codec: str
    pix_fmt: str
    color_transfer: str
    color_primaries: str
    color_space: str
    bit_rate: int  # bps (kann 0 sein)
    size_bytes: int
    # --- erweiterte Analyse (für die UI) ---
    profile: str = ""
    level: str = ""
    fps: float = 0.0
    bit_depth: int = 8
    hdr_type: str = "SDR"
    dolby_vision: bool = False
    dv_profile: int = 0
    overall_bitrate: int = 0
    container: str = ""
    audio: list = field(default_factory=list)
    subtitles: list = field(default_factory=list)

    @property
    def is_4k(self) -> bool:
        return self.height >= 1440 or self.width >= 2560

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.lower() in HDR_TRANSFERS or self.dolby_vision

    @property
    def video_bitrate(self) -> int:
        if self.bit_rate:
            return self.bit_rate
        # Schätzung: Gesamtbitrate aus Größe/Dauer, falls Stream-Bitrate fehlt
        if self.duration > 0 and self.size_bytes > 0:
            return int(self.size_bytes * 8 / self.duration)
        return 0

    def to_dict(self) -> dict:
        megapixels = round(self.width * self.height / 1_000_000, 1) if self.width else 0
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 2),
            "codec": self.codec,
            "pix_fmt": self.pix_fmt,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "color_space": self.color_space,
            "is_4k": self.is_4k,
            "is_hdr": self.is_hdr,
            "bit_rate": self.bit_rate,
            "size_bytes": self.size_bytes,
            "size_human": human_size(self.size_bytes),
            "resolution": f"{self.width}x{self.height}",
            "megapixels": megapixels,
            "duration_human": human_duration(self.duration),
            # erweiterte Felder
            "profile": self.profile,
            "level": self.level,
            "fps": round(self.fps, 3) if self.fps else 0,
            "bit_depth": self.bit_depth,
            "hdr_type": self.hdr_type,
            "dolby_vision": self.dolby_vision,
            "dv_profile": self.dv_profile,
            "overall_bitrate": self.overall_bitrate,
            "overall_bitrate_human": _bitrate_human(self.overall_bitrate),
            "video_bitrate": self.video_bitrate,
            "video_bitrate_human": _bitrate_human(self.video_bitrate),
            "container": self.container,
            "audio": self.audio,
            "subtitles": self.subtitles,
        }


def probe_with_error(path: Path) -> tuple[Optional[VideoInfo], Optional[str]]:
    """Vollständige Stream-Analyse via ffprobe. Liefert (VideoInfo, Fehler)."""
    from . import config
    cmd = [
        config.FFPROBE, "-v", "error",
        "-show_streams", "-show_format",
        "-of", "json",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        return None, "ffprobe nicht gefunden (PATH prüfen – /usr/local/bin)."
    except subprocess.TimeoutExpired:
        return None, "ffprobe-Timeout (Datei evtl. sehr groß oder Mount langsam)."
    except OSError as e:
        return None, f"ffprobe-Aufruf fehlgeschlagen: {e}"

    if out.returncode != 0:
        err = (out.stderr or "").strip() or f"Exit-Code {out.returncode}"
        return None, err
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe-Ausgabe nicht lesbar (kein gültiges JSON)."

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None, "Kein Video-Stream gefunden."

    duration = _f(video.get("duration")) or _f(fmt.get("duration")) or 0.0
    bit_rate = int(_f(video.get("bit_rate")) or 0)
    overall_bitrate = int(_f(fmt.get("bit_rate")) or 0)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = int(_f(fmt.get("size")) or 0)

    pix_fmt = video.get("pix_fmt", "?") or "?"
    transfer = (video.get("color_transfer", "") or "").lower()
    hdr_type, dovi, dv_profile = _detect_hdr(transfer, video)

    # Audio-/Untertitel-Spuren strukturiert sammeln
    audio = [_audio_entry(s, i) for i, s in
             enumerate(s for s in streams if s.get("codec_type") == "audio")]
    subs = [_subtitle_entry(s, i) for i, s in
            enumerate(s for s in streams if s.get("codec_type") == "subtitle")]

    info = VideoInfo(
        path=str(path),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        duration=duration,
        codec=video.get("codec_name", "?"),
        pix_fmt=pix_fmt,
        color_transfer=video.get("color_transfer", "") or "",
        color_primaries=video.get("color_primaries", "") or "",
        color_space=video.get("color_space", "") or "",
        bit_rate=bit_rate,
        size_bytes=size_bytes,
        profile=str(video.get("profile", "") or ""),
        level=str(video.get("level", "") or ""),
        fps=_parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        bit_depth=_bit_depth(pix_fmt, video),
        hdr_type=hdr_type,
        dolby_vision=dovi,
        dv_profile=dv_profile,
        overall_bitrate=overall_bitrate,
        container=fmt.get("format_name", "") or "",
        audio=audio,
        subtitles=subs,
    )
    return info, None


def _detect_hdr(transfer: str, video: dict) -> tuple[str, bool, int]:
    """Bestimmt HDR-Typ, ob Dolby Vision vorliegt und ggf. das DV-Profil."""
    dovi = False
    dv_profile = 0
    for sd in video.get("side_data_list", []) or []:
        t = str(sd.get("side_data_type", "")).lower()
        if "dolby vision" in t or "dovi" in t:
            dovi = True
            # ffprobe liefert bei DV-Konfig-Records das Feld "dv_profile".
            raw = sd.get("dv_profile")
            if raw is not None:
                try:
                    dv_profile = int(raw)
                except (TypeError, ValueError):
                    pass
    if transfer in ("smpte2084", "smptest2084"):
        base = "HDR10 (PQ)"
    elif transfer == "arib-std-b67":
        base = "HLG"
    else:
        base = "SDR"
    if dovi:
        label = f"Dolby Vision {dv_profile}" if dv_profile else "Dolby Vision"
        return (f"{label} + {base}" if base != "SDR" else label), True, dv_profile
    return base, False, 0


def _bit_depth(pix_fmt: str, video: dict) -> int:
    raw = video.get("bits_per_raw_sample")
    if raw and str(raw).isdigit():
        return int(raw)
    if "12" in pix_fmt:
        return 12
    if "10" in pix_fmt:
        return 10
    return 8


def _audio_entry(s: dict, index: int = 0) -> dict:
    tags = s.get("tags", {}) or {}
    br = int(_f(s.get("bit_rate")) or 0)
    return {
        "index": index,  # relativer Audio-Index (0:a:index)
        "codec": s.get("codec_name", "?"),
        "channels": s.get("channels", 0),
        "layout": s.get("channel_layout", "") or "",
        "sample_rate": s.get("sample_rate", "") or "",
        "bitrate": br,
        "bitrate_human": _bitrate_human(br) if br else "—",
        "language": tags.get("language", "") or tags.get("LANGUAGE", "") or "und",
        "title": tags.get("title", "") or tags.get("TITLE", "") or "",
    }


def _subtitle_entry(s: dict, index: int = 0) -> dict:
    tags = s.get("tags", {}) or {}
    disp = s.get("disposition", {}) or {}
    return {
        "index": index,  # relativer Untertitel-Index (0:s:index)
        "codec": s.get("codec_name", "?"),
        "language": tags.get("language", "") or tags.get("LANGUAGE", "") or "und",
        "title": tags.get("title", "") or tags.get("TITLE", "") or "",
        "forced": bool(disp.get("forced")),
        "default": bool(disp.get("default")),
    }


def _parse_fraction(value: Optional[str]) -> float:
    if not value or "/" not in str(value):
        return _f(value) or 0.0
    num, _, den = str(value).partition("/")
    n, d = _f(num), _f(den)
    if not n or not d:
        return 0.0
    return n / d


def _bitrate_human(bps: float) -> str:
    if not bps:
        return "—"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbit/s"
    return f"{bps / 1000:.0f} kbit/s"


def ffprobe(path: Path) -> Optional[VideoInfo]:
    """Liest Auflösung, Dauer, Codec und Farbinformationen via ffprobe aus."""
    info, err = probe_with_error(path)
    if err:
        logger.warning("ffprobe fehlgeschlagen für %s: %s", path, err)
    return info


# ----------------------------------------------------------------- Encoder-Map

# Encoder-Name pro Plattform/Codec. Intel gibt es in zwei Ausprägungen:
# QSV (oneVPL) und VAAPI – beide laufen mit dem Image (Ubuntu 24.04, libva 2.20).
ENCODERS = {
    "nvidia": {"av1": "av1_nvenc", "hevc": "hevc_nvenc", "h264": "h264_nvenc"},
    "intel": {"av1": "av1_qsv", "hevc": "hevc_qsv", "h264": "h264_qsv"},
    "intel_vaapi": {"av1": "av1_vaapi", "hevc": "hevc_vaapi", "h264": "h264_vaapi"},
    "amd": {"av1": "av1_vaapi", "hevc": "hevc_vaapi", "h264": "h264_vaapi"},
    "cpu": {"av1": "libsvtav1", "hevc": "libx265", "h264": "libx264"},
}


def intel_uses_vaapi() -> bool:
    """True, wenn die Intel-Plattform über VAAPI (statt QSV) encodieren soll."""
    from . import config
    return getattr(config, "INTEL_ENCODER", "vaapi") != "qsv"


def _encoder_map(platform: str) -> dict:
    if platform == "intel" and intel_uses_vaapi():
        return ENCODERS["intel_vaapi"]
    return ENCODERS.get(platform, ENCODERS["cpu"])


def encoder_name(platform: str, codec: str) -> str:
    return _encoder_map(platform).get(codec, "libsvtav1")


def encoder_backend(platform: str) -> str:
    """Konkretes HW-Backend: 'nvenc' | 'qsv' | 'vaapi' | 'cpu'."""
    if platform == "nvidia":
        return "nvenc"
    if platform == "amd":
        return "vaapi"
    if platform == "intel":
        return "vaapi" if intel_uses_vaapi() else "qsv"
    return "cpu"


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    """Erste Zeile von `ffmpeg -version` (z. B. 'ffmpeg version n8.1 …')."""
    from . import config
    try:
        out = subprocess.run(
            [config.FFMPEG, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        first = (out.stdout or "").splitlines()
        return first[0].strip() if first else "unbekannt"
    except (OSError, subprocess.SubprocessError):
        return "unbekannt"


@functools.lru_cache(maxsize=1)
def available_encoders() -> frozenset:
    """Liest die im FFmpeg-Build kompilierten Encoder (`ffmpeg -encoders`)."""
    from . import config
    try:
        out = subprocess.run(
            [config.FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in out.stdout.splitlines():
        m = _ENCODER_LINE.match(line)
        if m:
            names.add(m.group(1))
    return frozenset(names)


def encoder_available(platform: str, codec: str) -> bool:
    enc = encoder_name(platform, codec)
    avail = available_encoders()
    # Wenn die Liste leer ist (ffmpeg nicht abfragbar), nicht fälschlich blocken.
    return not avail or enc in avail


def quality_args(platform: str, value: int) -> list[str]:
    """Liefert die herstellerspezifischen Qualitäts-Argumente.

    - NVENC: -cq
    - QSV:   -global_quality
    - VAAPI (AMD und Intel-VAAPI): -rc_mode CQP -qp
    - CPU:   -crf
    """
    backend = encoder_backend(platform)
    if backend == "nvenc":
        return ["-cq", str(value)]
    if backend == "qsv":
        return ["-global_quality", str(value)]
    if backend == "vaapi":
        return ["-rc_mode", "CQP", "-qp", str(value)]
    return ["-crf", str(value)]


def bitrate_args(platform: str, codec: str, kbps: int, abr: bool = False) -> list[str]:
    """Festbitrate (CBR) oder Average-Bitrate (VBR-Ziel) in kbit/s."""
    br = f"{kbps}k"
    enc = encoder_name(platform, codec)
    if abr:
        args = ["-b:v", br, "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
        if "nvenc" in enc:
            args += ["-rc", "vbr"]
        return args
    args = ["-b:v", br]
    if "nvenc" in enc:
        args += ["-maxrate", br, "-bufsize", f"{kbps * 2}k", "-rc", "cbr"]
    return args


# Ziel-Audiocodec -> FFmpeg-Encoder
AUDIO_ENCODERS = {
    "aac": "aac",
    "opus": "libopus",
    "ac3": "ac3",
    "eac3": "eac3",
    "flac": "flac",
}


def audio_args(
    mode: str = "copy",
    codec: str = "aac",
    bitrate_kbps: int = 160,
    channels: int = 0,
    normalize: bool = False,
) -> list[str]:
    """Baut die Audio-Argumente.

    mode:      "copy" (Stream 1:1 übernehmen) | "encode" (neu codieren) | "none"
    codec:     Zielcodec bei mode="encode" (aac/opus/ac3/eac3/flac)
    bitrate:   kbit/s pro Stream (bei verlustbehafteten Codecs)
    channels:  0 = Original behalten, 1 = Mono, 2 = Stereo (Downmix)
    normalize: EBU-R128-Lautheitsnormalisierung (loudnorm)
    """
    if mode == "none":
        return ["-an"]
    # Direktes Kopieren nur möglich, wenn keine Umwandlung nötig ist.
    if mode == "copy" and channels == 0 and not normalize:
        return ["-c:a", "copy"]

    enc = AUDIO_ENCODERS.get(codec, "aac")
    args = ["-c:a", enc]
    if enc != "flac":
        args += ["-b:a", f"{max(32, int(bitrate_kbps))}k"]
    if channels in (1, 2):
        args += ["-ac", str(channels)]
    if normalize:
        # Zielwerte nach Streaming-Standard (EBU R128).
        args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    return args


def audio_track_args(tracks: list) -> list[str]:
    """Mapping + Codec-Argumente pro einzelner Ausgabe-Tonspur.

    `tracks`: geordnete Liste ausgewählter Spuren, je Eintrag ein Dict mit
    index (Quell-Audio-Index), mode ("copy"/"encode"), codec, bitrate,
    channels, normalize. Die Ausgabe-Spur-Reihenfolge bestimmt die
    Stream-Specifier (a:0, a:1, …).
    """
    if not tracks:
        return ["-an"]
    args: list[str] = []
    for t in tracks:
        args += ["-map", f"0:a:{int(t.get('index', 0))}?"]
    for out_idx, t in enumerate(tracks):
        mode = t.get("mode", "copy")
        ch = int(t.get("channels", 0) or 0)
        norm = bool(t.get("normalize"))
        if mode != "encode" and ch == 0 and not norm:
            args += [f"-c:a:{out_idx}", "copy"]
            continue
        enc = AUDIO_ENCODERS.get(t.get("codec", "aac"), "aac")
        args += [f"-c:a:{out_idx}", enc]
        if enc != "flac":
            args += [f"-b:a:{out_idx}", f"{max(32, int(t.get('bitrate', 160) or 160))}k"]
        if ch in (1, 2):
            args += [f"-ac:a:{out_idx}", str(ch)]
        if norm:
            args += [f"-filter:a:{out_idx}", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    return args


def subtitle_track_args(tracks: list) -> list[str]:
    """Mapping + Disposition (Default/Forced) pro gewählter Untertitelspur.

    `tracks`: geordnete Liste ausgewählter Untertitel, je Eintrag ein Dict mit
    index (Quell-Untertitel-Index) und optional default/forced (bool). Alle
    Spuren werden verlustfrei kopiert (`-c:s copy`). Leere Liste => keine
    Untertitel im Output.
    """
    if not tracks:
        return []
    args: list[str] = []
    for t in tracks:
        args += ["-map", f"0:s:{int(t.get('index', 0))}?"]
    args += ["-c:s", "copy"]
    for out_idx, t in enumerate(tracks):
        flags = []
        if t.get("default"):
            flags.append("default")
        if t.get("forced"):
            flags.append("forced")
        # "0" setzt alle Dispositions-Flags zurück (kein Default/Forced).
        args += [f"-disposition:s:{out_idx}", "+".join(flags) if flags else "0"]
    return args


def hwaccel_input_args(platform: str) -> list[str]:
    """Hardware-Decode-/Init-Argumente, die VOR dem -i Input stehen."""
    from . import config
    backend = encoder_backend(platform)
    if platform == "nvidia":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if backend == "qsv":
        return ["-hwaccel", "qsv", "-qsv_device", config.VAAPI_DEVICE]
    if backend == "vaapi":
        return ["-hwaccel", "vaapi", "-vaapi_device", config.VAAPI_DEVICE]
    return []


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _f(value) -> Optional[float]:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
