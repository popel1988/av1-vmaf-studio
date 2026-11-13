"""Video-Editor: Segmente schneiden, sortieren, exportieren.

Zwei Export-Modi:
  - remux: Concat-Demuxer mit inpoint/outpoint (-c copy, Keyframe-genau)
  - encode: filter_complex trim/atrim + concat + Re-Encode
    (Fades, Tempo, Crop/Scale, Schwarzclips, UT-Burn-in, Überblendung)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from . import config, ffmpeg_utils as ff
from . import remux

logger = logging.getLogger("vcompress.editor")

_GEN_KINDS = ("black", "silence")
_TRANSITIONS = (
    "fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight",
    "slideleft", "slideright", "smoothleft", "smoothright",
)
_ROTATE_FILTERS = {
    90: "transpose=1",
    180: "hflip,vflip",
    270: "transpose=2",
}
_BLACK_RE = re.compile(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")
_SCENE_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_CACHE = None  # lazy: DATA_DIR/editor_cache


def _cache_dir() -> Path:
    global _CACHE
    if _CACHE is None:
        _CACHE = config.DATA_DIR / "editor_cache"
        _CACHE.mkdir(parents=True, exist_ok=True)
    return _CACHE


def resolve_path(rel: str) -> Optional[Path]:
    """Medienpfad oder ``upload:<name>`` sicher auflösen."""
    p = str(rel or "").strip()
    if p.startswith("upload:"):
        return remux._abs_external(p)
    return config.resolve_input(p)


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _audio_list(raw, fallback: int) -> list[int]:
    """Gewählte Tonspuren normalisieren (Reihenfolge = Ausgabereihenfolge)."""
    out: list[int] = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v >= 0 and v not in out:
                out.append(v)
        return out
    if fallback >= 0:
        return [int(fallback)]
    return []


def _audio_tracks(path: Optional[Path]) -> list[dict]:
    """Tonspuren einer Datei (für Anzahl und Sprach-Metadaten)."""
    if not path:
        return []
    data, _ = ff.probe_streams(path)
    if not data:
        return []
    tracks = data.get("audio") or []
    return tracks if isinstance(tracks, list) else []


def _clip_out_dur(s: dict) -> float:
    """Dauer auf der Ausgabe-Timeline (nach Tempo, bei freeze = Standbild-Dauer)."""
    if str(s.get("kind") or "").lower() == "freeze":
        return max(0.2, min(60.0, _f(s.get("end"), 3.0) - _f(s.get("start"), 0.0)))
    raw = max(0.0, float(s["end"]) - float(s["start"]))
    speed = max(0.25, min(4.0, _f(s.get("speed"), 1.0) or 1.0))
    return raw / speed if speed else raw


def normalize_segments(raw: list) -> tuple[list[dict], str]:
    """Segmente prüfen/normalisieren. Liefert (segments, error)."""
    if not raw:
        return [], "Mindestens ein Segment nötig."
    out: list[dict] = []
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict):
            return [], f"Segment {i + 1}: ungültig."
        kind = str(s.get("kind") or "media").lower().strip()
        if kind not in ("media", "black", "silence", "freeze"):
            kind = "media"
        path = str(s.get("path") or "").strip()
        target: Optional[Path] = None
        start = remux.parse_time(s.get("start"))
        end = remux.parse_time(s.get("end"))
        if kind in _GEN_KINDS:
            if end <= start:
                end = start + 3.0
            if end - start < 0.05:
                return [], f"Segment {i + 1}: generierter Clip zu kurz."
        else:
            if not path:
                return [], f"Segment {i + 1}: kein Pfad."
            target = resolve_path(path)
            if target is None or not target.is_file():
                return [], f"Segment {i + 1}: Datei nicht gefunden ({path})."
            if kind == "freeze":
                if end <= start:
                    end = start + 3.0
            else:
                if end <= 0:
                    end = remux.probe_duration(target)
                if end <= start:
                    return [], f"Segment {i + 1}: Ende muss nach dem Start liegen."
        aidx = s.get("audio_index", 0)
        try:
            aidx = int(aidx)
        except (TypeError, ValueError):
            aidx = 0
        alist = _audio_list(s.get("audio_indexes"), aidx)
        mute = bool(s.get("mute")) or kind in _GEN_KINDS or kind == "freeze"
        if mute:
            alist = []
        elif not alist:
            mute = True
        sidx = s.get("sub_index", -1)
        try:
            sidx = int(sidx)
        except (TypeError, ValueError):
            sidx = -1
        slist = [] if (kind in _GEN_KINDS or kind == "freeze") else _audio_list(s.get("sub_indexes"), sidx)
        title = str(s.get("title") or "").strip() or f"Clip {i + 1}"
        crop = str(s.get("crop") or "").strip()
        delogo = str(s.get("delogo") or "").strip()
        text_ov = str(s.get("text_overlay") or "").strip()[:140]
        text_pos = str(s.get("text_pos") or "bottom").lower().strip()
        if text_pos not in ("bottom", "top", "center", "tl", "tr", "bl", "br"):
            text_pos = "bottom"
        look_preset = str(s.get("look_preset") or "none").lower().strip()
        if look_preset not in ("none", "night", "vivid", "clean", "bw", "cinema"):
            look_preset = "none"
        try:
            scale = int(s.get("scale") or 0)
        except (TypeError, ValueError):
            scale = 0
        try:
            rotate = int(s.get("rotate") or 0)
        except (TypeError, ValueError):
            rotate = 0
        if rotate not in (0, 90, 180, 270):
            rotate = 0
        out.append({
            "path": path,
            "abs": target,
            "kind": kind,
            "start": float(start),
            "end": float(end),
            "title": title,
            "audio_index": alist[0] if alist else -1,
            "audio_indexes": alist,
            "mute": mute,
            "sub_index": slist[0] if slist else -1,
            "sub_indexes": slist,
            "burn_subs": bool(s.get("burn_subs")) and (slist[0] if slist else sidx) >= 0,
            "fade_in": max(0.0, min(30.0, _f(s.get("fade_in")))),
            "fade_out": max(0.0, min(30.0, _f(s.get("fade_out")))),
            "speed": max(0.25, min(4.0, _f(s.get("speed"), 1.0) or 1.0)),
            "crop": crop,
            "delogo": delogo,
            "text_overlay": text_ov,
            "text_pos": text_pos,
            "look_preset": look_preset,
            "voice_enhance": bool(s.get("voice_enhance")),
            "reverse": bool(s.get("reverse")),
            "scale": max(0, min(4320, scale)),
            "rotate": rotate,
            "hflip": bool(s.get("hflip")),
            "vflip": bool(s.get("vflip")),
            "volume_db": max(-30.0, min(24.0, _f(s.get("volume_db"), 0.0))),
            "loudnorm": bool(s.get("loudnorm")),
        })
    return out, ""


def total_duration(segments: list[dict], crossfade: float = 0.0) -> float:
    acc = 0.0
    xf = max(0.0, float(crossfade or 0))
    for i, s in enumerate(segments):
        acc += _clip_out_dur(s)
        if i > 0 and xf > 0:
            acc -= min(xf, _clip_out_dur(s) * 0.45, _clip_out_dur(segments[i - 1]) * 0.45)
    return max(0.0, acc)


def chapters_from_segments(segments: list[dict], crossfade: float = 0.0) -> list[dict]:
    """Kapitelmarken an Segmentgrenzen (Timeline-Zeit)."""
    chapters, acc = [], 0.0
    xf = max(0.0, float(crossfade or 0))
    for i, s in enumerate(segments):
        dur = max(0.001, _clip_out_dur(s))
        if i > 0 and xf > 0:
            acc -= min(xf, dur * 0.45, _clip_out_dur(segments[i - 1]) * 0.45)
            acc = max(0.0, acc)
        chapters.append({
            "start": acc,
            "end": acc + dur,
            "title": s.get("title") or "Clip",
        })
        acc += dur
    return chapters


def segment_needs_encode(s: dict) -> bool:
    if (s.get("kind") or "media") in _GEN_KINDS or s.get("kind") == "freeze":
        return True
    if _f(s.get("fade_in")) > 0 or _f(s.get("fade_out")) > 0:
        return True
    if abs(_f(s.get("speed"), 1.0) - 1.0) > 0.001:
        return True
    if s.get("crop") or s.get("delogo") or s.get("text_overlay") or s.get("reverse") or int(s.get("scale") or 0) > 0:
        return True
    if (s.get("look_preset") or "none") != "none" or s.get("voice_enhance"):
        return True
    if int(s.get("rotate") or 0) in (90, 180, 270) or s.get("hflip") or s.get("vflip"):
        return True
    if abs(_f(s.get("volume_db"), 0.0)) > 0.05 or s.get("loudnorm"):
        return True
    if s.get("burn_subs"):
        return True
    return False


def any_needs_encode(segments: list[dict], crossfade: float = 0.0,
                     overlays: Optional[list[dict]] = None,
                     bgm_path: str = "", pad_mode: str = "black",
                     aspect_mode: str = "source") -> bool:
    if float(crossfade or 0) > 0.01:
        return True
    if overlays or bgm_path:
        return True
    if (pad_mode or "black") != "black" or (aspect_mode or "source") != "source":
        return True
    return any(segment_needs_encode(s) for s in segments)


def check_remux_compat(segments: list[dict]) -> dict:
    """Kompatibilität für verlustfreien Concat-Export (wie concat_compat)."""
    if any_needs_encode(segments):
        return {
            "compatible": False,
            "warnings": [
                "Fades, Tempo, Crop/Scale, Schwarzclips oder UT-Burn-in "
                "brauchen den Encode-Modus.",
            ],
            "streams": [],
        }
    if len({tuple(s.get("audio_indexes") or []) for s in segments}) > 1:
        return {
            "compatible": False,
            "warnings": [
                "Die Clips übernehmen unterschiedliche Tonspuren – "
                "verlustfreies Kopieren kann nur eine Streamstruktur.",
            ],
            "streams": [],
        }
    if len({tuple(s.get("sub_indexes") or []) for s in segments}) > 1:
        return {
            "compatible": False,
            "warnings": [
                "Die Clips übernehmen unterschiedliche Untertitel – "
                "verlustfreies Kopieren kann nur eine Streamstruktur.",
            ],
            "streams": [],
        }
    files = [str(s["abs"]) for s in segments if s.get("abs")]
    seen, uniq = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    if len(uniq) < 2:
        return {"compatible": True, "warnings": [], "streams": []}
    return remux.concat_compat(uniq)


def segment_to_spec(s: dict) -> dict:
    """Queue-JSON ohne Path-Objekt."""
    return {
        "path": s.get("path") or "",
        "kind": s.get("kind") or "media",
        "start": s["start"],
        "end": s["end"],
        "title": s.get("title") or "",
        "audio_index": s.get("audio_index", 0),
        "audio_indexes": list(s.get("audio_indexes") or []),
        "mute": bool(s.get("mute")),
        "sub_index": int(s.get("sub_index", -1)),
        "sub_indexes": list(s.get("sub_indexes") or []),
        "burn_subs": bool(s.get("burn_subs")),
        "fade_in": _f(s.get("fade_in")),
        "fade_out": _f(s.get("fade_out")),
        "speed": _f(s.get("speed"), 1.0) or 1.0,
        "crop": s.get("crop") or "",
        "delogo": s.get("delogo") or "",
        "text_overlay": s.get("text_overlay") or "",
        "text_pos": s.get("text_pos") or "bottom",
        "look_preset": s.get("look_preset") or "none",
        "voice_enhance": bool(s.get("voice_enhance")),
        "reverse": bool(s.get("reverse")),
        "scale": int(s.get("scale") or 0),
        "rotate": int(s.get("rotate") or 0),
        "hflip": bool(s.get("hflip")),
        "vflip": bool(s.get("vflip")),
        "volume_db": _f(s.get("volume_db"), 0.0),
        "loudnorm": bool(s.get("loudnorm")),
    }


def normalize_overlays(raw: list) -> tuple[list[dict], str]:
    """B-Roll / PiP-Spur (V2) prüfen und normalisieren."""
    out: list[dict] = []
    for i, ov in enumerate(raw or []):
        if not isinstance(ov, dict):
            continue
        path = str(ov.get("path") or "").strip()
        if not path:
            continue
        target = resolve_path(path)
        if target is None or not target.is_file():
            return [], f"Overlay {i + 1}: Datei nicht gefunden ({path})."
        start = remux.parse_time(ov.get("start"))
        end = remux.parse_time(ov.get("end"))
        if end <= 0:
            end = remux.probe_duration(target)
        if end <= start:
            return [], f"Overlay {i + 1}: Ende muss nach dem Start liegen."
        tl_start = max(0.0, _f(ov.get("tl_start"), 0.0))
        pos = str(ov.get("position") or "pip_br").lower().strip()
        if pos not in ("pip_br", "pip_tr", "pip_bl", "pip_tl", "full", "side"):
            pos = "pip_br"
        size_pct = max(15, min(100, int(_f(ov.get("size_pct"), 30) or 30)))
        opacity = max(0.1, min(1.0, _f(ov.get("opacity"), 1.0) or 1.0))
        out.append({
            "path": path,
            "abs": target,
            "title": str(ov.get("title") or target.name).strip(),
            "start": float(start),
            "end": float(end),
            "tl_start": float(tl_start),
            "position": pos,
            "size_pct": size_pct,
            "opacity": opacity,
            "fade": max(0.0, min(5.0, _f(ov.get("fade"), 0.0))),
        })
    out.sort(key=lambda x: x["tl_start"])
    return out, ""


def overlay_to_spec(ov: dict) -> dict:
    return {
        "path": ov.get("path") or "",
        "title": ov.get("title") or "",
        "start": float(ov.get("start") or 0.0),
        "end": float(ov.get("end") or 0.0),
        "tl_start": float(ov.get("tl_start") or 0.0),
        "position": ov.get("position") or "pip_br",
        "size_pct": int(ov.get("size_pct") or 30),
        "opacity": float(ov.get("opacity") or 1.0),
        "fade": float(ov.get("fade") or 0.0),
    }


def _write_concat_list(segments: list[dict], work_dir: Path) -> tuple[Optional[Path], str]:
    """Concat-Demuxer-Liste mit inpoint/outpoint (Keyframe-genaue Copy-Schnitte)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    listfile = work_dir / f"editor_{uuid.uuid4().hex[:8]}.txt"
    lines = []
    for s in segments:
        if not s.get("abs"):
            return None, "Concat braucht eine Quelldatei je Clip."
        p = str(s["abs"]).replace("'", "'\\''")
        lines.append(f"file '{p}'")
        lines.append(f"inpoint {float(s['start']):.3f}")
        lines.append(f"outpoint {float(s['end']):.3f}")
    try:
        listfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return None, f"Konnte Liste nicht schreiben: {e}"
    return listfile, ""


