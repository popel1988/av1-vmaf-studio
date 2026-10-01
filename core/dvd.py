"""DVD-Video: VIDEO_TS-Ordner und unverschlüsselte DVD-ISOs.

Die Titelliste kommt aus den IFO-Dateien (VIDEO_TS.IFO und VTS_xx_0.IFO):
Titelnummer, Spieldauer, Kapitel. Lesen und Remuxen übernimmt der
``dvdvideo``-Demuxer von FFmpeg (libdvdread/libdvdnav, FFmpeg ≥ 7.0), der
Ordner und ISO direkt öffnet und Sprach-Tags sowie Kapitel mitliefert.
CSS-verschlüsselte Discs werden nicht entschlüsselt; sie scheitern beim Lesen.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_MIN_SEC = 15.0
_SECTOR = 2048
_TITLE_RE = re.compile(r"^dvd:(\d{1,3})$")


def _u16(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 2], "big")


def _u32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 4], "big")


def _bcd(b: int) -> int:
    return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)


def _bcd_time(buf: bytes, off: int) -> float:
    """DVD-Zeitstempel: hh mm ss ff als BCD, oberste zwei Bits von ff = Framerate."""
    if off + 4 > len(buf):
        return 0.0
    hh, mm, ss, fb = buf[off], buf[off + 1], buf[off + 2], buf[off + 3]
    fps = 25.0 if (fb >> 6) == 1 else 29.97
    frames = _bcd(fb & 0x3F)
    return _bcd(hh) * 3600 + _bcd(mm) * 60 + _bcd(ss) + frames / fps


def is_dvd_title(ref) -> bool:
    return bool(_TITLE_RE.match(str(ref or "")))


def title_number(ref) -> int:
    m = _TITLE_RE.match(str(ref or ""))
    return int(m.group(1)) if m else 0


def _find_file(folder: Path, name: str) -> Optional[Path]:
    for cand in (folder / name, folder / name.lower()):
        if cand.is_file():
            return cand
    return None


def disc_root(folder: Path) -> Optional[Path]:
    """Ordner, der VIDEO_TS enthält. Sammelordner mit vielen DVDs zählen nicht."""
    start = folder if folder.is_dir() else folder.parent
    for cand in (start, start.parent if start.name.upper() == "VIDEO_TS" else None):
        if cand is None:
            continue
        vts = None
        for name in ("VIDEO_TS", "video_ts"):
            if (cand / name).is_dir():
                vts = cand / name
                break
        if vts is not None and _find_file(vts, "VIDEO_TS.IFO") is not None:
            return cand
    return None


def _video_ts(root: Path) -> Path:
    for name in ("VIDEO_TS", "video_ts"):
        if (root / name).is_dir():
            return root / name
    return root / "VIDEO_TS"


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _title_table(vmg: bytes) -> list[dict]:
    """TT_SRPT: je Titel VTS-Nummer, Titel in diesem VTS, Kapitelzahl."""
    if len(vmg) < 0xC8 or vmg[:12] != b"DVDVIDEO-VMG":
        return []
    sector = _u32(vmg, 0xC4)
    base = sector * _SECTOR
    if base <= 0 or base + 8 > len(vmg):
        return []
    count = _u16(vmg, base)
    out = []
    off = base + 8
    for i in range(count):
        if off + 12 > len(vmg):
            break
        out.append({
            "title": i + 1,
            "angles": vmg[off + 1],
            "ptts": _u16(vmg, off + 2),
            "vts": vmg[off + 6],
            "vts_ttn": vmg[off + 7],
        })
        off += 12
    return out


def _pgc_for_title(vts: bytes, ttn: int) -> Optional[int]:
    """VTS_PTT_SRPT: erster Eintrag des Titels → PGC-Nummer (1-basiert)."""
    if len(vts) < 0xD0 or vts[:12] != b"DVDVIDEO-VTS":
        return None
    base = _u32(vts, 0xC8) * _SECTOR
    if base <= 0 or base + 8 > len(vts):
        return None
    count = _u16(vts, base)
    if ttn < 1 or ttn > count:
        return None
    off = _u32(vts, base + 8 + (ttn - 1) * 4)
    pos = base + off
    if pos + 2 > len(vts):
        return None
    return _u16(vts, pos)


def _pgc_info(vts: bytes, pgcn: int) -> Optional[dict]:
    """Spieldauer und Kapitelanfänge einer PGC."""
    base = _u32(vts, 0xCC) * _SECTOR
    if base <= 0 or base + 8 > len(vts):
        return None
    count = _u16(vts, base)
    if pgcn < 1 or pgcn > count:
        return None
    entry = base + 8 + (pgcn - 1) * 8
    pgc = base + _u32(vts, entry + 4)
    if pgc + 0xEC > len(vts):
        return None
    n_programs = vts[pgc + 2]
    n_cells = vts[pgc + 3]
    duration = _bcd_time(vts, pgc + 4)
    map_off = _u16(vts, pgc + 0xE6)
    cell_off = _u16(vts, pgc + 0xE8)
    cell_times: list[float] = []
    if cell_off:
        for c in range(n_cells):
            pos = pgc + cell_off + c * 24
            if pos + 24 > len(vts):
                break
            cell_times.append(_bcd_time(vts, pos + 4))
    starts: list[float] = []
    if map_off:
        for p in range(n_programs):
            pos = pgc + map_off + p
            if pos >= len(vts):
                break
            entry_cell = vts[pos]
            starts.append(sum(cell_times[:max(0, entry_cell - 1)]))
    if not duration and cell_times:
        duration = sum(cell_times)
    chapters = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else duration
        if end <= start:
            continue
        chapters.append({
            "start": round(start, 3), "end": round(end, 3),
            "title": f"Kapitel {i + 1}",
        })
    if len(chapters) < 2:
        chapters = []
    return {"duration": duration, "chapters": chapters}


def _vts_size(video_ts: Path, vts_nr: int) -> int:
    total = 0
    try:
        for f in video_ts.iterdir():
            name = f.name.upper()
            if name.startswith(f"VTS_{vts_nr:02d}_") and name.endswith(".VOB") \
                    and not name.endswith("_0.VOB"):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _scan_root(root: Path) -> list[dict]:
    video_ts = _video_ts(root)
    vmg_path = _find_file(video_ts, "VIDEO_TS.IFO")
    if vmg_path is None:
        return []
    titles = _title_table(_read(vmg_path))
    found: list[dict] = []
    vts_cache: dict[int, bytes] = {}
    for t in titles:
        vts_nr = t["vts"]
        if vts_nr not in vts_cache:
            p = _find_file(video_ts, f"VTS_{vts_nr:02d}_0.IFO")
            vts_cache[vts_nr] = _read(p) if p else b""
        vts = vts_cache[vts_nr]
        if not vts:
            continue
        pgcn = _pgc_for_title(vts, t["vts_ttn"])
        if not pgcn:
            continue
        info = _pgc_info(vts, pgcn)
        if not info or info["duration"] < _MIN_SEC:
            continue
        found.append({
            "title": t["title"],
            "duration": info["duration"],
            "chapters": info["chapters"],
            "size": _vts_size(video_ts, vts_nr),
            "angles": t["angles"],
        })
    if not found:
        return []
    found.sort(key=lambda x: (x["duration"], len(x["chapters"])), reverse=True)
    best = found[0]["duration"]
    for x in found:
        x["role"] = "main" if x["duration"] >= best - 1 else "extra"
    mains = [x for x in found if x["role"] == "main"]
    if len(mains) > 1:
        winner = max(mains, key=lambda x: (len(x["chapters"]), x["duration"]))
        for x in mains:
            if x is not winner:
                x["role"] = "extra"
    return found


def describe(folder: Path) -> Optional[dict]:
    """Titel einer DVD für den Browser. None, wenn hier kein VIDEO_TS liegt."""
    from . import config
    from .ffmpeg_utils import human_duration, human_size

    root = disc_root(folder)
    if root is None:
        return None
    raw = _scan_root(root)
    if not raw:
        return None
    root_rel = config.rel_input(root) or ""
    titles = []
    for t in raw:
        titles.append({
            "playlist": f"Titel {t['title']}",
            "role": t["role"],
            "duration": round(t["duration"], 3),
            "duration_human": human_duration(t["duration"]),
            "chapters": t["chapters"],
            "chapter_count": len(t["chapters"]),
            "clips": [f"dvd:{t['title']}"],
            "dvd_title": t["title"],
            "size": t["size"],
            "size_human": human_size(t["size"]),
            "source": root_rel,
        })
    return {"root": root_rel, "kind": "dvd", "titles": titles}


def demuxer_available() -> bool:
    """FFmpeg mit dvdvideo-Demuxer gebaut?"""
    import subprocess
    from . import config
    try:
        proc = subprocess.run([config.FFMPEG, "-hide_banner", "-h", "demuxer=dvdvideo"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "dvdvideo" in (proc.stdout or "").lower()


def input_args(title: int) -> list[str]:
    """Demuxer-Optionen vor ``-i`` für einen DVD-Titel."""
    return ["-f", "dvdvideo", "-title", str(int(title))]


def probe_title(source: Path, ref) -> tuple:
    """ffprobe eines DVD-Titels aus Ordner oder ISO. Liefert (VideoInfo, Fehler)."""
    from .ffmpeg_utils import probe_with_error

    n = title_number(ref) if isinstance(ref, str) else int(ref or 0)
    if n < 1:
        return None, "Ungültiger DVD-Titel"
    if not demuxer_available():
        return None, ("FFmpeg ohne dvdvideo-Demuxer. DVD-Quellen brauchen einen "
                      "GPL-Build mit libdvdread/libdvdnav.")
    size = None
    source = Path(source)
    if source.is_dir():
        size = _vts_size_for_title(source, n)
    info, err = probe_with_error(source, input_args=input_args(n), size_bytes=size)
    if info is None:
        low = (err or "").lower()
        if "encrypt" in low or "css" in low or "scrambl" in low:
            err = "Die DVD ist CSS-verschlüsselt. Verschlüsselte Discs werden nicht geöffnet."
        return None, err
    return info, None


def _vts_size_for_title(root: Path, title: int) -> int:
    video_ts = _video_ts(root)
    vmg_path = _find_file(video_ts, "VIDEO_TS.IFO")
    if vmg_path is None:
        return 0
    for t in _title_table(_read(vmg_path)):
        if t["title"] == title:
            return _vts_size(video_ts, t["vts"])
    return 0
