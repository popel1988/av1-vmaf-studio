"""Experimentelle Dolby-Vision-(RPU)-Erhaltung für Profil 8.1 via dovi_tool.

Der Re-Encode selbst kann keinen DV-RPU-Layer erzeugen (die HW-/SW-Encoder
schreiben nur HDR10). Dieser Post-Schritt extrahiert daher die dynamische
RPU-Schicht aus der Quelle, re-injiziert sie in den frisch codierten
HEVC-Stream (dovi_tool) und muxt das Ergebnis zurück in den Container.

Best-effort: schlägt irgendein Schritt fehl oder fehlt dovi_tool, bleibt der
normale HDR10-Encode unverändert erhalten.
"""
from __future__ import annotations

import functools
import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import config

logger = logging.getLogger("vcompress.dovi")

StatusCb = Optional[Callable[[str], None]]


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """True, wenn dovi_tool im Image aufrufbar ist."""
    try:
        r = subprocess.run([config.DOVI_TOOL, "--version"],
                           capture_output=True, text=True, timeout=15, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(cmd: list[str], label: str) -> bool:
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        logger.warning("%s fehlgeschlagen (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                       label, res.returncode, " ".join(cmd), (res.stderr or "")[-1500:])
    return res.returncode == 0


def _extract_rpu(source: Path, rpu: Path) -> bool:
    """RPU-Schicht aus der (Dolby-Vision-)Quelle in eine .bin extrahieren.

    ffmpeg liefert den HEVC-Elementarstream (Annex-B) an dovi_tool via Pipe.
    """
    ff_cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error",
              "-i", str(source), "-map", "0:v:0", "-c:v", "copy",
              "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"]
    dv_cmd = [config.DOVI_TOOL, "extract-rpu", "-", "-o", str(rpu)]
    try:
        p1 = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(dv_cmd, stdin=p1.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p1.stdout:
            p1.stdout.close()  # dovi_tool erhält EOF, wenn ffmpeg endet
        _, err = p2.communicate()
        p1.wait()
        ok = p2.returncode == 0 and rpu.exists() and rpu.stat().st_size > 0
        if not ok:
            logger.warning("RPU-Extraktion fehlgeschlagen: %s", (err or "")[-1500:])
        return ok
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("RPU-Extraktion – Ausnahme: %s", e)
        return False


def reinject(source: Path, encoded: Path, work_dir: Path,
             fps: float = 0.0, status: StatusCb = None) -> tuple[Optional[Path], str]:
    """DV-RPU aus `source` in den `encoded`-HEVC re-injizieren und remuxen.

    Gibt (Pfad zur neuen DV-Datei, "") bei Erfolg zurück, sonst (None, Grund).
    Der Aufrufer ersetzt bei Erfolg die Encode-Ausgabe durch die DV-Datei.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    rpu = work_dir / "rpu.bin"
    enc_hevc = work_dir / "encoded.hevc"
    inj_hevc = work_dir / "injected.hevc"
    final = encoded.with_name(f"{encoded.stem}.__dv__{encoded.suffix}")

    try:
        if status:
            status("Dolby Vision: RPU wird aus der Quelle extrahiert …")
        if not _extract_rpu(source, rpu):
            return None, "RPU-Extraktion fehlgeschlagen"

        if status:
            status("Dolby Vision: Encode-Stream wird vorbereitet …")
        if not _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", str(encoded), "-map", "0:v:0", "-c:v", "copy",
                     "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(enc_hevc)],
                    "HEVC-Extraktion (Encode)"):
            return None, "HEVC-Extraktion des Encodes fehlgeschlagen"

        if status:
            status("Dolby Vision: RPU wird re-injiziert …")
        if not _run([config.DOVI_TOOL, "inject-rpu", "-i", str(enc_hevc),
                     "--rpu-in", str(rpu), "-o", str(inj_hevc)],
                    "RPU-Injektion"):
            return None, "RPU-Injektion fehlgeschlagen"

        if status:
            status("Dolby Vision: Container wird gemuxt …")
        mux = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        if fps and fps > 0:
            # Roh-HEVC hat keine Framerate – sonst nimmt ffmpeg 25 fps an.
            mux += ["-r", f"{fps:.6f}"]
        mux += ["-i", str(inj_hevc), "-i", str(encoded),
                "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?",
                "-map_chapters", "1", "-c", "copy", str(final)]
        if not _run(mux, "DV-Mux"):
            return None, "DV-Mux fehlgeschlagen"

        if not (final.exists() and final.stat().st_size > 0):
            return None, "DV-Ausgabedatei leer"
        return final, ""
    finally:
        for f in (rpu, enc_hevc, inj_hevc):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
