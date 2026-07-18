"""Selbsttest/Diagnose: prüft beim Bedarf, ob alle Bausteine vorhanden und
nutzbar sind (FFmpeg/Encoder, VMAF-Modelle, dovi_tool, GPU/VAAPI, Datenordner).

Liefert einen strukturierten Report für die UI. Jede Prüfung hat einen Status
``ok`` | ``warn`` | ``fail`` und einen erklärenden Text. Alle Aufrufe sind
defensiv (Timeouts, Exceptions abgefangen), damit die Seite nie hängt.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import config
from . import ffmpeg_utils as ff

_PLATFORM_LABELS = {
    "nvidia": "NVIDIA (NVENC)", "intel": "Intel (QSV/VAAPI)",
    "amd": "AMD (VAAPI)", "cpu": "CPU (Software)",
}
_CODECS = ("av1", "hevc", "h264")


def _check(name: str, status: str, detail: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail}


def _section(title: str, checks: list) -> dict:
    order = {"fail": 0, "warn": 1, "ok": 2}
    worst = min((order.get(c["status"], 2) for c in checks), default=2)
    summary = {0: "fail", 1: "warn", 2: "ok"}[worst]
    return {"title": title, "status": summary, "checks": checks}


def _ffmpeg_has_filter(name: str) -> bool:
    try:
        out = subprocess.run([config.FFMPEG, "-hide_banner", "-filters"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20, check=False)
        return name in (out.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _tools_section() -> dict:
    checks: list[dict] = []

    ver = ff.ffmpeg_version()
    checks.append(_check(
        "FFmpeg", "ok" if ver != "unbekannt" else "fail",
        f"{ver} · {config.FFMPEG}"))

    probe_ok = bool(Path(config.FFPROBE).exists() or config.FFPROBE)
    checks.append(_check("ffprobe", "ok" if probe_ok else "fail", config.FFPROBE))

    checks.append(_check(
        "libvmaf-Filter", "ok" if _ffmpeg_has_filter("libvmaf") else "fail",
        "im FFmpeg-Build enthalten" if _ffmpeg_has_filter("libvmaf")
        else "fehlt – VMAF-Analyse nicht möglich"))

    try:
        from . import dolby_vision as dv
        if dv.available():
            try:
                r = subprocess.run([config.DOVI_TOOL, "--version"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=10, check=False)
                dver = (r.stdout or "").strip().splitlines()
                detail = dver[0] if dver else config.DOVI_TOOL
            except (OSError, subprocess.SubprocessError):
                detail = config.DOVI_TOOL
            checks.append(_check("dovi_tool (Dolby Vision)", "ok", detail))
        else:
            checks.append(_check("dovi_tool (Dolby Vision)", "warn",
                                 "nicht gefunden – DV-Erhaltung deaktiviert"))
    except Exception as e:  # pragma: no cover
        checks.append(_check("dovi_tool (Dolby Vision)", "warn", str(e)))

    return _section("FFmpeg & Werkzeuge", checks)


def _models_section() -> dict:
    checks: list[dict] = []
    models = [
        ("HD (1080p)", config.VMAF_MODEL_1080P),
        ("4K/UHD", config.VMAF_MODEL_4K),
        ("HD NEG (Anime)", config.VMAF_MODEL_1080P_NEG),
        ("4K NEG (Anime)", config.VMAF_MODEL_4K_NEG),
    ]
    for label, name in models:
        path = config.VMAF_MODEL_DIR / name
        exists = path.exists()
        # NEG-Modelle sind optional (Fallback aufs Standardmodell).
        neg = "NEG" in label
        status = "ok" if exists else ("warn" if neg else "fail")
        detail = str(path) if exists else f"fehlt: {path}"
        checks.append(_check(f"VMAF-Modell {label}", status, detail))
    return _section("VMAF-Modelle", checks)


def _last_error_line(stderr: str) -> str:
    """Kurze, aussagekräftige Fehlerzeile aus FFmpeg-stderr herausziehen."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    keys = ("not supported", "Error", "error", "Failed", "failed", "Invalid",
            "No capable", "Cannot load", "Unknown", "unsupported")
    for ln in reversed(lines):
        if any(k in ln for k in keys):
            return ln[:160]
    return (lines[-1][:160] if lines else "unbekannter Fehler")


