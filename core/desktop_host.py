"""Native host, narrowly exposed JS API and optional tray lifecycle."""
from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from core.desktop_win32 import (
    Win32SleepGuard,
    Win32Taskbar,
    compute_taskbar_status,
    execute_power_action,
    normalize_post_action,
    should_prevent_sleep,
    unblock_zone_identifiers,
)

LOG = logging.getLogger(__name__)
EVENT_PATHS = "studio:open-paths"
EVENT_CAPABILITIES = "studio:desktop-capabilities"


def load_webview():
    if os.name == "nt":
        import sys as _sys
        app_root = (Path(_sys.executable) if getattr(_sys, "frozen", False)
                    else Path(__file__).resolve().parent.parent).resolve().parent if getattr(_sys, "frozen", False) else Path(__file__).resolve().parent.parent
        unblock_zone_identifiers(app_root)
        # Select the supported runtime explicitly before importing clr/webview.
        # Never load a stray Python.Runtime.dll from cwd or the executable root.
        import pythonnet
        pythonnet.load("netfx")
    import webview
    if os.name == "nt":
        # Import edgechromium directly, NOT winforms: winforms silently selects
        # legacy MSHTML when runtime detection fails (and writes IE registry keys).
        import webview.platforms.edgechromium  # noqa: F401
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment
        version = CoreWebView2Environment.GetAvailableBrowserVersionString()
        if not version:
            raise RuntimeError("Microsoft Edge WebView2 Runtime ist nicht installiert.")
        # Mirror winforms' prerequisites before importing that backend, so an
        # undetected runtime cannot cause an implicit MSHTML/IE registry mutation.
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            if winreg.QueryValueEx(key, "Release")[0] < 394802:
                raise RuntimeError(".NET Framework 4.6.2 oder neuer wird benötigt.")
        registered = False
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            prefix = r"SOFTWARE\Microsoft" if hive == winreg.HKEY_CURRENT_USER else r"SOFTWARE\WOW6432Node\Microsoft"
            for app_id in ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                           "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
                           "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
                           "{65C35B14-6C1D-4122-AC46-7148CC9D6497}"):
                try:
                    with winreg.OpenKey(hive, prefix + rf"\EdgeUpdate\Clients\{app_id}") as key:
                        registered |= int(str(winreg.QueryValueEx(key, "pv")[0]).split(".")[0]) >= 86
                except (OSError, ValueError):
                    pass
        if not registered and not webview.settings["WEBVIEW2_RUNTIME_PATH"]:
            raise RuntimeError("WebView2 Runtime wird vom nativen Backend nicht erkannt.")
    return webview


