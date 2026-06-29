"""FFprobe-Abfragen und Mapping von Plattform/Codec auf FFmpeg-Flags."""
from __future__ import annotations

import functools
import json
import logging
import re
import subprocess
from dataclasses import dataclass
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

    @property
    def is_4k(self) -> bool:
        return self.height >= 1440 or self.width >= 2560

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.lower() in HDR_TRANSFERS

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 2),
            "codec": self.codec,
            "pix_fmt": self.pix_fmt,
            "color_transfer": self.color_transfer,
            "is_4k": self.is_4k,
            "is_hdr": self.is_hdr,
            "bit_rate": self.bit_rate,
            "size_bytes": self.size_bytes,
            "size_human": human_size(self.size_bytes),
            "resolution": f"{self.width}x{self.height}",
            "duration_human": human_duration(self.duration),
        }


def probe_with_error(path: Path) -> tuple[Optional[VideoInfo], Optional[str]]:
    """Wie ffprobe(), liefert aber zusätzlich eine Diagnose-Meldung zurück
    (z. B. ffprobe-stderr), damit Fehlerursachen sichtbar werden."""
    from . import config
    cmd = [
        config.FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,codec_name,pix_fmt,color_transfer,"
        "color_primaries,color_space,bit_rate,duration:format=duration,bit_rate",
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
    if not streams:
        return None, "Kein Video-Stream gefunden."
    s = streams[0]
    fmt = data.get("format", {})

    duration = _f(s.get("duration")) or _f(fmt.get("duration")) or 0.0
    bit_rate = int(_f(s.get("bit_rate")) or _f(fmt.get("bit_rate")) or 0)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0

    return VideoInfo(
        path=str(path),
        width=int(s.get("width") or 0),
        height=int(s.get("height") or 0),
        duration=duration,
        codec=s.get("codec_name", "?"),
        pix_fmt=s.get("pix_fmt", "?"),
        color_transfer=s.get("color_transfer", "") or "",
        color_primaries=s.get("color_primaries", "") or "",
        color_space=s.get("color_space", "") or "",
        bit_rate=bit_rate,
        size_bytes=size_bytes,
    ), None


def ffprobe(path: Path) -> Optional[VideoInfo]:
    """Liest Auflösung, Dauer, Codec und Farbinformationen via ffprobe aus."""
    info, err = probe_with_error(path)
    if err:
        logger.warning("ffprobe fehlgeschlagen für %s: %s", path, err)
    return info


# ----------------------------------------------------------------- Encoder-Map

# Encoder-Name pro Plattform/Codec
ENCODERS = {
    "nvidia": {"av1": "av1_nvenc", "hevc": "hevc_nvenc", "h264": "h264_nvenc"},
    "intel": {"av1": "av1_qsv", "hevc": "hevc_qsv", "h264": "h264_qsv"},
    "amd": {"av1": "av1_vaapi", "hevc": "hevc_vaapi", "h264": "h264_vaapi"},
    "cpu": {"av1": "libsvtav1", "hevc": "libx265", "h264": "libx264"},
}

# Qualitäts-Flag pro Plattform (herstellerspezifisch)
QUALITY_FLAG = {
    "nvidia": "-cq",
    "intel": "-global_quality",
    "amd": "-qp",
    "cpu": "-crf",
}


def encoder_name(platform: str, codec: str) -> str:
    return ENCODERS.get(platform, ENCODERS["cpu"]).get(codec, "libsvtav1")


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
    """Liefert die herstellerspezifischen Qualitäts-Argumente."""
    flag = QUALITY_FLAG.get(platform, "-crf")
    return [flag, str(value)]


def hwaccel_input_args(platform: str) -> list[str]:
    """Hardware-Decode-/Init-Argumente, die VOR dem -i Input stehen."""
    if platform == "nvidia":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if platform == "intel":
        return ["-hwaccel", "qsv", "-qsv_device", "/dev/dri/renderD128"]
    if platform == "amd":
        return ["-hwaccel", "vaapi", "-vaapi_device", "/dev/dri/renderD128"]
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
