"""Best-effort output audit. Metadata/sample checks are never a full decode.

This module is observational: it does not replace the existing integrity,
quality, cap or original-file safety decisions. Detail values are stable
translation keys, not claims that stream payloads were fully verified.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path

from . import config


STATUSES = ("ok", "warn", "fail", "not_checked")


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def probe_chapters(path: Path) -> tuple[list | None, str | None]:
    """Unlike the editor probe, failure is not an empty chapter list."""
    try:
        result = subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_chapters", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
        if result.returncode:
            return None, (result.stderr or f"Exit {result.returncode}").strip()[-500:]
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("chapters"), list):
            return None, "invalid_chapter_probe"
        chapters = []
        for chapter in data["chapters"]:
            start = _number(chapter.get("start_time"), None)
            end = _number(chapter.get("end_time"), None)
            if start is None or end is None:
                return None, "invalid_chapter_times"
            chapters.append({"start": start, "end": end,
                             "title": (chapter.get("tags") or {}).get("title", "")})
        return chapters, None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError) as exc:
        return None, str(exc)[-500:]


def _codec(value):
    aliases = {"libsvtav1": "av1", "libaom-av1": "av1", "libx265": "hevc",
               "libx264": "h264", "libvpx-vp9": "vp9", "libopus": "opus",
               "srt": "subrip", "libfdk_aac": "aac"}
    value = str(value or "")
    return aliases.get(
        value,
        value.split("_", 1)[0] if value.endswith(("_nvenc", "_qsv", "_vaapi", "_amf")) else value,
    )


def _option(command, *names):
    options = command.get("options") or {}
    for name in names:
        if options.get(name):
            return options[name][-1]
    return None


def _stream_option(command, name, kind, index):
    return _option(command, f"{name}:{kind}:{index}", f"{name}:{kind}", name)


def _planned_tracks(command, sources, kind):
    """Resolve only explicit, understood stream maps; never invent selections."""
    if ("-an" if kind == "a" else "-sn") in (command.get("options") or {}):
        return []
    maps = command.get("maps") or []
    if not maps:
        return None
    field = "audio" if kind == "a" else "subtitles"
    selected = []
    for mapping in maps:
        if mapping.startswith(("-", "[")):
            return None
        match = re.fullmatch(r"(\d+):([vasdt])(?::(\d+))?\??", mapping)
        if not match:
            return None
        input_id, stream_kind, stream_index = match.groups()
        if stream_kind != kind:
            continue
        src = sources.get(int(input_id))
        if src is None:
            return None
        tracks = src.get(field) or []
        if stream_index is None:
            chosen = tracks
        else:
            chosen = [entry for i, entry in enumerate(tracks)
                      if int(entry.get("index", i)) == int(stream_index)]
            if not chosen and not mapping.endswith("?"):
                return None
        for entry in chosen:
            index = len(selected)
            track = {key: entry.get(key) for key in
                     ("codec", "language", "title", "default", "forced", "channels") if key in entry}
            track.update(index=index, source_input=int(input_id), source_index=entry.get("index"))
            encoder = _stream_option(command, "-c", kind, index)
            if encoder and encoder != "copy":
                track["codec"] = _codec(encoder)
            elif not encoder:
                track["codec"] = None  # FFmpeg's container default is not a verified plan.
            channels = _stream_option(command, "-ac", kind, index)
            if channels is not None:
                track["channels"] = int(_number(channels))
            disposition = _option(command, f"-disposition:{kind}:{index}", f"-disposition:{kind}")
            if disposition is not None and not disposition.startswith(("+", "-")):
                track["default"] = "default" in disposition.split("+")
                track["forced"] = "forced" in disposition.split("+")
            else:
                # FFmpeg may mark the first stream default automatically.
                track["default"] = True if entry.get("default") else None
            for metadata in ((command.get("options") or {}).get(f"-metadata:s:{kind}:{index}") or []):
                key, _, value = str(metadata).partition("=")
                if key in ("language", "title"):
                    track[key] = value
            selected.append(track)
    return selected


def build_plan(source: dict, settings: dict, command: dict, *,
               sources: dict | None = None, chapters: list | None = None,
               estimate: dict | None = None) -> dict:
    sources = sources if sources is not None else {0: source}
    enc = command.get("encoder")
    copy = enc == "copy"
    codec = source.get("codec") if copy else _codec(enc) or None
    width, height = int(source.get("width") or 0), int(source.get("height") or 0)
    if not enc:
        width, height = 0, 0  # No command means no verified output geometry plan.
    filters = command.get("filters") or ""
    crop = re.search(r"(?:^|,)crop=(\d+):(\d+):", filters)
    if crop:
        width, height = int(crop[1]), int(crop[2])
    scale = re.search(r"(?:^|,)scale(?:_cuda)?=-2:(\d+)", filters)
    if scale and height:
        target = int(scale[1])
        width, height = int(math.floor(width * target / height / 2 + 0.5) * 2), target
    simple = settings.get("video_mode", "encode") not in ("concat", "editor", "split")
    if ("-filter_complex" in (command.get("options") or {})
            or re.search(r"(?:^|,)(?:pad|transpose|rotate|zoompan|v360)=", filters)
            or (re.search(r"(?:^|,)scale(?:_\w+)?=", filters) and not scale)):
        width, height = 0, 0
    video = {"codec": codec, "resolution": [width, height] if simple and width and height else None}
    for key, flag in (("color_transfer", "-color_trc"), ("color_primaries", "-color_primaries"),
                      ("color_space", "-colorspace"), ("color_range", "-color_range")):
        video[key] = source.get(key) if copy else _option(command, flag)
    pix_fmt = _option(command, "-pix_fmt")
    video["bit_depth"] = source.get("bit_depth") if copy else (
        10 if "p010" in (pix_fmt or filters) or "yuv420p10" in (pix_fmt or filters)
        else 8 if pix_fmt in ("yuv420p", "nv12") else None)
    video["dolby_vision"] = source.get("dolby_vision") if copy else (
        bool(settings.get("preserve_dv") and source.get("dolby_vision")) if enc else None)
    video["hdr10_plus"] = source.get("hdr10_plus") if copy else (bool(
        source.get("hdr10_plus") and settings.get("preserve_hdr") and not settings.get("tonemap")) if enc else None)
    chapter_map = _option(command, "-map_chapters")
    expected_chapters = [] if chapter_map == "-1" else chapters
    duration = _number(source.get("duration")) if simple else None
    length = _option(command, "-t")
    start = _number(_option(command, "-ss"))
    end = _option(command, "-to")
    if length is not None:
        duration = min(max(0, duration - start), _number(length)) if duration else _number(length)
    elif end is not None:
        duration = max(0, _number(end) - start)
    elif start and duration:
        duration = max(0, duration - start)
    if start or length or end or not simple:
        if chapter_map != "-1":
            expected_chapters = None  # Chapter timeline edits need a separate audit.
    return {"video": video, "audio": _planned_tracks(command, sources, "a"),
            "subtitles": _planned_tracks(command, sources, "s"),
            "chapters": expected_chapters, "duration": duration,
            "size_estimate": estimate, "size_target_bytes": int(_number(settings.get("size_target_mb")) * 1024 * 1024) or None}


def _summary(checks: list) -> dict:
    counts = {status: sum(c["status"] == status for c in checks) for status in STATUSES}
    status = next((s for s in ("fail", "warn") if counts[s]), "ok" if counts["ok"] else "not_checked")
    return {"status": status, "counts": counts,
            "checked": sum(c["checked"] for c in checks), "not_checked": counts["not_checked"],
            "complete": False, "scope": "metadata_and_existing_sample_checks"}


def record_postprocessing(report: dict, mode: str, output_path: str, *,
                          error: str = "", blocked: bool = False) -> None:
    """Keep the pre-move audit, but expose the final path and later failures."""
    report.setdefault("audited_path", report.get("output_path", ""))
    report["output_path"] = output_path
    status = "warn" if error or blocked else "ok"
    check = {"key": "output.postprocess", "status": status, "checked": True,
             "expected": mode, "actual": "blocked" if blocked else mode,
             "detail": "original_file_postprocessing", "error": error or None}
    report["checks"] = [c for c in report.get("checks", []) if c["key"] != check["key"]] + [check]
    if error:
        report.setdefault("errors", []).append({"key": check["key"], "error": error})
    report["summary"] = _summary(report["checks"])


def build_report(plan: dict, actual: dict | None, *, output_path: str = "",
                 actual_chapters: list | None = None, integrity_ok: bool | None = None,
                 integrity_msg: str = "", vmaf_score: float | None = None,
                 vmaf_target: float | None = None, caps_failed: bool = False,
                 caps_enabled: bool = False, caps_checked: bool = True,
                 probe_error: str | None = None,
                 chapter_error: str | None = None, errors: list | None = None,
                 operation_error: str = "") -> dict:
    checks = []
    errors = list(errors or [])
    if probe_error:
        errors.append({"key": "output.probe", "error": str(probe_error)})
    if chapter_error:
        errors.append({"key": "chapters.probe", "error": str(chapter_error)})

    def add(key, expected, observed, detail="metadata_only", *, mismatch="warn", status=None):
        if status is None:
            status = "not_checked" if expected is None or observed is None else (
                "ok" if expected == observed else mismatch)
        checks.append({"key": key, "status": status, "checked": status != "not_checked",
                       "expected": expected, "actual": observed, "detail": detail})

    add("output.probe", True, bool(actual), "output_metadata_probe", mismatch="fail")
    if actual is not None:
        expected_video = plan.get("video") or {}
        observed_video = dict(actual, resolution=[actual.get("width"), actual.get("height")])
        for key in ("codec", "resolution", "bit_depth", "color_transfer", "color_primaries",
                    "color_space", "color_range", "dolby_vision", "hdr10_plus"):
            observed = observed_video.get(key)
            if observed in ("", "?", "unknown"):
                observed = None
            add("video." + key, expected_video.get(key), observed,
                mismatch="fail" if key in ("codec", "resolution") else "warn")
        expected_duration, duration = plan.get("duration"), actual.get("duration")
        delta = abs(_number(duration) - _number(expected_duration))
        tolerance = max(2.0, _number(expected_duration) * 0.01)
        add("output.duration", expected_duration, duration, "duration_tolerance_2s_or_1pct",
            status=("not_checked" if not expected_duration or not duration
                    else "ok" if delta <= tolerance else "warn"))
        for kind in ("audio", "subtitles"):
            expected_tracks, tracks = plan.get(kind), actual.get(kind)
            add(kind + ".count", len(expected_tracks) if expected_tracks is not None else None,
                len(tracks) if tracks is not None else None, mismatch="fail")
            for index, track in enumerate(expected_tracks or []):
                observed = tracks[index] if tracks is not None and index < len(tracks) else {}
                for key in ("codec", "language", "title", "default", "forced", "channels"):
                    if key == "channels" and kind != "audio":
                        continue
                    add(f"{kind}.{index}.{key}", track.get(key), observed.get(key))
    else:
        for key in ("video.codec", "video.resolution", "video.dolby_vision", "video.hdr10_plus",
                    "audio.count", "subtitles.count", "output.duration"):
            add(key, None, None, "output_probe_unavailable")
    add("chapters.probe", True, actual_chapters is not None, "chapter_metadata_probe",
        status="fail" if chapter_error else "ok" if actual_chapters is not None else "not_checked")
    expected_chapters = plan.get("chapters")
    add("chapters.count", len(expected_chapters) if expected_chapters is not None else None,
        len(actual_chapters) if actual_chapters is not None else None)
    if expected_chapters is not None and actual_chapters is not None:
        matched = len(expected_chapters) == len(actual_chapters) and all(
            abs(_number(a.get("start")) - _number(b.get("start"))) <= 0.1
            and abs(_number(a.get("end")) - _number(b.get("end"))) <= 0.1
            and str(a.get("title") or "") == str(b.get("title") or "")
            for a, b in zip(expected_chapters, actual_chapters))
        add("chapters.timeline", expected_chapters, actual_chapters, "chapter_metadata_tolerance_100ms",
            status="ok" if matched else "warn")
    else:
        add("chapters.timeline", expected_chapters, actual_chapters, "chapter_timeline_unavailable")
    estimate = plan.get("size_estimate") or {}
    estimated = _number(estimate.get("bytes"), None)
    estimated = estimated if estimated is not None and estimated > 0 else None
    size = _number(actual.get("size_bytes"), None) if actual else None
    deviation = (100 * (size - estimated) / estimated) if size is not None and estimated else None
    add("output.size_estimate", estimated, size, "estimate_not_guarantee",
        status="not_checked" if deviation is None else "ok" if abs(deviation) <= 15 else "warn")
    target = plan.get("size_target_bytes")
    add("output.size_target", target, size, "size_target_not_cap",
        status="not_checked" if not target or size is None else "ok" if size <= target else "warn")
    add("integrity.samples", True, integrity_ok, "sample_decode_and_duration_not_full_decode",
        mismatch="fail")
    vmaf_target, vmaf_score = _number(vmaf_target, None), _number(vmaf_score, None)
    add("quality.vmaf", vmaf_target, vmaf_score, "sample_vmaf_not_full_video",
        status="not_checked" if vmaf_target is None or vmaf_score is None else (
            "ok" if vmaf_score >= vmaf_target else "warn"))
    add("output.caps", False if caps_enabled else None,
        caps_failed if caps_enabled and (caps_checked or caps_failed) else None,
        "existing_cap_check", mismatch="fail")
    if operation_error:
        add("operation.warning", None, operation_error, "existing_job_warning", status="warn")
    if errors:
        add("report.errors", [], errors, "audit_errors", status="fail")
    for key in ("decode.full", "audio.payload", "subtitles.payload", "hdr.dynamic_payload", "attachments.payload"):
        add(key, None, None, "not_performed")
    return {"schema_version": 1, "generated_at": time.time(), "output_path": output_path,
            "checks": checks, "summary": _summary(checks),
            "planned": plan, "actual": actual,
            "size": {"estimated_bytes": estimated, "estimate_basis": estimate.get("basis"),
                     "actual_bytes": size, "deviation_pct": round(deviation, 2) if deviation is not None else None},
            "probe_error": probe_error, "chapter_error": chapter_error,
            "integrity_message": integrity_msg, "errors": errors}
