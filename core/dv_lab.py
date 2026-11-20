"""Dolby Vision & HDR10+ Labor (Analyzer, 0-Disk-Space Bitrate-Scanner, Hybrid-Injector, Dual-HDR).

Implementiert alle Funktionen moderner DV-Analyse- und Hybrid-Copy-Werkzeuge
100 % nativ auf Basis quelloffener Standard-Tools (`dovi_tool`, `hdr10plus_tool`,
`ffmpeg`, `ffprobe`, optional `mkvmerge` & `mediainfo`):

1. **DV-Analyzer**:
   - Sofort-Analyse (Profil, Level, CM v2.9 / CM v4.0, Base-Layer-Typ, MEL vs. FEL,
     RPU Mastering Display, RPU MaxCLL/MaxFALL, HDR10+-Koexistenz).
   - **0-Disk-Space Bitrate-Scanner**: Streamt den HEVC-Annex-B-Datenstrom direkt per
     Pipe durch einen schnellen NALU-Parser im RAM (kein doppelter Speicherplatz!).
     Trennt Base Layer (BL), Enhancement Layer (EL, `UNSPEC63` / Layer 1) und RPU
     (`UNSPEC62`), berechnet Größe, Ø-Bitrate, Spitzen-Bitrate, EL-Anteil (%) und
     liefert Zeitreihen für den zoombaren Multi-Layer-Bitrate-Graphen.
   - **5-Format-Report-Export**: Standard-MediaInfo inkl. integriertem Dolby-Vision-Block
     als TXT, Markdown, JSON, CSV und HTML.

2. **Cross-Release DV-Transfer (Hybrid-Builder) & HDR-Metadaten**:
   - Kompatibilitätsprüfung zweier Releases inkl. automatischer Intro-Offset-Erkennung
     (Szenenschnitt-Korrelation über `scene_refresh_flag`) und L5-Active-Area-Schutz
     bei gecroppten Zieldateien.
   - Framegenaue RPU-Anpassung (`dovi_tool editor`: Intro-Trim/Padding, Endlängen-Sync,
     L5-Offsets).
   - HDR10+ → Dolby Vision 8.1, die umgekehrte Näherung DV-RPU → HDR10+ (RPU bleibt),
     Profil 7 → einzelner TV-Stream 8.1 (Enhancement-Layer-Pixel entfallen) und
     ein RPU-gegen-RPU-Vergleich (Frames, Schnitte, L1, L5).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import config, dolby_vision as dv, ffmpeg_utils as ff, hdr10plus, rpu_vault

logger = logging.getLogger("vcompress.dv_lab")

StatusCb = Optional[Callable[[str], None]]
ProgressCb = Optional[Callable[[float, str], None]]


def _fmt_bytes_gb_mb(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "0 MB"
    gb = num_bytes / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = num_bytes / (1024 ** 2)
    if mb >= 1.0:
        return f"{mb:.2f} MB"
    return f"{num_bytes / 1024.0:.1f} KB"


def _mediainfo_available() -> bool:
    try:
        r = subprocess.run(
            [config.MEDIAINFO, "--Version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _mkvmerge_available() -> bool:
    try:
        r = subprocess.run(
            [config.MKVMERGE, "--version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def get_mediainfo_text(path: Path) -> str:
    """Liefert den Standard-MediaInfo-Export (oder einen strukturierten ffprobe-Fallback)."""
    if _mediainfo_available():
        try:
            res = subprocess.run(
                [config.MEDIAINFO, str(path)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30, check=False,
            )
            if res.returncode == 0 and (res.stdout or "").strip():
                return res.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    # Sauberer Fallback im MediaInfo-Stil via ffprobe, falls mediainfo lokal nicht installiert ist
    info = ff.ffprobe(path)
    if info is None:
        return f"General\nComplete name                            : {path.name}\n"
    lines = [
        "General",
        f"Complete name                            : {path.name}",
        f"Format                                   : {path.suffix.lstrip('.').upper() or 'Container'}",
        f"File size                                : {_fmt_bytes_gb_mb(info.size_bytes)}",
        f"Duration                                 : {int(info.duration // 60)} min {int(info.duration % 60)} s",
        f"Overall bit rate                         : {(info.bit_rate / 1_000_000):.2f} Mb/s" if info.bit_rate else "",
        "",
        "Video",
        f"Format                                   : {(info.codec or '').upper()}",
        f"HDR format                               : {info.hdr_type}",
        f"Width                                    : {info.width} pixels",
        f"Height                                   : {info.height} pixels",
        f"Frame rate                               : {info.fps:.3f} FPS" if info.fps else "",
        f"Bit depth                                : {info.bit_depth} bits",
        f"Pixel format                             : {info.pix_fmt}",
        f"Color primaries                          : {info.color_primaries or 'BT.2020' if info.is_hdr else 'BT.709'}",
        f"Transfer characteristics                 : {info.color_transfer or '-'}",
    ]
    if info.master_max:
        lines.append(
            f"Mastering display luminance              : min: {(info.master_min or 0.005):.4f} cd/m2, max: {int(round(info.master_max))} cd/m2"
        )
    if info.max_cll or info.max_fall:
        lines.append(f"Maximum Content Light Level              : {info.max_cll} cd/m2")
        lines.append(f"Maximum Frame-Average Light Level        : {info.max_fall} cd/m2")
    for idx, aud in enumerate(info.audio or [], start=1):
        lines.extend([
            "",
            f"Audio #{idx}",
            f"Format                                   : {str(aud.get('codec') or '').upper()}",
            f"Channel(s)                               : {aud.get('channels') or '-'}",
            f"Language                                 : {aud.get('language') or 'und'}",
            f"Title                                    : {aud.get('title') or ''}",
        ])
    return "\n".join(line for line in lines if line is not None).strip()


def _base_layer_type(info: ff.VideoInfo) -> str:
    if int(info.dv_profile or 0) == 5:
        return "IPT-PQ-C2 (Proprietär, kein HDR10)"
    if info.hdr10_plus:
        return "HDR10+"
    if info.is_hdr:
        if "HLG" in (info.hdr_type or ""):
            return "HLG"
        return "HDR10"
    return "SDR"


def analyze_dv_file(path: Path, *, sample_seconds: float = 180.0) -> dict:
    """Schritt 1 des DV-Analyzers: Schnelle Struktur- und RPU-Analyse beim Laden."""
    if not path.is_file():
        return {"ok": False, "error": "Datei nicht gefunden."}
    info, err = ff.probe_with_error(path)
    if info is None:
        return {"ok": False, "error": f"ffprobe fehlgeschlagen: {err or 'unbekannt'}"}

    vtracks = info.video_tracks or []
    base_track = next((t for t in vtracks if t.get("role") == "base"), vtracks[0] if vtracks else {})
    el_track = next((t for t in vtracks if t.get("role") == "enhancement"), None)
    dv_track = base_track if base_track.get("dolby_vision") else (
        el_track if el_track and el_track.get("dolby_vision") else
        next((t for t in vtracks if t.get("dolby_vision")), {})
    )

    has_dv = bool(info.dolby_vision or dv_track.get("dolby_vision"))
    has_el = bool(base_track.get("dv_el") or el_track is not None or int(info.dv_profile or dv_track.get("dv_profile") or 0) == 7)
    has_bl = bool(base_track.get("dv_bl") is not False)
    has_rpu = bool(base_track.get("dv_rpu") or (el_track or {}).get("dv_rpu") or dv_track.get("dv_rpu") or has_dv)

    rpu_meta: dict = {}
    if has_dv and dv.available() and (info.codec or "").lower() in ("hevc", "h265", "av1"):
        with tempfile.TemporaryDirectory(prefix="vc_dvanalyze_") as tmp:
            rpu_bin = Path(tmp) / "sample.rpu.bin"
            # Für Dual-Track M2TS/MKV sitzt die RPU oft auf der EL-Spur (0:v:1), sonst auf 0:v:0
            extracted = dv._extract_rpu(
                path,
                "av1" if (info.codec or "").lower() == "av1" else "hevc",
                rpu_bin,
                mode=0,
                duration_limit=max(15.0, float(sample_seconds)) if sample_seconds > 0 else None,
            )
            if not extracted and el_track is not None:
                extracted = _extract_rpu_from_track(
                    path, 1, rpu_bin, duration_limit=sample_seconds
                )
            if extracted:
                rpu_meta = rpu_vault.inspect_rpu_file(rpu_bin)

    el_type = rpu_meta.get("el_type") or ""
    if not el_type and has_el and el_track is not None:
        # Heuristik aus Stream-Bitrate falls Dual-Track
        el_bps = int(el_track.get("bitrate") or 0)
        if el_bps > 4_000_000:
            el_type = "FEL"
        elif el_bps > 0:
            el_type = "MEL"

    codec_label = (info.codec or "").upper()
    if codec_label in ("HEVC", "H265"):
        codec_label = f"HEVC {base_track.get('profile') or 'Main 10'}".strip()

    mastering_str = rpu_meta.get("rpu_mastering_display") or ""
    if not mastering_str and info.master_max:
        mastering_str = f"{(info.master_min or 0.005):.4f}/{int(round(info.master_max))} nits"

    cll_str = rpu_meta.get("rpu_content_light_level") or ""
    if not cll_str and (info.max_cll or info.max_fall):
        cll_str = f"MaxCLL: {float(info.max_cll):.2f} nits, MaxFALL: {float(info.max_fall):.2f} nits"

    summary = {
        "ok": True,
        "path": config.rel_input(path) or path.name,
        "filename": path.name,
        "size_bytes": info.size_bytes,
        "duration": round(float(info.duration or 0.0), 3),
        "fps": round(float(info.fps or 0.0), 3),
        "dolby_vision": has_dv,
        "hdr10_plus": bool(info.hdr10_plus),
        "is_hdr": bool(info.is_hdr or has_dv),
        "hdr_format": info.hdr_type or "SDR",
        "profile": int(rpu_meta.get("profile") or dv_track.get("dv_profile") or info.dv_profile or 0),
        "profile_label": (
            dv_track.get("dv_profile_label")
            or info.dv_profile_label
            or (str(dv_track.get("dv_profile") or info.dv_profile) if (dv_track.get("dv_profile") or info.dv_profile) else "")
        ),
        "level": dv_track.get("dv_level") if dv_track.get("dv_level") is not None else base_track.get("dv_level"),
        "cm_version": rpu_meta.get("cm_version") or ("CM v2.9" if has_dv else ""),
        "base_layer": has_bl,
        "base_layer_type": _base_layer_type(info),
        "enhancement_layer": has_el,
        "enhancement_layer_type": el_type or ("MEL/FEL (Bitrate-Scan für Detail)" if has_el else "None"),
        "dual_track_el": el_track is not None,
        "rpu": has_rpu,
        "video_codec": codec_label,
        "codec_raw": info.codec,
        "resolution": f"{info.width} x {info.height}",
        "width": info.width,
        "height": info.height,
        "bit_depth": info.bit_depth,
        "rpu_mastering_display": mastering_str,
        "rpu_content_light_level": cll_str,
        "peak_l1_nits": float(rpu_meta.get("peak_l1_nits") or 0.0),
        "avg_l1_nits": float(rpu_meta.get("avg_l1_nits") or 0.0),
        "scene_cuts_sampled": int(rpu_meta.get("scene_cuts") or 0),
        "l5_offsets": rpu_meta.get("l5_offsets"),
        "mediainfo_text": get_mediainfo_text(path),
        "tools": {
            "dovi_tool": dv.available(),
            "hdr10plus_tool": hdr10plus.available(),
            "mkvmerge": _mkvmerge_available(),
            "mediainfo": _mediainfo_available(),
        },
    }
    _apply_mediainfo_hdr_hints(summary)
    if has_dv and "Dolby Vision" not in str(summary.get("hdr_format") or ""):
        shown = summary.get("profile_label") or summary.get("profile") or ""
        base_name = summary.get("hdr_format") or "HDR10"
        label = f"Dolby Vision {shown}".strip()
        summary["hdr_format"] = f"{label} + {base_name}" if base_name and base_name != "SDR" else label
    return summary


def _apply_mediainfo_hdr_hints(summary: dict) -> None:
    """MediaInfo liest HDR10+-SEI, die ffprobe an der Spur oft nicht als Side-Data liefert."""
    text = str(summary.get("mediainfo_text") or "")
    if summary.get("hdr10_plus") or ("HDR10+" not in text and "SMPTE ST 2094" not in text):
        return
    summary["hdr10_plus"] = True
    if summary.get("base_layer_type") == "HDR10":
        summary["base_layer_type"] = "HDR10+"
    hdr = str(summary.get("hdr_format") or "")
    if hdr.endswith(" + HDR10"):
        summary["hdr_format"] = hdr + "+"
    elif hdr == "HDR10":
        summary["hdr_format"] = "HDR10+"


def _mark_dv_from_measurement(base_info: dict, stats: dict) -> None:
    """RPU-NALUs auf der EL-Spur sind Dolby Vision, auch wenn Spur 1 nur HDR10 meldet."""
    if int(stats.get("rpu_size_bytes") or 0) <= 0:
        return
    base_info["rpu"] = True
    if base_info.get("dolby_vision"):
        return
    base_info["dolby_vision"] = True
    if not int(base_info.get("profile") or 0):
        base_info["profile"] = 7
        base_info["profile_label"] = base_info.get("profile_label") or "7"
    hdr = str(base_info.get("hdr_format") or "HDR10")
    if "Dolby Vision" not in hdr:
        shown = base_info.get("profile_label") or base_info.get("profile") or 7
        base_info["hdr_format"] = (
            f"Dolby Vision {shown} + {hdr}" if hdr and hdr != "SDR" else f"Dolby Vision {shown}".strip()
        )


def _extract_rpu_from_track(
    source: Path, v_track_pos: int, out_rpu: Path, duration_limit: Optional[float] = None
) -> bool:
    ff_cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error"]
    if duration_limit and duration_limit > 0:
        ff_cmd += ["-t", f"{float(duration_limit):.3f}"]
    ff_cmd += [
        "-i", str(source),
        "-map", f"0:v:{v_track_pos}",
        "-c:v", "copy",
        "-bsf:v", "hevc_mp4toannexb",
        "-f", "hevc", "-",
    ]
    dv_cmd = [config.DOVI_TOOL, "extract-rpu", "-", "-o", str(out_rpu)]
    try:
        p1 = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(dv_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p1.stdout:
            p1.stdout.close()
        p2.communicate()
        p1.wait()
        return p2.returncode == 0 and out_rpu.is_file() and out_rpu.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        return False


def parse_hevc_annexb_layers_stream(
    byte_iter,
    *,
    fps: float = 24.0,
    total_duration: float = 0.0,
    rpu_sink_file=None,
    progress_cb: ProgressCb = None,
) -> dict:
    """Zerlegt einen HEVC-Annex-B-Bytestrom im RAM (0 Byte Disk-Overhead) in BL, EL und RPU.

    Erkennt:
      * `nal_unit_type == 62` (`UNSPEC62`) -> Dolby Vision RPU
      * `nal_unit_type == 63` (`UNSPEC63`) oder `nuh_layer_id > 0` -> Enhancement Layer (EL)
      * Übrige NAL-Units (`layer_id == 0`) -> Base Layer (BL)

    Aggregiert gleichzeitig 1-Sekunden-Bitraten-Buckets für BL, EL und RPU.
    """
    eff_fps = fps if (fps and fps > 1.0) else 23.976
    buf = bytearray()
    bl_total = 0
    el_total = 0
    rpu_total = 0
    frame_count = 0
    seen_vcl_in_au = False

    bl_sec: list[int] = [0]
    el_sec: list[int] = [0]
    rpu_sec: list[int] = [0]
    last_report = time.monotonic()

    def _account_nalu(nalu_bytes: memoryview | bytes) -> None:
        nonlocal bl_total, el_total, rpu_total, frame_count, seen_vcl_in_au
        nlen = len(nalu_bytes)
        if nlen < 2:
            return
        # Bestimme Startcode-Länge (3 oder 4 Bytes)
        sc_len = 4 if (nlen >= 4 and nalu_bytes[0] == 0 and nalu_bytes[1] == 0 and nalu_bytes[2] == 0 and nalu_bytes[3] == 1) else 3
        if nlen <= sc_len + 1:
            return
        b0 = nalu_bytes[sc_len]
        b1 = nalu_bytes[sc_len + 1]
        nal_type = (b0 >> 1) & 0x3F
        layer_id = ((b0 & 1) << 5) | ((b1 >> 3) & 0x1F)

        # Access-Unit-/Frame-Zählung auf dem Basislayer
        if nal_type == 35 and layer_id == 0:  # AUD
            if seen_vcl_in_au:
                frame_count += 1
                seen_vcl_in_au = False
        elif nal_type <= 31 and layer_id == 0 and nlen > sc_len + 2:
            first_slice = (nalu_bytes[sc_len + 2] & 0x80) != 0
            if first_slice:
                if seen_vcl_in_au:
                    frame_count += 1
                seen_vcl_in_au = True

        sec_idx = int(frame_count / eff_fps)
        while len(bl_sec) <= sec_idx:
            bl_sec.append(0)
            el_sec.append(0)
            rpu_sec.append(0)

        if nal_type == 62:
            rpu_total += nlen
            rpu_sec[sec_idx] += nlen
            if rpu_sink_file is not None:
                rpu_sink_file.write(nalu_bytes)
        elif nal_type == 63 or layer_id > 0:
            el_total += nlen
            el_sec[sec_idx] += nlen
        else:
            bl_total += nlen
            bl_sec[sec_idx] += nlen

    start_code = b"\x00\x00\x01"
    for chunk in byte_iter:
        if not chunk:
            continue
        buf.extend(chunk)
        # Suche alle vollständigen NAL-Units im Buffer
        pos = buf.find(start_code)
        if pos < 0:
            # Kein Startcode im Puffer -> bis auf letzte 3 Bytes verwerfen/zählen
            if len(buf) > 4:
                bl_total += len(buf) - 3
                del buf[:-3]
            continue
        if pos > 0:
            # Falls direkt davor ein 0x00 für 4-Byte-Startcode liegt, behalten
            if buf[pos - 1] == 0:
                pos -= 1
            if pos > 0:
                bl_total += pos
                del buf[:pos]

        cursor = 0
        while True:
            nxt = buf.find(start_code, cursor + 3)
            if nxt < 0:
                break
            nalu_end = nxt - 1 if (nxt > 0 and buf[nxt - 1] == 0) else nxt
            _account_nalu(memoryview(buf)[cursor:nalu_end])
            cursor = nalu_end
        if cursor > 0:
            del buf[:cursor]

        now = time.monotonic()
        if progress_cb and total_duration > 0 and (now - last_report) >= 0.6:
            last_report = now
            cur_sec = frame_count / eff_fps
            pct = min(98.0, max(1.0, (cur_sec / total_duration) * 100.0))
            progress_cb(pct, f"Bitrate-Scan läuft: {int(cur_sec)}s / {int(total_duration)}s ({pct:.0f} %)")

    if buf:
        _account_nalu(bytes(buf))
    if seen_vcl_in_au:
        frame_count += 1

    dur_calc = (frame_count / eff_fps) if frame_count > 0 else max(1.0, total_duration)
    dur_calc = max(0.001, dur_calc)

    bl_mbps_series = [round((b * 8.0) / 1_000_000.0, 3) for b in bl_sec]
    el_mbps_series = [round((b * 8.0) / 1_000_000.0, 3) for b in el_sec]
    rpu_kbps_series = [round((b * 8.0) / 1_000.0, 2) for b in rpu_sec]
    timestamps = list(range(len(bl_mbps_series)))

    combined_video = bl_total + el_total
    el_share = round((el_total / combined_video) * 100.0, 2) if combined_video > 0 else 0.0

    return {
        "frames_counted": frame_count,
        "duration_scanned": round(dur_calc, 3),
        "bl_size_bytes": bl_total,
        "bl_size_human": _fmt_bytes_gb_mb(bl_total),
        "bl_avg_mbps": round((bl_total * 8.0) / dur_calc / 1_000_000.0, 2),
        "bl_max_mbps": round(max(bl_mbps_series) if bl_mbps_series else 0.0, 2),
        "el_size_bytes": el_total,
        "el_size_human": _fmt_bytes_gb_mb(el_total),
        "el_avg_mbps": round((el_total * 8.0) / dur_calc / 1_000_000.0, 2),
        "el_max_mbps": round(max(el_mbps_series) if el_mbps_series else 0.0, 2),
        "el_share_pct": el_share,
        "rpu_size_bytes": rpu_total,
        "rpu_size_human": _fmt_bytes_gb_mb(rpu_total),
        "rpu_avg_kbps": round((rpu_total * 8.0) / dur_calc / 1_000.0, 2),
        "rpu_max_kbps": round(max(rpu_kbps_series) if rpu_kbps_series else 0.0, 2),
        "timestamps": timestamps,
        "bl_mbps_series": bl_mbps_series,
        "el_mbps_series": el_mbps_series,
        "rpu_kbps_series": rpu_kbps_series,
    }


def scan_dv_bitrates(
    path: Path,
    *,
    duration_limit: float = 0.0,
    progress_cb: ProgressCb = None,
) -> dict:
    """Schritt 2 des DV-Analyzers: Vollständiger BL/EL/RPU-Bitrate-Scan ohne temporäres Video-Demuxing."""
    base_info = analyze_dv_file(path, sample_seconds=60.0)
    if not base_info.get("ok"):
        return base_info

    codec_raw = (base_info.get("codec_raw") or "").lower()
    fps = float(base_info.get("fps") or 23.976)
    total_dur = float(base_info.get("duration") or 0.0)
    scan_dur = min(total_dur, duration_limit) if (duration_limit and duration_limit > 0 and total_dur > 0) else total_dur

    if codec_raw not in ("hevc", "h265"):
        # Fallback für AV1 DV (Profil 10): Paketgrößen per ffprobe lesen
        return _scan_packet_bitrates_fallback(path, base_info, scan_dur)

    with tempfile.TemporaryDirectory(prefix="vc_dvscan_") as tmp:
        tmp_dir = Path(tmp)
        rpu_annexb = tmp_dir / "rpu_nalus.hevc"
        rpu_bin = tmp_dir / "full.rpu.bin"

        ff_cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error"]
        if duration_limit and duration_limit > 0:
            ff_cmd += ["-t", f"{float(duration_limit):.3f}"]
        ff_cmd += [
            "-i", str(path),
            "-map", "0:v:0",
            "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc", "-",
        ]

        try:
            proc = subprocess.Popen(
                ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=2 * 1024 * 1024
            )
            with open(rpu_annexb, "wb") as rpu_fh:
                def _chunks():
                    assert proc.stdout is not None
                    while True:
                        data = proc.stdout.read(1024 * 1024)
                        if not data:
                            break
                        yield data

                stats = parse_hevc_annexb_layers_stream(
                    _chunks(),
                    fps=fps,
                    total_duration=scan_dur,
                    rpu_sink_file=rpu_fh,
                    progress_cb=progress_cb,
                )
            _, stderr_bytes = proc.communicate(timeout=30)
            if proc.returncode not in (0, None) and stats["bl_size_bytes"] == 0:
                err_txt = (stderr_bytes or b"").decode("utf-8", errors="replace")[-500:]
                return {"ok": False, "error": f"FFmpeg-Stream-Scan fehlgeschlagen: {err_txt}"}
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"Bitrate-Scan fehlgeschlagen: {e}"}

        # Dual-Track-Spezialfall (z. B. M2TS / Dual-Track MP4 mit separater EL-Spur 0:v:1)
        if base_info.get("dual_track_el") and stats["el_size_bytes"] == 0:
            ff_el_cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error"]
            if duration_limit and duration_limit > 0:
                ff_el_cmd += ["-t", f"{float(duration_limit):.3f}"]
            ff_el_cmd += [
                "-i", str(path),
                "-map", "0:v:1",
                "-c:v", "copy",
                "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", "-",
            ]
            try:
                proc_el = subprocess.Popen(
                    ff_el_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1024 * 1024
                )
                with open(rpu_annexb, "wb") as rpu_fh2:
                    def _el_chunks():
                        assert proc_el.stdout is not None
                        while True:
                            d = proc_el.stdout.read(1024 * 1024)
                            if not d:
                                break
                            yield d

                    el_stats = parse_hevc_annexb_layers_stream(
                        _el_chunks(), fps=fps, total_duration=scan_dur, rpu_sink_file=rpu_fh2
                    )
                proc_el.wait(timeout=15)
                # Auf Spur 1 sind reguläre VCL-NALUs die Enhancement-Layer!
                el_bytes = el_stats["bl_size_bytes"] + el_stats["el_size_bytes"]
                dur_c = max(0.001, stats["duration_scanned"])
                el_series = [
                    round(a + b, 3)
                    for a, b in zip(el_stats["bl_mbps_series"], el_stats["el_mbps_series"])
                ]
                stats["el_size_bytes"] = el_bytes
                stats["el_size_human"] = _fmt_bytes_gb_mb(el_bytes)
                stats["el_avg_mbps"] = round((el_bytes * 8.0) / dur_c / 1_000_000.0, 2)
                stats["el_max_mbps"] = round(max(el_series) if el_series else 0.0, 2)
                stats["el_mbps_series"] = el_series
                comb = stats["bl_size_bytes"] + el_bytes
                stats["el_share_pct"] = round((el_bytes / comb) * 100.0, 2) if comb > 0 else 0.0
                if el_stats["rpu_size_bytes"] > stats["rpu_size_bytes"]:
                    stats["rpu_size_bytes"] = el_stats["rpu_size_bytes"]
                    stats["rpu_size_human"] = el_stats["rpu_size_human"]
                    stats["rpu_avg_kbps"] = el_stats["rpu_avg_kbps"]
                    stats["rpu_max_kbps"] = el_stats["rpu_max_kbps"]
                    stats["rpu_kbps_series"] = el_stats["rpu_kbps_series"]
            except (OSError, subprocess.SubprocessError):
                pass

        # Vollständige RPU-Metadaten + L1-Helligkeitsreihe aus den gesammelten RPU-NALUs (~50 MB) lesen
        nits_series: list[float] = []
        if dv.available() and rpu_annexb.is_file() and rpu_annexb.stat().st_size > 0:
            res_ex = subprocess.run(
                [config.DOVI_TOOL, "extract-rpu", str(rpu_annexb), "-o", str(rpu_bin)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, check=False,
            )
            if res_ex.returncode == 0 and rpu_bin.is_file():
                full_rpu = rpu_vault.inspect_rpu_file(rpu_bin)
                if full_rpu.get("el_type"):
                    base_info["enhancement_layer_type"] = full_rpu["el_type"]
                if full_rpu.get("cm_version"):
                    base_info["cm_version"] = full_rpu["cm_version"]
                if full_rpu.get("rpu_mastering_display"):
                    base_info["rpu_mastering_display"] = full_rpu["rpu_mastering_display"]
                if full_rpu.get("rpu_content_light_level"):
                    base_info["rpu_content_light_level"] = full_rpu["rpu_content_light_level"]
                if full_rpu.get("peak_l1_nits"):
                    base_info["peak_l1_nits"] = full_rpu["peak_l1_nits"]

        # Falls EL vorhanden und Typ noch unklar, aus gemessener EL-Bitrate bestimmen
        if stats["el_size_bytes"] > 0:
            base_info["enhancement_layer"] = True
            if base_info.get("enhancement_layer_type") not in ("FEL", "MEL"):
                base_info["enhancement_layer_type"] = "FEL" if stats["el_avg_mbps"] >= 3.0 else "MEL"
        elif int(base_info.get("profile") or 0) != 7:
            base_info["enhancement_layer"] = False
            base_info["enhancement_layer_type"] = "None"

        _mark_dv_from_measurement(base_info, stats)

        # L1-Nits-Kurve passend zur Sekundenachse ergänzen (falls DV vorhanden)
        if base_info.get("dolby_vision") and dv.available():
            l1 = dv.extract_l1_curve(
                path,
                source_codec="hevc",
                max_points=max(60, min(1200, len(stats["timestamps"]))),
                duration_limit=duration_limit if duration_limit > 0 else 7200.0,
            )
            if l1.get("ok"):
                nits_series = l1.get("series_max") or []

    stats["l1_max_nits_series"] = nits_series
    base_info["bitrate_scan"] = stats
    return base_info


_scan_jobs_lock = threading.Lock()
_scan_jobs: dict[str, dict] = {}


def start_bitrate_scan_job(path: Path, *, duration_limit: float = 0.0) -> str:
    """Startet Schritt 2 im Hintergrund und liefert eine Job-ID für die Fortschrittsanzeige."""
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _scan_jobs_lock:
        stale = [
            key for key, job in _scan_jobs.items()
            if job.get("status") != "running" and now - float(job.get("updated", now)) > 1800
        ]
        for key in stale:
            _scan_jobs.pop(key, None)
        _scan_jobs[job_id] = {
            "status": "running",
            "percent": 0.0,
            "message": "Bitrate-Scan startet …",
            "result": None,
            "updated": now,
        }

    def _run() -> None:
        def _progress(pct: float, message: str) -> None:
            with _scan_jobs_lock:
                job = _scan_jobs.get(job_id)
                if not job or job.get("status") != "running":
                    return
                job["percent"] = round(max(0.0, min(99.0, float(pct))), 1)
                job["message"] = message
                job["updated"] = time.time()

        try:
            result = scan_dv_bitrates(
                path,
                duration_limit=duration_limit,
                progress_cb=_progress,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Bitrate-Scan fehlgeschlagen")
            result = {"ok": False, "error": str(exc)}
        with _scan_jobs_lock:
            job = _scan_jobs.get(job_id)
            if not job:
                return
            ok = bool(result.get("ok"))
            job["result"] = result
            job["status"] = "done" if ok else "error"
            job["percent"] = 100.0 if ok else job.get("percent") or 0.0
            job["message"] = (
                "Bitrate-Scan abgeschlossen."
                if ok else (result.get("error") or "Bitrate-Scan fehlgeschlagen.")
            )
            job["updated"] = time.time()

    threading.Thread(target=_run, name=f"dv-scan-{job_id}", daemon=True).start()
    return job_id


def bitrate_scan_job(job_id: str) -> Optional[dict]:
    """Aktueller Stand eines Bitrate-Scans. `result` erst nach Abschluss."""
    with _scan_jobs_lock:
        job = _scan_jobs.get(job_id)
        if not job:
            return None
        out = {
            "ok": True,
            "job_id": job_id,
            "status": job["status"],
            "percent": job["percent"],
            "message": job["message"],
        }
        if job["status"] in ("done", "error"):
            out["result"] = job["result"]
        return out


def _scan_packet_bitrates_fallback(path: Path, base_info: dict, scan_dur: float) -> dict:
    """Fallback-Bitratescan über `ffprobe -show_packets` für AV1 / Nicht-HEVC."""
    cmd = [
        config.FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,size",
        "-of", "csv=p=0",
    ]
    if scan_dur > 0:
        cmd += ["-read_intervals", f"%+{scan_dur:.2f}"]
    cmd.append(str(path))
    sec_bytes: list[int] = [0]
    total_b = 0
    pkt_count = 0
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False
        )
        for line in (res.stdout or "").splitlines():
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            try:
                t = max(0.0, float(parts[0]))
                sz = int(parts[1])
            except ValueError:
                continue
            idx = int(t)
            while len(sec_bytes) <= idx:
                sec_bytes.append(0)
            sec_bytes[idx] += sz
            total_b += sz
            pkt_count += 1
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"Paket-Scan fehlgeschlagen: {e}"}

    dur_c = max(0.001, float(len(sec_bytes)))
    bl_series = [round((b * 8.0) / 1_000_000.0, 3) for b in sec_bytes]
    base_info["bitrate_scan"] = {
        "frames_counted": pkt_count,
        "duration_scanned": round(dur_c, 3),
        "bl_size_bytes": total_b,
        "bl_size_human": _fmt_bytes_gb_mb(total_b),
        "bl_avg_mbps": round((total_b * 8.0) / dur_c / 1_000_000.0, 2),
        "bl_max_mbps": round(max(bl_series) if bl_series else 0.0, 2),
        "el_size_bytes": 0,
        "el_size_human": "0 MB",
        "el_avg_mbps": 0.0,
        "el_max_mbps": 0.0,
        "el_share_pct": 0.0,
        "rpu_size_bytes": 0,
        "rpu_size_human": "— (in AV1-OBUs eingebettet)",
        "rpu_avg_kbps": 0.0,
        "rpu_max_kbps": 0.0,
        "timestamps": list(range(len(bl_series))),
        "bl_mbps_series": bl_series,
        "el_mbps_series": [0.0] * len(bl_series),
        "rpu_kbps_series": [0.0] * len(bl_series),
        "l1_max_nits_series": [],
    }
    return base_info


def format_dv_block_text(data: dict) -> str:
    """Erzeugt den strukturierten Dolby-Vision-Datenblock im MediaInfo-Stil."""
    lines = [
        "Dolby Vision (HDR / DV Studio)",
        f"Dolby Vision                             : {'Yes' if data.get('dolby_vision') else 'No'}",
        f"HDR Format                               : {data.get('hdr_format') or 'SDR'}",
    ]
    if data.get("dolby_vision"):
        lines.extend([
            f"Profile                                  : {data.get('profile_label') or data.get('profile') or '-'}",
            f"Level                                    : {data.get('level') if data.get('level') is not None else '-'}",
            f"CM Version                               : {data.get('cm_version') or '-'}",
            f"Base Layer                               : {'Yes' if data.get('base_layer') else 'No'}",
            f"Base Layer Type                          : {data.get('base_layer_type') or '-'}",
            f"Enhancement Layer                        : {'Yes' if data.get('enhancement_layer') else 'No'}",
            f"Enhancement Layer Type                   : {data.get('enhancement_layer_type') or 'None'}",
            f"RPU                                      : {'Yes' if data.get('rpu') else 'No'}",
        ])
    if data.get("hdr10_plus"):
        lines.append("HDR10+                                   : Yes (SMPTE ST 2094-40)")
    lines.extend([
        f"Video Codec                              : {data.get('video_codec') or '-'}",
        f"Resolution                               : {data.get('resolution') or '-'}",
    ])
    scan = data.get("bitrate_scan")
    if isinstance(scan, dict):
        lines.extend([
            "",
            f"BL Size                                  : {scan.get('bl_size_human')}",
            f"BL Average Bitrate                       : {scan.get('bl_avg_mbps'):.2f} Mbps",
            f"BL Max Bitrate                           : {scan.get('bl_max_mbps'):.2f} Mbps",
        ])
        if data.get("enhancement_layer") or (scan.get("el_size_bytes") or 0) > 0:
            lines.extend([
                f"EL Size                                  : {scan.get('el_size_human')}",
                f"EL Average Bitrate                       : {scan.get('el_avg_mbps'):.2f} Mbps",
                f"EL Max Bitrate                           : {scan.get('el_max_mbps'):.2f} Mbps",
                f"EL Share                                 : {scan.get('el_share_pct'):.2f} %",
            ])
        lines.extend([
            f"RPU Size                                 : {scan.get('rpu_size_human')}",
            f"RPU Bitrate                              : {scan.get('rpu_avg_kbps'):.2f} Kbps",
        ])
    if data.get("rpu_mastering_display"):
        lines.append(f"RPU mastering display                    : {data.get('rpu_mastering_display')}")
    if data.get("rpu_content_light_level"):
        lines.append(f"RPU content light level                  : {data.get('rpu_content_light_level')}")
    return "\n".join(lines)


def export_dv_report(data: dict, fmt: str = "txt") -> tuple[str, str, str]:
    """Exportiert den integrierten DV-Analyzer + MediaInfo-Bericht in 5 Formaten.

    Rückgabe: `(inhalt, mime_type, dateiendung)`.
    Unterstützte Formate: `txt`, `md`, `json`, `csv`, `html`.
    """
    f = (fmt or "txt").strip().lower()
    dv_block = format_dv_block_text(data)
    mi_text = str(data.get("mediainfo_text") or "").strip()
    fname_stem = Path(str(data.get("filename") or "dv_report")).stem

    if f == "json":
        payload = dict(data)
        return json.dumps(payload, indent=2, ensure_ascii=False), "application/json; charset=utf-8", f"{fname_stem}_dv_report.json"

    if f == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        scan = data.get("bitrate_scan") or {}
        ts = scan.get("timestamps") or []
        bl = scan.get("bl_mbps_series") or []
        el = scan.get("el_mbps_series") or []
        rpu_s = scan.get("rpu_kbps_series") or []
        writer.writerow(["second", "bl_mbps", "el_mbps", "rpu_kbps"])
        for i, sec in enumerate(ts):
            writer.writerow([
                sec,
                bl[i] if i < len(bl) else 0.0,
                el[i] if i < len(el) else 0.0,
                rpu_s[i] if i < len(rpu_s) else 0.0,
            ])
        return buf.getvalue(), "text/csv; charset=utf-8", f"{fname_stem}_dv_bitrates.csv"

    if f == "md":
        md = (
            f"# Dolby Vision & HDR Analyse – `{data.get('filename') or ''}`\n\n"
            f"## Dolby Vision Datenblock\n\n```text\n{dv_block}\n```\n\n"
            f"## MediaInfo Standard-Export\n\n```text\n{mi_text}\n```\n"
        )
        return md, "text/markdown; charset=utf-8", f"{fname_stem}_dv_report.md"

    if f == "html":
        import html as _html
        html_doc = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>DV Report – {_html.escape(str(data.get('filename') or ''))}</title>
<style>body{{font-family:system-ui,sans-serif;background:#111418;color:#e6edf3;padding:24px;max-width:980px;margin:auto}}
pre{{background:#1a1f26;border:1px solid #2d3642;border-radius:8px;padding:16px;overflow-x:auto;line-height:1.45}}
h1,h2{{color:#58a6ff}}</style></head><body>
<h1>Dolby Vision &amp; MediaInfo Report</h1>
<h2>Dolby Vision Daten</h2><pre>{_html.escape(dv_block)}</pre>
<h2>MediaInfo</h2><pre>{_html.escape(mi_text)}</pre>
</body></html>"""
        return html_doc, "text/html; charset=utf-8", f"{fname_stem}_dv_report.html"

    # Standard: TXT – Dolby-Vision-Block direkt in den MediaInfo-Export integriert
    if "\nVideo\n" in mi_text:
        parts = mi_text.split("\nVideo\n", 1)
        integrated = f"{parts[0]}\n\n{dv_block}\n\nVideo\n{parts[1]}"
    else:
        integrated = f"{dv_block}\n\n{mi_text}"
    return integrated.strip() + "\n", "text/plain; charset=utf-8", f"{fname_stem}_mediainfo_dv.txt"


