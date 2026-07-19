"""FFprobe-Abfragen und Mapping von Plattform/Codec auf FFmpeg-Flags."""
from __future__ import annotations

import functools
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("vcompress.ffprobe")

_ENCODER_LINE = re.compile(r"^\s*[VAS][.FSXBD]{5}\s+(\S+)")

# HDR-Transferfunktionen
HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "smptest2084"}


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    duration: float  # Sekunden
    codec: str
    pix_fmt: str
    color_transfer: str
    color_primaries: str
    color_space: str
    bit_rate: int  # bps (kann 0 sein)
    size_bytes: int
    # --- erweiterte Analyse (für die UI) ---
    profile: str = ""
    level: str = ""
    fps: float = 0.0
    bit_depth: int = 8
    hdr_type: str = "SDR"
    dolby_vision: bool = False
    dv_profile: int = 0
    overall_bitrate: int = 0
    container: str = ""
    audio: list = field(default_factory=list)
    subtitles: list = field(default_factory=list)

    @property
    def is_4k(self) -> bool:
        return self.height >= 1440 or self.width >= 2560

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.lower() in HDR_TRANSFERS or self.dolby_vision

    @property
    def video_bitrate(self) -> int:
        if self.bit_rate:
            return self.bit_rate
        # Ohne Stream-/Tag-Bitrate: aus der Gesamtbitrate die bekannten
        # Ton-Bitraten abziehen (sonst wäre Video ≈ Gesamt und der Ton-Anteil
        # würde doppelt zählen). Fällt auf Größe/Dauer zurück, wenn nötig.
        total = self.overall_bitrate
        if not total and self.duration > 0 and self.size_bytes > 0:
            total = int(self.size_bytes * 8 / self.duration)
        if total:
            aud = sum(int(a.get("bitrate") or 0) for a in (self.audio or []))
            est = total - aud
            return int(est) if est > 0 else int(total)
        return 0

    def to_dict(self) -> dict:
        megapixels = round(self.width * self.height / 1_000_000, 1) if self.width else 0
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 2),
            "codec": self.codec,
            "pix_fmt": self.pix_fmt,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "color_space": self.color_space,
            "is_4k": self.is_4k,
            "is_hdr": self.is_hdr,
            "bit_rate": self.bit_rate,
            "size_bytes": self.size_bytes,
            "size_human": human_size(self.size_bytes),
            "resolution": f"{self.width}x{self.height}",
            "megapixels": megapixels,
            "duration_human": human_duration(self.duration),
            # erweiterte Felder
            "profile": self.profile,
            "level": self.level,
            "fps": round(self.fps, 3) if self.fps else 0,
            "bit_depth": self.bit_depth,
            "hdr_type": self.hdr_type,
            "dolby_vision": self.dolby_vision,
            "dv_profile": self.dv_profile,
            "overall_bitrate": self.overall_bitrate,
            "overall_bitrate_human": _bitrate_human(self.overall_bitrate),
            "video_bitrate": self.video_bitrate,
            "video_bitrate_human": _bitrate_human(self.video_bitrate),
            "container": self.container,
            "audio": self.audio,
            "subtitles": self.subtitles,
        }


