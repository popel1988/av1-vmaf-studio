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

from core import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

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
<title>Video Studio</title><link rel=stylesheet href="/static/css/styles.css"></head>
<body style="display:flex;align-items:center;justify-content:center;min-height:100vh">
<form method=post action=/login style="background:var(--surface);padding:32px;border-radius:12px;border:1px solid var(--border);min-width:300px">
<h2 style="margin-top:0">Video Studio</h2>
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
    from core import profiles as prof
    prof.ensure_builtins()
    # Warteschlange aus /data/queue.json wiederherstellen (offen + fertig).
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
    try:
        from core import player_hls
        player_hls.cleanup_all()
    except Exception:
        pass


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
    from core import app_settings
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
            "media_dir": str(config.MEDIA_DIR),
            "media_roots": config.media_roots_public(),
            "multi_media": config.MULTI_MEDIA,
            "default_output": app_settings.default_output_rel(),
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
    """Verhindert Pfad-Traversal: alles muss innerhalb eines Media-Roots liegen.

    Bei genau einem Root und leerem Pfad wird der Root selbst geliefert
    (Verzeichnis-Listing). Bei mehreren Roots ist "" die virtuelle Wurzel und
    hat keinen Dateisystem-Pfad – das behandelt der Browser separat.
    """
    if not rel and not config.MULTI_MEDIA:
        return config.MEDIA_ROOTS[0][1].resolve()
    return config.resolve_input(rel)


@app.get("/api/browse")
async def browse_input(path: str = "", kind: str = "video"):
    # Virtuelle Wurzel bei mehreren Roots: die Roots als "Ordner" auflisten.
    if not path and config.MULTI_MEDIA:
        dirs = [{"name": r["name"], "rel": r["name"]}
                for r in config.media_roots_public()]
        return {"path": "", "parent": None, "is_root": True,
                "roots": True, "dirs": dirs, "files": []}

    target = _safe_resolve(path)
    if target is None or not target.exists():
        return JSONResponse({"error": "Pfad nicht gefunden"}, status_code=404)
    if not target.is_dir():
        return JSONResponse({"error": "Kein Verzeichnis"}, status_code=400)

    # kind=aux: zusätzlich Ton-/Untertitel-Dateien listen (Remux-Modus:
    # externe Spuren hinzufügen). Standard = nur Videos.
    if kind == "aux":
        allowed = (config.VIDEO_EXTENSIONS | config.AUDIO_EXTENSIONS
                   | config.SUBTITLE_EXTENSIONS)
    elif kind == "att":
        allowed = config.ATTACHMENT_EXTENSIONS
    else:
        allowed = config.VIDEO_EXTENSIONS

    dirs, files = [], []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            rel = config.rel_input(entry)
            if rel is None:
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "rel": rel})
            elif entry.suffix.lower() in allowed:
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

    rel_here = config.rel_input(target) or ""
    # Elternpfad: leer, wenn wir auf einem Root-Top stehen (dann zur Root-Liste
    # bei Multi-Media bzw. zum Root-Inhalt bei Einzel-Root).
    is_root_top = any(target.resolve() == b.resolve() for _, b in config.MEDIA_ROOTS)
    if is_root_top:
        # Multi-Root: von einem Root-Top zurück zur virtuellen Wurzel ("").
        parent = "" if config.MULTI_MEDIA else None
    else:
        parent = config.rel_input(target.parent)
    return {
        "path": rel_here,
        "parent": parent,
        "is_root": is_root_top and not config.MULTI_MEDIA,
        "dirs": dirs,
        "files": files,
    }


@app.get("/api/browse-output")
async def browse_output(root: str = "", path: str = ""):
    """Ordner-Browser im Medienbaum (gleich /api/browse).

    `root` wird ignoriert; `path` ist media-relativ.
    """
    return await browse_input(path=path, kind="video")


@app.get("/api/search")
async def search_input(path: str = "", q: str = "", limit: int = 500,
                       kind: str = "video"):
    """Rekursive Namenssuche ab `path`. Für große Bibliotheken.

    `kind=aux` sucht zusätzlich Ton-/Untertiteldateien (für den Remux-Picker),
    sonst nur Videos. Liefert Treffer inkl. relativem Unterordner. Begrenzt auf
    `limit` Ergebnisse, um die Payload beherrschbar zu halten.
    """
    query = (q or "").strip().lower()
    if not query:
        return {"files": [], "truncated": False}
    if kind == "aux":
        allowed = (config.VIDEO_EXTENSIONS | config.AUDIO_EXTENSIONS
                   | config.SUBTITLE_EXTENSIONS)
    elif kind == "att":
        allowed = config.ATTACHMENT_EXTENSIONS
    else:
        allowed = config.VIDEO_EXTENSIONS
    # Leerer Pfad = über alle Roots suchen; sonst genau dieser (root-aware) Ordner.
    if path:
        target = _safe_resolve(path)
        if target is None or not target.is_dir():
            return JSONResponse({"error": "Kein Verzeichnis"}, status_code=400)
        search_dirs = [target]
    else:
        search_dirs = config.scan_targets("")

    files: list[dict] = []
    truncated = False
    for start in search_dirs:
        for root, dirnames, filenames in os.walk(start):
            # Versteckte Ordner (.archiv, .previews …) überspringen.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames, key=str.lower):
                if fn.startswith(".") or Path(fn).suffix.lower() not in allowed:
                    continue
                if query not in fn.lower():
                    continue
                entry = Path(root) / fn
                rel = config.rel_input(entry)
                if rel is None:
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
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
    out_mode: str = "default"        # default | beside | custom
    out_subdir: str = ""             # bei custom: media-relativer Zielordner
    name_pattern: str = "{stem}{suffix}"
    on_duplicate: str = "ask"        # ask | skip | overwrite
    max_output_mb: float = 0
    max_video_bitrate_kbps: int = 0
    size_target_mb: float = 0        # Encode-Ziel Gesamtgröße inkl. Ton (0 = aus)
    suffix: str = "_av1"


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