def _sub_tracks(path: Optional[Path]) -> list[dict]:
    if not path:
        return []
    data, _ = ff.probe_streams(path)
    if not data:
        return []
    tracks = data.get("subtitles") or []
    return tracks if isinstance(tracks, list) else []


def _copy_audio_ok(segments: list[dict], crossfade: float = 0.0,
                   audio_channels: int = 0, bgm_path: str = "") -> bool:
    """Ton per Concat-Copy möglich: gleiche Spuren, keine Audio-Filter."""
    if bgm_path:
        return False
    if int(audio_channels or 0) in (1, 2):
        return False
    if float(crossfade or 0) > 0.01:
        return False
    sels = {tuple(s.get("audio_indexes") or []) for s in segments}
    if len(sels) > 1:
        return False
    for s in segments:
        if (s.get("kind") or "media") in _GEN_KINDS or s.get("kind") == "freeze":
            return False
        if s.get("reverse"):
            return False
        if _f(s.get("fade_in")) > 0 or _f(s.get("fade_out")) > 0:
            return False
        if abs(_f(s.get("speed"), 1.0) - 1.0) > 0.001:
            return False
        if abs(_f(s.get("volume_db"), 0.0)) > 0.05 or s.get("loudnorm") or s.get("voice_enhance"):
            return False
    return True


def _copy_subs_ok(segments: list[dict]) -> bool:
    sels = {tuple(s.get("sub_indexes") or []) for s in segments}
    if len(sels) > 1:
        return False
    return not any((s.get("kind") or "media") in _GEN_KINDS for s in segments)


def _map_copy_audio_subs(cmd: list[str], input_idx: int, audio_sel: list[int],
                         sub_sel: list[int], container: str,
                         first_abs: Optional[Path],
                         map_audio: bool = True) -> None:
    """Audio/UT vom Concat-Input mappen (Copy, inkl. MP4-UT-Codec)."""
    if map_audio:
        if audio_sel:
            for a in audio_sel:
                cmd += ["-map", f"{input_idx}:a:{int(a)}?"]
        else:
            cmd += ["-an"]
    sub_codecs = {int(t.get("index", i)): str(t.get("codec") or "")
                  for i, t in enumerate(_sub_tracks(first_abs))}
    out_i = 0
    for si in sub_sel:
        src = sub_codecs.get(int(si), "")
        dst = ff._sub_out_codec(src, "mp4" if container == "mp4" else "mkv")
        if dst is None:
            continue
        cmd += ["-map", f"{input_idx}:s:{int(si)}?"]
        cmd += [f"-c:s:{out_i}", dst]
        out_i += 1
    if map_audio and container in ("mkv", "mka") and first_abs:
        cmd += ["-map", f"{input_idx}:t?"]


