"""Fensterloser Desktop-Start darf Uvicorn nicht an fehlendem stdout scheitern lassen."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_main


class DesktopStdioTests(unittest.TestCase):
    def test_missing_stdio_gets_stream_with_isatty(self):
        saved_out, saved_err = sys.stdout, sys.stderr
        try:
            sys.stdout = None
            sys.stderr = None
            with tempfile.TemporaryDirectory() as tmp:
                desktop_main._ensure_stdio(Path(tmp))
                self.assertIsNotNone(sys.stdout)
                self.assertIsNotNone(sys.stderr)
                self.assertFalse(sys.stdout.isatty())
                self.assertFalse(sys.stderr.isatty())
                sys.stdout.write("desktop-log\n")
                sys.stdout.flush()
                text = (Path(tmp) / "studio.log").read_text(encoding="utf-8")
                self.assertIn("desktop-log", text)
                for stream in {sys.stdout, sys.stderr}:
                    if stream not in (saved_out, saved_err, None):
                        stream.close()
        finally:
            sys.stdout = saved_out
            sys.stderr = saved_err

    def test_existing_console_streams_stay_untouched(self):
        saved_out, saved_err = sys.stdout, sys.stderr
        try:
            with tempfile.TemporaryDirectory() as tmp:
                desktop_main._ensure_stdio(Path(tmp))
                self.assertIs(sys.stdout, saved_out)
                self.assertIs(sys.stderr, saved_err)
                self.assertFalse((Path(tmp) / "studio.log").exists())
        finally:
            sys.stdout = saved_out
            sys.stderr = saved_err


class DesktopWindowTests(unittest.TestCase):
    def test_frozen_windows_skips_pythonnet_webview(self):
        had_frozen = hasattr(sys, "frozen")
        previous = getattr(sys, "frozen", None)
        sys.frozen = True
        try:
            self.assertFalse(desktop_main._embedded_webview_ok())
        finally:
            if had_frozen:
                sys.frozen = previous
            else:
                del sys.frozen

    def test_frozen_windows_opens_edge_without_importing_webview(self):
        had_frozen = hasattr(sys, "frozen")
        previous = getattr(sys, "frozen", None)
        sys.frozen = True
        calls = []

        def fake_popen(cmd, *args, **kwargs):
            calls.append(cmd)
            class Proc:
                def wait(self):
                    return 0
            return Proc()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(desktop_main, "_find_app_browser", return_value=r"C:\Edge\msedge.exe"), \
                     patch.object(desktop_main.subprocess, "Popen", side_effect=fake_popen), \
                     patch.object(desktop_main.webbrowser, "open") as browser:
                    mode = desktop_main._open_desktop_window("http://127.0.0.1:8080", root)
                self.assertEqual(mode, "browser")
                self.assertEqual(calls[0][0], r"C:\Edge\msedge.exe")
                self.assertIn("--app=http://127.0.0.1:8080", calls[0])
                browser.assert_not_called()
        finally:
            if had_frozen:
                sys.frozen = previous
            else:
                del sys.frozen


if __name__ == "__main__":
    unittest.main()
