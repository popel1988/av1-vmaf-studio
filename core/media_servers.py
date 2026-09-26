"""Optionale Rückmeldung an Jellyfin, Sonarr und Radarr nach einem Encode.

Leere URLs bedeuten: dieser Dienst ist aus. Ein Fehler hier ändert den
Encode-Status nicht. Pfade können um ein Präfix verschoben werden, wenn das
Tool und der Server denselben Ordner unter verschiedenen Mounts sehen.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("vcompress.media")

_lock = threading.RLock()
_SECRETS = ("jellyfin_token", "sonarr_key", "radarr_key")


def _path() -> Path:
    return config.DATA_DIR / "media_servers.json"


def _defaults() -> dict:
    return {
        "jellyfin_url": "",
        "jellyfin_token": "",
        "sonarr_url": "",
        "sonarr_key": "",
        "radarr_url": "",
        "radarr_key": "",
        "path_from": "",
        "path_to": "",
    }


def load() -> dict:
    cfg = _defaults()
    with _lock:
        try:
            stored = json.loads(_path().read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update({k: stored.get(k, cfg[k]) for k in cfg})
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
            logger.warning("Medienserver-Konfig konnte nicht gespeichert werden: %s", e)
    return cur


def public_view(cfg: Optional[dict] = None) -> dict:
    """Konfig ohne Secrets, nur mit dem Hinweis ob ein Schlüssel gesetzt ist."""
    src = cfg if cfg is not None else load()
    return {
        "jellyfin_url": src.get("jellyfin_url") or "",
        "jellyfin_token_set": bool(src.get("jellyfin_token")),
        "sonarr_url": src.get("sonarr_url") or "",
        "sonarr_key_set": bool(src.get("sonarr_key")),
        "radarr_url": src.get("radarr_url") or "",
        "radarr_key_set": bool(src.get("radarr_key")),
        "path_from": src.get("path_from") or "",
        "path_to": src.get("path_to") or "",
    }


def map_path(path: str, cfg: Optional[dict] = None) -> str:
    """Tool-Pfad auf den Pfad umschreiben, den der Medienserver sieht."""
    src = cfg if cfg is not None else load()
    raw = str(path or "").replace("\\", "/")
    old = str(src.get("path_from") or "").replace("\\", "/").rstrip("/")
    new = str(src.get("path_to") or "").replace("\\", "/").rstrip("/")
    if not old or not new:
        return raw
    if raw == old or raw.startswith(old + "/"):
        return new + raw[len(old):]
    return raw


def _base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _request(url: str, *, method: str = "GET", payload: Optional[dict] = None,
             headers: Optional[dict] = None, timeout: int = 12) -> tuple[int, str]:
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(500).decode("utf-8", errors="replace")
        except Exception:
            pass
        return int(e.code), body or str(e.reason)
    except Exception as e:
        return 0, str(e)


def _best_library(items: list, file_path: str) -> Optional[dict]:
    """Serie oder Film, deren Ordner der Dateipfad ist oder ihn enthält."""
    fp = file_path.replace("\\", "/").rstrip("/")
    best = None
    best_len = -1
    for it in items:
        if not isinstance(it, dict):
            continue
        p = str(it.get("path") or "").replace("\\", "/").rstrip("/")
        if not p:
            continue
        if fp == p or fp.startswith(p + "/"):
            if len(p) > best_len:
                best = it
                best_len = len(p)
    return best


def _arr_rescan(kind: str, base: str, api_key: str, file_path: str,
                list_path: str, command: str, id_key: str) -> tuple[bool, str]:
    code, body = _request(
        f"{base}{list_path}",
        headers={"X-Api-Key": api_key})
    if code != 200:
        return False, f"{kind}: Status {code or 'nicht erreichbar'}"
    try:
        items = json.loads(body)
    except json.JSONDecodeError:
        return False, f"{kind}: Antwort nicht lesbar"
    if not isinstance(items, list):
        return False, f"{kind}: unerwartete Antwort"
    hit = _best_library(items, file_path)
    if not hit or hit.get("id") is None:
        return False, f"{kind}: kein Eintrag für diesen Pfad"
    code, _ = _request(
        f"{base}/api/v3/command",
        method="POST",
        payload={"name": command, id_key: hit["id"]},
        headers={"X-Api-Key": api_key})
    if code not in (200, 201):
        return False, f"{kind}: Rescan abgelehnt ({code or 'nicht erreichbar'})"
    title = hit.get("title") or file_path
    return True, f"{kind}: {title} wird neu eingelesen"


def _notify_jellyfin(cfg: dict, file_path: str) -> tuple[bool, str]:
    base = _base(cfg.get("jellyfin_url") or "")
    token = cfg.get("jellyfin_token") or ""
    if not base or not token:
        return True, ""
    parent = str(Path(file_path).parent).replace("\\", "/")
    updates = [{"Path": file_path, "UpdateType": "Modified"}]
    if parent and parent != file_path:
        updates.append({"Path": parent, "UpdateType": "Modified"})
    code, _ = _request(
        f"{base}/Library/Media/Updated",
        method="POST",
        payload={"Updates": updates},
        headers={"Authorization": f'MediaBrowser Token="{token}"'})
    if code not in (200, 204):
        return False, f"Jellyfin: Status {code or 'nicht erreichbar'}"
    return True, "Jellyfin: Pfad gemeldet"


def _notify_one(cfg: dict, file_path: str) -> list[str]:
    notes: list[str] = []
    ok, msg = _notify_jellyfin(cfg, file_path)
    if msg:
        notes.append(msg)
        if not ok:
            logger.warning(msg)
    if _base(cfg.get("sonarr_url") or "") and cfg.get("sonarr_key"):
        ok, msg = _arr_rescan(
            "Sonarr", _base(cfg["sonarr_url"]), cfg["sonarr_key"], file_path,
            "/api/v3/series", "RescanSeries", "seriesId")
        notes.append(msg)
        logger.info(msg) if ok else logger.warning(msg)
    if _base(cfg.get("radarr_url") or "") and cfg.get("radarr_key"):
        ok, msg = _arr_rescan(
            "Radarr", _base(cfg["radarr_url"]), cfg["radarr_key"], file_path,
            "/api/v3/movie", "RescanMovie", "movieId")
        notes.append(msg)
        logger.info(msg) if ok else logger.warning(msg)
    return notes


def notify_output(path: str) -> None:
    """Nach einem fertigen Encode die konfigurierten Server anstoßen."""
    cfg = load()
    mapped = map_path(path, cfg)
    if not mapped:
        return
    if not any(_base(cfg.get(k) or "") for k in ("jellyfin_url", "sonarr_url", "radarr_url")):
        return
    threading.Thread(target=_notify_one, args=(cfg, mapped), daemon=True).start()


def test_connections() -> dict:
    """Erreichbarkeit der ausgefüllten Dienste prüfen, ohne eine Bibliothek anzufassen."""
    cfg = load()
    out: dict[str, dict] = {}
    base = _base(cfg.get("jellyfin_url") or "")
    token = cfg.get("jellyfin_token") or ""
    if base and token:
        code, body = _request(
            f"{base}/System/Info",
            headers={"Authorization": f'MediaBrowser Token="{token}"'})
        name = ""
        if code == 200:
            try:
                name = json.loads(body).get("ServerName") or ""
            except json.JSONDecodeError:
                name = ""
        out["jellyfin"] = {
            "ok": code == 200,
            "detail": name or (f"Status {code}" if code else body[:180]),
        }
    base = _base(cfg.get("sonarr_url") or "")
    if base and cfg.get("sonarr_key"):
        code, body = _request(f"{base}/api/v3/system/status",
                              headers={"X-Api-Key": cfg["sonarr_key"]})
        name = ""
        if code == 200:
            try:
                name = json.loads(body).get("instanceName") or "Sonarr"
            except json.JSONDecodeError:
                name = "Sonarr"
        out["sonarr"] = {"ok": code == 200, "detail": name or f"Status {code or body[:180]}"}
    base = _base(cfg.get("radarr_url") or "")
    if base and cfg.get("radarr_key"):
        code, body = _request(f"{base}/api/v3/system/status",
                              headers={"X-Api-Key": cfg["radarr_key"]})
        name = ""
        if code == 200:
            try:
                name = json.loads(body).get("instanceName") or "Radarr"
            except json.JSONDecodeError:
                name = "Radarr"
        out["radarr"] = {"ok": code == 200, "detail": name or f"Status {code or body[:180]}"}
    return out
