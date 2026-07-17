"""Scheduler / Ruhezeiten / Last-Drosselung für die Encode-Warteschlange.

Steuert, ob NEUE Jobs starten dürfen (laufende werden nie unterbrochen):
- Zeitfenster (z. B. nur nachts 22–06 Uhr encoden).
- Last-Drosselung: keine neuen Jobs, solange die CPU-Auslastung über einem
  Schwellwert liegt.

Konfiguration in /data/scheduler.json, über die UI überschreibbar.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

import psutil

from . import config

logger = logging.getLogger("vcompress.scheduler")

_lock = threading.RLock()


def _path():
    return config.DATA_DIR / "scheduler.json"


def _defaults() -> dict:
    return {
        "enabled": False,
        "window_enabled": False,
        "start_hour": 22,          # inklusive
        "end_hour": 6,             # exklusive (Wrap über Mitternacht erlaubt)
        "throttle_enabled": False,
        "max_cpu_percent": 85,     # neue Jobs pausieren, wenn CPU darüber
    }


def load() -> dict:
    cfg = _defaults()
    with _lock:
        try:
            stored = json.loads(_path().read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in cfg})
        except (OSError, ValueError):
            pass
    return cfg


def save(cfg: dict) -> dict:
    cur = load()
    for k in cur:
        if k in cfg:
            cur[k] = cfg[k]
    # Grenzen absichern.
    cur["start_hour"] = max(0, min(23, int(cur["start_hour"])))
    cur["end_hour"] = max(0, min(24, int(cur["end_hour"])))
    cur["max_cpu_percent"] = max(10, min(100, int(cur["max_cpu_percent"])))
    with _lock:
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        except OSError as e:
            logger.warning("Scheduler-Konfig nicht speicherbar: %s", e)
    return cur


def _in_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True  # 24/7
    if start < end:
        return start <= hour < end
    # Fenster über Mitternacht (z. B. 22–6).
    return hour >= start or hour < end


def gate() -> tuple[bool, str]:
    """(darf_starten, Grund). Wird vom Queue-Dispatcher aufgerufen."""
    cfg = load()
    if not cfg["enabled"]:
        return True, ""
    now = datetime.now()
    if cfg.get("window_enabled"):
        s, e = int(cfg["start_hour"]), int(cfg["end_hour"])
        if not _in_window(now.hour, s, e):
            return False, f"Außerhalb des Zeitfensters ({s:02d}–{e:02d} Uhr)"
    if cfg.get("throttle_enabled"):
        # Nicht-blockierender Abruf: nutzt den zuletzt gemessenen Wert.
        cpu = psutil.cpu_percent(interval=None)
        limit = int(cfg["max_cpu_percent"])
        if cpu > limit:
            return False, f"Systemlast zu hoch ({cpu:.0f}% > {limit}%)"
    return True, ""


def status() -> dict:
    """Aktueller Zustand für die UI (Konfig + ob gerade freigegeben)."""
    allowed, reason = gate()
    cfg = load()
    cfg["active_now"] = allowed
    cfg["reason"] = reason
    return cfg
