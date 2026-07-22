"""Remux-/Bearbeiten-Modus: Container umbauen OHNE Video-Re-Encode.

Erlaubt das gezielte Entfernen von Ton-/Untertitelspuren, das Ändern von
Default-/Forced-Flags sowie Sprache/Titel und das Hinzufügen externer Ton-/
Untertitelspuren (zweites Input-File, optional mit Delay). Das Video wird immer
1:1 kopiert (``-c:v copy``) – es findet also kein Neu-Encoding statt.

Nur bei Container-Konflikten (z. B. TrueHD/DTS in MP4) wird die betroffene Spur
gezielt transkodiert; alles andere bleibt verlustfreier Stream-Copy.
"""
from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff
from .ffmpeg_utils import VideoInfo

logger = logging.getLogger("vcompress.remux")

# Elementarstrom-Endungen je Codec beim Extrahieren (Fallback: Matroska-Hülle,
# die mit -c copy immer funktioniert: .mka für Ton, .mks für Untertitel).
_AUDIO_EXT = {
    "aac": ".aac", "ac3": ".ac3", "eac3": ".eac3", "mp3": ".mp3",
    "opus": ".opus", "flac": ".flac", "truehd": ".thd", "mlp": ".mlp",
    "dts": ".dts", "alac": ".m4a", "vorbis": ".ogg",
}
_SUB_EXT = {
    "subrip": ".srt", "srt": ".srt", "ass": ".ass", "ssa": ".ssa",
    "webvtt": ".vtt", "mov_text": ".srt",
    "hdmv_pgs_subtitle": ".sup", "pgssub": ".sup",
    "dvd_subtitle": ".sub",
}
_FONT_MIME = {
    ".ttf": "application/x-truetype-font", ".ttc": "application/x-truetype-font",
    ".otf": "application/vnd.ms-opentype", ".pfb": "application/x-font-type1",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp",
}

# Audio-Codecs, die sich in MP4 verlustfrei kopieren lassen. Alles andere
# (TrueHD, DTS(-HD), FLAC, PCM, MLP, Vorbis …) muss für MP4 transkodiert werden.
_MP4_AUDIO_COPY = {"aac", "ac3", "eac3", "mp3", "opus", "alac", "ac4"}
# Text-Untertitel, die als mov_text nach MP4 wandern können. Bild-Untertitel
# (PGS/VobSub/DVB) sind in MP4 nicht möglich.
_TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
_IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub", "pgs"}


def external_kind(path: str) -> str:
    """'audio' | 'subtitle' | 'video' anhand der Dateiendung bestimmen."""
    suf = Path(path).suffix.lower()
    if suf in config.SUBTITLE_EXTENSIONS:
        return "subtitle"
    if suf in config.AUDIO_EXTENSIONS:
        return "audio"
    if suf in config.VIDEO_EXTENSIONS:
        return "video"
    return "audio"


def _codec_ok_in_container(codec: str, container: str, kind: str) -> bool:
    codec = (codec or "").lower()
    if container != "mp4":
        return True  # MKV kann praktisch alles
    if kind == "audio":
        return codec in _MP4_AUDIO_COPY
    if kind == "subtitle":
        return codec in _TEXT_SUBS  # nur Text (→ mov_text), keine Bild-Subs
    return True


def check_conflicts(info: VideoInfo, spec: dict) -> list[str]:
    """Container-Konflikte der aktuellen Auswahl auflisten (menschenlesbar).

    Ein Konflikt liegt vor, wenn eine zu KOPIERENDE Spur im Zielcontainer nicht
    erlaubt ist und der Nutzer sie nicht ohnehin transkodiert. Bild-Untertitel in
    MP4 sind grundsätzlich nicht möglich.
    """
    container = (spec.get("container") or "mkv").lower()
    if container != "mp4":
        return []
    out: list[str] = []
    audio_src = {int(a.get("index", 0)): a for a in (info.audio or [])}
    sub_src = {int(s.get("index", 0)): s for s in (info.subtitles or [])}

    for a in spec.get("audio", []) or []:
        if not a.get("keep"):
            continue
        if a.get("transcode"):
            continue
        src = audio_src.get(int(a.get("index", 0)))
        codec = (src or {}).get("codec", "")
        if not _codec_ok_in_container(codec, "mp4", "audio"):
            out.append(f"Tonspur #{a.get('index')} ({codec}) ist in MP4 nicht "
                       f"kopierbar – MKV wählen oder Spur transkodieren.")

    for s in spec.get("subtitles", []) or []:
        if not s.get("keep"):
            continue
        src = sub_src.get(int(s.get("index", 0)))
        codec = (src or {}).get("codec", "")
        if codec.lower() in _IMAGE_SUBS:
            out.append(f"Untertitel #{s.get('index')} ({codec}) ist ein "
                       f"Bild-Untertitel und in MP4 nicht möglich (MKV wählen).")

    for e in spec.get("external", []) or []:
        kind = e.get("type") or external_kind(e.get("path", ""))
        if kind == "subtitle" and Path(e.get("path", "")).suffix.lower() in {
                ".sup", ".pgs", ".idx", ".sub"}:
            name = Path(e.get("path", "")).name
            out.append(f"Externer Bild-Untertitel „{name}“ ist in MP4 nicht "
                       f"möglich (MKV wählen).")
    return out