def build_editor_remux_cmd(segments: list[dict], output: Path,
                           work_dir: Path,
                           add_chapters: bool = True) -> tuple[list, str]:
    """Verlustfreier Export via Concat-Demuxer (inpoint/outpoint).

    Schnitte sind Keyframe-genau (FFmpeg sucht den nächsten Keyframe).
    """
    if not segments:
        return [], "Keine Segmente."
    if any_needs_encode(segments):
        return [], "Dieser Schnitt braucht Encode (Fades/Tempo/Schwarz/…)."
    sels = {tuple(s.get("audio_indexes") or []) for s in segments}
    if len(sels) > 1:
        return [], ("Die Clips haben unterschiedliche Tonspuren gewählt – "
                    "das geht nur im Encode-Modus.")
    ssels = {tuple(s.get("sub_indexes") or []) for s in segments}
    if len(ssels) > 1:
        return [], ("Die Clips haben unterschiedliche Untertitel gewählt – "
                    "das geht nur im Encode-Modus.")
    sel = list(next(iter(sels))) if sels else []
    sub_sel = list(next(iter(ssels))) if ssels else []
    listfile, err = _write_concat_list(segments, work_dir)
    if err:
        return [], err

    meta_path = None
    if add_chapters and len(segments) >= 1:
        meta_path = remux.write_chapter_meta(
            chapters_from_segments(segments), work_dir)

    container = "mp4" if output.suffix.lower() == ".mp4" else "mkv"
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-f", "concat", "-safe", "0",
           "-i", str(listfile)]
    if meta_path:
        cmd += ["-i", str(meta_path), "-map_chapters", "1"]
    first_abs = next((s.get("abs") for s in segments if s.get("abs")), None)
    cmd += ["-map", "0:V?"]
    _map_copy_audio_subs(cmd, 0, sel, sub_sel, container, first_abs)
    cmd += ["-c:v", "copy"]
    if sel:
        cmd += ["-c:a", "copy"]
    cmd += ["-reset_timestamps", "1",
            "-progress", "pipe:1", "-nostats", str(output)]
    return cmd, ""


def _esc_sub_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", r"\'")


def _esc_drawtext(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "")
        .replace("%", "\\%")
    )


def _delogo_filter(spec: str) -> str:
    parts = [p.strip() for p in str(spec or "").split(":")]
    if len(parts) != 4:
        return ""
    try:
        w, h, x, y = (int(float(p)) for p in parts)
    except ValueError:
        return ""
    if w < 4 or h < 4 or x < 0 or y < 0:
        return ""
    return f"delogo=x={x}:y={y}:w={w}:h={h}:show=0"


def _drawtext_filter(text: str, pos: str, th: int) -> str:
    clean = _esc_drawtext(text)
    if not clean:
        return ""
    fs = max(18, int(round(th * 0.042)))
    coords = {
        "bottom": "x=(w-text_w)/2:y=h-text_h-40",
        "top": "x=(w-text_w)/2:y=40",
        "center": "x=(w-text_w)/2:y=(h-text_h)/2",
        "tl": "x=40:y=40",
        "tr": "x=w-text_w-40:y=40",
        "bl": "x=40:y=h-text_h-40",
        "br": "x=w-text_w-40:y=h-text_h-40",
    }.get(pos or "bottom", "x=(w-text_w)/2:y=h-text_h-40")
    return (
        f"drawtext=text='{clean}':{coords}:fontsize={fs}:fontcolor=white"
        f":box=1:boxcolor=black@0.55:boxborderw=10"
    )


_LOOK_FILTERS = {
    "night": "eq=brightness=0.07:contrast=1.12:gamma=1.35:saturation=1.08",
    "vivid": "eq=contrast=1.08:saturation=1.28",
    "clean": "hqdn3d=3:2:6:6,unsharp=5:5:0.75:5:5:0.0",
    "bw": "hue=s=0,eq=contrast=1.14",
    "cinema": "eq=contrast=1.10:saturation=0.92:gamma=0.95,unsharp=5:5:0.4:5:5:0.0",
}


def _look_filter(preset: str) -> str:
    return _LOOK_FILTERS.get(str(preset or "none").lower().strip(), "")


def _atempo_chain(speed: float) -> str:
    """atempo akzeptiert nur 0.5–2.0 – bei Bedarf ketten."""
    parts: list[str] = []
    s = float(speed)
    if s <= 0:
        s = 1.0
    while s > 2.0001:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.499:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 0.001:
        parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


def _ref_geometry(segments: list[dict]) -> tuple[int, int, float, Optional[ff.VideoInfo]]:
    """Ziel-Auflösung/FPS/Info für Concat (erste Mediendatei, sonst 1080p25)."""
    for s in segments:
        p = s.get("abs")
        if not p:
            continue
        info, _ = ff.probe_with_error(p)
        if info and getattr(info, "width", 0):
            fps = float(getattr(info, "fps", 0) or 0) or 25.0
            w, h = int(info.width), int(info.height)
            crop = str(s.get("crop") or "").strip()
            dims = ff.crop_dims(crop) if crop else None
            if dims:
                w, h = dims
            if int(s.get("rotate") or 0) in (90, 270):
                w, h = h, w
            sc = int(s.get("scale") or 0)
            if sc > 0 and h > 0:
                w = max(2, int(round(w * (sc / h) / 2.0)) * 2)
                h = max(2, int(round(sc / 2.0)) * 2)
            return w, h, fps, info
    return 1920, 1080, 25.0, None


_LAYOUTS = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "4.1",
            6: "5.1", 7: "6.1", 8: "7.1"}


def _layout_name(channels: int) -> str:
    return _LAYOUTS.get(int(channels or 0), "stereo")


