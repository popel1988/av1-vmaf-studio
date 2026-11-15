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

2. **Cross-Release DV-Transfer (Hybrid-Builder) & HDR10+ → DV Generator**:
   - Kompatibilitätsprüfung zweier Releases inkl. automatischer Intro-Offset-Erkennung
     (Szenenschnitt-Korrelation über `scene_refresh_flag`) und L5-Active-Area-Schutz
     bei gecroppten Zieldateien.
   - Framegenaue RPU-Anpassung (`dovi_tool editor`: Intro-Trim/Padding, Endlängen-Sync,
     L5-Offsets).
   - Verlustfreier Dual-HDR-Konverter: Erzeugt aus HDR10+ eine Dolby-Vision-8.1-RPU
     und bettet sie zusätzlich ein (HDR10 + HDR10+ + Dolby Vision gleichzeitig).
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

    has_dv = bool(info.dolby_vision)
    has_el = bool(base_track.get("dv_el") or el_track is not None or int(info.dv_profile or 0) == 7)
    has_bl = bool(base_track.get("dv_bl") is not False)
    has_rpu = bool(base_track.get("dv_rpu") or has_dv)

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
        "is_hdr": bool(info.is_hdr),
        "hdr_format": info.hdr_type or "SDR",
        "profile": int(rpu_meta.get("profile") or info.dv_profile or 0),
        "profile_label": base_track.get("dv_profile_label") or (str(info.dv_profile) if info.dv_profile else ""),
        "level": base_track.get("dv_level"),
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
    return summary


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
) -> tuple[bool, str]:
    """Muxt den neuen DV-HEVC-Stream mit allen Audio-/Untertitel-/Kapitel-/Font-Spuren des Ziels."""
    is_mkv = out_file.suffix.lower() in (".mkv", ".mk3d")
    if is_mkv and _mkvmerge_available():
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

    # Universeller Fallback via FFmpeg (MP4 & MKV, inkl. dvh1/dvhe Tagging bei MP4)
    mux = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    if fps and fps > 0:
        mux += ["-r", f"{fps:.6f}"]
    mux += [
        "-i", str(injected_hevc),
        "-i", str(target_container),
        "-map", "0:v:0",
        "-map", "1:a?",
        "-map", "1:s?",
        "-map", "1:t?",
        "-map_chapters", "1",
        "-map_metadata", "1",
        "-c", "copy",
    ]
    if out_file.suffix.lower() in (".mp4", ".m4v", ".mov"):
        mux += ["-strict", "unofficial", "-tag:v", "dvh1"]
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
