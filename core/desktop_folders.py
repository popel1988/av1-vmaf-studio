"""Persistent media roots explicitly selected through the native folder dialog."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

LOG = logging.getLogger(__name__)


class DesktopMediaFolders:
    def __init__(self, config, data: Path):
        self.config = config
        self.file = data / "desktop-folders.json"
        self._base_roots = list(getattr(config, "MEDIA_ROOTS", []))
        self._paths: list[str] = []
        self._lock = threading.RLock()

    @staticmethod
    def _validate(value: str) -> Path:
        if not isinstance(value, str) or not value or "\0" in value or len(value) > 32767:
            raise ValueError("Ungültiger Ordnerpfad.")
        path = Path(value)
        if not path.is_absolute() or value.startswith(("\\\\?\\", "\\\\.\\", "//?/")):
            raise ValueError("Nur absolute, reguläre Ordner sind erlaubt.")
        if os.name == "nt" and ":" in value[2:]:
            raise ValueError("NTFS-Datenströme sind nicht erlaubt.")
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Bitte einen vorhandenen Ordner oder ein Laufwerk auswählen.")
        return path

    def _register(self, path: Path) -> str:
        roots = self.config.all_media_roots()
        key = os.path.normcase(str(path))
        for name, base in roots:
            try:
                resolved = base.resolve()
            except (OSError, RuntimeError):
                continue
            if os.path.normcase(str(resolved)) == key:
                return self.config.rel_input(path) or name
        names = {name for name, _ in roots}
        name = self.config._slug(path.name or path.drive.replace(":", "") or "Ordner")
        candidate, suffix = name, 2
        while candidate in names:
            candidate, suffix = f"{name}{suffix}", suffix + 1
        self.config.MEDIA_ROOTS = [*self.config.MEDIA_ROOTS, (candidate, path)]
        self.config.MULTI_MEDIA = len(self.config.all_media_roots()) > 1
        return self.config.rel_input(path) or candidate

    def restore(self) -> None:
        try:
            values = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(values, list) or len(values) > 64 or not all(
                    isinstance(value, str) for value in values):
                raise ValueError("Ungültige gespeicherte Medienordner.")
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            LOG.exception("Gespeicherte Desktop-Ordner konnten nicht geladen werden")
            return
        with self._lock:
            self._paths = list(dict.fromkeys(values))
            for value in self._paths:
                try:
                    self._register(self._validate(value))
                except (OSError, ValueError, RuntimeError):
                    LOG.warning("Desktop-Medienordner derzeit nicht verfügbar: %s", value)

    def add_selected(self, value: str) -> dict:
        """Internal only: callers must use an actual native dialog result."""
        path = self._validate(value)
        with self._lock:
            values = list(self._paths)
            if os.path.normcase(str(path)) not in {os.path.normcase(p) for p in values}:
                if len(values) >= 64:
                    raise ValueError("Maximal 64 zusätzliche Medienordner.")
                values.append(str(path))
            # Persist before authorizing, so a failed write never grants a folder.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                                 dir=self.file.parent, delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(values, output, ensure_ascii=False, indent=2)
                temporary.replace(self.file)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._paths = values
            rel = self._register(path)
        return {"ok": True, "path": str(path), "rel": rel,
                "folders": self.list_folders(),
                "media_roots": self.config.media_roots_public()}

    def list_folders(self) -> list[dict]:
        """Liefert die vom Benutzer dauerhaft hinzugefügten Desktop-Ordner."""
        with self._lock:
            items = list(self._paths)
        out: list[dict] = []
        for raw in items:
            try:
                exists = Path(raw).is_dir()
            except OSError:
                exists = False
            out.append({"path": raw, "exists": exists})
        return out

    def remove_selected(self, value: str) -> dict:
        """Entfernt einen zuvor hinzugefügten Medienordner (reduziert nur Rechte)."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Ungültiger Ordnerpfad.")
        norm_target = os.path.normcase(value.strip().rstrip("\\/"))
        with self._lock:
            remaining = [
                p for p in self._paths
                if os.path.normcase(p.rstrip("\\/")) != norm_target
            ]
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                                 dir=self.file.parent, delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(remaining, output, ensure_ascii=False, indent=2)
                temporary.replace(self.file)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._paths = remaining
            self.config.MEDIA_ROOTS = list(self._base_roots)
            self.config.MULTI_MEDIA = len(self.config.all_media_roots()) > 1
            for kept in self._paths:
                try:
                    self._register(self._validate(kept))
                except (OSError, ValueError, RuntimeError):
                    LOG.warning("Desktop-Medienordner derzeit nicht verfügbar: %s", kept)
        return {
            "ok": True,
            "removed": value,
            "folders": self.list_folders(),
            "media_roots": self.config.media_roots_public(),
        }
