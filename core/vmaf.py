"""VMAF-Analyse-Pipeline mit flexiblen Testwerten, Bitrate-Modus und Screenshots."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config
from . import provenance as provenance_api
from . import storage_policy
from . import ffmpeg_utils as ff
from .encoder import (
    EncodeRunner, build_encode_cmd, reference_tonemap_setup,
)
from .ffmpeg_utils import VideoInfo

logger = logging.getLogger("vcompress.vmaf")


def _run_logged(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    # errors="replace": FFmpeg gibt Quell-Metadaten teils in Latin-1 aus –
    # ohne das würde das UTF-8-Decoding der stderr-Ausgabe abstürzen.
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=False)
    if res.returncode != 0:
        logger.error("%s fehlgeschlagen (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                     label, res.returncode, " ".join(cmd), (res.stderr or "")[-3000:])
    return res


@dataclass
class VmafOptions:
    """Konfiguration für einen VMAF-Lauf."""
    rate_mode: str = "cq"
    test_values: list = field(default_factory=lambda: [20, 24, 28, 32])
    clip_seconds: int = 30
    samples: int = 1  # Anzahl Stichproben-Clips (1 = nur Mitte)
    generate_screenshots: bool = True
    item_id: str = ""
    session_name: str = ""  # lesbarer Ordnername für Previews/Archiv
    source_title: str = ""  # Anzeigename der Quelle (für Archiv-Liste)
    source_path: str = ""   # Quelldatei (für spätere Neu-Analyse)
    params: dict = field(default_factory=dict)  # Job-Settings-Snapshot
    # Zusätzliche zu vergleichende Encoder als (plattform, codec)-Paare.
    # Der Basis-Encoder (Parameter platform/codec) wird immer mitgetestet.
    encoders: list = field(default_factory=list)
    # >0: Ziel-VMAF (Super-Tool) – dann wird der effizienteste Wert mit
    # VMAF >= Ziel empfohlen statt des Standard-Sweetspots.
    target_vmaf: float = 0.0
    # 10-bit-Test-Encodes gegen Banding. Die Messung selbst ist immer VMAF-NEG.
    anime: bool = False
    # False: nur die eingetragenen Testwerte (VMAF-Tool, Encoder-Bench).
    # True: ein Zwischenwert zwischen Treffer und Fehlschlag (Ziel-Encode).
    refine_midpoint: bool = True
    # Gesetzte Stichproben [(start, sekunden)]. Leer = gleichmäßig über den Film.
    sample_starts: list = field(default_factory=list)
    sample_windows: list = field(default_factory=list)
    # Nur Bitraten-Modus. CPU: zwei FFmpeg-Durchläufe. NVIDIA: Multipass.
    two_pass: bool = False
    # Filmzeit in Sekunden, ab der keine Testszenen mehr liegen. 0 = automatisch.
    credits_at: float = 0.0
    # True: Testclips encodieren, libvmaf auslassen. Vergleich im A/B-Player.
    visual_only: bool = False
    # Standbilder je Stichprobe für SSIMULACRA2 (1 = nur Mitte, bis 7).
    ssimulacra2_frames: int = 3
    # Alte Ein-Achsen-Varianten. Neue Vergleiche nutzen rows.
    variants: list = field(default_factory=list)
    # Volle Zusatzzeilen: Plattform, Codec, Speed, B-Frames, AQ, Keyframe.
    # Optional je Zeile: rate_mode, test_values, two_pass. Fehlen sie, gilt
    # die gemeinsame Steuerung des Laufs.
    rows: list = field(default_factory=list)


# Anzeigenamen je Codec (plattformabhängig verfeinert in _codec_disp)
_CODEC_NAMES = {"av1": "AV1", "hevc": "HEVC", "h264": "H.264"}


def _codec_disp(platform: str, codec: str) -> str:
    if platform == "cpu":
        return {"av1": "SVT-AV1", "hevc": "x265", "h264": "x264"}.get(codec, codec.upper())
    return _CODEC_NAMES.get(codec, codec.upper())


_BF_TAGS = {
    "auto": "Automatisch", "off": "Aus", "short": "Kurz",
    "medium": "Mittel", "deep": "Tief",
}


_KEYINT_TAGS = {0: "Keyframe automatisch", 2: "2 s", 5: "5 s", 10: "10 s"}


_TUNE_TAGS = {"auto": "Tune automatisch", "off": "Tune aus", "hq": "HQ", "uhq": "UHQ"}


def _rate_override(raw: dict) -> tuple[str, tuple | None, bool | None]:
    """Eigene Steuerung einer Vergleichszeile.

    Leeres Tupel heißt: Modus ist gesetzt, aber keine Testwerte. None heißt:
    die Zeile übernimmt Modus, Werte und Zwei-Pass des Laufs.
    """
    mode = str(raw.get("rate_mode") or "")
    if mode not in ("cq", "bitrate", "abr"):
        return "", None, None
    vals: list[int] = []
    for v in list(raw.get("test_values") or [])[:4]:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            vals.append(n)
    two = bool(raw.get("two_pass")) if mode in ("bitrate", "abr") else False
    return mode, tuple(vals), two


def _collect_runs(platform: str, codec: str, speed: str, b_frames: str,
                  encoders: list, variants: list,
                  aq_strength: int = 8, keyint_sec: int = 0,
                  nvenc_tune: str = "auto",
                  rows: list | None = None) -> list[tuple]:
    """Basiszeile, alte Zusatz-Encoder und volle Vergleichszeilen.

    Rückgabe: (plattform, codec, speed, b_frames, aq, keyint, tune,
    anzeige-suffix, rate_mode, testwerte, two_pass).
    rate_mode "" und testwerte None übernehmen die gemeinsame Steuerung.
    """
    runs: list[tuple] = []
    base_aq = max(1, min(15, int(aq_strength or 8)))
    base_ki = ff.normalize_keyint_sec(keyint_sec)
    base_tune = ff.normalize_nvenc_tune(nvenc_tune)

    def add(p: str, c: str, sp: str, bf: str, aq: int, ki: int, tune: str,
            rate_mode: str = "", values: tuple | None = None,
            two_pass: bool | None = None) -> None:
        enc = ff.encoder_name(p, c)
        native = ff.alias_to_native(enc, sp) if enc else sp
        mode = ff.normalize_b_frames(bf)
        strength = max(1, min(15, int(aq or 8)))
        gap = ff.normalize_keyint_sec(ki)
        mode_tune = ff.normalize_nvenc_tune(tune)
        rm = rate_mode if rate_mode in ("cq", "bitrate", "abr") else ""
        key = (p, c, native, mode, strength, gap, mode_tune, rm, values, two_pass)
        if any((*r[:7], r[8], r[9], r[10]) == key for r in runs):
            return
        if not ff.encoder_available(p, c):
            logger.warning("Vergleichs-Encoder übersprungen (nicht verfügbar): %s/%s", p, c)
            return
        runs.append((*key[:7], "", rm, values, two_pass))

    if ff.encoder_available(platform, codec):
        add(platform, codec, speed, b_frames, base_aq, base_ki, base_tune)
        base_enc = ff.encoder_name(platform, codec)
        for raw in list(variants or [])[:12]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "")
            val = str(raw.get("value") or "")
            if kind == "b_frames" and "nvenc" in base_enc:
                add(platform, codec, speed, val, base_aq, base_ki, base_tune)
            elif kind == "speed" and val:
                add(platform, codec, val, b_frames, base_aq, base_ki, base_tune)
    for p, c in encoders:
        add(p, c, speed, b_frames, base_aq, base_ki, base_tune)
    for raw in list(rows or [])[:6]:
        if not isinstance(raw, dict):
            continue
        rm, vals, two = _rate_override(raw)
        add(
            str(raw.get("platform") or ""),
            str(raw.get("codec") or ""),
            str(raw.get("encoder_speed") or raw.get("speed") or speed),
            str(raw.get("b_frames") or b_frames),
            int(raw.get("aq_strength") or base_aq),
            int(raw.get("keyint_sec") or 0),
            str(raw.get("nvenc_tune") or base_tune),
            rate_mode=rm, values=vals, two_pass=two,
        )
    if not runs:
        runs.append((
            platform, codec, speed, ff.normalize_b_frames(b_frames),
            base_aq, base_ki, base_tune, "", "", None, None,
        ))

    tagged = []
    for p, c, sp, bf, aq, ki, tune, _tag, rm, vals, two in runs:
        peers = [r for r in runs if r[0] == p and r[1] == c]
        parts = []
        if len({r[2] for r in peers}) > 1:
            parts.append(sp)
        if len({r[3] for r in peers}) > 1:
            parts.append(_BF_TAGS.get(bf, bf))
        if len({r[4] for r in peers}) > 1:
            parts.append(f"AQ {aq}")
        if len({r[5] for r in peers}) > 1:
            parts.append(_KEYINT_TAGS.get(ki, str(ki)))
        if len({r[6] for r in peers}) > 1:
            parts.append(_TUNE_TAGS.get(tune, tune))
        tagged.append((p, c, sp, bf, aq, ki, tune, " · ".join(parts), rm, vals, two))
    return tagged


# 1%-Low-Abstand zum Ziel-Mittel. Default 6 (einstellbar unter Einstellungen):
# bei Ziel 94 muss das Low ≥ 88 bleiben („gut“, nicht nur „ok“).
# 4 wäre streng (Action/Korn/GPU-Encoder fliegen oft raus), 8 großzügig
# (Low 86 bei Ziel 94 ist schon eine Qualitätsstufe darunter). 0 = Floor aus.
VMAF_P1_GAP = 6.0


def quality_score(mean: float, p1: float = 0.0, hmean: float = 0.0) -> float:
    """Sichtqualität für Empfehlungen: Mittel zählt, 1%-Low darf nicht einbrechen.

    55 % Mittel + 35 % 1%-Low + 10 % harmonisches Mittel. Der Slider „Ziel-VMAF“
    bleibt der Mittelwert – dieser Score ersetzt ihn nicht, straft aber
    Ausreißer, die das Mittel schönrechnen.
    """
    m = float(mean or 0)
    floor = float(p1) if p1 else m
    h = float(hmean) if hmean else m
    return 0.55 * m + 0.35 * floor + 0.10 * h


# Ungefährer CQ/CRF-Wert je Encoder für ~VMAF 95 ("Sweet Spot"). Dient nur als
# Referenz, um beim Codec-Vergleich die CQ-Testwerte so zu verschieben, dass
# alle Encoder im vergleichbaren Qualitätsbereich landen (CQ-Skalen/Effizienz
# unterscheiden sich je Codec). Werte sind Näherungen (Encoder-/Version-abhängig).
_CQ_SWEETSPOT = {
    ("cpu", "hevc"): 23, ("cpu", "av1"): 30, ("cpu", "h264"): 21,
    ("nvidia", "av1"): 32, ("nvidia", "hevc"): 26, ("nvidia", "h264"): 24,
    ("intel", "av1"): 32, ("intel", "hevc"): 26, ("intel", "h264"): 24,
    ("amd", "av1"): 32, ("amd", "hevc"): 26, ("amd", "h264"): 24,
}
# Optionale Feinjustierung per Env (CQ_SWEETSPOT), Defaults bleiben sonst aktiv.
_CQ_SWEETSPOT.update(getattr(config, "CQ_SWEETSPOT_OVERRIDES", {}) or {})


def _cq_offset(base: tuple, target: tuple) -> int:
    """CQ-Verschiebung, damit `target` im gleichen Qualitätsbereich wie `base` testet."""
    b = _CQ_SWEETSPOT.get(base)
    t = _CQ_SWEETSPOT.get(target)
    if b is None or t is None:
        return 0
    return t - b


@dataclass
class VmafResult:
    value: int
    rate_mode: str
    label: str
    vmaf: float
    clip_size_bytes: int
    predicted_size_bytes: int
    savings_percent: float
    codec: str = "av1"
    platform: str = "cpu"
    recommended: bool = False
    # Zusatzmetriken (Mittel über alle Stichproben; 0 = nicht gemessen).
    vmaf_hmean: float = 0.0   # harmonisches Mittel (straft Ausreißer stärker)
    vmaf_1pct: float = 0.0    # Mittel der schlechtesten 1 % Frames ("1%-Low")
    psnr: float = 0.0
    ssim: float = 0.0
    xpsnr: float = 0.0        # XPSNR (dB, 4:1:1 über Y/U/V gewichtet), FFmpeg ≥ 7.1
    cambi: Optional[float] = None          # Banding, höher = mehr; None = nicht gemessen
    ssimulacra2: Optional[float] = None    # ein Standbild je Szene, höher = besser
    screenshot_ref: str = ""            # Szene 0 (Rückwärtskompatibilität)
    screenshot_enc: str = ""
    screenshots: list = field(default_factory=list)  # [{scene, ref, enc}] je Szene
    scene_scores: list = field(default_factory=list)  # [{scene, vmaf}] je Stichprobe
    video_kbps: int = 0                 # gemessene Videobitrate der Testclips
    encoder_speed: str = ""
    b_frames: str = ""
    aq_strength: int = 0
    keyint_sec: int = 0
    nvenc_tune: str = ""
    encoder_args: str = ""          # Flags ab -c:v, wie der Test-Encode sie bekam
    two_pass: bool = False

    def to_dict(self) -> dict:
        d = {
            "value": self.value,
            "quality": self.value,  # Rückwärtskompatibilität UI
            "rate_mode": self.rate_mode,
            "label": self.label,
            "codec": self.codec,
            "platform": self.platform,
            "codec_disp": _codec_disp(self.platform, self.codec),
            "encoder_speed": self.encoder_speed,
            "b_frames": self.b_frames,
            "aq_strength": int(self.aq_strength or 0),
            "keyint_sec": int(self.keyint_sec or 0),
            "nvenc_tune": self.nvenc_tune or "",
            "encoder_args": self.encoder_args or "",
            "two_pass": bool(self.two_pass),
            "vmaf": round(self.vmaf, 2),
            "vmaf_score": round(quality_score(self.vmaf, self.vmaf_1pct, self.vmaf_hmean), 2),
            "clip_size_bytes": self.clip_size_bytes,
            "predicted_size_bytes": self.predicted_size_bytes,
            "predicted_human": ff.human_size(self.predicted_size_bytes),
            "savings_percent": round(self.savings_percent, 1),
            "recommended": self.recommended,
        }
        if self.video_kbps:
            d["video_kbps"] = int(self.video_kbps)
            d["video_bitrate_human"] = ff._bitrate_human(self.video_kbps * 1000)
        if self.vmaf_hmean:
            d["vmaf_hmean"] = round(self.vmaf_hmean, 2)
        if self.vmaf_1pct:
            d["vmaf_1pct"] = round(self.vmaf_1pct, 2)
        if self.psnr:
            d["psnr"] = round(self.psnr, 2)
        if self.ssim:
            d["ssim"] = round(self.ssim, 4)
        if self.xpsnr:
            d["xpsnr"] = round(self.xpsnr, 2)
        if self.cambi is not None:
            d["cambi"] = round(float(self.cambi), 2)
        if self.ssimulacra2 is not None:
            d["ssimulacra2"] = round(float(self.ssimulacra2), 2)
            mins = [
                float(s["ssimulacra2_min"]) for s in (self.scene_scores or [])
                if isinstance(s, dict) and s.get("ssimulacra2_min") is not None
                and not s.get("idle")
            ]
            if mins:
                d["ssimulacra2_min"] = round(min(mins), 2)
            counts = [
                int(s["ssimulacra2_n"]) for s in (self.scene_scores or [])
                if isinstance(s, dict) and s.get("ssimulacra2_n")
            ]
            if counts:
                d["ssimulacra2_n"] = max(counts)
        if self.screenshot_ref:
            d["screenshot_ref"] = f"/api/preview/{self.screenshot_ref}"
        if self.screenshot_enc:
            d["screenshot_enc"] = f"/api/preview/{self.screenshot_enc}"
        if self.screenshots:
            d["screenshots"] = [
                {
                    "scene": s.get("scene", 0),
                    "ref": f"/api/preview/{s['ref']}" if s.get("ref") else "",
                    "enc": f"/api/preview/{s['enc']}" if s.get("enc") else "",
                    **({"clip": s["clip"]} if s.get("clip") else {}),
                    **({"kbps": int(s["kbps"])} if s.get("kbps") else {}),
                }
                for s in self.screenshots
            ]
        if self.scene_scores:
            packed = []
            for s in self.scene_scores:
                item = {
                    "scene": s.get("scene", 0),
                    "vmaf": round(float(s.get("vmaf") or 0.0), 2),
                }
                if s.get("start") is not None:
                    item["start"] = round(float(s.get("start") or 0.0), 3)
                    item["length"] = round(float(s.get("length") or 0.0), 3)
                for key, nd in (("hmean", 2), ("p1", 2), ("psnr", 2), ("ssim", 4),
                                ("xpsnr", 2), ("xpsnr_min", 2)):
                    raw = s.get(key)
                    if raw:
                        item[key] = round(float(raw), nd)
                if s.get("cambi") is not None:
                    item["cambi"] = round(float(s["cambi"]), 2)
                if s.get("cambi_max") is not None:
                    item["cambi_max"] = round(float(s["cambi_max"]), 2)
                if s.get("ssimulacra2") is not None:
                    item["ssimulacra2"] = round(float(s["ssimulacra2"]), 2)
                if s.get("ssimulacra2_min") is not None:
                    item["ssimulacra2_min"] = round(float(s["ssimulacra2_min"]), 2)
                if s.get("ssimulacra2_n"):
                    item["ssimulacra2_n"] = int(s["ssimulacra2_n"])
                s2_vals = s.get("ssimulacra2_scores") or []
                if s2_vals:
                    item["ssimulacra2_scores"] = [round(float(x), 2) for x in s2_vals]
                s2_pairs = s.get("ssimulacra2_pairs") or []
                if s2_pairs:
                    item["ssimulacra2_pairs"] = [
                        {
                            "idx": int(p.get("idx", j)),
                            "at": round(float(p.get("at") or 0.0), 2),
                            "score": round(float(p.get("score") or 0.0), 2),
                            "ref": (p["ref"] if str(p.get("ref") or "").startswith("/api/preview/")
                                    else (f"/api/preview/{p['ref']}" if p.get("ref") else "")),
                            "enc": (p["enc"] if str(p.get("enc") or "").startswith("/api/preview/")
                                    else (f"/api/preview/{p['enc']}" if p.get("enc") else "")),
                        }
                        for j, p in enumerate(s2_pairs)
                        if isinstance(p, dict)
                    ]
                for key in ("frame_count", "p1_count", "scored_seconds", "scored_offset"):
                    if s.get(key) is not None:
                        item[key] = s[key]
                if s.get("clip"):
                    item["clip"] = s["clip"]
                frames = s.get("frames") or []
                if frames:
                    item["frames"] = frames
                if s.get("idle"):
                    item["idle"] = True
                if s.get("kbps"):
                    item["kbps"] = int(s["kbps"])
                if s.get("bitrate_sec"):
                    item["bitrate_sec"] = round(float(s["bitrate_sec"]), 3)
                if s.get("bitrate_align"):
                    item["bitrate_align"] = s.get("bitrate_align")
                curve = s.get("bitrate") or []
                if curve:
                    packed_curve = []
                    for b in curve:
                        if not isinstance(b, dict):
                            continue
                        point = {
                            "t": round(float(b.get("t") or 0), 4),
                            "kbps": round(float(b.get("kbps") or 0), 1),
                        }
                        if b.get("n") is not None:
                            point["n"] = int(b["n"])
                        packed_curve.append(point)
                    if packed_curve:
                        item["bitrate"] = packed_curve
                packed.append(item)
            d["scene_scores"] = packed
            vals = [s.get("vmaf") for s in self.scene_scores
                    if s.get("vmaf") is not None and not s.get("idle")]
            if len(vals) > 1:
                d["vmaf_min"] = round(min(vals), 2)
                d["vmaf_max"] = round(max(vals), 2)
        return d


@dataclass
class VmafAnalysis:
    results: list = field(default_factory=list)
    recommended_value: Optional[int] = None
    recommended_quality: Optional[int] = None  # Alias
    recommended_codec: Optional[str] = None
    recommended_platform: Optional[str] = None
    model: str = ""
    rate_mode: str = "cq"
    clip_seconds: int = 30
    error: str = ""            # Grund, falls keine Ergebnisse zustande kamen
    keep_source: bool = False  # Ziel nur mit größerer Datei erreichbar
    pick_warning: str = ""     # Floor verfehlt, Kompromiss Ersparnis/1%-Low
    target_lo: float = 0.0     # Mittelwert-Ziel, mit dem die Empfehlung gerechnet wurde
    target_gap: float = 0.0    # 1%-Low-Abstand dazu (0 = Floor aus)
    target_anchor: str = ""    # both | target | mean
    sample_windows: list = field(default_factory=list)
    sample_starts: list = field(default_factory=list)  # [(start, länge)] je Szene
    source_clips: list = field(default_factory=list)  # Remux je Szene: file, lead, length
    visual_only: bool = False   # Clips ohne libvmaf, zum Abspielen im A/B-Player
    provenance: dict = field(default_factory=dict)
    source_duration: float = 0.0

    def to_dict(self) -> dict:
        rec = self.recommended_value
        data = {
            **({"sample_starts": [[round(float(s), 3), round(float(l), 3)]
                                  for s, l in self.sample_starts]}
               if self.sample_starts else {}),
            "results": [r.to_dict() for r in self.results],
            "recommended_value": rec,
            "recommended_quality": rec,
            "recommended_codec": self.recommended_codec,
            "recommended_platform": self.recommended_platform,
            "multi_codec": len({(r.platform, r.codec) for r in self.results}) > 1,
            "model": self.model,
            "rate_mode": self.rate_mode,
            "clip_seconds": self.clip_seconds,
            "error": self.error,
            "keep_source": self.keep_source,
            "pick_warning": self.pick_warning,
            "visual_only": self.visual_only,
            **({"target_lo": self.target_lo, "target_gap": self.target_gap,
                 "target_anchor": self.target_anchor}
               if self.target_lo else {}),
            **({"sample_windows": self.sample_windows} if self.sample_windows else {}),
            **({"source_clips": self.source_clips} if self.source_clips else {}),
            "provenance": self.provenance,
            "source_duration": self.source_duration,
        }
        data["coverage"] = provenance_api.analysis_coverage(data, self.source_duration)
        return data


StatusCb = Optional[Callable[[str], None]]


def _label(rate_mode: str, value: int) -> str:
    if rate_mode == "cq":
        return f"CQ/QP {value}"
    if rate_mode == "abr":
        return f"ABR {value} kbit/s"
    return f"{value} kbit/s"


def _model_for(info: VideoInfo, neg: bool = False) -> tuple[str, Path]:
    if neg:
        name = config.VMAF_MODEL_4K_NEG if info.is_4k else config.VMAF_MODEL_1080P_NEG
        path = config.VMAF_MODEL_DIR / name
        if path.exists():
            return name, path
        logger.warning("NEG-VMAF-Modell fehlt (%s) – Standardmodell wird genutzt.", name)
    name = config.VMAF_MODEL_4K if info.is_4k else config.VMAF_MODEL_1080P
    return name, config.VMAF_MODEL_DIR / name


def _coerce_starts(raw) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                start = float(item[0])
                length = float(item[1])
            except (TypeError, ValueError):
                continue
            if length > 0 and start >= 0:
                out.append((round(start, 3), round(length, 3)))
    return out


def _middle_start(duration: float, clip_seconds: int) -> float:
    if duration <= clip_seconds:
        return 0.0
    return max(0.0, duration / 2.0 - clip_seconds / 2.0)


def _sample_starts(duration: float, clip_seconds: int, count: int,
                   credits_at: float = 0.0) -> list[tuple[float, float]]:
    """Startpositionen der Stichproben-Clips (start, clip_len).

    count=1 → nur Mitte. Bei mehreren gleichmäßig über den Film verteilt
    (max. 5). Ist der Film zu kurz für die gewünschte Cliplänge, werden die
    Ausschnitte verkürzt, statt auf eine einzige Mitte zusammenzufallen.
    ``credits_at`` begrenzt das Ende: die Clips liegen davor und verteilen
    sich neu über diesen kürzeren Bereich.
    """
    duration = float(duration or 0.0)
    try:
        end = float(credits_at or 0)
    except (TypeError, ValueError):
        end = 0.0
    if end > 30 and (duration <= 0 or end < duration):
        duration = end if duration <= 0 else min(duration, end)
    count = max(1, min(5, int(count or 1)))
    want = max(1.0, float(clip_seconds or 1))
    if duration <= 0:
        return [(0.0, 1.0)]
    if count <= 1:
        clip_len = min(want, duration) or 1.0
        return [(_middle_start(duration, int(round(clip_len))), clip_len)]
    clip_len = min(want, duration / count)
    if clip_len < 1.0:
        count = max(1, int(duration))
        clip_len = min(want, duration / max(1, count))
        if count <= 1:
            ln = min(want, duration) or 1.0
            return [(_middle_start(duration, 1), ln)]
    out: list[tuple[float, float]] = []
    for i in range(count):
        frac = (i + 1) / (count + 1)
        s = max(0.0, min(duration - clip_len, duration * frac - clip_len / 2.0))
        out.append((round(s, 3), round(clip_len, 3)))
    return out


def _extract_reference(
    info: VideoInfo, work: Path, tonemap: bool, start: float, clip_len: float,
    idx: int, status: StatusCb, crop: str = "",
    tonemap_algo: str = "hable", tonemap_desat: float = 0.0,
    tonemap_peak: float = 0.0, tonemap_engine: str = "cpu",
    platform: str = "cpu", provenance: Optional[dict] = None,
) -> Path:
    ref = work / f"reference_{idx}.mkv"
    binary, pre, piece = reference_tonemap_setup(
        tonemap_engine, platform, tonemap_algo, tonemap_desat, tonemap_peak,
        active=bool(tonemap and info.is_hdr),
    )
    if provenance is not None:
        provenance_api.record_binary(provenance, "reference", binary)
    cmd = [binary, "-y", "-hide_banner"]
    cmd += pre
    cmd += ["-ss", str(start), "-t", str(clip_len), "-i", str(info.path)]
    # Crop und Tonemap identisch zur Encode-Kette anwenden, damit der Vergleich
    # dieselbe (beschnittene/getonemappte) Bildfläche wie die Ausgabe nutzt.
    vf = []
    if crop:
        vf.append(f"crop={crop}")
    if piece:
        vf.append(piece)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-an", "-sn", "-c:v", "ffv1", "-level", "3", str(ref)]
    if status:
        status(f"Referenz-Clip {idx + 1} wird extrahiert …")
    _run_logged(cmd, f"VMAF-Referenz {idx}")
    return ref


def _source_clip_name(scene: int) -> str:
    try:
        name = f"source_s{int(scene)}.mkv"
    except (TypeError, ValueError):
        return ""
    return name if _SOURCE_CLIP.match(name) else ""


def _keyframe_before(path: Path, start: float) -> float:
    """Letzter Video-Keyframe bei oder vor der Szenenzeit. Sonst die Zeit selbst."""
    start = max(0.0, float(start))
    begin = max(0.0, start - 30.0)
    cmd = [
        config.FFPROBE, "-v", "error",
        "-select_streams", "v:0", "-skip_frame", "nokey",
        "-read_intervals", f"{begin:.3f}%{start + 0.05:.3f}",
        "-show_entries", "frame=pts_time", "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return start
    best = None
    for line in (proc.stdout or "").splitlines():
        piece = line.strip().split(",")[0]
        try:
            pts = float(piece)
        except ValueError:
            continue
        if 0 <= pts <= start + 0.002:
            best = pts if best is None else max(best, pts)
    return start if best is None else best


def _extract_source_remux(
    info: VideoInfo, work: Path, start: float, clip_len: float, idx: int,
) -> tuple[str, float]:
    """Videospur der Szene als Kopie. Der Vorlauf bis zum Keyframe bleibt im Clip.

    Der Testclip beginnt framegenau bei ``start``. Die Kopie beginnt am Keyframe
    davor. ``lead`` ist der Abstand, den der Player im Remux überspringt.
    """
    name = _source_clip_name(idx)
    if not name:
        return "", 0.0
    dest = work / name
    key = _keyframe_before(Path(info.path), start)
    lead = max(0.0, round(float(start) - key, 3))
    span = round(max(0.1, float(clip_len) + lead), 3)
    cmd = [
        config.FFMPEG, "-y", "-hide_banner",
        "-ss", f"{key:.3f}", "-i", str(info.path),
        "-t", f"{span:.3f}",
        "-map", "0:v:0", "-c", "copy", "-an", "-sn",
        "-avoid_negative_ts", "make_zero",
        str(dest),
    ]
    res = _run_logged(cmd, f"Quell-Remux Szene {idx + 1}")
    if res.returncode != 0 or not dest.is_file() or dest.stat().st_size <= 0:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return "", 0.0
    return name, lead


def _vmaf_threads() -> int:
    """libvmaf profitiert stark von mehreren Threads – an CPU koppeln."""
    return max(2, min(16, os.cpu_count() or 4))


# Rand der Stichprobe nicht werten. Schneller Seek (vor allem CUDA) und der
# Encoder-Flush am Clipende erzeugen oft ein paar Frames, die nicht zum Film
# gehören. Die ziehen den 1%-Low runter, ohne die Szene wirklich zu beschreiben.
_EDGE_SEC = 0.5


def _edge_margin(duration: float) -> float:
    duration = float(duration or 0)
    if duration < 4:
        return 0.0
    return min(_EDGE_SEC, duration * 0.08)


def _score_chain(duration: float, prefix: str = "") -> str:
    """Skalieren, Rand abschneiden, Zeitstempel auf 0 setzen."""
    parts = [p for p in (prefix,) if p]
    margin = _edge_margin(duration)
    if margin > 0 and duration > margin * 2 + 1:
        end = round(duration - margin, 3)
        parts.append(f"trim=start={margin}:end={end}")
    parts.append("setpts=PTS-STARTPTS")
    return ",".join(parts)


_xpsnr_ok: Optional[bool] = None


def xpsnr_available() -> bool:
    """FFmpeg mit xpsnr-Filter (ab 7.1)? Einmal geprüft, dann gecacht."""
    global _xpsnr_ok
    if _xpsnr_ok is None:
        try:
            proc = subprocess.run([config.FFMPEG, "-hide_banner", "-h", "filter=xpsnr"],
                                  capture_output=True, text=True, timeout=20)
            _xpsnr_ok = proc.returncode == 0 and "xpsnr" in (proc.stdout or "").lower()
        except (OSError, subprocess.TimeoutExpired):
            _xpsnr_ok = False
    return bool(_xpsnr_ok)


def _parse_xpsnr(stats: Path) -> Optional[dict]:
    """Per-Frame-Statistik des xpsnr-Filters mitteln (Y sowie 4:1:1-gewichtet)."""
    try:
        text = stats.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ys, ws = [], []
    identical = 0
    pat = re.compile(r"y:\s*([0-9.]+|inf)\s+u:\s*([0-9.]+|inf)\s+v:\s*([0-9.]+|inf)", re.I)
    for line in text.splitlines():
        m = pat.search(line)
        if not m:
            continue
        vals = []
        for raw in m.groups():
            try:
                vals.append(float(raw))
            except ValueError:
                vals.append(float("inf"))
        y, u, v = vals
        # Identische Frames (inf) verzerren den Schnitt; sie zählen nicht mit.
        if y == float("inf"):
            identical += 1
            continue
        u = min(u, 99.0)
        v = min(v, 99.0)
        ys.append(y)
        ws.append((4 * y + u + v) / 6.0)
    if not ys:
        if identical:
            return {"xpsnr": 99.0, "xpsnr_y": 99.0, "xpsnr_min": 99.0}
        return None
    return {"xpsnr": sum(ws) / len(ws), "xpsnr_y": sum(ys) / len(ys), "xpsnr_min": min(ys)}


def _run_libvmaf(
    distorted: Path, reference: Path, info: VideoInfo, work: Path, key: str,
    neg: bool, dims: Optional[tuple[int, int]], features: str,
    clip_len: float = 0.0, with_xpsnr: bool = False,
) -> Optional[dict]:
    """Ein libvmaf-Lauf; liefert das geparste JSON-Dict oder None.

    with_xpsnr: XPSNR im selben Lauf über ``split`` mitrechnen (kein zweiter
    Decode). Das Ergebnis landet unter ``data["xpsnr"]``.
    """
    _, model_path = _model_for(info, neg)
    log = work / f"vmaf_{key}.json"
    xlog = work / f"xpsnr_{key}.txt"
    w, h = dims if dims else (info.width, info.height)
    scale = f"scale={w}:{h}:flags=bicubic"
    dist_dur = ff._probe_duration(distorted) or float(clip_len or 0)
    ref_dur = ff._probe_duration(reference) or float(clip_len or 0)
    vmaf_filter = (
        f"libvmaf=model=path={model_path}:"
        f"{features}log_fmt=json:log_path={log}:shortest=1:n_threads={_vmaf_threads()}"
    )
    if with_xpsnr:
        xpath = str(xlog).replace("\\", "/").replace(":", "\\:")
        fc = (
            f"[0:v]{_score_chain(dist_dur, scale)},split[d1][d2];"
            f"[1:v]{_score_chain(ref_dur)},split[r1][r2];"
            f"[d1][r1]{vmaf_filter};"
            f"[d2][r2]xpsnr=stats_file={xpath}:shortest=1"
        )
    else:
        fc = (
            f"[0:v]{_score_chain(dist_dur, scale)}[dist];"
            f"[1:v]{_score_chain(ref_dur)}[ref];"
            f"[dist][ref]{vmaf_filter}"
        )
    cmd = [config.FFMPEG, "-y", "-hide_banner",
           "-i", str(distorted), "-i", str(reference),
           "-filter_complex", fc, "-f", "null", "-"]
    _run_logged(cmd, f"VMAF-Vergleich {key}")
    if not log.exists():
        return None
    try:
        data = json.loads(log.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # Tatsächlich bewertetes Fenster: beide Eingänge werden am Rand getrimmt.
    spans = [end - start for start, end in (
        _scored_window(dist_dur), _scored_window(ref_dur))]
    data["scored_seconds"] = max(0.0, min(spans))
    data["scored_offset"] = _scored_window(ref_dur)[0]
    if with_xpsnr and xlog.exists():
        parsed = _parse_xpsnr(xlog)
        if parsed:
            data["xpsnr"] = parsed
    return data


def _metrics_from_json(data: dict) -> Optional[dict]:
    """VMAF-Kennzahlen aus einem libvmaf-JSON extrahieren (inkl. 1%-Low)."""
    pooled = data.get("pooled_metrics", {}) or {}
    vm = pooled.get("vmaf", {}) or {}
    frames = data.get("frames", []) or []
    vals = sorted(
        f["metrics"]["vmaf"] for f in frames
        if f.get("metrics") and f["metrics"].get("vmaf") is not None
    )
    mean = vm.get("mean")
    if mean is None:
        mean = sum(vals) / len(vals) if vals else None
    if mean is None:
        return None
    # 1%-Low = Mittel der schlechtesten 1 % Frames (mind. 1 Frame).
    p1 = 0.0
    if vals:
        k = max(1, int(len(vals) * 0.01))
        p1 = sum(vals[:k]) / k
    psnr = (pooled.get("psnr_y", {}) or pooled.get("psnr", {}) or {}).get("mean") or 0.0
    ssim = (pooled.get("float_ssim", {}) or pooled.get("ssim", {}) or {}).get("mean") or 0.0
    series = [
        float(f["metrics"]["vmaf"]) for f in frames
        if f.get("metrics") and f["metrics"].get("vmaf") is not None
    ]
    xp = data.get("xpsnr") or {}
    out = {
        "vmaf": float(mean),
        "hmean": float(vm.get("harmonic_mean") or 0.0),
        "min": float(vm.get("min") or 0.0),
        "p1": float(p1),
        "frame_count": len(vals),
        "p1_count": max(1, int(len(vals) * 0.01)) if vals else 0,
        "scored_seconds": float(data.get("scored_seconds") or 0),
        "psnr": float(psnr),
        "ssim": float(ssim),
        "xpsnr": float(xp.get("xpsnr") or 0.0),
        "xpsnr_min": float(xp.get("xpsnr_min") or 0.0),
        "frames": _downsample_vmaf(series),
    }
    if data.get("scored_offset") is not None:
        out["scored_offset"] = float(data["scored_offset"])
    cambi_pool = pooled.get("cambi")
    if isinstance(cambi_pool, dict) and cambi_pool.get("mean") is not None:
        out["cambi"] = float(cambi_pool["mean"])
        if cambi_pool.get("max") is not None:
            out["cambi_max"] = float(cambi_pool["max"])
    return out


def _downsample_vmaf(vals: list[float], n: int = 80) -> list[float]:
    """Kurze Kurve über die Stichprobe. Je Abschnitt der Tiefstwert, damit Einbrüche bleiben."""
    if not vals:
        return []
    if len(vals) <= n:
        return [round(v, 2) for v in vals]
    out: list[float] = []
    step = len(vals) / n
    for i in range(n):
        a = int(i * step)
        b = max(a + 1, int((i + 1) * step))
        chunk = vals[a:b] or [vals[a]]
        out.append(round(min(chunk), 2))
    return out


def _vmaf_metrics(
    distorted: Path, reference: Path, info: VideoInfo, work: Path, key: str,
    neg: bool = False, dims: Optional[tuple[int, int]] = None,
    clip_len: float = 0.0,
) -> Optional[dict]:
    """Vollständige Metriken (VMAF + PSNR + SSIM + CAMBI + XPSNR + 1%-Low) für einen Vergleich.

    PSNR, SSIM und CAMBI laufen über die libvmaf-`feature`-Option, XPSNR im
    selben Lauf über den xpsnr-Filter (FFmpeg ≥ 7.1). Fehlt CAMBI im Build,
    fällt der Lauf auf PSNR/SSIM zurück. Die Kern-Metrik bleibt so verfügbar.
    """
    features_cambi = "feature=name=psnr|name=float_ssim|name=cambi:"
    features = "feature=name=psnr|name=float_ssim:"
    data = None
    if xpsnr_available():
        data = _run_libvmaf(distorted, reference, info, work, key, neg, dims,
                            features=features_cambi, clip_len=clip_len, with_xpsnr=True)
    metrics = _metrics_from_json(data) if data else None
    if metrics is None:
        data = _run_libvmaf(distorted, reference, info, work, key, neg, dims,
                            features=features_cambi, clip_len=clip_len)
        metrics = _metrics_from_json(data) if data else None
    if metrics is None and xpsnr_available():
        data = _run_libvmaf(distorted, reference, info, work, key, neg, dims,
                            features=features, clip_len=clip_len, with_xpsnr=True)
        metrics = _metrics_from_json(data) if data else None
    if metrics is None:
        data = _run_libvmaf(distorted, reference, info, work, key, neg, dims,
                            features=features, clip_len=clip_len)
        metrics = _metrics_from_json(data) if data else None
    if metrics is None:
        data = _run_libvmaf(distorted, reference, info, work, key, neg, dims,
                            features="", clip_len=clip_len)
        metrics = _metrics_from_json(data) if data else None
    return metrics


def _vmaf_compare(
    distorted: Path, reference: Path, info: VideoInfo, work: Path, key: str,
    neg: bool = False, dims: Optional[tuple[int, int]] = None,
) -> Optional[float]:
    """Rückwärtskompatibel: nur der VMAF-Mittelwert."""
    m = _vmaf_metrics(distorted, reference, info, work, key, neg, dims)
    return m["vmaf"] if m else None


def _extract_frame(
    src: Path, out_rel: str, clip_len: float, fps: float = 0.0, label: str = "frame",
) -> str:
    """Ein Einzelbild aus einem kurzen Clip an dessen Mitte extrahieren.

    Referenz-Clip und Test-Encode teilen denselben Frame-Index (beide starten bei
    Frame 0, identische FPS). Über `select=eq(n,N)` wird framegenau derselbe Frame
    getroffen (unabhängig von Keyframes/Zeitstempeln). Fehlt die FPS-Angabe, wird
    auf Output-Seeking zurückgegriffen. Gibt den relativen Pfad oder "" zurück.
    """
    (config.PREVIEW_DIR / out_rel).parent.mkdir(parents=True, exist_ok=True)
    base = [config.FFMPEG, "-y", "-hide_banner", "-i", str(src)]
    if fps and fps > 0:
        frame_no = max(0, int(round(fps * (clip_len / 2.0))))
        base += ["-vf", f"select=eq(n\\,{frame_no})", "-frames:v", "1", "-fps_mode", "passthrough"]
    else:
        base += ["-ss", str(max(0.0, clip_len / 2.0)), "-frames:v", "1"]
    base += ["-q:v", "2", str(config.PREVIEW_DIR / out_rel)]
    res = _run_logged(base, f"Screenshot {label}")
    return out_rel if res.returncode == 0 and (config.PREVIEW_DIR / out_rel).exists() else ""


_s2_ok: Optional[bool] = None


def _ssimulacra2_ok() -> bool:
    global _s2_ok
    if _s2_ok is None:
        path = Path(config.SSIMULACRA2)
        _s2_ok = path.is_file()
    return bool(_s2_ok)


def _png_at(src: Path, dest: Path, at_sec: float, fps: float) -> bool:
    """Ein PNG an einer Zeit im Clip. Frame-Index, damit Quelle und Encode dasselbe Bild treffen."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-i", str(src)]
    if fps and fps > 0:
        frame_no = max(0, int(round(fps * max(0.0, at_sec))))
        cmd += ["-vf", f"select=eq(n\\,{frame_no})", "-frames:v", "1", "-fps_mode", "passthrough"]
    else:
        cmd += ["-ss", f"{max(0.0, at_sec):.3f}", "-frames:v", "1"]
    cmd += [str(dest)]
    res = _run_logged(cmd, "SSIMULACRA2-Frame")
    return res.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


