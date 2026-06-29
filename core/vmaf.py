"""VMAF-Analyse-Pipeline.

Ablauf:
  1. 30s-Referenz aus der Mitte des Videos extrahieren (verlustfrei, FFV1).
     Bei HDR->SDR wird die Referenz ebenfalls getonemappt (gleiche Domäne).
  2. 4 Test-Encodes bei CQ/QP 20/24/28/32 (plattformspezifische Flags).
  3. VMAF-Vergleich. Bei Downscaling wird das Distorted-Signal in der
     FFmpeg-Filter-Pipeline wieder exakt auf die Referenzauflösung hochskaliert.
  4. Ergebnisse + Größenprognose + "Sweet Spot"-Empfehlung.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config
from . import ffmpeg_utils as ff
from .encoder import build_encode_cmd
from .ffmpeg_utils import VideoInfo


@dataclass
class VmafResult:
    quality: int
    vmaf: float
    clip_size_bytes: int
    predicted_size_bytes: int
    savings_percent: float
    recommended: bool = False

    def to_dict(self) -> dict:
        return {
            "quality": self.quality,
            "vmaf": round(self.vmaf, 2),
            "clip_size_bytes": self.clip_size_bytes,
            "predicted_size_bytes": self.predicted_size_bytes,
            "predicted_human": ff.human_size(self.predicted_size_bytes),
            "savings_percent": round(self.savings_percent, 1),
            "recommended": self.recommended,
        }


@dataclass
class VmafAnalysis:
    results: list = field(default_factory=list)
    recommended_quality: Optional[int] = None
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "recommended_quality": self.recommended_quality,
            "model": self.model,
        }


StatusCb = Optional[Callable[[str], None]]


def _model_for(info: VideoInfo) -> tuple[str, Path]:
    name = config.VMAF_MODEL_4K if info.is_4k else config.VMAF_MODEL_1080P
    return name, config.VMAF_MODEL_DIR / name


def _middle_start(duration: float) -> float:
    clip = config.VMAF_CLIP_SECONDS
    if duration <= clip:
        return 0.0
    return max(0.0, duration / 2.0 - clip / 2.0)


def _extract_reference(info: VideoInfo, work: Path, tonemap: bool, status: StatusCb) -> Path:
    """Verlustfreie Referenz (Quellauflösung, ggf. getonemappt)."""
    ref = work / "reference.mkv"
    start = _middle_start(info.duration)
    clip_len = min(config.VMAF_CLIP_SECONDS, info.duration)
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-ss", str(start), "-t", str(clip_len),
           "-i", str(info.path)]
    if tonemap and info.is_hdr:
        from .encoder import _TONEMAP_CHAIN  # gleiche Kette wie beim Encode
        cmd += ["-vf", _TONEMAP_CHAIN]
    cmd += ["-an", "-sn", "-c:v", "ffv1", "-level", "3", str(ref)]
    if status:
        status("Referenz-Clip wird extrahiert …")
    subprocess.run(cmd, capture_output=True, check=False)
    return ref


def _vmaf_compare(distorted: Path, reference: Path, info: VideoInfo,
                  work: Path, quality: int) -> Optional[float]:
    """Vergleicht distorted gegen reference. Skaliert distorted bei Bedarf
    wieder auf die Referenzauflösung hoch (Pflicht für VMAF)."""
    model_name, model_path = _model_for(info)
    log = work / f"vmaf_{quality}.json"

    # Distorted exakt auf Referenzauflösung bringen (Upscaling bei Downscale-Test)
    scale = f"scale={info.width}:{info.height}:flags=bicubic"
    n_threads = 4
    fc = (
        f"[0:v]{scale},setpts=PTS-STARTPTS[dist];"
        f"[1:v]setpts=PTS-STARTPTS[ref];"
        f"[dist][ref]libvmaf=model=path={model_path}:"
        f"log_fmt=json:log_path={log}:n_threads={n_threads}"
    )
    cmd = [config.FFMPEG, "-y", "-hide_banner",
           "-i", str(distorted), "-i", str(reference),
           "-filter_complex", fc, "-f", "null", "-"]
    subprocess.run(cmd, capture_output=True, check=False)

    if not log.exists():
        return None
    try:
        data = json.loads(log.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pooled = data.get("pooled_metrics", {}).get("vmaf", {})
    score = pooled.get("mean")
    if score is None:
        # Fallback für andere libvmaf-JSON-Layouts
        frames = data.get("frames", [])
        vals = [f.get("metrics", {}).get("vmaf") for f in frames if f.get("metrics")]
        vals = [v for v in vals if v is not None]
        score = sum(vals) / len(vals) if vals else None
    return float(score) if score is not None else None


def analyze(
    info: VideoInfo,
    platform: str,
    codec: str,
    target_height: Optional[int],
    tonemap: bool,
    status: StatusCb = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> VmafAnalysis:
    """Vollständige VMAF-Analyse und Größenprognose."""
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = config.WORK_DIR / f"vmaf_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    model_name, _ = _model_for(info)
    analysis = VmafAnalysis(model=model_name)

    try:
        reference = _extract_reference(info, work, tonemap, status)
        clip_len = min(config.VMAF_CLIP_SECONDS, info.duration) or 1.0
        start = _middle_start(info.duration)

        for q in config.VMAF_TEST_QUALITIES:
            if cancelled():
                break
            if status:
                status(f"Test-Encode @ {q} (Modell: {model_name}) …")
            test_file = work / f"test_{q}.mkv"
            cmd = build_encode_cmd(
                info, test_file, platform, codec, q,
                target_height, tonemap,
                duration_limit=clip_len, start_at=start,
            )
            subprocess.run(cmd, capture_output=True, check=False)
            if not test_file.exists() or test_file.stat().st_size == 0:
                continue

            if status:
                status(f"VMAF-Vergleich @ {q} …")
            score = _vmaf_compare(test_file, reference, info, work, q)
            if score is None:
                continue

            clip_size = test_file.stat().st_size
            predicted = int((clip_size / clip_len) * info.duration)
            savings = 0.0
            if info.size_bytes > 0:
                savings = (info.size_bytes - predicted) / info.size_bytes * 100.0

            analysis.results.append(VmafResult(
                quality=q,
                vmaf=score,
                clip_size_bytes=clip_size,
                predicted_size_bytes=predicted,
                savings_percent=savings,
            ))

        _pick_recommended(analysis)
    finally:
        _cleanup(work)

    return analysis


def _pick_recommended(analysis: VmafAnalysis) -> None:
    if not analysis.results:
        return
    lo, hi = config.VMAF_SWEETSPOT
    # Kandidaten >= unterer Sweet-Spot: höchste Qualitätszahl = stärkste
    # Kompression bei noch akzeptablem Score.
    candidates = [r for r in analysis.results if r.vmaf >= lo]
    if candidates:
        best = max(candidates, key=lambda r: r.quality)
    else:
        # Keiner erreicht den Sweet Spot -> bestmögliche Qualität wählen
        best = max(analysis.results, key=lambda r: r.vmaf)
    best.recommended = True
    analysis.recommended_quality = best.quality


def _cleanup(work: Path) -> None:
    try:
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        work.rmdir()
    except OSError:
        pass
