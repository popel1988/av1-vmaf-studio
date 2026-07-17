"""Bibliotheks-Scan: Eingabeordner rekursiv nach Videos durchsuchen, per
ffprobe analysieren und nach Kriterien filtern (z. B. „alle H.264 > 10 Mbit/s").

Läuft als Hintergrund-Thread mit Fortschritt, da das Proben vieler Dateien
dauert. Ergebnisse werden im Speicher gehalten und per Endpoint abgefragt.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff

logger = logging.getLogger("vcompress.library")

_lock = threading.RLock()
_state: dict = {
    "running": False,
    "done": False,
    "total": 0,
    "scanned": 0,
    "matched": [],
    "total_size_bytes": 0,
    "total_saved_bytes": 0,
    "error": "",
}
_thread: Optional[threading.Thread] = None

# Codecs, die bereits als effizient gelten (kein erneutes Transcoding nötig).
_EFFICIENT_CODECS = {"av1", "libsvtav1", "av01"}


def _target_bitrate_kbps(height: int, is_hdr: bool) -> int:
    """Grobe Ziel-Videobitrate für eine qualitativ gute AV1/HEVC-Ausgabe."""
    if height <= 720:
        base = 2000
    elif height <= 1080:
        base = 4000
    elif height <= 1440:
        base = 7000
    else:
        base = 12000
    return int(base * 1.5) if is_hdr else base


def project_savings(info, target_codec: str = "av1") -> dict:
    """Schätzt, wie viel eine Datei durch Transcoding einsparen würde.

    Heuristik auf Basis auflösungsabhängiger Ziel-Bitraten. Bereits effiziente
    Codecs oder Dateien nahe der Ziel-Bitrate gelten als „schon optimiert".
    """
    codec = (info.codec or "").lower()
    src_br = info.video_bitrate or 0
    target_br = _target_bitrate_kbps(info.height, info.is_hdr) * 1000
    already = codec in _EFFICIENT_CODECS or (src_br and src_br <= target_br * 1.15)
    if already or info.duration <= 0 or src_br <= 0:
        return {"already_optimized": bool(already), "est_new_size": info.size_bytes,
                "est_saved_bytes": 0}
    src_video_bytes = int(src_br / 8 * info.duration)
    new_video_bytes = int(target_br / 8 * info.duration)
    # Rest (Audio/Untertitel/Overhead) bleibt erhalten.
    rest = max(0, info.size_bytes - src_video_bytes)
    est_new = new_video_bytes + rest
    saved = max(0, info.size_bytes - est_new)
    return {"already_optimized": False, "est_new_size": est_new,
            "est_saved_bytes": saved}


def get_state() -> dict:
    with _lock:
        return {
            "running": _state["running"],
            "done": _state["done"],
            "total": _state["total"],
            "scanned": _state["scanned"],
            "matched": list(_state["matched"]),
            "total_size_bytes": _state["total_size_bytes"],
            "total_size_human": ff.human_size(_state["total_size_bytes"]),
            "total_saved_bytes": _state["total_saved_bytes"],
            "total_saved_human": ff.human_size(_state["total_saved_bytes"]),
            "error": _state["error"],
        }


def start_scan(root_rel: str, filters: dict) -> bool:
    """Startet einen Scan, sofern nicht bereits einer läuft."""
    global _thread
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, total=0, scanned=0,
                      matched=[], total_size_bytes=0, total_saved_bytes=0, error="")
    _thread = threading.Thread(target=_run, args=(root_rel, filters or {}), daemon=True)
    _thread.start()
    return True


def _run(root_rel: str, filters: dict) -> None:
    try:
        base = config.INPUT_DIR
        root = (base / root_rel.lstrip("/")).resolve() if root_rel else base.resolve()
        try:
            root.relative_to(base.resolve())
        except ValueError:
            root = base.resolve()

        files = [f for f in root.rglob("*")
                 if f.is_file() and f.suffix.lower() in config.VIDEO_EXTENSIONS]
        with _lock:
            _state["total"] = len(files)

        min_size = float(filters.get("min_size_mb") or 0) * 1024 * 1024
        min_bitrate = float(filters.get("min_bitrate_mbps") or 0) * 1_000_000
        min_height = int(filters.get("min_height") or 0)
        name_contains = str(filters.get("name_contains") or "").lower()
        name_exclude = [str(t).lower() for t in (filters.get("name_exclude") or [])
                        if str(t).strip()]
        inc = {c.lower() for c in (filters.get("codecs_include") or [])}
        exc = {c.lower() for c in (filters.get("codecs_exclude") or [])}
        target_codec = str(filters.get("target_codec") or "av1")
        skip_optimized = bool(filters.get("skip_optimized"))
        skip_processed = bool(filters.get("skip_processed"))
        if skip_processed:
            from . import history

        for f in files:
            with _lock:
                _state["scanned"] += 1
            try:
                if name_contains and name_contains not in f.name.lower():
                    continue
                # Ausschluss: greift auf den gesamten (relativen) Pfad, damit auch
                # ganze Ordner wie „.archiv" übersprungen werden können.
                if name_exclude:
                    try:
                        rel_low = str(f.relative_to(base)).replace("\\", "/").lower()
                    except ValueError:
                        rel_low = f.name.lower()
                    if any(t in rel_low for t in name_exclude):
                        continue
                if min_size and f.stat().st_size < min_size:
                    continue
            except OSError:
                continue

            info, err = ff.probe_with_error(f)
            if info is None:
                continue
            codec = (info.codec or "").lower()
            if inc and codec not in inc:
                continue
            if exc and codec in exc:
                continue
            if min_height and info.height < min_height:
                continue
            if min_bitrate and info.video_bitrate < min_bitrate:
                continue

            try:
                rel = str(f.relative_to(base)).replace("\\", "/")
            except ValueError:
                rel = f.name

            # Bereits verarbeitete Dateien überspringen (Historie).
            if skip_processed and history.is_processed(str(f)):
                continue

            proj = project_savings(info, target_codec)
            if skip_optimized and proj["already_optimized"]:
                continue
            with _lock:
                _state["matched"].append({
                    "path": rel,
                    "name": f.name,
                    "size_bytes": info.size_bytes,
                    "size_human": ff.human_size(info.size_bytes),
                    "codec": info.codec,
                    "resolution": f"{info.width}x{info.height}",
                    "height": info.height,
                    "video_bitrate": info.video_bitrate,
                    "video_bitrate_human": ff._bitrate_human(info.video_bitrate),
                    "hdr_type": info.hdr_type,
                    "duration_human": ff.human_duration(info.duration),
                    "already_optimized": proj["already_optimized"],
                    "est_saved_bytes": proj["est_saved_bytes"],
                    "est_saved_human": ff.human_size(proj["est_saved_bytes"]),
                })
                _state["total_size_bytes"] += info.size_bytes
                _state["total_saved_bytes"] += proj["est_saved_bytes"]
    except Exception as e:  # pragma: no cover
        logger.exception("Bibliotheks-Scan fehlgeschlagen")
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True
