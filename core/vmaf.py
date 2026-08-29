"""VMAF-Analyse-Pipeline mit flexiblen Testwerten, Bitrate-Modus und Screenshots."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config
from . import ffmpeg_utils as ff
from .encoder import build_encode_cmd, EncodeRunner, _TONEMAP_CHAIN
from .ffmpeg_utils import VideoInfo

logger = logging.getLogger("vcompress.vmaf")


def _run_logged(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    # errors="replace": FFmpeg gibt Quell-Metadaten teils in Latin-1 aus –
    # ohne das würde das UTF-8-Decoding der stderr-Ausgabe abstürzen.
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=False)
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
    samples: int = 1  # Anzahl Stichproben-Clips (1 = nur Mitte)
    generate_screenshots: bool = True
    item_id: str = ""
    session_name: str = ""  # lesbarer Ordnername für Previews/Archiv
    source_title: str = ""  # Anzeigename der Quelle (für Archiv-Liste)
    source_path: str = ""   # Quelldatei (für spätere Neu-Analyse)
    params: dict = field(default_factory=dict)  # Job-Settings-Snapshot
    # Zusätzliche zu vergleichende Encoder als (plattform, codec)-Paare.
    # Der Basis-Encoder (Parameter platform/codec) wird immer mitgetestet.
    encoders: list = field(default_factory=list)
    # >0: Ziel-VMAF (Super-Tool) – dann wird der effizienteste Wert mit
    # VMAF >= Ziel empfohlen statt des Standard-Sweetspots.
    target_vmaf: float = 0.0
    # Anime-Modus: NEG-Modell für die Bewertung + 10-bit-Test-Encodes.
    anime: bool = False


# Anzeigenamen je Codec (plattformabhängig verfeinert in _codec_disp)
_CODEC_NAMES = {"av1": "AV1", "hevc": "HEVC", "h264": "H.264"}


def _codec_disp(platform: str, codec: str) -> str:
    if platform == "cpu":
        return {"av1": "SVT-AV1", "hevc": "x265", "h264": "x264"}.get(codec, codec.upper())
    return _CODEC_NAMES.get(codec, codec.upper())


# 1%-Low-Abstand zum Ziel-Mittel. Default 6 (einstellbar unter Einstellungen):
# bei Ziel 94 muss das Low ≥ 88 bleiben („gut“, nicht nur „ok“).
# 4 wäre streng (Action/Korn/GPU-Encoder fliegen oft raus), 8 großzügig
# (Low 86 bei Ziel 94 ist schon eine Qualitätsstufe darunter). 0 = Floor aus.
VMAF_P1_GAP = 6.0


def quality_score(mean: float, p1: float = 0.0, hmean: float = 0.0) -> float:
    """Sichtqualität für Empfehlungen: Mittel zählt, 1%-Low darf nicht einbrechen.

    55 % Mittel + 35 % 1%-Low + 10 % harmonisches Mittel. Der Slider „Ziel-VMAF“
    bleibt der Mittelwert – dieser Score ersetzt ihn nicht, straft aber
    Ausreißer, die das Mittel schönrechnen.
    """
    m = float(mean or 0)
    floor = float(p1) if p1 else m
    h = float(hmean) if hmean else m
    return 0.55 * m + 0.35 * floor + 0.10 * h


# Ungefährer CQ/CRF-Wert je Encoder für ~VMAF 95 ("Sweet Spot"). Dient nur als
# Referenz, um beim Codec-Vergleich die CQ-Testwerte so zu verschieben, dass
# alle Encoder im vergleichbaren Qualitätsbereich landen (CQ-Skalen/Effizienz
# unterscheiden sich je Codec). Werte sind Näherungen (Encoder-/Version-abhängig).
_CQ_SWEETSPOT = {
    ("cpu", "hevc"): 23, ("cpu", "av1"): 30, ("cpu", "h264"): 21,
    ("nvidia", "av1"): 32, ("nvidia", "hevc"): 26, ("nvidia", "h264"): 24,
    ("intel", "av1"): 32, ("intel", "hevc"): 26, ("intel", "h264"): 24,
    ("amd", "av1"): 32, ("amd", "hevc"): 26, ("amd", "h264"): 24,
}
# Optionale Feinjustierung per Env (CQ_SWEETSPOT), Defaults bleiben sonst aktiv.
_CQ_SWEETSPOT.update(getattr(config, "CQ_SWEETSPOT_OVERRIDES", {}) or {})


def _cq_offset(base: tuple, target: tuple) -> int:
    """CQ-Verschiebung, damit `target` im gleichen Qualitätsbereich wie `base` testet."""
    b = _CQ_SWEETSPOT.get(base)
    t = _CQ_SWEETSPOT.get(target)
    if b is None or t is None:
        return 0
    return t - b


@dataclass
class VmafResult:
    value: int
    rate_mode: str
    label: str
    vmaf: float
    clip_size_bytes: int
    predicted_size_bytes: int
    savings_percent: float
    codec: str = "av1"
    platform: str = "cpu"
    recommended: bool = False
    # Zusatzmetriken (Mittel über alle Stichproben; 0 = nicht gemessen).
    vmaf_hmean: float = 0.0   # harmonisches Mittel (straft Ausreißer stärker)
    vmaf_1pct: float = 0.0    # Mittel der schlechtesten 1 % Frames ("1%-Low")
    psnr: float = 0.0
    ssim: float = 0.0
    screenshot_ref: str = ""            # Szene 0 (Rückwärtskompatibilität)
    screenshot_enc: str = ""
    screenshots: list = field(default_factory=list)  # [{scene, ref, enc}] je Szene
    scene_scores: list = field(default_factory=list)  # [{scene, vmaf}] je Stichprobe

    def to_dict(self) -> dict:
        d = {
            "value": self.value,
            "quality": self.value,  # Rückwärtskompatibilität UI
            "rate_mode": self.rate_mode,
            "label": self.label,
            "codec": self.codec,
            "platform": self.platform,
            "codec_disp": _codec_disp(self.platform, self.codec),
            "vmaf": round(self.vmaf, 2),
            "vmaf_score": round(quality_score(self.vmaf, self.vmaf_1pct, self.vmaf_hmean), 2),
            "clip_size_bytes": self.clip_size_bytes,
            "predicted_size_bytes": self.predicted_size_bytes,
            "predicted_human": ff.human_size(self.predicted_size_bytes),
            "savings_percent": round(self.savings_percent, 1),
            "recommended": self.recommended,
        }
        if self.vmaf_hmean:
            d["vmaf_hmean"] = round(self.vmaf_hmean, 2)
        if self.vmaf_1pct:
            d["vmaf_1pct"] = round(self.vmaf_1pct, 2)
        if self.psnr:
            d["psnr"] = round(self.psnr, 2)
        if self.ssim:
            d["ssim"] = round(self.ssim, 4)
        if self.screenshot_ref:
            d["screenshot_ref"] = f"/api/preview/{self.screenshot_ref}"
        if self.screenshot_enc:
            d["screenshot_enc"] = f"/api/preview/{self.screenshot_enc}"
        if self.screenshots:
            d["screenshots"] = [
                {
                    "scene": s.get("scene", 0),
                    "ref": f"/api/preview/{s['ref']}" if s.get("ref") else "",
                    "enc": f"/api/preview/{s['enc']}" if s.get("enc") else "",
                }
                for s in self.screenshots
            ]
        if self.scene_scores and len(self.scene_scores) > 1:
            d["scene_scores"] = [
                {"scene": s.get("scene", 0), "vmaf": round(s.get("vmaf", 0.0), 2)}
                for s in self.scene_scores
            ]
            vals = [s.get("vmaf") for s in self.scene_scores if s.get("vmaf") is not None]
            if vals:
                d["vmaf_min"] = round(min(vals), 2)
                d["vmaf_max"] = round(max(vals), 2)
        return d


@dataclass
class VmafAnalysis:
    results: list = field(default_factory=list)
    recommended_value: Optional[int] = None
    recommended_quality: Optional[int] = None  # Alias
    recommended_codec: Optional[str] = None
    recommended_platform: Optional[str] = None
    model: str = ""
    rate_mode: str = "cq"
    clip_seconds: int = 30
    error: str = ""            # Grund, falls keine Ergebnisse zustande kamen
    keep_source: bool = False  # Ziel nur mit größerer Datei erreichbar
    pick_warning: str = ""     # Floor verfehlt, Kompromiss Ersparnis/1%-Low

    def to_dict(self) -> dict:
        rec = self.recommended_value
        return {
            "results": [r.to_dict() for r in self.results],
            "recommended_value": rec,
            "recommended_quality": rec,
            "recommended_codec": self.recommended_codec,
            "recommended_platform": self.recommended_platform,
            "multi_codec": len({(r.platform, r.codec) for r in self.results}) > 1,
            "model": self.model,
            "rate_mode": self.rate_mode,
            "clip_seconds": self.clip_seconds,
            "error": self.error,
            "keep_source": self.keep_source,
            "pick_warning": self.pick_warning,
        }


StatusCb = Optional[Callable[[str], None]]


def _label(rate_mode: str, value: int) -> str:
    if rate_mode == "cq":
        return f"CQ/QP {value}"
    if rate_mode == "abr":
        return f"ABR {value} kbit/s"
    return f"{value} kbit/s"


def _model_for(info: VideoInfo, neg: bool = False) -> tuple[str, Path]:
    if neg:
        name = config.VMAF_MODEL_4K_NEG if info.is_4k else config.VMAF_MODEL_1080P_NEG
        path = config.VMAF_MODEL_DIR / name
        if path.exists():
            return name, path
        logger.warning("NEG-VMAF-Modell fehlt (%s) – Standardmodell wird genutzt.", name)
    name = config.VMAF_MODEL_4K if info.is_4k else config.VMAF_MODEL_1080P
    return name, config.VMAF_MODEL_DIR / name


def _middle_start(duration: float, clip_seconds: int) -> float:
    if duration <= clip_seconds:
        return 0.0
    return max(0.0, duration / 2.0 - clip_seconds / 2.0)


def _sample_starts(duration: float, clip_seconds: int, count: int) -> list[tuple[float, float]]:
    """Startpositionen der Stichproben-Clips (start, clip_len).

    count=1 → nur Mitte. Bei mehreren gleichmäßig über den Film verteilt
    (max. 5). Ist der Film zu kurz für die gewünschte Cliplänge, werden die
    Ausschnitte verkürzt, statt auf eine einzige Mitte zusammenzufallen.
    """
    duration = float(duration or 0.0)
    count = max(1, min(5, int(count or 1)))
    want = max(1.0, float(clip_seconds or 1))
    if duration <= 0:
        return [(0.0, 1.0)]
    if count <= 1:
        clip_len = min(want, duration) or 1.0
        return [(_middle_start(duration, int(round(clip_len))), clip_len)]
    clip_len = min(want, duration / count)
    if clip_len < 1.0:
        count = max(1, int(duration))
        clip_len = min(want, duration / max(1, count))
        if count <= 1:
            ln = min(want, duration) or 1.0
            return [(_middle_start(duration, 1), ln)]
    out: list[tuple[float, float]] = []
    for i in range(count):
        frac = (i + 1) / (count + 1)
        s = max(0.0, min(duration - clip_len, duration * frac - clip_len / 2.0))
        out.append((round(s, 3), round(clip_len, 3)))
    return out


def _extract_reference(
    info: VideoInfo, work: Path, tonemap: bool, start: float, clip_len: float,
    idx: int, status: StatusCb, crop: str = "",
) -> Path:
    ref = work / f"reference_{idx}.mkv"
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-ss", str(start), "-t", str(clip_len),
           "-i", str(info.path)]
    # Crop und Tonemap identisch zur Encode-Kette anwenden, damit der Vergleich
    # dieselbe (beschnittene/getonemappte) Bildfläche wie die Ausgabe nutzt.
    vf = []
    if crop:
        vf.append(f"crop={crop}")
    if tonemap and info.is_hdr:
        vf.append(_TONEMAP_CHAIN)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-an", "-sn", "-c:v", "ffv1", "-level", "3", str(ref)]
    if status:
        status(f"Referenz-Clip {idx + 1} wird extrahiert …")
    _run_logged(cmd, f"VMAF-Referenz {idx}")
    return ref


def _vmaf_threads() -> int:
    """libvmaf profitiert stark von mehreren Threads – an CPU koppeln."""
    return max(2, min(16, os.cpu_count() or 4))


def _run_libvmaf(
    distorted: Path, reference: Path, info: VideoInfo, work: Path, key: str,
    neg: bool, dims: Optional[tuple[int, int]], features: str,
) -> Optional[dict]:
    """Ein libvmaf-Lauf; liefert das geparste JSON-Dict oder None."""
    _, model_path = _model_for(info, neg)
    log = work / f"vmaf_{key}.json"
    w, h = dims if dims else (info.width, info.height)
    scale = f"scale={w}:{h}:flags=bicubic"
    fc = (
        f"[0:v]{scale},setpts=PTS-STARTPTS[dist];"
        f"[1:v]setpts=PTS-STARTPTS[ref];"
        f"[dist][ref]libvmaf=model=path={model_path}:"
        f"{features}log_fmt=json:log_path={log}:n_threads={_vmaf_threads()}"
    )
    cmd = [config.FFMPEG, "-y", "-hide_banner",
           "-i", str(distorted), "-i", str(reference),
           "-filter_complex", fc, "-f", "null", "-"]
    _run_logged(cmd, f"VMAF-Vergleich {key}")
    if not log.exists():
        return None
    try:
        return json.loads(log.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _metrics_from_json(data: dict) -> Optional[dict]:
    """VMAF-Kennzahlen aus einem libvmaf-JSON extrahieren (inkl. 1%-Low)."""
    pooled = data.get("pooled_metrics", {}) or {}
    vm = pooled.get("vmaf", {}) or {}
    frames = data.get("frames", []) or []
    vals = sorted(
        f["metrics"]["vmaf"] for f in frames
        if f.get("metrics") and f["metrics"].get("vmaf") is not None
    )
    mean = vm.get("mean")
    if mean is None:
        mean = sum(vals) / len(vals) if vals else None
    if mean is None:
        return None
    # 1%-Low = Mittel der schlechtesten 1 % Frames (mind. 1 Frame).
    p1 = 0.0
    if vals:
        k = max(1, int(len(vals) * 0.01))
        p1 = sum(vals[:k]) / k
    psnr = (pooled.get("psnr_y", {}) or pooled.get("psnr", {}) or {}).get("mean") or 0.0
    ssim = (pooled.get("float_ssim", {}) or pooled.get("ssim", {}) or {}).get("mean") or 0.0
    return {
        "vmaf": float(mean),
        "hmean": float(vm.get("harmonic_mean") or 0.0),
        "min": float(vm.get("min") or 0.0),
        "p1": float(p1),
        "psnr": float(psnr),
        "ssim": float(ssim),
    }


def _vmaf_metrics(
    distorted: Path, reference: Path, info: VideoInfo, work: Path, key: str,
    neg: bool = False, dims: Optional[tuple[int, int]] = None,
) -> Optional[dict]:
    """Vollständige Metriken (VMAF + PSNR + SSIM + 1%-Low) für einen Vergleich.

    PSNR/SSIM werden über die libvmaf-`feature`-Option mitberechnet. Schlägt der
    Lauf mit Features fehl (ältere FFmpeg-Builds), wird auf einen reinen
    VMAF-Lauf zurückgefallen – die Kern-Metrik bleibt so immer verfügbar.
    """
    data = _run_libvmaf(distorted, reference, info, work, key, neg, dims,
                        features="feature=name=psnr|name=float_ssim:")
    metrics = _metrics_from_json(data) if data else None
    if metrics is None:
        data = _run_libvmaf(distorted, reference, info, work, key, neg, dims,
                            features="")
        metrics = _metrics_from_json(data) if data else None
    return metrics


def _vmaf_compare(
    distorted: Path, reference: Path, info: VideoInfo, work: Path, key: str,
    neg: bool = False, dims: Optional[tuple[int, int]] = None,
) -> Optional[float]:
    """Rückwärtskompatibel: nur der VMAF-Mittelwert."""
    m = _vmaf_metrics(distorted, reference, info, work, key, neg, dims)
    return m["vmaf"] if m else None


def _extract_frame(
    src: Path, out_rel: str, clip_len: float, fps: float = 0.0, label: str = "frame",
) -> str:
    """Ein Einzelbild aus einem kurzen Clip an dessen Mitte extrahieren.

    Referenz-Clip und Test-Encode teilen denselben Frame-Index (beide starten bei
    Frame 0, identische FPS). Über `select=eq(n,N)` wird framegenau derselbe Frame
    getroffen (unabhängig von Keyframes/Zeitstempeln). Fehlt die FPS-Angabe, wird
    auf Output-Seeking zurückgegriffen. Gibt den relativen Pfad oder "" zurück.
    """
    (config.PREVIEW_DIR / out_rel).parent.mkdir(parents=True, exist_ok=True)
    base = [config.FFMPEG, "-y", "-hide_banner", "-i", str(src)]
    if fps and fps > 0:
        frame_no = max(0, int(round(fps * (clip_len / 2.0))))
        base += ["-vf", f"select=eq(n\\,{frame_no})", "-frames:v", "1", "-vsync", "0"]
    else:
        base += ["-ss", str(max(0.0, clip_len / 2.0)), "-frames:v", "1"]
    base += ["-q:v", "2", str(config.PREVIEW_DIR / out_rel)]
    res = _run_logged(base, f"Screenshot {label}")
    return out_rel if res.returncode == 0 and (config.PREVIEW_DIR / out_rel).exists() else ""


def measure_output_vmaf(
    info: VideoInfo,
    output: Path,
    *,
    tonemap: bool = False,
    preserve_hdr: bool = False,
    samples: int = 1,
    clip_seconds: int = 15,
    anime: bool = False,
    crop: str = "",
    cancelled: Callable[[], bool] = lambda: False,
) -> Optional[float]:
    """Misst den echten VMAF der fertigen Ausgabedatei gegen die Quelle.

    Für die Qualitäts-Guardrail: es werden dieselben Stichproben-Positionen wie
    bei der Analyse genutzt. Aus Quelle (ggf. getonemappt) und Ausgabe werden
    verlustfreie Clips gezogen und verglichen; der Mittelwert wird gemittelt.
    Downscale wird im Vergleich (scale auf Quellauflösung) berücksichtigt.
    """
    output = Path(output)
    if not output.exists():
        return None
    work = config.WORK_DIR / f"verify_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        specs = _sample_starts(info.duration, clip_seconds, samples)
        dims = ff.crop_dims(crop)  # bei Auto-Crop auf beschnittene Fläche vergleichen
        scores: list[float] = []
        for idx, (start, clip_len) in enumerate(specs):
            if cancelled():
                break
            ref = _extract_reference(info, work, tonemap, start, clip_len, idx,
                                     None, crop=crop)
            dist = work / f"out_{idx}.mkv"
            # Denselben Ausschnitt verlustfrei aus der Ausgabe ziehen.
            _run_logged(
                [config.FFMPEG, "-y", "-hide_banner", "-ss", str(start),
                 "-t", str(clip_len), "-i", str(output),
                 "-an", "-sn", "-c:v", "ffv1", "-level", "3", str(dist)],
                f"Verify-Clip {idx}")
            if not dist.exists():
                continue
            score = _vmaf_compare(dist, ref, info, work, f"verify_{idx}",
                                  neg=anime, dims=dims)
            if score is not None:
                scores.append(score)
        return round(sum(scores) / len(scores), 2) if scores else None
    finally:
        _cleanup(work)


def analyze(
    info: VideoInfo,
    platform: str,
    codec: str,
    target_height: Optional[int],
    tonemap: bool,
    opts: Optional[VmafOptions] = None,
    status: StatusCb = None,
    cancelled: Callable[[], bool] = lambda: False,
    preserve_hdr: bool = False,
    film_grain: int = 0,
    denoise: str = "off",
    crop: str = "",
    progress: Optional[Callable[[dict], None]] = None,
    encoder_speed: str = "balanced",
) -> VmafAnalysis:
    opts = opts or VmafOptions()
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    # Lesbarer Session-Name für Previews & Archiv (Fallback: item_id/uuid).
    sess = opts.session_name or opts.item_id or uuid.uuid4().hex[:8]
    work = config.WORK_DIR / f"vmaf_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    model_name, _ = _model_for(info, opts.anime)
    analysis = VmafAnalysis(
        model=model_name,
        rate_mode=opts.rate_mode,
        clip_seconds=opts.clip_seconds,
    )
    use_bitrate = opts.rate_mode in ("bitrate", "abr")
    values = [v for v in opts.test_values[:4] if v > 0]
    if not values:
        values = [20, 24, 28, 32] if not use_bitrate else [8000, 6000, 4000, 2000]

    # Encoder-Liste: Basis zuerst, dann Zusatz-Encoder – dedupliziert und nur
    # solche, die im FFmpeg-Build tatsächlich vorhanden sind.
    enc_list: list[tuple[str, str]] = []
    for p, c in [(platform, codec)] + list(opts.encoders):
        if (p, c) in enc_list:
            continue
        if ff.encoder_available(p, c):
            enc_list.append((p, c))
        else:
            logger.warning("Vergleichs-Encoder übersprungen (nicht verfügbar): %s/%s", p, c)
    if not enc_list:
        enc_list = [(platform, codec)]

    multi = len(enc_list) > 1

    # --- Fortschritts-Tracking --------------------------------------------
    n_samples = len(_sample_starts(info.duration, opts.clip_seconds, opts.samples))
    # „Einheiten" = pro (Encoder,Wert,Sample): 1 Encode + 1 VMAF-Vergleich.
    budget = {
        "steps": max(1, len(enc_list) * len(values)),
        "units": max(1, len(enc_list) * len(values) * n_samples * 2),
    }
    prog = {"done": 0, "step": 0}

    def emit(phase: str, fps=None, sub=None) -> None:
        if not progress:
            return
        pct = round(min(100.0, prog["done"] / budget["units"] * 100.0), 1)
        d = {"percent": pct, "phase": phase,
             "step": prog["step"], "steps": budget["steps"]}
        if fps is not None:
            d["fps"] = round(fps, 1)
        if sub is not None:
            d["sub_percent"] = round(sub, 1)
        progress(d)

    last_error = ""  # letzter Test-Encode-Fehler (für Diagnose, falls 0 Ergebnisse)

    def run_value(p: str, c: str, val: int) -> None:
        """Einen Qualitäts-/Bitrate-Punkt für einen Encoder testen."""
        nonlocal last_error
        if any(r.platform == p and r.codec == c and int(r.value) == int(val)
               for r in analysis.results):
            return
        disp = _codec_disp(p, c)
        key = f"{p}_{c}_{val}"
        rate_lbl = _label(opts.rate_mode, val)
        lbl = f"{disp} · {rate_lbl}" if multi else rate_lbl
        prog["step"] += 1

        total_size = 0
        total_dur = 0.0
        scores: list[float] = []
        hmeans: list[float] = []
        p1s: list[float] = []
        psnrs: list[float] = []
        ssims: list[float] = []
        shots: list[dict] = []
        scene_scores: list[dict] = []

        for si, (reference, start, clip_len) in enumerate(references):
            if cancelled():
                break
            skey = f"{key}_s{si}"
            smp = f" (Clip {si + 1}/{len(references)})" if len(references) > 1 else ""
            if status:
                status(f"Test-Encode {disp} @ {rate_lbl}{smp} …")
            emit("encode")
            test_file = work / f"test_{skey}.mkv"
            if use_bitrate:
                cmd = build_encode_cmd(
                    info, test_file, p, c, 28,
                    target_height, tonemap,
                    duration_limit=clip_len, start_at=start,
                    rate_mode=opts.rate_mode, bitrate_kbps=val,
                    include_progress=True, audio_mode="none",
                    preserve_hdr=preserve_hdr, film_grain=film_grain,
                    denoise=denoise, force_10bit=opts.anime, crop=crop,
                    encoder_speed=encoder_speed,
                )
            else:
                cmd = build_encode_cmd(
                    info, test_file, p, c, val,
                    target_height, tonemap,
                    duration_limit=clip_len, start_at=start,
                    include_progress=True, audio_mode="none",
                    preserve_hdr=preserve_hdr, film_grain=film_grain,
                    denoise=denoise, force_10bit=opts.anime, crop=crop,
                    encoder_speed=encoder_speed,
                )
            runner = EncodeRunner(on_progress=lambda pr: emit(
                "encode", fps=pr.fps, sub=pr.percent))
            rc, enc_err = runner.run(cmd, clip_len)
            prog["done"] += 1
            if not test_file.exists() or test_file.stat().st_size == 0:
                tail = (enc_err or "").strip().splitlines()
                last_error = (
                    f"Test-Encode fehlgeschlagen ({disp} @ {rate_lbl}, "
                    f"FFmpeg Exit {rc}): {tail[-1] if tail else 'keine Ausgabe'}"
                )
                logger.warning("%s\nCMD: %s\nSTDERR:\n%s",
                               last_error, " ".join(cmd), enc_err)
                continue
            if status:
                status(f"VMAF-Vergleich {disp} @ {rate_lbl}{smp} …")
            emit("vmaf")

            metrics = _vmaf_metrics(test_file, reference, info, work, skey,
                                    neg=opts.anime, dims=dims)
            prog["done"] += 1
            emit("vmaf")

            if metrics is None:
                continue
            score = metrics["vmaf"]
            scores.append(score)
            if metrics.get("hmean"):
                hmeans.append(metrics["hmean"])
            if metrics.get("p1"):
                p1s.append(metrics["p1"])
            if metrics.get("psnr"):
                psnrs.append(metrics["psnr"])
            if metrics.get("ssim"):
                ssims.append(metrics["ssim"])
            scene_scores.append({"scene": si, "vmaf": score})
            total_size += test_file.stat().st_size
            total_dur += clip_len
            if opts.generate_screenshots:
                enc_rel = _extract_frame(
                    test_file, f"{sess}/{key}_s{si}_enc.jpg",
                    clip_len, info.fps, label=f"Enc {lbl} S{si}")
                shots.append({
                    "scene": si,
                    "ref": ref_shots[si] if si < len(ref_shots) else "",
                    "enc": enc_rel,
                })

        if not scores or total_dur <= 0:
            return

        avg_score = sum(scores) / len(scores)
        # Test-Encodes sind video-only (`audio_mode=none`). Die echte Ausgabe
        # behält Ton/Untertitel – ohne den Aufschlag wirkt CQ 24 oft „kleiner
        # als die Quelle“, obwohl Video+Ton wächst.
        predicted_video = int((total_size / total_dur) * info.duration)
        predicted = predicted_video + _copied_payload_bytes(info, opts.params)
        savings = 0.0
        if info.size_bytes > 0:
            savings = (info.size_bytes - predicted) / info.size_bytes * 100.0

        def _avg(xs: list[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        analysis.results.append(VmafResult(
            value=val,
            rate_mode=opts.rate_mode,
            label=lbl,
            codec=c,
            platform=p,
            vmaf=avg_score,
            vmaf_hmean=_avg(hmeans),
            vmaf_1pct=_avg(p1s),
            psnr=_avg(psnrs),
            ssim=_avg(ssims),
            clip_size_bytes=total_size,
            predicted_size_bytes=predicted,
            savings_percent=savings,
            screenshot_ref=shots[0]["ref"] if shots else "",
            screenshot_enc=shots[0]["enc"] if shots else "",
            screenshots=shots,
            scene_scores=scene_scores,
        ))

    try:
        # Stichproben-Clips bestimmen und je eine (verlustfreie) Referenz ziehen.
        sample_specs = _sample_starts(info.duration, opts.clip_seconds, opts.samples)
        references: list[tuple[Path, float, float]] = []
        ref_shots: list[str] = []  # Referenz-Screenshot je Szene (einmalig)
        dims = ff.crop_dims(crop)  # Vergleichsauflösung bei Auto-Crop
        for si, (start, clip_len) in enumerate(sample_specs):
            emit("reference")
            ref = _extract_reference(info, work, tonemap, start, clip_len, si,
                                     status, crop=crop)
            references.append((ref, start, clip_len))
            if opts.generate_screenshots:
                ref_shots.append(_extract_frame(
                    ref, f"{sess}/scene{si}_ref.jpg", clip_len, info.fps,
                    label=f"Ref Szene {si}"))

        base_pc = enc_list[0]
        for p, c in enc_list:
            offset = 0 if use_bitrate else _cq_offset(base_pc, (p, c))
            for base_val in values:
                if cancelled():
                    break
                val = base_val if use_bitrate else max(1, min(63, base_val + offset))
                run_value(p, c, val)
            if cancelled():
                break

        # Ein Zwischenwert je Encoder zwischen letztem Treffer und erstem Fehlschlag.
        if not cancelled() and analysis.results:
            extras = _midpoint_jobs(analysis, opts.target_vmaf)
            if extras:
                budget["steps"] += len(extras)
                budget["units"] += len(extras) * n_samples * 2
                if status:
                    status("Zwischenwert zwischen Treffer und Fehlschlag …")
                for p, c, mid in extras:
                    if cancelled():
                        break
                    run_value(p, c, mid)

        if analysis.results:
            if use_bitrate:
                analysis.results.sort(
                    key=lambda r: (r.platform, r.codec, -int(r.value)))
            else:
                analysis.results.sort(
                    key=lambda r: (r.platform, r.codec, int(r.value)))

        _pick_recommended(analysis, opts.target_vmaf)
        if analysis.results:
            _save_session(sess, analysis, opts.source_title,
                          source_path=opts.source_path, params=opts.params)
        elif not cancelled():
            # Kein einziges Ergebnis – Grund weiterreichen, damit der Job nicht
            # kommentarlos „fertig"/leer wird.
            analysis.error = last_error or (
                "VMAF-Analyse ohne Ergebnis: alle Test-Encodes sind "
                "fehlgeschlagen (Encoder/Plattform prüfen).")
    finally:
        _finalize_work(work, sess)

    return analysis


def _session_meta_path(sess: str) -> Path:
    return config.PREVIEW_DIR / sess / "analysis.json"


def _save_session(sess: str, analysis: VmafAnalysis, title: str,
                  source_path: str = "", params: Optional[dict] = None) -> None:
    """Analyse samt Metadaten neben den Screenshots ablegen (für Archiv-Ansicht)."""
    import time

    try:
        target = _session_meta_path(sess)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Prüfen, ob die Quelle für eine spätere Neu-Analyse noch existiert.
        src_ok = bool(source_path) and Path(source_path).is_file()
        payload = {
            "session": sess,
            "title": title or sess,
            "created": time.time(),
            "analysis": analysis.to_dict(),
            "source_path": source_path,
            "source_available": src_ok,
            "params": params or {},
        }
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("VMAF-Session konnte nicht gespeichert werden: %s", e)


def list_sessions() -> list[dict]:
    """Alle archivierten VMAF-Vergleiche (neueste zuerst)."""
    root = config.PREVIEW_DIR
    if not root.exists():
        return []
    out: list[dict] = []
    for meta in root.glob("*/analysis.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        analysis = data.get("analysis", {})
        results = analysis.get("results", [])
        rec = next((r for r in results if r.get("recommended")), None)
        # Quelle ggf. neu prüfen (könnte inzwischen verschoben/gelöscht sein).
        src = data.get("source_path", "")
        src_ok = bool(src) and Path(src).is_file()
        out.append({
            "session": data.get("session", meta.parent.name),
            "title": data.get("title", meta.parent.name),
            "created": data.get("created", meta.stat().st_mtime),
            "model": analysis.get("model", ""),
            "rate_mode": analysis.get("rate_mode", ""),
            "count": len(results),
            "multi_codec": analysis.get("multi_codec", False),
            "recommended_label": (rec or {}).get("label", ""),
            "source_available": src_ok,
        })
    out.sort(key=lambda d: d.get("created", 0), reverse=True)
    return out


def load_session(name: str) -> Optional[dict]:
    """Gespeicherte Analyse eines Vergleichs laden (oder None)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    meta = _session_meta_path(name)
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