def _s2_frame_count(n: int) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = 3
    if v <= 0:
        return 0
    return max(1, min(7, v))


def _s2_times(clip_len: float, n: int) -> list[float]:
    """Gleichmäßig im Clip, nicht auf den Schnittkanten. 1 bleibt die Mitte."""
    count = _s2_frame_count(n)
    if count <= 0:
        return []
    length = max(0.0, float(clip_len or 0))
    return [length * (i / (count + 1)) for i in range(1, count + 1)]


def _ssimulacra2_pair(ref_png: Path, dist_png: Path) -> Optional[float]:
    proc = subprocess.run(
        [config.SSIMULACRA2, str(ref_png), str(dist_png)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, check=False,
    )
    if proc.returncode != 0:
        tail = ((proc.stderr or proc.stdout or "").strip().splitlines())
        logger.warning("SSIMULACRA2 fehlgeschlagen: %s", tail[-1] if tail else proc.returncode)
        return None
    parts = (proc.stdout or "").strip().split()
    if not parts:
        return None
    return float(parts[-1])


def _jpg_from_png(src_png: Path, out_rel: str) -> str:
    """PNG-Standbild platzsparend als JPG im Preview-Ordner ablegen."""
    dest = config.PREVIEW_DIR / out_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return out_rel
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src_png), "-q:v", "2", str(dest)]
    res = _run_logged(cmd, "SSIMULACRA2-Preview")
    return out_rel if res.returncode == 0 and dest.is_file() else ""