def probe_with_error(path: Path) -> tuple[Optional[VideoInfo], Optional[str]]:
    """Vollständige Stream-Analyse via ffprobe. Liefert (VideoInfo, Fehler)."""
    from . import config
    cmd = [
        config.FFPROBE, "-v", "error",
        "-show_streams", "-show_format",
        "-of", "json",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60, check=False)
    except FileNotFoundError:
        return None, "ffprobe nicht gefunden (PATH prüfen – /usr/local/bin)."
    except subprocess.TimeoutExpired:
        return None, "ffprobe-Timeout (Datei evtl. sehr groß oder Mount langsam)."
    except OSError as e:
        return None, f"ffprobe-Aufruf fehlgeschlagen: {e}"

    if out.returncode != 0:
        err = (out.stderr or "").strip() or f"Exit-Code {out.returncode}"
        return None, err
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe-Ausgabe nicht lesbar (kein gültiges JSON)."

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None, "Kein Video-Stream gefunden."

    duration = _f(video.get("duration")) or _f(fmt.get("duration")) or 0.0
    # Video-Bitrate robust bestimmen: ffprobe-bit_rate -> BPS-/Statistik-Tags
    # (viele MKVs liefern keine Stream-bit_rate, aber mkvmerge-Tags).
    bit_rate = _stream_bitrate(video, video.get("tags", {}) or {}, duration)
    overall_bitrate = int(_f(fmt.get("bit_rate")) or 0)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = int(_f(fmt.get("size")) or 0)
    if not overall_bitrate and duration > 0 and size_bytes > 0:
        overall_bitrate = int(size_bytes * 8 / duration)

    pix_fmt = video.get("pix_fmt", "?") or "?"
    transfer = (video.get("color_transfer", "") or "").lower()
    hdr_type, dovi, dv_profile = _detect_hdr(transfer, video)

    # Audio-/Untertitel-Spuren strukturiert sammeln
    audio = [_audio_entry(s, i, duration) for i, s in
             enumerate(s for s in streams if s.get("codec_type") == "audio")]
    subs = [_subtitle_entry(s, i) for i, s in
            enumerate(s for s in streams if s.get("codec_type") == "subtitle")]

    info = VideoInfo(
        path=str(path),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        duration=duration,
        codec=video.get("codec_name", "?"),
        pix_fmt=pix_fmt,
        color_transfer=video.get("color_transfer", "") or "",
        color_primaries=video.get("color_primaries", "") or "",
        color_space=video.get("color_space", "") or "",
        bit_rate=bit_rate,
        size_bytes=size_bytes,
        profile=str(video.get("profile", "") or ""),
        level=str(video.get("level", "") or ""),
        fps=_parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        bit_depth=_bit_depth(pix_fmt, video),
        hdr_type=hdr_type,
        dolby_vision=dovi,
        dv_profile=dv_profile,
        overall_bitrate=overall_bitrate,
        container=fmt.get("format_name", "") or "",
        audio=audio,
        subtitles=subs,
    )
    return info, None


def _detect_hdr(transfer: str, video: dict) -> tuple[str, bool, int]:
    """Bestimmt HDR-Typ, ob Dolby Vision vorliegt und ggf. das DV-Profil."""
    dovi = False
    dv_profile = 0
    for sd in video.get("side_data_list", []) or []:
        t = str(sd.get("side_data_type", "")).lower()
        if "dolby vision" in t or "dovi" in t:
            dovi = True
            # ffprobe liefert bei DV-Konfig-Records das Feld "dv_profile".
            raw = sd.get("dv_profile")
            if raw is not None:
                try:
                    dv_profile = int(raw)
                except (TypeError, ValueError):
                    pass
    if transfer in ("smpte2084", "smptest2084"):
        base = "HDR10 (PQ)"
    elif transfer == "arib-std-b67":
        base = "HLG"
    else:
        base = "SDR"
    if dovi:
        label = f"Dolby Vision {dv_profile}" if dv_profile else "Dolby Vision"
        return (f"{label} + {base}" if base != "SDR" else label), True, dv_profile
    return base, False, 0


def _bit_depth(pix_fmt: str, video: dict) -> int:
    raw = video.get("bits_per_raw_sample")
    if raw and str(raw).isdigit():
        return int(raw)
    if "12" in pix_fmt:
        return 12
    if "10" in pix_fmt:
        return 10
    return 8


def _tag_lookup(tags: dict, name: str):
    """Tag case-insensitiv holen – auch mit Sprach-Suffix (z. B. BPS-eng)."""
    up = name.upper()
    for k, v in (tags or {}).items():
        ku = str(k).upper()
        if ku == up or ku.startswith(up + "-"):
            return v
    return None


def _duration_tag_seconds(val) -> float:
    """MKV-DURATION-Tag ("HH:MM:SS.nnnnnnnnn") in Sekunden umrechnen."""
    if not val:
        return 0.0
    parts = str(val).split(":")
    if len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return 0.0
    return _f(val) or 0.0


def _stream_bitrate(s: dict, tags: dict, container_duration: float = 0.0) -> int:
    """Bitrate einer Spur robust bestimmen – wie MediaInfo.

    Reihenfolge: bit_rate (ffprobe) -> BPS-Tag (MKV/mkvmerge) ->
    NUMBER_OF_BYTES / DURATION (Statistik-Tags). So erscheint auch bei MKVs, die
    ffprobe keine bit_rate liefern, wieder eine Bitrate.
    """
    br = int(_f(s.get("bit_rate")) or 0)
    if br:
        return br
    bps = _f(_tag_lookup(tags, "BPS"))
    if bps:
        return int(bps)
    nbytes = _f(_tag_lookup(tags, "NUMBER_OF_BYTES"))
    dur = _duration_tag_seconds(_tag_lookup(tags, "DURATION")) or container_duration
    if nbytes and dur:
        return int(nbytes * 8 / dur)
    return 0


