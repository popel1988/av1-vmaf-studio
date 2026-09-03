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
import re
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
            try:
                extras = sidecar_details(f)
            except Exception:
                logger.exception("NFO/Sidecar für %s", f)
                extras = {"nfo": None, "sidecars": []}
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
                    "container": (info.container or "").split(",")[0],
                    "fps": round(info.fps, 3) if info.fps else 0,
                    "bit_depth": info.bit_depth,
                    "profile": info.profile or "",
                    "audio": info.audio or [],
                    "subtitles": info.subtitles or [],
                    "nfo": extras.get("nfo"),
                    "sidecars": extras.get("sidecars") or [],
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


# --- Sidecar / NFO (Kodi, Jellyfin, Emby) -----------------------------------

# Nur so viel lesen (DoS). Größere Dateien nicht verwerfen – Metadaten stehen vorn.
_NFO_MAX_BYTES = 2 * 1024 * 1024
_PLOT_MAX = 2000
_SIDECAR_SUB = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi"}
_NFO_ROOT_TAGS = ("movie", "tvshow", "episodedetails", "musicvideo")


def sidecar_details(video: Path) -> dict:
    """NFO + externe Untertitel neben der Videodatei (gleicher Ordner)."""
    return {
        "nfo": collect_nfo(video),
        "sidecars": _sidecar_subs(video),
    }


def file_details(video: Path, probe: bool = True) -> dict:
    """NFO/Sidecars sofort; ffprobe nur wenn ``probe`` (Ton/UT nachladen)."""
    extras = sidecar_details(video)
    if not probe:
        return extras
    info, err = ff.probe_with_error(video)
    if info is None:
        return {"error": err or "ffprobe fehlgeschlagen", **extras,
                "audio": [], "subtitles": []}
    return {
        "audio": info.audio or [],
        "subtitles": info.subtitles or [],
        "container": (info.container or "").split(",")[0],
        "fps": round(info.fps, 3) if info.fps else 0,
        "bit_depth": info.bit_depth,
        "profile": info.profile or "",
        "codec": info.codec,
        "resolution": f"{info.width}x{info.height}",
        **extras,
    }


def collect_nfo(video: Path) -> Optional[dict]:
    """Alle lesbaren .nfo im Filmordner (plus tvshow.nfo in Elternordnern)."""
    parsed: list[dict] = []
    for p in _nfo_paths(video):
        data = parse_nfo(p)
        if data:
            parsed.append(data)
    if not parsed:
        return None
    out: dict = {}
    for item in sorted(parsed, key=lambda d: 0 if d.get("kind") == "tvshow" else 1):
        for k, v in item.items():
            if v in (None, "", [], {}):
                continue
            if k == "title" and item.get("kind") == "tvshow" and out.get("title"):
                out["showtitle"] = v
                continue
            if k == "showtitle" and item.get("kind") == "tvshow" and out.get("title"):
                out["showtitle"] = v
                continue
            out[k] = v
    if len(parsed) > 1:
        out["files"] = [p.get("file") for p in parsed if p.get("file")]
    return out or None


_NFO_EP = re.compile(r"(?:s(\d{1,2})e(\d{1,3})|(\d{1,2})x(\d{1,3}))", re.I)
_NFO_SEASON_DIR = re.compile(
    r"(season|staffel)\s*\d+|\bs\d{1,2}\b", re.I)


def _nfo_ep_key(s: str) -> str:
    m = _NFO_EP.search(s or "")
    if not m:
        return ""
    if m.group(1) is not None:
        return f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
    return f"s{int(m.group(3)):02d}e{int(m.group(4)):02d}"


def _is_nfo_file(p: Path) -> bool:
    if p.suffix.lower() != ".nfo":
        return False
    try:
        return not p.is_dir()
    except OSError:
        return True


def _nfo_list_dir(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if _is_nfo_file(p)]
    except OSError:
        return []


def _nfo_dirs(video: Path) -> list[Path]:
    """Ordner der Datei plus Aufloesung, falls die Datei ein Symlink ist."""
    out: list[Path] = []
    seen: set[str] = set()

    def _add(d: Path) -> None:
        try:
            key = str(d)
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        out.append(d)

    _add(video.parent)
    try:
        if video.is_symlink():
            _add(video.resolve().parent)
    except OSError:
        pass
    return out


