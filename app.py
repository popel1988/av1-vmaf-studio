"""AV1/VMAF Compression Studio – FastAPI-Anwendung.

Stellt das Dashboard (Web-UI), den Datei-/Ordner-Browser, die Queue-API sowie
einen WebSocket für Live-Hardware-Metriken und Encode-Fortschritt bereit.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from core import config
from core import ffmpeg_utils as ff
from core.hardware import HardwareMonitor
from core.queue_manager import JobSettings, QueueManager

BASE_DIR = Path(__file__).parent
app = FastAPI(title="AV1/VMAF Compression Studio")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

monitor = HardwareMonitor()
queue = QueueManager()


@app.on_event("startup")
async def _startup() -> None:
    config.ensure_dirs()


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
        },
    )


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
async def browse(path: str = ""):
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
    post_processing: str = "keep"
    suffix: str = "_av1"


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
        post_processing=req.post_processing,
        suffix=req.suffix,
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


@app.post("/api/queue/{item_id}/cancel")
async def cancel(item_id: str):
    return {"ok": queue.cancel(item_id)}


@app.post("/api/queue/clear")
async def clear():
    queue.clear_finished()
    return {"ok": True}


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