# =============================================================================
#  Cross-Release DV-Copy, Auto-Intro-Sync, L5-Crop & HDR10+ -> DV Generator
# =============================================================================

_PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def _detect_video_scene_frames(path: Path, fps: float, probe_seconds: float = 240.0) -> list[int]:
    """Erkennt harte Szenenschnitte in den ersten `probe_seconds` Sekunden eines Videos."""
    eff_fps = fps if (fps and fps > 1.0) else 23.976
    cmd = [
        config.FFMPEG, "-hide_banner",
        "-t", f"{max(30.0, float(probe_seconds)):.1f}",
        "-skip_frame", "nokey",
        "-i", str(path),
        "-vf", "select='gt(scene,0.30)',showinfo",
        "-an", "-sn", "-f", "null", "-",
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=45, check=False,
        )
        frames = [0]
        for m in _PTS_TIME_RE.finditer(res.stderr or ""):
            fr = int(round(float(m.group(1)) * eff_fps))
            if fr > 0 and (not frames or abs(fr - frames[-1]) >= 4):
                frames.append(fr)
        return frames
    except (OSError, subprocess.SubprocessError):
        return []


def correlate_scene_cut_offset(
    donor_cuts: list[int],
    target_cuts: list[int],
    *,
    max_abs_offset_frames: int = 720,
    tolerance_frames: int = 2,
) -> tuple[int, float]:
    """Bestimmt den optimalen Frame-Offset (`target_frame - donor_frame`) aus zwei Szenenschnitt-Listen.

    Ein positiver Wert (+48) bedeutet: Das Zielvideo hat 48 zusätzliche Intro-Frames am Anfang
    (die RPU muss am Anfang um +48 Frames aufgefüllt werden).
    Ein negativer Wert (-48) bedeutet: Die Spender-RPU hat 48 Intro-Frames mehr als das Ziel
    (die ersten 48 Frames der RPU müssen entfernt werden).
    Rückgabe: `(best_offset_frames, confidence_0_to_1)`.
    """
    d_list = [int(x) for x in donor_cuts if x > 0]
    t_list = [int(x) for x in target_cuts if x > 0]
    if len(d_list) < 2 or len(t_list) < 2:
        return 0, 0.0

    # Kandidaten-Offsets aus paarweisen Differenzen sammeln
    votes: dict[int, int] = {}
    for d in d_list[:60]:
        for t in t_list[:60]:
            diff = t - d
            if abs(diff) <= max_abs_offset_frames:
                for delta in range(-tolerance_frames, tolerance_frames + 1):
                    cand = diff + delta
                    votes[cand] = votes.get(cand, 0) + (2 if delta == 0 else 1)

    if not votes:
        return 0, 0.0

    best_offset = max(votes.keys(), key=lambda k: (votes[k], -abs(k)))
    # Prüfe echte Trefferquote bei best_offset
    d_set = d_list[:60]
    matches = 0
    for t in t_list[:60]:
        if any(abs((d + best_offset) - t) <= tolerance_frames for d in d_set):
            matches += 1
    denom = max(1, min(len(d_set), len(t_list[:60])))
    confidence = round(min(1.0, matches / denom), 2)
    if matches < 2:
        return 0, 0.0
    return best_offset, confidence


