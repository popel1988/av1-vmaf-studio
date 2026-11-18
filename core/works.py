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
_EPISODE_IN_SEASON_TAG = re.compile(
    r"(?:^|[\s._(-])(?:e|ep|episode|folge|teil|part)[\s._-]*0*(\d{1,3})\b",
    re.I,
)
_EPISODE_IN_SEASON_NUM = re.compile(
    r"^\s*0*(\d{1,3})(?:\s*[-._ ]\s+(?P<rest>.+)|\s*$)"
)
_YEAR_PARENS = re.compile(r"^(?P<title>.+?)\s*[\(\[](?P<year>(?:19|20)\d{2})[\)\]]\s*$")
_YEAR_BRACKET = re.compile(r"[\(\[]((?:19|20)\d{2})[\)\]]")
_YEAR_TOKEN = re.compile(r"\b((?:19|20)\d{2})\b")
_DATE_ISO = re.compile(r"(?P<y>20\d{2})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})")
_DATE_DE = re.compile(r"(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>20\d{2})")
_SEASON_DIR = re.compile(
    r"^(?:(?:staffel|season|series)[\s._-]*0*(\d{1,3})|s0*(\d{1,3}))(?:\s*[\(\[].*[\)\]]|\s+-\s+.*)?$",
    re.I,
)
_SPECIALS_DIR = re.compile(r"^(?:specials?|season[\s._-]*0+|staffel[\s._-]*0+)$", re.I)
_EXTRA = re.compile(
    r"\b(trailer|featurettes?|making[- ]of|interview|extras?|bonus|behind[- ]the[- ]scenes|deleted[- ]scenes?|outtakes?|probe|sample|vorschau)\b",
    re.I,
)
_DOCU = re.compile(r"\b(dokus?|dokumentation(?:en)?|documentar(?:y|ies)|docu)\b|doku|document", re.I)

# Allgemeine Sammel-/Kategorieordner, die kein eigener Werktitel sind.
_BUCKET_DIRS = {
    "film", "filme", "movie", "movies", "kino", "spielfilme",
    "serie", "serien", "series", "tv", "tv shows", "tv-shows", "tv serien", "tv-serien", "shows",
    "anime", "animes", "cartoons", "kinder", "kids",
    "doku", "dokus", "dokumentation", "dokumentationen", "documentary", "documentaries",
    "sendung", "sendungen", "mediathek", "tv-aufnahmen",
    "aufnahme", "aufnahmen", "recording", "recordings", "clips",
    "video", "videos", "media", "medien", "bibliothek", "library",
    "download", "downloads", "incoming", "import", "watch", "fertig", "done",
    "archiv", "archive", "archives", "backup", "backups", "sammlung", "collection", "collections",
    "urlaub", "privat", "familie", "heimvideos", "homevideos", "sonstiges", "misc", "other", "unsorted",
    "neu", "new", "temp", "tmp", "test", "input", "output", "encode", "encoded", "encodes", "remux", "remuxes",
    "hd", "uhd", "fhd", "sd", "4k", "8k", "2160p", "1440p", "1080p", "720p", "576p", "480p",
    "hdr", "hdr10", "hdr10+", "dolby vision", "dv", "sdr", "av1", "hevc", "h265", "h264", "x265", "x264",
    "bluray", "blu-ray", "uhd bluray", "web", "web-dl",
}

# Technische Unterordner innerhalb eines Werkordners (Disc-Struktur, Fassung, Auflösung).
_TECH_SUBDIR = re.compile(
    r"^(?:bdmv|stream|video_ts|audio_ts|certificate|playlist|clipinf|"
    r"(?:cd|disc|disk|dvd|bd|uhd|part|teil)[\s._-]*\d{1,2}|"
    r"(?:2160p|1440p|1080p|720p|576p|480p|4k|uhd|fhd|sd|hdr10\+?|hdr|dv|dovi|sdr|"
    r"av1|hevc|h265|h264|x265|x264|remux|bluray|blu-ray|web-dl|webrip|"
    r"director.?s[\s._-]*cut|extended(?:[\s._-]*(?:cut|edition))?|kinofassung|langfassung|"
    r"theatrical|unrated|uncut|imax|open[\s._-]*matte|remastered|3d)"
    r"(?:[\s._+-]+(?:2160p|1080p|720p|4k|uhd|hdr10\+?|hdr|dv|dovi|sdr|av1|hevc|x265|x264|remux))*)$",
    re.I,
)