def _ssimulacra2_clip(
    reference: Path, distorted: Path, work: Path, key: str,
    clip_len: float, fps: float, frames: int = 3,
    *, sess: str = "", scene_idx: int = 0, save_previews: bool = False,
) -> Optional[dict]:
    """Mehrere Standbilder je Stichprobe. Höher ist besser, bis 100."""
    if _s2_frame_count(frames) <= 0 or not _ssimulacra2_ok():
        return None
    scores: list[float] = []
    pairs: list[dict] = []
    times = _s2_times(clip_len, frames)
    try:
        for i, at in enumerate(times):
            ref_png = work / f"s2_{key}_{i}_ref.png"
            dist_png = work / f"s2_{key}_{i}_enc.png"
            try:
                if not _png_at(reference, ref_png, at, fps):
                    continue
                if not _png_at(distorted, dist_png, at, fps):
                    continue
                score = _ssimulacra2_pair(ref_png, dist_png)
                if score is not None:
                    scores.append(score)
                    ref_rel = ""
                    enc_rel = ""
                    if save_previews and sess:
                        ref_rel = _jpg_from_png(ref_png, f"{sess}/s2_s{scene_idx}_f{i}_ref.jpg")
                        enc_rel = _jpg_from_png(dist_png, f"{sess}/s2_s{scene_idx}_f{i}_{key}.jpg")
                    pairs.append({
                        "idx": i,
                        "at": round(float(at), 2),
                        "score": round(float(score), 2),
                        "ref": ref_rel,
                        "enc": enc_rel,
                    })
            finally:
                for path in (ref_png, dist_png):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("SSIMULACRA2: %s", exc)
        return None
    if not scores:
        return None
    return {
        "mean": sum(scores) / len(scores),
        "min": min(scores),
        "n": len(scores),
        "scores": [round(float(x), 2) for x in scores],
        "pairs": pairs,
    }