def _nfo_paths(video: Path) -> list[Path]:
    """Im Filmordner jede .nfo. In Staffelordnern nur passende Episode + tvshow."""
    found: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            return
        if not _is_nfo_file(p):
            return
        seen.add(key)
        found.append(p)

    dirs = _nfo_dirs(video)
    nfo_here: list[Path] = []
    for d in dirs:
        nfo_here.extend(_nfo_list_dir(d))

    seasonish = any(_NFO_SEASON_DIR.search(d.name or "") for d in dirs)
    v_ep = _nfo_ep_key(video.stem)
    if seasonish:
        for p in nfo_here:
            n_ep = _nfo_ep_key(p.stem)
            if n_ep:
                if v_ep and n_ep == v_ep:
                    add(p)
            else:
                add(p)
    else:
        for p in nfo_here:
            add(p)

    cur = video.parent
    for _ in range(3):
        parent = cur.parent
        if not parent or parent == cur:
            break
        add(parent / "tvshow.nfo")
        add(parent / "TVShow.nfo")
        cur = parent
    return found


def _sidecar_subs(video: Path) -> list[dict]:
    stem_l = video.stem.lower()
    out: list[dict] = []
    try:
        for p in video.parent.iterdir():
            if not p.is_file() or p.suffix.lower() not in _SIDECAR_SUB:
                continue
            if not p.name.lower().startswith(stem_l):
                continue
            out.append({"name": p.name, "ext": p.suffix.lower().lstrip(".")})
    except OSError:
        return []
    out.sort(key=lambda x: x["name"].lower())
    return out


def parse_nfo(path: Path) -> Optional[dict]:
    """Kodi/Jellyfin-NFO (movie / tvshow / episodedetails) als kompaktes Dict."""
    text = _read_nfo_text(path)
    if not text:
        return None
    data = _parse_nfo_xml(text)
    if not data:
        data = _parse_nfo_loose(text)
    if not data:
        return None
    data["file"] = path.name
    return data


