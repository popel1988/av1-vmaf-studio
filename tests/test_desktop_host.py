"""Desktop regression tests: never launch UI, mutate registry or encode media."""
from __future__ import annotations

import io
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import desktop_main
from core.desktop_diagnostics import sanitize, writable_directory
from core.desktop_host import DesktopBridge
from core.desktop_ipc import SingleInstance, _receive
from core.desktop_paths import DesktopPathGrants, validate_paths


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "Video ü & test.mkv"
        self.file.write_bytes(b"not actually encoded")

    def test_valid_paths_deduplicated(self):
        self.assertEqual(validate_paths([str(self.file), str(self.file)]), [str(self.file.resolve())])

    def test_rejects_non_media_directory_relative_device_and_missing(self):
        for candidate in (self.temp.name, "relative.mkv", str(self.file) + "missing",
                          "\\\\?\\C:\\video.mkv", "C:\\a.mkv\0", ""):
            with self.subTest(candidate=candidate), self.assertRaises((ValueError, OSError)):
                validate_paths([candidate])
        with self.assertRaises(ValueError):
            validate_paths([str(self.file)] * 257)
        with self.assertRaises(ValueError):
            validate_paths("not-a-list")

    def test_exact_file_grant_never_grants_siblings_or_parent(self):
        grants = DesktopPathGrants()
        sibling = self.file.with_name("sibling.mkv")
        sibling.write_bytes(b"media")
        grants.grant([str(self.file)])
        self.assertEqual(grants.resolve(str(self.file)), self.file.resolve())
        self.assertIsNone(grants.resolve(str(sibling)))
        self.assertIsNone(grants.resolve(self.temp.name))

    def test_validation_atomic_no_partial_grant(self):
        grants = DesktopPathGrants()
        with self.assertRaises((ValueError, OSError)):
            grants.grant([str(self.file), str(self.file) + ".missing"])
        self.assertIsNone(grants.resolve(str(self.file)))

    def test_config_wrapper_preserves_existing_roots(self):
        configured = Path(self.temp.name) / "configured"
        config = SimpleNamespace(resolve_input=lambda path: configured if path == "valid" else None)
        grants = DesktopPathGrants()
        grants.install(config)
        self.assertEqual(config.resolve_input("valid"), configured)
        self.assertIsNone(config.resolve_input(str(self.file)))
        grants.grant([str(self.file)])
        self.assertEqual(config.resolve_input(str(self.file)), self.file.resolve())


class IPCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.instance = SingleInstance(Path(self.temp.name))
        self.assertTrue(self.instance.acquire())
        self.addCleanup(self.instance.close)

    def test_single_instance_and_forward(self):
        second = SingleInstance(Path(self.temp.name))
        self.assertFalse(second.acquire())
        received = []
        self.instance.listen(received.append)
        second.forward(["one"])
        self.assertEqual(received, [["one"]])
        second.close()  # Must not delete the real instance's endpoint.
        self.assertTrue(self.instance.endpoint.exists())

    def test_bad_token_rejected_before_callback(self):
        callback = Mock()
        self.instance.listen(callback)
        endpoint = json.loads(self.instance.endpoint.read_text())
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=2) as conn:
            conn.sendall(b'{"token":"bad","action":"open","paths":[]}\n')
            self.assertFalse(_receive(conn)["ok"])
        callback.assert_not_called()

    def test_invalid_paths_not_acknowledged(self):
        self.instance.listen(validate_paths)
        with self.assertRaises(RuntimeError):
            SingleInstance(Path(self.temp.name)).forward(["relative.mkv"])

    def test_primary_close_removes_endpoint_and_releases_lock(self):
        self.instance.listen(Mock())
        self.instance.close()
        self.assertFalse(self.instance.endpoint.exists())
        second = SingleInstance(Path(self.temp.name))
        self.assertTrue(second.acquire())
        second.close()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.queue = Mock()
        self.queue.state.return_value = {"active_ids": []}
        self.stop = Mock()
        self.bridge = DesktopBridge(DesktopPathGrants(), self.queue, self.stop, Path("."))

    def test_ipc_pending_event_has_id_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "film.mkv"
            file.write_bytes(b"")
            self.bridge._accept_paths([str(file)], "ipc")
            pending = self.bridge.take_pending()
        self.assertEqual(pending[0]["source"], "ipc")
        self.assertTrue(pending[0]["id"])
        self.assertEqual(self.bridge.take_pending(), [])

    def test_javascript_string_injection_escaped(self):
        self.bridge._window = Mock()
        self.bridge._dispatch("studio:open-paths", {"paths": ['");alert(1);//\n']})
        script = self.bridge._window.evaluate_js.call_args.args[0]
        self.assertIn('\\"', script)
        self.assertIn("\\n", script)
        self.assertNotIn('["");alert', script)

    def test_other_origin_cannot_use_bridge_or_receive_path_events(self):
        from urllib.parse import urlsplit
        self.bridge._origin = urlsplit("http://127.0.0.1:1234")[:2]
        self.bridge._window = Mock()
        self.bridge._window.get_current_url.return_value = "https://example.org"
        with self.assertRaises(PermissionError):
            self.bridge.take_pending()
        with self.assertLogs("core.desktop_host", level="ERROR"):
            self.bridge._dispatch("studio:open-paths", {"paths": ["private"]})
        self.bridge._window.evaluate_js.assert_not_called()

    def test_native_attempt_is_not_disabled_in_frozen_executable(self):
        with patch.object(sys, "frozen", True, create=True), tempfile.TemporaryDirectory() as tmp:
            bridge = Mock()
            mode = desktop_main._open_desktop_window("http://127.0.0.1:1234", Path(tmp), bridge)
        self.assertEqual(mode, "webview")
        bridge._open_native.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Native WebView2 additional objects")
    def test_drop_ignores_javascript_forged_path_and_uses_native_files(self):
        class Event:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class CoreWebView2File:
            Path = r"C:\Videos\native.mkv"

        events = SimpleNamespace(dragenter=Event(), dragover=Event(), drop=Event())
        native = Event()
        window = Mock()
        window.dom.document.events = events
        window.native.browser.webview.WebMessageReceived = native
        self.bridge._window = window
        fake_dom = SimpleNamespace(DOMEventHandler=lambda callback, *args, **kwargs: callback)
        with patch.dict(sys.modules, {"webview.dom": fake_dom}):
            self.bridge._loaded()
        delivered = threading.Event()
        accept = Mock(side_effect=lambda paths, source: delivered.set())
        self.bridge._accept_paths = accept
        events.drop.handlers[0]({
            "dataTransfer": {"files": [{"pywebviewFullPath": r"C:\private\forged.mkv"}]}})
        accept.assert_not_called()
        args = Mock()
        args.get_WebMessageAsJson.return_value = '"FilesDropped"'
        args.get_AdditionalObjects.return_value = [CoreWebView2File()]
        native.handlers[0](None, args)
        self.assertTrue(delivered.wait(2))
        accept.assert_called_once_with([r"C:\Videos\native.mkv"], "drop")

    @unittest.skipUnless(os.name == "nt", "Windows native close choices")
    def test_close_cancel_does_not_stop_jobs(self):
        self.queue.state.return_value = {"active_ids": ["running"]}
        with patch("ctypes.windll.user32.MessageBoxW", return_value=7):
            self.assertFalse(self.bridge._request_close())
        self.stop.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows native close choices")
    def test_background_requires_tray_and_hides_window(self):
        self.queue.state.return_value = {"active_ids": ["running"]}
        self.bridge._tray = Mock()
        self.bridge._window = Mock()
        with patch("ctypes.windll.user32.MessageBoxW", return_value=7):
            self.assertFalse(self.bridge._request_close())
        self.bridge._window.hide.assert_called_once()
        self.stop.assert_not_called()

    def test_shutdown_waits_before_destroying_window(self):
        release = threading.Event()
        started = threading.Event()
        self.bridge._window = Mock()

        def stop():
            started.set()
            release.wait(3)

        self.bridge._stop_server = stop
        self.bridge._request_close()
        self.assertTrue(started.wait(2))
        self.bridge._window.destroy.assert_not_called()
        # Bounded event synchronization, no UI loop required.
        finished = threading.Event()
        self.bridge._window.destroy.side_effect = finished.set
        release.set()
        self.assertTrue(finished.wait(3))
        self.assertTrue(self.bridge._allow_close)

    def test_fallback_advertises_no_native_drop_or_dialog(self):
        self.bridge._set_fallback("edge", Mock())
        capabilities = self.bridge.capabilities()
        self.assertFalse(capabilities["native_drop"])
        self.assertFalse(capabilities["file_dialog"])
        self.assertFalse(capabilities["native_host"])
        self.assertEqual(capabilities["mode"], "edge")


