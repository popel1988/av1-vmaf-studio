"""Aufbau der FFmpeg-Encode-Kommandos inkl. Skalierung/Tonemapping sowie ein
Runner, der den Live-Fortschritt (FPS, Bitrate, ETA) über `-progress` ausliest.
"""
from __future__ import annotations

import logging
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

# Kurven je Engine:
# - CPU-Filter ``tonemap`` (vf_tonemap) kennt kein ``bt2390``.
# - ``tonemap_vaapi`` nutzt die Kurve des VAAPI-Treibers (keine ``tonemap``-Option).
# - ``libplacebo``, ``tonemap_cuda``, ``tonemap_opencl`` und ``tonemapx`` kennen alle 7.
_TONEMAP_ALGOS = ("hable", "bt2390", "reinhard", "mobius", "gamma", "clip", "linear")
_CPU_TONEMAP_ALGOS = ("hable", "reinhard", "mobius", "gamma", "clip", "linear")
TONEMAP_ALGO_LABELS = {
    "hable": "Hable",
    "bt2390": "BT.2390",
    "reinhard": "Reinhard",
    "mobius": "Mobius",
    "gamma": "Gamma",
    "clip": "Clip",
    "linear": "Linear",
}


def normalize_tonemap_algo(value: str) -> str:
    v = str(value or "hable").strip().lower()
    return v if v in _TONEMAP_ALGOS else "hable"


def tonemap_algo_for_engine(algo: str, engine: str) -> str:
    """Aufzulösende Kurve passend zur Engine (CPU-Filter kennt kein bt2390)."""
    a = normalize_tonemap_algo(algo)
    if engine == "cpu" and a not in _CPU_TONEMAP_ALGOS:
        return "hable"
    return a


def normalize_tonemap_desat(value) -> float:
    """0 lässt die Farben in den Spitzen stehen. Obergrenze 2."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:
        return 0.0
    return round(max(0.0, min(2.0, v)), 2)


def normalize_tonemap_peak(value) -> float:
    """0 heißt: Peak aus den Metadaten. Sonst Nits, gedeckelt bei 10000."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v <= 0:
        return 0.0
    return round(min(10000.0, v), 1)


def tonemap_chain(algo: str = "hable", desat: float = 0.0, peak: float = 0.0) -> str:
    """HDR (PQ/HLG) -> SDR (BT.709) auf der CPU. NVIDIA encodiert danach."""
    algo = tonemap_algo_for_engine(algo, "cpu")
    desat = normalize_tonemap_desat(desat)
    peak = normalize_tonemap_peak(peak)
    tone = f"tonemap=tonemap={algo}:desat={desat:g}"
    if peak > 0:
        tone += f":peak={peak:g}"
    return (
        "zscale=t=linear:npl=100,format=gbrpf32le,"
        f"zscale=p=bt709,{tone},"
        "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
    )


# Bisherige Kette: Hable, keine Entsättigung, Peak aus den Metadaten.
# Der Player bleibt auf diesem String und auf BtbN.
_TONEMAP_CHAIN = tonemap_chain()

_TONEMAP_ENGINES = ("cpu", "libplacebo", "cuda", "opencl", "vaapi", "tonemapx")
_JELLYFIN_ENGINES = ("cuda", "opencl", "vaapi", "tonemapx")
_JELLYFIN_FILTER = {
    "cuda": "tonemap_cuda",
    "opencl": "tonemap_opencl",
    "vaapi": "tonemap_vaapi",
    "tonemapx": "tonemapx",
}
TONEMAP_ENGINE_LABELS = {
    "cpu": "CPU",
    "libplacebo": "libplacebo",
    "cuda": "CUDA",
    "opencl": "OpenCL",
    "vaapi": "VAAPI",
    "tonemapx": "tonemapx",
}
_jelly_filter_cache: Optional[set[str]] = None
_PLACEBO_CURVE = {
    "hable": "hable",
    "bt2390": "bt.2390",
    "reinhard": "reinhard",
    "mobius": "mobius",
    "gamma": "gamma",
    "clip": "clip",
    "linear": "linear",
}
# gradfun: Stärke:Radius. Wirkt auf SDR-Bilder, auch ohne Tonemap.
_DEBAND = {
    "light": "gradfun=1.2:16",
    "medium": "gradfun=1.6:16",
    "strong": "gradfun=2.0:16",
}
_SVT_LOOKS = ("standard", "detail")


def normalize_tonemap_engine(value: str) -> str:
    v = str(value or "cpu").strip().lower()
    return v if v in _TONEMAP_ENGINES else "cpu"


