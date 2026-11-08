"""Desktop-Starter für AV1 / VMAF Compression Studio (Windows .exe & lokal).

Startet den FastAPI/Uvicorn-Server im Hintergrund und öffnet die Oberfläche
direkt in einem eigenen nativen Desktop-Fenster (pywebview / Edge WebView2
oder Chromium/Edge im App-Modus). Beim Schließen des Fensters werden Server
und laufende Vorschau-Prozesse sauber beendet.
"""
from __future__ import annotations

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
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _setup_environment() -> Path:
    root = _app_root()
    bin_dir = root / "bin"
    model_dir = bin_dir / "model"
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    # ./bin an den Anfang von PATH stellen, damit DLLs und Hilfsprogramme
    # (ffmpeg, ffprobe, mkvpropedit, dovi_tool, hdr10plus_tool) sofort gefunden werden.
    cur_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{cur_path}"
    os.environ.setdefault("DATA_DIR", str(data_dir))
    if model_dir.is_dir():
        os.environ.setdefault("VMAF_MODEL_DIR", str(model_dir))

    # Unter Windows alle vorhandenen Laufwerke (C:, D:, E:, ...) plus Benutzer-Videos
    # automatisch als Medienwurzeln anbieten, falls MEDIA_DIRS nicht manuell gesetzt ist.
    if os.name == "nt" and not os.environ.get("MEDIA_DIRS") and not os.environ.get("MEDIA_DIR"):
        roots: list[str] = []
        videos = Path.home() / "Videos"
        if videos.is_dir():
            roots.append(f"Videos={videos}")
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            try:
                if drive.exists():
                    roots.append(f"Laufwerk_{letter}={drive}")
            except OSError:
                continue
        if roots:
            os.environ["MEDIA_DIRS"] = ";".join(roots)
    return root


class _NullStream:
    """Ersatz für fehlendes stdout/stderr, falls die Logdatei nicht schreibbar ist."""

    def write(self, data: str) -> int:
        return 0

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def _ensure_stdio(log_dir: Path) -> None:
    """PyInstaller --windowed setzt stdout/stderr auf None.

    Uvicorn ruft beim Logging-Setup ``stdout.isatty()`` auf und bricht sonst mit
    ``AttributeError: 'NoneType' object has no attribute 'isatty'`` ab.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream: object
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "studio.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = _NullStream()
    if sys.stdout is None:
        sys.stdout = stream  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream  # type: ignore[assignment]


def _find_free_port(preferred: int = 8080) -> int:
    for port in (preferred, 8081, 8082, 8765, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return int(s.getsockname()[1])
            except OSError:
                continue
    return preferred


def _wait_for_server(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _find_app_browser() -> str | None:
    """Sucht Microsoft Edge oder Chrome für den nativen --app=... Fenstermodus."""
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


def main() -> None:
    root = _setup_environment()
    _ensure_stdio(root / "data")
    port = int(os.environ.get("PORT", "0")) or _find_free_port(8080)
    url = f"http://127.0.0.1:{port}"

    import uvicorn
    import app as studio_app

    cfg = uvicorn.Config(
        studio_app.app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(cfg)
    srv_thread = threading.Thread(target=server.run, name="studio-uvicorn", daemon=True)
    srv_thread.start()

    _wait_for_server(f"{url}/api/config/paths", timeout=25.0)

    # 1. Bevorzugt: Natives Desktop-Fenster über pywebview (Edge WebView2)
    try:
        import webview  # type: ignore
        webview.create_window(
            "AV1 / VMAF Compression Studio",
            url,
            width=1480,
            height=920,
            min_size=(1024, 680),
            text_select=True,
        )
        webview.start()
        server.should_exit = True
        return
    except ImportError:
        pass

    # 2. Fallback: Edge / Chrome im nativen App-Fenster-Modus (--app=...) ohne Adressleiste
    browser_exe = _find_app_browser()
    if browser_exe and os.name == "nt":
        profile_dir = root / "data" / "window_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([
            browser_exe,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--window-size=1480,920",
            "--no-first-run",
            "--no-default-browser-check",
        ])
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return

    # 3. Letzter Fallback: Standardbrowser öffnen und Prozess am Leben halten
    webbrowser.open(url)
    try:
        while srv_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True


if __name__ == "__main__":
    main()
