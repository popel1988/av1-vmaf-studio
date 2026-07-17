"""Super-Tool: geführter, autonomer Stapel-Assistent (FileFlows-artig).

Scannt einen Ordner rekursiv nach Videos (mit Format-/Bibliotheksfiltern) und
legt die Treffer je nach Qualitätsmodus als Batch-Gruppe in die Warteschlange:

- ``target_vmaf``   : jede Datei bekommt eine eigene VMAF-Analyse (auto),
                      empfohlen wird der effizienteste Wert mit VMAF >= Ziel.
- ``representative``: eine VMAF-Analyse pro Gruppe (erste Datei), der ermittelte
                      Wert wird auf alle weiteren angewendet.
- ``fixed``         : fixer CQ/Bitrate ohne VMAF-Analyse.
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff

logger = logging.getLogger("vcompress.supertool")

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


def start_scan(filters: dict) -> bool:
    """Startet einen Scan, sofern nicht bereits einer läuft."""
    global _thread
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, total=0, scanned=0,
                      matched=[], error="")
    _thread = threading.Thread(target=_run, args=(filters or {},), daemon=True)
    _thread.start()
    return True


def _run(filters: dict) -> None:
    try:
        base = config.INPUT_DIR
        folder = str(filters.get("folder") or "")
        root = (base / folder.lstrip("/")).resolve() if folder else base.resolve()
        try:
            root.relative_to(base.resolve())
        except ValueError:
            root = base.resolve()

        # Format-/Container-Filter: gewählte Endungen (ohne Punkt) oder alle.
        exts_raw = [str(e).lower().lstrip(".") for e in (filters.get("extensions") or [])]
        allowed = {"." + e for e in exts_raw if e} or set(config.VIDEO_EXTENSIONS)

        files = [f for f in root.rglob("*")
                 if f.is_file() and f.suffix.lower() in allowed]
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

        for f in files:
            with _lock:
                _state["scanned"] += 1
            try:
                if name_contains and name_contains not in f.name.lower():
                    continue
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
            with _lock:
                _state["matched"].append({
                    "path": rel,
                    "name": f.name,
                    "size_bytes": info.size_bytes,
                    "size_human": ff.human_size(info.size_bytes),
                    "container": (info.container or "").split(",")[0],
                    "codec": info.codec,
                    "resolution": f"{info.width}x{info.height}",
                    "height": info.height,
                    "video_bitrate": info.video_bitrate,
                    "video_bitrate_human": ff._bitrate_human(info.video_bitrate),
                    "hdr_type": info.hdr_type,
                    "duration_human": ff.human_duration(info.duration),
                })
    except Exception as e:  # pragma: no cover
        logger.exception("Super-Tool-Scan fehlgeschlagen")
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True


def quick_list(filters: dict) -> dict:
    """Schnelle Dateiliste OHNE ffprobe für die Live-Vorschau neben der Ordnerwahl.

    Wendet nur die günstigen Filter an (Ordner, Format/Endung, Name enthält/
    ausschließen, Mindestgröße). Codec-/Bitraten-/Höhen-Filter benötigen einen
    Probe und greifen erst beim eigentlichen Scan.
    """
    base = config.INPUT_DIR
    folder = str(filters.get("folder") or "")
    root = (base / folder.lstrip("/")).resolve() if folder else base.resolve()
    try:
        root.relative_to(base.resolve())
    except ValueError:
        root = base.resolve()

    exts_raw = [str(e).lower().lstrip(".") for e in (filters.get("extensions") or [])]
    allowed = {"." + e for e in exts_raw if e} or set(config.VIDEO_EXTENSIONS)
    min_size = float(filters.get("min_size_mb") or 0) * 1024 * 1024
    name_contains = str(filters.get("name_contains") or "").lower()
    name_exclude = [str(t).lower() for t in (filters.get("name_exclude") or [])
                    if str(t).strip()]

    items: list[dict] = []
    truncated = False
    limit = 1000
    try:
        for f in sorted(root.rglob("*")):
            if not (f.is_file() and f.suffix.lower() in allowed):
                continue
            if name_contains and name_contains not in f.name.lower():
                continue
            if name_exclude:
                try:
                    rel_low = str(f.relative_to(base)).replace("\\", "/").lower()
                except ValueError:
                    rel_low = f.name.lower()
                if any(t in rel_low for t in name_exclude):
                    continue
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            if min_size and sz < min_size:
                continue
            if len(items) >= limit:
                truncated = True
                break
            try:
                rel = str(f.relative_to(base)).replace("\\", "/")
            except ValueError:
                rel = f.name
            items.append({
                "path": rel, "name": f.name,
                "size_bytes": sz, "size_human": ff.human_size(sz),
            })
    except OSError as e:
        return {"files": [], "count": 0, "truncated": False, "error": str(e)}
    return {"files": items, "count": len(items), "truncated": truncated, "error": ""}


def _safe_resolve(rel: str) -> Optional[Path]:
    base = config.INPUT_DIR.resolve()
    target = (base / rel.lstrip("/")).resolve() if rel else base
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def start_batch(queue, paths: list, settings: dict, mode: str) -> tuple[int, str, str]:
    """Treffer je nach Modus als Batch-Gruppe einreihen.

    Rückgabe: (Anzahl hinzugefügt, group_id, Fehlermeldung).
    """
    from .queue_manager import build_job_settings

    mode = mode if mode in ("target_vmaf", "representative", "fixed") else "representative"
    group = uuid.uuid4().hex[:8]
    batch = uuid.uuid4().hex[:8]  # Dashboard-Kennung über alle Dateien
    added = 0

    for i, rel in enumerate(paths):
        target = _safe_resolve(rel)
        if target is None or not target.is_file():
            continue
        d = dict(settings)
        d["batch_id"] = batch
        if mode == "fixed":
            d["vmaf_check"] = False
            d["workflow"] = "auto"
        elif mode == "target_vmaf":
            # Jede Datei erhält eine eigene Analyse (eigene Gruppe → i. d. R.
            # kein gemeinsamer Wert). workflow=auto encodet direkt mit dem
            # effizientesten Wert >= Ziel-VMAF.
            d["vmaf_check"] = True
            d["workflow"] = "auto"
        else:  # representative
            # Nur die erste Datei analysiert; alle übernehmen den Wert.
            d["vmaf_check"] = (i == 0)
            d["workflow"] = "auto"
        s = build_job_settings(d)
        # target_vmaf: jede Datei eigene Gruppe, damit sie einzeln analysiert.
        gid = uuid.uuid4().hex[:8] if mode == "target_vmaf" else group
        item = queue.add_file(str(target), s, group_id=gid)
        if item:
            added += 1

    if not added:
        return 0, "", "Keine gültigen Dateien gefunden."
    return added, batch, ""