def jellyfin_filters() -> set[str]:
    """Filter des zweiten Binary. Leer, wenn ffmpeg-jellyfin fehlt."""
    global _jelly_filter_cache
    if _jelly_filter_cache is not None:
        return _jelly_filter_cache
    from . import config
    found: set[str] = set()
    path = Path(config.FFMPEG_JELLYFIN)
    if path.is_file():
        try:
            out = subprocess.run(
                [str(path), "-hide_banner", "-filters"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=20, check=False,
            )
            for line in (out.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] in _JELLYFIN_FILTER.values():
                    found.add(parts[1])
        except (OSError, subprocess.SubprocessError):
            found = set()
    _jelly_filter_cache = found
    return found


def tonemap_engine_for(engine: str, platform: str) -> str:
    """Engine, die auf dieser Plattform und mit dem vorhandenen Binary läuft.

    CUDA nur mit NVIDIA. VAAPI nur, wenn der Encoder selbst VAAPI-Frames
    erwartet (AMD, Intel im VAAPI-Modus). OpenCL und tonemapx auf allen
    Plattformen. Fehlt Jellyfin-FFmpeg oder der Filter, gilt die CPU-Kurve.
    """
    engine = normalize_tonemap_engine(engine)
    platform = platform or "cpu"
    if engine == "libplacebo" and platform not in ("cpu", "nvidia"):
        return "cpu"
    if engine == "cuda" and platform != "nvidia":
        return "cpu"
    if engine == "vaapi" and not (
            platform == "amd" or (platform == "intel" and ff.intel_uses_vaapi())):
        return "cpu"
    needed = _JELLYFIN_FILTER.get(engine)
    if needed and needed not in jellyfin_filters():
        return "cpu"
    return engine


def effective_tonemap_engine(engine: str, platform: str, *,
                             preserve_dv: bool = False) -> str:
    """Dolby Vision bleibt auf BtbN. Jellyfin-Engines fallen dann auf die CPU."""
    resolved = tonemap_engine_for(engine, platform)
    if preserve_dv and resolved in _JELLYFIN_ENGINES:
        return "cpu"
    return resolved


def ffmpeg_binary_for(engine: str) -> str:
    from . import config
    if engine in _JELLYFIN_ENGINES:
        return config.FFMPEG_JELLYFIN
    return config.FFMPEG


def normalize_deband(value: str) -> str:
    v = str(value or "off").strip().lower()
    return v if v in _DEBAND else "off"


def normalize_svt_look(value: str) -> str:
    v = str(value or "standard").strip().lower()
    return v if v in _SVT_LOOKS else "standard"


def tonemap_placebo(algo: str) -> str:
    """HDR nach SDR auf Vulkan. Peak kommt aus den Metadaten, Bild für Bild."""
    curve = _PLACEBO_CURVE[normalize_tonemap_algo(algo)]
    return (
        f"libplacebo=tonemapping={curve}:colorspace=bt709"
        ":color_primaries=bt709:color_trc=bt709:range=limited"
        ":format=yuv420p:peak_detect=1"
    )


def _jelly_tone_filter(kind: str, algo: str, desat: float, peak: float,
                       fmt: str) -> str:
    """Ein Jellyfin-Tonemap-Filter. Peak 0 lässt die Metadaten gelten.

    ``desat`` wird immer gesetzt. Die Vorgabe der Jellyfin-Filter wäre 0,5.
    ``tonemap_vaapi`` hat keine ``tonemap``-Kurvenoption (Treiber-Shader).
    """
    algo = normalize_tonemap_algo(algo)
    desat = normalize_tonemap_desat(desat)
    peak = normalize_tonemap_peak(peak)
    if kind == "tonemapx":
        parts = [
            f"tonemap={algo}",
            f"desat={desat:g}",
            "matrix=bt709",
            "primaries=bt709",
            "range=tv",
            "transfer=bt709",
            f"format={fmt}",
        ]
        if peak > 0:
            parts.insert(2, f"peak={peak:g}")
    elif kind == "tonemap_vaapi":
        parts = [
            f"desat={desat:g}",
            "p=bt709",
            "t=bt709",
            "m=bt709",
            "range=tv",
            f"format={fmt}",
        ]
        if peak > 0:
            parts.insert(1, f"peak={peak:g}")
    else:
        parts = [
            f"tonemap={algo}",
            f"desat={desat:g}",
            "p=bt709",
            "t=bt709",
            "m=bt709",
            "range=tv",
            f"format={fmt}",
        ]
        if peak > 0:
            parts.insert(2, f"peak={peak:g}")
    return kind + "=" + ":".join(parts)


def _scale_cuda(target_height: Optional[int], downscale: bool, ten_bit: bool) -> str:
    """scale_cuda immer mit Zielformat, sonst werden CUDA-Frames bei NVENC grün."""
    sc = f"scale_cuda=-2:{target_height}" if downscale else "scale_cuda"
    fmt = "p010le" if ten_bit else "nv12"
    if sc == "scale_cuda":
        return f"{sc}=format={fmt}"
    return f"{sc}:format={fmt}"


def reference_tonemap_setup(
    engine: str, platform: str, algo: str, desat: float, peak: float, *,
    active: bool,
) -> tuple[str, list[str], str]:
    """Binary, Argumente vor ``-i`` und das Tonemap-Stück für Referenz und Standbild.

    Die Ausgabe ist immer ein Software-Frame. libvmaf selbst bleibt bei BtbN.
    """
    from . import config
    if not active:
        return config.FFMPEG, [], ""
    engine = tonemap_engine_for(engine, platform)
    pre: list[str] = []
    if engine == "libplacebo":
        pre = ["-init_hw_device", "vulkan"]
        piece = tonemap_placebo(algo)
    elif engine == "cuda":
        pre = ["-hwaccel", "cuda"]
        piece = ",".join([
            "format=p010le", "hwupload_cuda",
            _jelly_tone_filter("tonemap_cuda", algo, desat, peak, "nv12"),
            "hwdownload", "format=nv12", "format=yuv420p",
        ])
    elif engine == "opencl":
        if platform == "intel" and not ff.intel_uses_vaapi():
            pre = ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}",
                   "-init_hw_device", "qsv=qs@va",
                   "-filter_hw_device", "qs"]
            upload = "format=p010le,hwupload,hwmap=derive_device=opencl"
        elif platform in ("amd", "intel"):
            pre = ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}",
                   "-filter_hw_device", "va"]
            upload = "format=p010le,hwupload,hwmap=derive_device=opencl"
        else:
            pre = ["-init_hw_device", "opencl=ocl", "-filter_hw_device", "ocl"]
            upload = "format=p010le,hwupload"
        piece = ",".join([
            upload,
            _jelly_tone_filter("tonemap_opencl", algo, desat, peak, "nv12"),
            "hwdownload", "format=nv12", "format=yuv420p",
        ])
    elif engine == "vaapi":
        pre = ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}",
               "-filter_hw_device", "va"]
        piece = ",".join([
            "format=p010le", "hwupload",
            _jelly_tone_filter("tonemap_vaapi", algo, desat, peak, "nv12"),
            "hwdownload", "format=nv12", "format=yuv420p",
        ])
    elif engine == "tonemapx":
        piece = _jelly_tone_filter("tonemapx", algo, desat, peak, "yuv420p")
    else:
        piece = tonemap_chain(algo, desat, peak)
    return ffmpeg_binary_for(engine), pre, piece


