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
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from . import config, ffmpeg_utils as ff
from . import remux

logger = logging.getLogger("vcompress.editor")

_GEN_KINDS = ("black", "silence")
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
    """Dauer auf der Ausgabe-Timeline (nach Tempo)."""
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
        if kind not in ("media", "black", "silence"):
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
        mute = bool(s.get("mute")) or kind in _GEN_KINDS
        if mute:
            alist = []
        elif not alist:
            mute = True
        sidx = s.get("sub_index", -1)
        try:
            sidx = int(sidx)
        except (TypeError, ValueError):
            sidx = -1
        slist = [] if kind in _GEN_KINDS else _audio_list(s.get("sub_indexes"), sidx)
        title = str(s.get("title") or "").strip() or f"Clip {i + 1}"
        crop = str(s.get("crop") or "").strip()
        try:
            scale = int(s.get("scale") or 0)
        except (TypeError, ValueError):
            scale = 0
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
            "scale": max(0, min(4320, scale)),
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
    if (s.get("kind") or "media") in _GEN_KINDS:
        return True
    if _f(s.get("fade_in")) > 0 or _f(s.get("fade_out")) > 0:
        return True
    if abs(_f(s.get("speed"), 1.0) - 1.0) > 0.001:
        return True
    if s.get("crop") or int(s.get("scale") or 0) > 0:
        return True
    if s.get("burn_subs"):
        return True
    return False


def any_needs_encode(segments: list[dict], crossfade: float = 0.0) -> bool:
    if float(crossfade or 0) > 0.01:
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
        "scale": int(s.get("scale") or 0),
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
                   audio_channels: int = 0) -> bool:
    """Ton per Concat-Copy möglich: gleiche Spuren, keine Audio-Filter."""
    if int(audio_channels or 0) in (1, 2):
        return False
    if float(crossfade or 0) > 0.01:
        return False
    sels = {tuple(s.get("audio_indexes") or []) for s in segments}
    if len(sels) > 1:
        return False
    for s in segments:
        if (s.get("kind") or "media") in _GEN_KINDS:
            return False
        if _f(s.get("fade_in")) > 0 or _f(s.get("fade_out")) > 0:
            return False
        if abs(_f(s.get("speed"), 1.0) - 1.0) > 0.001:
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


