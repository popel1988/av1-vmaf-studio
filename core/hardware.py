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
    util: Optional[float] = None  # Auslastung in %
    mem_used: Optional[float] = None  # MB
    mem_total: Optional[float] = None  # MB
    temperature: Optional[float] = None  # °C


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
        self._nvidia_smi = self._find_nvidia_smi()
        self._has_nvidia = self._nvidia_smi is not None
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
            gpus.append(
                GpuMetric(
                    vendor="nvidia",
                    name=name,
                    util=_to_float(util),
                    mem_used=_to_float(mused),
                    mem_total=_to_float(mtot),
                    temperature=_to_float(temp),
                )
            )
        return gpus

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
        for card in self._drm_cards:
            if card["vendor"] == "amd":
                gpus.append(self._amd_sysfs(card["path"]))
            elif card["vendor"] == "intel":
                gpus.append(self._intel(card["path"]))

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
        platforms = ["cpu"]
        if self._has_nvidia:
            platforms.insert(0, "nvidia")
        vendors = {c["vendor"] for c in self._drm_cards}
        if "intel" in vendors:
            platforms.insert(0, "intel")
        if "amd" in vendors:
            platforms.insert(0, "amd")
        return platforms

    # ---------------------------------------------------- Encoder-Kapazität
    def _nvidia_names(self) -> list[str]:
        out = _run([
            self._nvidia_smi or "nvidia-smi",
            "--query-gpu=name", "--format=csv,noheader",
        ])
        if not out:
            return []
        return [ln.strip() for ln in out.strip().splitlines() if ln.strip()]

    def encode_capacity(self) -> dict:
        """Best-effort-Schätzung, wie viele Encodes parallel sinnvoll sind.

        Die genaue Zahl der NVENC-Engines wird von nvidia-smi nicht direkt
        gemeldet; wir schätzen sie über den GPU-Namen und erlauben dem Nutzer,
        den Wert im UI zu überschreiben.
        """
        gpus: list[dict] = []
        nvenc_engines = 0
        if self._has_nvidia:
            names = self._nvidia_names() or ["NVIDIA GPU"]
            for name in names:
                eng = _nvenc_engines(name)
                nvenc_engines += eng
                gpus.append({"vendor": "nvidia", "name": name, "encoders": eng})
        for card in self._drm_cards:
            label = "Intel GPU (QSV)" if card["vendor"] == "intel" else "AMD GPU (VAAPI)"
            gpus.append({"vendor": card["vendor"], "name": label, "encoders": 1})

        cpu_threads = psutil.cpu_count(logical=True) or 2

        if self._has_nvidia and nvenc_engines >= 1:
            # NVENC verarbeitet mehrere Sessions gut nebenläufig; 2 pro Engine
            # ist ein praxistauglicher Durchsatz-Sweetspot.
            suggested = min(6, max(2, nvenc_engines * 2))
        elif gpus:
            # QSV/VAAPI: eine feste Engine -> parallele Encodes bringen kaum etwas.
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
    (("A100",), 0), (("H100",), 0),        # reine Rechen-GPUs ohne NVENC
    (("L40",), 3), (("L20",), 3),          # Ada Datacenter
    (("RTX", "6000", "ADA"), 2),
    (("RTX", "5000", "ADA"), 2),
    (("RTX", "4500", "ADA"), 2),
    (("RTX", "4000", "ADA"), 2),           # inkl. RTX 4000 SFF Ada
    (("RTX", "4090"), 2),
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
