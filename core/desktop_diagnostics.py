"""Desktop bootstrap diagnostics. Standard library only; safe before app imports."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
import tempfile
import threading
import traceback
from pathlib import Path


def writable_directory(preferred: Path | None = None) -> Path:
    candidates = []
    if preferred is not None:
        candidates.append(preferred)
    local = os.environ.get("LOCALAPPDATA")
    try:
        candidates.append((Path(local) if local else Path.home() / ".local" / "share")
                          / "AV1-VMAF-Studio")
    except RuntimeError:
        pass  # Damaged/missing user environment: private temporary directory below.
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory):
                pass
            return directory
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="av1-vmaf-studio-"))


def sanitize(text: str) -> str:
    for key in ("USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(key)
        if value:
            text = re.sub(re.escape(value), f"<{key}>", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)(password|token|secret|authorization)(\s*[=:]\s*)[^\s,;]+",
                  r"\1\2<redacted>", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@", r"\1<redacted>@", text)
    return text


def _copy_to_clipboard(text: str) -> None:
    # Tk is deliberately not required in the frozen bundle.
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    raw = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(0x0042, len(raw))
    if not handle:
        return
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return
        ctypes.memmove(pointer, raw, len(raw))
        kernel32.GlobalUnlock(handle)
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                if user32.SetClipboardData(13, handle):
                    handle = None  # Clipboard now owns this allocation.
            finally:
                user32.CloseClipboard()
    finally:
        if handle:
            kernel32.GlobalFree(handle)


def report_error(exc: BaseException, directory: Path, interactive: bool = True) -> None:
    details = sanitize("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    logging.getLogger("desktop").error("%s", details)
    try:
        (directory / "desktop-error.txt").write_text(details, encoding="utf-8")
    except OSError:
        pass
    if interactive and os.name == "nt":
        message = (
            "AV1 / VMAF Studio konnte die Desktop-Funktion nicht ausführen.\n\n"
            f"{sanitize(str(exc))}\n\nProtokoll: {directory / 'studio.log'}\n"
            "Bereinigte Diagnose in die Zwischenablage kopieren?"
        )
        if ctypes.windll.user32.MessageBoxW(None, message, "AV1 / VMAF Studio", 0x14) == 6:
            try:
                _copy_to_clipboard(details)
            except OSError:
                pass


def install_hooks(directory: Path, interactive: bool = True) -> None:
    handler = logging.FileHandler(directory / "studio.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    def hook(kind, exc, tb):
        if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
            report_error(exc.with_traceback(tb), directory, interactive)

    sys.excepthook = hook
    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)
