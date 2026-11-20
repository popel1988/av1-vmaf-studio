"""Persistente Job-Historie in SQLite (übersteht Neustarts) inkl. Statistik.

Bewusst schlank gehalten: eine Tabelle `jobs`, thread-sicher über ein Lock
und eine dauerhaft offene Verbindung (check_same_thread=False).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
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
    if "steps_json" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN steps_json TEXT")
    for name in ("details_json", "report_json"):
        if name not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")


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
        _ensure_perf_table(_conn)
        _ensure_quality_table(_conn)
        _ensure_alias_table(_conn)
        _migrate_kind_labels(_conn)
        _backfill_perf_samples(_conn)
        _anonymize_named_sessions(_conn)
        _conn.commit()


def _ensure_quality_table(conn: sqlite3.Connection) -> None:
    """Persistente Qualitäts- & Effizienz-Datenbank für Encoder/Codec-Bewertung."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quality_samples (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            REAL NOT NULL,
            session_id    TEXT NOT NULL DEFAULT '',
            source_kind   TEXT NOT NULL DEFAULT 'vmaf_session',
            platform      TEXT NOT NULL DEFAULT '',
            codec         TEXT NOT NULL DEFAULT '',
            speed         TEXT NOT NULL DEFAULT 'balanced',
            rate_mode     TEXT NOT NULL DEFAULT 'cq',
            quality_val   INTEGER NOT NULL DEFAULT 0,
            two_pass      INTEGER NOT NULL DEFAULT 0,
            height_bucket INTEGER NOT NULL DEFAULT 1080,
            is_hdr        INTEGER NOT NULL DEFAULT 0,
            anime         INTEGER NOT NULL DEFAULT 0,
            vmaf          REAL,
            vmaf_1pct     REAL,
            vmaf_hmean    REAL,
            vmaf_score    REAL,
            psnr          REAL,
            ssim          REAL,
            ms_ssim       REAL,
            xpsnr         REAL,
            cambi         REAL,
            ssimulacra2   REAL,
            butteraugli   REAL,
            video_kbps    INTEGER NOT NULL DEFAULT 0,
            savings_pct   REAL,
            speed_x       REAL,
            encode_fps    REAL,
            recommended   INTEGER NOT NULL DEFAULT 0,
            sample_key    TEXT UNIQUE
        )
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(quality_samples)").fetchall()}
    if "complexity" not in cols:
        conn.execute("ALTER TABLE quality_samples ADD COLUMN complexity TEXT NOT NULL DEFAULT ''")
    if "source_density" not in cols:
        conn.execute("ALTER TABLE quality_samples ADD COLUMN source_density TEXT NOT NULL DEFAULT ''")
    if "source_alias" not in cols:
        conn.execute("ALTER TABLE quality_samples ADD COLUMN source_alias TEXT NOT NULL DEFAULT ''")
    if "encoder_version" not in cols:
        conn.execute("ALTER TABLE quality_samples ADD COLUMN encoder_version TEXT NOT NULL DEFAULT ''")
    if "butteraugli" not in cols:
        conn.execute("ALTER TABLE quality_samples ADD COLUMN butteraugli REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qual_pc ON quality_samples(platform, codec, speed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qual_sess ON quality_samples(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qual_cx ON quality_samples(complexity, source_density)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qual_alias ON quality_samples(source_alias)")


_NAMED_SESSION = re.compile(
    r"^(?P<stem>.+?)_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2})_(?P<run>[A-Za-z0-9]{4,12})$"
)
_OPAQUE_SESSION = re.compile(r"^s[0-9a-f]{16}_[0-9a-f]{4,8}$")


def _ensure_alias_table(conn: sqlite3.Connection) -> None:
    """Nur lokale Zuordnung von undurchsichtiger Kennung zu Titel und Pfad."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_aliases (
            alias     TEXT PRIMARY KEY,
            local_key TEXT UNIQUE,
            title     TEXT NOT NULL DEFAULT '',
            path      TEXT NOT NULL DEFAULT '',
            created   REAL NOT NULL
        )
        """
    )


def _device_secret() -> bytes:
    path = config.DATA_DIR / "source_alias.key"
    try:
        if path.is_file():
            raw = path.read_bytes()
            if len(raw) >= 16:
                return raw
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = secrets.token_bytes(32)
        path.write_bytes(raw)
        return raw
    except OSError:
        return b"vcompress-local-alias"


def _bind_alias_locked(conn: sqlite3.Connection, path: str = "", title: str = "", fallback_key: str = "") -> str:
    """Stabile Kennung für dieselbe Quelle auf diesem Gerät. Der Alias ist Zufall, kein Hash des Titels."""
    norm_path = str(path or "").strip()
    norm_title = str(title or "").strip()
    norm_fb = str(fallback_key or "").strip()
    row = None
    if norm_path:
        row = conn.execute(
            "SELECT alias, title FROM source_aliases WHERE local_key=?",
            (f"path:{norm_path}",),
        ).fetchone()
    if row is None and norm_title:
        row = conn.execute(
            "SELECT alias, title, path, local_key FROM source_aliases WHERE local_key=?",
            (f"title:{norm_title.casefold()}",),
        ).fetchone()
        if row is not None and norm_path and str(row["local_key"]).startswith("title:"):
            conn.execute(
                "UPDATE source_aliases SET local_key=?, path=?, title=CASE WHEN title='' THEN ? ELSE title END WHERE alias=?",
                (f"path:{norm_path}", norm_path, norm_title, row["alias"]),
            )
    if row is None and norm_fb:
        row = conn.execute(
            "SELECT alias, title FROM source_aliases WHERE local_key=?",
            (norm_fb,),
        ).fetchone()
    if row is not None:
        if norm_title and not str(row["title"] or ""):
            conn.execute("UPDATE source_aliases SET title=? WHERE alias=?", (norm_title, row["alias"]))
        return str(row["alias"])
    alias = secrets.token_hex(8)
    if norm_path:
        local_key, stored_title, stored_path = f"path:{norm_path}", norm_title, norm_path
    elif norm_title:
        local_key, stored_title, stored_path = f"title:{norm_title.casefold()}", norm_title, ""
    elif norm_fb:
        local_key, stored_title, stored_path = norm_fb, "", ""
    else:
        local_key, stored_title, stored_path = f"anon:{alias}", "", ""
    conn.execute(
        "INSERT INTO source_aliases (alias, local_key, title, path, created) VALUES (?,?,?,?,?)",
        (alias, local_key, stored_title, stored_path, time.time()),
    )
    return alias


def _opaque_session_locked(conn: sqlite3.Connection, session_name: str, path: str = "", title: str = "") -> tuple[str, str]:
    """Liefert (öffentliche Lauf-ID, Quellen-Alias) ohne Filmtitel."""
    name = str(session_name or "").strip()
    named = _NAMED_SESSION.fullmatch(name)
    display = str(title or "").strip() or (named.group("stem").replace("_", " ") if named else "")
    alias = _bind_alias_locked(conn, path, display, fallback_key=f"session:{name}" if name else "")
    if name.startswith("job:"):
        return name, alias
    if _OPAQUE_SESSION.fullmatch(name):
        return name, name[1:17]
    if named:
        run = named.group("run").lower()
    else:
        run = hmac.new(_device_secret(), name.encode("utf-8"), hashlib.sha256).hexdigest()[:8]
    return f"s{alias}_{run}", alias


def resolve_source_alias(alias: str) -> Optional[dict]:
    """Lokale Zuordnung einer Kennung zu Titel und Pfad. Gehört nicht in einen Austausch."""
    if _conn is None or not alias:
        return None
    try:
        with _lock:
            row = _conn.execute(
                "SELECT alias, title, path FROM source_aliases WHERE alias=?",
                (str(alias),),
            ).fetchone()
        if not row:
            return None
        return {"alias": row["alias"], "title": row["title"] or "", "path": row["path"] or ""}
    except sqlite3.Error:
        return None