def check_hybrid_compatibility(
    target_path: Path,
    *,
    donor_path: Optional[Path] = None,
    vault_id: str = "",
    auto_sync_intro: bool = True,
) -> dict:
    """Prüft, ob Spender-DV/HDR10+ (oder Tresor-RPU) und Ziel-Release zusammenpassen."""
    if not target_path.is_file():
        return {"ok": False, "error": "Zieldatei nicht gefunden."}
    t_info = ff.ffprobe(target_path)
    if t_info is None:
        return {"ok": False, "error": "Zieldatei konnte nicht gelesen werden."}

    t_fps = float(t_info.fps or 23.976)
    t_frames = int(round((t_info.duration or 0.0) * t_fps))
    t_crop = ff.detect_crop(t_info, samples=2, probe_seconds=1)

    # Bestimme aktive Bildränder der Zieldatei für L5
    if t_crop:
        parts = [int(x) for x in t_crop.split(":")]
        cw, ch, cx, cy = parts[0], parts[1], parts[2], parts[3]
        target_l5 = {
            "left": max(0, cx),
            "right": max(0, t_info.width - (cx + cw)),
            "top": max(0, cy),
            "bottom": max(0, t_info.height - (cy + ch)),
        }
    else:
        # Keine schwarzen Balken im Ziel -> gecropptes Video (Active Area = 0 an allen Kanten)
        target_l5 = {"left": 0, "right": 0, "top": 0, "bottom": 0}

    donor_summary: dict = {}
    donor_cuts: list[int] = []
    d_frames = 0
    d_fps = t_fps

    if vault_id:
        entry = rpu_vault.get_entry(vault_id)
        if not entry or not entry.get("rpu_path"):
            return {"ok": False, "error": "RPU-Eintrag im Tresor nicht gefunden."}
        d_frames = int(entry.get("frames") or 0)
        d_fps = float(entry.get("fps") or t_fps)
        donor_cuts = list(entry.get("scene_cut_preview") or [])
        donor_summary = {
            "source_type": "vault",
            "name": entry.get("title") or entry.get("filename"),
            "origin": entry.get("origin_label") or "RPU-Tresor",
            "profile": entry.get("profile"),
            "frames": d_frames,
            "fps": d_fps,
            "l5_offsets": entry.get("l5_offsets"),
        }
    elif donor_path and donor_path.is_file():
        d_info = ff.ffprobe(donor_path)
        if d_info is None:
            return {"ok": False, "error": "Spenderdatei konnte nicht gelesen werden."}
        if not d_info.dolby_vision and not d_info.hdr10_plus:
            return {"ok": False, "error": "Spenderdatei enthält weder Dolby Vision noch HDR10+."}
        d_fps = float(d_info.fps or t_fps)
        d_frames = int(round((d_info.duration or 0.0) * d_fps))
        donor_summary = {
            "source_type": "file",
            "name": donor_path.name,
            "origin": d_info.hdr_type,
            "profile": d_info.dv_profile,
            "frames": d_frames,
            "fps": round(d_fps, 3),
            "width": d_info.width,
            "height": d_info.height,
        }
        if auto_sync_intro and abs(t_frames - d_frames) > 0:
            donor_cuts = _detect_video_scene_frames(donor_path, d_fps, probe_seconds=210.0)
    else:
        return {"ok": False, "error": "Weder Spenderdatei noch RPU-Tresor-Eintrag angegeben."}

    frame_diff = t_frames - d_frames
    recommended_offset = 0
    sync_confidence = 1.0 if frame_diff == 0 else 0.0
    sync_method = "exact_frame_match" if frame_diff == 0 else "frame_count_delta"

    if auto_sync_intro and frame_diff != 0:
        target_cuts = _detect_video_scene_frames(target_path, t_fps, probe_seconds=210.0)
        if donor_cuts and target_cuts:
            corr_offset, conf = correlate_scene_cut_offset(donor_cuts, target_cuts)
            if conf >= 0.35:
                recommended_offset = corr_offset
                sync_confidence = conf
                sync_method = "scene_cut_correlation"
            elif abs(frame_diff) <= 750:
                recommended_offset = frame_diff
                sync_confidence = 0.5
                sync_method = "intro_length_difference"
        elif abs(frame_diff) <= 750:
            recommended_offset = frame_diff
            sync_confidence = 0.5
            sync_method = "intro_length_difference"

    warnings: list[str] = []
    if (t_info.codec or "").lower() not in ("hevc", "h265"):
        warnings.append(
            f"Ziel-Codec ist {(t_info.codec or '').upper()} – verlustfreie RPU-Injektion per dovi_tool erfordert HEVC."
        )
    if d_fps and t_fps and abs(d_fps - t_fps) > 0.05:
        warnings.append(f"Abweichende Framerate: Spender {d_fps:.3f} FPS vs. Ziel {t_fps:.3f} FPS.")
    residual_diff = abs((d_frames + recommended_offset) - t_frames)
    if residual_diff > int(t_fps * 3):
        warnings.append(
            f"Nach Intro-Korrektur ({recommended_offset:+d} Frames) verbleiben {residual_diff} Frames Laufzeitdifferenz "
            "(möglicherweise anderer Filmschnitt wie Extended vs. Kinofassung)."
        )

    donor_l5 = donor_summary.get("l5_offsets")
    needs_l5_crop_fix = bool(
        (t_info.height < 2160 and t_info.width >= 3800)
        or (t_info.height < 1080 and 1900 <= t_info.width <= 1920)
        or (donor_l5 and donor_l5 != target_l5)
    )

    return {
        "ok": True,
        "compatible": (t_info.codec or "").lower() in ("hevc", "h265"),
        "donor": donor_summary,
        "target": {
            "name": target_path.name,
            "codec": t_info.codec,
            "hdr_type": t_info.hdr_type,
            "dolby_vision": t_info.dolby_vision,
            "hdr10_plus": t_info.hdr10_plus,
            "frames": t_frames,
            "fps": round(t_fps, 3),
            "resolution": f"{t_info.width}x{t_info.height}",
            "detected_l5_offsets": target_l5,
        },
        "frame_diff": frame_diff,
        "recommended_frame_offset": recommended_offset,
        "sync_confidence": sync_confidence,
        "sync_method": sync_method,
        "needs_l5_crop_fix": needs_l5_crop_fix,
        "warnings": warnings,
    }


