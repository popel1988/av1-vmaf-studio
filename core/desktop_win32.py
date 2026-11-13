"""Windows-spezifische Desktop-Integration: Taskleisten-Fortschritt,
Standby-Schutz (SetThreadExecutionState) und Energieaktionen nach Queue-Ende.

Reine Berechnungsfunktionen (``compute_taskbar_status``, ``should_prevent_sleep``)
sind plattformunabhängig testbar; COM-/Win32-Aufrufe laufen defensiv nur unter
``os.name == 'nt'``.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from typing import Optional

LOG = logging.getLogger(__name__)

# ITaskbarList3 Flags
TBPF_NOPROGRESS = 0x0
TBPF_INDETERMINATE = 0x1
TBPF_NORMAL = 0x2
TBPF_ERROR = 0x4
TBPF_PAUSED = 0x8

# SetThreadExecutionState Flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

VALID_POST_ACTIONS = ("none", "sleep", "hibernate", "shutdown")


def normalize_post_action(value: str) -> str:
    v = str(value or "none").strip().lower()
    return v if v in VALID_POST_ACTIONS else "none"


def compute_taskbar_status(queue_state: dict) -> tuple[int, int, int]:
    """Berechnet ``(tbp_flag, completed_permille, total_permille)`` aus ``queue.state()``.

    - Grün (``TBPF_NORMAL``): Mindestens ein Auftrag läuft aktiv.
    - Unbestimmt (``TBPF_INDETERMINATE``): Auftrag läuft, aber noch 0 % Fortschritt.
    - Gelb (``TBPF_PAUSED``): Queue pausiert, alle aktiven Aufträge angehalten
      oder ein Auftrag wartet auf Benutzerauswahl (``awaiting``).
    - Rot (``TBPF_ERROR``): Kein Auftrag aktiv/wartend und der zuletzt beendete
      Auftrag schlug fehl.
    - Aus (``TBPF_NOPROGRESS``): Leerlauf / alles regulär beendet.
    """
    if not isinstance(queue_state, dict):
        return (TBPF_NOPROGRESS, 0, 1000)

    items = queue_state.get("items") or []
    active_ids = set(queue_state.get("active_ids") or [])
    paused = bool(queue_state.get("paused"))
    counts = queue_state.get("counts") or {}
    waiting = int(counts.get("waiting") or 0)
    awaiting = int(counts.get("awaiting") or 0)

    active_items = [
        it for it in items
        if it.get("id") in active_ids or it.get("status") in ("in arbeit", "vmaf-test")
    ]

    if active_items:
        pcts: list[float] = []
        all_suspended = True
        for it in active_items:
            prog = it.get("progress") or {}
            if not prog.get("suspended"):
                all_suspended = False
            pct = prog.get("pct")
            try:
                pct_f = float(pct) if pct is not None else 0.0
            except (TypeError, ValueError):
                pct_f = 0.0
            if pct_f <= 0.0 and it.get("status") == "vmaf-test":
                step = int(prog.get("step") or 0)
                steps = int(prog.get("steps") or 0)
                if steps > 0 and step > 0:
                    pct_f = 100.0 * step / steps
            pcts.append(max(0.0, min(100.0, pct_f)))

        avg_pct = sum(pcts) / len(pcts) if pcts else 0.0
        permille = int(round(avg_pct * 10.0))

        if paused or all_suspended:
            return (TBPF_PAUSED, max(50, permille), 1000)
        if permille <= 0:
            return (TBPF_INDETERMINATE, 0, 1000)
        return (TBPF_NORMAL, max(1, min(1000, permille)), 1000)

    if awaiting > 0:
        return (TBPF_PAUSED, 500, 1000)

    if waiting > 0 and paused:
        return (TBPF_PAUSED, 100, 1000)

    if waiting == 0:
        finished = [
            it for it in items
            if it.get("status") in ("fertig", "fehlgeschlagen") and it.get("finished_at")
        ]
        if finished:
            latest = max(finished, key=lambda x: float(x.get("finished_at") or 0.0))
            if latest.get("status") == "fehlgeschlagen":
                return (TBPF_ERROR, 1000, 1000)

    return (TBPF_NOPROGRESS, 0, 1000)


def should_prevent_sleep(queue_state: dict, enabled: bool = True) -> bool:
    """True, solange der Rechner wegen aktiver Verarbeitung wach bleiben muss."""
    if not enabled or not isinstance(queue_state, dict):
        return False
    items = queue_state.get("items") or []
    active_ids = set(queue_state.get("active_ids") or [])
    active_items = [
        it for it in items
        if it.get("id") in active_ids or it.get("status") in ("in arbeit", "vmaf-test")
    ]
    if not active_items:
        return False
    # Wenn alle aktiven Prozesse explizit eingefroren (suspended) sind, Standby erlauben.
    if all(bool((it.get("progress") or {}).get("suspended")) for it in active_items):
        return False
    return True


class Win32SleepGuard:
    """Verwaltet ``SetThreadExecutionState`` konsistent auf demselben Thread."""

    def __init__(self) -> None:
        self._blocking = False

    @property
    def blocking(self) -> bool:
        return self._blocking

    def update(self, prevent: bool) -> None:
        if os.name != "nt" or prevent == self._blocking:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if prevent else ES_CONTINUOUS
            res = kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
            if res != 0:
                self._blocking = prevent
        except Exception:
            LOG.debug("SetThreadExecutionState fehlgeschlagen", exc_info=True)

    def clear(self) -> None:
        self.update(False)


class Win32Taskbar:
    """Kapselt ``ITaskbarList3`` für Fortschrittsanzeige am Taskleisten-Symbol."""

    def __init__(self) -> None:
        self._ptr = ctypes.c_void_p()
        self._set_val = None
        self._set_state = None
        self._release = None
        self._coinit = False
        self._last: Optional[tuple[int, int, int, int]] = None

    def _ensure_com(self) -> bool:
        if os.name != "nt":
            return False
        if self._ptr and self._set_state and self._set_val:
            return True
        try:
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            ole32 = ctypes.windll.ole32
            hr_init = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
            if hr_init in (0, 1):  # S_OK, S_FALSE
                self._coinit = True

            clsid = GUID(
                0x56FDF344, 0xFD6D, 0x11D0,
                (ctypes.c_ubyte * 8)(0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90),
            )
            iid = GUID(
                0xEA1AFB91, 0x9E28, 0x4B86,
                (ctypes.c_ubyte * 8)(0x90, 0xE9, 0x9E, 0x9F, 0x8A, 0x5E, 0xEF, 0xAF),
            )
            tbl = ctypes.c_void_p()
            if ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(tbl)
            ) != 0 or not tbl:
                return False

            vtbl = ctypes.cast(
                ctypes.cast(tbl, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )
            hr_init_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[3])
            release_fn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])
            if hr_init_fn(tbl) != 0:
                release_fn(tbl)
                return False

            self._set_val = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.HWND,
                ctypes.c_ulonglong, ctypes.c_ulonglong,
            )(vtbl[9])
            self._set_state = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_int,
            )(vtbl[10])
            self._release = release_fn
            self._ptr = tbl
            return True
        except Exception:
            LOG.debug("ITaskbarList3 nicht verfügbar", exc_info=True)
            return False

    def apply(self, hwnd: int, state: int, completed: int = 0, total: int = 1000) -> None:
        if not hwnd or os.name != "nt":
            return
        sig = (hwnd, state, completed, total)
        if sig == self._last:
            return
        if not self._ensure_com():
            return
        try:
            if state in (TBPF_NOPROGRESS, TBPF_INDETERMINATE):
                self._set_state(self._ptr, hwnd, state)
            else:
                self._set_val(self._ptr, hwnd, max(0, int(completed)), max(1, int(total)))
                self._set_state(self._ptr, hwnd, state)
            self._last = sig
        except Exception:
            LOG.debug("Taskbar-Status konnte nicht aktualisiert werden", exc_info=True)

    def close(self) -> None:
        if self._ptr and self._release:
            try:
                self._release(self._ptr)
            except Exception:
                pass
        self._ptr = ctypes.c_void_p()
        self._set_val = None
        self._set_state = None
        self._release = None
        if self._coinit and os.name == "nt":
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
            self._coinit = False


def execute_power_action(action: str) -> bool:
    """Führt nach Ablauf des Countdowns Energie sparen / Ruhezustand / Shutdown aus."""
    action = normalize_post_action(action)
    if os.name != "nt" or action == "none":
        return False
    try:
        if action == "sleep":
            ctypes.windll.powrprof.SetSuspendState(0, 0, 0)
            return True
        if action == "hibernate":
            ctypes.windll.powrprof.SetSuspendState(1, 0, 0)
            return True
        if action == "shutdown":
            subprocess.Popen([
                "shutdown.exe", "/s", "/t", "5",
                "/c", "AV1 / VMAF Studio: Alle Aufträge der Warteschlange sind abgeschlossen.",
            ])
            return True
    except Exception:
        LOG.exception("Energieaktion (%s) fehlgeschlagen", action)
    return False


def unblock_zone_identifiers(base_dir) -> int:
    """Entfernt Windows-Mark-of-the-Web (``:Zone.Identifier`` NTFS-Stream) von
    gebündelten ``.dll``-, ``.pyd``- und ``.exe``-Dateien, damit .NET Framework
    (``pythonnet`` / ``WebView2``) nach einem ZIP-Entpacken ohne ``0x80131515`` startet.

    Gibt die Anzahl bereinigter Dateien zurück (0 auf Nicht-Windows oder ohne Stream).
    """
    if os.name != "nt" or not base_dir:
        return 0
    from pathlib import Path
    root = Path(base_dir)
    if not root.is_dir():
        return 0
    removed = 0
    try:
        delete_file = ctypes.windll.kernel32.DeleteFileW
        targets = [root / "_internal", root / "bin", root]
        seen: set[str] = set()
        for target in targets:
            if not target.is_dir():
                continue
            iterator = target.rglob("*") if target.name in ("_internal", "bin") else target.glob("*")
            for entry in iterator:
                if entry.suffix.lower() not in (".dll", ".pyd", ".exe"):
                    continue
                key = os.path.normcase(str(entry))
                if key in seen:
                    continue
                seen.add(key)
                if delete_file(f"{entry}:Zone.Identifier"):
                    removed += 1
    except Exception:
        LOG.debug("Zone.Identifier-Bereinigung übersprungen", exc_info=True)
    return removed