def export_quality_exchange() -> list[dict]:
    """Qualitätszeilen ohne Titel und Pfad, geeignet für einen späteren Austausch."""
    if _conn is None:
        init_db()
    if _conn is None:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                """
                SELECT ts, session_id, source_alias, source_kind, platform, codec, speed,
                       encoder_version, rate_mode, quality_val, two_pass, height_bucket, is_hdr, anime,
                       vmaf, vmaf_1pct, vmaf_hmean, vmaf_score, psnr, ssim, ms_ssim,
                       xpsnr, cambi, ssimulacra2, butteraugli, video_kbps, savings_pct, speed_x,
                       encode_fps, recommended, complexity, source_density
                FROM quality_samples ORDER BY ts DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _anonymize_named_sessions(conn: sqlite3.Connection) -> None:
    """Ersetzt bereits gespeicherte Filmtitel in Lauf-IDs und Sample-Schlüsseln."""
    pairs: list[tuple[str, str, str]] = []
    try:
        sess_rows = conn.execute(
            "SELECT DISTINCT session_id FROM quality_samples WHERE session_id != ''"
        ).fetchall()
    except sqlite3.Error:
        sess_rows = []
    seen: set[str] = set()
    for row in sess_rows:
        token = str(row["session_id"] or "")
        match = _NAMED_SESSION.fullmatch(token) or _NAMED_SESSION.search(token)
        if not match or match.group(0) in seen:
            continue
        seen.add(match.group(0))
        public, alias = _opaque_session_locked(
            conn, match.group(0), title=match.group("stem").replace("_", " "),
        )
        pairs.append((match.group(0), public, alias))
    for old, public, alias in pairs:
        qual_rows = conn.execute(
            "SELECT id, sample_key FROM quality_samples WHERE session_id=? OR sample_key LIKE ?",
            (old, f"%{old}%"),
        ).fetchall()
        for item in qual_rows:
            new_key = str(item["sample_key"] or "").replace(old, public)
            clash = conn.execute(
                "SELECT 1 FROM quality_samples WHERE sample_key=? AND id!=?",
                (new_key, item["id"]),
            ).fetchone() if new_key else None
            if clash:
                conn.execute("DELETE FROM quality_samples WHERE id=?", (item["id"],))
                continue
            conn.execute(
                "UPDATE quality_samples SET session_id=?, source_alias=?, sample_key=? WHERE id=?",
                (public, alias, new_key or None, item["id"]),
            )
        perf_rows = conn.execute(
            "SELECT id, sample_key FROM perf_samples WHERE sample_key LIKE ?",
            (f"%{old}%",),
        ).fetchall()
        for item in perf_rows:
            new_key = str(item["sample_key"] or "").replace(old, public)
            clash = conn.execute(
                "SELECT 1 FROM perf_samples WHERE sample_key=? AND id!=?",
                (new_key, item["id"]),
            ).fetchone() if new_key else None
            if clash:
                conn.execute("DELETE FROM perf_samples WHERE id=?", (item["id"],))
                continue
            conn.execute("UPDATE perf_samples SET sample_key=? WHERE id=?", (new_key or None, item["id"]))


def _opt_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _insert_quality_sample(
    conn: sqlite3.Connection,
    *,
    ts: float,
    session_id: str,
    source_kind: str,
    platform: str,
    codec: str,
    speed: str = "balanced",
    rate_mode: str = "cq",
    quality_val: int = 0,
    two_pass: bool = False,
    height_b: int = 1080,
    is_hdr: bool = False,
    anime: bool = False,
    vmaf: Optional[float] = None,
    vmaf_1pct: Optional[float] = None,
    vmaf_hmean: Optional[float] = None,
    vmaf_score: Optional[float] = None,
    psnr: Optional[float] = None,
    ssim: Optional[float] = None,
    ms_ssim: Optional[float] = None,
    xpsnr: Optional[float] = None,
    cambi: Optional[float] = None,
    ssimulacra2: Optional[float] = None,
    butteraugli: Optional[float] = None,
    video_kbps: int = 0,
    savings_pct: Optional[float] = None,
    speed_x: Optional[float] = None,
    encode_fps: Optional[float] = None,
    recommended: bool = False,
    complexity: str = "",
    source_density: str = "",
    source_alias: str = "",
    encoder_version: str = "",
    sample_key: Optional[str] = None,
    upsert_metrics: bool = False,
) -> None:
    plat = str(platform or "").lower().strip()
    cod = str(codec or "").lower().strip()
    if not plat or not cod:
        return
    if vmaf is None or vmaf <= 0:
        return
    sql_head = (
        """
        INSERT INTO quality_samples
        (ts, session_id, source_kind, platform, codec, speed, rate_mode, quality_val,
         two_pass, height_bucket, is_hdr, anime, vmaf, vmaf_1pct, vmaf_hmean, vmaf_score,
         psnr, ssim, ms_ssim, xpsnr, cambi, ssimulacra2, butteraugli, video_kbps, savings_pct,
         speed_x, encode_fps, recommended, complexity, source_density, source_alias,
         encoder_version, sample_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        + (
            """
            ON CONFLICT(sample_key) DO UPDATE SET
                vmaf        = COALESCE(excluded.vmaf, quality_samples.vmaf),
                vmaf_1pct   = COALESCE(excluded.vmaf_1pct, quality_samples.vmaf_1pct),
                vmaf_hmean  = COALESCE(excluded.vmaf_hmean, quality_samples.vmaf_hmean),
                vmaf_score  = COALESCE(excluded.vmaf_score, quality_samples.vmaf_score),
                psnr        = COALESCE(excluded.psnr, quality_samples.psnr),
                ssim        = COALESCE(excluded.ssim, quality_samples.ssim),
                ms_ssim     = COALESCE(excluded.ms_ssim, quality_samples.ms_ssim),
                xpsnr       = COALESCE(excluded.xpsnr, quality_samples.xpsnr),
                cambi       = COALESCE(excluded.cambi, quality_samples.cambi),
                ssimulacra2 = COALESCE(excluded.ssimulacra2, quality_samples.ssimulacra2),
                butteraugli = COALESCE(excluded.butteraugli, quality_samples.butteraugli),
                encoder_version = CASE
                    WHEN excluded.encoder_version != '' THEN excluded.encoder_version
                    ELSE quality_samples.encoder_version
                END
            """
            if (upsert_metrics and sample_key)
            else " ON CONFLICT(sample_key) DO NOTHING"
        )
    )
    conn.execute(
        sql_head,
        (
            float(ts or time.time()), str(session_id or ""), str(source_kind or "vmaf_session"),
            plat, cod, str(speed or "balanced"), str(rate_mode or "cq"), int(quality_val or 0),
            1 if two_pass else 0, int(height_b or 1080), 1 if is_hdr else 0, 1 if anime else 0,
            _opt_float(vmaf), _opt_float(vmaf_1pct), _opt_float(vmaf_hmean), _opt_float(vmaf_score),
            _opt_float(psnr), _opt_float(ssim), _opt_float(ms_ssim), _opt_float(xpsnr),
            _opt_float(cambi), _opt_float(ssimulacra2), _opt_float(butteraugli), max(0, int(video_kbps or 0)),
            _opt_float(savings_pct), _opt_float(speed_x), _opt_float(encode_fps),
            1 if recommended else 0,
            str(complexity or ""), str(source_density or ""), str(source_alias or ""),
            str(encoder_version or "")[:80],
            sample_key,
        ),
    )


def _complexity_bucket(role: str) -> str:
    """VMAF-Szenenrolle (Bitratenprofil) auf drei Komplexitätsklassen."""
    role = str(role or "").lower()
    if role in ("peak", "high"):
        return "complex"
    if role in ("low", "quiet"):
        return "quiet"
    if role == "typical":
        return "typical"
    return ""


def _source_density(bit_rate: float, width: int, height: int, fps: float) -> str:
    """Bits pro Pixel und Frame der Quelle: leicht / mittel / dicht (Korn, Bewegung)."""
    try:
        br = float(bit_rate or 0)
        px = float(width or 0) * float(height or 0) * float(fps or 0)
    except (TypeError, ValueError):
        return ""
    if br <= 0 or px <= 0:
        return ""
    bpp = br / px
    if bpp >= 0.12:
        return "dense"
    if bpp >= 0.05:
        return "mid"
    return "light"


