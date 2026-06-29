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
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    gpus: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


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

    def __init__(self) -> None:
        self._nvidia_smi = self._find_nvidia_smi()
        self._has_nvidia = self._nvidia_smi is not None
        self._has_radeontop = shutil.which("radeontop") is not None
        self._has_intel_gpu_top = shutil.which("intel_gpu_top") is not None
        self._drm_cards = self._discover_drm_cards()
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
        snap = HardwareSnapshot(
            cpu_percent=round(psutil.cpu_percent(interval=None), 1),
            cpu_cores=psutil.cpu_count(logical=True) or 0,
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
