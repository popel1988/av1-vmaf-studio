"""Windows desktop entry point. No application/.NET imports before bootstrap."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _app_root() -> Path:
    return (Path(sys.executable) if getattr(sys, "frozen", False)
            else Path(__file__)).resolve().parent


class _NullStream:
    def write(self, data: str) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def _ensure_stdio(log_dir: Path) -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "studio.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = _NullStream()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _setup_environment() -> Path:
    from core.desktop_diagnostics import writable_directory
    root = _app_root()
    data = writable_directory(Path(os.environ.get("DATA_DIR", root / "data")))
    os.environ["DATA_DIR"] = str(data)
    os.environ["PATH"] = f"{root / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    if (root / "bin" / "model").is_dir():
        os.environ.setdefault("VMAF_MODEL_DIR", str(root / "bin" / "model"))
    # Keep config's configured roots. Never auto-authorize entire Windows drives.
    return root


def _find_free_port(preferred: int = 8080) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 25, thread=None, server=None) -> bool:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if thread is not None and not thread.is_alive():
            return False
        if server is not None and not server.started:
            time.sleep(0.1)
            continue
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def _find_app_browser() -> str | None:
    candidates = [
        shutil.which("msedge"), shutil.which("chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


def _open_desktop_window(url: str, data: Path, bridge) -> str:
    try:
        bridge._open_native(url)
        return "webview"
    except Exception:
        logging.getLogger("desktop").exception("Nativer Host nicht verfügbar; Browser-Fallback")
        if bridge._window is not None:
            # A partially initialized native UI cannot safely be mixed with Edge.
            raise RuntimeError("Der native Host brach nach der Fenstererstellung ab.") from None
    browser_exe = _find_app_browser() if os.name == "nt" else None
    profile = data / "window_profile"
    profile.mkdir(parents=True, exist_ok=True)

    def reopen():
        if browser_exe:
            subprocess.Popen([
                browser_exe, f"--app={url}", f"--user-data-dir={profile}",
                "--window-size=1480,920", "--no-first-run", "--no-default-browser-check",
            ])
        else:
            webbrowser.open(url)

    bridge._set_fallback("edge" if browser_exe else "browser", reopen)
    reopen()
    if os.name == "nt":
        # Browser subprocess lifetime is NOT browser-window lifetime. Keep a real
        # native controller so closing Edge cannot silently kill running jobs.
        while not bridge._allow_close:
            answer = ctypes.windll.user32.MessageBoxW(
                None, "Die Oberfläche läuft im Browser-Fallback.\n"
                "Explorer-Drop und native Dateiauswahl sind dort nicht verfügbar.\n"
                "Dateiöffnungen werden über die laufende Instanz weitergereicht.\n\n"
                "OK: Desktop-Server beenden (aktive Aufträge werden bestätigt).\n"
                "Abbrechen: Oberfläche erneut öffnen.",
                "AV1 / VMAF Studio – Browser-Fallback", 0x41,
            )
            if answer == 2:
                reopen()
                continue
            bridge._request_close(False)
            while bridge._stopping and not bridge._allow_close:
                time.sleep(0.1)
    else:
        while not bridge._allow_close:
            time.sleep(0.5)
    return "edge" if browser_exe else "browser"


def _smoke_test() -> dict:
    """No UI, server, registry writes or encode jobs. Suitable for --windowed EXE."""
    import importlib.metadata
    from core.desktop_host import load_webview
    versions = {name: importlib.metadata.version(name)
                for name in ("pywebview", "pythonnet", "clr_loader")}
    load_webview()
    if os.name == "nt":
        import clr
        clr.AddReference("System.Windows.Forms")
        from webview.util import interop_dll_path
        for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
            clr.AddReference(interop_dll_path(name))
        # Importing the backend loads Python.Runtime and the bundled WebView2 DLLs,
        # but does not create a form or install/modify the Edge runtime.
        import webview.platforms.winforms  # noqa: F401
    return {"ok": True, "frozen": bool(getattr(sys, "frozen", False)),
            "versions": versions, "interactive_webview_verified": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AV1 / VMAF Studio Desktop")
    parser.add_argument("--desktop-smoke-test", action="store_true")
    parser.add_argument("--tray", action="store_true", help="Optionales Symbol im Infobereich")
    parser.add_argument("--register-open-with", action="store_true",
                        help="Explizite Registrierung für den aktuellen Benutzer")
    parser.add_argument("files", nargs="*")
    options = parser.parse_args(argv)
    from core.desktop_diagnostics import install_hooks, report_error, writable_directory
    logs = writable_directory()
    _ensure_stdio(logs)
    install_hooks(logs, interactive=not options.desktop_smoke_test)
    instance = None
    bridge = None
    server = None
    server_thread = None
    server_socket = None
    stopped = threading.Event()
    stop_lock = threading.Lock()

    def stop_server():
        with stop_lock:
            if stopped.is_set() or server is None:
                return
            server.should_exit = True
            if server_thread is not None:
                server_thread.join(60)
                if server_thread.is_alive():
                    raise RuntimeError(
                        "Server beendet sich noch nicht. Der Prozess bleibt für die "
                        "Auftragsbereinigung geöffnet; erneut Beenden versuchen.")
            stopped.set()

    try:
        if options.desktop_smoke_test:
            result = _smoke_test()
            (logs / "desktop-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result), flush=True)
            return 0
        if options.register_open_with:
            # CLI also requires a visible confirmation before any registry write.
            from core.desktop_host import DesktopBridge
            result = DesktopBridge(None, None, None, logs).register_open_with()
            return 0 if result.get("ok") else 1
        from core.desktop_ipc import SingleInstance
        from core.desktop_paths import DesktopPathGrants, validate_paths
        from core.desktop_processes import install_no_console_processes
        install_no_console_processes()
        paths = validate_paths([str(Path(value).absolute()) for value in options.files])
        instance = SingleInstance(logs)
        if not instance.acquire():
            instance.forward(paths)
            return 0
        root = _setup_environment()
        del root
        data = Path(os.environ["DATA_DIR"])
        import uvicorn
        import app as studio_app
        from core import config
        from core.desktop_folders import DesktopMediaFolders
        from core.desktop_host import DesktopBridge, install_desktop_routes
        grants = DesktopPathGrants()
        grants.install(config)
        folders = DesktopMediaFolders(config, data)
        folders.restore()
        # Bind once and pass this actual socket to Uvicorn, avoiding port races.
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        requested_port = int(os.environ.get("PORT", "0"))
        server_socket.bind(("127.0.0.1", requested_port))
        server_socket.listen(128)
        port = server_socket.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        bridge = DesktopBridge(grants, studio_app.queue, stop_server, logs, options.tray,
                               folders=folders)
        install_desktop_routes(studio_app.app, bridge, url)
        studio_app.app.state.desktop_open_folder = bridge._open_folder
        instance.listen(lambda incoming: bridge._accept_paths(incoming, "ipc"))
        bridge._accept_paths(paths, "command-line")
        cfg = uvicorn.Config(studio_app.app, host="127.0.0.1", port=port,
                             log_level="info", access_log=False)
        server = uvicorn.Server(cfg)
        server_thread = threading.Thread(
            target=lambda: server.run(sockets=[server_socket]), name="studio-uvicorn", daemon=True)
        server_thread.start()
        # The root redirects to the login form when APP_PASSWORD is configured.
        # Probe that route instead of demanding unauthenticated access to an API.
        if not _wait_for_server(f"{url}/", thread=server_thread, server=server):
            raise RuntimeError("Der lokale Server wurde nicht bereit. Details stehen in studio.log.")
        _open_desktop_window(url, data, bridge)
        stop_server()
        return 0
    except KeyboardInterrupt:
        stop_server()
        return 0
    except Exception as exc:
        if options.desktop_smoke_test:
            result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
            from core.desktop_diagnostics import sanitize
            (logs / "desktop-smoke.json").write_text(sanitize(json.dumps(result)), encoding="utf-8")
        report_error(exc, logs, interactive=not options.desktop_smoke_test)
        return 1
    finally:
        if server is not None and not stopped.is_set():
            try:
                stop_server()
            except Exception:
                logging.getLogger("desktop").exception("Server-Bereinigung unvollständig")
                # Never let a daemon server disappear while cleanup still owns jobs.
                if server_thread is not None:
                    server_thread.join()
        if bridge is not None:
            bridge._dispose()
        if instance is not None:
            instance.close()
        if server_socket is not None:
            server_socket.close()


if __name__ == "__main__":
    # Catch even failures importing bootstrap modules or creating the log directory.
    try:
        raise SystemExit(main())
    except Exception as bootstrap_error:
        if os.name == "nt" and "--desktop-smoke-test" not in sys.argv:
            ctypes.windll.user32.MessageBoxW(
                None, f"Desktop-Start fehlgeschlagen ({type(bootstrap_error).__name__}).\n"
                "Bitte Schreibrechte für LOCALAPPDATA und die Installation prüfen.",
                "AV1 / VMAF Studio", 0x10)
        raise SystemExit(1)