def _ensure_perf_table(conn: sqlite3.Connection) -> None:
    """Persistente Performance-Messwerte aus realen Encodes & VMAF-Testläufen."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS perf_samples (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            kind         TEXT NOT NULL,
            source_kind  TEXT NOT NULL,
            platform     TEXT NOT NULL DEFAULT '',
            codec        TEXT NOT NULL DEFAULT '',
            speed        TEXT NOT NULL DEFAULT 'balanced',
            rate_mode    TEXT NOT NULL DEFAULT 'cq',
            two_pass     INTEGER NOT NULL DEFAULT 0,
            tonemap      INTEGER NOT NULL DEFAULT 0,
            width        INTEGER NOT NULL DEFAULT 1920,
            height       INTEGER NOT NULL DEFAULT 1080,
            fps          REAL NOT NULL DEFAULT 24.0,
            media_sec    REAL NOT NULL DEFAULT 0.0,
            elapsed_sec  REAL NOT NULL DEFAULT 0.0,
            fps_measured REAL NOT NULL DEFAULT 0.0,
            metrics_mask TEXT NOT NULL DEFAULT '',
            subsample    INTEGER NOT NULL DEFAULT 1,
            vmaf_gpu     INTEGER NOT NULL DEFAULT 0,
            sample_key   TEXT UNIQUE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_kind_pc ON perf_samples(kind, platform, codec)")


def _norm_speed_alias(platform: str, codec: str, raw_speed: str) -> str:
    try:
        from . import ffmpeg_utils as ff
        enc = ff.encoder_name(platform or "cpu", codec or "av1")
        return ff.nearest_alias(enc, str(raw_speed or "balanced"))
    except Exception:
        return "balanced"


def _effective_dims(src: dict, settings: dict) -> tuple[int, int, float]:
    w = int((src or {}).get("width") or 1920)
    h = int((src or {}).get("height") or 1080)
    fps = float((src or {}).get("fps") or 24.0)
    if fps <= 0 or fps > 240:
        fps = 24.0
    th = int((settings or {}).get("target_height") or 0)
    if th > 0 and h > 0 and th < h:
        w = max(16, int(round(w * (th / h))))
        h = th
    return max(320, w), max(180, h), fps


def _insert_perf_sample(
    conn: sqlite3.Connection,
    *,
    ts: float,
    kind: str,
    source_kind: str,
    platform: str = "",
    codec: str = "",
    speed: str = "balanced",
    rate_mode: str = "cq",
    two_pass: bool = False,
    tonemap: bool = False,
    width: int = 1920,
    height: int = 1080,
    fps: float = 24.0,
    media_sec: float = 0.0,
    elapsed_sec: float = 0.0,
    fps_measured: float = 0.0,
    metrics_mask: str = "",
    subsample: int = 1,
    vmaf_gpu: bool = False,
    sample_key: Optional[str] = None,
) -> None:
    if elapsed_sec <= 0.05 or media_sec <= 0.0:
        return
    if fps_measured <= 0.0:
        if kind == "s2":
            fps_measured = round(elapsed_sec / max(1.0, media_sec), 4)
        else:
            fps_measured = round((media_sec * max(1.0, fps)) / elapsed_sec, 3)
    if fps_measured <= 0.0:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO perf_samples
        (ts, kind, source_kind, platform, codec, speed, rate_mode, two_pass, tonemap,
         width, height, fps, media_sec, elapsed_sec, fps_measured,
         metrics_mask, subsample, vmaf_gpu, sample_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            float(ts or time.time()), kind, source_kind,
            str(platform or "").lower(), str(codec or "").lower(),
            str(speed or "balanced"), str(rate_mode or "cq"),
            1 if two_pass else 0, 1 if tonemap else 0,
            int(width or 1920), int(height or 1080), float(fps or 24.0),
            round(float(media_sec), 3), round(float(elapsed_sec), 3),
            float(fps_measured), str(metrics_mask or ""),
            max(1, int(subsample or 1)), 1 if vmaf_gpu else 0,
            sample_key,
        ),
    )


def _metrics_mask_from_settings(s: dict) -> str:
    keys = []
    for k, name in (
        ("vmaf_psnr", "psnr"),
        ("vmaf_ssim", "ssim"),
        ("vmaf_ms_ssim", "ms_ssim"),
        ("vmaf_xpsnr", "xpsnr"),
        ("vmaf_cambi", "cambi"),
    ):
        if s.get(k, True):
            keys.append(name)
    return ",".join(keys)


def _metrics_mask_from_timing(tm: dict, fallback_settings: dict) -> str:
    m = tm.get("metrics") if isinstance(tm, dict) else None
    if isinstance(m, list):
        norm = {"PSNR": "psnr", "SSIM": "ssim", "MS-SSIM": "ms_ssim", "XPSNR": "xpsnr", "CAMBI": "cambi"}
        return ",".join(norm[x] for x in m if x in norm)
    return _metrics_mask_from_settings(fallback_settings or {})


def _clip_totals_from_analysis(vmaf_data: dict, s: dict) -> tuple[int, float]:
    starts = vmaf_data.get("sample_starts") if isinstance(vmaf_data, dict) else None
    if isinstance(starts, list) and starts:
        tot = 0.0
        cnt = 0
        for pair in starts:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                try:
                    l = float(pair[1])
                except (TypeError, ValueError):
                    l = 0.0
                if l > 0:
                    tot += l
                    cnt += 1
        if cnt > 0 and tot > 0:
            return cnt, tot
    samples_n = max(1, int((s or {}).get("samples") or 1))
    clip_sec = max(5.0, float((s or {}).get("clip_seconds") or 20.0))
    return samples_n, samples_n * clip_sec


def _detect_enc_version(platform: str, codec: str, explicit: str = "", *, live: bool = True) -> str:
    ev = str(explicit or "").strip()
    if ev:
        return ev[:80]
    if not live:
        return ""
    try:
        from . import ffmpeg_utils as ff
        return str(ff.encoder_version_label(platform, codec) or "")[:80]
    except Exception:
        return ""


def _record_vmaf_results_locked(
    conn: sqlite3.Connection,
    *,
    key_prefix: str,
    vmaf_data: dict,
    s: dict,
    w: int,
    h: int,
    fps: float,
    now: float,
    is_hdr: bool = False,
    session_id: str = "",
    source_bit_rate: float = 0.0,
    source_alias: str = "",
    live: bool = True,
    upsert_metrics: bool = False,
) -> None:
    results = (vmaf_data.get("results") or []) if isinstance(vmaf_data, dict) else []
    if not results:
        return
    samples_n, total_clip_sec = _clip_totals_from_analysis(vmaf_data, s)
    s2_frames = max(0, int(s.get("ssimulacra2_frames") if s.get("ssimulacra2_frames") is not None else 3))
    sub = max(1, int(s.get("vmaf_subsample") or 1))
    vgpu = bool(s.get("vmaf_gpu"))
    tonemap = bool(s.get("tonemap"))
    hb = height_bucket(h) or 1080
    eff_hdr = bool(is_hdr and not tonemap)
    anime = bool(s.get("anime"))
    density = _source_density(source_bit_rate, w, h, fps)
    windows = vmaf_data.get("sample_windows") if isinstance(vmaf_data.get("sample_windows"), list) else []
    sess_ref = str(session_id or vmaf_data.get("session") or key_prefix)
    for idx, r in enumerate(results):
        if not isinstance(r, dict):
            continue
        r_plat = str(r.get("platform") or s.get("platform") or "")
        r_codec = str(r.get("codec") or s.get("codec") or "")
        r_sp = _norm_speed_alias(r_plat, r_codec, r.get("encoder_speed") or s.get("encoder_speed") or "balanced")
        r_rm = str(r.get("rate_mode") or s.get("rate_mode") or "cq")
        r_tp = bool(r.get("two_pass")) and r_rm in ("abr", "bitrate")
        r_ver = _detect_enc_version(r_plat, r_codec, r.get("encoder_version") or s.get("encoder_version") or "", live=live)
        tm = r.get("timing") if isinstance(r.get("timing"), dict) else {}
        r_enc = float(tm.get("encode_sec") or 0.0)
        r_vmaf = float(tm.get("vmaf_sec") or 0.0)
        r_s2 = float(tm.get("s2_sec") or 0.0)
        mask = _metrics_mask_from_timing(tm, s)
        speed_x = round(total_clip_sec / r_enc, 2) if (r_enc >= 0.4 and total_clip_sec > 0) else None
        enc_fps = round((total_clip_sec * max(1.0, fps)) / r_enc, 2) if (r_enc >= 0.4 and total_clip_sec > 0) else None

        scenes = [sc for sc in (r.get("scene_scores") or []) if isinstance(sc, dict)]
        scene_rows = []
        for si, sc in enumerate(scenes):
            svm = _opt_float(sc.get("vmaf"))
            if not svm or svm <= 0:
                continue
            role = ""
            if si < len(windows) and isinstance(windows[si], dict):
                role = str(windows[si].get("role") or "")
            scene_rows.append((si, sc, svm, _complexity_bucket(role)))
        # Szenen getrennt speichern, damit komplexe und ruhige Ausschnitte nicht in einen Mittelwert fallen.
        # Ohne Szenenwerte bleibt eine Gesamtzeile.
        if scene_rows and r_plat and r_codec:
            for si, sc, svm, cx in scene_rows:
                _insert_quality_sample(
                    conn,
                    ts=now,
                    session_id=sess_ref,
                    source_kind="vmaf_scene",
                    platform=r_plat,
                    codec=r_codec,
                    speed=r_sp,
                    rate_mode=r_rm,
                    quality_val=int(r.get("value") or r.get("quality") or 0),
                    two_pass=r_tp,
                    height_b=hb,
                    is_hdr=eff_hdr,
                    anime=anime,
                    vmaf=svm,
                    vmaf_1pct=_opt_float(sc.get("p1")),
                    vmaf_hmean=_opt_float(sc.get("hmean")),
                    psnr=_opt_float(sc.get("psnr")),
                    ssim=_opt_float(sc.get("ssim")),
                    ms_ssim=_opt_float(sc.get("ms_ssim")),
                    xpsnr=_opt_float(sc.get("xpsnr")),
                    cambi=_opt_float(sc.get("cambi")),
                    ssimulacra2=_opt_float(sc.get("ssimulacra2")),
                    butteraugli=_opt_float(sc.get("butteraugli")),
                    video_kbps=int(sc.get("kbps") or 0),
                    savings_pct=_opt_float(r.get("savings_percent")),
                    speed_x=speed_x,
                    encode_fps=enc_fps,
                    recommended=bool(r.get("recommended")),
                    complexity=cx,
                    source_density=density,
                    source_alias=source_alias,
                    encoder_version=r_ver,
                    sample_key=f"{key_prefix}:q{idx}:s{si}" if key_prefix else None,
                    upsert_metrics=upsert_metrics,
                )
        else:
            vm = _opt_float(r.get("vmaf"))
            if vm and vm > 0 and r_plat and r_codec:
                _insert_quality_sample(
                    conn,
                    ts=now,
                    session_id=sess_ref,
                    source_kind="vmaf_session",
                    platform=r_plat,
                    codec=r_codec,
                    speed=r_sp,
                    rate_mode=r_rm,
                    quality_val=int(r.get("value") or r.get("quality") or 0),
                    two_pass=r_tp,
                    height_b=hb,
                    is_hdr=eff_hdr,
                    anime=anime,
                    vmaf=vm,
                    vmaf_1pct=_opt_float(r.get("vmaf_1pct")),
                    vmaf_hmean=_opt_float(r.get("vmaf_hmean")),
                    vmaf_score=_opt_float(r.get("vmaf_score")),
                    psnr=_opt_float(r.get("psnr")),
                    ssim=_opt_float(r.get("ssim")),
                    ms_ssim=_opt_float(r.get("ms_ssim")),
                    xpsnr=_opt_float(r.get("xpsnr")),
                    cambi=_opt_float(r.get("cambi")),
                    ssimulacra2=_opt_float(r.get("ssimulacra2")),
                    butteraugli=_opt_float(r.get("butteraugli")),
                    video_kbps=int(r.get("video_kbps") or 0),
                    savings_pct=_opt_float(r.get("savings_percent")),
                    speed_x=speed_x,
                    encode_fps=enc_fps,
                    recommended=bool(r.get("recommended")),
                    source_density=density,
                    source_alias=source_alias,
                    encoder_version=r_ver,
                    sample_key=f"{key_prefix}:q{idx}" if key_prefix else None,
                    upsert_metrics=upsert_metrics,
                )

        if not tm:
            continue
        if r_enc >= 0.4 and r_plat and r_codec:
            eff_enc = r_enc / (1.72 if (r_tp and r_plat == "cpu") else (1.15 if r_tp else 1.0))
            _insert_perf_sample(
                conn, ts=now, kind="encode", source_kind="vmaf_test",
                platform=r_plat, codec=r_codec, speed=r_sp, rate_mode=r_rm,
                two_pass=r_tp, tonemap=tonemap, width=w, height=h, fps=fps,
                media_sec=total_clip_sec, elapsed_sec=eff_enc,
                sample_key=f"{key_prefix}:v{idx}:enc" if key_prefix else None,
            )
        if r_vmaf >= 0.4:
            _insert_perf_sample(
                conn, ts=now, kind="vmaf_metric", source_kind="vmaf_test",
                platform=r_plat, codec=r_codec, speed=r_sp, rate_mode=r_rm,
                two_pass=False, tonemap=tonemap, width=w, height=h, fps=fps,
                media_sec=total_clip_sec, elapsed_sec=r_vmaf,
                metrics_mask=mask, subsample=sub, vmaf_gpu=vgpu,
                sample_key=f"{key_prefix}:v{idx}:vmaf" if key_prefix else None,
            )
        if r_s2 >= 0.2 and s2_frames > 0:
            pairs_n = float(samples_n * s2_frames)
            _insert_perf_sample(
                conn, ts=now, kind="s2", source_kind="vmaf_test",
                width=w, height=h, fps=fps,
                media_sec=pairs_n, elapsed_sec=r_s2,
                fps_measured=round(r_s2 / pairs_n, 4),
                sample_key=f"{key_prefix}:v{idx}:s2" if key_prefix else None,
            )


def _record_item_perf_locked(conn: sqlite3.Connection, item, now: float) -> None:
    """Extrahiert reale Performance-Samples aus abgeschlossenen Encodes & VMAF-Teststufen."""
    if getattr(item, "status", "") not in ("fertig", "done"):
        return
    s_obj = getattr(item, "settings", None)
    s = (s_obj if isinstance(s_obj, dict) else (vars(s_obj) if s_obj is not None else {})) or {}
    src = source_summary(getattr(item, "info", None))
    w, h, fps = _effective_dims(src, s)
    job_id = str(getattr(item, "id", "") or "")
    tonemap = bool(s.get("tonemap"))

    # 1) Vollständiger Encode
    enc_sec = float(getattr(item, "encode_seconds", 0.0) or 0.0)
    dur = float(src.get("duration") or 0.0)
    vmode = str(s.get("video_mode") or "encode")
    if enc_sec >= 1.0 and dur >= 2.0 and vmode == "encode" and not s.get("remux_only"):
        plat = str(s.get("platform") or "")
        codec = str(s.get("codec") or "")
        if plat and codec:
            sp = _norm_speed_alias(plat, codec, s.get("encoder_speed") or "balanced")
            rm = str(s.get("rate_mode") or "cq")
            tp = bool(s.get("two_pass")) and rm in ("abr", "bitrate")
            eff_elapsed = enc_sec / (1.72 if (tp and plat == "cpu") else (1.15 if tp else 1.0))
            _insert_perf_sample(
                conn, ts=now, kind="encode", source_kind="full_encode",
                platform=plat, codec=codec, speed=sp, rate_mode=rm,
                two_pass=tp, tonemap=tonemap, width=w, height=h, fps=fps,
                media_sec=dur, elapsed_sec=eff_elapsed,
                sample_key=f"job:{job_id}:enc" if job_id else None,
            )
            vm_job = _pick_vmaf(item)
            if vm_job and vm_job > 0:
                orig_b = int(getattr(item, "original_size", 0) or 0)
                out_b = int(getattr(item, "output_size", 0) or 0)
                sav_pct = round((orig_b - out_b) / orig_b * 100.0, 1) if (orig_b > 0 and out_b > 0) else None
                est_kbps = int((out_b * 8) / (dur * 1000)) if (out_b > 0 and dur > 0) else 0
                _insert_quality_sample(
                    conn,
                    ts=now,
                    session_id=f"job:{job_id}" if job_id else "",
                    source_kind="full_encode",
                    platform=plat,
                    codec=codec,
                    speed=sp,
                    rate_mode=rm,
                    quality_val=int(s.get("quality") or 0),
                    two_pass=tp,
                    height_b=height_bucket(h) or 1080,
                    is_hdr=bool(src.get("is_hdr") and not tonemap),
                    anime=bool(s.get("anime")),
                    vmaf=vm_job,
                    video_kbps=est_kbps,
                    savings_pct=sav_pct,
                    speed_x=round(dur / enc_sec, 2),
                    encode_fps=round((dur * max(1.0, fps)) / enc_sec, 2),
                    recommended=True,
                    source_density=_source_density(src.get("bit_rate") or 0, w, h, fps),
                    source_alias=_bind_alias_locked(
                        conn, str(getattr(item, "path", "") or ""), str(getattr(item, "title", "") or ""),
                    ),
                    encoder_version=_detect_enc_version(plat, codec, s.get("encoder_version") or "", live=True),
                    sample_key=f"job:{job_id}:qenc" if job_id else None,
                )

    # 2) Einzelne VMAF-Teststufen (Encode + libvmaf + SSIMULACRA2 + Qualität)
    vmaf_data = getattr(item, "vmaf", None)
    if isinstance(vmaf_data, dict):
        sess = str(vmaf_data.get("session") or "")
        public, alias = _opaque_session_locked(
            conn, sess, path=str(getattr(item, "path", "") or ""), title=str(getattr(item, "title", "") or ""),
        ) if sess else ("", _bind_alias_locked(conn, str(getattr(item, "path", "") or ""), str(getattr(item, "title", "") or "")))
        key_prefix = public or (f"job:{job_id}" if job_id else "")
        _record_vmaf_results_locked(
            conn, key_prefix=key_prefix, vmaf_data=vmaf_data, s=s, w=w, h=h, fps=fps, now=now,
            is_hdr=bool(src.get("is_hdr")),
            session_id=public or (f"job:{job_id}" if job_id else ""),
            source_bit_rate=float(src.get("bit_rate") or 0),
            source_alias=alias,
        )


def record_vmaf_analysis_perf(
    sess_id: str,
    analysis_dict: dict,
    src_info: Optional[dict] = None,
    params: Optional[dict] = None,
    ts: Optional[float] = None,
    source_path: str = "",
    source_title: str = "",
    upsert_metrics: bool = False,
) -> None:
    """Speichert Performance- & Qualitäts-Samples direkt beim Abschluss einer VMAF-Analyse."""
    if _conn is None:
        init_db()
    if _conn is None or not isinstance(analysis_dict, dict):
        return
    s = params if isinstance(params, dict) else {}
    src = source_summary(src_info) if src_info else {}
    w, h, fps = _effective_dims(src, s)
    now = float(ts or time.time())
    try:
        with _lock:
            public, alias = _opaque_session_locked(
                _conn, str(sess_id or ""), path=source_path, title=source_title,
            )
            _record_vmaf_results_locked(
                _conn, key_prefix=public, vmaf_data=analysis_dict, s=s, w=w, h=h, fps=fps, now=now,
                is_hdr=bool(src.get("is_hdr")), session_id=public,
                source_bit_rate=float(src.get("bit_rate") or 0),
                source_alias=alias,
                live=not upsert_metrics,
                upsert_metrics=upsert_metrics,
            )
            _conn.commit()
    except sqlite3.Error as exc:
        logger.debug("VMAF-Perf-Samples nicht speicherbar: %s", exc)


def _backfill_vmaf_sessions(conn: sqlite3.Connection) -> None:
    """Übernimmt gespeicherte VMAF-Sessions (Qualität + Timing) einmalig in SQLite."""
    root = getattr(config, "PREVIEW_DIR", None)
    if not root or not root.exists():
        return
    try:
        files = sorted(root.glob("*/analysis.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
    except OSError:
        return
    for meta in files:
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        analysis = data.get("analysis")
        if not isinstance(analysis, dict):
            continue
        sess = str(data.get("session") or meta.parent.name)
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        src = data.get("source_info") if isinstance(data.get("source_info"), dict) else {}
        w, h, fps = _effective_dims(src, params)
        ts = float(data.get("created") or time.time())
        public, alias = _opaque_session_locked(
            conn, sess,
            path=str(data.get("source_path") or ""),
            title=str(data.get("title") or ""),
        )
        _record_vmaf_results_locked(
            conn, key_prefix=public, vmaf_data=analysis, s=params, w=w, h=h, fps=fps, now=ts,
            is_hdr=bool(src.get("is_hdr")), session_id=public,
            source_bit_rate=float(src.get("bit_rate") or 0),
            source_alias=alias,
            live=False,
        )


def _backfill_perf_samples(conn: sqlite3.Connection) -> None:
    """Übernimmt vorhandene abgeschlossene Encodes & VMAF-Steps einmalig in perf_samples/quality_samples."""
    try:
        rows = conn.execute(
            "SELECT id, platform, codec, rate_mode, quality, vmaf, original_size, output_size, "
            "finished, encode_seconds, source_json, settings_json, steps_json FROM jobs "
            "WHERE status IN ('fertig', 'done') ORDER BY finished DESC LIMIT 400"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        try:
            src = json.loads(r["source_json"] or "{}") if r["source_json"] else {}
            s = json.loads(r["settings_json"] or "{}") if r["settings_json"] else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(src, dict) or not isinstance(s, dict):
            continue
        w, h, fps = _effective_dims(src, s)
        ts = float(r["finished"] or time.time())
        jid = str(r["id"] or "")
        enc_sec = float(r["encode_seconds"] or 0.0)
        dur = float(src.get("duration") or 0.0)
        plat = str(r["platform"] or s.get("platform") or "")
        codec = str(r["codec"] or s.get("codec") or "")
        vmode = str(s.get("video_mode") or "encode")
        if enc_sec >= 1.0 and dur >= 2.0 and plat and codec and vmode == "encode" and not s.get("remux_only"):
            sp = _norm_speed_alias(plat, codec, s.get("encoder_speed") or "balanced")
            rm = str(r["rate_mode"] or s.get("rate_mode") or "cq")
            tp = bool(s.get("two_pass")) and rm in ("abr", "bitrate")
            eff_enc = enc_sec / (1.72 if (tp and plat == "cpu") else (1.15 if tp else 1.0))
            _insert_perf_sample(
                conn, ts=ts, kind="encode", source_kind="full_encode",
                platform=plat, codec=codec, speed=sp, rate_mode=rm,
                two_pass=tp, tonemap=bool(s.get("tonemap")), width=w, height=h, fps=fps,
                media_sec=dur, elapsed_sec=eff_enc,
                sample_key=f"job:{jid}:enc" if jid else None,
            )
            vm_val = _opt_float(r["vmaf"])
            if vm_val and vm_val > 0:
                orig_b = int(r["original_size"] or 0)
                out_b = int(r["output_size"] or 0)
                sav_pct = round((orig_b - out_b) / orig_b * 100.0, 1) if (orig_b > 0 and out_b > 0) else None
                est_kbps = int((out_b * 8) / (dur * 1000)) if (out_b > 0 and dur > 0) else 0
                _insert_quality_sample(
                    conn,
                    ts=ts,
                    session_id=f"job:{jid}" if jid else "",
                    source_kind="full_encode",
                    platform=plat,
                    codec=codec,
                    speed=sp,
                    rate_mode=rm,
                    quality_val=int(r["quality"] or s.get("quality") or 0),
                    two_pass=tp,
                    height_b=height_bucket(h) or 1080,
                    is_hdr=bool(src.get("is_hdr") and not s.get("tonemap")),
                    anime=bool(s.get("anime")),
                    vmaf=vm_val,
                    video_kbps=est_kbps,
                    savings_pct=sav_pct,
                    speed_x=round(dur / enc_sec, 2),
                    encode_fps=round((dur * max(1.0, fps)) / enc_sec, 2),
                    recommended=True,
                    source_density=_source_density(src.get("bit_rate") or 0, w, h, fps),
                    sample_key=f"job:{jid}:qenc" if jid else None,
                )
        # Auch historische VMAF-Teststufen mit subtiming aus steps_json übernehmen
        raw_steps = r["steps_json"] or ""
        if raw_steps:
            try:
                steps = json.loads(raw_steps)
            except (TypeError, ValueError):
                steps = []
            samples_n = max(1, int(s.get("samples") or 1))
            clip_sec = max(5.0, float(s.get("clip_seconds") or 20.0))
            tot_sec = samples_n * clip_sec
            s2_frames = max(0, int(s.get("ssimulacra2_frames") if s.get("ssimulacra2_frames") is not None else 3))
            mask = _metrics_mask_from_settings(s)
            sub = max(1, int(s.get("vmaf_subsample") or 1))
            vgpu = bool(s.get("vmaf_gpu"))
            for idx, st in enumerate(steps if isinstance(steps, list) else []):
                if not isinstance(st, dict) or st.get("key") != "analysis.candidate":
                    continue
                subt = (st.get("context") or {}).get("subtiming")
                if not isinstance(subt, dict):
                    continue
                sp = _norm_speed_alias(plat or "cpu", codec or "av1", s.get("encoder_speed") or "balanced")
                s_enc = float(subt.get("encode_sec") or 0.0)
                s_vmaf = float(subt.get("vmaf_sec") or 0.0)
                s_s2 = float(subt.get("s2_sec") or 0.0)
                if s_enc >= 0.4 and plat and codec:
                    _insert_perf_sample(
                        conn, ts=ts, kind="encode", source_kind="vmaf_test",
                        platform=plat, codec=codec, speed=sp,
                        rate_mode=str(s.get("rate_mode") or "cq"),
                        tonemap=bool(s.get("tonemap")), width=w, height=h, fps=fps,
                        media_sec=tot_sec, elapsed_sec=s_enc,
                        sample_key=f"job:{jid}:st{idx}:enc" if jid else None,
                    )
                if s_vmaf >= 0.4:
                    _insert_perf_sample(
                        conn, ts=ts, kind="vmaf_metric", source_kind="vmaf_test",
                        platform=plat, codec=codec, speed=sp,
                        tonemap=bool(s.get("tonemap")), width=w, height=h, fps=fps,
                        media_sec=tot_sec, elapsed_sec=s_vmaf,
                        metrics_mask=mask, subsample=sub, vmaf_gpu=vgpu,
                        sample_key=f"job:{jid}:st{idx}:vmaf" if jid else None,
                    )
                if s_s2 >= 0.2 and s2_frames > 0:
                    pairs_n = float(samples_n * s2_frames)
                    _insert_perf_sample(
                        conn, ts=ts, kind="s2", source_kind="vmaf_test",
                        width=w, height=h, fps=fps,
                        media_sec=pairs_n, elapsed_sec=s_s2,
                        fps_measured=round(s_s2 / pairs_n, 4),
                        sample_key=f"job:{jid}:st{idx}:s2" if jid else None,
                    )
    _backfill_vmaf_sessions(conn)


def record_perf_sample(**kwargs) -> None:
    """Öffentlicher Helfer (z. B. aus Encoder-Bench), um eine reale Laufzeitmessung abzulegen."""
    if _conn is None:
        init_db()
    if _conn is None:
        return
    try:
        with _lock:
            _insert_perf_sample(_conn, ts=kwargs.pop("ts", time.time()), **kwargs)
            _conn.commit()
    except sqlite3.Error as exc:
        logger.debug("Performance-Sample nicht speicherbar: %s", exc)


def list_perf_samples(limit: int = 800) -> list[dict]:
    """Liefert die jüngsten Performance-Messwerte (neueste zuerst)."""
    if _conn is None:
        init_db()
    if _conn is None:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "SELECT * FROM perf_samples ORDER BY ts DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def clear_perf_samples() -> int:
    """Leert gelernte Performance-Samples (nicht die Job-Historie)."""
    if _conn is None:
        init_db()
    if _conn is None:
        return 0
    try:
        with _lock:
            cur = _conn.execute("DELETE FROM perf_samples")
            _conn.commit()
            return int(cur.rowcount or 0)
    except sqlite3.Error:
        return 0


def _med_opt(vals: list[float]) -> Optional[float]:
    clean = sorted(v for v in vals if v is not None)
    if not clean:
        return None
    n = len(clean)
    return clean[n // 2] if n % 2 else (clean[n // 2 - 1] + clean[n // 2]) / 2.0


def quality_ranking(
    *,
    height_filter: int = 0,
    hdr_filter: str = "all",
    min_vmaf: float = 0.0,
    group_by_speed: bool = True,
    group_by_version: bool = False,
    complexity_filter: str = "all",
    density_filter: str = "all",
    anime_filter: str = "all",
) -> dict:
    """Aggregierte Encoder-/Codec-Bewertung + faire Head-to-Head-Direktvergleiche am selben Clip."""
    if _conn is None:
        init_db()
    if _conn is None:
        return {"total_samples": 0, "total_sessions": 0, "rankings": [], "head_to_head": []}

    where = ["vmaf > 0"]
    params: list = []
    if height_filter > 0:
        if height_filter <= 720:
            where.append("height_bucket <= 720")
        elif height_filter <= 1080:
            where.append("height_bucket IN (1080, 1440)")
        else:
            where.append("height_bucket >= 2160")
    if hdr_filter == "hdr":
        where.append("is_hdr = 1")
    elif hdr_filter == "sdr":
        where.append("is_hdr = 0")
    if complexity_filter in ("complex", "typical", "quiet"):
        where.append("complexity = ?")
        params.append(complexity_filter)
    if density_filter in ("dense", "mid", "light"):
        where.append("source_density = ?")
        params.append(density_filter)
    if anime_filter == "anime":
        where.append("anime = 1")
    elif anime_filter == "live":
        where.append("anime = 0")
    if min_vmaf > 0:
        where.append("vmaf >= ?")
        params.append(float(min_vmaf))

    try:
        with _lock:
            rows = _conn.execute(
                f"SELECT * FROM quality_samples WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT 4000",
                tuple(params),
            ).fetchall()
    except sqlite3.Error:
        return {"total_samples": 0, "total_sessions": 0, "rankings": [], "head_to_head": []}

    items = [dict(r) for r in rows]
    if not items:
        return {"total_samples": 0, "total_sessions": 0, "rankings": [], "head_to_head": []}
    # Liegen für eine Session Szenenwerte vor, zählt die alte Gesamtzeile nicht noch einmal.
    scene_sessions = {str(r.get("session_id") or "") for r in items if str(r.get("complexity") or "")}
    if scene_sessions:
        items = [
            r for r in items
            if str(r.get("complexity") or "") or str(r.get("session_id") or "") not in scene_sessions
        ]
    if not items:
        return {"total_samples": 0, "total_sessions": 0, "rankings": [], "head_to_head": []}

    # 1) Head-to-Head nur innerhalb derselben Szenenkomplexität (Peak nicht gegen Ruhe)
    by_session: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for r in items:
        sid = str(r.get("session_id") or "")
        if not sid or sid.startswith("job:"):
            continue
        cx = str(r.get("complexity") or "")
        ekey = f"{r['platform']}:{r['codec']}"
        by_session.setdefault((sid, cx), {}).setdefault(ekey, []).append(r)

    duels_map: dict[tuple[str, str, str], dict] = {}
    h2h_adv: dict[str, list[float]] = {}  # Encoder -> Liste von Bitraten-Vorteilen (%) ggü. Gegnern

    for (_sid, cx), enc_map in by_session.items():
        if len(enc_map) < 2:
            continue
        keys = sorted(enc_map.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                ka, kb = keys[i], keys[j]
                ra_list, rb_list = enc_map[ka], enc_map[kb]
                # Beste vergleichbare Paarung finden (kleinster VMAF-Abstand, bevorzugt bei VMAF >= 90)
                best_pair = None
                best_diff = 999.0
                for a in ra_list:
                    for b in rb_list:
                        va, vb = float(a.get("vmaf") or 0), float(b.get("vmaf") or 0)
                        diff = abs(va - vb)
                        # Bonus für empfohlene / qualitativ relevante Stufen (>= 90)
                        pen = 0.0 if (va >= 90 and vb >= 90) else 2.5
                        if diff + pen < best_diff:
                            best_diff = diff + pen
                            best_pair = (a, b, diff)
                if not best_pair or best_pair[2] > 3.5:
                    continue
                a, b, _ = best_pair
                kb_a = int(a.get("video_kbps") or 0)
                kb_b = int(b.get("video_kbps") or 0)
                d = duels_map.setdefault((ka, kb, cx), {
                    "a": ka, "b": kb, "complexity": cx, "duels": 0,
                    "br_deltas": [], "vmaf_deltas": [], "p1_deltas": [], "spd_ratios": [],
                })
                d["duels"] += 1
                d["vmaf_deltas"].append(float(a["vmaf"]) - float(b["vmaf"]))
                if a.get("vmaf_1pct") is not None and b.get("vmaf_1pct") is not None:
                    d["p1_deltas"].append(float(a["vmaf_1pct"]) - float(b["vmaf_1pct"]))
                if kb_a > 0 and kb_b > 0:
                    # Negativ = A braucht WENIGER Bitrate als B (besser für A)
                    pct = ((kb_a - kb_b) / kb_b) * 100.0
                    d["br_deltas"].append(pct)
                    h2h_adv.setdefault(ka, []).append(-pct)
                    h2h_adv.setdefault(kb, []).append(pct)
                if a.get("speed_x") and b.get("speed_x") and float(b["speed_x"]) > 0:
                    d["spd_ratios"].append(float(a["speed_x"]) / float(b["speed_x"]))

    head_to_head = []
    for d in sorted(duels_map.values(), key=lambda item: item["duels"], reverse=True):
        br_med = _med_opt(d["br_deltas"])
        vm_med = _med_opt(d["vmaf_deltas"])
        p1_med = _med_opt(d["p1_deltas"])
        sr_med = _med_opt(d["spd_ratios"])
        head_to_head.append({
            "a": d["a"],
            "b": d["b"],
            "complexity": d.get("complexity") or "",
            "duels": d["duels"],
            "bitrate_delta_pct": round(br_med, 1) if br_med is not None else None,
            "vmaf_delta": round(vm_med, 2) if vm_med is not None else None,
            "p1_delta": round(p1_med, 2) if p1_med is not None else None,
            "speed_ratio": round(sr_med, 2) if sr_med is not None else None,
        })

    # 2) Gruppierte Rangliste pro (platform, codec[, speed][, encoder_version])
    groups: dict[tuple, list[dict]] = {}
    for r in items:
        ver_key = (str(r.get("encoder_version") or "").strip() or "—") if group_by_version else ""
        key = (r["platform"], r["codec"], r["speed"] if group_by_speed else "alle", ver_key)
        groups.setdefault(key, []).append(r)

    rankings = []
    for (plat, cod, sp, ver_key), rows_g in groups.items():
        sess_set = {str(x.get("session_id") or x.get("id")) for x in rows_g}
        ver_counts: dict[str, int] = {}
        for x in rows_g:
            v_str = str(x.get("encoder_version") or "").strip()
            if v_str:
                ver_counts[v_str] = ver_counts.get(v_str, 0) + 1
        versions_sorted = [v for v, _ in sorted(ver_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        primary_ver = (ver_key if (group_by_version and ver_key != "—") else (versions_sorted[0] if versions_sorted else ""))
        vm = _med_opt([float(x["vmaf"]) for x in rows_g if x.get("vmaf")])
        p1 = _med_opt([float(x["vmaf_1pct"]) for x in rows_g if x.get("vmaf_1pct") is not None])
        ms = _med_opt([float(x["ms_ssim"]) for x in rows_g if x.get("ms_ssim") is not None])
        s2 = _med_opt([float(x["ssimulacra2"]) for x in rows_g if x.get("ssimulacra2") is not None])
        ba = _med_opt([float(x["butteraugli"]) for x in rows_g if x.get("butteraugli") is not None])
        cb = _med_opt([float(x["cambi"]) for x in rows_g if x.get("cambi") is not None])
        kb = _med_opt([float(x["video_kbps"]) for x in rows_g if int(x.get("video_kbps") or 0) > 0])
        sv = _med_opt([float(x["savings_pct"]) for x in rows_g if x.get("savings_pct") is not None])
        sx = _med_opt([float(x["speed_x"]) for x in rows_g if x.get("speed_x") is not None and float(x["speed_x"]) > 0])
        adv = _med_opt(h2h_adv.get(f"{plat}:{cod}", []))

        # Transparenter Effizienz-Score (0–100):
        # 50% Qualität (VMAF + 1%-Low), 35% Kompression (Ersparnis + ggf. Head-to-Head-Vorteil), 15% Banding/S2
        q_part = min(100.0, max(0.0, ((vm or 80.0) - 75.0) * 4.0))
        if p1 is not None:
            p1_part = min(100.0, max(0.0, (p1 - 65.0) * 3.0))
            q_part = q_part * 0.65 + p1_part * 0.35
        eff_part = min(100.0, max(0.0, (sv if sv is not None else 35.0) * 1.25))
        if adv is not None:
            eff_part = min(100.0, max(0.0, eff_part + adv * 1.2))
        detail_part = 75.0
        if s2 is not None:
            detail_part = min(100.0, max(0.0, (s2 - 50.0) * 2.5))
        if cb is not None and cb > 4.0:
            detail_part = max(0.0, detail_part - (cb - 4.0) * 6.0)
        score = round(q_part * 0.50 + eff_part * 0.35 + detail_part * 0.15, 1)

        tags = []
        if adv is not None and adv >= 6.0:
            tags.append("Sehr effizient")
        if sx is not None and sx >= 4.0:
            tags.append("Sehr schnell")
        if cb is not None and cb >= 5.0:
            tags.append("Banding prüfen")
        if p1 is not None and vm is not None and (vm - p1) >= 12.0:
            tags.append("1%-Low-Abfall")

        rankings.append({
            "encoder": f"{plat}:{cod}",
            "platform": plat,
            "codec": cod,
            "speed": sp,
            "encoder_version": primary_ver,
            "encoder_versions": versions_sorted,
            "samples": len(rows_g),
            "sessions": len(sess_set),
            "vmaf_median": round(vm, 2) if vm is not None else None,
            "vmaf_1pct_median": round(p1, 2) if p1 is not None else None,
            "ms_ssim_median": round(ms, 4) if ms is not None else None,
            "s2_median": round(s2, 1) if s2 is not None else None,
            "butteraugli_median": round(ba, 3) if ba is not None else None,
            "cambi_median": round(cb, 2) if cb is not None else None,
            "kbps_median": int(round(kb)) if kb is not None else None,
            "savings_median": round(sv, 1) if sv is not None else None,
            "speed_x_median": round(sx, 2) if sx is not None else None,
            "h2h_bitrate_adv_pct": round(adv, 1) if adv is not None else None,
            "score": score,
            "tags": tags,
        })

    rankings.sort(key=lambda x: (x["score"], x["samples"]), reverse=True)
    all_sessions = {str(x.get("session_id") or x.get("id")) for x in items}
    return {
        "total_samples": len(items),
        "total_sessions": len(all_sessions),
        "rankings": rankings,
        "head_to_head": head_to_head[:12],
    }


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
        _dump_steps(getattr(item, "steps", None)),
        json.dumps(getattr(item, "details", {}) or {}, ensure_ascii=False, default=str),
        json.dumps(getattr(item, "report", {}) or {}, ensure_ascii=False, default=str),
    )
    try:
        with _lock:
            _conn.execute(
                """
                INSERT OR REPLACE INTO jobs
                (id, title, path, status, platform, codec, rate_mode, quality,
                 vmaf, original_size, output_size, saved_bytes, duration,
                 created, finished, settings_json, output_path, source_json,
                 encode_seconds, steps_json, details_json, report_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
            _record_item_perf_locked(_conn, item, row[14])
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
    """Aggregierte Kennzahlen über alle gespeicherten Jobs inkl. Encoder-Vergleich & Monatsverlauf."""
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
                "SELECT codec, COUNT(*) c, COALESCE(SUM(saved_bytes),0) s, AVG(vmaf) v "
                "FROM jobs WHERE status='fertig' AND output_size > 0 "
                "GROUP BY codec ORDER BY c DESC"
            ).fetchall()
            avg_dur = _conn.execute(
                "SELECT AVG(duration) a FROM jobs "
                "WHERE status='fertig' AND output_size > 0 AND duration > 0 LIMIT 1"
            ).fetchone()
            enc_rows = _conn.execute(
                "SELECT id, title, platform, codec, vmaf, original_size, output_size, "
                "saved_bytes, duration, encode_seconds, finished, source_json "
                "FROM jobs WHERE status='fertig' AND output_size > 0 "
                "ORDER BY finished DESC"
            ).fetchall()
    except sqlite3.Error as e:
        logger.warning("Statistik konnte nicht gelesen werden: %s", e)
        return _empty_stats()

    orig = int(done["o"] or 0)
    saved = int(done["s"] or 0)
    ratio = (saved / orig * 100.0) if orig else 0.0

    # --- Encoder-Leaderboard, Auflösungen, Monatsverlauf & Top-Platzsparer ----
    enc_agg: dict[tuple[str, str], dict] = {}
    res_agg: dict[str, dict] = {}
    month_agg: dict[str, dict] = {}
    total_media_sec = 0.0
    total_enc_for_media = 0.0

    for r in enc_rows:
        plat = str(r["platform"] or "cpu").lower()
        cod = str(r["codec"] or "av1").lower()
        o_sz = int(r["original_size"] or 0)
        n_sz = int(r["output_size"] or 0)
        s_sz = int(r["saved_bytes"] or 0)
        v_val = float(r["vmaf"]) if r["vmaf"] is not None else None
        enc_sec = float(r["encode_seconds"] or r["duration"] or 0.0)
        fin = float(r["finished"] or 0.0)
        try:
            src = json.loads(r["source_json"] or "{}") or {}
        except (TypeError, ValueError):
            src = {}
        media_dur = float(src.get("duration") or 0.0)
        if media_dur > 0:
            total_media_sec += media_dur
            if enc_sec > 0:
                total_enc_for_media += enc_sec
        h_val = int(src.get("height") or 0)
        if h_val >= 1800:
            res_key = "4K / UHD"
        elif h_val >= 960:
            res_key = "1080p FullHD"
        elif h_val >= 650:
            res_key = "720p HD"
        elif h_val > 0:
            res_key = "SD / DVD"
        else:
            res_key = "Unbekannt"

        # Nur echte Video-Encodes ins Encoder-Speed-Ranking aufnehmen
        if cod not in ("remux", "audio-opt", "concat", "split"):
            ek = (plat, cod)
            eb = enc_agg.setdefault(ek, {
                "platform": plat, "codec": cod, "count": 0,
                "orig": 0, "saved": 0, "vmafs": [], "speeds": [],
            })
            eb["count"] += 1
            eb["orig"] += o_sz
            eb["saved"] += s_sz
            if v_val is not None and v_val > 0:
                eb["vmafs"].append(v_val)
            if media_dur > 0 and enc_sec > 0:
                eb["speeds"].append(media_dur / enc_sec)

        rb = res_agg.setdefault(res_key, {
            "resolution": res_key, "count": 0, "orig": 0, "saved": 0, "vmafs": [],
        })
        rb["count"] += 1
        rb["orig"] += o_sz
        rb["saved"] += s_sz
        if v_val is not None and v_val > 0:
            rb["vmafs"].append(v_val)

        if fin > 0:
            ym = time.strftime("%Y-%m", time.localtime(fin))
            mb = month_agg.setdefault(ym, {"month": ym, "count": 0, "saved_bytes": 0, "original_bytes": 0})
            mb["count"] += 1
            mb["saved_bytes"] += s_sz
            mb["original_bytes"] += o_sz

    by_encoder: list[dict] = []
    for (plat, cod), eb in enc_agg.items():
        avg_sp = round(sum(eb["speeds"]) / len(eb["speeds"]), 2) if eb["speeds"] else None
        avg_v = round(sum(eb["vmafs"]) / len(eb["vmafs"]), 2) if eb["vmafs"] else None
        sv_pct = round(eb["saved"] / eb["orig"] * 100.0, 1) if eb["orig"] > 0 else 0.0
        by_encoder.append({
            "platform": plat,
            "codec": cod,
            "label": f"{plat.upper()} · {cod.upper()}",
            "count": eb["count"],
            "saved_bytes": eb["saved"],
            "saved_percent": sv_pct,
            "avg_vmaf": avg_v,
            "avg_speed_x": avg_sp,
            "speed_x": avg_sp,
        })
    by_encoder.sort(key=lambda x: (-x["count"], -x["saved_bytes"]))

    fastest = max(
        (e for e in by_encoder if e.get("avg_speed_x") is not None),
        key=lambda e: e["avg_speed_x"],
        default=None,
    )
    most_efficient = max(
        (e for e in by_encoder if e.get("saved_percent", 0) > 0),
        key=lambda e: e["saved_percent"],
        default=None,
    )

    by_resolution = [
        {
            "resolution": rb["resolution"],
            "count": rb["count"],
            "saved_bytes": rb["saved"],
            "saved_percent": round(rb["saved"] / rb["orig"] * 100.0, 1) if rb["orig"] > 0 else 0.0,
            "avg_vmaf": round(sum(rb["vmafs"]) / len(rb["vmafs"]), 2) if rb["vmafs"] else None,
        }
        for rb in sorted(res_agg.values(), key=lambda x: -x["saved"])
    ]

    timeline = [month_agg[k] for k in sorted(month_agg.keys())[-12:]]

    top_savers = [
        {
            "id": r["id"],
            "title": r["title"],
            "codec": r["codec"],
            "platform": r["platform"],
            "original_size": int(r["original_size"] or 0),
            "output_size": int(r["output_size"] or 0),
            "saved_bytes": int(r["saved_bytes"] or 0),
            "vmaf": round(float(r["vmaf"]), 2) if r["vmaf"] is not None else None,
        }
        for r in sorted(enc_rows, key=lambda x: int(x["saved_bytes"] or 0), reverse=True)
        if int(r["saved_bytes"] or 0) > 0
    ][:5]

    realtime_factor = round(total_media_sec / total_enc_for_media, 2) if total_enc_for_media > 0 else None

    return {
        "count_done": int(done["c"] or 0),
        "count_failed": int(failed or 0),
        "original_bytes": orig,
        "output_bytes": int(done["n"] or 0),
        "saved_bytes": saved,
        "saved_percent": round(ratio, 1),
        "encode_seconds": int(done["d"] or 0),
        "media_seconds": round(total_media_sec, 1),
        "realtime_factor": realtime_factor,
        "avg_vmaf": round(done["v"], 2) if done["v"] is not None else None,
        "avg_duration": float(avg_dur["a"] or 0) if avg_dur else 0.0,
        "by_codec": [
            {
                "codec": r["codec"],
                "count": r["c"],
                "saved_bytes": int(r["s"] or 0),
                "avg_vmaf": round(float(r["v"]), 2) if r["v"] is not None else None,
            }
            for r in by_codec
        ],
        "by_encoder": by_encoder,
        "fastest_encoder": fastest,
        "most_efficient_encoder": most_efficient,
        "by_resolution": by_resolution,
        "timeline": timeline,
        "top_savers": top_savers,
    }


