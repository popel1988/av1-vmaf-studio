"""Nicht blockierende Messwerte und registrierte, eigene Medienprozesse.

Kein Prozess-Scanning, keine GPU-Schätzwerte und keine Hintergrund-Pollingtasks.
Die erste CPU-Probe hat noch keinen Messzeitraum und liefert deshalb None.
"""
from __future__ import annotations

import copy
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
except ImportError:  # Runtime-Anzeige darf Wiedergabe nicht voraussetzen.
    psutil = None

OWNERS = frozenset({"player", "ab_standard", "ab_nerd", "preview", "editor"})


def clean_owner(owner: str) -> str:
    return owner if owner in OWNERS else "player"


def process_running(proc) -> bool:
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except (OSError, ProcessLookupError):
        # Unbekannt ist nicht "beendet": insbesondere bei Stop nicht verlieren.
        return getattr(proc, "returncode", None) is None


class ProcessMetrics:
    """Ein psutil-Handle pro Popen; keine Messung fremder/wiederverwendeter PIDs."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proc = None
        self._handle = None
        self._primed = False

    def sample(self, proc) -> dict:
        result = {"cpu_percent": None, "rss_bytes": None, "ram_mb": None,
                  "metrics_available": False, "gpu": None}
        with self._lock:
            if not process_running(proc) or psutil is None:
                return result
            try:
                if self._proc is not proc:
                    self._proc = proc
                    self._handle = psutil.Process(int(proc.pid))
                    self._primed = False
                handle = self._handle
                if handle is None or not handle.is_running():
                    return result
                # Popen besitzt weiterhin den Kindprozess. Nach seinem Ende
                # werden dessen PID/Messwerte auch bei PID-Reuse nicht benutzt.
                if not process_running(proc):
                    return result
                cpu = handle.cpu_percent(interval=None)
                rss = int(handle.memory_info().rss)
                result.update(cpu_percent=float(cpu) if self._primed else None,
                              rss_bytes=rss, ram_mb=round(rss / 1048576, 2),
                              metrics_available=True)
                self._primed = True
            except (OSError, ValueError, psutil.Error):
                self._handle = None
        return result


@dataclass
class _Stream:
    id: str
    proc: subprocess.Popen
    owner: str
    label: str
    source: str
    effective: dict
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    stopping: bool = False
    metrics: ProcessMetrics = field(default_factory=ProcessMetrics)


_LOCK = threading.RLock()
_STREAMS: dict[str, _Stream] = {}


def register_process(proc, *, owner: str = "player", label: str = "",
                     source: str = "", effective: Optional[dict] = None) -> str:
    sid = "stream-" + uuid.uuid4().hex[:12]
    row = _Stream(sid, proc, clean_owner(owner), str(label or "Medienstream")[:200],
                  str(source), copy.deepcopy(effective or {}))
    with _LOCK:
        _STREAMS[sid] = row
    # Prime CPU outside the registry lock; never wait for a sample.
    row.metrics.sample(proc)
    return sid


def finish_process(sid: str) -> None:
    with _LOCK:
        row = _STREAMS.get(sid)
        if row and not process_running(row.proc):
            row.finished_at = row.finished_at or time.time()


def stop_session(sid: str, *, owner: Optional[str] = None) -> bool:
    with _LOCK:
        row = _STREAMS.get(sid)
        if row is None or (owner is not None and row.owner != owner):
            return False
        row.stopping = True
    # Only the registered Popen, never an arbitrary client-supplied PID.
    if process_running(row.proc):
        if os.name == "posix":
            try:
                os.killpg(row.proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            row.proc.kill()
        except OSError:
            pass
    try:
        row.proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Weiter laufende Prozesse bleiben als stopping sichtbar.
    finish_process(sid)
    return True


def list_runtime_sessions(*, owner: Optional[str] = None,
                          include_finished: bool = True) -> list[dict]:
    with _LOCK:
        rows = list(_STREAMS.values())
    result = []
    for row in rows:
        if owner is not None and row.owner != owner:
            continue
        running = process_running(row.proc)
        with _LOCK:
            if not running:
                row.finished_at = row.finished_at or time.time()
            state = "stopping" if running and row.stopping else "running" if running else "finished"
            finished_at = row.finished_at
        if not include_finished and state == "finished":
            continue
        result.append({
            "id": row.id, "session_id": row.id, "kind": "media_stream",
            "owner": row.owner, "label": row.label, "path": row.source,
            "source": {"path": row.source}, "state": state,
            "running": running, "process_running": running,
            "encode_paused": False, "pid": row.proc.pid,
            "created_at": row.created_at, "finished_at": finished_at,
            "mode": "stream", "effective": copy.deepcopy(row.effective),
            "requested": {}, "fallback": False, "fallback_reasons": [],
            "can_stop": running, "error": "",
            **row.metrics.sample(row.proc),
        })
    return result


def stop_all() -> None:
    with _LOCK:
        ids = list(_STREAMS)
    for sid in ids:
        stop_session(sid)


def cleanup_idle(max_idle: float = 3600) -> int:
    """Nur abgeschlossene Stream-Einträge entfernen; aktive Streams nie erraten."""
    now = time.time()
    removed = 0
    with _LOCK:
        for sid, row in list(_STREAMS.items()):
            if not process_running(row.proc):
                row.finished_at = row.finished_at or now
                if now - row.finished_at > max_idle:
                    _STREAMS.pop(sid, None)
                    removed += 1
    return removed