def _extract_source_still(
    info: VideoInfo, out_rel: str, at_sec: float, *,
    tonemap: bool, crop: str, tonemap_algo: str, tonemap_desat: float,
    tonemap_peak: float, tonemap_engine: str, platform: str,
) -> str:
    """Ein Standbild der Quelle an einer Filmzeit, mit derselben Tonemap wie der Encode."""
    dest = config.PREVIEW_DIR / out_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    binary, pre, piece = reference_tonemap_setup(
        tonemap_engine, platform, tonemap_algo, tonemap_desat, tonemap_peak,
        active=bool(tonemap and info.is_hdr),
    )
    cmd = [binary, "-y", "-hide_banner", "-loglevel", "error"]
    cmd += pre
    cmd += ["-ss", f"{max(0.0, float(at_sec)):.3f}", "-i", str(info.path)]
    vf: list[str] = []
    if crop:
        vf.append(f"crop={crop}")
    if piece:
        vf.append(piece)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-frames:v", "1", "-q:v", "2", str(dest)]
    res = _run_logged(cmd, "Standbild Quelle")
    return out_rel if res.returncode == 0 and dest.exists() else ""


def measure_output_vmaf(
    info: VideoInfo,
    output: Path,
    *,
    tonemap: bool = False,
    preserve_hdr: bool = False,
    samples: int = 1,
    clip_seconds: int = 15,
    anime: bool = False,
    crop: str = "",
    tonemap_algo: str = "hable",
    tonemap_desat: float = 0.0,
    tonemap_peak: float = 0.0,
    tonemap_engine: str = "cpu",
    platform: str = "cpu",
    cancelled: Callable[[], bool] = lambda: False,
) -> Optional[float]:
    """Misst den echten VMAF der fertigen Ausgabedatei gegen die Quelle.

    Die Messung ist immer VMAF-NEG. ``anime`` bleibt im Aufruf, steuert hier
    aber kein Modell mehr.

    Für die Qualitäts-Guardrail: es werden dieselben Stichproben-Positionen wie
    bei der Analyse genutzt. Aus Quelle (ggf. getonemappt) und Ausgabe werden
    verlustfreie Clips gezogen und verglichen; der Mittelwert wird gemittelt.
    Downscale wird im Vergleich (scale auf Quellauflösung) berücksichtigt.
    """
    output = Path(output)
    if not output.exists():
        return None
    work = config.WORK_DIR / f"verify_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        specs = _sample_starts(info.duration, clip_seconds, samples)
        dims = ff.crop_dims(crop)  # bei Auto-Crop auf beschnittene Fläche vergleichen
        scores: list[float] = []
        for idx, (start, clip_len) in enumerate(specs):
            if cancelled():
                break
            ref = _extract_reference(
                info, work, tonemap, start, clip_len, idx, None, crop=crop,
                tonemap_algo=tonemap_algo, tonemap_desat=tonemap_desat,
                tonemap_peak=tonemap_peak, tonemap_engine=tonemap_engine,
                platform=platform)
            dist = work / f"out_{idx}.mkv"
            # Denselben Ausschnitt verlustfrei aus der Ausgabe ziehen.
            _run_logged(
                [config.FFMPEG, "-y", "-hide_banner", "-ss", str(start),
                 "-t", str(clip_len), "-i", str(output),
                 "-an", "-sn", "-c:v", "ffv1", "-level", "3", str(dist)],
                f"Verify-Clip {idx}")
            if not dist.exists():
                continue
            score = _vmaf_compare(dist, ref, info, work, f"verify_{idx}",
                                  neg=True, dims=dims)
            if score is not None:
                scores.append(score)
        return round(sum(scores) / len(scores), 2) if scores else None
    finally:
        _cleanup(work)


def _with_active_session(function):
    from functools import wraps
    from dataclasses import replace

    @wraps(function)
    def wrapped(*args, **kwargs):
        positional = list(args)
        opts = kwargs.get("opts") if "opts" in kwargs else (positional[5] if len(positional) > 5 else None)
        opts = opts or VmafOptions()
        session = opts.session_name or opts.item_id or uuid.uuid4().hex[:8]
        opts = replace(opts, session_name=session)
        if len(positional) > 5:
            positional[5] = opts
        else:
            kwargs["opts"] = opts
        with storage_policy.active_session(session):
            return function(*positional, **kwargs)
    return wrapped


