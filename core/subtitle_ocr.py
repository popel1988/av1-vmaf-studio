"""OCR-Umwandlung für Bild-Untertitel (PGS / SUP & VobSub) nach SRT.

Unterstützt zwei Backends:
1. ``SubtitleEdit`` CLI (falls installiert) für PGS & VobSub.
2. Eingebauter PGS-Parser (.sup) + ``tesseract`` CLI (leichtgewichtig, ohne .NET).
"""
from __future__ import annotations

import functools
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Callable, Optional

from . import config

logger = logging.getLogger("vcompress.ocr")

# Tesseract Sprach-Mapping (ISO 639-2/1 -> Tesseract lang code)
_TESS_LANG = {
    "ger": "deu", "deu": "deu", "de": "deu",
    "eng": "eng", "en": "eng",
    "fra": "fra", "fre": "fra", "fr": "fra",
    "spa": "spa", "es": "spa",
    "ita": "ita", "it": "ita",
    "jpn": "jpn", "ja": "jpn",
    "por": "por", "pt": "por",
    "rus": "rus", "ru": "rus",
    "nld": "nld", "dut": "nld", "nl": "nld",
    "pol": "pol", "pl": "pol",
    "tur": "tur", "tr": "tur",
}


def _find_tesseract() -> str:
    env = os.getenv("TESSERACT_BIN")
    if env and Path(env).is_file():
        return env
    for cand in config._local_bin_candidates("tesseract"):
        if cand.is_file():
            return str(cand)
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name == "nt":
        for p in (
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        ):
            if p.is_file():
                return str(p)
    return ""


