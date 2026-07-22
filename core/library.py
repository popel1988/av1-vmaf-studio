"""Bibliotheks-Scan: Eingabeordner rekursiv nach Videos durchsuchen und per
ffprobe analysieren.

Der Scan liefert die **volle** Bibliothek (alle Video-Endungen im gewählten
Root). Filter (Name, Codec, Bitrate, …) werden in der UI live auf den letzten
Scan angewendet – ein Rescan ist nur nötig, wenn neue Dateien hinzukommen.

Ergebnisse werden **pro Root** (Unterbibliothek / Medienbaum) im Speicher und
als JSON gecacht. Beim Wechsel der Bibliothek kann die UI sofort den Cache
zeigen oder eine leere Liste, falls noch nie gescannt.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff

logger = logging.getLogger("vcompress.library")

_lock = threading.RLock()
_state: dict = {
    "running": False,
    "done": False,
    "total": 0,
    "scanned": 0,
    "matched": [],
    "total_size_bytes": 0,
    "total_saved_bytes": 0,
    "error": "",
    "generated_at": 0.0,
    "root": "",
}
# Abgeschlossene Scans pro Root ("" = gesamter Medienbaum).
_by_root: dict[str, dict] = {}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

_CACHE_PATH = config.DATA_DIR / "library_scan.json"

# Codecs, die bereits als effizient gelten (kein erneutes Transcoding nötig).
_EFFICIENT_CODECS = {"av1", "libsvtav1", "av01"}


def _norm_root(root: str) -> str:
    return (root or "").replace("\\", "/").strip().strip("/")


def _target_bitrate_kbps(height: int, is_hdr: bool, target_codec: str = "av1") -> int:
    """Grobe Ziel-Videobitrate für eine qualitativ gute Ausgabe.

    HDR bekommt mehr Bitrate; HEVC braucht ggü. AV1 etwas mehr für gleiche Güte.
    """
    if height <= 720:
        base = 2000
    elif height <= 1080:
        base = 4000
    elif height <= 1440:
        base = 7000
    else:
        base = 12000
    if is_hdr:
        base = int(base * 1.5)
    if target_codec == "hevc":
        base = int(base * 1.25)
    return base


def project_savings(info, target_codec: str = "av1") -> dict:
    """Schätzt, wie viel eine Datei durch Transcoding einsparen würde.

    Heuristik auf Basis auflösungsabhängiger Ziel-Bitraten. Bereits effiziente
    Codecs oder Dateien nahe der Ziel-Bitrate gelten als „schon optimiert".
    """
    codec = (info.codec or "").lower()
    src_br = info.video_bitrate or 0
    target_br = _target_bitrate_kbps(info.height, info.is_hdr, target_codec) * 1000
    already = codec in _EFFICIENT_CODECS or (src_br and src_br <= target_br * 1.15)
    if already or info.duration <= 0 or src_br <= 0:
        return {"already_optimized": bool(already), "est_new_size": info.size_bytes,
                "est_saved_bytes": 0}
    src_video_bytes = int(src_br / 8 * info.duration)
    new_video_bytes = int(target_br / 8 * info.duration)
    # Rest (Audio/Untertitel/Overhead) bleibt erhalten.
    rest = max(0, info.size_bytes - src_video_bytes)
    est_new = new_video_bytes + rest
    saved = max(0, info.size_bytes - est_new)
    return {"already_optimized": False, "est_new_size": est_new,
            "est_saved_bytes": saved}


def suggest_encode(info, target_codec: str = "av1") -> dict:
    """Automatischer Encode-Vorschlag je Quelle (Codec + HDR/DV-Behandlung).

    Liefert Overrides, die sich mit den Basis-Einstellungen mischen lassen, sowie
    ein menschenlesbares Label für die UI.
    """
    codec = target_codec if target_codec in ("av1", "hevc") else "av1"
    hdr_mode = ""
    dv_mode = ""
    if info.dolby_vision:
        prof = info.dv_profile or 0
        if prof == 5:
            dv_mode = "tonemap"      # kein HDR10-Fallback -> sicher: SDR
        else:
            dv_mode = "preserve"     # 7 -> 8.1, 8/10 -> behalten
    elif info.is_hdr:
        hdr_mode = "preserve"        # HDR10/HLG behalten
    else:
        hdr_mode = "tonemap"         # SDR: no-op (nur relevant bei HDR-Quellen)

    if dv_mode == "preserve":
        label = f"{codec.upper()} · DV übernehmen"
    elif dv_mode == "tonemap":
        label = f"{codec.upper()} · DV → SDR (Tonemap)"
    elif hdr_mode == "preserve":
        label = f"{codec.upper()} · HDR behalten"
    else:
        label = f"{codec.upper()} · SDR"
    return {"codec": codec, "hdr_mode": hdr_mode, "dv_mode": dv_mode, "label": label}


def _compute_stats(matched: list) -> dict:
    """Dashboard-Statistik über die Treffer: Codec-Verteilung, HDR/DV-Anteil,
    größte Platzfresser."""
    by_codec: dict = {}
    hdr = dv = sdr = 0
    for m in matched:
        c = (m.get("codec") or "?").lower()
        by_codec[c] = by_codec.get(c, 0) + 1
        if m.get("dolby_vision"):
            dv += 1
        elif m.get("is_hdr"):
            hdr += 1
        else:
            sdr += 1
    codec_dist = sorted(({"codec": k, "count": v} for k, v in by_codec.items()),
                        key=lambda x: x["count"], reverse=True)
    hogs = sorted(matched, key=lambda m: m.get("est_saved_bytes", 0), reverse=True)[:10]
    top_hogs = [{"name": h.get("name"), "path": h.get("path"),
                 "size_human": h.get("size_human"),
                 "est_saved_human": h.get("est_saved_human"),
                 "est_saved_bytes": h.get("est_saved_bytes", 0)} for h in hogs]
    return {
        "codec_distribution": codec_dist,
        "hdr_count": hdr, "dv_count": dv, "sdr_count": sdr,
        "top_hogs": top_hogs,
    }


def _empty_snapshot(root: str = "") -> dict:
    root = _norm_root(root)
    return {
        "running": False,
        "done": False,
        "total": 0,
        "scanned": 0,
        "matched": [],
        "total_size_bytes": 0,
        "total_size_human": ff.human_size(0),
        "total_saved_bytes": 0,
        "total_saved_human": ff.human_size(0),
        "error": "",
        "generated_at": 0.0,
        "root": root,
        "stats": _compute_stats([]),
    }


def _entry_to_snapshot(entry: dict, *, running: bool = False) -> dict:
    matched = list(entry.get("matched") or [])
    size = int(entry.get("total_size_bytes") or 0)
    saved = int(entry.get("total_saved_bytes") or 0)
    root = _norm_root(str(entry.get("root") or ""))
    return {
        "running": running,
        "done": bool(entry.get("done", True)),
        "total": int(entry.get("total") or len(matched)),
        "scanned": int(entry.get("scanned") or len(matched)),
        "matched": matched,
        "total_size_bytes": size,
        "total_size_human": ff.human_size(size),
        "total_saved_bytes": saved,
        "total_saved_human": ff.human_size(saved),
        "error": str(entry.get("error") or ""),
        "generated_at": float(entry.get("generated_at") or 0.0),
        "root": root,
        "stats": _compute_stats(matched),
    }


def _snapshot_locked() -> dict:
    """Aktiver Scan-State (laufend oder zuletzt im Worker)."""
    matched = list(_state["matched"])
    size = _state["total_size_bytes"]
    saved = _state["total_saved_bytes"]
    return {
        "running": _state["running"],
        "done": _state["done"],
        "total": _state["total"],
        "scanned": _state["scanned"],
        "matched": matched,
        "total_size_bytes": size,
        "total_size_human": ff.human_size(size),
        "total_saved_bytes": saved,
        "total_saved_human": ff.human_size(saved),
        "error": _state["error"],
        "generated_at": _state["generated_at"],
        "root": _norm_root(_state["root"]),
        "stats": _compute_stats(matched),
    }


def _store_root_locked(snap: dict) -> None:
    """Abgeschlossenen Scan unter seinem Root ablegen."""
    root = _norm_root(snap.get("root") or "")
    _by_root[root] = {
        "matched": list(snap.get("matched") or []),
        "total": int(snap.get("total") or 0),
        "scanned": int(snap.get("scanned") or 0),
        "total_size_bytes": int(snap.get("total_size_bytes") or 0),
        "total_saved_bytes": int(snap.get("total_saved_bytes") or 0),
        "generated_at": float(snap.get("generated_at") or 0.0),
        "root": root,
        "done": True,
        "error": str(snap.get("error") or ""),
    }


def get_state() -> dict:
    with _lock:
        return _snapshot_locked()


def get_cached(root: str = "") -> dict:
    """Gespeicherten Scan für einen Root laden (leer, falls nie gescannt).

    Läuft gerade ein Scan für genau diesen Root, liefert den Live-Stand.
    """
    root = _norm_root(root)
    with _lock:
        if _state["running"] and _norm_root(_state["root"]) == root:
            return _snapshot_locked()
        entry = _by_root.get(root)
        if entry:
            return _entry_to_snapshot(entry)
        return _empty_snapshot(root)


def list_caches() -> dict:
    """Alle gecachten Roots + aktueller Worker-State."""
    with _lock:
        by_root = {
            k: _entry_to_snapshot(v)
            for k, v in _by_root.items()
        }
        # Laufenden Scan einblenden (noch nicht final geschrieben).
        if _state["running"]:
            live = _snapshot_locked()
            by_root[_norm_root(live["root"])] = live
        return {
            "by_root": by_root,
            "running": _state["running"],
            "active_root": _norm_root(_state["root"]) if _state["running"] else "",
            "state": _snapshot_locked(),
        }


def _save_cache() -> None:
    """Alle Root-Caches atomar nach DATA_DIR schreiben."""
    try:
        with _lock:
            data = {
                "version": 2,
                "by_root": {
                    k: {
                        "matched": list(v.get("matched") or []),
                        "total": int(v.get("total") or 0),
                        "scanned": int(v.get("scanned") or 0),
                        "total_size_bytes": int(v.get("total_size_bytes") or 0),
                        "total_saved_bytes": int(v.get("total_saved_bytes") or 0),
                        "generated_at": float(v.get("generated_at") or 0.0),
                        "root": _norm_root(k),
                        "done": True,
                        "error": str(v.get("error") or ""),
                    }
                    for k, v in _by_root.items()
                },
            }
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as e:  # pragma: no cover
        logger.debug("Library-Cache schreiben fehlgeschlagen: %s", e)


def _load_cache_file() -> None:
    """Cache-Datei in ``_by_root`` laden (Aufrufer hält ``_lock``)."""
    global _by_root
    if not _CACHE_PATH.exists():
        return
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover
        logger.debug("Library-Cache lesen fehlgeschlagen: %s", e)
        return

    by_root: dict[str, dict] = {}
    if isinstance(data, dict) and int(data.get("version") or 0) >= 2:
        raw = data.get("by_root") or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                root = _norm_root(str(v.get("root") if v.get("root") is not None else k))
                by_root[root] = {
                    "matched": list(v.get("matched") or []),
                    "total": int(v.get("total") or 0),
                    "scanned": int(v.get("scanned") or 0),
                    "total_size_bytes": int(v.get("total_size_bytes") or 0),
                    "total_saved_bytes": int(v.get("total_saved_bytes") or 0),
                    "generated_at": float(v.get("generated_at") or 0.0),
                    "root": root,
                    "done": True,
                    "error": str(v.get("error") or ""),
                }
    elif isinstance(data, dict) and (data.get("matched") is not None or data.get("root") is not None):
        # v1: einzelner Scan
        root = _norm_root(str(data.get("root") or ""))
        by_root[root] = {
            "matched": list(data.get("matched") or []),
            "total": int(data.get("total") or 0),
            "scanned": int(data.get("scanned") or 0),
            "total_size_bytes": int(data.get("total_size_bytes") or 0),
            "total_saved_bytes": int(data.get("total_saved_bytes") or 0),
            "generated_at": float(data.get("generated_at") or 0.0),
            "root": root,
            "done": True,
            "error": str(data.get("error") or ""),
        }
    _by_root = by_root


def load_last(root: Optional[str] = None) -> dict:
    """Caches laden; optional Snapshot für einen Root zurückgeben.

    Ohne ``root``: ``by_root`` + aktiver State (für UI-Init).
    Mit ``root``: Snapshot genau dieser Bibliothek (leer wenn unbekannt).
    """
    with _lock:
        if not _by_root and not _state["running"] and not _state["matched"]:
            _load_cache_file()
        if _state["matched"] and not _state["running"]:
            snap = _snapshot_locked()
            key = _norm_root(snap["root"])
            if key not in _by_root:
                _store_root_locked(snap)

    if root is not None:
        return get_cached(root)
    return list_caches()


def start_scan(root_rel: str, filters: dict) -> bool:
    """Startet einen Scan, sofern nicht bereits einer läuft."""
    global _thread
    root_rel = _norm_root(root_rel)
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, total=0, scanned=0,
                      matched=[], total_size_bytes=0, total_saved_bytes=0,
                      error="", generated_at=time.time(), root=root_rel)
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(root_rel, filters or {}), daemon=True)
    _thread.start()
    return True


def cancel_scan() -> bool:
    """Laufenden Scan abbrechen (kooperativ). True, wenn einer lief."""
    with _lock:
        running = _state["running"]
    if running:
        _stop.set()
    return running


def clear(root: Optional[str] = None) -> dict:
    """Cache leeren: einen Root oder alle (nur wenn kein Scan läuft)."""
    with _lock:
        if _state["running"]:
            return list_caches() if root is None else get_cached(root or "")
        if root is None:
            _by_root.clear()
            _state.update(done=False, total=0, scanned=0, matched=[],
                          total_size_bytes=0, total_saved_bytes=0,
                          error="", generated_at=0.0, root="")
        else:
            key = _norm_root(root)
            _by_root.pop(key, None)
            if _norm_root(_state["root"]) == key:
                _state.update(done=False, total=0, scanned=0, matched=[],
                              total_size_bytes=0, total_saved_bytes=0,
                              error="", generated_at=0.0, root=key)
    try:
        if root is None:
            _CACHE_PATH.unlink(missing_ok=True)
        else:
            _save_cache()
            if not _by_root and _CACHE_PATH.exists():
                _CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    if root is None:
        return list_caches()
    return get_cached(root)


def _dynamic_match_one(info, dynamic_filter: str) -> bool:
    """Prüft einen einzelnen Dynamik-Filter (SDR/HDR/DV/DV-Profil)."""
    if not dynamic_filter:
        return True
    if dynamic_filter == "sdr":
        return not info.is_hdr
    if dynamic_filter == "hdr":
        return info.is_hdr and not info.dolby_vision
    if dynamic_filter == "dv":
        return bool(info.dolby_vision)
    if dynamic_filter.startswith("dv"):
        try:
            want = int(dynamic_filter[2:])
        except ValueError:
            return bool(info.dolby_vision)
        return bool(info.dolby_vision) and (info.dv_profile or 0) == want
    return True


def _dynamic_match(info, dynamic_filters: list) -> bool:
    """Prüft mehrere Dynamik-Filter (ODER-Verknüpfung). Leer = alle."""
    active = [d for d in (dynamic_filters or []) if d]
    if not active:
        return True
    return any(_dynamic_match_one(info, d) for d in active)


def _run(root_rel: str, filters: dict) -> None:
    """Scannt die Bibliothek vollständig (ffprobe).

    Filter (Name, Codec, Bitrate, …) werden **nicht** mehr serverseitig
    angewendet – die UI filtert den letzten Scan live. ``filters`` wird nur
    noch für optionale Kompatibilität akzeptiert; der Scan nimmt immer alle
    bekannten Video-Endungen im gewählten Root.
    """
    try:
        from . import history

        _ = filters  # bewusst ungenutzt (Live-Filter in der UI)
        allowed = set(config.VIDEO_EXTENSIONS)
        files = list(config.iter_input_files(root_rel, allowed))
        with _lock:
            _state["total"] = len(files)
            _state["root"] = root_rel or ""

        # Default-Projektion AV1 (UI rechnet bei Ziel-Codec-Wechsel neu).
        target_codec = "av1"

        for f in files:
            if _stop.is_set():
                break
            with _lock:
                _state["scanned"] += 1

            info, _err = ff.probe_with_error(f)
            if info is None:
                continue

            rel = config.rel_input(f) or f.name
            folder = str(Path(rel).parent).replace("\\", "/")
            proj = project_savings(info, target_codec)
            sug = suggest_encode(info, target_codec)
            processed = False
            try:
                processed = history.is_processed(str(f.resolve()))
            except OSError:
                processed = history.is_processed(str(f))

            with _lock:
                _state["matched"].append({
                    "path": rel,
                    "name": f.name,
                    "folder": "" if folder == "." else folder,
                    "ext": f.suffix.lower().lstrip("."),
                    "size_bytes": info.size_bytes,
                    "size_human": ff.human_size(info.size_bytes),
                    "codec": info.codec,
                    "resolution": f"{info.width}x{info.height}",
                    "width": info.width,
                    "height": info.height,
                    "video_bitrate": info.video_bitrate,
                    "video_bitrate_human": ff._bitrate_human(info.video_bitrate),
                    "hdr_type": info.hdr_type,
                    "is_hdr": info.is_hdr,
                    "dolby_vision": info.dolby_vision,
                    "dv_profile": info.dv_profile,
                    "duration": round(info.duration, 2),
                    "duration_human": ff.human_duration(info.duration),
                    "processed": processed,
                    "already_optimized": proj["already_optimized"],
                    "est_saved_bytes": proj["est_saved_bytes"],
                    "est_saved_human": ff.human_size(proj["est_saved_bytes"]),
                    "suggest": sug,
                })
                _state["total_size_bytes"] += info.size_bytes
                _state["total_saved_bytes"] += proj["est_saved_bytes"]
    except Exception as e:  # pragma: no cover
        logger.exception("Bibliotheks-Scan fehlgeschlagen")
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True
            snap = _snapshot_locked()
            if not _state["error"]:
                _store_root_locked(snap)
        if not snap.get("error"):
            _save_cache()


def export_csv(root: Optional[str] = None) -> str:
    """Treffer eines Roots (oder aktiver State) als CSV-Text."""
    import csv
    import io
    if root is not None:
        matched = list(get_cached(root).get("matched") or [])
    else:
        with _lock:
            matched = list(_state["matched"])
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Pfad", "Ordner", "Codec", "Aufloesung", "Bitrate",
                "HDR/DV", "Dauer", "Groesse", "Einsparung", "Vorschlag"])
    for m in matched:
        dv = m.get("hdr_type") or ("Dolby Vision" if m.get("dolby_vision") else
                                   ("HDR" if m.get("is_hdr") else "SDR"))
        w.writerow([
            m.get("path", ""), m.get("folder", ""), m.get("codec", ""),
            m.get("resolution", ""), m.get("video_bitrate_human", ""),
            dv, m.get("duration_human", ""), m.get("size_human", ""),
            m.get("est_saved_human", ""),
            (m.get("suggest") or {}).get("label", ""),
        ])
    return buf.getvalue()
