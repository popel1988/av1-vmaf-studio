"""VMAF- & Encoder-Systemkalibrierung („Grundtest“) unter Diagnose.

Führt einen kurzen, vollständig lokalen Basis-Benchmark ohne Internetbedarf aus
und speichert ein Hardware-Geschwindigkeitsprofil in ``data/vmaf_calibration.json``:
- Test-Encode-Geschwindigkeit (FPS) aller verfügbaren Encoder (Plattform × Codec)
  bei verschiedenen Speed-Stufen sowie 1080p- und 4K-Skalierung
- Reine ``libvmaf``-Geschwindigkeit (1080p & 4K, Subsampling 1 vs. 2, optional GPU)
- Zeitaufschlag je optionaler Zusatzmetrik (PSNR, SSIM, MS-SSIM, XPSNR, CAMBI)
- ``SSIMULACRA2``-Dauer pro Bildpaar (1080p & 4K inkl. Frame-Extraktion)
- Referenz-Vorbereitung (verlustfreier Schnitt vs. Tonemapping-Filterpfad)

Darauf aufbauend berechnet ``estimate_vmaf_run(...)`` bereits vor dem Start
(und live bei jeder Einstellungsänderung in der UI) eine realistische Dauerprognose.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from . import capabilities as caps_mod
from . import config
from . import ffmpeg_utils as ff
from . import vmaf as vmaf_mod
from .encoder import build_encode_cmd

logger = logging.getLogger("vcompress.vmaf_calib")

CALIB_PATH = config.DATA_DIR / "vmaf_calibration.json"

_lock = threading.RLock()
_cancel = threading.Event()
_thread: Optional[threading.Thread] = None
_state: dict = {
    "running": False,
    "percent": 0,
    "step": 0,
    "total": 0,
    "message": "",
    "error": "",
}

_PLATFORMS = ("nvidia", "intel", "amd", "cpu")
_CODECS = ("av1", "hevc", "h264", "vp9")
_SPEED_EXTRAP = {
    "fastest": 1.65,
    "fast": 1.32,
    "balanced": 1.0,
    "slow": 0.48,
    "slowest": 0.22,
}


def load_calibration() -> Optional[dict]:
    """Lädt das gespeicherte Kalibrierungsprofil oder None."""
    if not CALIB_PATH.is_file():
        return None
    try:
        data = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema_version") == 1:
            return data
    except Exception as exc:
        logger.debug("VMAF-Kalibrierung nicht lesbar: %s", exc)
    return None


def _save_calibration(data: dict) -> None:
    from .provenance import atomic_json_write
    CONFIG_DIR = CALIB_PATH.parent
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_json_write(CALIB_PATH, data)


def snapshot() -> dict:
    with _lock:
        st = dict(_state)
    raw = load_calibration()
    eff = effective_profile(calib=raw)
    st["calibration"] = eff
    st["has_raw_calibration"] = raw is not None
    st["learned"] = (eff or {}).get("learned") or {
        "total": 0, "full_encodes": 0, "vmaf_tests": 0, "vmaf_metrics": 0, "s2_samples": 0,
    }
    return st


def cancel() -> None:
    _cancel.set()


def start_calibration() -> Optional[str]:
    """Startet den Grundtest im Hintergrund. Gibt Fehlermeldung zurück, falls bereits aktiv."""
    global _thread
    with _lock:
        if _state["running"]:
            return "Kalibrierung läuft bereits."
        _cancel.clear()
        _state.update(running=True, percent=0, step=0, total=1,
                      message="Bereite Referenzclips vor …", error="")
        _thread = threading.Thread(target=_worker, name="vmaf-calib", daemon=True)
        _thread.start()
    return None


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _synth_clip(dest: Path, width: int, height: int, fps: int, seconds: float, hdr: bool = False) -> bool:
    """Erzeugt einen synthetischen Referenzclip mit Bewegung & feinem Korn."""
    vf = f"noise=alls=8:allf=t"
    pix = "yuv420p10le"
    cmd = [
        config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={seconds}",
        "-vf", vf,
        "-pix_fmt", pix,
        "-c:v", "libx264" if not hdr else "libx265",
        "-preset", "ultrafast",
        "-crf", "12",
    ]
    if hdr:
        cmd += [
            "-x265-params",
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)",
        ]
    cmd.append(str(dest))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=90, check=False)
        return res.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Synthetischer Referenzclip fehlgeschlagen (%sx%s): %s", width, height, exc)
        return False


def _cap_ok(entry) -> Optional[bool]:
    """Capabilities-Cache speichert je Encoder einen bool, ältere Einträge ein Dict."""
    if isinstance(entry, dict):
        return bool(entry.get("ok"))
    if isinstance(entry, bool):
        return entry
    return None


def _available_pairs() -> list[tuple[str, str]]:
    cap_res = caps_mod.results_map()
    pairs: list[tuple[str, str]] = []
    for plat in _PLATFORMS:
        for codec in _CODECS:
            if plat != "cpu" and codec == "vp9":
                continue
            known = _cap_ok(cap_res.get(f"{plat}:{codec}")) if cap_res else None
            ok = known if known is not None else ff.encoder_available(plat, codec)
            if ok:
                pairs.append((plat, codec))
    return pairs


def _time_cmd(cmd: list[str], timeout: int = 180) -> Optional[float]:
    t0 = time.perf_counter()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    dt = max(0.02, time.perf_counter() - t0)
    return dt if res.returncode == 0 else None


def _measure_encode_fps(info: ff.VideoInfo, work: Path, tag: str,
                        plat: str, codec: str, speed: str, clip_sec: float) -> Optional[float]:
    out = work / f"enc_{tag}.mkv"
    cq = 30 if codec == "av1" else (26 if codec == "hevc" else 24)
    try:
        cmd = build_encode_cmd(
            info, out, plat, codec, cq, None, False,
            duration_limit=clip_sec, start_at=0.0,
            include_progress=False, audio_mode="none",
            encoder_speed=speed,
        )
    except Exception:
        return None
    dt = _time_cmd(cmd, timeout=180)
    if dt is None or not out.is_file() or out.stat().st_size == 0:
        return None
    frames = max(1.0, float(info.fps or 24.0) * clip_sec)
    return round(frames / dt, 2)


def _measure_vmaf_sec(dist: Path, ref: Path, info: ff.VideoInfo, work: Path, tag: str,
                      clip_sec: float, *, subsample: int = 1, gpu_accel: bool = False,
                      psnr: bool = False, ssim: bool = False, ms_ssim: bool = False,
                      cambi: bool = False, xpsnr: bool = False) -> Optional[float]:
    t0 = time.perf_counter()
    m = vmaf_mod._vmaf_metrics(
        dist, ref, info, work, tag,
        neg=True, dims=(info.width, info.height), clip_len=clip_sec,
        subsample=subsample, gpu_accel=gpu_accel,
        with_psnr=psnr, with_ssim=ssim, with_ms_ssim=ms_ssim,
        with_cambi=cambi, with_xpsnr=xpsnr,
    )
    dt = max(0.02, time.perf_counter() - t0)
    return round(dt, 3) if m is not None else None


def _worker() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vmaf_calib_", dir=str(config.WORK_DIR if config.WORK_DIR.exists() else Path(tempfile.gettempdir()))))
    t_start = time.time()
    try:
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        pairs = _available_pairs()
        if not pairs:
            _set(running=False, error="Keine funktionsfähigen Encoder gefunden.", percent=0, message="")
            return

        gpu_stat = vmaf_mod.vmaf_gpu_status()
        has_gpu_vmaf = bool(gpu_stat.get("available"))
        has_xpsnr = bool(vmaf_mod.xpsnr_available())
        has_s2 = bool(vmaf_mod._ssimulacra2_ok())

        # Aufgabenliste für sauberen Fortschrittsbalken planen
        enc_tasks: list[tuple[str, str, str, str]] = []
        for plat, codec in pairs:
            speeds = ("fast", "balanced", "slow") if plat in ("cpu", "nvidia", "intel") else ("balanced",)
            for sp in speeds:
                enc_tasks.append((plat, codec, sp, "1080p"))
            enc_tasks.append((plat, codec, "balanced", "2160p"))

        metric_tasks = [
            "vmaf_base_1080p",
            "vmaf_sub2_1080p",
            "vmaf_psnr_ssim_1080p",
            "vmaf_ms_ssim_1080p",
            "vmaf_cambi_1080p",
        ]
        if has_xpsnr:
            metric_tasks.append("vmaf_xpsnr_1080p")
        metric_tasks.extend(["vmaf_all_1080p", "vmaf_base_2160p", "vmaf_all_2160p"])
        if has_gpu_vmaf:
            metric_tasks.extend(["vmaf_gpu_1080p", "vmaf_gpu_2160p"])
        if has_s2:
            metric_tasks.extend(["s2_1080p", "s2_2160p"])
        metric_tasks.append("ref_tonemap_1080p")

        total_steps = 2 + len(enc_tasks) + len(metric_tasks)
        done = 0

        def bump(msg: str) -> None:
            nonlocal done
            done += 1
            pct = min(99, int(round(done / max(1, total_steps) * 100)))
            _set(step=done, total=total_steps, percent=pct, message=msg)

        # 1) Synthetische Referenzclips (1080p: 3.0s = 72 Frames, 2160p: 2.0s = 48 Frames)
        _set(total=total_steps, message="Erzeuge 1080p-Referenzclip …")
        ref_1080 = tmp / "ref_1080p.mkv"
        sec_1080 = 3.0
        if not _synth_clip(ref_1080, 1920, 1080, 24, sec_1080):
            _set(running=False, error="1080p-Referenzclip konnte nicht erzeugt werden.", percent=0, message="")
            return
        info_1080 = ff.ffprobe(ref_1080)
        bump("Erzeuge 4K-Referenzclip …")
        if _cancel.is_set():
            _set(running=False, message="Kalibrierung abgebrochen.", percent=0)
            return

        ref_2160 = tmp / "ref_2160p.mkv"
        sec_2160 = 2.0
        has_2160 = _synth_clip(ref_2160, 3840, 2160, 24, sec_2160)
        info_2160 = ff.ffprobe(ref_2160) if has_2160 else None
        bump("Starte Encoder-Messungen …")

        if info_1080 is None:
            _set(running=False, error="ffprobe für Referenzclip fehlgeschlagen.", percent=0, message="")
            return

        # 2) Encoder-Geschwindigkeiten messen
        encoders_out: dict[str, dict] = {}
        dist_1080: Optional[Path] = None
        dist_2160: Optional[Path] = None

        for plat, codec, sp, res_tag in enc_tasks:
            if _cancel.is_set():
                _set(running=False, message="Kalibrierung abgebrochen.", percent=0)
                return
            label = f"{plat.upper()} · {codec.upper()} ({sp}, {res_tag})"
            _set(message=f"Messe Encoder: {label} …")
            if res_tag == "2160p":
                if not info_2160:
                    bump(f"Überspringe {label}")
                    continue
                fps_val = _measure_encode_fps(info_2160, tmp, f"{plat}_{codec}_{sp}_2160", plat, codec, sp, sec_2160)
                if fps_val:
                    entry = encoders_out.setdefault(f"{plat}:{codec}", {"speeds_1080p": {}, "fps_2160p_balanced": 0.0})
                    entry["fps_2160p_balanced"] = fps_val
                    cand = tmp / f"enc_{plat}_{codec}_{sp}_2160.mkv"
                    if dist_2160 is None and cand.is_file():
                        dist_2160 = cand
            else:
                fps_val = _measure_encode_fps(info_1080, tmp, f"{plat}_{codec}_{sp}_1080", plat, codec, sp, sec_1080)
                if fps_val:
                    entry = encoders_out.setdefault(f"{plat}:{codec}", {"speeds_1080p": {}, "fps_2160p_balanced": 0.0})
                    entry["speeds_1080p"][sp] = fps_val
                    cand = tmp / f"enc_{plat}_{codec}_{sp}_1080.mkv"
                    if dist_1080 is None and cand.is_file():
                        dist_1080 = cand
            bump(f"Encoder gemessen: {label}")

        # Fehlende Speed-Stufen (fastest / slowest) sauber aus Nachbarwerten ableiten
        for key, entry in encoders_out.items():
            sp_map = entry.get("speeds_1080p") or {}
            bal = sp_map.get("balanced") or next(iter(sp_map.values()), 0.0)
            if bal > 0:
                if "fast" not in sp_map:
                    sp_map["fast"] = round(bal * _SPEED_EXTRAP["fast"], 2)
                if "fastest" not in sp_map:
                    sp_map["fastest"] = round(sp_map["fast"] * 1.28, 2)
                if "slow" not in sp_map:
                    sp_map["slow"] = round(bal * _SPEED_EXTRAP["slow"], 2)
                if "slowest" not in sp_map:
                    sp_map["slowest"] = round(sp_map["slow"] * 0.46, 2)
                if not entry.get("fps_2160p_balanced"):
                    entry["fps_2160p_balanced"] = round(bal * 0.27, 2)
                entry["scale_2160_to_1080"] = round(entry["fps_2160p_balanced"] / bal, 3)

        if dist_1080 is None:
            dist_1080 = ref_1080
        if dist_2160 is None and ref_2160.is_file():
            dist_2160 = ref_2160

        # 3) VMAF & Zusatzmetriken messen
        frames_1080 = float(info_1080.fps or 24.0) * sec_1080
        frames_2160 = float((info_2160.fps if info_2160 else 24.0) or 24.0) * sec_2160

        vmaf_timings: dict[str, float] = {}
        for mtask in metric_tasks:
            if _cancel.is_set():
                _set(running=False, message="Kalibrierung abgebrochen.", percent=0)
                return
            if mtask == "vmaf_base_1080p":
                _set(message="Messe reines libvmaf (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080)
                if dt:
                    vmaf_timings["base_1080p_sec"] = dt
            elif mtask == "vmaf_sub2_1080p":
                _set(message="Messe libvmaf mit Subsampling=2 (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080, subsample=2)
                if dt:
                    vmaf_timings["sub2_1080p_sec"] = dt
            elif mtask == "vmaf_psnr_ssim_1080p":
                _set(message="Messe libvmaf + PSNR + SSIM (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080, psnr=True, ssim=True)
                if dt:
                    vmaf_timings["psnr_ssim_1080p_sec"] = dt
            elif mtask == "vmaf_ms_ssim_1080p":
                _set(message="Messe libvmaf + MS-SSIM (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080, ms_ssim=True)
                if dt:
                    vmaf_timings["ms_ssim_1080p_sec"] = dt
            elif mtask == "vmaf_cambi_1080p":
                _set(message="Messe libvmaf + CAMBI (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080, cambi=True)
                if dt:
                    vmaf_timings["cambi_1080p_sec"] = dt
            elif mtask == "vmaf_xpsnr_1080p":
                _set(message="Messe libvmaf + XPSNR (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080, xpsnr=True)
                if dt:
                    vmaf_timings["xpsnr_1080p_sec"] = dt
            elif mtask == "vmaf_all_1080p":
                _set(message="Messe libvmaf + alle Zusatzmetriken (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080,
                                       psnr=True, ssim=True, ms_ssim=True, cambi=True, xpsnr=has_xpsnr)
                if dt:
                    vmaf_timings["all_1080p_sec"] = dt
            elif mtask == "vmaf_base_2160p" and info_2160 and dist_2160:
                _set(message="Messe reines libvmaf (4K) …")
                dt = _measure_vmaf_sec(dist_2160, ref_2160, info_2160, tmp, mtask, sec_2160)
                if dt:
                    vmaf_timings["base_2160p_sec"] = dt
            elif mtask == "vmaf_all_2160p" and info_2160 and dist_2160:
                _set(message="Messe libvmaf + alle Zusatzmetriken (4K) …")
                dt = _measure_vmaf_sec(dist_2160, ref_2160, info_2160, tmp, mtask, sec_2160,
                                       psnr=True, ssim=True, ms_ssim=True, cambi=True, xpsnr=has_xpsnr)
                if dt:
                    vmaf_timings["all_2160p_sec"] = dt
            elif mtask == "vmaf_gpu_1080p":
                _set(message="Messe VMAF GPU-Beschleunigung (1080p) …")
                dt = _measure_vmaf_sec(dist_1080, ref_1080, info_1080, tmp, mtask, sec_1080, gpu_accel=True)
                if dt:
                    vmaf_timings["gpu_1080p_sec"] = dt
            elif mtask == "vmaf_gpu_2160p" and info_2160 and dist_2160:
                _set(message="Messe VMAF GPU-Beschleunigung (4K) …")
                dt = _measure_vmaf_sec(dist_2160, ref_2160, info_2160, tmp, mtask, sec_2160, gpu_accel=True)
                if dt:
                    vmaf_timings["gpu_2160p_sec"] = dt
            elif mtask == "s2_1080p":
                _set(message="Messe SSIMULACRA2 (1080p) …")
                t0 = time.perf_counter()
                s2_res = vmaf_mod._ssimulacra2_clip(ref_1080, dist_1080, tmp, "calib1080", sec_1080, 24.0, 2, save_previews=False)
                dt = max(0.02, time.perf_counter() - t0)
                if s2_res is not None:
                    vmaf_timings["s2_pair_1080p_sec"] = round(dt / 2.0, 3)
            elif mtask == "s2_2160p" and info_2160 and dist_2160:
                _set(message="Messe SSIMULACRA2 (4K) …")
                t0 = time.perf_counter()
                s2_res = vmaf_mod._ssimulacra2_clip(ref_2160, dist_2160, tmp, "calib2160", sec_2160, 24.0, 1, save_previews=False)
                dt = max(0.02, time.perf_counter() - t0)
                if s2_res is not None:
                    vmaf_timings["s2_pair_2160p_sec"] = round(dt, 3)
            elif mtask == "ref_tonemap_1080p":
                _set(message="Messe HDR-Referenz-Vorbereitung …")
                hdr_src = tmp / "hdr_1080p.mkv"
                if _synth_clip(hdr_src, 1920, 1080, 24, 2.0, hdr=True):
                    hdr_info = ff.ffprobe(hdr_src)
                    if hdr_info:
                        tm_out = tmp / "hdr_tm_ref.mkv"
                        cmd_tm = build_encode_cmd(hdr_info, tm_out, "cpu", "h264", 14, None, True,
                                                  duration_limit=2.0, start_at=0.0, include_progress=False,
                                                  audio_mode="none", encoder_speed="fastest")
                        dt_tm = _time_cmd(cmd_tm, timeout=90)
                        if dt_tm:
                            vmaf_timings["tonemap_ref_fps_1080p"] = round(48.0 / dt_tm, 2)
            bump(f"Abgeschlossen: {mtask}")

        base_1080 = max(0.05, vmaf_timings.get("base_1080p_sec") or 1.5)
        base_2160 = max(0.10, vmaf_timings.get("base_2160p_sec") or (base_1080 * (frames_2160 / frames_1080) * 3.6))
        vmaf_fps_1080 = round(frames_1080 / base_1080, 2)
        vmaf_fps_2160 = round(frames_2160 / base_2160, 2)

        # Relative Aufschläge je Zusatztest (in Bruchteilen der Basiszeit, mind. 0.0)
        ps_both = max(0.0, (vmaf_timings.get("psnr_ssim_1080p_sec", base_1080) - base_1080) / base_1080)
        ov_psnr = round(min(0.6, ps_both * 0.35), 3)
        ov_ssim = round(min(0.8, ps_both * 0.65), 3)
        ov_ms_ssim = round(min(1.5, max(0.0, (vmaf_timings.get("ms_ssim_1080p_sec", base_1080 * 1.12) - base_1080) / base_1080)), 3)
        ov_cambi = round(min(2.0, max(0.0, (vmaf_timings.get("cambi_1080p_sec", base_1080 * 1.22) - base_1080) / base_1080)), 3)
        ov_xpsnr = round(min(1.5, max(0.0, (vmaf_timings.get("xpsnr_1080p_sec", base_1080 * 1.15) - base_1080) / base_1080)), 3) if has_xpsnr else 0.0

        all_measured = max(0.0, (vmaf_timings.get("all_1080p_sec", base_1080 * 1.55) - base_1080) / base_1080)
        sum_ov = ov_psnr + ov_ssim + ov_ms_ssim + ov_cambi + ov_xpsnr
        # Gemeinsamer Decode spart gegenüber Einzel-Aufschlägen Zeit -> kombinierter Dämpfungsfaktor
        combo_scale = round(min(1.15, max(0.45, all_measured / sum_ov)), 3) if sum_ov > 0.05 else 1.0

        sub2_ratio = round(min(0.95, max(0.35, vmaf_timings.get("sub2_1080p_sec", base_1080 * 0.62) / base_1080)), 3)
        # sub2_ratio = decode_share + (1 - decode_share) / 2  => decode_share = 2 * sub2_ratio - 1
        decode_share = round(min(0.85, max(0.15, 2.0 * sub2_ratio - 1.0)), 3)

        gpu_fps_1080 = round(frames_1080 / vmaf_timings["gpu_1080p_sec"], 2) if vmaf_timings.get("gpu_1080p_sec") else 0.0
        gpu_fps_2160 = round(frames_2160 / vmaf_timings["gpu_2160p_sec"], 2) if vmaf_timings.get("gpu_2160p_sec") else 0.0

        profile = {
            "schema_version": 1,
            "calibrated_at": round(time.time(), 1),
            "calibration_duration_sec": round(time.time() - t_start, 1),
            "encoders": encoders_out,
            "vmaf": {
                "fps_1080p": vmaf_fps_1080,
                "fps_2160p": vmaf_fps_2160,
                "gpu_fps_1080p": gpu_fps_1080,
                "gpu_fps_2160p": gpu_fps_2160,
                "decode_share": decode_share,
                "overheads": {
                    "psnr": ov_psnr,
                    "ssim": ov_ssim,
                    "ms_ssim": ov_ms_ssim,
                    "cambi": ov_cambi,
                    "xpsnr": ov_xpsnr,
                },
                "all_overhead_1080p": round(all_measured, 3),
                "combo_scale": combo_scale,
            },
            "ssimulacra2": {
                "available": has_s2,
                "sec_per_pair_1080p": vmaf_timings.get("s2_pair_1080p_sec", 0.45 if has_s2 else 0.0),
                "sec_per_pair_2160p": vmaf_timings.get("s2_pair_2160p_sec", 1.35 if has_s2 else 0.0),
            },
            "reference": {
                "tonemap_fps_1080p": vmaf_timings.get("tonemap_ref_fps_1080p", 45.0),
            },
        }
        _save_calibration(profile)
        _set(running=False, percent=100, step=total_steps, total=total_steps,
             message=f"Kalibrierung abgeschlossen in {round(time.time() - t_start, 1):.0f} s.", error="")
    except Exception as exc:
        logger.exception("VMAF-Kalibrierung fehlgeschlagen")
        _set(running=False, error=str(exc), message="")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _interp_res(val_1080: float, val_2160: float, pixels: float) -> float:
    """Interpoliert FPS bzw. Zeit anhand der Pixelzahl (1080p = 2.07M, 2160p = 8.29M)."""
    px_1080 = 1920.0 * 1080.0
    px_2160 = 3840.0 * 2160.0
    p = max(640.0 * 360.0, float(pixels or px_1080))
    if p <= px_1080:
        # Unterhalb 1080p (z. B. 720p): moderat schneller, aber mit I/O-Sockel
        ratio = (px_1080 / p) ** 0.72
        return val_1080 * min(2.6, ratio)
    t = min(1.5, (p - px_1080) / (px_2160 - px_1080))
    return max(0.05, val_1080 + (val_2160 - val_1080) * t)


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Gewichteter Median über (wert, gewicht)-Paare – robust gegen Ausreißer."""
    valid = sorted((v, w) for v, w in pairs if v > 0 and w > 0)
    if not valid:
        return 0.0
    total_w = sum(w for _, w in valid)
    acc = 0.0
    for v, w in valid:
        acc += w
        if acc >= total_w * 0.5:
            return v
    return valid[-1][0]