def _find_subtitle_edit() -> str:
    env = os.getenv("SUBTITLEEDIT_BIN")
    if env and Path(env).is_file():
        return env
    for cand in config._local_bin_candidates("SubtitleEdit"):
        if cand.is_file():
            return str(cand)
    found = shutil.which("SubtitleEdit") or shutil.which("subtitleedit")
    if found:
        return found
    if os.name == "nt":
        for p in (
            Path(r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe"),
            Path(r"C:\Program Files (x86)\Subtitle Edit\SubtitleEdit.exe"),
        ):
            if p.is_file():
                return str(p)
    return ""


@functools.lru_cache(maxsize=1)
def ocr_status() -> dict:
    """Prüft verfügbare OCR-Backends."""
    se = _find_subtitle_edit()
    tess = _find_tesseract()
    langs: list[str] = []
    if tess:
        try:
            r = subprocess.run(
                [tess, "--list-langs"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=8, check=False,
            )
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line and not line.lower().startswith("list of"):
                    langs.append(line)
        except OSError:
            pass
    return {
        "available": bool(se or tess),
        "engine": "subtitleedit" if se else ("tesseract" if tess else ""),
        "subtitle_edit": bool(se),
        "tesseract": bool(tess),
        "languages": langs,
    }


def available() -> bool:
    return bool(ocr_status().get("available"))


def _fmt_srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _decode_pgs_rle(data: bytes, width: int, height: int) -> bytes:
    """Dekodiert PGS Run-Length-Encoded Bitmap-Daten in Palette-Indizes (width*height)."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n and len(out) < width * height:
        b = data[i]
        i += 1
        if b != 0:
            out.append(b)
        else:
            if i >= n:
                break
            b2 = data[i]
            i += 1
            if b2 == 0:
                # End of line: ggf. bis Zeilenende auffüllen
                rem = len(out) % width
                if rem:
                    out.extend(b"\x00" * (width - rem))
            elif (b2 & 0xC0) == 0x00:
                count = b2 & 0x3F
                out.extend(b"\x00" * count)
            elif (b2 & 0xC0) == 0x40:
                if i >= n:
                    break
                count = ((b2 & 0x3F) << 8) | data[i]
                i += 1
                out.extend(b"\x00" * count)
            elif (b2 & 0xC0) == 0x80:
                if i >= n:
                    break
                count = b2 & 0x3F
                color = data[i]
                i += 1
                out.extend(bytes([color]) * count)
            elif (b2 & 0xC0) == 0xC0:
                if i + 1 >= n:
                    break
                count = ((b2 & 0x3F) << 8) | data[i]
                color = data[i + 1]
                i += 2
                out.extend(bytes([color]) * count)
    expected = width * height
    if len(out) < expected:
        out.extend(b"\x00" * (expected - len(out)))
    return bytes(out[:expected])


def _write_pgm_for_ocr(indices: bytes, palette: dict[int, tuple[int, int]],
                       width: int, height: int, out_pgm: Path) -> None:
    """Erzeugt ein adaptiv vorverarbeitetes, kantengeglättetes Graustufenbild
    (schwarzer Text auf weißem Grund, Kontur-/Schatten-Bereinigung + 2× Upscaling
    bei kleinen Cues) für deutlich präzisere Tesseract-OCR ohne Whisper."""
    # 1) Maximale Text-Helligkeit aus den deckenden Paletteneinträgen ermitteln
    #    (wichtig für gelbe/gedimmte HDR-PGS-Untertitel, deren Y oft bei 110–145 liegt!).
    opaque_lumas = [luma for luma, alpha in palette.values() if alpha >= 64]
    peak_luma = max(opaque_lumas) if opaque_lumas else 235
    peak_eff = max(95, peak_luma)
    hi_thresh = max(85, int(round(peak_eff * 0.72)))
    lo_thresh = max(48, int(round(peak_eff * 0.44)))
    if hi_thresh <= lo_thresh:
        hi_thresh = lo_thresh + 20

    raw_gray = bytearray(b"\xff" * (width * height))
    for i in range(min(len(indices), width * height)):
        luma, alpha = palette.get(indices[i], (0, 0))
        if alpha < 48:
            val = 255
        else:
            eff = (luma * alpha) // 255
            if eff >= hi_thresh:
                val = 0  # Reiner Textkern -> Tiefschwarz
            elif eff <= lo_thresh:
                val = 255  # Dunkle PGS-Umrandung / Schlagschatten -> Weiß entfernen
            else:
                # Weicher Anti-Aliasing-Übergang für saubere Buchstabenkanten
                frac = (eff - lo_thresh) / float(hi_thresh - lo_thresh)
                val = max(0, min(255, int(round(255.0 * (1.0 - frac)))))
        raw_gray[i] = val

    # 2) Kleine Bitmaps (< 96 px Höhe) bilinear 2× hochskalieren (hilft Tesseract enorm)
    if width > 0 and height > 0 and height < 96:
        sw, sh = width * 2, height * 2
        scaled = bytearray(sw * sh)
        for sy in range(sh):
            fy = (sy + 0.5) / 2.0 - 0.5
            y0 = max(0, min(height - 1, int(fy)))
            y1 = max(0, min(height - 1, y0 + 1))
            wy = max(0.0, fy - y0)
            row0 = y0 * width
            row1 = y1 * width
            dst_row = sy * sw
            for sx in range(sw):
                fx = (sx + 0.5) / 2.0 - 0.5
                x0 = max(0, min(width - 1, int(fx)))
                x1 = max(0, min(width - 1, x0 + 1))
                wx = max(0.0, fx - x0)
                v00 = raw_gray[row0 + x0]
                v01 = raw_gray[row0 + x1]
                v10 = raw_gray[row1 + x0]
                v11 = raw_gray[row1 + x1]
                top = v00 * (1.0 - wx) + v01 * wx
                bot = v10 * (1.0 - wx) + v11 * wx
                scaled[dst_row + sx] = int(round(top * (1.0 - wy) + bot * wy))
        width, height, raw_gray = sw, sh, scaled

    # 3) Großzügiger weißer Ruhe-Rand (24 px) für Tesseract-Zeilensegmentierung
    pad = 24
    w2 = width + pad * 2
    h2 = height + pad * 2
    buf = bytearray(b"\xff" * (w2 * h2))
    for y in range(height):
        src_off = y * width
        dst_off = (y + pad) * w2 + pad
        buf[dst_off:dst_off + width] = raw_gray[src_off:src_off + width]

    header = f"P5\n{w2} {h2}\n255\n".encode("ascii")
    out_pgm.write_bytes(header + bytes(buf))


def _clean_ocr_text(raw: str) -> str:
    """Bereinigt typische Bitmap-OCR-Artefakte (z. B. senkrechter Strich '|' statt 'I')."""
    import re as _re
    lines: list[str] = []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        # Häufiger Tesseract-Fehler bei serifenlosen Film-Fonts: '|' am Wortanfang statt 'I'
        s = _re.sub(r"(^|[\s\"'„(])\|(?=[a-zäöüßA-ZÄÖÜ])", r"\1I", s)
        s = _re.sub(r"(?<=\s)\|(?=\s|$|[,.!?])", "I", s)
        lines.append(s)
    return "\n".join(lines).strip()


def _write_png_from_pgm_data(pgm_path: Path, png_path: Path) -> Path:
    """Konvertiert PGM in unkomprimiertes/zlib Graustufen-PNG, falls Tesseract PNG bevorzugt."""
    raw = pgm_path.read_bytes()
    parts = raw.split(b"\n", 3)
    if len(parts) < 4 or parts[0] != b"P5":
        return pgm_path
    dims = parts[1].decode("ascii").split()
    w, h = int(dims[0]), int(dims[1])
    pixels = parts[3]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    scanlines = bytearray()
    for y in range(h):
        scanlines.append(0)
        scanlines.extend(pixels[y * w:(y + 1) * w])
    idat = zlib.compress(bytes(scanlines), 6)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    png_path.write_bytes(png)
    return png_path


def _iter_pgs_events(sup_path: Path):
    """Liest Display-Sets aus einer Blu-ray PGS (.sup) Datei.
    Liefert Tupel (start_sec, end_sec, width, height, indices, palette).
    """
    data = sup_path.read_bytes()
    pos = 0
    n = len(data)
    palette: dict[int, tuple[int, int]] = {}
    obj_rle = bytearray()
    obj_w = 0
    obj_h = 0
    active_event = None  # (start_pts, w, h, indices, pal)

    while pos + 13 <= n:
        if data[pos:pos + 2] != b"PG":
            pos += 1
            continue
        pts = struct.unpack(">I", data[pos + 2:pos + 6])[0] / 90000.0
        seg_type = data[pos + 10]
        seg_size = struct.unpack(">H", data[pos + 11:pos + 13])[0]
        seg_start = pos + 13
        seg_end = seg_start + seg_size
        if seg_end > n:
            break
        payload = data[seg_start:seg_end]
        pos = seg_end

        if seg_type == 0x16 and len(payload) >= 11:  # PCS
            num_objs = payload[10]
            if num_objs == 0 and active_event is not None:
                st, w, h, idxs, pal = active_event
                end_t = pts if pts > st else st + 3.5
                yield (st, end_t, w, h, idxs, pal)
                active_event = None
                obj_rle.clear()
        elif seg_type == 0x14 and len(payload) >= 2:  # PDS
            palette = {}
            off = 2
            while off + 5 <= len(payload):
                entry_id = payload[off]
                y_val = payload[off + 1]
                alpha = payload[off + 4]
                palette[entry_id] = (y_val, alpha)
                off += 5
        elif seg_type == 0x15 and len(payload) >= 7:  # ODS
            seq_flag = payload[3]
            if seq_flag & 0x80:  # First in sequence
                if len(payload) >= 11:
                    obj_w = struct.unpack(">H", payload[7:9])[0]
                    obj_h = struct.unpack(">H", payload[9:11])[0]
                    obj_rle = bytearray(payload[11:])
            else:
                obj_rle.extend(payload[4:])
            if (seq_flag & 0x40) and obj_w > 0 and obj_h > 0:  # Last in sequence
                if active_event is not None:
                    st, w, h, idxs, pal = active_event
                    end_t = pts if pts > st else st + 3.5
                    yield (st, end_t, w, h, idxs, pal)
                idxs = _decode_pgs_rle(bytes(obj_rle), obj_w, obj_h)
                active_event = (pts, obj_w, obj_h, idxs, dict(palette))
                obj_rle.clear()

    if active_event is not None:
        st, w, h, idxs, pal = active_event
        yield (st, st + 3.5, w, h, idxs, pal)


def _ocr_sup_with_tesseract(sup_path: Path, out_srt: Path, lang: str = "eng",
                            on_progress: Optional[Callable[[int, int], None]] = None) -> tuple[bool, str]:
    tess = _find_tesseract()
    if not tess:
        return False, "Tesseract OCR nicht gefunden."
    st = ocr_status()
    avail_langs = set(st.get("languages") or [])
    tess_lang = _TESS_LANG.get((lang or "").lower(), "eng")
    if avail_langs and tess_lang not in avail_langs:
        tess_lang = "eng" if "eng" in avail_langs else (next(iter(avail_langs)) if avail_langs else "eng")

    events = list(_iter_pgs_events(sup_path))
    if not events:
        return False, "Keine lesbaren PGS-Segmente in der Untertitelspur gefunden."

    entries: list[str] = []
    total = len(events)
    with tempfile.TemporaryDirectory(prefix="vc_ocr_") as tmp:
        tmp_dir = Path(tmp)
        for idx, (start_t, end_t, w, h, idxs, pal) in enumerate(events, start=1):
            if on_progress and (idx == 1 or idx % 10 == 0 or idx == total):
                on_progress(idx, total)
            pgm = tmp_dir / f"sub_{idx}.pgm"
            png = tmp_dir / f"sub_{idx}.png"
            _write_pgm_for_ocr(idxs, pal, w, h, pgm)
            img = _write_png_from_pgm_data(pgm, png)
            cmd = [tess, str(img), "stdout", "-l", tess_lang, "--psm", "6", "--dpi", "300"]
            res = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=20, check=False,
            )
            txt = _clean_ocr_text(res.stdout or "")
            if txt:
                entries.append(
                    f"{len(entries) + 1}\n{_fmt_srt_time(start_t)} --> {_fmt_srt_time(end_t)}\n{txt}\n"
                )
            pgm.unlink(missing_ok=True)
            png.unlink(missing_ok=True)

    if not entries:
        return False, "OCR lieferte keinen erkannten Text."
    out_srt.write_text("\n".join(entries), encoding="utf-8")
    return True, ""


def convert_subtitle_track_to_srt(
    source: Path,
    sub_index: int,
    out_srt: Path,
    *,
    codec: str = "",
    language: str = "eng",
    duration_limit: Optional[float] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[bool, str]:
    """Extrahiert eine Bild-Untertitelspur (`0:s:{sub_index}`) aus `source`
    und wandelt sie per OCR in `out_srt` (.srt) um."""
    se = _find_subtitle_edit()
    tess = _find_tesseract()
    if not se and not tess:
        return False, "Kein OCR-Backend verfügbar (weder Tesseract noch SubtitleEdit installiert)."

    out_srt.parent.mkdir(parents=True, exist_ok=True)
    c = (codec or "").lower()
    is_pgs = c in ("hdmv_pgs_subtitle", "pgssub", "")

    with tempfile.TemporaryDirectory(prefix="vc_subext_") as tmp:
        tmp_dir = Path(tmp)
        if is_pgs:
            ext_file = tmp_dir / "track.sup"
            ff_cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
            if duration_limit and duration_limit > 0:
                ff_cmd += ["-t", f"{float(duration_limit):.3f}"]
            ff_cmd += ["-i", str(source), "-map", f"0:s:{int(sub_index)}", "-c:s", "copy", str(ext_file)]
            res = subprocess.run(ff_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if res.returncode != 0 or not ext_file.is_file() or ext_file.stat().st_size == 0:
                return False, f"Extraktion der PGS-Spur #{sub_index} fehlgeschlagen."

            if se:
                se_cmd = [se, "/convert", str(ext_file), "subrip", f"/outputfolder:{tmp_dir}"]
                subprocess.run(se_cmd, capture_output=True, text=True, timeout=300, check=False)
                cand = tmp_dir / "track.srt"
                if cand.is_file() and cand.stat().st_size > 0:
                    shutil.copy2(cand, out_srt)
                    return True, ""

            if tess:
                return _ocr_sup_with_tesseract(ext_file, out_srt, lang=language, on_progress=on_progress)
            return False, "SubtitleEdit konnte die Spur nicht konvertieren und Tesseract fehlt."

        # VobSub / DVD-Subtitles: erst SubtitleEdit, sonst FFmpeg-Bitmap-Vorfilter + Tesseract
        if se:
            tmp_mkv = tmp_dir / "subonly.mkv"
            ff_cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
            if duration_limit and duration_limit > 0:
                ff_cmd += ["-t", f"{float(duration_limit):.3f}"]
            ff_cmd += ["-i", str(source), "-map", f"0:s:{int(sub_index)}", "-c:s", "copy", str(tmp_mkv)]
            res = subprocess.run(ff_cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and tmp_mkv.is_file():
                se_cmd = [se, "/convert", str(tmp_mkv), "subrip", f"/outputfolder:{tmp_dir}"]
                subprocess.run(se_cmd, capture_output=True, text=True, timeout=300, check=False)
                for srt_cand in tmp_dir.glob("*.srt"):
                    if srt_cand.stat().st_size > 0:
                        shutil.copy2(srt_cand, out_srt)
                        return True, ""

        if tess and c in ("dvd_subtitle", "vobsub", "dvb_subtitle"):
            return _ocr_bitmap_stream_via_ffmpeg(
                source, sub_index, out_srt, tmp_dir,
                lang=language, duration_limit=duration_limit, on_progress=on_progress,
            )
        return False, f"OCR für Untertitel-Codec '{codec}' benötigt SubtitleEdit CLI oder Tesseract."


def _ocr_bitmap_stream_via_ffmpeg(
    source: Path,
    sub_index: int,
    out_srt: Path,
    tmp_dir: Path,
    *,
    lang: str = "eng",
    duration_limit: Optional[float] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[bool, str]:
    """Rendert VobSub/DVB-Bitmap-Cues über FFmpeg mit Kontrast-/Schwellenwert-Vorfilterung
    (weißer Hintergrund, schwarze Schrift, 2× Lanczos-Skalierung) für Tesseract."""
    import json as _json

    tess = _find_tesseract()
    if not tess:
        return False, "Tesseract OCR nicht gefunden."
    st = ocr_status()
    avail_langs = set(st.get("languages") or [])
    tess_lang = _TESS_LANG.get((lang or "").lower(), "eng")
    if avail_langs and tess_lang not in avail_langs:
        tess_lang = "eng" if "eng" in avail_langs else (next(iter(avail_langs)) if avail_langs else "eng")

    probe_cmd = [
        config.FFPROBE, "-v", "error",
        "-select_streams", f"s:{int(sub_index)}",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "json", str(source),
    ]
    pr = subprocess.run(probe_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    try:
        pkts = (_json.loads(pr.stdout or "{}").get("packets") or [])
    except ValueError:
        pkts = []

    cues: list[tuple[float, float]] = []
    for p in pkts:
        try:
            pts = float(p.get("pts_time"))
        except (TypeError, ValueError):
            continue
        if duration_limit and duration_limit > 0 and pts >= duration_limit:
            break
        try:
            dur = float(p.get("duration_time") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        end = pts + (dur if 0.15 <= dur <= 12.0 else 3.2)
        if cues and abs(pts - cues[-1][0]) < 0.08:
            continue
        cues.append((pts, end))

    if not cues:
        return False, "Keine VobSub-Zeitstempel gefunden."

    entries: list[str] = []
    total = len(cues)
    for idx, (start_t, end_t) in enumerate(cues, start=1):
        if on_progress and (idx == 1 or idx % 10 == 0 or idx == total):
            on_progress(idx, total)
        mid_t = start_t + min(0.25, max(0.05, (end_t - start_t) * 0.25))
        png = tmp_dir / f"vob_{idx}.png"
        # Schwarze Canvas -> Bitmap-UT darüberlegen -> Graustufen -> Kontur abschneiden & invertieren
        fc = (
            f"color=c=black:s=1280x720:r=25[bg];"
            f"[bg][0:s:{int(sub_index)}]overlay=shortest=1,"
            f"crop=iw:ih/2:0:ih/2,scale=iw*2:ih*2:flags=lanczos,"
            f"format=gray,lut=y='if(gt(val,125),0,255)'"
        )
        ff_cmd = [
            config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, mid_t - 0.15):.3f}",
            "-i", str(source),
            "-filter_complex", fc,
            "-frames:v", "1", str(png),
        ]
        subprocess.run(ff_cmd, capture_output=True, text=True, timeout=15, check=False)
        if not png.is_file() or png.stat().st_size == 0:
            continue
        res = subprocess.run(
            [tess, str(png), "stdout", "-l", tess_lang, "--psm", "6", "--dpi", "300"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
        )
        txt = _clean_ocr_text(res.stdout or "")
        if txt:
            entries.append(
                f"{len(entries) + 1}\n{_fmt_srt_time(start_t)} --> {_fmt_srt_time(end_t)}\n{txt}\n"
            )
        png.unlink(missing_ok=True)

    if not entries:
        return False, "OCR lieferte keinen erkannten VobSub-Text."
    out_srt.write_text("\n".join(entries), encoding="utf-8")
    return True, ""
