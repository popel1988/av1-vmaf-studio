"""Größenziel: Video-Bitrate so wählen, dass Video + Tonspuren unter Budget passen."""
from __future__ import annotations

from typing import Any, Optional


# Mindest-Videobitrate (kbit/s), darunter lohnt Encode kaum / Encoder meckern.
_MIN_VIDEO_KBPS = 400
# Fallback je Tonspur ohne Bitrate-Info.
_FALLBACK_AUDIO_KBPS = 640
# Container/Subs-Overhead relativ zum Ziel.
_OVERHEAD_FRAC = 0.015


def _audio_bitrate_bps(track: dict) -> int:
    br = track.get("bitrate") or track.get("bit_rate") or 0
    try:
        br = int(br)
    except (TypeError, ValueError):
        br = 0
    if br > 0:
        # Manche Probe liefern kbit/s statt bit/s
        if br < 100_000:
            return br * 1000
        return br
    return _FALLBACK_AUDIO_KBPS * 1000


def select_audio_tracks(info, settings) -> list[dict]:
    """Behaltene Tonspuren aus Probe-Info + JobSettings ableiten."""
    audio = list(getattr(info, "audio", None) or [])
    if not audio and isinstance(info, dict):
        audio = list(info.get("audio") or [])
    if not audio:
        return []

    # Explizite Spurauswahl (Indizes relativ zur Audio-Liste oder absolute stream index)
    tracks = getattr(settings, "audio_tracks", None)
    if tracks is None and isinstance(settings, dict):
        tracks = settings.get("audio_tracks")
    mode = getattr(settings, "audio_mode", None)
    if mode is None and isinstance(settings, dict):
        mode = settings.get("audio_mode")
    if mode == "none":
        return []

    if tracks is not None and len(tracks) == 0:
        return []
    if tracks:
        want = {int(x) for x in tracks}
        # Match gegen relative Position oder .index
        selected = []
        for i, a in enumerate(audio):
            idx = a.get("index", i) if isinstance(a, dict) else i
            if i in want or int(idx) in want:
                selected.append(a if isinstance(a, dict) else {"index": i})
        return selected or list(audio)

    # Sprach-Whitelist
    langs = getattr(settings, "audio_languages", None)
    if langs is None and isinstance(settings, dict):
        langs = settings.get("audio_languages")
    prefer = {x.strip().lower() for x in str(langs or "").replace(";", ",").split(",") if x.strip()}
    if prefer:
        kept = [a for a in audio
                if str((a.get("language") if isinstance(a, dict) else "") or "").lower() in prefer]
        if kept:
            return kept
    return list(audio)


def compute_video_bitrate_kbps(
    *,
    size_target_mb: float,
    duration: float,
    audio_tracks: list[dict],
    min_kbps: int = _MIN_VIDEO_KBPS,
) -> dict[str, Any]:
    """Video-kbit/s für Größenziel berechnen.

    Rückgabe: {ok, video_kbps, target_bytes, audio_bytes, overhead_bytes, message}
    """
    target_mb = float(size_target_mb or 0)
    if target_mb <= 0:
        return {"ok": False, "video_kbps": 0, "message": "Kein Größenziel"}
    dur = float(duration or 0)
    if dur <= 1:
        return {"ok": False, "video_kbps": 0, "message": "Dauer unbekannt"}

    target_bytes = int(target_mb * 1024 * 1024)
    overhead = int(target_bytes * _OVERHEAD_FRAC)
    audio_bytes = 0
    for a in audio_tracks or []:
        bps = _audio_bitrate_bps(a if isinstance(a, dict) else {})
        audio_bytes += int(bps / 8.0 * dur)

    video_bytes = target_bytes - audio_bytes - overhead
    if video_bytes < int(min_kbps * 1000 / 8 * dur):
        return {
            "ok": False,
            "video_kbps": 0,
            "target_bytes": target_bytes,
            "audio_bytes": audio_bytes,
            "overhead_bytes": overhead,
            "message": (
                f"Ziel {target_mb:.0f} MB zu klein für Tonspuren "
                f"({audio_bytes / (1024 * 1024):.1f} MB Audio + Overhead)"
            ),
        }
    video_kbps = int(video_bytes * 8 / dur / 1000)
    video_kbps = max(min_kbps, video_kbps)
    return {
        "ok": True,
        "video_kbps": video_kbps,
        "target_bytes": target_bytes,
        "audio_bytes": audio_bytes,
        "overhead_bytes": overhead,
        "message": (
            f"Ziel {target_mb:.0f} MB -> Video ~{video_kbps} kbit/s "
            f"(Audio ~{audio_bytes / (1024 * 1024):.1f} MB)"
        ),
    }


def apply_size_target(settings, info) -> Optional[str]:
    """Settings für Größenziel anpassen (ABR + Bitrate). Rückgabe: Statusmeldung oder None."""
    target = float(getattr(settings, "size_target_mb", 0) or 0)
    if target <= 0:
        return None
    duration = float(getattr(info, "duration", 0) or 0)
    tracks = select_audio_tracks(info, settings)
    res = compute_video_bitrate_kbps(
        size_target_mb=target, duration=duration, audio_tracks=tracks)
    if not res.get("ok"):
        return res.get("message") or "Größenziel nicht anwendbar"
    settings.rate_mode = "abr"
    settings.quality = int(res["video_kbps"])
    # Cap als zusätzliche Obergrenze mitziehen
    cap = int(getattr(settings, "max_video_bitrate_kbps", 0) or 0)
    if cap > 0:
        settings.quality = min(settings.quality, cap)
    else:
        settings.max_video_bitrate_kbps = int(res["video_kbps"])
    return res.get("message") or ""
