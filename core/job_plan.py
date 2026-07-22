"""Pre-Flight: Naming-Templates, Output-Planung, Dry-Run, Duplikat-Check."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, history


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    s = _UNSAFE.sub("_", (name or "").strip())
    s = s.strip(" .")
    return s or "output"


def render_name(pattern: str, *, stem: str, suffix: str = "", codec: str = "",
                height: int = 0, vmaf: float = 0.0, ext: str = ".mkv") -> str:
    """Dateiname aus Pattern rendern (ohne Verzeichnis, mit Extension)."""
    pat = (pattern or "{stem}{suffix}").strip() or "{stem}{suffix}"
    h = int(height or 0)
    height_s = f"{h}p" if h else ""
    height_suffix = f"_{h}p" if h else ""
    vals = {
        "stem": stem,
        "suffix": suffix or "",
        "codec": (codec or "").lower(),
        "height": height_s,
        "height_suffix": height_suffix,
        "vmaf": f"{vmaf:.1f}" if vmaf else "",
        "date": datetime.now().strftime("%Y%m%d"),
    }
    try:
        name = pat.format(**vals)
    except (KeyError, ValueError):
        name = f"{stem}{suffix}"
    name = sanitize_filename(name)
    if ext and not name.lower().endswith(ext.lower()):
        # Extension aus Pattern entfernen falls doppelt, dann anhängen
        for e in (".mkv", ".mp4", ".m4v", ".webm"):
            if name.lower().endswith(e):
                name = name[: -len(e)]
                break
        name = sanitize_filename(name) + ext
    return name


def _container_ext(settings) -> str:
    from .queue_manager import _container_ext as _ce
    return _ce(settings)


def planned_output_path(src: Path, settings) -> Path:
    """Zielpfad wie queue_manager._output_path, ohne QueueItem."""
    from .queue_manager import _effective_out_mode

    ext = _container_ext(settings)
    if getattr(settings, "post_processing", "keep") == "inplace":
        return src.with_name(f"{src.stem}.__tmp__{ext}")

    height = int(getattr(settings, "target_height", 0) or 0)
    pattern = getattr(settings, "name_pattern", "") or "{stem}{suffix}"
    name = render_name(
        pattern,
        stem=src.stem,
        suffix=getattr(settings, "suffix", "") or "",
        codec=getattr(settings, "codec", "") or "",
        height=height,
        ext=ext,
    )
    mode = _effective_out_mode(settings)
    sub = config.safe_subdir(getattr(settings, "out_subdir", ""))

    if mode == "beside":
        return src.parent / name
    if mode == "custom" and sub:
        base = config.resolve_input(sub) or config.default_output_path()
        return base / name
    base = config.default_output_path()
    rel = config.rel_input(src) or src.name
    return (base / rel).with_name(name)


def plan_one(src_path: str, settings, *, est_saved_bytes: int = 0,
             est_output_bytes: int = 0) -> dict:
    """Einen geplanten Job beschreiben (ohne Einreihen)."""
    src = Path(src_path)
    out = planned_output_path(src, settings)
    exists = out.exists()
    hist = history.is_processed(str(src.resolve()) if src.exists() else str(src))
    rel_src = config.rel_input(src) if src.exists() else None
    rel_out = config.rel_input(out)
    return {
        "source": str(src),
        "source_rel": rel_src or src.name,
        "source_name": src.name,
        "output": str(out),
        "output_rel": rel_out or out.name,
        "output_name": out.name,
        "exists": exists,
        "history_done": hist,
        "duplicate": bool(exists or hist),
        "est_saved_bytes": int(est_saved_bytes or 0),
        "est_output_bytes": int(est_output_bytes or 0),
        "codec": getattr(settings, "codec", ""),
        "out_mode": getattr(settings, "out_mode", "default"),
        "suffix": getattr(settings, "suffix", ""),
        "name_pattern": getattr(settings, "name_pattern", "{stem}{suffix}"),
    }


def next_free_suffix(src: Path, settings) -> str:
    """Suffix finden, dessen geplanter Output noch nicht existiert (_remux → _remux2)."""
    from .queue_manager import JobSettings, build_job_settings

    base = str(getattr(settings, "suffix", "") or "")
    # Settings als dict, damit wir Suffix austauschen können.
    if isinstance(settings, JobSettings):
        d = dict(settings.__dict__)
    else:
        d = dict(settings or {})
    for n in range(2, 100):
        trial = f"{base}{n}"
        d["suffix"] = trial
        out = planned_output_path(src, build_job_settings(d))
        if not out.exists():
            return trial
    # Fallback: Zeitstempel
    from datetime import datetime as _dt
    return f"{base}_{_dt.now().strftime('%H%M%S')}"


def apply_requeue_conflict(settings_dict: dict, src_path: str, mode: str) -> tuple[dict, dict]:
    """Settings für Requeue anpassen.

    mode:
      - overwrite: gleiches Ziel, Ausgabe wird ersetzt
      - suffix: freien Suffix wählen (_remux2, …), wenn Ziel existiert
    Rückgabe: (settings_dict, plan_info)
    """
    from .queue_manager import build_job_settings

    d = dict(settings_dict or {})
    src = Path(src_path)
    settings = build_job_settings(d)
    plan = plan_one(str(src), settings)
    mode = (mode or "overwrite").lower().strip()
    if mode == "suffix" and plan.get("exists"):
        d["suffix"] = next_free_suffix(src, settings)
        d["on_duplicate"] = "overwrite"
        settings = build_job_settings(d)
        plan = plan_one(str(src), settings)
        plan["conflict_mode"] = "suffix"
    else:
        d["on_duplicate"] = "overwrite"
        plan["conflict_mode"] = "overwrite"
    return d, plan


def preview_batch(paths: list[str], settings_dict: dict,
                  estimates: Optional[dict] = None) -> dict:
    """Dry-Run für mehrere Pfade. estimates: rel/abs path -> {est_saved_bytes,...}."""
    from .queue_manager import build_job_settings

    estimates = estimates or {}
    settings = build_job_settings(settings_dict or {})
    items = []
    dup_count = 0
    for p in paths or []:
        target = config.resolve_input(p) if not Path(p).is_absolute() else Path(p)
        if target is None:
            target = Path(p)
        key = config.rel_input(target) if target.exists() else p
        est = estimates.get(key) or estimates.get(p) or {}
        row = plan_one(
            str(target), settings,
            est_saved_bytes=est.get("est_saved_bytes", 0),
            est_output_bytes=est.get("est_output_bytes", 0),
        )
        if row["duplicate"]:
            dup_count += 1
        items.append(row)
    return {
        "count": len(items),
        "duplicates": dup_count,
        "items": items,
        "on_duplicate": getattr(settings, "on_duplicate", "ask") or "ask",
    }
