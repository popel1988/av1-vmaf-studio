"""AV1/VMAF Compression Studio – FastAPI-Anwendung.

Stellt das Dashboard (Web-UI), den Datei-/Ordner-Browser, die Queue-API sowie
einen WebSocket für Live-Hardware-Metriken und Encode-Fortschritt bereit.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from core import config
from core import ffmpeg_utils as ff
from core.data_browser import (
    DATA_ROOTS,
    browse as browse_data_zone,
    delete_all_in_root,
    delete_item,
    storage_summary,
)
from core.hardware import HardwareMonitor
from core.queue_manager import JobSettings, QueueManager

BASE_DIR = Path(__file__).parent
app = FastAPI(title="AV1/VMAF Compression Studio")


def _is_authed(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    return request.cookies.get(config.AUTH_COOKIE) == config.auth_token()


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if config.APP_PASSWORD:
        path = request.url.path
        allow = path.startswith("/static") or path in ("/login", "/favicon.ico")
        if not allow and not _is_authed(request):
            if path.startswith("/api"):
                return JSONResponse({"error": "Nicht angemeldet"}, status_code=401)
            from starlette.responses import RedirectResponse
            return RedirectResponse("/login", status_code=302)
    return await call_next(request)


_LOGIN_HTML = """<!DOCTYPE html><html lang=de><head><meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Login</title><link rel=stylesheet href="/static/css/styles.css"></head>
<body style="display:flex;align-items:center;justify-content:center;min-height:100vh">
<form method=post action=/login style="background:var(--surface);padding:32px;border-radius:12px;border:1px solid var(--border);min-width:300px">
<h2 style="margin-top:0">Compression Studio</h2>
<p style="color:var(--text-muted);font-size:13px">Bitte Passwort eingeben.</p>
{error}
<input type=password name=password placeholder=Passwort autofocus
 style="width:100%;padding:10px;margin:12px 0;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;color:var(--text)">
<button class="btn btn-primary btn-block" type=submit>Anmelden</button>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if _is_authed(request):
        from starlette.responses import RedirectResponse
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_LOGIN_HTML.format(error=""))


