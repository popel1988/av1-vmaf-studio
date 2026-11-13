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
import uuid
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


def _normalize_hour(val) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return max(0, min(23, int(val)))
    except (TypeError, ValueError):
        return None


def _normalize_rule(raw: dict, idx: int = 0) -> dict:
    return {
        "id": str(raw.get("id") or f"rule_{idx + 1}_{uuid.uuid4().hex[:6]}"),
        "enabled": bool(raw.get("enabled", True)),
        "folder": str(raw.get("folder") or "").strip(),
        "profile": str(raw.get("profile") or "").strip(),
        "active_start": _normalize_hour(raw.get("active_start")),
        "active_end": _normalize_hour(raw.get("active_end")),
    }


def default_config() -> dict:
    return {
        "enabled": False,
        "folder": "",          # relativ zu MEDIA_DIR ("" = gesamter Medienbaum)
        "interval_min": 15,    # Prüfintervall in Minuten
        "profile": "",         # anzuwendendes Encode-Profil (Name) oder leer
        "active_start": None,  # Startstunde (0–23) oder null = immer
        "active_end": None,    # Endstunde (0–23)
        "rules": [],           # Mehrere Watch-Folder-Regeln mit eigenen Profilen
    }


def load_config() -> dict:
    cfg = default_config()
    try:
        stored = json.loads(_cfg_path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in cfg})
    except (OSError, ValueError):
        pass
    raw_rules = cfg.get("rules")
    if isinstance(raw_rules, list) and raw_rules:
        cfg["rules"] = [_normalize_rule(r, i) for i, r in enumerate(raw_rules) if isinstance(r, dict)]
    elif cfg.get("folder") or cfg.get("profile") or cfg.get("enabled"):
        cfg["rules"] = [_normalize_rule({
            "id": "default",
            "enabled": True,
            "folder": cfg.get("folder", ""),
            "profile": cfg.get("profile", ""),
            "active_start": cfg.get("active_start"),
            "active_end": cfg.get("active_end"),
        }, 0)]
    else:
        cfg["rules"] = []
    return cfg


def save_config(cfg: dict) -> dict:
    cur = load_config()
    for k in cur:
        if k in cfg:
            cur[k] = cfg[k]
    if "rules" in cfg and isinstance(cfg["rules"], list):
        cur["rules"] = [_normalize_rule(r, i) for i, r in enumerate(cfg["rules"]) if isinstance(r, dict)]
        if cur["rules"]:
            first = cur["rules"][0]
            cur["folder"] = first["folder"]
            cur["profile"] = first["profile"]
            cur["active_start"] = first["active_start"]
            cur["active_end"] = first["active_end"]
    elif any(k in cfg for k in ("folder", "profile", "active_start", "active_end")):
        # Legacy-Aufruf ohne rules: erste Regel synchron halten
        rule0 = _normalize_rule({
            "id": (cur["rules"][0]["id"] if cur.get("rules") else "default"),
            "enabled": True,
            "folder": cur.get("folder", ""),
            "profile": cur.get("profile", ""),
            "active_start": cur.get("active_start"),
            "active_end": cur.get("active_end"),
        }, 0)
        if cur.get("rules"):
            cur["rules"][0] = rule0
        else:
            cur["rules"] = [rule0]
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
            if cfg.get("enabled") and self._queue is not None:
                try:
                    self._scan(cfg, force=False)
                except Exception:  # pragma: no cover
                    logger.exception("Watch-Scan fehlgeschlagen")
                self._last_run = time.time()
            # Warten bis zum nächsten Intervall oder bis Reconfigure/Stop.
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _effective_rules(self, cfg: dict) -> list[dict]:
        rules = cfg.get("rules")
        if isinstance(rules, list) and rules:
            return [r for r in rules if isinstance(r, dict) and r.get("enabled", True)]
        return [{
            "id": "default",
            "enabled": True,
            "folder": str(cfg.get("folder", "")),
            "profile": str(cfg.get("profile", "")),
            "active_start": cfg.get("active_start"),
            "active_end": cfg.get("active_end"),
        }]

    def _scan(self, cfg: dict, force: bool = True) -> None:
        if self._queue is None:
            return
        processed = _load_processed()
        in_queue = {i["path"] for i in self._queue.state()["items"]}
        rules = self._effective_rules(cfg)
        # Spezifischere Unterordner-Regeln zuerst prüfen, damit eine allgemeine
        # Root-Regel ("") Unterordner mit eigenem Profil nicht vorweg abgreift.
        rules_sorted = sorted(rules, key=lambda r: len(str(r.get("folder") or "")), reverse=True)

        added = 0
        for rule in rules_sorted:
            if not force and not _in_window(rule):
                continue
            folder = str(rule.get("folder", ""))
            settings_dict = {}
            prof_name = str(rule.get("profile") or "")
            if prof_name:
                from . import profiles
                prof = profiles.get(prof_name)
                if prof:
                    settings_dict = prof.get("settings", {})
            for f in sorted(config.iter_input_files(folder, config.VIDEO_EXTENSIONS)):
                ap = str(f)
                if ap in processed or ap in in_queue:
                    continue
                settings = qm.build_job_settings(settings_dict)
                item = self._queue.add_file(ap, settings)
                processed.add(ap)
                if item is not None:
                    in_queue.add(ap)
                    added += 1
        self._last_added = added
        if added:
            _save_processed(processed)
            logger.info("Watch-Folder: %s neue Datei(en) eingereiht.", added)
            try:
                from . import notify
                notify.send("👁 Watch-Folder", f"{added} neue Datei(en) automatisch eingereiht.")
            except Exception:  # pragma: no cover
                pass


watcher = Watcher()