def sessions_for_source(abs_path: str, limit: int = 20) -> list[dict]:
    """Archivierte VMAF-Sessions zu einem Quellpfad."""
    if not abs_path:
        return []
    want = str(Path(abs_path).resolve()) if Path(abs_path).exists() else str(abs_path)
    out: list[dict] = []
    root = config.PREVIEW_DIR
    if not root.exists():
        return []
    for meta in root.glob("*/analysis.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        src = data.get("source_path") or ""
        try:
            src_r = str(Path(src).resolve()) if src and Path(src).exists() else src
        except OSError:
            src_r = src
        if src_r != want and src != abs_path:
            continue
        analysis = data.get("analysis", {})
        results = analysis.get("results", [])
        rec = next((r for r in results if r.get("recommended")), None)
        out.append({
            "session": data.get("session", meta.parent.name),
            "title": data.get("title", meta.parent.name),
            "created": data.get("created", meta.stat().st_mtime),
            "recommended_label": (rec or {}).get("label", ""),
            "recommended_vmaf": (rec or {}).get("vmaf"),
            "count": len(results),
        })
    out.sort(key=lambda d: d.get("created", 0), reverse=True)
    return out[:limit]


def _copied_payload_bytes(info: VideoInfo, params: Optional[dict] = None) -> int:
    """Geschätzte Bytes, die neben dem neuen Video in die Ausgabe wandern (Ton)."""
    params = params or {}
    dur = float(info.duration or 0.0)
    if dur <= 0:
        return 0
    audio_mode = str(params.get("audio_mode") or "copy").lower()
    if audio_mode in ("none", "drop", "off"):
        return 0
    audios = list(info.audio or [])
    tracks = params.get("audio_tracks") or []
    if tracks and audios:
        sel = []
        for idx in tracks:
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(audios):
                sel.append(audios[i])
        if sel:
            audios = sel
    n = max(1, len(audios)) if audios else 1
    if audio_mode in ("encode", "transcode"):
        kbps = int(params.get("audio_bitrate") or params.get("audio_bitrate_kbps") or 160)
        return int(kbps * 1000 / 8.0 * dur * n)
    # copy: Stream-Bitraten, sonst Dateigröße minus Video-Schätzung
    copied = 0
    for a in audios:
        br = int(a.get("bitrate") or 0) if isinstance(a, dict) else 0
        if br > 0:
            copied += int(br / 8.0 * dur)
    if copied > 0:
        return copied
    vb = int(info.video_bitrate or 0)
    if vb > 0 and info.size_bytes > 0:
        return max(0, info.size_bytes - int(vb / 8.0 * dur))
    return 0


def _result_solid(r: VmafResult, lo: float, gap: float) -> bool:
    if r.vmaf < lo:
        return False
    if gap > 0:
        p1 = r.vmaf_1pct if r.vmaf_1pct else r.vmaf
        if p1 < lo - gap:
            return False
    return True


def _midpoint_jobs(analysis: VmafAnalysis, target_vmaf: float) -> list[tuple[str, str, int]]:
    """Ein Zwischenwert je Encoder zwischen letztem Treffer und erstem Fehlschlag."""
    from . import app_settings
    lo = target_vmaf if target_vmaf and target_vmaf > 0 else config.VMAF_SWEETSPOT[0]
    gap = app_settings.vmaf_p1_gap()
    bitrate = analysis.rate_mode in ("bitrate", "abr")
    jobs: list[tuple[str, str, int]] = []
    groups: dict[tuple[str, str], list[VmafResult]] = {}
    for r in analysis.results:
        groups.setdefault((r.platform, r.codec), []).append(r)
    for (p, c), rows in groups.items():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: r.value, reverse=bitrate)
        seen = {int(r.value) for r in rows}
        pair = None
        for a, b in zip(rows, rows[1:]):
            if _result_solid(a, lo, gap) and not _result_solid(b, lo, gap):
                pair = (int(a.value), int(b.value))
                break
        if not pair:
            # Nichts solid: Mitte zwischen bestem 1%-Low und größter Ersparnis
            # unter den Sparern, damit der Kompromiss nicht nur am groben Raster hängt.
            min_sav = app_settings.vmaf_min_savings()
            if min_sav is None:
                continue
            savers = [r for r in rows
                      if r.savings_percent >= min_sav and r.vmaf >= lo]
            if len(savers) < 2:
                continue
            a = max(savers, key=lambda r: (
                r.vmaf_1pct if r.vmaf_1pct else r.vmaf, -r.predicted_size_bytes))
            b = min(savers, key=lambda r: r.predicted_size_bytes)
            if int(a.value) == int(b.value):
                continue
            pair = (int(a.value), int(b.value))
        v1, v2 = pair
        if bitrate:
            if abs(v1 - v2) < 250:
                continue
            mid = int(round((v1 + v2) / 2 / 100.0) * 100)
            if mid <= 0 or mid in seen:
                continue
        else:
            if abs(v1 - v2) < 2:
                continue
            mid = (v1 + v2) // 2
            if mid in seen:
                continue
        jobs.append((p, c, mid))
    return jobs