def build_rpu_editor_config(
    *,
    donor_frames: int,
    target_frames: int,
    frame_offset: int = 0,
    l5_offsets: Optional[dict] = None,
) -> dict:
    """Erstellt die JSON-Konfiguration für `dovi_tool editor` (Frame-Sync & L5-Active-Area)."""
    cfg: dict = {}
    cur_len = max(0, int(donor_frames))
    remove_ranges: list[str] = []
    duplicates: list[dict] = []

    if frame_offset < 0 and cur_len > 1:
        trim = min(cur_len - 1, abs(int(frame_offset)))
        remove_ranges.append(f"0-{trim - 1}")
        cur_len -= trim
    elif frame_offset > 0:
        pad = int(frame_offset)
        duplicates.append({"source": 0, "offset": 0, "length": pad})
        cur_len += pad

    # Ende an Ziellänge angleichen, falls kleiner Versatz am Abspann existiert
    if target_frames > 0 and cur_len > 0 and cur_len != target_frames:
        diff_tail = target_frames - cur_len
        if diff_tail > 0 and diff_tail <= 5000:
            last_src = max(0, (donor_frames - 1) if frame_offset >= 0 else cur_len - 1)
            duplicates.append({"source": last_src, "offset": last_src, "length": diff_tail})
        elif diff_tail < 0 and abs(diff_tail) < cur_len:
            # Nur kürzen, wenn kein gleichzeitiges Intro-Remove kollidiert; sonst zweistufig
            if not remove_ranges:
                start_rm = max(1, cur_len + diff_tail)
                remove_ranges.append(f"{start_rm}-{cur_len - 1}")

    if remove_ranges:
        cfg["remove"] = remove_ranges
    if duplicates:
        cfg["duplicate"] = duplicates

    if isinstance(l5_offsets, dict):
        left = max(0, int(l5_offsets.get("left", 0) or 0))
        right = max(0, int(l5_offsets.get("right", 0) or 0))
        top = max(0, int(l5_offsets.get("top", 0) or 0))
        bottom = max(0, int(l5_offsets.get("bottom", 0) or 0))
        if left == 0 and right == 0 and top == 0 and bottom == 0:
            cfg["active_area"] = {"crop": True}
        else:
            cfg["active_area"] = {
                "crop": False,
                "presets": [
                    {
                        "id": 0,
                        "left": left,
                        "right": right,
                        "top": top,
                        "bottom": bottom,
                    }
                ],
                "edits": {"all": 0},
            }
    return cfg


