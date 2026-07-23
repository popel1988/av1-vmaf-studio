"""Einfacher Video-Editor: Segmente schneiden, sortieren, exportieren.

Zwei Export-Modi:
  - remux: Concat-Demuxer mit inpoint/outpoint (-c copy, Keyframe-genau)
  - encode: filter_complex trim/atrim + concat + Re-Encode
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from . import config, ffmpeg_utils as ff
from . import remux

logger = logging.getLogger("vcompress.editor")


def resolve_path(rel: str) -> Optional[Path]:
    """Medienpfad oder ``upload:<name>`` sicher auflösen."""
    p = str(rel or "").strip()
    if p.startswith("upload:"):
        return remux._abs_external(p)
    return config.resolve_input(p)


def normalize_segments(raw: list) -> tuple[list[dict], str]:
    """Segmente prüfen/normalisieren. Liefert (segments, error)."""
    if not raw:
        return [], "Mindestens ein Segment nötig."
    out: list[dict] = []
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict):
            return [], f"Segment {i + 1}: ungültig."
        path = str(s.get("path") or "").strip()
        if not path:
            return [], f"Segment {i + 1}: kein Pfad."
        target = resolve_path(path)
        if target is None or not target.is_file():
            return [], f"Segment {i + 1}: Datei nicht gefunden ({path})."
        start = remux.parse_time(s.get("start"))
        end = remux.parse_time(s.get("end"))
        if end <= 0:
            end = remux.probe_duration(target)
        if end <= start:
            return [], f"Segment {i + 1}: Ende muss nach dem Start liegen."
        aidx = s.get("audio_index", 0)
        try:
            aidx = int(aidx)
        except (TypeError, ValueError):
            aidx = 0
        title = str(s.get("title") or "").strip() or f"Clip {i + 1}"
        out.append({
            "path": path,
            "abs": target,
            "start": float(start),
            "end": float(end),
            "title": title,
            "audio_index": aidx,
            "mute": bool(s.get("mute")),
        })
    return out, ""


def total_duration(segments: list[dict]) -> float:
    return sum(max(0.0, float(s["end"]) - float(s["start"])) for s in segments)


def chapters_from_segments(segments: list[dict]) -> list[dict]:
    """Kapitelmarken an Segmentgrenzen (Timeline-Zeit)."""
    chapters, acc = [], 0.0
    for s in segments:
        dur = max(0.001, float(s["end"]) - float(s["start"]))
        chapters.append({
            "start": acc,
            "end": acc + dur,
            "title": s.get("title") or "Clip",
        })
        acc += dur
    return chapters


def check_remux_compat(segments: list[dict]) -> dict:
    """Kompatibilität für verlustfreien Concat-Export (wie concat_compat)."""
    files = [str(s["abs"]) for s in segments]
    # Unique files in order of first appearance – same file twice is fine.
    seen, uniq = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    if len(uniq) < 2:
        # Einzeln / gleiche Quelle: immer „kompatibel“ (ein Stream-Layout).
        return {"compatible": True, "warnings": [], "streams": []}
    return remux.concat_compat(uniq)


def build_editor_remux_cmd(segments: list[dict], output: Path,
                           work_dir: Path,
                           add_chapters: bool = True) -> tuple[list, str]:
    """Verlustfreier Export via Concat-Demuxer (inpoint/outpoint).

    Schnitte sind Keyframe-genau (FFmpeg sucht den nächsten Keyframe).
    """
    if not segments:
        return [], "Keine Segmente."
    work_dir.mkdir(parents=True, exist_ok=True)
    listfile = work_dir / f"editor_{uuid.uuid4().hex[:8]}.txt"
    lines = []
    for s in segments:
        p = str(s["abs"]).replace("'", "'\\''")
        lines.append(f"file '{p}'")
        lines.append(f"inpoint {float(s['start']):.3f}")
        lines.append(f"outpoint {float(s['end']):.3f}")
    try:
        listfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return [], f"Konnte Liste nicht schreiben: {e}"

    meta_path = None
    if add_chapters and len(segments) >= 1:
        meta_path = remux.write_chapter_meta(
            chapters_from_segments(segments), work_dir)

    cmd = [config.FFMPEG, "-y", "-hide_banner", "-f", "concat", "-safe", "0",
           "-i", str(listfile)]
    if meta_path:
        cmd += ["-i", str(meta_path), "-map_chapters", "1"]
    cmd += ["-map", "0", "-c", "copy", "-reset_timestamps", "1",
            "-progress", "pipe:1", "-nostats", str(output)]
    return cmd, ""


def build_editor_encode_cmd(
    segments: list[dict],
    output: Path,
    platform: str = "cpu",
    codec: str = "av1",
    cq: int = 30,
    audio_codec: str = "aac",
    audio_bitrate: int = 192,
    burn_subs: bool = False,
    sub_index: int = -1,
) -> tuple[list, str]:
    """Re-Encode-Export: trim/atrim je Segment, dann concat-Filter."""
    if not segments:
        return [], "Keine Segmente."
    enc = ff.encoder_name(platform, codec)
    backend = ff.encoder_backend(platform)
    n = len(segments)

    # Unique inputs – map each segment to input index.
    path_to_idx: dict[str, int] = {}
    inputs: list[Path] = []
    for s in segments:
        key = str(s["abs"])
        if key not in path_to_idx:
            path_to_idx[key] = len(inputs)
            inputs.append(s["abs"])

    cmd = [config.FFMPEG, "-y", "-hide_banner"]
    if backend == "vaapi":
        cmd += ["-vaapi_device", "/dev/dri/renderD128"]
    for inp in inputs:
        cmd += ["-i", str(inp)]

    parts: list[str] = []
    concat_pads: list[str] = []
    _ = (burn_subs, sub_index)  # reserviert für künftiges UT-Burn-in
    for i, s in enumerate(segments):
        ii = path_to_idx[str(s["abs"])]
        start, end = float(s["start"]), float(s["end"])
        dur = max(0.001, end - start)
        vlabel = f"v{i}"
        alabel = f"a{i}"
        parts.append(
            f"[{ii}:v:0]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=PTS-STARTPTS[{vlabel}]")

        aidx = int(s.get("audio_index") if s.get("audio_index") is not None else 0)
        if s.get("mute") or aidx < 0:
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[{alabel}]")
        else:
            parts.append(
                f"[{ii}:a:{aidx}]atrim=start={start:.3f}:end={end:.3f},"
                f"asetpts=PTS-STARTPTS[{alabel}]")
        concat_pads.append(f"[{vlabel}][{alabel}]")

    filt = ";".join(parts)
    filt += ";" + "".join(concat_pads) + f"concat=n={n}:v=1:a=1[vc][a]"
    if backend == "vaapi":
        filt += ";[vc]format=nv12,hwupload[v]"
    else:
        filt += ";[vc]setsar=1[v]"

    cmd += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]", "-c:v", enc]
    cq = int(cq or 30)
    if backend == "nvenc":
        cmd += ["-rc", "vbr", "-cq", str(cq), "-preset", "p5"]
    elif backend == "qsv":
        cmd += ["-global_quality", str(cq)]
    elif backend == "vaapi":
        cmd += ["-rc_mode", "CQP", "-qp", str(cq)]
    else:
        cmd += ["-crf", str(cq)]
        if enc == "libsvtav1":
            cmd += ["-preset", "6"]
        elif enc == "libx265":
            cmd += ["-preset", "medium"]
        elif enc == "libx264":
            cmd += ["-preset", "medium"]

    ac = (audio_codec or "aac").lower()
    if ac not in ("aac", "opus", "ac3", "eac3", "flac"):
        ac = "aac"
    cmd += ["-c:a", ac]
    if ac != "flac":
        cmd += ["-b:a", f"{int(audio_bitrate or 192)}k"]
    cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    return cmd, ""


def probe_source(rel: str) -> tuple[Optional[dict], str]:
    """Kurze Probe-Info für die Editor-UI."""
    target = resolve_path(rel)
    if target is None or not target.is_file():
        return None, "Datei nicht gefunden"
    data, err = ff.probe_streams(target)
    if data is None:
        return None, err or "ffprobe fehlgeschlagen"
    info, _ = ff.probe_with_error(target)
    duration = float(getattr(info, "duration", 0) or 0) if info else 0.0
    if not duration:
        duration = remux.probe_duration(target)
    data["path"] = rel
    data["name"] = target.name
    data["duration"] = duration
    data["size"] = target.stat().st_size if target.exists() else 0
    data["size_human"] = ff.human_size(data["size"])
    return data, ""