def _norm_fps_to_1080(fps_val: float, width: int, height: int, scale_4k: float = 0.27) -> float:
    """Rechnet eine gemessene FPS bei beliebiger Auflösung auf das 1080p-Äquivalent um."""
    px = max(640.0 * 360.0, float((width or 1920) * (height or 1080)))
    factor = _interp_res(1.0, max(0.10, min(0.85, float(scale_4k or 0.27))), px)
    return fps_val / max(0.08, factor)


def _blend(base_val: float, hist_val: float, hist_weight: float) -> float:
    """Mischt Grundtest-Wert (Basis) und reale Messungen sanft nach Sample-Gewicht."""
    if hist_val <= 0:
        return base_val
    if base_val <= 0:
        return hist_val
    # Ab ~3 gewichteten Praxis-Samples (z. B. 1-2 volle Encodes oder 4 VMAF-Tests)
    # übernimmt die reale Historie >50 %, gedeckelt auf 88 % damit Grundtest als Anker bleibt.
    w = min(0.88, hist_weight / (hist_weight + 2.4))
    return base_val * (1.0 - w) + hist_val * w


def effective_profile(calib: Optional[dict] = None, samples: Optional[list[dict]] = None) -> Optional[dict]:
    """Fusioniert Grundtest-Kalibrierung mit realen Encodes & VMAF-Messungen aus SQLite.

    Funktioniert auch nur mit Grundtest ODER nur mit realer Historie – idealerweise mit beiden.
    """
    raw = calib if calib is not None else load_calibration()
    if samples is None:
        try:
            from . import history
            samples = history.list_perf_samples(limit=800)
        except Exception:
            samples = []

    if not raw and not samples:
        return None

    import copy
    prof = copy.deepcopy(raw) if isinstance(raw, dict) else {
        "schema_version": 1,
        "calibrated_at": 0,
        "encoders": {},
        "vmaf": {
            "fps_1080p": 45.0,
            "fps_2160p": 12.0,
            "gpu_fps_1080p": 0.0,
            "gpu_fps_2160p": 0.0,
            "decode_share": 0.35,
            "overheads": {"psnr": 0.03, "ssim": 0.06, "ms_ssim": 0.14, "cambi": 0.22, "xpsnr": 0.15},
            "combo_scale": 0.85,
        },
        "ssimulacra2": {"available": True, "sec_per_pair_1080p": 0.45, "sec_per_pair_2160p": 1.35},
        "reference": {"tonemap_fps_1080p": 45.0},
    }

    now = time.time()
    counts = {"total": 0, "full_encodes": 0, "vmaf_tests": 0, "vmaf_metrics": 0, "s2_samples": 0}
    enc_groups: dict[tuple[str, str], list[tuple[float, float]]] = {}
    enc_4k_groups: dict[str, list[tuple[float, float]]] = {}
    enc_sample_counts: dict[str, int] = {}

    vmaf_cpu_1080: list[tuple[float, float]] = []
    vmaf_cpu_2160: list[tuple[float, float]] = []
    vmaf_gpu_1080: list[tuple[float, float]] = []
    vmaf_gpu_2160: list[tuple[float, float]] = []
    s2_1080: list[tuple[float, float]] = []
    s2_2160: list[tuple[float, float]] = []

    vc = prof.setdefault("vmaf", {})
    ov_map = vc.get("overheads") or {"psnr": 0.03, "ssim": 0.06, "ms_ssim": 0.14, "cambi": 0.22, "xpsnr": 0.15}
    combo_scale = float(vc.get("combo_scale") or 0.85)
    dec_share = float(vc.get("decode_share") or 0.35)

    for idx, s in enumerate(samples):
        kind = s.get("kind")
        src_kind = s.get("source_kind") or ""
        w = int(s.get("width") or 1920)
        h = int(s.get("height") or 1080)
        px = float(w * h)
        val = float(s.get("fps_measured") or 0.0)
        if val <= 0:
            continue
        # Jüngere Messungen wiegen etwas stärker als sehr alte (Halbwertszeit ~60 Tage).
        age_days = max(0.0, (now - float(s.get("ts") or now)) / 86400.0)
        recency = 0.5 ** (age_days / 60.0)
        counts["total"] += 1

        if kind == "encode":
            plat = str(s.get("platform") or "").lower()
            codec = str(s.get("codec") or "").lower()
            sp = str(s.get("speed") or "balanced")
            if not plat or not codec:
                continue
            key = f"{plat}:{codec}"
            if src_kind == "full_encode":
                counts["full_encodes"] += 1
                base_w = 2.2
            else:
                counts["vmaf_tests"] += 1
                base_w = 0.85
            wt = base_w * recency
            existing = (prof.get("encoders") or {}).get(key) or {}
            scale_4k = float(existing.get("scale_2160_to_1080") or 0.27)
            fps_1080 = _norm_fps_to_1080(val, w, h, scale_4k)
            enc_groups.setdefault((key, sp), []).append((fps_1080, wt))
            enc_sample_counts[key] = enc_sample_counts.get(key, 0) + 1
            if px >= 3200 * 1800 and sp == "balanced":
                enc_4k_groups.setdefault(key, []).append((val, wt))

        elif kind == "vmaf_metric":
            counts["vmaf_metrics"] += 1
            mask_tokens = {tok.strip() for tok in str(s.get("metrics_mask") or "").split(",") if tok.strip()}
            raw_ov = sum(float(ov_map.get(k) or 0.0) for k in mask_tokens)
            eff_ov = raw_ov * combo_scale
            sub = max(1, int(s.get("subsample") or 1))
            sub_factor = dec_share + (1.0 - dec_share) / sub
            # Gemessene FPS auf reines Basis-libvmaf (ohne Zusatztests, subsample=1) normieren
            base_equiv = val * (1.0 + eff_ov) * sub_factor
            wt = 1.0 * recency
            is_gpu = bool(s.get("vmaf_gpu"))
            if px >= 3200 * 1800:
                (vmaf_gpu_2160 if is_gpu else vmaf_cpu_2160).append((base_equiv, wt))
            else:
                norm_1080 = _norm_fps_to_1080(base_equiv, w, h, 0.27)
                (vmaf_gpu_1080 if is_gpu else vmaf_cpu_1080).append((norm_1080, wt))

        elif kind == "s2":
            counts["s2_samples"] += 1
            wt = 1.0 * recency
            if px >= 3200 * 1800:
                s2_2160.append((val, wt))
            else:
                s2_1080.append((val, wt))

    # Encoder-Werte fusionieren
    encoders_out = prof.setdefault("encoders", {})
    seen_keys = set(encoders_out.keys()) | {k for k, _ in enc_groups.keys()}
    for key in seen_keys:
        entry = encoders_out.setdefault(key, {"speeds_1080p": {}, "fps_2160p_balanced": 0.0, "scale_2160_to_1080": 0.27})
        sp_map = entry.setdefault("speeds_1080p", {})
        for sp in ("fastest", "fast", "balanced", "slow", "slowest"):
            pts = enc_groups.get((key, sp))
            if pts:
                hist_fps = _weighted_median(pts)
                tot_w = sum(w for _, w in pts)
                base_fps = float(sp_map.get(sp) or 0.0)
                sp_map[sp] = round(_blend(base_fps, hist_fps, tot_w), 2)
        # Fehlende Speed-Stufen (z. B. wenn nur Historie existiert) sauber ableiten
        bal = float(sp_map.get("balanced") or 0.0)
        if bal <= 0 and sp_map:
            ref_sp, ref_v = next(iter(sp_map.items()))
            bal = ref_v / max(0.1, _SPEED_EXTRAP.get(ref_sp, 1.0))
            sp_map["balanced"] = round(bal, 2)
        if bal > 0:
            for sp, mult in _SPEED_EXTRAP.items():
                if not sp_map.get(sp):
                    sp_map[sp] = round(bal * mult, 2)
            pts_4k = enc_4k_groups.get(key)
            if pts_4k:
                h4k = _weighted_median(pts_4k)
                entry["fps_2160p_balanced"] = round(_blend(float(entry.get("fps_2160p_balanced") or 0.0), h4k, sum(w for _, w in pts_4k)), 2)
            elif not entry.get("fps_2160p_balanced"):
                entry["fps_2160p_balanced"] = round(bal * float(entry.get("scale_2160_to_1080") or 0.27), 2)
            entry["scale_2160_to_1080"] = round(max(0.12, min(0.75, float(entry["fps_2160p_balanced"]) / bal)), 3)
        if enc_sample_counts.get(key):
            entry["learned_samples"] = enc_sample_counts[key]

    # libvmaf CPU/GPU fusionieren
    if vmaf_cpu_1080:
        vc["fps_1080p"] = round(_blend(float(vc.get("fps_1080p") or 0.0), _weighted_median(vmaf_cpu_1080), sum(w for _, w in vmaf_cpu_1080)), 2)
    if vmaf_cpu_2160:
        vc["fps_2160p"] = round(_blend(float(vc.get("fps_2160p") or 0.0), _weighted_median(vmaf_cpu_2160), sum(w for _, w in vmaf_cpu_2160)), 2)
    if vmaf_gpu_1080:
        vc["gpu_fps_1080p"] = round(_blend(float(vc.get("gpu_fps_1080p") or 0.0), _weighted_median(vmaf_gpu_1080), sum(w for _, w in vmaf_gpu_1080)), 2)
    if vmaf_gpu_2160:
        vc["gpu_fps_2160p"] = round(_blend(float(vc.get("gpu_fps_2160p") or 0.0), _weighted_median(vmaf_gpu_2160), sum(w for _, w in vmaf_gpu_2160)), 2)

    # SSIMULACRA2 fusionieren
    s2c = prof.setdefault("ssimulacra2", {"available": True})
    if s2_1080:
        s2c["sec_per_pair_1080p"] = round(_blend(float(s2c.get("sec_per_pair_1080p") or 0.0), _weighted_median(s2_1080), sum(w for _, w in s2_1080)), 3)
    if s2_2160:
        s2c["sec_per_pair_2160p"] = round(_blend(float(s2c.get("sec_per_pair_2160p") or 0.0), _weighted_median(s2_2160), sum(w for _, w in s2_2160)), 3)

    prof["learned"] = counts
    prof["has_raw_calibration"] = raw is not None
    if raw and counts["total"] > 0:
        prof["basis"] = "calibration+history"
    elif raw:
        prof["basis"] = "calibration"
    else:
        prof["basis"] = "history"
    return prof


