"""Per-Szene / Chunked Adaptive Encoding.

Der Film wird in Segmente zerlegt, jedes Segment erhält einen an die
Komplexität angepassten CQ-Wert (aufwändige Szenen → bessere Qualität, ruhige
Szenen → kleiner) und wird einzeln encodiert. Anschließend werden die Segmente
verlustfrei zusammengefügt und Audio/Untertitel/Kapitel aus der Quelle gemuxt.

Die Komplexität wird günstig über die Quell-Bitrate je Segment geschätzt
(variable Bitrate der Quelle korreliert gut mit Detail/Bewegung) – ohne teure
Test-Encodes. So bleibt der Durchsatz praxistauglich.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import config
from . import ffmpeg_utils as ff
from .encoder import EncodeProgress, EncodeRunner, build_encode_cmd
from .ffmpeg_utils import VideoInfo

logger = logging.getLogger("vcompress.chunked")

StatusCb = Optional[Callable[[str], None]]


def _chunk_bounds(duration: float, chunk_seconds: int) -> list[tuple[float, float]]:
    if duration <= 0:
        return [(0.0, 0.0)]
    chunk = max(15, int(chunk_seconds))
    bounds: list[tuple[float, float]] = []
    t = 0.0
    while t < duration - 0.5:
        length = min(chunk, duration - t)
        bounds.append((round(t, 3), round(length, 3)))
        t += chunk
    return bounds or [(0.0, duration)]


def _probe_keyframes(info: VideoInfo, timeout: float = 25.0) -> list[float]:
    """Liest Keyframe-Zeitstempel rein auf Demuxer-Ebene (ohne Decode)."""
    cmd = [
        config.FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
        str(info.path),
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
        if res.returncode != 0 or not res.stdout:
            return []
        pts: list[float] = []
        for line in res.stdout.splitlines():
            parts = line.strip().split(",")
            if len(parts) < 2 or "K" not in parts[1]:
                continue
            try:
                t = float(parts[0])
                if math.isfinite(t) and t >= 0:
                    pts.append(t)
            except ValueError:
                continue
        return sorted(set(round(x, 3) for x in pts))
    except (OSError, subprocess.SubprocessError):
        return []


def _scene_chunk_bounds(info: VideoInfo, chunk_seconds: int,
                        scene_align: bool = True) -> list[tuple[float, float]]:
    """Teilt entlang echter Keyframes/Szenenwechsel der Quelle statt starrer Blöcke."""
    duration = float(info.duration or 0.0)
    if duration <= 0 or not scene_align:
        return _chunk_bounds(duration, chunk_seconds)
    target = max(15.0, float(chunk_seconds))
    if duration <= target * 1.35:
        return [(0.0, round(duration, 3))]
    kfs = _probe_keyframes(info)
    if len(kfs) < 2:
        return _chunk_bounds(duration, chunk_seconds)
    min_len = max(12.0, target * 0.55)
    max_len = max(min_len + 5.0, target * 1.5)
    bounds: list[tuple[float, float]] = []
    t = 0.0
    while t < duration - 0.5:
        rem = duration - t
        if rem <= max_len:
            bounds.append((round(t, 3), round(rem, 3)))
            break
        lo = t + min_len
        hi = min(duration - 4.0, t + max_len)
        cands = [k for k in kfs if lo <= k <= hi]
        if cands:
            ideal = t + target
            nxt = min(cands, key=lambda k: abs(k - ideal))
        else:
            nxt = min(duration, t + target)
        bounds.append((round(t, 3), round(nxt - t, 3)))
        t = nxt
    return bounds or _chunk_bounds(duration, chunk_seconds)


def _resume_work_dir(info: VideoInfo, s, enc_kw: dict) -> Path:
    """Deterministisches Arbeitsverzeichnis für Crash-Resume eines Chunked-Jobs."""
    try:
        st = Path(info.path).stat()
        src_sig = f"{info.path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        src_sig = f"{info.path}|{info.size_bytes}"
    sig_obj = {
        "src": src_sig,
        "platform": getattr(s, "platform", "cpu"),
        "codec": getattr(s, "codec", "av1"),
        "quality": int(getattr(s, "quality", 28)),
        "height": getattr(s, "target_height", None),
        "tonemap": bool(getattr(s, "tonemap", False)),
        "chunk_seconds": int(getattr(s, "chunk_seconds", 60)),
        "chunk_cq_range": int(getattr(s, "chunk_cq_range", 6)),
        "scene_align": bool(getattr(s, "chunk_scene_align", True)),
        "speed": getattr(s, "encoder_speed", "balanced"),
        "crop": enc_kw.get("crop") or "",
        "denoise": enc_kw.get("denoise") or "off",
        "sharpen": enc_kw.get("sharpen") or "off",
        "deband": enc_kw.get("deband") or "off",
        "film_grain": enc_kw.get("film_grain") or 0,
    }
    digest = hashlib.sha256(
        json.dumps(sig_obj, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:12]
    return config.WORK_DIR / f"chunk_resume_{digest}"


def _valid_segment(seg: Path, expected_len: float) -> bool:
    """Prüft, ob ein vorhandenes Segment aus einem früheren Lauf intakt ist."""
    try:
        if not seg.is_file() or seg.stat().st_size < 1024:
            return False
    except OSError:
        return False
    probed = ff.ffprobe(seg)
    if probed is None or probed.width <= 0:
        return False
    if expected_len > 2.0 and probed.duration > 0:
        return abs(probed.duration - expected_len) <= max(2.5, expected_len * 0.15)
    return True


def _source_chunk_bitrate(info: VideoInfo, start: float, length: float,
                          work: Path, idx: int) -> float:
    """Bitrate eines Quell-Segments (bit/s) über einen schnellen Stream-Copy."""
    if length <= 0:
        return 0.0
    tmp = work / f"probe_{idx}.mkv"
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", str(start), "-t", str(length), "-i", str(info.path),
           "-map", "0:v:0", "-c", "copy", str(tmp)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        size = tmp.stat().st_size if tmp.exists() else 0
    except (OSError, subprocess.SubprocessError):
        size = 0
    finally:
        tmp.unlink(missing_ok=True)
    return (size * 8 / length) if (size and length) else 0.0


def _adaptive_cq(base_cq: int, br: float, median: float, rng: int) -> int:
    """CQ je Segment relativ zur Median-Komplexität (±rng)."""
    if br <= 0 or median <= 0:
        return base_cq
    # log2-Verhältnis: doppelte Bitrate ~ 1 Stufe komplexer.
    delta = round(max(-rng, min(rng, math.log2(br / median) * (rng / 2.0))))
    # Komplexer (delta>0) → niedrigerer CQ (bessere Qualität).
    return max(1, base_cq - delta)


def encode(
    info: VideoInfo,
    output: Path,
    s,
    *,
    set_active: Callable[[Optional[EncodeRunner]], None],
    cancelled: Callable[[], bool],
    status: StatusCb = None,
    progress: Optional[Callable[[dict], None]] = None,
    enc_kw: Optional[dict] = None,
) -> tuple[bool, str]:
    """Chunked-Encode ausführen. Liefert (ok, Fehlermeldung)."""
    enc_kw = dict(enc_kw or {})
    work = _resume_work_dir(info, s, enc_kw) if getattr(s, "chunk_resume", True) else (
        config.WORK_DIR / f"chunk_{uuid.uuid4().hex[:8]}"
    )
    work.mkdir(parents=True, exist_ok=True)
    # Chunks sind reines Video – Audio/Untertitel/Kapitel kommen beim finalen Mux.
    for k in ("audio_mode",):
        enc_kw[k] = "none"
    enc_kw.update(keep_subtitles=False, subtitle_per_track=False,
                  keep_chapters=False, keep_metadata=False)
    enc_kw.pop("audio_per_track", None)
    enc_kw.pop("audio_track_settings", None)

    completed_ok = False
    try:
        manifest_path = work / "manifest.json"
        bounds: list[tuple[float, float]] = []
        bitrates: list[float] = []
        if manifest_path.is_file():
            try:
                saved = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (isinstance(saved, dict)
                        and isinstance(saved.get("bounds"), list)
                        and isinstance(saved.get("bitrates"), list)
                        and len(saved["bounds"]) == len(saved["bitrates"]) > 0):
                    bounds = [(float(a), float(b)) for a, b in saved["bounds"]]
                    bitrates = [float(x) for x in saved["bitrates"]]
            except (OSError, ValueError, TypeError):
                bounds, bitrates = [], []

        if not bounds:
            scene_align = bool(getattr(s, "chunk_scene_align", True))
            bounds = _scene_chunk_bounds(
                info, getattr(s, "chunk_seconds", 60), scene_align=scene_align
            )
            n = len(bounds)
            if status:
                mode_lbl = "szenengenau" if scene_align else "feste Blöcke"
                status(f"Chunked ({mode_lbl}): {n} Segmente werden analysiert …")
            for i, (start, length) in enumerate(bounds):
                if cancelled():
                    return False, "Abgebrochen"
                bitrates.append(_source_chunk_bitrate(info, start, length, work, i))
            try:
                manifest_path.write_text(
                    json.dumps({"bounds": bounds, "bitrates": bitrates}),
                    encoding="utf-8",
                )
            except OSError:
                pass

        n = len(bounds)
        valid = sorted(b for b in bitrates if b > 0)
        median = valid[len(valid) // 2] if valid else 0.0
        rng = max(0, int(getattr(s, "chunk_cq_range", 6)))

        # 2) Segmente einzeln encodieren (mit Crash-/Abbruch-Resume).
        seg_files: list[Path] = []
        resumed_count = 0
        for i, (start, length) in enumerate(bounds):
            if cancelled():
                return False, "Abgebrochen"
            cq = _adaptive_cq(int(s.quality), bitrates[i], median, rng)
            seg = work / f"seg_{i:04d}.mkv"
            if _valid_segment(seg, length):
                resumed_count += 1
                seg_files.append(seg)
                if progress:
                    progress({
                        "percent": round(min(99.0, ((i + 1) / n) * 100.0), 1),
                        "fps": 0.0, "bitrate": "—", "speed": "Resume",
                        "eta": 0, "eta_human": "—",
                        "current_size": sum(p.stat().st_size for p in seg_files if p.exists()),
                        "current_human": ff.human_size(sum(p.stat().st_size for p in seg_files if p.exists())),
                        "saved_human": "—",
                    })
                continue
            part = work / f"seg_{i:04d}.part.mkv"
            part.unlink(missing_ok=True)
            resume_note = f" · {resumed_count} wiederaufgenommen" if resumed_count else ""
            if status:
                status(f"Chunked: Segment {i + 1}/{n} (CQ {cq}{resume_note}) …")
            cmd = build_encode_cmd(
                info, part, s.platform, s.codec, cq,
                s.target_height, s.tonemap,
                start_at=start, duration_limit=length,
                include_progress=True, **enc_kw,
            )
            runner = EncodeRunner(on_progress=_seg_progress(progress, i, n))
            set_active(runner)
            rc, stderr = runner.run(cmd, length)
            if runner._cancel:
                part.unlink(missing_ok=True)
                return False, "Abgebrochen"
            if rc != 0 or not part.exists():
                part.unlink(missing_ok=True)
                return False, (f"Segment {i + 1} fehlgeschlagen (Exit {rc}): "
                               f"{(stderr or '')[-400:]}")
            part.replace(seg)
            seg_files.append(seg)

        # 3) Segmente verlustfrei zusammenfügen.
        if status:
            status("Chunked: Segmente werden zusammengefügt …")
        concat_video = work / "joined.mkv"
        listfile = work / "list.txt"
        listfile.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in seg_files), encoding="utf-8")
        rc = _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "concat", "-safe", "0", "-i", str(listfile),
                   "-c", "copy", str(concat_video)], "Concat")
        if rc != 0 or not concat_video.exists():
            return False, "Zusammenfügen der Segmente fehlgeschlagen"

        # 4) Finaler Mux: Video (copy) + Audio/Untertitel/Kapitel aus der Quelle.
        if status:
            status("Chunked: Audio/Untertitel werden gemuxt …")
        rc = _run(_mux_cmd(info, concat_video, output, s), "Final-Mux")
        if rc != 0 or not output.exists():
            return False, "Finaler Mux fehlgeschlagen"
        completed_ok = True
        return True, ""
    finally:
        set_active(None)
        if completed_ok or not getattr(s, "chunk_resume", True):
            shutil.rmtree(work, ignore_errors=True)


def _seg_progress(progress, idx: int, total: int):
    if progress is None:
        return None

    def cb(p: EncodeProgress) -> None:
        base = (idx / total) * 100.0
        within = (p.percent / total) if p.percent else 0.0
        progress({
            "percent": round(min(99.0, base + within), 1),
            "fps": round(p.fps, 1), "bitrate": p.bitrate, "speed": p.speed,
            "eta": 0, "eta_human": "—",
            "current_size": p.current_size,
            "current_human": ff.human_size(p.current_size),
            "saved_human": "—",
        })
    return cb


def _mux_cmd(info: VideoInfo, video: Path, output: Path, s) -> list[str]:
    """Concat-Video (copy) mit Audio/Untertitel/Kapitel der Quelle zusammenmuxen."""
    cont = "mp4" if str(output).lower().endswith(".mp4") else "mkv"
    raw_ext = list(getattr(s, "_sidecar_subs_cache", None) or [])
    ext_subs = []
    for sub in raw_ext:
        p = str(sub.get("path") or "").strip()
        if not p or not Path(p).is_file():
            continue
        is_bmp = bool(sub.get("bitmap")) or Path(p).suffix.lower() in (".sup", ".sub", ".idx")
        if cont == "mp4" and is_bmp:
            continue
        ext_subs.append(sub)

    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-i", str(info.path)]
    for sub in ext_subs:
        cmd += ["-i", str(sub["path"])]
    cmd += ["-map", "0:v:0", "-c:v", "copy"]
    # Audio (aus Quelle = Input 1). Per-Spur oder global – analog zum Encoder.
    if s.audio_mode == "none":
        cmd += ["-an"]
    elif getattr(s, "audio_per_track", False):
        tracks = list(getattr(s, "audio_track_settings", []) or [])
        cmd += _remap_audio_args(ff.audio_track_args(tracks))
    else:
        if getattr(s, "audio_tracks", None):
            for idx in s.audio_tracks:
                cmd += ["-map", f"1:a:{int(idx)}?"]
        else:
            cmd += ["-map", "1:a?"]
        cmd += ff.audio_args(s.audio_mode, s.audio_codec, s.audio_bitrate,
                             s.audio_channels, s.audio_normalize)
    # Untertitel/Kapitel/Metadaten aus der Quelle (container-abhängig).
    internal_sub_count = 0
    if getattr(s, "subtitle_per_track", False):
        sub_args = _remap_sub_args(ff.subtitle_track_args(
            list(getattr(s, "subtitle_track_settings", []) or []), info, cont))
        internal_sub_count = sum(1 for a in sub_args if a.startswith("1:s:"))
        if internal_sub_count or not ext_subs:
            cmd += sub_args
    elif s.keep_subtitles:
        sub_args = ff.subtitle_copy_args(info, 1, cont)
        if "-sn" not in sub_args:
            internal_sub_count = len(info.subtitles or [])
            cmd += sub_args
        elif not ext_subs:
            cmd += sub_args
    elif not ext_subs:
        cmd += ["-sn"]
    for i, sub in enumerate(ext_subs):
        in_idx = 2 + i
        out_s = internal_sub_count + i
        p_ext = Path(str(sub["path"])).suffix.lower()
        is_bmp = bool(sub.get("bitmap")) or p_ext in (".sup", ".sub", ".idx")
        sub_codec = "mov_text" if cont == "mp4" else ("copy" if is_bmp or p_ext in (".srt", ".ass", ".ssa") else "srt")
        cmd += ["-map", f"{in_idx}:s:0?", f"-c:s:{out_s}", sub_codec]
        lang = ff.lang_to_iso3(str(sub.get("language") or "")) or "und"
        cmd += [f"-metadata:s:s:{out_s}", f"language={lang}"]
        title = str(sub.get("title") or "").strip()
        if title:
            cmd += [f"-metadata:s:s:{out_s}", f"title={title}"]
        dispo = [flag for flag in ("default", "forced") if sub.get(flag)]
        cmd += [f"-disposition:s:{out_s}", "+".join(dispo) if dispo else "0"]
    cmd += ["-map_chapters", "1" if s.keep_chapters else "-1"]
    cmd += ["-map_metadata", "1" if s.keep_metadata else "-1"]
    cmd += [str(output)]
    return cmd


def _remap_audio_args(args: list[str]) -> list[str]:
    """audio_track_args nutzt Input 0 (0:a:…) – hier auf Input 1 (Quelle) umbiegen."""
    return ["1:a:" + a.split("0:a:", 1)[1] if a.startswith("0:a:") else a for a in args]


def _remap_sub_args(args: list[str]) -> list[str]:
    return ["1:s:" + a.split("0:s:", 1)[1] if a.startswith("0:s:") else a for a in args]


def _run(cmd: list[str], label: str) -> int:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=False)
        if res.returncode != 0:
            logger.warning("%s fehlgeschlagen (Exit %s): %s", label, res.returncode,
                           (res.stderr or "")[-500:])
        return res.returncode
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("%s – Ausnahme: %s", label, e)
        return 1
