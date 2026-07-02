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
    "error": "",
}
_thread: Optional[threading.Thread] = None


def get_state() -> dict:
    with _lock:
        return {
            "running": _state["running"],
            "done": _state["done"],
            "total": _state["total"],
            "scanned": _state["scanned"],
            "matched": list(_state["matched"]),
            "error": _state["error"],
        }


def start_scan(root_rel: str, filters: dict) -> bool:
    """Startet einen Scan, sofern nicht bereits einer läuft."""
    global _thread
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, total=0, scanned=0,
                      matched=[], error="")
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
        inc = {c.lower() for c in (filters.get("codecs_include") or [])}
        exc = {c.lower() for c in (filters.get("codecs_exclude") or [])}

        for f in files:
            with _lock:
                _state["scanned"] += 1
            try:
                if name_contains and name_contains not in f.name.lower():
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
                })
    except Exception as e:  # pragma: no cover
        logger.exception("Bibliotheks-Scan fehlgeschlagen")
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True
