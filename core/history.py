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
    # Quelldaten (Codec, Höhe, Bitrate, Dauer, fps) und reine Encode-Zeit:
    # Grundlage für den CQ-Vorschlag und die ETA aus der Historie.
    if "source_json" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN source_json TEXT")
    if "encode_seconds" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN encode_seconds REAL")


def job_kind_fields(settings) -> tuple[str, str, int]:
    """Anzeige-Felder (codec, rate_mode, quality) für Historie/Statistik.

    Remux/Copy/Merge/Split haben keine Encode-CQ – speichern wir als
    eigene „Codec"-Labels statt Default AV1/CQ28.
    """
    if settings is None:
        return "av1", "cq", 28
    if isinstance(settings, dict):
        mode = str(settings.get("video_mode") or "encode")
        remux_only = bool(settings.get("remux_only"))
        container = (settings.get("edit_spec") or {}).get("container") \
            if isinstance(settings.get("edit_spec"), dict) else None
        container = container or settings.get("container") or "mkv"
        codec = str(settings.get("codec") or "av1")
        rate_mode = str(settings.get("rate_mode") or "cq")
        quality = int(settings.get("quality") or 0)
    else:
        mode = str(getattr(settings, "video_mode", "encode") or "encode")
        remux_only = bool(getattr(settings, "remux_only", False))
        spec = getattr(settings, "edit_spec", None) or {}
        container = (spec.get("container") if isinstance(spec, dict) else None) \
            or getattr(settings, "container", None) or "mkv"
        codec = str(getattr(settings, "codec", "av1") or "av1")
        rate_mode = str(getattr(settings, "rate_mode", "cq") or "cq")
        quality = int(getattr(settings, "quality", 0) or 0)

    if remux_only or mode == "edit":
        return "remux", str(container or "mkv"), 0
    if mode == "copy":
        return "audio-opt", "copy", 0
    if mode == "concat":
        return "concat", "copy", 0
    if mode == "split":
        return "split", "copy", 0
    return codec, rate_mode, quality


