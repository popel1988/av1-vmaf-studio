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

# --- Mehrere Eingabe-Ordner ("virtuelle Roots") ------------------------------
# Wer verschiedene Ordnerstrukturen einbinden will (Filme, Serien, Anime …),
# ohne das ganze Laufwerk zu mounten, gibt sie über INPUT_DIRS an:
#   INPUT_DIRS="Filme=/media/filme;Serien=/media/serien;Anime=/media/anime"
# Einträge sind durch ; oder Zeilenumbruch getrennt, jeweils "Name=/pfad"
# (Name optional – sonst der Ordnername). Ohne INPUT_DIRS gilt der einzelne
# INPUT_DIR wie bisher. Relative Pfade sind bei mehreren Roots mit dem Root-Namen
# als erstem Segment prefixed ("Filme/Unterordner/film.mkv").
from typing import Iterator, Optional  # noqa: E402


def _slug(name: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (name or "").strip())
    return s.strip("_") or "root"


def _parse_roots(env_name: str, default_dir: Path,
                 default_name: str) -> "list[tuple[str, Path]]":
    raw = os.getenv(env_name, "").strip()
    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()
    if raw:
        parts = [p.strip() for line in raw.splitlines() for p in line.split(";")]
        for part in parts:
            if not part:
                continue
            if "=" in part:
                name, _, path = part.partition("=")
            else:
                name, path = "", part
            p = Path(path.strip())
            nm = _slug(name.strip() or p.name or "root")
            base_nm, k = nm, 2
            while nm in seen:
                nm = f"{base_nm}{k}"
                k += 1
            seen.add(nm)
            roots.append((nm, p))
    if not roots:
        roots = [(_slug(default_dir.name or default_name), default_dir)]
    return roots


INPUT_ROOTS: "list[tuple[str, Path]]" = _parse_roots("INPUT_DIRS", INPUT_DIR, "input")
MULTI_INPUT = len(INPUT_ROOTS) > 1

# --- Mehrere Ausgabe-Ordner (optional, analog zu INPUT_DIRS) -----------------
# OUTPUT_DIRS="Standard=/media/output;NAS=/mnt/nas/encodes" – erlaubt pro Job die
# Wahl eines Ziel-Volumes. Ohne die Variable gilt der einzelne OUTPUT_DIR.
OUTPUT_ROOTS: "list[tuple[str, Path]]" = _parse_roots("OUTPUT_DIRS", OUTPUT_DIR, "output")
MULTI_OUTPUT = len(OUTPUT_ROOTS) > 1


def input_roots_public() -> "list[dict]":
    """Roots für UI/API (Name + Pfad + Existenz)."""
    return [{"name": n, "path": str(p), "exists": p.exists()} for n, p in INPUT_ROOTS]


def output_roots_public() -> "list[dict]":
    return [{"name": n, "path": str(p), "exists": p.exists()} for n, p in OUTPUT_ROOTS]


def resolve_output_base(root_name: str) -> Path:
    """Basis-Verzeichnis eines Output-Roots (Fallback: erster Root)."""
    for name, base in OUTPUT_ROOTS:
        if name == (root_name or ""):
            return base
    return OUTPUT_ROOTS[0][1]


def safe_subdir(sub: str) -> str:
    """Freien Ziel-Unterordner säubern (kein Pfad-Traversal, keine Wurzel)."""
    parts = []
    for seg in str(sub or "").replace("\\", "/").split("/"):
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        parts.append(seg)
    return "/".join(parts)


def _within(target: Path, base: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def resolve_input(rel: str) -> Optional[Path]:
    """Relativen (root-aware) Pfad sicher in einen absoluten Pfad auflösen.

    Einzel-Root: `rel` ist relativ zum Root (optionaler Root-Prefix erlaubt).
    Multi-Root: erstes Segment = Root-Name, Rest = Unterpfad.
    """
    rel = (rel or "").strip().strip("/")
    if len(INPUT_ROOTS) == 1:
        name, base = INPUT_ROOTS[0]
        base = base.resolve()
        first, _, rest = rel.partition("/")
        if first == name:
            rel = rest
        target = (base / rel).resolve() if rel else base
        return target if _within(target, base) else None
    if not rel:
        return None
    first, _, rest = rel.partition("/")
    for name, base in INPUT_ROOTS:
        if name == first:
            b = base.resolve()
            target = (b / rest).resolve() if rest else b
            return target if _within(target, b) else None
    return None


def rel_input(abs_path) -> Optional[str]:
    """Absoluten Pfad in einen root-aware relativen Pfad umwandeln (für UI/Output).

    Einzel-Root: Unterpfad ohne Prefix (rückwärtskompatibel).
    Multi-Root: "Root-Name/Unterpfad".
    """
    ap = Path(abs_path).resolve()
    for name, base in INPUT_ROOTS:
        b = base.resolve()
        if _within(ap, b):
            sub = ap.relative_to(b).as_posix()
            if len(INPUT_ROOTS) == 1:
                return "" if sub == "." else sub
            return name if sub == "." else f"{name}/{sub}"
    return None


def scan_targets(root_rel: str) -> "list[Path]":
    """Zu durchsuchende Verzeichnisse für einen (root-aware) Pfad bestimmen.

    Leerer Pfad → alle vorhandenen Roots. Konkreter Pfad → genau dieser Ordner.
    """
    rel = (root_rel or "").strip().strip("/")
    if rel:
        target = resolve_input(rel)
        if target and target.is_dir():
            return [target]
    return [b.resolve() for _, b in INPUT_ROOTS if b.exists()]


def iter_input_files(root_rel: str, exts: set) -> "Iterator[Path]":
    """Alle passenden Dateien unter dem/den Ziel-Root(s) (rekursiv)."""
    for scan_dir in scan_targets(root_rel):
        for f in scan_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in exts:
                yield f

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

# Externe Ton-/Untertitel-Dateien für den Remux-/Bearbeiten-Modus (Spuren
# hinzufügen ohne Re-Encode). Videos zählen ebenfalls als Quelle (Spur daraus).
AUDIO_EXTENSIONS = {
    ".eac3", ".ac3", ".dts", ".thd", ".truehd", ".mlp", ".flac", ".aac",
    ".m4a", ".mka", ".opus", ".ogg", ".mp3", ".wav", ".ac4",
}
SUBTITLE_EXTENSIONS = {
    ".srt", ".ass", ".ssa", ".sup", ".sub", ".vtt", ".idx", ".pgs",
}
# Attachments (Fonts/Cover) für den Remux-Modus.
ATTACHMENT_EXTENSIONS = {
    ".ttf", ".otf", ".ttc", ".pfb", ".jpg", ".jpeg", ".png", ".webp",
    ".txt",  # Kapitel-/ffmetadata-Dateien
}

ARCHIVE_DIRNAME = ".archiv"
METRICS_INTERVAL = float(os.getenv("METRICS_INTERVAL", "1.5"))

# --- Logging ------------------------------------------------------------------
# LOG_LEVEL steuert die Ausführlichkeit der Container-Logs (DEBUG, INFO,
# WARNING, ERROR). Standard INFO. LOG_FFMPEG_CMD=1 loggt zusätzlich die
# vollständige FFmpeg-Kommandozeile jedes Encodes (sehr geschwätzig, aber
# hilfreich beim Debuggen). Standard: an, da explizit gewünscht.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_FFMPEG_CMD = _env_bool("LOG_FFMPEG_CMD", True)

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