class RemuxEnqueueRequest(BaseModel):
    path: str
    spec: dict = {}                  # Edit-Spec (audio/subtitles/external/…)
    container: str = "mkv"           # mkv | mp4
    post_processing: str = "keep"    # keep | inplace | archive
    safe_replace: bool = True
    integrity_check: bool = True
    suffix: str = "_remux"
    name_pattern: str = "{stem}{suffix}"
    on_duplicate: str = "ask"
    out_mode: str = "default"
    out_subdir: str = ""


@app.get("/api/remux/probe")
async def remux_probe(path: str):
    """Ton-/Untertitel-Streams einer (auch reinen Audio-/Untertitel-)Datei listen."""
    target = _safe_resolve(path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    data, err = ff.probe_streams(target)
    if data is None:
        return JSONResponse({"error": f"ffprobe: {err or 'unbekannt'}"}, status_code=500)
    data["name"] = target.name
    return data


@app.post("/api/remux/upload")
async def remux_upload(file: UploadFile = File(...)):
    """Externe Ton-/Untertiteldatei vom PC hochladen und ihre Spuren listen.

    Die Datei wird im Upload-Ordner gespeichert; der zurückgegebene Pfad
    (``upload:<name>``) kann wie eine externe Spur weiterverwendet werden.
    """
    import uuid as _uuid

    orig = Path(file.filename or "upload").name
    suffix = Path(orig).suffix.lower()
    stem = Path(orig).stem[:60] or "track"
    safe_stem = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)
    stored = f"{safe_stem}_{_uuid.uuid4().hex[:8]}{suffix}"
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOAD_DIR / stored
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except OSError as e:
        return JSONResponse({"error": f"Upload fehlgeschlagen: {e}"}, status_code=500)
    finally:
        await file.close()

    data, err = ff.probe_streams(dest)
    if data is None:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": f"ffprobe: {err or 'unbekannt'}"}, status_code=400)
    if not (data.get("audio") or data.get("subtitles")):
        dest.unlink(missing_ok=True)
        return JSONResponse(
            {"error": f"{orig}: keine Ton-/Untertitelspuren gefunden."},
            status_code=400)
    data["name"] = orig
    data["path"] = f"upload:{stored}"
    return data