def apply_rpu_edits(
    rpu_in: Path,
    rpu_out: Path,
    *,
    donor_frames: int,
    target_frames: int,
    frame_offset: int = 0,
    l5_offsets: Optional[dict] = None,
) -> tuple[bool, str]:
    """Wendet Intro-Frame-Sync und L5-Active-Area-Anpassung per `dovi_tool editor` an."""
    cfg = build_rpu_editor_config(
        donor_frames=donor_frames,
        target_frames=target_frames,
        frame_offset=frame_offset,
        l5_offsets=l5_offsets,
    )
    if not cfg:
        shutil.copyfile(rpu_in, rpu_out)
        return True, ""

    with tempfile.TemporaryDirectory(prefix="vc_rpuedit_") as tmp:
        cfg_path = Path(tmp) / "editor.json"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        res = subprocess.run(
            [config.DOVI_TOOL, "editor", "-i", str(rpu_in), "-j", str(cfg_path), "-o", str(rpu_out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, check=False,
        )
        if res.returncode == 0 and rpu_out.is_file() and rpu_out.stat().st_size > 0:
            return True, ""
        err = (res.stderr or res.stdout or "").strip()[-500:]
        return False, f"RPU-Editor fehlgeschlagen: {err}"


def _mux_injected_hevc(
    injected_hevc: Path,
    target_container: Path,
    out_file: Path,
    fps: float,
    *,
    keep_second_video: bool = False,
    video_tag: str = "",
) -> tuple[bool, str]:
    """Muxt den neuen HEVC-Stream mit Audio, Untertiteln und Kapiteln des Ziels.

    `keep_second_video` behält die zweite Videospur (Dual-Track-Enhancement-Layer).
    `video_tag` setzt den MP4-Codec-Tag der neuen Spur (`dvh1` oder `hvc1`).
    """
    is_mkv = out_file.suffix.lower() in (".mkv", ".mk3d")
    tag = (video_tag or ("hvc1" if keep_second_video else "dvh1")).lower()
    if is_mkv and _mkvmerge_available() and not keep_second_video:
        cmd = [config.MKVMERGE, "-o", str(out_file)]
        if fps and fps > 0:
            cmd += ["--default-duration", f"0:{fps:.6f}fps"]
        cmd += [str(injected_hevc), "--no-video", str(target_container)]
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
        # mkvmerge Exit-Code 0 = OK, 1 = Warnung (Ausgabe dennoch gültig)
        if res.returncode in (0, 1) and out_file.is_file() and out_file.stat().st_size > 0:
            return True, ""

    # Universeller Fallback via FFmpeg (MP4 & MKV, inkl. dvh1/hvc1 Tagging bei MP4)
    mux = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    if fps and fps > 0:
        mux += ["-r", f"{fps:.6f}"]
    mux += [
        "-i", str(injected_hevc),
        "-i", str(target_container),
        "-map", "0:v:0",
    ]
    if keep_second_video:
        mux += ["-map", "1:v:1"]
    mux += [
        "-map", "1:a?",
        "-map", "1:s?",
        "-map", "1:t?",
        "-map_chapters", "1",
        "-map_metadata", "1",
        "-c", "copy",
    ]
    if out_file.suffix.lower() in (".mp4", ".m4v", ".mov"):
        mux += ["-strict", "unofficial", "-tag:v:0", tag]
    mux.append(str(out_file))

    res = subprocess.run(
        mux, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if res.returncode != 0 or not out_file.is_file() or out_file.stat().st_size == 0:
        return False, (res.stderr or "Mux fehlgeschlagen")[-600:]
    if is_mkv:
        ff.add_mkv_statistics_tags(out_file)
    return True, ""


def transfer_dv_to_release(
    target_path: Path,
    *,
    donor_path: Optional[Path] = None,
    vault_id: str = "",
    frame_offset: Optional[int] = None,
    auto_sync_intro: bool = True,
    adjust_l5_crop: bool = True,
    keep_temp_files: bool = False,
    save_to_vault: bool = False,
    replace_target: bool = False,
    output_suffix: str = ".DV",
    status: StatusCb = None,
) -> dict:
    """Überträgt Dolby Vision (oder HDR10+→DV) verlustfrei auf ein Ziel-Release."""
    if not dv.available():
        return {"ok": False, "error": "dovi_tool ist nicht verfügbar."}
    if not target_path.is_file():
        return {"ok": False, "error": "Zieldatei nicht gefunden."}

    t_info = ff.ffprobe(target_path)
    if t_info is None:
        return {"ok": False, "error": "Zieldatei konnte nicht gelesen werden."}
    if (t_info.codec or "").lower() not in ("hevc", "h265"):
        return {
            "ok": False,
            "error": f"Verlustfreie DV-Injektion benötigt ein HEVC-Zielvideo (gefunden: {t_info.codec}).",
        }

    # Arbeitsordner im zentralen WORK_DIR mit Filmname + Datum (erscheint bei keep_temp_files in Daten & Archive)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", target_path.stem).strip("._")[:55] or "dv_job"
    ts_str = time.strftime("%Y-%m-%d_%H-%M")
    work_dir = config.WORK_DIR / f"dv_{safe_stem}_{ts_str}_{uuid.uuid4().hex[:6]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    rpu_raw = work_dir / "extracted.rpu.bin"
    rpu_ready = work_dir / "ready.rpu.bin"
    target_es = work_dir / "target.hevc"
    injected_es = work_dir / "target_dv.hevc"

    suffix_ext = target_path.suffix if target_path.suffix.lower() in (".mkv", ".mp4", ".m4v") else ".mkv"
    out_path = (
        target_path.with_name(f"{target_path.stem}.__dv_tmp__{suffix_ext}")
        if replace_target
        else target_path.with_name(f"{target_path.stem}{output_suffix}{suffix_ext}")
    )

    try:
        # 1. RPU beschaffen (aus Tresor, aus DV-Spender oder per HDR10+->DV aus Spender)
        if vault_id:
            entry = rpu_vault.get_entry(vault_id)
            if not entry or not entry.get("rpu_path"):
                return {"ok": False, "error": "RPU-Eintrag im Tresor nicht gefunden."}
            if status:
                status(f"Lade RPU aus Tresor: {entry.get('title')} …")
            shutil.copyfile(entry["rpu_path"], rpu_raw)
        elif donor_path and donor_path.is_file():
            d_info = ff.ffprobe(donor_path)
            if d_info is None:
                return {"ok": False, "error": "Spenderdatei nicht lesbar."}
            if d_info.dolby_vision:
                mode = 2 if int(d_info.dv_profile or 0) == 7 else 0
                if status:
                    status(f"Extrahiere RPU aus {donor_path.name} …")
                src_c = "av1" if (d_info.codec or "").lower() == "av1" else "hevc"
                if not dv._extract_rpu(donor_path, src_c, rpu_raw, mode=mode):
                    return {"ok": False, "error": "RPU-Extraktion aus Spenderdatei fehlgeschlagen."}
            elif d_info.hdr10_plus:
                if not hdr10plus.available():
                    return {"ok": False, "error": "hdr10plus_tool nicht verfügbar."}
                if status:
                    status(f"Extrahiere HDR10+ aus {donor_path.name} und generiere DV Profil 8.1 …")
                h10_json = work_dir / "hdr10plus.json"
                donor_es = work_dir / "donor.hevc"
                if not hdr10plus._extract_json(donor_path, h10_json, donor_es):
                    return {"ok": False, "error": "HDR10+-Extraktion aus Spender fehlgeschlagen."}
                donor_es.unlink(missing_ok=True)
                ok_gen, err_gen = rpu_vault.generate_rpu_from_hdr10plus_json(
                    h10_json,
                    rpu_raw,
                    peak_nits=int(round(d_info.master_max or 1000)),
                    min_nits=float(d_info.master_min if d_info.master_min is not None else 0.005),
                    max_cll=int(d_info.max_cll or 0),
                    max_fall=int(d_info.max_fall or 0),
                )
                if not ok_gen:
                    return {"ok": False, "error": err_gen}
            else:
                return {"ok": False, "error": "Spenderdatei besitzt weder Dolby Vision noch HDR10+."}
        else:
            return {"ok": False, "error": "Keine gültige Spenderquelle gewählt."}

        if save_to_vault and donor_path and donor_path.is_file():
            rpu_vault.extract_to_vault(donor_path, convert_to_81=True)

        # 2. Kompatibilität, Intro-Sync & L5-Active-Area prüfen
        rpu_info = rpu_vault.inspect_rpu_file(rpu_raw)
        donor_frames = int(rpu_info.get("frames") or 0)
        t_fps = float(t_info.fps or 23.976)
        target_frames = int(round((t_info.duration or 0.0) * t_fps))

        eff_offset = 0
        if frame_offset is not None:
            eff_offset = int(frame_offset)
        elif auto_sync_intro and donor_frames > 0 and donor_frames != target_frames:
            if status:
                status("Prüfe Intro-Versatz und Szenenschnitte …")
            t_cuts = _detect_video_scene_frames(target_path, t_fps, probe_seconds=210.0)
            d_cuts = rpu_info.get("scene_cut_frames") or []
            corr_off, conf = correlate_scene_cut_offset(d_cuts, t_cuts)
            if conf >= 0.35:
                eff_offset = corr_off
            elif abs(target_frames - donor_frames) <= 750:
                eff_offset = target_frames - donor_frames

        l5_target = None
        if adjust_l5_crop:
            crop_str = ff.detect_crop(t_info, samples=2, probe_seconds=1)
            if crop_str:
                cw, ch, cx, cy = [int(x) for x in crop_str.split(":")]
                l5_target = {
                    "left": max(0, cx),
                    "right": max(0, t_info.width - (cx + cw)),
                    "top": max(0, cy),
                    "bottom": max(0, t_info.height - (cy + ch)),
                }
            else:
                l5_target = {"left": 0, "right": 0, "top": 0, "bottom": 0}

        if status:
            status(f"Passe RPU an (Offset: {eff_offset:+d} Frames) …")
        ok_ed, err_ed = apply_rpu_edits(
            rpu_raw,
            rpu_ready,
            donor_frames=donor_frames,
            target_frames=target_frames,
            frame_offset=eff_offset,
            l5_offsets=l5_target,
        )
        if not ok_ed:
            return {"ok": False, "error": err_ed}

        # 3. Ziel-HEVC extrahieren & RPU injizieren
        if status:
            status("Extrahiere Ziel-Videostream …")
        res_es = subprocess.run(
            [
                config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(target_path),
                "-map", "0:v:0", "-c:v", "copy",
                "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(target_es),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if res_es.returncode != 0 or not target_es.is_file():
            return {"ok": False, "error": "Extraktion des Ziel-HEVC-Streams fehlgeschlagen."}

        if status:
            status("Injiziere Dolby Vision RPU in Ziel-Stream …")
        res_inj = subprocess.run(
            [
                config.DOVI_TOOL, "inject-rpu",
                "-i", str(target_es),
                "--rpu-in", str(rpu_ready),
                "-o", str(injected_es),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if res_inj.returncode != 0 or not injected_es.is_file():
            err_i = (res_inj.stderr or res_inj.stdout or "")[-500:]
            return {"ok": False, "error": f"RPU-Injektion fehlgeschlagen: {err_i}"}

        if not keep_temp_files:
            target_es.unlink(missing_ok=True)

        # 4. Zurückmuxen in MKV/MP4 (Audio, Untertitel, Kapitel, HDR10+ bleiben erhalten)
        if status:
            status("Muxe finale Hybrid-Datei …")
        ok_mux, err_mux = _mux_injected_hevc(injected_es, target_path, out_path, t_fps)
        if not ok_mux:
            return {"ok": False, "error": f"Container-Mux fehlgeschlagen: {err_mux}"}

        final_out = out_path
        if replace_target:
            target_path.unlink(missing_ok=True)
            out_path.replace(target_path)
            final_out = target_path

        out_probe = ff.ffprobe(final_out)
        return {
            "ok": True,
            "output_path": config.rel_input(final_out) or final_out.name,
            "output_name": final_out.name,
            "applied_frame_offset": eff_offset,
            "applied_l5_offsets": l5_target,
            "kept_work_dir": str(work_dir) if keep_temp_files else "",
            "hdr_result": out_probe.hdr_type if out_probe else "Dolby Vision 8.1",
            "dolby_vision": bool(out_probe.dolby_vision) if out_probe else True,
            "hdr10_plus": bool(out_probe.hdr10_plus) if out_probe else bool(t_info.hdr10_plus),
        }
    finally:
        if not keep_temp_files:
            shutil.rmtree(work_dir, ignore_errors=True)


def convert_hdr10plus_to_dv_dual_hdr(
    source_path: Path,
    *,
    replace_source: bool = False,
    save_rpu_to_vault: bool = True,
    keep_temp_files: bool = False,
    status: StatusCb = None,
) -> dict:
    """Macht aus einer HDR10+-Datei verlustfrei eine Dual-HDR-Datei (HDR10 + HDR10+ + Dolby Vision 8.1)."""
    return transfer_dv_to_release(
        source_path,
        donor_path=source_path,
        frame_offset=0,
        auto_sync_intro=False,
        adjust_l5_crop=True,
        keep_temp_files=keep_temp_files,
        save_to_vault=save_rpu_to_vault,
        replace_target=replace_source,
        output_suffix=".DV-HDR10Plus",
        status=status,
    )


# hdr10plus_tool speichert MaxScl und AverageMaxrgb in Einheiten von 0,1 cd/m².
# 65535 ist die Obergrenze, die das JSON-Schema als 16-Bit-Wert annimmt (~6553 nits).
_H10_DIST_INDEX = [1, 5, 10, 25, 50, 75, 90, 95, 99]
_H10_RAW_MAX = 65535


def nits_to_hdr10plus_raw(nits: float) -> int:
    """Wandelt cd/m² in die 0,1-Nit-Einheit der HDR10+-JSON um."""
    raw = int(round(max(0.0, float(nits)) * 10.0))
    return max(0, min(_H10_RAW_MAX, raw))


def hdr10plus_raw_to_nits(value: float) -> float:
    """Liest eine HDR10+-JSON-Helligkeit (0,1 cd/m²) als Nits."""
    return max(0.0, float(value or 0)) / 10.0


def _frame_l1_nits(entry: dict) -> Optional[tuple[float, float, float]]:
    """L1 einer exportierten RPU-Frame als (min, avg, max) in Nits."""
    if not isinstance(entry, dict):
        return None
    vdr = entry.get("vdr_dm_data") or {}
    blocks: list = []
    for key in ("cmv40_metadata", "cmv29_metadata"):
        meta = vdr.get(key)
        if isinstance(meta, dict):
            blocks.extend(meta.get("ext_metadata_blocks") or [])
    if isinstance(vdr.get("ext_metadata_blocks"), list):
        blocks.extend(vdr["ext_metadata_blocks"])
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("Level1"), dict):
            level = block["Level1"]
            return (
                dv._pq_to_nits(float(level.get("min_pq") or 0)),
                dv._pq_to_nits(float(level.get("avg_pq") or 0)),
                dv._pq_to_nits(float(level.get("max_pq") or 0)),
            )
    return None


def _distribution_raw(min_n: float, avg_n: float, max_n: float) -> list[int]:
    """Neun Perzentile zwischen Minimum, Durchschnitt und Peak, monoton steigend."""
    anchors = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
    cap = nits_to_hdr10plus_raw(max_n)
    vals: list[int] = []
    for pos in anchors:
        if pos <= 0.5:
            nits = min_n + (avg_n - min_n) * (pos / 0.5)
        else:
            nits = avg_n + (max_n - avg_n) * (((pos - 0.5) / 0.5) ** 0.7)
        raw = min(cap, nits_to_hdr10plus_raw(nits))
        if vals and raw < vals[-1]:
            raw = vals[-1]
        vals.append(raw)
    if vals:
        vals[-1] = cap
    return vals


def rpu_export_to_hdr10plus_json(raw: list) -> dict:
    """Nähert eine HDR10+-Profil-A-JSON aus den L1-Werten einer exportierten RPU.

    Szenen folgen `scene_refresh_flag`. MaxScl ist der Szenenpeak, AverageMaxrgb
    der mittlere L1-Durchschnitt. Eine Bezier-Kurve wird nicht erfunden: der
    Fernseher leitet die Tonkurve aus den Perzentilen ab. Das ist eine Näherung,
    keine Rückrechnung der Dolby-Reshape-Kurve.
    """
    scenes: list[dict] = []
    for idx, entry in enumerate(raw or []):
        if not isinstance(entry, dict):
            continue
        flag = ((entry.get("vdr_dm_data") or {}).get("scene_refresh_flag") == 1)
        if idx == 0 or flag or not scenes:
            scenes.append({"start": idx, "samples": []})
        sample = _frame_l1_nits(entry)
        if sample is not None:
            scenes[-1]["samples"].append(sample)

    scene_info: list[dict] = []
    for scene in scenes:
        samples = scene["samples"]
        if not samples:
            continue
        min_n = min(item[0] for item in samples)
        avg_n = sum(item[1] for item in samples) / len(samples)
        max_n = max(item[2] for item in samples)
        peak_raw = nits_to_hdr10plus_raw(max_n)
        scene_info.append({
            "SceneId": len(scene_info),
            "SequenceFrameIndex": int(scene["start"]),
            "SceneFrameIndex": 0,
            "LuminanceParameters": {
                "AverageMaxrgb": nits_to_hdr10plus_raw(avg_n),
                "MaxScl": [peak_raw, peak_raw, peak_raw],
                "LuminanceDistributions": {
                    "DistributionIndex": list(_H10_DIST_INDEX),
                    "DistributionValues": _distribution_raw(min_n, avg_n, max_n),
                },
            },
            "TargetedSystemDisplayMaximumLuminance": 0,
        })
    return {
        "JSONInfo": {"HDR10plusProfile": "A", "Version": "1.0"},
        "SceneInfo": scene_info,
    }


def hdr10plus_json_to_curve(
    payload: dict,
    *,
    fps: float,
    duration_sec: float = 0.0,
    max_cll: float = 0.0,
    max_fall: float = 0.0,
    max_points: int = 900,
) -> dict:
    """Baut eine Sekundenreihe aus HDR10+-Szenen und stellt sie MaxCLL/MaxFALL gegenüber."""
    parsed: list[tuple[int, float, float]] = []
    for scene in (payload or {}).get("SceneInfo") or []:
        if not isinstance(scene, dict):
            continue
        lum = scene.get("LuminanceParameters") or {}
        maxscl = lum.get("MaxScl") or [0]
        try:
            peak = hdr10plus_raw_to_nits(max(float(x or 0) for x in maxscl))
        except (TypeError, ValueError):
            peak = 0.0
        avg_raw = lum.get("AverageMaxrgb", lum.get("AverageRGB", lum.get("AverageRgb", 0)))
        try:
            avg = hdr10plus_raw_to_nits(float(avg_raw or 0))
        except (TypeError, ValueError):
            avg = 0.0
        parsed.append((int(scene.get("SequenceFrameIndex") or 0), peak, avg))
    if not parsed:
        return {"ok": False, "error": "HDR10+-JSON enthält keine Szenen."}
    parsed.sort(key=lambda item: item[0])
    eff_fps = fps if fps and fps > 1.0 else 23.976
    last_sec = parsed[-1][0] / eff_fps
    total = float(duration_sec or 0.0)
    if total <= last_sec:
        total = last_sec + 1.0
    total_sec = max(1, int(math.ceil(total)))
    step = max(1, math.ceil(total_sec / max(1, max_points)))
    timestamps: list[int] = []
    max_series: list[float] = []
    avg_series: list[float] = []
    cursor = 0
    for sec in range(0, total_sec, step):
        frame_at = int(sec * eff_fps)
        while cursor + 1 < len(parsed) and parsed[cursor + 1][0] <= frame_at:
            cursor += 1
        timestamps.append(sec)
        max_series.append(round(parsed[cursor][1], 1))
        avg_series.append(round(parsed[cursor][2], 1))
    measured_peak = round(max(item[1] for item in parsed), 1)
    measured_fall = round(max(item[2] for item in parsed), 1)
    notes: list[str] = []
    if max_cll:
        if measured_peak > float(max_cll) + 1.0:
            notes.append(
                f"Der höchste Szenenpeak ({measured_peak:.0f} nits) liegt über dem statischen MaxCLL ({float(max_cll):.0f} nits)."
            )
        else:
            notes.append(
                f"Der höchste Szenenpeak ({measured_peak:.0f} nits) bleibt innerhalb des statischen MaxCLL ({float(max_cll):.0f} nits)."
            )
    if max_fall:
        if measured_fall > float(max_fall) + 1.0:
            notes.append(
                f"Der höchste Szenendurchschnitt ({measured_fall:.0f} nits) liegt über MaxFALL ({float(max_fall):.0f} nits)."
            )
        else:
            notes.append(
                f"Der höchste Szenendurchschnitt ({measured_fall:.0f} nits) bleibt innerhalb von MaxFALL ({float(max_fall):.0f} nits)."
            )
    return {
        "ok": True,
        "fps": round(eff_fps, 3),
        "step_seconds": step,
        "scene_count": len(parsed),
        "measured_peak_nits": measured_peak,
        "measured_fall_nits": measured_fall,
        "max_cll_nits": float(max_cll or 0),
        "max_fall_nits": float(max_fall or 0),
        "peak_vs_maxcll": round(measured_peak - float(max_cll), 1) if max_cll else None,
        "fall_vs_maxfall": round(measured_fall - float(max_fall), 1) if max_fall else None,
        "timestamps": timestamps,
        "max_nits": max_series,
        "avg_nits": avg_series,
        "notes": notes,
    }


def rpu_l1_overlay(raw: list, points: int = 240) -> list[float]:
    """L1-Maximum auf eine feste Punktzahl gebracht, damit zwei Filme übereinanderliegen."""
    vals: list[float] = []
    for entry in raw or []:
        sample = _frame_l1_nits(entry)
        vals.append(sample[2] if sample else 0.0)
    if not vals:
        return []
    points = max(1, int(points))
    out: list[float] = []
    for i in range(points):
        start = int(i * len(vals) / points)
        end = int((i + 1) * len(vals) / points)
        chunk = vals[start:max(start + 1, end)]
        out.append(round(max(chunk) if chunk else 0.0, 1))
    return out


def compare_rpu_exports(
    left_raw: list,
    right_raw: list,
    *,
    left_title: str = "",
    right_title: str = "",
) -> dict:
    """Stellt zwei RPU-Exporte gegenüber: Frames, Schnitte, L1, L5 und Intro-Versatz."""
    def _side(raw: list, title: str) -> dict:
        info: dict = {}
        rpu_vault._enrich_from_exported_rpu(raw, info)
        return {
            "title": title,
            "frames": int(info.get("frames") or 0),
            "profile": int(info.get("profile") or 0),
            "el_type": info.get("el_type") or "",
            "cm_version": info.get("cm_version") or "",
            "scene_cuts": int(info.get("scene_cuts") or 0),
            "peak_l1_nits": float(info.get("peak_l1_nits") or 0),
            "avg_l1_nits": float(info.get("avg_l1_nits") or 0),
            "max_cll_nits": float(info.get("max_cll_nits") or 0),
            "max_fall_nits": float(info.get("max_fall_nits") or 0),
            "l5_offsets": info.get("l5_offsets"),
            "l1_max_series": rpu_l1_overlay(raw),
            "_cuts": list(info.get("scene_cut_frames") or []),
        }

    left = _side(left_raw, left_title or "A")
    right = _side(right_raw, right_title or "B")
    offset, confidence = correlate_scene_cut_offset(left.pop("_cuts"), right.pop("_cuts"))
    l5_left = left.get("l5_offsets") if isinstance(left.get("l5_offsets"), dict) else {}
    l5_right = right.get("l5_offsets") if isinstance(right.get("l5_offsets"), dict) else {}
    l5_delta = None
    if l5_left or l5_right:
        l5_delta = {
            key: int((l5_right or {}).get(key) or 0) - int((l5_left or {}).get(key) or 0)
            for key in ("left", "right", "top", "bottom")
        }
    return {
        "ok": True,
        "left": left,
        "right": right,
        "frames_delta": int(right["frames"]) - int(left["frames"]),
        "scene_cuts_delta": int(right["scene_cuts"]) - int(left["scene_cuts"]),
        "peak_l1_delta": round(float(right["peak_l1_nits"]) - float(left["peak_l1_nits"]), 1),
        "avg_l1_delta": round(float(right["avg_l1_nits"]) - float(left["avg_l1_nits"]), 1),
        "l5_delta": l5_delta,
        "suggested_offset": int(offset),
        "offset_confidence": float(confidence),
    }


def write_base_layer_annexb(src: Path, dst: Path) -> dict:
    """Schreibt nur Basisspur-NALs. Enhancement Layer (Typ 63 / Layer > 0) und RPU (Typ 62) entfallen."""
    kept = 0
    dropped_el = 0
    dropped_rpu = 0
    buf = bytearray()
    start_code = b"\x00\x00\x01"

    def _emit(nalu: memoryview | bytes) -> None:
        nonlocal kept, dropped_el, dropped_rpu
        nlen = len(nalu)
        if nlen < 5:
            return
        sc_len = 4 if (nlen >= 4 and nalu[0] == 0 and nalu[1] == 0 and nalu[2] == 0 and nalu[3] == 1) else 3
        if nlen <= sc_len + 1:
            return
        b0 = nalu[sc_len]
        b1 = nalu[sc_len + 1]
        nal_type = (b0 >> 1) & 0x3F
        layer_id = ((b0 & 1) << 5) | ((b1 >> 3) & 0x1F)
        if nal_type == 62:
            dropped_rpu += nlen
            return
        if nal_type == 63 or layer_id > 0:
            dropped_el += nlen
            return
        out.write(nalu)
        kept += nlen

    with src.open("rb") as inp, dst.open("wb") as out:
        while True:
            chunk = inp.read(1024 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            pos = buf.find(start_code)
            if pos < 0:
                if len(buf) > 4:
                    del buf[:-3]
                continue
            if pos > 0 and buf[pos - 1] == 0:
                pos -= 1
            if pos > 0:
                del buf[:pos]
            cursor = 0
            while True:
                nxt = buf.find(start_code, cursor + 3)
                if nxt < 0:
                    break
                nalu_end = nxt - 1 if (nxt > 0 and buf[nxt - 1] == 0) else nxt
                _emit(memoryview(buf)[cursor:nalu_end])
                cursor = nalu_end
            if cursor > 0:
                del buf[:cursor]
        if buf:
            _emit(bytes(buf))
    return {
        "kept_bytes": kept,
        "dropped_el_bytes": dropped_el,
        "dropped_rpu_bytes": dropped_rpu,
    }


def _dv_profile_of(info) -> int:
    if int(getattr(info, "dv_profile", 0) or 0):
        return int(info.dv_profile)
    for track in getattr(info, "video_tracks", None) or []:
        if track.get("dolby_vision") and int(track.get("dv_profile") or 0):
            return int(track.get("dv_profile") or 0)
    return 0


def _dual_enhancement_track(info) -> bool:
    for track in getattr(info, "video_tracks", None) or []:
        if track.get("attached_pic"):
            continue
        if track.get("role") == "enhancement" and track.get("dolby_vision"):
            return True
    return False


def _file_has_hdr10plus(info, path: Path) -> bool:
    if getattr(info, "hdr10_plus", False):
        return True
    text = get_mediainfo_text(path)
    return "HDR10+" in text or "SMPTE ST 2094" in text


def _job_work_dir(source: Path, kind: str) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._")[:55] or "dv_job"
    work = config.WORK_DIR / f"{kind}_{safe_stem}_{time.strftime('%Y-%m-%d_%H-%M')}_{uuid.uuid4().hex[:6]}"
    work.mkdir(parents=True, exist_ok=True)
    return work


def _output_beside(source: Path, suffix: str, replace: bool) -> Path:
    suffix_ext = source.suffix if source.suffix.lower() in (".mkv", ".mp4", ".m4v", ".mov") else ".mkv"
    if replace:
        return source.with_name(f"{source.stem}.__dv_tmp__{suffix_ext}")
    clean = "".join(ch for ch in (suffix or ".DV") if ch.isalnum() or ch in "._- ")[:32] or ".DV"
    if not clean.startswith((".", "_", "-")):
        clean = "." + clean
    return source.with_name(f"{source.stem}{clean}{suffix_ext}")


def _demux_base_hevc(source: Path, dest: Path, *, duration_limit: float = 0.0) -> tuple[bool, str]:
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    if duration_limit and duration_limit > 0:
        cmd += ["-t", f"{float(duration_limit):.3f}"]
    cmd += [
        "-i", str(source),
        "-map", "0:v:0",
        "-c:v", "copy",
        "-bsf:v", "hevc_mp4toannexb",
        "-f", "hevc",
        str(dest),
    ]
    res = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if res.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        return False, (res.stderr or "Extraktion der Basisspur fehlgeschlagen.")[-500:]
    return True, ""


def _export_rpu_file(rpu_path: Path) -> list:
    if not rpu_path.is_file() or rpu_path.stat().st_size <= 0:
        raise ValueError("RPU-Datei ist leer.")
    with tempfile.TemporaryDirectory(prefix="vc_rpuexp_") as tmp:
        exp = Path(tmp) / "rpu.json"
        res = subprocess.run(
            [config.DOVI_TOOL, "export", "-i", str(rpu_path), "-d", f"all={exp}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, check=False,
        )
        if res.returncode != 0 or not exp.is_file():
            err = (res.stderr or res.stdout or "dovi_tool export fehlgeschlagen").strip()[-400:]
            raise ValueError(err)
        raw = json.loads(exp.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("RPU-Export enthält keine Frames.")
    return raw


def _load_rpu_raw(*, vault_id: str = "", path: Optional[Path] = None) -> tuple[list, str]:
    if vault_id:
        entry = rpu_vault.get_entry(vault_id)
        if not entry or not entry.get("rpu_path"):
            raise LookupError("RPU-Eintrag im Tresor nicht gefunden.")
        return _export_rpu_file(Path(entry["rpu_path"])), str(entry.get("title") or vault_id)
    if path is None or not path.is_file():
        raise LookupError("Für jede Seite einen Tresor-Eintrag oder eine Dolby-Vision-Datei wählen.")
    info = ff.ffprobe(path)
    if info is None or not info.dolby_vision:
        raise LookupError(f"{path.name} enthält kein Dolby Vision.")
    profile = _dv_profile_of(info)
    codec = "av1" if (info.codec or "").lower() == "av1" else "hevc"
    with tempfile.TemporaryDirectory(prefix="vc_rpu_cmp_") as tmp:
        rpu = Path(tmp) / "side.rpu.bin"
        if not dv._extract_rpu(path, codec, rpu, mode=2 if profile == 7 else 0):
            raise LookupError(f"RPU aus {path.name} nicht lesbar.")
        return _export_rpu_file(rpu), path.name


def convert_dv_to_hdr10plus(
    source_path: Path,
    *,
    replace_source: bool = False,
    keep_temp_files: bool = False,
    force: bool = False,
    status: StatusCb = None,
) -> dict:
    """Schreibt eine aus der RPU genäherte HDR10+-Schicht in die Datei und behält Dolby Vision.

    Dual-Track-Profil 7: HDR10+ kommt auf die Basisspur, die Enhancement-Spur bleibt.
    Einzelspur: die RPU wird nach der HDR10+-Injektion wieder in dieselbe Spur gelegt.
    Profil 5 hat keine HDR10-Basis und wird abgelehnt.
    """
    if not source_path.is_file():
        return {"ok": False, "error": "Quelldatei nicht gefunden."}
    info = ff.ffprobe(source_path)
    if info is None:
        return {"ok": False, "error": "Quelldatei konnte nicht gelesen werden."}
    codec = (info.codec or "").lower()
    if codec not in ("hevc", "h265"):
        return {
            "ok": False,
            "error": f"HDR10+ schreibt hdr10plus_tool nur in HEVC (gefunden: {info.codec or 'unbekannt'}).",
        }
    profile = _dv_profile_of(info)
    if profile == 5:
        return {
            "ok": False,
            "error": "Profil 5 hat keine HDR10-Basis. HDR10+ braucht die Bildpunkte einer HDR10-Schicht.",
        }
    if not info.dolby_vision:
        return {
            "ok": False,
            "error": "Die Datei enthält kein Dolby Vision, dessen RPU sich nach HDR10+ nähern ließe.",
        }
    if not force and _file_has_hdr10plus(info, source_path):
        return {
            "ok": True,
            "already_present": True,
            "message": "Die Datei trägt bereits HDR10+. Es wurde nichts geschrieben.",
        }
    if not dv.available():
        return {"ok": False, "error": "dovi_tool ist nicht verfügbar."}
    if not hdr10plus.available():
        return {"ok": False, "error": "hdr10plus_tool ist nicht verfügbar."}

    dual = _dual_enhancement_track(info)
    work_dir: Optional[Path] = None
    try:
        work_dir = _job_work_dir(source_path, "h10fromdv")
        out_path = _output_beside(source_path, ".HDR10Plus", replace_source)
        if status:
            status("RPU wird gelesen …")
        rpu = work_dir / "source.rpu.bin"
        if not dv._extract_rpu(source_path, "hevc", rpu, mode=0):
            return {"ok": False, "error": "RPU-Extraktion fehlgeschlagen."}
        try:
            raw = _export_rpu_file(rpu)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"RPU-Export fehlgeschlagen: {exc}"}
        payload = rpu_export_to_hdr10plus_json(raw)
        scenes = payload.get("SceneInfo") or []
        if not scenes:
            return {"ok": False, "error": "Die RPU enthält keine L1-Helligkeit, daraus lässt sich kein HDR10+ bauen."}
        meta = work_dir / "hdr10plus.json"
        meta.write_text(json.dumps(payload), encoding="utf-8")
        if status:
            status("Basisspur wird vorbereitet …")
        base_es = work_dir / "base.hevc"
        ok_es, err_es = _demux_base_hevc(source_path, base_es)
        if not ok_es:
            return {"ok": False, "error": err_es or "Extraktion der Basisspur fehlgeschlagen."}
        h10_es = work_dir / "base_hdr10plus.hevc"
        if status:
            status("HDR10+ wird in die Basisspur geschrieben …")
        if not hdr10plus._run(
            [config.HDR10PLUS_TOOL, "inject", "-i", str(base_es), "-j", str(meta), "-o", str(h10_es)],
            "HDR10+-Injektion aus RPU",
        ):
            return {"ok": False, "error": "HDR10+-Injektion fehlgeschlagen."}
        final_es = h10_es
        if not dual:
            if status:
                status("Dolby-Vision-RPU wird wieder eingesetzt …")
            reinjected = work_dir / "base_hdr10plus_rpu.hevc"
            res_inj = subprocess.run(
                [config.DOVI_TOOL, "inject-rpu", "-i", str(h10_es), "--rpu-in", str(rpu), "-o", str(reinjected)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            if res_inj.returncode != 0 or not reinjected.is_file():
                err_i = (res_inj.stderr or res_inj.stdout or "")[-500:]
                return {"ok": False, "error": f"RPU-Rückinjektion fehlgeschlagen: {err_i}"}
            final_es = reinjected
        if status:
            status("Container wird gemuxt …")
        ok_mux, err_mux = _mux_injected_hevc(
            final_es,
            source_path,
            out_path,
            float(info.fps or 0.0),
            keep_second_video=dual,
            video_tag="hvc1" if dual else "dvh1",
        )
        if not ok_mux:
            return {"ok": False, "error": f"Container-Mux fehlgeschlagen: {err_mux}"}
        final_out = out_path
        if replace_source:
            source_path.unlink(missing_ok=True)
            out_path.replace(source_path)
            final_out = source_path
        curve = hdr10plus_json_to_curve(
            payload,
            fps=float(info.fps or 23.976),
            duration_sec=float(info.duration or 0.0),
            max_cll=float(info.max_cll or 0),
            max_fall=float(info.max_fall or 0),
        )
        if dual:
            message = (
                "HDR10+ liegt auf der Basisspur. Die Dolby-Vision-Spur mit Enhancement Layer "
                "und RPU ist unverändert. Die Helligkeitswerte sind aus der RPU genähert."
            )
        elif profile == 7:
            message = (
                "HDR10+ und die bisherige RPU liegen in derselben HEVC-Spur. "
                "Die Helligkeitswerte sind aus der RPU genähert. "
                "Enhancement-Layer-Pixel in derselben Spur überstehen das nur, wenn das Schreibwerkzeug sie mitkopiert. "
                "Einen Stream ohne diese Pixel schreibt „Profil 7 → TV-Stream“."
            )
        else:
            message = (
                "HDR10+ und die bisherige RPU liegen in derselben HEVC-Spur. "
                "Die Helligkeitswerte sind aus der RPU genähert."
            )
        return {
            "ok": True,
            "output_path": config.rel_input(final_out) or final_out.name,
            "output_name": final_out.name,
            "approximation": True,
            "already_present": False,
            "scenes": len(scenes),
            "el_kept": dual,
            "dolby_vision_kept": True,
            "measured_peak_nits": curve.get("measured_peak_nits") or 0,
            "message": message,
            "kept_work_dir": str(work_dir) if keep_temp_files else "",
        }
    finally:
        if work_dir is not None and not keep_temp_files:
            shutil.rmtree(work_dir, ignore_errors=True)


def make_profile7_tv_stream(
    source_path: Path,
    *,
    replace_source: bool = False,
    keep_temp_files: bool = False,
    status: StatusCb = None,
) -> dict:
    """Macht aus Profil 7 einen einzelnen Profil-8.1-Stream. Die Enhancement-Layer-Pixel entfallen."""
    if not source_path.is_file():
        return {"ok": False, "error": "Quelldatei nicht gefunden."}
    info = ff.ffprobe(source_path)
    if info is None:
        return {"ok": False, "error": "Quelldatei konnte nicht gelesen werden."}
    if (info.codec or "").lower() not in ("hevc", "h265"):
        return {
            "ok": False,
            "error": f"Der TV-Stream braucht eine HEVC-Basisspur (gefunden: {info.codec or 'unbekannt'}).",
        }
    profile = _dv_profile_of(info)
    if profile != 7:
        shown = profile or "ohne Profil"
        return {
            "ok": False,
            "error": f"Ein einzelner TV-Stream entsteht aus Profil 7. Diese Datei ist Profil {shown}.",
        }
    if not dv.available():
        return {"ok": False, "error": "dovi_tool ist nicht verfügbar."}

    dual = _dual_enhancement_track(info)
    where = "der zweiten Spur" if dual else "derselben Spur"
    warning = (
        f"Die Enhancement-Layer-Pixel aus {where} entfallen. Die neue Datei enthält nur "
        "die HDR10-Basisschicht plus eine Profil-8.1-RPU."
    )
    work_dir: Optional[Path] = None
    try:
        work_dir = _job_work_dir(source_path, "dv81")
        out_path = _output_beside(source_path, ".DV81", replace_source)
        injected = work_dir / "tv.hevc"
        dropped: dict = {"dropped_el_bytes": 0}
        used_fast_path = False

        # dovi_tool >= 2.3.4 Fast-Path für Single-Track-MKV: konvertiert direkt aus MKV
        # nach Profil 8.1 (--discard) ohne 50-GB-Zwischendateien (full.hevc + base.hevc).
        if not dual and source_path.suffix.lower() == ".mkv" and dv.supports_mkv_fast_path():
            if status:
                status("Direkte Profil-7→8.1-Konvertierung aus MKV (dovi_tool Fast-Path) …")
            ok_fast, err_fast = dv.convert_mkv_to_81_es(source_path, injected, discard_el=True)
            if ok_fast:
                used_fast_path = True
                logger.info("DV Profil 7→8.1 per dovi_tool MKV-Fast-Path erfolgreich (%s)", source_path.name)
            else:
                logger.info("dovi_tool MKV-Fast-Path fehlgeschlagen – Fallback auf Standardpfad: %s", err_fast)

        if not used_fast_path:
            if status:
                status("Profil-7-RPU wird nach 8.1 gewandelt …")
            rpu = work_dir / "p81.rpu.bin"
            if not dv._extract_rpu(source_path, "hevc", rpu, mode=2):
                return {"ok": False, "error": "RPU-Extraktion (Profil 7 → 8.1) fehlgeschlagen."}
            if status:
                status("Basisspur wird ohne Enhancement Layer geschrieben …")
            full_es = work_dir / "full.hevc"
            base_es = work_dir / "base.hevc"
            ok_es, err_es = _demux_base_hevc(source_path, full_es)
            if not ok_es:
                return {"ok": False, "error": err_es or "Extraktion der Basisspur fehlgeschlagen."}
            dropped = write_base_layer_annexb(full_es, base_es)
            full_es.unlink(missing_ok=True)
            if status:
                status("Profil-8.1-RPU wird in die Basisspur gelegt …")
            res_inj = subprocess.run(
                [config.DOVI_TOOL, "inject-rpu", "-i", str(base_es), "--rpu-in", str(rpu), "-o", str(injected)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            if res_inj.returncode != 0 or not injected.is_file():
                err_i = (res_inj.stderr or res_inj.stdout or "")[-500:]
                return {"ok": False, "error": f"RPU-Injektion fehlgeschlagen: {err_i}"}
        if status:
            status("TV-Stream wird gemuxt …")
        ok_mux, err_mux = _mux_injected_hevc(
            injected,
            source_path,
            out_path,
            float(info.fps or 0.0),
            keep_second_video=False,
            video_tag="dvh1",
        )
        if not ok_mux:
            return {"ok": False, "error": f"Container-Mux fehlgeschlagen: {err_mux}"}
        final_out = out_path
        if replace_source:
            source_path.unlink(missing_ok=True)
            out_path.replace(source_path)
            final_out = source_path
        return {
            "ok": True,
            "output_path": config.rel_input(final_out) or final_out.name,
            "output_name": final_out.name,
            "el_dropped": True,
            "dual_track": dual,
            "fast_path": used_fast_path,
            "dropped_el_bytes": int(dropped.get("dropped_el_bytes") or 0),
            "profile": "8.1",
            "warning": warning,
            "message": warning,
            "kept_work_dir": str(work_dir) if keep_temp_files else "",
        }
    finally:
        if work_dir is not None and not keep_temp_files:
            shutil.rmtree(work_dir, ignore_errors=True)


def load_dynamic_luminance_curve(path: Path, *, duration_limit: float = 0.0) -> dict:
    """HDR10+-Kurve auf der Filmzeitachse, sonst die aus der RPU genäherte Kurve."""
    if not path.is_file():
        return {"ok": False, "error": "Datei nicht gefunden."}
    info = ff.ffprobe(path)
    if info is None:
        return {"ok": False, "error": "Datei konnte nicht gelesen werden."}
    fps = float(info.fps or 23.976)
    duration = float(info.duration or 0.0)
    if duration_limit and duration_limit > 0:
        duration = min(duration, float(duration_limit)) if duration else float(duration_limit)
    max_cll = float(info.max_cll or 0)
    max_fall = float(info.max_fall or 0)
    profile = _dv_profile_of(info)
    payload = None
    source = ""
    note = ""

    if _file_has_hdr10plus(info, path) and (info.codec or "").lower() in ("hevc", "h265"):
        if not hdr10plus.available():
            return {"ok": False, "error": "hdr10plus_tool ist nicht verfügbar."}
        with tempfile.TemporaryDirectory(prefix="vc_h10curve_") as tmp:
            tmp_dir = Path(tmp)
            meta = tmp_dir / "hdr10plus.json"
            work_es = tmp_dir / "src.hevc"
            extracted = False
            if duration_limit and duration_limit > 0:
                ok_es, _err = _demux_base_hevc(path, work_es, duration_limit=duration_limit)
                extracted = ok_es and hdr10plus._run(
                    [config.HDR10PLUS_TOOL, "extract", str(work_es), "-o", str(meta)],
                    "HDR10+-Kurve",
                )
            else:
                extracted = hdr10plus._extract_json(path, meta, work_es)
            if extracted:
                try:
                    payload = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
                    source = "hdr10plus"
                    note = "Kurve aus der HDR10+-JSON der Datei."
                except (OSError, ValueError):
                    payload = None

    if payload is None and info.dolby_vision and profile != 5:
        if not dv.available():
            return {"ok": False, "error": "dovi_tool ist nicht verfügbar."}
        with tempfile.TemporaryDirectory(prefix="vc_rpucurve_") as tmp:
            rpu = Path(tmp) / "curve.rpu.bin"
            limit = float(duration_limit) if duration_limit and duration_limit > 0 else None
            if not dv._extract_rpu(path, "hevc", rpu, mode=2 if profile == 7 else 0, duration_limit=limit):
                return {"ok": False, "error": "RPU-Extraktion für die Helligkeitskurve fehlgeschlagen."}
            try:
                raw = _export_rpu_file(rpu)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return {"ok": False, "error": f"RPU-Export fehlgeschlagen: {exc}"}
        payload = rpu_export_to_hdr10plus_json(raw)
        source = "rpu_approximation"
        note = "Genähert aus der Dolby-Vision-RPU (L1 pro Szene). Die Datei hat keine HDR10+-JSON."

    if payload is None:
        if profile == 5:
            return {
                "ok": False,
                "error": "Profil 5 hat keine HDR10-Basis. Eine HDR10+-Kurve braucht HDR10-Bildpunkte oder eine HDR10+-JSON.",
            }
        return {"ok": False, "error": "Weder HDR10+ noch eine Dolby-Vision-RPU gefunden."}

    curve = hdr10plus_json_to_curve(
        payload, fps=fps, duration_sec=duration, max_cll=max_cll, max_fall=max_fall,
    )
    if curve.get("ok"):
        curve["source"] = source
        curve["note"] = note
    return curve


def compare_rpu_sources(
    *,
    left_vault_id: str = "",
    right_vault_id: str = "",
    left_path: Optional[Path] = None,
    right_path: Optional[Path] = None,
) -> dict:
    """Vergleicht zwei Tresoreinträge oder zwei Dolby-Vision-Dateien."""
    if not (left_vault_id or left_path) or not (right_vault_id or right_path):
        return {"ok": False, "error": "Für jede Seite einen Tresor-Eintrag oder eine Dolby-Vision-Datei wählen."}
    if not dv.available():
        return {"ok": False, "error": "dovi_tool ist nicht verfügbar."}
    try:
        left_raw, left_title = _load_rpu_raw(vault_id=left_vault_id, path=left_path)
        right_raw, right_title = _load_rpu_raw(vault_id=right_vault_id, path=right_path)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"RPU-Vergleich fehlgeschlagen: {exc}"}
    return compare_rpu_exports(
        left_raw, right_raw, left_title=left_title, right_title=right_title,
    )
