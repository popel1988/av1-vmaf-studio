"""Dolby-Vision-(RPU)-Erhaltung via dovi_tool – nur HEVC-Ziel (Profil 8.1).

Die HW-Encoder schreiben nur den HDR10-Basislayer (keinen DV-RPU-Layer). Dieser
Post-Schritt extrahiert daher die dynamische RPU aus der Quelle, re-injiziert sie
in den frisch codierten HEVC-Stream (dovi_tool) und muxt das Ergebnis zurück.

Unterstützt:
  * HEVC-Ziel  -> Dolby Vision Profil 8.1 (HDR10-Basis + RPU)
  * Quelle Profil 7 (Dual-Layer) -> Konvertierung nach 8.1 (--mode 2, EL entfällt)
  * Quelle Profil 5              -> RPU unverändert (Mode 0, DV-fähiger Player nötig)

WICHTIG – AV1: dovi_tool kann AV1 auf der CLI NICHT verarbeiten (weder extract
noch inject; es meldet "Invalid input file type"). AV1-Dolby-Vision (Profil 10.1)
entsteht daher ausschließlich beim Encoden selbst über libsvtav1 (`-dolbyvision`),
siehe core/encoder.py – dieses Modul wird für AV1-Ziele nicht mehr aufgerufen.

Best-effort: schlägt ein Schritt fehl oder fehlt dovi_tool, bleibt der normale
HDR10-Encode erhalten (der Basislayer ist HDR10-kompatibel).
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


def _extract_rpu(source: Path, source_codec: str, rpu: Path, mode: int = 0,
                 duration_limit: Optional[float] = None) -> bool:
    """RPU-Schicht aus der (Dolby-Vision-)Quelle in eine .bin extrahieren.

    ffmpeg liefert den Elementarstream (HEVC-Annex-B bzw. AV1-OBU) via Pipe an
    dovi_tool. `mode` konvertiert die RPU dabei (z. B. Profil 7 -> 8.1).
    """
    fmt, bsf = _es_args(source_codec)
    ff_cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error"]
    if duration_limit and duration_limit > 0:
        ff_cmd += ["-t", f"{float(duration_limit):.3f}"]
    ff_cmd += ["-i", str(source), "-map", "0:v:0", "-c:v", "copy",
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
             duration_limit: Optional[float] = None,
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
        if not _extract_rpu(source, source_codec, rpu, mode, duration_limit=duration_limit):
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


def _pq_to_nits(pq_12bit: float) -> float:
    """Wandelt 12-bit SMPTE ST.2084 PQ-Codewert (0..4095) in cd/m² (Nits) um."""
    v = max(0.0, min(1.0, float(pq_12bit) / 4095.0))
    if v <= 0.0:
        return 0.0
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    vp = v ** (1.0 / m2)
    num = max(0.0, vp - c1)
    den = c2 - c3 * vp
    if den <= 1e-9:
        return 10000.0
    return round(min(10000.0, 10000.0 * ((num / den) ** (1.0 / m1))), 1)


def extract_l1_curve(source: Path, *, source_codec: str = "hevc",
                     max_points: int = 240,
                     duration_limit: Optional[float] = 600.0) -> dict:
    """Extrahiert Dolby-Vision-L1-Helligkeitsmetadaten (Max/Avg/Min Nits pro Frame/Szene)
    für die interaktive Kurven-Ansicht.

    Standardmäßig werden bis zu `duration_limit` Sekunden analysiert (für schnelle UI-Antwort).
    """
    import json
    import tempfile

    if not available():
        return {"ok": False, "error": "dovi_tool ist nicht verfügbar."}
    if (source_codec or "").lower() not in ("hevc", "h265", "av1"):
        return {"ok": False, "error": f"Codec {source_codec} wird von dovi_tool nicht unterstützt."}

    with tempfile.TemporaryDirectory(prefix="vc_dvl1_") as tmp:
        tmp_dir = Path(tmp)
        rpu = tmp_dir / "rpu.bin"
        exp_json = tmp_dir / "rpu_export.json"
        if not _extract_rpu(source, "av1" if source_codec == "av1" else "hevc",
                            rpu, mode=0, duration_limit=duration_limit):
            return {"ok": False, "error": "RPU konnte nicht aus der Quelle extrahiert werden."}

        res = subprocess.run(
            [config.DOVI_TOOL, "export", "-i", str(rpu), "-d", f"all={exp_json}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        if res.returncode != 0 or not exp_json.is_file():
            return {"ok": False, "error": "RPU-JSON-Export fehlgeschlagen."}

        try:
            raw = json.loads(exp_json.read_text(encoding="utf-8", errors="replace"))
        except ValueError as e:
            return {"ok": False, "error": f"Ungültiges RPU-JSON: {e}"}

    if not isinstance(raw, list) or not raw:
        return {"ok": False, "error": "Keine RPU-Frames gefunden."}

    max_list: list[float] = []
    avg_list: list[float] = []
    min_list: list[float] = []
    scene_cuts: list[int] = []
    profile = 0

    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        hdr = entry.get("header") or {}
        if not profile and hdr.get("guessed_profile"):
            profile = int(hdr.get("guessed_profile") or 0)
        vdr_dm = entry.get("vdr_dm_data") or {}
        if vdr_dm.get("scene_refresh_flag") == 1:
            scene_cuts.append(idx)
        # L1 kann direkt unter cmv29_metadata / cmv40_metadata oder blocks liegen
        blocks = []
        for container_key in ("cmv29_metadata", "cmv40_metadata"):
            cm = vdr_dm.get(container_key)
            if isinstance(cm, dict) and isinstance(cm.get("ext_metadata_blocks"), list):
                blocks.extend(cm["ext_metadata_blocks"])
        if not blocks and isinstance(vdr_dm.get("ext_metadata_blocks"), list):
            blocks = vdr_dm["ext_metadata_blocks"]

        l1 = None
        for b in blocks:
            if isinstance(b, dict) and "Level1" in b:
                l1 = b["Level1"]
                break
        if l1:
            max_pq = float(l1.get("max_pq", 0) or 0)
            avg_pq = float(l1.get("avg_pq", 0) or 0)
            min_pq = float(l1.get("min_pq", 0) or 0)
            max_list.append(_pq_to_nits(max_pq))
            avg_list.append(_pq_to_nits(avg_pq))
            min_list.append(_pq_to_nits(min_pq))

    total_frames = len(max_list)
    if total_frames == 0:
        return {"ok": False, "error": "Keine Level-1-Helligkeitsdaten in der RPU enthalten."}

    step = max(1, total_frames // max(20, int(max_points)))
    sampled_max = [round(max(max_list[i:i + step]), 1) for i in range(0, total_frames, step)]
    sampled_avg = [round(sum(avg_list[i:i + step]) / len(avg_list[i:i + step]), 1) for i in range(0, total_frames, step)]
    sampled_min = [round(min(min_list[i:i + step]), 3) for i in range(0, total_frames, step)]

    return {
        "ok": True,
        "profile": profile,
        "frames_analyzed": total_frames,
        "scenes": len(scene_cuts),
        "peak_nits": round(max(max_list), 1),
        "max_avg_nits": round(max(avg_list), 1),
        "mean_avg_nits": round(sum(avg_list) / total_frames, 1),
        "series_max": sampled_max,
        "series_avg": sampled_avg,
        "series_min": sampled_min,
    }