def _read_nfo_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    if len(raw) > _NFO_MAX_BYTES:
        raw = raw[:_NFO_MAX_BYTES]
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        head = raw[:240].decode("ascii", errors="replace")
        enc_m = re.search(r"encoding\s*=\s*['\"]([^'\"]+)['\"]", head, re.I)
        enc = (enc_m.group(1).strip() if enc_m else "utf-8") or "utf-8"
        try:
            text = raw.decode(enc, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
    return text.lstrip("\ufeff").strip() or ""


def _parse_nfo_xml(text: str) -> Optional[dict]:
    root = _nfo_root(text)
    if root is None:
        return None
    tag = _nfo_tag(root)
    if tag not in _NFO_ROOT_TAGS:
        nested = _nfo_child(root, *_NFO_ROOT_TAGS)
        if nested is not None:
            root = nested
            tag = _nfo_tag(root)
    kind = {"movie": "movie", "tvshow": "tvshow",
            "episodedetails": "episode", "musicvideo": "movie"}.get(tag, tag or "nfo")

    def one(*names: str) -> str:
        el = _nfo_child(root, *names)
        return _xml_text(el)

    genres = [_xml_text(g) for g in _nfo_children(root, "genre") if _xml_text(g)]
    studios = [_xml_text(s) for s in _nfo_children(root, "studio") if _xml_text(s)]
    plot = one("plot", "outline")
    if len(plot) > _PLOT_MAX:
        plot = plot[:_PLOT_MAX].rstrip() + "…"
    rating = _nfo_rating(root)
    year = one("year")
    if not year:
        prem = one("premiered", "aired")
        if len(prem) >= 4 and prem[:4].isdigit():
            year = prem[:4]
    out = {
        "kind": kind,
        "title": one("title", "localtitle"),
        "originaltitle": one("originaltitle"),
        "showtitle": one("showtitle"),
        "year": year,
        "plot": plot,
        "tagline": one("tagline"),
        "rating": rating,
        "mpaa": one("mpaa"),
        "runtime": one("runtime"),
        "season": one("season"),
        "episode": one("episode"),
        "aired": one("aired", "premiered"),
        "genres": genres[:8],
        "studio": studios[0] if studios else "",
        "uniqueid": _nfo_unique_id(root),
    }
    if not any(out.get(k) for k in ("title", "plot", "year", "showtitle", "originaltitle")):
        return None
    if not out.get("title") and out.get("originaltitle"):
        out["title"] = out["originaltitle"]
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _nfo_sanitize(text: str) -> str:
    """Deklaration/DOCTYPE entfernen; HTML-Entities, die XML sonst abbricht."""
    import html as _html
    raw = re.sub(r"<\?xml\b.*?\?>", "", text, count=1, flags=re.I | re.S)
    raw = re.sub(
        r"<!DOCTYPE\b[^[>]*(\[[\s\S]*?\]\s*)?>", "", raw, count=1, flags=re.I)
    raw = re.sub(r"<!--.*?-->", "", raw, count=1, flags=re.S)

    def _ent(m: re.Match) -> str:
        name = m.group(1)
        low = name.lower()
        if low in {"lt", "gt", "amp", "apos", "quot"} or name.startswith("#"):
            return m.group(0)
        return _html.unescape(m.group(0))

    return re.sub(r"&(#?\w+);", _ent, raw).strip()


def _nfo_document(text: str) -> str:
    m = re.search(
        rf"<({'|'.join(_NFO_ROOT_TAGS)})\b[\s\S]*?</\1\s*>",
        text, flags=re.I)
    return m.group(0) if m else text


def _nfo_root(text: str):
    """XML-Wurzel: Encoding-Deklaration, DOCTYPE und Namespaces stören fromstring(str)."""
    import xml.etree.ElementTree as ET

    def _try(blob: str):
        blob = (blob or "").strip()
        if not blob:
            return None
        try:
            return ET.fromstring(blob)
        except ET.ParseError:
            return None

    clean = _nfo_sanitize(text)
    root = _try(clean) or _try(_nfo_document(clean))
    if root is not None:
        return root
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return _try(_nfo_document(text))


def _nfo_tag(el) -> str:
    if el is None:
        return ""
    return (el.tag or "").split("}")[-1].lower()


def _nfo_child(root, *names):
    want = {n.lower() for n in names}
    for el in list(root):
        if _nfo_tag(el) in want:
            return el
    return None


def _nfo_children(root, name: str) -> list:
    want = name.lower()
    return [el for el in list(root) if _nfo_tag(el) == want]


def _xml_text(el) -> str:
    if el is None:
        return ""
    parts = [t.strip() for t in el.itertext() if t and t.strip()]
    return " ".join(parts).strip()


def _nfo_rating(root) -> str:
    el = _nfo_child(root, "rating")
    if el is not None:
        raw = _xml_text(el)
        if raw:
            return raw.split()[0]
        val = _nfo_child(el, "value")
        if val is not None:
            raw = _xml_text(val)
            if raw:
                return raw
    ratings = _nfo_child(root, "ratings")
    if ratings is not None:
        for r in _nfo_children(ratings, "rating"):
            val = _nfo_child(r, "value")
            raw = _xml_text(val) if val is not None else _xml_text(r)
            if raw:
                return raw.split()[0]
    return ""


def _nfo_unique_id(root) -> str:
    for el in _nfo_children(root, "uniqueid"):
        raw = _xml_text(el)
        if not raw:
            continue
        typ = str(el.attrib.get("type") or "").lower()
        return f"{typ}:{raw}" if typ else raw
    for name, label in (("imdbid", "imdb"), ("tmdbid", "tmdb"), ("tvdbid", "tvdb")):
        el = _nfo_child(root, name)
        raw = _xml_text(el) if el is not None else ""
        if raw:
            return f"{label}:{raw}"
    return ""


def _parse_nfo_loose(text: str) -> Optional[dict]:
    """Fallback, wenn das XML nicht wohlgeformt ist."""
    def grab(tag: str) -> str:
        m = re.search(
            rf"<{tag}(?:\s[^>]*)?>(?:<!\[CDATA\[(.*?)\]\]>|([^<]+))</{tag}>",
            text, flags=re.I | re.S)
        if not m:
            return ""
        return " ".join((m.group(1) or m.group(2) or "").split()).strip()

    title = grab("title") or grab("localtitle") or grab("originaltitle")
    plot = grab("plot") or grab("outline")
    if plot and len(plot) > _PLOT_MAX:
        plot = plot[:_PLOT_MAX].rstrip() + "…"
    year = grab("year")
    if not year:
        prem = grab("premiered") or grab("aired")
        if len(prem) >= 4 and prem[:4].isdigit():
            year = prem[:4]
    out = {
        "title": title,
        "year": year,
        "plot": plot,
        "rating": grab("rating"),
        "showtitle": grab("showtitle"),
        "originaltitle": grab("originaltitle"),
    }
    if not any(out.values()):
        return None
    if not out.get("title") and out.get("originaltitle"):
        out["title"] = out["originaltitle"]
    out["kind"] = "nfo"
    return {k: v for k, v in out.items() if v}