def estimate_encode_run(params: dict, calib: Optional[dict] = None) -> dict:
    """Berechnet die geschätzte reine Encode-Dauer für einen vollständigen Film/Clip."""
    calib = calib if calib is not None else effective_profile()
    if not calib:
        return {"calibrated": False}

    duration = float(params.get("duration") or 0.0)
    if duration <= 0:
        return {"calibrated": True, "has_duration": False, "encode_sec": 0.0}

    width = int(params.get("width") or 1920)
    height = int(params.get("height") or 1080)
    target_h = int(params.get("target_height") or 0)
    if target_h and target_h < height:
        width = int(round(width * (target_h / height)))
        height = target_h
    pixels = float(width * height)
    fps = float(params.get("fps") or 24.0)
    if fps <= 0:
        fps = 24.0

    plat = str(params.get("platform") or "cpu").lower()
    codec = str(params.get("codec") or "av1").lower()
    sp = ff.nearest_alias(ff.encoder_name(plat, codec), str(params.get("encoder_speed") or "balanced"))
    rate_mode = str(params.get("rate_mode") or "cq").lower()
    two_pass = bool(params.get("two_pass")) and rate_mode in ("abr", "bitrate")

    enc_db = calib.get("encoders") or {}
    prof = enc_db.get(f"{plat}:{codec}")
    if not prof:
        return {"calibrated": False, "reason": "encoder_unknown"}

    sp_map = prof.get("speeds_1080p") or {}
    fps_1080 = float(sp_map.get(sp) or sp_map.get("balanced") or 0.0)
    if fps_1080 <= 0:
        return {"calibrated": False, "reason": "encoder_unknown"}
    scale_4k = float(prof.get("scale_2160_to_1080") or 0.27)
    enc_fps = max(0.2, _interp_res(fps_1080, fps_1080 * scale_4k, pixels))

    two_mult = (1.72 if plat == "cpu" else 1.15) if two_pass else 1.0
    # Optionaler Tonemapping-Aufschlag bei Software-Tonemapping auf schnellen GPU-Encodern
    tm_sec = 0.0
    if bool(params.get("tonemap")) and str(params.get("tonemap_engine") or "cpu") == "cpu":
        tm_1080 = float((calib.get("reference") or {}).get("tonemap_fps_1080p") or 45.0)
        tm_fps = max(1.0, _interp_res(tm_1080, tm_1080 * 0.28, pixels))
        if tm_fps < enc_fps:
            enc_fps = tm_fps

    total_frames = duration * fps
    encode_sec = (total_frames / enc_fps) * two_mult + tm_sec
    speed_x = duration / max(0.1, encode_sec)
    learned = calib.get("learned") or {}
    return {
        "calibrated": True,
        "has_duration": True,
        "encode_sec": round(encode_sec, 1),
        "speed_x": round(speed_x, 2),
        "enc_fps": round(enc_fps, 1),
        "resolution": f"{width}×{height}",
        "basis": calib.get("basis") or "calibration",
        "learned_samples": int(learned.get("total") or 0),
        "encoder_samples": int(prof.get("learned_samples") or 0),
    }


