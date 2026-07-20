"""Speicherbare Encode-Profile (Presets) als JSON im Datenordner.

Ein Profil ist einfach ein Name + das komplette Settings-Objekt, das die UI
ohnehin sendet. Beim Anwenden füllt die UI die Felder daraus.
Eingebaute Medientyp-Presets werden beim Start angelegt, sofern fehlend.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from . import config

logger = logging.getLogger("vcompress.profiles")

_lock = threading.RLock()

# Eingebaute Vorlagen (nur anlegen, wenn Name noch nicht existiert).
_BUILTINS = [
    {
        "name": "Film",
        "builtin": True,
        "settings": {
            "platform": "cpu", "codec": "av1", "rate_mode": "cq", "quality": 28,
            "suffix": "_av1", "name_pattern": "{stem}{suffix}",
            "anime": False, "keep_subtitles": True, "keep_chapters": True,
            "audio_mode": "copy", "post_processing": "keep",
            "out_mode": "default", "on_duplicate": "ask",
            "integrity_check": True, "safe_replace": True,
        },
    },
    {
        "name": "Serie",
        "builtin": True,
        "settings": {
            "platform": "cpu", "codec": "av1", "rate_mode": "cq", "quality": 30,
            "suffix": "_av1", "name_pattern": "{stem}{suffix}",
            "anime": False, "keep_subtitles": True, "keep_chapters": True,
            "audio_mode": "copy", "audio_languages": "de, en",
            "subtitle_languages": "de, en",
            "post_processing": "keep", "out_mode": "default", "on_duplicate": "ask",
            "integrity_check": True, "safe_replace": True,
        },
    },
    {
        "name": "Anime",
        "builtin": True,
        "settings": {
            "platform": "cpu", "codec": "av1", "rate_mode": "cq", "quality": 26,
            "suffix": "_av1", "name_pattern": "{stem}{suffix}",
            "anime": True, "film_grain": 0, "keep_subtitles": True,
            "keep_chapters": True, "audio_mode": "copy",
            "post_processing": "keep", "out_mode": "default", "on_duplicate": "ask",
            "integrity_check": True, "safe_replace": True,
        },
    },
    {
        "name": "Remux-only",
        "builtin": True,
        "settings": {
            "video_mode": "edit", "vmaf_check": False, "workflow": "auto",
            "suffix": "_remux", "name_pattern": "{stem}{suffix}",
            "container": "mkv", "post_processing": "keep",
            "out_mode": "beside", "on_duplicate": "ask",
            "integrity_check": True, "safe_replace": True,
            "remux_only": True,
        },
    },
]


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


def ensure_builtins() -> list[dict]:
    """Fehlende eingebaute Presets anlegen (überschreibt Nutzerprofile nicht)."""
    with _lock:
        profiles = load()
        names = {p.get("name") for p in profiles}
        changed = False
        for b in _BUILTINS:
            if b["name"] not in names:
                profiles.append(dict(b))
                changed = True
        if changed:
            profiles.sort(key=lambda p: p.get("name", "").lower())
            try:
                _write(profiles)
            except OSError as e:
                logger.warning("Builtin-Profile nicht speicherbar: %s", e)
        return profiles


def save_profile(name: str, settings: dict) -> list[dict]:
    name = (name or "").strip()[:60]
    if not name:
        return load()
    with _lock:
        profiles = load()
        profiles = [p for p in profiles if p.get("name") != name]
        profiles.append({"name": name, "settings": settings, "builtin": False})
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