def _abs_external(path: str) -> Optional[Path]:
    """Externe Datei sicher auflösen.

    ``upload:<name>`` verweist auf eine vom Nutzer hochgeladene Datei im
    Upload-Ordner; sonst wird root-aware innerhalb eines Input-Roots aufgelöst.
    """
    p = str(path or "")
    if p.startswith("upload:"):
        name = p[len("upload:"):].strip().replace("\\", "/")
        # Nur Dateiname zulassen (kein Pfad-Traversal).
        if "/" in name or name in ("", ".", ".."):
            return None
        base = config.UPLOAD_DIR.resolve()
        target = (base / name).resolve()
        if not config._within(target, base):
            return None
        return target if target.is_file() else None
    target = config.resolve_input(p)
    return target if (target and target.is_file()) else None


def _disp(kind: str, out_idx: int, entry: dict) -> list[str]:
    """`-disposition:<kind>:<idx>`-Flags aus default/forced ableiten."""
    flags = []
    if entry.get("default"):
        flags.append("default")
    if entry.get("forced"):
        flags.append("forced")
    value = "+".join(flags) if flags else "0"
    return [f"-disposition:{kind}:{out_idx}", value]


def _meta(kind: str, out_idx: int, entry: dict) -> list[str]:
    """Sprache/Titel als Stream-Metadaten setzen (nur wenn angegeben)."""
    args: list[str] = []
    lang = (entry.get("language") or "").strip()
    title = (entry.get("title") or "").strip()
    if lang and lang.lower() != "und":
        args += [f"-metadata:s:{kind}:{out_idx}", f"language={lang}"]
    if title:
        args += [f"-metadata:s:{kind}:{out_idx}", f"title={title}"]
    return args


def _mimetype(path: str) -> str:
    return _FONT_MIME.get(Path(path).suffix.lower(), "application/octet-stream")


def _attach_filename_meta(path: Path, att_out: int) -> list[str]:
    """filename= + mimetype für Player-Font-Namen."""
    return [
        f"-metadata:s:t:{att_out}", f"mimetype={_mimetype(str(path))}",
        f"-metadata:s:t:{att_out}", f"filename={path.name}",
    ]


