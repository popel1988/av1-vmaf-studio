"""Bibliotheks-Scan: Eingabeordner rekursiv nach Videos durchsuchen, per
ffprobe analysieren und nach Kriterien filtern (z. B. „alle H.264 > 10 Mbit/s").

Läuft als Hintergrund-Thread mit Fortschritt, da das Proben vieler Dateien
dauert. Ergebnisse werden im Speicher gehalten, per Endpoint abgefragt und nach
Abschluss zusätzlich als JSON gecacht (letzter Scan bleibt über Neustarts hinweg
verfügbar). Pro Treffer werden HDR/DV-Infos, eine Einspar-Schätzung sowie ein
automatischer Encode-Vorschlag mitgeliefert.
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
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

_CACHE_PATH = config.DATA_DIR / "library_scan.json"

# Codecs, die bereits als effizient gelten (kein erneutes Transcoding nötig).
_EFFICIENT_CODECS = {"av1", "libsvtav1", "av01"}


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


def _snapshot_locked() -> dict:
    matched = list(_state["matched"])
    return {
        "running": _state["running"],
        "done": _state["done"],
        "total": _state["total"],
        "scanned": _state["scanned"],
        "matched": matched,
        "total_size_bytes": _state["total_size_bytes"],
        "total_size_human": ff.human_size(_state["total_size_bytes"]),
        "total_saved_bytes": _state["total_saved_bytes"],
        "total_saved_human": ff.human_size(_state["total_saved_bytes"]),
        "error": _state["error"],
        "generated_at": _state["generated_at"],
        "root": _state["root"],
        "stats": _compute_stats(matched),
    }


def get_state() -> dict:
    with _lock:
        return _snapshot_locked()


def _save_cache() -> None:
    """Letzten (abgeschlossenen) Scan atomar nach DATA_DIR schreiben."""
    try:
        with _lock:
            data = _snapshot_locked()
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as e:  # pragma: no cover
        logger.debug("Library-Cache schreiben fehlgeschlagen: %s", e)


def load_last() -> dict:
    """Zuletzt gecachten Scan laden (für Anzeige beim Öffnen der Seite).

    Füllt den In-Memory-State nur, wenn gerade kein Scan läuft.
    """
    with _lock:
        if _state["running"]:
            return _snapshot_locked()
        if _state["matched"]:
            return _snapshot_locked()
    try:
        if not _CACHE_PATH.exists():
            return get_state()
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        with _lock:
            _state.update(
                running=False, done=True,
                total=int(data.get("total") or 0),
                scanned=int(data.get("scanned") or 0),
                matched=list(data.get("matched") or []),
                total_size_bytes=int(data.get("total_size_bytes") or 0),
                total_saved_bytes=int(data.get("total_saved_bytes") or 0),
                error="",
                generated_at=float(data.get("generated_at") or 0.0),
                root=str(data.get("root") or ""),
            )
    except Exception as e:  # pragma: no cover
        logger.debug("Library-Cache lesen fehlgeschlagen: %s", e)
    return get_state()


def start_scan(root_rel: str, filters: dict) -> bool:
    """Startet einen Scan, sofern nicht bereits einer läuft."""
    global _thread
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, total=0, scanned=0,
                      matched=[], total_size_bytes=0, total_saved_bytes=0,
                      error="", generated_at=time.time(), root=root_rel or "")
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


def clear() -> dict:
    """Aktuelle Treffer/Cache leeren (nur wenn kein Scan läuft)."""
    with _lock:
        if _state["running"]:
            return get_state()
        _state.update(done=False, total=0, scanned=0, matched=[],
                      total_size_bytes=0, total_saved_bytes=0,
                      error="", generated_at=0.0, root="")
    try:
        _CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return get_state()


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
    try:
        # Format-/Container-Filter: gewählte Endungen (ohne Punkt) oder alle.
        exts_raw = [str(e).lower().lstrip(".") for e in (filters.get("extensions") or [])]
        allowed = {"." + e for e in exts_raw if e} or set(config.VIDEO_EXTENSIONS)
        files = list(config.iter_input_files(root_rel, allowed))
        with _lock:
            _state["total"] = len(files)

        min_size = float(filters.get("min_size_mb") or 0) * 1024 * 1024
        min_bitrate = float(filters.get("min_bitrate_mbps") or 0) * 1_000_000
        min_height = int(filters.get("min_height") or 0)
        name_contains = str(filters.get("name_contains") or "").lower()
        name_exclude = [str(t).lower() for t in (filters.get("name_exclude") or [])
                        if str(t).strip()]
        inc = {c.lower() for c in (filters.get("codecs_include") or [])}
        exc = {c.lower() for c in (filters.get("codecs_exclude") or [])}
        target_codec = str(filters.get("target_codec") or "av1")
        # Mehrfach-Auswahl (dynamic_filters) mit Rückfall auf Einzelwert.
        dynamic_filters = [str(d) for d in (filters.get("dynamic_filters") or []) if str(d)]
        single = str(filters.get("dynamic_filter") or "")
        if single and not dynamic_filters:
            dynamic_filters = [single]
        skip_optimized = bool(filters.get("skip_optimized"))
        skip_processed = bool(filters.get("skip_processed"))
        if skip_processed:
            from . import history

        for f in files:
            if _stop.is_set():
                break
            with _lock:
                _state["scanned"] += 1
            try:
                if name_contains and name_contains not in f.name.lower():
                    continue
                # Ausschluss: greift auf den gesamten (relativen) Pfad, damit auch
                # ganze Ordner wie „.archiv" übersprungen werden können.
                if name_exclude:
                    rel_low = (config.rel_input(f) or f.name).lower()
                    if any(t in rel_low for t in name_exclude):
                        continue
                if min_size and f.stat().st_size < min_size:
                    continue
            except OSError:
                continue

            info, err = ff.probe_with_error(f)
            if info is None:
                continue
            codec = (info.codec or "").lower()
            if inc and codec not in inc:
                continue
            if exc and codec in exc:
                continue
            if min_height and info.height < min_height:
                continue
            if min_bitrate and info.video_bitrate < min_bitrate:
                continue
            if not _dynamic_match(info, dynamic_filters):
                continue

            rel = config.rel_input(f) or f.name

            # Bereits verarbeitete Dateien überspringen (Historie).
            if skip_processed and history.is_processed(str(f)):
                continue

            proj = project_savings(info, target_codec)
            if skip_optimized and proj["already_optimized"]:
                continue
            folder = str(Path(rel).parent).replace("\\", "/")
            with _lock:
                _state["matched"].append({
                    "path": rel,
                    "name": f.name,
                    "folder": "" if folder == "." else folder,
                    "size_bytes": info.size_bytes,
                    "size_human": ff.human_size(info.size_bytes),
                    "codec": info.codec,
                    "resolution": f"{info.width}x{info.height}",
                    "height": info.height,
                    "video_bitrate": info.video_bitrate,
                    "video_bitrate_human": ff._bitrate_human(info.video_bitrate),
                    "hdr_type": info.hdr_type,
                    "is_hdr": info.is_hdr,
                    "dolby_vision": info.dolby_vision,
                    "dv_profile": info.dv_profile,
                    "duration": round(info.duration, 2),
                    "duration_human": ff.human_duration(info.duration),
                    "already_optimized": proj["already_optimized"],
                    "est_saved_bytes": proj["est_saved_bytes"],
                    "est_saved_human": ff.human_size(proj["est_saved_bytes"]),
                    "suggest": suggest_encode(info, target_codec),
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
        if not _state["error"]:
            _save_cache()


def export_csv() -> str:
    """Aktuelle Treffer als CSV-Text (für Download)."""
    import csv
    import io
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
