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
    work = config.WORK_DIR / f"chunk_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    enc_kw = dict(enc_kw or {})
    # Chunks sind reines Video – Audio/Untertitel/Kapitel kommen beim finalen Mux.
    for k in ("audio_mode",):
        enc_kw[k] = "none"
    enc_kw.update(keep_subtitles=False, subtitle_per_track=False,
                  keep_chapters=False, keep_metadata=False)
    enc_kw.pop("audio_per_track", None)
    enc_kw.pop("audio_track_settings", None)

    try:
        bounds = _chunk_bounds(info.duration, getattr(s, "chunk_seconds", 60))
        n = len(bounds)
        if status:
            status(f"Chunked: {n} Segmente werden analysiert …")

        # 1) Komplexität je Segment schätzen.
        bitrates: list[float] = []
        for i, (start, length) in enumerate(bounds):
            if cancelled():
                return False, "Abgebrochen"
            bitrates.append(_source_chunk_bitrate(info, start, length, work, i))
        valid = sorted(b for b in bitrates if b > 0)
        median = valid[len(valid) // 2] if valid else 0.0
        rng = max(0, int(getattr(s, "chunk_cq_range", 6)))

        # 2) Segmente einzeln encodieren.
        seg_files: list[Path] = []
        for i, (start, length) in enumerate(bounds):
            if cancelled():
                return False, "Abgebrochen"
            cq = _adaptive_cq(int(s.quality), bitrates[i], median, rng)
            seg = work / f"seg_{i:04d}.mkv"
            if status:
                status(f"Chunked: Segment {i + 1}/{n} (CQ {cq}) …")
            cmd = build_encode_cmd(
                info, seg, s.platform, s.codec, cq,
                s.target_height, s.tonemap,
                start_at=start, duration_limit=length,
                include_progress=True, **enc_kw,
            )
            runner = EncodeRunner(on_progress=_seg_progress(progress, i, n))
            set_active(runner)
            rc, stderr = runner.run(cmd, length)
            if runner._cancel:
                return False, "Abgebrochen"
            if rc != 0 or not seg.exists():
                return False, (f"Segment {i + 1} fehlgeschlagen (Exit {rc}): "
                               f"{(stderr or '')[-400:]}")
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
        return True, ""
    finally:
        set_active(None)
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
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-i", str(info.path),
           "-map", "0:v:0", "-c:v", "copy"]
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
    cont = "mp4" if str(output).lower().endswith(".mp4") else "mkv"
    if getattr(s, "subtitle_per_track", False):
        cmd += _remap_sub_args(ff.subtitle_track_args(
            list(getattr(s, "subtitle_track_settings", []) or []), info, cont))
    elif s.keep_subtitles:
        cmd += ff.subtitle_copy_args(info, 1, cont)
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
