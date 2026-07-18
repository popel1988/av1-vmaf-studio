"""Dolby-Vision-(RPU)-Erhaltung via dovi_tool – HEVC (Profil 8.x) und AV1 (10.x).

Die Encoder selbst schreiben nur den HDR10-Basislayer (keinen DV-RPU-Layer).
Dieser Post-Schritt extrahiert daher die dynamische RPU-Schicht aus der Quelle,
re-injiziert sie in den frisch codierten Stream (dovi_tool) und muxt das Ergebnis
zurück in den Container.

Unterstützt:
  * HEVC-Ziel  -> Dolby Vision Profil 8.1 (HDR10-Basis + RPU)
  * AV1-Ziel   -> Dolby Vision Profil 10.1 (HDR10-Basis + RPU)
  * Quelle Profil 7 (Dual-Layer) -> Konvertierung nach 8.1 (--mode 2, EL entfällt)
  * Quelle Profil 5              -> Konvertierung nach 8.1 (--mode 3, best-effort)

Die RPU-Metadaten sind codec-übergreifend: eine HEVC-DV-Quelle kann so auch in
eine AV1-Ausgabe (Profil 10.1) übernommen werden und umgekehrt.

Best-effort: schlägt irgendein Schritt fehl oder fehlt dovi_tool, bleibt der
normale HDR10-Encode unverändert erhalten (der Basislayer ist HDR10-kompatibel,
Player ohne DV zeigen also HDR10).
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
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _mode_for_profile(profile: int) -> int:
    """dovi_tool-Konvertierungsmodus je Quell-Profil.

    7 -> Dual-Layer (FEL/MEL) auf Single-Layer 8.1 (--mode 2, EL entfällt).
    5/8/10 -> RPU unverändert übernehmen (Mode 0). Bei Profil 5 bleibt es damit
        faithful Profil 5: dovi_tool ändert nur die RPU, nicht die Basis-Pixel –
        eine „Konvertierung" nach 8.1 (--mode 3) würde die im DV5-Farbformat
        codierte Basis fehlfarbig lassen. Profil 5 hat keinen HDR10-Fallback und
        braucht daher einen DV-fähigen Player.
    """
    if profile == 7:
        return 2
    return 0


def _es_args(codec: str) -> tuple[str, list[str]]:
    """(-f-Format, zusätzliche Bitstream-Filter) für den Elementarstream je Codec.

    AV1 wird als roher OBU-Stream verarbeitet, HEVC als Annex-B.
    """
    if codec == "av1":
        return "obu", []
    return "hevc", ["-bsf:v", "hevc_mp4toannexb"]


def _run(cmd: list[str], label: str) -> bool:
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=False)
    if res.returncode != 0:
        logger.warning("%s fehlgeschlagen (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                       label, res.returncode, " ".join(cmd), (res.stderr or "")[-1500:])
    return res.returncode == 0


def _extract_rpu(source: Path, source_codec: str, rpu: Path, mode: int = 0) -> bool:
    """RPU-Schicht aus der (Dolby-Vision-)Quelle in eine .bin extrahieren.

    ffmpeg liefert den Elementarstream (HEVC-Annex-B bzw. AV1-OBU) via Pipe an
    dovi_tool. `mode` konvertiert die RPU dabei (z. B. Profil 7 -> 8.1).
    """
    fmt, bsf = _es_args(source_codec)
    ff_cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error",
              "-i", str(source), "-map", "0:v:0", "-c:v", "copy",
              *bsf, "-f", fmt, "-"]
    dv_cmd = [config.DOVI_TOOL]
    if mode:
        dv_cmd += ["-m", str(mode)]
    dv_cmd += ["extract-rpu", "-", "-o", str(rpu)]
    try:
        p1 = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(dv_cmd, stdin=p1.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace")
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


def reinject(source: Path, encoded: Path, work_dir: Path, *,
             source_codec: str = "hevc", target_codec: str = "hevc",
             profile: int = 0, fps: float = 0.0,
             status: StatusCb = None) -> tuple[Optional[Path], str]:
    """DV-RPU aus `source` in den `encoded`-Stream re-injizieren und remuxen.

    `source_codec`/`target_codec`: hevc|av1 (Quelle bzw. Encode-Ausgabe).
    `profile`: DV-Profil der Quelle (steuert eine evtl. Konvertierung 7/5 -> 8.1).

    Gibt (Pfad zur neuen DV-Datei, "") bei Erfolg zurück, sonst (None, Grund).
    Der Aufrufer ersetzt bei Erfolg die Encode-Ausgabe durch die DV-Datei.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    mode = _mode_for_profile(profile)
    tgt_fmt, tgt_bsf = _es_args(target_codec)
    ext = "obu" if target_codec == "av1" else "hevc"
    rpu = work_dir / "rpu.bin"
    enc_es = work_dir / f"encoded.{ext}"
    inj_es = work_dir / f"injected.{ext}"
    final = encoded.with_name(f"{encoded.stem}.__dv__{encoded.suffix}")
    dv_target = "Profil 10.1 (AV1)" if target_codec == "av1" else "Profil 8.1 (HEVC)"

    try:
        if status:
            conv = f" (Profil {profile} → 8.1)" if mode else ""
            status(f"Dolby Vision: RPU wird aus der Quelle extrahiert{conv} …")
        if not _extract_rpu(source, source_codec, rpu, mode):
            return None, "RPU-Extraktion fehlgeschlagen"

        if status:
            status(f"Dolby Vision: Encode-Stream wird vorbereitet ({dv_target}) …")
        if not _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", str(encoded), "-map", "0:v:0", "-c:v", "copy",
                     *tgt_bsf, "-f", tgt_fmt, str(enc_es)],
                    "Elementarstream-Extraktion (Encode)"):
            return None, "Elementarstream-Extraktion des Encodes fehlgeschlagen"

        if status:
            status("Dolby Vision: RPU wird re-injiziert …")
        if not _run([config.DOVI_TOOL, "inject-rpu", "-i", str(enc_es),
                     "--rpu-in", str(rpu), "-o", str(inj_es)],
                    "RPU-Injektion"):
            return None, "RPU-Injektion fehlgeschlagen"

        if status:
            status("Dolby Vision: Container wird gemuxt …")
        mux = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        if fps and fps > 0:
            # Roh-Elementarstream hat keine Framerate – sonst rät ffmpeg (25 fps).
            mux += ["-r", f"{fps:.6f}"]
        mux += ["-i", str(inj_es), "-i", str(encoded),
                "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?",
                "-map_chapters", "1", "-c", "copy", str(final)]
        if not _run(mux, "DV-Mux"):
            return None, "DV-Mux fehlgeschlagen"

        if not (final.exists() and final.stat().st_size > 0):
            return None, "DV-Ausgabedatei leer"
        return final, ""
    finally:
        for f in (rpu, enc_es, inj_es):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
