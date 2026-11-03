"""Conservative filesystem allowances and opt-in, clip-only archive retention."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config
from .provenance import atomic_json_write

_LOCK = threading.RLock()
_TEST_CLIP = re.compile(r"^test_[A-Za-z0-9._-]+_s\d+\.mkv$")


def valid_session(name: str) -> bool:
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and not name.startswith(".") and not any(c in name for c in '/\\:\x00')
            and Path(name).name == name)


def session_directory(root: Path, name: str) -> Path:
    """One real direct child only; reject symlink/junction roots and sessions."""
    root = Path(root)
    if not valid_session(name):
        raise ValueError("Invalid session")
    base = root.resolve()
    path = root / name
    if root.is_symlink() or path.is_symlink() or path.resolve().parent != base:
        raise ValueError("Unsafe session directory")
    if hasattr(path, "is_junction") and (path.is_junction() or root.is_junction()):
        raise ValueError("Unsafe junction")
    return path


@contextmanager
def session_guard(session: str):
    """Serialize retention/protection/activation. A stale lock fails closed.

    Exclusive lockfiles also coordinate cooperating app processes. Never remove
    another owner's lock automatically: an operator can inspect it after a crash.
    """
    with _LOCK:
        folder = session_directory(config.PREVIEW_DIR, session)
        folder.mkdir(parents=True, exist_ok=True)
        lock = folder / ".archive-lock"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            yield folder
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)


@contextmanager
def active_session(session: str):
    """Hold an activity marker for the entire analysis/finalization lifetime."""
    token = uuid.uuid4().hex
    with session_guard(session) as folder:
        marker = folder / ".analysis-active"
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token)
    try:
        yield
    finally:
        # The marker itself prevents cleanup while we acquire the short lock.
        try:
            with session_guard(session) as folder:
                marker = folder / ".analysis-active"
                if marker.read_text(encoding="ascii") == token:
                    marker.unlink()
        except (OSError, ValueError):
            pass  # Unknown state remains protected, not optimistically inactive.


def set_session_protected(session: str, protected: bool) -> dict | None:
    """Atomically persist protection; missing sessions are not created as archives."""
    if not isinstance(protected, bool):
        raise ValueError("protected must be boolean")
    try:
        target = session_directory(config.PREVIEW_DIR, session) / "analysis.json"
        if target.is_symlink() or not target.is_file():
            return None
        with session_guard(session) as folder:
            path = folder / "analysis.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            data["protected"] = protected
            atomic_json_write(path, data)
            return {"session": session, "protected": protected}
    except (OSError, ValueError):
        return None


def _active_names(provider) -> set[str] | None:
    try:
        values = provider() if callable(provider) else provider
        if values is None:
            return set() if provider is None else None
        if isinstance(values, str):
            return {values}
        return {str(value) for value in values}
    except Exception:
        return None


def _identity(path: Path) -> dict:
    stat = path.lstat()
    return {"device": stat.st_dev, "inode": stat.st_ino,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _session_candidates(session: str, cutoff: float, active_sessions) -> tuple[list[dict], str]:
    active = _active_names(active_sessions)
    if active is None:
        return [], "active_state_unknown"
    folder = session_directory(config.PREVIEW_DIR, session)
    if session in active or (folder / ".analysis-active").exists():
        return [], "active"
    meta = folder / "analysis.json"
    if meta.is_symlink() or not meta.is_file():
        return [], "metadata_unknown"
    data = json.loads(meta.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("analysis"), dict):
        return [], "metadata_unknown"
    if data.get("protected"):
        return [], "protected"
    created = float(data.get("created", 0))
    # Missing/invalid timestamps are not permission to delete.
    if not math.isfinite(created) or created <= 0 or created > cutoff or meta.stat().st_mtime > cutoff:
        return [], "not_old"
    root = session_directory(config.VMAF_SESSIONS_DIR, session)
    if not root.is_dir():
        return [], "no_clips"
    source = Path(data["source_path"]).resolve() if data.get("source_path") else None
    candidates = []
    for clip in root.iterdir():
        if not _TEST_CLIP.fullmatch(clip.name) or clip.is_symlink() or not clip.is_file():
            continue
        stat = clip.lstat()
        # Hardlinks can refer to user sources/outputs; never operate on those.
        if stat.st_nlink != 1 or stat.st_mtime > cutoff or clip.resolve().parent != root.resolve():
            continue
        if source is not None and clip.resolve() == source:
            continue
        candidates.append({"session": session, "filename": clip.name, **_identity(clip)})
    return candidates, "eligible"


def dry_run_cleanup(*, min_age_days: float, active_sessions=None, now: float | None = None) -> dict:
    """Plan old archived testclips only. Never scan previews or work for deletion.

    active_sessions may be an iterable or a callable returning current session IDs.
    A failed callable skips all affected sessions. Use active_session() for atomic
    protection when adding new backend workflows; the callback is an extra guard.
    """
    age = float(min_age_days)
    if not math.isfinite(age) or age <= 0:
        raise ValueError("min_age_days must be finite and positive")
    timestamp = time.time() if now is None else float(now)
    if not math.isfinite(timestamp):
        raise ValueError("now must be finite")
    plan = {"schema_version": 1, "dry_run": True, "min_age_days": age,
            "created": timestamp, "candidates": [], "skipped": [], "bytes": 0}
    if not config.PREVIEW_DIR.is_dir():
        return plan
    for meta in config.PREVIEW_DIR.glob("*/analysis.json"):
        session = meta.parent.name
        try:
            with session_guard(session):
                candidates, reason = _session_candidates(session, timestamp - age * 86400, active_sessions)
            plan["candidates"].extend(candidates)
            if reason != "eligible":
                plan["skipped"].append({"session": session, "reason": reason})
        except (OSError, ValueError, TypeError):
            plan["skipped"].append({"session": session, "reason": "unsafe_or_busy"})
    plan["bytes"] = sum(c["size_bytes"] for c in plan["candidates"])
    return plan


def apply_cleanup(plan: dict, *, active_sessions=None) -> dict:
    """Revalidate policy, activity and file identity under a lock before unlinking.

    A stale plan cannot remove replaced/modified clips or new files. Reapplying
    the same plan is harmless. JSON/frame logs/screenshots/directories survive.
    """
    age = float(plan["min_age_days"])
    if not math.isfinite(age) or age <= 0:
        raise ValueError("Invalid cleanup plan")
    result = {"dry_run": False, "deleted": 0, "bytes": 0, "skipped": [], "errors": []}
    grouped = {}
    for candidate in plan.get("candidates") or []:
        grouped.setdefault(candidate.get("session", ""), []).append(candidate)
    for session, candidates in grouped.items():
        try:
            with session_guard(session):
                eligible, reason = _session_candidates(session, time.time() - age * 86400, active_sessions)
                allowed = {c["filename"]: c for c in eligible}
                for candidate in candidates:
                    filename = candidate.get("filename", "")
                    if filename not in allowed or allowed[filename] != candidate:
                        result["skipped"].append({"session": session, "filename": filename,
                                                  "reason": reason if reason != "eligible" else "changed_or_missing"})
                        continue
                    # A dynamic provider can become active after the initial scan.
                    active = _active_names(active_sessions)
                    if active is None or session in active:
                        result["skipped"].append({"session": session, "reason": "active_or_unknown"})
                        continue
                    root = session_directory(config.VMAF_SESSIONS_DIR, session)
                    clip = root / filename
                    if clip.is_symlink() or not clip.is_file() or clip.stat().st_nlink != 1 or _identity(clip) != {
                        k: candidate[k] for k in ("device", "inode", "size_bytes", "mtime_ns")
                    }:
                        result["skipped"].append({"session": session, "filename": filename, "reason": "changed_or_missing"})
                        continue
                    clip.unlink()
                    result["deleted"] += 1
                    result["bytes"] += candidate["size_bytes"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["errors"].append({"session": session, "reason": str(exc)})
    return result


def _positive(value) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _filesystem(path: Path) -> dict:
    """Resolve a not-yet-existing target to the nearest existing filesystem."""
    original = str(path)
    try:
        probe = path.resolve()
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        stat = probe.stat()
        usage = shutil.disk_usage(probe)
        # Some network/virtual filesystems provide no usable device identity.
        device = stat.st_dev
        if device is None or device == 0:
            raise OSError("unknown device")
        return {"key": f"device:{device}", "device": device, "path": str(probe),
                "free_bytes": usage.free, "target": original}
    except (OSError, ValueError):
        return {"key": f"unknown:{original}", "device": None, "path": original,
                "free_bytes": None, "target": original}


def storage_plan(items: list[dict], settings: dict, estimates: dict | None = None) -> dict:
    """Planning allowances, not exact sizes or guaranteed upper bounds.

    Includes all batch outputs and conservatively sums temporary allowances for
    every job (safe for parallelism). Existing outputs/originals are never credited
    as free space. Unknown dimensions/fps/duration stay unknown, not zero bytes.
    """
    estimates = estimates or {}
    filesystems = {}
    warnings = []

    def allocation(path: Path, amount, kind: str, source: str, basis: str) -> dict:
        fs = _filesystem(path)
        group = filesystems.setdefault(fs["key"], {
            "device": fs["device"], "path": fs["path"], "free_bytes": fs["free_bytes"],
            "required_bytes": 0, "unknown_components": [], "allocations": []})
        if fs["free_bytes"] is not None:
            group["free_bytes"] = min(group["free_bytes"], fs["free_bytes"]) if group["free_bytes"] is not None else fs["free_bytes"]
        row = {"kind": kind, "source": source, "path": str(path),
               "bytes": int(math.ceil(amount)) if amount is not None else None, "basis": basis}
        group["allocations"].append(row)
        if amount is None:
            group["unknown_components"].append(kind)
        else:
            group["required_bytes"] += row["bytes"]
        return fs

    for item in items:
        source = item["source"]
        estimate = estimates.get(item.get("source_rel")) or estimates.get(source) or {}
        info = dict(estimate.get("source_info") or estimate.get("info") or estimate)
        analysis_enabled = bool(settings.get("vmaf_check", True)) and settings.get("video_mode", "encode") == "encode" and not settings.get("remux_only")
        needs_tests = analysis_enabled or bool(settings.get("verify_vmaf"))
        # Ohne Probe bliebe jede gewöhnliche Vorschau unnötig unbekannt. Die
        # Vorschau läuft im Worker-Thread; vorliegende Metadaten werden genutzt.
        if needs_tests and not all(_positive(info.get(k)) for k in ("duration", "width", "height", "fps")) and Path(source).is_file():
            from .ffmpeg_utils import ffprobe
            probed = ffprobe(Path(source))
            if probed:
                info = {**info, **probed.to_dict()}
        source_size = _positive(info.get("size_bytes"))
        if not source_size:
            try:
                source_size = Path(source).stat().st_size
            except OSError:
                pass
        predicted = _positive(item.get("est_output_bytes"))
        output = predicted * 1.25 if predicted else (source_size * 1.5 if source_size else None)
        compare_only = analysis_enabled and settings.get("workflow") == "compare_only"
        if not compare_only:
            allocation(Path(item["output"]).parent, output, "output", source,
                       "sample_estimate_plus_25_percent" if predicted else "source_size_times_1_5_not_an_upper_bound")
        duration, width, height, fps = [_positive(info.get(k)) for k in ("duration", "width", "height", "fps")]
        if needs_tests:
            count = max(1, min(5, int(_positive(settings.get("samples")) or 1)))
            seconds = max(1, _positive(settings.get("clip_seconds")) or 30) * count
            if settings.get("sample_ranges"):
                seconds = sum(_positive(pair[1]) for pair in settings["sample_ranges"])
            if duration:
                seconds = min(seconds, duration)
            runs = 1 + len(settings.get("compare_encoders") or []) + len(settings.get("compare_variants") or []) + len(settings.get("compare_rows") or [])
            values = max(4, len(settings.get("test_values") or [])) + 1  # optional midpoint
            # FFV1 can be poorly compressible. Budget 8 bytes/pixel/frame incl. overhead.
            references = width * height * fps * seconds * 8 if all((width, height, fps)) else None
            clips = source_size * seconds / duration * runs * values * 2 if duration and source_size else None
            logs = fps * seconds * runs * values * 2048 if fps else None
            visual = bool(settings.get("visual_only"))
            amount = None if (clips is None or (not visual and (references is None or logs is None))) else clips + (0 if visual else references + logs)
            work_fs = allocation(config.WORK_DIR, amount, "vmaf_temp", source,
                                 "raw_reference_plus_testclips_and_frame_logs_all_jobs")
            archive_fs = _filesystem(config.VMAF_SESSIONS_DIR)
            # Same-device rename doesn't duplicate testclips/logs already budgeted in work.
            if config.RETAIN_VMAF_SESSIONS and (work_fs["device"] is None or archive_fs["device"] != work_fs["device"]):
                archived = clips + (logs or 0) if clips is not None and (visual or logs is not None) else None
                allocation(config.VMAF_SESSIONS_DIR, archived, "vmaf_archive", source, "cross_device_copy_of_testclips_and_logs")
        if not compare_only and (settings.get("preserve_dv") or (settings.get("preserve_hdr") and settings.get("codec", "av1") == "hevc")):
            # Dynamic HDR might be present even if source probe metadata is absent.
            temp = source_size + 2 * output + 64 * 1024**2 if source_size and output is not None else None
            allocation(config.WORK_DIR, temp, "hdr_temp", source, "source_stream_two_output_streams_and_metadata_allowance")
            allocation(Path(item["output"]).parent, output, "hdr_remux", source, "second_output_container_during_remux")
        if not compare_only and settings.get("mobile_copy"):
            allocation(Path(item["output"]).parent, None, "additional_outputs", source, "workflow_specific_size_unknown")
        if not compare_only and settings.get("chunked"):
            allocation(config.WORK_DIR, None, "chunk_temp", source, "workflow_specific_size_unknown")
        if settings.get("generate_screenshots", True) and analysis_enabled:
            allocation(config.PREVIEW_DIR, None, "screenshots", source, "image_size_unknown")

    for fs in filesystems.values():
        known_insufficient = fs["free_bytes"] is not None and fs["required_bytes"] > fs["free_bytes"]
        fs["status"] = "insufficient" if known_insufficient else (
            "unknown" if fs["free_bytes"] is None or fs["unknown_components"] else "estimated_sufficient")
        fs["required_is_lower_bound"] = bool(fs["unknown_components"])
        if fs["status"] in ("insufficient", "unknown"):
            warnings.append({"code": "storage_" + fs["status"], "path": fs["path"], "device": fs["device"]})
    return {"schema_version": 1, "is_estimate": True, "filesystems": list(filesystems.values()),
            "warnings": warnings, "note": "Konservative Planungsreserve, keine garantierte Obergrenze. Unbekannte Anteile bleiben offen; Quellen/alte Outputs werden nicht als freier Platz angerechnet."}
