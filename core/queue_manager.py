"""Asynchrone Hintergrund-Warteschlange (eigener Worker-Thread).

Jeder Eintrag (QueueItem) entspricht einer Datei. Ein Batch erzeugt mehrere
Einträge mit gemeinsamer group_id: Der VMAF-Test wird repräsentativ für die
erste Datei der Gruppe durchgeführt und der ermittelte Qualitätswert auf alle
weiteren Dateien angewendet.
"""
from __future__ import annotations

import logging
import threading
import time
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
    target_height: Optional[int] = None  # None = Originalauflösung
    tonemap: bool = False
    vmaf_check: bool = True
    post_processing: str = "keep"  # keep | inplace | archive
    suffix: str = "_av1"


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
            "original_size": self.original_size,
            "original_human": ff.human_size(self.original_size) if self.original_size else "—",
            "output_size": self.output_size,
            "output_human": ff.human_size(self.output_size) if self.output_size else "—",
            "saved_bytes": self.saved_bytes,
            "saved_human": ff.human_size(self.saved_bytes) if self.saved_bytes else "—",
            "output_path": self.output_path,
        }


class QueueManager:
    def __init__(self) -> None:
        self._items: list[QueueItem] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._runner: Optional[EncodeRunner] = None
        self._active_id: Optional[str] = None
        self._group_quality: dict[str, int] = {}
        self._status_msg = ""
        self._stop = False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

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
            if item_id == self._active_id and self._runner:
                self._runner.cancel()
                return True
            for it in self._items:
                if it.id == item_id and it.status == STATUS_WAITING:
                    it.status = STATUS_CANCELLED
                    return True
        return False

    def clear_finished(self) -> None:
        with self._lock:
            self._items = [
                it for it in self._items
                if it.status in (STATUS_WAITING, STATUS_RUNNING, STATUS_ANALYZING)
            ]

    # ------------------------------------------------------------------- State
    def state(self) -> dict:
        with self._lock:
            items = [it.to_dict() for it in self._items]
            active = self._active_id
            msg = self._status_msg
        total_saved = sum(i["saved_bytes"] for i in items if i["saved_bytes"] > 0)
        return {
            "items": items,
            "active_id": active,
            "status_message": msg,
            "total_saved_bytes": total_saved,
            "total_saved_human": ff.human_size(total_saved) if total_saved else "0 B",
            "counts": _counts(items),
        }

    # ------------------------------------------------------------------ Worker
    def _next_item(self) -> Optional[QueueItem]:
        with self._lock:
            for it in self._items:
                if it.status == STATUS_WAITING:
                    return it
        return None

    def _worker(self) -> None:
        while not self._stop:
            item = self._next_item()
            if item is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            self._process(item)

    def _set_msg(self, msg: str) -> None:
        with self._lock:
            self._status_msg = msg

    def _process(self, item: QueueItem) -> None:
        self._active_id = item.id
        info, probe_err = probe_with_error(Path(item.path))
        if info is None:
            item.status = STATUS_FAILED
            item.error = f"ffprobe: {probe_err or 'kein gültiges Video'}"
            self._active_id = None
            return

        s = item.settings

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
            self._active_id = None
            return

        # --- VMAF-Test (optional, repräsentativ pro Gruppe) -------------------
        if s.vmaf_check and item.group_id not in self._group_quality:
            item.status = STATUS_ANALYZING
            self._set_msg(f"VMAF-Analyse: {item.title}")
            self._runner = None
            analysis = vmaf_mod.analyze(
                info, s.platform, s.codec, s.target_height, s.tonemap,
                status=self._set_msg,
            )
            item.vmaf = analysis.to_dict()
            if analysis.recommended_quality is not None:
                s.quality = analysis.recommended_quality
                self._group_quality[item.group_id] = analysis.recommended_quality
        elif item.group_id in self._group_quality:
            s.quality = self._group_quality[item.group_id]

        # --- Haupt-Encode -----------------------------------------------------
        item.status = STATUS_RUNNING
        self._set_msg(f"Encode: {item.title} (Q{s.quality})")
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

        self._runner = EncodeRunner(on_progress=on_prog)
        cmd = build_encode_cmd(
            info, out_path, s.platform, s.codec, s.quality,
            s.target_height, s.tonemap,
        )
        rc, stderr = self._runner.run(cmd, info.duration)

        if self._runner._cancel:
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

        self._runner = None
        self._active_id = None
        self._set_msg("")

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
        if self._runner:
            self._runner.cancel()
        self._wake.set()


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


def _counts(items: list[dict]) -> dict:
    c = {"waiting": 0, "running": 0, "done": 0, "failed": 0}
    for it in items:
        st = it["status"]
        if st == STATUS_WAITING:
            c["waiting"] += 1
        elif st in (STATUS_RUNNING, STATUS_ANALYZING):
            c["running"] += 1
        elif st == STATUS_DONE:
            c["done"] += 1
        elif st in (STATUS_FAILED, STATUS_CANCELLED):
            c["failed"] += 1
    return c