class DesktopBridge:
    def __init__(self, grants, queue, stop_server, diagnostics, tray_enabled=False,
                 folders=None):
        self._grants = grants
        self._folders = folders
        self._queue = queue
        self._stop_server = stop_server
        self._diagnostics = diagnostics
        self._window = None
        self._webview = None
        self._tray = None
        self._tray_enabled = tray_enabled
        self._pending = deque(maxlen=64)
        self._lock = threading.RLock()
        self._ready = False
        self._page_loaded = threading.Event()
        self._origin = None
        self._native_drop_handler = None
        self._allow_close = False
        self._stopping = False
        self._prompting = False
        self._reopen_callback = None
        self._prevent_sleep = True
        self._post_queue_action = "none"
        self._had_active_work = False
        self._power_countdown = None  # {"action": str, "seconds": int}
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._sleep_guard = Win32SleepGuard()
        self._taskbar = Win32Taskbar()
        self._capabilities = {
            "mode": "starting", "native_host": False, "native_drop": False,
            "file_dialog": False, "single_instance": True, "open_paths": True,
            "folder_dialog": False, "folder_management": False,
            "tray": False, "close_confirmation": False,
            "open_with_registration": os.name == "nt", "upload_required": False,
            "open_folder": os.name == "nt",
            "taskbar_progress": os.name == "nt",
            "sleep_prevention": os.name == "nt",
            "post_queue_actions": os.name == "nt",
        }

    def capabilities(self):
        return dict(self._capabilities)

    def take_pending(self):
        self._check_origin()
        with self._lock:
            values = list(self._pending)
            self._pending.clear()
        return values

    def select_files(self):
        self._check_origin()
        if self._window is None or self._webview is None:
            return {"ok": False, "error": "Nativer Dateidialog nicht verfügbar."}
        selected = self._window.create_file_dialog(
            self._webview.FileDialog.OPEN, allow_multiple=True,
            file_types=("Mediendateien (*.mkv;*.mp4;*.m4v;*.mov;*.avi;*.webm;*.ts;*.m2ts;*.flac;*.wav;*.mp3)",
                        "Alle Dateien (*.*)"),
        )
        if selected:
            return self._accept_paths(list(selected), "dialog")
        return {"ok": True, "paths": [], "cancelled": True}

    def select_folder(self):
        self._check_origin()
        if self._window is None or self._webview is None or self._folders is None:
            return {"ok": False, "error": "Nativer Ordnerdialog nicht verfügbar."}
        selected = self._window.create_file_dialog(self._webview.FileDialog.FOLDER)
        if not selected:
            return {"ok": True, "cancelled": True}
        # No JS/HTTP argument can grant a path: use only the native dialog result.
        result = self._folders.add_selected(str(selected[0]))
        self._dispatch("studio:media-roots-changed", result)
        return result

    def list_folders(self):
        self._check_origin()
        if self._folders is None:
            return {"ok": True, "folders": [], "media_roots": []}
        return {
            "ok": True,
            "folders": self._folders.list_folders(),
            "media_roots": self._folders.config.media_roots_public(),
        }

    def remove_folder(self, path: str):
        self._check_origin()
        if self._folders is None:
            return {"ok": False, "error": "Ordnerverwaltung nicht verfügbar."}
        result = self._folders.remove_selected(str(path))
        self._dispatch("studio:media-roots-changed", result)
        return result

    def get_power_settings(self):
        self._check_origin()
        with self._lock:
            cd = dict(self._power_countdown) if self._power_countdown else None
            return {
                "ok": True,
                "prevent_sleep": bool(self._prevent_sleep),
                "sleep_blocking": bool(self._sleep_guard.blocking),
                "post_queue_action": self._post_queue_action,
                "countdown": cd,
            }

    def set_prevent_sleep(self, enabled: bool):
        self._check_origin()
        with self._lock:
            self._prevent_sleep = bool(enabled)
        return self.get_power_settings()

    def set_post_queue_action(self, action: str):
        self._check_origin()
        norm = normalize_post_action(action)
        with self._lock:
            self._post_queue_action = norm
            if norm == "none":
                self._power_countdown = None
        self._dispatch("studio:desktop-power", self.get_power_settings())
        return self.get_power_settings()

    def cancel_post_queue_action(self):
        self._check_origin()
        with self._lock:
            self._post_queue_action = "none"
            self._power_countdown = None
        self._dispatch("studio:desktop-power", self.get_power_settings())
        return self.get_power_settings()

    def register_open_with(self):
        self._check_origin()
        # Only an explicit user invocation of this bridge method reaches the registry.
        if os.name != "nt":
            return {"ok": False, "error": "Nur unter Windows verfügbar."}
        if ctypes.windll.user32.MessageBoxW(
            None, "AV1 / VMAF Studio für Ihr Benutzerkonto unter „Öffnen mit“ anbieten?\n"
            "Standardprogramme werden nicht geändert.", "Öffnen mit registrieren", 0x24,
        ) != 6:
            return {"ok": False, "cancelled": True}
        from core.desktop_associations import register_open_with
        return register_open_with()

    def status(self):
        state = self._queue.state()
        return {"active": len(state.get("active_ids") or []),
                "stopping": self._stopping, "capabilities": self.capabilities()}

    def _check_origin(self):
        if self._window is not None and self._origin is not None:
            current = self._window.get_current_url()
            if not current or urlsplit(str(current))[:2] != self._origin:
                raise PermissionError("Desktop-Brücke ist nur für die lokale App freigegeben.")

    def _accept_paths(self, paths, source="ipc"):
        accepted = self._grants.grant(paths)
        detail = {"id": uuid.uuid4().hex, "paths": accepted, "source": source}
        with self._lock:
            if accepted:
                if len(self._pending) == self._pending.maxlen:
                    raise ValueError("Zu viele noch nicht übernommene Dateiöffnungen.")
                self._pending.append(detail)
        if self._ready:
            # Do not let a blocked renderer delay the authenticated IPC ack.
            threading.Thread(target=lambda: self._dispatch(EVENT_PATHS, detail),
                             name="desktop-deliver", daemon=True).start()
        if self._window is not None or self._reopen_callback is not None:
            threading.Thread(target=self._reopen, name="desktop-reopen", daemon=True).start()
        return {"ok": True, **detail}

    def _dispatch(self, event, detail):
        if self._window is None:
            return
        try:
            self._check_origin()
            self._window.evaluate_js(
                f"window.dispatchEvent(new CustomEvent({json.dumps(event)},"
                f"{{detail:{json.dumps(detail, ensure_ascii=True)}}}));"
            )
        except Exception:
            LOG.exception("Desktop-Ereignis konnte nicht zugestellt werden")

    def _reopen(self):
        if self._window is not None:
            self._window.show()
            self._window.restore()
        elif self._reopen_callback is not None:
            self._reopen_callback()

    def _loaded(self):
        self._ready = False
        self._check_origin()
        from webview.dom import DOMEventHandler
        document = self._window.dom.document

        def accept_drop(paths):
            if paths:
                try:
                    self._check_origin()
                    self._accept_paths(paths, "drop")
                except (ValueError, OSError) as exc:
                    self._dispatch("studio:desktop-error", {"message": str(exc)})

        if os.name == "nt" and self._native_drop_handler is None:
            # Do not trust a serialized pywebviewFullPath supplied by JavaScript.
            # Read actual COM-backed files from WebView2's native additional
            # objects, the same source pywebview uses to enrich its DOM events.
            def native_drop(sender, args):
                if str(args.get_WebMessageAsJson()) != '"FilesDropped"':
                    return
                objects = args.get_AdditionalObjects()
                if objects is None:
                    return
                paths = [str(file.Path) for file in list(objects)
                         if "CoreWebView2File" in str(type(file))]
                threading.Thread(target=lambda: accept_drop(paths),
                                 name="desktop-native-drop", daemon=True).start()

            self._native_drop_handler = native_drop
            self._window.native.browser.webview.WebMessageReceived += native_drop

        def dropped(event):
            # DOM listeners enable pywebview's native AdditionalObjects capture
            # and suppress browser navigation/upload. On Windows the DOM's
            # serialized path fields are intentionally never authorized.
            if os.name != "nt":
                accept_drop([entry["pywebviewFullPath"]
                             for entry in event.get("dataTransfer", {}).get("files", [])
                             if entry.get("pywebviewFullPath")])

        document.events.dragenter += DOMEventHandler(lambda event: None, True, True)
        document.events.dragover += DOMEventHandler(lambda event: None, True, True, debounce=250)
        document.events.drop += DOMEventHandler(dropped, True, True)
        self._capabilities["native_drop"] = True
        self._ready = True
        self._page_loaded.set()
        self._dispatch(EVENT_CAPABILITIES, self.capabilities())
        # Pending entries remain until take_pending() acknowledges them, avoiding
        # lost command-line opens when frontend listeners register after loaded.
        for detail in list(self._pending):
            self._dispatch(EVENT_PATHS, detail)

    def _start_tray(self):
        if not self._tray_enabled:
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
            image = Image.new("RGB", (64, 64), "#192b45")
            ImageDraw.Draw(image).text((12, 22), "AV1", fill="white")

            def _post_item(label, key):
                return pystray.MenuItem(
                    label,
                    lambda icon, item: self._set_post_action_internal(key),
                    checked=lambda item: self._post_queue_action == key,
                    radio=True,
                )

            self._tray = pystray.Icon(
                "AV1-VMAF-Studio", image, "AV1 / VMAF Studio",
                pystray.Menu(
                    pystray.MenuItem("Fenster öffnen", lambda icon, item: self._reopen(), default=True),
                    pystray.MenuItem(lambda item: f"Aktive Aufträge: {self.status()['active']}",
                                     lambda icon, item: None, enabled=False),
                    pystray.MenuItem(
                        "Nach Warteschlange",
                        pystray.Menu(
                            _post_item("Nichts tun", "none"),
                            _post_item("Energie sparen", "sleep"),
                            _post_item("Ruhezustand", "hibernate"),
                            _post_item("Herunterfahren", "shutdown"),
                        ),
                    ),
                    pystray.MenuItem("Beenden …", lambda icon, item: self._request_close(False)),
                ),
            )
            self._tray.run_detached()
            self._capabilities["tray"] = True
        except Exception:
            LOG.exception("Tray nicht verfügbar; Hintergrundmodus deaktiviert")
            self._tray = None

    def _set_post_action_internal(self, action: str):
        norm = normalize_post_action(action)
        with self._lock:
            self._post_queue_action = norm
            if norm == "none":
                self._power_countdown = None
        if self._ready:
            self._dispatch("studio:desktop-power", {
                "ok": True,
                "prevent_sleep": bool(self._prevent_sleep),
                "sleep_blocking": bool(self._sleep_guard.blocking),
                "post_queue_action": self._post_queue_action,
                "countdown": dict(self._power_countdown) if self._power_countdown else None,
            })

    def _notify_local(self, title: str, message: str) -> None:
        clean_title = str(title or "AV1 / VMAF Studio").replace("\n", " ").strip()[:60]
        clean_msg = str(message or "").strip()[:240]
        if self._tray is not None:
            try:
                self._tray.notify(clean_msg or clean_title, clean_title)
            except Exception:
                LOG.debug("Tray-Benachrichtigung nicht verfügbar", exc_info=True)
        if self._ready:
            self._dispatch("studio:desktop-toast", {"title": clean_title, "message": clean_msg})

    def _get_hwnd(self) -> int:
        win = self._window
        if win is None:
            return 0
        try:
            native = getattr(win, "native", None)
            handle = getattr(native, "Handle", None)
            if handle is not None:
                if hasattr(handle, "ToInt64"):
                    return int(handle.ToInt64())
                return int(handle)
        except Exception:
            pass
        return 0

    def _monitor_tick(self, dt_sec: float = 2.0) -> Optional[str]:
        """Einzelner Überwachungsschritt (Standby, Taskleiste, Post-Queue-Countdown).

        Gibt eine auszuführende Energieaktion zurück, falls der Countdown abgelaufen ist.
        """
        if self._queue is None:
            return None
        try:
            qstate = self._queue.state() or {}
        except Exception:
            return None

        # 1. Standby-Schutz auf demselben Thread aktualisieren
        prevent = should_prevent_sleep(qstate, enabled=self._prevent_sleep)
        self._sleep_guard.update(prevent)

        # 2. Taskleisten-Fortschritt (Grün / Gelb / Rot)
        hwnd = self._get_hwnd()
        if hwnd:
            tbp_state, comp, tot = compute_taskbar_status(qstate)
            self._taskbar.apply(hwnd, tbp_state, comp, tot)

        # 3. Nach-Warteschlange-Aktion prüfen
        counts = qstate.get("counts") or {}
        busy = (
            bool(qstate.get("active_ids"))
            or int(counts.get("waiting") or 0) > 0
            or int(counts.get("awaiting") or 0) > 0
        )
        trigger_action: Optional[str] = None
        dispatch_power = False
        toast_payload: Optional[tuple[str, str]] = None

        with self._lock:
            if busy:
                self._had_active_work = True
                if self._power_countdown is not None:
                    self._power_countdown = None
                    dispatch_power = True
            elif not qstate.get("paused") and self._had_active_work:
                self._had_active_work = False
                if self._post_queue_action != "none":
                    act = self._post_queue_action
                    self._post_queue_action = "none"
                    self._power_countdown = {"action": act, "seconds": 30}
                    dispatch_power = True
                    labels = {
                        "sleep": "Energie sparen",
                        "hibernate": "Ruhezustand",
                        "shutdown": "Herunterfahren",
                    }
                    toast_payload = (
                        "Warteschlange abgeschlossen",
                        f"{labels.get(act, act)} in 30 Sekunden (in der App abbrechbar).",
                    )
            elif self._power_countdown is not None:
                rem = max(0, int(round(self._power_countdown["seconds"] - dt_sec)))
                self._power_countdown["seconds"] = rem
                dispatch_power = True
                if rem <= 0:
                    trigger_action = self._power_countdown["action"]
                    self._power_countdown = None

            power_snapshot = {
                "ok": True,
                "prevent_sleep": bool(self._prevent_sleep),
                "sleep_blocking": bool(self._sleep_guard.blocking),
                "post_queue_action": self._post_queue_action,
                "countdown": dict(self._power_countdown) if self._power_countdown else None,
            }

        if toast_payload:
            self._notify_local(*toast_payload)
        if dispatch_power and self._ready:
            self._dispatch("studio:desktop-power", power_snapshot)
        return trigger_action

    def _start_runtime_monitor(self) -> None:
        if self._monitor_thread is not None or self._queue is None:
            return
        self._monitor_stop.clear()

        def loop():
            last = time.monotonic()
            try:
                while not self._monitor_stop.wait(1.5):
                    now = time.monotonic()
                    dt = max(1.0, now - last)
                    last = now
                    act = self._monitor_tick(dt_sec=dt)
                    if act:
                        self._sleep_guard.clear()
                        execute_power_action(act)
            finally:
                self._sleep_guard.clear()
                self._taskbar.close()

        self._monitor_thread = threading.Thread(
            target=loop, name="desktop-runtime-monitor", daemon=True
        )
        self._monitor_thread.start()

    def _request_close(self, allow_background=True):
        if self._allow_close:
            return True
        if self._stopping or self._prompting:
            return False
        active = bool(self._queue.state().get("active_ids"))
        if active:
            self._prompting = True
            try:
                if os.name != "nt":
                    return False  # No silent cancellation without a native confirmation.
                background = allow_background and self._tray is not None
                text = "Es laufen Aufträge.\nJa: Aufträge stoppen und beenden.\n"
                text += ("Nein: im Infobereich weiterarbeiten.\nAbbrechen: Fenster offen lassen."
                         if background else "Nein: Fenster offen lassen.")
                answer = ctypes.windll.user32.MessageBoxW(
                    None, text, "AV1 / VMAF Studio schließen", 0x23 if background else 0x24)
                if background and answer == 7:
                    if self._window is not None:
                        self._window.hide()
                    return False
                if answer != 6:
                    return False
            finally:
                self._prompting = False
        self._stopping = True

        def stop():
            try:
                # Uvicorn triggers FastAPI's shutdown: queue/preview cleanup then join.
                self._stop_server()
            except Exception as exc:
                self._stopping = False
                from core.desktop_diagnostics import report_error
                report_error(exc, self._diagnostics)
                return
            self._allow_close = True
            if self._tray is not None:
                self._tray.stop()
            if self._window is not None:
                self._window.destroy()

        threading.Thread(target=stop, name="desktop-shutdown", daemon=True).start()
        return False

    def _open_native(self, url):
        self._webview = load_webview()
        self._origin = urlsplit(url)[:2]
        self._window = self._webview.create_window(
            "AV1 / VMAF Compression Studio", url, js_api=self,
            width=1480, height=920, min_size=(1024, 680), text_select=True,
        )
        self._capabilities.update(mode="webview", native_host=True, file_dialog=True,
                                  folder_dialog=self._folders is not None,
                                  folder_management=self._folders is not None,
                                  close_confirmation=True)
        self._window.events.loaded += self._loaded
        self._window.events.closing += self._request_close
        self._start_tray()
        self._start_runtime_monitor()

        def watchdog():
            if not self._page_loaded.wait(35):
                from core.desktop_diagnostics import report_error
                report_error(RuntimeError("Das native WebView2-Fenster wurde nicht bereit."),
                             self._diagnostics)
                self._request_close(False)

        self._webview.start(watchdog, gui="edgechromium" if os.name == "nt" else None)

    def _set_fallback(self, mode, reopen):
        self._window = None
        self._ready = False
        self._reopen_callback = reopen
        self._capabilities.update(mode=mode, native_host=False, native_drop=False,
                                  file_dialog=False, folder_dialog=False,
                                  folder_management=False, close_confirmation=False,
                                  open_with_registration=False, taskbar_progress=False)
        self._start_tray()
        self._start_runtime_monitor()

    def _open_folder(self, value):
        # Called only with a completed job's existing output parent by app.py.
        target = Path(value).resolve(strict=True)
        if os.name != "nt" or not target.is_dir():
            raise ValueError("Kein verfügbarer Windows-Ausgabeordner.")
        os.startfile(str(target))

    def _dispose(self):
        self._page_loaded.set()
        self._monitor_stop.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)
        self._sleep_guard.clear()
        self._taskbar.close()
        if self._tray is not None:
            self._tray.stop()