# Generische oder rein technische Dateinamen, bei denen immer der Werkordner zählt.
_GENERIC_STEM = re.compile(
    r"^(?:movie|film|video|feature|main|hauptfilm|stream|play|index|full|complete|"
    r"output|encode|encoded|remux|converted|export|rip|capture|"
    r"title[\s._-]*t?\d{1,3}|t\d{2,3}|0*\d{1,5}|"
    r"vts[\s._-]*\d{1,2}[\s._-]*\d{1,2}|"
    r"(?:cd|disc|disk|dvd|part|teil)[\s._-]*\d{1,2})$",
    re.I,
)

_RELEASE_NOISE = re.compile(
    r"\b(?:2160p|1440p|1080p|1080i|720p|576p|480p|4k|uhd|fhd|hd|sd|"
    r"hdr10\+|hdr10plus|hdr10|hdr|hlg|sdr|dv|dovi|"
    r"hevc|av1|avc|h\.?264|h\.?265|x264|x265|vp9|mpeg2|vc-?1|10bit|8bit|10-bit|"
    r"bluray|blu-ray|bdrip|brrip|uhdrip|remux|web-dl|webdl|webrip|hdtv|dvdrip|bd50|bd25|"
    r"german|deutsch|ger|english|eng|multi|dual|dl|ml|dubbed|subbed|uncut|unrated|"
    r"repack|proper|rerip|internal|retail|readnfo|remastered|extended|imax|"
    r"truehd|atmos|dts(?:-?hd)?(?:-?ma)?|ddp?\+?[257]?\.?[01]?|eac3|ac3|aac|flac|opus|pcm|mp3|"
    r"5\.1|7\.1|2\.0)\b",
    re.I,
)
_TRAILING_GROUP_OR_SUFFIX = re.compile(
    r"(?:_(?:av1|hevc|h264|vp9|remux|tonemap|hdr10|dv))+|"
    r"\s+-\s*[A-Za-z0-9]{2,14}$",
    re.I,
)

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
    folder_parts = [p for p in str(folder or "").replace("\\", "/").split("/") if p and p != "."]
    is_extra = bool(_EXTRA.search(stem.replace(".", " ")) or any(_EXTRA.fullmatch(p) for p in folder_parts))
    ed_source = f"{' '.join(folder_parts[-2:])} {stem}" if folder_parts else stem
    key = m['identity'] or f"datei:{(Path(folder) / name).as_posix()}"
    episode = (f"S{m['season']:02d}E{m['episode']:02d}" if m['season'] is not None and m['episode'] is not None else '')
    return dict(key=key, group=key, kind=m['kind'], title=m['show'] or m['title'], year=m['year'],
                subtitle=m['title'] if m['show'] and m['show'] != m['title'] else '',
                episode=episode, edition=_edition(ed_source), extra=is_extra)


def _title_key(text: str) -> str:
    return ' '.join(re.findall(r'[^\W_]+', text.casefold(), re.UNICODE))


