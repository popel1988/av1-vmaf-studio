"""Asynchrone Hintergrund-Warteschlange (eigener Worker-Thread).

Jeder Eintrag (QueueItem) entspricht einer Datei. Ein Batch erzeugt mehrere
Einträge mit gemeinsamer group_id: Der VMAF-Test wird repräsentativ für die
erste Datei der Gruppe durchgeführt und der ermittelte Qualitätswert auf alle
weiteren Dateien angewendet.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
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
    vmaf_check: bool = True
    workflow: str = "auto"         # auto | manual | compare_only
    rate_mode: str = "cq"          # cq | bitrate | abr
    # Zusätzliche Vergleichs-Encoder als "plattform:codec"-Strings (z. B. "cpu:hevc")
    compare_encoders: list = field(default_factory=list)
    test_values: list = field(default_factory=lambda: [20, 24, 28, 32])
    clip_seconds: int = 30
    generate_screenshots: bool = True
    post_processing: str = "keep"
    suffix: str = "_av1"
    # Audio
    audio_mode: str = "copy"       # copy | encode | none
    audio_codec: str = "aac"       # aac | opus | ac3 | eac3 | flac
    audio_bitrate: int = 160       # kbit/s pro Stream
    audio_channels: int = 0        # 0 = Original, 1 = Mono, 2 = Stereo
    audio_normalize: bool = False
    audio_tracks: list = field(default_factory=list)  # leer = alle Spuren
    selected_result_index: Optional[int] = None


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
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
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
            "output_path": self.output_path,
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
        self._stop = False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

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
        p = Path(path)
        if not p.is_file():
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
        self._wake.set()
        return True

    def skip_encode(self, item_id: str) -> bool:
        """Vergleich-only oder manuell abbrechen (auch für Batch-Folgedateien)."""
        with self._lock:
            for it in self._items:
                if it.id == item_id and it.status == STATUS_AWAITING:
                    it.status = STATUS_DONE
                    self._group_skip.add(it.group_id)
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

    # ------------------------------------------------------------------- State
    def state(self) -> dict:
        with self._lock:
            items = [it.to_dict() for it in self._items]
            active_ids = list(self._active.keys())
            msg = self._status_msg
            parallel = self._max_parallel
        total_saved = sum(i["saved_bytes"] for i in items if i["saved_bytes"] > 0)
        return {
            "items": items,
            "active_ids": active_ids,
            "active_id": active_ids[0] if active_ids else None,  # Rückwärtskompat.
            "status_message": msg,
            "max_parallel": parallel,
            "total_saved_bytes": total_saved,
            "total_saved_human": ff.human_size(total_saved) if total_saved else "0 B",
            "counts": _counts(items),
        }

    # ------------------------------------------------------------------ Worker
    def _claim_next_locked(self) -> Optional[QueueItem]:
        """Nächsten startbaren Job holen (Lock muss gehalten werden)."""
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
        self._wake.set()

    def _run_job(self, item: QueueItem) -> None:
        try:
            self._process(item)
        except Exception as e:  # pragma: no cover - Schutz vor Worker-Absturz
            item.status = STATUS_FAILED
            item.error = f"Interner Fehler: {e}"
            logger.exception("Job abgestürzt: %s", item.title)
        finally:
            self._finish_active(item.id)

    def _process(self, item: QueueItem) -> None:
        info, probe_err = probe_with_error(Path(item.path))
        if info is None:
            item.status = STATUS_FAILED
            item.error = f"ffprobe: {probe_err or 'kein gültiges Video'}"
            return

        s = item.settings

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
                generate_screenshots=s.generate_screenshots,
                item_id=item.id,
                session_name=_session_name(item),
                source_title=item.title,
                encoders=_parse_encoders(s.compare_encoders),
            )
            analysis = vmaf_mod.analyze(
                info, s.platform, s.codec, s.target_height, s.tonemap,
                opts=vmaf_opts,
                status=lambda m: setattr(item, "message", m),
                cancelled=lambda: item.id in self._cancel_ids,
            )
            item.vmaf = analysis.to_dict()
            item.message = ""

            if item.id in self._cancel_ids:
                item.status = STATUS_CANCELLED
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

        enc_kw: dict = {
            "audio_mode": s.audio_mode,
            "audio_codec": s.audio_codec,
            "audio_bitrate_kbps": s.audio_bitrate,
            "audio_channels": s.audio_channels,
            "audio_normalize": s.audio_normalize,
            "audio_tracks": list(s.audio_tracks),
        }
        if s.rate_mode in ("bitrate", "abr"):
            enc_kw["rate_mode"] = s.rate_mode
            enc_kw["bitrate_kbps"] = s.quality

        runner = EncodeRunner(on_progress=on_prog)
        with self._lock:
            self._active[item.id] = runner
        cmd = build_encode_cmd(
            info, out_path, s.platform, s.codec, s.quality,
            s.target_height, s.tonemap, **enc_kw,
        )
        rc, stderr = runner.run(cmd, info.duration)

        if runner._cancel:
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
            self._post_process(item, out_path)
            item.status = STATUS_DONE
            item.progress["percent"] = 100.0
        item.message = ""

    # ------------------------------------------------------------ Postprocessing
    @staticmethod
    def _post_process(item: QueueItem, out_path: Path) -> None:
        mode = item.settings.post_processing
        src = Path(item.path)
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
            # mode == "keep": Original bleibt, Output liegt in OUTPUT_DIR
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


def _output_path(item: QueueItem) -> Path:
    src = Path(item.path)
    ext = CONTAINER.get(item.settings.codec, ".mkv")
    if item.settings.post_processing == "inplace":
        # Temporäre Datei neben dem Original
        return src.with_name(f"{src.stem}.__tmp__{ext}")
    # In das Output-Volume spiegeln (relativer Pfad ab INPUT_DIR)
    try:
        rel = src.relative_to(config.INPUT_DIR)
        target = config.OUTPUT_DIR / rel
    except ValueError:
        target = config.OUTPUT_DIR / src.name
    return target.with_name(f"{src.stem}{item.settings.suffix}{ext}")


def _quality_label(s: JobSettings) -> str:
    if s.rate_mode == "abr":
        return f"ABR {s.quality} kbit/s"
    if s.rate_mode == "bitrate":
        return f"{s.quality} kbit/s"
    return f"CQ/QP {s.quality}"


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
