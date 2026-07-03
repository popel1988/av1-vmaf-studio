"""Aufbau der FFmpeg-Encode-Kommandos inkl. Skalierung/Tonemapping sowie ein
Runner, der den Live-Fortschritt (FPS, Bitrate, ETA) über `-progress` ausliest.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
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

# Denoise-Stufen (hqdn3d: luma_spatial:chroma_spatial:luma_tmp:chroma_tmp)
_DENOISE = {
    "light": "hqdn3d=1:1:4:4",
    "medium": "hqdn3d=3:2:6:6",
    "strong": "hqdn3d=6:4:9:9",
}


def build_video_filters(
    info: VideoInfo,
    platform: str,
    target_height: Optional[int],
    tonemap: bool,
    *,
    nvidia_cuda_frames: bool = False,
    preserve_hdr: bool = False,
    denoise: str = "off",
) -> Optional[str]:
    """Baut die `-vf`-Kette: Tonemapping -> Downscale -> HW-Upload/Format.

    `nvidia_cuda_frames=True` => reine GPU-Pipeline (Frames bleiben als CUDA-
    Surfaces, Skalierung via scale_cuda). Sonst Software-Filterpfad.
    `preserve_hdr=True` => 10-bit-Surface (p010le) beim HW-Upload, damit HDR
    auf Intel/AMD erhalten bleibt (statt nv12/8-bit).
    """
    downscale = bool(target_height and info.height and target_height < info.height)
    keep_hdr = preserve_hdr and info.is_hdr

    # --- Reine NVIDIA-GPU-Pipeline (kein Tonemap) --------------------------
    if platform == "nvidia" and nvidia_cuda_frames:
        if downscale:
            return f"scale_cuda=-2:{target_height}"
        return None

    filters: list[str] = []

    if tonemap and info.is_hdr:
        filters.append(_TONEMAP_CHAIN)

    if denoise in _DENOISE:
        filters.append(_DENOISE[denoise])

    if downscale:
        # -2 hält das Seitenverhältnis (gerade Pixelzahl für die Encoder).
        filters.append(f"scale=-2:{target_height}:flags=lanczos")

    # Plattformspezifischer Upload/Pixelformat-Schritt.
    # AMD/Intel benötigen Frames auf einer HW-Surface.
    # Für HDR-Erhalt 10-bit (p010le), sonst nv12 (8-bit).
    hw_fmt = "p010le" if keep_hdr else "nv12"
    if platform == "amd" or (platform == "intel" and ff.intel_uses_vaapi()):
        filters.append(f"format={hw_fmt},hwupload")
    elif platform == "intel":
        filters.append(f"format={hw_fmt},hwupload=extra_hw_frames=64")

    if not filters:
        return None
    return ",".join(filters)


def _hdr_output_args(info: VideoInfo, codec: str, enc: str) -> list[str]:
    """Farb-/HDR-Metadaten für den Output, damit HDR10/HLG erhalten bleibt.

    Überträgt Primaries/Transfer/Matrix aus der Quelle und erzwingt 10-bit.
    Dolby Vision wird dabei nicht rekonstruiert (nur der HDR10-Basislayer).
    """
    prim = info.color_primaries or "bt2020"
    trc = info.color_transfer or "smpte2084"
    space = info.color_space or "bt2020nc"
    args = ["-colorspace", space, "-color_primaries", prim, "-color_trc", trc]

    if enc == "libx265":
        params = (f"colorprim={prim}:transfer={trc}:colormatrix={space}"
                  ":hdr10=1:repeat-headers=1")
        args += ["-pix_fmt", "yuv420p10le", "-x265-params", params]
    elif enc == "libsvtav1":
        args += ["-pix_fmt", "yuv420p10le"]
    elif enc == "libx264":
        args += ["-pix_fmt", "yuv420p10le"]
    elif "nvenc" in enc and codec == "hevc":
        args += ["-profile:v", "main10"]
    # AV1-NVENC/QSV/VAAPI führen 10-bit über das Surface-Format (p010) mit.
    return args


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
    audio_per_track: bool = False,
    audio_track_settings: Optional[list] = None,
    preserve_hdr: bool = False,
    keep_subtitles: bool = False,
    keep_chapters: bool = False,
    keep_metadata: bool = False,
    film_grain: int = 0,
    denoise: str = "off",
    two_pass: bool = False,
    pass_num: Optional[int] = None,
    passlog: Optional[str] = None,
) -> list[str]:
    """Erzeugt das vollständige FFmpeg-Kommando für einen Encode."""
    from . import config
    cmd: list[str] = [config.FFMPEG, "-y", "-hide_banner"]

    keep_hdr = bool(preserve_hdr and info.is_hdr)
    denoise_on = denoise in _DENOISE
    nvidia_cuda_frames = False

    # --- Hardware-Decode-/Device-Initialisierung (VOR dem Input) -----------
    if platform == "nvidia":
        if (tonemap and info.is_hdr) or denoise_on:
            # GPU-Decode, aber Download nach RAM für Software-Filter
            # (Tonemapping/Denoise laufen nicht als reine CUDA-Pipeline).
            cmd += ["-hwaccel", "cuda"]
        else:
            # Komplett auf der GPU: Decode -> (scale_cuda) -> NVENC.
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            nvidia_cuda_frames = True
    elif platform == "amd" or (platform == "intel" and ff.intel_uses_vaapi()):
        # VAAPI-Device als Upload-Ziel für den Software-Filterpfad (AMD sowie
        # Intel im VAAPI-Modus).
        cmd += ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}",
                "-filter_hw_device", "va"]
    elif platform == "intel":
        # QSV (oneVPL) wird unter Linux aus einem VAAPI-Device abgeleitet
        # (dokumentierter Weg: qsv=qs@va).
        cmd += ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}",
                "-init_hw_device", "qsv=qs@va",
                "-filter_hw_device", "qs"]

    if start_at is not None:
        cmd += ["-ss", str(start_at)]

    cmd += ["-i", str(info.path)]

    if duration_limit is not None:
        cmd += ["-t", str(duration_limit)]

    vf = build_video_filters(info, platform, target_height, tonemap,
                             nvidia_cuda_frames=nvidia_cuda_frames,
                             preserve_hdr=keep_hdr, denoise=denoise)
    if vf:
        cmd += ["-vf", vf]

    enc = ff.encoder_name(platform, codec)
    cmd += ["-c:v", enc]
    is_bitrate = rate_mode in ("bitrate", "abr") and bitrate_kbps
    if is_bitrate:
        cmd += ff.bitrate_args(platform, codec, bitrate_kbps, abr=(rate_mode == "abr"))
    else:
        cmd += ff.quality_args(platform, quality)

    if keep_hdr:
        cmd += _hdr_output_args(info, codec, enc)

    if enc == "libsvtav1":
        svt = "tune=0"
        if film_grain > 0:
            svt += f":film-grain={int(film_grain)}:film-grain-denoise=0"
        cmd += ["-preset", "6", "-svtav1-params", svt]
    elif enc.startswith("libx"):
        cmd += ["-preset", "medium"]
    elif "nvenc" in enc and not is_bitrate:
        cmd += ["-preset", "p5", "-rc", "vbr", "-tune", "hq"]
    elif "nvenc" in enc:
        cmd += ["-preset", "p5", "-tune", "hq"]
        if two_pass:
            cmd += ["-multipass", "fullres"]  # NVENC-eigenes 2-Pass (1 Durchlauf)
    elif "qsv" in enc:
        cmd += ["-preset", "slower"]

    # Echtes Zwei-Pass (zwei Durchläufe) nur für CPU-Encoder im Bitraten-Modus.
    if two_pass and pass_num in (1, 2) and passlog:
        cmd += ["-pass", str(pass_num), "-passlogfile", passlog]

    # Erster Durchlauf beim Zwei-Pass: nur Statistik erzeugen, kein Output.
    if pass_num == 1:
        cmd += ["-map", "0:v:0", "-an", "-sn", "-f", "null", os.devnull]
        return cmd

    cmd += ["-map", "0:v:0"]
    if audio_mode == "none":
        cmd += ["-an"]
    elif audio_per_track:
        # Jede Tonspur einzeln konfiguriert (Auswahl + Codec/Bitrate/… pro Spur).
        # Leere Liste => keine Tonspur behalten.
        cmd += ff.audio_track_args(audio_track_settings or [])
    else:
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

    # Untertitel/Kapitel/Metadaten aus der Quelle übernehmen (optional).
    if keep_subtitles:
        cmd += ["-map", "0:s?", "-c:s", "copy"]
    if not keep_chapters:
        cmd += ["-map_chapters", "-1"]
    if not keep_metadata:
        cmd += ["-map_metadata", "-1"]

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
        """Startet das Kommando, parst `-progress`. Gibt (returncode, stderr).

        stderr wird in einem eigenen Thread geleert, damit ein voller stderr-
        Puffer (z. B. gesprächiger QSV/VAAPI-Init) nicht mit dem stdout-Lesen
        deadlockt und der komplette Fehlertext für die Diagnose erhalten bleibt.
        """
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        prog = EncodeProgress()
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            if self.proc is None or self.proc.stderr is None:
                return
            for err_line in self.proc.stderr:
                stderr_lines.append(err_line.rstrip("\n"))

        err_thread = threading.Thread(target=_drain_stderr, daemon=True)
        err_thread.start()

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
        err_thread.join(timeout=5)
        stderr_tail = "\n".join(stderr_lines[-40:])
        return self.proc.returncode, stderr_tail

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