def install_desktop_routes(app, bridge, url):
    """Fallback transport: no privileged filesystem operations are HTTP-exposed."""
    from fastapi import HTTPException, Request

    @app.middleware("http")
    async def desktop_origin_guard(request, call_next):
        # OS-side actions (including the job-scoped Explorer opener) may not be
        # triggered by a cross-origin HTML form. Ordinary browser POST fetches
        # include Origin; non-browser callers must provide it explicitly.
        if (request.url.path.startswith("/api/desktop/")
                and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get("origin") != url):
            from starlette.responses import JSONResponse
            return JSONResponse({"error": "Nur vom lokalen App-Fenster erlaubt."}, status_code=403)
        return await call_next(request)

    @app.get("/api/desktop/capabilities")
    def capabilities():
        return bridge.capabilities()

    # POST + custom header prevents cross-origin <img>/<form> queue draining.
    async def pending(request):
        if (request.headers.get("x-studio-desktop") != "1"
                or request.headers.get("origin", url) != url):
            raise HTTPException(403, "Desktop-Anfrage muss vom lokalen App-Fenster stammen.")
        # Browser transport is independently origin/header-checked above.
        with bridge._lock:
            events = list(bridge._pending)
            bridge._pending.clear()
        return {"events": events, "capabilities": bridge.capabilities()}

    pending.__annotations__["request"] = Request
    app.add_api_route("/api/desktop/pending", pending, methods=["POST"])
