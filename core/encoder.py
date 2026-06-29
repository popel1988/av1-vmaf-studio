"""Aufbau der FFmpeg-Encode-Kommandos inkl. Skalierung/Tonemapping sowie ein
Runner, der den Live-Fortschritt (FPS, Bitrate, ETA) über `-progress` ausliest.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import ffmpeg_utils as ff
from .ffmpeg_utils import VideoInfo

# Standard-Tonemapping-Kette HDR (PQ/HLG) -> SDR (BT.709), Software.
_TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,format=gbrpf32le,"
    "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


def build_video_filters(
    info: VideoInfo,
    platform: str,
    target_height: Optional[int],
    tonemap: bool,
) -> Optional[str]:
    """Baut die `-vf`-Kette: Tonemapping -> Downscale -> HW-Upload/Format.

    Es wird bewusst eine Software-Filterkette mit Hardware-*Encoder* genutzt –
    das ist herstellerübergreifend am robustesten (NVENC/QSV/VAAPI nehmen die
    gefilterten Frames entgegen).
    """
    filters: list[str] = []

    if tonemap and info.is_hdr:
        filters.append(_TONEMAP_CHAIN)

    if target_height and info.height and target_height < info.height:
        # -2 hält das Seitenverhältnis (gerade Pixelzahl für die Encoder).
        filters.append(f"scale=-2:{target_height}:flags=lanczos")

    # Plattformspezifischer Upload/Pixelformat-Schritt.
    # AMD (VAAPI) und Intel (QSV/VPL) benötigen Frames auf einer HW-Surface,
    # daher explizites format=nv12 + hwupload auf das initialisierte Device.
    if platform == "amd":
        filters.append("format=nv12,hwupload")
    elif platform == "intel":
        filters.append("format=nv12,hwupload=extra_hw_frames=64")
    elif platform == "nvidia" and not filters:
        # NVENC akzeptiert Software-Frames direkt; nichts nötig.
        pass

    if not filters:
        return None
    return ",".join(filters)


def build_encode_cmd(
    info: VideoInfo,
    output: Path,
    platform: str,
    codec: str,
    quality: int,
    target_height: Optional[int],
    tonemap: bool,
    *,
    duration_limit: Optional[float] = None,
    start_at: Optional[float] = None,
) -> list[str]:
    """Erzeugt das vollständige FFmpeg-Kommando für einen Encode."""
    from . import config
    cmd: list[str] = [config.FFMPEG, "-y", "-hide_banner"]

    # HW-Device-Initialisierung für den hwupload-Schritt der Filterkette.
    if platform == "amd":
        # VAAPI-Device als Upload-Ziel.
        cmd += ["-init_hw_device", "vaapi=va:/dev/dri/renderD128",
                "-filter_hw_device", "va"]
    elif platform == "intel":
        # QSV (oneVPL) wird unter Linux aus einem VAAPI-Device abgeleitet
        # (dokumentierter, robuster Weg: qsv=qs@va).
        cmd += ["-init_hw_device", "vaapi=va:/dev/dri/renderD128",
                "-init_hw_device", "qsv=qs@va",
                "-filter_hw_device", "qs"]

    if start_at is not None:
        cmd += ["-ss", str(start_at)]

    cmd += ["-i", str(info.path)]

    if duration_limit is not None:
        cmd += ["-t", str(duration_limit)]

    vf = build_video_filters(info, platform, target_height, tonemap)
    if vf:
        cmd += ["-vf", vf]

    cmd += ["-c:v", ff.encoder_name(platform, codec)]
    cmd += ff.quality_args(platform, quality)

    # Sinnvolle Defaults je Encoder-Familie
    enc = ff.encoder_name(platform, codec)
    if enc == "libsvtav1":
        cmd += ["-preset", "6", "-svtav1-params", "tune=0"]
    elif enc.startswith("libx"):
        cmd += ["-preset", "medium"]
    elif "nvenc" in enc:
        cmd += ["-preset", "p5", "-rc", "vbr", "-tune", "hq"]
    elif "qsv" in enc:
        cmd += ["-preset", "slower"]

    # Video + alle Audiospuren übernehmen (Audio platzsparend als AAC).
    # Untertitel werden bewusst nicht kopiert, um Container-Inkompatibilitäten
    # (z. B. Bild-Untertitel in MP4) und damit Encode-Abbrüche zu vermeiden.
    cmd += ["-map", "0:v:0", "-map", "0:a?", "-c:a", "aac", "-b:a", "160k"]

    cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    return cmd


@dataclass
class EncodeProgress:
    percent: float = 0.0
    fps: float = 0.0
    bitrate: str = "—"
    speed: str = "—"
    out_time: float = 0.0
    eta: float = 0.0
    current_size: int = 0


_SPEED_RE = re.compile(r"([\d.]+)x")


class EncodeRunner:
    """Führt ein FFmpeg-Encode aus und meldet den Fortschritt per Callback."""

    def __init__(self, on_progress: Optional[Callable[[EncodeProgress], None]] = None):
        self.on_progress = on_progress
        self.proc: Optional[subprocess.Popen] = None
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def run(self, cmd: list[str], duration: float) -> tuple[int, str]:
        """Startet das Kommando, parst `-progress`. Gibt (returncode, stderr)."""
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        prog = EncodeProgress()
        stderr_tail: list[str] = []

        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if self._cancel:
                break
            line = line.strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            self._apply(prog, key, val, duration)
            if key == "progress" and self.on_progress:
                self.on_progress(prog)

        self.proc.wait()
        # Stderr-Reste (für Fehlerdiagnose)
        if self.proc.stderr is not None:
            stderr_tail = self.proc.stderr.read().splitlines()[-20:]
        return self.proc.returncode, "\n".join(stderr_tail)

    @staticmethod
    def _apply(prog: EncodeProgress, key: str, val: str, duration: float) -> None:
        if key == "fps":
            prog.fps = _safe_float(val)
        elif key == "bitrate":
            prog.bitrate = val if val and val != "N/A" else "—"
        elif key == "total_size":
            prog.current_size = int(_safe_float(val))
        elif key == "out_time_us":
            prog.out_time = _safe_float(val) / 1_000_000.0
        elif key == "speed":
            prog.speed = val
            m = _SPEED_RE.search(val)
            spd = float(m.group(1)) if m else 0.0
            if duration > 0:
                prog.percent = min(100.0, round(prog.out_time / duration * 100, 1))
                remaining = max(0.0, duration - prog.out_time)
                prog.eta = remaining / spd if spd > 0 else 0.0


def _safe_float(val: str) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
