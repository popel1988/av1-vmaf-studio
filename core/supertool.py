"""Super-Tool: geführter, autonomer Stapel-Assistent (FileFlows-artig).

Scannt einen Ordner rekursiv nach Videos (mit Format-/Bibliotheksfiltern) und
legt die Treffer je nach Qualitätsmodus als Batch-Gruppe in die Warteschlange:

- ``target_vmaf``   : jede Datei bekommt eine eigene VMAF-Analyse (auto),
                      empfohlen wird der effizienteste Wert mit VMAF >= Ziel.
- ``representative``: eine VMAF-Analyse pro Gruppe (erste Datei), der ermittelte
                      Wert wird auf alle weiteren angewendet.
- ``fixed``         : fixer CQ/Bitrate ohne VMAF-Analyse.
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff

logger = logging.getLogger("vcompress.supertool")

_lock = threading.RLock()
_stop = threading.Event()
_state: dict = {
    "running": False,
    "done": False,
    "total": 0,
    "scanned": 0,
    "matched": [],
    "error": "",
}
_thread: Optional[threading.Thread] = None


def get_state() -> dict:
    with _lock:
        return {
            "running": _state["running"],
            "done": _state["done"],
            "total": _state["total"],
            "scanned": _state["scanned"],
            "matched": list(_state["matched"]),
            "error": _state["error"],
        }


def start_scan(filters: dict) -> bool:
    """Startet einen Scan, sofern nicht bereits einer läuft."""
    global _thread
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, total=0, scanned=0,
                      matched=[], error="")
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(filters or {},), daemon=True)
    _thread.start()
    return True


def cancel_scan() -> bool:
    """Laufenden Scan abbrechen (kooperativ). True, wenn einer lief."""
    with _lock:
        running = _state["running"]
    if running:
        _stop.set()
    return running


def _run(filters: dict) -> None:
    try:
        folder = str(filters.get("folder") or "")

        # Format-/Container-Filter: gewählte Endungen (ohne Punkt) oder alle.
        exts_raw = [str(e).lower().lstrip(".") for e in (filters.get("extensions") or [])]
        allowed = {"." + e for e in exts_raw if e} or set(config.VIDEO_EXTENSIONS)

        files = list(config.iter_input_files(folder, allowed))
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

        for f in files:
            if _stop.is_set():
                break
            with _lock:
                _state["scanned"] += 1
            try:
                if name_contains and name_contains not in f.name.lower():
                    continue
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

            rel = config.rel_input(f) or f.name
            with _lock:
                _state["matched"].append({
                    "path": rel,
                    "name": f.name,
                    "size_bytes": info.size_bytes,
                    "size_human": ff.human_size(info.size_bytes),
                    "container": (info.container or "").split(",")[0],
                    "codec": info.codec,
                    "resolution": f"{info.width}x{info.height}",
                    "height": info.height,
                    "video_bitrate": info.video_bitrate,
                    "video_bitrate_human": ff._bitrate_human(info.video_bitrate),
                    "hdr_type": info.hdr_type,
                    "is_hdr": info.is_hdr,
                    "dolby_vision": info.dolby_vision,
                    "dv_profile": info.dv_profile,
                    "audio": info.audio,
                    "subtitles": info.subtitles,
                    "duration_human": ff.human_duration(info.duration),
                })
    except Exception as e:  # pragma: no cover
        logger.exception("Super-Tool-Scan fehlgeschlagen")
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True


def quick_list(filters: dict) -> dict:
    """Schnelle Dateiliste OHNE ffprobe für die Live-Vorschau neben der Ordnerwahl.

    Wendet nur die günstigen Filter an (Ordner, Format/Endung, Name enthält/
    ausschließen, Mindestgröße). Codec-/Bitraten-/Höhen-Filter benötigen einen
    Probe und greifen erst beim eigentlichen Scan.
    """
    folder = str(filters.get("folder") or "")
    exts_raw = [str(e).lower().lstrip(".") for e in (filters.get("extensions") or [])]
    allowed = {"." + e for e in exts_raw if e} or set(config.VIDEO_EXTENSIONS)
    min_size = float(filters.get("min_size_mb") or 0) * 1024 * 1024
    name_contains = str(filters.get("name_contains") or "").lower()
    name_exclude = [str(t).lower() for t in (filters.get("name_exclude") or [])
                    if str(t).strip()]

    items: list[dict] = []
    truncated = False
    limit = 1000
    try:
        for f in sorted(config.iter_input_files(folder, allowed)):
            if name_contains and name_contains not in f.name.lower():
                continue
            if name_exclude:
                rel_low = (config.rel_input(f) or f.name).lower()
                if any(t in rel_low for t in name_exclude):
                    continue
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            if min_size and sz < min_size:
                continue
            if len(items) >= limit:
                truncated = True
                break
            rel = config.rel_input(f) or f.name
            items.append({
                "path": rel, "name": f.name,
                "size_bytes": sz, "size_human": ff.human_size(sz),
            })
    except OSError as e:
        return {"files": [], "count": 0, "truncated": False, "error": str(e)}
    return {"files": items, "count": len(items), "truncated": truncated, "error": ""}


def _safe_resolve(rel: str) -> Optional[Path]:
    return config.resolve_input(rel)


# Häufige Sprachbezeichnungen auf einen kanonischen 2-Buchstaben-Code abbilden,
# damit die Whitelist "de, en" auch ISO-639-2-Codes wie "ger"/"deu"/"eng" trifft.
_LANG_ALIASES = {
    "de": {"de", "deu", "ger", "german", "deutsch"},
    "en": {"en", "eng", "english"},
    "fr": {"fr", "fra", "fre", "french", "francais", "français"},
    "es": {"es", "spa", "spanish", "espanol", "español", "castellano"},
    "it": {"it", "ita", "italian", "italiano"},
    "pt": {"pt", "por", "portuguese", "portugues", "português"},
    "nl": {"nl", "nld", "dut", "dutch", "nederlands"},
    "ru": {"ru", "rus", "russian"},
    "ja": {"ja", "jpn", "japanese"},
    "zh": {"zh", "zho", "chi", "chinese", "mandarin"},
    "ko": {"ko", "kor", "korean"},
    "pl": {"pl", "pol", "polish"},
    "sv": {"sv", "swe", "swedish"},
    "da": {"da", "dan", "danish"},
    "no": {"no", "nor", "norwegian"},
    "fi": {"fi", "fin", "finnish"},
    "cs": {"cs", "cze", "ces", "czech"},
    "hu": {"hu", "hun", "hungarian"},
    "tr": {"tr", "tur", "turkish"},
    "ar": {"ar", "ara", "arabic"},
    "hi": {"hi", "hin", "hindi"},
    "und": {"und", "undetermined", "unknown"},
}
_LANG_TO_CANON = {form: canon for canon, forms in _LANG_ALIASES.items()
                  for form in forms}


def _canon_lang(s) -> str:
    t = str(s or "").strip().lower()
    return _LANG_TO_CANON.get(t, t)


def _parse_langs(val) -> set:
    """Whitelist (Liste oder Komma-/Semikolon-String) → Menge kanonischer Codes."""
    if isinstance(val, str):
        parts = val.replace(";", ",").split(",")
    elif isinstance(val, (list, tuple)):
        parts = list(val)
    else:
        parts = []
    return {_canon_lang(p) for p in parts if str(p).strip()}


def _tracks_by_lang(tracks, wanted: set) -> list:
    """Relative Indizes der Spuren, deren Sprache in der Whitelist liegt."""
    return [int(t.get("index", 0)) for t in (tracks or [])
            if _canon_lang(t.get("language")) in wanted]


def _selected_indices(info, pf: dict, audio_langs: set, sub_langs: set):
    """Zu behaltende Ton-/Untertitel-Indizes je Datei.

    Rückgabe: (audio_sel, sub_sel). ``None`` bedeutet „Standard beibehalten"
    (bei Ton: alle; bei Untertiteln: alle). Eine leere Liste bei Untertiteln
    bedeutet bewusst „keine". Precedence: manuelle Auswahl > Sprach-Whitelist.
    """
    a_sel = None
    if isinstance(pf.get("audio_tracks"), list):
        a_sel = [int(x) for x in pf["audio_tracks"]]
    elif audio_langs and info is not None and info.audio:
        picked = _tracks_by_lang(info.audio, audio_langs)
        if picked:  # ohne Treffer alle behalten (kein Ton-Verlust)
            a_sel = picked

    s_sel = None
    if isinstance(pf.get("subtitle_tracks"), list):
        s_sel = [int(x) for x in pf["subtitle_tracks"]]
    elif sub_langs and info is not None:
        s_sel = _tracks_by_lang(info.subtitles, sub_langs)
    return a_sel, s_sel


def _container_choice(info, choice: str) -> str:
    """Ziel-Container fürs Remuxen: mkv/mp4 oder aus der Quelle ableiten."""
    c = str(choice or "mkv").lower()
    if c in ("mkv", "mp4"):
        return c
    src = (getattr(info, "container", "") or "").lower()
    if "mp4" in src or "mov" in src or "m4v" in src or "m4a" in src:
        return "mp4"
    return "mkv"


def _remux_spec(info, container: str, a_sel, s_sel,
                prefer_langs: Optional[set] = None,
                add_sidecar_att: bool = False) -> dict:
    """Edit-Spec für einen reinen Remux-Job (Video 1:1, Spuren gefiltert).

    Disposition wird intelligent gesetzt (Default-Audio/Forced-Subs).
    Optional: Fonts/Cover neben der Quelle anhängen.
    """
    from . import remux

    def entry(t, sel):
        idx = int(t.get("index", 0))
        return {
            "index": idx,
            "keep": (sel is None) or (idx in sel),
            "default": bool(t.get("default")),
            "forced": bool(t.get("forced")),
            "language": t.get("language") or "",
            "title": t.get("title") or "",
        }
    audio = [entry(a, a_sel) for a in (info.audio or [])]
    subs = [entry(s, s_sel) for s in (info.subtitles or [])]
    remux.apply_smart_disposition(audio, subs, prefer_langs)
    spec = {
        "container": container,
        "audio": audio,
        "subtitles": subs,
        "keep_chapters": True,
        "keep_metadata": True,
        "keep_attachments": True,
    }
    if add_sidecar_att:
        try:
            src = Path(info.path)
            att = remux.find_sidecar_attachments(src)
            if att:
                spec["add_attachments"] = att
        except Exception:
            pass
    return spec


def start_batch(queue, paths: list, settings: dict, mode: str,
                per_file: Optional[dict] = None,
                dry_run: bool = False) -> tuple:
    """Treffer je nach Modus als Batch-Gruppe einreihen.

    ``per_file`` erlaubt pro Datei (Schlüssel = relativer Pfad) individuelle
    Overrides – aktuell die Auswahl der Ton-/Untertitelspuren aus der
    Trefferliste. Alles Übrige stammt aus dem gemeinsamen ``settings``.

    Bei ``dry_run=True``: (0, "", "", preview_dict) ohne Einreihen.
    Sonst: (Anzahl hinzugefügt, group_id, Fehlermeldung).
    """
    from .queue_manager import build_job_settings
    from . import library, job_plan

    per_file = per_file or {}
    mode = mode if mode in ("target_vmaf", "representative", "fixed") else "representative"
    # Reiner Remux-Stapel (kein Re-Encode): Video 1:1 kopieren, nur Spuren/
    # Container ändern. Dynamik/VMAF/Qualität sind dann irrelevant.
    remux_only = bool(settings.get("remux_only"))
    remux_container = settings.get("remux_container", "mkv")
    sidecar_att = bool(settings.get("sidecar_attachments"))
    # Dynamik-Behandlung (HDR/Dolby Vision) für den ganzen Stapel. ``auto``
    # entscheidet pro Datei anhand der Quelle (wie im Encoding), die übrigen
    # Werte erzwingen ein festes Verhalten für alle Treffer.
    dynamik = str(settings.get("dynamik", "auto") or "auto")
    _DYN_MAP = {
        "preserve": ("preserve", "preserve"),
        "hdr10": ("preserve", "hdr10"),
        "tonemap": ("tonemap", "tonemap"),
    }
    # Sprach-Whitelist (leer = alle behalten). Ein Probe je Datei ist nur nötig,
    # wenn Auto-Dynamik, eine Whitelist oder Remux aktiv ist.
    audio_langs = _parse_langs(settings.get("audio_languages"))
    sub_langs = _parse_langs(settings.get("subtitle_languages"))
    need_probe = remux_only or (dynamik == "auto") or bool(audio_langs) or bool(sub_langs)
    group = uuid.uuid4().hex[:8]
    batch = uuid.uuid4().hex[:8]  # Dashboard-Kennung über alle Dateien
    added = 0

    _CLEAN = ("dynamik", "audio_languages", "subtitle_languages",
              "remux_only", "remux_container", "sidecar_attachments")
    prefer = audio_langs | sub_langs
    preview_items = []

    for i, rel in enumerate(paths):
        target = _safe_resolve(rel)
        if target is None or not target.is_file():
            continue
        d = dict(settings)
        for k in _CLEAN:
            d.pop(k, None)

        info = None
        if need_probe:
            info, _err = ff.probe_with_error(target)

        pf = per_file.get(rel) or {}
        a_sel, s_sel = _selected_indices(info, pf, audio_langs, sub_langs)

        # --- Reiner Remux (kein Video-Re-Encode) ------------------------------
        if remux_only:
            if info is None:
                continue  # ohne Probe kein sinnvoller Remux
            container = _container_choice(info, remux_container)
            d["video_mode"] = "edit"
            d["vmaf_check"] = False
            d["workflow"] = "auto"
            d["container"] = container
            # Suffix des Encode-Modus (z. B. _av1) ist beim Remux irreführend.
            d["suffix"] = "_remux"
            d["edit_spec"] = _remux_spec(
                info, container, a_sel, s_sel,
                prefer_langs=prefer, add_sidecar_att=sidecar_att)
            d["batch_id"] = batch
            s = build_job_settings(d)
            if dry_run:
                preview_items.append(job_plan.plan_one(str(target), s))
                continue
            item = queue.add_file(str(target), s, group_id=group)
            if item:
                added += 1
            continue

        # --- Encode-Stapel ----------------------------------------------------
        if dynamik == "auto":
            if info is not None:
                sug = library.suggest_encode(info, d.get("codec", "av1"))
                d["hdr_mode"], d["dv_mode"] = sug["hdr_mode"], sug["dv_mode"]
            else:
                d["hdr_mode"], d["dv_mode"] = "tonemap", ""
        elif dynamik in _DYN_MAP:
            d["hdr_mode"], d["dv_mode"] = _DYN_MAP[dynamik]

        # Spurauswahl (manuelle Auswahl > Sprach-Whitelist > Standard).
        if a_sel is not None:
            d["audio_tracks"] = a_sel
            d["audio_per_track"] = False
        if s_sel is not None:
            d["subtitle_per_track"] = True
            d["subtitle_track_settings"] = [{"index": j} for j in s_sel]
            d["keep_subtitles"] = bool(s_sel)

        d["batch_id"] = batch
        if mode == "fixed":
            d["vmaf_check"] = False
            d["workflow"] = "auto"
        elif mode == "target_vmaf":
            # Jede Datei erhält eine eigene Analyse (eigene Gruppe → i. d. R.
            # kein gemeinsamer Wert). workflow=auto encodet direkt mit dem
            # effizientesten Wert >= Ziel-VMAF.
            d["vmaf_check"] = True
            d["workflow"] = "auto"
        else:  # representative
            # Nur die erste Datei analysiert; alle übernehmen den Wert.
            d["vmaf_check"] = (i == 0)
            d["workflow"] = "auto"
        s = build_job_settings(d)
        if dry_run:
            preview_items.append(job_plan.plan_one(str(target), s))
            continue
        # target_vmaf: jede Datei eigene Gruppe, damit sie einzeln analysiert.
        gid = uuid.uuid4().hex[:8] if mode == "target_vmaf" else group
        item = queue.add_file(str(target), s, group_id=gid)
        if item:
            added += 1

    if dry_run:
        return 0, "", "", {
            "count": len(preview_items),
            "duplicates": sum(1 for x in preview_items if x.get("duplicate")),
            "items": preview_items,
            "on_duplicate": settings.get("on_duplicate", "ask"),
        }
    if not added:
        return 0, "", "Keine gültigen Dateien gefunden."
    return added, batch, ""
