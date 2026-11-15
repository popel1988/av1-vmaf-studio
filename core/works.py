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


def describe_work(name: str, folder: str = "", nfo: Optional[dict] = None) -> dict:
    """Leitet Art, Titel und Fassung einer Videodatei ab."""
    nfo = nfo if isinstance(nfo, dict) else {}
    stem = Path(str(name or "video")).stem
    parent, show_dir = _folder_hints(folder)
    extra = bool(_EXTRA.search(stem))
    edition = _edition(stem)
    genres = nfo.get("genres") if isinstance(nfo.get("genres"), list) else []

    episode = _episode_from_nfo(nfo) or _episode_from_name(stem)
    if episode:
        show = str(nfo.get("showtitle") or "").strip() or _clean_show(stem, episode, show_dir)
        subtitle = str(nfo.get("title") or "").strip()
        if subtitle.lower() == show.lower():
            subtitle = ""
        year = _year_of(nfo, f"{parent} {show_dir} {stem}")
        return _pack(
            kind="serie", title=show or stem, year=year, subtitle=subtitle,
            edition=edition, extra=extra, episode=episode,
            key=f"serie:{_slug(show or stem)}:{episode}",
        )

    aired = _date_in(stem)
    if aired and not _YEAR_PARENS.match(parent):
        show, subtitle = _split_broadcast(stem, aired)
        if not show:
            show = str(nfo.get("showtitle") or nfo.get("title") or parent or stem).strip()
        return _pack(
            kind="sendung", title=show, year=int(aired[:4]), subtitle=subtitle,
            edition=edition, extra=extra, episode="",
            key=f"sendung:{_slug(show)}:{aired}",
        )

    unique = str(nfo.get("uniqueid") or "").strip()
    nfo_title = str(nfo.get("title") or nfo.get("originaltitle") or "").strip()
    if unique and nfo_title and str(nfo.get("kind") or "") in ("", "movie", "nfo"):
        year = _year_of(nfo, parent or stem)
        kind = _film_kind(genres)
        return _pack(
            kind=kind, title=nfo_title, year=year, subtitle="",
            edition=edition, extra=extra, episode="",
            key=f"film:{_slug(unique)}",
        )

    titled = _title_year(parent) or _title_year(stem) or _title_year(nfo_title)
    if titled:
        title, year = titled
        kind = _film_kind(genres)
        return _pack(
            kind=kind, title=title, year=year, subtitle="",
            edition=edition, extra=extra, episode="",
            key=f"film:{_slug(title)}:{year or 0}",
        )

    if nfo_title:
        year = _year_of(nfo, stem)
        kind = _film_kind(genres)
        return _pack(
            kind=kind, title=nfo_title, year=year, subtitle="",
            edition=edition, extra=extra, episode="",
            key=f"film:{_slug(nfo_title)}:{year or 0}",
        )

    title = _clean_loose(stem) or stem
    return _pack(
        kind=_film_kind(genres), title=title, year=0, subtitle="",
        edition=edition, extra=extra, episode="",
        key=f"film:{_slug(title)}:0",
    )


def _pack(*, kind, title, year, subtitle, edition, extra, episode, key) -> dict:
    group = key
    stored = f"{key}:extra" if extra else key
    return {
        "key": stored,
        "group": group,
        "kind": kind,
        "title": title,
        "year": int(year or 0),
        "subtitle": subtitle,
        "edition": edition,
        "extra": extra,
        "episode": episode,
    }


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


def _clean_show(stem: str, episode: str, show_dir: str) -> str:
    if show_dir:
        titled = _title_year(show_dir)
        return titled[0] if titled else _clean_loose(show_dir)
    cut = _EPISODE.sub(" ", stem)
    cut = _EPISODE_X.sub(" ", cut)
    return _clean_loose(cut)


def _date_in(text: str) -> str:
    match = _DATE_ISO.search(text or "")
    if match:
        return f"{match.group('y')}-{match.group('m')}-{match.group('d')}"
    match = _DATE_DE.search(text or "")
    if match:
        return f"{match.group('y')}-{match.group('m')}-{match.group('d')}"
    return ""


def _split_broadcast(stem: str, aired: str) -> tuple[str, str]:
    plain = stem
    for token in (aired, aired.replace("-", ".")):
        plain = plain.replace(token, " ")
    plain = _clean_loose(plain)
    parts = [p.strip(" -") for p in re.split(r"\s+-\s+", plain) if p.strip(" -")]
    if len(parts) >= 2:
        return parts[0], " - ".join(parts[1:])
    return plain, ""


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


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9äöüß]+", "", str(text or "").lower())
    return slug or "werk"


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
        return _public(data)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()[:80]


def _empty() -> dict:
    return {"collections": [], "assignments": {}}


def _read() -> dict:
    if not _CATALOG_PATH.is_file():
        return _empty()
    try:
        raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
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
    return {"collections": collections, "assignments": assignments}


def _public(data: dict) -> dict:
    return {
        "collections": list(data.get("collections") or []),
        "assignments": dict(data.get("assignments") or {}),
    }


def _write(data: dict) -> None:
    _CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CATALOG_PATH.write_text(
        json.dumps(_public(data), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
