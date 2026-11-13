"""Windows-native GPU-Erkennung & Echtzeit-Metriken ohne Prozess-Starts.

Nutzt ausschließlich mitgelieferte System-DLLs via ``ctypes``:
- ``nvml.dll`` (NVIDIA Management Library, Teil jedes NVIDIA-Treibers) für
  NVIDIA-Karten inkl. dedizierter NVENC-/NVDEC-Auslastung, VRAM und Temperatur
  ohne ``nvidia-smi.exe``-Prozess-Overhead.
- ``dxgi.dll`` (``CreateDXGIFactory1`` / ``EnumAdapters1``) für alle DirectX-
  Hardware-Adapter (AMD Radeon, Intel Arc/Iris/UHD, NVIDIA) inkl. exaktem
  Modellnamen, Vendor-ID, VRAM und Adapter-LUID.
- ``pdh.dll`` (Performance Data Helper) für LUID-zugeordnete Echtzeit-Auslastung
  (3D, VideoEncode, VideoDecode, VideoProcessing) und dedizierte VRAM-Belegung
  von AMD- und Intel-GPUs unter Windows.
"""
from __future__ import annotations

import ctypes
import os
import re
import threading
from typing import Optional

_LUID_ENG_RE = re.compile(
    r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)_phys_\d+.*_engtype_([A-Za-z0-9_]+)"
)
_LUID_MEM_RE = re.compile(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)_phys_\d+")


# ----------------------------------------------------------------------- NVML
class _NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class NvmlReader:
    """Direkter In-Process-Leser für ``nvml.dll`` / ``libnvidia-ml.so.1``."""

    def __init__(self) -> None:
        self._lib = None
        self._lock = threading.Lock()
        self._ok = False
        self._init_lib()

    def _init_lib(self) -> None:
        candidates = (
            ("nvml.dll", r"C:\Windows\System32\nvml.dll",
             r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll")
            if os.name == "nt"
            else ("libnvidia-ml.so.1", "/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1")
        )
        for name in candidates:
            try:
                lib = ctypes.WinDLL(name) if os.name == "nt" else ctypes.CDLL(name)
                if lib.nvmlInit_v2() == 0:
                    self._lib = lib
                    self._ok = True
                    return
            except Exception:
                continue

    @property
    def available(self) -> bool:
        return self._ok and self._lib is not None

    def driver_version(self) -> str:
        if not self.available:
            return ""
        buf = ctypes.create_string_buffer(80)
        with self._lock:
            try:
                if self._lib.nvmlSystemGetDriverVersion(buf, 80) == 0:
                    return buf.value.decode("utf-8", errors="replace").strip()
            except Exception:
                pass
        return ""

    def device_names(self) -> list[str]:
        return [d["name"] for d in self.read_gpus() if d.get("name")]

    def read_gpus(self) -> list[dict]:
        if not self.available:
            return []
        out: list[dict] = []
        with self._lock:
            try:
                cnt = ctypes.c_uint(0)
                if self._lib.nvmlDeviceGetCount_v2(ctypes.byref(cnt)) != 0:
                    return []
                for idx in range(cnt.value):
                    dev = ctypes.c_void_p()
                    if self._lib.nvmlDeviceGetHandleByIndex_v2(idx, ctypes.byref(dev)) != 0:
                        continue
                    nbuf = ctypes.create_string_buffer(96)
                    name = "NVIDIA GPU"
                    if self._lib.nvmlDeviceGetName(dev, nbuf, 96) == 0:
                        name = nbuf.value.decode("utf-8", errors="replace").strip() or name

                    render_u: Optional[float] = None
                    util = _NvmlUtilization()
                    if self._lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util)) == 0:
                        render_u = float(util.gpu)

                    enc_u: Optional[float] = None
                    eu = ctypes.c_uint(0)
                    es = ctypes.c_uint(0)
                    if self._lib.nvmlDeviceGetEncoderUtilization(
                            dev, ctypes.byref(eu), ctypes.byref(es)) == 0:
                        enc_u = float(eu.value)

                    dec_u: Optional[float] = None
                    du = ctypes.c_uint(0)
                    ds = ctypes.c_uint(0)
                    if self._lib.nvmlDeviceGetDecoderUtilization(
                            dev, ctypes.byref(du), ctypes.byref(ds)) == 0:
                        dec_u = float(du.value)

                    mem_used: Optional[float] = None
                    mem_total: Optional[float] = None
                    mem = _NvmlMemory()
                    if self._lib.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(mem)) == 0:
                        mem_used = round(mem.used / (1024 * 1024), 1)
                        mem_total = round(mem.total / (1024 * 1024), 1)

                    temp: Optional[float] = None
                    tval = ctypes.c_uint(0)
                    # 0 == NVML_TEMPERATURE_GPU
                    if self._lib.nvmlDeviceGetTemperature(dev, 0, ctypes.byref(tval)) == 0:
                        temp = float(tval.value)

                    candidates = [v for v in (render_u, enc_u, dec_u) if v is not None]
                    eff_util = max(candidates) if candidates else None
                    out.append({
                        "vendor": "nvidia",
                        "name": name,
                        "util": eff_util,
                        "render_util": render_u,
                        "enc_util": enc_u,
                        "dec_util": dec_u,
                        "mem_used": mem_used,
                        "mem_total": mem_total,
                        "temperature": temp,
                    })
            except Exception:
                return []
        return out


