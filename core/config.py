"""Zentrale Konfiguration und Pfade."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _resolve_binary(name: str) -> str:
    """Bevorzugt unseren eigenen FFmpeg-Build in /usr/local/bin.

    Wichtig auf QNAP: Der Nvidia-Treiber bringt eigene (teils kaputte)
    ffmpeg/ffprobe-Binaries mit und steht oft vorne im PATH. Wir nutzen
    daher den absoluten Pfad zu unserem Build, statt uns auf PATH zu verlassen.
    """
    env_override = os.getenv(name.upper() + "_BIN")
    if env_override:
        return env_override
    local = Path("/usr/local/bin") / name
    if local.exists():
        return str(local)
    return shutil.which(name) or name


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Pfade zu den (eigenen) FFmpeg-Binaries – immun gegen PATH-Shadowing.
FFMPEG = _resolve_binary("ffmpeg")
FFPROBE = _resolve_binary("ffprobe")

# dovi_tool: für die (experimentelle) Dolby-Vision-RPU-Erhaltung bei HEVC.
DOVI_TOOL = _resolve_binary("dovi_tool")

# --- Optionaler Zugriffsschutz -----------------------------------------------
# Ist APP_PASSWORD gesetzt, verlangt die App einen Login. Ohne Variable läuft
# alles offen wie bisher (Standardverhalten).
import hashlib as _hashlib

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
AUTH_COOKIE = "vc_auth"


def auth_token() -> str:
    """Cookie-Token, das nur bei korrektem Passwort reproduzierbar ist."""
    return _hashlib.sha256(("vcompress:" + APP_PASSWORD).encode()).hexdigest()

# --- Medien-Volumes (Quelle / fertige Encodes) --------------------------------
INPUT_DIR = Path(os.getenv("INPUT_DIR", "/media/input"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/media/output"))

# --- Persistenter Datenordner (App-intern, per Docker-Volume mounten) ---------
# DATA_DIR ist die Wurzel für alles, was die App zwischen speichert:
#   work/      – kurzlebige Encode-Zwischendateien
#   previews/  – VMAF-Screenshots (Original vs. Test-Encode)
#   vmaf/      – optional aufbewahrte VMAF-Sessions (Referenz, Testclips, Logs)
#
# In Docker: Host-Ordner nach /data mounten (siehe docker-compose.yml).
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
WORK_DIR = Path(os.getenv("WORK_DIR", str(DATA_DIR / "work")))
PREVIEW_DIR = Path(os.getenv("PREVIEW_DIR", str(DATA_DIR / "previews")))
VMAF_SESSIONS_DIR = Path(os.getenv("VMAF_SESSIONS_DIR", str(DATA_DIR / "vmaf")))

# VMAF-Artefakte nach Analyse behalten (statt work-Ordner zu löschen)
RETAIN_VMAF_SESSIONS = _env_bool("RETAIN_VMAF_SESSIONS", True)

# VMAF-Modelle (im Docker-Image unter /usr/local/share/model/)
VMAF_MODEL_DIR = Path(os.getenv("VMAF_MODEL_DIR", "/usr/local/share/model"))
VMAF_MODEL_1080P = os.getenv("VMAF_MODEL_1080P", "vmaf_v0.6.1.json")
VMAF_MODEL_4K = os.getenv("VMAF_MODEL_4K", "vmaf_4k_v0.6.1.json")
# NEG-Varianten ("no enhancement gain") – strafen Schärfungs-/Kontrast-Tricks
# ab und urteilen bei Animation/Anime oft realistischer.
VMAF_MODEL_1080P_NEG = os.getenv("VMAF_MODEL_1080P_NEG", "vmaf_v0.6.1neg.json")
VMAF_MODEL_4K_NEG = os.getenv("VMAF_MODEL_4K_NEG", "vmaf_4k_v0.6.1neg.json")

# --- VMAF-Parameter (Defaults; UI kann clip_seconds pro Job überschreiben) ----
VMAF_CLIP_SECONDS = int(os.getenv("VMAF_CLIP_SECONDS", "30"))
VMAF_TEST_QUALITIES = [20, 24, 28, 32]
VMAF_SWEETSPOT = (93.0, 95.0)

# --- Qualitäts-Guardrail (Post-Encode-Verifikation) --------------------------
# Nach dem finalen Encode wird der echte VMAF der Ausgabedatei stichprobenartig
# gemessen. Liegt er unter dem Ziel, kann automatisch mit höherer Qualität neu
# encodiert werden. Werte pro Job im UI überschreibbar.
VERIFY_MAX_RETRIES = max(0, int(os.getenv("VERIFY_MAX_RETRIES", "2")))
VERIFY_CQ_STEP = max(1, int(os.getenv("VERIFY_CQ_STEP", "3")))       # CQ pro Retry senken
VERIFY_BITRATE_FACTOR = float(os.getenv("VERIFY_BITRATE_FACTOR", "1.25"))  # Bitrate pro Retry ×
VERIFY_CLIP_SECONDS = max(5, int(os.getenv("VERIFY_CLIP_SECONDS", "15")))

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".m4v", ".ts", ".m2ts",
    ".mpg", ".mpeg", ".wmv", ".flv", ".webm", ".vob", ".mts",
}

ARCHIVE_DIRNAME = ".archiv"
METRICS_INTERVAL = float(os.getenv("METRICS_INTERVAL", "1.5"))

# --- GPU-Render-Node (Intel QSV / AMD VAAPI) ---------------------------------
# Der DRM-Render-Knoten der zu nutzenden GPU. Auf Systemen mit MEHREREN GPUs
# (z. B. Nvidia-Karte + Intel-iGPU) ist /dev/dri/renderD128 evtl. NICHT die
# Intel-iGPU – dann hier den richtigen Knoten setzen (z. B. renderD129).
# Ermitteln im Container: `ls -l /dev/dri/by-path/` bzw. `vainfo --display drm
# --device /dev/dri/renderD129`.
VAAPI_DEVICE = os.getenv("VAAPI_DEVICE", "/dev/dri/renderD128")

# Intel-Encoder-Backend: "vaapi" (Standard) oder "qsv".
# Das Basis-Image (Ubuntu 24.04) bringt libva 2.20 (VA-API 1.21) + oneVPL mit,
# daher funktionieren beide Backends auf derselben Intel-Hardware. VAAPI ist als
# robuster Standard gesetzt; QSV/oneVPL bietet zusätzliche Feinheiten (Look-ahead
# etc.) – bei Bedarf einfach auf "qsv" umstellen.
INTEL_ENCODER = os.getenv("INTEL_ENCODER", "vaapi").strip().lower()

# NVENC-Dekodierpfad: Standardmäßig wird per CUDA dekodiert, die Frames aber in
# den System-RAM heruntergeladen (robust). Die reine GPU-Pipeline
# (`-hwaccel_output_format cuda` + scale_cuda) ist schneller, führt aber je nach
# Treiber/Quelle zu komplett grünen Ausgaben. Wer die volle GPU-Pipeline
# erzwingen will, setzt NVENC_FULL_GPU=1.
NVENC_FULL_GPU = _env_bool("NVENC_FULL_GPU", False)

# --- CQ-Sweetspot-Overrides ---------------------------------------------------
# Optional per Env feinjustierbar. Format (kommagetrennt):
#   CQ_SWEETSPOT="cpu:hevc=22,nvidia:av1=33"
# Ohne Env läuft alles mit den in vmaf.py hinterlegten Standardwerten.
def _parse_cq_overrides(raw: str) -> dict:
    out: dict[tuple, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part or ":" not in part.split("=")[0]:
            continue
        key, _, val = part.partition("=")
        plat, _, codec = key.strip().partition(":")
        try:
            out[(plat.strip(), codec.strip())] = int(val.strip())
        except ValueError:
            continue
    return out


CQ_SWEETSPOT_OVERRIDES = _parse_cq_overrides(os.getenv("CQ_SWEETSPOT", ""))

# --- Parallele Encodes --------------------------------------------------------
# MAX_PARALLEL_ENCODES = 0 -> beim Start automatisch aus der Hardware ableiten.
# PARALLEL_ENCODES_LIMIT begrenzt, was der Nutzer im UI maximal wählen darf.
MAX_PARALLEL_ENCODES = int(os.getenv("MAX_PARALLEL_ENCODES", "0"))
PARALLEL_ENCODES_LIMIT = max(1, int(os.getenv("PARALLEL_ENCODES_LIMIT", "6")))


def data_paths_dict() -> dict:
    """Alle relevanten Pfade für API / UI."""
    return {
        "data_dir": str(DATA_DIR),
        "work_dir": str(WORK_DIR),
        "preview_dir": str(PREVIEW_DIR),
        "vmaf_sessions_dir": str(VMAF_SESSIONS_DIR),
        "input_dir": str(INPUT_DIR),
        "output_dir": str(OUTPUT_DIR),
        "retain_vmaf_sessions": RETAIN_VMAF_SESSIONS,
    }


def ensure_dirs() -> None:
    """Stellt sicher, dass alle Arbeitsverzeichnisse existieren."""
    for d in (INPUT_DIR, OUTPUT_DIR, DATA_DIR, WORK_DIR, PREVIEW_DIR, VMAF_SESSIONS_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
