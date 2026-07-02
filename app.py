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
    logger = logging.getLogger("vcompress.startup")
    logger.info("FFmpeg-Binary: %s | FFprobe: %s", config.FFMPEG, config.FFPROBE)
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


# ----------------------------------------------------------------------- Views
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "platforms": monitor.available_platforms(),
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
    vmaf_check: bool = True
    workflow: str = "auto"           # auto | manual | compare_only
    rate_mode: str = "cq"            # cq | bitrate | abr
    compare_encoders: list[str] = []  # zusätzliche "plattform:codec"-Vergleiche
    test_values: list[int] = [20, 24, 28, 32]
    clip_seconds: int = 30
    generate_screenshots: bool = True
    post_processing: str = "keep"
    suffix: str = "_av1"
    audio_mode: str = "copy"         # copy | encode | none
    audio_codec: str = "aac"         # aac | opus | ac3 | eac3 | flac
    audio_bitrate: int = 160
    audio_channels: int = 0          # 0 = Original, 1 = Mono, 2 = Stereo
    audio_normalize: bool = False
    audio_tracks: list[int] = []     # leer = alle Tonspuren


class ApproveRequest(BaseModel):
    result_index: int


@app.post("/api/enqueue")
async def enqueue(req: EnqueueRequest):
    target = _safe_resolve(req.path)
    if target is None or not target.exists():
        return JSONResponse({"error": "Pfad nicht gefunden"}, status_code=404)

    settings = JobSettings(
        platform=req.platform,
        codec=req.codec,
        quality=req.quality,
        target_height=req.target_height,
        tonemap=req.tonemap,
        vmaf_check=req.vmaf_check,
        workflow=req.workflow,
        rate_mode=req.rate_mode,
        compare_encoders=list(req.compare_encoders),
        test_values=req.test_values[:4],
        clip_seconds=max(5, min(120, req.clip_seconds)),
        generate_screenshots=req.generate_screenshots,
        post_processing=req.post_processing,
        suffix=req.suffix,
        audio_mode=req.audio_mode,
        audio_codec=req.audio_codec,
        audio_bitrate=req.audio_bitrate,
        audio_channels=req.audio_channels,
        audio_normalize=req.audio_normalize,
        audio_tracks=list(req.audio_tracks),
    )
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


@app.post("/api/queue/{item_id}/cancel")
async def cancel(item_id: str):
    return {"ok": queue.cancel(item_id)}


@app.post("/api/queue/clear")
async def clear():
    queue.clear_finished()
    return {"ok": True}


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
