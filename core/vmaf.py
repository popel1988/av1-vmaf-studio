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
    session_name: str = ""  # lesbarer Ordnername für Previews/Archiv
    source_title: str = ""  # Anzeigename der Quelle (für Archiv-Liste)
    # Zusätzliche zu vergleichende Encoder als (plattform, codec)-Paare.
    # Der Basis-Encoder (Parameter platform/codec) wird immer mitgetestet.
    encoders: list = field(default_factory=list)


# Anzeigenamen je Codec (plattformabhängig verfeinert in _codec_disp)
_CODEC_NAMES = {"av1": "AV1", "hevc": "HEVC", "h264": "H.264"}


def _codec_disp(platform: str, codec: str) -> str:
    if platform == "cpu":
        return {"av1": "SVT-AV1", "hevc": "x265", "h264": "x264"}.get(codec, codec.upper())
    return _CODEC_NAMES.get(codec, codec.upper())


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
    screenshot_ref: str = ""
    screenshot_enc: str = ""

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
    recommended_codec: Optional[str] = None
    recommended_platform: Optional[str] = None
    model: str = ""
    rate_mode: str = "cq"
    clip_seconds: int = 30

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
    reference: Path,
    test_file: Path,
    session: str,
    key: str,
    clip_len: float,
) -> tuple[str, str]:
    """Screenshots aus Referenz-Clip und Test-Encode zur gleichen Position.

    Beide Quellen sind bereits kurze Clips (Start = 0). Der Referenzclip hat
    ein evtl. nötiges Tonemapping schon angewendet, daher sind Original- und
    Encode-Screenshot direkt vergleichbar.
    """
    # Unterordner je Session MUSS existieren, sonst kann FFmpeg nicht schreiben.
    (config.PREVIEW_DIR / session).mkdir(parents=True, exist_ok=True)
    rel_ref = f"{session}/{key}_ref.jpg"
    rel_enc = f"{session}/{key}_enc.jpg"
    ts = max(0.0, clip_len / 2.0)

    # Framegenau: -ss NACH -i (dekodiert vom Clip-Anfang). Input-Seeking (-ss
    # vor -i) würde bei Inter-Frame-Encodes zum nächsten Keyframe springen und
    # damit einen anderen Frame als in der (Intra-)Referenz treffen.
    ref_cmd = [
        config.FFMPEG, "-y", "-hide_banner",
        "-i", str(reference), "-ss", str(ts),
        "-frames:v", "1", "-q:v", "2",
        str(config.PREVIEW_DIR / rel_ref),
    ]
    enc_cmd = [
        config.FFMPEG, "-y", "-hide_banner",
        "-i", str(test_file), "-ss", str(ts),
        "-frames:v", "1", "-q:v", "2",
        str(config.PREVIEW_DIR / rel_enc),
    ]
    ref_res = _run_logged(ref_cmd, f"Screenshot Ref {key}")
    enc_res = _run_logged(enc_cmd, f"Screenshot Enc {key}")
    ok_ref = ref_res.returncode == 0 and (config.PREVIEW_DIR / rel_ref).exists()
    ok_enc = enc_res.returncode == 0 and (config.PREVIEW_DIR / rel_enc).exists()
    return (rel_ref if ok_ref else ""), (rel_enc if ok_enc else "")


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
    # Lesbarer Session-Name für Previews & Archiv (Fallback: item_id/uuid).
    sess = opts.session_name or opts.item_id or uuid.uuid4().hex[:8]
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

    try:
        reference, start, clip_len = _extract_reference(
            info, work, tonemap, opts.clip_seconds, status,
        )

        base_pc = enc_list[0]
        for p, c in enc_list:
            disp = _codec_disp(p, c)
            # Im CQ-Modus die Werte pro Codec in einen vergleichbaren
            # Qualitätsbereich verschieben (CQ-Skalen sind nicht identisch).
            offset = 0 if use_bitrate else _cq_offset(base_pc, (p, c))
            for base_val in values:
                if cancelled():
                    break
                val = base_val if use_bitrate else max(1, min(63, base_val + offset))
                key = f"{p}_{c}_{val}"
                rate_lbl = _label(opts.rate_mode, val)
                lbl = f"{disp} · {rate_lbl}" if multi else rate_lbl
                if status:
                    status(f"Test-Encode {disp} @ {rate_lbl} …")

                test_file = work / f"test_{key}.mkv"
                if use_bitrate:
                    cmd = build_encode_cmd(
                        info, test_file, p, c, 28,
                        target_height, tonemap,
                        duration_limit=clip_len, start_at=start,
                        rate_mode=opts.rate_mode, bitrate_kbps=val,
                        include_progress=False, audio_mode="copy",
                    )
                else:
                    cmd = build_encode_cmd(
                        info, test_file, p, c, val,
                        target_height, tonemap,
                        duration_limit=clip_len, start_at=start,
                        include_progress=False, audio_mode="copy",
                    )
                _run_logged(cmd, f"VMAF-Test {lbl}")
                if not test_file.exists() or test_file.stat().st_size == 0:
                    continue

                if status:
                    status(f"VMAF-Vergleich {disp} @ {rate_lbl} …")
                score = _vmaf_compare(test_file, reference, info, work, key)
                if score is None:
                    continue

                clip_size = test_file.stat().st_size
                predicted = int((clip_size / clip_len) * info.duration)
                savings = 0.0
                if info.size_bytes > 0:
                    savings = (info.size_bytes - predicted) / info.size_bytes * 100.0

                scr_ref, scr_enc = "", ""
                if opts.generate_screenshots:
                    scr_ref, scr_enc = _extract_screenshots(
                        reference, test_file, sess, key, clip_len,
                    )

                analysis.results.append(VmafResult(
                    value=val,
                    rate_mode=opts.rate_mode,
                    label=lbl,
                    codec=c,
                    platform=p,
                    vmaf=score,
                    clip_size_bytes=clip_size,
                    predicted_size_bytes=predicted,
                    savings_percent=savings,
                    screenshot_ref=scr_ref,
                    screenshot_enc=scr_enc,
                ))

        _pick_recommended(analysis)
        if analysis.results:
            _save_session(sess, analysis, opts.source_title)
    finally:
        _finalize_work(work, sess)

    return analysis


