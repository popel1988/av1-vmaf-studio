"""Lokale Dolby-Vision-RPU-Datenbank (RPU-Tresor) & RPU-Metadaten-Helfer.

Speichert extrahierte oder aus HDR10+ generierte `.rpu.bin`-Dateien in
`config.RPU_VAULT_DIR` samt strukturierter Metadaten-Indexdatei (`index.json`).
Unterstützt Suche, Filterung, Spalteninformationen (Profil, CM-Version, Frames,
FPS, Peak-Nits, Mastering-Display, MaxCLL/MaxFALL, Herkunft) sowie Export/Löschen.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import config, dolby_vision as dv, ffmpeg_utils as ff, hdr10plus

logger = logging.getLogger("vcompress.rpu_vault")

_VAULT_LOCK = threading.Lock()
StatusCb = Optional[Callable[[str], None]]


def _vault_dir() -> Path:
    d = config.RPU_VAULT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return _vault_dir() / "index.json"


def _load_index() -> list[dict]:
    p = _index_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_index(entries: list[dict]) -> None:
    p = _index_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _clean_slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip()).strip("_.")
    return (s[:90] or "rpu")


_RE_FRAMES = re.compile(r"Frames\s*:\s*(\d+)", re.IGNORECASE)
_RE_PROFILE = re.compile(r"Profile\s*:\s*(\d+)", re.IGNORECASE)
_RE_SUBPROFILE = re.compile(r"(?:Subprofile|EL\s*type|Type)\s*:\s*(FEL|MEL)", re.IGNORECASE)
_RE_CM_VER = re.compile(r"CM\s*version\s*:\s*([^\r\n]+)", re.IGNORECASE)
_RE_DM_VER = re.compile(r"DM\s*version\s*:\s*([^\r\n]+)", re.IGNORECASE)
_RE_MASTERING = re.compile(r"RPU\s*mastering\s*display\s*:\s*([^\r\n]+)", re.IGNORECASE)
_RE_CLL = re.compile(r"RPU\s*content\s*light\s*level[^\r\n]*:\s*([^\r\n]+)", re.IGNORECASE)
_RE_L6 = re.compile(r"L6\s*metadata[^\r\n]*:\s*([^\r\n]+)", re.IGNORECASE)
_RE_MAXCLL_NUM = re.compile(r"MaxCLL\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_RE_MAXFALL_NUM = re.compile(r"MaxFALL\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_RE_SCENES = re.compile(r"Scene\s*cuts\s*:\s*(\d+)", re.IGNORECASE)


def parse_dovi_summary_text(summary_text: str) -> dict:
    """Parst die Textausgabe von `dovi_tool info -s <rpu.bin>` robust."""
    txt = summary_text or ""
    out: dict = {
        "frames": 0,
        "profile": 0,
        "el_type": "",
        "cm_version": "",
        "dm_version": "",
        "rpu_mastering_display": "",
        "rpu_content_light_level": "",
        "max_cll_nits": 0.0,
        "max_fall_nits": 0.0,
        "scene_cuts": 0,
        "raw_summary": txt.strip(),
    }
    m = _RE_FRAMES.search(txt)
    if m:
        out["frames"] = int(m.group(1))
    m = _RE_PROFILE.search(txt)
    if m:
        out["profile"] = int(m.group(1))
    m = _RE_SUBPROFILE.search(txt)
    if m:
        out["el_type"] = m.group(1).upper()
    elif "FEL" in txt:
        out["el_type"] = "FEL"
    elif "MEL" in txt:
        out["el_type"] = "MEL"
    m = _RE_CM_VER.search(txt)
    if m:
        raw_cm = m.group(1).strip()
        if "4.0" in raw_cm or "v4" in raw_cm.lower() or "40" in raw_cm:
            out["cm_version"] = "CM v4.0"
        elif "2.9" in raw_cm or "v2" in raw_cm.lower() or "29" in raw_cm:
            out["cm_version"] = "CM v2.9"
        else:
            out["cm_version"] = raw_cm
    m = _RE_DM_VER.search(txt)
    if m:
        out["dm_version"] = m.group(1).strip()
        if not out["cm_version"]:
            if "4.0" in out["dm_version"] or "v4" in out["dm_version"].lower():
                out["cm_version"] = "CM v4.0"
            elif "2.9" in out["dm_version"] or "v2" in out["dm_version"].lower():
                out["cm_version"] = "CM v2.9"
    m = _RE_MASTERING.search(txt)
    if m:
        out["rpu_mastering_display"] = m.group(1).strip()
    m = _RE_CLL.search(txt) or _RE_L6.search(txt)
    if m:
        out["rpu_content_light_level"] = m.group(1).strip()
    mc = _RE_MAXCLL_NUM.search(txt)
    if mc:
        out["max_cll_nits"] = round(float(mc.group(1)), 2)
    mf = _RE_MAXFALL_NUM.search(txt)
    if mf:
        out["max_fall_nits"] = round(float(mf.group(1)), 2)
    ms = _RE_SCENES.search(txt)
    if ms:
        out["scene_cuts"] = int(ms.group(1))
    return out


def inspect_rpu_file(rpu_path: Path) -> dict:
    """Liest detaillierte Metadaten aus einer `.rpu.bin`-Datei (`dovi_tool info` + JSON-Fallback)."""
    info: dict = {
        "rpu_size_bytes": rpu_path.stat().st_size if rpu_path.is_file() else 0,
        "frames": 0,
        "profile": 0,
        "el_type": "",
        "cm_version": "",
        "rpu_mastering_display": "",
        "rpu_content_light_level": "",
        "max_cll_nits": 0.0,
        "max_fall_nits": 0.0,
        "peak_l1_nits": 0.0,
        "avg_l1_nits": 0.0,
        "min_l1_nits": 0.0,
        "scene_cuts": 0,
        "l5_offsets": None,
        "scene_cut_frames": [],
    }
    if not rpu_path.is_file() or not dv.available():
        return info

    # 1. Schnelle Zusammenfassung via `dovi_tool info --summary`
    try:
        res = subprocess.run(
            [config.DOVI_TOOL, "info", "-i", str(rpu_path), "-s"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=45, check=False,
        )
        if res.returncode != 0 or not (res.stdout or "").strip():
            res = subprocess.run(
                [config.DOVI_TOOL, "info", str(rpu_path), "-s"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=45, check=False,
            )
        if res.returncode == 0 and (res.stdout or "").strip():
            parsed = parse_dovi_summary_text(res.stdout)
            info.update({k: v for k, v in parsed.items() if v or k not in info})
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("dovi_tool info -s Ausnahme: %s", e)

    # 2. JSON-Export für präzise L1/L5/L6/Szenenschnitt-Werte
    with tempfile.TemporaryDirectory(prefix="vc_rpu_insp_") as tmp:
        exp_json = Path(tmp) / "rpu_export.json"
        try:
            res_exp = subprocess.run(
                [config.DOVI_TOOL, "export", "-i", str(rpu_path), "-d", f"all={exp_json}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=90, check=False,
            )
            if res_exp.returncode == 0 and exp_json.is_file():
                raw = json.loads(exp_json.read_text(encoding="utf-8", errors="replace"))
                if isinstance(raw, list) and raw:
                    _enrich_from_exported_rpu(raw, info)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            logger.debug("dovi_tool export JSON Ausnahme: %s", e)

    return info


def _enrich_from_exported_rpu(raw: list, info: dict) -> None:
    """Extrahiert L1-Nits, L5-Active-Area, L6, CM-Version und Szenenschnitte aus dem RPU-JSON."""
    info["frames"] = len(raw)
    max_nits: list[float] = []
    avg_nits: list[float] = []
    min_nits: list[float] = []
    scene_frames: list[int] = []
    has_cm40 = False
    has_cm29 = False

    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        hdr = entry.get("header") or {}
        if not info.get("profile") and hdr.get("guessed_profile"):
            info["profile"] = int(hdr.get("guessed_profile") or 0)
        subp = str(hdr.get("el_type") or "").upper()
        if not info.get("el_type") and subp in ("FEL", "MEL"):
            info["el_type"] = subp

        vdr_dm = entry.get("vdr_dm_data") or {}
        if vdr_dm.get("scene_refresh_flag") == 1:
            scene_frames.append(idx)

        # Mastering Luminance aus vdr_dm_data (PQ oder direkt)
        if not info.get("rpu_mastering_display"):
            min_pq = vdr_dm.get("min_pq")
            max_pq = vdr_dm.get("max_pq")
            if min_pq is not None and max_pq is not None:
                lo = round(dv._pq_to_nits(float(min_pq)), 4)
                hi = int(round(dv._pq_to_nits(float(max_pq))))
                if hi > 0:
                    info["rpu_mastering_display"] = f"{lo:.4f}/{hi} nits"

        blocks = []
        if isinstance(vdr_dm.get("cmv40_metadata"), dict):
            has_cm40 = True
            blocks.extend(vdr_dm["cmv40_metadata"].get("ext_metadata_blocks") or [])
        if isinstance(vdr_dm.get("cmv29_metadata"), dict):
            has_cm29 = True
            blocks.extend(vdr_dm["cmv29_metadata"].get("ext_metadata_blocks") or [])
        if not blocks and isinstance(vdr_dm.get("ext_metadata_blocks"), list):
            blocks = vdr_dm["ext_metadata_blocks"]

        for b in blocks:
            if not isinstance(b, dict):
                continue
            if "Level1" in b:
                l1 = b["Level1"] or {}
                max_nits.append(dv._pq_to_nits(float(l1.get("max_pq", 0) or 0)))
                avg_nits.append(dv._pq_to_nits(float(l1.get("avg_pq", 0) or 0)))
                min_nits.append(dv._pq_to_nits(float(l1.get("min_pq", 0) or 0)))
            elif "Level5" in b and info.get("l5_offsets") is None:
                l5 = b["Level5"] or {}
                info["l5_offsets"] = {
                    "left": int(l5.get("active_area_left_offset", 0) or 0),
                    "right": int(l5.get("active_area_right_offset", 0) or 0),
                    "top": int(l5.get("active_area_top_offset", 0) or 0),
                    "bottom": int(l5.get("active_area_bottom_offset", 0) or 0),
                }
            elif "Level6" in b and not info.get("max_cll_nits"):
                l6 = b["Level6"] or {}
                mc = float(l6.get("max_content_light_level", 0) or 0)
                mf = float(l6.get("max_frame_average_light_level", 0) or 0)
                if mc > 0 or mf > 0:
                    info["max_cll_nits"] = mc
                    info["max_fall_nits"] = mf
                    if not info.get("rpu_content_light_level"):
                        info["rpu_content_light_level"] = f"MaxCLL: {mc:.2f} nits, MaxFALL: {mf:.2f} nits"

    if not info.get("cm_version"):
        if has_cm40:
            info["cm_version"] = "CM v4.0"
        elif has_cm29:
            info["cm_version"] = "CM v2.9"

    if max_nits:
        peak = round(max(max_nits), 1)
        mean_avg = round(sum(avg_nits) / len(avg_nits), 1)
        floor = round(min(min_nits), 4)
        info["peak_l1_nits"] = peak
        info["avg_l1_nits"] = mean_avg
        info["min_l1_nits"] = floor
        if not info.get("max_cll_nits"):
            info["max_cll_nits"] = peak
        if not info.get("max_fall_nits"):
            info["max_fall_nits"] = round(max(avg_nits), 1)
        if not info.get("rpu_content_light_level"):
            info["rpu_content_light_level"] = (
                f"MaxCLL: {info['max_cll_nits']:.2f} nits, MaxFALL: {info['max_fall_nits']:.2f} nits"
            )

    info["scene_cuts"] = len(scene_frames)
    info["scene_cut_frames"] = scene_frames[:500]


def generate_rpu_from_hdr10plus_json(
    hdr10plus_json: Path,
    out_rpu: Path,
    *,
    peak_nits: int = 1000,
    min_nits: float = 0.005,
    primaries: str = "bt2020",
    max_cll: int = 0,
    max_fall: int = 0,
) -> tuple[bool, str]:
    """Erzeugt eine Dolby-Vision-Profil-8.1-RPU aus einer `hdr10plus_tool`-JSON-Datei."""
    if not dv.available():
        return False, "dovi_tool ist nicht verfügbar."
    if not hdr10plus_json.is_file() or hdr10plus_json.stat().st_size <= 2:
        return False, "HDR10+-JSON ist leer oder fehlt."

    # Ermittle Peak/Avg aus der HDR10+-JSON falls MaxCLL/MaxFALL nicht übergeben
    if max_cll <= 0 or max_fall <= 0:
        try:
            raw = json.loads(hdr10plus_json.read_text(encoding="utf-8", errors="replace"))
            frames = raw.get("SceneInfo") if isinstance(raw, dict) else None
            if isinstance(frames, list) and frames:
                maxscl_vals = []
                avg_vals = []
                for fr in frames:
                    if not isinstance(fr, dict):
                        continue
                    ms = fr.get("LuminanceParameters", {}).get("MaxScl") or fr.get("MaxScl")
                    av = fr.get("LuminanceParameters", {}).get("AverageRgb") or fr.get("AverageRgb")
                    if isinstance(ms, list) and ms:
                        # In HDR10+ JSON sind MaxScl-Werte in 0.1 Nits Einheiten codiert
                        v = max(float(x or 0) for x in ms)
                        maxscl_vals.append(v / 10.0 if v > 10000 else v)
                    if av is not None:
                        av_f = float(av or 0)
                        avg_vals.append(av_f / 10.0 if av_f > 10000 else av_f)
                if maxscl_vals and max_cll <= 0:
                    max_cll = int(round(min(10000.0, max(maxscl_vals))))
                if avg_vals and max_fall <= 0:
                    max_fall = int(round(min(10000.0, max(avg_vals))))
        except (OSError, ValueError, TypeError):
            pass

    target_md = "1000-nits"
    if peak_nits >= 3500:
        target_md = "4000-nits"
    elif peak_nits >= 1800:
        target_md = "2000-nits"

    gen_cfg = {
        "cm_version": "V29",
        "length": 0,
        "profile": "8.1",
        "source_min_pq": None,
        "source_max_pq": None,
        "level6": {
            "max_display_mastering_luminance": int(4000 if peak_nits >= 3500 else (2000 if peak_nits >= 1800 else 1000)),
            "min_display_mastering_luminance": int(round(max(0.0001, min_nits) * 10000)),
            "max_content_light_level": max(0, int(max_cll)),
            "max_frame_average_light_level": max(0, int(max_fall)),
        },
    }

    with tempfile.TemporaryDirectory(prefix="vc_dvgen_") as tmp:
        cfg_file = Path(tmp) / "generate.json"
        cfg_file.write_text(json.dumps(gen_cfg, indent=2), encoding="utf-8")

        # Versuch 1: dovi_tool generate -j <cfg> --hdr10plus-json <hdr10plus.json> -o <out_rpu>
        cmds = [
            [
                config.DOVI_TOOL, "generate",
                "-j", str(cfg_file),
                "--hdr10plus-json", str(hdr10plus_json),
                "-o", str(out_rpu),
            ],
            [
                config.DOVI_TOOL, "generate",
                "--hdr10plus-json", str(hdr10plus_json),
                "--profile", "8.1",
                "--dm-version", "29",
                "--target-nits", target_md,
                "-o", str(out_rpu),
            ],
            [
                config.DOVI_TOOL, "generate",
                "--hdr10plus-json", str(hdr10plus_json),
                "-p", "8.1",
                "-o", str(out_rpu),
            ],
        ]
        last_err = ""
        for cmd in cmds:
            out_rpu.unlink(missing_ok=True)
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180, check=False,
            )
            if res.returncode == 0 and out_rpu.is_file() and out_rpu.stat().st_size > 0:
                return True, ""
            last_err = (res.stderr or res.stdout or "").strip()[-600:]
        return False, f"dovi_tool generate fehlgeschlagen: {last_err or 'unbekannter Fehler'}"


def list_entries(query: str = "") -> list[dict]:
    """Liefert alle gespeicherten RPU-Einträge im Tresor (neueste zuerst), optional gefiltert."""
    with _VAULT_LOCK:
        items = _load_index()
        valid: list[dict] = []
        changed = False
        vdir = _vault_dir()
        for it in items:
            if not isinstance(it, dict):
                changed = True
                continue
            fname = str(it.get("filename") or "")
            fpath = vdir / fname if fname else Path("")
            exists = bool(fname and fpath.is_file())
            it["exists"] = exists
            if exists and not it.get("rpu_size_bytes"):
                it["rpu_size_bytes"] = fpath.stat().st_size
            valid.append(it)
        if changed:
            _save_index(valid)

    valid.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    q = (query or "").strip().lower()
    if q:
        valid = [
            it for it in valid
            if q in str(it.get("title") or "").lower()
            or q in str(it.get("source_name") or "").lower()
            or q in str(it.get("origin") or "").lower()
            or q in str(it.get("cm_version") or "").lower()
        ]
    return valid


def get_entry(entry_id: str) -> Optional[dict]:
    with _VAULT_LOCK:
        for it in _load_index():
            if str(it.get("id")) == str(entry_id):
                fname = str(it.get("filename") or "")
                p = _vault_dir() / fname if fname else None
                it["rpu_path"] = str(p) if (p and p.is_file()) else ""
                return it
    return None


def delete_entry(entry_id: str) -> bool:
    with _VAULT_LOCK:
        items = _load_index()
        kept: list[dict] = []
        removed: Optional[dict] = None
        for it in items:
            if str(it.get("id")) == str(entry_id):
                removed = it
            else:
                kept.append(it)
        if removed is None:
            return False
        _save_index(kept)
    fname = str(removed.get("filename") or "")
    if fname:
        (_vault_dir() / fname).unlink(missing_ok=True)
    orig_fname = str(removed.get("original_rpu_filename") or "")
    if orig_fname:
        (_vault_dir() / orig_fname).unlink(missing_ok=True)
    return True


def extract_to_vault(
    source_path: Path,
    *,
    title: str = "",
    convert_to_81: bool = True,
    keep_original_rpu: bool = False,
    allow_hdr10plus_conversion: bool = True,
    status: StatusCb = None,
) -> dict:
    """Extrahiert eine RPU aus einer DV- oder HDR10+-Videodatei und legt sie im Tresor ab."""
    if not source_path.is_file():
        return {"ok": False, "error": "Quelldatei nicht gefunden."}
    if not dv.available():
        return {"ok": False, "error": "dovi_tool ist nicht verfügbar."}

    info = ff.probe(source_path)
    if info is None:
        return {"ok": False, "error": "Quelldatei konnte nicht mit ffprobe gelesen werden."}

    entry_id = uuid.uuid4().hex[:12]
    clean_title = (title or source_path.stem).strip()
    slug = _clean_slug(clean_title)
    vdir = _vault_dir()
    dest_rpu = vdir / f"{slug}_{entry_id}.rpu.bin"
    orig_rpu_name = ""
    origin = "dolby_vision"
    origin_label = "Dolby Vision"

    with tempfile.TemporaryDirectory(prefix="vc_vault_") as tmp:
        tmp_dir = Path(tmp)
        if info.dolby_vision:
            src_codec = "av1" if (info.codec or "").lower() == "av1" else "hevc"
            orig_profile = int(info.dv_profile or 0)
            origin_label = f"Dolby Vision P{orig_profile}" if orig_profile else "Dolby Vision"

            if keep_original_rpu and orig_profile == 7 and convert_to_81:
                if status:
                    status(f"Extrahiere Original-RPU (Profil {orig_profile}) aus {source_path.name} …")
                orig_rpu_path = vdir / f"{slug}_{entry_id}.orig_p{orig_profile}.rpu.bin"
                if dv._extract_rpu(source_path, src_codec, orig_rpu_path, mode=0):
                    orig_rpu_name = orig_rpu_path.name

            mode = 2 if (convert_to_81 and orig_profile == 7) else 0
            if status:
                conv_note = " (Konvertierung P7 → P8.1)" if mode == 2 else ""
                status(f"Extrahiere RPU{conv_note} aus {source_path.name} …")
            if not dv._extract_rpu(source_path, src_codec, dest_rpu, mode=mode):
                return {"ok": False, "error": "RPU-Extraktion aus Dolby-Vision-Quelle fehlgeschlagen."}
            if mode == 2:
                origin_label = f"DV P{orig_profile} → P8.1"

        elif info.hdr10_plus and allow_hdr10plus_conversion:
            if not hdr10plus.available():
                return {"ok": False, "error": "hdr10plus_tool ist nicht verfügbar."}
            if status:
                status(f"Extrahiere HDR10+-Metadaten aus {source_path.name} …")
            h10_json = tmp_dir / "hdr10plus.json"
            src_es = tmp_dir / "source.hevc"
            if not hdr10plus._extract_json(source_path, h10_json, src_es):
                return {"ok": False, "error": "Keine gültigen HDR10+-Szenendaten im Stream gefunden."}
            if status:
                status("Generiere Dolby Vision Profil 8.1 RPU aus HDR10+ …")
            peak_nits = int(round(info.master_max or 1000))
            min_nits = float(info.master_min if info.master_min is not None else 0.005)
            ok, err = generate_rpu_from_hdr10plus_json(
                h10_json, dest_rpu,
                peak_nits=peak_nits,
                min_nits=min_nits,
                max_cll=int(info.max_cll or 0),
                max_fall=int(info.max_fall or 0),
            )
            if not ok:
                return {"ok": False, "error": err}
            origin = "hdr10plus_converted"
            origin_label = "Generiert aus HDR10+"
        else:
            return {
                "ok": False,
                "error": "Die Quelldatei enthält weder Dolby Vision noch HDR10+.",
            }

    if status:
        status("Analysiere RPU-Metadaten (Nits, CM-Version, Szenen) …")
    rpu_meta = inspect_rpu_file(dest_rpu)
    # Scene-Cut-Liste nicht riesig im Hauptindex speichern (nur Statistik + erste 120 für Schnellabgleich)
    scene_cut_preview = (rpu_meta.get("scene_cut_frames") or [])[:120]

    record = {
        "id": entry_id,
        "title": clean_title,
        "source_name": source_path.name,
        "source_rel": config.rel_to_media(source_path) or source_path.name,
        "filename": dest_rpu.name,
        "original_rpu_filename": orig_rpu_name,
        "origin": origin,
        "origin_label": origin_label,
        "source_dv_profile": int(info.dv_profile or 0),
        "profile": int(rpu_meta.get("profile") or (8 if origin == "hdr10plus_converted" else info.dv_profile or 8)),
        "el_type": rpu_meta.get("el_type") or "",
        "cm_version": rpu_meta.get("cm_version") or "CM v2.9",
        "frames": int(rpu_meta.get("frames") or 0),
        "fps": round(float(info.fps or 0.0), 3),
        "width": int(info.width or 0),
        "height": int(info.height or 0),
        "duration": round(float(info.duration or 0.0), 3),
        "rpu_size_bytes": int(rpu_meta.get("rpu_size_bytes") or dest_rpu.stat().st_size),
        "peak_l1_nits": float(rpu_meta.get("peak_l1_nits") or 0.0),
        "avg_l1_nits": float(rpu_meta.get("avg_l1_nits") or 0.0),
        "max_cll_nits": float(rpu_meta.get("max_cll_nits") or info.max_cll or 0.0),
        "max_fall_nits": float(rpu_meta.get("max_fall_nits") or info.max_fall or 0.0),
        "rpu_mastering_display": rpu_meta.get("rpu_mastering_display") or (
            f"{(info.master_min or 0.005):.4f}/{int(round(info.master_max))} nits" if info.master_max else ""
        ),
        "rpu_content_light_level": rpu_meta.get("rpu_content_light_level") or "",
        "scene_cuts": int(rpu_meta.get("scene_cuts") or 0),
        "scene_cut_preview": scene_cut_preview,
        "l5_offsets": rpu_meta.get("l5_offsets"),
        "created_at": round(time.time(), 2),
    }

    with _VAULT_LOCK:
        entries = _load_index()
        entries.append(record)
        _save_index(entries)

    return {"ok": True, "entry": record}


def batch_extract_to_vault(
    paths: list[Path],
    *,
    convert_to_81: bool = True,
    keep_original_rpu: bool = False,
    status: StatusCb = None,
) -> dict:
    """Extrahiert RPUs aus beliebig vielen Filmen hintereinander in den RPU-Tresor."""
    results: list[dict] = []
    ok_count = 0
    for idx, p in enumerate(paths, start=1):
        if status:
            status(f"[{idx}/{len(paths)}] Verarbeite {p.name} …")
        res = extract_to_vault(
            p,
            convert_to_81=convert_to_81,
            keep_original_rpu=keep_original_rpu,
            allow_hdr10plus_conversion=True,
            status=status,
        )
        if res.get("ok"):
            ok_count += 1
            results.append({"path": p.name, "ok": True, "entry": res["entry"]})
        else:
            results.append({"path": p.name, "ok": False, "error": res.get("error", "Fehler")})
    return {
        "ok": ok_count > 0,
        "processed": len(paths),
        "succeeded": ok_count,
        "failed": len(paths) - ok_count,
        "results": results,
    }
