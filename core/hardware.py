"""Echtzeit-Hardware-Überwachung: CPU, RAM und GPU (Nvidia / Intel / AMD).

Die GPU-Erkennung ist defensiv aufgebaut: Es wird automatisch erkannt, welche
Werkzeuge / sysfs-Knoten vorhanden sind. Fehlt eine GPU oder ein Tool, wird der
Wert sauber als "nicht verfügbar" behandelt, statt die UI zu blockieren.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import psutil


@dataclass
class GpuMetric:
    vendor: str  # "nvidia" | "intel" | "amd"
    name: str
    util: Optional[float] = None  # Auslastung in % (max. aus 3D / Video-Engine)
    mem_used: Optional[float] = None  # MB
    mem_total: Optional[float] = None  # MB
    temperature: Optional[float] = None  # °C
    enc_util: Optional[float] = None  # Video-Encoder-Auslastung in %
    dec_util: Optional[float] = None  # Video-Decoder-Auslastung in %
    render_util: Optional[float] = None  # Reine 3D-/Shader-Auslastung in %


@dataclass
class HardwareSnapshot:
    cpu_percent: float = 0.0
    cpu_cores: int = 0
    cpu_temp: Optional[float] = None       # °C (falls Sensor lesbar)
    cpu_freq_mhz: Optional[float] = None   # aktuelle Taktfrequenz
    load_avg: Optional[list] = None        # [1m, 5m, 15m]
    cpu_per_core: list = field(default_factory=list)  # Auslastung je Thread
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    gpus: list = field(default_factory=list)
    history: dict = field(default_factory=dict)  # {t, cpu, ram, gpu}

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _cpu_extra() -> tuple[Optional[float], Optional[float], Optional[list]]:
    """CPU-Temperatur (°C), Taktfrequenz (MHz) und Load-Average – best effort."""
    temp: Optional[float] = None
    freq: Optional[float] = None
    load: Optional[list] = None
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors:
        try:
            temps = sensors() or {}
            # Bevorzugte Quellen für die eigentliche CPU-Package-Temperatur.
            for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal",
                        "acpitz", "cpu-thermal"):
                arr = temps.get(key)
                if arr:
                    vals = [t.current for t in arr if t.current]
                    if vals:
                        temp = round(max(vals), 1)
                        break
            if temp is None and temps:
                vals = [t.current for arr in temps.values() for t in arr if t.current]
                if vals:
                    temp = round(max(vals), 1)
        except Exception:  # pragma: no cover - Sensoren sind optional
            pass
    try:
        f = psutil.cpu_freq()
        if f and f.current:
            freq = round(f.current)
    except Exception:  # pragma: no cover
        pass
    try:
        load = [round(x, 2) for x in psutil.getloadavg()]
    except (OSError, AttributeError):
        pass
    return temp, freq, load


def _run(cmd: list[str], timeout: float = 4.0) -> Optional[str]:
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return None


class HardwareMonitor:
    """Erkennt vorhandene GPUs einmalig und liefert danach Snapshots."""

    # Mögliche Pfade für nvidia-smi (Standard-PATH + QNAP-Treiber-Mount).
    _NVIDIA_SMI_CANDIDATES = (
        "/usr/local/nvidia/bin/nvidia-smi",
        "/usr/bin/nvidia-smi",
    )

    # Verlaufspuffer: ~2 Minuten bei METRICS_INTERVAL ~1,5 s.
    _HISTORY_MAX = 160

    def __init__(self) -> None:
        from .win_gpu import NvmlReader, PdhGpuMonitor, discover_dxgi_adapters
        self._nvml = NvmlReader()
        self._nvidia_smi = self._find_nvidia_smi()
        self._dxgi_adapters = discover_dxgi_adapters()
        self._pdh = PdhGpuMonitor() if any(
            a["vendor"] in ("amd", "intel") for a in self._dxgi_adapters
        ) else None
        self._has_nvidia = (
            self._nvml.available
            or self._nvidia_smi is not None
            or any(a["vendor"] == "nvidia" for a in self._dxgi_adapters)
        )
        self._has_radeontop = shutil.which("radeontop") is not None
        self._has_intel_gpu_top = shutil.which("intel_gpu_top") is not None
        self._drm_cards = self._discover_drm_cards()
        self._hist: deque = deque(maxlen=self._HISTORY_MAX)
        import time as _t
        self._time = _t
        # Erstaufruf, damit cpu_percent beim nächsten Mal sinnvolle Werte liefert
        psutil.cpu_percent(interval=None)

    # ------------------------------------------------------------------ DRM
    @staticmethod
    def _discover_drm_cards() -> list[dict]:
        """Findet AMD/Intel GPUs über sysfs (/sys/class/drm/cardX)."""
        cards: list[dict] = []
        drm = Path("/sys/class/drm")
        if not drm.exists():
            return cards
        for card in sorted(drm.glob("card[0-9]*")):
            device = card / "device"
            vendor_file = device / "vendor"
            if not vendor_file.exists():
                continue
            try:
                vendor_id = vendor_file.read_text().strip().lower()
            except OSError:
                continue
            # 0x1002 = AMD, 0x8086 = Intel
            vendor = {"0x1002": "amd", "0x8086": "intel"}.get(vendor_id)
            if not vendor:
                continue
            cards.append({"vendor": vendor, "path": device})
        return cards

    # --------------------------------------------------------------- Nvidia
    @classmethod
    def _find_nvidia_smi(cls) -> Optional[str]:
        found = shutil.which("nvidia-smi")
        if found:
            return found
        # Fallback u. a. für QNAP, wo der Treiber nach /usr/local/nvidia
        # gemountet und nicht im PATH liegt.
        for cand in cls._NVIDIA_SMI_CANDIDATES:
            if Path(cand).exists():
                return cand
        return None

    def _nvidia(self) -> list[GpuMetric]:
        if getattr(self, "_nvml", None) is not None and self._nvml.available:
            nvml_gpus = self._nvml.read_gpus()
            if nvml_gpus:
                return [
                    GpuMetric(
                        vendor="nvidia",
                        name=g["name"],
                        util=g.get("util"),
                        mem_used=g.get("mem_used"),
                        mem_total=g.get("mem_total"),
                        temperature=g.get("temperature"),
                        enc_util=g.get("enc_util"),
                        dec_util=g.get("dec_util"),
                        render_util=g.get("render_util"),
                    )
                    for g in nvml_gpus
                ]
        out = _run([
            self._nvidia_smi or "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,utilization.encoder,utilization.decoder",
            "--format=csv,noheader,nounits",
        ])
        if not out:
            # Fallback auf 5-Spalten-Abfrage bei sehr alten nvidia-smi-Versionen
            out = _run([
                self._nvidia_smi or "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ])
        gpus: list[GpuMetric] = []
        if not out:
            return gpus
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            name, util, mused, mtot, temp = parts[:5]
            render_u = _to_float(util)
            enc_u = _to_float(parts[5]) if len(parts) >= 6 else None
            dec_u = _to_float(parts[6]) if len(parts) >= 7 else None
            cands = [v for v in (render_u, enc_u, dec_u) if v is not None]
            eff_util = max(cands) if cands else None
            gpus.append(
                GpuMetric(
                    vendor="nvidia",
                    name=name,
                    util=eff_util,
                    mem_used=_to_float(mused),
                    mem_total=_to_float(mtot),
                    temperature=_to_float(temp),
                    enc_util=enc_u,
                    dec_util=dec_u,
                    render_util=render_u,
                )
            )
        return gpus

    def _windows_non_nvidia(self) -> list[GpuMetric]:
        """Liest AMD- und Intel-GPUs unter Windows via DXGI + PDH."""
        adapters = [
            a for a in (getattr(self, "_dxgi_adapters", None) or [])
            if a.get("vendor") in ("amd", "intel")
        ]
        if not adapters:
            return []
        pdh_mon = getattr(self, "_pdh", None)
        by_luid = pdh_mon.sample_by_luid() if pdh_mon is not None else {}
        out: list[GpuMetric] = []
        for ad in adapters:
            sample = by_luid.get(ad.get("luid") or "", {})
            out.append(
                GpuMetric(
                    vendor=ad["vendor"],
                    name=ad["name"],
                    util=sample.get("util"),
                    mem_used=sample.get("mem_used"),
                    mem_total=ad.get("vram_mb"),
                    temperature=None,
                    enc_util=sample.get("enc_util"),
                    dec_util=sample.get("dec_util"),
                    render_util=sample.get("render_util"),
                )
            )
        return out

    # ---------------------------------------------------------- AMD (sysfs)
    @staticmethod
    def _amd_sysfs(device: Path) -> GpuMetric:
        util = _read_int(device / "gpu_busy_percent")
        mem_used = _read_int(device / "mem_info_vram_used")
        mem_total = _read_int(device / "mem_info_vram_total")
        # hwmon-Temperatur suchen
        temp: Optional[float] = None
        hwmon = device / "hwmon"
        if hwmon.exists():
            for h in hwmon.glob("hwmon*"):
                t = _read_int(h / "temp1_input")
                if t is not None:
                    temp = t / 1000.0
                    break
        return GpuMetric(
            vendor="amd",
            name="AMD GPU (VAAPI)",
            util=float(util) if util is not None else None,
            mem_used=(mem_used / 1024 / 1024) if mem_used else None,
            mem_total=(mem_total / 1024 / 1024) if mem_total else None,
            temperature=temp,
        )

    # ------------------------------------------------------- Intel (sysfs/top)
    def _intel(self, device: Path) -> GpuMetric:
        util: Optional[float] = None
        # intel_gpu_top liefert die zuverlässigste Auslastung (Render/3D Engine)
        if self._has_intel_gpu_top:
            out = _run(["intel_gpu_top", "-J", "-s", "500"], timeout=3.0)
            util = _parse_intel_gpu_top(out)
        return GpuMetric(
            vendor="intel",
            name="Intel GPU (QSV/VAAPI)",
            util=util,
        )

    # --------------------------------------------------------------- Snapshot
    def snapshot(self) -> HardwareSnapshot:
        vm = psutil.virtual_memory()
        cpu_pct = round(psutil.cpu_percent(interval=None), 1)
        temp, freq, load = _cpu_extra()
        try:
            per_core = [round(x, 1) for x in psutil.cpu_percent(interval=None, percpu=True)]
        except Exception:  # pragma: no cover
            per_core = []
        snap = HardwareSnapshot(
            cpu_percent=cpu_pct,
            cpu_cores=psutil.cpu_count(logical=True) or 0,
            cpu_temp=temp,
            cpu_freq_mhz=freq,
            load_avg=load,
            cpu_per_core=per_core,
            ram_percent=round(vm.percent, 1),
            ram_used_gb=round((vm.total - vm.available) / (1024 ** 3), 2),
            ram_total_gb=round(vm.total / (1024 ** 3), 2),
        )

        gpus: list[GpuMetric] = []
        if self._has_nvidia:
            gpus.extend(self._nvidia())
        if self._drm_cards:
            for card in self._drm_cards:
                if card["vendor"] == "amd":
                    gpus.append(self._amd_sysfs(card["path"]))
                elif card["vendor"] == "intel":
                    gpus.append(self._intel(card["path"]))
        else:
            gpus.extend(self._windows_non_nvidia())

        snap.gpus = [asdict(g) for g in gpus]

        # Verlaufspuffer fortschreiben (max. GPU-Auslastung als Sammelwert).
        gpu_utils = [g.util for g in gpus if g.util is not None]
        gpu_val = round(max(gpu_utils), 1) if gpu_utils else None
        self._hist.append((round(self._time.time(), 1), cpu_pct, snap.ram_percent, gpu_val))
        snap.history = {
            "t": [h[0] for h in self._hist],
            "cpu": [h[1] for h in self._hist],
            "ram": [h[2] for h in self._hist],
            "gpu": [h[3] for h in self._hist],
            "has_gpu": bool(gpus),
        }
        return snap

    def available_platforms(self) -> list[str]:
        """Welche Encode-Plattformen sind aufgrund der HW vermutlich nutzbar?"""
        vendors = {c["vendor"] for c in self._drm_cards} | {
            a["vendor"] for a in (getattr(self, "_dxgi_adapters", None) or [])
        }
        platforms: list[str] = []
        if self._has_nvidia or "nvidia" in vendors:
            platforms.append("nvidia")
        if "intel" in vendors:
            platforms.append("intel")
        if "amd" in vendors:
            platforms.append("amd")
        platforms.append("cpu")
        return platforms

    # ---------------------------------------------------- Encoder-Kapazität
    def _nvidia_names(self) -> list[str]:
        if getattr(self, "_nvml", None) is not None and self._nvml.available:
            names = self._nvml.device_names()
            if names:
                return names
        out = _run([
            self._nvidia_smi or "nvidia-smi",
            "--query-gpu=name", "--format=csv,noheader",
        ])
        if out:
            return [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
        dxgi_names = [
            a["name"] for a in (getattr(self, "_dxgi_adapters", None) or [])
            if a.get("vendor") == "nvidia" and a.get("name")
        ]
        return dxgi_names

    @classmethod
    def nvidia_driver_info(cls) -> dict:
        """Ermittelt die installierte NVIDIA-Treiberversion und prüft die FFmpeg-Linien-Vorgabe.

        Linie `latest` (main, FFmpeg 9.0 / CUDA 13.x / NVENC 13.1) verlangt Host-Treiber >= 610.
        Linie `legacy` (`latest-legacy`, FFmpeg 8.1) funktioniert auch mit älteren Treibern (< 610, ab ~525).
        """
        from . import config
        from .win_gpu import NvmlReader
        channel = str(getattr(config, "IMAGE_CHANNEL", "latest") or "latest").strip().lower()
        min_major = 525 if channel == "legacy" else 610
        smi = cls._find_nvidia_smi()
        raw = ""
        present = bool(smi)
        if smi:
            out = _run([smi, "--query-gpu=driver_version", "--format=csv,noheader"])
            raw = ((out or "").strip().splitlines() or [""])[0].strip()
        if not raw:
            nvml = NvmlReader()
            if nvml.available:
                present = True
                raw = nvml.driver_version()
        if not present:
            return {
                "present": False,
                "driver_version": "",
                "driver_major": 0,
                "image_channel": channel,
                "required_major": min_major,
                "channel_compatible": False,
                "reason": "Keine NVIDIA-GPU / nvidia-smi nicht gefunden",
            }
        major = 0
        if raw:
            head = raw.split(".", 1)[0].strip()
            try:
                major = int(head)
            except ValueError:
                major = 0
        # Auf Windows-Treiber-Nummern (z. B. 572.xx / 580.xx / 610.xx) gilt dieselbe Hauptversion.
        compatible = bool(major >= min_major) if major > 0 else False
        if not raw:
            reason = "NVIDIA-Treiberversion konnte nicht gelesen werden"
        elif compatible:
            reason = f"NVIDIA-Treiber {raw} kompatibel mit Linie '{channel}' (Mindestvorgabe ≥ {min_major})"
        elif channel != "legacy":
            reason = (
                f"NVIDIA-Treiber {raw} ist älter als {min_major} – "
                f"FFmpeg auf Linie '{channel}' (9.0) benötigt Treiber ≥ {min_major}. "
                f"Für ältere Treiber das Image 'latest-legacy' nutzen."
            )
        else:
            reason = f"NVIDIA-Treiber {raw} unterschreitet Mindestvorgabe ≥ {min_major} für Linie 'legacy'"
        return {
            "present": True,
            "driver_version": raw,
            "driver_major": major,
            "image_channel": channel,
            "required_major": min_major,
            "channel_compatible": compatible,
            "reason": reason,
        }

    def encode_capacity(self) -> dict:
        """Best-effort-Schätzung, wie viele Encodes parallel sinnvoll sind.

        Die genaue Zahl der NVENC-Engines wird von nvidia-smi nicht direkt
        gemeldet; wir schätzen sie über den GPU-Namen und erlauben dem Nutzer,
        den Wert im UI zu überschreiben.
        """
        import os as _os
        gpus: list[dict] = []
        nvenc_engines = 0
        if self._has_nvidia:
            names = self._nvidia_names() or ["NVIDIA GPU"]
            for name in names:
                eng = _nvenc_engines(name)
                nvenc_engines += eng
                gpus.append({"vendor": "nvidia", "name": name, "encoders": eng})
        if self._drm_cards:
            for card in self._drm_cards:
                if card["vendor"] == "intel":
                    from . import ffmpeg_utils as _ff
                    label = "Intel GPU (%s)" % ("VAAPI" if _ff.intel_uses_vaapi() else "QSV")
                else:
                    label = "AMD GPU (%s)" % ("AMF" if _os.name == "nt" else "VAAPI")
                gpus.append({"vendor": card["vendor"], "name": label, "encoders": 1})
        else:
            for ad in (getattr(self, "_dxgi_adapters", None) or []):
                if ad.get("vendor") == "intel":
                    gpus.append({"vendor": "intel", "name": f"{ad['name']} (QSV)", "encoders": 1})
                elif ad.get("vendor") == "amd":
                    gpus.append({"vendor": "amd", "name": f"{ad['name']} (AMF)", "encoders": 1})

        cpu_threads = psutil.cpu_count(logical=True) or 2

        if self._has_nvidia and nvenc_engines >= 1:
            # NVENC verarbeitet mehrere Sessions gut nebenläufig; 2 pro Engine
            # ist ein praxistauglicher Durchsatz-Sweetspot.
            suggested = min(6, max(2, nvenc_engines * 2))
        elif gpus:
            # QSV/VAAPI/AMF: eine feste Engine -> parallele Encodes bringen kaum etwas.
            suggested = 1
        else:
            # Reines CPU-Encoding: grob an Threads koppeln.
            suggested = max(1, min(3, cpu_threads // 8))

        return {
            "gpus": gpus,
            "nvenc_engines": nvenc_engines,
            "cpu_threads": cpu_threads,
            "suggested_parallel": suggested,
        }


# NVENC-Engine-Anzahl je GPU (nvidia-smi meldet sie nicht direkt).
# Ein Eintrag matcht, wenn ALLE Teilstrings im GPU-Namen vorkommen; damit
# greift z. B. ("RTX","4000","ADA") auch für "RTX 4000 SFF Ada Generation".
# Multi-GPU-Boards (z. B. A16) listet nvidia-smi ohnehin je GPU einzeln,
# daher nur Engines pro Einzel-GPU. Fallback = 1.
_NVENC_MULTI = (
    (("A100",), 0), (("H100",), 0), (("B200",), 0), (("H200",), 0),  # reine Rechen-GPUs ohne NVENC
    (("RTX", "PRO", "6000"), 4),           # Blackwell Workstation
    (("RTX", "5090"), 3),                  # Blackwell Flaggschiff (3× 9th-Gen NVENC)
    (("L40",), 3), (("L20",), 3),          # Ada Datacenter
    (("RTX", "5080"), 2),                  # Blackwell (2× NVENC)
    (("RTX", "5070", "TI"), 2),            # Blackwell AD/GB203 (2× NVENC)
    (("RTX", "6000", "ADA"), 2),
    (("RTX", "5000", "ADA"), 2),
    (("RTX", "4500", "ADA"), 2),
    (("RTX", "4000", "ADA"), 2),           # inkl. RTX 4000 SFF Ada
    (("RTX", "4090"), 2),
    (("RTX", "4080"), 2),
    (("RTX", "4070", "TI"), 2),
)


def _nvenc_engines(name: str) -> int:
    up = name.upper()
    for tokens, count in _NVENC_MULTI:
        if all(t in up for t in tokens):
            return count
    return 1


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _parse_intel_gpu_top(out: Optional[str]) -> Optional[float]:
    """intel_gpu_top -J gibt einen JSON-Stream aus; wir nehmen das letzte
    vollständige Objekt und lesen die höchste Engine-Auslastung."""
    if not out:
        return None
    # Der Stream besteht aus aneinandergereihten JSON-Objekten.
    objects = re.findall(r"\{.*?\}(?=\s*\{|\s*$)", out, flags=re.DOTALL)
    for raw in reversed(objects):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        engines = data.get("engines", {})
        busy_values = [
            e.get("busy", 0.0) for e in engines.values() if isinstance(e, dict)
        ]
        if busy_values:
            return round(max(busy_values), 1)
    return None
