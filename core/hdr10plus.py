"""HDR10+-Metadaten nach einem HEVC-Encode zurückschreiben.

hdr10plus_tool (quietvoid, 1.7.x) liest und schreibt die dynamischen
Szenenmetadaten nur bei HEVC. AV1 ist in diesem Binary nicht enthalten.
Hardware-Encoder setzen die Daten nicht selbst; deshalb Extraktion aus der
Quelle, Injektion in den fertigen HEVC-Stream und Remux.

Best-effort: schlägt ein Schritt fehl oder fehlt das Werkzeug, bleibt die
HDR10-Basis erhalten. Der Job bricht deshalb nicht ab.
"""
from __future__ import annotations

import functools
import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import config

logger = logging.getLogger("vcompress.hdr10plus")

StatusCb = Optional[Callable[[str], None]]

_HEVC = {"hevc", "h265", "libx265"}


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """True, wenn hdr10plus_tool im Image aufrufbar ist."""
    try:
        r = subprocess.run([config.HDR10PLUS_TOOL, "--version"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def applicable(*, target_codec: str, source_codec: str, preserve_hdr: bool,
               tonemap: bool, hdr10_plus: bool) -> Optional[str]:
    """None, wenn die Reinjektion laufen soll, sonst der Grund zum Überspringen.

    ``keine Quelle`` und ``nicht beibehalten`` sind stille Gründe. Die übrigen
    Gründe soll der Aufrufer als Warnung an den Job hängen.
    """
    if not hdr10_plus:
        return "keine Quelle"
    if tonemap or not preserve_hdr:
        return "nicht beibehalten"
    if (target_codec or "").lower() != "hevc":
        return "nur HEVC-Ziel"
    if (source_codec or "").lower() not in _HEVC:
        return "Quelle ist kein HEVC"
    return None


def _run(cmd: list[str], label: str) -> bool:
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=False)
    if res.returncode != 0:
        logger.warning("%s fehlgeschlagen (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                       label, res.returncode, " ".join(cmd), (res.stderr or "")[-1500:])
    return res.returncode == 0


def _json_ok(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 2
    except OSError:
        return False


def _extract_json(source: Path, dest: Path, work_es: Path) -> bool:
    """JSON aus einer HEVC-Quelle. MKV direkt, sonst über einen Annex-B-Stream."""
    if source.suffix.lower() in {".mkv", ".mk3d"}:
        if _run([config.HDR10PLUS_TOOL, "extract", str(source), "-o", str(dest)],
                "HDR10+-Extraktion (MKV)"):
            if _json_ok(dest):
                return True
        dest.unlink(missing_ok=True)
    if not _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(source), "-map", "0:v:0", "-c:v", "copy",
                 "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(work_es)],
                "HDR10+-Elementarstream (Quelle)"):
        return False
    if not _run([config.HDR10PLUS_TOOL, "extract", str(work_es), "-o", str(dest)],
                "HDR10+-Extraktion"):
        return False
    return _json_ok(dest)


def reinject(source: Path, encoded: Path, work_dir: Path, *,
             fps: float = 0.0, status: StatusCb = None) -> tuple[Optional[Path], str]:
    """HDR10+-JSON aus `source` in den HEVC-Encode schreiben und remuxen.

    Gibt (Pfad zur neuen Datei, "") bei Erfolg zurück, sonst (None, Grund).
    Der Aufrufer ersetzt bei Erfolg die Encode-Ausgabe.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    meta = work_dir / "hdr10plus.json"
    src_es = work_dir / "source.hevc"
    enc_es = work_dir / "encoded.hevc"
    inj_es = work_dir / "injected.hevc"
    final = encoded.with_name(f"{encoded.stem}.__hdr10plus__{encoded.suffix}")
    try:
        if status:
            status("HDR10+: Metadaten werden aus der Quelle gelesen …")
        if not _extract_json(source, meta, src_es):
            return None, "Extraktion fehlgeschlagen oder keine HDR10+-Daten im HEVC-Stream"

        if status:
            status("HDR10+: Encode-Stream wird vorbereitet …")
        if not _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", str(encoded), "-map", "0:v:0", "-c:v", "copy",
                     "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(enc_es)],
                    "HDR10+-Elementarstream (Encode)"):
            return None, "Elementarstream des Encodes fehlgeschlagen"

        if status:
            status("HDR10+: Metadaten werden eingesetzt …")
        if not _run([config.HDR10PLUS_TOOL, "inject", "-i", str(enc_es),
                     "-j", str(meta), "-o", str(inj_es)],
                    "HDR10+-Injektion"):
            return None, "Injektion fehlgeschlagen"

        if status:
            status("HDR10+: Container wird gemuxt …")
        mux = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        if fps and fps > 0:
            mux += ["-r", f"{fps:.6f}"]
        mux += ["-i", str(inj_es), "-i", str(encoded),
                "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?",
                "-map_chapters", "1", "-c", "copy", str(final)]
        if not _run(mux, "HDR10+-Mux"):
            return None, "Mux fehlgeschlagen"
        if not (final.exists() and final.stat().st_size > 0):
            return None, "Ausgabedatei leer"
        return final, ""
    finally:
        for f in (meta, src_es, enc_es, inj_es):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
