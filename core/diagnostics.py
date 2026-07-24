"""Selbsttest/Diagnose: prüft beim Bedarf, ob alle Bausteine vorhanden und
nutzbar sind (FFmpeg/Encoder, VMAF-Modelle, dovi_tool, GPU/VAAPI, Datenordner).

Liefert einen strukturierten Report für die UI. Jede Prüfung hat einen Status
``ok`` | ``warn`` | ``fail`` und einen erklärenden Text. Alle Aufrufe sind
defensiv (Timeouts, Exceptions abgefangen), damit die Seite nie hängt.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
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


def _soft_sample_encoder(codec: str) -> tuple[str, list[str]]:
    """Software-Encoder + Extra-Args für eine kurze Testclip-Datei."""
    if codec == "hevc":
        return "libx265", ["-preset", "ultrafast", "-x265-params", "log-level=error"]
    if codec == "av1":
        return "libsvtav1", ["-preset", "12"]
    return "libx264", ["-preset", "ultrafast"]


def _test_decode(platform: str, codec: str, timeout: int = 45) -> tuple[bool, str]:
    """Kurzer Clip software-encoden, dann mit HW-Decode lesen.

    Entspricht dem Player-Pfad (``-hwaccel cuda|qsv|vaapi``). CPU gilt immer
    als ok, sofern der generische Decoder im Build steckt.
    """
    if platform == "cpu":
        return True, ""

    soft, soft_extra = _soft_sample_encoder(codec)
    avail_enc = ff.available_encoders()
    if avail_enc and soft not in avail_enc:
        return False, f"kein Software-Encoder ({soft}) für Testclip"

    backend = ff.encoder_backend(platform)
    sample = None
    try:
        fd, sample = tempfile.mkstemp(suffix=".mkv")
        os.close(fd)
        gen = [
            config.FFMPEG, "-hide_banner", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25",
            "-frames:v", "8", "-c:v", soft, *soft_extra,
            "-an", sample,
        ]
        r = subprocess.run(gen, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, check=False)
        if r.returncode != 0 or not Path(sample).is_file():
            return False, f"Testclip fehlgeschlagen: {_last_error_line(r.stderr)}"

        cmd = [config.FFMPEG, "-hide_banner", "-y"]
        if backend == "nvenc":
            # Wie Player: generisches CUDA-hwaccel (Decoder je nach Stream).
            dec = ff.decoder_name(platform, codec)
            cmd += ["-hwaccel", "cuda"]
            if dec and (not ff.available_decoders() or dec in ff.available_decoders()):
                cmd += ["-c:v", dec]
        elif backend == "qsv":
            dec = ff.decoder_name(platform, codec)
            cmd += ["-hwaccel", "qsv", "-c:v", dec]
        elif backend == "vaapi":
            cmd += ["-hwaccel", "vaapi", "-hwaccel_device", config.VAAPI_DEVICE]
        else:
            return True, ""

        cmd += ["-i", sample, "-frames:v", "5", "-f", "null", "-"]
        r2 = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=timeout, check=False)
        if r2.returncode == 0:
            return True, ""
        return False, _last_error_line(r2.stderr)
    except subprocess.TimeoutExpired:
        return False, "Zeitüberschreitung beim Test-Decode"
    except OSError as e:
        return False, str(e)
    finally:
        if sample:
            try:
                Path(sample).unlink(missing_ok=True)
            except OSError:
                pass


def _caps_cache_meta() -> tuple[dict, dict, str]:
    """(encode_map, decode_map, cache_age_label) aus Capabilities-Cache."""
    try:
        from . import capabilities as caps
        cached = caps.get_cached() or {}
        enc = dict(cached.get("results") or {})
        dec = dict(cached.get("decode") or {})
        ts = float(cached.get("generated_at") or 0)
        age = ""
        if ts > 0:
            age_h = max(0, int((time.time() - ts) / 3600))
            age = f"Cache {age_h}h alt" if age_h else "Cache frisch"
        return enc, dec, age
    except Exception:  # pragma: no cover
        return {}, {}, ""


def _platform_codec_checks(
    platforms: list[str],
    *,
    kind: str,
    name_fn,
    present_fn,
    results: dict,
    deep: bool,
) -> list[dict]:
    """Gemeinsame Diagnose-Zeilen für Encode oder Decode."""
    checks: list[dict] = []
    for p in platforms:
        codec_states = []
        codec_status = []
        for c in _CODECS:
            name = name_fn(p, c)
            if not present_fn(p, c):
                codec_states.append(f"{c.upper()}=✗ ({name}, nicht im Build)")
                codec_status.append("skip")
                continue
            key = f"{p}:{c}"
            if deep or (results and key in results):
                ok = bool(results.get(key)) if results else False
                if deep and key not in results:
                    # Fallback falls Cache unvollständig
                    if kind == "encode":
                        ok, _ = _test_encode(p, c)
                    else:
                        ok, _ = _test_decode(p, c)
                if ok:
                    tag = "getestet" if not deep else ""
                    suffix = f", {tag}" if tag else ""
                    codec_states.append(f"{c.upper()}=✓ ({name}{suffix})")
                    codec_status.append("ok")
                else:
                    what = "Encoder" if kind == "encode" else "Decoder"
                    codec_states.append(
                        f"{c.upper()}=✗ ({name}: HW unterstützt diesen {what} nicht)")
                    codec_status.append("warn")
            else:
                codec_states.append(
                    f"{c.upper()}=? ({name}, nur im Build – Funktionstest ausstehend)")
                codec_status.append("warn")

        present_any = any(s != "skip" for s in codec_status)
        if not present_any:
            status = "warn"
        elif any(s == "warn" for s in codec_status):
            status = "warn"
        else:
            status = "ok"
        checks.append(_check(_PLATFORM_LABELS.get(p, p), status,
                             " · ".join(codec_states)))
    return checks


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

    enc_map, _, cache_age = _caps_cache_meta()
    checks = _platform_codec_checks(
        platforms,
        kind="encode",
        name_fn=ff.encoder_name,
        present_fn=lambda p, c: ff.encoder_name(p, c) in avail,
        results=enc_map,
        deep=deep,
    )

    if deep:
        title = "Encoder-Funktionstest (echte Mini-Encodes)"
    elif enc_map:
        title = f"Encoder-Verfügbarkeit ({cache_age or 'aus Cache'})"
    else:
        title = "Encoder-Verfügbarkeit (noch kein Funktionstest)"
    return _section(title, checks)


def _decoder_section(monitor, deep: bool = False) -> dict:
    """HW-Decode-Fähigkeit – relevant für Player-Transcode (CUDA/QSV/VAAPI)."""
    try:
        platforms = monitor.available_platforms()
    except Exception:  # pragma: no cover
        platforms = ["cpu"]
    if "cpu" not in platforms:
        platforms = list(platforms) + ["cpu"]

    avail = ff.available_decoders()
    if not avail:
        return _section(
            "Decoder",
            [_check("Decoder-Liste", "warn",
                    "FFmpeg -decoders nicht lesbar – Prüfung übersprungen")],
        )

    _, dec_map, cache_age = _caps_cache_meta()
    checks = _platform_codec_checks(
        platforms,
        kind="decode",
        name_fn=ff.decoder_name,
        present_fn=ff.decoder_available,
        results=dec_map,
        deep=deep,
    )

    if deep:
        title = "Decoder-Funktionstest (HW-Decode der Quellcodecs)"
    elif dec_map:
        title = f"Decoder-Verfügbarkeit ({cache_age or 'aus Cache'})"
    else:
        title = "Decoder-Verfügbarkeit (noch kein Funktionstest)"
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
        (f"Media [{name}]", base, True)
        for name, base in config.MEDIA_ROOTS
    ] + [
        ("Standard-Ausgabe", config.default_output_path(), True),
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

    deep=True führt zusätzlich echte Mini-Encode- und Decode-Tests je
    Plattform/Codec aus (aktualisiert den Capabilities-Cache)."""
    if deep:
        try:
            from . import capabilities as caps
            caps.compute(monitor)
        except Exception:  # pragma: no cover
            pass
    sections = [
        _tools_section(),
        _models_section(),
        _encoder_section(monitor, deep=deep),
        _decoder_section(monitor, deep=deep),
        _hardware_section(monitor),
        _storage_section(),
    ]
    order = {"fail": 0, "warn": 1, "ok": 2}
    worst = min((order[s["status"]] for s in sections), default=2)
    overall = {0: "fail", 1: "warn", 2: "ok"}[worst]
    return {"overall": overall, "sections": sections}