# ----------------------------------------------------------------- DXGI & PDH
def discover_dxgi_adapters() -> list[dict]:
    """Liefert alle physischen Windows-Grafikadapter via ``DXGI 1.1``.

    Software-Adapter (``DXGI_ADAPTER_FLAG_SOFTWARE == 2``, z. B. Microsoft Basic
    Render Driver) werden ausgefiltert. Rückgabe enthält ``vendor``
    (``nvidia`` | ``amd`` | ``intel``), ``name``, ``vram_mb`` und ``luid``.
    """
    if os.name != "nt":
        return []
    try:
        from ctypes import wintypes

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class DXGI_ADAPTER_DESC1(ctypes.Structure):
            _fields_ = [
                ("Description", wintypes.WCHAR * 128),
                ("VendorId", wintypes.UINT),
                ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT),
                ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID),
                ("Flags", wintypes.UINT),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # IID_IDXGIFactory1 = {770aae78-f26f-4dba-a829-253c83d1b387}
        iid = GUID(
            0x770AAE78, 0xF26F, 0x4DBA,
            (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
        )
        dxgi = ctypes.WinDLL("dxgi.dll")
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0 or not factory:
            return []

        vtbl = ctypes.cast(
            ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        enum_adapters1 = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[12])
        release_factory = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])

        adapters: list[dict] = []
        seen_luids: set[str] = set()
        try:
            idx = 0
            while idx < 16:
                adapter = ctypes.c_void_p()
                if enum_adapters1(factory, idx, ctypes.byref(adapter)) != 0 or not adapter:
                    break
                idx += 1
                avtbl = ctypes.cast(
                    ctypes.cast(adapter, ctypes.POINTER(ctypes.c_void_p))[0],
                    ctypes.POINTER(ctypes.c_void_p),
                )
                get_desc1 = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC1)
                )(avtbl[10])
                release_adapter = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(avtbl[2])
                try:
                    desc = DXGI_ADAPTER_DESC1()
                    if get_desc1(adapter, ctypes.byref(desc)) != 0:
                        continue
                    # DXGI_ADAPTER_FLAG_SOFTWARE = 2
                    if desc.Flags & 2:
                        continue
                    vendor = {
                        0x10DE: "nvidia",
                        0x1002: "amd",
                        0x1022: "amd",
                        0x8086: "intel",
                    }.get(int(desc.VendorId))
                    if not vendor:
                        continue
                    luid = f"0x{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08X}_0x{desc.AdapterLuid.LowPart:08X}"
                    if luid.lower() in seen_luids:
                        continue
                    seen_luids.add(luid.lower())
                    vram_bytes = int(desc.DedicatedVideoMemory or 0)
                    adapters.append({
                        "vendor": vendor,
                        "name": str(desc.Description or "").strip() or f"{vendor.upper()} GPU",
                        "vram_mb": round(vram_bytes / (1024 * 1024), 1) if vram_bytes > 0 else None,
                        "luid": luid.lower(),
                        "device_id": int(desc.DeviceId),
                    })
                finally:
                    release_adapter(adapter)
        finally:
            release_factory(factory)
        return adapters
    except Exception:
        return []


