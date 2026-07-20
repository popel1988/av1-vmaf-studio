"""Asynchrone Hintergrund-Warteschlange (eigener Worker-Thread).

Jeder Eintrag (QueueItem) entspricht einer Datei. Ein Batch erzeugt mehrere
Einträge mit gemeinsamer group_id: Der VMAF-Test wird repräsentativ für die
erste Datei der Gruppe durchgeführt und der ermittelte Qualitätswert auf alle
weiteren Dateien angewendet.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import config
from . import ffmpeg_utils as ff
from . import vmaf as vmaf_mod
from .encoder import EncodeProgress, EncodeRunner, build_encode_cmd
from .ffmpeg_utils import VideoInfo, ffprobe, probe_with_error

logger = logging.getLogger("vcompress.queue")

STATUS_WAITING = "wartend"
STATUS_ANALYZING = "vmaf-test"
STATUS_AWAITING = "auswahl"
STATUS_RUNNING = "in arbeit"
STATUS_DONE = "fertig"
STATUS_FAILED = "fehlgeschlagen"
STATUS_CANCELLED = "abgebrochen"

# Container-Endung je Zielcodec
CONTAINER = {"av1": ".mkv", "hevc": ".mkv", "h264": ".mp4"}


@dataclass
class JobSettings:
    platform: str = "cpu"
    codec: str = "av1"
    quality: int = 28
    target_height: Optional[int] = None
    tonemap: bool = False
    preserve_hdr: bool = False       # HDR10/HLG erhalten statt SDR-Tonemapping
    preserve_dv: bool = False        # Dolby-Vision-RPU nach Encode re-injizieren (HEVC 8.1 / AV1 10.1)
    keep_subtitles: bool = True      # Untertitel-Spuren übernehmen (alle)
    subtitle_per_track: bool = False # Untertitel pro Spur konfiguriert
    subtitle_track_settings: list = field(default_factory=list)  # je Spur: index/default/forced
    keep_chapters: bool = True       # Kapitelmarken übernehmen
    keep_metadata: bool = True       # Container-/Stream-Metadaten übernehmen
    film_grain: int = 0              # AV1 (SVT) Film-Grain-Synthese 0=aus..50
    denoise: str = "off"             # off | light | medium | strong
    two_pass: bool = False           # Zwei-Pass (nur Bitraten-Modus sinnvoll)
    anime: bool = False              # Anime-Modus: VMAF-NEG-Modell + 10-bit-Ausgabe
    # Audio-Optimierung: video_mode="copy" => nur Remux (Video 1:1), Tonspuren
    # werden je nach scope (bloated|all) transcodiert. Kein VMAF/Encode.
    video_mode: str = "encode"       # encode | copy
    audio_opt_scope: str = "bloated" # bloated | all
    audio_min_bitrate_kbps: int = 700
    # Per-Szene / Chunked Adaptive Encoding (nur CQ-Modus): Segmente mit
    # komplexitätsabhängigem CQ, danach verlustfrei zusammengefügt.
    chunked: bool = False
    chunk_seconds: int = 60
    chunk_cq_range: int = 6
    # Qualitäts-Guardrail: echten VMAF nach dem Encode messen und optional
    # bei Unterschreiten des Ziels automatisch mit höherer Qualität neu encoden.
    verify_vmaf: bool = False
    verify_min: float = 93.0
    verify_retry: bool = False
    # Playability-/Integritäts-Check der Ausgabe nach dem Encode (Voll-Decode +
    # Dauer-Abgleich). Voraussetzung für die sichere Original-Nachbehandlung.
    integrity_check: bool = True
    # Sichere Original-Nachbehandlung: Original nur löschen/verschieben (inplace/
    # archive), wenn Integritäts-Check UND (falls aktiv) Guardrail bestanden sind.
    safe_replace: bool = True
    # Auto-Crop: schwarze Balken automatisch erkennen (cropdetect) und beschneiden.
    autocrop: bool = False
    vmaf_check: bool = True
    workflow: str = "auto"         # auto | manual | compare_only
    target_vmaf: float = 0.0       # >0: Ziel-VMAF (Super-Tool), sonst Sweetspot
    rate_mode: str = "cq"          # cq | bitrate | abr
    # Zusätzliche Vergleichs-Encoder als "plattform:codec"-Strings (z. B. "cpu:hevc")
    compare_encoders: list = field(default_factory=list)
    test_values: list = field(default_factory=lambda: [20, 24, 28, 32])
    clip_seconds: int = 30
    samples: int = 1               # VMAF-Stichproben-Clips (1 = nur Mitte)
    generate_screenshots: bool = True
    post_processing: str = "keep"
    container: str = "auto"        # auto | mkv | mp4 (Ausgabe-Container)
    suffix: str = "_av1"
    # Dateiname: Platzhalter {stem}{suffix}{codec}{height}{height_suffix}{vmaf}{date}
    name_pattern: str = "{stem}{suffix}"
    # Bei existierendem Ziel / History: ask | skip | overwrite
    on_duplicate: str = "ask"
    # Caps: 0 = aus. Nach Encode prüfen; bei ABR zusätzlich als Obergrenze.
    max_output_mb: float = 0.0
    max_video_bitrate_kbps: int = 0
    # Audio
    audio_mode: str = "copy"       # copy | encode | none
    audio_codec: str = "aac"       # aac | opus | ac3 | eac3 | flac
    audio_bitrate: int = 160       # kbit/s pro Stream
    audio_channels: int = 0        # 0 = Original, 1 = Mono, 2 = Stereo
    audio_normalize: bool = False
    audio_tracks: list = field(default_factory=list)  # leer = alle Spuren
    audio_per_track: bool = False                      # Audio pro Spur konfiguriert
    audio_track_settings: list = field(default_factory=list)  # je Spur ein Dict
    selected_result_index: Optional[int] = None
    batch_id: str = ""             # Super-Tool-Stapelkennung (Dashboard-Filter)
    # Remux-/Bearbeiten-Modus (video_mode="edit"): Spuren entfernen/umsortieren,
    # Flags/Sprache/Titel ändern, externe Spuren hinzufügen – ohne Re-Encode.
    edit_spec: dict = field(default_factory=dict)
    # Ablageort: default = Standard-Ausgabe (Settings), beside = neben Quelle,
    # custom = out_subdir als media-relativer Ordner.
    out_mode: str = "default"
    out_subdir: str = ""
    out_root: str = ""  # veraltet (Queue-Persistenz); wird über out_mode abgebildet


@dataclass
class QueueItem:
    id: str
    title: str
    path: str
    settings: JobSettings
    group_id: str
    status: str = STATUS_WAITING
    info: Optional[dict] = None
    progress: dict = field(default_factory=dict)
    vmaf: Optional[dict] = None
    error: str = ""
    original_size: int = 0
    output_size: int = 0
    output_path: str = ""
    saved_bytes: int = 0
    vmaf_verify: Optional[float] = None  # gemessener VMAF der Ausgabe (Guardrail)
    verify_attempts: int = 0             # Anzahl Encode-Versuche (>1 = Retry lief)
    integrity_ok: Optional[bool] = None  # Playability-/Integritäts-Check bestanden?
    integrity_msg: str = ""              # Kurzbegründung des Integritäts-Checks
    caps_failed: bool = False            # Größen-/Bitrate-Cap überschritten
    crop: str = ""                       # erkannter Auto-Crop "w:h:x:y" (leer = kein)
    message: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    duration: float = 0.0

    def to_dict(self) -> dict:
        # Laufende Dauer bei aktiven Jobs live mitzählen, sonst die Endzeit.
        if self.duration:
            dur = self.duration
        elif self.started_at:
            dur = (self.finished_at or time.time()) - self.started_at
        else:
            dur = 0.0
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "group_id": self.group_id,
            "status": self.status,
            "settings": self.settings.__dict__,
            "info": self.info,
            "progress": self.progress,
            "vmaf": self.vmaf,
            "error": self.error,
            "message": self.message,
            "original_size": self.original_size,
            "original_human": ff.human_size(self.original_size) if self.original_size else "—",
            "output_size": self.output_size,
            "output_human": ff.human_size(self.output_size) if self.output_size else "—",
            "saved_bytes": self.saved_bytes,
            "saved_human": ff.human_size(self.saved_bytes) if self.saved_bytes else "—",
            "vmaf_verify": self.vmaf_verify,
            "verify_attempts": self.verify_attempts,
            "integrity_ok": self.integrity_ok,
            "integrity_msg": self.integrity_msg,
            "caps_failed": self.caps_failed,
            "crop": self.crop,
            "output_path": self.output_path,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(dur, 1),
            "duration_human": ff.human_duration(dur) if dur else "—",
        }


class QueueManager:
    def __init__(self, max_parallel: int = 1) -> None:
        self._items: list[QueueItem] = []
        self._lock = threading.RLock()
        self._wake = threading.Event()
        # Aktive Jobs: item_id -> EncodeRunner (None während VMAF/Setup).
        self._active: dict[str, Optional[EncodeRunner]] = {}
        self._cancel_ids: set[str] = set()
        self._max_parallel = max(1, int(max_parallel))
        self._group_quality: dict[str, dict] = {}
        # Gruppen, deren Repräsentant NICHT encodiert werden soll
        # (Workflow "compare_only" oder manueller Skip). Folgedateien im
        # Batch werden dann ebenfalls übersprungen statt mit Default zu encoden.
        self._group_skip: set[str] = set()
        self._status_msg = ""
        self._paused = False
        self._stop = False
        # Scheduler-Gate: liefert (darf_starten, Grund). Blockiert nur NEUE Jobs,
        # laufende Encodes werden nie unterbrochen.
        self._gate = None
        self._gate_msg = ""
        # Persistenz der Warteschlange: offene Aufträge überleben Neustarts.
        self._persist_path = config.DATA_DIR / "queue.json"
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- Persistenz
    # Nur nicht-terminale Zustände werden gesichert; beim Laden auf "wartend"
    # normalisiert, damit unterbrochene Jobs sauber neu starten.
    _PERSIST_STATES = (STATUS_WAITING, STATUS_RUNNING, STATUS_ANALYZING, STATUS_AWAITING)

    def _persistable_locked(self) -> list[dict]:
        out: list[dict] = []
        for it in self._items:
            if it.status not in self._PERSIST_STATES:
                continue
            entry = {
                "id": it.id,
                "title": it.title,
                "path": it.path,
                "group_id": it.group_id,
                "settings": asdict(it.settings),
                "info": it.info,
                "original_size": it.original_size,
                "created_at": it.created_at,
            }
            # „Auswahl"-Jobs (manuelles Approval) zusätzlich mit ihrer bereits
            # berechneten VMAF-Analyse sichern, damit sie nach einem Neustart/
            # Rebuild nicht neu analysiert werden müssen, sondern direkt wieder
            # zur Auswahl bereitstehen.
            if it.status == STATUS_AWAITING and it.vmaf:
                entry["status"] = STATUS_AWAITING
                entry["vmaf"] = it.vmaf
            out.append(entry)
        return out

    def _persist(self) -> None:
        try:
            with self._lock:
                data = self._persistable_locked()
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._persist_path)
        except Exception as e:  # pragma: no cover - Persistenz darf nie crashen
            logger.debug("Queue-Persistenz fehlgeschlagen: %s", e)

    def restore(self) -> int:
        """Gesicherte, offene Aufträge nach einem Neustart wieder einreihen."""
        try:
            if not self._persist_path.exists():
                return 0
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception as e:  # pragma: no cover
            logger.warning("Warteschlange konnte nicht geladen werden: %s", e)
            return 0
        fields = set(JobSettings.__dataclass_fields__)
        restored = 0
        with self._lock:
            existing = {it.id for it in self._items}
            for d in data if isinstance(data, list) else []:
                path = d.get("path", "")
                # Nur Dateien wieder aufnehmen, die es noch gibt.
                if not path or not Path(path).is_file():
                    continue
                if d.get("id") in existing:
                    continue
                raw = d.get("settings", {}) or {}
                s = JobSettings(**{k: v for k, v in raw.items() if k in fields})
                # „Auswahl"-Jobs mit gesicherter Analyse wieder als AWAITING
                # herstellen (kein erneutes Analysieren nötig). Alle übrigen
                # unterbrochenen Jobs starten sauber neu als „wartend".
                saved_vmaf = d.get("vmaf")
                is_awaiting = (d.get("status") == STATUS_AWAITING
                               and isinstance(saved_vmaf, dict)
                               and saved_vmaf.get("results"))
                item = QueueItem(
                    id=d.get("id") or uuid.uuid4().hex[:10],
                    title=d.get("title") or Path(path).name,
                    path=path,
                    settings=s,
                    group_id=d.get("group_id") or uuid.uuid4().hex[:8],
                    status=STATUS_AWAITING if is_awaiting else STATUS_WAITING,
                    info=d.get("info"),
                    vmaf=saved_vmaf if is_awaiting else None,
                    original_size=int(d.get("original_size", 0) or 0),
                    created_at=float(d.get("created_at", time.time())),
                )
                self._items.append(item)
                restored += 1
        if restored:
            logger.info("Warteschlange wiederhergestellt: %d offene Auftrag/Aufträge", restored)
            self._wake.set()
        return restored

    def set_gate(self, fn) -> None:
        """Callable setzen, das (bool, str) liefert und neue Jobs freigibt/sperrt."""
        self._gate = fn
        self._wake.set()

    # ------------------------------------------------------------- Pause / Reihenfolge
    def set_paused(self, paused: bool) -> bool:
        with self._lock:
            self._paused = bool(paused)
        if not paused:
            self._wake.set()
        return self._paused

    def is_paused(self) -> bool:
        return self._paused

    def move(self, item_id: str, direction: int) -> bool:
        """Wartenden Job in der Reihenfolge nach oben (-1) / unten (+1) schieben."""
        with self._lock:
            idx = next((i for i, it in enumerate(self._items) if it.id == item_id), None)
            if idx is None or self._items[idx].status != STATUS_WAITING:
                return False
            j = idx + direction
            # Nur mit einem anderen wartenden Job tauschen.
            while 0 <= j < len(self._items) and self._items[j].status != STATUS_WAITING:
                j += direction
            if not (0 <= j < len(self._items)):
                return False
            self._items[idx], self._items[j] = self._items[j], self._items[idx]
        self._persist()
        return True

    # ------------------------------------------------------------- Parallelität
    def set_parallel(self, n: int) -> int:
        with self._lock:
            self._max_parallel = max(1, int(n))
        self._wake.set()
        return self._max_parallel

    def get_parallel(self) -> int:
        return self._max_parallel

    # ------------------------------------------------------------- Hinzufügen
    def add_file(self, path: str, settings: JobSettings, group_id: Optional[str] = None) -> Optional[QueueItem]:
        from . import job_plan
        p = Path(path)
        if not p.is_file():
            return None
        # Duplikat: skip → nicht einreihen; ask/overwrite → einreihen (UI warnt vorher).
        on_dup = (getattr(settings, "on_duplicate", "ask") or "ask").lower()
        if on_dup == "skip":
            plan = job_plan.plan_one(str(p), settings)
            if plan.get("duplicate"):
                return None
        item = QueueItem(
            id=uuid.uuid4().hex[:10],
            title=p.name,
            path=str(p),
            settings=settings,
            group_id=group_id or uuid.uuid4().hex[:8],
        )
        info = ffprobe(p)
        if info:
            item.info = info.to_dict()
            item.original_size = info.size_bytes
        with self._lock:
            self._items.append(item)
        self._persist()
        self._wake.set()
        return item

    def add_batch(self, folder: str, settings: JobSettings) -> list[QueueItem]:
        base = Path(folder)
        if not base.is_dir():
            return []
        group = uuid.uuid4().hex[:8]
        added: list[QueueItem] = []
        files = sorted(
            f for f in base.rglob("*")
            if f.is_file() and f.suffix.lower() in config.VIDEO_EXTENSIONS
        )
        for i, f in enumerate(files):
            # Nur die erste Datei der Gruppe macht ggf. den VMAF-Test
            s = JobSettings(**settings.__dict__)
            if i > 0:
                s.vmaf_check = False
            item = self.add_file(str(f), s, group_id=group)
            if item:
                added.append(item)
        return added

    # ---------------------------------------------------------------- Steuerung
    def cancel(self, item_id: str) -> bool:
        with self._lock:
            if item_id in self._active:
                # Läuft gerade (Encode oder VMAF-Phase) -> Abbruch anfordern.
                self._cancel_ids.add(item_id)
                runner = self._active.get(item_id)
                if runner:
                    runner.cancel()
                return True
            for it in self._items:
                if it.id == item_id and it.status in (STATUS_WAITING, STATUS_AWAITING):
                    it.status = STATUS_CANCELLED
                    self._persist()
                    return True
        return False

    def approve(self, item_id: str, result_index: int) -> bool:
        """Nutzer wählt VMAF-Ergebnis → Encode startet."""
        with self._lock:
            item = next((i for i in self._items if i.id == item_id), None)
            if not item or item.status != STATUS_AWAITING:
                return False
            if not item.vmaf or result_index >= len(item.vmaf.get("results", [])):
                return False
            res = item.vmaf["results"][result_index]
            item.settings.selected_result_index = result_index
            self._apply_vmaf_choice(item.settings, res)
            self._group_quality[item.group_id] = {
                "value": res["value"],
                "rate_mode": res.get("rate_mode", item.settings.rate_mode),
                "codec": item.settings.codec,
                "platform": item.settings.platform,
            }
            item.status = STATUS_WAITING
        self._persist()
        self._wake.set()
        return True

    def skip_encode(self, item_id: str) -> bool:
        """Vergleich-only oder manuell abbrechen (auch für Batch-Folgedateien)."""
        with self._lock:
            for it in self._items:
                if it.id == item_id and it.status == STATUS_AWAITING:
                    it.status = STATUS_DONE
                    self._group_skip.add(it.group_id)
                    self._persist()
                    self._wake.set()
                    return True
        return False

    @staticmethod
    def _apply_vmaf_choice(settings: JobSettings, res: dict) -> None:
        settings.rate_mode = res.get("rate_mode", settings.rate_mode)
        settings.quality = res["value"]
        # Falls ein anderer Codec gewählt wurde, für den Encode übernehmen.
        if res.get("codec"):
            settings.codec = res["codec"]
            settings.suffix = "_" + settings.codec
        if res.get("platform"):
            settings.platform = res["platform"]

    def clear_finished(self) -> None:
        with self._lock:
            self._items = [
                it for it in self._items
                if it.status in (STATUS_WAITING, STATUS_RUNNING, STATUS_ANALYZING, STATUS_AWAITING)
            ]
        self._persist()

    # ------------------------------------------------------------------- State
    def get_item(self, item_id: str) -> Optional[QueueItem]:
        with self._lock:
            return next((it for it in self._items if it.id == item_id), None)

    def state(self) -> dict:
        with self._lock:
            items = [it.to_dict() for it in self._items]
            active_ids = list(self._active.keys())
            msg = self._status_msg
            parallel = self._max_parallel
            paused = self._paused
            gate_msg = self._gate_msg
        total_saved = sum(i["saved_bytes"] for i in items if i["saved_bytes"] > 0)
        # Gesamt-ETA: Summe aktiver ETAs + wartende Jobs × Ø-Dauer aus Historie.
        from . import history as _hist
        avg_dur = float((_hist.stats() or {}).get("avg_duration") or 0)
        eta_active = 0.0
        waiting_n = 0
        for i in items:
            st = i.get("status") or ""
            if st in ("in arbeit", "vmaf-test"):
                p = i.get("progress") or {}
                eta = p.get("eta")
                if eta is None and p.get("eta_human"):
                    eta = 0
                try:
                    eta_active += max(0.0, float(eta or 0))
                except (TypeError, ValueError):
                    pass
                # VMAF: grobe ETA aus Schrittfortschritt, falls keine Encode-ETA.
                if (not eta) and st == "vmaf-test":
                    step = int(p.get("step") or 0)
                    steps = int(p.get("steps") or 0)
                    started = float(i.get("started_at") or 0)
                    if steps > 0 and step > 0 and started:
                        elapsed = max(0.0, time.time() - started)
                        per = elapsed / step
                        eta_active += per * max(0, steps - step)
            elif st == "wartend":
                waiting_n += 1
        eta_total = eta_active + waiting_n * avg_dur
        return {
            "items": items,
            "active_ids": active_ids,
            "active_id": active_ids[0] if active_ids else None,  # Rückwärtskompat.
            "status_message": msg,
            "gate_message": gate_msg,
            "max_parallel": parallel,
            "paused": paused,
            "total_saved_bytes": total_saved,
            "total_saved_human": ff.human_size(total_saved) if total_saved else "0 B",
            "counts": _counts(items),
            "avg_job_duration": round(avg_dur, 1),
            "queue_eta_seconds": round(eta_total, 0),
            "queue_eta_human": ff.human_duration(eta_total) if eta_total else "—",
        }

    # ------------------------------------------------------------------ Worker
    def _claim_next_locked(self) -> Optional[QueueItem]:
        """Nächsten startbaren Job holen (Lock muss gehalten werden)."""
        if self._paused:
            return None
        # Scheduler-Gate prüfen (Zeitfenster/Last). Läuft ein Job bereits,
        # nicht durch das Gate blockieren – nur den Start neuer Jobs steuern.
        if self._gate is not None and not self._active:
            try:
                allowed, reason = self._gate()
            except Exception:  # pragma: no cover - Gate darf Worker nicht kippen
                allowed, reason = True, ""
            self._gate_msg = "" if allowed else (reason or "")
            if not allowed:
                return None
        else:
            self._gate_msg = ""
        for it in self._items:
            if it.status != STATUS_WAITING:
                continue
            if it.id in self._active:
                continue
            # Nur Folgedateien (ohne eigenen VMAF-Check) auf die Gruppen-
            # Entscheidung warten lassen – der Repräsentant selbst darf nie
            # durch seine eigene Gruppe blockiert werden (sonst Deadlock).
            if not it.settings.vmaf_check and self._group_blocked_locked(it.group_id):
                continue
            return it
        return None

    def _group_blocked_locked(self, group_id: str) -> bool:
        """Batch-Follower warten, bis der VMAF-Repräsentant entschieden ist."""
        if group_id in self._group_quality or group_id in self._group_skip:
            return False
        # Existiert ein noch nicht entschiedener Repräsentant (VMAF-Job) der
        # Gruppe? Dann Folgedateien zurückhalten, damit sie den ermittelten
        # Qualitätswert übernehmen statt vorzeitig mit Default zu starten.
        for i in self._items:
            if i.group_id != group_id or not i.settings.vmaf_check:
                continue
            if i.status in (STATUS_WAITING, STATUS_ANALYZING, STATUS_AWAITING):
                return True
            if i.id in self._active:
                return True
        return False

    def _worker(self) -> None:
        """Dispatcher: startet bis zu _max_parallel Jobs in eigenen Threads."""
        while not self._stop:
            while not self._stop:
                with self._lock:
                    if len(self._active) >= self._max_parallel:
                        break
                    item = self._claim_next_locked()
                    if item is None:
                        break
                    self._active[item.id] = None  # als aktiv markieren (Claim)
                threading.Thread(
                    target=self._run_job, args=(item,), daemon=True,
                ).start()
            self._wake.wait(timeout=2.0)
            self._wake.clear()

    def _set_msg(self, msg: str) -> None:
        with self._lock:
            self._status_msg = msg

    def _refresh_global_msg(self) -> None:
        n = len(self._active)
        self._status_msg = f"{n} Encode(s) aktiv" if n else ""

    def _finish_active(self, item_id: str) -> None:
        with self._lock:
            self._active.pop(item_id, None)
            self._cancel_ids.discard(item_id)
            self._refresh_global_msg()
        # Terminal-Status wird nicht mehr gesichert -> Datei aktuell halten.
        self._persist()
        self._wake.set()

    def _run_job(self, item: QueueItem) -> None:
        started = time.time()
        item.started_at = started
        try:
            self._process(item)
        except Exception as e:  # pragma: no cover - Schutz vor Worker-Absturz
            item.status = STATUS_FAILED
            item.error = f"Interner Fehler: {e}"
            logger.exception("Job abgestürzt: %s", item.title)
        finally:
            item.finished_at = time.time()
            item.duration = item.finished_at - started
            self._record_history(item, item.duration)
            self._finish_active(item.id)

    @staticmethod
    def _record_history(item: QueueItem, duration: float) -> None:
        """Echte Encodes und Fehler in die persistente Historie schreiben."""
        try:
            from . import history
            encoded = item.status == STATUS_DONE and getattr(item, "output_size", 0)
            if encoded or item.status == STATUS_FAILED:
                history.record_job(item, duration=duration)
                try:
                    from . import notify
                    notify.notify_job(item)
                except Exception as ne:  # pragma: no cover
                    logger.debug("Benachrichtigung übersprungen: %s", ne)
        except Exception as e:  # pragma: no cover
            logger.debug("Historie überspringen: %s", e)

    def _process(self, item: QueueItem) -> None:
        info, probe_err = probe_with_error(Path(item.path))
        if info is None:
            item.status = STATUS_FAILED
            item.error = f"ffprobe: {probe_err or 'kein gültiges Video'}"
            return

        s = item.settings

        # --- Remux/Bearbeiten: Spuren editieren, Video 1:1 (kein Re-Encode) ---
        if s.video_mode == "edit":
            self._process_remux_edit(item, info)
            return
        if s.video_mode == "concat":
            self._process_concat(item, info)
            return
        if s.video_mode == "split":
            self._process_split(item, info)
            return

        # --- Audio-Optimierung: nur Remux (Video 1:1 kopieren) ----------------
        if s.video_mode == "copy":
            self._process_audio_remux(item, info)
            return

        # --- Chunked Adaptive Encoding (nur CQ-Modus) -------------------------
        if s.chunked and s.rate_mode == "cq":
            self._process_chunked(item, info)
            return

        # --- Batch-Folgedatei einer übersprungenen Gruppe → nicht encodieren -
        if item.group_id in self._group_skip and not s.vmaf_check:
            item.status = STATUS_DONE
            return

        # --- Vorab-Check: ist der gewählte Encoder im FFmpeg-Build vorhanden? -
        from . import ffmpeg_utils as ffu
        if not ffu.encoder_available(s.platform, s.codec):
            enc = ffu.encoder_name(s.platform, s.codec)
            avail = sorted(e for e in ffu.available_encoders()
                           if any(x in e for x in ("nvenc", "qsv", "vaapi", "av1", "x264", "x265", "svt")))
            item.status = STATUS_FAILED
            item.error = (f"Encoder '{enc}' ist im FFmpeg-Build nicht verfügbar. "
                          f"Verfügbar: {', '.join(avail) or 'keine erkannt'}. "
                          f"Anderen Codec/Plattform wählen oder Image neu bauen.")
            logger.error("Encoder fehlt: %s | verfügbar: %s", enc, avail)
            return

        # --- Auto-Crop: schwarze Balken einmalig erkennen ---------------------
        # Das Ergebnis fließt in VMAF-Analyse, Haupt-Encode und Guardrail ein,
        # damit alle drei dieselbe (beschnittene) Bildfläche nutzen.
        if s.autocrop and not item.crop:
            item.message = "Auto-Crop: schwarze Balken werden erkannt …"
            crop = ff.detect_crop(info)
            item.crop = crop
            if crop:
                logger.info("Auto-Crop erkannt (%s): crop=%s (Quelle %dx%d)",
                            item.title, crop, info.width, info.height)
            else:
                logger.info("Auto-Crop: keine nennenswerten Balken (%s)", item.title)

        # --- VMAF-Test (optional, repräsentativ pro Gruppe) -------------------
        with self._lock:
            do_vmaf = s.vmaf_check and item.group_id not in self._group_quality
        if do_vmaf:
            item.status = STATUS_ANALYZING
            item.message = "VMAF-Analyse läuft …"
            self._refresh_global_msg()
            vmaf_opts = vmaf_mod.VmafOptions(
                rate_mode=s.rate_mode,
                test_values=list(s.test_values),
                clip_seconds=s.clip_seconds,
                samples=s.samples,
                generate_screenshots=s.generate_screenshots,
                item_id=item.id,
                session_name=_session_name(item),
                source_title=item.title,
                source_path=item.path,
                params=asdict(s),
                encoders=_parse_encoders(s.compare_encoders),
                target_vmaf=s.target_vmaf,
                anime=s.anime,
            )
            analysis = vmaf_mod.analyze(
                info, s.platform, s.codec, s.target_height, s.tonemap,
                preserve_hdr=s.preserve_hdr,
                film_grain=s.film_grain, denoise=s.denoise, crop=item.crop,
                opts=vmaf_opts,
                status=lambda m: setattr(item, "message", m),
                cancelled=lambda: item.id in self._cancel_ids,
                progress=lambda d: setattr(item, "progress", d),
            )
            item.progress = {}
            item.vmaf = analysis.to_dict()
            item.message = ""

            if item.id in self._cancel_ids:
                item.status = STATUS_CANCELLED
                return

            # Keine VMAF-Ergebnisse → Analyse ist fehlgeschlagen. Nicht still
            # weiterlaufen/„fertig" melden, sondern den Grund anzeigen.
            if not analysis.results:
                item.status = STATUS_FAILED
                item.error = analysis.error or (
                    "VMAF-Analyse lieferte keine Ergebnisse. Bitte Encoder/"
                    "Plattform prüfen (Logs beachten).")
                logger.error("VMAF ohne Ergebnis: %s | %s", item.title, item.error)
                with self._lock:
                    self._group_skip.add(item.group_id)
                return

            if s.workflow == "compare_only":
                item.status = STATUS_DONE
                with self._lock:
                    self._group_skip.add(item.group_id)
                return

            if s.workflow == "manual":
                item.status = STATUS_AWAITING
                return

            # auto: empfohlenen Wert (inkl. Gewinner-Codec) übernehmen
            if analysis.recommended_value is not None:
                s.quality = analysis.recommended_value
                if analysis.recommended_codec:
                    s.codec = analysis.recommended_codec
                    s.suffix = "_" + s.codec
                if analysis.recommended_platform:
                    s.platform = analysis.recommended_platform
                with self._lock:
                    self._group_quality[item.group_id] = {
                        "value": analysis.recommended_value,
                        "rate_mode": s.rate_mode,
                        "codec": s.codec,
                        "platform": s.platform,
                    }
        else:
            with self._lock:
                gq = self._group_quality.get(item.group_id)
            if gq:
                s.quality = gq["value"]
                s.rate_mode = gq.get("rate_mode", s.rate_mode)
                s.codec = gq.get("codec", s.codec)
                s.platform = gq.get("platform", s.platform)
                s.suffix = "_" + s.codec

        # --- Haupt-Encode -----------------------------------------------------
        item.status = STATUS_RUNNING
        qlabel = _quality_label(s)
        item.message = f"Encode ({qlabel})"
        self._refresh_global_msg()
        out_path = _output_path(item)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _log_job_start(item, info, out_path, "Encode")

        def on_prog(p: EncodeProgress) -> None:
            saved = max(0, item.original_size - p.current_size)
            item.progress = {
                "percent": p.percent,
                "fps": round(p.fps, 1),
                "bitrate": p.bitrate,
                "speed": p.speed,
                "eta": round(p.eta, 0),
                "eta_human": ff.human_duration(p.eta),
                "current_size": p.current_size,
                "current_human": ff.human_size(p.current_size),
                "saved_human": ff.human_size(saved),
            }

        # Guardrail: nach dem Encode den echten VMAF messen; bei Unterschreiten
        # des Ziels optional automatisch mit höherer Qualität neu encoden.
        do_verify = bool(s.verify_vmaf and s.workflow != "compare_only")
        max_attempts = 1 + (config.VERIFY_MAX_RETRIES if (do_verify and s.verify_retry) else 0)

        rc, stderr, cmd, cancelled = 0, "", [], False
        for attempt in range(1, max_attempts + 1):
            item.verify_attempts = attempt
            if attempt > 1:
                item.message = (f"Encode-Wiederholung {attempt}/{max_attempts} "
                                f"({_quality_label(s)})")
            rc, stderr, cmd, cancelled = self._encode_to(item, s, info, out_path, on_prog)
            if cancelled or rc != 0:
                break
            # Dolby Vision: Bei HEVC wird die RPU nach dem Encode via dovi_tool
            # re-injiziert (Profil 8.1). AV1-DV wird bereits beim Encoden durch
            # libsvtav1 nativ eingebettet (dovi_tool kann kein AV1) – daher hier
            # nur HEVC.
            if s.preserve_dv and s.codec == "hevc":
                self._reinject_dv(item, out_path)
            if not do_verify:
                break
            score = self._verify_output(item, s, info, out_path)
            item.vmaf_verify = score
            if (score is not None and score < s.verify_min
                    and s.verify_retry and attempt < max_attempts):
                logger.info("Guardrail: VMAF %.1f < Ziel %.1f – höhere Qualität (%s)",
                            score, s.verify_min, item.title)
                _bump_quality(s)
                out_path.unlink(missing_ok=True)
                continue
            break

        if cancelled:
            item.status = STATUS_CANCELLED
            out_path.unlink(missing_ok=True)
        elif rc != 0:
            item.status = STATUS_FAILED
            item.error = stderr[-1500:] if stderr else f"FFmpeg exit {rc}"
            logger.error("Encode fehlgeschlagen: %s (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                         item.title, rc, " ".join(cmd), stderr)
            out_path.unlink(missing_ok=True)
        else:
            item.output_path = str(out_path)
            item.output_size = out_path.stat().st_size if out_path.exists() else 0
            item.saved_bytes = max(0, item.original_size - item.output_size)
            # Trotz ausgeschöpfter Versuche unter Ziel? Als Warnung vermerken.
            if (do_verify and item.vmaf_verify is not None
                    and item.vmaf_verify < s.verify_min):
                item.error = (f"Qualitätswarnung: gemessener VMAF "
                              f"{item.vmaf_verify:.1f} < Ziel {s.verify_min:.0f}.")
            self._run_integrity(item, s, info, out_path)
            self._run_caps(item, s, out_path)
            ff.add_mkv_statistics_tags(out_path)
            self._post_process(item, out_path)
            item.status = STATUS_DONE
            item.progress["percent"] = 100.0
        item.message = ""

    def _process_chunked(self, item: "QueueItem", info) -> None:
        """Per-Szene/Chunked Adaptive Encoding: Segmente mit adaptivem CQ."""
        from . import chunked
        s = item.settings
        item.status = STATUS_RUNNING
        # Auto-Crop einmalig erkennen (Chunked umgeht den Standard-Encode-Pfad).
        if s.autocrop and not item.crop:
            item.message = "Auto-Crop: schwarze Balken werden erkannt …"
            item.crop = ff.detect_crop(info)
            if item.crop:
                logger.info("Auto-Crop erkannt (%s): crop=%s", item.title, item.crop)
        item.message = "Chunked Adaptive Encoding …"
        self._refresh_global_msg()
        out_path = _output_path(item)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _log_job_start(item, info, out_path, "Chunked-Encode")

        enc_kw = {
            "preserve_hdr": s.preserve_hdr,
            "film_grain": s.film_grain,
            "denoise": s.denoise,
            "force_10bit": s.anime,
            "crop": item.crop,
            "audio_mode": s.audio_mode,
            "audio_codec": s.audio_codec,
            "audio_bitrate_kbps": s.audio_bitrate,
            "audio_channels": s.audio_channels,
            "audio_normalize": s.audio_normalize,
            "audio_tracks": list(s.audio_tracks),
        }

        def set_active(runner) -> None:
            with self._lock:
                self._active[item.id] = runner

        ok, err = chunked.encode(
            info, out_path, s,
            set_active=set_active,
            cancelled=lambda: item.id in self._cancel_ids,
            status=lambda m: setattr(item, "message", m),
            progress=lambda d: setattr(item, "progress", d),
            enc_kw=enc_kw,
        )
        if item.id in self._cancel_ids or (not ok and err == "Abgebrochen"):
            item.status = STATUS_CANCELLED
            out_path.unlink(missing_ok=True)
        elif not ok:
            item.status = STATUS_FAILED
            item.error = err or "Chunked-Encode fehlgeschlagen"
            logger.error("Chunked fehlgeschlagen: %s | %s", item.title, err)
            out_path.unlink(missing_ok=True)
        else:
            # Dolby Vision nach Chunked-Encode nur für HEVC (dovi_tool). AV1-DV
            # ist über zusammengefügte Segmente nicht zuverlässig einbettbar –
            # dort bleibt der HDR10-Basislayer erhalten.
            if s.preserve_dv and s.codec == "hevc":
                self._reinject_dv(item, out_path)
            item.output_path = str(out_path)
            item.output_size = out_path.stat().st_size if out_path.exists() else 0
            item.saved_bytes = max(0, item.original_size - item.output_size)
            self._run_integrity(item, s, info, out_path)
            self._run_caps(item, s, out_path)
            ff.add_mkv_statistics_tags(out_path)
            self._post_process(item, out_path)
            item.status = STATUS_DONE
            item.progress["percent"] = 100.0
        item.message = ""

    def _process_audio_remux(self, item: "QueueItem", info) -> None:
        """Nur-Audio-Optimierung: Video/Untertitel/Kapitel kopieren, aufgeblähte
        Tonspuren transcodieren. Kein VMAF, kein Video-Encode."""
        from . import audio_opt
        s = item.settings
        settings = {
            "audio_codec": s.audio_codec,
            "audio_channels": s.audio_channels,
            "audio_bitrate": s.audio_bitrate,
            "audio_normalize": s.audio_normalize,
            "scope": s.audio_opt_scope,
            "min_bitrate_kbps": s.audio_min_bitrate_kbps,
        }
        if not audio_opt.has_candidates(info, settings):
            item.status = STATUS_DONE
            item.message = "Keine optimierbaren Tonspuren – übersprungen."
            return

        item.status = STATUS_RUNNING
        item.message = "Audio-Optimierung (Remux) …"
        self._refresh_global_msg()
        out_path = _output_path(item)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _log_job_start(item, info, out_path, "Audio-Remux")

        def on_prog(p: EncodeProgress) -> None:
            saved = max(0, item.original_size - p.current_size)
            item.progress = {
                "percent": p.percent, "fps": round(p.fps, 1),
                "bitrate": p.bitrate, "speed": p.speed,
                "eta": round(p.eta, 0), "eta_human": ff.human_duration(p.eta),
                "current_size": p.current_size,
                "current_human": ff.human_size(p.current_size),
                "saved_human": ff.human_size(saved),
            }

        cmd = audio_opt.build_remux_cmd(info, out_path, settings)
        _log_cmd(item, cmd)
        runner = EncodeRunner(on_progress=on_prog)
        with self._lock:
            self._active[item.id] = runner
        rc, stderr = runner.run(cmd, info.duration)

        if runner._cancel:
            item.status = STATUS_CANCELLED
            out_path.unlink(missing_ok=True)
        elif rc != 0:
            item.status = STATUS_FAILED
            item.error = stderr[-1500:] if stderr else f"FFmpeg exit {rc}"
            logger.error("Audio-Remux fehlgeschlagen: %s (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                         item.title, rc, " ".join(cmd), stderr)
            out_path.unlink(missing_ok=True)
        else:
            item.output_path = str(out_path)
            item.output_size = out_path.stat().st_size if out_path.exists() else 0
            item.saved_bytes = max(0, item.original_size - item.output_size)
            self._run_integrity(item, s, info, out_path)
            self._run_caps(item, s, out_path)
            ff.add_mkv_statistics_tags(out_path)
            self._post_process(item, out_path)
            item.status = STATUS_DONE
            item.progress["percent"] = 100.0
        item.message = ""

    def _process_remux_edit(self, item: "QueueItem", info) -> None:
        """Remux/Bearbeiten: Spuren entfernen/umsortieren, Flags/Sprache/Titel
        ändern, externe Spuren hinzufügen – ohne Video-Re-Encode."""
        from . import remux
        s = item.settings
        spec = dict(s.edit_spec or {})
        # Container aus dem Spec zieht (mkv/mp4) – für Ausgabe-Pfad & Kompatibilität.
        spec.setdefault("container", "mkv")

        conflicts = remux.check_conflicts(info, spec)
        if conflicts:
            item.status = STATUS_FAILED
            item.error = "Container-Konflikt: " + " ".join(conflicts)
            logger.error("Remux abgebrochen (Konflikt): %s\n%s",
                         item.title, "\n".join(conflicts))
            item.message = ""
            return

        out_path = _output_path(item)
        cmd, err = remux.build_edit_cmd(info, out_path, spec)
        if err:
            item.status = STATUS_FAILED
            item.error = err
            item.message = ""
            return
        self._run_copy_job(item, info, cmd, out_path, "Remux/Bearbeiten (kein Re-Encode)")

    def _run_copy_job(self, item: "QueueItem", info, cmd: list, out_path: Path,
                      label: str) -> None:
        """Gemeinsamer Ablauf für Copy-Jobs (Remux/Concat/Split): laufen lassen,
        Integrität prüfen, Nachbehandlung. Kein VMAF/Re-Encode."""
        s = item.settings
        item.status = STATUS_RUNNING
        item.message = label
        self._refresh_global_msg()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _log_job_start(item, info, out_path, label)

        def on_prog(p: EncodeProgress) -> None:
            item.progress = {
                "percent": p.percent, "fps": round(p.fps, 1),
                "bitrate": p.bitrate, "speed": p.speed,
                "eta": round(p.eta, 0), "eta_human": ff.human_duration(p.eta),
                "current_size": p.current_size,
                "current_human": ff.human_size(p.current_size),
            }

        _log_cmd(item, cmd)
        runner = EncodeRunner(on_progress=on_prog)
        with self._lock:
            self._active[item.id] = runner
        rc, stderr = runner.run(cmd, getattr(info, "duration", 0.0) or 0.0)

        if runner._cancel:
            item.status = STATUS_CANCELLED
            out_path.unlink(missing_ok=True)
        elif rc != 0:
            item.status = STATUS_FAILED
            item.error = stderr[-1500:] if stderr else f"FFmpeg exit {rc}"
            logger.error("%s fehlgeschlagen: %s (Exit %s)\nCMD: %s\nSTDERR:\n%s",
                         label, item.title, rc, " ".join(cmd), stderr)
            out_path.unlink(missing_ok=True)
        else:
            item.output_path = str(out_path)
            item.output_size = out_path.stat().st_size if out_path.exists() else 0
            item.saved_bytes = item.original_size - item.output_size
            self._run_integrity(item, s, info, out_path)
            self._run_caps(item, s, out_path)
            ff.add_mkv_statistics_tags(out_path)
            self._post_process(item, out_path)
            item.status = STATUS_DONE
            item.progress["percent"] = 100.0
        item.message = ""

    def _process_concat(self, item: "QueueItem", info) -> None:
        """Mehrere Dateien zusammenführen (concat, optional mit Re-Encode)."""
        from . import remux
        s = item.settings
        spec = s.edit_spec or {}
        rels = list(spec.get("concat_files", []))
        files = []
        for r in rels:
            t = config.resolve_input(r)
            if t is None or not t.is_file():
                item.status = STATUS_FAILED
                item.error = f"Datei nicht gefunden: {r}"
                return
            files.append(t)
        out_path = _output_path(item)
        if spec.get("unify"):
            cmd, err = remux.build_concat_reencode_cmd(
                files, out_path, spec.get("platform", "cpu"),
                spec.get("codec", "av1"), int(spec.get("cq", 30) or 30))
            label = "Zusammenführen (Re-Encode)"
        else:
            cmd, err = remux.build_concat_cmd(
                files, out_path, config.WORK_DIR,
                add_chapters=bool(spec.get("chapters_at_joins")))
            label = "Zusammenführen (concat)"
        if err:
            item.status = STATUS_FAILED
            item.error = err
            return
        self._run_copy_job(item, info, cmd, out_path, label)

    def _process_split(self, item: "QueueItem", info) -> None:
        """Quelle verlustfrei splitten bzw. Ausschnitte exportieren."""
        from . import remux
        s = item.settings
        spec = s.edit_spec or {}
        mode = spec.get("split_mode", "chapters")
        base = _output_path(item)
        base.parent.mkdir(parents=True, exist_ok=True)

        if mode == "range":
            # Ein Output je Bereich – mehrere FFmpeg-Läufe.
            cmds = remux.build_cut_cmds(
                info, base.parent, spec.get("split_ranges") or [], base.suffix)
            if not cmds:
                item.status = STATUS_FAILED
                item.error = "Keine gültigen Bereiche zum Ausschneiden."
                return
            item.status = STATUS_RUNNING
            self._refresh_global_msg()
            _log_job_start(item, info, base.parent, "Ausschnitt exportieren")
            outputs = []
            for idx, (cmd, out_path) in enumerate(cmds, start=1):
                item.message = f"Ausschnitt {idx}/{len(cmds)} …"
                _log_cmd(item, cmd)
                runner = EncodeRunner()
                with self._lock:
                    self._active[item.id] = runner
                rc, stderr = runner.run(cmd, getattr(info, "duration", 0.0) or 0.0)
                if runner._cancel:
                    item.status = STATUS_CANCELLED
                    item.message = ""
                    return
                if rc != 0:
                    item.status = STATUS_FAILED
                    item.error = stderr[-1500:] if stderr else f"FFmpeg exit {rc}"
                    logger.error("Ausschnitt fehlgeschlagen: %s (Exit %s)\nSTDERR:\n%s",
                                 item.title, rc, stderr)
                    return
                outputs.append(str(out_path))
            item.output_path = outputs[0] if outputs else ""
            item.status = STATUS_DONE
            item.progress["percent"] = 100.0
            item.message = ""
            return

        pattern = base.with_name(f"{Path(item.path).stem}_%03d{base.suffix}")
        cmd, err = remux.build_split_cmd(
            info, pattern, mode, spec.get("split_value"),
            spec.get("split_times"))
        if err:
            item.status = STATUS_FAILED
            item.error = err
            return
        # Split erzeugt mehrere Dateien; Integritäts-/Nachbehandlung entfällt.
        item.status = STATUS_RUNNING
        item.message = "Splitten …"
        self._refresh_global_msg()
        pattern.parent.mkdir(parents=True, exist_ok=True)
        _log_job_start(item, info, pattern, "Splitten")
        _log_cmd(item, cmd)
        runner = EncodeRunner()
        with self._lock:
            self._active[item.id] = runner
        rc, stderr = runner.run(cmd, getattr(info, "duration", 0.0) or 0.0)
        if runner._cancel:
            item.status = STATUS_CANCELLED
        elif rc != 0:
            item.status = STATUS_FAILED
            item.error = stderr[-1500:] if stderr else f"FFmpeg exit {rc}"
            logger.error("Splitten fehlgeschlagen: %s (Exit %s)\nSTDERR:\n%s",
                         item.title, rc, stderr)
        else:
            item.status = STATUS_DONE
            item.progress["percent"] = 100.0
            item.message = ""

    def _encode_to(self, item: "QueueItem", s: "JobSettings", info,
                   out_path: Path, on_prog) -> tuple:
        """Ein Encode-Durchlauf (Ein-/Zwei-Pass). Liefert
        (rc, stderr, cmd, cancelled). enc_kw wird je Aufruf frisch aus `s`
        gebaut, damit ein Guardrail-Retry die erhöhte Qualität übernimmt."""
        enc_kw: dict = {
            "audio_mode": s.audio_mode,
            "audio_codec": s.audio_codec,
            "audio_bitrate_kbps": s.audio_bitrate,
            "audio_channels": s.audio_channels,
            "audio_normalize": s.audio_normalize,
            "audio_tracks": list(s.audio_tracks),
            "audio_per_track": s.audio_per_track,
            "audio_track_settings": list(s.audio_track_settings),
            "preserve_hdr": s.preserve_hdr,
            "keep_subtitles": s.keep_subtitles,
            "subtitle_per_track": s.subtitle_per_track,
            "subtitle_track_settings": list(s.subtitle_track_settings),
            "keep_chapters": s.keep_chapters,
            "keep_metadata": s.keep_metadata,
            "film_grain": s.film_grain,
            "denoise": s.denoise,
            "force_10bit": s.anime,
            "container": _container_ext(s).lstrip("."),
            "preserve_dv": s.preserve_dv,
            "crop": item.crop,
        }
        if s.rate_mode in ("bitrate", "abr"):
            enc_kw["rate_mode"] = s.rate_mode
            br = int(s.quality or 0)
            cap = int(getattr(s, "max_video_bitrate_kbps", 0) or 0)
            if cap > 0:
                br = min(br, cap) if br > 0 else cap
            enc_kw["bitrate_kbps"] = br

        runner = EncodeRunner(on_progress=on_prog)
        with self._lock:
            self._active[item.id] = runner

        # Zwei-Pass (echt) nur für CPU-Encoder im Bitraten-Modus: erst Analyse-,
        # dann Encode-Pass. NVENC nutzt stattdessen -multipass (ein Durchlauf).
        do_two_pass = (s.two_pass and s.platform == "cpu"
                       and s.rate_mode in ("bitrate", "abr"))
        if do_two_pass:
            passlog = str(config.WORK_DIR / f"pass_{item.id}")
            config.WORK_DIR.mkdir(parents=True, exist_ok=True)
            cmd = build_encode_cmd(
                info, out_path, s.platform, s.codec, s.quality,
                s.target_height, s.tonemap, two_pass=True, pass_num=1,
                passlog=passlog, **enc_kw,
            )
            item.message = "Zwei-Pass: Analyse-Durchlauf (1/2) …"
            _log_cmd(item, cmd, "Zwei-Pass 1/2")
            rc, stderr = runner.run(cmd, info.duration)
            if rc == 0 and not runner._cancel:
                item.message = "Zwei-Pass: Encode-Durchlauf (2/2) …"
                cmd = build_encode_cmd(
                    info, out_path, s.platform, s.codec, s.quality,
                    s.target_height, s.tonemap, two_pass=True, pass_num=2,
                    passlog=passlog, **enc_kw,
                )
                _log_cmd(item, cmd, "Zwei-Pass 2/2")
                rc, stderr = runner.run(cmd, info.duration)
            self._cleanup_passlog(passlog)
        elif s.two_pass and s.platform == "nvidia":
            cmd = build_encode_cmd(
                info, out_path, s.platform, s.codec, s.quality,
                s.target_height, s.tonemap, two_pass=True, **enc_kw,
            )
            _log_cmd(item, cmd)
            rc, stderr = runner.run(cmd, info.duration)
        else:
            cmd = build_encode_cmd(
                info, out_path, s.platform, s.codec, s.quality,
                s.target_height, s.tonemap, **enc_kw,
            )
            _log_cmd(item, cmd)
            rc, stderr = runner.run(cmd, info.duration)
        return rc, stderr, cmd, runner._cancel

    def _verify_output(self, item: "QueueItem", s: "JobSettings", info,
                       out_path: Path) -> Optional[float]:
        """Misst den echten VMAF der fertigen Ausgabe (stichprobenartig)."""
        try:
            item.message = "Qualitätsprüfung: VMAF der Ausgabe wird gemessen …"
            score = vmaf_mod.measure_output_vmaf(
                info, out_path,
                tonemap=s.tonemap, preserve_hdr=s.preserve_hdr,
                samples=min(3, max(1, s.samples)),
                clip_seconds=config.VERIFY_CLIP_SECONDS,
                anime=s.anime, crop=item.crop,
            )
            if score is not None:
                logger.info("Guardrail-VMAF %.2f (%s)", score, item.title)
            return score
        except Exception as e:  # pragma: no cover - Messfehler darf Job nicht kippen
            logger.warning("Guardrail-Messung fehlgeschlagen (%s): %s", item.title, e)
            return None

    def _reinject_dv(self, item: QueueItem, out_path: Path) -> None:
        """DV-RPU nach dem Encode übernehmen (HEVC->8.1 / AV1->10.1, best-effort)."""
        import shutil
        from . import dolby_vision as dv

        if not (item.info and item.info.get("dolby_vision")):
            return  # Quelle hat kein Dolby Vision – nichts zu tun
        if not dv.available():
            item.error = ("Dolby Vision: dovi_tool nicht verfügbar – "
                          "Ausgabe als HDR10 gespeichert.")
            logger.warning("dovi_tool fehlt – DV nicht übernommen: %s", item.title)
            return
        item.message = "Dolby Vision: RPU wird übernommen …"
        work = config.WORK_DIR / f"dv_{item.id}"
        info = item.info or {}
        fps = float(info.get("fps") or 0.0)
        source_codec = "av1" if str(info.get("codec", "")).lower() in ("av1", "libaom-av1") else "hevc"
        target_codec = item.settings.codec
        profile = int(info.get("dv_profile") or 0)
        try:
            final, err = dv.reinject(
                Path(item.path), out_path, work, fps=fps,
                source_codec=source_codec, target_codec=target_codec,
                profile=profile,
                status=lambda m: setattr(item, "message", m))
            if final and final.exists():
                out_path.unlink(missing_ok=True)
                final.replace(out_path)
                logger.info("Dolby Vision übernommen: %s", item.title)
            else:
                item.error = (f"Dolby Vision nicht übernommen ({err}) – "
                              f"Ausgabe als HDR10 gespeichert.")
                logger.warning("DV-Reinjektion fehlgeschlagen (%s): %s", err, item.title)
        except Exception as e:  # pragma: no cover - Fallback darf Encode nicht kippen
            item.error = f"Dolby Vision Fehler: {e} – Ausgabe als HDR10 gespeichert."
            logger.exception("DV-Reinjektion abgestürzt: %s", item.title)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _cleanup_passlog(passlog: str) -> None:
        """FFmpeg-2-Pass-Logdateien (*.log, *.log.mbtree) entfernen."""
        base = Path(passlog)
        for p in base.parent.glob(base.name + "*"):
            try:
                p.unlink()
            except OSError:
                pass

    # ------------------------------------------------------ Integritäts-Check
    @staticmethod
    def _run_integrity(item: QueueItem, s: "JobSettings", info,
                       out_path: Path) -> None:
        """Playability-/Integritäts-Check der Ausgabe (Voll-Decode + Dauer).

        Läuft, wenn explizit aktiviert oder eine destruktive Nachbehandlung
        (inplace/archive) ansteht – dann ist der Check die Voraussetzung, um das
        Original gefahrlos ersetzen/verschieben zu können.
        """
        need = s.integrity_check or s.post_processing in ("inplace", "archive")
        if not need:
            return
        item.message = "Integritäts-Check der Ausgabe …"
        expected = float(getattr(info, "duration", 0.0) or 0.0)
        ok, msg = ff.verify_playable(out_path, expected)
        item.integrity_ok = ok
        item.integrity_msg = msg
        if ok:
            logger.info("Integritäts-Check bestanden: %s", item.title)
        else:
            item.error = ((item.error + " ") if item.error else "") + \
                f"Integritäts-Check fehlgeschlagen: {msg}."
            logger.warning("Integritäts-Check fehlgeschlagen (%s): %s",
                           item.title, msg)

    @staticmethod
    def _run_caps(item: QueueItem, s: "JobSettings", out_path: Path) -> None:
        """Größen-/Bitrate-Obergrenzen prüfen (Warnung, safe_replace blockieren)."""
        item.caps_failed = False
        max_mb = float(getattr(s, "max_output_mb", 0) or 0)
        max_br = int(getattr(s, "max_video_bitrate_kbps", 0) or 0)
        if max_mb <= 0 and max_br <= 0:
            return
        reasons = []
        size = out_path.stat().st_size if out_path.exists() else 0
        if max_mb > 0 and size > max_mb * 1024 * 1024:
            reasons.append(f"Größe {size / (1024 * 1024):.1f} MB > Cap {max_mb:.0f} MB")
        if max_br > 0 and size > 0:
            dur = 0.0
            try:
                info2, _ = ff.probe_with_error(out_path)
                if info2:
                    dur = float(info2.duration or 0)
                    vbr = int(getattr(info2, "video_bitrate", 0) or 0)
                    if vbr > max_br * 1000:
                        reasons.append(
                            f"Video-Bitrate {vbr // 1000} kbit/s > Cap {max_br} kbit/s")
                    elif dur > 1 and vbr <= 0:
                        overall = int(size * 8 / dur)
                        if overall > max_br * 1000 * 1.15:
                            reasons.append(
                                f"Ø-Bitrate ~{overall // 1000} kbit/s > Cap {max_br} kbit/s")
            except Exception:
                pass
        if reasons:
            item.caps_failed = True
            msg = "; ".join(reasons)
            item.error = ((item.error + " ") if item.error else "") + \
                f"Cap-Warnung: {msg}."
            logger.warning("Cap überschritten (%s): %s", item.title, msg)

    # ------------------------------------------------------------ Postprocessing
    @staticmethod
    def _post_process(item: QueueItem, out_path: Path) -> None:
        s = item.settings
        mode = s.post_processing
        src = Path(item.path)

        # Sichere Original-Nachbehandlung: Original nur dann löschen/verschieben,
        # wenn die Ausgabe intakt ist UND (falls Guardrail aktiv) die Qualität
        # erreicht wurde. Sonst das Original vorsichtshalber behalten.
        if mode in ("inplace", "archive") and s.safe_replace:
            quality_ok = True
            if s.verify_vmaf and item.vmaf_verify is not None:
                quality_ok = item.vmaf_verify >= s.verify_min
            caps_ok = not getattr(item, "caps_failed", False)
            if item.integrity_ok is False or not quality_ok or not caps_ok:
                if item.integrity_ok is False:
                    reason = "Integritäts-Check fehlgeschlagen"
                elif not caps_ok:
                    reason = "Größen-/Bitrate-Cap überschritten"
                else:
                    reason = (f"VMAF {item.vmaf_verify:.1f} < Ziel "
                              f"{s.verify_min:.0f}")
                item.error = ((item.error + " ") if item.error else "") + \
                    f"Original behalten (Sicherheit): {reason}."
                logger.warning("Sichere Nachbehandlung – Original behalten (%s): %s",
                               item.title, reason)
                return  # Original bleibt unangetastet, Output liegt in der Standard-Ausgabe

        try:
            if mode == "inplace":
                # Original durch neues File ersetzen (am Originalort)
                target = src.with_suffix(out_path.suffix)
                src.unlink(missing_ok=True)
                out_path.replace(target)
                item.output_path = str(target)
            elif mode == "archive":
                archive_dir = src.parent / config.ARCHIVE_DIRNAME
                archive_dir.mkdir(parents=True, exist_ok=True)
                src.replace(archive_dir / src.name)
            # mode == "keep": Original bleibt, Output liegt in der Standard-Ausgabe
        except OSError as e:
            item.error = f"Post-Processing-Warnung: {e}"

    def shutdown(self) -> None:
        self._stop = True
        with self._lock:
            for runner in self._active.values():
                if runner:
                    runner.cancel()
        self._wake.set()


_VALID_CODECS = {"av1", "hevc", "h264"}
_VALID_PLATFORMS = {"cpu", "nvidia", "intel", "amd"}


def _bump_quality(s: JobSettings) -> None:
    """Qualität für einen Guardrail-Retry erhöhen: CQ senken bzw. Bitrate anheben."""
    if s.rate_mode in ("bitrate", "abr"):
        s.quality = int(round(s.quality * config.VERIFY_BITRATE_FACTOR))
    else:
        s.quality = max(1, s.quality - config.VERIFY_CQ_STEP)


def build_job_settings(d: dict) -> JobSettings:
    """UI-/Profil-Dict → JobSettings (zentrale Zuordnung inkl. HDR-Modus)."""
    hdr_mode = d.get("hdr_mode", "tonemap")
    codec = d.get("codec", "av1")
    # DV-Behandlung: `dv_mode` (aus der UI bei DV-Quellen) hat Vorrang und legt
    # sowohl HDR- als auch DV-Verhalten fest. Ohne dv_mode gilt hdr_mode + Legacy.
    #   preserve -> DV-RPU übernehmen (HEVC->8.1, AV1->10.1), HDR bleibt
    #   hdr10    -> nur HDR10-Basis behalten (DV verwerfen)
    #   tonemap  -> HDR->SDR
    dv_mode = d.get("dv_mode", "") or ""
    if dv_mode == "preserve":
        preserve_dv, preserve_hdr, tonemap = True, True, False
    elif dv_mode == "hdr10":
        preserve_dv, preserve_hdr, tonemap = False, True, False
    elif dv_mode == "tonemap":
        preserve_dv, preserve_hdr, tonemap = False, False, True
    else:
        preserve_dv = bool(d.get("preserve_dv", False))
        preserve_hdr = (hdr_mode == "preserve") or preserve_dv
        tonemap = ((hdr_mode == "tonemap") or bool(d.get("tonemap"))) and not preserve_dv

    platform = d.get("platform", "cpu")
    # AV1-Dolby-Vision entsteht nur beim Encoden selbst und kann in FFmpeg
    # ausschließlich libsvtav1 (CPU) als Metadaten-OBUs einbetten. Hardware-
    # Encoder (NVENC/QSV/VAAPI) können DV weder einbetten noch nachträglich
    # injizieren – dovi_tool unterstützt AV1 auf der CLI nicht. Daher hier
    # ehrlich auf den HDR10-Basislayer zurückfallen statt später zu scheitern.
    if preserve_dv and codec == "av1" and platform != "cpu":
        preserve_dv, preserve_hdr, tonemap = False, True, False
        logger.info("AV1+DV nur mit CPU/libsvtav1 möglich – %s: HDR10-Fallback "
                    "(Encoder %s kann keine DV-RPU einbetten).",
                    d.get("title") or d.get("path") or "?", platform)

    return JobSettings(
        platform=platform,
        codec=codec,
        quality=int(d.get("quality", 28) or 28),
        target_height=d.get("target_height"),
        tonemap=tonemap,
        preserve_hdr=preserve_hdr,
        preserve_dv=preserve_dv,
        keep_subtitles=bool(d.get("keep_subtitles", True)),
        subtitle_per_track=bool(d.get("subtitle_per_track", False)),
        subtitle_track_settings=list(d.get("subtitle_track_settings", [])),
        keep_chapters=bool(d.get("keep_chapters", True)),
        keep_metadata=bool(d.get("keep_metadata", True)),
        film_grain=max(0, min(50, int(d.get("film_grain", 0) or 0))),
        denoise=d.get("denoise", "off"),
        two_pass=bool(d.get("two_pass", False)),
        anime=bool(d.get("anime", False)),
        video_mode=d.get("video_mode", "encode"),
        audio_opt_scope=d.get("audio_opt_scope", "bloated"),
        audio_min_bitrate_kbps=int(d.get("audio_min_bitrate_kbps", 700) or 700),
        chunked=bool(d.get("chunked", False)),
        chunk_seconds=max(15, int(d.get("chunk_seconds", 60) or 60)),
        chunk_cq_range=max(0, min(12, int(d.get("chunk_cq_range", 6) or 6))),
        verify_vmaf=bool(d.get("verify_vmaf", False)),
        verify_min=float(d.get("verify_min", 93) or 93),
        verify_retry=bool(d.get("verify_retry", False)),
        integrity_check=bool(d.get("integrity_check", True)),
        safe_replace=bool(d.get("safe_replace", True)),
        autocrop=bool(d.get("autocrop", False)),
        vmaf_check=bool(d.get("vmaf_check", True)),
        workflow=d.get("workflow", "auto"),
        target_vmaf=float(d.get("target_vmaf", 0) or 0),
        rate_mode=d.get("rate_mode", "cq"),
        compare_encoders=list(d.get("compare_encoders", [])),
        test_values=list(d.get("test_values", [20, 24, 28, 32]))[:4],
        clip_seconds=max(5, min(120, int(d.get("clip_seconds", 30) or 30))),
        samples=max(1, min(5, int(d.get("samples", 1) or 1))),
        generate_screenshots=bool(d.get("generate_screenshots", True)),
        post_processing=d.get("post_processing", "keep"),
        container=d.get("container", "auto") if d.get("container") in ("mkv", "mp4") else "auto",
        suffix=d.get("suffix", "_" + codec),
        audio_mode=d.get("audio_mode", "copy"),
        audio_codec=d.get("audio_codec", "aac"),
        audio_bitrate=int(d.get("audio_bitrate", 160) or 160),
        audio_channels=int(d.get("audio_channels", 0) or 0),
        audio_normalize=bool(d.get("audio_normalize", False)),
        audio_tracks=list(d.get("audio_tracks", [])),
        audio_per_track=bool(d.get("audio_per_track", False)),
        audio_track_settings=list(d.get("audio_track_settings", [])),
        batch_id=str(d.get("batch_id", "") or ""),
        edit_spec=dict(d.get("edit_spec", {}) or {}),
        out_mode=str(d.get("out_mode", "") or "default"),
        out_root=str(d.get("out_root", "") or ""),
        out_subdir=str(d.get("out_subdir", "") or ""),
        name_pattern=str(d.get("name_pattern", "") or "{stem}{suffix}"),
        on_duplicate=str(d.get("on_duplicate", "") or "ask"),
        max_output_mb=float(d.get("max_output_mb", 0) or 0),
        max_video_bitrate_kbps=int(d.get("max_video_bitrate_kbps", 0) or 0),
    )


def _session_name(item: QueueItem) -> str:
    """Lesbarer, eindeutiger Ordnername für Previews/VMAF-Archiv."""
    stem = Path(item.title).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:60] or "clip"
    return f"{safe}_{item.id[:6]}"


def _parse_encoders(entries: list) -> list:
    """"plattform:codec"-Strings zu geprüften (plattform, codec)-Tupeln."""
    out: list[tuple[str, str]] = []
    for e in entries or []:
        if ":" not in str(e):
            continue
        p, c = str(e).split(":", 1)
        if p in _VALID_PLATFORMS and c in _VALID_CODECS and (p, c) not in out:
            out.append((p, c))
    return out


def _container_ext(s: JobSettings) -> str:
    """Ziel-Container: explizite Wahl (mkv/mp4) oder Standard je Codec."""
    choice = getattr(s, "container", "auto")
    if choice == "mkv":
        return ".mkv"
    if choice == "mp4":
        return ".mp4"
    return CONTAINER.get(s.codec, ".mkv")


def _effective_out_mode(s: JobSettings) -> str:
    mode = str(getattr(s, "out_mode", "") or "").strip().lower()
    if mode in ("default", "beside", "custom"):
        return mode
    # Alte Queue-Einträge: in:* bedeutete „neben der Quelle".
    root = str(getattr(s, "out_root", "") or "")
    if root.startswith("in:"):
        return "beside"
    return "default"


def _output_path(item: QueueItem) -> Path:
    from . import job_plan
    out = job_plan.planned_output_path(Path(item.path), item.settings)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return out


def _quality_label(s: JobSettings) -> str:
    if s.rate_mode == "abr":
        return f"ABR {s.quality} kbit/s"
    if s.rate_mode == "bitrate":
        return f"{s.quality} kbit/s"
    return f"CQ/QP {s.quality}"


def _describe_dynamic(s: JobSettings) -> str:
    """HDR-/Dolby-Vision-Behandlung als lesbarer Text fürs Log."""
    if getattr(s, "preserve_dv", False):
        if s.codec == "av1":
            return "Dolby Vision übernehmen (nativ via libsvtav1 → Profil 10.1)"
        return "Dolby Vision übernehmen (dovi_tool-Reinjektion → Profil 8.1)"
    if getattr(s, "preserve_hdr", False):
        return "HDR10/HLG erhalten"
    if getattr(s, "tonemap", False):
        return "HDR→SDR (Tonemapping)"
    return "unverändert (SDR / keine HDR-Behandlung)"


def _describe_audio(s: JobSettings) -> str:
    """Audio-Einstellungen als lesbarer Text fürs Log."""
    mode = s.audio_mode
    if mode == "none":
        return "keine Tonspur"
    if mode == "encode":
        ch = {0: "Original-Kanäle", 1: "Mono", 2: "Stereo"}.get(
            s.audio_channels, f"{s.audio_channels} Kanäle")
        base = f"transkodieren → {s.audio_codec.upper()} {s.audio_bitrate} kbit/s, {ch}"
        if s.audio_normalize:
            base += ", normalisiert"
    else:
        base = "Original kopieren (Copy)"
    tracks = "alle Spuren" if not s.audio_tracks else f"Spuren {list(s.audio_tracks)}"
    if s.audio_per_track:
        tracks += " (pro Spur konfiguriert)"
    return f"{base}; {tracks}"


def _log_job_start(item: "QueueItem", info, out_path: Path, kind: str = "Encode") -> None:
    """Ausführliches Start-Log eines Jobs (alle wesentlichen Parameter).

    Landet über den Standard-Logger in den Docker-Container-Logs.
    """
    s = item.settings
    lines = [
        f"▶ {kind} gestartet: {item.title}",
        f"    Job-ID         : {item.id}  (Gruppe {item.group_id})",
        f"    Quelle         : {item.path}",
        f"    Ausgabe        : {out_path}",
    ]
    if info is not None:
        dv = f", Dolby Vision Profil {info.dv_profile}" if info.dolby_vision else ""
        lines.append(
            f"    Quelle-Info    : {(info.codec or '?').upper()} {info.width}x{info.height}, "
            f"{ff.human_duration(info.duration)}, {info.bit_depth}-bit, {info.hdr_type}{dv}, "
            f"{ff.human_size(info.size_bytes)}")

    if kind == "Audio-Remux":
        lines.append("    Video          : 1:1 kopieren (kein Re-Encode)")
        lines.append(f"    Container      : {_container_ext(s)}  |  Nachbearbeitung: {s.post_processing}")
        lines.append(
            f"    Audio          : {_describe_audio(s)} "
            f"(Scope: {s.audio_opt_scope}, ab {s.audio_min_bitrate_kbps} kbit/s)")
    else:
        enc_name = ff.encoder_name(s.platform, s.codec)
        scale = (f"{info.height}p → {s.target_height}p"
                 if (info is not None and s.target_height) else "Original beibehalten")
        extra = " · Zwei-Pass" if s.two_pass else ""
        if s.chunked:
            extra += f" · Chunked ({s.chunk_seconds}s, ±CQ {s.chunk_cq_range})"
        lines.append(
            f"    Encoder        : {s.platform}/{s.codec} → {enc_name}  "
            f"|  Container: {_container_ext(s)}")
        lines.append(f"    Rate/Qualität  : {_quality_label(s)}{extra}")
        if s.autocrop:
            crop_txt = (f"erkannt crop={item.crop}" if item.crop
                        else ("noch nicht erkannt" if item.crop == ""
                              else "keine Balken"))
            lines.append(f"    Auto-Crop      : aktiv ({crop_txt})")
        lines.append(f"    Skalierung     : {scale}")
        lines.append(f"    HDR/DV         : {_describe_dynamic(s)}")
        lines.append(
            f"    Video-Extras   : Film-Grain={s.film_grain}, Denoise={s.denoise}, "
            f"10-bit/Anime={'ja' if s.anime else 'nein'}")
        lines.append(f"    Audio          : {_describe_audio(s)}")
        lines.append(
            f"    Untertitel     : {'übernehmen' if s.keep_subtitles else 'verwerfen'}"
            + (" (pro Spur)" if s.subtitle_per_track else "")
            + f"  |  Kapitel: {'ja' if s.keep_chapters else 'nein'}"
            + f"  |  Metadaten: {'ja' if s.keep_metadata else 'nein'}")
        if s.verify_vmaf:
            lines.append(
                f"    Guardrail      : VMAF ≥ {s.verify_min:.0f}"
                + (f", bis zu {config.VERIFY_MAX_RETRIES} Wiederholung(en)"
                   if s.verify_retry else ", ohne Wiederholung"))
        integ = "an" if (s.integrity_check or s.post_processing in ("inplace", "archive")) else "aus"
        safe = "sicher (nur bei bestandener Prüfung)" if s.safe_replace else "immer"
        lines.append(
            f"    Nachbearbeitung: {s.post_processing} ({safe})  "
            f"|  Integritäts-Check: {integ}  |  Suffix: {s.suffix}")

    logger.info("\n".join(lines))


def _log_cmd(item: "QueueItem", cmd: list, label: str = "") -> None:
    """Vollständige FFmpeg-Kommandozeile ins Log schreiben (falls aktiviert)."""
    if not config.LOG_FFMPEG_CMD or not cmd:
        return
    tag = f" [{label}]" if label else ""
    logger.info("FFmpeg-Kommando%s (%s):\n    %s",
                tag, item.title, " ".join(str(c) for c in cmd))


def _counts(items: list[dict]) -> dict:
    c = {"waiting": 0, "running": 0, "done": 0, "failed": 0, "awaiting": 0}
    for it in items:
        st = it["status"]
        if st == STATUS_WAITING:
            c["waiting"] += 1
        elif st in (STATUS_RUNNING, STATUS_ANALYZING):
            c["running"] += 1
        elif st == STATUS_AWAITING:
            c["awaiting"] += 1
        elif st == STATUS_DONE:
            c["done"] += 1
        elif st in (STATUS_FAILED, STATUS_CANCELLED):
            c["failed"] += 1
    return c
