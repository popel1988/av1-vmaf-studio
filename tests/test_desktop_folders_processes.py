"""Explicit native folder grants and console-free Windows subprocesses."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import config, desktop_processes
from core.desktop_folders import DesktopMediaFolders
from core.desktop_host import DesktopBridge


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.default = self.root / "Videos"
        self.default.mkdir()
        self.other = self.root / "Movies"
        self.other.mkdir()
        for name, value in (("MEDIA_ROOTS", [("Videos", self.default)]),
                            ("_SHARE_ROOTS", []), ("MULTI_MEDIA", False)):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folders = DesktopMediaFolders(config, self.root)

    def test_selected_folder_can_be_browsed_and_is_persisted(self):
        video = self.other / "test.mkv"
        video.write_bytes(b"media")
        result = self.folders.add_selected(str(self.other))
        self.assertTrue(config.MULTI_MEDIA)
        self.assertEqual(config.resolve_input(result["rel"] + "/test.mkv"), video.resolve())
        self.assertEqual(json.loads(self.folders.file.read_text(encoding="utf-8")),
                         [str(self.other.resolve())])
        self.assertEqual(len(result["media_roots"]), 2)

    def test_no_new_roots_without_explicit_selection(self):
        self.folders.restore()
        self.assertEqual(config.MEDIA_ROOTS, [("Videos", self.default)])
        self.assertIsNone(config.resolve_input(str(self.other)))

    def test_restores_and_deduplicates_and_preserves_existing_paths(self):
        original = self.default / "source.mkv"
        original.write_bytes(b"media")
        self.folders.add_selected(str(self.other))
        self.folders.add_selected(str(self.other))
        config.MEDIA_ROOTS = [("Videos", self.default)]
        config.MULTI_MEDIA = False
        DesktopMediaFolders(config, self.root).restore()
        self.assertEqual(len(config.MEDIA_ROOTS), 2)
        self.assertEqual(config.resolve_input("source.mkv"), original.resolve())
        self.assertEqual(config.default_output_path().parent, self.default.resolve())

    def test_bad_or_missing_saved_folders_do_not_break_startup(self):
        self.folders.file.write_text('{"not":"a list"}', encoding="utf-8")
        with self.assertLogs("core.desktop_folders", level="ERROR"):
            self.folders.restore()
        self.folders.file.write_text(json.dumps([str(self.root / "missing")]), encoding="utf-8")
        with self.assertLogs("core.desktop_folders", level="WARNING"):
            self.folders.restore()
        self.assertEqual(len(config.MEDIA_ROOTS), 1)

    def test_rejects_files_relative_and_device_paths(self):
        file = self.root / "file.mkv"
        file.write_bytes(b"media")
        for value in (str(file), "relative", "\\\\?\\C:\\", str(self.root / "missing")):
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                self.folders.add_selected(value)
        self.assertFalse(self.folders.file.exists())

    def test_failed_persistence_does_not_authorize_folder(self):
        with patch("core.desktop_folders.tempfile.NamedTemporaryFile", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.folders.add_selected(str(self.other))
        self.assertEqual(len(config.MEDIA_ROOTS), 1)

    def test_bridge_grants_only_native_dialog_result(self):
        bridge = DesktopBridge(None, Mock(), Mock(), self.root, folders=self.folders)
        bridge._window = Mock()
        bridge._webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER="folder"))
        bridge._window.create_file_dialog.return_value = [str(self.other)]
        result = bridge.select_folder()
        bridge._window.create_file_dialog.assert_called_once_with("folder")
        self.assertTrue(result["ok"])
        self.assertEqual(config.resolve_input(result["rel"]), self.other.resolve())
        with self.assertRaises(TypeError):
            bridge.select_folder(str(self.root))  # JS cannot supply an arbitrary path.

    def test_cancel_and_foreign_origin_never_grant(self):
        bridge = DesktopBridge(None, Mock(), Mock(), self.root, folders=self.folders)
        bridge._window = Mock()
        bridge._webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER="folder"))
        bridge._window.create_file_dialog.return_value = None
        self.assertTrue(bridge.select_folder()["cancelled"])
        bridge._origin = ("http", "127.0.0.1:1234")
        bridge._window.get_current_url.return_value = "https://example.org"
        with self.assertRaises(PermissionError):
            bridge.select_folder()
        self.assertEqual(len(config.MEDIA_ROOTS), 1)


class ProcessTests(unittest.TestCase):
    def test_policy_is_desktop_windows_only_idempotent_and_preserves_flags(self):
        class FakePopen:
            def __init__(self, args, bufsize=-1, executable=None, stdin=None, stdout=None,
                         stderr=None, preexec_fn=None, close_fds=True, shell=False,
                         cwd=None, env=None, universal_newlines=None, startupinfo=None,
                         creationflags=0):
                self.flags = creationflags

        with patch.object(subprocess, "Popen", FakePopen), patch.object(
                desktop_processes, "os", SimpleNamespace(name="posix")):
            desktop_processes.install_no_console_processes()
            self.assertIs(subprocess.Popen, FakePopen)
        with patch.object(subprocess, "Popen", FakePopen), patch.object(
                desktop_processes, "os", SimpleNamespace(name="nt")), patch.multiple(
                subprocess, CREATE_NO_WINDOW=0x08000000, CREATE_NEW_CONSOLE=0x10,
                DETACHED_PROCESS=0x08, create=True):
            desktop_processes.install_no_console_processes()
            installed = subprocess.Popen
            desktop_processes.install_no_console_processes()
            self.assertIs(subprocess.Popen, installed)
            self.assertEqual(subprocess.Popen(["tool"]).flags, 0x08000000)
            self.assertEqual(subprocess.Popen(["tool"], creationflags=0x200).flags, 0x08000200)
            for flag in (0x10, 0x08):
                self.assertEqual(subprocess.Popen(["tool"], creationflags=flag).flags, flag)
            positional = [["tool"]] + [-1, None, None, None, None, None, True,
                                       False, None, None, None, None, 0x200]
            self.assertEqual(subprocess.Popen(*positional).flags, 0x08000200)

    @unittest.skipUnless(os.name == "nt", "Windows subprocess policy")
    def test_actual_run_still_captures_stdout(self):
        with patch.object(subprocess, "Popen", subprocess.Popen):
            desktop_processes.install_no_console_processes()
            result = subprocess.run([sys.executable, "-c", "print('hidden console')"],
                                    capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual(result.stdout.strip(), "hidden console")


if __name__ == "__main__":
    unittest.main()