def _out_layouts(segments: list[dict], n_audio: int, audio_channels: int) -> list[str]:
    """Kanal-Layout je Ausgabespur.

    Concat verlangt pro Spur überall dasselbe Layout. Bei „wie Quelle" gibt der
    erste Clip mit Tonauswahl vor, was die jeweilige Spur wird (5.1 bleibt 5.1).
    """
    if audio_channels == 1:
        return ["mono"] * n_audio
    if audio_channels == 2:
        return ["stereo"] * n_audio
    ref = next((s for s in segments
                if s.get("abs") and (s.get("audio_indexes") or [])), None)
    tracks = _audio_tracks(ref["abs"]) if ref else []
    out: list[str] = []
    for j in range(n_audio):
        sel = list(ref["audio_indexes"]) if ref else []
        idx = sel[j] if j < len(sel) else None
        if idx is None or not (0 <= int(idx) < len(tracks)):
            out.append("stereo")
            continue
        out.append(_layout_name((tracks[int(idx)] or {}).get("channels") or 2))
    return out


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
    crossfade: float = 0.0,
    transition: str = "fade",
    rate_mode: str = "cq",
    v_bitrate: int = 0,
    audio_channels: int = 0,
    work_dir: Optional[Path] = None,
    container: str = "mkv",
    encoder_speed: str = "balanced",
    b_frames: str = "auto",
    keyint_sec: int = 0,
    aq_strength: int = 8,
    nvenc_tune: str = "auto",
    tonemap: bool = False,
    preserve_hdr: bool = True,
    overlays: Optional[list[dict]] = None,
    bgm_path: str = "",
    bgm_volume_db: float = -12.0,
    bgm_ducking: bool = True,
    pad_mode: str = "black",
    aspect_mode: str = "source",
    add_chapters: bool = True,
    preserve_dv: bool = False,
) -> tuple[list, str]:
    """Re-Encode-Export: trim/atrim je Segment, optional xfade + V2-Overlays + BGM, dann concat."""
    if preserve_dv:
        return [], "Editor-Re-Encode unterstützt keine zeitgenaue Dolby-Vision-Metadatenübernahme."
    if not segments:
        return [], "Keine Segmente."
    from . import encoder as enc_mod
    enc = ff.encoder_name(platform, codec)
    backend = ff.encoder_backend(platform)
    n = len(segments)
    xf = max(0.0, float(crossfade or 0))
    if n < 2:
        xf = 0.0
    trans = str(transition or "fade").lower().strip()
    if trans not in _TRANSITIONS:
        trans = "fade"
    tw, th, tfps, ref_info = _ref_geometry(segments)
    asp = str(aspect_mode or "source").lower().strip()
    if asp == "9:16":
        base_h = max(720, min(1920, max(tw, th)))
        th = max(2, int(round(base_h / 2.0)) * 2)
        tw = max(2, int(round(th * 9.0 / 16.0 / 2.0)) * 2)
    elif asp == "1:1":
        side = max(480, min(1440, min(tw, th)))
        tw = th = max(2, int(round(side / 2.0)) * 2)
    elif asp == "16:9" and tw < th:
        tw, th = th, tw
    use_blur_pad = str(pad_mode or "black").lower().strip() == "blur"
    is_hdr = bool(ref_info and getattr(ref_info, "is_hdr", False))
    do_tonemap = bool(tonemap and is_hdr)
    keep_hdr = bool(preserve_hdr and is_hdr and not do_tonemap)
    ten_bit = bool(
        (keep_hdr or (ref_info and getattr(ref_info, "bit_depth", 8) >= 10 and not do_tonemap))
        and enc_mod._codec_supports_10bit(platform, codec)
    )
    pix_fmt = "yuv420p10le" if ten_bit else "yuv420p"
    ov_list = list(overlays or [])
    bgm_abs = resolve_path(bgm_path) if bgm_path else None
    if bgm_abs is not None and not bgm_abs.is_file():
        bgm_abs = None

    path_to_idx: dict[str, int] = {}
    file_inputs: list[Path] = []
    lavfi: list[str] = []  # extra lavfi nach den Dateien

    def file_idx(path: Path) -> int:
        key = str(path)
        if key not in path_to_idx:
            path_to_idx[key] = len(file_inputs)
            file_inputs.append(path)
        return path_to_idx[key]

    for s in segments:
        if s.get("abs"):
            file_idx(s["abs"])
    for ov in ov_list:
        if ov.get("abs"):
            file_idx(ov["abs"])

    cmd = [config.FFMPEG, "-y", "-hide_banner"]
    if backend == "vaapi":
        cmd += ["-vaapi_device", "/dev/dri/renderD128"]
    for inp in file_inputs:
        cmd += ["-i", str(inp)]

    n_files = len(file_inputs)
    parts: list[str] = []
    v_pads: list[str] = []
    a_pads: list[list[str]] = []
    durs: list[float] = []
    want_copy_a = (audio_codec or "").lower() == "copy"
    copy_audio = want_copy_a and _copy_audio_ok(
        segments, xf, int(audio_channels or 0), bgm_path=str(bgm_abs or ""))
    copy_subs = _copy_subs_ok(segments) and any(
        s.get("sub_indexes") for s in segments)
    # Alle Clips müssen gleich viele Tonspuren liefern – fehlende werden still.
    n_audio = 0 if copy_audio else max(
        1, max((len(s.get("audio_indexes") or []) for s in segments), default=1))
    layouts = _out_layouts(segments, max(1, n_audio), int(audio_channels or 0))

    for i, s in enumerate(segments):
        start, end = float(s["start"]), float(s["end"])
        dur = max(0.001, end - start)
        speed = max(0.25, min(4.0, _f(s.get("speed"), 1.0) or 1.0))
        out_d = dur / speed
        durs.append(out_d)
        vlabel = f"v{i}"
        kind = s.get("kind") or "media"
        fi = min(_f(s.get("fade_in")), out_d * 0.45)
        fo = min(_f(s.get("fade_out")), out_d * 0.45)

        if kind in _GEN_KINDS:
            li = n_files + len(lavfi)
            lavfi.append(
                f"color=c=black:s={tw}x{th}:r={tfps:.3f}:d={out_d:.3f}")
            parts.append(
                f"[{li}:v]format={pix_fmt},setsar=1,fps={tfps:.3f}[{vlabel}]")
            mute = True
        elif kind == "freeze":
            ii = file_idx(s["abs"])
            f_chain = [
                f"trim=start={start:.3f}:end={start + 0.08:.3f}",
                "setpts=PTS-STARTPTS",
                f"tpad=stop_mode=clone:stop_duration={out_d:.3f}",
                f"trim=duration={out_d:.3f}",
            ]
            if do_tonemap:
                f_chain.append(enc_mod.tonemap_chain("hable", 0.0, 0.0))
            crop = str(s.get("crop") or "").strip()
            if crop and ":" in crop:
                f_chain.append(f"crop={crop}")
            dl = _delogo_filter(s.get("delogo") or "")
            if dl:
                f_chain.append(dl)
            lf = _look_filter(s.get("look_preset") or "none")
            if lf:
                f_chain.append(lf)
            rot = int(s.get("rotate") or 0)
            if rot in _ROTATE_FILTERS:
                f_chain.append(_ROTATE_FILTERS[rot])
            if s.get("hflip"):
                f_chain.append("hflip")
            if s.get("vflip"):
                f_chain.append("vflip")
            sc = int(s.get("scale") or 0)
            if sc > 0:
                f_chain.append(f"scale=-2:{sc}")
            f_chain.append(f"scale={tw}:{th}:force_original_aspect_ratio=decrease")
            f_chain.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2")
            dt = _drawtext_filter(s.get("text_overlay") or "", s.get("text_pos") or "bottom", th)
            if dt:
                f_chain.append(dt)
            f_chain.append(f"format={pix_fmt}")
            f_chain.append(f"fps={tfps:.3f}")
            f_chain.append("setsar=1")
            if fi > 0.02:
                f_chain.append(f"fade=t=in:st=0:d={fi:.3f}")
            if fo > 0.02:
                f_chain.append(f"fade=t=out:st={max(0.0, out_d - fo):.3f}:d={fo:.3f}")
            parts.append(f"[{ii}:v:0]" + ",".join(f_chain) + f"[{vlabel}]")
            mute = True
        else:
            ii = file_idx(s["abs"])
            chain = [f"trim=start={start:.3f}:end={end:.3f}", "setpts=PTS-STARTPTS"]
            if s.get("reverse") and dur <= 60.0:
                chain.append("reverse")
            if s.get("burn_subs") or (burn_subs and int(s.get("sub_index", sub_index)) >= 0
                                      and i == 0):
                si = int(s.get("sub_index", sub_index) if s.get("sub_index", -1) >= 0
                         else sub_index)
                if si >= 0 and s.get("abs"):
                    chain.insert(
                        0,
                        f"subtitles='{_esc_sub_path(s['abs'])}':si={si}",
                    )
            if do_tonemap:
                chain.append(enc_mod.tonemap_chain("hable", 0.0, 0.0))
            crop = str(s.get("crop") or "").strip()
            if crop and ":" in crop:
                chain.append(f"crop={crop}")
            dl = _delogo_filter(s.get("delogo") or "")
            if dl:
                chain.append(dl)
            lf = _look_filter(s.get("look_preset") or "none")
            if lf:
                chain.append(lf)
            rot = int(s.get("rotate") or 0)
            if rot in _ROTATE_FILTERS:
                chain.append(_ROTATE_FILTERS[rot])
            if s.get("hflip"):
                chain.append("hflip")
            if s.get("vflip"):
                chain.append("vflip")
            sc = int(s.get("scale") or 0)
            if sc > 0:
                chain.append(f"scale=-2:{sc}")
            if abs(speed - 1.0) > 0.001:
                chain.append(f"setpts=PTS/{speed:.4f}")
            dt = _drawtext_filter(s.get("text_overlay") or "", s.get("text_pos") or "bottom", th)
            if use_blur_pad:
                pre_lbl = f"vpre{i}"
                bg_lbl = f"vbg{i}"
                fg_lbl = f"vfg{i}"
                ov_lbl = f"vovpad{i}"
                parts.append(f"[{ii}:v:0]" + ",".join(chain) + f",split=2[{bg_lbl}][{fg_lbl}]")
                parts.append(
                    f"[{bg_lbl}]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                    f"crop={tw}:{th},boxblur=20:2,eq=brightness=-0.08[{pre_lbl}_bg]"
                )
                parts.append(
                    f"[{fg_lbl}]scale={tw}:{th}:force_original_aspect_ratio=decrease[{pre_lbl}_fg]"
                )
                tail = []
                if dt:
                    tail.append(dt)
                tail.append(f"format={pix_fmt}")
                tail.append(f"fps={tfps:.3f}")
                tail.append("setsar=1")
                if fi > 0.02:
                    tail.append(f"fade=t=in:st=0:d={fi:.3f}")
                if fo > 0.02:
                    tail.append(f"fade=t=out:st={max(0.0, out_d - fo):.3f}:d={fo:.3f}")
                parts.append(
                    f"[{pre_lbl}_bg][{pre_lbl}_fg]overlay=(W-w)/2:(H-h)/2,"
                    + ",".join(tail) + f"[{vlabel}]"
                )
            else:
                chain.append(f"scale={tw}:{th}:force_original_aspect_ratio=decrease")
                chain.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2")
                if dt:
                    chain.append(dt)
                chain.append(f"format={pix_fmt}")
                chain.append(f"fps={tfps:.3f}")
                chain.append("setsar=1")
                if fi > 0.02:
                    chain.append(f"fade=t=in:st=0:d={fi:.3f}")
                if fo > 0.02:
                    chain.append(f"fade=t=out:st={max(0.0, out_d - fo):.3f}:d={fo:.3f}")
                parts.append(f"[{ii}:v:0]" + ",".join(chain) + f"[{vlabel}]")
            mute = bool(s.get("mute"))

        v_pads.append(vlabel)
        if copy_audio:
            continue

        sel = [] if (mute or kind in _GEN_KINDS) else list(s.get("audio_indexes") or [])
        labels: list[str] = []
        vol_db = _f(s.get("volume_db"), 0.0)
        do_ln = bool(s.get("loudnorm"))
        for j in range(n_audio):
            alabel = f"a{i}_{j}"
            src_idx = sel[j] if j < len(sel) else None
            lay = layouts[j] if j < len(layouts) else "stereo"
            if src_idx is None:
                ali = n_files + len(lavfi)
                lavfi.append(
                    f"anullsrc=channel_layout={lay}:sample_rate=48000:d={out_d:.3f}")
                parts.append(
                    f"[{ali}:a]aformat=sample_fmts=fltp:channel_layouts={lay}[{alabel}]")
            else:
                ii = file_idx(s["abs"])
                achain = [
                    f"atrim=start={start:.3f}:end={end:.3f}",
                    "asetpts=PTS-STARTPTS",
                ]
                if s.get("reverse") and dur <= 60.0:
                    achain.append("areverse")
                achain += [
                    f"aformat=sample_fmts=fltp:channel_layouts={lay}",
                    "aresample=48000",
                ]
                at = _atempo_chain(speed)
                if at:
                    achain.append(at)
                if s.get("voice_enhance"):
                    achain.append("highpass=f=85,lowpass=f=12000,afftdn=nf=-25")
                if do_ln:
                    achain.append("loudnorm=I=-16:TP=-1.5:LRA=11")
                if abs(vol_db) > 0.05:
                    achain.append(f"volume={vol_db:.2f}dB")
                if fi > 0.02:
                    achain.append(f"afade=t=in:st=0:d={fi:.3f}")
                if fo > 0.02:
                    achain.append(
                        f"afade=t=out:st={max(0.0, out_d - fo):.3f}:d={fo:.3f}")
                parts.append(f"[{ii}:a:{int(src_idx)}]" + ",".join(achain) + f"[{alabel}]")
            labels.append(alabel)

        a_pads.append(labels)

    for spec in lavfi:
        cmd += ["-f", "lavfi", "-i", spec]

    concat_idx = -1
    if copy_audio or copy_subs:
        wd = work_dir or (_cache_dir() / "concat")
        listfile, err = _write_concat_list(
            [s for s in segments if s.get("abs")], wd)
        if err:
            return [], err
        concat_idx = n_files + len(lavfi)
        cmd += ["-f", "concat", "-safe", "0", "-i", str(listfile)]

    meta_idx = -1
    if add_chapters and len(segments) >= 1:
        wd = work_dir or (_cache_dir() / "concat")
        meta_path = remux.write_chapter_meta(
            chapters_from_segments(segments, xf), wd)
        if meta_path:
            meta_idx = n_files + len(lavfi) + (1 if concat_idx >= 0 else 0)
            cmd += ["-i", str(meta_path)]

    bgm_idx = -1
    if bgm_abs is not None and not copy_audio:
        bgm_idx = n_files + len(lavfi) + (1 if concat_idx >= 0 else 0) + (1 if meta_idx >= 0 else 0)
        cmd += ["-stream_loop", "-1", "-i", str(bgm_abs)]

    if xf > 0.02 and n >= 2:
        cur_v = v_pads[0]
        cur_a = list(a_pads[0]) if a_pads else []
        acc = durs[0]
        for i in range(1, n):
            d = min(xf, durs[i] * 0.45, durs[i - 1] * 0.45)
            off = max(0.0, acc - d)
            nv = f"xfv{i}"
            parts.append(
                f"[{cur_v}][{v_pads[i]}]xfade=transition={trans}:duration={d:.3f}"
                f":offset={off:.3f}[{nv}]")
            if not copy_audio:
                for j in range(n_audio):
                    na = f"xfa{i}_{j}"
                    parts.append(
                        f"[{cur_a[j]}][{a_pads[i][j]}]acrossfade=d={d:.3f}[{na}]")
                    cur_a[j] = na
            cur_v = nv
            acc = acc + durs[i] - d
        v_out, a_outs = cur_v, (cur_a if not copy_audio else [])
    elif copy_audio:
        concat = "".join(f"[{v}]" for v in v_pads)
        parts.append(concat + f"concat=n={n}:v=1:a=0[vc]")
        v_out, a_outs = "vc", []
    else:
        concat = "".join(
            f"[{v}]" + "".join(f"[{a}]" for a in al)
            for v, al in zip(v_pads, a_pads))
        a_outs = [f"ac{j}" for j in range(n_audio)]
        parts.append(concat + f"concat=n={n}:v=1:a={n_audio}[vc]"
                     + "".join(f"[{a}]" for a in a_outs))
        v_out = "vc"

    # Zweite Videospur (V2: B-Roll / Picture-in-Picture Overlays)
    for k, ov in enumerate(ov_list):
        if not ov.get("abs"):
            continue
        oii = file_idx(ov["abs"])
        ostart = float(ov.get("start") or 0.0)
        oend = float(ov.get("end") or 0.0)
        odur = max(0.05, oend - ostart)
        otl = max(0.0, float(ov.get("tl_start") or 0.0))
        pos = str(ov.get("position") or "pip_br")
        pct = max(15, min(100, int(ov.get("size_pct") or 30)))
        opac = max(0.1, min(1.0, float(ov.get("opacity") or 1.0)))
        ofade = min(float(ov.get("fade") or 0.0), odur * 0.45)
        ow = max(2, int(round(tw * (pct / 100.0) / 2.0)) * 2) if pos != "full" else tw
        oh = max(2, int(round(th * (pct / 100.0) / 2.0)) * 2) if pos != "full" else th
        ochain = [
            f"trim=start={ostart:.3f}:end={oend:.3f}",
            f"setpts=PTS-STARTPTS+{otl:.3f}/TB",
            f"scale={ow}:{oh}:force_original_aspect_ratio=decrease",
            f"fps={tfps:.3f}",
            "setsar=1",
        ]
        if opac < 0.99 or ofade > 0.02:
            ochain.append("format=yuva420p")
            if ofade > 0.02:
                ochain.append(f"fade=t=in:st={otl:.3f}:d={ofade:.3f}:alpha=1")
                ochain.append(f"fade=t=out:st={max(otl, otl + odur - ofade):.3f}:d={ofade:.3f}:alpha=1")
            if opac < 0.99:
                ochain.append(f"colorchannelmixer=aa={opac:.2f}")
        else:
            ochain.append(f"format={pix_fmt}")
        olabel = f"ov{k}"
        next_v = f"vov{k}"
        parts.append(f"[{oii}:v:0]" + ",".join(ochain) + f"[{olabel}]")
        margin = max(16, int(round(tw * 0.02)))
        coords = {
            "pip_br": f"x=W-w-{margin}:y=H-h-{margin}",
            "pip_tr": f"x=W-w-{margin}:y={margin}",
            "pip_bl": f"x={margin}:y=H-h-{margin}",
            "pip_tl": f"x={margin}:y={margin}",
            "full": "x=(W-w)/2:y=(H-h)/2",
            "side": f"x=W-w-{margin}:y=(H-h)/2",
        }.get(pos, f"x=W-w-{margin}:y=H-h-{margin}")
        parts.append(
            f"[{v_out}][{olabel}]overlay={coords}:eof_action=pass"
            f":enable='between(t,{otl:.3f},{otl + odur:.3f})'"
            f",format={pix_fmt}[{next_v}]"
        )
        v_out = next_v

    # Hintergrundmusik (A2) mit optionalem Auto-Ducking (sidechaincompress)
    if bgm_idx >= 0 and a_outs:
        tot_d = max(0.2, total_duration(segments, xf))
        lay0 = layouts[0] if layouts else "stereo"
        b_vol = max(-36.0, min(12.0, float(bgm_volume_db if bgm_volume_db is not None else -12.0)))
        parts.append(
            f"[{bgm_idx}:a:0]atrim=0:{tot_d:.3f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:channel_layouts={lay0},aresample=48000,"
            f"volume={b_vol:.2f}dB[bgm_raw]"
        )
        main_a = a_outs[0]
        if bgm_ducking:
            parts.append(f"[{main_a}]asplit=2[main_keep][main_sc]")
            parts.append(
                "[bgm_raw][main_sc]sidechaincompress="
                "threshold=0.03:ratio=6:attack=35:release=350[bgm_duck]"
            )
            parts.append(
                "[main_keep][bgm_duck]amix=inputs=2:duration=first:dropout_transition=0[a_bgm_out]"
            )
        else:
            parts.append(
                f"[{main_a}][bgm_raw]amix=inputs=2:duration=first:dropout_transition=0[a_bgm_out]"
            )
        a_outs[0] = "a_bgm_out"

    filt = ";".join(parts)
    if backend == "vaapi":
        va_fmt = "p010le" if ten_bit else "nv12"
        filt += f";[{v_out}]format={va_fmt},hwupload[v]"
        map_v = "[v]"
    else:
        map_v = f"[{v_out}]"

    cmd += ["-filter_complex", filt, "-map", map_v]
    for a in a_outs:
        cmd += ["-map", f"[{a}]"]
    if meta_idx >= 0:
        cmd += ["-map_chapters", str(meta_idx)]
    cmd += ["-c:v", enc]
    if keep_hdr and ref_info is not None:
        cmd += enc_mod._hdr_output_args(ref_info, codec, enc)
    elif ten_bit:
        cmd += enc_mod._ten_bit_output_args(codec, enc)
        if ref_info is not None:
            cmd += enc_mod.color_output_args(ref_info, tonemapped=do_tonemap, keep_hdr=False)
    elif ref_info is not None:
        cmd += enc_mod.color_output_args(ref_info, tonemapped=do_tonemap, keep_hdr=False)
    br = int(v_bitrate or 0)
    rm = str(rate_mode or "cq").lower()
    if rm in ("bitrate", "abr") and br > 0:
        cmd += ff.bitrate_args(platform, codec, br, abr=(rm == "abr"))
    else:
        if backend == "nvenc":
            cmd += ["-rc", "vbr"]
        cmd += ff.quality_args(platform, int(cq or 30))
    cmd += ff.encoder_preset_args(enc, encoder_speed)
    if enc == "libvpx-vp9":
        cmd += ff.vp9_args(encoder_speed, cq_mode=rm not in ("bitrate", "abr"))
    elif "nvenc" in enc:
        cmd += ff.nvenc_archive_args(
            enc, aq_strength, width=int(tw or 0), height=int(th or 0),
            multipass=None if rm in ("bitrate", "abr") else "qres",
            b_frames=b_frames, rate_mode=rm, tune=nvenc_tune)
    ff.apply_cpu_aq(cmd, enc, aq_strength)
    cmd += ff.gop_args(float(tfps or 0), keyint_sec)

    first_abs = next((s.get("abs") for s in segments if s.get("abs")), None)
    audio_sel = list(next((s.get("audio_indexes") or [] for s in segments), []))
    sub_sel = list(next((s.get("sub_indexes") or [] for s in segments), []))
    cont = "mp4" if str(container or output.suffix).lower().endswith("mp4") else "mkv"

    if copy_audio and concat_idx >= 0:
        _map_copy_audio_subs(cmd, concat_idx, audio_sel, sub_sel if copy_subs else [],
                             cont, first_abs)
        if audio_sel:
            cmd += ["-c:a", "copy"]
        cmd += _audio_metadata(segments, len(audio_sel) or 1)
    else:
        ac = (audio_codec or "aac").lower()
        if ac == "copy":
            ac = "aac"
        enc_a = ff.AUDIO_ENCODERS.get(ac, "aac")
        cmd += ["-c:a", enc_a]
        if enc_a != "flac":
            cmd += ["-b:a", f"{max(32, int(audio_bitrate or 192))}k"]
        cmd += _audio_metadata(segments, n_audio)
        if copy_subs and concat_idx >= 0 and sub_sel:
            _map_copy_audio_subs(cmd, concat_idx, [], sub_sel, cont, first_abs,
                                 map_audio=False)
    cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    return cmd, ""