def estimate_vmaf_run(params: dict, calib: Optional[dict] = None) -> dict:
    """Berechnet die geschätzte VMAF-Laufdauer (Gesamt + Encode + Messung + S2)."""
    calib = calib if calib is not None else effective_profile()
    if not calib:
        return {"calibrated": False}

    width = int(params.get("width") or 1920)
    height = int(params.get("height") or 1080)
    target_h = int(params.get("target_height") or 0)
    if target_h and target_h < height:
        width = int(round(width * (target_h / height)))
        height = target_h
    pixels = float(width * height)
    fps = float(params.get("fps") or 24.0)
    if fps <= 0:
        fps = 24.0

    samples = max(1, min(5, int(params.get("samples") or 1)))
    clip_sec = max(5.0, min(120.0, float(params.get("clip_seconds") or 20.0)))
    total_clip_sec = samples * clip_sec
    total_frames = total_clip_sec * fps
    batch_count = max(1, int(params.get("file_count") or 1))

    # Referenz-Vorbereitung
    tonemap = bool(params.get("tonemap"))
    ref_info = calib.get("reference") or {}
    if tonemap:
        tm_fps_1080 = float(ref_info.get("tonemap_fps_1080p") or 45.0)
        tm_fps = _interp_res(tm_fps_1080, tm_fps_1080 * 0.28, pixels)
        ref_sec = total_frames / max(1.0, tm_fps)
    else:
        ref_sec = samples * max(0.6, clip_sec * 0.04)
    if params.get("sample_mode") == "bitrate":
        ref_sec += 2.5

    # Encoder-Zeilen (Basis + optionale Vergleichszeilen)
    base_row = {
        "platform": str(params.get("platform") or "cpu"),
        "codec": str(params.get("codec") or "av1"),
        "encoder_speed": str(params.get("encoder_speed") or "balanced"),
        "rate_mode": str(params.get("rate_mode") or "cq"),
        "test_values": list(params.get("test_values") or [20, 24, 28, 32]),
        "two_pass": bool(params.get("two_pass")),
    }
    rows = [base_row]
    for extra in list(params.get("compare_rows") or [])[:6]:
        if isinstance(extra, dict):
            rows.append({
                "platform": str(extra.get("platform") or base_row["platform"]),
                "codec": str(extra.get("codec") or base_row["codec"]),
                "encoder_speed": str(extra.get("encoder_speed") or extra.get("speed") or base_row["encoder_speed"]),
                "rate_mode": str(extra.get("rate_mode") or base_row["rate_mode"]),
                "test_values": list(extra.get("test_values") or base_row["test_values"]),
                "two_pass": bool(extra.get("two_pass") if extra.get("two_pass") is not None else base_row["two_pass"]),
            })

    search_mode = str(params.get("vmaf_search_mode") or "grid")
    refine = bool(params.get("refine_midpoint", False))
    visual_only = bool(params.get("visual_only", False))

    enc_db = calib.get("encoders") or {}
    encode_sec_total = 0.0
    total_tiers = 0.0

    for row in rows:
        plat = row["platform"]
        codec = row["codec"]
        sp = ff.nearest_alias(ff.encoder_name(plat, codec), row["encoder_speed"])
        n_vals = max(1, len(row["test_values"] or [24]))
        tiers = 5.0 if search_mode == "binary" else (n_vals + (0.5 if refine and n_vals >= 2 else 0.0))
        total_tiers += tiers

        prof = enc_db.get(f"{plat}:{codec}") or {}
        sp_map = prof.get("speeds_1080p") or {}
        fps_1080 = float(sp_map.get(sp) or sp_map.get("balanced") or (18.0 if plat == "cpu" else 140.0))
        scale_4k = float(prof.get("scale_2160_to_1080") or 0.27)
        fps_2160 = fps_1080 * scale_4k
        enc_fps = max(0.3, _interp_res(fps_1080, fps_2160, pixels))

        two_mult = 1.0
        if row["two_pass"] and row["rate_mode"] in ("abr", "bitrate"):
            two_mult = 1.72 if plat == "cpu" else 1.15
        encode_sec_total += tiers * ((total_frames / enc_fps) * two_mult + samples * 0.35)

    vmaf_sec_total = 0.0
    s2_sec_total = 0.0
    if not visual_only:
        vc = calib.get("vmaf") or {}
        use_gpu = bool(params.get("vmaf_gpu")) and float(vc.get("gpu_fps_1080p") or 0) > 0
        b1080 = float(vc.get("gpu_fps_1080p") if use_gpu else vc.get("fps_1080p") or 45.0)
        b2160 = float(vc.get("gpu_fps_2160p") if (use_gpu and vc.get("gpu_fps_2160p")) else vc.get("fps_2160p") or (b1080 * 0.27))
        base_vmaf_fps = max(0.5, _interp_res(b1080, b2160, pixels))

        ov = vc.get("overheads") or {}
        raw_ov = 0.0
        for flag_key, ov_key in (
            ("vmaf_psnr", "psnr"),
            ("vmaf_ssim", "ssim"),
            ("vmaf_ms_ssim", "ms_ssim"),
            ("vmaf_xpsnr", "xpsnr"),
            ("vmaf_cambi", "cambi"),
        ):
            if bool(params.get(flag_key, True)):
                raw_ov += float(ov.get(ov_key) or 0.0)
        eff_ov = raw_ov * float(vc.get("combo_scale") or 1.0)

        subsample = max(1, int(params.get("vmaf_subsample") or 1))
        dec_share = float(vc.get("decode_share") or 0.35)
        sub_factor = dec_share + (1.0 - dec_share) / subsample

        base_tier_sec = (total_frames / base_vmaf_fps) * (1.0 + eff_ov) * sub_factor + samples * 0.3
        vmaf_sec_total = total_tiers * base_tier_sec

        s2_frames = max(0, min(7, int(params.get("ssimulacra2_frames") if params.get("ssimulacra2_frames") is not None else 3)))
        s2_cfg = calib.get("ssimulacra2") or {}
        if s2_frames > 0 and s2_cfg.get("available", True):
            p1080 = float(s2_cfg.get("sec_per_pair_1080p") or 0.45)
            p2160 = float(s2_cfg.get("sec_per_pair_2160p") or 1.35)
            px_t = min(1.5, max(0.4, (pixels - 1920 * 1080) / max(1.0, (3840 * 2160 - 1920 * 1080)))) if pixels > 1920 * 1080 else max(0.5, (pixels / (1920 * 1080)) ** 0.7)
            sec_pair = (p1080 + (p2160 - p1080) * px_t) if pixels > 1920 * 1080 else (p1080 * px_t)
            s2_sec_total = total_tiers * samples * s2_frames * sec_pair

    total_one = ref_sec + encode_sec_total + vmaf_sec_total + s2_sec_total
    total_all = total_one * batch_count
    learned = calib.get("learned") or {}
    return {
        "calibrated": True,
        "calibrated_at": calib.get("calibrated_at"),
        "basis": calib.get("basis") or "calibration",
        "learned_samples": int(learned.get("total") or 0),
        "total_sec": round(total_all, 1),
        "ref_sec": round(ref_sec * batch_count, 1),
        "encode_sec": round(encode_sec_total * batch_count, 1),
        "vmaf_sec": round(vmaf_sec_total * batch_count, 1),
        "s2_sec": round(s2_sec_total * batch_count, 1),
        "tiers": round(total_tiers, 1),
        "resolution": f"{width}×{height}",
    }
