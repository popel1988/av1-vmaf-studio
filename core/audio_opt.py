"""Audio-Optimierungspass: erkennt aufgeblähte (verlustfreie/hochbitratige)
Tonspuren und wandelt sie platzsparend um – ohne das Video neu zu encoden.

Das spart bei Remuxes/Blu-ray-Rips oft mehrere GB pro Datei (TrueHD, DTS-HD MA,
PCM …) in Sekunden, da nur die Tonspuren transcodiert und Video/Untertitel/
Kapitel 1:1 kopiert werden (``-c:v copy``).
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff
from .ffmpeg_utils import VideoInfo

logger = logging.getLogger("vcompress.audio")

# Verlustfreie / immer als "aufgebläht" geltende Audiocodecs.
LOSSLESS = {"truehd", "mlp", "flac", "alac", "ape", "wavpack", "tak"}
# PCM-Varianten heißen pcm_s16le, pcm_s24le … – per Präfix erfasst.
PCM_PREFIX = "pcm_"
# DTS deckt sowohl DTS als auch DTS-HD MA ab (Profil nicht immer verfügbar) –
# als Kandidat behandeln, sobald die Bitrate hoch ist.
DTS = {"dts"}


def _est_source_bitrate(tr: dict) -> int:
    """Bitrate einer Tonspur in bit/s – gemessen oder für verlustfrei geschätzt."""
    br = int(tr.get("bitrate") or 0)
    if br > 0:
        return br
    ch = int(tr.get("channels") or 2) or 2
    codec = (tr.get("codec") or "").lower()
    if codec == "truehd" or codec.startswith(PCM_PREFIX):
        per_ch = 900_000       # TrueHD/PCM sind sehr groß
    elif codec in LOSSLESS or codec in DTS:
        per_ch = 700_000
    else:
        per_ch = 128_000
    return per_ch * ch


def is_bloated(tr: dict, min_bitrate_kbps: int = 700) -> bool:
    """True, wenn die Tonspur als optimierungswürdig gilt."""
    codec = (tr.get("codec") or "").lower()
    if codec in LOSSLESS or codec.startswith(PCM_PREFIX):
        return True
    est = _est_source_bitrate(tr)
    return est >= max(1, int(min_bitrate_kbps)) * 1000


def _target_bitrate_kbps(tr: dict, settings: dict) -> int:
    """Ziel-Bitrate der transcodierten Spur (kbit/s), an Kanäle gekoppelt."""
    fixed = int(settings.get("audio_bitrate") or 0)
    if fixed > 0:
        return fixed
    ch_out = int(settings.get("audio_channels") or 0) or int(tr.get("channels") or 2) or 2
    # ~96 kbit/s je Kanal ist für E-AC3/Opus transparent-nah.
    return max(96, min(1024, ch_out * 96))


def plan_tracks(info: VideoInfo, settings: dict) -> list[dict]:
    """Pro Tonspur festlegen, ob transcodiert oder kopiert wird (+ Schätzwerte)."""
    scope = settings.get("scope", "bloated")   # bloated | all
    min_br = int(settings.get("min_bitrate_kbps") or 700)
    plan: list[dict] = []
    for tr in info.audio or []:
        bloated = is_bloated(tr, min_br)
        transcode = bloated if scope == "bloated" else True
        old_br = _est_source_bitrate(tr)
        new_br = _target_bitrate_kbps(tr, settings) * 1000 if transcode else old_br
        saved = 0
        if transcode and info.duration > 0 and old_br > new_br:
            saved = int((old_br - new_br) / 8 * info.duration)
        plan.append({
            "index": tr.get("index", 0),
            "codec": tr.get("codec", "?"),
            "channels": tr.get("channels", 0),
            "language": tr.get("language", "und"),
            "bitrate_human": tr.get("bitrate_human", "—"),
            "bloated": bloated,
            "transcode": transcode,
            "est_saved_bytes": saved,
        })
    return plan


def estimate_savings(info: VideoInfo, settings: dict) -> int:
    return sum(p["est_saved_bytes"] for p in plan_tracks(info, settings))


def has_candidates(info: VideoInfo, settings: dict) -> bool:
    return any(p["transcode"] for p in plan_tracks(info, settings))


def build_remux_cmd(info: VideoInfo, output: Path, settings: dict) -> list[str]:
    """FFmpeg-Kommando: Video/Untertitel/Kapitel kopieren, Tonspuren optimieren."""
    codec = settings.get("audio_codec", "eac3")
    enc = ff.AUDIO_ENCODERS.get(codec, "eac3")
    ch_out = int(settings.get("audio_channels") or 0)
    normalize = bool(settings.get("audio_normalize"))
    plan = plan_tracks(info, settings)

    cmd = [config.FFMPEG, "-y", "-hide_banner", "-i", str(info.path),
           "-map", "0:v", "-c:v", "copy"]

    for out_idx, p in enumerate(plan):
        cmd += ["-map", f"0:a:{int(p['index'])}?"]
        if not p["transcode"]:
            cmd += [f"-c:a:{out_idx}", "copy"]
            continue
        cmd += [f"-c:a:{out_idx}", enc]
        if enc != "flac":
            br = _target_bitrate_kbps(
                {"channels": p["channels"]}, settings)
            cmd += [f"-b:a:{out_idx}", f"{br}k"]
        if ch_out in (1, 2, 6, 8):
            cmd += [f"-ac:a:{out_idx}", str(ch_out)]
        if normalize:
            cmd += [f"-filter:a:{out_idx}", "loudnorm=I=-16:TP=-1.5:LRA=11"]

    # Untertitel, Kapitel, Attachments und Metadaten unverändert übernehmen.
    cmd += ["-map", "0:s?", "-c:s", "copy",
            "-map", "0:t?", "-c:t", "copy",
            "-map_chapters", "0", "-map_metadata", "0",
            "-progress", "pipe:1", "-nostats", str(output)]
    return cmd


# --------------------------------------------------------------- Ordner-Scan
_lock = threading.RLock()
_state: dict = {
    "running": False, "done": False, "total": 0, "scanned": 0,
    "matched": [], "total_saved_bytes": 0, "error": "",
}
_thread: Optional[threading.Thread] = None


def get_state() -> dict:
    with _lock:
        return {
            "running": _state["running"], "done": _state["done"],
            "total": _state["total"], "scanned": _state["scanned"],
            "matched": list(_state["matched"]),
            "total_saved_bytes": _state["total_saved_bytes"],
            "total_saved_human": ff.human_size(_state["total_saved_bytes"]),
            "error": _state["error"],
        }


def start_scan(payload: dict) -> bool:
    global _thread
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, total=0, scanned=0,
                      matched=[], total_saved_bytes=0, error="")
    _thread = threading.Thread(target=_run_scan, args=(payload or {},), daemon=True)
    _thread.start()
    return True


def _run_scan(payload: dict) -> None:
    try:
        settings = payload.get("settings") or {}
        base = config.INPUT_DIR
        folder = str(payload.get("folder") or "")
        root = (base / folder.lstrip("/")).resolve() if folder else base.resolve()
        try:
            root.relative_to(base.resolve())
        except ValueError:
            root = base.resolve()

        exts_raw = [str(e).lower().lstrip(".") for e in (payload.get("extensions") or [])]
        allowed = {"." + e for e in exts_raw if e} or set(config.VIDEO_EXTENSIONS)
        files = [f for f in root.rglob("*")
                 if f.is_file() and f.suffix.lower() in allowed]
        with _lock:
            _state["total"] = len(files)

        for f in files:
            with _lock:
                _state["scanned"] += 1
            info, err = ff.probe_with_error(f)
            if info is None or not info.audio:
                continue
            plan = plan_tracks(info, settings)
            saved = sum(p["est_saved_bytes"] for p in plan)
            if not any(p["transcode"] for p in plan) or saved <= 0:
                continue
            try:
                rel = str(f.relative_to(base)).replace("\\", "/")
            except ValueError:
                rel = f.name
            bloated = [p for p in plan if p["transcode"]]
            with _lock:
                _state["matched"].append({
                    "path": rel,
                    "name": f.name,
                    "size_bytes": info.size_bytes,
                    "size_human": ff.human_size(info.size_bytes),
                    "duration_human": ff.human_duration(info.duration),
                    "tracks": [
                        f"{p['codec']} {p['channels']}ch ({p['bitrate_human']})"
                        for p in bloated],
                    "est_saved_bytes": saved,
                    "est_saved_human": ff.human_size(saved),
                })
                _state["total_saved_bytes"] += saved
    except Exception as e:  # pragma: no cover
        logger.exception("Audio-Scan fehlgeschlagen")
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True


def _safe_resolve(rel: str) -> Optional[Path]:
    base = config.INPUT_DIR.resolve()
    try:
        target = (base / str(rel).lstrip("/")).resolve()
        target.relative_to(base)
        return target
    except (ValueError, OSError):
        return None


def start_batch(queue, paths: list, settings: dict) -> tuple[int, str, str]:
    """Ausgewählte Dateien als Nur-Audio-Optimierung (Remux) einreihen.

    Rückgabe: (Anzahl hinzugefügt, batch_id, Fehlermeldung).
    """
    import uuid as _uuid
    from .queue_manager import build_job_settings

    batch = _uuid.uuid4().hex[:8]
    added = 0
    for rel in paths or []:
        target = _safe_resolve(rel)
        if target is None or not target.is_file():
            continue
        d = dict(settings or {})
        d.update(
            video_mode="copy",
            vmaf_check=False,
            workflow="auto",
            batch_id=batch,
            suffix=d.get("suffix", "_audioopt"),
        )
        s = build_job_settings(d)
        item = queue.add_file(str(target), s, group_id=_uuid.uuid4().hex[:8])
        if item:
            added += 1
    if not added:
        return 0, "", "Keine gültigen Dateien gefunden."
    return added, batch, ""