def _p1_of(r: VmafResult) -> float:
    return r.vmaf_1pct if r.vmaf_1pct else r.vmaf


def _p1_slack(gap: float) -> float:
    """Spielraum unter dem besten 1%-Low der Sparer, wenn der Floor verfehlt ist.

    2 Punkte sind oft kaum sichtbar (außer in den schwierigsten Frames).
    gap/3 koppelt das an den Floor-Slider (Vorgabe 6 → 2).
    """
    return max(2.0, float(gap or 0) / 3.0)


def _compromise_saver(pool: list, gap: float) -> VmafResult:
    """Unter sparenden Stufen: nahe am besten 1%-Low, dort die kleinste Datei."""
    best_p1 = max(_p1_of(r) for r in pool)
    limit = best_p1 - _p1_slack(gap)
    near = [r for r in pool if _p1_of(r) + 1e-9 >= limit]
    if not near:
        near = list(pool)
    return min(near, key=lambda r: (r.predicted_size_bytes, -_p1_of(r)))


def _savings_pick_warning(best: VmafResult, lo: float, gap: float,
                          pool: list) -> str:
    """Hinweis: Floor verfehlt, Kompromiss aus Ersparnis-Pflicht und 1%-Low."""
    p1 = _p1_of(best)
    floor = lo - gap if gap > 0 else lo
    slack = _p1_slack(gap)
    best_p1_row = max(pool, key=lambda r: (_p1_of(r), -r.predicted_size_bytes))
    most_save = min(pool, key=lambda r: r.predicted_size_bytes)
    bits = [
        f"Kompromiss: 1%-Low unter Floor {floor:.0f} (Ziel {lo:.0f}), "
        f"Ersparnis war Pflicht. Gewählt: {best.label} · VMAF {best.vmaf:.1f} · "
        f"1%-Low {p1:.1f} (Fenster {slack:.0f} Punkte unter bestem Sparer "
        f"{_p1_of(best_p1_row):.1f}) · Ersparnis {best.savings_percent:+.1f} %.",
    ]
    if int(best_p1_row.value) != int(best.value):
        bits.append(
            f"Näher am 1%-Low wäre {best_p1_row.label} "
            f"({_p1_of(best_p1_row):.1f}, {best_p1_row.savings_percent:+.1f} %).")
    if int(most_save.value) != int(best.value):
        bits.append(
            f"Mehr Ersparnis wäre {most_save.label} "
            f"({_p1_of(most_save):.1f}, {most_save.savings_percent:+.1f} %).")
    return " ".join(bits)