def svt_param_string(film_grain: int = 0, look: str = "standard",
                     keep_hdr: bool = False) -> str:
    """Mainline-SVT. ``detail`` nutzt Parameter, die SVT-AV1-HDR in die Hauptlinie gebracht hat.

    Tune 6, die PQ-Kurve 3 und ``--noise`` gibt es nur im Fork, nicht hier.
    """
    parts = ["tune=0"]
    if normalize_svt_look(look) == "detail":
        parts += [
            "enable-variance-boost=1",
            "variance-boost-strength=2",
            "ac-bias=1",
            "sharpness=1",
            "tf-strength=1",
        ]
        if keep_hdr:
            parts.append("luminance-qp-bias=20")
    grain = max(0, min(50, int(film_grain or 0)))
    if grain > 0:
        parts.append(f"film-grain={grain}")
        parts.append("film-grain-denoise=0")
    return ":".join(parts)

# Denoise-Stufen (hqdn3d: luma_spatial:chroma_spatial:luma_tmp:chroma_tmp)
_DENOISE = {
    "light": "hqdn3d=1:1:4:4",
    "medium": "hqdn3d=3:2:6:6",
    "strong": "hqdn3d=6:4:9:9",
}
# Unsharp: luma-Stärke. Chroma bleibt unangetastet.
_SHARPEN = {
    "light": "unsharp=5:5:0.4:5:5:0.0",
    "medium": "unsharp=5:5:0.8:5:5:0.0",
    "strong": "unsharp=5:5:1.2:5:5:0.0",
}
# Sichtbares Korn. Kostet Bits, anders als die AV1-Film-Grain-Synthese.
_GRAIN = {
    "light": "noise=alls=4:allf=t",
    "medium": "noise=alls=8:allf=t",
    "strong": "noise=alls=16:allf=t",
}
_PICTURE_LEVELS = ("off", "light", "medium", "strong")


def normalize_picture_level(value: str) -> str:
    v = str(value or "off").strip().lower()
    return v if v in _PICTURE_LEVELS else "off"


def picture_filters(deband: str, denoise: str, sharpen: str, grain: str) -> list[str]:
    """Deband, Denoise, Schärfen, Korn. Dieselbe Reihenfolge wie der Encode."""
    out: list[str] = []
    band = _DEBAND.get(normalize_deband(deband), "")
    if band:
        out.append(band)
    level = normalize_picture_level(denoise)
    if level in _DENOISE:
        out.append(_DENOISE[level])
    level = normalize_picture_level(sharpen)
    if level in _SHARPEN:
        out.append(_SHARPEN[level])
    level = normalize_picture_level(grain)
    if level in _GRAIN:
        out.append(_GRAIN[level])
    return out


def player_preview_setup(
    *,
    tone: bool,
    engine: str,
    platform: str,
    algo: str,
    desat: float,
    peak: float,
    deband: str = "off",
    denoise: str = "off",
    sharpen: str = "off",
    grain: str = "off",
    encoder: str = "",
) -> tuple[str, list[str], list[str]]:
    """Binary (leer = BtbN behalten), Argumente vor ``-i``, Filterstücke.

    Nach einem Seek immer der Speicher-Pfad, wie bei den VMAF-Clips.
    Bildfilter folgen dem Tonemap. Nutzt der HLS-Encoder schon VAAPI oder
    QSV auf demselben Chip, bleibt dessen Device stehen.
    """
    from . import config
    pre: list[str] = []
    parts: list[str] = []
    binary = ""
    if tone:
        eng = effective_tonemap_engine(engine, platform)
        bin_path, tone_pre, piece = reference_tonemap_setup(
            eng, platform, algo, desat, peak, active=True)
        owns = ("vaapi" in (encoder or "")) or ("qsv" in (encoder or ""))
        if owns and eng in ("opencl", "vaapi") and (platform or "") in ("intel", "amd"):
            tone_pre = []
        if bin_path and bin_path != config.FFMPEG:
            binary = bin_path
        pre = list(tone_pre)
        if piece:
            parts.append(piece)
    parts.extend(picture_filters(deband, denoise, sharpen, grain))
    return binary, pre, parts


def preview_look_summary(look: dict, *, is_hdr: bool) -> str:
    """Kurzer Text für die Player-Statuszeile."""
    if not look:
        return ""
    bits: list[str] = []
    if is_hdr and look.get("tonemap"):
        eng = effective_tonemap_engine(
            str(look.get("engine") or "cpu"), str(look.get("platform") or "cpu"))
        bits.append(TONEMAP_ENGINE_LABELS.get(eng, eng))
        if eng != "vaapi":
            algo = tonemap_algo_for_engine(str(look.get("algo") or "hable"), eng)
            bits.append(TONEMAP_ALGO_LABELS.get(algo, algo))
    for label, key in (
        ("Deband", "deband"), ("Denoise", "denoise"),
        ("Schärfen", "sharpen"), ("Korn", "grain"),
    ):
        raw = str(look.get(key) or "off")
        val = normalize_deband(raw) if key == "deband" else normalize_picture_level(raw)
        if val != "off":
            bits.append(f"{label} {val}")
    return " · ".join(bits)


def deinterlace_filter(mode: str, interlaced: bool) -> str:
    """bwdif, ein Frame pro Eingabe-Frame, damit Dauer und Metadaten passen.

    ``auto`` nur bei erkanntem Halbbild. ``on`` immer. ``send_frame`` verdoppelt
    die Bildzahl nicht.
    """
    m = (mode or "auto").lower()
    if m == "on":
        return "bwdif=mode=send_frame:parity=auto:deint=all"
    if m == "auto" and interlaced:
        return "bwdif=mode=send_frame:parity=auto:deint=interlaced"
    return ""


