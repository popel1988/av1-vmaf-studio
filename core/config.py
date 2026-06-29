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


# Pfade zu den (eigenen) FFmpeg-Binaries – immun gegen PATH-Shadowing.
FFMPEG = _resolve_binary("ffmpeg")
FFPROBE = _resolve_binary("ffprobe")

# --- Verzeichnisse (werden via Docker-Volumes gemountet) ---------------------
INPUT_DIR = Path(os.getenv("INPUT_DIR", "/media/input"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/media/output"))

# VMAF-Modelle (im Docker-Image unter /usr/local/share/model/)
VMAF_MODEL_DIR = Path(os.getenv("VMAF_MODEL_DIR", "/usr/local/share/model"))
VMAF_MODEL_1080P = os.getenv("VMAF_MODEL_1080P", "vmaf_v0.6.1.json")
VMAF_MODEL_4K = os.getenv("VMAF_MODEL_4K", "vmaf_4k_v0.6.1.json")

# Temporäres Arbeitsverzeichnis für VMAF-Testclips / Encodes
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/vcompress"))

# --- VMAF-Parameter ----------------------------------------------------------
VMAF_CLIP_SECONDS = int(os.getenv("VMAF_CLIP_SECONDS", "30"))
VMAF_TEST_QUALITIES = [20, 24, 28, 32]
# Empfohlener "Sweet Spot"-Bereich
VMAF_SWEETSPOT = (93.0, 95.0)

# Unterstützte Video-Endungen für den Datei-Browser / Batch-Modus
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".m4v", ".ts", ".m2ts",
    ".mpg", ".mpeg", ".wmv", ".flv", ".webm", ".vob", ".mts",
}

# Verzeichnis-Name für das Archiv beim Post-Processing
ARCHIVE_DIRNAME = ".archiv"

# Refresh-Intervall der Hardware-Metriken (Sekunden)
METRICS_INTERVAL = float(os.getenv("METRICS_INTERVAL", "1.5"))


def ensure_dirs() -> None:
    """Stellt sicher, dass die Arbeitsverzeichnisse existieren."""
    for d in (OUTPUT_DIR, WORK_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Im Dev-Betrieb (ohne /media) ignorieren wir fehlende Mounts.
            pass