def _session_meta_path(sess: str) -> Path:
    return config.PREVIEW_DIR / sess / "analysis.json"


def _save_session(sess: str, analysis: VmafAnalysis, title: str) -> None:
    """Analyse samt Metadaten neben den Screenshots ablegen (für Archiv-Ansicht)."""
    import time

    try:
        target = _session_meta_path(sess)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session": sess,
            "title": title or sess,
            "created": time.time(),
            "analysis": analysis.to_dict(),
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
        out.append({
            "session": data.get("session", meta.parent.name),
            "title": data.get("title", meta.parent.name),
            "created": data.get("created", meta.stat().st_mtime),
            "model": analysis.get("model", ""),
            "rate_mode": analysis.get("rate_mode", ""),
            "count": len(results),
            "multi_codec": analysis.get("multi_codec", False),
            "recommended_label": (rec or {}).get("label", ""),
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


def _pick_recommended(analysis: VmafAnalysis) -> None:
    if not analysis.results:
        return
    lo, _ = config.VMAF_SWEETSPOT
    candidates = [r for r in analysis.results if r.vmaf >= lo]
    # Codec-übergreifend: beste Effizienz = kleinste prognostizierte Datei bei
    # noch gutem VMAF. Das wählt automatisch den effizientesten Encoder.
    if candidates:
        best = min(candidates, key=lambda r: r.predicted_size_bytes)
    else:
        best = max(analysis.results, key=lambda r: r.vmaf)
    best.recommended = True
    analysis.recommended_value = best.value
    analysis.recommended_quality = best.value
    analysis.recommended_codec = best.codec
    analysis.recommended_platform = best.platform


def _finalize_work(work: Path, item_id: str) -> None:
    """VMAF-Arbeitsordner löschen oder dauerhaft unter vmaf/ ablegen."""
    if not work.exists():
        return
    # Die verlustfreie Referenz (FFV1) ist riesig (mehrere GB bei 4K/HDR) und
    # nach der Analyse wertlos – vor dem Archivieren immer entfernen.
    try:
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
