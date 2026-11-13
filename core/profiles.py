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
            "audio_mode": "copy",
            "audio_languages": "de, en", "audio_keep_original": True,
            "audio_exclude_commentary": True,
            "subtitle_languages": "de, en", "sub_exclude_sdh": False,
            "sub_keep_forced": True, "embed_sidecar_subs": True,
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


load_all = load


def export_bundle(
    *,
    include_builtins: bool = False,
    include_watch: bool = True,
    watch_rules: Optional[list] = None,
) -> dict:
    """Exportiert Profile (und optional Watch-Folder-Regeln) als portables JSON-Bundle."""
    import time as _time
    all_profiles = ensure_builtins()
    selected = [
        {"name": p["name"], "settings": dict(p.get("settings") or {}), "builtin": bool(p.get("builtin"))}
        for p in all_profiles
        if include_builtins or not p.get("builtin")
    ]
    bundle: dict = {
        "schema": "av1studio.profile_bundle",
        "version": 1,
        "exported_at": int(_time.time()),
        "profiles": selected,
    }
    if include_watch:
        if watch_rules is not None:
            bundle["watch_rules"] = list(watch_rules)
        else:
            try:
                from . import watcher
                cfg = watcher.load_config()
                bundle["watch"] = cfg
                bundle["watch_rules"] = list(cfg.get("rules") or [])
            except Exception as e:
                logger.debug("Watch-Config beim Export übersprungen: %s", e)
                bundle["watch_rules"] = []
    return bundle


def import_bundle(
    payload: dict | list,
    *,
    overwrite: bool = True,
    import_watch: bool = True,
) -> dict:
    """Importiert Profile (und optional Watch-Folder-Regeln) aus einem JSON-Bundle oder einer Liste."""
    raw_profiles: list = []
    watch_cfg = None
    watch_rules: list = []
    if isinstance(payload, list):
        raw_profiles = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("profiles"), list):
            raw_profiles = payload["profiles"]
        elif payload.get("name") and isinstance(payload.get("settings"), dict):
            raw_profiles = [payload]
        if import_watch:
            if isinstance(payload.get("watch_rules"), list):
                watch_rules = [r for r in payload["watch_rules"] if isinstance(r, dict)]
            if isinstance(payload.get("watch"), dict):
                watch_cfg = payload["watch"]
                if not watch_rules and isinstance(watch_cfg.get("rules"), list):
                    watch_rules = [r for r in watch_cfg["rules"] if isinstance(r, dict)]
    else:
        raise ValueError("Ungültiges JSON-Format für Profil-Import.")

    imported = 0
    skipped = 0
    with _lock:
        current = ensure_builtins()
        by_name = {str(p.get("name") or ""): p for p in current if p.get("name")}
        for entry in raw_profiles:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()[:60]
            settings = entry.get("settings")
            if not name or not isinstance(settings, dict):
                continue
            existing = by_name.get(name)
            if existing is not None and not overwrite:
                skipped += 1
                continue
            is_builtin = bool(existing.get("builtin")) if existing else False
            by_name[name] = {
                "name": name,
                "settings": dict(settings),
                "builtin": is_builtin,
            }
            imported += 1
        merged = sorted(by_name.values(), key=lambda p: p.get("name", "").lower())
        _write(merged)

    watch_imported = False
    if watch_cfg is not None:
        try:
            from . import watcher
            watcher.save_config(watch_cfg)
            watch_imported = True
        except Exception as e:
            logger.warning("Watch-Config konnte nicht importiert werden: %s", e)

    return {
        "imported": imported,
        "skipped": skipped,
        "watch_imported": watch_imported,
        "watch_rules": watch_rules,
        "profiles": load(),
    }
