"""Browser-kompatibles Live-Playback: Video copy + Audio→AAC (+ optional WebVTT)."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
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
    rebase_ts: bool = False,
    video_codec: str = "",
) -> list[str]:
    """FFmpeg: Video copy, eine Tonspur → AAC (oder copy), fMP4 auf stdout.

    ``start_sec``: Sprung vor dem Demux (Keyframe-Seek) für Player-Suche.

    Sync-Hinweise: Bei Video-copy + Audio-Encode und ``frag_keyframe`` puffert
    der Muxer oft bis zum nächsten Keyframe – Ton läuft im Browser voraus.
    Deshalb zeitbasierte Fragmente + Timestamp-Normalisierung.
    """
    cmd = [
        config.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        # Fehlende/kaputte Timestamps regenerieren (DTS/TrueHD → AAC).
        "-fflags", "+genpts",
    ]
    if start_sec and start_sec > 0:
        # Input-Seek: schnell. Fein-Sync über genpts / avoid_negative_ts.
        cmd += ["-ss", f"{float(start_sec):.3f}"]
    cmd += [
        "-i", str(path),
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-map", "0:v:0?",
        "-c:v", "copy",
    ]
    vc = (video_codec or "").lower().strip()
    if vc in ("hevc", "h265"):
        cmd += ["-tag:v", "hvc1"]
    elif vc == "av1":
        cmd += ["-tag:v", "av01"]
    if rebase_ts:
        # Eine Kopie behält die Filmzeit (z. B. 4700 s). Der Clip-Player zählt
        # ab 0. Beide Uhren auf das erste Paket legen, Abstände bleiben.
        cmd += ["-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS"]
    if audio_index is None or audio_index < 0:
        cmd += ["-an"]
    else:
        cmd += ["-map", f"0:a:{int(audio_index)}?"]
        if audio_can_copy(audio_codec):
            cmd += ["-c:a", "copy"]
        else:
            # Nach einem Seek beginnt das kopierte Video am Keyframe *vor* dem
            # Ziel, der Ton exakt beim Ziel. first_pts=0 würde den Ton auf den
            # Videostart ziehen – er liefe dann bis zu einer GOP-Länge voraus.
            afilter = ("aresample=async=1" if start_sec and start_sec > 0
                       else "aresample=async=1000:first_pts=0")
            cmd += [
                "-c:a", "aac", "-ac", "2", "-b:a", "192k",
                "-af", afilter,
            ]
    cmd += [
        "-sn", "-dn",
        "-avoid_negative_ts", "make_zero",
        # Eng interleaven – verhindert, dass der Browser Ton vor Video puffert.
        "-max_interleave_delta", "0",
        "-muxdelay", "0",
        "-muxpreload", "0",
        # Zeitbasierte Fragmente (0.5 s) statt nur an Keyframes – bei langen
        # GOPs sonst oft A/V-Asynchronität im HTML5-Player.
        "-movflags", "empty_moov+default_base_moof",
        "-frag_duration", "500000",
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


class _MediaProcess:
    """Registriert nur eigene Prozesse; stderr darf stdout nie blockieren."""

    def __init__(self, cmd, *, owner="player", label="", source=""):
        from . import runtime
        from .encoder import command_details
        self.log = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self.log,
                                         stdin=subprocess.DEVNULL,
                                         start_new_session=os.name == "posix")
            self.sid = runtime.register_process(
                self.proc, owner=owner, label=label, source=source,
                effective={**command_details(cmd), "video_mode": "none" if "webvtt" in cmd else "copy",
                           "audio_mode": "none" if "-an" in cmd else (
                               "copy" if "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy" else "aac")})
        except BaseException:
            proc = getattr(self, "proc", None)
            if proc is not None:
                proc.kill()
                proc.wait(timeout=5)
            self.log.close()
            raise

    def close(self):
        from . import runtime
        try:
            runtime.stop_session(self.sid)
            if self.proc.poll() is not None:
                self.log.seek(0, 2)
                size = self.log.tell()
                self.log.seek(max(0, size - 400))
                err = self.log.read()
                if self.proc.returncode not in (0, None, -9, 255) and err:
                    logger.debug("stream ffmpeg rc=%s: %s", self.proc.returncode,
                                 err.decode("utf-8", "replace"))
        finally:
            if self.proc.stdout:
                self.proc.stdout.close()
            self.log.close()
            runtime.finish_process(self.sid)


def stream_bytes(cmd: list[str], chunk: int = 65536, *, owner="player", label="", source="") -> Iterator[bytes]:
    """Synchroner Aufrufer: bei EOF/close wird der eigene Prozess beendet."""
    stream = _MediaProcess(cmd, owner=owner, label=label, source=source)
    try:
        while True:
            data = stream.proc.stdout.read(chunk)
            if not data:
                break
            yield data
    finally:
        stream.close()


async def async_stream_bytes(cmd: list[str], chunk: int = 65536, *, owner="player", label="", source=""):
    """HTTP-Disconnect beendet FFmpeg auch während eines blockierten Pipe-Reads.

    Einen synchronen Generator allein kann Starlette während read() nicht
    schließen. Hier erreicht Cancellation das finally sofort; das Aufräumen
    läuft abgeschirmt in einem Thread, der zuerst den Prozess beendet.
    """
    stream = _MediaProcess(cmd, owner=owner, label=label, source=source)
    try:
        while True:
            data = await asyncio.to_thread(stream.proc.stdout.read, chunk)
            if not data:
                break
            yield data
    finally:
        # shield schützt den Cleanup-Task auch vor der CancelScope des HTTP-
        # Streams. Der Thread existiert unabhängig vom geschlossenen Request.
        await asyncio.shield(asyncio.create_task(asyncio.to_thread(stream.close)))