def _test_encode(platform: str, codec: str, timeout: int = 40) -> tuple[bool, str]:
    """Führt einen winzigen echten Encode (5 Frames, 320x240) mit dem konkreten
    Encoder aus und meldet, ob er WIRKLICH läuft. So werden HW-Grenzen erkannt,
    die die reine `-encoders`-Liste nicht zeigt (z. B. Intel-iGPU ohne AV1)."""
    enc = ff.encoder_name(platform, codec)
    backend = ff.encoder_backend(platform)
    src = "color=c=black:s=320x240:r=25"
    cmd = [config.FFMPEG, "-hide_banner", "-y"]
    vf = None
    if backend == "vaapi":
        cmd += ["-vaapi_device", config.VAAPI_DEVICE,
                "-f", "lavfi", "-i", src, "-frames:v", "5"]
        vf = "format=nv12,hwupload"
    elif backend == "qsv":
        # QSV über VAAPI initialisieren (wie im Encoder-Pfad des Projekts).
        cmd += ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}",
                "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs",
                "-f", "lavfi", "-i", src, "-frames:v", "5"]
        vf = "format=nv12,hwupload=extra_hw_frames=64"
    else:  # nvenc (lädt Systemframes selbst hoch) / cpu
        cmd += ["-f", "lavfi", "-i", src, "-frames:v", "5"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", enc]
    if enc == "libsvtav1":
        cmd += ["-preset", "12"]
    elif enc in ("libx264", "libx265"):
        cmd += ["-preset", "ultrafast"]
    cmd += ["-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, check=False)
        if r.returncode == 0:
            return True, ""
        return False, _last_error_line(r.stderr)
    except subprocess.TimeoutExpired:
        return False, "Zeitüberschreitung beim Test-Encode"
    except OSError as e:
        return False, str(e)


def _encoder_section(monitor, deep: bool = False) -> dict:
    checks: list[dict] = []
    try:
        platforms = monitor.available_platforms()
    except Exception:  # pragma: no cover
        platforms = ["cpu"]
    if "cpu" not in platforms:
        platforms = list(platforms) + ["cpu"]

    avail = ff.available_encoders()
    if not avail:
        checks.append(_check("Encoder-Liste", "warn",
                             "FFmpeg -encoders nicht lesbar – Prüfung übersprungen"))
        return _section("Encoder", checks)

    # Im Funktionstest die echten Ergebnisse ermitteln UND den Cache aktualisieren,
    # damit VMAF-Tool/Encoding danach nur noch lauffähige Encoder anbieten.
    deep_results: dict = {}
    if deep:
        try:
            from . import capabilities as caps
            deep_results = caps.compute(monitor).get("results", {})
        except Exception:  # pragma: no cover
            deep_results = {}

    for p in platforms:
        codec_states = []
        codec_status = []  # pro Codec: "ok" | "warn" | "skip"
        for c in _CODECS:
            enc = ff.encoder_name(p, c)
            present = enc in avail
            if not present:
                codec_states.append(f"{c.upper()}=✗ ({enc}, nicht im Build)")
                codec_status.append("skip")
                continue
            if not deep:
                codec_states.append(f"{c.upper()}=✓ ({enc})")
                codec_status.append("ok")
                continue
            # Echter Mini-Encode: prüft die tatsächliche HW-Fähigkeit.
            key = f"{p}:{c}"
            if key in deep_results:
                ok, err = bool(deep_results[key]), "HW unterstützt diesen Encoder nicht"
            else:
                ok, err = _test_encode(p, c)
            if ok:
                codec_states.append(f"{c.upper()}=✓ ({enc})")
                codec_status.append("ok")
            else:
                codec_states.append(f"{c.upper()}=✗ ({enc}: {err})")
                codec_status.append("warn")

        present_any = any(s != "skip" for s in codec_status)
        if not present_any:
            status = "warn"
        elif deep and any(s == "warn" for s in codec_status):
            status = "warn"
        else:
            status = "ok"
        checks.append(_check(_PLATFORM_LABELS.get(p, p), status,
                             " · ".join(codec_states)))

    title = ("Encoder-Funktionstest (echte Mini-Encodes)"
             if deep else "Encoder-Verfügbarkeit")
    return _section(title, checks)


def _hardware_section(monitor) -> dict:
    checks: list[dict] = []
    try:
        cap = monitor.encode_capacity()
    except Exception:  # pragma: no cover
        cap = {}

    gpus = cap.get("gpus", [])
    if gpus:
        for g in gpus:
            checks.append(_check(
                g.get("name", "GPU"), "ok",
                f"{g.get('encoders', 1)} Encoder-Engine(s) · {g.get('vendor', '')}"))
    else:
        checks.append(_check("GPU", "warn",
                             "keine GPU erkannt – es wird über die CPU encodiert"))

    checks.append(_check(
        "CPU-Threads", "ok", str(cap.get("cpu_threads", "?"))))
    checks.append(_check(
        "Empfohlene Parallelität", "ok", str(cap.get("suggested_parallel", 1))))

    # VAAPI-Render-Node (nur für Intel/AMD relevant).
    dev = Path(config.VAAPI_DEVICE)
    if dev.exists():
        checks.append(_check("VAAPI-Gerät", "ok", str(dev)))
    else:
        checks.append(_check("VAAPI-Gerät", "warn",
                             f"{dev} nicht vorhanden (nur für Intel/AMD nötig)"))
    return _section("Hardware", checks)


def _storage_section() -> dict:
    checks: list[dict] = []
    dirs = [
        (f"Eingabe [{name}]", base, False)
        for name, base in config.INPUT_ROOTS
    ] + [
        ("Ausgabe", config.OUTPUT_DIR, True),
        ("Daten", config.DATA_DIR, True),
        ("Arbeitsordner", config.WORK_DIR, True),
        ("Previews", config.PREVIEW_DIR, True),
        ("VMAF-Sessions", config.VMAF_SESSIONS_DIR, True),
    ]
    for label, path, need_write in dirs:
        p = Path(path)
        if not p.exists():
            status = "fail" if need_write else "warn"
            checks.append(_check(label, status, f"fehlt: {p}"))
            continue
        writable = os.access(p, os.W_OK)
        if need_write and not writable:
            checks.append(_check(label, "fail", f"nicht beschreibbar: {p}"))
        else:
            checks.append(_check(label, "ok", str(p)))
    return _section("Datenordner", checks)


def run_diagnostics(monitor, deep: bool = False) -> dict:
    """Führt alle Prüfungen aus und liefert den Gesamtreport.

    deep=True führt zusätzlich echte Mini-Encodes je Plattform/Codec aus, um die
    tatsächliche Hardware-Fähigkeit zu prüfen (dauert einige Sekunden länger)."""
    sections = [
        _tools_section(),
        _models_section(),
        _encoder_section(monitor, deep=deep),
        _hardware_section(monitor),
        _storage_section(),
    ]
    order = {"fail": 0, "warn": 1, "ok": 2}
    worst = min((order[s["status"]] for s in sections), default=2)
    overall = {0: "fail", 1: "warn", 2: "ok"}[worst]
    return {"overall": overall, "sections": sections}
