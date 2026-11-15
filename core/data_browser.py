"""Sicherer Browser & Löschen für den persistierten Datenordner (/data)."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional

from . import config
from .ffmpeg_utils import human_size

# Erlaubte Wurzeln unter DATA_DIR (kein Zugriff auf input/output)
DATA_ROOTS = {
    "vmaf": config.VMAF_SESSIONS_DIR,
    "previews": config.PREVIEW_DIR,
    "rpu_vault": config.RPU_VAULT_DIR,
    "work": config.WORK_DIR,
}

ROOT_LABELS = {
    "vmaf": "VMAF-Sessions",
    "previews": "Screenshots",
    "rpu_vault": "RPU-Archiv (Dolby Vision)",
    "work": "Arbeit / Temp",
}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mkv", ".mp4", ".webm", ".mov"}
JSON_EXT = {".json"}
RPU_EXT = {".bin"}


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


def _fmt_mtime(ts: float) -> str:
    if not ts or ts <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OSError, ValueError):
        return ""


def _file_entry(root_key: str, path: Path, base: Path) -> dict:
    rel = str(path.relative_to(base)).replace("\\", "/")
    ext = path.suffix.lower()
    try:
        st = path.stat()
        size = st.st_size if path.is_file() else 0
        mtime = float(st.st_mtime or 0.0)
    except OSError:
        size = 0
        mtime = 0.0
    entry = {
        "name": path.name,
        "rel": rel,
        "is_dir": path.is_dir(),
        "size": size,
        "size_human": human_size(size) if path.is_file() else "—",
        "mtime": round(mtime, 2),
        "mtime_human": _fmt_mtime(mtime),
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
        elif ext in RPU_EXT or path.name.lower().endswith(".rpu.bin"):
            entry["kind"] = "rpu"
            if root_key == "rpu_vault":
                try:
                    from . import rpu_vault
                    meta = rpu_vault.entry_by_filename(path.name)
                    if meta:
                        entry["rpu_meta"] = {
                            "id": meta.get("id"),
                            "title": meta.get("title"),
                            "origin_label": meta.get("origin_label"),
                            "profile": meta.get("profile"),
                            "el_type": meta.get("el_type"),
                            "cm_version": meta.get("cm_version"),
                            "frames": meta.get("frames"),
                            "fps": meta.get("fps"),
                            "peak_l1_nits": meta.get("peak_l1_nits"),
                            "rpu_mastering_display": meta.get("rpu_mastering_display"),
                            "rpu_content_light_level": meta.get("rpu_content_light_level"),
                        }
                except Exception:
                    pass
    return entry


def _sort_entries(items: list[dict], sort_mode: str) -> list[dict]:
    mode = (sort_mode or "name").strip().lower()
    if mode == "date_desc":
        return sorted(items, key=lambda x: (float(x.get("mtime") or 0.0), x["name"].lower()), reverse=True)
    if mode == "date_asc":
        return sorted(items, key=lambda x: (float(x.get("mtime") or 0.0), x["name"].lower()))
    if mode == "size_desc":
        return sorted(items, key=lambda x: (int(x.get("size") or 0), x["name"].lower()), reverse=True)
    return sorted(items, key=lambda x: x["name"].lower())


def browse(root_key: str, rel: str = "", sort: str = "name") -> dict:
    target = _safe_resolve(root_key, rel)
    if target is None:
        return {"error": "Ungültiger Pfad"}
    if not target.exists():
        if target == DATA_ROOTS[root_key].resolve():
            target.mkdir(parents=True, exist_ok=True)
        else:
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
        for entry in target.iterdir():
            if entry.name.startswith("."):
                continue
            fe = _file_entry(root_key, entry, base)
            if entry.is_dir():
                try:
                    folder_size = 0
                    latest_mtime = float(fe.get("mtime") or 0.0)
                    for f in entry.rglob("*"):
                        if f.is_file():
                            st = f.stat()
                            folder_size += st.st_size
                            if st.st_mtime > latest_mtime:
                                latest_mtime = st.st_mtime
                except OSError:
                    folder_size = 0
                    latest_mtime = float(fe.get("mtime") or 0.0)
                fe["size"] = folder_size
                fe["size_human"] = human_size(folder_size)
                fe["mtime"] = round(latest_mtime, 2)
                fe["mtime_human"] = _fmt_mtime(latest_mtime)
                dirs.append(fe)
                total += folder_size
            else:
                files.append(fe)
                total += fe["size"]
    except OSError as e:
        return {"error": str(e)}

    dirs = _sort_entries(dirs, sort)
    files = _sort_entries(files, sort)

    return {
        "root": root_key,
        "root_label": ROOT_LABELS.get(root_key, root_key),
        "path": rel_here,
        "parent": parent,
        "is_root": target == base,
        "sort": sort,
        "dirs": dirs,
        "files": files,
        "total_human": human_size(total),
    }


def _sync_rpu_vault_if_needed(root_key: str) -> None:
    if root_key == "rpu_vault":
        try:
            from . import rpu_vault
            rpu_vault.sync_index_with_disk()
        except Exception:
            pass


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
        _sync_rpu_vault_if_needed(root_key)
        return True, ""
    except OSError as e:
        return False, str(e)


def delete_many(root_key: str, rels: list[str]) -> tuple[int, str]:
    """Mehrere Dateien oder Ordner in einer Zone löschen."""
    deleted = 0
    errors: list[str] = []
    seen: set[str] = set()
    for raw in rels:
        rel = str(raw or "").strip().replace("\\", "/").lstrip("/")
        if not rel or rel in seen or rel == ".":
            continue
        seen.add(rel)
        ok, err = delete_item(root_key, rel)
        if ok:
            deleted += 1
        else:
            errors.append(err or rel)
    if errors and not deleted:
        return 0, errors[0]
    if errors:
        return deleted, "; ".join(errors[:5])
    return deleted, ""


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
        _sync_rpu_vault_if_needed(root_key)
        return count, str(e)
    _sync_rpu_vault_if_needed(root_key)
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