@_with_active_session
def analyze(
    info: VideoInfo,
    platform: str,
    codec: str,
    target_height: Optional[int],
    tonemap: bool,
    opts: Optional[VmafOptions] = None,
    status: StatusCb = None,
    cancelled: Callable[[], bool] = lambda: False,
    preserve_hdr: bool = False,
    film_grain: int = 0,
    denoise: str = "off",
    sharpen: str = "off",
    grain: str = "off",
    deinterlace: str = "auto",
    aq_strength: int = 8,
    crop: str = "",
    progress: Optional[Callable[[dict], None]] = None,
    encoder_speed: str = "balanced",
    b_frames: str = "auto",
    keyint_sec: int = 0,
    nvenc_tune: str = "auto",
    on_step: Optional[Callable[[str], None]] = None,
    tonemap_algo: str = "hable",
    tonemap_desat: float = 0.0,
    tonemap_peak: float = 0.0,
    tonemap_engine: str = "cpu",
    deband: str = "off",
    svt_look: str = "standard",
) -> VmafAnalysis:
    opts = opts or VmafOptions()
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    # Lesbarer Session-Name für Previews & Archiv (Fallback: item_id/uuid).
    sess = opts.session_name or opts.item_id or uuid.uuid4().hex[:8]
    work = config.WORK_DIR / f"vmaf_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    from dataclasses import asdict
    model_name, model_path = _model_for(info, True)
    effective_params = {
        **dict(opts.params or {}), "options": asdict(opts),
        "platform": platform, "codec": codec, "target_height": target_height,
        "tonemap": tonemap, "preserve_hdr": preserve_hdr, "film_grain": film_grain,
        "denoise": denoise, "sharpen": sharpen, "grain": grain,
        "deinterlace": deinterlace, "aq_strength": aq_strength, "crop": crop,
        "encoder_speed": encoder_speed, "b_frames": b_frames,
        "keyint_sec": keyint_sec, "nvenc_tune": nvenc_tune,
        "tonemap_algo": tonemap_algo, "tonemap_desat": tonemap_desat,
        "tonemap_peak": tonemap_peak, "tonemap_engine": tonemap_engine,
        "deband": deband, "svt_look": svt_look, "edge_seconds": _EDGE_SEC,
    }
    analysis = VmafAnalysis(
        model="" if opts.visual_only else model_name,
        rate_mode=opts.rate_mode,
        clip_seconds=opts.clip_seconds,
        visual_only=bool(opts.visual_only),
        source_duration=float(info.duration or 0),
        provenance=provenance_api.capture(
            source_path=str(info.path),
            model_name="" if opts.visual_only else model_name,
            model_path=None if opts.visual_only else model_path,
            params=effective_params,
            source_info={key: getattr(info, key, None) for key in (
                "duration", "width", "height", "fps", "pix_fmt", "bit_depth", "is_4k")}),
    )
    use_bitrate = opts.rate_mode in ("bitrate", "abr")
    values = [v for v in opts.test_values[:4] if v > 0]
    if not values:
        values = [20, 24, 28, 32] if not use_bitrate else [8000, 6000, 4000, 2000]

    runs = _collect_runs(
        platform, codec, encoder_speed, b_frames,
        list(opts.encoders), list(opts.variants or []),
        aq_strength=int(aq_strength or 8),
        keyint_sec=int(keyint_sec or 0),
        nvenc_tune=nvenc_tune,
        rows=list(opts.rows or []))
    multi = len(runs) > 1

    # --- Fortschritts-Tracking --------------------------------------------
    n_samples = len(_sample_starts(
        info.duration, opts.clip_seconds, opts.samples, opts.credits_at))

    def _mode_of(run) -> str:
        rm = run[8] if len(run) > 8 else ""
        return rm if rm in ("cq", "bitrate", "abr") else opts.rate_mode

    def _vals_of(run) -> list[int]:
        mode = _mode_of(run)
        raw = run[9] if len(run) > 9 else None
        if run[8] if len(run) > 8 else "":
            if raw:
                return [int(v) for v in raw if int(v) > 0]
            return [20, 24, 28, 32] if mode == "cq" else [8000, 6000, 4000, 2000]
        return list(values)

    def _two_of(run) -> bool:
        if _mode_of(run) not in ("bitrate", "abr"):
            return False
        flag = run[10] if len(run) > 10 else None
        if flag is None:
            return bool(opts.two_pass)
        return bool(flag)

    # Einheiten je Sample: Encode + VMAF. CPU-Zwei-Pass zählt den ersten Lauf extra.
    steps = 0
    units = 0
    two_groups: dict[tuple, set] = {}
    for run in runs:
        vals_n = max(1, len(_vals_of(run)))
        extra = 1 if (_two_of(run) and run[0] == "cpu") else 0
        steps += vals_n
        # Encode, und ohne reinen Sichtvergleich zusätzlich die VMAF-Messung.
        measure = 0 if opts.visual_only else 1
        units += n_samples * (1 + measure + extra) * vals_n
        gk = (*run[:7], _mode_of(run))
        two_groups.setdefault(gk, set()).add(_two_of(run))
    budget = {"steps": max(1, steps), "units": max(1, units)}
    prog = {"done": 0, "step": 0}

    def emit(phase: str, fps=None, sub=None) -> None:
        if not progress:
            return
        pct = round(min(100.0, prog["done"] / budget["units"] * 100.0), 1)
        d = {"percent": pct, "phase": phase,
             "step": prog["step"], "steps": budget["steps"]}
        if fps is not None:
            d["fps"] = round(fps, 1)
        if sub is not None:
            d["sub_percent"] = round(sub, 1)
        progress(d)

    last_error = ""  # letzter Test-Encode-Fehler (für Diagnose, falls 0 Ergebnisse)

    def run_value(p: str, c: str, val: int, sp: str, bf: str,
                  tag: str = "", extra: bool = False,
                  aq: int | None = None, ki: int | None = None,
                  tune: str = "auto", rate_mode: str = "",
                  row_two: bool | None = None, mark_two: bool = False) -> None:
        """Einen Qualitäts-/Bitrate-Punkt für einen Encoder testen."""
        nonlocal last_error
        aq_use = int(aq_strength if aq is None else aq)
        ki_use = int(keyint_sec if ki is None else ki)
        tune_use = ff.normalize_nvenc_tune(tune)
        mode = rate_mode if rate_mode in ("cq", "bitrate", "abr") else opts.rate_mode
        point_bitrate = mode in ("bitrate", "abr")
        if row_two is None:
            do_two = bool(point_bitrate and opts.two_pass)
        else:
            do_two = bool(point_bitrate and row_two)
        if any(r.platform == p and r.codec == c and r.encoder_speed == sp
               and r.b_frames == bf and int(r.aq_strength) == aq_use
               and int(r.keyint_sec) == ki_use
               and (r.nvenc_tune or "auto") == tune_use
               and (r.rate_mode or "cq") == mode
               and bool(r.two_pass) == do_two
               and int(r.value) == int(val)
               for r in analysis.results):
            return
        disp = _codec_disp(p, c)
        shown = f"{disp} · {tag}" if tag else disp
        key = f"{p}_{c}_{sp}_{bf}_{aq_use}_{ki_use}_{tune_use}_{mode}_{int(do_two)}_{val}"
        rate_lbl = _label(mode, val)
        if mark_two and do_two:
            rate_lbl += " · Zwei-Pass"
        if extra:
            rate_lbl += " · Zwischenwert"
        lbl = f"{shown} · {rate_lbl}" if (multi or tag) else rate_lbl
        if on_step:
            on_step(lbl)
        prog["step"] += 1

        total_size = 0
        total_dur = 0.0
        scores: list[float] = []
        hmeans: list[float] = []
        p1s: list[float] = []
        psnrs: list[float] = []
        ssims: list[float] = []
        xpsnrs: list[float] = []
        cambis: list[float] = []
        s2s: list[float] = []
        shots: list[dict] = []
        scene_scores: list[dict] = []
        enc_args = ""

        for si, (reference, start, clip_len) in enumerate(references):
            if cancelled():
                break
            skey = f"{key}_s{si}"
            smp = f" (Clip {si + 1}/{len(references)})" if len(references) > 1 else ""
            test_file = work / f"test_{skey}.mkv"
            passlog = str(work / f"pass_{skey}")
            cpu_two = bool(do_two and p == "cpu")
            nv_two = bool(do_two and p == "nvidia")

            def _test_cmd(pass_num: Optional[int] = None) -> list[str]:
                kw = dict(
                    duration_limit=clip_len, start_at=start,
                    include_progress=True, audio_mode="none",
                    preserve_hdr=preserve_hdr, film_grain=film_grain,
                    denoise=denoise, sharpen=sharpen, grain=grain,
                    deinterlace=deinterlace, aq_strength=aq_use, crop=crop,
                    force_10bit=opts.anime,
                    encoder_speed=sp,
                    b_frames=bf,
                    keyint_sec=ki_use,
                    nvenc_tune=tune_use,
                    tonemap_algo=tonemap_algo,
                    tonemap_desat=tonemap_desat,
                    tonemap_peak=tonemap_peak,
                    tonemap_engine=tonemap_engine,
                    deband=deband,
                    svt_look=svt_look,
                )
                if point_bitrate:
                    kw["rate_mode"] = mode
                    kw["bitrate_kbps"] = val
                    quality = 28
                else:
                    quality = val
                if cpu_two and pass_num in (1, 2):
                    kw["two_pass"] = True
                    kw["pass_num"] = pass_num
                    kw["passlog"] = passlog
                elif nv_two:
                    kw["two_pass"] = True
                command = build_encode_cmd(
                    info, test_file, p, c, quality, target_height, tonemap, **kw)
                provenance_api.record_binary(analysis.provenance, "encode", command[0])
                return command

            runner = EncodeRunner(on_progress=lambda pr: emit(
                "encode", fps=pr.fps, sub=pr.percent))
            if cpu_two:
                if status:
                    status(f"Test-Encode {shown} @ {rate_lbl}{smp} · Pass 1/2 …")
                emit("encode")
                rc1, err1 = runner.run(_test_cmd(1), clip_len)
                prog["done"] += 1
                if cancelled():
                    break
                if rc1 != 0:
                    tail = (err1 or "").strip().splitlines()
                    last_error = (
                        f"Test-Encode Pass 1 fehlgeschlagen ({shown} @ {rate_lbl}): "
                        f"{tail[-1] if tail else 'keine Ausgabe'}"
                    )
                    logger.warning("%s", last_error)
                    continue
                if status:
                    status(f"Test-Encode {shown} @ {rate_lbl}{smp} · Pass 2/2 …")
            elif status:
                status(f"Test-Encode {shown} @ {rate_lbl}{smp} …")
            emit("encode")
            cmd = _test_cmd(2 if cpu_two else None)
            rc, enc_err = runner.run(cmd, clip_len)
            prog["done"] += 1
            if not enc_args:
                enc_args = _encoder_args_text(cmd)
            if not test_file.exists() or test_file.stat().st_size == 0:
                tail = (enc_err or "").strip().splitlines()
                last_error = (
                    f"Test-Encode fehlgeschlagen ({shown} @ {rate_lbl}, "
                    f"FFmpeg Exit {rc}): {tail[-1] if tail else 'keine Ausgabe'}"
                )
                logger.warning("%s\nCMD: %s\nSTDERR:\n%s",
                               last_error, " ".join(cmd), enc_err)
                continue
            if opts.visual_only:
                score = 0.0
                metrics = None
            else:
                if status:
                    status(f"VMAF-Vergleich {shown} @ {rate_lbl}{smp} …")
                emit("vmaf")
                metrics = _vmaf_metrics(test_file, reference, info, work, skey,
                                        neg=True, dims=dims, clip_len=clip_len)
                prog["done"] += 1
                emit("vmaf")
                if metrics is None:
                    continue
                score = metrics["vmaf"]
                scores.append(score)
                if metrics.get("hmean"):
                    hmeans.append(metrics["hmean"])
                if metrics.get("p1"):
                    p1s.append(metrics["p1"])
                if metrics.get("psnr"):
                    psnrs.append(metrics["psnr"])
                if metrics.get("ssim"):
                    ssims.append(metrics["ssim"])
                if metrics.get("xpsnr"):
                    xpsnrs.append(metrics["xpsnr"])
                if metrics.get("cambi") is not None:
                    cambis.append(float(metrics["cambi"]))
                if reference is not None and _s2_frame_count(opts.ssimulacra2_frames) > 0:
                    if status:
                        status(f"SSIMULACRA2 {shown} @ {rate_lbl}{smp} …")
                    s2 = _ssimulacra2_clip(
                        reference, test_file, work, skey, clip_len,
                        float(info.fps or 0), opts.ssimulacra2_frames,
                        sess=sess, scene_idx=si,
                        save_previews=bool(opts.generate_screenshots))
                    if s2 is not None:
                        metrics["ssimulacra2"] = s2["mean"]
                        metrics["ssimulacra2_min"] = s2["min"]
                        metrics["ssimulacra2_n"] = s2["n"]
                        metrics["ssimulacra2_scores"] = s2.get("scores") or []
                        metrics["ssimulacra2_pairs"] = s2.get("pairs") or []
                        s2s.append(s2["mean"])
            scene = {
                "scene": si,
                "start": float(start),      # Position im Film (s) – für Sprünge im Player
                "length": float(clip_len),
                "vmaf": score,
                "hmean": (metrics or {}).get("hmean") or 0.0,
                "p1": (metrics or {}).get("p1") or 0.0,
                "psnr": (metrics or {}).get("psnr") or 0.0,
                "ssim": (metrics or {}).get("ssim") or 0.0,
                "xpsnr": (metrics or {}).get("xpsnr") or 0.0,
                "xpsnr_min": (metrics or {}).get("xpsnr_min") or 0.0,
                "frames": (metrics or {}).get("frames") or [],
            }
            scene["clip"] = test_file.name
            if metrics:
                for field in ("frame_count", "p1_count", "scored_seconds", "scored_offset"):
                    if metrics.get(field) is not None:
                        scene[field] = metrics[field]
            if metrics and metrics.get("cambi") is not None:
                scene["cambi"] = metrics["cambi"]
            if metrics and metrics.get("cambi_max") is not None:
                scene["cambi_max"] = metrics["cambi_max"]
            if metrics and metrics.get("ssimulacra2") is not None:
                scene["ssimulacra2"] = metrics["ssimulacra2"]
            if metrics and metrics.get("ssimulacra2_min") is not None:
                scene["ssimulacra2_min"] = metrics["ssimulacra2_min"]
            if metrics and metrics.get("ssimulacra2_n"):
                scene["ssimulacra2_n"] = int(metrics["ssimulacra2_n"])
            if metrics and metrics.get("ssimulacra2_scores"):
                scene["ssimulacra2_scores"] = list(metrics["ssimulacra2_scores"])
            if metrics and metrics.get("ssimulacra2_pairs"):
                scene["ssimulacra2_pairs"] = list(metrics["ssimulacra2_pairs"])
            scene_scores.append(scene)
            clip_bytes = test_file.stat().st_size
            total_size += clip_bytes
            total_dur += clip_len
            scene_kbps = measured_kbps(clip_bytes, clip_len)
            if scene_scores:
                scene_scores[-1]["kbps"] = scene_kbps
                try:
                    dur = ff._probe_duration(test_file) or float(clip_len)
                    curve = scored_bitrate(test_file, dur, float(info.fps or 0))
                except Exception:
                    curve = {}
                if curve.get("bins"):
                    scene_scores[-1]["bitrate"] = curve["bins"]
                    scene_scores[-1]["bitrate_sec"] = curve.get("scored_sec") or 0
                    scene_scores[-1]["bitrate_align"] = curve.get("align") or ""
            if opts.generate_screenshots:
                enc_rel = _extract_frame(
                    test_file, f"{sess}/{key}_s{si}_enc.jpg",
                    clip_len, info.fps, label=f"Enc {lbl} S{si}")
                shots.append({
                    "scene": si,
                    "ref": ref_shots[si] if si < len(ref_shots) else "",
                    "enc": enc_rel,
                    "clip": f"test_{skey}.mkv",
                    "kbps": scene_kbps,
                })

        if opts.visual_only:
            if not scene_scores or total_dur <= 0:
                return
            avg_score = 0.0
        elif not scores or total_dur <= 0:
            return
        else:
            avg_score = sum(scores) / len(scores)
        # Test-Encodes sind video-only (`audio_mode=none`). Die echte Ausgabe
        # behält Ton/Untertitel – ohne den Aufschlag wirkt CQ 24 oft „kleiner
        # als die Quelle“, obwohl Video+Ton wächst.
        predicted_video = int((total_size / total_dur) * info.duration)
        predicted = predicted_video + _copied_payload_bytes(info, opts.params)
        savings = 0.0
        if info.size_bytes > 0:
            savings = (info.size_bytes - predicted) / info.size_bytes * 100.0

        def _avg(xs: list[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        analysis.results.append(VmafResult(
            value=val,
            rate_mode=mode,
            two_pass=do_two,
            label=lbl,
            codec=c,
            platform=p,
            vmaf=avg_score,
            vmaf_hmean=_avg(hmeans),
            vmaf_1pct=_avg(p1s),
            psnr=_avg(psnrs),
            ssim=_avg(ssims),
            xpsnr=_avg(xpsnrs),
            cambi=_avg(cambis) if cambis else None,
            ssimulacra2=_avg(s2s) if s2s else None,
            clip_size_bytes=total_size,
            predicted_size_bytes=predicted,
            savings_percent=savings,
            screenshot_ref=shots[0]["ref"] if shots else "",
            screenshot_enc=shots[0]["enc"] if shots else "",
            screenshots=shots,
            scene_scores=scene_scores,
            video_kbps=measured_kbps(total_size, total_dur),
            encoder_speed=sp,
            b_frames=bf,
            aq_strength=aq_use,
            keyint_sec=ki_use,
            nvenc_tune=tune_use,
            encoder_args=enc_args,
        ))

    try:
        # Stichproben-Clips bestimmen und je eine (verlustfreie) Referenz ziehen.
        sample_specs = _coerce_starts(opts.sample_starts)
        if not sample_specs:
            sample_specs = _sample_starts(
                info.duration, opts.clip_seconds, opts.samples, opts.credits_at)
        analysis.sample_windows = list(opts.sample_windows or [])
        analysis.sample_starts = [(float(s), float(l)) for s, l in sample_specs]
        references: list[tuple[Optional[Path], float, float]] = []
        ref_shots: list[str] = []  # Referenz-Screenshot je Szene (einmalig)
        dims = ff.crop_dims(crop)  # Vergleichsauflösung bei Auto-Crop
        source_clips: list[dict] = []
        for si, (start, clip_len) in enumerate(sample_specs):
            if cancelled():
                break
            if status:
                status(f"Quellclip · Szene {si + 1}")
            source_name, source_lead = _extract_source_remux(
                info, work, float(start), float(clip_len), si)
            if source_name:
                source_clips.append({
                    "scene": si,
                    "file": source_name,
                    "lead": source_lead,
                    "length": round(float(clip_len), 3),
                })
            if opts.visual_only:
                references.append((None, start, clip_len))
                if opts.generate_screenshots:
                    ref_shots.append(_extract_source_still(
                        info, f"{sess}/scene{si}_ref.jpg",
                        float(start) + float(clip_len) / 2.0,
                        tonemap=tonemap, crop=crop,
                        tonemap_algo=tonemap_algo, tonemap_desat=tonemap_desat,
                        tonemap_peak=tonemap_peak, tonemap_engine=tonemap_engine,
                        platform=platform))
                continue
            emit("reference")
            if on_step:
                on_step(f"Referenz · Szene {si + 1}")
            ref = _extract_reference(
                info, work, tonemap, start, clip_len, si, status, crop=crop,
                tonemap_algo=tonemap_algo, tonemap_desat=tonemap_desat,
                tonemap_peak=tonemap_peak, tonemap_engine=tonemap_engine,
                platform=platform, provenance=analysis.provenance)
            references.append((ref, start, clip_len))
            if opts.generate_screenshots:
                ref_shots.append(_extract_frame(
                    ref, f"{sess}/scene{si}_ref.jpg", clip_len, info.fps,
                    label=f"Ref Szene {si}"))
        analysis.source_clips = source_clips

        base_pc = (runs[0][0], runs[0][1])
        for run in runs:
            p, c, sp, bf, aq, ki, tune, tag = run[:8]
            mode = _mode_of(run)
            point_br = mode in ("bitrate", "abr")
            # Eigene Testwerte nicht auf die CQ-Skala eines anderen Codecs schieben.
            own_vals = bool((run[8] if len(run) > 8 else "") and (run[9] if len(run) > 9 else None))
            offset = 0 if (point_br or own_vals) else _cq_offset(base_pc, (p, c))
            mark_two = len(two_groups.get((*run[:7], mode), ())) > 1
            for base_val in _vals_of(run):
                if cancelled():
                    break
                val = base_val if point_br else max(1, min(63, base_val + offset))
                run_value(p, c, val, sp, bf, tag, aq=aq, ki=ki, tune=tune,
                          rate_mode=mode, row_two=_two_of(run), mark_two=mark_two)
            if cancelled():
                break

        # Ein Zwischenwert je Encoder zwischen letztem Treffer und erstem Fehlschlag.
        # Nur wenn der Lauf eine Stufe zum Encoden sucht. Ein reiner Vergleich
        # bleibt bei den eingetragenen Werten.
        if opts.refine_midpoint and not cancelled() and analysis.results:
            extras = _midpoint_jobs(analysis, opts.target_vmaf)
            if extras:
                budget["steps"] += len(extras)
                budget["units"] += len(extras) * n_samples * 2
                if status:
                    status("Zwischenwert zwischen Treffer und Fehlschlag …")
                for p, c, sp, bf, aq, ki, tune, mid in extras:
                    if cancelled():
                        break
                    tag = next((run[7] for run in runs
                                if run[:7] == (p, c, sp, bf, aq, ki, tune)), "")
                    run_value(p, c, mid, sp, bf, tag, extra=True, aq=aq, ki=ki, tune=tune)

        if analysis.results:
            analysis.results.sort(
                key=lambda r: (
                    r.platform, r.codec, r.encoder_speed, r.b_frames,
                    r.aq_strength, r.keyint_sec, r.nvenc_tune or "",
                    r.rate_mode or "", int(bool(r.two_pass)),
                    -int(r.value) if (r.rate_mode or "") in ("bitrate", "abr") else int(r.value),
                ))

        apply_idle_scenes(analysis.results)
        if opts.visual_only:
            analysis.recommended_value = None
            analysis.recommended_quality = None
        else:
            _pick_recommended(analysis, opts.target_vmaf)
        if analysis.results:
            _save_session(sess, analysis, opts.source_title,
                          source_path=opts.source_path or str(info.path), params=opts.params)
        elif not cancelled():
            # Kein einziges Ergebnis – Grund weiterreichen, damit der Job nicht
            # kommentarlos „fertig"/leer wird.
            analysis.error = last_error or (
                "VMAF-Analyse ohne Ergebnis: alle Test-Encodes sind "
                "fehlgeschlagen (Encoder/Plattform prüfen).")
    finally:
        _finalize_work(work, sess, keep_clips=True if opts.visual_only else None)

    return analysis


def _session_meta_path(sess: str) -> Path:
    return storage_policy.session_directory(config.PREVIEW_DIR, sess) / "analysis.json"


def _save_session(sess: str, analysis: VmafAnalysis, title: str,
                  source_path: str = "", params: Optional[dict] = None) -> None:
    """Analyse samt Metadaten neben den Screenshots ablegen (für Archiv-Ansicht)."""
    import time

    try:
        target = _session_meta_path(sess)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Prüfen, ob die Quelle für eine spätere Neu-Analyse noch existiert.
        src_ok = bool(source_path) and Path(source_path).is_file()
        payload = {
            "session": sess,
            "title": title or sess,
            "created": time.time(),
            "analysis": analysis.to_dict(),
            "source_path": source_path,
            "source_available": src_ok,
            "params": params or {},
        }
        with storage_policy.session_guard(sess):
            # Bei erneutem Speichern eines noch aktiven Laufs Schutz nicht verlieren.
            if target.is_file():
                previous = json.loads(target.read_text(encoding="utf-8"))
                payload["protected"] = bool(previous.get("protected", False))
            provenance_api.atomic_json_write(target, payload)
    except (OSError, ValueError) as e:
        logger.warning("VMAF-Session konnte nicht gespeichert werden: %s", e)


def list_sessions() -> list[dict]:
    """Alle archivierten VMAF-Vergleiche (neueste zuerst)."""
    root = config.PREVIEW_DIR
    if not root.exists():
        return []
    out: list[dict] = []
    for meta in root.glob("*/analysis.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("analysis"), dict):
            continue
        try:
            if _session_meta_path(meta.parent.name) != meta or meta.is_symlink():
                continue
        except (OSError, ValueError):
            continue
        analysis = data.get("analysis", {})
        results = analysis.get("results", [])
        rec = next((r for r in results if r.get("recommended")), None)
        # Quelle ggf. neu prüfen (könnte inzwischen verschoben/gelöscht sein).
        src = data.get("source_path", "")
        src_ok = bool(src) and Path(src).is_file()
        out.append({
            "session": data.get("session", meta.parent.name),
            "title": data.get("title", meta.parent.name),
            "created": data.get("created", meta.stat().st_mtime),
            "model": analysis.get("model", ""),
            "rate_mode": analysis.get("rate_mode", ""),
            "count": len(results),
            "multi_codec": analysis.get("multi_codec", False),
            "recommended_label": (rec or {}).get("label", ""),
            "source_available": src_ok,
            "protected": bool(data.get("protected", False)),
            "archive_context": provenance_api.archive_context(data),
            "coverage": provenance_api.analysis_coverage(analysis),
        })
    out.sort(key=lambda d: d.get("created", 0), reverse=True)
    return out


_CLIP_NAME = re.compile(r"^test_[A-Za-z0-9._-]+_s\d+\.mkv$")
_SOURCE_CLIP = re.compile(r"^source_s\d+\.mkv$")


def _clip_name(platform: str, codec: str, value, scene) -> str:
    try:
        val = int(value)
        sc = int(scene)
    except (TypeError, ValueError):
        return ""
    name = f"test_{platform}_{codec}_{val}_s{sc}.mkv"
    return name if _CLIP_NAME.match(name) else ""


def measured_kbps(size_bytes: int, seconds: float) -> int:
    """Mittlere Videobitrate aus Dateigröße und Dauer. Testclips sind video-only."""
    try:
        size = int(size_bytes)
        sec = float(seconds)
    except (TypeError, ValueError):
        return 0
    if size <= 0 or sec <= 0:
        return 0
    return int(round(size * 8 / sec / 1000))


def annotate_clips(session: str, analysis: dict) -> None:
    """Hängt vorhandene Testclips an die Screenshots (Dateiname, kein URL).

    Fehlende Bitraten kommen aus der Datei, sonst aus der gespeicherten
    Clip-Größe. So bleibt der Vergleich zum ABR-Ziel auch bei älteren Läufen.
    """
    if not session or not isinstance(analysis, dict):
        return
    analysis["session"] = session
    root = config.VMAF_SESSIONS_DIR / session
    seconds = float(analysis.get("clip_seconds") or 0)
    leads: dict[int, float] = {}
    for item in analysis.get("source_clips") or []:
        if not isinstance(item, dict):
            continue
        try:
            leads[int(item.get("scene"))] = float(item.get("lead") or 0)
        except (TypeError, ValueError):
            continue
    for raw in analysis.get("results") or []:
        if not isinstance(raw, dict):
            continue
        shot_kbps: list[int] = []
        for shot in raw.get("screenshots") or []:
            if not isinstance(shot, dict):
                continue
            name = shot.get("clip") or _clip_name(
                raw.get("platform") or "", raw.get("codec") or "",
                raw.get("value", raw.get("quality")), shot.get("scene", 0))
            path = root / name if name and _CLIP_NAME.match(name) else None
            if path is not None and path.is_file():
                shot["clip"] = name
                if not shot.get("kbps") and seconds > 0:
                    kbps = measured_kbps(path.stat().st_size, seconds)
                    if kbps:
                        shot["kbps"] = kbps
            else:
                shot.pop("clip", None)
            source_name = _source_clip_name(shot.get("scene", -1))
            source_path = root / source_name if source_name else None
            if source_path is not None and source_path.is_file():
                shot["source_clip"] = source_name
                shot["source_lead"] = round(float(leads.get(int(shot.get("scene") or 0), 0.0)), 3)
            else:
                shot.pop("source_clip", None)
                shot.pop("source_lead", None)
            if shot.get("kbps"):
                shot_kbps.append(int(shot["kbps"]))
        if raw.get("video_kbps"):
            continue
        kbps = 0
        if shot_kbps:
            kbps = int(round(sum(shot_kbps) / len(shot_kbps)))
        else:
            n = len(raw.get("scene_scores") or []) or len(shot_kbps) or 1
            kbps = measured_kbps(int(raw.get("clip_size_bytes") or 0), seconds * n)
        if kbps:
            raw["video_kbps"] = kbps
            raw["video_bitrate_human"] = ff._bitrate_human(kbps * 1000)


def _playable_clip(filename: str) -> bool:
    return bool(filename and (_CLIP_NAME.match(filename) or _SOURCE_CLIP.match(filename)))


def clip_path(session: str, filename: str) -> Optional[Path]:
    """Testclip oder Quell-Remux unter vmaf/<session>/, nur der erwartete Dateiname."""
    if not session or "/" in session or "\\" in session or session.startswith("."):
        return None
    if not _playable_clip(filename):
        return None
    root = (config.VMAF_SESSIONS_DIR / session).resolve()
    target = (root / filename).resolve()
    if target.parent != root or not target.is_file():
        return None
    return target


_LOG_NAME = re.compile(r"^vmaf_[A-Za-z0-9._-]+_s\d+\.json$")


def _encoder_args_text(cmd: list[str]) -> str:
    """Videofahnen ab ``-c:v`` bis vor ``-map`` oder ``-passlogfile``.

    Das ist der Teil, den der Encoder gesehen hat, nach der Karten-Probe.
    Eingabe, Filter und Ausgabe bleiben draußen. ``-pass 2`` bleibt drin.
    """
    try:
        start = cmd.index("-c:v")
    except ValueError:
        return ""
    stop = len(cmd)
    for i in range(start + 1, len(cmd)):
        if cmd[i] in ("-map", "-passlogfile", "-progress"):
            stop = i
            break
    return " ".join(cmd[start:stop])


def _frame_log_name(platform: str, codec: str, value, scene,
                    speed: str = "", b_frames: str = "",
                    aq: int | None = None, keyint: int | None = None,
                    tune: str | None = None,
                    rate_mode: str = "", two_pass: bool | None = None) -> str:
    try:
        val = int(value)
        sc = int(scene)
    except (TypeError, ValueError):
        return ""
    # Speed, B-Frames, AQ, Keyframe, Steuerungsmodus und Zwei-Pass gehören
    # zum Dateinamen. Sonst überschreiben sich ABR, CBR und Zwei-Pass bei
    # gleicher Bitrate, und der Leser findet die Datei nicht.
    extra = f"_{speed}_{b_frames}" if (speed or b_frames) else ""
    if extra and aq is not None:
        extra += f"_{int(aq)}_{int(keyint or 0)}"
    if extra and tune:
        extra += f"_{tune}"
    mode = ""
    if rate_mode:
        mode = f"_{rate_mode}_{1 if two_pass else 0}"
    name = f"vmaf_{platform}_{codec}{extra}{mode}_{val}_s{sc}.json"
    return name if _LOG_NAME.match(name) else ""


def _frame_log_path(root, raw: dict, scene: int):
    """Log-Datei eines Ergebnisses. Zuerst der aktuelle Name, dann der alte ohne Modus."""
    aq = raw["aq_strength"] if isinstance(raw, dict) and "aq_strength" in raw else None
    args = (
        raw.get("platform") or "", raw.get("codec") or "",
        raw.get("value", raw.get("quality")), scene,
        raw.get("encoder_speed") or "", raw.get("b_frames") or "",
        None if aq is None else int(aq),
        None if aq is None else int(raw.get("keyint_sec") or 0),
        raw.get("nvenc_tune") if "nvenc_tune" in raw else None,
    )
    mode = raw.get("rate_mode") or ""
    names = [
        _frame_log_name(*args, mode, bool(raw.get("two_pass"))),
        _frame_log_name(*args),
    ]
    for name in names:
        if not name:
            continue
        path = root / name
        if path.is_file():
            return path
    return None


def _pct(ordered: list[float], p: float) -> float:
    n = len(ordered)
    if n == 1:
        return ordered[0]
    k = (n - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _scored_window(duration: float) -> tuple[float, float]:
    """Gleicher Rand wie bei der VMAF-Bewertung, in Sekunden der Datei."""
    margin = _edge_margin(duration)
    if margin > 0 and duration > margin * 2 + 1:
        return margin, round(duration - margin, 3)
    return 0.0, float(duration or 0)


# show_existing_frame: ein paar Byte, das Bild wurde schon vorher codiert.
_SHOW_BYTES = 64


def _shown_share(packets: list[dict]) -> list[float]:
    """Bytes eines codierten Pakets auf dieses und folgende leere Anzeige-Frames teilen.

    Der Mittelwert über die Szene bleibt gleich. Ein leeres Paket am Anfang,
    dessen codiertes Bild vor dem Bewertungsfenster liegt, behält seine eigene Größe.
    """
    n = len(packets)
    out = [0.0] * n
    i = 0
    while i < n:
        if int(packets[i]["bytes"]) < _SHOW_BYTES:
            out[i] = float(packets[i]["bytes"])
            i += 1
            continue
        j = i + 1
        while j < n and int(packets[j]["bytes"]) < _SHOW_BYTES:
            j += 1
        total = sum(int(packets[k]["bytes"]) for k in range(i, j))
        share = total / (j - i)
        for k in range(i, j):
            out[k] = share
        i = j
    return out


# Drei Frames links und rechts. Ein einzelnes Referenzbild fällt darüber auf,
# eine Stelle, an der die Nachbarn selbst hoch sind, bleibt stehen.
_PEAK_HALF = 3
_PEAK_FACTOR = 2.5


def _body_kbps(values: list[float]) -> list[float]:
    """Einzelne Spitzen auf den Median der Nachbarframes setzen.

    Ein Frame über dem 2,5-fachen dieses Medians ist ein Referenzbild, das
    allein aus der Reihe tanzt. Liegen die Nachbarn genauso hoch, bleibt der
    Wert. Es wird nicht über die Zeit gemittelt, der Szenenschnitt der Kurve
    sinkt deshalb unter die Dateigröße.
    """
    n = len(values)
    if n < 3:
        return list(values)
    out = list(values)
    for i in range(n):
        lo = max(0, i - _PEAK_HALF)
        hi = min(n, i + _PEAK_HALF + 1)
        ordered = sorted(values[lo:hi])
        med = ordered[len(ordered) // 2]
        if med > 0 and values[i] > med * _PEAK_FACTOR:
            out[i] = med
    return out


def _curve_from_packets(packets: list[dict], duration: float = 0, fps: float = 0,
                        full: bool = False) -> dict:
    """Frame-Bitrate aus Paketen, deren Zeit bei 0 am Clipanfang liegt.

    ``full`` behält die ganze Szene. Sonst nur das bewertete Fenster, und t = 0
    ist dann das erste bewertete Bild.
    """
    if not packets:
        return {"bins": [], "scored_sec": 0, "frame": True}
    if fps <= 0:
        deltas = [
            packets[i + 1]["t"] - packets[i]["t"]
            for i in range(len(packets) - 1)
            if packets[i + 1]["t"] > packets[i]["t"] + 0.001
        ]
        fps = (1.0 / sorted(deltas)[len(deltas) // 2]) if deltas else 24.0
    frame_dt = 1.0 / max(fps, 1.0)
    if duration <= 0:
        duration = packets[-1]["t"] + frame_dt
    margin, win_end = _scored_window(duration)
    if full:
        start, end = 0.0, float(duration)
    else:
        start, end = margin, win_end
    kept = [pkt for pkt in packets if start <= float(pkt["t"]) < end]
    shares = _shown_share(kept)
    raw = [share * 8 / frame_dt / 1000 for share in shares]
    body = _body_kbps(raw)
    bins = []
    for n, pkt in enumerate(kept):
        rel = float(pkt["t"]) - start
        bins.append({
            "n": n,
            "t": round(max(0.0, rel), 4),
            "kbps": round(body[n], 1),
        })
    out = {
        "bins": bins,
        "scored_sec": round(max(0.0, end - start), 3),
        "frame": True,
        "align": "body",
    }
    if full:
        out["span"] = "scene"
        out["margin"] = round(margin, 3)
        out["scene_sec"] = round(float(duration), 3)
    return out


def scored_bitrate(path: Path, duration: float = 0, fps: float = 0) -> dict:
    """Bitrate je angezeigtem Frame, Zeitachse wie die bewerteten VMAF-Frames.

    t = 0 ist das erste bewertete Bild. n ist der Frame-Index dazu.
    Leere AV1-Anzeige-Frames teilen sich die Bits mit dem codierten Bild davor.
    Einzelne Referenz-Spitzen werden auf das Niveau der Nachbarframes gesetzt.
    """
    from . import bitrate_profile
    return _curve_from_packets(bitrate_profile.clip_packets(path), duration, fps)


def _read_source_cache(cache: Path) -> Optional[dict]:
    if not cache.is_file():
        return None
    try:
        blob = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(blob, dict) and blob.get("bins"):
        return blob
    return None


def source_scene_bitrate(session: str, scene: int) -> Optional[dict]:
    """Frame-Bitrate der Quelle über die ganze Szene.

    Kein VMAF. t = 0 ist der Szenenanfang, auch der Rand, den die Bewertung
    auslässt. Das Ergebnis liegt neben der Session. Eine ältere Datei, die nur
    das bewertete Fenster enthält, wird neu gemessen, solange die Quelle noch
    da ist. Fehlt die Quelle, bleibt diese ältere Kurve der Ersatz.
    """
    data = load_session(session)
    if data is None:
        return None
    cache = _session_meta_path(session).parent / f"source_bitrate_s{int(scene)}.json"
    blob = _read_source_cache(cache)
    if isinstance(blob, dict) and blob.get("span") == "scene":
        return blob
    src = Path(str(data.get("source_path") or ""))
    if not src.is_file():
        return blob
    start = length = None
    analysis = data.get("analysis") or {}
    for raw in analysis.get("results") or []:
        if not isinstance(raw, dict):
            continue
        for item in raw.get("scene_scores") or []:
            if not isinstance(item, dict) or item.get("scene") != int(scene):
                continue
            if item.get("start") is None:
                continue
            start = float(item.get("start") or 0)
            length = float(item.get("length") or 0)
            break
        if start is not None:
            break
    if start is None or length <= 0:
        return blob
    info = ff.ffprobe(src)
    fps = float(info.fps or 0) if info else 0.0
    from . import bitrate_profile
    end = start + length
    packets = bitrate_profile.clip_packets(src, start, end)
    if not packets:
        packets = [
            pkt for pkt in bitrate_profile.clip_packets(src)
            if start <= float(pkt["t"]) < end
        ]
    shifted = []
    for pkt in packets:
        rel = float(pkt["t"]) - start
        if rel < -0.001 or rel > length + 0.5:
            continue
        shifted.append({"t": max(0.0, rel), "bytes": int(pkt["bytes"])})
    curve = _curve_from_packets(shifted, length, fps, full=True)
    if not curve.get("bins"):
        return blob
    try:
        cache.write_text(json.dumps(curve), encoding="utf-8")
    except OSError as exc:
        logger.warning("Quell-Bitrate konnte nicht gespeichert werden: %s", exc)
    return curve


def _series_stats(frames: list[dict], clip_seconds: float) -> dict:
    """Verteilung der Frame-VMAFs. Keine neue Messung, nur die vorhandenen Logs."""
    vals = [float(f["vmaf"]) for f in frames]
    n = len(vals)
    if not n:
        return {}
    mean = sum(vals) / n
    stdev = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
    ordered = sorted(vals)
    k1 = max(1, int(n * 0.01))
    p1 = sum(ordered[:k1]) / k1
    longest = cur = 0
    start_at = at = 0
    for f in frames:
        if float(f["vmaf"]) < 90.0:
            if cur == 0:
                at = int(f["n"])
            cur += 1
            if cur > longest:
                longest = cur
                start_at = at
        else:
            cur = 0
    dip_sec = 0.0
    if longest and clip_seconds > 0:
        dip_sec = longest * float(clip_seconds) / n
    weak_n = max(1, int(round(n * 0.05)))
    ranked = sorted(frames, key=lambda f: float(f["vmaf"]))[:weak_n]
    out = {
        "mean": round(mean, 2),
        "median": round(_pct(ordered, 50), 2),
        "stdev": round(stdev, 2),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
        "p1": round(p1, 2),
        "p5": round(_pct(ordered, 5), 2),
        "p95": round(_pct(ordered, 95), 2),
        "under90": round(100.0 * sum(1 for v in vals if v < 90.0) / n, 1),
        "dip_frames": longest,
        "dip_at": start_at if longest else None,
        "dip_sec": round(dip_sec, 2) if dip_sec else 0,
    }
    psnr_vals = [float(f["psnr"]) for f in frames if f.get("psnr") is not None]
    psnr_weak = [float(f["psnr"]) for f in ranked if f.get("psnr") is not None]
    if psnr_vals and psnr_weak:
        pm = sum(psnr_vals) / len(psnr_vals)
        pw = sum(psnr_weak) / len(psnr_weak)
        out["psnr_mean"] = round(pm, 2)
        out["psnr_weak"] = round(pw, 2)
        out["psnr_delta"] = round(pm - pw, 2)
    return out


def _finite(value, ndigits: int):
    """NaN und Inf würden die ganze JSON-Antwort ungültig machen."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, ndigits)


def _dip_line(frames: list[dict], floor: float, frame_sec: float) -> str:
    """Dieselbe Zeile wie in den Nerd-Daten. Gerechnet über alle Frames."""
    n = len(frames)
    if not n:
        return ""
    under = 0
    longest = 0
    cur = 0
    start_at = 0
    at = 0
    for f in frames:
        if float(f["vmaf"]) < floor:
            under += 1
            if cur == 0:
                at = int(f["n"])
            cur += 1
            if cur > longest:
                longest = cur
                start_at = at
        else:
            cur = 0
    pct = 100.0 * under / n
    level = int(floor)
    if longest > 0:
        line = f"{pct:.1f} % unter {level} · längster Einbruch {longest} Frames ab Frame {start_at}"
        if frame_sec > 0:
            line += f" (~{longest * frame_sec:.2f} s)"
        return line
    return f"{pct:.1f} % unter {level} · kein Frame unter {level}"


def _downsample_min(frames: list[dict], buckets: int = 400) -> list[dict]:
    """Je Abschnitt den tiefsten Frame behalten. Die Tabelle nutzt die vollen Zahlen."""
    n = len(frames)
    if n <= buckets:
        return frames
    out = []
    for b in range(buckets):
        a = b * n // buckets
        z = max(a + 1, (b + 1) * n // buckets)
        out.append(min(frames[a:z], key=lambda f: float(f["vmaf"])))
    return out


def _low_scored_seconds(scene: dict, analysis: dict, params: dict) -> float:
    """Bewertetes Fenster, nicht die nominelle Cliplänge (Archiv-Fallback)."""
    stored = _finite(scene.get("scored_seconds"), 6)
    if stored is not None and stored > 0:
        return stored
    for value in (scene.get("length"), analysis.get("clip_seconds"), params.get("clip_seconds")):
        duration = _finite(value, 6)
        if duration is not None and duration > 0:
            start, end = _scored_window(duration)
            return max(0.0, end - start)
    return 0.0


def scene_frame_logs(session: str, scene: int, full: bool = False) -> Optional[dict]:
    """Jeden bewerteten Frame einer Szene aus den libvmaf-Logs."""
    data = load_session(session)
    if data is None:
        return None
    analysis = data.get("analysis") or {}
    params = data.get("params") or {}
    root = config.VMAF_SESSIONS_DIR / session
    series = []
    for raw in analysis.get("results") or []:
        if not isinstance(raw, dict):
            continue
        path = _frame_log_path(root, raw, scene)
        if path is None:
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        frames = []
        for fr in blob.get("frames") or []:
            metrics = fr.get("metrics") or {}
            vmaf = _finite(metrics.get("vmaf"), 2)
            if vmaf is None:
                continue
            item = {
                "n": int(fr.get("frameNum") if fr.get("frameNum") is not None else len(frames)),
                "vmaf": vmaf,
            }
            if full:
                # Grenzvergleiche im Kontext nicht mit auf 2 Stellen gerundeten Scores.
                item["vmaf_raw"] = float(metrics["vmaf"])
            psnr = _finite(metrics.get("psnr_y", metrics.get("psnr")), 2)
            ssim = _finite(metrics.get("float_ssim", metrics.get("ssim")), 4)
            if psnr is not None:
                item["psnr"] = psnr
            if ssim is not None:
                item["ssim"] = ssim
            frames.append(item)
        worst = sorted(frames, key=lambda x: x["vmaf"])[:8]
        n_frames = len(frames)
        scene_score = next((sc for sc in raw.get("scene_scores") or []
                            if sc.get("scene") == scene), {})
        scored_seconds = _low_scored_seconds(scene_score, analysis, params)
        frame_sec = scored_seconds / n_frames if n_frames else 0.0
        dips = {
            str(level): _dip_line(frames, float(level), frame_sec)
            for level in range(80, 100)
        }
        series.append({
            "label": raw.get("label") or "",
            "count": n_frames,
            "frame_sec": round(frame_sec, 5),
            "min": worst[0]["vmaf"] if worst else None,
            "min_frame": worst[0]["n"] if worst else None,
            "frames": frames if full else _downsample_min(frames),
            "dips": dips,
            "worst": worst,
            "stats": _series_stats(frames, scored_seconds),
            "low_context": {
                "count": n_frames,
                "p1_count": max(1, int(n_frames * 0.01)) if n_frames else 0,
                "scored_seconds": scored_seconds,
            },
        })
    return {"scene": int(scene), "series": series}


def weak_spots(session: str, result_index: Optional[int] = None,
               duration: float = 0.0, limit: int = 12) -> Optional[dict]:
    """Die schwächsten Stellen eines Ergebnisses als Filmzeiten.

    Aus den libvmaf-Frame-Logs je Szene: Szenenstart + Frame-Index × Framedauer.
    Ohne Frame-Logs bleibt der Szenenstart mit dem Szenen-VMAF. Für den
    Vorher/Nachher-Vergleich im Player.
    """
    data = load_session(session)
    if data is None:
        return None
    analysis = data.get("analysis") or {}
    params = data.get("params") or {}
    results = [r for r in (analysis.get("results") or []) if isinstance(r, dict)]
    if not results:
        return None
    if result_index is None or not (0 <= int(result_index) < len(results)):
        idx = next((i for i, r in enumerate(results) if r.get("recommended")), 0)
    else:
        idx = int(result_index)
    res = results[idx]
    try:
        clip_seconds = float(analysis.get("clip_seconds") or params.get("clip_seconds") or 0)
    except (TypeError, ValueError):
        clip_seconds = 0.0
    scenes = [s for s in (res.get("scene_scores") or []) if isinstance(s, dict)]
    # Szenenstarts: aus den Scores, sonst aus der Analyse, sonst nachrechnen.
    starts = analysis.get("sample_starts") or []
    if not starts and duration > 0:
        try:
            n = int(params.get("samples") or len(scenes) or 1)
            starts = _sample_starts(float(duration), int(clip_seconds or 30), n)
        except (TypeError, ValueError):
            starts = []
    spots = []
    root = config.VMAF_SESSIONS_DIR / session
    for s in scenes:
        si = int(s.get("scene", 0))
        start = s.get("start")
        length = s.get("length") or clip_seconds
        if start is None and si < len(starts):
            start, length = float(starts[si][0]), float(starts[si][1] or clip_seconds)
        if start is None:
            continue
        start = float(start)
        frames = []
        path = _frame_log_path(root, res, si)
        if path is not None:
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                for fr in blob.get("frames") or []:
                    v = (fr.get("metrics") or {}).get("vmaf")
                    if v is None:
                        continue
                    frames.append((int(fr.get("frameNum") or len(frames)), float(v)))
            except (OSError, ValueError):
                frames = []
        if frames:
            frame_sec = float(length) / len(frames) if length else 0.0
            # Pro Szene die drei schwächsten, aber nicht direkt benachbart
            # (sonst dreimal dieselbe Stelle).
            picked: list[tuple[int, float]] = []
            for n, v in sorted(frames, key=lambda f: f[1]):
                if any(abs(n - p[0]) * frame_sec < 1.0 for p in picked):
                    continue
                picked.append((n, v))
                if len(picked) >= 3:
                    break
            for n, v in picked:
                spots.append({"time": round(start + n * frame_sec, 2), "vmaf": round(v, 2),
                              "scene": si, "frame": n, "kind": "frame"})
        else:
            spots.append({"time": round(start, 2), "vmaf": round(float(s.get("vmaf") or 0), 2),
                          "scene": si, "kind": "scene"})
    spots.sort(key=lambda x: x["vmaf"])
    return {
        "session": session,
        "label": res.get("label") or "",
        "vmaf": res.get("vmaf"),
        "spots": spots[:max(1, int(limit))],
        "scenes": [{"scene": int(s.get("scene", 0)),
                    "start": (float(s["start"]) if s.get("start") is not None
                              else (float(starts[int(s.get("scene", 0))][0])
                                    if int(s.get("scene", 0)) < len(starts) else None)),
                    "vmaf": round(float(s.get("vmaf") or 0), 2)} for s in scenes],
    }


def repick_analysis(analysis: dict, target_vmaf: float = 0.0) -> dict:
    """Empfehlung mit den aktuellen Einstellungen neu setzen. Werte bleiben.

    Szenen, die bei jeder Stufe bei 99 oder darüber liegen, fallen vorher aus
    Schnitt und 1%-Low. Die Szenenzeile bleibt stehen.
    """
    apply_idle_scenes(analysis.get("results") or [])
    built: list[tuple[dict, VmafResult]] = []
    for raw in analysis.get("results") or []:
        if not isinstance(raw, dict):
            continue
        result = VmafResult(
            value=int(raw.get("value") or raw.get("quality") or 0),
            rate_mode=raw.get("rate_mode") or analysis.get("rate_mode") or "cq",
            label=raw.get("label") or "",
            vmaf=float(raw.get("vmaf") or 0),
            clip_size_bytes=int(raw.get("clip_size_bytes") or 0),
            predicted_size_bytes=int(raw.get("predicted_size_bytes") or 0),
            savings_percent=float(raw.get("savings_percent") or 0),
            codec=raw.get("codec") or "av1",
            platform=raw.get("platform") or "cpu",
            encoder_speed=str(raw.get("encoder_speed") or ""),
            b_frames=str(raw.get("b_frames") or ""),
            aq_strength=int(raw.get("aq_strength") or 0),
            keyint_sec=int(raw.get("keyint_sec") or 0),
            nvenc_tune=str(raw.get("nvenc_tune") or ""),
            two_pass=bool(raw.get("two_pass")),
            vmaf_hmean=float(raw.get("vmaf_hmean") or 0),
            vmaf_1pct=float(raw.get("vmaf_1pct") or 0),
            psnr=float(raw.get("psnr") or 0),
            ssim=float(raw.get("ssim") or 0),
            scene_scores=list(raw.get("scene_scores") or []),
        )
        raw["recommended"] = False
        built.append((raw, result))
    picked = VmafAnalysis(
        results=[result for _, result in built],
        rate_mode=analysis.get("rate_mode") or "cq",
    )
    _pick_recommended(picked, target_vmaf)
    flags = {
        (r.platform, r.codec, r.encoder_speed, r.b_frames,
         int(r.aq_strength), int(r.keyint_sec), r.nvenc_tune or "",
         r.rate_mode or "", int(bool(r.two_pass)), int(r.value)): r.recommended
        for r in picked.results
    }
    for raw, result in built:
        raw["recommended"] = bool(flags.get(
            (result.platform, result.codec, result.encoder_speed,
             result.b_frames, int(result.aq_strength), int(result.keyint_sec),
             result.nvenc_tune or "", result.rate_mode or "",
             int(bool(result.two_pass)), int(result.value))))
    analysis["recommended_value"] = picked.recommended_value
    analysis["recommended_quality"] = picked.recommended_quality
    analysis["recommended_codec"] = picked.recommended_codec
    analysis["recommended_platform"] = picked.recommended_platform
    analysis["keep_source"] = picked.keep_source
    analysis["pick_warning"] = picked.pick_warning
    analysis["target_lo"] = picked.target_lo
    analysis["target_gap"] = picked.target_gap
    analysis["target_anchor"] = picked.target_anchor
    return analysis


def repick_session(session: str) -> Optional[dict]:
    """Gespeicherte Session neu einordnen und zurückschreiben."""
    try:
        with storage_policy.session_guard(session):
            data = load_session(session)
            if data is None or not isinstance(data.get("analysis"), dict):
                return None
            params = data.get("params") or {}
            try:
                target = float(params.get("target_vmaf") or 0)
            except (TypeError, ValueError):
                target = 0.0
            repick_analysis(data["analysis"], target)
            saved = {k: v for k, v in data.items() if k not in ("archive_context", "coverage")}
            provenance_api.atomic_json_write(_session_meta_path(session), saved)
    except (OSError, ValueError) as e:
        logger.warning("Neu-Einordnung konnte nicht gespeichert werden: %s", e)
        return None
    return data


def load_session(name: str) -> Optional[dict]:
    """Gespeicherte Analyse eines Vergleichs laden (oder None)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    try:
        meta = _session_meta_path(name)
        if meta.is_symlink() or not meta.is_file():
            return None
        data = json.loads(meta.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
    except (OSError, ValueError):
        return None
    analysis = data.get("analysis")
    if isinstance(analysis, dict):
        annotate_clips(data.get("session") or name, analysis)
        # Anzeige korrigieren, ohne die Datei umzuschreiben. Neu einordnen speichert.
        if apply_idle_scenes(analysis.get("results") or []):
            params = data.get("params") or {}
            try:
                target = float(params.get("target_vmaf") or 0)
            except (TypeError, ValueError):
                target = 0.0
            repick_analysis(analysis, target)
    return provenance_api.enrich_session(data)


def sessions_for_source(abs_path: str, limit: int = 20) -> list[dict]:
    """Archivierte VMAF-Sessions zu einem Quellpfad."""
    if not abs_path:
        return []
    want = str(Path(abs_path).resolve()) if Path(abs_path).exists() else str(abs_path)
    out: list[dict] = []
    root = config.PREVIEW_DIR
    if not root.exists():
        return []
    for meta in root.glob("*/analysis.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        src = data.get("source_path") or ""
        try:
            src_r = str(Path(src).resolve()) if src and Path(src).exists() else src
        except OSError:
            src_r = src
        if src_r != want and src != abs_path:
            continue
        analysis = data.get("analysis", {})
        results = analysis.get("results", [])
        rec = next((r for r in results if r.get("recommended")), None)
        out.append({
            "session": data.get("session", meta.parent.name),
            "title": data.get("title", meta.parent.name),
            "created": data.get("created", meta.stat().st_mtime),
            "recommended_label": (rec or {}).get("label", ""),
            "recommended_vmaf": (rec or {}).get("vmaf"),
            "count": len(results),
        })
    out.sort(key=lambda d: d.get("created", 0), reverse=True)
    return out[:limit]


def _copied_payload_bytes(info: VideoInfo, params: Optional[dict] = None) -> int:
    """Geschätzte Bytes, die neben dem neuen Video in die Ausgabe wandern (Ton)."""
    params = params or {}
    dur = float(info.duration or 0.0)
    if dur <= 0:
        return 0
    audio_mode = str(params.get("audio_mode") or "copy").lower()
    if audio_mode in ("none", "drop", "off"):
        return 0
    audios = list(info.audio or [])
    tracks = params.get("audio_tracks") or []
    if tracks and audios:
        sel = []
        for idx in tracks:
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(audios):
                sel.append(audios[i])
        if sel:
            audios = sel
    n = max(1, len(audios)) if audios else 1
    if audio_mode in ("encode", "transcode"):
        kbps = int(params.get("audio_bitrate") or params.get("audio_bitrate_kbps") or 160)
        return int(kbps * 1000 / 8.0 * dur * n)
    # copy: Stream-Bitraten, sonst Dateigröße minus Video-Schätzung
    copied = 0
    for a in audios:
        br = int(a.get("bitrate") or 0) if isinstance(a, dict) else 0
        if br > 0:
            copied += int(br / 8.0 * dur)
    if copied > 0:
        return copied
    vb = int(info.video_bitrate or 0)
    if vb > 0 and info.size_bytes > 0:
        return max(0, info.size_bytes - int(vb / 8.0 * dur))
    return 0


def _target_lo(target_vmaf: float) -> float:
    """Job-Ziel, sonst das Ziel aus den Einstellungen."""
    if target_vmaf and target_vmaf > 0:
        return float(target_vmaf)
    from . import app_settings
    return float(app_settings.vmaf_target())


# Szene zählt nicht mit, wenn jede Stufe dort mindestens so hoch liegt.
# Darunter trennt sie die Einstellungen nicht (Abspann, Standbild).
IDLE_VMAF = 99.0


def _scene_rows(results) -> list[list]:
    rows = []
    for row in results or []:
        scenes = row.get("scene_scores") if isinstance(row, dict) else getattr(row, "scene_scores", None)
        if scenes:
            rows.append(scenes)
    return rows


def idle_scene_ids(results) -> set[int]:
    """Szenen, die bei jeder Stufe bei VMAF 99 oder darüber liegen.

    Bleibt mindestens eine Szene übrig. Fehlt einer Stufe die Szene, bleibt sie drin.
    """
    rows = _scene_rows(results)
    if not rows:
        return set()
    sets: list[set[int]] = []
    for scenes in rows:
        ids: set[int] = set()
        for s in scenes:
            if not isinstance(s, dict) or s.get("vmaf") is None:
                continue
            try:
                ids.add(int(s.get("scene", 0)))
            except (TypeError, ValueError):
                continue
        if not ids:
            return set()
        sets.append(ids)
    common = set.intersection(*sets)
    if len(common) < 2:
        return set()
    idle: set[int] = set()
    for si in common:
        vals: list[float] = []
        for scenes in rows:
            for s in scenes:
                try:
                    if int(s.get("scene", -1)) != si:
                        continue
                    vals.append(float(s.get("vmaf")))
                except (TypeError, ValueError):
                    continue
                break
        if vals and min(vals) >= IDLE_VMAF:
            idle.add(si)
    if not idle or len(idle) >= len(common):
        return set()
    return idle


def _put_metric(row, name: str, value: float) -> None:
    shown = round(float(value), 4 if name == "ssim" else 2)
    if isinstance(row, dict):
        row[name] = shown
    else:
        setattr(row, name, shown)


def apply_idle_scenes(results) -> set[int]:
    """Schnitt und 1%-Low ohne Szenen, die jede Stufe bei 99 oder darüber lassen.

    Die Szenenzeile bleibt, mit ``idle``. Größe und Bitrate bleiben unverändert.
    """
    idle = idle_scene_ids(results)
    if not idle:
        return set()
    pairs = (
        ("vmaf", "vmaf"),
        ("hmean", "vmaf_hmean"),
        ("p1", "vmaf_1pct"),
        ("psnr", "psnr"),
        ("ssim", "ssim"),
        ("xpsnr", "xpsnr"),
        ("cambi", "cambi"),
        ("ssimulacra2", "ssimulacra2"),
    )
    for row in results or []:
        scenes = row.get("scene_scores") if isinstance(row, dict) else row.scene_scores
        if not scenes:
            continue
        buckets: dict[str, list[float]] = {src: [] for src, _dest in pairs}
        counted: list[dict] = []
        for s in scenes:
            if not isinstance(s, dict):
                continue
            try:
                si = int(s.get("scene", 0))
            except (TypeError, ValueError):
                s["idle"] = False
                continue
            on = si in idle
            s["idle"] = on
            if on:
                continue
            counted.append(s)
            for src, _dest in pairs:
                raw = s.get(src)
                if src in ("cambi", "ssimulacra2"):
                    if raw is None:
                        continue
                elif not raw:
                    continue
                try:
                    buckets[src].append(float(raw))
                except (TypeError, ValueError):
                    continue
        if not buckets["vmaf"]:
            continue
        for src, dest in pairs:
            vals = buckets[src]
            if vals:
                _put_metric(row, dest, sum(vals) / len(vals))
        if isinstance(row, dict):
            row["vmaf_score"] = round(quality_score(
                float(row.get("vmaf") or 0),
                float(row.get("vmaf_1pct") or 0),
                float(row.get("vmaf_hmean") or 0),
            ), 2)
            vm = [float(s["vmaf"]) for s in counted if s.get("vmaf") is not None]
            if vm:
                row["vmaf_min"] = round(min(vm), 2)
                row["vmaf_max"] = round(max(vm), 2)
    return idle


def floor_p1(scene_scores, overall: float = 0.0, mean: float = 0.0) -> float:
    """1%-Low für den Floor: schwächste Szene, nicht der Schnitt der Szenen.

    Ohne Szenen-1%-Low (ältere Archive) bleibt der bisherige Gesamtwert.
    Szenen, die bei jeder Stufe auf 99 oder darüber liegen, zählen nicht.
    """
    lows = []
    for s in scene_scores or []:
        if not isinstance(s, dict) or s.get("idle"):
            continue
        raw = s.get("p1")
        if raw:
            lows.append(float(raw))
    if lows:
        return min(lows)
    if overall:
        return float(overall)
    return float(mean or 0)


def _p1_floors(mean: float, lo: float, gap: float, anchor: str) -> list[float]:
    """Untere Grenzen für das 1%-Low der schwächsten Szene."""
    if gap <= 0:
        return []
    floors: list[float] = []
    if anchor in ("both", "target"):
        floors.append(float(lo) - float(gap))
    if anchor in ("both", "mean"):
        floors.append(float(mean) - float(gap))
    return floors


def _result_solid(r: VmafResult, lo: float, gap: float) -> bool:
    if r.vmaf < lo:
        return False
    from . import app_settings
    p1 = floor_p1(r.scene_scores, r.vmaf_1pct, r.vmaf)
    return all(p1 + 1e-9 >= lim for lim in _p1_floors(
        r.vmaf, lo, gap, app_settings.vmaf_p1_anchor()))


def _midpoint_jobs(analysis: VmafAnalysis, target_vmaf: float) -> list[tuple]:
    """Ein Zwischenwert je Encoder zwischen letztem Treffer und erstem Fehlschlag."""
    from . import app_settings
    lo = _target_lo(target_vmaf)
    gap = app_settings.vmaf_p1_gap()
    bitrate = analysis.rate_mode in ("bitrate", "abr")
    jobs: list[tuple] = []
    groups: dict[tuple, list[VmafResult]] = {}
    for r in analysis.results:
        groups.setdefault(
            (r.platform, r.codec, r.encoder_speed, r.b_frames,
             int(r.aq_strength), int(r.keyint_sec), r.nvenc_tune or ""), []).append(r)
    for (p, c, sp, bf, aq, ki, tune), rows in groups.items():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: r.value, reverse=bitrate)
        seen = {int(r.value) for r in rows}
        pair = None
        for a, b in zip(rows, rows[1:]):
            if _result_solid(a, lo, gap) and not _result_solid(b, lo, gap):
                pair = (int(a.value), int(b.value))
                break
        if not pair:
            # Nichts solid: Mitte zwischen bestem 1%-Low und größter Ersparnis
            # unter den Sparern, damit der Kompromiss nicht nur am groben Raster hängt.
            min_sav = app_settings.vmaf_min_savings()
            if min_sav is None:
                continue
            savers = [r for r in rows
                      if r.savings_percent >= min_sav and r.vmaf >= lo]
            if len(savers) < 2:
                continue
            a = max(savers, key=lambda r: (
                r.vmaf_1pct if r.vmaf_1pct else r.vmaf, -r.predicted_size_bytes))
            b = min(savers, key=lambda r: r.predicted_size_bytes)
            if int(a.value) == int(b.value):
                continue
            pair = (int(a.value), int(b.value))
        v1, v2 = pair
        if bitrate:
            if abs(v1 - v2) < 250:
                continue
            mid = int(round((v1 + v2) / 2 / 100.0) * 100)
            if mid <= 0 or mid in seen:
                continue
        else:
            if abs(v1 - v2) < 2:
                continue
            mid = (v1 + v2) // 2
            if mid in seen:
                continue
        jobs.append((p, c, sp, bf, aq, ki, tune, mid))
    return jobs


def _p1_of(r: VmafResult) -> float:
    """Vergleichswert der Empfehlung: 1%-Low der schwächsten Szene."""
    return floor_p1(r.scene_scores, r.vmaf_1pct, r.vmaf)


def _p1_slack(gap: float) -> float:
    """Spielraum unter dem besten 1%-Low der Sparer, wenn der Floor verfehlt ist.

    2 Punkte sind oft kaum sichtbar (außer in den schwierigsten Frames).
    gap/3 koppelt das an den Floor-Slider (Vorgabe 6 → 2).
    """
    return max(2.0, float(gap or 0) / 3.0)


def _compromise_saver(pool: list, gap: float) -> VmafResult:
    """Unter sparenden Stufen: nahe am besten 1%-Low, dort die kleinste Datei."""
    best_p1 = max(_p1_of(r) for r in pool)
    limit = best_p1 - _p1_slack(gap)
    near = [r for r in pool if _p1_of(r) + 1e-9 >= limit]
    if not near:
        near = list(pool)
    return min(near, key=lambda r: (r.predicted_size_bytes, -_p1_of(r)))


def _savings_pick_warning(best: VmafResult, lo: float, gap: float,
                          pool: list) -> str:
    """Hinweis: Floor verfehlt, Kompromiss aus Ersparnis-Pflicht und 1%-Low."""
    p1 = _p1_of(best)
    from . import app_settings
    anchor = app_settings.vmaf_p1_anchor()
    floor = lo - gap if gap > 0 else lo
    slack = _p1_slack(gap)
    best_p1_row = max(pool, key=lambda r: (_p1_of(r), -r.predicted_size_bytes))
    most_save = min(pool, key=lambda r: r.predicted_size_bytes)
    if anchor == "mean":
        rule = f"mehr als {gap:.0f} unter dem Filmschnitt"
    elif anchor == "both":
        rule = (f"unter Floor {floor:.0f} oder mehr als {gap:.0f} unter dem Filmschnitt "
                f"(Ziel {lo:.0f})")
    else:
        rule = f"unter Floor {floor:.0f} (Ziel {lo:.0f})"
    bits = [
        f"Kompromiss: 1%-Low der schwächsten Szene {rule}, "
        f"Ersparnis war Pflicht. Gewählt: {best.label} · "
        f"VMAF {best.vmaf:.1f} · 1%-Low {p1:.1f} (Fenster {slack:.0f} Punkte "
        f"unter bestem Sparer "
        f"{_p1_of(best_p1_row):.1f}) · Ersparnis {best.savings_percent:+.1f} %.",
    ]
    if int(best_p1_row.value) != int(best.value):
        bits.append(
            f"Näher am 1%-Low wäre {best_p1_row.label} "
            f"({_p1_of(best_p1_row):.1f}, {best_p1_row.savings_percent:+.1f} %).")
    if int(most_save.value) != int(best.value):
        bits.append(
            f"Mehr Ersparnis wäre {most_save.label} "
            f"({_p1_of(most_save):.1f}, {most_save.savings_percent:+.1f} %).")
    return " ".join(bits)


def _pick_recommended(analysis: VmafAnalysis, target_vmaf: float = 0.0) -> None:
    if not analysis.results:
        return
    # Ziel-VMAF (Slider / Super-Tool) bleibt der Mittelwert – so wie angegeben.
    # Der Floor gilt für die schwächste Szene, nicht für den Schnitt der
    # Szenen-1%-Lows. Vier ruhige Szenen dürfen eine harte Szene nicht verdecken.
    lo = _target_lo(target_vmaf)
    from . import app_settings
    gap = app_settings.vmaf_p1_gap()
    analysis.target_lo = float(lo)
    analysis.target_gap = float(gap)
    analysis.target_anchor = app_settings.vmaf_p1_anchor()
    min_sav = app_settings.vmaf_min_savings()

    def _most_savings(rows: list) -> VmafResult:
        return min(rows, key=lambda r: (r.predicted_size_bytes, -_p1_of(r)))

    mean_ok = [r for r in analysis.results if r.vmaf >= lo]
    solid = [r for r in analysis.results if _result_solid(r, lo, gap)]

    def _saving_ok(rows: list) -> list:
        if min_sav is None:
            return list(rows)
        return [r for r in rows if r.savings_percent >= min_sav]

    compact = _saving_ok(solid)
    analysis.keep_source = False
    analysis.pick_warning = ""
    if compact:
        best = _most_savings(compact)
    elif solid:
        # Ziel gehalten, aber jede Stufe wäre größer als die Quelle.
        analysis.keep_source = True
        best = _most_savings(solid)
    elif min_sav is not None and (_saving_ok(mean_ok) or _saving_ok(analysis.results)):
        # Floor verfehlt: Ersparnis ist Pflicht, 1%-Low bleibt ein Fenster –
        # nicht die kleinste Datei um jeden Preis und nicht das beste Low.
        pool = _saving_ok(mean_ok) or _saving_ok(analysis.results)
        best = _compromise_saver(pool, gap)
        analysis.pick_warning = _savings_pick_warning(best, lo, gap, pool)
    elif mean_ok:
        if min_sav is not None:
            analysis.keep_source = True
        best = max(mean_ok, key=lambda r: (_p1_of(r), -r.predicted_size_bytes))
    else:
        compact_any = _saving_ok(analysis.results)
        if compact_any:
            best = max(
                compact_any,
                key=lambda r: (
                    quality_score(r.vmaf, r.vmaf_1pct, r.vmaf_hmean), r.vmaf),
            )
        else:
            analysis.keep_source = True
            best = max(
                analysis.results,
                key=lambda r: (
                    quality_score(r.vmaf, r.vmaf_1pct, r.vmaf_hmean), r.vmaf),
            )
    if not analysis.keep_source:
        best.recommended = True
    analysis.recommended_value = best.value
    analysis.recommended_quality = best.value
    analysis.recommended_codec = best.codec
    analysis.recommended_platform = best.platform


def _finalize_work(work: Path, item_id: str, keep_clips: Optional[bool] = None) -> None:
    """VMAF-Arbeitsordner löschen oder dauerhaft unter vmaf/ ablegen."""
    if not work.exists():
        return
    # Die verlustfreien Referenzen (FFV1) sind riesig (mehrere GB bei 4K/HDR)
    # und nach der Analyse wertlos – vor dem Archivieren immer entfernen.
    # Testclips (die encodierten Szenen) bleiben, solange die Einstellung das will.
    try:
        for ref in work.glob("reference_*.mkv"):
            ref.unlink(missing_ok=True)
        (work / "reference.mkv").unlink(missing_ok=True)
        from . import app_settings
        retain = app_settings.keep_vmaf_clips() if keep_clips is None else bool(keep_clips)
        if not retain:
            for clip in work.glob("test_*.mkv"):
                clip.unlink(missing_ok=True)
    except OSError:
        pass
    if config.RETAIN_VMAF_SESSIONS and item_id:
        config.VMAF_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.VMAF_SESSIONS_DIR / item_id
        _cleanup(dest)
        try:
            work.rename(dest)
            return
        except OSError:
            import shutil
            try:
                shutil.move(str(work), str(dest))
                return
            except OSError as e:
                logger.warning("VMAF-Session konnte nicht archiviert werden: %s", e)
    _cleanup(work)


def _cleanup(work: Path) -> None:
    import shutil
    try:
        if work.is_dir():
            shutil.rmtree(work, ignore_errors=True)
        elif work.exists():
            work.unlink(missing_ok=True)
    except OSError:
        pass
