"""Echte Encoder-Fähigkeiten (per Mini-Encode getestet), gecacht + persistiert.

Die reine `ffmpeg -encoders`-Liste sagt nur, was im Build steckt – nicht, was die
Hardware wirklich kann (z. B. Intel-iGPU ohne AV1). Dieses Modul führt für jede
Plattform/Codec-Kombination einen winzigen echten Encode aus (via
``diagnostics._test_encode``) und merkt sich das Ergebnis, damit die UI nur noch
das anbietet, was tatsächlich läuft.

Das Ergebnis wird nach ``/data/capabilities.json`` geschrieben und beim Start
geladen, sodass der (einige Sekunden dauernde) Test nicht bei jedem Neustart
erneut laufen muss. Über die Diagnose-Seite (Funktionstest) lässt er sich neu
auslösen.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from . import config
from . import ffmpeg_utils as ff

logger = logging.getLogger("vcompress.capabilities")

_CACHE_PATH = config.DATA_DIR / "capabilities.json"
_CODECS = ("av1", "hevc", "h264")

_lock = threading.RLock()
_cache: Optional[dict] = None
_computing = False


def _load() -> Optional[dict]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            if _CACHE_PATH.exists():
                _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as e:  # pragma: no cover
            logger.debug("Capabilities-Cache nicht lesbar: %s", e)
            _cache = None
        return _cache


def get_cached() -> Optional[dict]:
    """Gecachtes Ergebnis (oder None, wenn noch nie getestet)."""
    return _load()


def results_map() -> dict:
    """{"plattform:codec": bool} – leeres Dict, wenn noch nicht getestet."""
    c = _load()
    return dict((c or {}).get("results", {}))


def compute(monitor, platforms: Optional[list] = None) -> dict:
    """Führt die echten Encode-Tests aus, cached + persistiert das Ergebnis."""
    from . import diagnostics as diag  # spät, um Import-Zyklus zu vermeiden

    if platforms is None:
        try:
            platforms = list(monitor.available_platforms())
        except Exception:  # pragma: no cover
            platforms = ["cpu"]
    if "cpu" not in platforms:
        platforms = list(platforms) + ["cpu"]

    results: dict[str, bool] = {}
    for p in platforms:
        for c in _CODECS:
            key = f"{p}:{c}"
            if not ff.encoder_available(p, c):
                results[key] = False
                continue
            ok, _ = diag._test_encode(p, c)
            results[key] = bool(ok)

    data = {"generated_at": time.time(), "results": results}
    with _lock:
        global _cache
        _cache = data
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CACHE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_CACHE_PATH)
        except Exception as e:  # pragma: no cover
            logger.debug("Capabilities-Cache nicht schreibbar: %s", e)
    logger.info("Encoder-Fähigkeiten ermittelt: %s",
                ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in results.items()))
    return data


def compute_async(monitor) -> None:
    """Test im Hintergrund starten (nur eine Ausführung gleichzeitig)."""
    global _computing
    with _lock:
        if _computing:
            return
        _computing = True

    def _run():
        global _computing
        try:
            compute(monitor)
        finally:
            with _lock:
                _computing = False

    threading.Thread(target=_run, daemon=True).start()


def ensure_async(monitor) -> None:
    """Beim Start: nur testen, wenn noch kein Cache existiert."""
    if _load() is None:
        compute_async(monitor)