def _audio_entry(s: dict, index: int = 0, container_duration: float = 0.0) -> dict:
    tags = s.get("tags", {}) or {}
    disp = s.get("disposition", {}) or {}
    br = _stream_bitrate(s, tags, container_duration)
    return {
        "index": index,  # relativer Audio-Index (0:a:index)
        "codec": s.get("codec_name", "?"),
        "channels": s.get("channels", 0),
        "layout": s.get("channel_layout", "") or "",
        "sample_rate": s.get("sample_rate", "") or "",
        "bitrate": br,
        "bitrate_human": _bitrate_human(br) if br else "—",
        "language": tags.get("language", "") or tags.get("LANGUAGE", "") or "und",
        "title": tags.get("title", "") or tags.get("TITLE", "") or "",
        "forced": bool(disp.get("forced")),
        "default": bool(disp.get("default")),
    }


def _subtitle_entry(s: dict, index: int = 0) -> dict:
    tags = s.get("tags", {}) or {}
    disp = s.get("disposition", {}) or {}
    return {
        "index": index,  # relativer Untertitel-Index (0:s:index)
        "codec": s.get("codec_name", "?"),
        "language": tags.get("language", "") or tags.get("LANGUAGE", "") or "und",
        "title": tags.get("title", "") or tags.get("TITLE", "") or "",
        "forced": bool(disp.get("forced")),
        "default": bool(disp.get("default")),
    }


def _parse_fraction(value: Optional[str]) -> float:
    if not value or "/" not in str(value):
        return _f(value) or 0.0
    num, _, den = str(value).partition("/")
    n, d = _f(num), _f(den)
    if not n or not d:
        return 0.0
    return n / d


def _bitrate_human(bps: float) -> str:
    if not bps:
        return "—"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbit/s"
    return f"{bps / 1000:.0f} kbit/s"


def ffprobe(path: Path) -> Optional[VideoInfo]:
    """Liest Auflösung, Dauer, Codec und Farbinformationen via ffprobe aus."""
    info, err = probe_with_error(path)
    if err:
        logger.warning("ffprobe fehlgeschlagen für %s: %s", path, err)
    return info