@app.post("/login")
async def login_submit(request: Request):
    from starlette.responses import RedirectResponse
    form = await request.form()
    if form.get("password") == config.APP_PASSWORD:
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(config.AUTH_COOKIE, config.auth_token(),
                        httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp
    err = '<p style="color:var(--bad,#e5484d);font-size:13px">Falsches Passwort.</p>'
    return HTMLResponse(_LOGIN_HTML.format(error=err), status_code=401)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

monitor = HardwareMonitor()
CAPACITY = monitor.encode_capacity()
# Startwert für parallele Encodes: explizite Env-Vorgabe oder HW-Empfehlung,
# begrenzt durch das konfigurierte Limit.
_initial_parallel = config.MAX_PARALLEL_ENCODES or CAPACITY["suggested_parallel"]
_initial_parallel = max(1, min(config.PARALLEL_ENCODES_LIMIT, _initial_parallel))
queue = QueueManager(max_parallel=_initial_parallel)


@app.on_event("startup")
async def _startup() -> None:
    config.ensure_dirs()
    from core import history
    history.init_db()
    from core.watcher import watcher
    watcher.attach(queue)
    watcher.start()
    logger = logging.getLogger("vcompress.startup")
    logger.info("FFmpeg-Binary: %s | FFprobe: %s", config.FFMPEG, config.FFPROBE)
    logger.info("FFmpeg-Version: %s", ff.ffmpeg_version())
    from core import vmaf as _vmaf
    logger.info("VMAF-Beschleunigung: %s (libvmaf_cuda verfügbar: %s)",
                config.VMAF_HWACCEL, _vmaf.vmaf_cuda_available())
    logger.info("Datenordner: %s", config.data_paths_dict())
    encs = sorted(e for e in ff.available_encoders()
                  if any(x in e for x in ("nvenc", "qsv", "vaapi", "svt", "x264", "x265")))
    logger.info("Verfügbare relevante Encoder: %s", ", ".join(encs) or "KEINE erkannt!")
    logger.info("Encoder-Kapazität: %s GPU(s), NVENC-Engines: %s, Threads: %s → "
                "empfohlen %s parallel, aktiv %s (Limit %s)",
                len(CAPACITY["gpus"]), CAPACITY["nvenc_engines"], CAPACITY["cpu_threads"],
                CAPACITY["suggested_parallel"], queue.get_parallel(),
                config.PARALLEL_ENCODES_LIMIT)


@app.on_event("shutdown")
async def _shutdown() -> None:
    queue.shutdown()
    from core.watcher import watcher
    watcher.stop()


# ----------------------------------------------------------------------- Views
_PLATFORM_LABELS = {
    "nvidia": "NVIDIA (GPU · NVENC)",
    "intel": "Intel (GPU · QSV)",
    "amd": "AMD (GPU · VAAPI)",
    "cpu": "CPU (Software)",
}
_CODEC_LABELS = {"av1": "AV1", "hevc": "HEVC / H.265", "h264": "H.264"}
_ALL_CODECS = ("av1", "hevc", "h264")


def _encoder_options() -> list[dict]:
    """Alle tatsächlich nutzbaren Encoder (erkannte Plattform × im Build vorhanden).

    Damit weiß das UI zuverlässig, welche Plattform/Codec-Kombination verfügbar
    ist – statt fest verdrahteter Annahmen. CPU ist immer dabei (Software-Fallback).
    """
    plats = monitor.available_platforms()
    if "cpu" not in plats:
        plats = plats + ["cpu"]
    out: list[dict] = []
    for p in plats:
        for c in _ALL_CODECS:
            out.append({
                "platform": p,
                "codec": c,
                "value": f"{p}:{c}",
                "platform_label": _PLATFORM_LABELS.get(p, p.upper()),
                "codec_label": _CODEC_LABELS.get(c, c.upper()),
                "encoder": ff.encoder_name(p, c),
                "kind": "cpu" if p == "cpu" else "gpu",
                "available": ff.encoder_available(p, c),
            })
    return out


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    plats = monitor.available_platforms()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "platforms": plats,
            "platform_options": [
                {"value": p, "label": _PLATFORM_LABELS.get(p, p.upper())}
                for p in plats
            ],
            "encoder_options": _encoder_options(),
            "input_dir": str(config.INPUT_DIR),
            "sweetspot": config.VMAF_SWEETSPOT,
            "test_qualities": config.VMAF_TEST_QUALITIES,
            "capacity": CAPACITY,
            "parallel_limit": config.PARALLEL_ENCODES_LIMIT,
            "parallel_current": queue.get_parallel(),
            "asset_version": _asset_version(),
        },
    )


def _asset_version() -> str:
    """Cache-Busting-Token aus der Änderungszeit der statischen Assets."""
    import os

    latest = 0.0
    for rel in ("static/js/app.js", "static/css/styles.css"):
        try:
            latest = max(latest, os.path.getmtime(BASE_DIR / rel))
        except OSError:
            continue
    return str(int(latest)) if latest else "1"


# ------------------------------------------------------------------- Browser
def _safe_resolve(rel: str) -> Optional[Path]:
    """Verhindert Pfad-Traversal: alles muss innerhalb INPUT_DIR liegen."""
    base = config.INPUT_DIR.resolve()
    target = (base / rel.lstrip("/")).resolve() if rel else base
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


@app.get("/api/browse")
async def browse_input(path: str = ""):
    target = _safe_resolve(path)
    if target is None or not target.exists():
        return JSONResponse({"error": "Pfad nicht gefunden"}, status_code=404)
    if not target.is_dir():
        return JSONResponse({"error": "Kein Verzeichnis"}, status_code=400)

    base = config.INPUT_DIR.resolve()
    dirs, files = [], []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            rel = str(entry.resolve().relative_to(base)).replace("\\", "/")
            if entry.is_dir():
                dirs.append({"name": entry.name, "rel": rel})
            elif entry.suffix.lower() in config.VIDEO_EXTENSIONS:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                files.append({
                    "name": entry.name, "rel": rel,
                    "size": size, "size_human": ff.human_size(size),
                })
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    rel_here = str(target.resolve().relative_to(base)).replace("\\", "/")
    parent = "" if target.resolve() == base else str(
        target.resolve().parent.relative_to(base)).replace("\\", "/")
    return {
        "path": rel_here,
        "parent": parent if rel_here else None,
        "is_root": target.resolve() == base,
        "dirs": dirs,
        "files": files,
    }


