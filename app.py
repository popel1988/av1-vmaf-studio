"""AV1/VMAF Compression Studio – FastAPI-Anwendung.

Stellt das Dashboard (Web-UI), den Datei-/Ordner-Browser, die Queue-API sowie
einen WebSocket für Live-Hardware-Metriken und Encode-Fortschritt bereit.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

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


def _api_key_from(request: Request) -> str:
    return (request.headers.get("X-API-Key")
            or request.query_params.get("apikey") or "")


def _api_key_ok(request: Request) -> bool:
    from core import apikeys
    return apikeys.validate(_api_key_from(request))


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    # Externe REST-API (/api/v1): per API-Schlüssel absichern – unabhängig vom
    # UI-Passwort. Eine gültige UI-Session zählt ebenfalls als berechtigt.
    if path.startswith("/api/v1"):
        from core import apikeys
        if apikeys.any_configured() and not _api_key_ok(request) and not _is_authed(request):
            return JSONResponse({"error": "Ungültiger oder fehlender API-Schlüssel"},
                                status_code=401)
        return await call_next(request)
    if config.APP_PASSWORD:
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
    # Offene Aufträge aus der letzten Sitzung wiederherstellen (überleben Neustart).
    queue.restore()
    # Echte Encoder-Fähigkeiten im Hintergrund testen (nur falls noch kein Cache).
    from core import capabilities as caps
    caps.ensure_async(monitor)
    from core import scheduler
    queue.set_gate(scheduler.gate)
    from core.watcher import watcher
    watcher.attach(queue)
    watcher.start()
    logger = logging.getLogger("vcompress.startup")
    logger.info("FFmpeg-Binary: %s | FFprobe: %s", config.FFMPEG, config.FFPROBE)
    logger.info("FFmpeg-Version: %s", ff.ffmpeg_version())
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
def _platform_label(p: str) -> str:
    if p == "nvidia":
        return "NVIDIA (GPU · NVENC)"
    if p == "amd":
        return "AMD (GPU · VAAPI)"
    if p == "intel":
        backend = "VAAPI" if ff.intel_uses_vaapi() else "QSV"
        return f"Intel (GPU · {backend})"
    return "CPU (Software)"


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
    from core import capabilities as caps
    working = caps.results_map()  # {} solange noch nicht getestet
    out: list[dict] = []
    for p in plats:
        for c in _ALL_CODECS:
            out.append({
                "platform": p,
                "codec": c,
                "value": f"{p}:{c}",
                "platform_label": _platform_label(p),
                "codec_label": _CODEC_LABELS.get(c, c.upper()),
                "encoder": ff.encoder_name(p, c),
                "kind": "cpu" if p == "cpu" else "gpu",
                "available": ff.encoder_available(p, c),
                # True/False aus echtem Mini-Encode-Test; None = noch nicht getestet.
                "working": working.get(f"{p}:{c}"),
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
                {"value": p, "label": _platform_label(p)}
                for p in plats
            ],
            "encoder_options": _encoder_options(),
            "video_extensions": sorted(e.lstrip(".") for e in config.VIDEO_EXTENSIONS),
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
    for rel in ("static/js/app.js", "static/js/i18n.js", "static/css/styles.css"):
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


@app.get("/api/search")
async def search_input(path: str = "", q: str = "", limit: int = 500):
    """Rekursive Namenssuche ab `path` (Videos). Für große Bibliotheken.

    Liefert Treffer inkl. relativem Unterordner, damit die Herkunft klar ist.
    Begrenzt auf `limit` Ergebnisse, um die Payload beherrschbar zu halten.
    """
    query = (q or "").strip().lower()
    if not query:
        return {"files": [], "truncated": False}
    target = _safe_resolve(path)
    if target is None or not target.is_dir():
        return JSONResponse({"error": "Kein Verzeichnis"}, status_code=400)

    base = config.INPUT_DIR.resolve()
    files: list[dict] = []
    truncated = False
    for root, dirnames, filenames in os.walk(target):
        # Versteckte Ordner (.archiv, .previews …) überspringen.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames, key=str.lower):
            if fn.startswith(".") or Path(fn).suffix.lower() not in config.VIDEO_EXTENSIONS:
                continue
            if query not in fn.lower():
                continue
            entry = Path(root) / fn
            try:
                rel = str(entry.resolve().relative_to(base)).replace("\\", "/")
                size = entry.stat().st_size
            except (OSError, ValueError):
                continue
            folder = str(Path(rel).parent).replace("\\", "/")
            files.append({
                "name": fn, "rel": rel, "size": size,
                "size_human": ff.human_size(size),
                "folder": "" if folder == "." else folder,
            })
            if len(files) >= max(1, min(2000, limit)):
                truncated = True
                break
        if truncated:
            break
    return {"files": files, "truncated": truncated}


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
    dv_mode: str = ""                # DV-Quellen: preserve | hdr10 | tonemap (Vorrang vor hdr_mode)
    preserve_dv: bool = False        # Legacy: Dolby-Vision-RPU beibehalten
    keep_subtitles: bool = True
    subtitle_per_track: bool = False
    subtitle_track_settings: list[dict] = []  # je Spur: index/default/forced
    keep_chapters: bool = True
    keep_metadata: bool = True
    film_grain: int = 0
    denoise: str = "off"             # off | light | medium | strong
    two_pass: bool = False
    anime: bool = False              # Anime-Modus: VMAF-NEG-Modell + 10-bit-Ausgabe
    verify_vmaf: bool = False        # Guardrail: echten VMAF nach Encode messen
    verify_min: float = 93.0         # Ziel-VMAF für die Guardrail
    verify_retry: bool = False       # bei Unterschreiten automatisch neu encoden
    video_mode: str = "encode"       # encode | copy (nur Audio optimieren)
    audio_opt_scope: str = "bloated" # bloated | all
    audio_min_bitrate_kbps: int = 700
    chunked: bool = False            # Per-Szene/Chunked Adaptive Encoding
    chunk_seconds: int = 60
    chunk_cq_range: int = 6
    vmaf_check: bool = True
    workflow: str = "auto"           # auto | manual | compare_only
    target_vmaf: float = 0.0         # >0: Ziel-VMAF (Super-Tool)
    rate_mode: str = "cq"            # cq | bitrate | abr
    compare_encoders: list[str] = []  # zusätzliche "plattform:codec"-Vergleiche
    test_values: list[int] = [20, 24, 28, 32]
    clip_seconds: int = 30
    samples: int = 1
    generate_screenshots: bool = True
    post_processing: str = "keep"
    container: str = "auto"          # auto | mkv | mp4 (Ausgabe-Container)
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
    name_exclude: list[str] = []
    min_size_mb: float = 0
    min_bitrate_mbps: float = 0
    min_height: int = 0
    codecs_include: list[str] = []
    codecs_exclude: list[str] = []
    target_codec: str = "av1"        # Ziel für die Einspar-Projektion
    dynamic_filter: str = ""         # ""|sdr|hdr|dv|dv5|dv7|dv8 (Dynamik-Filter)
    skip_optimized: bool = False     # bereits effiziente Dateien ausblenden
    skip_processed: bool = False     # bereits verarbeitete Dateien ausblenden


@app.post("/api/library/scan")
async def library_scan(req: LibraryScanRequest):
    from core import library
    started = library.start_scan(req.root, req.model_dump())
    return {"started": started, "state": library.get_state()}


@app.get("/api/library/scan")
async def library_scan_state():
    from core import library
    return library.get_state()


@app.get("/api/library/last")
async def library_last():
    """Zuletzt gecachten Scan laden (Anzeige beim Öffnen der Bibliothek)."""
    from core import library
    return library.load_last()


@app.get("/api/library/export.csv")
async def library_export_csv():
    """Aktuelle Treffer als CSV herunterladen."""
    from fastapi.responses import Response
    from core import library
    csv_text = library.export_csv()
    return Response(
        content=csv_text, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=library_scan.csv"})


# ---------------------------------------------------------------- Super-Tool
class SuperScanRequest(BaseModel):
    folder: str = ""
    extensions: list[str] = []       # z. B. ["mkv","mp4"] – leer = alle
    name_contains: str = ""
    name_exclude: list[str] = []
    min_size_mb: float = 0
    min_bitrate_mbps: float = 0
    min_height: int = 0
    codecs_include: list[str] = []
    codecs_exclude: list[str] = []


@app.post("/api/supertool/scan")
async def supertool_scan(req: SuperScanRequest):
    from core import supertool
    started = supertool.start_scan(req.model_dump())
    return {"started": started, "state": supertool.get_state()}


@app.get("/api/supertool/scan")
async def supertool_scan_state():
    from core import supertool
    return supertool.get_state()


@app.post("/api/supertool/list")
async def supertool_list(req: SuperScanRequest):
    """Schnelle Datei-Vorschau (ohne Probe) für die Live-Liste neben der Ordnerwahl."""
    from core import supertool
    return supertool.quick_list(req.model_dump())


class SuperStartRequest(BaseModel):
    paths: list[str] = []
    mode: str = "representative"      # target_vmaf | representative | fixed
    settings: dict = {}


@app.post("/api/supertool/start")
async def supertool_start(req: SuperStartRequest):
    from core import supertool
    if not req.paths:
        return JSONResponse({"error": "Keine Dateien ausgewählt"}, status_code=400)
    added, group_id, err = supertool.start_batch(
        queue, req.paths, req.settings or {}, req.mode)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"added": added, "group_id": group_id}


class AudioScanRequest(BaseModel):
    folder: str = ""
    extensions: list[str] = []
    settings: dict = {}              # audio_codec/channels/bitrate/scope/min_bitrate_kbps


@app.post("/api/audio/scan")
async def audio_scan(req: AudioScanRequest):
    from core import audio_opt
    started = audio_opt.start_scan(req.model_dump())
    return {"started": started, "state": audio_opt.get_state()}


@app.get("/api/audio/scan")
async def audio_scan_state():
    from core import audio_opt
    return audio_opt.get_state()


class AudioStartRequest(BaseModel):
    paths: list[str] = []
    settings: dict = {}


@app.post("/api/audio/start")
async def audio_start(req: AudioStartRequest):
    from core import audio_opt
    if not req.paths:
        return JSONResponse({"error": "Keine Dateien ausgewählt"}, status_code=400)
    added, batch_id, err = audio_opt.start_batch(queue, req.paths, req.settings or {})
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"added": added, "batch_id": batch_id}


@app.get("/api/supertool/status")
async def supertool_status(batch_id: str = ""):
    """Fortschritt eines Super-Tool-Stapels (aus der Queue nach batch_id)."""
    st = queue.state()
    items = [it for it in st["items"]
             if not batch_id or it["settings"].get("batch_id") == batch_id]
    return {"items": items, "counts": st["counts"]}


# ====================================================================== REST v1
def _resolve_under_input(raw: str):
    """Pfad (relativ zum Eingabeordner oder absolut) sicher innerhalb INPUT_DIR."""
    from pathlib import Path
    if not raw:
        return None
    base = config.INPUT_DIR.resolve()
    p = Path(raw)
    try:
        target = p.resolve() if p.is_absolute() else (base / raw.lstrip("/")).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    return target if target.exists() else None


def _remap_arr_path(raw: str) -> str:
    """Optionales Pfad-Remapping für *arr (Env ARR_PATH_MAP='from:to,from2:to2')."""
    mapping = os.getenv("ARR_PATH_MAP", "")
    for pair in mapping.split(","):
        if ":" in pair:
            frm, _, to = pair.partition(":")
            frm, to = frm.strip(), to.strip()
            if frm and raw.startswith(frm):
                return to + raw[len(frm):]
    return raw


def _enqueue_external(target, profile=None, settings=None, is_batch=False) -> int:
    from core import profiles as prof
    from core.queue_manager import build_job_settings
    d: dict = {}
    if profile:
        p = prof.get(profile)
        if p:
            d.update(p.get("settings", {}))
    if settings:
        d.update(settings)
    js = build_job_settings(d)
    if is_batch:
        return len(queue.add_batch(str(target), js))
    return 1 if queue.add_file(str(target), js) else 0


class ApiEnqueueRequest(BaseModel):
    path: str
    is_batch: bool = False
    profile: str = ""
    settings: dict = {}


@app.get("/api/v1/health")
async def v1_health():
    return {"status": "ok", "version": 1, "ffmpeg": ff.ffmpeg_version()}


@app.get("/api/v1/queue")
async def v1_queue():
    return queue.state()


@app.get("/api/v1/stats")
async def v1_stats():
    from core import history
    return history.stats()


@app.post("/api/v1/enqueue")
async def v1_enqueue(req: ApiEnqueueRequest):
    target = _resolve_under_input(req.path)
    if target is None:
        return JSONResponse(
            {"error": "Pfad nicht gefunden oder außerhalb des Eingabeordners"},
            status_code=404)
    added = _enqueue_external(target, req.profile or None, req.settings or None, req.is_batch)
    if not added:
        return JSONResponse({"error": "Nichts hinzugefügt"}, status_code=400)
    return {"added": added}


def _extract_arr_paths(payload: dict) -> list[str]:
    """Dateipfade aus einem Sonarr/Radarr-Webhook-Payload ziehen."""
    paths: list[str] = []
    mf = payload.get("movieFile") or {}
    if isinstance(mf, dict) and mf.get("path"):
        paths.append(mf["path"])
    ef = payload.get("episodeFile") or {}
    if isinstance(ef, dict) and ef.get("path"):
        paths.append(ef["path"])
    for e in payload.get("episodeFiles") or []:
        if isinstance(e, dict) and e.get("path"):
            paths.append(e["path"])
    return [p for p in paths if p]


@app.post("/api/v1/webhook/arr")
async def v1_webhook_arr(request: Request):
    """Empfänger für Sonarr/Radarr-Webhooks ('On Import'/'On Upgrade').

    Erwartet den JSON-Payload; der Dateipfad wird (optional per ARR_PATH_MAP
    remappt) unter dem Eingabeordner aufgelöst und in die Warteschlange gelegt.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Kein gültiges JSON"}, status_code=400)
    event = str(payload.get("eventType", "")).lower()
    if event in ("test", "healthcheck", "grab", "rename"):
        return {"ok": True, "ignored": event}
    profile = request.query_params.get("profile", "")
    raw_paths = _extract_arr_paths(payload)
    added = 0
    for rp in raw_paths:
        target = _resolve_under_input(_remap_arr_path(rp))
        if target is not None:
            added += _enqueue_external(target, profile or None)
    return {"ok": True, "event": event, "found": len(raw_paths), "added": added}


