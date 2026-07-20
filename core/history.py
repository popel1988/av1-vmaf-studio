"""Persistente Job-Historie in SQLite (übersteht Neustarts) inkl. Statistik.

Bewusst schlank gehalten: eine Tabelle `jobs`, thread-sicher über ein Lock
und eine dauerhaft offene Verbindung (check_same_thread=False).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Optional

from . import config

logger = logging.getLogger("vcompress.history")

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _db_path() -> str:
    return str(config.DATA_DIR / "history.db")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Migration: settings_json / output_path nachrüsten."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "settings_json" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN settings_json TEXT")
    if "output_path" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN output_path TEXT")


def init_db() -> None:
    """Legt DB/Tabelle an (idempotent)."""
    global _conn
    with _lock:
        if _conn is not None:
            return
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                title         TEXT,
                path          TEXT,
                status        TEXT,
                platform      TEXT,
                codec         TEXT,
                rate_mode     TEXT,
                quality       INTEGER,
                vmaf          REAL,
                original_size INTEGER,
                output_size   INTEGER,
                saved_bytes   INTEGER,
                duration      REAL,
                created       REAL,
                finished      REAL,
                settings_json TEXT,
                output_path   TEXT
            )
            """
        )
        _ensure_columns(_conn)
        _conn.commit()


def _pick_vmaf(item) -> Optional[float]:
    """Bestes/gewähltes VMAF-Ergebnis aus dem Analyse-Dict ziehen (oder None)."""
    measured = getattr(item, "vmaf_verify", None)
    if measured is not None:
        return float(measured)
    v = getattr(item, "vmaf", None)
    if not v:
        return None
    results = v.get("results", []) if isinstance(v, dict) else []
    if not results:
        return None
    idx = getattr(item.settings, "selected_result_index", None)
    if idx is not None and 0 <= idx < len(results):
        return results[idx].get("vmaf")
    rec = next((r for r in results if r.get("recommended")), None)
    return (rec or results[0]).get("vmaf")


def record_job(item, duration: float = 0.0) -> None:
    """Einen abgeschlossenen Job speichern (Encode fertig oder fehlgeschlagen)."""
    if _conn is None:
        return
    s = item.settings
    try:
        settings_json = json.dumps(s.__dict__, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        settings_json = "{}"
    row = (
        item.id,
        item.title,
        item.path,
        item.status,
        s.platform,
        s.codec,
        s.rate_mode,
        int(s.quality or 0),
        _pick_vmaf(item),
        int(getattr(item, "original_size", 0) or 0),
        int(getattr(item, "output_size", 0) or 0),
        int(getattr(item, "saved_bytes", 0) or 0),
        float(duration or 0.0),
        float(getattr(item, "created_at", 0.0) or 0.0),
        time.time(),
        settings_json,
        str(getattr(item, "output_path", "") or ""),
    )
    try:
        with _lock:
            _conn.execute(
                """
                INSERT OR REPLACE INTO jobs
                (id, title, path, status, platform, codec, rate_mode, quality,
                 vmaf, original_size, output_size, saved_bytes, duration,
                 created, finished, settings_json, output_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
            _conn.commit()
    except sqlite3.Error as e:
        logger.warning("Job-Historie konnte nicht gespeichert werden: %s", e)


def stats() -> dict:
    """Aggregierte Kennzahlen über alle gespeicherten Jobs."""
    if _conn is None:
        return _empty_stats()
    try:
        with _lock:
            done = _conn.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(original_size),0) o, "
                "COALESCE(SUM(output_size),0) n, COALESCE(SUM(saved_bytes),0) s, "
                "COALESCE(SUM(duration),0) d, AVG(vmaf) v "
                "FROM jobs WHERE status='fertig' AND output_size > 0"
            ).fetchone()
            failed = _conn.execute(
                "SELECT COUNT(*) c FROM jobs WHERE status='fehlgeschlagen'"
            ).fetchone()["c"]
            by_codec = _conn.execute(
                "SELECT codec, COUNT(*) c, COALESCE(SUM(saved_bytes),0) s "
                "FROM jobs WHERE status='fertig' AND output_size > 0 "
                "GROUP BY codec ORDER BY c DESC"
            ).fetchall()
            avg_dur = _conn.execute(
                "SELECT AVG(duration) a FROM jobs "
                "WHERE status='fertig' AND duration > 0 LIMIT 1"
            ).fetchone()
    except sqlite3.Error as e:
        logger.warning("Statistik konnte nicht gelesen werden: %s", e)
        return _empty_stats()

    orig = int(done["o"] or 0)
    saved = int(done["s"] or 0)
    ratio = (saved / orig * 100.0) if orig else 0.0
    return {
        "count_done": int(done["c"] or 0),
        "count_failed": int(failed or 0),
        "original_bytes": orig,
        "output_bytes": int(done["n"] or 0),
        "saved_bytes": saved,
        "saved_percent": round(ratio, 1),
        "encode_seconds": int(done["d"] or 0),
        "avg_vmaf": round(done["v"], 2) if done["v"] is not None else None,
        "avg_duration": float(avg_dur["a"] or 0) if avg_dur else 0.0,
        "by_codec": [
            {"codec": r["codec"], "count": r["c"], "saved_bytes": int(r["s"] or 0)}
            for r in by_codec
        ],
    }


def recent(limit: int = 100) -> list[dict]:
    """Letzte Jobs (neueste zuerst)."""
    if _conn is None:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "SELECT * FROM jobs ORDER BY finished DESC LIMIT ?", (int(limit),)
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def get(job_id: str) -> Optional[dict]:
    """Einen einzelnen Job (nach ID) aus der Historie holen."""
    if _conn is None or not job_id:
        return None
    try:
        with _lock:
            row = _conn.execute(
                "SELECT * FROM jobs WHERE id=? LIMIT 1", (str(job_id),)
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def by_source(path: str, limit: int = 20) -> list[dict]:
    """Jobs zu einem Quellpfad (für VMAF-/Encode-Historie)."""
    if _conn is None or not path:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "SELECT id, title, path, status, platform, codec, quality, "
                "rate_mode, vmaf, original_size, output_size, saved_bytes, "
                "duration, finished, output_path FROM jobs "
                "WHERE path=? ORDER BY finished DESC LIMIT ?",
                (str(path), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def is_processed(path: str) -> bool:
    """True, wenn zu diesem Quellpfad bereits ein erfolgreicher Job existiert."""
    if _conn is None or not path:
        return False
    try:
        with _lock:
            row = _conn.execute(
                "SELECT 1 FROM jobs WHERE path=? AND status='fertig' LIMIT 1",
                (str(path),),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def clear() -> int:
    """Gesamte Historie löschen. Gibt Anzahl gelöschter Zeilen zurück."""
    if _conn is None:
        return 0
    with _lock:
        cur = _conn.execute("DELETE FROM jobs")
        _conn.commit()
        return cur.rowcount


def _empty_stats() -> dict:
    return {
        "count_done": 0, "count_failed": 0, "original_bytes": 0,
        "output_bytes": 0, "saved_bytes": 0, "saved_percent": 0.0,
        "encode_seconds": 0, "avg_vmaf": None, "avg_duration": 0.0, "by_codec": [],
    }