class BootstrapTests(unittest.TestCase):
    def test_logs_fallback_from_unwritable_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "file"
            blocked.write_text("not a directory")
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                result = writable_directory(blocked)
            self.assertEqual(result, Path(tmp) / "AV1-VMAF-Studio")

    def test_diagnostics_redact_home_and_credentials(self):
        with patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\Private"}):
            text = sanitize("C:\\Users\\Private\\video.mkv token=abc password=secret "
                            "https://bob:secret@example.org/a")
        self.assertNotIn("Private", text)
        self.assertNotIn("abc", text)
        self.assertNotIn("bob:", text)
        self.assertNotIn("secret ", text)

    def test_failed_server_thread_does_not_wait_full_timeout(self):
        thread = Mock()
        thread.is_alive.return_value = False
        self.assertFalse(desktop_main._wait_for_server("http://127.0.0.1:1", thread=thread))

    def test_non_ready_server_does_not_probe_unrelated_port(self):
        with patch("desktop_main.urllib.request.build_opener") as opener:
            self.assertFalse(desktop_main._wait_for_server(
                "http://127.0.0.1:1", timeout=0.01,
                server=SimpleNamespace(started=False)))
        opener.return_value.open.assert_not_called()

    def test_setup_does_not_add_drives_to_media_roots(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with patch.object(desktop_main, "_app_root", return_value=Path(tmp)):
                desktop_main._setup_environment()
                self.assertNotIn("MEDIA_DIRS", os.environ)

    def test_smoke_flag_does_not_start_server_or_single_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("core.desktop_diagnostics.writable_directory", return_value=Path(tmp)), \
                 patch("core.desktop_diagnostics.install_hooks"), \
                 patch.object(desktop_main, "_smoke_test", return_value={"ok": True}), \
                 patch("core.desktop_ipc.SingleInstance") as instance, \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(desktop_main.main(["--desktop-smoke-test"]), 0)
                instance.assert_not_called()
            self.assertEqual(json.loads((Path(tmp) / "desktop-smoke.json").read_text()), {"ok": True})

    def test_smoke_failure_is_noninteractive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("core.desktop_diagnostics.writable_directory", return_value=Path(tmp)), \
                 patch("core.desktop_diagnostics.install_hooks"), \
                 patch("core.desktop_diagnostics.report_error") as report, \
                 patch.object(desktop_main, "_smoke_test", side_effect=ImportError("no runtime")):
                self.assertEqual(desktop_main.main(["--desktop-smoke-test"]), 1)
                self.assertFalse(report.call_args.kwargs["interactive"])
            self.assertFalse(json.loads((Path(tmp) / "desktop-smoke.json").read_text())["ok"])


class DesktopRouteTests(unittest.TestCase):
    def setUp(self):
        from core.desktop_host import install_desktop_routes
        self.routes = {}
        app = SimpleNamespace(
            middleware=lambda kind: lambda function: self.routes.setdefault("middleware", function),
            get=lambda path: lambda function: self.routes.setdefault(path, function),
            add_api_route=lambda path, function, **kwargs: self.routes.setdefault(path, function))

        class HTTPException(Exception):
            pass

        self.error = HTTPException
        self.bridge = DesktopBridge(DesktopPathGrants(), Mock(), Mock(), Path("."))
        fake_fastapi = SimpleNamespace(HTTPException=HTTPException, Request=object)
        with patch.dict(sys.modules, {"fastapi": fake_fastapi}):
            install_desktop_routes(app, self.bridge, "http://127.0.0.1:1234")

    def test_capabilities_endpoint(self):
        self.assertTrue(self.routes["/api/desktop/capabilities"]()["single_instance"])

    def test_pending_requires_header_and_same_origin(self):
        for headers in ({}, {"x-studio-desktop": "1", "origin": "https://evil.example"}):
            with self.subTest(headers=headers), self.assertRaises(self.error):
                asyncio.run(self.routes["/api/desktop/pending"](SimpleNamespace(headers=headers)))

    def test_pending_consumes_only_after_origin_check(self):
        self.bridge._pending.append({"id": "event", "paths": [], "source": "ipc"})
        result = asyncio.run(self.routes["/api/desktop/pending"](SimpleNamespace(
            headers={"x-studio-desktop": "1", "origin": "http://127.0.0.1:1234"})))
        self.assertEqual(result["events"][0]["id"], "event")
        self.assertEqual(list(self.bridge._pending), [])


class AssociationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows command syntax")
    def test_registration_only_writes_current_user_and_openwith_not_defaults(self):
        from core.desktop_associations import EXTENSIONS, PROG_ID, register_open_with
        registry = Mock()
        registry.HKEY_CURRENT_USER = object()
        registry.CreateKey.return_value.__enter__ = Mock(return_value="key")
        registry.CreateKey.return_value.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, {"winreg": registry}):
            result = register_open_with()
        self.assertTrue(result["ok"])
        self.assertFalse(result["default_changed"])
        paths = []
        for call in registry.CreateKey.call_args_list:
            self.assertIs(call.args[0], registry.HKEY_CURRENT_USER)
            paths.append(call.args[1])
        for extension in EXTENSIONS:
            self.assertIn(rf"Software\Classes\{extension}\OpenWithProgids", paths)
            self.assertNotIn(rf"Software\Classes\{extension}", paths)
        self.assertIn(rf"Software\Classes\{PROG_ID}\shell\open\command", paths)
        self.assertTrue(all("UserChoice" not in path for path in paths))

    def test_openwith_command_quotes_file_placeholder(self):
        from core.desktop_associations import registration_command
        self.assertTrue(registration_command().endswith(' "%1"'))


if __name__ == "__main__":
    unittest.main()