def build_video_filters(
    info: VideoInfo,
    platform: str,
    target_height: Optional[int],
    tonemap: bool,
    *,
    nvidia_cuda_frames: bool = False,
    preserve_hdr: bool = False,
    denoise: str = "off",
    sharpen: str = "off",
    grain: str = "off",
    deinterlace: str = "auto",
    force_10bit: bool = False,
    crop: str = "",
    tonemap_algo: str = "hable",
    tonemap_desat: float = 0.0,
    tonemap_peak: float = 0.0,
    tonemap_engine: str = "cpu",
    deband: str = "off",
    cuda_surface: bool = False,
) -> Optional[str]:
    """Baut die `-vf`-Kette: Tonemapping -> Downscale -> HW-Upload/Format.

    `nvidia_cuda_frames=True` => reine GPU-Pipeline (Frames bleiben als CUDA-
    Surfaces, Skalierung via scale_cuda). Sonst Software-Filterpfad.
    `preserve_hdr=True` => 10-bit-Surface (p010le) beim HW-Upload, damit HDR
    auf Intel/AMD erhalten bleibt (statt nv12/8-bit).
    `force_10bit=True` (Anime-Modus) => auch SDR-Quellen als 10-bit codieren,
    um Banding in Farbverläufen zu reduzieren.
    """
    downscale = bool(target_height and info.height and target_height < info.height)
    keep_hdr = preserve_hdr and info.is_hdr
    want_10bit = keep_hdr or force_10bit

    # --- Reine NVIDIA-GPU-Pipeline (kein Tonemap) --------------------------
    if platform == "nvidia" and nvidia_cuda_frames:
        # Immer mit definiertem Zielformat durch scale_cuda leiten. Geht die
        # rohe CUDA-Surface ohne Formatnormalisierung direkt an NVENC, entstehen
        # bei manchen Quellen/Treibern komplett grüne Ausgaben. p010le = 10-bit
        # (Anime), sonst nv12 (8-bit).
        return _scale_cuda(target_height, downscale, force_10bit)

    filters: list[str] = []

    # Halbbilder zuerst auflösen, bevor Crop oder Skalierung die Kämme festschreiben.
    deint = deinterlace_filter(deinterlace, bool(getattr(info, "interlaced", False)))
    if deint:
        filters.append(deint)

    # Auto-Crop schwarzer Balken zuerst (vor Tonemap/Scale), damit die Balken
    # gar nicht erst mitcodiert werden.
    if crop:
        filters.append(f"crop={crop}")

    engine = tonemap_engine_for(tonemap_engine, platform)
    cuda_out = bool(cuda_surface and engine == "cuda")
    if tonemap and info.is_hdr:
        if engine == "libplacebo":
            filters.append(tonemap_placebo(tonemap_algo))
        elif engine == "cuda":
            if not cuda_surface:
                filters += ["format=p010le", "hwupload_cuda"]
            filters.append(_jelly_tone_filter(
                "tonemap_cuda", tonemap_algo, tonemap_desat, tonemap_peak,
                "p010" if force_10bit and cuda_out else "nv12"))
            if cuda_out:
                filters.append(_scale_cuda(target_height, downscale, force_10bit))
            else:
                filters += ["hwdownload", "format=nv12", "format=yuv420p"]
        elif engine == "opencl":
            filters.append("format=p010le")
            filters.append("hwupload")
            if platform in ("amd", "intel"):
                # hwupload trifft das VAAPI-/QSV-Device. tonemap_opencl braucht
                # OpenCL-Frames, hwmap leitet sie daraus ab.
                filters.append("hwmap=derive_device=opencl")
            filters += [
                _jelly_tone_filter(
                    "tonemap_opencl", tonemap_algo, tonemap_desat, tonemap_peak, "nv12"),
                "hwdownload", "format=nv12", "format=yuv420p",
            ]
        elif engine == "vaapi":
            filters += [
                "format=p010le", "hwupload",
                _jelly_tone_filter(
                    "tonemap_vaapi", tonemap_algo, tonemap_desat, tonemap_peak, "nv12"),
                "hwdownload", "format=nv12", "format=yuv420p",
            ]
        elif engine == "tonemapx":
            filters.append(_jelly_tone_filter(
                "tonemapx", tonemap_algo, tonemap_desat, tonemap_peak, "yuv420p"))
        else:
            filters.append(tonemap_chain(tonemap_algo, tonemap_desat, tonemap_peak))

    if not cuda_out:
        deband_f = _DEBAND.get(normalize_deband(deband), "")
        if deband_f:
            filters.append(deband_f)

        if denoise in _DENOISE:
            filters.append(_DENOISE[denoise])
        if sharpen in _SHARPEN:
            filters.append(_SHARPEN[sharpen])
        if grain in _GRAIN:
            filters.append(_GRAIN[grain])

        if downscale:
            # -2 hält das Seitenverhältnis (gerade Pixelzahl für die Encoder).
            filters.append(f"scale=-2:{target_height}:flags=lanczos")

        # Plattformspezifischer Upload/Pixelformat-Schritt.
        # AMD/Intel benötigen Frames auf einer HW-Surface.
        # Für 10-bit (HDR-Erhalt ODER Anime-Modus) p010le, sonst nv12 (8-bit).
        hw_fmt = "p010le" if want_10bit else "nv12"
        if platform == "amd" or (platform == "intel" and ff.intel_uses_vaapi()):
            filters.append(f"format={hw_fmt},hwupload")
        elif platform == "intel":
            filters.append(f"format={hw_fmt},hwupload=extra_hw_frames=64")
        elif force_10bit and not keep_hdr:
            # CPU- bzw. NVENC-Software-Pfad: Pixelformat direkt auf 10-bit setzen.
            filters.append("format=yuv420p10le")

    if not filters:
        return None
    return ",".join(filters)


