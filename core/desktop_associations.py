"""Explicit, optional current-user 'Open with' registration; never changes defaults."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROG_ID = "AV1VMAFStudio.Media"
EXTENSIONS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts")


def registration_command() -> str:
    executable = str(Path(sys.executable).resolve())
    args = [executable]
    if not getattr(sys, "frozen", False):
        args.append(str(Path(__file__).resolve().parent.parent / "desktop_main.py"))
    # %1 is always quoted, including files with whitespace or shell metacharacters.
    return subprocess.list2cmdline(args) + ' "%1"'


def register_open_with() -> dict:
    if os.name != "nt":
        raise RuntimeError("Öffnen mit ist nur unter Windows verfügbar.")
    import winreg

    root = r"Software\Classes"
    entries = (
        (rf"{root}\{PROG_ID}", "", "AV1 / VMAF Studio Mediendatei"),
        (rf"{root}\{PROG_ID}\shell\open\command", "", registration_command()),
    )
    for path, name, value in entries:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    for extension in EXTENSIONS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"{root}\{extension}\OpenWithProgids") as key:
            winreg.SetValueEx(key, PROG_ID, 0, winreg.REG_NONE, b"")
    return {"ok": True, "extensions": list(EXTENSIONS), "default_changed": False}