def _count_attachments(path: Path) -> int:
    """Zahl der Attachment-Streams einer Datei (für korrekte t:-Indizes)."""
    try:
        out = subprocess.run(
            [config.FFPROBE, "-v", "error", "-select_streams", "t",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        return len([l for l in (out.stdout or "").splitlines() if l.strip()])
    except (OSError, subprocess.TimeoutExpired):
        return 0


def write_chapter_meta(chapters: list, work_dir: Path) -> Optional[Path]:
    """FFMETADATA-Datei aus Kapiteln (Start/Ende in Sekunden + Titel) schreiben."""
    if not chapters:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    path = work_dir / f"chapters_{uuid.uuid4().hex[:8]}.txt"
    lines = [";FFMETADATA1"]
    for ch in chapters:
        try:
            start = int(round(float(ch.get("start") or 0) * 1000))
            end = int(round(float(ch.get("end") or 0) * 1000))
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + 1
        title = str(ch.get("title") or "").replace("\n", " ").replace("=", "\\=")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={start}", f"END={end}", f"title={title}"]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def probe_chapters(path: Path) -> list:
    """Kapitel einer Datei via ffprobe lesen (Start/Ende in Sekunden + Titel)."""
    import json
    try:
        out = subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_chapters", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    chapters = []
    for c in data.get("chapters", []) or []:
        try:
            start = float(c.get("start_time") or 0)
            end = float(c.get("end_time") or 0)
        except (TypeError, ValueError):
            continue
        title = (c.get("tags", {}) or {}).get("title", "") or ""
        chapters.append({"start": round(start, 3), "end": round(end, 3), "title": title})
    return chapters


def build_edit_cmd(info: VideoInfo, output: Path, spec: dict) -> tuple[list[str], str]:
    """Erzeugt das FFmpeg-Kommando für den Remux-/Bearbeiten-Job.

    Rückgabe: (cmd, fehler). Bei fehler != "" darf der Job nicht laufen.
    """
    container = (spec.get("container") or "mkv").lower()
    is_mp4 = container == "mp4"

    # --- Inputs: Quelle + externe Dateien (mit optionalem Delay) ---------------
    # Mehrere Spuren aus derselben Datei (gleicher Delay) teilen sich EINEN Input.
    inputs: list[str] = ["-i", str(info.path)]
    externals: list[dict] = []
    input_map: dict[tuple[str, float], int] = {}
    for e in spec.get("external", []) or []:
        if e.get("keep") is False:
            continue
        target = _abs_external(e.get("path", ""))
        if target is None:
            return [], f"Externe Datei nicht gefunden: {e.get('path','?')}"
        delay = float(e.get("delay") or 0.0)
        key = (str(target), delay)
        idx = input_map.get(key)
        if idx is None:
            if delay:
                inputs += ["-itsoffset", str(delay)]
            inputs += ["-i", str(target)]
            idx = 1 + len(input_map)  # Input-Index (0 = Quelle)
            input_map[key] = idx
        e = dict(e)
        e["_input"] = idx
        e["_kind"] = e.get("type") or external_kind(e.get("path", ""))
        e["_stream"] = int(e.get("stream", 0) or 0)  # relativer Stream in der Datei
        externals.append(e)

    # Kapitel-Quelle: eigene ffmetadata (umbenannt) oder externe Kapiteldatei.
    chapter_input: Optional[int] = None
    chap_file = spec.get("chapters_file")
    chap_list = spec.get("chapters")
    if chap_file:
        target = _abs_external(chap_file)
        if target is None:
            return [], f"Kapiteldatei nicht gefunden: {chap_file}"
        inputs += ["-i", str(target)]
        chapter_input = 1 + len(input_map)
        input_map[("__chapters__", 0.0)] = chapter_input
    elif chap_list:
        meta = write_chapter_meta(chap_list, config.WORK_DIR)
        if meta is not None:
            inputs += ["-i", str(meta)]
            chapter_input = 1 + len(input_map)
            input_map[("__chapters__", 0.0)] = chapter_input

    cmd = [config.FFMPEG, "-y", "-hide_banner"] + inputs

    # --- Video immer 1:1 kopieren ---------------------------------------------
    cmd += ["-map", "0:v", "-c:v", "copy"]

    audio_src = {int(a.get("index", 0)): a for a in (info.audio or [])}

    def _ext_key(entry: dict) -> tuple:
        return (str(entry.get("path") or ""), int(entry.get("stream", 0) or 0))

    def _is_ext_entry(entry: dict) -> bool:
        return bool(entry.get("external") or entry.get("path"))

    def _map_audio_internal(a: dict, out_idx: int) -> list[str]:
        idx = int(a.get("index", 0))
        src = audio_src.get(idx, {})
        codec = (src.get("codec") or "").lower()
        transcode = bool(a.get("transcode"))
        if is_mp4 and not transcode and not _codec_ok_in_container(codec, "mp4", "audio"):
            transcode = True
        args = ["-map", f"0:a:{idx}?"]
        if transcode:
            tgt = (a.get("codec") or ("aac" if is_mp4 else "eac3")).lower()
            enc = ff.AUDIO_ENCODERS.get(tgt, "aac" if is_mp4 else "eac3")
            args += [f"-c:a:{out_idx}", enc]
            if enc != "flac":
                br = int(a.get("bitrate") or 0) or 640
                args += [f"-b:a:{out_idx}", f"{max(32, br)}k"]
            ch = int(a.get("channels") or 0)
            if ch in (1, 2, 6, 8):
                args += [f"-ac:a:{out_idx}", str(ch)]
        else:
            args += [f"-c:a:{out_idx}", "copy"]
        args += _disp("a", out_idx, a)
        args += _meta("a", out_idx, a)
        return args

    def _map_audio_external(e: dict, out_idx: int) -> list[str]:
        transcode = bool(e.get("transcode"))
        if is_mp4 and not transcode and not _codec_ok_in_container(
                (e.get("src_codec") or "").lower(), "mp4", "audio"):
            transcode = True
        args = ["-map", f"{e['_input']}:a:{e['_stream']}?"]
        if transcode:
            tgt = (e.get("codec") or ("aac" if is_mp4 else "eac3")).lower()
            enc = ff.AUDIO_ENCODERS.get(tgt, "aac" if is_mp4 else "eac3")
            args += [f"-c:a:{out_idx}", enc]
            if enc != "flac":
                br = int(e.get("bitrate") or 0) or 640
                args += [f"-b:a:{out_idx}", f"{max(32, br)}k"]
            ch = int(e.get("channels") or 0)
            if ch in (1, 2, 6, 8):
                args += [f"-ac:a:{out_idx}", str(ch)]
        else:
            args += [f"-c:a:{out_idx}", "copy"]
        args += _disp("a", out_idx, e)
        args += _meta("a", out_idx, e)
        return args

    # --- Tonspuren in Spec-Reihenfolge (intern + extern gemischt) --------------
    ext_audio = { _ext_key(e): e for e in externals if e["_kind"] == "audio" }
    audio_entries = list(spec.get("audio", []) or [])
    used_ext_audio: set[tuple] = set()
    out_a = 0
    for a in audio_entries:
        if a.get("keep") is False:
            continue
        if _is_ext_entry(a):
            key = _ext_key(a)
            e = ext_audio.get(key)
            if e is None or e.get("keep") is False:
                continue
            merged = {**e}
            for k in ("transcode", "codec", "bitrate", "channels",
                      "default", "forced", "language", "title"):
                if k in a:
                    merged[k] = a[k]
            cmd += _map_audio_external(merged, out_a)
            used_ext_audio.add(key)
            out_a += 1
        else:
            cmd += _map_audio_internal(a, out_a)
            out_a += 1

    # Legacy: externe Tonspuren, die nicht in audio[] stehen, ans Ende.
    for e in externals:
        if e["_kind"] != "audio" or e.get("keep") is False:
            continue
        key = _ext_key(e)
        if key in used_ext_audio:
            continue
        cmd += _map_audio_external(e, out_a)
        out_a += 1

    if out_a == 0:
        # Ohne Tonspur ist ein Film selten gewollt – erlauben wir zwar, aber die
        # UI warnt. Hier kein harter Fehler.
        pass

    # --- Untertitel in Spec-Reihenfolge (intern + extern gemischt) -------------
    sub_src = {int(s.get("index", 0)): s for s in (info.subtitles or [])}
    out_s = 0
    sub_codec = "mov_text" if is_mp4 else "copy"
    ext_subs = { _ext_key(e): e for e in externals if e["_kind"] == "subtitle" }
    sub_entries = list(spec.get("subtitles", []) or [])
    used_ext_subs: set[tuple] = set()
    for s in sub_entries:
        if s.get("keep") is False:
            continue
        if _is_ext_entry(s):
            key = _ext_key(s)
            e = ext_subs.get(key)
            if e is None or e.get("keep") is False:
                continue
            meta = {**e, **{k: s[k] for k in ("default", "forced", "language", "title")
                            if k in s}}
            cmd += ["-map", f"{e['_input']}:s:{e['_stream']}?", f"-c:s:{out_s}", sub_codec]
            cmd += _disp("s", out_s, meta)
            cmd += _meta("s", out_s, meta)
            used_ext_subs.add(key)
            out_s += 1
        else:
            if not s.get("keep", True):
                continue
            idx = int(s.get("index", 0))
            src = sub_src.get(idx, {})
            codec = (src.get("codec") or "").lower()
            if is_mp4 and codec in _IMAGE_SUBS:
                continue  # Bild-Untertitel in MP4 nicht möglich → auslassen
            cmd += ["-map", f"0:s:{idx}?", f"-c:s:{out_s}", sub_codec]
            cmd += _disp("s", out_s, s)
            cmd += _meta("s", out_s, s)
            out_s += 1

    for e in externals:
        if e["_kind"] != "subtitle" or e.get("keep") is False:
            continue
        key = _ext_key(e)
        if key in used_ext_subs:
            continue
        cmd += ["-map", f"{e['_input']}:s:{e['_stream']}?", f"-c:s:{out_s}", sub_codec]
        cmd += _disp("s", out_s, e)
        cmd += _meta("s", out_s, e)
        out_s += 1

    # --- Attachments (nur MKV) -------------------------------------------------
    # Bestehende behalten (optional) und/oder externe Fonts/Cover hinzufügen.
    att_out = 0
    if not is_mp4:
        keep_att = spec.get("keep_attachments", True)
        if keep_att:
            cmd += ["-map", "0:t?", "-c:t", "copy"]
            att_out += _count_attachments(Path(info.path))
        for add in spec.get("add_attachments", []) or []:
            target = _abs_external(add.get("path") if isinstance(add, dict) else add)
            if target is None:
                return [], f"Attachment nicht gefunden: {add}"
            cmd += ["-attach", str(target)] + _attach_filename_meta(target, att_out)
            att_out += 1

    # --- Kapitel ---------------------------------------------------------------
    if chapter_input is not None:
        cmd += ["-map_chapters", str(chapter_input)]
    elif spec.get("keep_chapters", True):
        cmd += ["-map_chapters", "0"]
    else:
        cmd += ["-map_chapters", "-1"]

    # --- Globale Metadaten -----------------------------------------------------
    if spec.get("keep_metadata", True):
        cmd += ["-map_metadata", "0"]
    else:
        cmd += ["-map_metadata", "-1"]

    # --- Verlustfreies Trimmen (Start/Ende in Sekunden, Keyframe-genau) --------
    trim = spec.get("trim") or {}
    try:
        t_start = float(trim.get("start") or 0)
        t_end = float(trim.get("end") or 0)
    except (TypeError, ValueError):
        t_start, t_end = 0.0, 0.0
    if t_start > 0:
        cmd += ["-ss", f"{t_start:.3f}"]
    if t_end > t_start:
        cmd += ["-to", f"{t_end:.3f}"]

    cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    return cmd, ""


# --------------------------------------------------------- Extract / Merge / Split
def build_extract_cmds(info: VideoInfo, out_dir: Path, tracks: list) -> list:
    """Je gewählter Spur ein Extract-Kommando (Stream-Copy).

    Rückgabe: Liste von (cmd, out_path). Endung richtet sich nach Codec, mit
    Matroska-Fallback (.mka/.mks), der mit -c copy immer funktioniert.
    """
    audio_src = {int(a.get("index", 0)): a for a in (info.audio or [])}
    sub_src = {int(s.get("index", 0)): s for s in (info.subtitles or [])}
    stem = Path(info.path).stem
    cmds: list = []
    for t in tracks or []:
        kind = t.get("type")
        idx = int(t.get("index", 0))
        if kind == "audio":
            codec = (audio_src.get(idx, {}).get("codec") or "").lower()
            ext = _AUDIO_EXT.get(codec, ".mka")
            sel = f"0:a:{idx}"
            lang = audio_src.get(idx, {}).get("language", "und")
        elif kind == "subtitle":
            codec = (sub_src.get(idx, {}).get("codec") or "").lower()
            ext = _SUB_EXT.get(codec, ".mks")
            sel = f"0:s:{idx}"
            lang = sub_src.get(idx, {}).get("language", "und")
        else:
            continue
        out_path = out_dir / f"{stem}.{kind}{idx}.{lang}{ext}"
        cmd = [config.FFMPEG, "-y", "-hide_banner", "-i", str(info.path),
               "-map", sel, "-c", "copy", str(out_path)]
        cmds.append((cmd, out_path))
    return cmds


def probe_duration(path: Path) -> float:
    """Dauer einer Datei in Sekunden (ffprobe, 0 bei Fehler)."""
    try:
        out = subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        return float((out.stdout or "0").strip() or 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0.0


def concat_compat(files: list) -> dict:
    """Codec/Auflösung/Pixelformat/Audio-Layout je Datei ermitteln und auf
    Kompatibilität für verlustfreies Concat prüfen."""
    import json
    entries = []
    for f in files:
        info = {"file": Path(f).name, "vcodec": "", "width": 0, "height": 0,
                "pix_fmt": "", "acodec": "", "channels": 0, "error": ""}
        try:
            out = subprocess.run(
                [config.FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(f)],
                capture_output=True, text=True, timeout=30, check=False)
            data = json.loads(out.stdout or "{}")
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and not info["vcodec"]:
                    info["vcodec"] = s.get("codec_name", "")
                    info["width"] = int(s.get("width") or 0)
                    info["height"] = int(s.get("height") or 0)
                    info["pix_fmt"] = s.get("pix_fmt", "")
                elif s.get("codec_type") == "audio" and not info["acodec"]:
                    info["acodec"] = s.get("codec_name", "")
                    info["channels"] = int(s.get("channels") or 0)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            info["error"] = str(e)
        entries.append(info)
    warnings = []
    if entries:
        ref = entries[0]
        for e in entries[1:]:
            diffs = []
            if e["vcodec"] != ref["vcodec"]:
                diffs.append(f"Video-Codec ({e['vcodec']} ≠ {ref['vcodec']})")
            if (e["width"], e["height"]) != (ref["width"], ref["height"]):
                diffs.append(f"Auflösung ({e['width']}x{e['height']} ≠ {ref['width']}x{ref['height']})")
            if e["pix_fmt"] != ref["pix_fmt"]:
                diffs.append(f"Pixelformat ({e['pix_fmt']} ≠ {ref['pix_fmt']})")
            if e["acodec"] != ref["acodec"]:
                diffs.append(f"Audio-Codec ({e['acodec']} ≠ {ref['acodec']})")
            if diffs:
                warnings.append(f"{e['file']}: " + ", ".join(diffs))
    return {"streams": entries, "warnings": warnings,
            "compatible": not warnings}


def build_concat_cmd(files: list, output: Path, work_dir: Path,
                     add_chapters: bool = False) -> tuple[list, str]:
    """Mehrere Dateien verlustfrei aneinanderhängen (concat-Demuxer, -c copy).

    Voraussetzung: identische Codecs/Parameter (sonst schlägt der Mux fehl).
    `add_chapters=True` setzt an jeder Verbindungsstelle eine Kapitelmarke
    (Titel = Dateiname).
    """
    if len(files) < 2:
        return [], "Zum Zusammenführen mindestens zwei Dateien wählen."
    work_dir.mkdir(parents=True, exist_ok=True)
    listfile = work_dir / f"concat_{uuid.uuid4().hex[:8]}.txt"
    lines = []
    for f in files:
        p = str(Path(f)).replace("'", "'\\''")
        lines.append(f"file '{p}'")
    try:
        listfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return [], f"Konnte Liste nicht schreiben: {e}"

    meta_path = None
    if add_chapters:
        chapters, acc = [], 0.0
        for f in files:
            dur = probe_duration(Path(f))
            chapters.append({"start": acc, "end": acc + max(dur, 0.001),
                             "title": Path(f).stem})
            acc += dur
        meta_path = write_chapter_meta(chapters, work_dir)

    cmd = [config.FFMPEG, "-y", "-hide_banner", "-f", "concat", "-safe", "0",
           "-i", str(listfile)]
    if meta_path:
        cmd += ["-i", str(meta_path), "-map_chapters", "1"]
    cmd += ["-map", "0", "-c", "copy",
            "-progress", "pipe:1", "-nostats", str(output)]
    return cmd, ""


def build_concat_reencode_cmd(files: list, output: Path, platform: str,
                              codec: str, cq: int) -> tuple[list, str]:
    """Inkompatible Dateien via concat-Filter vereinheitlichen (mit Re-Encode).

    Nutzt je Datei die erste Video- und erste Tonspur. Bild-/Attachment-Spuren
    und weitere Tonspuren gehen dabei verloren (bewusste Vereinfachung).
    """
    if len(files) < 2:
        return [], "Zum Zusammenführen mindestens zwei Dateien wählen."
    enc = ff.encoder_name(platform, codec)
    backend = ff.encoder_backend(platform)
    n = len(files)
    cmd = [config.FFMPEG, "-y", "-hide_banner"]
    if backend == "vaapi":
        cmd += ["-vaapi_device", "/dev/dri/renderD128"]
    for f in files:
        cmd += ["-i", str(f)]
    inputs = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    filt = f"{inputs}concat=n={n}:v=1:a=1[vc][a]"
    if backend == "vaapi":
        filt += ";[vc]format=nv12,hwupload[v]"
    else:
        filt += ";[vc]setsar=1[v]"
    cmd += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]", "-c:v", enc]
    cq = int(cq or 30)
    if backend == "nvenc":
        cmd += ["-rc", "vbr", "-cq", str(cq), "-preset", "p5"]
    elif backend == "qsv":
        cmd += ["-global_quality", str(cq)]
    elif backend == "vaapi":
        cmd += ["-rc_mode", "CQP", "-qp", str(cq)]
    else:
        cmd += ["-crf", str(cq)]
        if enc == "libsvtav1":
            cmd += ["-preset", "6"]
    cmd += ["-c:a", "aac", "-b:a", "384k",
            "-progress", "pipe:1", "-nostats", str(output)]
    return cmd, ""


def parse_time(val) -> float:
    """Zeitangabe (Sekunden-Zahl oder HH:MM:SS[.ms]) → Sekunden (float)."""
    if val is None:
        return 0.0
    s = str(val).strip()
    if not s:
        return 0.0
    try:
        if ":" in s:
            parts = [float(p) for p in s.split(":")]
            secs = 0.0
            for p in parts:
                secs = secs * 60 + p
            return secs
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def build_split_cmd(info: VideoInfo, out_pattern: Path, mode: str,
                    value=None, times=None) -> tuple[list, str]:
    """Verlustfreies Splitten am Segment-Muxer (-c copy).

    modi:
      chapters – an Kapitelgrenzen
      duration – alle `value` Sekunden
      parts    – in `value` gleich große Teile
      times    – an den Zeitmarken in `times` (Sekunden oder HH:MM:SS)
      size     – nach max. `value` MB je Teil (nur ca.: Keyframe-Grenzen)
    `out_pattern` muss ein %d-Muster enthalten (z. B. film_%03d.mkv).
    """
    seg = [config.FFMPEG, "-y", "-hide_banner", "-i", str(info.path),
           "-map", "0", "-c", "copy", "-f", "segment", "-reset_timestamps", "1"]
    dur = float(getattr(info, "duration", 0) or 0)
    if mode == "chapters":
        chaps = probe_chapters(Path(info.path))
        pts = [f"{c['start']:.3f}" for c in chaps if c["start"] > 0]
        if not pts:
            return [], "Keine Kapitel zum Splitten gefunden."
        seg += ["-segment_times", ",".join(pts)]
    elif mode == "parts":
        try:
            n = int(float(value or 0))
        except (TypeError, ValueError):
            n = 0
        if n < 2:
            return [], "Anzahl Teile muss mindestens 2 sein."
        if dur <= 0:
            return [], "Dauer unbekannt – Splitten in N Teile nicht möglich."
        step = dur / n
        pts = [f"{step * k:.3f}" for k in range(1, n)]
        seg += ["-segment_times", ",".join(pts)]
    elif mode == "times":
        pts = sorted({round(parse_time(t), 3) for t in (times or []) if parse_time(t) > 0})
        pts = [p for p in pts if dur <= 0 or p < dur]
        if not pts:
            return [], "Keine gültigen Zeitmarken angegeben."
        seg += ["-segment_times", ",".join(f"{p:.3f}" for p in pts)]
    elif mode == "size":
        try:
            mb = float(value or 0)
        except (TypeError, ValueError):
            mb = 0
        if mb <= 0:
            return [], "Ungültige Zielgröße."
        br = getattr(info, "overall_bitrate", 0) or (
            int(info.size_bytes * 8 / dur) if dur > 0 and info.size_bytes > 0 else 0)
        if not br or br <= 0:
            return [], "Bitrate unbekannt – Splitten nach Größe nicht möglich."
        secs = max(1, int(mb * 1024 * 1024 * 8 / br))
        seg += ["-segment_time", str(secs)]
    else:  # duration
        try:
            secs = max(1, int(float(value or 0)))
        except (TypeError, ValueError):
            return [], "Ungültige Segmentlänge."
        seg += ["-segment_time", str(secs)]
    seg += ["-progress", "pipe:1", "-nostats", str(out_pattern)]
    return seg, ""


def build_cut_cmds(info: VideoInfo, out_dir: Path, ranges: list,
                   ext: str = ".mkv") -> list:
    """Ausschnitt(e) verlustfrei herausschneiden (ein Output je Bereich).

    ranges: Liste von {start, end, title?} in Sekunden. Rückgabe: Liste von
    (cmd, out_path). Nutzt Output-seitiges Seeking (-ss/-to nach dem Input),
    damit -to als absolute Quell-Zeit gilt.
    """
    stem = Path(info.path).stem
    cmds: list = []
    for i, r in enumerate(ranges or [], start=1):
        start = parse_time(r.get("start"))
        end = parse_time(r.get("end"))
        if end <= start:
            continue
        title = str(r.get("title") or "").strip()
        safe = "".join(c if (c.isalnum() or c in " -_") else "_" for c in title).strip()
        label = safe.replace(" ", "_") if safe else f"cut{i}"
        out_path = out_dir / f"{stem}_{label}{ext}"
        cmd = [config.FFMPEG, "-y", "-hide_banner", "-i", str(info.path),
               "-map", "0", "-c", "copy"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-to", f"{end:.3f}", "-reset_timestamps", "1",
                "-progress", "pipe:1", "-nostats", str(out_path)]
        cmds.append((cmd, out_path))
    return cmds


def build_single_cut_cmd(src: Path, out: Path, start, end) -> tuple[list, str]:
    """Schneller Einzel-Ausschnitt (verlustfrei) für den Direkt-Download.

    Nutzt Input-seitiges Seeking (`-ss` vor `-i`) für Tempo; der Schnitt beginnt
    am nächstliegenden Keyframe. Für den Download völlig ausreichend.
    """
    s = parse_time(start)
    e = parse_time(end)
    if e <= s:
        return [], "Ende muss nach dem Start liegen."
    cmd = [config.FFMPEG, "-y", "-hide_banner"]
    if s > 0:
        cmd += ["-ss", f"{s:.3f}"]
    cmd += ["-i", str(src), "-t", f"{e - s:.3f}", "-map", "0", "-c", "copy",
            "-avoid_negative_ts", "make_zero", str(out)]
    return cmd, ""


# --- Heuristiken / Import-Helfer --------------------------------------------

def apply_smart_disposition(audio: list, subs: list,
                            prefer_langs: Optional[set] = None) -> None:
    """Default/Forced intelligent setzen (in-place).

    - Genau eine Default-Audio (Whitelist-Sprache bevorzugt, sonst erste)
    - Forced-Subs: Flag oder Titel enthält forced/zwang/signs
    - Genau ein Default-Sub (nicht-forced bevorzugt, sonst erste Forced)
    """
    prefer_langs = prefer_langs or set()

    def _lang(t):
        return str(t.get("language") or "").strip().lower()

    def _title(t):
        return str(t.get("title") or "").lower()

    # Audio defaults
    for a in audio:
        a["default"] = False
    kept_a = [a for a in audio if a.get("keep", True)]
    pick = None
    if prefer_langs:
        pick = next((a for a in kept_a if _lang(a) in prefer_langs), None)
    if pick is None and kept_a:
        pick = next((a for a in kept_a if a.get("default")), kept_a[0])
    if pick is not None:
        pick["default"] = True

    # Subs: forced from title/flag
    for s in subs:
        title = _title(s)
        if any(k in title for k in ("forced", "zwang", "signs", "song")):
            s["forced"] = True
        s["default"] = False
    kept_s = [s for s in subs if s.get("keep", True)]
    forced = [s for s in kept_s if s.get("forced")]
    normal = [s for s in kept_s if not s.get("forced")]
    # Default: erste Whitelist-Sprache unter normalen, sonst erste normale, sonst forced
    pick_s = None
    pool = normal or forced
    if prefer_langs and pool:
        pick_s = next((s for s in pool if _lang(s) in prefer_langs), None)
    if pick_s is None and pool:
        pick_s = pool[0]
    if pick_s is not None:
        pick_s["default"] = True


def find_sidecar_attachments(video_path: Path) -> list[dict]:
    """Fonts/Cover neben der Quelle: Fonts/-Ordner + Stem-gleiche Dateien."""
    found: list[Path] = []
    parent = video_path.parent
    fonts_dir = parent / "Fonts"
    if fonts_dir.is_dir():
        for f in fonts_dir.iterdir():
            if f.is_file() and f.suffix.lower() in config.ATTACHMENT_EXTENSIONS:
                found.append(f)
    stem = video_path.stem
    for f in parent.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in config.ATTACHMENT_EXTENSIONS:
            continue
        if f.stem == stem or f.stem.startswith(stem + "."):
            found.append(f)
    # Dedup
    seen = set()
    out = []
    for f in found:
        key = str(f.resolve())
        if key in seen:
            continue
        seen.add(key)
        rel = config.rel_input(f)
        out.append({"path": rel or str(f), "name": f.name})
    return out


def parse_chapters_file(path: Path) -> list[dict]:
    """Kapitel aus NFO (Kodi), ffmetadata oder 'HH:MM:SS Titel'-Text lesen."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    chapters: list[dict] = []
    # FFMETADATA
    if ";FFMETADATA" in text or "[CHAPTER]" in text.upper():
        cur = None
        for line in text.splitlines():
            line = line.strip()
            if line.upper() == "[CHAPTER]":
                if cur and "start" in cur:
                    chapters.append(cur)
                cur = {}
            elif cur is not None and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip().lower(), v.strip()
                if k == "start":
                    cur["start"] = int(v) / 1000.0 if int(v) > 10000 else float(v)
                elif k == "end":
                    cur["end"] = int(v) / 1000.0 if int(v) > 10000 else float(v)
                elif k == "title":
                    cur["title"] = v
        if cur and "start" in cur:
            chapters.append(cur)
        return chapters

    # Kodi NFO XML (einfach)
    if "<chapter" in text.lower() or "<file>" in text.lower():
        import re
        for m in re.finditer(
            r"<chapter[^>]*>.*?<start[^>]*>([^<]+)</start>.*?"
            r"(?:<name[^>]*>([^<]*)</name>|<title[^>]*>([^<]*)</title>)?.*?</chapter>",
            text, flags=re.I | re.S):
            try:
                start = parse_time(m.group(1).strip())
            except Exception:
                continue
            title = (m.group(2) or m.group(3) or "").strip()
            chapters.append({"start": start, "end": 0, "title": title})
        # Endzeiten auffüllen
        for i, ch in enumerate(chapters):
            if i + 1 < len(chapters):
                ch["end"] = chapters[i + 1]["start"]
        return chapters

    # Zeilen: HH:MM:SS Titel  oder  Sekunden Titel
    import re
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = re.match(r"^(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?|\d+(?:\.\d+)?)\s+(.+)$", line)
        if not m:
            continue
        chapters.append({"start": parse_time(m.group(1)), "end": 0, "title": m.group(2).strip()})
    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            ch["end"] = chapters[i + 1]["start"]
    return chapters