def _range_flag(info: VideoInfo) -> str:
    """Fernsehbereich (tv) oder PC-Bereich (pc). Unbekannt bleibt tv: Filme sind 16–235."""
    v = (getattr(info, "color_range", "") or "").lower()
    if v in ("pc", "jpeg", "full"):
        return "pc"
    return "tv"


def _color_tuple(info: VideoInfo, *, tonemapped: bool,
                 keep_hdr: bool) -> tuple[str, str, str, str]:
    """Primaries, Transfer, Matrix, Bereich. Keine Pixelumrechnung."""
    if tonemapped:
        return "bt709", "bt709", "bt709", "tv"
    if keep_hdr or info.is_hdr:
        return (info.color_primaries or "bt2020",
                info.color_transfer or "smpte2084",
                info.color_space or "bt2020nc",
                _range_flag(info))
    return (info.color_primaries or "bt709",
            info.color_transfer or "bt709",
            info.color_space or "bt709",
            _range_flag(info))


def _setparams(prim: str, trc: str, space: str, rng: str) -> str:
    """Nur die Kennung am Frame, keine Umrechnung der Helligkeit."""
    limited = "full" if rng == "pc" else "limited"
    return (f"setparams=range={limited}:color_primaries={prim}"
            f":color_trc={trc}:colorspace={space}")


def _with_setparams(vf: Optional[str], sp: str) -> str:
    """Kennung auf den SDR-Frames, direkt vor dem Encoder-Upload.

    Ein GPU-Tonemap lädt vorher HDR-Frames hoch. Die Kennung darf nicht vor
    diesem ersten Upload stehen, sonst markiert sie die Quelle schon als BT.709.
    """
    if not vf:
        return sp
    parts = vf.split(",")
    download = next((i for i, part in enumerate(parts) if part.startswith("hwdownload")), None)
    if download is not None:
        later = [i for i, part in enumerate(parts) if i > download and part.startswith("hwupload")]
        if later:
            parts.insert(later[-1], sp)
        else:
            parts.append(sp)
        return ",".join(parts)
    for i, part in enumerate(parts):
        if part.startswith("hwupload"):
            parts.insert(i, sp)
            return ",".join(parts)
    return f"{vf},{sp}"


def color_output_args(info: VideoInfo, *, tonemapped: bool, keep_hdr: bool) -> list[str]:
    """Farbkennung der Ausgabe. Die Pixelwerte werden dabei nicht gestreckt.

    Ohne diese Angabe markiert NVENC die Datei oft als vollen Bereich, obwohl
    Schwarz bei 16 liegt. Der Player hebt die Schatten dann an.
    Nach einem HDR→SDR-Tonemap gilt BT.709 im Fernsehbereich, passend zur
    Filterkette (``zscale=r=tv``).
    """
    prim, trc, space, rng = _color_tuple(info, tonemapped=tonemapped, keep_hdr=keep_hdr)
    return ["-colorspace", space, "-color_primaries", prim,
            "-color_trc", trc, "-color_range", rng]


def _hdr_output_args(info: VideoInfo, codec: str, enc: str) -> list[str]:
    """Farb-/HDR-Metadaten für den Output, damit HDR10/HLG erhalten bleibt.

    Überträgt Primaries/Transfer/Matrix und den Helligkeitsbereich aus der
    Quelle und erzwingt 10-bit. Dolby Vision wird dabei nicht rekonstruiert
    (nur der HDR10-Basislayer).
    """
    prim = info.color_primaries or "bt2020"
    trc = info.color_transfer or "smpte2084"
    space = info.color_space or "bt2020nc"
    rng = _range_flag(info)
    args = ["-colorspace", space, "-color_primaries", prim, "-color_trc", trc,
            "-color_range", rng]

    if enc == "libx265":
        x265_range = "full" if rng == "pc" else "limited"
        params = (f"colorprim={prim}:transfer={trc}:colormatrix={space}"
                  f":range={x265_range}:hdr10=1:repeat-headers=1")
        args += ["-pix_fmt", "yuv420p10le", "-x265-params", params]
    elif enc in ("libsvtav1", "libx264", "libvpx-vp9"):
        args += ["-pix_fmt", "yuv420p10le"]
    elif "nvenc" in enc and codec == "hevc":
        args += ["-profile:v", "main10"]
    # AV1-NVENC/QSV/VAAPI führen 10-bit über das Surface-Format (p010) mit.
    return args


def _codec_supports_10bit(platform: str, codec: str) -> bool:
    """10-bit-Ausgabe je Encoder. H.264 nur in Software (libx264 High10);
    HW-H.264 (NVENC/QSV/VAAPI) beherrscht kein 10-bit."""
    if codec in ("hevc", "av1", "vp9"):
        return True
    if codec == "h264":
        return platform == "cpu"
    return False


