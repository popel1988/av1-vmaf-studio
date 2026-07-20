"""Watch-Folder: überwacht einen Unterordner des Eingabeordners und legt neue
Videos automatisch in die Warteschlange – optional nur in einem Zeitfenster.

Konfiguration in /data/watch.json, bereits verarbeitete Dateien werden in
/data/watch_state.json gemerkt, damit nichts doppelt läuft.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from . import queue_manager as qm

logger = logging.getLogger("vcompress.watcher")


def _cfg_path():
    return config.DATA_DIR / "watch.json"


def _state_path():
    return config.DATA_DIR / "watch_state.json"


def default_config() -> dict:
    return {
        "enabled": False,
        "folder": "",          # relativ zu MEDIA_DIR ("" = gesamter Medienbaum)
        "interval_min": 15,    # Prüfintervall in Minuten
        "profile": "",         # anzuwendendes Encode-Profil (Name) oder leer
        "active_start": None,  # Startstunde (0–23) oder null = immer
        "active_end": None,    # Endstunde (0–23)
    }


def load_config() -> dict:
    cfg = default_config()
    try:
        stored = json.loads(_cfg_path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> dict:
    cur = load_config()
    for k in cur:
        if k in cfg:
            cur[k] = cfg[k]
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _cfg_path().write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Watch-Konfig konnte nicht gespeichert werden: %s", e)
    return cur


def _load_processed() -> set:
    try:
        return set(json.loads(_state_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _save_processed(paths: set) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(sorted(paths)), encoding="utf-8")
    except OSError:
        pass


def _in_window(cfg: dict) -> bool:
    a, b = cfg.get("active_start"), cfg.get("active_end")
    if a is None or b is None:
        return True
    h = datetime.now().hour
    a, b = int(a), int(b)
    if a == b:
        return True
    if a < b:
        return a <= h < b
    return h >= a or h < b  # über Mitternacht


class Watcher:
    def __init__(self) -> None:
        self._queue: Optional[qm.QueueManager] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run = 0.0
        self._last_added = 0

    def attach(self, queue: "qm.QueueManager") -> None:
        self._queue = queue

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def reconfigure(self) -> None:
        self._wake.set()

    def status(self) -> dict:
        cfg = load_config()
        cfg["last_run"] = self._last_run
        cfg["last_added"] = self._last_added
        cfg["processed_count"] = len(_load_processed())
        return cfg

    def _loop(self) -> None:
        while not self._stop.is_set():
            cfg = load_config()
            interval = max(1, int(cfg.get("interval_min", 15))) * 60
            if cfg.get("enabled") and self._queue is not None and _in_window(cfg):
                try:
                    self._scan(cfg)
                except Exception:  # pragma: no cover
                    logger.exception("Watch-Scan fehlgeschlagen")
                self._last_run = time.time()
            # Warten bis zum nächsten Intervall oder bis Reconfigure/Stop.
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _scan(self, cfg: dict) -> None:
        # Leerer Ordner = alle Input-Roots überwachen; sonst der gewählte Pfad.
        folder = str(cfg.get("folder", ""))

        processed = _load_processed()
        # Bereits in der Queue befindliche Pfade nicht erneut hinzufügen.
        in_queue = {i["path"] for i in self._queue.state()["items"]}

        settings_dict = {}
        if cfg.get("profile"):
            from . import profiles
            prof = profiles.get(cfg["profile"])
            if prof:
                settings_dict = prof.get("settings", {})

        added = 0
        for f in sorted(config.iter_input_files(folder, config.VIDEO_EXTENSIONS)):
            ap = str(f)
            if ap in processed or ap in in_queue:
                continue
            settings = qm.build_job_settings(settings_dict)
            item = self._queue.add_file(ap, settings)
            processed.add(ap)
            if item is not None:
                added += 1
        if added:
            _save_processed(processed)
            self._last_added = added
            logger.info("Watch-Folder: %s neue Datei(en) eingereiht.", added)
            try:
                from . import notify
                notify.send("👁 Watch-Folder", f"{added} neue Datei(en) automatisch eingereiht.")
            except Exception:  # pragma: no cover
                pass


watcher = Watcher()
