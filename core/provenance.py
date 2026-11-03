"""Archive provenance and coverage; never re-score or re-pick saved results."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from . import config


def atomic_json_write(path: Path, payload: dict) -> None:
    """Replace one complete JSON document, leaving the old file intact on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".analysis-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def source_fingerprint(path: str) -> dict:
    """Metadata-only fingerprint: no reads/hashes of large source media."""
    if not path:
        return {"status": "unknown", "size_bytes": None, "mtime_ns": None}
    try:
        source = Path(path)
        stat = source.stat()
        if not source.is_file():
            return {"status": "missing", "size_bytes": None, "mtime_ns": None}
        return {"status": "present", "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "method": "size_mtime_ns"}
    except FileNotFoundError:
        return {"status": "missing", "size_bytes": None, "mtime_ns": None}
    except OSError:
        return {"status": "unknown", "size_bytes": None, "mtime_ns": None}


@lru_cache(maxsize=32)
def _model_digest(path: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_identity(name: str, path: Path | None) -> dict:
    identity = {"name": name, "path": str(path) if path else "", "sha256": None,
                "status": "unknown" if name else "not_applicable"}
    if path is None:
        return identity
    try:
        stat = Path(path).stat()
        identity.update(sha256=_model_digest(str(path), stat.st_size, stat.st_mtime_ns), status="present")
    except FileNotFoundError:
        identity["status"] = "missing"
    except OSError:
        pass
    return identity


@lru_cache(maxsize=32)
def _binary_version(binary: str, size: int | None, mtime_ns: int | None) -> str:
    try:
        result = subprocess.run([binary, "-hide_banner", "-version"],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=10, check=False)
        if result.returncode == 0:
            return next(iter((result.stdout or "").splitlines()), "")
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def binary_identity(binary: str) -> dict:
    resolved = shutil.which(str(binary)) or str(binary)
    stamp = source_fingerprint(resolved)
    return {"binary": resolved, "version": _binary_version(
        resolved, stamp.get("size_bytes"), stamp.get("mtime_ns")),
        "size_bytes": stamp.get("size_bytes"), "mtime_ns": stamp.get("mtime_ns")}


def capture(*, source_path: str, model_name: str = "", model_path: Path | None = None,
            params: dict | None = None, source_info: dict | None = None) -> dict:
    return {"schema_version": 1, "source": source_fingerprint(source_path),
            "source_info": dict(source_info or {}),
            "image_channel": config.IMAGE_CHANNEL,
            "ffmpeg": binary_identity(config.FFMPEG), "binaries": {},
            "model": model_identity(model_name, model_path),
            "params": dict(params or {})}


def record_binary(provenance: dict, role: str, binary: str) -> None:
    """Record the actual command's binary, not a guessed engine selection."""
    binaries = provenance.setdefault("binaries", {})
    key = f"{role}:{binary}"
    if key not in binaries:
        config_key = "FFMPEG_JELLYFIN" if str(binary) == str(config.FFMPEG_JELLYFIN) else (
            "FFMPEG" if str(binary) == str(config.FFMPEG) else None)
        binaries[key] = {"role": role, "config_key": config_key, **binary_identity(binary)}


def _current_model(saved: dict, info: dict) -> dict:
    if not saved.get("name"):
        return model_identity("", None)
    is_4k = bool(info.get("is_4k"))
    # The analysis pipeline always requests NEG and falls back to the standard model.
    name = config.VMAF_MODEL_4K_NEG if is_4k else config.VMAF_MODEL_1080P_NEG
    path = config.VMAF_MODEL_DIR / name
    if not path.is_file():
        name = config.VMAF_MODEL_4K if is_4k else config.VMAF_MODEL_1080P
        path = config.VMAF_MODEL_DIR / name
    return model_identity(name, path)


def archive_context(data: dict) -> dict:
    """Non-mutating live checks. Unknown old archives are not labelled unchanged."""
    provenance = data.get("provenance") or (data.get("analysis") or {}).get("provenance") or {}
    saved = provenance.get("source") or {}
    current = source_fingerprint(data.get("source_path") or "")
    state = current["status"]
    if state == "present":
        state = "unknown" if any(saved.get(k) is None for k in ("size_bytes", "mtime_ns")) else (
            "unchanged" if all(saved[k] == current[k] for k in ("size_bytes", "mtime_ns")) else "changed")
    warnings = []
    if state != "unchanged":
        warnings.append({"code": f"source_{state}"})
    model = provenance.get("model") or {}
    actual = _current_model(model, provenance.get("source_info") or {})
    comparable = None
    if model.get("status") == "not_applicable":
        model_status = "not_applicable"
    elif not model.get("sha256"):
        model_status = "unknown"
    elif actual.get("status") == "missing":
        model_status = "missing"
    elif not actual.get("sha256"):
        model_status = "unknown"
    else:
        comparable = model["sha256"] == actual["sha256"]
        model_status = "same" if comparable else "changed"
    if model_status not in ("same", "not_applicable"):
        warnings.append({"code": f"model_{model_status}"})
    saved_binary = provenance.get("ffmpeg") or {}
    # Listing archives must not execute FFmpeg. Compare cheap binary metadata instead.
    resolved = shutil.which(str(config.FFMPEG)) or str(config.FFMPEG)
    stamp = source_fingerprint(resolved)
    runtime_status = "unknown"
    if saved_binary.get("mtime_ns") is not None and stamp.get("mtime_ns") is not None:
        runtime_status = "same" if (saved_binary.get("binary") == resolved and all(
            saved_binary.get(k) == stamp.get(k) for k in ("size_bytes", "mtime_ns")) and
            provenance.get("image_channel") == config.IMAGE_CHANNEL) else "changed"
    # Jede tatsächlich verwendete Referenz-/Encoder-Binärdatei prüfen, auch
    # das optionale Jellyfin-Binary. Dabei nie archivierte Befehle ausführen.
    binary_checks = []
    for saved_tool in (provenance.get("binaries") or {}).values():
        config_key = saved_tool.get("config_key")
        candidate = getattr(config, config_key) if config_key in ("FFMPEG", "FFMPEG_JELLYFIN") else saved_tool.get("binary", "")
        candidate = shutil.which(str(candidate)) or str(candidate)
        stamp = source_fingerprint(candidate)
        tool_status = "unknown"
        if stamp.get("status") == "missing":
            tool_status = "changed"
        elif all(saved_tool.get(k) is not None and stamp.get(k) is not None for k in ("size_bytes", "mtime_ns")):
            tool_status = "same" if saved_tool.get("binary") == candidate and all(
                saved_tool[k] == stamp[k] for k in ("size_bytes", "mtime_ns")) else "changed"
        binary_checks.append({"role": saved_tool.get("role"), "binary": candidate, "status": tool_status})
    if any(row["status"] == "changed" for row in binary_checks):
        runtime_status = "changed"
    elif runtime_status == "same" and any(row["status"] == "unknown" for row in binary_checks):
        runtime_status = "unknown"
    if runtime_status != "same":
        warnings.append({"code": f"runtime_{runtime_status}"})
    return {"source": {"status": state, "saved": saved, "current": current},
            "model": {"status": model_status, "comparable": comparable,
                      "saved": model, "current": actual},
            "runtime_status": runtime_status, "binaries": binary_checks, "warnings": warnings,
            "scores_reinterpreted": False}


def session_comparability(left: dict, right: dict) -> dict:
    """Exact model hashes and source metadata, not model filenames, permit comparison."""
    a, b = left.get("provenance") or {}, right.get("provenance") or {}
    reasons = []
    hashes = [(p.get("model") or {}).get("sha256") for p in (a, b)]
    if not all(hashes):
        reasons.append("model_unknown")
    elif hashes[0] != hashes[1]:
        reasons.append("different_model")
    sources = [p.get("source") or {} for p in (a, b)]
    if any(any(s.get(k) is None for k in ("size_bytes", "mtime_ns")) for s in sources):
        reasons.append("source_unknown")
    elif any(sources[0].get(k) != sources[1].get(k) for k in ("size_bytes", "mtime_ns")):
        reasons.append("different_source")
    if left.get("source_path") != right.get("source_path"):
        reasons.append("different_source_path")
    return {"comparable": False if any(r.startswith("different_") for r in reasons)
            else (None if reasons else True), "reasons": reasons,
            "note": "Different sampling or processing parameters can still affect scores."}


def _number(value) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    end = -1.0
    for start, stop in sorted(intervals):
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return total


def _known_nonnegative(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def analysis_coverage(analysis: dict, source_duration: float | None = None) -> dict:
    """Coverage per result; never multiply film coverage by encoder/test-value count.

    Missing legacy measurements remain null. Frame count is exact; seconds describe
    the actual measured clips' trimmed windows, not a statistical confidence bound.
    """
    duration = _number(source_duration or analysis.get("source_duration"))
    results = []
    for index, raw in enumerate(analysis.get("results") or []):
        scenes, intervals = [], []
        for scene in raw.get("scene_scores") or []:
            count = _known_nonnegative(scene.get("frame_count"))
            count = int(count) if count is not None and count.is_integer() else None
            seconds = _known_nonnegative(scene.get("scored_seconds"))
            if analysis.get("visual_only"):
                count, seconds = None, None
            known = count is not None and seconds is not None
            start = _known_nonnegative(scene.get("start"))
            offset = _known_nonnegative(scene.get("scored_offset"))
            # Older measurements stored the measured duration but not its exact offset.
            if known and count and offset is not None and start is not None:
                a = start + offset
                b = a + seconds
                if duration:
                    a, b = min(a, duration), min(b, duration)
                intervals.append((a, b))
            scenes.append({"scene": scene.get("scene", 0), "start": start,
                           "scored_seconds": seconds, "frame_count": count,
                           "scored_offset": offset, "known": known})
        complete = bool(scenes) and all(s["known"] for s in scenes)
        position_known = complete and all(s["scored_offset"] is not None and s["start"] is not None for s in scenes if s["frame_count"])
        union = _union_seconds(intervals) if position_known else None
        results.append({"result_index": index, "scenes": scenes,
                        "scored_seconds": sum(s["scored_seconds"] for s in scenes) if scenes and all(s["scored_seconds"] is not None for s in scenes) else None,
                        "frame_count": sum(s["frame_count"] for s in scenes) if scenes and all(s["frame_count"] is not None for s in scenes) else None,
                        "unique_scored_seconds": union,
                        "film_fraction": min(1.0, union / duration) if union is not None and duration else None,
                        "complete": complete})
    return {"source_duration": duration or None, "is_sample": True,
            "note": "Stichprobe, keine Vollfilmbewertung und keine statistische Konfidenzaussage.",
            "seconds_basis": "measured_clip_trimmed_window", "results": results}


def enrich_session(data: dict) -> dict:
    """Add transient API fields without changing any stored score or recommendation."""
    analysis = data.get("analysis") or {}
    provenance = data.get("provenance") or analysis.get("provenance") or {}
    duration = (provenance.get("source_info") or {}).get("duration")
    data["archive_context"] = archive_context(data)
    data["source_available"] = data["archive_context"]["source"]["current"]["status"] == "present"
    data["coverage"] = analysis_coverage(analysis, duration)
    data["protected"] = bool(data.get("protected", False))
    return data