def _ten_bit_output_args(codec: str, enc: str) -> list[str]:
    """Output-Argumente für 10-bit SDR (Anime-Modus), analog zu HDR – aber ohne
    Farb-/HDR-Metadaten. Das Surface-Format (p010) setzt build_video_filters."""
    if enc in ("libx265", "libsvtav1", "libx264", "libvpx-vp9"):
        # x265/x264 wählen Main10/High10 automatisch anhand des Pixelformats.
        return ["-pix_fmt", "yuv420p10le"]
    if "nvenc" in enc and codec == "hevc":
        return ["-profile:v", "main10"]
    if ("qsv" in enc or "vaapi" in enc) and codec == "hevc":
        return ["-profile:v", "main10"]
    # AV1 (NVENC/QSV/VAAPI) führt 10-bit über das p010-Surface – kein Profil-Flag.
    return []


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
    subtitle_per_track: bool = False,
    subtitle_track_settings: Optional[list] = None,
    keep_chapters: bool = False,
    keep_metadata: bool = False,
    film_grain: int = 0,
    denoise: str = "off",
    sharpen: str = "off",
    grain: str = "off",
    deinterlace: str = "auto",
    aq_strength: int = 8,
    force_10bit: bool = False,
    two_pass: bool = False,
    pass_num: Optional[int] = None,
    passlog: Optional[str] = None,
    container: str = "mkv",
    preserve_dv: bool = False,
    crop: str = "",
    encoder_speed: str = "balanced",
    b_frames: str = "auto",
    keyint_sec: int = 0,
    nvenc_tune: str = "auto",
    mobile: Optional[dict] = None,
    tonemap_algo: str = "hable",
    tonemap_desat: float = 0.0,
    tonemap_peak: float = 0.0,
    tonemap_engine: str = "cpu",
    deband: str = "off",
    svt_look: str = "standard",
) -> list[str]:
    """Erzeugt das vollständige FFmpeg-Kommando für einen Encode.

    mobile: zweite Ausgabe aus demselben Decode (``{"path", "height", "quality"}``):
    H.264 8-bit SDR in MP4 mit AAC-Stereo, z. B. fürs Handy. Beide Ausgaben
    hängen am selben Input; FFmpeg dekodiert einmal und bedient beide Encoder.
    """
    from . import config
    engine = effective_tonemap_engine(
        tonemap_engine, platform, preserve_dv=bool(preserve_dv))
    do_tone = bool(tonemap and info.is_hdr)
    mobile_tone = bool(mobile) and info.is_hdr
    binary = ffmpeg_binary_for(engine) if (do_tone or mobile_tone) else config.FFMPEG
    cmd: list[str] = [binary, "-y", "-hide_banner"]

    keep_hdr = bool(preserve_hdr and info.is_hdr)
    downscale = bool(target_height and info.height and target_height < info.height)
    # 10-bit erzwingen (Anime-Modus) nur, wenn der Encoder das kann; sonst 8-bit.
    ten_bit = bool(force_10bit) and _codec_supports_10bit(platform, codec)
    denoise_on = denoise in _DENOISE
    sharpen_on = sharpen in _SHARPEN
    grain_on = grain in _GRAIN
    deint_on = bool(deinterlace_filter(deinterlace, bool(getattr(info, "interlaced", False))))
    # Die Mobile-Fassung braucht Software-Filter (Tonemap/Scale) und damit
    # Frames im RAM; eine reine CUDA-Pipeline kommt dann nicht in Frage.
    deband_on = normalize_deband(deband) in _DEBAND
    software_filters = (denoise_on or sharpen_on or grain_on or deint_on
                        or bool(crop) or bool(mobile) or deband_on)
    pre_sw = bool(crop) or deint_on
    post_sw = denoise_on or sharpen_on or grain_on or deband_on or bool(mobile)
    enc = ff.encoder_name(platform, codec)
    # CUDA-Frames nur, wenn tonemap_cuda der erste Filter ist und danach nichts
    # mehr in den RAM muss. Seek und Software-Filter bleiben beim Download.
    cuda_surface = (
        engine == "cuda" and platform == "nvidia" and do_tone
        and not pre_sw and not post_sw and start_at is None
        and "nvenc" in (enc or "")
    )
    nvidia_cuda_frames = False

    # --- Hardware-Decode-/Device-Initialisierung (VOR dem Input) -----------
    if platform == "nvidia":
        # Reine GPU-Pipeline nur, wenn ausdrücklich gewünscht (NVENC_FULL_GPU)
        # UND kein Software-Filter/Seek nötig ist. Standard: CUDA-Decode mit
        # Download in den RAM – robust gegen grüne Ausgaben, die die reine
        # CUDA-Surface-Pipeline (`-hwaccel_output_format cuda`) je nach
        # Treiber/Quelle erzeugt. Software-Filter (Tonemapping/Denoise) und
        # Positions-Sprünge (VMAF-/Verify-Clips) brauchen ohnehin den RAM-Pfad.
        # Ausnahme: tonemap_cuda ohne Software-Filter bleibt auf der Surface.
        full_gpu = (config.NVENC_FULL_GPU and not do_tone
                    and not software_filters and start_at is None)
        if full_gpu or cuda_surface:
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            nvidia_cuda_frames = full_gpu
        else:
            cmd += ["-hwaccel", "cuda"]
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

    if engine == "libplacebo" and info.is_hdr and (do_tone or mobile_tone):
        cmd += ["-init_hw_device", "vulkan"]
    elif engine == "opencl" and (do_tone or mobile_tone) and platform in ("nvidia", "cpu"):
        # Intel/AMD lassen das VAAPI-/QSV-Device stehen. tonemap_opencl leitet
        # OpenCL daraus ab, der Encoder-Upload danach trifft wieder dasselbe Device.
        cmd += ["-init_hw_device", "opencl=ocl", "-filter_hw_device", "ocl"]

    if start_at is not None:
        cmd += ["-ss", str(start_at)]

    cmd += ["-i", str(info.path)]

    if duration_limit is not None:
        cmd += ["-t", str(duration_limit)]

    vf = build_video_filters(info, platform, target_height, tonemap,
                             nvidia_cuda_frames=nvidia_cuda_frames,
                             preserve_hdr=keep_hdr, denoise=denoise,
                             sharpen=sharpen, grain=grain, deinterlace=deinterlace,
                             force_10bit=ten_bit, crop=crop,
                             tonemap_algo=tonemap_algo, tonemap_desat=tonemap_desat,
                             tonemap_peak=tonemap_peak, tonemap_engine=engine,
                             deband=deband, cuda_surface=cuda_surface)
    tonemapped = bool(tonemap and info.is_hdr and not keep_hdr)
    av1_dv = bool(
        enc == "libsvtav1" and preserve_dv and info.dolby_vision
        and not tonemap and not downscale and not software_filters
    )
    # setparams ändert keine Helligkeit, schreibt die Kennung aber in den Frame.
    # NVENC liest die oft von dort und setzt sonst video_full_range_flag.
    # Auf einer reinen CUDA-Surface gibt es keinen Software-Filter.
    # WICHTIG für AV1-Dolby-Vision: Sobald FFmpeg einen Filtergraph anlegt,
    # übernimmt es beim Öffnen des Encoders nur globale Frame-Side-Data;
    # AV_FRAME_DATA_DOVI_METADATA fällt dabei weg und libsvtav1 schaltet
    # Dolby Vision stumm ab. Ohne Filtergraph bleibt die RPU erhalten.
    if not nvidia_cuda_frames and not cuda_surface and not av1_dv:
        prim, trc, space, rng = _color_tuple(
            info, tonemapped=tonemapped, keep_hdr=keep_hdr)
        sp = _setparams(prim, trc, space, rng)
        vf = _with_setparams(vf, sp)
    if vf:
        cmd += ["-vf", vf]

    cmd += ["-c:v", enc]
    # HEVC in MP4 braucht das hvc1-Tag für breite Player-Kompatibilität (Apple).
    if container == "mp4" and codec == "hevc":
        cmd += ["-tag:v", "hvc1"]
    is_bitrate = rate_mode in ("bitrate", "abr") and bitrate_kbps
    if is_bitrate:
        cmd += ff.bitrate_args(platform, codec, int(bitrate_kbps),
                               abr=(rate_mode == "abr"))
    else:
        cmd += ff.quality_args(platform, quality)

    if keep_hdr:
        cmd += _hdr_output_args(info, codec, enc)
    elif ten_bit:
        cmd += _ten_bit_output_args(codec, enc)
        cmd += color_output_args(info, tonemapped=tonemapped, keep_hdr=False)
    else:
        cmd += color_output_args(info, tonemapped=tonemapped, keep_hdr=False)

    if enc == "libsvtav1":
        svt = svt_param_string(film_grain, svt_look, keep_hdr=keep_hdr)
        cmd += ff.encoder_preset_args(enc, encoder_speed)
        cmd += ["-svtav1-params", svt]
        # Dolby Vision (Profil 10.1) nativ einbetten: FFmpeg liest die DV-RPU der
        # Quelle als Frame-Side-Data, libsvtav1 schreibt sie als T.35-Metadaten-
        # OBUs mit. Das ist der EINZIGE Weg zu AV1-DV (dovi_tool kann kein AV1).
        # Voraussetzung: DV-Quelle, kein Tonemapping, kein Downscale und kein
        # Videofilter – jeder Filtergraph verwirft in FFmpeg beim Öffnen des
        # Encoders die nicht-globale DV-Side-Data. "1" statt "auto", damit
        # FFmpeg bei fehlender RPU nicht stumm nur HDR10 schreibt.
        if av1_dv:
            cmd += ["-dolbyvision", "1"]
            if container == "mp4":
                cmd += ["-strict", "unofficial"]
    elif enc.startswith("libx"):
        cmd += ff.encoder_preset_args(enc, encoder_speed)
    elif enc == "libvpx-vp9":
        cmd += ff.vp9_args(encoder_speed, cq_mode=not is_bitrate)
    elif "nvenc" in enc and not is_bitrate:
        out_w, out_h = ff.scaled_frame_size(info.width, info.height, target_height)
        cmd += ff.encoder_preset_args(enc, encoder_speed)
        cmd += ["-rc", "vbr"]
        cmd += ff.nvenc_archive_args(
            enc, aq_strength, anime=bool(force_10bit),
            width=out_w, height=out_h, multipass="qres",
            b_frames=b_frames, rate_mode=rate_mode,
            tune=nvenc_tune)
    elif "nvenc" in enc:
        out_w, out_h = ff.scaled_frame_size(info.width, info.height, target_height)
        cmd += ff.encoder_preset_args(enc, encoder_speed)
        cmd += ff.nvenc_archive_args(
            enc, aq_strength, anime=bool(force_10bit),
            width=out_w, height=out_h,
            multipass="fullres" if two_pass else None,
            b_frames=b_frames, rate_mode=rate_mode,
            tune=nvenc_tune)
    elif "qsv" in enc:
        cmd += ff.encoder_preset_args(enc, encoder_speed)

    # Echtes Zwei-Pass (zwei Durchläufe) nur für CPU-Encoder im Bitraten-Modus.
    if two_pass and pass_num in (1, 2) and passlog:
        cmd += ["-pass", str(pass_num), "-passlogfile", passlog]

    ff.apply_cpu_aq(cmd, enc, aq_strength)
    cmd += ff.gop_args(float(getattr(info, "fps", 0) or 0), keyint_sec)

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
    if subtitle_per_track:
        # Gezielte Spurauswahl inkl. Default/Forced-Flags (Einzeldatei).
        # Leere Liste => keine Untertitel.
        cmd += ff.subtitle_track_args(subtitle_track_settings or [], info, container)
    elif keep_subtitles:
        # Container-abhängig: MKV -> srt/copy, MP4 -> mov_text (Bild-Subs entfallen).
        cmd += ff.subtitle_copy_args(info, 0, container)
    if not keep_chapters:
        cmd += ["-map_chapters", "-1"]
    if not keep_metadata:
        cmd += ["-map_metadata", "-1"]

    if include_progress:
        cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    else:
        cmd += [str(output)]
    if mobile and pass_num != 1:
        cmd += mobile_output_args(info, platform, mobile, deinterlace=deinterlace,
                                  crop=crop, keep_chapters=keep_chapters,
                                  tonemap_algo=tonemap_algo,
                                  tonemap_desat=tonemap_desat,
                                  tonemap_peak=tonemap_peak,
                                  tonemap_engine=engine,
                                  deband=deband)
    return cmd