@app.get("/api/probe")
async def probe(path: str):
    target = _safe_resolve(path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    info, err = ff.probe_with_error(target)
    if info is None:
        return JSONResponse({"error": f"ffprobe: {err or 'unbekannt'}"}, status_code=500)
    return info.to_dict()


# --------------------------------------------------------------------- Queue
class EnqueueRequest(BaseModel):
    path: str
    is_batch: bool = False
    platform: str = "cpu"
    codec: str = "av1"
    quality: int = 28
    target_height: Optional[int] = None
    tonemap: bool = False
    hdr_mode: str = "tonemap"        # tonemap (HDR->SDR) | preserve (HDR behalten)
    keep_subtitles: bool = True
    keep_chapters: bool = True
    keep_metadata: bool = True
    film_grain: int = 0
    denoise: str = "off"             # off | light | medium | strong
    two_pass: bool = False
    vmaf_check: bool = True
    workflow: str = "auto"           # auto | manual | compare_only
    rate_mode: str = "cq"            # cq | bitrate | abr
    compare_encoders: list[str] = []  # zusätzliche "plattform:codec"-Vergleiche
    test_values: list[int] = [20, 24, 28, 32]
    clip_seconds: int = 30
    samples: int = 1
    vmaf_engine: str = "auto"
    generate_screenshots: bool = True
    post_processing: str = "keep"
    suffix: str = "_av1"
    audio_mode: str = "copy"         # copy | encode | none
    audio_codec: str = "aac"         # aac | opus | ac3 | eac3 | flac
    audio_bitrate: int = 160
    audio_channels: int = 0          # 0 = Original, 1 = Mono, 2 = Stereo
    audio_normalize: bool = False
    audio_tracks: list[int] = []     # leer = alle Tonspuren
    audio_per_track: bool = False    # Audio pro Spur konfiguriert
    audio_track_settings: list[dict] = []  # je Spur: index/mode/codec/bitrate/…


class ApproveRequest(BaseModel):
    result_index: int


@app.post("/api/enqueue")
async def enqueue(req: EnqueueRequest):
    target = _safe_resolve(req.path)
    if target is None or not target.exists():
        return JSONResponse({"error": "Pfad nicht gefunden"}, status_code=404)

    from core.queue_manager import build_job_settings
    settings = build_job_settings(req.model_dump())
    if req.is_batch:
        items = queue.add_batch(str(target), settings)
        if not items:
            return JSONResponse({"error": "Keine Videos im Ordner gefunden"}, status_code=400)
        return {"added": len(items)}
    item = queue.add_file(str(target), settings)
    if item is None:
        return JSONResponse({"error": "Datei konnte nicht hinzugefügt werden"}, status_code=400)
    return {"added": 1, "id": item.id}


@app.get("/api/queue")
async def get_queue():
    return queue.state()


@app.post("/api/queue/{item_id}/approve")
async def approve(item_id: str, req: ApproveRequest):
    return {"ok": queue.approve(item_id, req.result_index)}


@app.post("/api/queue/{item_id}/skip")
async def skip_encode(item_id: str):
    return {"ok": queue.skip_encode(item_id)}


@app.get("/api/preview/{path:path}")
async def preview_image(path: str):
    """Liefert VMAF-Vergleichs-Screenshots aus dem Preview-Verzeichnis."""
    target = (config.PREVIEW_DIR / path).resolve()
    try:
        target.relative_to(config.PREVIEW_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "Ungültiger Pfad"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    return FileResponse(target, media_type="image/jpeg")


class ProfileRequest(BaseModel):
    name: str
    settings: dict


@app.get("/api/profiles")
async def list_profiles():
    from core import profiles
    return {"profiles": profiles.load()}


@app.post("/api/profiles")
async def save_profile(req: ProfileRequest):
    from core import profiles
    return {"profiles": profiles.save_profile(req.name, req.settings)}


@app.delete("/api/profiles/{name}")
async def delete_profile(name: str):
    from core import profiles
    return {"profiles": profiles.delete(name)}


class LibraryScanRequest(BaseModel):
    root: str = ""
    name_contains: str = ""
    min_size_mb: float = 0
    min_bitrate_mbps: float = 0
    min_height: int = 0
    codecs_include: list[str] = []
    codecs_exclude: list[str] = []


@app.post("/api/library/scan")
async def library_scan(req: LibraryScanRequest):
    from core import library
    started = library.start_scan(req.root, req.model_dump())
    return {"started": started, "state": library.get_state()}


@app.get("/api/library/scan")
async def library_scan_state():
    from core import library
    return library.get_state()


@app.get("/api/notify")
async def get_notify():
    from core import notify
    cfg = notify.load()
    # Secrets nicht im Klartext an die UI zurückgeben – nur ob gesetzt.
    return {
        "webhook_url": cfg["webhook_url"],
        "discord_url": cfg["discord_url"],
        "telegram_chat": cfg["telegram_chat"],
        "telegram_token_set": bool(cfg["telegram_token"]),
        "on_done": cfg["on_done"],
        "on_failed": cfg["on_failed"],
    }


class NotifyRequest(BaseModel):
    webhook_url: str = ""
    discord_url: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""
    on_done: bool = True
    on_failed: bool = True


@app.post("/api/notify")
async def set_notify(req: NotifyRequest):
    from core import notify
    payload = req.model_dump()
    # Leeres Telegram-Token bedeutet „unverändert lassen" (UI kennt es nicht).
    if not payload.get("telegram_token"):
        payload.pop("telegram_token", None)
    notify.save(payload)
    return {"ok": True}


@app.post("/api/notify/test")
async def test_notify():
    from core import notify
    notify.send("🔔 Testbenachrichtigung", "Verbindung von Compression Studio funktioniert.")
    return {"ok": True}


@app.get("/api/watch")
async def get_watch():
    from core.watcher import watcher
    return watcher.status()


class WatchRequest(BaseModel):
    enabled: bool = False
    folder: str = ""
    interval_min: int = 15
    profile: str = ""
    active_start: Optional[int] = None
    active_end: Optional[int] = None


@app.post("/api/watch")
async def set_watch(req: WatchRequest):
    from core import watcher as watcher_mod
    data = req.model_dump()
    data["interval_min"] = max(1, min(1440, data["interval_min"]))
    for k in ("active_start", "active_end"):
        if data[k] is not None:
            data[k] = max(0, min(23, int(data[k])))
    watcher_mod.save_config(data)
    watcher_mod.watcher.reconfigure()
    return {"ok": True}


@app.post("/api/watch/scan")
async def watch_scan_now():
    """Watch-Ordner sofort einmalig prüfen (unabhängig vom Zeitfenster)."""
    from core.watcher import watcher, load_config
    cfg = load_config()
    watcher._scan(cfg)
    return {"ok": True, "added": watcher._last_added}


@app.get("/api/stats")
async def get_stats():
    """Aggregierte Kennzahlen + letzte Jobs aus der persistenten Historie."""
    from core import history
    return {"stats": history.stats(), "recent": history.recent(100)}


@app.post("/api/stats/clear")
async def clear_stats():
    from core import history
    return {"deleted": history.clear()}


@app.get("/api/vmaf/sessions")
async def vmaf_sessions():
    """Liste archivierter VMAF-Vergleiche für die Verlaufsauswahl."""
    from core import vmaf as vmaf_mod
    return {"sessions": vmaf_mod.list_sessions()}


@app.get("/api/vmaf/session/{name}")
async def vmaf_session(name: str):
    """Gespeicherte Analyse eines früheren Vergleichs laden."""
    from core import vmaf as vmaf_mod
    data = vmaf_mod.load_session(name)
    if data is None:
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    return data


class ReanalyzeRequest(BaseModel):
    session: str
    vmaf_engine: str = "cpu"   # cpu | gpu | both


@app.post("/api/vmaf/reanalyze")
async def vmaf_reanalyze(req: ReanalyzeRequest):
    """Einen früheren Vergleich erneut rechnen (z. B. jetzt per GPU).

    Referenz und Test-Encodes werden aus der Quelle neu erzeugt – dafür muss die
    Originaldatei noch am selben Pfad liegen. Läuft als reiner Vergleich
    (compare_only), es wird also nichts encodiert/überschrieben.
    """
    from core import vmaf as vmaf_mod
    from core.queue_manager import JobSettings
    import dataclasses

    data = vmaf_mod.load_session(req.session)
    if data is None:
        return JSONResponse({"error": "Vergleich nicht gefunden"}, status_code=404)
    src = data.get("source_path", "")
    if not src or not Path(src).is_file():
        return JSONResponse(
            {"error": "Quelldatei nicht mehr verfügbar – Neu-Analyse nicht möglich."},
            status_code=400)

    params = data.get("params", {}) or {}
    valid = {f.name for f in dataclasses.fields(JobSettings)}
    settings = JobSettings(**{k: v for k, v in params.items() if k in valid})
    # Als reinen Vergleich mit gewählter Engine ausführen.
    settings.workflow = "compare_only"
    settings.vmaf_check = True
    settings.vmaf_engine = req.vmaf_engine if req.vmaf_engine in ("cpu", "gpu", "both") else "cpu"

    item = queue.add_file(src, settings)
    if item is None:
        return JSONResponse({"error": "Konnte nicht eingereiht werden."}, status_code=400)
    return {"ok": True, "item_id": item.id}


@app.post("/api/queue/{item_id}/cancel")
async def cancel(item_id: str):
    return {"ok": queue.cancel(item_id)}


@app.post("/api/queue/clear")
async def clear():
    queue.clear_finished()
    return {"ok": True}


class PauseRequest(BaseModel):
    paused: bool


@app.post("/api/queue/pause")
async def pause_queue(req: PauseRequest):
    return {"paused": queue.set_paused(req.paused)}


@app.post("/api/queue/{item_id}/move")
async def move_item(item_id: str, direction: int = 1):
    return {"ok": queue.move(item_id, -1 if direction < 0 else 1)}


class ParallelRequest(BaseModel):
    value: int = Field(..., ge=1)


@app.get("/api/config/parallel")
async def get_parallel():
    return {
        "value": queue.get_parallel(),
        "limit": config.PARALLEL_ENCODES_LIMIT,
        "capacity": CAPACITY,
    }


@app.post("/api/config/parallel")
async def set_parallel(req: ParallelRequest):
    n = max(1, min(config.PARALLEL_ENCODES_LIMIT, req.value))
    return {"value": queue.set_parallel(n)}


@app.get("/api/config/paths")
async def config_paths():
    data = config.data_paths_dict()
    data["storage"] = storage_summary()
    return data


class DataDeleteRequest(BaseModel):
    root: str = Field(..., pattern="^(vmaf|previews|work)$")
    path: str = ""


@app.get("/api/data/browse")
async def data_browse(root: str = "vmaf", path: str = ""):
    if root not in DATA_ROOTS:
        return JSONResponse({"error": "Unbekannte Zone"}, status_code=400)
    result = browse_data_zone(root, path)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@app.get("/api/data/file")
async def data_file(root: str, path: str):
    """Dateien aus dem Datenordner ausliefern (Bilder, JSON, Videos)."""
    target = _safe_data_file(root, path)
    if target is None:
        return JSONResponse({"error": "Ungültiger Pfad"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    media = _guess_media_type(target)
    return FileResponse(target, media_type=media)


def _safe_data_file(root: str, path: str):
    from core.data_browser import _safe_resolve
    return _safe_resolve(root, path)


def _guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif",
        ".json": "application/json",
        ".mkv": "video/x-matroska", ".mp4": "video/mp4", ".webm": "video/webm",
    }.get(ext, "application/octet-stream")


@app.post("/api/data/delete")
async def data_delete(req: DataDeleteRequest):
    if not req.path:
        return JSONResponse({"error": "Pfad fehlt"}, status_code=400)
    ok, err = delete_item(req.root, req.path)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.post("/api/data/delete-all")
async def data_delete_all(root: str):
    if root not in DATA_ROOTS:
        return JSONResponse({"error": "Unbekannte Zone"}, status_code=400)
    count, err = delete_all_in_root(root)
    if err:
        return JSONResponse({"error": err, "deleted": count}, status_code=500)
    return {"ok": True, "deleted": count}


@app.get("/api/hardware")
async def hardware():
    return monitor.snapshot().to_dict()


# ------------------------------------------------------------------- WebSocket
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if config.APP_PASSWORD and websocket.cookies.get(config.AUTH_COOKIE) != config.auth_token():
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            # Hardware-Snapshot kann blockieren (subprocess) -> Thread auslagern
            snap = await asyncio.to_thread(monitor.snapshot)
            payload = {
                "hardware": snap.to_dict(),
                "queue": queue.state(),
            }
            await websocket.send_json(payload)
            await asyncio.sleep(config.METRICS_INTERVAL)
    except WebSocketDisconnect:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8080, log_level="info")
