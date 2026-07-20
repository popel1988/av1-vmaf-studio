"""Persistente App-Einstellungen (unter /data/settings.json)."""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Optional

from . import config

_lock = threading.RLock()
_cache: dict | None = None

_DEFAULTS: dict[str, Any] = {
    # Relativer Pfad unter den Media-Roots (z. B. "output" → /media/output).
    "default_output": "output",
    # Benannte Unterbibliotheken: [{id, name, path}, ...]
    "libraries": [],
}


def _path():
    return config.DATA_DIR / "settings.json"


def load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        data = dict(_DEFAULTS)
        data["libraries"] = []
        p = _path()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    if "default_output" in raw:
                        data["default_output"] = raw["default_output"]
                    libs = raw.get("libraries")
                    if isinstance(libs, list):
                        data["libraries"] = _normalize_libraries(libs)
            except (OSError, ValueError, TypeError):
                pass
        _cache = data
        return dict(_cache)


def _normalize_libraries(libs: list) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in libs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:80]
        path = config.safe_subdir(str(item.get("path") or ""))
        if not name:
            continue
        lid = str(item.get("id") or "").strip() or uuid.uuid4().hex[:10]
        if lid in seen:
            lid = uuid.uuid4().hex[:10]
        seen.add(lid)
        out.append({"id": lid, "name": name, "path": path})
    out.sort(key=lambda x: x["name"].lower())
    return out


def _write(cur: dict) -> dict:
    global _cache
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    _cache = cur
    return dict(cur)


def save(updates: dict) -> dict:
    with _lock:
        cur = load()
        if "default_output" in updates:
            rel = config.safe_subdir(str(updates.get("default_output") or ""))
            cur["default_output"] = rel or "output"
        if "libraries" in updates:
            cur["libraries"] = _normalize_libraries(list(updates.get("libraries") or []))
        return _write(cur)


def default_output_rel() -> str:
    return str(load().get("default_output") or "output")


def list_libraries() -> list[dict]:
    return list(load().get("libraries") or [])


def get_library(lib_id: str) -> Optional[dict]:
    for lib in list_libraries():
        if lib.get("id") == lib_id:
            return dict(lib)
    return None


def add_library(name: str, path: str) -> tuple[Optional[dict], str]:
    """Neue Unterbibliothek. Rückgabe (lib, error)."""
    name = (name or "").strip()[:80]
    path = config.safe_subdir(path or "")
    if not name:
        return None, "Name fehlt"
    # Pfad darf leer sein (= gesamter Medienbaum) oder existierendes/ geplantes Dir.
    if path:
        target = config.resolve_input(path)
        if target is None:
            return None, "Pfad außerhalb der Media-Roots"
        if target.exists() and not target.is_dir():
            return None, "Pfad ist keine Ordner"
    with _lock:
        cur = load()
        libs = list(cur.get("libraries") or [])
        if any(l.get("name", "").lower() == name.lower() for l in libs):
            return None, "Name bereits vergeben"
        lib = {"id": uuid.uuid4().hex[:10], "name": name, "path": path}
        libs.append(lib)
        cur["libraries"] = _normalize_libraries(libs)
        _write(cur)
        return dict(lib), ""


def update_library(lib_id: str, name: Optional[str] = None,
                   path: Optional[str] = None) -> tuple[Optional[dict], str]:
    with _lock:
        cur = load()
        libs = list(cur.get("libraries") or [])
        idx = next((i for i, l in enumerate(libs) if l.get("id") == lib_id), -1)
        if idx < 0:
            return None, "Nicht gefunden"
        lib = dict(libs[idx])
        if name is not None:
            name = name.strip()[:80]
            if not name:
                return None, "Name fehlt"
            if any(i != idx and l.get("name", "").lower() == name.lower()
                   for i, l in enumerate(libs)):
                return None, "Name bereits vergeben"
            lib["name"] = name
        if path is not None:
            path = config.safe_subdir(path)
            if path:
                target = config.resolve_input(path)
                if target is None:
                    return None, "Pfad außerhalb der Media-Roots"
                if target.exists() and not target.is_dir():
                    return None, "Pfad ist keine Ordner"
            lib["path"] = path
        libs[idx] = lib
        cur["libraries"] = _normalize_libraries(libs)
        _write(cur)
        return dict(lib), ""


def delete_library(lib_id: str) -> tuple[bool, str]:
    with _lock:
        cur = load()
        libs = list(cur.get("libraries") or [])
        new = [l for l in libs if l.get("id") != lib_id]
        if len(new) == len(libs):
            return False, "Nicht gefunden"
        cur["libraries"] = new
        _write(cur)
        return True, ""