def mobile_output_args(info: VideoInfo, platform: str, mobile: dict, *,
                       deinterlace: str = "auto", crop: str = "",
                       keep_chapters: bool = True,
                       tonemap_algo: str = "hable", tonemap_desat: float = 0.0,
                       tonemap_peak: float = 0.0, tonemap_engine: str = "cpu",
                       deband: str = "off") -> list[str]:
    """Zweite Ausgabe am selben Input: H.264, 8-bit SDR, AAC-Stereo, MP4.

    Ausgabeoptionen (``-vf``, ``-c:v``, ``-map``) gelten in FFmpeg je Ausgabedatei;
    deshalb steht dieser Block nach der Hauptausgabe und bekommt eigene Werte.
    """
    height = int(mobile.get("height") or 720)
    quality = int(mobile.get("quality") or 23)
    path = str(mobile.get("path") or "")
    enc = ff.encoder_name(platform, "h264") or "libx264"
    # Tonemap bei HDR-Quellen immer (H.264 fürs Handy ist SDR), 8-bit, kein 10-bit.
    vf = build_video_filters(info, platform, height, True,
                             nvidia_cuda_frames=False, preserve_hdr=False,
                             deinterlace=deinterlace, force_10bit=False, crop=crop,
                             tonemap_algo=tonemap_algo, tonemap_desat=tonemap_desat,
                             tonemap_peak=tonemap_peak, tonemap_engine=tonemap_engine,
                             deband=deband)
    prim, trc, space, rng = _color_tuple(
        info, tonemapped=bool(info.is_hdr), keep_hdr=False)
    sp = _setparams(prim, trc, space, rng)
    vf = _with_setparams(vf, sp)
    args: list[str] = ["-vf", vf, "-map", "0:v:0", "-c:v", enc]
    args += ff.quality_args(platform, quality)
    args += ff.encoder_preset_args(enc, "fast")
    args += ["-colorspace", space, "-color_primaries", prim,
             "-color_trc", trc, "-color_range", rng]
    if enc == "libx264":
        args += ["-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1"]
    elif "nvenc" in enc:
        args += ["-pix_fmt", "yuv420p", "-rc", "vbr", "-profile:v", "high"]
    args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-sn"]
    args += ["-map_chapters", "0" if keep_chapters else "-1"]
    args += ["-movflags", "+faststart", path]
    return args