def _ref_geometry(segments: list[dict]) -> tuple[int, int, float]:
    """Ziel-Auflösung/FPS für Concat (erste Mediendatei, sonst 1080p25)."""
    for s in segments:
        p = s.get("abs")
        if not p:
            continue
        info, _ = ff.probe_with_error(p)
        if info and getattr(info, "width", 0):
            fps = float(getattr(info, "fps", 0) or 0) or 25.0
            return int(info.width), int(info.height), fps
    return 1920, 1080, 25.0


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
    rate_mode: str = "cq",
    v_bitrate: int = 0,
    audio_channels: int = 0,
    work_dir: Optional[Path] = None,
    container: str = "mkv",
) -> tuple[list, str]:
    """Re-Encode-Export: trim/atrim je Segment, optional xfade, dann concat."""
    if not segments:
        return [], "Keine Segmente."
    enc = ff.encoder_name(platform, codec)
    backend = ff.encoder_backend(platform)
    n = len(segments)
    xf = max(0.0, float(crossfade or 0))
    if n < 2:
        xf = 0.0
    tw, th, tfps = _ref_geometry(segments)

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
        segments, xf, int(audio_channels or 0))
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
                f"[{li}:v]format=yuv420p,setsar=1,fps={tfps:.3f}[{vlabel}]")
            mute = True
        else:
            ii = file_idx(s["abs"])
            chain = [f"trim=start={start:.3f}:end={end:.3f}", "setpts=PTS-STARTPTS"]
            if s.get("burn_subs") or (burn_subs and int(s.get("sub_index", sub_index)) >= 0
                                      and i == 0):
                si = int(s.get("sub_index", sub_index) if s.get("sub_index", -1) >= 0
                         else sub_index)
                if si >= 0 and s.get("abs"):
                    chain.insert(
                        0,
                        f"subtitles='{_esc_sub_path(s['abs'])}':si={si}",
                    )
            crop = str(s.get("crop") or "").strip()
            if crop and ":" in crop:
                chain.append(f"crop={crop}")
            sc = int(s.get("scale") or 0)
            if sc > 0:
                chain.append(f"scale=-2:{sc}")
            if abs(speed - 1.0) > 0.001:
                chain.append(f"setpts=PTS/{speed:.4f}")
            chain.append(f"scale={tw}:{th}:force_original_aspect_ratio=decrease")
            chain.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2")
            chain.append("format=yuv420p")
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
                    f"aformat=sample_fmts=fltp:channel_layouts={lay}",
                    "aresample=48000",
                ]
                at = _atempo_chain(speed)
                if at:
                    achain.append(at)
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

    if xf > 0.02 and n >= 2:
        cur_v = v_pads[0]
        cur_a = list(a_pads[0]) if a_pads else []
        acc = durs[0]
        for i in range(1, n):
            d = min(xf, durs[i] * 0.45, durs[i - 1] * 0.45)
            off = max(0.0, acc - d)
            nv = f"xfv{i}"
            parts.append(
                f"[{cur_v}][{v_pads[i]}]xfade=transition=fade:duration={d:.3f}"
                f":offset={off:.3f}[{nv}]")
            if not copy_audio:
                for j in range(n_audio):
                    na = f"xfa{i}_{j}"
                    parts.append(
                        f"[{cur_a[j]}][{a_pads[i][j]}]acrossfade=d={d:.3f}[{na}]")
                    cur_a[j] = na
            cur_v = nv
            acc = acc + durs[i] - d
        filt = ";".join(parts)
        v_out, a_outs = cur_v, (cur_a if not copy_audio else [])
    elif copy_audio:
        concat = "".join(f"[{v}]" for v in v_pads)
        filt = ";".join(parts)
        filt += ";" + concat + f"concat=n={n}:v=1:a=0[vc]"
        v_out, a_outs = "vc", []
    else:
        concat = "".join(
            f"[{v}]" + "".join(f"[{a}]" for a in al)
            for v, al in zip(v_pads, a_pads))
        filt = ";".join(parts)
        a_outs = [f"ac{j}" for j in range(n_audio)]
        filt += (";" + concat + f"concat=n={n}:v=1:a={n_audio}[vc]"
                 + "".join(f"[{a}]" for a in a_outs))
        v_out = "vc"

    if backend == "vaapi":
        filt += f";[{v_out}]format=nv12,hwupload[v]"
        map_v = "[v]"
    else:
        map_v = f"[{v_out}]"

    cmd += ["-filter_complex", filt, "-map", map_v]
    for a in a_outs:
        cmd += ["-map", f"[{a}]"]
    cmd += ["-c:v", enc]
    br = int(v_bitrate or 0)
    rm = str(rate_mode or "cq").lower()
    if rm in ("bitrate", "abr") and br > 0:
        cmd += ff.bitrate_args(platform, codec, br, abr=(rm == "abr"))
    else:
        if backend == "nvenc":
            cmd += ["-rc", "vbr"]
        cmd += ff.quality_args(platform, int(cq or 30))
    if backend == "nvenc":
        cmd += ["-preset", "p5"]
    elif enc in ("libsvtav1", "libx265", "libx264"):
        cmd += ["-preset", "6" if enc == "libsvtav1" else "medium"]

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
    """Filmstrip + Waveform erzeugen (Cache)."""
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
    n = max(8, min(48, int(duration / 8) if duration else 24))
    if not strip.is_file() and duration > 0:
        fps = max(0.02, n / duration)
        cmd = [
            config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(target),
            "-vf", f"fps={fps:.5f},scale=80:45:force_original_aspect_ratio=decrease,"
                   f"pad=80:45:(ow-iw)/2:(oh-ih)/2,tile={n}x1",
            "-frames:v", "1", str(strip),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not wave.is_file():
        cmd = [
            config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(target),
            "-filter_complex",
            "aformat=channel_layouts=mono,compand,showwavespic=s=1280x48:colors=#22d3ee",
            "-frames:v", "1", str(wave),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "key": key,
        "tiles": n,
        "filmstrip": f"/api/editor/asset/{key}/filmstrip.jpg" if strip.is_file() else "",
        "waveform": f"/api/editor/asset/{key}/wave.png" if wave.is_file() else "",
        "duration": duration,
    }


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
