"""Process-local, exact-file grants originating from trusted native interactions.

No drive roots are added and no HTTP/JS method accepts arbitrary grant requests.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

MEDIA_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".mts", ".m2ts",
    ".mpg", ".mpeg", ".vob", ".wmv", ".flv", ".ogv", ".mxf", ".iso",
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff",
})
MAX_FILES = 256


def validate_paths(values: list[str]) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_FILES:
        raise ValueError("Maximal 256 Mediendateien gleichzeitig öffnen.")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or "\0" in value or len(value) > 32767:
            raise ValueError("Ungültiger Dateipfad.")
        # Do not permit device names, NT namespaces, ADS or relative paths over IPC.
        path = Path(value)
        if not path.is_absolute() or value.startswith(("\\\\?\\", "\\\\.\\", "//?/")):
            raise ValueError("Nur absolute, reguläre Mediendateipfade sind erlaubt.")
        if os.name == "nt" and ":" in value[2:]:
            raise ValueError("NTFS-Datenströme sind nicht erlaubt.")
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError("Keine unterstützte Mediendatei.")
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(str(resolved))
    return result


class DesktopPathGrants:
    def __init__(self) -> None:
        self._paths: set[str] = set()
        self._lock = threading.RLock()

    def grant(self, values: list[str]) -> list[str]:
        paths = validate_paths(values)
        with self._lock:
            self._paths.update(os.path.normcase(p) for p in paths)
        return paths

    def resolve(self, value: str) -> Path | None:
        try:
            paths = validate_paths([value])
        except (ValueError, OSError, RuntimeError):
            return None
        with self._lock:
            return Path(paths[0]) if os.path.normcase(paths[0]) in self._paths else None

    def install(self, config) -> None:
        """Extend only this desktop process's existing validator with exact grants.

        Existing roots/HTTP checks are preserved; siblings and output directories
        are NOT implicitly authorized. Restarting revokes these ephemeral grants.
        """
        original = config.resolve_input

        def resolve_input(value: str):
            result = original(value)
            return result if result is not None else self.resolve(value)

        config.resolve_input = resolve_input