def _audio_metadata(segments: list[dict], n_audio: int) -> list[str]:
    """Sprache/Titel der Quellspuren auf die Ausgabespuren übertragen."""
    ref = next((s for s in segments
                if s.get("abs") and (s.get("audio_indexes") or [])), None)
    if not ref:
        return []
    tracks = _audio_tracks(ref["abs"])
    if not tracks:
        return []
    out: list[str] = []
    for j, src_idx in enumerate(list(ref["audio_indexes"])[:n_audio]):
        if not (0 <= int(src_idx) < len(tracks)):
            continue
        t = tracks[int(src_idx)] or {}
        lang = str(t.get("language") or "").strip()
        if lang and lang.lower() != "und":
            out += [f"-metadata:s:a:{j}", f"language={lang}"]
        title = str(t.get("title") or "").strip()
        if title:
            out += [f"-metadata:s:a:{j}", f"title={title}"]
    return out


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
    data["codec"] = getattr(info, "codec", "") if info else ""
    data["container"] = getattr(info, "container", "") if info else data.get("container", "")
    data["width"] = int(getattr(info, "width", 0) or 0) if info else 0
    data["height"] = int(getattr(info, "height", 0) or 0) if info else 0
    data["fps"] = float(getattr(info, "fps", 0) or 0) if info else 0.0
    vbr = int(getattr(info, "video_bitrate", 0) or 0) if info else 0
    total_br = int(getattr(info, "overall_bitrate", 0) or 0) if info else 0
    data["video_bitrate"] = vbr
    data["video_bitrate_human"] = ff._bitrate_human(vbr) if vbr else ""
    data["overall_bitrate"] = total_br
    data["overall_bitrate_human"] = ff._bitrate_human(total_br) if total_br else ""
    data["bit_depth"] = int(getattr(info, "bit_depth", 8) or 8) if info else 8
    data["hdr_type"] = str(getattr(info, "hdr_type", "SDR") or "SDR") if info else "SDR"
    data["is_hdr"] = bool(getattr(info, "is_hdr", False)) if info else False
    data["chapters"] = remux.probe_chapters(target)
    return data, ""


