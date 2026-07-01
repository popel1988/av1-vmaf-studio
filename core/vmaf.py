"""VMAF-Analyse-Pipeline mit flexiblen Testwerten, Bitrate-Modus und Screenshots."""
from __future__ import annotations

import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config
from . import ffmpeg_utils as ff
from .encoder import build_encode_cmd, _TONEMAP_CHAIN
from .ffmpeg_utils import VideoInfo

logger = logging.getLogger("vcompress.vmaf")


def _run_logged(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        logger.error("%s fehlgeschlagen (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                     label, res.returncode, " ".join(cmd), (res.stderr or "")[-3000:])
    return res


@dataclass
class VmafOptions:
    """Konfiguration für einen VMAF-Lauf."""
    rate_mode: str = "cq"
    test_values: list = field(default_factory=lambda: [20, 24, 28, 32])
    clip_seconds: int = 30
    generate_screenshots: bool = True
    item_id: str = ""


@dataclass
class VmafResult:
    value: int
    rate_mode: str
    label: str
    vmaf: float
    clip_size_bytes: int
    predicted_size_bytes: int
    savings_percent: float
    recommended: bool = False
    screenshot_ref: str = ""
    screenshot_enc: str = ""

    def to_dict(self) -> dict:
        d = {
            "value": self.value,
            "quality": self.value,  # Rückwärtskompatibilität UI
            "rate_mode": self.rate_mode,
            "label": self.label,
            "vmaf": round(self.vmaf, 2),
            "clip_size_bytes": self.clip_size_bytes,
            "predicted_size_bytes": self.predicted_size_bytes,
            "predicted_human": ff.human_size(self.predicted_size_bytes),
            "savings_percent": round(self.savings_percent, 1),
            "recommended": self.recommended,
        }
        if self.screenshot_ref:
            d["screenshot_ref"] = f"/api/preview/{self.screenshot_ref}"
        if self.screenshot_enc:
            d["screenshot_enc"] = f"/api/preview/{self.screenshot_enc}"
        return d


@dataclass
class VmafAnalysis:
    results: list = field(default_factory=list)
    recommended_value: Optional[int] = None
    recommended_quality: Optional[int] = None  # Alias
    model: str = ""
    rate_mode: str = "cq"
    clip_seconds: int = 30

    def to_dict(self) -> dict:
        rec = self.recommended_value
        return {
            "results": [r.to_dict() for r in self.results],
            "recommended_value": rec,
            "recommended_quality": rec,
            "model": self.model,
            "rate_mode": self.rate_mode,
            "clip_seconds": self.clip_seconds,
        }


StatusCb = Optional[Callable[[str], None]]


def _label(rate_mode: str, value: int) -> str:
    if rate_mode == "cq":
        return f"CQ/QP {value}"
    if rate_mode == "abr":
        return f"ABR {value} kbit/s"
    return f"{value} kbit/s"


def _model_for(info: VideoInfo) -> tuple[str, Path]:
    name = config.VMAF_MODEL_4K if info.is_4k else config.VMAF_MODEL_1080P
    return name, config.VMAF_MODEL_DIR / name


def _middle_start(duration: float, clip_seconds: int) -> float:
    if duration <= clip_seconds:
        return 0.0
    return max(0.0, duration / 2.0 - clip_seconds / 2.0)


def _extract_reference(
    info: VideoInfo, work: Path, tonemap: bool, clip_seconds: int, status: StatusCb,
) -> tuple[Path, float, float]:
    ref = work / "reference.mkv"
    start = _middle_start(info.duration, clip_seconds)
    clip_len = min(clip_seconds, info.duration) or 1.0
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-ss", str(start), "-t", str(clip_len),
           "-i", str(info.path)]
    if tonemap and info.is_hdr:
        cmd += ["-vf", _TONEMAP_CHAIN]
    cmd += ["-an", "-sn", "-c:v", "ffv1", "-level", "3", str(ref)]
    if status:
        status("Referenz-Clip wird extrahiert …")
    _run_logged(cmd, "VMAF-Referenz")
    return ref, start, clip_len


def _vmaf_compare(
    distorted: Path, reference: Path, info: VideoInfo, work: Path, key: str,
) -> Optional[float]:
    _, model_path = _model_for(info)
    log = work / f"vmaf_{key}.json"
    scale = f"scale={info.width}:{info.height}:flags=bicubic"
    fc = (
        f"[0:v]{scale},setpts=PTS-STARTPTS[dist];"
        f"[1:v]setpts=PTS-STARTPTS[ref];"
        f"[dist][ref]libvmaf=model=path={model_path}:"
        f"log_fmt=json:log_path={log}:n_threads=4"
    )
    cmd = [config.FFMPEG, "-y", "-hide_banner",
           "-i", str(distorted), "-i", str(reference),
           "-filter_complex", fc, "-f", "null", "-"]
    _run_logged(cmd, f"VMAF-Vergleich {key}")
    if not log.exists():
        return None
    try:
        data = json.loads(log.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pooled = data.get("pooled_metrics", {}).get("vmaf", {})
    score = pooled.get("mean")
    if score is None:
        frames = data.get("frames", [])
        vals = [f.get("metrics", {}).get("vmaf") for f in frames if f.get("metrics")]
        vals = [v for v in vals if v is not None]
        score = sum(vals) / len(vals) if vals else None
    return float(score) if score is not None else None


def _extract_screenshots(
    info: VideoInfo,
    test_file: Path,
    preview_dir: Path,
    item_id: str,
    key: str,
    source_start: float,
    clip_len: float,
) -> tuple[str, str]:
    """Screenshots aus Quelle und Test-Encode zur gleichen relativen Position."""
    preview_dir.mkdir(parents=True, exist_ok=True)
    rel_ref = f"{item_id}/{key}_ref.jpg"
    rel_enc = f"{item_id}/{key}_enc.jpg"
    ts_src = source_start + clip_len / 2.0
    ts_enc = clip_len / 2.0

    ref_cmd = [
        config.FFMPEG, "-y", "-hide_banner",
        "-ss", str(ts_src), "-i", str(info.path),
        "-frames:v", "1", "-q:v", "2",
        str(config.PREVIEW_DIR / rel_ref),
    ]
    enc_cmd = [
        config.FFMPEG, "-y", "-hide_banner",
        "-ss", str(ts_enc), "-i", str(test_file),
        "-frames:v", "1", "-q:v", "2",
        str(config.PREVIEW_DIR / rel_enc),
    ]
    _run_logged(ref_cmd, f"Screenshot Ref {key}")
    _run_logged(enc_cmd, f"Screenshot Enc {key}")
    return rel_ref, rel_enc


def analyze(
    info: VideoInfo,
    platform: str,
    codec: str,
    target_height: Optional[int],
    tonemap: bool,
    opts: Optional[VmafOptions] = None,
    status: StatusCb = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> VmafAnalysis:
    opts = opts or VmafOptions()
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    work = config.WORK_DIR / f"vmaf_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    model_name, _ = _model_for(info)
    analysis = VmafAnalysis(
        model=model_name,
        rate_mode=opts.rate_mode,
        clip_seconds=opts.clip_seconds,
    )
    use_bitrate = opts.rate_mode in ("bitrate", "abr")
    values = [v for v in opts.test_values[:4] if v > 0]
    if not values:
        values = [20, 24, 28, 32] if not use_bitrate else [8000, 6000, 4000, 2000]

    try:
        reference, start, clip_len = _extract_reference(
            info, work, tonemap, opts.clip_seconds, status,
        )

        for val in values:
            if cancelled():
                break
            key = str(val)
            lbl = _label(opts.rate_mode, val)
            if status:
                status(f"Test-Encode @ {lbl} …")

            if use_bitrate:
                cmd = build_encode_cmd(
                    info, test_file, platform, codec, 28,
                    target_height, tonemap,
                    duration_limit=clip_len, start_at=start,
                    rate_mode=opts.rate_mode, bitrate_kbps=val,
                    include_progress=False, audio_copy=True,
                )
            else:
                cmd = build_encode_cmd(
                    info, test_file, platform, codec, val,
                    target_height, tonemap,
                    duration_limit=clip_len, start_at=start,
                    include_progress=False, audio_copy=True,
                )
            _run_logged(cmd, f"VMAF-Test {lbl}")
            if not test_file.exists() or test_file.stat().st_size == 0:
                continue

            if status:
                status(f"VMAF-Vergleich @ {lbl} …")
            score = _vmaf_compare(test_file, reference, info, work, key)
            if score is None:
                continue

            clip_size = test_file.stat().st_size
            predicted = int((clip_size / clip_len) * info.duration)
            savings = 0.0
            if info.size_bytes > 0:
                savings = (info.size_bytes - predicted) / info.size_bytes * 100.0

            scr_ref, scr_enc = "", ""
            if opts.generate_screenshots and opts.item_id:
                scr_ref, scr_enc = _extract_screenshots(
                    info, test_file, config.PREVIEW_DIR, opts.item_id, key,
                    start, clip_len,
                )

            analysis.results.append(VmafResult(
                value=val,
                rate_mode=opts.rate_mode,
                label=lbl,
                vmaf=score,
                clip_size_bytes=clip_size,
                predicted_size_bytes=predicted,
                savings_percent=savings,
                screenshot_ref=scr_ref,
                screenshot_enc=scr_enc,
            ))

        _pick_recommended(analysis)
    finally:
        _finalize_work(work, opts.item_id)

    return analysis


def _pick_recommended(analysis: VmafAnalysis) -> None:
    if not analysis.results:
        return
    lo, _ = config.VMAF_SWEETSPOT
    candidates = [r for r in analysis.results if r.vmaf >= lo]
    if analysis.rate_mode == "cq":
        best = max(candidates, key=lambda r: r.value) if candidates else max(
            analysis.results, key=lambda r: r.vmaf)
    else:
        # Bitrate: niedrigste Bitrate bei noch gutem VMAF = maximale Ersparnis
        best = min(candidates, key=lambda r: r.value) if candidates else max(
            analysis.results, key=lambda r: r.vmaf)
    best.recommended = True
    analysis.recommended_value = best.value
    analysis.recommended_quality = best.value


def _finalize_work(work: Path, item_id: str) -> None:
    """VMAF-Arbeitsordner löschen oder dauerhaft unter vmaf/ ablegen."""
    if not work.exists():
        return
    if config.RETAIN_VMAF_SESSIONS and item_id:
        config.VMAF_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.VMAF_SESSIONS_DIR / item_id
        _cleanup(dest)
        try:
            work.rename(dest)
            return
        except OSError:
            import shutil
            try:
                shutil.move(str(work), str(dest))
                return
            except OSError as e:
                logger.warning("VMAF-Session konnte nicht archiviert werden: %s", e)
    _cleanup(work)


def _cleanup(work: Path) -> None:
    import shutil
    try:
        if work.is_dir():
            shutil.rmtree(work, ignore_errors=True)
        elif work.exists():
            work.unlink(missing_ok=True)
    except OSError:
        pass
