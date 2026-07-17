"""API-Schlüsselverwaltung für die externe REST-API (/api/v1).

Schlüssel kommen aus der Env-Variable API_KEYS (kommagetrennt) und/oder aus
/data/apikeys.json (über die UI generier-/widerrufbar). Ist KEIN Schlüssel
konfiguriert, ist die API offen (Bootstrapping) – sobald einer existiert, wird
er erzwungen.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading

from . import config

logger = logging.getLogger("vcompress.apikeys")

_lock = threading.RLock()


def _path():
    return config.DATA_DIR / "apikeys.json"


def _env_keys() -> list[str]:
    return [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]


def _load_file() -> list[str]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        keys = data.get("keys", []) if isinstance(data, dict) else []
        return [str(k) for k in keys if str(k).strip()]
    except (OSError, ValueError):
        return []


def _save_file(keys: list[str]) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps({"keys": keys}, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("API-Schlüssel nicht speicherbar: %s", e)


def all_keys() -> list[str]:
    with _lock:
        return list(dict.fromkeys(_env_keys() + _load_file()))


def any_configured() -> bool:
    return bool(all_keys())


def validate(key: str) -> bool:
    if not key:
        return False
    return any(secrets.compare_digest(key, k) for k in all_keys())


def generate(label: str = "") -> str:
    """Neuen Schlüssel erzeugen, speichern und zurückgeben."""
    key = "vc_" + secrets.token_urlsafe(24)
    with _lock:
        keys = _load_file()
        keys.append(key)
        _save_file(keys)
    return key


def revoke_index(idx: int) -> bool:
    with _lock:
        keys = _load_file()
        if 0 <= idx < len(keys):
            keys.pop(idx)
            _save_file(keys)
            return True
    return False


def _mask(k: str) -> str:
    return (k[:6] + "…" + k[-4:]) if len(k) > 12 else "…"


def list_masked() -> dict:
    """Für die UI: maskierte Datei-Schlüssel + Anzahl Env-Schlüssel."""
    with _lock:
        file_keys = _load_file()
    return {
        "file_keys": [{"index": i, "masked": _mask(k)} for i, k in enumerate(file_keys)],
        "env_count": len(_env_keys()),
        "any": any_configured(),
    }
