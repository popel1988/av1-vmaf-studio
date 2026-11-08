"""Fensterloser Desktop-Start darf Uvicorn nicht an fehlendem stdout scheitern lassen."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