class PdhGpuMonitor:
    """Liest Windows-GPU-Engine-Auslastung & VRAM-Nutzung je Adapter-LUID via PDH."""

    _FMT_DOUBLE = 0x00000200

    def __init__(self) -> None:
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._eng_counter = ctypes.c_void_p()
        self._mem_counter = ctypes.c_void_p()
        self._lock = threading.Lock()
        self._ok = False
        if os.name == "nt":
            self._init_pdh()

    def _init_pdh(self) -> None:
        try:
            pdh = ctypes.WinDLL("pdh.dll")
            hq = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(hq)) != 0:
                return
            hc_eng = ctypes.c_void_p()
            hc_mem = ctypes.c_void_p()
            r1 = pdh.PdhAddEnglishCounterW(
                hq, r"\GPU Engine(*)\Utilization Percentage", 0, ctypes.byref(hc_eng)
            )
            r2 = pdh.PdhAddEnglishCounterW(
                hq, r"\GPU Adapter Memory(*)\Dedicated Usage", 0, ctypes.byref(hc_mem)
            )
            if r1 != 0 and r2 != 0:
                pdh.PdhCloseQuery(hq)
                return
            pdh.PdhCollectQueryData(hq)
            self._pdh = pdh
            self._query = hq
            self._eng_counter = hc_eng if r1 == 0 else ctypes.c_void_p()
            self._mem_counter = hc_mem if r2 == 0 else ctypes.c_void_p()
            self._ok = True
        except Exception:
            self._ok = False

    def _read_array(self, counter: ctypes.c_void_p) -> list[tuple[str, float]]:
        if not counter or not self._pdh:
            return []

        class _Val(ctypes.Structure):
            _fields_ = [("CStatus", ctypes.c_ulong), ("doubleValue", ctypes.c_double)]

        class _Item(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _Val)]

        buf_size = ctypes.c_ulong(0)
        item_count = ctypes.c_ulong(0)
        self._pdh.PdhGetFormattedCounterArrayW(
            counter, self._FMT_DOUBLE, ctypes.byref(buf_size), ctypes.byref(item_count), None
        )
        if buf_size.value == 0 or item_count.value == 0:
            return []
        buf = (ctypes.c_byte * buf_size.value)()
        if self._pdh.PdhGetFormattedCounterArrayW(
            counter, self._FMT_DOUBLE, ctypes.byref(buf_size), ctypes.byref(item_count), buf
        ) != 0:
            return []
        arr = ctypes.cast(buf, ctypes.POINTER(_Item))
        out: list[tuple[str, float]] = []
        for i in range(item_count.value):
            item = arr[i]
            if item.FmtValue.CStatus == 0 and item.szName:
                out.append((item.szName, float(item.FmtValue.doubleValue)))
        return out

    def sample_by_luid(self) -> dict[str, dict]:
        """Gibt pro LUID ``{util, render_util, enc_util, dec_util, mem_used}`` zurück."""
        if not self._ok or not self._pdh:
            return {}
        with self._lock:
            try:
                if self._pdh.PdhCollectQueryData(self._query) != 0:
                    return {}
                eng_items = self._read_array(self._eng_counter)
                mem_items = self._read_array(self._mem_counter)
            except Exception:
                return {}

        # Summiere Auslastung je (luid, engine_type) über alle Prozesse
        by_luid_type: dict[str, dict[str, float]] = {}
        for name, val in eng_items:
            if val <= 0.0:
                continue
            m = _LUID_ENG_RE.search(name)
            if not m:
                continue
            luid = m.group(1).lower()
            etype = m.group(2).lower()
            bucket = by_luid_type.setdefault(luid, {})
            bucket[etype] = bucket.get(etype, 0.0) + val

        mem_by_luid: dict[str, float] = {}
        for name, val in mem_items:
            m = _LUID_MEM_RE.search(name)
            if m:
                luid = m.group(1).lower()
                mem_by_luid[luid] = mem_by_luid.get(luid, 0.0) + val

        all_luids = set(by_luid_type.keys()) | set(mem_by_luid.keys())
        result: dict[str, dict] = {}
        for luid in all_luids:
            types = by_luid_type.get(luid, {})
            render = min(100.0, round(
                max([types.get("3d", 0.0), types.get("compute", 0.0),
                     types.get("highpriority3d", 0.0), types.get("highprioritycompute", 0.0)]), 1
            ))
            enc = min(100.0, round(
                sum(v for k, v in types.items() if "videoencode" in k or k.startswith("video_encode")), 1
            ))
            dec = min(100.0, round(
                sum(v for k, v in types.items()
                    if "videodecode" in k or "videoprocessing" in k or "videocodec" in k), 1
            ))
            any_max = min(100.0, round(max([render, enc, dec] + list(types.values()) + [0.0]), 1))
            mem_mb = round(mem_by_luid[luid] / (1024 * 1024), 1) if luid in mem_by_luid else None
            result[luid] = {
                "util": any_max,
                "render_util": render,
                "enc_util": enc,
                "dec_util": dec,
                "mem_used": mem_mb,
            }
        return result

    def close(self) -> None:
        with self._lock:
            if self._ok and self._pdh and self._query:
                try:
                    self._pdh.PdhCloseQuery(self._query)
                except Exception:
                    pass
            self._ok = False
