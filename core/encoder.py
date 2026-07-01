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
    *,
    nvidia_cuda_frames: bool = False,
) -> Optional[str]:
    """Baut die `-vf`-Kette: Tonemapping -> Downscale -> HW-Upload/Format.

    `nvidia_cuda_frames=True` => reine GPU-Pipeline (Frames bleiben als CUDA-
    Surfaces, Skalierung via scale_cuda). Sonst Software-Filterpfad.
    """
    downscale = bool(target_height and info.height and target_height < info.height)

    # --- Reine NVIDIA-GPU-Pipeline (kein Tonemap) --------------------------
    if platform == "nvidia" and nvidia_cuda_frames:
        if downscale:
            return f"scale_cuda=-2:{target_height}"
        return None

    filters: list[str] = []

    if tonemap and info.is_hdr:
        filters.append(_TONEMAP_CHAIN)

    if downscale:
        # -2 hält das Seitenverhältnis (gerade Pixelzahl für die Encoder).
        filters.append(f"scale=-2:{target_height}:flags=lanczos")

    # Plattformspezifischer Upload/Pixelformat-Schritt.
    # AMD (VAAPI) und Intel (QSV/VPL) benötigen Frames auf einer HW-Surface,
    # daher explizites format=nv12 + hwupload auf das initialisierte Device.
    if platform == "amd":
        filters.append("format=nv12,hwupload")
    elif platform == "intel":
        filters.append("format=nv12,hwupload=extra_hw_frames=64")

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
    rate_mode: str = "cq",
    bitrate_kbps: Optional[int] = None,
    include_progress: bool = True,
    audio_mode: str = "copy",
    audio_codec: str = "aac",
    audio_bitrate_kbps: int = 160,
    audio_channels: int = 0,
    audio_normalize: bool = False,
    audio_tracks: Optional[list] = None,
) -> list[str]:
    """Erzeugt das vollständige FFmpeg-Kommando für einen Encode."""
    from . import config
    cmd: list[str] = [config.FFMPEG, "-y", "-hide_banner"]

    nvidia_cuda_frames = False

    # --- Hardware-Decode-/Device-Initialisierung (VOR dem Input) -----------
    if platform == "nvidia":
        if tonemap and info.is_hdr:
            # GPU-Decode, aber Download nach RAM fürs Software-Tonemapping.
            cmd += ["-hwaccel", "cuda"]
        else:
            # Komplett auf der GPU: Decode -> (scale_cuda) -> NVENC.
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            nvidia_cuda_frames = True
    elif platform == "amd":
        # VAAPI-Device als Upload-Ziel für den Software-Filterpfad.
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

    vf = build_video_filters(info, platform, target_height, tonemap,
                             nvidia_cuda_frames=nvidia_cuda_frames)
    if vf:
        cmd += ["-vf", vf]

    enc = ff.encoder_name(platform, codec)
    cmd += ["-c:v", enc]
    if rate_mode in ("bitrate", "abr") and bitrate_kbps:
        cmd += ff.bitrate_args(platform, codec, bitrate_kbps, abr=(rate_mode == "abr"))
    else:
        cmd += ff.quality_args(platform, quality)

    if enc == "libsvtav1":
        cmd += ["-preset", "6", "-svtav1-params", "tune=0"]
    elif enc.startswith("libx"):
        cmd += ["-preset", "medium"]
    elif "nvenc" in enc and rate_mode not in ("bitrate", "abr"):
        cmd += ["-preset", "p5", "-rc", "vbr", "-tune", "hq"]
    elif "nvenc" in enc:
        cmd += ["-preset", "p5", "-tune", "hq"]
    elif "qsv" in enc:
        cmd += ["-preset", "slower"]

    cmd += ["-map", "0:v:0"]
    if audio_mode != "none":
        if audio_tracks:
            # Nur ausgewählte Tonspuren übernehmen ("?" = fehlende ignorieren,
            # wichtig für Batch-Dateien mit abweichender Spurzahl).
            for idx in audio_tracks:
                cmd += ["-map", f"0:a:{int(idx)}?"]
        else:
            cmd += ["-map", "0:a?"]
    cmd += ff.audio_args(
        audio_mode, audio_codec, audio_bitrate_kbps, audio_channels, audio_normalize,
    )

    if include_progress:
        cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    else:
        cmd += [str(output)]
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