def _dump_steps(raw) -> str:
    steps = []
    if isinstance(raw, list):
        for step in raw:
            if not isinstance(step, dict):
                continue
            name = str(step.get("name") or "").strip()
            if not name:
                continue
            try:
                secs = round(max(0.0, float(step.get("seconds") or 0)), 1)
            except (TypeError, ValueError):
                secs = 0.0
            steps.append({"name": name[:160], "seconds": secs})
    try:
        return json.dumps(steps, ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def _attach_steps(row: dict) -> dict:
    raw = row.get("steps_json") or ""
    steps = []
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            for step in parsed:
                if not isinstance(step, dict):
                    continue
                name = str(step.get("name") or "").strip()
                if not name:
                    continue
                try:
                    secs = round(max(0.0, float(step.get("seconds") or 0)), 1)
                except (TypeError, ValueError):
                    secs = 0.0
                steps.append({"name": name, "seconds": secs})
    row["steps"] = steps
    return row


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
    return [_attach_steps(dict(r)) for r in rows]


def get(job_id: str) -> Optional[dict]:
    """Einen einzelnen Job (nach ID) aus der Historie holen."""
    if _conn is None or not job_id:
        return None
    try:
        with _lock:
            row = _conn.execute(
                "SELECT * FROM jobs WHERE id=? LIMIT 1", (str(job_id),)
            ).fetchone()
        return _attach_steps(dict(row)) if row else None
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
                "duration, finished, output_path, settings_json, steps_json FROM jobs "
                "WHERE path=? ORDER BY finished DESC LIMIT ?",
                (str(path), int(limit)),
            ).fetchall()
        return [_attach_steps(dict(r)) for r in rows]
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


def delete_job(job_id: str) -> bool:
    """Löscht einen einzelnen Historien-Eintrag nach ID."""
    if _conn is None or not job_id:
        return False
    with _lock:
        cur = _conn.execute("DELETE FROM jobs WHERE id=?", (str(job_id),))
        _conn.commit()
        return cur.rowcount > 0


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
        "encode_seconds": 0, "media_seconds": 0.0, "realtime_factor": None, "avg_vmaf": None,
        "avg_duration": 0.0, "by_codec": [], "by_encoder": [],
        "fastest_encoder": None, "most_efficient_encoder": None,
        "by_resolution": [], "timeline": [], "top_savers": [],
    }