@app.get("/api/remux/chapters")
async def remux_chapters(path: str):
    """Kapitel einer Datei (Start/Ende/Titel) für den Kapitel-Editor."""
    target = _safe_resolve(path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    from core import remux
    return {"chapters": remux.probe_chapters(target)}


class RemuxExtractRequest(BaseModel):
    path: str
    tracks: list[dict] = []          # je Spur: type (audio|subtitle), index
    out_mode: str = "default"
    out_subdir: str = ""


@app.post("/api/remux/extract")
async def remux_extract(req: RemuxExtractRequest):
    """Ausgewählte Spuren als eigene Dateien extrahieren (Stream-Copy)."""
    import subprocess
    from core import remux
    target = _safe_resolve(req.path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    if not req.tracks:
        return JSONResponse({"error": "Keine Spuren gewählt"}, status_code=400)
    info, err = ff.probe_with_error(target)
    if info is None:
        return JSONResponse({"error": f"ffprobe: {err or 'unbekannt'}"}, status_code=500)

    out_dir = config.resolve_out_dir(target, req.out_mode, req.out_subdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results, errors = [], []
    for cmd, out_path in remux.build_extract_cmds(info, out_dir, req.tracks):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=1800, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(str(e))
            continue
        if proc.returncode == 0 and out_path.exists():
            results.append({"name": out_path.name,
                            "size_human": ff.human_size(out_path.stat().st_size)})
        else:
            errors.append((proc.stderr or "")[-300:] or f"Exit {proc.returncode}")
    return {"extracted": results, "errors": errors}


class RemuxConcatRequest(BaseModel):
    paths: list[str] = []            # geordnete Liste (rel) der zu verbindenden Dateien
    container: str = "mkv"
    suffix: str = "_merged"
    post_processing: str = "keep"
    chapters_at_joins: bool = False  # Kapitelmarke an jeder Verbindungsstelle
    unify: bool = False              # inkompatible Dateien per Re-Encode vereinheitlichen
    platform: str = "cpu"            # Re-Encode: Plattform/Codec/CQ
    codec: str = "av1"
    cq: int = 30
    out_mode: str = "default"
    out_subdir: str = ""


class RemuxConcatCheckRequest(BaseModel):
    paths: list[str] = []


@app.post("/api/remux/concat/check")
async def remux_concat_check(req: RemuxConcatCheckRequest):
    """Kompatibilität der zu verbindenden Dateien prüfen (Codec/Auflösung/…)."""
    from core import remux
    files = []
    for p in req.paths or []:
        t = _safe_resolve(p)
        if t is None or not t.is_file():
            return JSONResponse({"error": f"Datei nicht gefunden: {p}"}, status_code=404)
        files.append(str(t))
    if len(files) < 2:
        return JSONResponse({"error": "Mindestens zwei Dateien wählen"}, status_code=400)
    return remux.concat_compat(files)


@app.post("/api/remux/concat")
async def remux_concat(req: RemuxConcatRequest):
    """Mehrere Dateien zusammenführen (concat; optional Kapitel/Re-Encode)."""
    from core.queue_manager import build_job_settings
    if len(req.paths or []) < 2:
        return JSONResponse({"error": "Mindestens zwei Dateien wählen"}, status_code=400)
    first = _safe_resolve(req.paths[0])
    if first is None or not first.is_file():
        return JSONResponse({"error": "Erste Datei nicht gefunden"}, status_code=404)
    container = req.container if req.container in ("mkv", "mp4") else "mkv"
    # Auch der Re-Encode-Merge läuft über den concat-Prozess (multi-input).
    d = {
        "video_mode": "concat", "vmaf_check": False, "workflow": "auto",
        "container": container, "post_processing": req.post_processing,
        "suffix": req.suffix or "_merged", "integrity_check": True,
        "edit_spec": {
            "concat_files": list(req.paths), "container": container,
            "chapters_at_joins": bool(req.chapters_at_joins),
            "unify": bool(req.unify), "platform": req.platform,
            "codec": req.codec, "cq": req.cq,
        },
        "out_mode": req.out_mode, "out_subdir": req.out_subdir,
    }
    item = queue.add_file(str(first), build_job_settings(d))
    if item is None:
        return JSONResponse({"error": "Auftrag nicht hinzugefügt"}, status_code=400)
    return {"added": 1, "id": item.id}


class RemuxSplitRequest(BaseModel):
    path: str
    mode: str = "chapters"           # chapters | duration | parts | times | size | range
    value: float = 0                 # Sekunden (duration) · Teile (parts) · MB (size)
    times: list[str] = []            # Zeitmarken bei mode=times (Sek. oder HH:MM:SS)
    ranges: list[dict] = []          # [{start,end,title?}] bei mode=range (Ausschnitt)
    container: str = "mkv"
    suffix: str = "_part"
    out_mode: str = "default"
    out_subdir: str = ""


@app.post("/api/remux/split")
async def remux_split(req: RemuxSplitRequest):
    """Datei verlustfrei splitten oder Ausschnitte exportieren (kein Re-Encode)."""
    from core.queue_manager import build_job_settings
    target = _safe_resolve(req.path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    container = req.container if req.container in ("mkv", "mp4") else "mkv"
    d = {
        "video_mode": "split", "vmaf_check": False, "workflow": "auto",
        "container": container, "post_processing": "keep",
        "suffix": req.suffix or "_part",
        "edit_spec": {"split_mode": req.mode, "split_value": req.value,
                      "split_times": list(req.times or []),
                      "split_ranges": list(req.ranges or []),
                      "container": container},
        "out_mode": req.out_mode, "out_subdir": req.out_subdir,
    }
    item = queue.add_file(str(target), build_job_settings(d))
    if item is None:
        return JSONResponse({"error": "Auftrag nicht hinzugefügt"}, status_code=400)
    return {"added": 1, "id": item.id}


class RemuxCutRequest(BaseModel):
    path: str
    start: str = "0"
    end: str = ""
    container: str = "mkv"


@app.post("/api/remux/cut")
def remux_cut(req: RemuxCutRequest):
    """Einen Ausschnitt verlustfrei schneiden und direkt als Download liefern.

    Läuft synchron (im Threadpool, da normale def-Route); das Ergebnis landet in
    einem temporären Arbeitsverzeichnis und wird nach der Auslieferung gelöscht.
    """
    import subprocess
    import uuid as _uuid
    from starlette.background import BackgroundTask
    from core import remux

    target = _safe_resolve(req.path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    container = req.container if req.container in ("mkv", "mp4") else "mkv"
    ext = "." + container
    dl_dir = config.WORK_DIR / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    out = dl_dir / f"cut_{_uuid.uuid4().hex[:10]}{ext}"
    cmd, err = remux.build_single_cut_cmd(target, out, req.start, req.end)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        out.unlink(missing_ok=True)
        return JSONResponse({"error": f"FFmpeg-Fehler: {e}"}, status_code=500)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        tail = (proc.stderr or "")[-800:]
        return JSONResponse({"error": f"Schnitt fehlgeschlagen: {tail}"}, status_code=500)
    dl_name = f"{target.stem}_cut{ext}"
    return FileResponse(
        out, filename=dl_name, media_type="application/octet-stream",
        background=BackgroundTask(lambda: out.unlink(missing_ok=True)))


# ---------------------------------------------------------------- Video-Editor
class EditorEnqueueRequest(BaseModel):
    segments: list[dict] = []
    mode: str = "remux"              # remux | encode
    container: str = "mkv"
    suffix: str = "_edit"
    chapters_from_cuts: bool = True
    force_remux: bool = False        # Kompatibilitätswarnung ignorieren
    platform: str = "cpu"
    codec: str = "av1"
    cq: int = 30
    audio_codec: str = "aac"
    audio_bitrate: int = 192
    burn_subs: bool = False
    sub_index: int = -1
    out_mode: str = "default"
    out_subdir: str = ""
    post_processing: str = "keep"


class EditorCheckRequest(BaseModel):
    segments: list[dict] = []


@app.post("/api/editor/upload")
async def editor_upload(
    file: UploadFile = File(...),
    dest: str = Form(""),
):
    """Video für den Editor hochladen.

    ``dest`` leer / ``upload`` → Standard-Upload-Ordner (``upload:<name>``).
    Sonst media-relativer Ordner (Datei landet im Medienbaum).
    """
    import uuid as _uuid

    orig = Path(file.filename or "upload").name
    suffix = Path(orig).suffix.lower()
    if suffix not in config.VIDEO_EXTENSIONS:
        return JSONResponse(
            {"error": f"Kein unterstütztes Video-Format ({suffix or 'ohne Endung'})."},
            status_code=400)
    stem = Path(orig).stem[:60] or "video"
    safe_stem = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)
    stored = f"{safe_stem}_{_uuid.uuid4().hex[:8]}{suffix}"

    dest_key = (dest or "").strip()
    use_upload = not dest_key or dest_key.lower() in ("upload", "uploads", ".")
    api_path = ""
    if use_upload:
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        out_path = config.UPLOAD_DIR / stored
        api_path = f"upload:{stored}"
    else:
        folder = config.resolve_input(dest_key)
        if folder is None:
            return JSONResponse({"error": f"Zielordner ungültig: {dest_key}"},
                                status_code=400)
        if folder.is_file():
            folder = folder.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return JSONResponse({"error": f"Zielordner nicht beschreibbar: {e}"},
                                status_code=400)
        if not folder.is_dir():
            return JSONResponse({"error": "Ziel ist kein Ordner"}, status_code=400)
        out_path = folder / stored
        rel = config.rel_input(out_path)
        if not rel:
            return JSONResponse({"error": "Ziel außerhalb des Medienbaums"},
                                status_code=400)
        api_path = rel

    try:
        with out_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except OSError as e:
        out_path.unlink(missing_ok=True)
        return JSONResponse({"error": f"Upload fehlgeschlagen: {e}"}, status_code=500)
    finally:
        await file.close()

    from core import editor
    data, err = editor.probe_source(api_path)
    if data is None:
        out_path.unlink(missing_ok=True)
        return JSONResponse({"error": err or "Analyse fehlgeschlagen"}, status_code=400)
    if not data.get("has_video"):
        out_path.unlink(missing_ok=True)
        return JSONResponse({"error": f"{orig}: keine Videospur gefunden."}, status_code=400)
    data["name"] = orig
    data["path"] = api_path
    data["dest"] = "upload" if use_upload else dest_key
    return data


@app.get("/api/editor/probe")
async def editor_probe(path: str):
    """Quelle für den Editor analysieren (Medienpfad oder upload:…)."""
    from core import editor
    data, err = editor.probe_source(path)
    if data is None:
        return JSONResponse({"error": err or "Nicht gefunden"}, status_code=404)
    return data


@app.post("/api/editor/check")
async def editor_check(req: EditorCheckRequest):
    """Kompatibilität der Segmente für Remux-Export prüfen."""
    from core import editor
    segs, err = editor.normalize_segments(req.segments or [])
    if err:
        return JSONResponse({"error": err}, status_code=400)
    compat = editor.check_remux_compat(segs)
    compat["duration"] = editor.total_duration(segs)
    compat["chapters"] = editor.chapters_from_segments(segs)
    return compat


@app.post("/api/editor/enqueue")
async def editor_enqueue(req: EditorEnqueueRequest):
    """Editor-Projekt in die Warteschlange legen (Remux oder Encode)."""
    from core import editor
    from core.queue_manager import build_job_settings

    segs, err = editor.normalize_segments(req.segments or [])
    if err:
        return JSONResponse({"error": err}, status_code=400)
    mode = (req.mode or "remux").lower()
    if mode not in ("remux", "encode"):
        mode = "remux"
    container = req.container if req.container in ("mkv", "mp4") else "mkv"
    first = segs[0]["abs"]
    # Persistierte Segmente ohne abs-Pfad (Queue-JSON).
    clean_segs = [{
        "path": s["path"], "start": s["start"], "end": s["end"],
        "title": s["title"], "audio_index": s["audio_index"], "mute": s["mute"],
    } for s in segs]
    d = {
        "video_mode": "editor",
        "vmaf_check": False,
        "workflow": "auto",
        "container": container,
        "post_processing": req.post_processing or "keep",
        "suffix": req.suffix or "_edit",
        "integrity_check": True,
        "platform": req.platform or "cpu",
        "codec": req.codec or "av1",
        "quality": int(req.cq or 30),
        "rate_mode": "cq",
        "audio_mode": "encode" if mode == "encode" else "copy",
        "audio_codec": req.audio_codec or "aac",
        "audio_bitrate": int(req.audio_bitrate or 192),
        "edit_spec": {
            "segments": clean_segs,
            "mode": mode,
            "container": container,
            "chapters_from_cuts": bool(req.chapters_from_cuts),
            "force_remux": bool(req.force_remux),
            "platform": req.platform or "cpu",
            "codec": req.codec or "av1",
            "cq": int(req.cq or 30),
            "audio_codec": req.audio_codec or "aac",
            "audio_bitrate": int(req.audio_bitrate or 192),
            "burn_subs": bool(req.burn_subs),
            "sub_index": int(req.sub_index if req.sub_index is not None else -1),
        },
        "out_mode": req.out_mode,
        "out_subdir": req.out_subdir,
    }
    item = queue.add_file(str(first), build_job_settings(d))
    if item is None:
        return JSONResponse({"error": "Auftrag nicht hinzugefügt"}, status_code=400)
    # Titel aussagekräftiger machen
    item.title = f"Editor · {len(segs)} Clip(s) · {first.name}"
    queue._persist()
    return {
        "added": 1, "id": item.id,
        "duration": editor.total_duration(segs),
        "mode": mode,
    }


@app.post("/api/remux/enqueue")
async def remux_enqueue(req: RemuxEnqueueRequest):
    """Remux-/Bearbeiten-Job einreihen (kein Video-Re-Encode)."""
    target = _safe_resolve(req.path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)

    from core.queue_manager import build_job_settings
    container = req.container if req.container in ("mkv", "mp4") else "mkv"
    spec = dict(req.spec or {})
    spec["container"] = container
    d = {
        "video_mode": "edit",
        "remux_only": True,
        "vmaf_check": False,
        "workflow": "auto",
        "container": container,
        "post_processing": req.post_processing,
        "safe_replace": req.safe_replace,
        "integrity_check": req.integrity_check,
        "suffix": req.suffix or "_remux",
        "name_pattern": req.name_pattern or "{stem}{suffix}",
        "on_duplicate": req.on_duplicate or "ask",
        "edit_spec": spec,
        "out_mode": req.out_mode,
        "out_subdir": req.out_subdir,
    }
    settings = build_job_settings(d)
    item = queue.add_file(str(target), settings)
    if item is None:
        return JSONResponse({"error": "Datei konnte nicht hinzugefügt werden"},
                            status_code=400)
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
    return {"profiles": profiles.ensure_builtins()}


@app.post("/api/profiles")
async def save_profile(req: ProfileRequest):
    from core import profiles
    return {"profiles": profiles.save_profile(req.name, req.settings)}


@app.delete("/api/profiles/{name}")
async def delete_profile(name: str):
    from core import profiles
    return {"profiles": profiles.delete(name)}


class RequeueRequest(BaseModel):
    # overwrite = gleiches Ziel ersetzen; suffix = neuer Name (_remux2, …)
    mode: str = "overwrite"


@app.post("/api/queue/{item_id}/requeue")
async def requeue_item(item_id: str, req: RequeueRequest | None = None):
    """Live-Job mit denselben Settings erneut einreihen."""
    from core import job_plan
    from core.queue_manager import build_job_settings
    item = queue.get_item(item_id)
    if item is None:
        return JSONResponse({"error": "Auftrag nicht gefunden"}, status_code=404)
    mode = (req.mode if req else "overwrite")
    d, plan = job_plan.apply_requeue_conflict(
        item.settings.__dict__, item.path, mode)
    new = queue.add_file(item.path, build_job_settings(d))
    if new is None:
        return JSONResponse({"error": "Konnte nicht erneut eingereiht werden"}, status_code=400)
    return {"added": 1, "id": new.id, "output_name": plan.get("output_name"),
            "conflict_mode": plan.get("conflict_mode"), "suffix": d.get("suffix")}


@app.post("/api/history/{item_id}/requeue")
async def requeue_history(item_id: str, req: RequeueRequest | None = None):
    """Historien-Job erneut einreihen (benötigt settings_json)."""
    import json
    from core import history, job_plan
    from core.queue_manager import build_job_settings
    rec = history.get(item_id)
    if not rec:
        return JSONResponse({"error": "Nicht in Historie"}, status_code=404)
    raw = rec.get("settings_json") or "{}"
    try:
        d = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        d = {}
    if not d:
        return JSONResponse({"error": "Keine Settings gespeichert (älterer Eintrag)"},
                            status_code=400)
    path = rec.get("path") or ""
    target = Path(path)
    if not target.is_file():
        return JSONResponse({"error": "Quelldatei nicht mehr vorhanden"}, status_code=404)
    mode = (req.mode if req else "overwrite")
    d, plan = job_plan.apply_requeue_conflict(d, str(target), mode)
    new = queue.add_file(str(target), build_job_settings(d))
    if new is None:
        return JSONResponse({"error": "Konnte nicht erneut eingereiht werden"}, status_code=400)
    return {"added": 1, "id": new.id, "output_name": plan.get("output_name"),
            "conflict_mode": plan.get("conflict_mode"), "suffix": d.get("suffix")}


@app.get("/api/vmaf/by-source")
async def vmaf_by_source(path: str = ""):
    """VMAF-/Encode-Historie zu einer Quelldatei (Sessions + Jobs)."""
    from core import history, vmaf
    if not path:
        return {"jobs": [], "sessions": []}
    target = _safe_resolve(path)
    abs_path = str(target) if target else path
    return {
        "jobs": history.by_source(abs_path),
        "sessions": vmaf.sessions_for_source(abs_path),
        "path": abs_path,
    }


@app.post("/api/remux/smart-disposition")
async def remux_smart_disposition(req: dict):
    """Default/Forced-Heuristik auf übergebene Spurlisten anwenden."""
    from core import remux
    audio = list(req.get("audio") or [])
    subs = list(req.get("subtitles") or [])
    langs = req.get("prefer_langs") or []
    prefer = {str(x).lower() for x in langs if str(x).strip()}
    remux.apply_smart_disposition(audio, subs, prefer)
    return {"audio": audio, "subtitles": subs}


@app.post("/api/remux/import-chapters")
async def remux_import_chapters(req: dict):
    """Kapitel aus NFO/ffmetadata/Textdatei importieren."""
    from core import remux
    path = req.get("path") or ""
    target = _safe_resolve(path) if path else None
    if target is None or not target.is_file():
        p = Path(path)
        if p.is_file() and config.rel_input(p) is not None:
            target = p
        else:
            return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    chapters = remux.parse_chapters_file(target)
    return {"chapters": chapters, "count": len(chapters)}


@app.get("/api/remux/sidecar-attachments")
async def remux_sidecar_attachments(path: str = ""):
    """Fonts/Cover neben einer Videodatei auflisten."""
    from core import remux
    target = _safe_resolve(path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    return {"attachments": remux.find_sidecar_attachments(target)}


class LibraryScanRequest(BaseModel):
    root: str = ""
    extensions: list[str] = []       # z. B. ["mkv","mp4"] – leer = alle
    name_contains: str = ""
    name_exclude: list[str] = []
    min_size_mb: float = 0
    min_bitrate_mbps: float = 0
    min_height: int = 0
    codecs_include: list[str] = []
    codecs_exclude: list[str] = []
    target_codec: str = "av1"        # Ziel für die Einspar-Projektion
    dynamic_filter: str = ""         # (alt) einzelner Dynamik-Filter
    dynamic_filters: list[str] = []  # Mehrfach-Auswahl: sdr|hdr|dv|dv5|dv7|dv8
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


@app.post("/api/library/scan/cancel")
async def library_scan_cancel():
    """Laufenden Bibliotheks-Scan abbrechen."""
    from core import library
    cancelled = library.cancel_scan()
    return {"cancelled": cancelled, "state": library.get_state()}


@app.post("/api/library/clear")
async def library_clear(root: str | None = None):
    """Cache leeren: optional nur einen Root, sonst alle (nur wenn kein Scan läuft)."""
    from core import library
    return library.clear(root)


@app.get("/api/library/last")
async def library_last(root: str | None = None):
    """Gecachte Scans laden.

    Ohne ``root``: alle Caches (`by_root`). Mit ``root``: Snapshot dieser Bibliothek
    (leere Liste, falls noch nie gescannt).
    """
    from core import library
    return library.load_last(root)


@app.get("/api/library/cached")
async def library_cached(root: str = ""):
    """Gescannten Stand einer Bibliothek (sofort, ohne Rescan)."""
    from core import library
    return library.get_cached(root)


@app.get("/api/library/export.csv")
async def library_export_csv(root: str | None = None):
    """Treffer als CSV herunterladen (optional für einen Root)."""
    from fastapi.responses import Response
    from core import library
    csv_text = library.export_csv(root)
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


@app.post("/api/supertool/scan/cancel")
async def supertool_scan_cancel():
    """Laufenden Super-Tool-Scan abbrechen."""
    from core import supertool
    cancelled = supertool.cancel_scan()
    return {"cancelled": cancelled, "state": supertool.get_state()}


@app.post("/api/supertool/list")
async def supertool_list(req: SuperScanRequest):
    """Schnelle Datei-Vorschau (ohne Probe) für die Live-Liste neben der Ordnerwahl."""
    from core import supertool
    return supertool.quick_list(req.model_dump())


class SuperStartRequest(BaseModel):
    paths: list[str] = []
    mode: str = "representative"      # target_vmaf | representative | fixed
    settings: dict = {}
    per_file: dict = {}              # rel-Pfad -> {audio_tracks, subtitle_tracks}
    dry_run: bool = False


@app.post("/api/supertool/start")
async def supertool_start(req: SuperStartRequest):
    from core import supertool
    if not req.paths:
        return JSONResponse({"error": "Keine Dateien ausgewählt"}, status_code=400)
    result = supertool.start_batch(
        queue, req.paths, req.settings or {}, req.mode, req.per_file or {},
        dry_run=req.dry_run)
    if req.dry_run:
        _a, _g, _e, preview = result
        return {"dry_run": True, "preview": preview}
    added, group_id, err = result
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"added": added, "group_id": group_id}


class PreviewRequest(BaseModel):
    paths: list[str] = []
    settings: dict = {}
    estimates: dict = {}


@app.post("/api/preview")
async def preview_jobs(req: PreviewRequest):
    """Dry-Run: geplante Ausgaben ohne Einreihen."""
    from core import job_plan
    if not req.paths:
        return JSONResponse({"error": "Keine Dateien"}, status_code=400)
    return job_plan.preview_batch(req.paths, req.settings or {}, req.estimates or {})


class SizeTargetPreviewRequest(BaseModel):
    path: str = ""
    size_target_mb: float = 0
    audio_tracks: list = []
    audio_mode: str = "copy"
    audio_languages: str = ""


@app.post("/api/size-target/preview")
async def size_target_preview(req: SizeTargetPreviewRequest):
    """Berechnete Video-Bitrate für ein Größenziel (inkl. Tonspuren)."""
    from core import size_target
    from types import SimpleNamespace
    target = _safe_resolve(req.path) if req.path else None
    if target is None or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"}, status_code=404)
    info, err = ff.probe_with_error(target)
    if info is None:
        return JSONResponse({"error": f"ffprobe: {err or 'unbekannt'}"}, status_code=500)
    settings = SimpleNamespace(
        audio_tracks=list(req.audio_tracks or []),
        audio_mode=req.audio_mode or "copy",
        audio_languages=req.audio_languages or "",
    )
    tracks = size_target.select_audio_tracks(info, settings)
    res = size_target.compute_video_bitrate_kbps(
        size_target_mb=req.size_target_mb,
        duration=float(info.duration or 0),
        audio_tracks=tracks,
    )
    return {**res, "audio_tracks": len(tracks), "duration": info.duration}


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
    """Pfad (relativ oder absolut) sicher innerhalb eines Input-Roots auflösen."""
    from pathlib import Path
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        try:
            rp = p.resolve()
        except OSError:
            return None
        for _, base in config.MEDIA_ROOTS:
            if config._within(rp, base.resolve()):
                return rp if rp.exists() else None
        return None
    target = config.resolve_input(raw)
    return target if (target and target.exists()) else None


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


class AppSettingsRequest(BaseModel):
    default_output: str = "output"


@app.get("/api/settings")
async def get_app_settings():
    from core import app_settings
    cfg = app_settings.load()
    out_abs = config.default_output_path()
    return {
        **cfg,
        "default_output_abs": str(out_abs),
        "media_dir": str(config.MEDIA_DIR),
        "media_roots": config.media_roots_public(),
    }


@app.post("/api/settings")
async def set_app_settings(req: AppSettingsRequest):
    from core import app_settings
    rel = config.safe_subdir(req.default_output)
    if rel:
        # Erlaubter Pfad unter Media-Roots (auch wenn Ordner noch fehlt).
        probe = config.resolve_input(rel)
        if probe is None and config.MULTI_MEDIA:
            # Bei Multi-Root muss das erste Segment ein Root-Name sein.
            first = rel.split("/", 1)[0]
            if first not in {n for n, _ in config.MEDIA_ROOTS}:
                return JSONResponse(
                    {"error": "Pfad liegt außerhalb der Media-Roots"},
                    status_code=400)
        elif probe is None and not config.MULTI_MEDIA:
            # Einzel-Root: relativen Pfad immer akzeptieren und anlegen.
            pass
    cfg = app_settings.save({"default_output": rel or "output"})
    try:
        config.default_output_path().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return {**cfg, "default_output_abs": str(config.default_output_path())}


class LibraryDefRequest(BaseModel):
    name: str = ""
    path: str = ""


@app.get("/api/libraries")
async def libraries_list():
    from core import app_settings
    return {"libraries": app_settings.list_libraries()}


@app.post("/api/libraries")
async def libraries_add(req: LibraryDefRequest):
    from core import app_settings
    lib, err = app_settings.add_library(req.name, req.path)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"library": lib, "libraries": app_settings.list_libraries()}


@app.put("/api/libraries/{lib_id}")
async def libraries_update(lib_id: str, req: LibraryDefRequest):
    from core import app_settings
    # Nur gesetzte Felder aktualisieren (leerer name im Body = nicht ändern).
    name = req.name if req.name.strip() else None
    # path: immer übernehmen wenn im Request (auch leer = gesamter Baum)
    lib, err = app_settings.update_library(lib_id, name=name, path=req.path)
    if err:
        code = 404 if err == "Nicht gefunden" else 400
        return JSONResponse({"error": err}, status_code=code)
    return {"library": lib, "libraries": app_settings.list_libraries()}


@app.delete("/api/libraries/{lib_id}")
async def libraries_delete(lib_id: str):
    from core import app_settings
    ok, err = app_settings.delete_library(lib_id)
    if not ok:
        return JSONResponse({"error": err}, status_code=404)
    return {"libraries": app_settings.list_libraries()}


def _resolve_media_root(path: str, root: str = "media"):
    """Datei im Medienbaum sicher auflösen (`root` nur noch für API-Kompatibilität)."""
    return config.resolve_input(path)


@app.get("/api/media")
async def media(path: str, root: str = "media"):
    """Streamt eine Video-Datei (mit Range-Support) für den A/B-Vergleichsplayer."""
    target = _resolve_media_root(path, root)
    if target is None:
        return JSONResponse({"error": "Ungültiger Pfad"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    return FileResponse(target)


@app.get("/api/media/stream")
async def media_stream(
    path: str,
    root: str = "media",
    audio: int = 0,
    start: float = 0.0,
    acodec: str = "",
):
    """Live-Playback: Video copy + gewählte Tonspur als AAC (fragmentiertes MP4).

    ``audio=-1`` = ohne Ton. ``start`` = Seek in Sekunden (Keyframe).
    ``acodec`` = Quell-Codec der Tonspur (für Copy statt Re-Encode, z. B. aac).
    """
    from fastapi.responses import StreamingResponse
    from core import media_stream as ms

    target = _resolve_media_root(path, root)
    if target is None:
        return JSONResponse({"error": "Ungültiger Pfad"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    aidx = None if audio < 0 else int(audio)
    cmd = ms.build_play_cmd(
        target, aidx, start_sec=max(0.0, float(start or 0.0)),
        audio_codec=acodec or "",
    )
    return StreamingResponse(
        ms.stream_bytes(cmd),
        media_type="video/mp4",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------- Full Player (HLS)
class PlayerSessionRequest(BaseModel):
    path: str
    audio: int = 0
    subtitle: int = -1
    start: float = 0.0
    profile: str = "auto"
    burn_subs: bool = False
    client_direct_ok: bool = False
    platform: str = "auto"          # auto|nvidia|intel|amd|cpu
    codec: str = "h264"             # h264|hevc|av1 (Browser muss können)
    height: int = 0                 # custom Höhe (bei profile=custom)
    v_bitrate: int = 0              # custom kbit/s
    client_codecs: list[str] = []   # was der Browser abspielen kann
    lookahead_sec: float = 30       # Zielpuffer; 0 = Encode nicht drosseln
    audio_copy: bool = False        # Ton nicht umcodieren (Stream-Copy)


@app.get("/api/player/options")
async def player_options():
    """Verfügbare Player-Profile, Plattformen, Codecs (Capabilities)."""
    from core import player_hls
    return player_hls.player_options()


@app.post("/api/player/session")
async def player_session_start(req: PlayerSessionRequest):
    """Direct-Play oder HLS-Session (HW/SW-Transcode, Qualitätsstufen)."""
    from core import player_hls
    player_hls.cleanup_idle()
    return player_hls.start_session(
        req.path, audio_index=req.audio, subtitle_index=req.subtitle,
        start_sec=req.start, profile=req.profile, burn_subs=req.burn_subs,
        client_direct_ok=req.client_direct_ok, platform=req.platform,
        codec=req.codec, height=req.height, v_bitrate=req.v_bitrate,
        client_codecs=req.client_codecs or None,
        lookahead_sec=req.lookahead_sec,
        audio_copy=bool(req.audio_copy),
    )


@app.get("/api/player/session/{sid}")
async def player_session_get(sid: str):
    from core import player_hls
    sess = player_hls.get_session(sid)
    if not sess:
        return JSONResponse({"error": "Session nicht gefunden"}, status_code=404)
    return {"session": sess.to_dict()}


@app.post("/api/player/session/{sid}/pause")
async def player_session_pause(sid: str):
    """Encode bei Player-Pause anhalten (SIGSTOP) – senkt CPU/GPU-Last."""
    from core import player_hls
    return player_hls.pause_encode(sid)


@app.post("/api/player/session/{sid}/resume")
async def player_session_resume(sid: str):
    """Encode nach Pause fortsetzen (SIGCONT)."""
    from core import player_hls
    return player_hls.resume_encode(sid)


@app.delete("/api/player/session/{sid}")
async def player_session_stop(sid: str):
    from core import player_hls
    return {"ok": player_hls.stop_session(sid)}


@app.get("/api/player/session/{sid}/{name}")
async def player_session_file(sid: str, name: str):
    """Playlist oder Segment einer HLS-Session ausliefern."""
    from core import player_hls
    target = player_hls.resolve_session_file(sid, name)
    if target is None:
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    media = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else (
        "video/mp4" if name.endswith((".mp4", ".m4s")) else "application/octet-stream"
    )
    return FileResponse(
        target, media_type=media,
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/api/media/vtt")
async def media_vtt(path: str, root: str = "media", subtitle: int = 0):
    """Text-Untertitelspur als WebVTT für den HTML5-Player."""
    from fastapi.responses import StreamingResponse
    from core import media_stream as ms

    target = _resolve_media_root(path, root)
    if target is None:
        return JSONResponse({"error": "Ungültiger Pfad"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    cmd = ms.build_vtt_cmd(target, int(subtitle))
    return StreamingResponse(
        ms.stream_bytes(cmd),
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/ffprobe")
async def ffprobe_any(path: str, root: str = "media"):
    """ffprobe für eine Datei im Medienbaum (Detail-Ansicht)."""
    target = _resolve_media_root(path, root)
    if target is None:
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


def _details_from_history(rec: dict) -> dict:
    """Detail-Antwort aus einem Historien-Datensatz (Job nicht mehr in Queue)."""
    from pathlib import Path
    src_abs = rec.get("path") or ""
    src = None
    if src_abs:
        p = Path(src_abs)
        rel = config.rel_input(p)
        src = {"name": p.name, "exists": p.is_file(), "media": None,
               "info": None, "root": "media", "rel": rel}
        if rel is not None and p.is_file():
            src["media"] = f"/api/media?root=media&path={quote(rel)}"
            info, _ = ff.probe_with_error(p)
            if info:
                src["info"] = info.to_dict()
    orig = int(rec.get("original_size") or 0)
    saved = int(rec.get("saved_bytes") or 0)
    dur = float(rec.get("duration") or 0)
    stats = {
        "duration": dur,
        "duration_human": ff.human_duration(dur) if dur else "—",
        "status": rec.get("status") or "—",
        "vmaf_verify": rec.get("vmaf"),
        "original_human": ff.human_size(orig) if orig else "—",
        "output_human": ff.human_size(int(rec.get("output_size") or 0)) if rec.get("output_size") else "—",
        "saved_human": ff.human_size(saved) if saved else "—",
        "savings_percent": (round(saved / orig * 100, 1) if orig and saved else None),
        "speed_x": None, "avg_fps": None,
    }
    out = None
    out_abs = rec.get("output_path") or ""
    if out_abs:
        op = Path(out_abs)
        orel = config.rel_input(op)
        out = {"name": op.name, "exists": op.is_file(), "media": None,
               "info": None, "root": "media", "rel": orel}
        if orel is not None and op.is_file():
            out["media"] = f"/api/media?root=media&path={quote(orel)}"
            info, _ = ff.probe_with_error(op)
            if info:
                out["info"] = info.to_dict()
    settings = {}
    raw = rec.get("settings_json") or ""
    if raw:
        import json as _json
        try:
            settings = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            settings = {}
    return {"id": rec.get("id"), "title": rec.get("title") or "Details",
            "status": rec.get("status") or "—", "path": src_abs,
            "from_history": True, "settings": settings,
            "source": src, "output": out, "stats": stats}


@app.get("/api/queue/{item_id}/details")
async def queue_details(item_id: str):
    """Detail-Ansicht eines (auch abgeschlossenen) Auftrags: ffprobe von Quelle
    und Ausgabe, Medien-URLs für den Player sowie Encode-Kennzahlen."""
    item = queue.get_item(item_id)
    if item is None:
        # Fallback: nicht mehr in der Warteschlange (z. B. nach Neustart) →
        # aus der Historie rekonstruieren (Quelle + Kennzahlen, ohne Ausgabe).
        from core import history
        rec = history.get(item_id)
        if rec is None:
            return JSONResponse({"error": "Auftrag nicht gefunden"}, status_code=404)
        return _details_from_history(rec)

    def _pack(abs_path, root="media"):
        from pathlib import Path
        if not abs_path:
            return None
        p = Path(abs_path)
        rel = config.rel_input(p)
        entry = {"name": p.name, "exists": p.is_file(), "media": None,
                 "info": None, "root": root, "rel": rel}
        if rel is not None and p.is_file():
            entry["media"] = f"/api/media?root=media&path={quote(rel)}"
            info, _ = ff.probe_with_error(p)
            if info:
                entry["info"] = info.to_dict()
        return entry

    src = _pack(item.path)
    out = _pack(item.output_path)

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
        "path": item.path, "source": src, "output": out, "stats": stats,
        "settings": item.settings.__dict__ if item.settings else {},
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
    """Echte Encode-/Decode-Fähigkeiten (per Mini-Test). Leeres results/decode =
    noch nicht getestet -> UI fällt auf die Build-Verfügbarkeit zurück."""
    from core import capabilities as caps
    data = caps.get_cached()
    if data is None or "decode" not in (data or {}):
        # Test evtl. noch nicht durch / alter Cache ohne Decode -> anstoßen.
        caps.compute_async(monitor)
        if data is None:
            return {"results": {}, "decode": {}, "generated_at": 0, "pending": True}
    return data


@app.post("/api/capabilities/refresh")
async def capabilities_refresh():
    """Encode-/Decode-Fähigkeiten neu ermitteln (echte Mini-Tests)."""
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
