"""Encoder-Speed-Bank: Referenzclips laden und Speed × Qualität per VMAF vergleichen."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff
from .ffmpeg_utils import (
    ENCODER_SPEEDS, alias_to_native, native_speed_presets, nearest_alias,
    normalize_encoder_speed, speed_label,
)

logger = logging.getLogger("vcompress.encoder_bench")

BENCH_DIR = config.DATA_DIR / "encoder_bench"
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
# Browser-UA: manche Spiegel (früher GCS) blocken sonst mit 403.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
_HTTP_HDR = {"User-Agent": _UA, "Accept": "*/*"}
_probe_cache: dict[str, tuple[float, int, dict]] = {}

# Kurzclips unterschiedlicher Bildtypen. Nur diese HTTPS-URLs werden geladen
# (Allowlist, kein freies URL-Feld → kein SSRF). Fallbacks, falls ein Spiegel tot ist.
CLIPS: list[dict] = [
    {
        "id": "anim",
        "title": "Animation (Flächen, Banding)",
        "kind": "animation",
        "license": "CC-BY · Blender Foundation · Big Buck Bunny",
        "filename": "bbb_1080_10s.mp4",
        "approx_mb": 30,
        "why": "Flächen und harte Kanten – zeigt, ob schnelle Presets banden.",
        "urls": [
            "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/1080/Big_Buck_Bunny_1080_10s_30MB.mp4",
            "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4",
        ],
    },
    {
        "id": "cgi",
        "title": "Film-CGI (Detail, Dunkel)",
        "kind": "cgi",
        "license": "CC-BY · Blender Foundation · Sintel",
        "filename": "sintel_trailer_1080p.mp4",
        "approx_mb": 50,
        "why": "Weiche Gradienten und dunkle Szenen – typisch für Spielfilm.",
        "urls": [
            "https://download.blender.org/durian/trailer/sintel_trailer-1080p.mp4",
            "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
        ],
    },
    {
        "id": "live",
        "title": "Live-Action / VFX",
        "kind": "live",
        "license": "CC-BY · Blender Foundation · Tears of Steel (Fallback: Intel CC-BY)",
        "filename": "tos_live_20s.mp4",
        "approx_mb": 40,
        "why": "Echte Kamera plus Effekte – Korn und hohe Komplexität.",
        "urls": [
            {
                "url": "https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov",
                "extract": {"start": 90, "seconds": 18},
            },
            "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/people-detection.mp4",
        ],
    },
    {
        "id": "motion",
        "title": "Viel Bewegung",
        "kind": "motion",
        "license": "CC-BY 4.0 · Intel sample-videos · car-detection",
        "filename": "car_detection.mp4",
        "approx_mb": 3,
        "why": "Schnelle Kamerafahrten – Speed-Presets sparen hier oft falsch.",
        "urls": [
            "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/car-detection.mp4",
            "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/people-detection.mp4",
        ],
    },
    {
        "id": "camera",
        "title": "Handkamera / Straße",
        "kind": "camera",
        "license": "CC-BY 4.0 · Intel sample-videos · walking",
        "filename": "people_walking.mp4",
        "approx_mb": 6,
        "why": "Rauschen, Detail im Hintergrund – näher an Serien-Remuxes als CGI.",
        "urls": [
            "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/face-demographics-walking.mp4",
            "https://upload.wikimedia.org/wikipedia/commons/transcoded/8/87/Schlossbergbahn.webm/Schlossbergbahn.webm.720p.vp9.webm",
        ],
    },
]

_CLIP_BY_ID = {c["id"]: c for c in CLIPS}

_lock = threading.RLock()
_cancel = threading.Event()
_thread: Optional[threading.Thread] = None
_state: dict = {
    "running": False,
    "phase": "idle",
    "message": "",
    "percent": 0,
    "rows": [],
    "recommendation": None,
    "error": "",
}


def _clip_path(clip: dict) -> Path:
    return BENCH_DIR / clip["filename"]


def _pretty_codec(name: str) -> str:
    n = (name or "").lower()
    return {
        "h264": "H.264", "avc": "H.264", "avc1": "H.264",
        "hevc": "HEVC", "h265": "HEVC", "hev1": "HEVC", "hvc1": "HEVC",
        "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
        "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2", "prores": "ProRes",
    }.get(n, (name or "?").upper())


def _probe_clip(path: Path) -> dict:
    """Codec, Auflösung, fps, Bitrate – gecacht nach mtime/Größe."""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    hit = _probe_cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return dict(hit[2])
    info, _ = ff.probe_with_error(path)
    data: dict = {}
    if info:
        br = ff._bitrate_human(info.video_bitrate) if info.video_bitrate else ""
        fps = f"{info.fps:.0f} fps" if info.fps else ""
        res = f"{info.width}×{info.height}" if info.width else ""
        dur = ff.human_duration(info.duration) if info.duration else ""
        parts = [p for p in (_pretty_codec(info.codec), res, fps, br, dur) if p]
        data = {
            "codec": info.codec,
            "width": info.width,
            "height": info.height,
            "fps": round(info.fps, 2) if info.fps else 0,
            "duration_human": dur,
            "video_bitrate_human": br,
            "media": " · ".join(parts),
        }
    _probe_cache[key] = (st.st_mtime, st.st_size, data)
    return dict(data)


def _local_info(clip: dict) -> dict:
    p = _clip_path(clip)
    ok = p.is_file() and p.stat().st_size > 50_000
    out = {
        "present": ok,
        "bytes": p.stat().st_size if ok else 0,
        "human": ff.human_size(p.stat().st_size) if ok else "",
        "path": str(p) if ok else "",
        "media": "",
    }
    if ok:
        out.update(_probe_clip(p))
    return out


def catalog() -> list[dict]:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for c in CLIPS:
        d = {k: c[k] for k in ("id", "title", "kind", "license", "filename",
                               "approx_mb", "why")}
        d.update(_local_info(c))
        out.append(d)
    return out


def snapshot() -> dict:
    with _lock:
        last = _load_last()
        return {
            **dict(_state),
            "clips": catalog(),
            "last": last,
            "speed_presets": ff.speed_preset_catalog(),
        }


def _load_last() -> Optional[dict]:
    p = BENCH_DIR / "last.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_last(payload: dict) -> None:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    (BENCH_DIR / "last.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _set(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _sources(clip: dict):
    """(url, extract|None) – extract = {start, seconds} für ffmpeg-Ausschnitt."""
    for item in clip.get("urls") or []:
        if isinstance(item, str):
            yield item, None
        elif isinstance(item, dict) and item.get("url"):
            yield str(item["url"]), item.get("extract")


def _http_err(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason or 'Fehler'}"
    return str(exc) or type(exc).__name__


def _ffmpeg_extract(url: str, dest: Path, extract: dict) -> str:
    """Kurzen Ausschnitt per HTTP-Range holen (ganze ToS-Datei wäre ~350 MB)."""
    start = float((extract or {}).get("start") or 0)
    seconds = float((extract or {}).get("seconds") or 15)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    base = [
        config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-user_agent", _UA,
        "-ss", f"{start:.3f}",
        "-t", f"{seconds:.3f}",
        "-i", url,
    ]
    variants = [
        ["-c", "copy", "-an", "-movflags", "+faststart"],
        ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-an",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    ]
    last = "ffmpeg-Ausschnitt fehlgeschlagen"
    for extra in variants:
        if _cancel.is_set():
            return "Abgebrochen"
        try:
            proc = subprocess.run(base + extra + [str(tmp)],
                                  capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            last = str(e)
            try:
                tmp.unlink()
            except OSError:
                pass
            continue
        err = (proc.stderr or "").strip()
        if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 50_000:
            tmp.replace(dest)
            return ""
        last = err or f"ffmpeg-Ausschnitt fehlgeschlagen ({proc.returncode})"
        try:
            tmp.unlink()
        except OSError:
            pass
    return last


def _download_one(clip: dict, on_bytes) -> str:
    """Lädt den Clip; leerer String = ok, sonst Fehlertext."""
    dest = _clip_path(clip)
    if dest.is_file() and dest.stat().st_size > 50_000:
        return ""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = "kein Spiegel erreichbar"
    for url, extract in _sources(clip):
        if _cancel.is_set():
            return "Abgebrochen"
        if extract:
            last_err = _ffmpeg_extract(url, dest, extract)
            if not last_err and dest.is_file() and dest.stat().st_size > 50_000:
                return ""
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            req = urllib.request.Request(url, headers=_HTTP_HDR)
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                if total > MAX_DOWNLOAD_BYTES:
                    last_err = f"Datei zu groß ({ff.human_size(total)})"
                    continue
                got = 0
                too_big = False
                with tmp.open("wb") as f:
                    while True:
                        if _cancel.is_set():
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                            return "Abgebrochen"
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > MAX_DOWNLOAD_BYTES:
                            too_big = True
                            last_err = "Download über Limit (400 MB)"
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                            break
                        f.write(chunk)
                        if on_bytes:
                            on_bytes(clip["id"], got, total)
                if too_big:
                    continue
                if got < 50_000:
                    last_err = f"Zu wenig Daten von {url.split('/')[2]}"
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    continue
                tmp.replace(dest)
                return ""
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            last_err = _http_err(e)
            try:
                tmp.unlink()
            except OSError:
                pass
            continue
    return last_err


def download(ids: list[str]) -> None:
    """Synchroner Download (im Thread von start_download aufrufen)."""
    wanted = [c for c in CLIPS if c["id"] in set(ids or [])]
    if not wanted:
        _set(running=False, phase="idle", message="Nichts zu laden.", percent=0)
        return
    n = len(wanted)
    for i, clip in enumerate(wanted):
        if _cancel.is_set():
            _set(running=False, phase="idle", message="Abgebrochen.", percent=0)
            return
        _set(phase="download",
             message=f"Lade {clip['title']} ({i + 1}/{n}) …",
             percent=round(i / n * 100, 1))

        def on_bytes(_cid, got, total, clip=clip, i=i, n=n):
            frac = (got / total) if total else 0
            _set(percent=round((i + frac) / n * 100, 1),
                 message=f"Lade {clip['title']}: {ff.human_size(got)}"
                         + (f" / {ff.human_size(total)}" if total else ""))

        err = _download_one(clip, on_bytes)
        if err:
            _set(running=False, phase="error", error=err,
                 message=f"{clip['title']}: {err}", percent=0)
            return
    _set(running=False, phase="idle", message="Download fertig.", percent=100, error="")


def start_download(ids: list[str]) -> Optional[str]:
    wanted = [c["id"] for c in CLIPS if c["id"] in set(ids or [])]
    if not wanted:
        return "Keine bekannten Clips gewählt."
    with _lock:
        if _state.get("running"):
            return "Es läuft bereits ein Test oder Download."
        _state.update(running=True, phase="download", message="Download …",
                      percent=0, error="")
        _cancel.clear()
    t = threading.Thread(target=download, args=(wanted,), daemon=True)
    t.start()
    return None


def cancel() -> None:
    _cancel.set()


def clear_results() -> Optional[str]:
    """Letzten Test aus Speicher und UI entfernen (nicht während eines Laufs)."""
    with _lock:
        if _state.get("running"):
            return "Es läuft bereits ein Test oder Download."
        _state.update(rows=[], recommendation=None, phase="idle",
                      message="", percent=0, error="")
    p = BENCH_DIR / "last.json"
    try:
        p.unlink(missing_ok=True)
    except OSError as e:
        return str(e)
    return None


def start_bench(cfg: dict) -> Optional[str]:
    with _lock:
        if _state.get("running"):
            return "Es läuft bereits ein Test oder Download."
        _state.update(running=True, phase="bench", message="Start …",
                      percent=0, rows=[], recommendation=None, error="")
        _cancel.clear()
        global _thread
        _thread = threading.Thread(target=_run_bench, args=(cfg,), daemon=True)
        _thread.start()
    return None


def _resolve_extras(paths: list[str]) -> list[dict]:
    out = []
    for i, rel in enumerate((paths or [])[:4]):
        p = config.resolve_input(str(rel or ""))
        if p is None or not p.is_file():
            continue
        out.append({
            "id": f"user{i + 1}",
            "title": p.name,
            "kind": "own",
            "path": p,
            "why": "Eigene Datei aus der Bibliothek",
        })
    return out


def _run_bench(cfg: dict) -> None:
    from . import vmaf as vmaf_mod

    try:
        _run_bench_inner(cfg, vmaf_mod)
    except Exception as e:
        logger.exception("Encoder-Bench fehlgeschlagen")
        _set(running=False, phase="error", error=str(e), message=str(e))


def _run_bench_inner(cfg: dict, vmaf_mod) -> None:
    platform = str(cfg.get("platform") or "cpu")
    codec = str(cfg.get("codec") or "av1")
    if not ff.encoder_available(platform, codec):
        _set(running=False, phase="error", error="Encoder nicht verfügbar",
             message=f"{platform}/{codec} ist auf diesem System nicht nutzbar.")
        return
    enc = ff.encoder_name(platform, codec)
    natives = native_speed_presets(enc)
    native_vals = [p["value"] for p in natives]
    allowed = set(native_vals) | set(ENCODER_SPEEDS)
    wanted: list[str] = []
    for raw in cfg.get("speeds") or []:
        s = normalize_encoder_speed(raw)
        if s not in allowed:
            continue
        n = alias_to_native(enc, s) if natives else s
        if n not in wanted:
            wanted.append(n)
    speeds = [v for v in native_vals if v in set(wanted)] if native_vals else wanted
    if not speeds:
        speeds = [alias_to_native(enc, "balanced")] if natives else ["balanced"]
    rate_mode = str(cfg.get("rate_mode") or "cq")
    if rate_mode not in ("cq", "abr", "bitrate"):
        rate_mode = "cq"
    values = [int(v) for v in (cfg.get("values") or []) if int(v) > 0][:4]
    if not values:
        values = [24, 28, 32] if rate_mode == "cq" else [8000, 6000, 4000]
    clip_seconds = max(5, min(60, int(cfg.get("clip_seconds") or 12)))
    samples = max(1, min(5, int(cfg.get("samples") or 3)))
    anime = bool(cfg.get("anime"))

    jobs: list[dict] = []
    for cid in cfg.get("clip_ids") or []:
        spec = _CLIP_BY_ID.get(cid)
        if not spec:
            continue
        loc = _local_info(spec)
        if not loc["present"]:
            _set(phase="download", message=f"Lade {spec['title']} …")
            err = _download_one(spec, None)
            if err:
                if err == "Abgebrochen" or _cancel.is_set():
                    _set(running=False, phase="idle", message="Abgebrochen.",
                         percent=0, error="")
                    return
                _set(running=False, phase="error", error=err,
                     message=f"{spec['title']}: {err}", percent=0)
                return
            loc = _local_info(spec)
        if not loc["present"]:
            _set(running=False, phase="error",
                 error=f"Clip fehlt: {spec['title']} – zuerst herunterladen.",
                 message=f"{spec['title']} ist noch nicht geladen.")
            return
        jobs.append({
            "id": spec["id"], "title": spec["title"], "kind": spec["kind"],
            "path": _clip_path(spec),
        })
    jobs.extend(_resolve_extras(cfg.get("extra_paths") or []))
    if not jobs:
        _set(running=False, phase="error", error="Keine Clips gewählt.",
             message="Mindestens einen Clip laden oder eine eigene Datei wählen.")
        return

    _set(phase="bench", message="Start …", percent=0)

    total = max(1, len(jobs) * len(speeds))
    done = 0
    rows: list[dict] = []

    for job in jobs:
        if _cancel.is_set():
            _set(running=False, phase="idle", message="Abgebrochen.", percent=0)
            return
        info = ff.ffprobe(job["path"])
        if info is None:
            rows.append({
                "clip_id": job["id"], "clip": job["title"], "kind": job["kind"],
                "error": "ffprobe fehlgeschlagen",
            })
            done += len(speeds)
            continue
        for speed in speeds:
            if _cancel.is_set():
                _set(running=False, phase="idle", message="Abgebrochen.", percent=0)
                return
            done += 1
            _set(percent=round((done - 1) / total * 100, 1),
                 message=f"{job['title']} · {speed} ({done}/{total}) …")
            opts = vmaf_mod.VmafOptions(
                rate_mode=rate_mode,
                test_values=list(values),
                clip_seconds=clip_seconds,
                samples=samples,
                generate_screenshots=False,
                session_name=f"bench_{job['id']}_{speed}",
                source_title=job["title"],
                source_path=str(job["path"]),
                anime=anime,
            )
            t0 = time.time()
            analysis = vmaf_mod.analyze(
                info, platform, codec, None, False,
                opts=opts,
                encoder_speed=speed,
                cancelled=lambda: _cancel.is_set(),
            )
            elapsed = round(time.time() - t0, 1)
            if _cancel.is_set():
                _set(running=False, phase="idle", message="Abgebrochen.", percent=0)
                return
            if analysis.error and not analysis.results:
                rows.append({
                    "clip_id": job["id"], "clip": job["title"], "kind": job["kind"],
                    "speed": speed, "error": analysis.error, "seconds": elapsed,
                })
                continue
            nres = max(1, len(analysis.results))
            per = round(elapsed / nres, 2)
            src_human = ff.human_size(info.size_bytes) if info.size_bytes else ""
            for r in analysis.results:
                rows.append({
                    "clip_id": job["id"],
                    "clip": job["title"],
                    "kind": job["kind"],
                    "speed": speed,
                    "rate_mode": r.rate_mode,
                    "value": r.value,
                    "vmaf": round(r.vmaf, 2),
                    "vmaf_1pct": round(r.vmaf_1pct, 2) if r.vmaf_1pct else None,
                    "vmaf_hmean": round(r.vmaf_hmean, 2) if r.vmaf_hmean else None,
                    "size_bytes": r.clip_size_bytes,
                    "size_human": ff.human_size(r.clip_size_bytes),
                    "predicted_bytes": r.predicted_size_bytes,
                    "predicted_human": ff.human_size(r.predicted_size_bytes),
                    "savings_percent": round(r.savings_percent, 1),
                    "source_human": src_human,
                    "seconds": per,
                    "seconds_pack": elapsed,
                    "platform": platform,
                    "codec": codec,
                })
            _set(rows=list(rows), percent=round(done / total * 100, 1))

    rec = _recommend(rows, speeds, values[0] if values else 28, enc)
    payload = {
        "rows": rows,
        "recommendation": rec,
        "platform": platform,
        "codec": codec,
        "encoder": enc,
        "rate_mode": rate_mode,
        "values": values,
        "speeds": speeds,
        "clip_seconds": clip_seconds,
        "samples": samples,
        "finished_at": time.time(),
    }
    try:
        _save_last(payload)
    except OSError as e:
        logger.warning("Bench-Ergebnis nicht gespeichert: %s", e)
    _set(running=False, phase="done", percent=100, rows=rows,
         recommendation=rec, message="Test fertig.", error="")


def _quality(st: dict) -> float:
    from .vmaf import quality_score
    return quality_score(st.get("vmaf") or 0, st.get("p1") or 0, st.get("hmean") or 0)


def _recommend(rows: list[dict], speeds: list[str], ref_value: int, enc: str) -> dict:
    """Nächstlangsamere Stufe nur, wenn Score (Mittel+1%-Low) sich lohnt."""
    usable = [r for r in rows if r.get("vmaf") and r.get("speed") and not r.get("error")]
    fallback = alias_to_native(enc, "balanced") if enc else "balanced"
    if not usable:
        return {
            "speed": fallback,
            "alias": nearest_alias(enc, fallback),
            "reason": "Keine VMAF-Werte – Standard bleibt Ausgewogen.",
        }
    at_ref = [r for r in usable if r.get("value") == ref_value] or usable
    stats: dict[str, dict] = {}
    for s in speeds:
        chunk = [r for r in at_ref if r["speed"] == s]
        if not chunk:
            continue
        p1s = [r["vmaf_1pct"] for r in chunk if r.get("vmaf_1pct")]
        hms = [r["vmaf_hmean"] for r in chunk if r.get("vmaf_hmean")]
        st = {
            "vmaf": sum(r["vmaf"] for r in chunk) / len(chunk),
            "p1": (sum(p1s) / len(p1s)) if p1s else None,
            "hmean": (sum(hms) / len(hms)) if hms else None,
            "seconds": sum(float(r.get("seconds") or 0) for r in chunk) / len(chunk),
            "size": sum(int(r.get("size_bytes") or 0) for r in chunk) / len(chunk),
        }
        st["score"] = _quality(st)
        stats[s] = st
    order = [s for s in speeds if s in stats]
    if not order:
        return {
            "speed": fallback,
            "alias": nearest_alias(enc, fallback),
            "reason": "Keine vergleichbaren Stufen.",
        }
    chosen = order[0]
    notes = []
    from . import app_settings
    gap = app_settings.vmaf_p1_gap()
    for nxt in order[1:]:
        a, b = stats[chosen], stats[nxt]
        dq = b["score"] - a["score"]
        dmean = b["vmaf"] - a["vmaf"]
        p1a = a["p1"] if a["p1"] is not None else a["vmaf"]
        p1b = b["p1"] if b["p1"] is not None else b["vmaf"]
        dp1 = p1b - p1a
        t0 = max(0.05, a["seconds"])
        tr = b["seconds"] / t0
        sz = b["size"] / max(1.0, a["size"])
        floor_bad = bool(gap > 0 and p1a < (a["vmaf"] - gap))
        lbl_a = speed_label(enc, chosen)
        lbl_b = speed_label(enc, nxt)
        detail = (f"{lbl_b} gegen {lbl_a}: Score {b['score']:.1f} vs {a['score']:.1f} "
                  f"(Mittel {dmean:+.1f}, 1%-Low {dp1:+.1f}, Zeit ×{tr:.1f}, Größe ×{sz:.2f})")
        # Score-Gewinn klar und Zeit im Rahmen – oder 1%-Low zieht spürbar an.
        if dq >= 0.4 and (tr < 3.5 or dq >= 1.0 or dp1 >= 1.5):
            notes.append(detail + ".")
            chosen = nxt
        elif floor_bad and dp1 >= 1.0 and tr < 5.0:
            notes.append(detail + " – 1%-Low war zu niedrig, langsamere Stufe hebt die schlechtesten Frames.")
            chosen = nxt
        else:
            notes.append(detail + " – lohnt kaum.")
            break
    reason = f"Empfehlung: {speed_label(enc, chosen)}."
    ch = stats.get(chosen) or {}
    if ch:
        p1txt = f"{ch['p1']:.1f}" if ch.get("p1") is not None else "—"
        reason += (f" Score {ch['score']:.1f} (Mittel {ch['vmaf']:.1f}, 1%-Low {p1txt})"
                   " – Mittel allein würde Ausreißer (z. B. 95 bei 1%-Low 79) schönrechnen.")
    if notes:
        reason += " " + " ".join(notes)
    reason += " Gemittelt über die getesteten Clips, gemessen am ersten Qualitätswert."
    return {
        "speed": chosen,
        "alias": nearest_alias(enc, chosen),
        "reason": reason,
        "stats": {
            s: {
                "vmaf": round(v["vmaf"], 2),
                "p1": round(v["p1"], 2) if v.get("p1") is not None else None,
                "score": round(v["score"], 2),
                "seconds": round(v["seconds"], 1),
            }
            for s, v in stats.items()
        },
    }
