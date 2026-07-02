"""Speicherbare Encode-Profile (Presets) als JSON im Datenordner.

Ein Profil ist einfach ein Name + das komplette Settings-Objekt, das die UI
ohnehin sendet. Beim Anwenden füllt die UI die Felder daraus.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from . import config

logger = logging.getLogger("vcompress.profiles")

_lock = threading.RLock()


def _path():
    return config.DATA_DIR / "profiles.json"


def load() -> list[dict]:
    with _lock:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    return data if isinstance(data, list) else []


def _write(profiles: list[dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")


def save_profile(name: str, settings: dict) -> list[dict]:
    name = (name or "").strip()[:60]
    if not name:
        return load()
    with _lock:
        profiles = load()
        profiles = [p for p in profiles if p.get("name") != name]
        profiles.append({"name": name, "settings": settings})
        profiles.sort(key=lambda p: p.get("name", "").lower())
        try:
            _write(profiles)
        except OSError as e:
            logger.warning("Profil konnte nicht gespeichert werden: %s", e)
        return profiles


def delete(name: str) -> list[dict]:
    with _lock:
        profiles = [p for p in load() if p.get("name") != name]
        try:
            _write(profiles)
        except OSError as e:
            logger.warning("Profil konnte nicht gelöscht werden: %s", e)
        return profiles


def get(name: str) -> Optional[dict]:
    for p in load():
        if p.get("name") == name:
            return p
    return None