def command_details(cmd: list[str], output: Optional[str] = None) -> dict:
    """JSON-safe facts from the command, not inferred encoder defaults.

    With multiple outputs only options preceding the primary output are read.
    Absent native options remain None: driver/encoder defaults are not verified.
    """
    argv = [str(x) for x in cmd]
    primary = argv.index(str(output)) if output and str(output) in argv else len(argv)
    tokens = argv[1:primary]
    options: dict = {}
    flags = {"-y", "-n", "-hide_banner", "-nostats", "-an", "-sn", "-vn"}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("-"):
            i += 1
            continue
        value = None
        if token not in flags and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if not nxt.startswith("-") or re.match(r"^-\d", nxt):
                value = nxt
                i += 1
        options.setdefault(token, []).append(value)
        i += 1

    def last(*names):
        for name in names:
            if options.get(name):
                return options[name][-1]
        return None

    filters = last("-vf", "-filter:v", "-filter_complex")
    tone = None
    for marker, engine in (("tonemap_cuda=", "cuda"), ("tonemap_opencl=", "opencl"),
                           ("tonemap_vaapi=", "vaapi"), ("tonemapx=", "tonemapx"),
                           ("libplacebo=", "libplacebo"), ("tonemap=", "cpu")):
        if marker in (filters or ""):
            tone = engine
            break
    enc = last("-c:v", "-c:v:0", "-vcodec", "-c", "-codec")
    backend = ("copy" if enc == "copy" else "nvidia" if "nvenc" in (enc or "")
               else "intel_qsv" if "qsv" in (enc or "")
               else "vaapi" if "vaapi" in (enc or "")
               else "cpu" if (enc or "").startswith("lib") else None)
    return {
        "argv": argv, "binary": argv[0] if argv else "", "output": output,
        "encoder": enc, "backend": backend, "preset": last("-preset", "-preset:v", "-cpu-used"),
        "rate_control": {key: last("-" + key) for key in
                         ("crf", "cq", "qp", "global_quality", "b:v", "maxrate", "bufsize", "rc")},
        "pass": last("-pass"), "multipass": last("-multipass"),
        "b_frames": last("-bf", "-bf:v"), "keyint": last("-g", "-g:v"),
        "tune": last("-tune", "-tune:v"), "pixel_format": last("-pix_fmt"),
        "encoder_params": {key: last("-" + key) for key in
                           ("svtav1-params", "aom-params", "x265-params", "x264-params")},
        "filters": filters, "tonemap_engine": tone,
        "hardware": {"decode": last("-hwaccel"),
                     "frame_format": last("-hwaccel_output_format"),
                     "devices": list(options.get("-init_hw_device") or []),
                     "filter_device": last("-filter_hw_device")},
        "maps": list(options.get("-map") or []),
        "inputs": list(options.get("-i") or []), "options": options,
    }


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

    def __init__(self, on_progress: Optional[Callable[[EncodeProgress], None]] = None,
                 on_command: Optional[Callable[[list[str]], None]] = None):
        self.on_progress = on_progress
        self.on_command = on_command
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
        if self.on_command:
            try:
                self.on_command(list(cmd))
            except Exception:
                # Observability cannot stop an encode, but failure must be visible.
                logging.getLogger(__name__).exception("Encoding command reporting failed")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",  # Latin-1-Metadaten dürfen das Lesen nicht killen
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
