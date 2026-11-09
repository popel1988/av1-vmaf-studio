"""Regressionen für Windows-Laufwerke und Live-Daten ohne native Fenster."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as server
from core import config


class WindowsBrowseTests(unittest.TestCase):
    def test_unreadable_entry_does_not_break_readable_drive(self):
        class UnreadablePath(type(Path())):
            def is_dir(self):
                raise PermissionError("System Volume Information")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Videos"
            folder.mkdir()
            video = root / "film.mkv"
            video.write_bytes(b"video")
            blocked = UnreadablePath(root / "System Volume Information")
            blocked.mkdir()
            with patch.object(config, "MEDIA_ROOTS", [("drive", root)]), patch.object(
                    config, "_SHARE_ROOTS", []), patch.object(
                    config, "MULTI_MEDIA", False), patch.object(
                    type(root), "iterdir", return_value=iter([blocked, folder, video])), patch(
                    "core.bluray.describe", return_value=None), patch(
                    "core.dvd.describe", return_value=None):
                result = asyncio.run(server.browse_input("", "video"))
            self.assertEqual(result["skipped_entries"], 1)
            self.assertEqual([d["name"] for d in result["dirs"]], ["Videos"])
            self.assertEqual([f["name"] for f in result["files"]], ["film.mkv"])

    def test_multiple_roots_can_be_entered_and_navigated_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = [(name, Path(tmp) / name) for name in ("C", "D")]
            for _, root in roots:
                (root / "Videos").mkdir(parents=True)
            with patch.object(config, "MEDIA_ROOTS", roots), patch.object(
                    config, "_SHARE_ROOTS", []), patch.object(config, "MULTI_MEDIA", True):
                overview = asyncio.run(server.browse_input("", "video"))
                drive = asyncio.run(server.browse_input(overview["dirs"][0]["rel"], "video"))
                folder = asyncio.run(server.browse_input(drive["dirs"][0]["rel"], "video"))
            self.assertEqual(drive["parent"], "")
            self.assertEqual(folder["parent"], "C")
            self.assertEqual(folder["path"], "C/Videos")

    def test_live_transport_failure_is_logged(self):
        websocket = Mock()
        from unittest.mock import AsyncMock
        websocket.accept = AsyncMock()
        websocket.close = AsyncMock()
        with patch.object(config, "APP_PASSWORD", ""), patch.object(
                server.monitor, "snapshot", side_effect=RuntimeError("sensor unavailable")):
            with self.assertLogs("vcompress.websocket", level="ERROR") as logs:
                asyncio.run(server.ws(websocket))
        self.assertIn("sensor unavailable", "\n".join(logs.output))
        websocket.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