def _migrate_kind_labels(conn: sqlite3.Connection) -> None:
    """Einmalig: Remux-/Copy-Jobs nicht mehr als AV1 CQ28 in codec/quality belassen."""
    try:
        rows = conn.execute(
            "SELECT id, codec, quality, settings_json FROM jobs "
            "WHERE settings_json IS NOT NULL AND settings_json != ''"
        ).fetchall()
    except sqlite3.Error:
        return
    updated = 0
    for row in rows:
        raw = row["settings_json"] or ""
        try:
            d = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        mode = str(d.get("video_mode") or "encode")
        if not (d.get("remux_only") or mode in ("edit", "copy", "concat", "split")):
            continue
        codec, rate_mode, quality = job_kind_fields(d)
        if (row["codec"] or "") == codec and int(row["quality"] or 0) == quality:
            continue
        conn.execute(
            "UPDATE jobs SET codec=?, rate_mode=?, quality=? WHERE id=?",
            (codec, rate_mode, quality, row["id"]),
        )
        updated += 1
    if updated:
        logger.info("Historie: %s Remux/Copy-Jobs neu gelabelt (codec/quality).", updated)


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
        _migrate_kind_labels(_conn)
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
    hist_codec, hist_rate, hist_q = job_kind_fields(s)
    row = (
        item.id,
        item.title,
        item.path,
        item.status,
        s.platform,
        hist_codec,
        hist_rate,
        int(hist_q or 0),
        _pick_vmaf(item),
        int(getattr(item, "original_size", 0) or 0),
        int(getattr(item, "output_size", 0) or 0),
        int(getattr(item, "saved_bytes", 0) or 0),
        float(duration or 0.0),
        float(getattr(item, "created_at", 0.0) or 0.0),
        time.time(),
        settings_json,
        str(getattr(item, "output_path", "") or ""),
        json.dumps(source_summary(getattr(item, "info", None)), ensure_ascii=False),
        float(getattr(item, "encode_seconds", 0.0) or 0.0),
    )
    try:
        with _lock:
            _conn.execute(
                """
                INSERT OR REPLACE INTO jobs
                (id, title, path, status, platform, codec, rate_mode, quality,
                 vmaf, original_size, output_size, saved_bytes, duration,
                 created, finished, settings_json, output_path, source_json,
                 encode_seconds)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
            _conn.commit()
    except sqlite3.Error as e:
        logger.warning("Job-Historie konnte nicht gespeichert werden: %s", e)


# ---------------------------------------------------------------------------
# Vorschlag und ETA aus der Historie
# ---------------------------------------------------------------------------

def source_summary(info) -> dict:
    """Die wenigen Quellmerkmale, die für Ähnlichkeit zählen."""
    if not info:
        return {}
    g = info.get if isinstance(info, dict) else (lambda k, d=None: getattr(info, k, d))
    dur = float(g("duration") or 0)
    size = int(g("size_bytes") or 0)
    bit_rate = int(g("bit_rate") or 0)
    if not bit_rate and dur > 0 and size > 0:
        bit_rate = int(size * 8 / dur)
    return {
        "codec": str(g("codec") or ""),
        "width": int(g("width") or 0),
        "height": int(g("height") or 0),
        "duration": round(dur, 1),
        "fps": float(g("fps") or 0),
        "bit_rate": bit_rate,
        "is_hdr": bool(g("is_hdr")),
        "interlaced": bool(g("interlaced")),
    }


def height_bucket(height: int) -> int:
    """Grobe Auflösungsklasse: 576 / 720 / 1080 / 1440 / 2160."""
    h = int(height or 0)
    if h <= 0:
        return 0
    if h <= 620:
        return 576
    if h <= 800:
        return 720
    if h <= 1200:
        return 1080
    if h <= 1600:
        return 1440
    return 2160


def _bits_per_pixel(src: dict) -> float:
    w, h, fps, br = (src.get("width") or 0), (src.get("height") or 0), \
        (src.get("fps") or 0), (src.get("bit_rate") or 0)
    if not (w and h and br):
        return 0.0
    return float(br) / (w * h * (fps or 24.0))


def _similarity(a: dict, b: dict) -> float:
    """0..1: wie ähnlich zwei Quellen sind (Auflösung, Codec, Bitrate, HDR)."""
    if not a or not b:
        return 0.0
    score = 0.0
    if height_bucket(a.get("height", 0)) == height_bucket(b.get("height", 0)):
        score += 0.4
    elif abs(height_bucket(a.get("height", 0)) - height_bucket(b.get("height", 0))) <= 360:
        score += 0.15
    if (a.get("codec") or "") == (b.get("codec") or ""):
        score += 0.2
    if bool(a.get("is_hdr")) == bool(b.get("is_hdr")):
        score += 0.1
    bpp_a, bpp_b = _bits_per_pixel(a), _bits_per_pixel(b)
    if bpp_a > 0 and bpp_b > 0:
        ratio = max(bpp_a, bpp_b) / min(bpp_a, bpp_b)
        if ratio <= 1.3:
            score += 0.3
        elif ratio <= 2.0:
            score += 0.2
        elif ratio <= 3.0:
            score += 0.08
    return score


def _load_rows(where: str, params: tuple, limit: int = 400) -> list[dict]:
    if _conn is None:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "SELECT platform, codec, rate_mode, quality, vmaf, duration, "
                "encode_seconds, source_json, settings_json FROM jobs "
                f"WHERE status='fertig' AND {where} ORDER BY finished DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["source"] = json.loads(d.get("source_json") or "{}") or {}
        except (TypeError, ValueError):
            d["source"] = {}
        try:
            d["settings"] = json.loads(d.get("settings_json") or "{}") or {}
        except (TypeError, ValueError):
            d["settings"] = {}
        out.append(d)
    return out


def suggest_quality(source: dict, platform: str, codec: str,
                    rate_mode: str = "cq", target_vmaf: float = 94.0,
                    target_height: int = 0) -> Optional[dict]:
    """CQ-Vorschlag aus früheren Encodes ähnlicher Quellen – ohne Testlauf.

    Nimmt fertige Jobs mit gemessenem/gewähltem VMAF, gleichem Encoder
    (Plattform + Codec) und ähnlicher Quelle. Aus den Paaren (CQ, VMAF) wird
    der höchste CQ gewählt, der das Ziel noch hält; fehlt ein solcher, der
    niedrigste bekannte CQ mit Hinweis. Rückgabe None ohne brauchbare Daten.
    """
    if not source:
        return None
    rm = "cq" if rate_mode in ("cq", "vmaf") else rate_mode
    rows = _load_rows("vmaf IS NOT NULL AND quality > 0 AND platform=? AND codec=? "
                      "AND rate_mode=?", (platform, codec, rm))
    cands = []
    for r in rows:
        src = r.get("source") or {}
        if not src.get("height"):
            continue
        sim = _similarity(source, src)
        if sim < 0.5:
            continue
        # Skalierung verändert die Qualität pro CQ deutlich – nur gleiche Zielhöhe.
        th = int((r.get("settings") or {}).get("target_height") or 0)
        if int(target_height or 0) != th:
            continue
        cands.append((sim, int(r["quality"]), float(r["vmaf"])))
    if not cands:
        return None
    cands.sort(key=lambda c: -c[0])
    cands = cands[:25]
    # Pro CQ den (ähnlichkeitsgewichteten) mittleren VMAF bilden.
    by_q: dict[int, list[tuple[float, float]]] = {}
    for sim, q, v in cands:
        by_q.setdefault(q, []).append((sim, v))
    table = []
    for q, pairs in by_q.items():
        wsum = sum(p[0] for p in pairs)
        table.append((q, sum(p[0] * p[1] for p in pairs) / wsum, len(pairs)))
    table.sort()
    holding = [t for t in table if t[1] >= float(target_vmaf)]
    if holding:
        q, v, n = max(holding, key=lambda t: t[0])  # höchster CQ, der hält
        confident = True
    else:
        q, v, n = min(table, key=lambda t: t[0])    # bester bekannter CQ
        confident = False
    # Interpolation: liegt zwischen „hält“ und „hält nicht“ eine Lücke,
    # den Wert dazwischen anbieten (lineare Näherung zwischen den Nachbarn).
    if holding:
        above = [t for t in table if t[0] > q]
        if above:
            q2, v2, _ = min(above, key=lambda t: t[0])
            if q2 - q > 1 and v != v2:
                frac = (v - float(target_vmaf)) / (v - v2)
                q_est = q + frac * (q2 - q)
                q_i = int(q_est)  # abrunden = sicherer (niedriger CQ)
                if q_i > q:
                    v = v + (v2 - v) * (q_i - q) / (q2 - q)
                    q = q_i
    total = sum(t[2] for t in table)
    return {
        "quality": int(q),
        "vmaf_expected": round(float(v), 1),
        "target_vmaf": float(target_vmaf),
        "samples": int(total),
        "confident": bool(confident),
        "best_similarity": round(cands[0][0], 2),
        "platform": platform,
        "codec": codec,
    }


def _median(vals: list[float]) -> float:
    vals = sorted(v for v in vals if v > 0)
    if not vals:
        return 0.0
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def estimate_speed(platform: str, codec: str, height: int,
                   encoder_speed: str = "balanced", target_height: int = 0) -> Optional[dict]:
    """Mittlere Encode-Geschwindigkeit (x Echtzeit) für Plattform/Codec/Höhe.

    Nutzt die reine Encode-Zeit (ohne VMAF-Analyse). Erst gleiche
    Auflösungsklasse und Encoder-Speed, sonst stufenweise lockerer.
    """
    rows = _load_rows("encode_seconds > 0 AND platform=? AND codec=?", (platform, codec))
    if not rows:
        return None
    want_b = height_bucket(target_height or height)

    def speed_of(r: dict) -> float:
        src = r.get("source") or {}
        dur = float(src.get("duration") or 0)
        enc = float(r.get("encode_seconds") or 0)
        return dur / enc if (dur > 0 and enc > 0) else 0.0

    def out_bucket(r: dict) -> int:
        src = r.get("source") or {}
        th = int((r.get("settings") or {}).get("target_height") or 0)
        return height_bucket(th or src.get("height", 0))

    tiers = [
        lambda r: out_bucket(r) == want_b
        and (r.get("settings") or {}).get("encoder_speed", "balanced") == encoder_speed,
        lambda r: out_bucket(r) == want_b,
        lambda r: True,
    ]
    for level, ok in enumerate(tiers):
        vals = [speed_of(r) for r in rows if ok(r)]
        vals = [v for v in vals if v > 0]
        if len(vals) >= 1:
            return {"speed_x": round(_median(vals), 2), "samples": len(vals),
                    "exact": level == 0}
    return None


def estimate_eta(source: dict, settings) -> Optional[dict]:
    """Geschätzte Laufzeit eines Encodes in Sekunden (ohne VMAF-Analyse)."""
    if not source or not source.get("duration"):
        return None
    g = settings.get if isinstance(settings, dict) else (lambda k, d=None: getattr(settings, k, d))
    if (g("video_mode") or "encode") != "encode" or g("remux_only"):
        return None
    sp = estimate_speed(str(g("platform") or ""), str(g("codec") or ""),
                        int(source.get("height") or 0),
                        str(g("encoder_speed") or "balanced"),
                        int(g("target_height") or 0))
    if not sp or sp["speed_x"] <= 0:
        return None
    secs = float(source["duration"]) / sp["speed_x"]
    if g("two_pass") and (g("rate_mode") in ("bitrate", "abr")):
        secs *= 1.8
    return {"seconds": round(secs), "speed_x": sp["speed_x"],
            "samples": sp["samples"], "exact": sp["exact"]}


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
                "duration, finished, output_path, settings_json FROM jobs "
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
