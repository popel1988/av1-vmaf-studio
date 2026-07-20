"""Persistente App-Einstellungen (unter /data/settings.json)."""
from __future__ import annotations

import json
import threading
from typing import Any

from . import config

_lock = threading.RLock()
_cache: dict | None = None

_DEFAULTS: dict[str, Any] = {
    # Relativer Pfad unter den Media-Roots (z. B. "output" → /media/output).
    "default_output": "output",
}


def _path():
    return config.DATA_DIR / "settings.json"


def load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        data = dict(_DEFAULTS)
        p = _path()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update({k: raw[k] for k in _DEFAULTS if k in raw})
            except (OSError, ValueError, TypeError):
                pass
        _cache = data
        return dict(data)


def save(updates: dict) -> dict:
    global _cache
    with _lock:
        cur = load()
        if "default_output" in updates:
            rel = config.safe_subdir(str(updates.get("default_output") or ""))
            cur["default_output"] = rel or "output"
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
        _cache = cur
        return dict(cur)


def default_output_rel() -> str:
    return str(load().get("default_output") or "output")
