"""Echte Encode-/Decode-Fähigkeiten (per Mini-Test), gecacht + persistiert.

Die reine `ffmpeg -encoders/-decoders`-Liste sagt nur, was im Build steckt –
nicht, was die Hardware wirklich kann (z. B. Intel-iGPU ohne AV1). Dieses Modul
führt für jede Plattform/Codec-Kombination winzige echte Encode- und Decode-
Tests aus und merkt sich das Ergebnis, damit UI und Player nur noch das
anbieten bzw. nutzen, was tatsächlich läuft.

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
    """Encode: {"plattform:codec": bool} – leeres Dict, wenn noch nicht getestet."""
    c = _load()
    return dict((c or {}).get("results", {}) or {})


def decode_results_map() -> dict:
    """Decode: {"plattform:codec": bool} – leeres Dict, wenn noch nicht getestet."""
    c = _load()
    return dict((c or {}).get("decode", {}) or {})


def decode_ok(platform: str, codec: str) -> bool:
    """Ob HW-Decode für Plattform/Codec laut Cache (oder Build-Fallback) ok ist."""
    codec = ff.normalize_video_codec(codec) or (codec or "").lower()
    if codec not in _CODECS:
        return False
    if (platform or "").lower() == "cpu":
        return True
    results = decode_results_map()
    key = f"{platform}:{codec}"
    if results:
        return bool(results.get(key))
    return ff.decoder_available(platform, codec)


def _persist(data: dict) -> None:
    global _cache
    with _lock:
        _cache = data
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CACHE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_CACHE_PATH)
        except Exception as e:  # pragma: no cover
            logger.debug("Capabilities-Cache nicht schreibbar: %s", e)


def compute(monitor, platforms: Optional[list] = None) -> dict:
    """Führt Encode- und Decode-Tests aus, cached + persistiert das Ergebnis."""
    from . import diagnostics as diag  # spät, um Import-Zyklus zu vermeiden

    if platforms is None:
        try:
            platforms = list(monitor.available_platforms())
        except Exception:  # pragma: no cover
            platforms = ["cpu"]
    if "cpu" not in platforms:
        platforms = list(platforms) + ["cpu"]

    results: dict[str, bool] = {}
    decode: dict[str, bool] = {}
    for p in platforms:
        for c in _CODECS:
            key = f"{p}:{c}"
            if not ff.encoder_available(p, c):
                results[key] = False
            else:
                ok, _ = diag._test_encode(p, c)
                results[key] = bool(ok)

            if p == "cpu":
                decode[key] = True
            elif not ff.decoder_available(p, c):
                decode[key] = False
            else:
                ok_d, _ = diag._test_decode(p, c)
                decode[key] = bool(ok_d)

    data = {
        "generated_at": time.time(),
        "results": results,
        "decode": decode,
    }
    _persist(data)
    logger.info(
        "Encoder-Fähigkeiten: %s",
        ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in results.items()),
    )
    logger.info(
        "Decoder-Fähigkeiten: %s",
        ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in decode.items()),
    )
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
    """Beim Start: testen, wenn noch kein Cache oder Decode-Teil fehlt."""
    c = _load()
    if c is None or "decode" not in (c or {}):
        compute_async(monitor)
