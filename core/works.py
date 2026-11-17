"""Werke in der Bibliothek: Film, Folge, Sendung, Doku und eigene Sammlungen.

Die Zuordnung liest Titel, Jahr, Folge und Sendedatum aus dem Dateinamen,
dem Ordner und einer vorhandenen NFO. Technische Zusätze wie Auflösung
werden zur Fassung, nicht zum Titel. Eigene Sammlungen fassen Dateien
zusammen, die kein gemeinsames Werk im Namen tragen, zum Beispiel
mehrere eigene Aufnahmen zum selben Thema.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import config

_CATALOG_PATH = config.DATA_DIR / "library_catalog.json"
_lock = threading.RLock()

_KINDS = {"film", "serie", "sendung", "sammlung"}

_EPISODE = re.compile(r"\bS(\d{1,2})[\s._-]*E(\d{1,3})\b", re.I)
_EPISODE_X = re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.I)
_YEAR_PARENS = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")
_YEAR_TOKEN = re.compile(r"\b((?:19|20)\d{2})\b")
_DATE_ISO = re.compile(r"(?P<y>20\d{2})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})")
_DATE_DE = re.compile(r"(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>20\d{2})")
_SEASON_DIR = re.compile(r"^(?:staffel|season)\s*0*(\d+)$", re.I)
_EXTRA = re.compile(
    r"\b(trailer|featurette|making[- ]of|interview|extras?|probe|sample|vorschau)\b",
    re.I,
)
_DOCU = re.compile(r"doku|document", re.I)

_EDITION_RES = (
    ("2160p", "2160p"),
    ("1080p", "1080p"),
    ("720p", "720p"),
    ("576p", "576p"),
    ("480p", "480p"),
)


def describe_work(name: str, folder: str = "", nfo: Optional[dict] = None, *, media: Optional[dict] = None) -> dict:
    """Compatibility shape for older clients, using the same conservative groups."""
    m = media or describe_media(name, folder, folder_movie=nfo)
    stem = Path(name).stem
    key = m['identity'] or f"datei:{(Path(folder) / name).as_posix()}"
    episode = (f"S{m['season']:02d}E{m['episode']:02d}" if m['season'] is not None and m['episode'] is not None else '')
    return dict(key=key, group=key, kind=m['kind'], title=m['show'] or m['title'], year=m['year'],
                subtitle=m['title'] if m['show'] and m['show'] != m['title'] else '',
                episode=episode, edition=_edition(stem), extra=bool(_EXTRA.search(stem)))


def _title_key(text: str) -> str:
    return ' '.join(re.findall(r'[^\W_]+', text.casefold(), re.UNICODE))


def describe_media(name: str, folder: str = '', *, file_nfo: Optional[dict] = None,
                   folder_movie: Optional[dict] = None, show_nfo: Optional[dict] = None,
                   cover: bool = False, dedicated_folder: bool = False) -> dict:
    """Vorschläge und sichere Medienidentität getrennt bestimmen.

    IDs stammen aus der Datei-NFO oder einer passenden movie.nfo im Einzelfilmordner.
    In gemischten Ordnern liefern gemeinsame Metadaten höchstens Anzeigeinformationen.
    """
    stem = Path(name or 'Video').stem.replace('_', ' ')
    ep = _EPISODE.search(stem) or _EPISODE_X.search(stem)
    date = _date_in(stem) if not ep else ''
    year_match = re.search(r'\(((?:19|20)\d{2})\)', stem) or _YEAR_TOKEN.search(stem)
    # Ein Titel wie „1917“ ist für sich genommen kein Erscheinungsjahr.
    year = int(year_match[1]) if year_match and stem[:year_match.start()].strip(' .-(') else 0
    name_title = _clean_loose(stem[:year_match.start()].rstrip(' (.-') if year else stem) or stem
    media = dict(title=name_title, year=year, kind='film' if year else 'video',
                 description='', show='', season=None, episode=None, identity='',
                 cover=bool(cover), source='suggestion')
    if ep:
        prefix = _clean_loose(stem[:ep.start()])
        subtitle = _clean_loose(stem[ep.end():])
        media.update(kind='serie', season=int(ep[1]), episode=int(ep[2]),
                     title=subtitle or prefix or ep[0], show=prefix)
    elif date:
        media.update(kind='sendung', year=int(date[:4]), title=_clean_loose(
            _DATE_DE.sub(' ', _DATE_ISO.sub(' ', stem))) or stem)
    elif _DOCU.search(name_title):
        media['kind'] = 'doku'

    nfo = file_nfo or {}
    if not nfo and folder_movie and not ep and not date:
        # Ein Ordnername allein belegt keine Zugehörigkeit zur movie.nfo.
        titles = {_title_key(folder_movie.get(k, '')) for k in ('title', 'originaltitle')}
        fy = _year_of(folder_movie, '')
        if _title_key(name_title) in titles and not (year and fy and year != fy):
            nfo = folder_movie
    show_nfo = show_nfo or {}
    if ep or nfo.get('kind') == 'episode':
        _, series_dir = _folder_hints(folder)
        folder_title = _title_year(series_dir)
        media['show_year'] = (_year_of(show_nfo, '') or (folder_title[1] if folder_title else 0)
                              or (year if ep and year_match and year_match.start() < ep.start() else 0))
        if not media['show']:
            media['show'] = show_nfo.get('title') or _clean_loose(series_dir)
            if show_nfo.get('title'):
                media['source'] = 'nfo'
    if not nfo or nfo.get('kind') == 'tvshow':
        return media

    title = nfo.get('title') or nfo.get('originaltitle') or ''
    nfo_year = _year_of(nfo, '')
    kind = nfo.get('kind')
    ids = nfo.get('provider_ids') or {}
    known = bool((file_nfo or dedicated_folder) and ids and title)
    if _EXTRA.search(stem.replace('.', ' ')) or any(_EXTRA.fullmatch(p) for p in re.split(r'[/\\]', folder)):
        known = False
    if kind == 'episode':
        coords = _episode_from_nfo(nfo)
        known = known and bool(coords) and (not ep or coords == _episode_from_name(stem))
        if coords:
            s, e = coords[1:].split('E')
            media.update(season=int(s), episode=int(e))
        media.update(kind='serie', show=nfo.get('showtitle') or media['show'] or show_nfo.get('title', ''))
        # Eine versehentlich in die Folgen-NFO kopierte Serien-ID ist keine Folgen-ID.
        show_ids = show_nfo.get('provider_ids') or {}
        known = known and not any(show_ids.get(k) == v for k, v in ids.items())
    elif kind == 'movie' and not ep:
        media['kind'] = _film_kind(nfo.get('genres') or [])
        if year and not date:
            known = known and (not nfo_year or year == nfo_year) and _title_key(name_title) in {
                _title_key(title), _title_key(nfo.get('originaltitle', ''))}
    else:
        known = False
    desc = nfo.get('plot') or nfo.get('tagline') or ''
    if title or nfo_year or desc:
        media.update(title=title or media['title'], year=nfo_year or media['year'],
                     description=desc, source='nfo')
    if known:
        # Auch eine falsch wiederverwendete Provider-ID darf sich widersprechende
        # Titel, Jahre, Serien oder Folgen nicht zusammenführen.
        normalized = _title_key(title)
        parts = [kind, sorted(ids.items()), normalized, media['year']]
        if kind == 'episode':
            parts += [_title_key(media['show']), media['show_year'], media['season'], media['episode']]
        if normalized:
            media['identity'] = json.dumps(parts, ensure_ascii=False, separators=(',', ':'))
    return media


def _film_kind(genres: list) -> str:
    blob = " ".join(str(g) for g in genres)
    if _DOCU.search(blob):
        return "doku"
    return "film"


def _folder_hints(folder: str) -> tuple[str, str]:
    parts = [p for p in str(folder or "").replace("\\", "/").split("/") if p and p != "."]
    parent = parts[-1] if parts else ""
    grand = parts[-2] if len(parts) > 1 else ""
    if _SEASON_DIR.match(parent):
        return "", grand
    return parent, ""


def _episode_from_nfo(nfo: dict) -> str:
    season = str(nfo.get("season") or "").strip()
    episode = str(nfo.get("episode") or "").strip()
    if season.isdigit() and episode.isdigit():
        return f"S{int(season):02d}E{int(episode):02d}"
    return ""


def _episode_from_name(stem: str) -> str:
    match = _EPISODE.search(stem) or _EPISODE_X.search(stem)
    if not match:
        return ""
    return f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"


def _date_in(text: str) -> str:
    match = _DATE_ISO.search(text or "")
    if match:
        return f"{match.group('y')}-{match.group('m')}-{match.group('d')}"
    match = _DATE_DE.search(text or "")
    if match:
        return f"{match.group('y')}-{match.group('m')}-{match.group('d')}"
    return ""


def _title_year(text: str) -> Optional[tuple[str, int]]:
    match = _YEAR_PARENS.match(str(text or "").strip())
    if not match:
        return None
    title = _clean_loose(match.group("title"))
    if not title:
        return None
    return title, int(match.group("year"))


def _year_of(nfo: dict, text: str) -> int:
    raw = str(nfo.get("year") or "")
    if len(raw) >= 4 and raw[:4].isdigit():
        return int(raw[:4])
    match = _YEAR_TOKEN.search(text or "")
    return int(match.group(1)) if match else 0


def _edition(stem: str) -> str:
    low = stem.lower().replace("_", " ").replace(".", " ")
    bits: list[str] = []
    for token, label in _EDITION_RES:
        if re.search(rf"\b{token}\b", low):
            bits.append(label)
            break
    if not bits and re.search(r"\b(4k|uhd)\b", low):
        bits.append("4K")
    if "hdr10+" in low or "hdr10plus" in low:
        bits.append("HDR10+")
    elif re.search(r"\bhdr\b", low):
        bits.append("HDR")
    if re.search(r"director.?s\s+cut", low):
        bits.append("Director's Cut")
    elif re.search(r"\bkinofassung\b", low):
        bits.append("Kinofassung")
    elif re.search(r"\blangfassung\b", low):
        bits.append("Langfassung")
    return " · ".join(bits)


def _clean_loose(text: str) -> str:
    raw = str(text or "").replace(".", " ").replace("_", " ")
    raw = re.sub(r"\[[^\]]*\]", " ", raw)
    raw = re.sub(r"\((?:19|20)\d{2}\)", " ", raw)
    raw = _DATE_ISO.sub(" ", raw)
    raw = _EPISODE.sub(" ", raw)
    raw = _EPISODE_X.sub(" ", raw)
    raw = re.sub(
        r"\b(2160p|1080p|720p|576p|480p|4k|uhd|fhd|hdr10\+|hdr|sdr|hevc|av1|h264|h265|x264|x265)\b",
        " ", raw, flags=re.I,
    )
    raw = re.sub(r"\s+", " ", raw).strip(" -")
    return raw


def list_catalog() -> dict:
    with _lock:
        return _read()


def create_collection(name: str, kind: str = "sammlung") -> dict:
    title = _clean_name(name)
    if not title:
        raise ValueError("Die Sammlung braucht einen Namen.")
    chosen = kind if kind in _KINDS else "sammlung"
    entry = {
        "id": uuid.uuid4().hex[:8],
        "name": title,
        "kind": chosen,
        "created_at": time.time(),
    }
    with _lock:
        data = _read()
        data["collections"].append(entry)
        _write(data)
    return entry


def update_collection(collection_id: str, *, name: str = "", kind: str = "") -> Optional[dict]:
    with _lock:
        data = _read()
        for entry in data["collections"]:
            if entry.get("id") != collection_id:
                continue
            if name:
                title = _clean_name(name)
                if title:
                    entry["name"] = title
            if kind in _KINDS:
                entry["kind"] = kind
            _write(data)
            return dict(entry)
    return None


def delete_collection(collection_id: str) -> bool:
    with _lock:
        data = _read()
        kept = [c for c in data["collections"] if c.get("id") != collection_id]
        if len(kept) == len(data["collections"]):
            return False
        data["collections"] = kept
        data["assignments"] = {
            path: item for path, item in data["assignments"].items()
            if item.get("collection_id") != collection_id
        }
        _write(data)
    return True


def assign_paths(paths: list[str], collection_id: str) -> dict:
    """Legt Dateien in eine Sammlung. Leere collection_id hebt die Zuordnung auf."""
    clean = []
    for raw in paths or []:
        rel = str(raw or "").replace("\\", "/").strip().strip("/")
        if not rel or any(part == ".." for part in rel.split("/")):
            continue
        clean.append(rel)
    with _lock:
        data = _read()
        if collection_id:
            known = {c.get("id") for c in data["collections"]}
            if collection_id not in known:
                raise ValueError("Sammlung nicht gefunden.")
            for rel in clean:
                data["assignments"][rel] = {"collection_id": collection_id}
        else:
            for rel in clean:
                data["assignments"].pop(rel, None)
        _write(data)
        return data


def catalog_media_path(raw: str) -> str:
    """Portable relative video key; also works for offline library entries."""
    rel = raw.replace("\\", "/")
    if (not rel or len(rel) > 4096 or not rel.isprintable() or ":" in rel
            or any(part in ("", ".", "..") for part in rel.split("/"))
            or Path(rel).suffix.lower() not in config.VIDEO_EXTENSIONS):
        raise ValueError("Ein relativer Videopfad wird benötigt.")
    return rel


def decide_paths(paths: list[str], action: str, title: str) -> dict:
    if not 1 <= len(paths) <= 512 or action not in {"confirm", "standalone", "version", "reset"}:
        raise ValueError("Ungültige Auswahl oder Aktion.")
    keys = sorted({catalog_media_path(path) for path in paths})
    if action == "version" and len(keys) < 2:
        raise ValueError("Mindestens zwei verschiedene Dateien für Fassungen auswählen.")
    title = title.strip()
    if action != "reset" and (not title or len(title) > 200 or not title.isprintable()):
        raise ValueError("Ein Titel mit höchstens 200 Zeichen wird benötigt.")
    # Same files -> same opaque group, including after renaming or reordering.
    group = ("manual:" + uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(keys)).hex
             if action == "version" else "")
    with _lock:
        data = _read()
        for key in keys:
            if action == "reset":
                data["decisions"].pop(key, None)
            else:
                data["decisions"][key] = {"title": title, "group": group, "status": action}
        _write(data)
        return data


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()[:80]


def _read() -> dict:
    try:
        raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except ValueError:
        # Never replace an unreadable catalog with a fresh, empty one.
        raise ValueError("Der lokale Bibliothekskatalog ist nicht lesbar.") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("decisions", {}), dict):
        raise ValueError("Ungültiges Format des lokalen Bibliothekskatalogs.")
    collections = []
    for item in raw.get("collections") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        kind = item.get("kind") if item.get("kind") in _KINDS else "sammlung"
        collections.append({
            "id": str(item["id"]),
            "name": _clean_name(item["name"]),
            "kind": kind,
            "created_at": float(item.get("created_at") or 0),
        })
    assignments = {}
    for path, item in (raw.get("assignments") or {}).items():
        if not isinstance(item, dict) or not item.get("collection_id"):
            continue
        rel = str(path).replace("\\", "/").strip().strip("/")
        if rel:
            assignments[rel] = {"collection_id": str(item["collection_id"])}
    return {"collections": collections, "assignments": assignments, "decisions": raw.get("decisions", {})}


def _write(data: dict) -> None:
    from .provenance import atomic_json_write
    atomic_json_write(_CATALOG_PATH, data)
