"""Browser-kompatibles Live-Playback: Video copy + Audio→AAC (+ optional WebVTT)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterator, Optional

from . import config, ffmpeg_utils as ff

logger = logging.getLogger("vcompress.media_stream")

_IMAGE_SUBS = set(ff._SUB_IMAGE) if hasattr(ff, "_SUB_IMAGE") else {
    "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
    "dvb_subtitle", "dvbsub", "xsub",
}

# Toncodecs, die in fragmentiertem MP4 meist ohne Re-Encode durchgereicht
# werden können (schneller → mehr Puffer; Dauer-Problem bleibt ohne UI-Hilfe).
_AUDIO_COPY_OK = {"aac", "mp3", "mp4a", "mp4a.40.2", "opus"}


def is_text_subtitle(codec: str) -> bool:
    c = (codec or "").lower()
    if not c or c in _IMAGE_SUBS:
        return False
    return True


def audio_can_copy(codec: str) -> bool:
    c = (codec or "").lower().strip()
    if not c:
        return False
    if c in _AUDIO_COPY_OK:
        return True
    return any(c.startswith(p) for p in ("mp4a", "aac"))


def build_play_cmd(
    path: Path,
    audio_index: Optional[int] = 0,
    start_sec: float = 0.0,
    audio_codec: str = "",
) -> list[str]:
    """FFmpeg: Video copy, eine Tonspur → AAC (oder copy), fMP4 auf stdout.

    ``start_sec``: Sprung vor dem Demux (Keyframe-Seek) für Player-Suche.
    """
    cmd = [
        config.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
    ]
    if start_sec and start_sec > 0:
        cmd += ["-ss", f"{float(start_sec):.3f}"]
    cmd += [
        "-i", str(path),
        "-map", "0:v:0?",
        "-c:v", "copy",
    ]
    if audio_index is None or audio_index < 0:
        cmd += ["-an"]
    else:
        cmd += ["-map", f"0:a:{int(audio_index)}?"]
        if audio_can_copy(audio_codec):
            cmd += ["-c:a", "copy"]
        else:
            cmd += [
                "-c:a", "aac", "-ac", "2", "-b:a", "192k",
                "-af", "aresample=async=1",
            ]
    cmd += [
        "-sn", "-dn",
        # empty_moov → Dauer im Browser oft Infinity / nur Buffer-Länge.
        # Die UI setzt die echte Dauer aus ffprobe und seekt per -ss-Neustart.
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return cmd


def build_vtt_cmd(path: Path, subtitle_index: int) -> list[str]:
    """Text-Untertitelspur als WebVTT auf stdout."""
    return [
        config.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(path),
        "-map", f"0:s:{int(subtitle_index)}?",
        "-f", "webvtt", "pipe:1",
    ]


def stream_bytes(cmd: list[str], chunk: int = 65536) -> Iterator[bytes]:
    """Führt FFmpeg aus und liefert stdout-Chunks (Prozess wird am Ende beendet)."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    try:
        while True:
            data = proc.stdout.read(chunk)
            if not data:
                break
            yield data
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        err = b""
        try:
            if proc.stderr:
                err = proc.stderr.read() or b""
        except Exception:
            pass
        if proc.returncode not in (0, None, -9, 255) and err:
            logger.debug("stream ffmpeg rc=%s: %s", proc.returncode,
                         err.decode("utf-8", "replace")[-400:])