def probe_streams(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Alle Ton-/Untertitel-Streams einer Datei listen – auch ohne Video-Stream.

    Für den Remux-Modus (externe Ton-/Untertiteldateien wie .eac3/.srt haben
    keinen Video-Stream und würden bei ``probe_with_error`` scheitern).
    Rückgabe: ({audio, subtitles, has_video, container}, fehler).
    """
    from . import config
    cmd = [config.FFPROBE, "-v", "error", "-show_streams", "-show_format",
           "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60, check=False)
    except (FileNotFoundError, OSError) as e:
        return None, f"ffprobe-Aufruf fehlgeschlagen: {e}"
    except subprocess.TimeoutExpired:
        return None, "ffprobe-Timeout."
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or f"Exit-Code {out.returncode}"
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe-Ausgabe nicht lesbar."

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    duration = _f(fmt.get("duration")) or 0.0
    audio = [_audio_entry(s, i, duration) for i, s in
             enumerate(s for s in streams if s.get("codec_type") == "audio")]
    subs = [_subtitle_entry(s, i) for i, s in
            enumerate(s for s in streams if s.get("codec_type") == "subtitle")]
    has_video = any(s.get("codec_type") == "video" for s in streams)
    return {"audio": audio, "subtitles": subs, "has_video": has_video,
            "container": fmt.get("format_name", "") or ""}, None


# ----------------------------------------------------------------- Encoder-Map

# Encoder-Name pro Plattform/Codec. Intel gibt es in zwei Ausprägungen:
# QSV (oneVPL) und VAAPI – beide laufen mit dem Image (Ubuntu 24.04, libva 2.20).
ENCODERS = {
    "nvidia": {"av1": "av1_nvenc", "hevc": "hevc_nvenc", "h264": "h264_nvenc"},
    "intel": {"av1": "av1_qsv", "hevc": "hevc_qsv", "h264": "h264_qsv"},
    "intel_vaapi": {"av1": "av1_vaapi", "hevc": "hevc_vaapi", "h264": "h264_vaapi"},
    "amd": {"av1": "av1_vaapi", "hevc": "hevc_vaapi", "h264": "h264_vaapi"},
    "cpu": {"av1": "libsvtav1", "hevc": "libx265", "h264": "libx264"},
}


def intel_uses_vaapi() -> bool:
    """True, wenn die Intel-Plattform über VAAPI (statt QSV) encodieren soll."""
    from . import config
    return getattr(config, "INTEL_ENCODER", "vaapi") != "qsv"


def _encoder_map(platform: str) -> dict:
    if platform == "intel" and intel_uses_vaapi():
        return ENCODERS["intel_vaapi"]
    return ENCODERS.get(platform, ENCODERS["cpu"])


def encoder_name(platform: str, codec: str) -> str:
    return _encoder_map(platform).get(codec, "libsvtav1")


def encoder_backend(platform: str) -> str:
    """Konkretes HW-Backend: 'nvenc' | 'qsv' | 'vaapi' | 'cpu'."""
    if platform == "nvidia":
        return "nvenc"
    if platform == "amd":
        return "vaapi"
    if platform == "intel":
        return "vaapi" if intel_uses_vaapi() else "qsv"
    return "cpu"


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    """Erste Zeile von `ffmpeg -version` (z. B. 'ffmpeg version n8.1 …')."""
    from . import config
    try:
        out = subprocess.run(
            [config.FFMPEG, "-hide_banner", "-version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
        first = (out.stdout or "").splitlines()
        return first[0].strip() if first else "unbekannt"
    except (OSError, subprocess.SubprocessError):
        return "unbekannt"


@functools.lru_cache(maxsize=1)
def available_encoders() -> frozenset:
    """Liest die im FFmpeg-Build kompilierten Encoder (`ffmpeg -encoders`)."""
    from . import config
    try:
        out = subprocess.run(
            [config.FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in out.stdout.splitlines():
        m = _ENCODER_LINE.match(line)
        if m:
            names.add(m.group(1))
    return frozenset(names)


def encoder_available(platform: str, codec: str) -> bool:
    enc = encoder_name(platform, codec)
    avail = available_encoders()
    # Wenn die Liste leer ist (ffmpeg nicht abfragbar), nicht fälschlich blocken.
    return not avail or enc in avail


def quality_args(platform: str, value: int) -> list[str]:
    """Liefert die herstellerspezifischen Qualitäts-Argumente.

    - NVENC: -cq
    - QSV:   -global_quality
    - VAAPI (AMD und Intel-VAAPI): -rc_mode CQP -qp
    - CPU:   -crf
    """
    backend = encoder_backend(platform)
    if backend == "nvenc":
        return ["-cq", str(value)]
    if backend == "qsv":
        return ["-global_quality", str(value)]
    if backend == "vaapi":
        return ["-rc_mode", "CQP", "-qp", str(value)]
    return ["-crf", str(value)]


def bitrate_args(platform: str, codec: str, kbps: int, abr: bool = False) -> list[str]:
    """Festbitrate (CBR) oder Average-Bitrate (VBR-Ziel) in kbit/s."""
    br = f"{kbps}k"
    enc = encoder_name(platform, codec)
    # SVT-AV1 unterstützt maxrate/bufsize nur im CRF-Modus (sonst „Max Bitrate
    # only supported with CRF mode"). -b:v allein ergibt ein VBR-Ziel – für ABR
    # und CBR gleichermaßen genutzt.
    if enc == "libsvtav1":
        return ["-b:v", br]
    if abr:
        args = ["-b:v", br, "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
        if "nvenc" in enc:
            args += ["-rc", "vbr"]
        return args
    args = ["-b:v", br]
    if "nvenc" in enc:
        args += ["-maxrate", br, "-bufsize", f"{kbps * 2}k", "-rc", "cbr"]
    return args


# Ziel-Audiocodec -> FFmpeg-Encoder
AUDIO_ENCODERS = {
    "aac": "aac",
    "opus": "libopus",
    "ac3": "ac3",
    "eac3": "eac3",
    "flac": "flac",
}


def audio_args(
    mode: str = "copy",
    codec: str = "aac",
    bitrate_kbps: int = 160,
    channels: int = 0,
    normalize: bool = False,
) -> list[str]:
    """Baut die Audio-Argumente.

    mode:      "copy" (Stream 1:1 übernehmen) | "encode" (neu codieren) | "none"
    codec:     Zielcodec bei mode="encode" (aac/opus/ac3/eac3/flac)
    bitrate:   kbit/s pro Stream (bei verlustbehafteten Codecs)
    channels:  0 = Original behalten, 1 = Mono, 2 = Stereo (Downmix)
    normalize: EBU-R128-Lautheitsnormalisierung (loudnorm)
    """
    if mode == "none":
        return ["-an"]
    # Direktes Kopieren nur möglich, wenn keine Umwandlung nötig ist.
    if mode == "copy" and channels == 0 and not normalize:
        return ["-c:a", "copy"]

    enc = AUDIO_ENCODERS.get(codec, "aac")
    args = ["-c:a", enc]
    if enc != "flac":
        args += ["-b:a", f"{max(32, int(bitrate_kbps))}k"]
    if channels in (1, 2):
        args += ["-ac", str(channels)]
    if normalize:
        # Zielwerte nach Streaming-Standard (EBU R128).
        args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    return args


def audio_track_args(tracks: list) -> list[str]:
    """Mapping + Codec-Argumente pro einzelner Ausgabe-Tonspur.

    `tracks`: geordnete Liste ausgewählter Spuren, je Eintrag ein Dict mit
    index (Quell-Audio-Index), mode ("copy"/"encode"), codec, bitrate,
    channels, normalize. Die Ausgabe-Spur-Reihenfolge bestimmt die
    Stream-Specifier (a:0, a:1, …).
    """
    if not tracks:
        return ["-an"]
    args: list[str] = []
    for t in tracks:
        args += ["-map", f"0:a:{int(t.get('index', 0))}?"]
    for out_idx, t in enumerate(tracks):
        mode = t.get("mode", "copy")
        ch = int(t.get("channels", 0) or 0)
        norm = bool(t.get("normalize"))
        if mode != "encode" and ch == 0 and not norm:
            args += [f"-c:a:{out_idx}", "copy"]
            continue
        enc = AUDIO_ENCODERS.get(t.get("codec", "aac"), "aac")
        args += [f"-c:a:{out_idx}", enc]
        if enc != "flac":
            args += [f"-b:a:{out_idx}", f"{max(32, int(t.get('bitrate', 160) or 160))}k"]
        if ch in (1, 2):
            args += [f"-ac:a:{out_idx}", str(ch)]
        if norm:
            args += [f"-filter:a:{out_idx}", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    return args


# MP4-Text-Untertitel (mov_text/tx3g) können nicht per copy in eine MKV, da
# Matroska mov_text nicht kennt. Sie werden nach SRT (subrip) umgewandelt.
_SUB_TEXT_MP4 = {"mov_text", "tx3g", "text"}
# Bild-Untertitel: in MP4 nicht möglich (nur in MKV per copy).
_SUB_IMAGE = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
              "dvb_subtitle", "dvbsub", "xsub"}


def _sub_out_codec(src_codec: str, container: str = "mkv") -> Optional[str]:
    """Ziel-Codec einer Untertitelspur je Container.

    MKV: mov_text -> srt, sonst copy (behält ASS/SRT/PGS/…).
    MP4: alles Textbasierte -> mov_text; Bild-Untertitel (PGS/VobSub) sind in
    MP4 nicht möglich -> None (Spur wird ausgelassen).
    """
    c = (src_codec or "").lower()
    if container == "mp4":
        if c in _SUB_IMAGE:
            return None
        return "mov_text"
    return "srt" if c in _SUB_TEXT_MP4 else "copy"


def _sub_codec_map(info) -> dict:
    """{Untertitel-Index: Quell-Codec} aus der Analyse (für Codec-Wahl)."""
    out: dict = {}
    for s in (getattr(info, "subtitles", []) or []):
        try:
            out[int(s.get("index", 0))] = s.get("codec", "") or ""
        except (TypeError, ValueError):
            continue
    return out


def subtitle_copy_args(info, input_idx: int = 0, container: str = "mkv") -> list[str]:
    """Alle Untertitel der Quelle übernehmen (container-abhängig).

    MKV: mov_text/tx3g -> SRT, Rest (ASS/SRT/PGS/VobSub) verlustfrei kopieren.
    MP4: Text -> mov_text; Bild-Untertitel (PGS/VobSub) werden ausgelassen.
    `input_idx` = Eingang, aus dem die Untertitel stammen (0 = Hauptinput,
    1 = Quelle beim Chunked-Mux).
    """
    subs = list(getattr(info, "subtitles", []) or [])
    if not subs:
        if container == "mp4":
            # Ohne Analyse in MP4 nicht blind mappen (Bild-Subs würden crashen).
            return []
        return ["-map", f"{input_idx}:s?", "-c:s", "copy"]
    maps: list[str] = []
    codecs: list[str] = []
    for sub in subs:
        oc = _sub_out_codec(sub.get("codec", ""), container)
        if oc is None:
            continue  # z. B. PGS in MP4 -> auslassen
        maps += ["-map", f"{input_idx}:s:{int(sub.get('index', 0))}?"]
        codecs.append(oc)
    args: list[str] = list(maps)
    for out_idx, oc in enumerate(codecs):
        args += [f"-c:s:{out_idx}", oc]
    return args


def subtitle_track_args(tracks: list, info=None, container: str = "mkv") -> list[str]:
    """Mapping + Codec + Disposition (Default/Forced) pro gewählter Spur.

    `tracks`: geordnete Liste ausgewählter Untertitel, je Eintrag ein Dict mit
    index (Quell-Untertitel-Index) und optional default/forced (bool). Der
    Ziel-Codec richtet sich nach Quelle UND Container (MKV: copy/srt, MP4:
    mov_text; Bild-Untertitel entfallen in MP4). Leere Liste => keine Untertitel.
    """
    if not tracks:
        return []
    codec_map = _sub_codec_map(info) if info is not None else {}
    kept: list[tuple[dict, str]] = []
    for t in tracks:
        oc = _sub_out_codec(codec_map.get(int(t.get("index", 0)), ""), container)
        if oc is None:
            continue  # Bild-Untertitel in MP4 -> auslassen
        kept.append((t, oc))
    if not kept:
        return []
    args: list[str] = []
    for t, _ in kept:
        args += ["-map", f"0:s:{int(t.get('index', 0))}?"]
    for out_idx, (_, oc) in enumerate(kept):
        args += [f"-c:s:{out_idx}", oc]
    for out_idx, (t, _) in enumerate(kept):
        flags = []
        if t.get("default"):
            flags.append("default")
        if t.get("forced"):
            flags.append("forced")
        # "0" setzt alle Dispositions-Flags zurück (kein Default/Forced).
        args += [f"-disposition:s:{out_idx}", "+".join(flags) if flags else "0"]
    return args


@functools.lru_cache(maxsize=1)
def _mkvpropedit() -> str:
    return shutil.which("mkvpropedit") or ""


def add_mkv_statistics_tags(path) -> bool:
    """Schreibt Bitraten-/Größen-Statistik-Tags pro Spur (BPS, NUMBER_OF_BYTES,
    DURATION) – wie mkvmerge. FFmpeg schreibt diese nicht, daher zeigen
    ffprobe/MediaInfo bei kopierten Tonspuren sonst keine Bitrate an.

    Nur für .mkv und nur wenn mkvpropedit vorhanden ist; ansonsten still
    übersprungen (kein Fehler). Läuft in-place ohne Remux (schnell).
    """
    p = Path(path)
    if p.suffix.lower() != ".mkv" or not p.exists():
        return False
    tool = _mkvpropedit()
    if not tool:
        return False
    try:
        res = subprocess.run(
            [tool, str(p), "--add-track-statistics-tags"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, check=False)
        if res.returncode != 0:
            logger.warning("mkvpropedit (Statistik-Tags) fehlgeschlagen (Exit %s): %s",
                           res.returncode, (res.stderr or "")[-300:])
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("mkvpropedit nicht ausführbar: %s", e)
        return False


def hwaccel_input_args(platform: str) -> list[str]:
    """Hardware-Decode-/Init-Argumente, die VOR dem -i Input stehen."""
    from . import config
    backend = encoder_backend(platform)
    if platform == "nvidia":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if backend == "qsv":
        return ["-hwaccel", "qsv", "-qsv_device", config.VAAPI_DEVICE]
    if backend == "vaapi":
        return ["-hwaccel", "vaapi", "-vaapi_device", config.VAAPI_DEVICE]
    return []


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _f(value) -> Optional[float]:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------- Integritäts-Check
def verify_playable(path: Path, expected_duration: float = 0.0) -> tuple[bool, str]:
    """Prüft, ob die Ausgabedatei sauber decodierbar ist und die Dauer passt.

    1) Voll-Decode (nur Fehler-Log, `-xerror` bricht beim ersten Fehler ab) –
       erkennt abgeschnittene/korrupte Streams.
    2) Dauer-Abgleich gegen die erwartete Länge (Toleranz max. 2 s bzw. 1 %).

    Gibt (ok, meldung) zurück. Bei ok=True ist die Meldung ein kurzer Status.
    """
    from . import config

    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False, "Ausgabedatei fehlt oder ist leer"

    # 1) Decodier-Test (Video + Audio), bricht beim ersten Fehler ab.
    try:
        res = subprocess.run(
            [config.FFMPEG, "-v", "error", "-xerror",
             "-i", str(path), "-map", "0:v:0?", "-map", "0:a?",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Decoder-Test nicht ausführbar: {e}"
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        return False, f"Decodier-Fehler: {tail[-1] if tail else 'unbekannt'}"

    # 2) Dauer prüfen (nur wenn eine Erwartung vorliegt).
    if expected_duration and expected_duration > 0:
        try:
            pr = subprocess.run(
                [config.FFPROBE, "-v", "error", "-show_entries",
                 "format=duration", "-of", "default=nk=1:nw=1", str(path)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
            )
            out_dur = _f((pr.stdout or "").strip()) or 0.0
        except (OSError, subprocess.SubprocessError):
            out_dur = 0.0
        if out_dur > 0:
            tol = max(2.0, expected_duration * 0.01)
            if abs(out_dur - expected_duration) > tol:
                return (False,
                        f"Dauer weicht ab: {human_duration(out_dur)} statt "
                        f"{human_duration(expected_duration)}")
    return True, "ok"


# ------------------------------------------------------------------- Auto-Crop
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def detect_crop(info: "VideoInfo", samples: int = 3, probe_seconds: int = 2) -> str:
    """Ermittelt via ffmpeg `cropdetect` schwarze Ränder und liefert "w:h:x:y".

    Es werden mehrere Positionen (20/50/80 %) kurz analysiert und der häufigste
    Crop-Vorschlag gewählt. Rückgabe "" wenn kein nennenswerter Crop (< 2 %) oder
    ungültig. So bleibt die volle Fläche erhalten, wenn keine Balken existieren.
    """
    from . import config

    dur = info.duration or 0.0
    if dur <= 0 or not info.width or not info.height:
        return ""
    fracs = [0.2, 0.5, 0.8][:max(1, min(3, samples))]
    counts: dict[str, int] = {}
    for frac in fracs:
        start = max(0.0, dur * frac)
        try:
            res = subprocess.run(
                [config.FFMPEG, "-hide_banner", "-ss", str(start),
                 "-i", str(info.path), "-t", str(probe_seconds),
                 "-vf", "cropdetect=round=2:reset=0", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for m in _CROP_RE.finditer(res.stderr or ""):
            counts[m.group(0)] = counts.get(m.group(0), 0) + 1
    if not counts:
        return ""
    best = max(counts, key=lambda k: counts[k])
    m = _CROP_RE.search(best)
    if not m:
        return ""
    w, h, x, y = (int(m.group(i)) for i in range(1, 5))
    if w <= 0 or h <= 0 or w > info.width or h > info.height:
        return ""
    if w + x > info.width or h + y > info.height:
        return ""
    # Zu kleiner Crop (< 2 % je Achse) → ignorieren (Rauschen/Rundung).
    if (info.height - h) < info.height * 0.02 and (info.width - w) < info.width * 0.02:
        return ""
    return f"{w}:{h}:{x}:{y}"


def crop_dims(crop: str) -> Optional[tuple[int, int]]:
    """(Breite, Höhe) aus einem "w:h:x:y"-Crop-String oder None."""
    if not crop:
        return None
    parts = crop.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None
