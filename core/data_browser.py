"""Sicherer Browser & Löschen für den persistierten Datenordner (/data)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from . import config
from .ffmpeg_utils import human_size

# Erlaubte Wurzeln unter DATA_DIR (kein Zugriff auf input/output)
DATA_ROOTS = {
    "vmaf": config.VMAF_SESSIONS_DIR,
    "previews": config.PREVIEW_DIR,
    "work": config.WORK_DIR,
}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mkv", ".mp4", ".webm", ".mov"}
JSON_EXT = {".json"}


def _safe_resolve(root_key: str, rel: str) -> Optional[Path]:
    if root_key not in DATA_ROOTS:
        return None
    base = DATA_ROOTS[root_key].resolve()
    target = (base / rel.lstrip("/")).resolve() if rel else base
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def _file_entry(root_key: str, path: Path, base: Path) -> dict:
    rel = str(path.relative_to(base)).replace("\\", "/")
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        size = 0
    entry = {
        "name": path.name,
        "rel": rel,
        "is_dir": path.is_dir(),
        "size": size,
        "size_human": human_size(size) if path.is_file() else "—",
        "ext": ext,
    }
    if path.is_file():
        if ext in IMAGE_EXT:
            if root_key == "previews":
                entry["preview_url"] = f"/api/preview/{rel}"
            else:
                entry["preview_url"] = f"/api/data/file?root={root_key}&path={rel}"
        elif ext in JSON_EXT:
            entry["kind"] = "json"
        elif ext in VIDEO_EXT:
            entry["kind"] = "video"
    return entry


def browse(root_key: str, rel: str = "") -> dict:
    target = _safe_resolve(root_key, rel)
    if target is None:
        return {"error": "Ungültiger Pfad"}
    if not target.exists():
        return {"error": "Pfad nicht gefunden"}
    if not target.is_dir():
        return {"error": "Kein Verzeichnis"}

    base = DATA_ROOTS[root_key].resolve()
    rel_here = str(target.relative_to(base)).replace("\\", "/") if target != base else ""
    parent = None
    if rel_here:
        parent = str(target.parent.relative_to(base)).replace("\\", "/")

    dirs, files = [], []
    total = 0
    try:
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            fe = _file_entry(root_key, entry, base)
            if entry.is_dir():
                # Größe des Ordners (summe)
                try:
                    folder_size = sum(
                        f.stat().st_size for f in entry.rglob("*") if f.is_file()
                    )
                except OSError:
                    folder_size = 0
                fe["size"] = folder_size
                fe["size_human"] = human_size(folder_size)
                dirs.append(fe)
                total += folder_size
            else:
                files.append(fe)
                total += fe["size"]
    except OSError as e:
        return {"error": str(e)}

    return {
        "root": root_key,
        "root_label": {"vmaf": "VMAF-Sessions", "previews": "Screenshots", "work": "Arbeit"}[root_key],
        "path": rel_here,
        "parent": parent,
        "is_root": target == base,
        "dirs": dirs,
        "files": files,
        "total_human": human_size(total),
    }


def delete_item(root_key: str, rel: str) -> tuple[bool, str]:
    target = _safe_resolve(root_key, rel)
    if target is None:
        return False, "Ungültiger Pfad"
    if target == DATA_ROOTS[root_key].resolve():
        return False, "Wurzelverzeichnis kann nicht gelöscht werden"
    if not target.exists():
        return False, "Nicht gefunden"
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True, ""
    except OSError as e:
        return False, str(e)


def delete_all_in_root(root_key: str) -> tuple[int, str]:
    """Löscht alle Inhalte einer Zone (nicht die Wurzel selbst)."""
    base = _safe_resolve(root_key, "")
    if base is None or not base.is_dir():
        return 0, "Ungültige Zone"
    count = 0
    try:
        for entry in list(base.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            count += 1
    except OSError as e:
        return count, str(e)
    return count, ""


def storage_summary() -> dict:
    """Übersicht: Anzahl & Größe je Zone."""
    out = {}
    for key, base in DATA_ROOTS.items():
        if not base.exists():
            out[key] = {"items": 0, "size_human": "0 B"}
            continue
        items = 0
        total = 0
        try:
            for f in base.rglob("*"):
                if f.is_file():
                    items += 1
                    total += f.stat().st_size
            # Top-Level Ordner zählen wenn leer
            if items == 0:
                items = sum(1 for e in base.iterdir() if not e.name.startswith("."))
        except OSError:
            pass
        out[key] = {"items": items, "size_human": human_size(total)}
    return out
