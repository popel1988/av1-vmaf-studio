"""Bitrate-Verlauf einer Quelle aus den Paketgrößen, ohne Dekodieren."""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

from . import config

logger = logging.getLogger("vcompress.bitrate")

BIN_SEC = 10
_ROLES = {
    1: ["peak"],
    2: ["peak", "typical"],
    3: ["peak", "typical", "quiet"],
    4: ["peak", "high", "typical", "quiet"],
    5: ["peak", "high", "typical", "low", "quiet"],
}


def _cache_path(path: Path) -> Path:
    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")).hexdigest()[:20]
    return config.WORK_DIR / "bitrate_cache" / f"{key}.json"


def _body_range(duration: float) -> tuple[float, float]:
    """Vorspann und Abspann aus der Szenenwahl lassen."""
    if duration < 180:
        return 0.0, duration
    head = min(90.0, duration * 0.04)
    tail = min(240.0, duration * 0.08)
    if head + tail + 30 >= duration:
        return 0.0, duration
    return head, duration - tail


def _scan_packets(path: Path, duration: float) -> list[dict]:
    cmd = [
        config.FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,size",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as e:
        raise RuntimeError(f"ffprobe: {e}") from e
    sizes: dict[int, int] = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            pts = float(parts[0])
            size = int(float(parts[1]))
        except ValueError:
            continue
        if pts < 0 or size <= 0:
            continue
        sizes[int(pts // BIN_SEC)] = sizes.get(int(pts // BIN_SEC), 0) + size
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError("ffprobe fehlgeschlagen")
    if not sizes:
        raise RuntimeError("Keine Videopakete")
    last = max(sizes) * BIN_SEC
    span = duration if duration > 0 else last + BIN_SEC
    n = max(1, int(span // BIN_SEC) + 1)
    bins = []
    for i in range(n):
        raw = sizes.get(i, 0)
        bins.append({
            "t": i * BIN_SEC,
            "kbps": round(raw * 8 / BIN_SEC / 1000, 1),
        })
    return bins


def _stats(bins: list[dict], duration: float) -> dict:
    head, tail = _body_range(duration)
    vals = [float(b["kbps"]) for b in bins if head <= b["t"] < tail]
    if not vals:
        vals = [float(b["kbps"]) for b in bins] or [0.0]
    ordered = sorted(vals)
    avg = sum(vals) / len(vals)
    peak = ordered[-1]
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "avg_kbps": round(avg, 1),
        "peak_kbps": round(peak, 1),
        "p95_kbps": round(p95, 1),
        "peak_ratio": round(peak / avg, 2) if avg > 0 else 0,
    }


def _window_score(bins: list[dict], start: float, length: float) -> float:
    covered = [float(b["kbps"]) for b in bins if start <= b["t"] < start + length]
    if not covered:
        return 0.0
    return sum(covered) / len(covered)


def _score_target(role: str, scores: list[float]) -> float:
    ordered = sorted(scores)
    n = len(ordered)
    if role == "peak":
        return ordered[-1]
    if role == "quiet":
        return ordered[0]
    if role == "high":
        return ordered[min(n - 1, int(n * 0.75))]
    if role == "low":
        return ordered[min(n - 1, int(n * 0.25))]
    return ordered[n // 2]


def pick_windows(bins: list[dict], duration: float, clip: float, count: int) -> list[dict]:
    """Schwere, typische und ruhige Ausschnitte, ohne Überlappung."""
    duration = float(duration or 0)
    clip = max(5.0, float(clip or 30))
    count = max(1, min(5, int(count or 1)))
    if duration <= 0 or not bins:
        return []
    clip = min(clip, duration)
    head, tail = _body_range(duration)
    starts = []
    t = head
    while t + clip <= tail + 0.05:
        starts.append(round(t, 3))
        t += BIN_SEC
    if not starts:
        starts = [0.0]
    scored = [(s, _window_score(bins, s, clip)) for s in starts]
    positive = [sc for _, sc in scored if sc > 0]
    med = sorted(positive)[len(positive) // 2] if positive else 0.0
    usable = [(s, sc) for s, sc in scored if med <= 0 or sc >= med * 0.2] or scored
    chosen: list[tuple[float, float, str]] = []
    pool = list(usable)
    for role in _ROLES[count]:
        if not pool:
            break
        target = _score_target(role, [sc for _, sc in pool])
        if role == "peak":
            start, score = max(pool, key=lambda item: item[1])
        elif role == "quiet":
            start, score = min(pool, key=lambda item: item[1])
        else:
            start, score = min(pool, key=lambda item: abs(item[1] - target))
        chosen.append((start, score, role))
        pool = [(s, sc) for s, sc in pool if abs(s - start) >= clip * 0.75]
    chosen.sort(key=lambda item: item[0])
    return [
        {
            "start": round(start, 3),
            "length": round(clip, 3),
            "role": role,
            "kbps": round(score, 1),
        }
        for start, score, role in chosen
    ]


def load_bins(path: Path, duration: float) -> tuple[list[dict], dict]:
    """Bins und Kennzahlen, beim zweiten Mal aus dem Cache."""
    cache = _cache_path(path)
    if cache.is_file():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            bins = blob.get("bins") or []
            if bins:
                return bins, _stats(bins, duration)
        except (OSError, ValueError):
            pass
    bins = _scan_packets(path, duration)
    stats = _stats(bins, duration if duration > 0 else (bins[-1]["t"] + BIN_SEC))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"bins": bins}, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("Bitrate-Cache nicht geschrieben: %s", e)
    return bins, stats


def profile(path: Path, duration: float, clip: float, samples: int) -> dict:
    bins, stats = load_bins(path, duration)
    span = duration if duration > 0 else (bins[-1]["t"] + BIN_SEC if bins else 0)
    return {
        "bin_sec": BIN_SEC,
        "duration": round(span, 3),
        "bins": bins,
        "windows": pick_windows(bins, span, clip, samples),
        **stats,
    }