def _is_bucket_dir(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    # Auch „Archiv (2001)“ oder „Filme [4K]“ bleibt ein Sammelordner.
    stripped = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", raw)
    norm = re.sub(r"[\s._-]+", " ", stripped).strip().casefold()
    return not norm or norm in _BUCKET_DIRS


def _extract_title_and_year(text: str) -> tuple[str, int]:
    """Liest sauberen Titel + Erscheinungsjahr aus Ordner- oder Dateinamen."""
    raw = str(text or "").replace("_", " ").strip()
    if not raw:
        return "", 0
    bracket = _YEAR_BRACKET.search(raw)
    if bracket and raw[:bracket.start()].strip(" .-[("):
        year = int(bracket.group(1))
        title = _clean_loose(raw[:bracket.start()].rstrip(" ([.-"))
        return (title or _clean_loose(raw)), year
    # Ohne Klammern: letztes plausibles Erscheinungsjahr mit nicht-leerem Titel davor
    # (damit „1917 2019 2160p“ → Titel „1917“, Jahr 2019 und „Blade Runner 2049 2017“ → 2017).
    candidates = [
        m for m in _YEAR_TOKEN.finditer(raw)
        if 1900 <= int(m.group(1)) <= 2032 and raw[:m.start()].strip(" .-[(")
    ]
    if candidates:
        chosen = candidates[-1]
        prefix = _clean_loose(raw[:chosen.start()].rstrip(" ([.-"))
        if prefix:
            return prefix, int(chosen.group(1))
    return _clean_loose(raw), 0


def _is_generic_stem(stem: str, cleaned: str) -> bool:
    raw = str(stem or "").strip()
    if not raw or not cleaned:
        return True
    norm = re.sub(r"(?:_(?:av1|hevc|h264|vp9|remux|tonemap|hdr10|dv))+$", "", raw, flags=re.I).strip()
    if _GENERIC_STEM.match(norm) or _TECH_SUBDIR.match(norm):
        return True
    return False


def _tokens_overlap(a: str, b: str) -> bool:
    ta = {w for w in _title_key(a).split() if len(w) >= 3}
    tb = {w for w in _title_key(b).split() if len(w) >= 3}
    if not ta or not tb:
        return False
    return bool(ta & tb)


def _season_number(dirname: str) -> Optional[int]:
    raw = str(dirname or "").strip()
    if not raw:
        return None
    if _SPECIALS_DIR.match(raw):
        return 0
    m = _SEASON_DIR.match(raw)
    if m:
        num = m.group(1) or m.group(2)
        return int(num)
    return None


def _episode_in_season_folder(stem: str, season: int) -> Optional[tuple[int, int, str]]:
    """Erkennt Folgen in einem eindeutigen Staffelordner (z. B. ‚01 - Titel‘, ‚Folge 2‘, ‚E03‘)."""
    tag = _EPISODE_IN_SEASON_TAG.search(stem)
    if tag:
        ep_no = int(tag.group(1))
        rest = _clean_loose(stem[tag.end():]) or _clean_loose(stem[:tag.start()])
        return season, ep_no, rest
    num = _EPISODE_IN_SEASON_NUM.match(stem)
    if num:
        ep_no = int(num.group(1))
        if ep_no not in (264, 265, 480, 576, 720) and ep_no <= 399:
            rest = _clean_loose(num.group("rest") or "")
            return season, ep_no, rest
    return None


def describe_media(name: str, folder: str = '', *, file_nfo: Optional[dict] = None,
                   folder_movie: Optional[dict] = None, show_nfo: Optional[dict] = None,
                   cover: bool = False, dedicated_folder: Optional[bool] = None) -> dict:
    """Vorschläge und sichere Medienidentität getrennt bestimmen.

    Ordnernamen (Werk-/Serien-/Staffelordner) haben bei der Benennung Vorrang vor
    kryptischen oder technisch überladenen Dateinamen. IDs stammen weiterhin nur aus
    der Datei-NFO oder einer passenden movie.nfo im Einzelfilmordner.
    """
    raw_stem = Path(name or 'Video').stem
    stem = raw_stem.replace('_', ' ')
    work_dir, series_dir, season_dir_num, in_extra_dir, in_docu_dir = _folder_context(folder)
    is_extra = bool(in_extra_dir or _EXTRA.search(stem.replace('.', ' ')))

    ep = _EPISODE.search(stem) or _EPISODE_X.search(stem)
    season_ep = (
        _episode_in_season_folder(stem, season_dir_num)
        if (not ep and not is_extra and season_dir_num is not None)
        else None
    )
    date = _date_in(stem) if (not ep and not season_ep) else ''

    stem_title, stem_year = _extract_title_and_year(stem)
    if not stem_title:
        stem_title = stem.strip() or 'Video'
    folder_title, folder_year = _extract_title_and_year(work_dir) if work_dir else ('', 0)

    # Ordner-Vorrang für Filme/Dokus:
    # 1) Einzelfilmordner (dedicated_folder=True) ohne widersprüchliches Dateijahr
    # 2) Generischer/technischer Dateiname (movie.mkv, title_t00.mkv, 00001.m2ts, 1080p.mkv, …)
    # 3) Werkordner mit „Titel (Jahr)“ oder passendem Werknamen (solange kein Sammelordner
    #    mit mehreren unabhängigen Aufnahmen wie dedicated_folder=False ohne Namensbezug).
    use_folder = False
    if folder_title and not ep and not season_ep and not date and not is_extra:
        year_conflict = bool(stem_year and folder_year and stem_year != folder_year)
        generic_file = _is_generic_stem(raw_stem, stem_title)
        noisy_release = bool(_RELEASE_NOISE.search(raw_stem.replace('.', ' ').replace('_', ' ')))
        short_code = bool(folder_year and ' ' not in raw_stem.strip() and ('-' in raw_stem or any(c.isdigit() for c in raw_stem)))
        if not year_conflict:
            if generic_file or dedicated_folder is True:
                use_folder = True
            elif dedicated_folder is None and (folder_year or noisy_release or short_code or _tokens_overlap(folder_title, stem_title)):
                use_folder = True
            elif dedicated_folder is False and (_tokens_overlap(folder_title, stem_title) or (folder_year and (noisy_release or short_code))):
                use_folder = True

    year = (folder_year or stem_year) if use_folder else (stem_year or (folder_year if _tokens_overlap(folder_title, stem_title) else 0))
    name_title = folder_title if use_folder else stem_title

    media = dict(title=name_title, year=year, kind='film' if year else 'video',
                 description='', show='', season=None, episode=None, identity='',
                 cover=bool(cover), source='suggestion')
    if ep or season_ep:
        if ep:
            s_num, e_num = int(ep[1]), int(ep[2])
            prefix = _clean_loose(stem[:ep.start()])
            subtitle = _clean_loose(stem[ep.end():])
            fallback_ep_lbl = f"S{s_num:02d}E{e_num:02d}"
        else:
            s_num, e_num, subtitle = season_ep
            prefix = ''
            fallback_ep_lbl = f"S{s_num:02d}E{e_num:02d}"
        folder_show, _ = _extract_title_and_year(series_dir) if series_dir else ('', 0)
        chosen_show = folder_show or prefix
        if subtitle and chosen_show and _title_key(subtitle) == _title_key(chosen_show):
            subtitle = ''
        media.update(
            kind='serie',
            season=s_num,
            episode=e_num,
            title=subtitle or (prefix if not folder_show else '') or fallback_ep_lbl,
            show=chosen_show,
        )
    elif date:
        media.update(kind='sendung', year=int(date[:4]), title=_clean_loose(
            _DATE_DE.sub(' ', _DATE_ISO.sub(' ', stem))) or stem)
    elif _DOCU.search(name_title) or (in_docu_dir and not is_extra):
        media['kind'] = 'doku'

    nfo = file_nfo or {}
    if not nfo and folder_movie and not ep and not season_ep and not date:
        # Ein gemischter Ordner allein belegt keine Zugehörigkeit zur movie.nfo;
        # im Einzelfilmordner zählt auch der passende Werkordnername.
        titles = {_title_key(folder_movie.get(k, '')) for k in ('title', 'originaltitle')} - {''}
        fy = _year_of(folder_movie, '')
        candidate_keys = {_title_key(stem_title), _title_key(name_title)}
        if dedicated_folder is True and folder_title:
            candidate_keys.add(_title_key(folder_title))
        if (candidate_keys & titles) and not (stem_year and fy and stem_year != fy):
            nfo = folder_movie
    show_nfo = show_nfo or {}
    if ep or season_ep or nfo.get('kind') == 'episode':
        folder_show_pair = _extract_title_and_year(series_dir) if series_dir else ('', 0)
        ep_prefix_year = (stem_year if ep and stem_year and stem.find(str(stem_year)) < ep.start() else 0)
        media['show_year'] = (_year_of(show_nfo, '') or folder_show_pair[1] or ep_prefix_year)
        if show_nfo.get('title'):
            media['show'] = show_nfo['title']
            if not nfo:
                media['source'] = 'nfo'
        elif folder_show_pair[0]:
            media['show'] = folder_show_pair[0]
        elif not media['show'] and series_dir:
            media['show'] = _clean_loose(series_dir)
    if not nfo or nfo.get('kind') == 'tvshow':
        return media

    title = nfo.get('title') or nfo.get('originaltitle') or ''
    nfo_year = _year_of(nfo, '')
    kind = nfo.get('kind')
    ids = nfo.get('provider_ids') or {}
    known = bool((file_nfo or dedicated_folder is True) and ids and title)
    if is_extra:
        known = False
    if kind == 'episode':
        coords = _episode_from_nfo(nfo)
        name_coords = _episode_from_name(stem)
        if not name_coords and season_ep:
            name_coords = f"S{season_ep[0]:02d}E{season_ep[1]:02d}"
        known = known and bool(coords) and (not (ep or season_ep) or coords == name_coords)
        if coords:
            s, e = coords[1:].split('E')
            media.update(season=int(s), episode=int(e))
        media.update(kind='serie', show=nfo.get('showtitle') or show_nfo.get('title') or media['show'])
        # Eine versehentlich in die Folgen-NFO kopierte Serien-ID ist keine Folgen-ID.
        show_ids = show_nfo.get('provider_ids') or {}
        known = known and not any(show_ids.get(k) == v for k, v in ids.items())
    elif kind == 'movie' and not ep and not season_ep:
        media['kind'] = _film_kind(nfo.get('genres') or [])
        nfo_titles = {_title_key(title), _title_key(nfo.get('originaltitle', ''))} - {''}
        if file_nfo:
            if stem_year and not date:
                known = known and (not nfo_year or stem_year == nfo_year) and _title_key(stem_title) in nfo_titles
        else:
            effective_year = stem_year or folder_year
            effective_keys = {_title_key(stem_title), _title_key(name_title)}
            if folder_title:
                effective_keys.add(_title_key(folder_title))
            if effective_year and not date:
                known = known and (not nfo_year or effective_year == nfo_year) and bool(effective_keys & nfo_titles)
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


def _folder_context(folder: str) -> tuple[str, str, Optional[int], bool, bool]:
    """Zerlegt den relativen Ordnerpfad in (werk_ordner, serien_ordner, staffel_nr, extra_ordner, doku_ordner)."""
    parts = [p.strip() for p in str(folder or "").replace("\\", "/").split("/") if p.strip() and p.strip() != "."]
    if not parts:
        return "", "", None, False, False
    in_extra = any(bool(_EXTRA.fullmatch(p)) for p in parts)
    in_docu = any(p.casefold() in {"doku", "dokus", "dokumentation", "dokumentationen", "documentary", "documentaries"} for p in parts[:-1])
    # Technische Unterordner (z. B. BDMV/STREAM, 2160p, Director's Cut) von hinten abtragen.
    trimmed = list(parts)
    while len(trimmed) > 1 and (_TECH_SUBDIR.match(trimmed[-1]) or _EXTRA.fullmatch(trimmed[-1])):
        trimmed.pop()
    parent = trimmed[-1] if trimmed else ""
    grand = trimmed[-2] if len(trimmed) > 1 else ""
    season_num = _season_number(parent)
    if season_num is not None:
        series_dir = grand if not _is_bucket_dir(grand) else ""
        return "", series_dir, season_num, in_extra, in_docu
    work_dir = parent if (not _is_bucket_dir(parent) and not _TECH_SUBDIR.match(parent)) else ""
    series_dir = work_dir
    return work_dir, series_dir, None, in_extra, in_docu


def _folder_hints(folder: str) -> tuple[str, str]:
    work_dir, series_dir, season_num, _, _ = _folder_context(folder)
    if season_num is not None:
        return "", series_dir
    return work_dir, series_dir


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
    raw = str(text or "")
    raw = re.sub(r"(?:_(?:av1|hevc|h264|vp9|remux|tonemap|hdr10|dv))+$", " ", raw, flags=re.I)
    raw = re.sub(
        r"\b(x264|x265|h264|h265|hevc|av1|avc|vp9|bluray|webrip|webdl|web|hdtv|remux|"
        r"1080p|2160p|720p|4k|uhd|hdr|dv|dts|ac3|eac3|aac|atmos|dl|ml|ger|german|internal|proper|repack)"
        r"-[A-Za-z0-9]{2,14}$",
        r"\1 ", raw, flags=re.I,
    )
    raw = raw.replace(".", " ").replace("_", " ")
    raw = re.sub(r"\[[^\]]*\]", " ", raw)
    raw = re.sub(r"\((?:19|20)\d{2}\)", " ", raw)
    raw = _DATE_ISO.sub(" ", raw)
    raw = _EPISODE.sub(" ", raw)
    raw = _EPISODE_X.sub(" ", raw)
    raw = _RELEASE_NOISE.sub(" ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" -–—·")
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
