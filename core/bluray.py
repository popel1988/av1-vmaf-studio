"""Blu-ray-Ordner und unverschlüsselte ISO-Abbilder.

Eine Disc liegt als Ordner vor (BDMV/PLAYLIST/*.mpls und BDMV/STREAM/*.m2ts)
oder als ISO. Die Playlist sagt, welche M2TS in welcher Reihenfolge den Film
ergeben und wo die Kapitel sitzen. Die längste Playlist ist der Hauptfilm.
Ein Abbild wird nur lesend eingehängt (UDF, sonst ISO9660). Verschlüsselte
Abbilder (AACS) werden nicht geöffnet und nicht entschlüsselt.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

_TICK = 45000  # Blu-ray-Zeitbasis
_MIN_SEC = 15.0


def _u16(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 2], "big")


def _u32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 4], "big")


_DISC_PARTS = {
    "BDMV", "PLAYLIST", "STREAM", "CLIPINF", "BACKUP", "CERTIFICATE", "JAR", "AUXDATA",
}


def disc_root(folder: Path) -> Optional[Path]:
    """Ordner, dessen BDMV direkt hier liegt.

    Ein Sammelordner mit vielen Filmen darunter zählt nicht. Nach oben wird
    nur gegangen, solange man schon in der Disc steht (BDMV, STREAM, …).
    """
    start = folder if folder.is_dir() else folder.parent
    if (start / "BDMV" / "PLAYLIST").is_dir():
        return start
    cur = start
    for _ in range(6):
        name = cur.name.upper()
        if name == "BDMV" and (cur / "PLAYLIST").is_dir():
            return cur.parent
        if name not in _DISC_PARTS:
            return None
        parent = cur.parent
        if parent == cur:
            return None
        if (parent / "BDMV" / "PLAYLIST").is_dir():
            return parent
        cur = parent
    return None


def _parse_mpls(data: bytes) -> Optional[dict]:
    if len(data) < 16 or data[:4] != b"MPLS":
        return None
    list_pos = _u32(data, 8)
    mark_pos = _u32(data, 12)
    if list_pos + 10 > len(data):
        return None
    n_items = _u16(data, list_pos + 6)
    off = list_pos + 10
    clips: list[dict] = []
    for _ in range(n_items):
        if off + 22 > len(data):
            break
        length = _u16(data, off)
        payload = off + 2
        end = payload + length
        if length < 20 or end > len(data):
            break
        clip_id = data[payload:payload + 5].decode("ascii", "replace").strip()
        codec = data[payload + 5:payload + 9]
        inn = _u32(data, payload + 12)
        out = _u32(data, payload + 16)
        if codec == b"M2TS" and clip_id and out > inn:
            clips.append({"id": clip_id, "inn": inn, "out": out})
        off = end
    if not clips:
        return None
    return {"clips": clips, "chapters": _chapters(data, mark_pos, clips)}


def _chapters(data: bytes, mark_pos: int, clips: list[dict]) -> list[dict]:
    if mark_pos <= 0 or mark_pos + 6 > len(data):
        return []
    count = _u16(data, mark_pos + 4)
    spans = []
    acc = 0
    for clip in clips:
        dur = clip["out"] - clip["inn"]
        spans.append((acc, clip["inn"], dur))
        acc += dur
    starts: list[float] = []
    off = mark_pos + 6
    for _ in range(count):
        if off + 14 > len(data):
            break
        mark_type = data[off + 1]
        ref = _u16(data, off + 2)
        tick = _u32(data, off + 4)
        off += 14
        if mark_type != 1 or ref >= len(spans):
            continue
        base, inn, dur = spans[ref]
        rel = min(max(0, tick - inn), dur)
        starts.append((base + rel) / _TICK)
    if len(starts) < 2:
        return []
    starts.sort()
    total = acc / _TICK
    out = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else total
        if end <= start:
            continue
        out.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "title": f"Kapitel {i + 1}",
        })
    return out


def _scan_root(root: Path) -> list[dict]:
    playlist_dir = root / "BDMV" / "PLAYLIST"
    stream_dir = root / "BDMV" / "STREAM"
    if not playlist_dir.is_dir():
        return []
    found: list[dict] = []
    seen: set[tuple] = set()
    try:
        files = sorted(playlist_dir.glob("*.mpls")) + sorted(playlist_dir.glob("*.MPLS"))
    except OSError:
        return []
    for mpls in files:
        try:
            parsed = _parse_mpls(mpls.read_bytes())
        except OSError:
            continue
        if not parsed:
            continue
        paths: list[Path] = []
        missing = False
        for clip in parsed["clips"]:
            hit = None
            for name in (f"{clip['id']}.m2ts", f"{clip['id']}.M2TS"):
                candidate = stream_dir / name
                if candidate.is_file():
                    hit = candidate
                    break
            if hit is None:
                missing = True
                break
            paths.append(hit)
        if missing or not paths:
            continue
        duration = sum((c["out"] - c["inn"]) / _TICK for c in parsed["clips"])
        if duration < _MIN_SEC:
            continue
        key = tuple(p.name.lower() for p in paths)
        # Dieselbe Clipfolge nur einmal, die Variante mit mehr Kapiteln behalten.
        if key in seen:
            for prev in found:
                if tuple(Path(x).name.lower() for x in prev["paths"]) == key:
                    if len(parsed["chapters"]) > len(prev["chapters"]):
                        prev["chapters"] = parsed["chapters"]
                        prev["playlist"] = mpls.stem
                    break
            continue
        seen.add(key)
        size = 0
        for p in paths:
            try:
                size += p.stat().st_size
            except OSError:
                pass
        found.append({
            "playlist": mpls.stem,
            "duration": duration,
            "chapters": parsed["chapters"],
            "paths": paths,
            "size": size,
        })
    if not found:
        return []
    found.sort(key=lambda t: (t["duration"], len(t["chapters"])), reverse=True)
    best = found[0]["duration"]
    for title in found:
        title["role"] = "main" if title["duration"] >= best - 1 else "extra"
    # Liegen zwei fast gleich lange Playlists vorn, behält die mit mehr
    # Kapiteln den Hauptfilm.
    mains = [t for t in found if t["role"] == "main"]
    if len(mains) > 1:
        winner = max(mains, key=lambda t: (len(t["chapters"]), t["duration"]))
        for title in mains:
            if title is not winner:
                title["role"] = "extra"
    return found


def describe(folder: Path, *, media_rels: bool = True) -> Optional[dict]:
    """Titel einer Disc für den Browser. None, wenn hier keine BDMV-Struktur liegt.

    media_rels: Clip-Pfade relativ zum Medienbaum. Bei einem Mountpunkt außerhalb
    des Baums (ISO) stehen die Pfade relativ zur Disc (BDMV/STREAM/…).
    """
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
    for title in raw:
        clips = []
        for path in title["paths"]:
            if media_rels:
                rel = config.rel_input(path)
            else:
                try:
                    rel = path.resolve().relative_to(root.resolve()).as_posix()
                except ValueError:
                    rel = None
            if rel:
                clips.append(rel)
        if len(clips) != len(title["paths"]):
            continue
        titles.append({
            "playlist": title["playlist"],
            "role": title["role"],
            "duration": round(title["duration"], 3),
            "duration_human": human_duration(title["duration"]),
            "chapters": title["chapters"],
            "chapter_count": len(title["chapters"]),
            "clips": clips,
            "size": title["size"],
            "size_human": human_size(title["size"]),
        })
    if not titles:
        return None
    return {"root": root_rel, "titles": titles}


def clip_path(mount: Path, rel: str) -> Path:
    """Datei im eingehängten Abbild. Lehnt absolute Pfade und .. ab."""
    root = mount.resolve()
    raw = Path(str(rel).replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts:
        raise RuntimeError("Ungültiger Pfad im Abbild")
    dest = (root / raw).resolve()
    try:
        dest.relative_to(root)
    except ValueError:
        raise RuntimeError("Ungültiger Pfad im Abbild") from None
    if not dest.is_file():
        raise RuntimeError(f"Playlist-Teil fehlt im Abbild: {rel}")
    return dest


def mount_iso(iso: Path) -> Path:
    """ISO nur lesend einhängen. Erst UDF, dann ISO9660. Kein Entschlüsseln."""
    iso = Path(iso).resolve()
    if not iso.is_file():
        raise RuntimeError("ISO nicht gefunden")
    dest = Path(tempfile.mkdtemp(prefix="vcompress-iso-"))
    errors: list[str] = []
    for fstype in ("udf", "iso9660"):
        try:
            proc = subprocess.run(
                ["mount", "-t", fstype, "-o", "loop,ro", str(iso), str(dest)],
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            try:
                dest.rmdir()
            except OSError:
                pass
            raise RuntimeError(
                "mount fehlt in dieser Umgebung. Ein unverschlüsseltes Abbild "
                "lässt sich im Container lesen, nicht auf einem Rechner ohne Mount."
            ) from None
        except subprocess.TimeoutExpired:
            errors.append(f"{fstype}: Zeitüberschreitung")
            continue
        if proc.returncode == 0:
            return dest
        errors.append(f"{fstype}: {(proc.stderr or proc.stdout or '').strip()}")
    try:
        dest.rmdir()
    except OSError:
        pass
    raise RuntimeError(
        "Abbild konnte nicht gelesen werden. Nur unverschlüsselte ISO-Dateien, "
        "und der Container braucht das Recht zum Mounten. " + " ".join(errors)
    )


def unmount_iso(dest: Optional[Path]) -> None:
    if dest is None:
        return
    for args in (["umount", str(dest)], ["umount", "-l", str(dest)]):
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
    try:
        Path(dest).rmdir()
    except OSError:
        pass


def inspect_image(iso: Path) -> dict:
    """Titel lesen und prüfen, dass der Hauptfilm als Video lesbar ist."""
    from .ffmpeg_utils import probe_with_error

    mount = mount_iso(iso)
    try:
        described = describe(mount, media_rels=False)
        if not described or not described.get("titles"):
            raise RuntimeError(
                "Keine Blu-ray-Struktur in diesem Abbild. "
                "Verschlüsselte ISOs werden nicht geöffnet."
            )
        main = next(
            (t for t in described["titles"] if t["role"] == "main"),
            described["titles"][0],
        )
        first = clip_path(mount, main["clips"][0])
        info, _err = probe_with_error(first)
        if info is None or not getattr(info, "codec", None):
            raise RuntimeError(
                "Das Abbild ist nicht als Video lesbar. "
                "Verschlüsselte ISOs werden nicht geöffnet."
            )
        return described
    finally:
        unmount_iso(mount)