# --------------------------------------------------------- API-Schlüssel (UI)
@app.get("/api/apikeys")
async def get_apikeys():
    from core import apikeys
    return apikeys.list_masked()


@app.post("/api/apikeys/generate")
async def generate_apikey():
    from core import apikeys
    return {"key": apikeys.generate()}


class ApiKeyRevokeRequest(BaseModel):
    index: int


@app.post("/api/apikeys/revoke")
async def revoke_apikey(req: ApiKeyRevokeRequest):
    from core import apikeys
    return {"ok": apikeys.revoke_index(req.index)}


@app.get("/api/scheduler")
async def get_scheduler():
    from core import scheduler
    return scheduler.status()


class SchedulerRequest(BaseModel):
    enabled: bool = False
    window_enabled: bool = False
    start_hour: int = 22
    end_hour: int = 6
    throttle_enabled: bool = False
    max_cpu_percent: int = 85


@app.post("/api/scheduler")
async def set_scheduler(req: SchedulerRequest):
    from core import scheduler
    saved = scheduler.save(req.model_dump())
    return {"ok": True, "config": saved}


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


@app.get("/api/media")
async def media(path: str, root: str = "input"):
    """Streamt eine Video-Datei (mit Range-Support) für den A/B-Vergleichsplayer."""
    from pathlib import Path
    roots = {"input": config.INPUT_DIR, "output": config.OUTPUT_DIR}
    base = roots.get(root, config.INPUT_DIR).resolve()
    target = (base / path.lstrip("/")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"error": "Ungültiger Pfad"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    return FileResponse(target)


@app.get("/api/ffprobe")
async def ffprobe_any(path: str, root: str = "input"):
    """ffprobe für eine Datei aus dem Input- oder Output-Ordner (Detail-Ansicht)."""
    from pathlib import Path
    roots = {"input": config.INPUT_DIR, "output": config.OUTPUT_DIR}
    base = roots.get(root, config.INPUT_DIR).resolve()
    target = (base / path.lstrip("/")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"error": "Ungültiger Pfad"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    info, err = ff.probe_with_error(target)
    if info is None:
        return JSONResponse({"error": f"ffprobe: {err or 'unbekannt'}"}, status_code=500)
    return info.to_dict()


def _rel_to(base, abs_path) -> Optional[str]:
    from pathlib import Path
    try:
        return str(Path(abs_path).resolve().relative_to(Path(base).resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return None


@app.get("/api/queue/{item_id}/details")
async def queue_details(item_id: str):
    """Detail-Ansicht eines (auch abgeschlossenen) Auftrags: ffprobe von Quelle
    und Ausgabe, Medien-URLs für den Player sowie Encode-Kennzahlen."""
    item = queue.get_item(item_id)
    if item is None:
        return JSONResponse({"error": "Auftrag nicht gefunden"}, status_code=404)

    def _pack(abs_path, root):
        from pathlib import Path
        if not abs_path:
            return None
        p = Path(abs_path)
        rel = _rel_to(config.INPUT_DIR if root == "input" else config.OUTPUT_DIR, p)
        entry = {"name": p.name, "exists": p.is_file(), "media": None,
                 "info": None, "root": root, "rel": rel}
        if rel is not None and p.is_file():
            entry["media"] = f"/api/media?root={root}&path={quote(rel)}"
            info, _ = ff.probe_with_error(p)
            if info:
                entry["info"] = info.to_dict()
        return entry

    src = _pack(item.path, "input")
    out = _pack(item.output_path, "output")

    # Encode-Kennzahlen: Geschwindigkeit (x-fach), Ø FPS, Ersparnis.
    stats = {
        "duration": item.duration,
        "duration_human": ff.human_duration(item.duration) if item.duration else "—",
        "status": item.status,
        "vmaf_verify": item.vmaf_verify,
        "original_human": ff.human_size(item.original_size) if item.original_size else "—",
        "output_human": ff.human_size(item.output_size) if item.output_size else "—",
        "saved_human": ff.human_size(item.saved_bytes) if item.saved_bytes else "—",
        "savings_percent": (round(item.saved_bytes / item.original_size * 100, 1)
                            if item.original_size and item.saved_bytes else None),
        "speed_x": None,
        "avg_fps": None,
    }
    vid_dur = (item.info or {}).get("duration") if item.info else None
    if vid_dur and item.duration and item.duration > 0:
        stats["speed_x"] = round(vid_dur / item.duration, 2)
        fps = (item.info or {}).get("fps")
        if fps:
            stats["avg_fps"] = round(fps * stats["speed_x"], 1)

    return {
        "id": item.id, "title": item.title, "status": item.status,
        "source": src, "output": out, "stats": stats,
    }


@app.get("/api/diagnostics")
async def diagnostics(deep: bool = False):
    """Selbsttest: FFmpeg/Encoder, VMAF-Modelle, dovi_tool, GPU/VAAPI, Ordner.

    deep=1 führt echte Mini-Encodes je Encoder aus (prüft die tatsächliche
    Hardware-Fähigkeit, dauert etwas länger) und aktualisiert dabei den
    Encoder-Fähigkeits-Cache, den VMAF-Tool/Encoding zum Ausblenden nutzen."""
    from core import diagnostics as diag
    # Blockierende ffmpeg-Aufrufe im Threadpool, damit der Event-Loop frei bleibt.
    return await asyncio.to_thread(diag.run_diagnostics, monitor, deep)


@app.get("/api/capabilities")
async def capabilities():
    """Echte Encoder-Fähigkeiten (per Mini-Encode getestet). Leeres results =
    noch nicht getestet -> UI fällt auf die Build-Verfügbarkeit zurück."""
    from core import capabilities as caps
    data = caps.get_cached()
    if data is None:
        # Test evtl. noch nicht durch -> im Hintergrund anstoßen.
        caps.compute_async(monitor)
        return {"results": {}, "generated_at": 0, "pending": True}
    return data


@app.post("/api/capabilities/refresh")
async def capabilities_refresh():
    """Encoder-Fähigkeiten neu ermitteln (echte Mini-Encodes)."""
    from core import capabilities as caps
    return await asyncio.to_thread(caps.compute, monitor, None)


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
