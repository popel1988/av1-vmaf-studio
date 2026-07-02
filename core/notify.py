"""Benachrichtigungen via generischem Webhook, Discord oder Telegram.

Konfiguration aus Env-Variablen (Defaults) und optional aus /data/notify.json
(über die UI überschreibbar). Versand ohne Zusatz-Abhängigkeiten via urllib in
einem eigenen Thread, damit die Queue nicht blockiert.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Optional

from . import config
from . import ffmpeg_utils as ff

logger = logging.getLogger("vcompress.notify")

_lock = threading.RLock()


def _path():
    return config.DATA_DIR / "notify.json"


def _defaults() -> dict:
    return {
        "webhook_url": os.getenv("NOTIFY_WEBHOOK_URL", ""),
        "discord_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat": os.getenv("TELEGRAM_CHAT_ID", ""),
        "on_done": True,
        "on_failed": True,
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
    with _lock:
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("Notify-Konfig konnte nicht gespeichert werden: %s", e)
    return cur


def _post_json(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def _send_all(cfg: dict, title: str, message: str) -> None:
    text = f"{title}\n{message}"
    try:
        if cfg.get("discord_url"):
            _post_json(cfg["discord_url"], {"content": text})
    except Exception as e:  # pragma: no cover
        logger.warning("Discord-Benachrichtigung fehlgeschlagen: %s", e)
    try:
        if cfg.get("telegram_token") and cfg.get("telegram_chat"):
            url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
            _post_json(url, {"chat_id": cfg["telegram_chat"], "text": text})
    except Exception as e:  # pragma: no cover
        logger.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", e)
    try:
        if cfg.get("webhook_url"):
            _post_json(cfg["webhook_url"], {"title": title, "message": message})
    except Exception as e:  # pragma: no cover
        logger.warning("Webhook-Benachrichtigung fehlgeschlagen: %s", e)


def send(title: str, message: str) -> None:
    """Nachricht an alle konfigurierten Kanäle senden (nicht blockierend)."""
    cfg = load()
    if not any(cfg.get(k) for k in ("webhook_url", "discord_url", "telegram_token")):
        return
    threading.Thread(target=_send_all, args=(cfg, title, message), daemon=True).start()


def notify_job(item) -> None:
    """Benachrichtigung für einen abgeschlossenen Job (Erfolg/Fehler)."""
    cfg = load()
    status = getattr(item, "status", "")
    if status == "fertig" and getattr(item, "output_size", 0):
        if not cfg.get("on_done"):
            return
        saved = getattr(item, "saved_bytes", 0)
        msg = (f"Fertig: {item.title}\n"
               f"Ergebnis: {ff.human_size(getattr(item, 'output_size', 0))} "
               f"(−{ff.human_size(saved)})")
        send("✅ Encode fertig", msg)
    elif status == "fehlgeschlagen":
        if not cfg.get("on_failed"):
            return
        send("❌ Encode fehlgeschlagen", f"{item.title}\n{(item.error or '')[:300]}")
