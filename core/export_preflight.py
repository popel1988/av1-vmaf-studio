"""Server-owned export planning and checks, shared by preview and execution."""
from __future__ import annotations

from dataclasses import asdict
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

from . import config, job_plan


class ExportRejected(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("; ".join(x["message"] for x in report["blockers"]))


def identity(path):
    p = Path(path)
    stat = p.stat()
    return {"path": str(p.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev, "inode": stat.st_ino}


def fingerprint(path, settings, span=None):
    value = {"source": identity(path), "settings": asdict(settings)}
    # Internal test metadata is not part of the requested final settings.
    value["settings"].pop("test_export", None)
    if span is not None:
        value["span"] = span
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def key(path):
    return os.path.normcase(str(Path(path).resolve()))


def overlaps(a, b):
    """Patterns only originate here; user filename metacharacters are escaped."""
    if a.get("family") and b.get("family"):
        return key(a["path"]) == key(b["path"])
    if a.get("family"):
        return Path(key(a["path"])).parent == Path(key(b["path"])).parent and fnmatch.fnmatchcase(
            Path(key(b["path"])).name, a["family"])
    if b.get("family"):
        return overlaps(b, a)
    if key(a["path"]) == key(b["path"]):
        return True
    try:
        return Path(a["path"]).samefile(b["path"])
    except OSError:
        return False


def destinations(path, settings, info=None):
    src = Path(path)
    test = getattr(settings, "test_export", None) or {}
    if settings.workflow == "compare_only" and not test:
        return []
    if test:
        base = Path(test["directory"]) / ("test" + job_plan._container_ext(settings))
    else:
        base = job_plan.planned_output_path(src, settings)
    result = [{"path": str(base), "role": "main"}]
    if settings.video_mode == "split":
        spec = settings.edit_spec or {}
        if spec.get("split_mode") == "range":
            from . import remux
            result = [{"path": str(out), "role": "split"} for _, out in remux.build_cut_cmds(
                info or SimpleNamespace(path=str(src)), base.parent,
                spec.get("split_ranges") or [], base.suffix)]
        else:
            pattern = base.with_name(f"{src.stem}_%03d{base.suffix}")
            literal = os.path.normcase(src.stem + "_")
            family = "".join("[" + c + "]" if c in "*?[" else c for c in literal)
            result = [{"path": str(pattern), "role": "split", "family": family + "[0-9]*" + base.suffix}]
    if settings.mobile_copy and settings.video_mode == "encode" and not settings.chunked:
        mobile = (src.with_name(f"{src.stem}_mobile.mp4")
                  if settings.post_processing == "inplace" and not test
                  else base.with_name(f"{base.stem}_mobile.mp4"))
        result.append({"path": str(mobile), "role": "mobile"})
    if not test and settings.post_processing == "inplace":
        result.append({"path": str(src.with_suffix(base.suffix)), "role": "replacement"})
    if not test and settings.post_processing == "archive":
        result.append({"path": str(src.parent / config.ARCHIVE_DIRNAME / src.name), "role": "archive"})
    return result


def test_support(settings, info):
    reasons = []
    if settings.vmaf_check:
        reasons.append("Für den Kurzexport zuerst das VMAF-Ergebnis als feste Qualität übernehmen.")
    if settings.video_mode != "encode":
        reasons.append("Nur reguläre Einzeldatei-Encodes unterstützen den exakten Kurzexport.")
    if settings.chunked:
        reasons.append("Chunked-Encoding unterstützt keinen identischen Kurzexport.")
    if settings.preserve_dv:
        reasons.append("Zeitgenaue Dolby-Vision-Metadatenübernahme ist beim Kurzexport nicht unterstützt.")
    if info.get("hdr10_plus") and settings.preserve_hdr:
        reasons.append("Zeitgenaue HDR10+-Metadatenübernahme ist beim Kurzexport nicht unterstützt.")
    if settings.verify_vmaf:
        reasons.append("VMAF-Guardrail auf vollständiger Ausgabe ist beim Kurzexport nicht unterstützt.")
    if settings.edit_spec:
        reasons.append("Zusammengesetzte Quellen unterstützen keinen identischen Kurzexport.")
    return {"supported": not reasons, "reasons": reasons}


def source_paths(path, settings):
    """Protect all referenced editor/concat inputs as well as the primary source."""
    paths = [Path(path)]
    spec = settings.edit_spec or {}
    for row in (spec.get("segments") or []) + (spec.get("overlays") or []):
        value = row.get("path") or row.get("source")
        if value:
            from .editor import resolve_path
            resolved = resolve_path(value)
            if resolved:
                paths.append(Path(resolved))
    for value in ((spec.get("paths") or []) + (spec.get("concat_paths") or [])
                  + (spec.get("concat_files") or []) + (spec.get("playlist_clips") or [])):
        resolved = config.resolve_input(value)
        if resolved:
            paths.append(Path(resolved))
    for value in [spec.get("bgm_path")] + [
            x.get("path") for x in (spec.get("external") or []) if isinstance(x, dict)]:
        if value:
            from .editor import resolve_path
            resolved = resolve_path(value)
            if resolved:
                paths.append(Path(resolved))
    return paths


def inspect(path, settings, *, info=None, reservations=(), protected_sources=()):
    """No writes, no encoder subprocess. Unknown disk/probe state is visible."""
    src = Path(path)
    media = info.to_dict() if hasattr(info, "to_dict") else dict(info or {})
    blockers, warnings = [], []
    def issue(target, code, message, **extra):
        target.append({"code": code, "message": message, **extra})
    if not src.exists():
        issue(blockers, "source_missing", "Quelle nicht gefunden.")
    if settings.out_mode == "custom" and not getattr(settings, "test_export", None):
        clean = config.safe_subdir(settings.out_subdir)
        if not clean or config.resolve_input(clean) is None or ".." in settings.out_subdir.replace("\\", "/").split("/"):
            issue(blockers, "invalid_destination", "Benutzerdefinierter Zielordner ist ungültig.")
    outputs = destinations(src, settings, info)
    sources = source_paths(src, settings)
    for source in sources:
        for reserved in reservations:
            if overlaps({"path": str(source)}, reserved):
                issue(blockers, "source_reserved", "Quelle ist als Ausgabe eines anderen Auftrags reserviert.",
                      path=str(source))
    test = getattr(settings, "test_export", None) or {}
    if not test and settings.post_processing == "inplace" and not settings.approve_inplace:
        issue(blockers, "inplace_consent_required", "Original ersetzen erfordert ausdrückliche Freigabe.")
    for idx, out in enumerate(outputs):
        p = Path(out["path"])
        for source in sources:
            if overlaps(out, {"path": str(source)}):
                intentional = (out["role"] == "replacement" and key(source) == key(src)
                               and settings.approve_inplace and not test)
                if not intentional:
                    issue(blockers, "source_is_destination", "Ausgabe würde eine Quelle überschreiben.", path=str(p))
        for source in protected_sources:
            if overlaps(out, {"path": str(source)}):
                issue(blockers, "source_in_use", "Ausgabe würde die Quelle eines anderen Auftrags verändern.",
                      path=str(p))
        if any(overlaps(out, earlier) for earlier in outputs[:idx]):
            issue(blockers, "duplicate_destination", "Mehrere Ausgaben verwenden dasselbe Ziel.", path=str(p))
        for reserved in reservations:
            if overlaps(out, reserved):
                issue(blockers, "destination_reserved", "Ziel ist durch einen anderen Auftrag reserviert.", path=str(p))
        if out.get("family"):
            existing = [q for q in p.parent.iterdir() if fnmatch.fnmatchcase(os.path.normcase(q.name), out["family"])] if p.parent.is_dir() else []
        else:
            existing = [p] if p.exists() or p.is_symlink() else []
        out["existing"] = [str(q) for q in existing]
        for q in existing:
            intentional = out["role"] == "replacement" and key(q) == key(src) and settings.approve_inplace
            if q.is_dir():
                issue(blockers, "destination_directory", "Ausgabeziel ist ein Verzeichnis.", path=str(q))
            elif not intentional and settings.on_duplicate != "overwrite":
                issue(blockers, "overwrite_consent_required", "Vorhandenes Ausgabeziel erfordert Überschreibfreigabe.", path=str(q))
            elif not intentional:
                issue(warnings, "overwrite", "Vorhandene Ausgabe wird ausdrücklich überschrieben.", path=str(q))
    if settings.preserve_dv and (settings.codec not in ("hevc", "av1") or
                                (settings.codec == "av1" and settings.platform != "cpu")):
        issue(blockers, "unsupported_dv", "Gewählter Encoder unterstützt Dolby-Vision-Erhalt nicht.")
    if settings.preserve_dv and (settings.video_mode == "concat" or
                                (settings.video_mode == "editor" and settings.edit_spec.get("mode") != "remux")):
        issue(blockers, "unsupported_dv", "Timeline-/Concat-Exporte unterstützen keine Dolby-Vision-Neuberechnung.")
    if media.get("dolby_vision") and settings.preserve_dv and settings.codec == "av1":
        filters = (settings.mobile_copy or settings.autocrop or
                   (settings.target_height and settings.target_height < media.get("height", 0)) or
                   any(getattr(settings, attr) != "off" for attr in ("denoise", "sharpen", "grain", "deband")) or
                   settings.deinterlace == "on" or (settings.deinterlace == "auto" and media.get("interlaced")))
        if filters:
            issue(blockers, "unsupported_dv_filters", "AV1-Dolby-Vision ist mit diesen Videofiltern nicht exakt erhaltbar.")
    if media.get("dolby_vision") and not settings.preserve_dv and settings.video_mode == "encode":
        issue(warnings, "dv_loss", "Dolby-Vision-Metadaten entfallen.")
    if media.get("is_hdr") and settings.video_mode == "encode" and not settings.preserve_hdr:
        issue(warnings, "hdr_loss", "HDR wird in SDR umgewandelt." if settings.tonemap else "HDR wird nicht erhalten.")
    if media.get("hdr10_plus") and not settings.preserve_hdr and settings.video_mode == "encode":
        issue(warnings, "hdr10plus_loss", "HDR10+-Metadaten entfallen.")
    for index, track in enumerate(media.get("audio") or []):
        index = track.get("index", index)
        cfg = next((x for x in settings.audio_track_settings if x.get("index") == index), {})
        removed = (settings.audio_mode == "none" or
                   (settings.audio_per_track and not cfg) or
                   (not settings.audio_per_track and bool(settings.audio_tracks) and index not in settings.audio_tracks))
        if settings.video_mode == "edit":
            cfg = next((x for x in settings.edit_spec.get("audio", [])
                        if x.get("index") == index and not x.get("path") and not x.get("external")), {})
            removed = not cfg or cfg.get("keep") is False
        transcoded = ((cfg.get("mode", "copy") == "encode" or cfg.get("channels") or cfg.get("normalize"))
                      if settings.audio_per_track else
                      (settings.audio_mode == "encode" or settings.audio_channels or settings.audio_normalize))
        if settings.video_mode == "edit":
            from .remux import _codec_ok_in_container
            transcoded = cfg.get("transcode") or (job_plan._container_ext(settings) == ".mp4"
                          and not _codec_ok_in_container(track.get("codec", ""), "mp4", "audio"))
        if removed:
            issue(warnings, "audio_removed", "Tonspur entfällt.", track=index)
        elif transcoded:
            issue(warnings, "audio_transcoded", "Tonspur wird neu kodiert; Qualitäts-/Atmos-Verlust möglich.", track=index)
    for index, track in enumerate(media.get("subtitles") or []):
        index = track.get("index", index)
        cfg = next((x for x in settings.subtitle_track_settings if x.get("index") == index), {})
        removed = not cfg if settings.subtitle_per_track else not settings.keep_subtitles
        if settings.video_mode == "edit":
            cfg = next((x for x in settings.edit_spec.get("subtitles", [])
                        if x.get("index") == index and not x.get("path") and not x.get("external")), {})
            removed = not cfg or cfg.get("keep") is False
        if removed:
            issue(warnings, "subtitle_removed", "Untertitelspur entfällt.", track=index)
        elif job_plan._container_ext(settings) == ".mp4" and track.get("codec") in (
                "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "dvbsub", "xsub"):
            issue(warnings, "subtitle_container_loss", "Bilduntertitel sind im MP4-Ziel nicht übernehmbar.", track=index)
        elif job_plan._container_ext(settings) == ".mp4" and track.get("codec") in ("ass", "ssa"):
            issue(warnings, "subtitle_styling_loss", "MP4-Textuntertitel verlieren ASS/SSA-Gestaltung.", track=index)
    disk = []
    seen = set()
    for out in outputs:
        parent = Path(out["path"]).parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.is_dir():
            issue(blockers, "destination_parent_not_directory", "Ein Zielpfad-Bestandteil ist kein Ordner.",
                  path=str(parent))
            continue
        if key(parent) in seen:
            continue
        seen.add(key(parent))
        try:
            free = shutil.disk_usage(parent).free
            # CQ output size is unknowable: report a conservative estimate, not a guarantee.
            estimated = int(media.get("size_bytes") or (src.stat().st_size if src.is_file() else 0))
            if test and media.get("duration"):
                estimated = int(estimated * test["seconds"] / media["duration"])
            estimated *= 2 if settings.mobile_copy else 1
            reserve = max(16 * 1024 * 1024, estimated)
            disk.append({"path": str(parent), "free_bytes": free, "estimated_required_bytes": reserve,
                         "estimate_only": True})
            if free < reserve:
                issue(blockers, "disk_space", "Geschätzter freier Speicher reicht nicht aus.", path=str(parent))
            if not os.access(parent, os.W_OK):
                issue(blockers, "destination_not_writable", "Zielordner ist nicht beschreibbar.", path=str(parent))
        except OSError:
            disk.append({"path": str(parent), "free_bytes": None, "estimate_only": True})
            issue(warnings, "disk_unknown", "Freier Speicher konnte nicht ermittelt werden.")
    return {"schema_version": 1, "ok": not blockers, "settings": asdict(settings),
            "fingerprint": fingerprint(src, settings) if src.is_file() else None,
            "outputs": outputs, "blockers": blockers, "warnings": warnings, "disk": disk,
            "test_export": test_support(settings, media)}


def require(report):
    if report["blockers"]:
        raise ExportRejected(report)
    return report