def _cache_key(path: Path, extra: str = "") -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{extra}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def list_keyframes(rel: str, max_n: int = 4000) -> tuple[list[float], str]:
    """Keyframe-Zeiten (Sekunden) via Packet-Flags."""
    target = resolve_path(rel)
    if target is None or not target.is_file():
        return [], "Datei nicht gefunden"
    cache = _cache_dir() / f"kf_{_cache_key(target)}.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return [float(x) for x in data.get("t") or []], ""
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    cmd = [
        config.FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0", str(target),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], str(e)
    times: list[float] = []
    for line in (out.stdout or "").splitlines():
        if "K" not in line:
            continue
        parts = line.split(",")
        if not parts:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        if t >= 0:
            times.append(t)
        if len(times) >= max_n:
            break
    try:
        cache.write_text(json.dumps({"t": times}), encoding="utf-8")
    except OSError:
        pass
    return times, ""


def nearest_keyframe(rel: str, t: float) -> dict:
    times, err = list_keyframes(rel)
    if err:
        return {"error": err, "t": t}
    if not times:
        return {"t": t, "snapped": t, "delta": 0}
    best = min(times, key=lambda x: abs(x - float(t)))
    return {"t": t, "snapped": best, "delta": best - float(t)}


def ensure_preview_assets(rel: str) -> dict:
    """Filmstrip + Waveform erzeugen (Cache). Bei langen Dateien schneller Multi-Input-Seek."""
    target = resolve_path(rel)
    if target is None or not target.is_file():
        return {"error": "Datei nicht gefunden"}
    key = _cache_key(target)
    folder = _cache_dir() / key
    folder.mkdir(parents=True, exist_ok=True)
    strip = folder / "filmstrip.jpg"
    wave = folder / "wave.png"
    info, _ = ff.probe_with_error(target)
    duration = float(getattr(info, "duration", 0) or 0) if info else remux.probe_duration(target)
    n = max(8, min(32, int(duration / 8) if duration else 20))
    if not strip.is_file() and duration > 0:
        if duration > 180:
            # Schneller Keyframe-Seek je Kachel: dauert auch bei 60 GB < 3 s.
            cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
            step = duration / n
            for i in range(n):
                t = max(0.0, min(duration - 0.1, (i + 0.5) * step))
                cmd += ["-ss", f"{t:.2f}", "-i", str(target)]
            pads = []
            parts = []
            for i in range(n):
                parts.append(
                    f"[{i}:v:0]trim=end_frame=1,scale=128:72:force_original_aspect_ratio=decrease,"
                    f"pad=128:72:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
                )
                pads.append(f"[v{i}]")
            parts.append("".join(pads) + f"hstack=inputs={n}[out]")
            cmd += ["-filter_complex", ";".join(parts), "-map", "[out]", "-frames:v", "1", str(strip)]
        else:
            fps = max(0.02, n / duration)
            cmd = [
                config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(target),
                "-vf", f"fps={fps:.5f},scale=128:72:force_original_aspect_ratio=decrease,"
                       f"pad=128:72:(ow-iw)/2:(oh-ih)/2,tile={n}x1",
                "-frames:v", "1", str(strip),
            ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not wave.is_file() and 0 < duration <= 2400:
        cmd = [
            config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(target),
            "-filter_complex",
            "aresample=8000,aformat=channel_layouts=mono,compand,showwavespic=s=1280x48:colors=#22d3ee",
            "-frames:v", "1", str(wave),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "key": key,
        "tiles": n,
        "filmstrip": f"/api/editor/asset/{key}/filmstrip.jpg" if strip.is_file() else "",
        "waveform": f"/api/editor/asset/{key}/wave.png" if wave.is_file() else "",
        "duration": duration,
    }


def detect_source_crop(rel: str) -> dict:
    """Schwarze Balken einer Quelle via cropdetect erkennen."""
    target = resolve_path(rel)
    if target is None or not target.is_file():
        return {"error": "Datei nicht gefunden"}
    info, err = ff.probe_with_error(target)
    if not info:
        return {"error": err or "Analyse fehlgeschlagen"}
    crop = ff.detect_crop(info)
    return {
        "crop": crop or "",
        "width": int(getattr(info, "width", 0) or 0),
        "height": int(getattr(info, "height", 0) or 0),
    }


def detect_cuts(
    rel: str,
    kind: str = "black",
    start_sec: float = 0.0,
    end_sec: float = 0.0,
    min_duration: float = 0.5,
    threshold: float = 0.0,
) -> dict:
    """Schwarzbilder, Stille oder Szenenwechsel erkennen und Behalten-Bereiche liefern."""
    target = resolve_path(rel)
    if target is None or not target.is_file():
        return {"error": "Datei nicht gefunden"}
    info, _ = ff.probe_with_error(target)
    dur = float(getattr(info, "duration", 0) or 0) if info else remux.probe_duration(target)
    if dur <= 0:
        return {"error": "Dauer unbekannt"}
    s0 = max(0.0, float(start_sec or 0.0))
    s1 = min(dur, float(end_sec)) if end_sec and float(end_sec) > s0 else dur
    span = max(0.5, min(3600.0, s1 - s0))
    mode = str(kind or "black").lower().strip()
    min_d = max(0.1, min(30.0, float(min_duration or 0.5)))

    cmd = [config.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "info"]
    if s0 > 0:
        cmd += ["-ss", f"{s0:.3f}"]
    cmd += ["-i", str(target), "-t", f"{span:.3f}"]

    if mode == "silence":
        db = float(threshold) if threshold < 0 else -35.0
        cmd += ["-vn", "-sn", "-af", f"silencedetect=noise={db:.1f}dB:d={min_d:.2f}", "-f", "null", "-"]
    elif mode == "scene":
        sc_th = max(0.08, min(0.8, float(threshold) if threshold > 0 else 0.32))
        cmd += [
            "-an", "-sn", "-vf",
            f"fps=8,scale=320:-2,select='gt(scene,{sc_th:.2f})',showinfo",
            "-f", "null", "-",
        ]
    else:
        mode = "black"
        pix_th = max(0.01, min(0.5, float(threshold) if threshold > 0 else 0.10))
        cmd += [
            "-an", "-sn", "-vf",
            f"fps=12,scale=320:-2,blackdetect=d={min_d:.2f}:pix_th={pix_th:.2f}",
            "-f", "null", "-",
        ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"Analyse abgebrochen: {e}"}

    stderr = proc.stderr or ""
    gaps: list[dict] = []
    scenes: list[float] = []

    if mode == "black":
        for m in _BLACK_RE.finditer(stderr):
            try:
                bs = s0 + float(m.group(1))
                be = s0 + float(m.group(2))
                bd = float(m.group(3))
                if be > bs:
                    gaps.append({"start": round(bs, 2), "end": round(be, 2), "duration": round(bd, 2)})
            except ValueError:
                continue
    elif mode == "silence":
        cur_start: Optional[float] = None
        for line in stderr.splitlines():
            ms = _SILENCE_START_RE.search(line)
            if ms:
                try:
                    cur_start = s0 + float(ms.group(1))
                except ValueError:
                    cur_start = None
                continue
            me = _SILENCE_END_RE.search(line)
            if me and cur_start is not None:
                try:
                    se = s0 + float(me.group(1))
                    sd = float(me.group(2))
                    if se > cur_start:
                        gaps.append({"start": round(cur_start, 2), "end": round(se, 2), "duration": round(sd, 2)})
                except ValueError:
                    pass
                cur_start = None
    elif mode == "scene":
        last_t = s0
        for m in _SCENE_PTS_RE.finditer(stderr):
            try:
                t = round(s0 + float(m.group(1)), 2)
                if t - last_t >= min_d and s0 < t < s1 - 0.1:
                    scenes.append(t)
                    last_t = t
            except ValueError:
                continue

    keeps: list[dict] = []
    if mode == "scene":
        pts = [round(s0, 2)] + scenes + [round(s1, 2)]
        for i in range(len(pts) - 1):
            if pts[i + 1] - pts[i] >= 0.25:
                keeps.append({"start": pts[i], "end": pts[i + 1], "title": f"Szene {i + 1}"})
    else:
        cursor = s0
        idx = 1
        for g in gaps:
            if g["start"] - cursor >= 0.25:
                keeps.append({"start": round(cursor, 2), "end": round(g["start"], 2), "title": f"Teil {idx}"})
                idx += 1
            cursor = max(cursor, g["end"])
        if s1 - cursor >= 0.25:
            keeps.append({"start": round(cursor, 2), "end": round(s1, 2), "title": f"Teil {idx}"})

    return {
        "kind": mode,
        "gaps": gaps,
        "scenes": scenes,
        "keeps": keeps,
        "duration": dur,
    }


def _keyframes_around(path: Path, start: float, end: float) -> list[float]:
    """Keyframes einer Datei (gecacht über list_keyframes oder per kurzem Probe)."""
    begin = max(0.0, float(start) - 25.0)
    stop = max(begin + 1.0, float(end) + 25.0)
    cmd = [
        config.FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-read_intervals", f"{begin:.3f}%{stop:.3f}",
        "-show_entries", "frame=pts_time", "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=40, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: list[float] = []
    for line in (proc.stdout or "").splitlines():
        piece = line.strip().split(",")[0]
        try:
            pts = float(piece)
        except ValueError:
            continue
        if pts >= 0:
            out.append(pts)
    return sorted(set(out))


def _smart_match_video_args(info: Optional[ff.VideoInfo]) -> list[str]:
    """Encoder-Argumente passend zum Quell-Codec/Profil für Smart-Cut-Ränder."""
    codec = (getattr(info, "codec", "") or "h264").lower()
    ten = int(getattr(info, "bit_depth", 8) or 8) >= 10
    pix = "yuv420p10le" if ten else "yuv420p"
    if codec in ("hevc", "h265"):
        args = ["-c:v", "libx265", "-preset", "fast", "-crf", "16", "-pix_fmt", pix]
        if getattr(info, "is_hdr", False):
            prim = info.color_primaries or "bt2020"
            trc = info.color_transfer or "smpte2084"
            space = info.color_space or "bt2020nc"
            args += [
                "-colorspace", space, "-color_primaries", prim, "-color_trc", trc,
                "-x265-params", f"colorprim={prim}:transfer={trc}:colormatrix={space}:hdr10=1:repeat-headers=1",
            ]
        else:
            args += ["-x265-params", "repeat-headers=1"]
        return args
    if codec == "av1":
        return ["-c:v", "libsvtav1", "-preset", "6", "-crf", "18", "-pix_fmt", pix]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "15", "-pix_fmt", pix]


def build_editor_smart_plan(
    segments: list[dict],
    output: Path,
    work_dir: Path,
    add_chapters: bool = True,
) -> tuple[list[tuple[list[str], Path, str, float]], list[str], list[Path], str]:
    """Smart Cut: Nur angeschnittene GOP-Anfänge re-encodieren, Hauptteil per Stream-Copy.

    Liefert (part_jobs, final_concat_cmd, temp_files, error).
    Jeder part_job ist (cmd, part_path, label, duration).
    """
    if not segments:
        return [], [], [], "Keine Segmente."
    if any_needs_encode(segments):
        return [], [], [], "Smart Cut unterstützt keine Filter/Fades/Tempo – dafür Encode wählen."
    compat = check_remux_compat(segments)
    if not compat.get("compatible"):
        return [], [], [], "; ".join(compat.get("warnings") or ["Quellen nicht kompatibel."])

    work_dir.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    part_jobs: list[tuple[list[str], Path, str, float]] = []
    part_files: list[Path] = []
    temp_files: list[Path] = []
    sel = list(next((s.get("audio_indexes") or [] for s in segments), []))
    sub_sel = list(next((s.get("sub_indexes") or [] for s in segments), []))
    first_abs = next((s.get("abs") for s in segments if s.get("abs")), None)
    container = "mp4" if output.suffix.lower() == ".mp4" else "mkv"

    for idx, s in enumerate(segments):
        src: Optional[Path] = s.get("abs")
        if not src:
            return [], [], [], f"Clip {idx + 1}: Quelldatei fehlt."
        start = float(s["start"])
        end = float(s["end"])
        dur = max(0.05, end - start)
        info, _ = ff.probe_with_error(src)
        kfs = _keyframes_around(src, start, end)
        # Liegt start bereits nahe an einem Keyframe (<= 0.08s), reicht Stream-Copy.
        near_kf = next((k for k in kfs if abs(k - start) <= 0.08), None)
        next_kf = next((k for k in kfs if start + 0.15 < k < end - 0.25), None)

        if near_kf is None and next_kf is not None and (next_kf - start) <= 18.0:
            # Teil 1a: Kurzer Re-Encode von start bis zum nächsten Keyframe
            head_dur = round(next_kf - start, 3)
            head_path = work_dir / f"smart_{tag}_{idx}_head.mkv"
            head_cmd = [
                config.FFMPEG, "-y", "-hide_banner",
                "-ss", f"{start:.3f}", "-i", str(src),
                "-t", f"{head_dur:.3f}",
                "-map", "0:v:0",
            ]
            head_cmd += _smart_match_video_args(info)
            _map_copy_audio_subs(head_cmd, 0, sel, sub_sel, "mkv", src)
            if sel:
                head_cmd += ["-c:a", "copy"]
            head_cmd += ["-avoid_negative_ts", "make_zero", "-progress", "pipe:1", "-nostats", str(head_path)]
            part_jobs.append((head_cmd, head_path, f"Smart-Cut Rand Clip {idx + 1}", head_dur))
            part_files.append(head_path)
            temp_files.append(head_path)

            # Teil 1b: Stream-Copy ab next_kf bis end
            body_dur = round(end - next_kf, 3)
            body_path = work_dir / f"smart_{tag}_{idx}_body.mkv"
            body_cmd = [
                config.FFMPEG, "-y", "-hide_banner",
                "-ss", f"{next_kf:.3f}", "-i", str(src),
                "-t", f"{body_dur:.3f}",
                "-map", "0:v:0", "-c:v", "copy",
            ]
            _map_copy_audio_subs(body_cmd, 0, sel, sub_sel, "mkv", src)
            if sel:
                body_cmd += ["-c:a", "copy"]
            body_cmd += ["-avoid_negative_ts", "make_zero", "-progress", "pipe:1", "-nostats", str(body_path)]
            part_jobs.append((body_cmd, body_path, f"Smart-Cut Kopie Clip {idx + 1}", body_dur))
            part_files.append(body_path)
            temp_files.append(body_path)
        else:
            # Komplettes Segment kopieren (oder sehr kurzer Clip komplett passend encodieren)
            part_path = work_dir / f"smart_{tag}_{idx}_full.mkv"
            if near_kf is None and dur <= 12.0:
                p_cmd = [
                    config.FFMPEG, "-y", "-hide_banner",
                    "-ss", f"{start:.3f}", "-i", str(src),
                    "-t", f"{dur:.3f}",
                    "-map", "0:v:0",
                ]
                p_cmd += _smart_match_video_args(info)
                lbl = f"Smart-Cut Kurzclip {idx + 1}"
            else:
                cut_from = near_kf if near_kf is not None else start
                p_cmd = [
                    config.FFMPEG, "-y", "-hide_banner",
                    "-ss", f"{cut_from:.3f}", "-i", str(src),
                    "-t", f"{max(0.05, end - cut_from):.3f}",
                    "-map", "0:v:0", "-c:v", "copy",
                ]
                lbl = f"Smart-Cut Kopie Clip {idx + 1}"
            _map_copy_audio_subs(p_cmd, 0, sel, sub_sel, "mkv", src)
            if sel:
                p_cmd += ["-c:a", "copy"]
            p_cmd += ["-avoid_negative_ts", "make_zero", "-progress", "pipe:1", "-nostats", str(part_path)]
            part_jobs.append((p_cmd, part_path, lbl, dur))
            part_files.append(part_path)
            temp_files.append(part_path)

    listfile = work_dir / f"smart_{tag}_concat.txt"
    lines = []
    for pf in part_files:
        escaped_pf = str(pf).replace("'", "'\\''")
        lines.append(f"file '{escaped_pf}'")
    listfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_files.append(listfile)

    meta_path = None
    if add_chapters and len(segments) >= 1:
        meta_path = remux.write_chapter_meta(chapters_from_segments(segments), work_dir)
        if meta_path:
            temp_files.append(meta_path)

    final_cmd = [
        config.FFMPEG, "-y", "-hide_banner",
        "-f", "concat", "-safe", "0", "-i", str(listfile),
    ]
    if meta_path:
        final_cmd += ["-i", str(meta_path), "-map_chapters", "1"]
    final_cmd += ["-map", "0:v:0", "-c:v", "copy"]
    if sel:
        final_cmd += ["-map", "0:a?", "-c:a", "copy"]
    else:
        final_cmd += ["-an"]
    if sub_sel:
        for out_i in range(len(sub_sel)):
            final_cmd += ["-map", f"0:s:{out_i}?"]
        if container == "mp4":
            final_cmd += ["-c:s", "mov_text"]
        else:
            final_cmd += ["-c:s", "copy"]
    final_cmd += ["-reset_timestamps", "1", "-progress", "pipe:1", "-nostats", str(output)]
    return part_jobs, final_cmd, temp_files, ""


def render_transition_preview(
    segments: list[dict],
    cut_index: int = 0,
    crossfade: float = 0.0,
    transition: str = "fade",
    overlays: Optional[list[dict]] = None,
) -> tuple[Optional[Path], str]:
    """Kurze 5-Sekunden-Vorschau (720p H.264 MP4) rund um eine Schnittkante rendern."""
    segs, err = normalize_segments(segments)
    if err:
        return None, err
    ovs, err_ov = normalize_overlays(overlays or [])
    if err_ov:
        return None, err_ov
    if len(segs) < 1:
        return None, "Mindestens ein Clip nötig."
    idx = max(0, min(len(segs) - 2, int(cut_index or 0))) if len(segs) >= 2 else 0
    sub_segs = []
    # Letzte 2.5s von Clip idx und erste 2.5s von Clip idx+1
    s_a = dict(segs[idx])
    dur_a = max(0.2, float(s_a["end"]) - float(s_a["start"]))
    if dur_a > 2.8:
        s_a["start"] = float(s_a["end"]) - 2.5
        s_a["fade_in"] = 0.0
    s_a["scale"] = min(int(s_a.get("scale") or 720) or 720, 720)
    sub_segs.append(s_a)

    if len(segs) >= 2 and idx + 1 < len(segs):
        s_b = dict(segs[idx + 1])
        dur_b = max(0.2, float(s_b["end"]) - float(s_b["start"]))
        if dur_b > 2.8:
            s_b["end"] = float(s_b["start"]) + 2.5
            s_b["fade_out"] = 0.0
        s_b["scale"] = min(int(s_b.get("scale") or 720) or 720, 720)
        sub_segs.append(s_b)

    out_dir = _cache_dir() / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"trans_{uuid.uuid4().hex[:10]}.mp4"
    cmd, cerr = build_editor_encode_cmd(
        sub_segs, out_file,
        platform="cpu", codec="h264", cq=23,
        audio_codec="aac", audio_bitrate=128,
        crossfade=crossfade, transition=transition,
        container="mp4", encoder_speed="fast",
        tonemap=True, preserve_hdr=False,
        overlays=[], add_chapters=False,
    )
    if cerr:
        return None, cerr
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        out_file.unlink(missing_ok=True)
        return None, str(e)
    if proc.returncode != 0 or not out_file.is_file() or out_file.stat().st_size <= 0:
        out_file.unlink(missing_ok=True)
        return None, (proc.stderr or "")[-400:] or "Vorschau-Render fehlgeschlagen"
    return out_file, ""


def extract_snapshot(rel: str, t: float = 0.0) -> tuple[Optional[Path], str, str]:
    """Einzelbild (PNG) an Sekunde t extrahieren. Liefert (path, download_name, err)."""
    target = resolve_path(rel)
    if target is None or not target.is_file():
        return None, "", "Datei nicht gefunden"
    sec = max(0.0, float(t or 0.0))
    out_dir = _cache_dir() / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"snap_{uuid.uuid4().hex[:10]}.png"
    cmd = [
        config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{sec:.3f}", "-i", str(target),
        "-frames:v", "1", "-fps_mode", "passthrough",
        str(out_file),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        out_file.unlink(missing_ok=True)
        return None, "", str(e)
    if proc.returncode != 0 or not out_file.is_file() or out_file.stat().st_size <= 0:
        out_file.unlink(missing_ok=True)
        return None, "", (proc.stderr or "")[-300:] or "Snapshot fehlgeschlagen"
    stamp = f"{int(sec // 60):02d}m{sec % 60:05.2f}s".replace(".", "_")
    dl_name = f"{target.stem}_{stamp}.png"
    return out_file, dl_name, ""


def resolve_asset(key: str, name: str) -> Optional[Path]:
    if not key or not name or "/" in key or "\\" in key or ".." in key:
        return None
    if name not in ("filmstrip.jpg", "wave.png"):
        return None
    p = (_cache_dir() / key / name).resolve()
    root = _cache_dir().resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None