def _pick_recommended(analysis: VmafAnalysis, target_vmaf: float = 0.0) -> None:
    if not analysis.results:
        return
    # Ziel-VMAF (Slider / Super-Tool) bleibt der Mittelwert – so wie angegeben.
    # 1%-Low ist ein Floor: Kandidaten mit klaffendem Low (z. B. 95 / 79)
    # gelten nicht als „Ziel erreicht“, auch wenn das Mittel passt.
    lo = target_vmaf if target_vmaf and target_vmaf > 0 else config.VMAF_SWEETSPOT[0]
    from . import app_settings
    gap = app_settings.vmaf_p1_gap()
    min_sav = app_settings.vmaf_min_savings()

    def _most_savings(rows: list) -> VmafResult:
        return min(rows, key=lambda r: (r.predicted_size_bytes, -_p1_of(r)))

    mean_ok = [r for r in analysis.results if r.vmaf >= lo]
    solid = [r for r in analysis.results if _result_solid(r, lo, gap)]

    def _saving_ok(rows: list) -> list:
        if min_sav is None:
            return list(rows)
        return [r for r in rows if r.savings_percent >= min_sav]

    compact = _saving_ok(solid)
    analysis.keep_source = False
    analysis.pick_warning = ""
    if compact:
        best = _most_savings(compact)
    elif solid:
        # Ziel gehalten, aber jede Stufe wäre größer als die Quelle.
        analysis.keep_source = True
        best = _most_savings(solid)
    elif min_sav is not None and (_saving_ok(mean_ok) or _saving_ok(analysis.results)):
        # Floor verfehlt: Ersparnis ist Pflicht, 1%-Low bleibt ein Fenster –
        # nicht die kleinste Datei um jeden Preis und nicht das beste Low.
        pool = _saving_ok(mean_ok) or _saving_ok(analysis.results)
        best = _compromise_saver(pool, gap)
        analysis.pick_warning = _savings_pick_warning(best, lo, gap, pool)
    elif mean_ok:
        if min_sav is not None:
            analysis.keep_source = True
        best = max(mean_ok, key=lambda r: (_p1_of(r), -r.predicted_size_bytes))
    else:
        compact_any = _saving_ok(analysis.results)
        if compact_any:
            best = max(
                compact_any,
                key=lambda r: (
                    quality_score(r.vmaf, r.vmaf_1pct, r.vmaf_hmean), r.vmaf),
            )
        else:
            analysis.keep_source = True
            best = max(
                analysis.results,
                key=lambda r: (
                    quality_score(r.vmaf, r.vmaf_1pct, r.vmaf_hmean), r.vmaf),
            )
    if not analysis.keep_source:
        best.recommended = True
    analysis.recommended_value = best.value
    analysis.recommended_quality = best.value
    analysis.recommended_codec = best.codec
    analysis.recommended_platform = best.platform


def _finalize_work(work: Path, item_id: str) -> None:
    """VMAF-Arbeitsordner löschen oder dauerhaft unter vmaf/ ablegen."""
    if not work.exists():
        return
    # Die verlustfreien Referenzen (FFV1) sind riesig (mehrere GB bei 4K/HDR)
    # und nach der Analyse wertlos – vor dem Archivieren immer entfernen.
    try:
        for ref in work.glob("reference_*.mkv"):
            ref.unlink(missing_ok=True)
        (work / "reference.mkv").unlink(missing_ok=True)
    except OSError:
        pass
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
