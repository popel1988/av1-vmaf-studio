"""Desktop-only subprocess policy: background tools must not open console windows."""
from __future__ import annotations

import inspect
import os
import subprocess


def install_no_console_processes() -> None:
    """Apply once, before importing modules that start FFmpeg or hardware tools.

    GUI executables still show their normal windows. Explicit new-console or
    detached-process requests are preserved. Docker/web launches do not call this.
    """
    if os.name != "nt" or getattr(subprocess.Popen, "_studio_no_console", False):
        return
    original = subprocess.Popen
    signature = inspect.signature(original)

    class BackgroundPopen(original):
        _studio_no_console = True

        def __init__(self, *args, **kwargs):
            arguments = signature.bind_partial(*args, **kwargs)
            flags = arguments.arguments.get("creationflags", 0)
            if not flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS):
                arguments.arguments["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
            super().__init__(*arguments.args, **arguments.kwargs)

    subprocess.Popen = BackgroundPopen
