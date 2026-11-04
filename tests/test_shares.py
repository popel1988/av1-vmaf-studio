"""SMB-Freigaben: Prüfung, Credential-Datei und Eintrag als Medienwurzel."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import config
from core import shares


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._data = config.DATA_DIR
        self._share_roots = list(config._SHARE_ROOTS)
        config.DATA_DIR = self.root
        config.set_share_roots([])

    def tearDown(self):
        config.DATA_DIR = self._data
        config.set_share_roots(self._share_roots)
        self.tmp.cleanup()

    def test_invalid_server_is_rejected_before_a_mount(self):
        with patch.object(shares, "_activate") as activate:
            public, err = shares.add({
                "name": "Filme", "host": "bad host", "share": "video",
                "username": "daniel", "password": "geheim",
            })
        activate.assert_not_called()
        self.assertFalse(public)
        self.assertIn("Server", err)
        self.assertFalse((self.root / "shares.json").exists())

    def test_saved_share_hides_the_password_and_becomes_a_root(self):
        nas = self.root / "mounted"
        nas.mkdir()
        (nas / "a.mkv").write_bytes(b"x")
        with patch.object(shares, "_activate", return_value=(nas, "")):
            public, err = shares.add({
                "name": "Filme", "host": "nas.local", "share": "video",
                "username": "daniel", "password": "geheim", "readonly": True,
            })
        self.assertEqual(err, "")
        self.assertNotIn("password", public)
        self.assertTrue(public["password_set"])
        self.assertTrue(public["mounted"])
        saved = json.loads((self.root / "shares.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["shares"][0]["password"], "geheim")
        self.assertEqual(
            config.resolve_input(public["root"] + "/a.mkv"),
            (nas / "a.mkv").resolve())
        spec = saved["shares"][0]
        options = shares.linux_options(spec, Path("/data/share-mounts/x.cred"))
        self.assertIn("credentials=" + str(Path("/data/share-mounts/x.cred")), options)
        self.assertIn("ro", options)
        self.assertNotIn("geheim", ",".join(options))
        command = shares.linux_mount_command("//nas.local/video", nas, options)
        self.assertEqual(command[:3], ["mount", "-t", "cifs"])
        self.assertNotIn("geheim", " ".join(command))
        primary = config.MEDIA_ROOTS[0][1]
        self.assertEqual(
            config.resolve_input("output/clip.mkv"),
            (primary / "output" / "clip.mkv").resolve())
        with patch.object(shares, "_detach"):
            self.assertEqual(shares.remove(public["id"]), "")
        self.assertNotIn(public["root"], {name for name, _ in config.all_media_roots()})

    def test_guest_needs_no_user_and_a_duplicate_is_refused(self):
        nas = self.root / "guest"
        nas.mkdir()
        payload = {"name": "Offen", "host": "nas.local", "share": "public", "guest": True}
        with patch.object(shares, "_activate", return_value=(nas, "")):
            public, err = shares.add(payload)
            again, duplicate = shares.add(payload)
        self.assertEqual(err, "")
        self.assertFalse(public["password_set"])
        self.assertTrue(public["guest"])
        self.assertFalse(again)
        self.assertIn("schon", duplicate)
        options = shares.linux_options({"guest": True, "readonly": False}, None)
        self.assertIn("guest", options)
        self.assertNotIn("ro", options)
        self.assertFalse(any(item.startswith("credentials=") for item in options))

    def test_smb_url_is_only_the_server(self):
        spec, err = shares.normalize({
            "name": "NAS", "host": "smb://192.168.168.200/", "share": "video",
            "username": "daniel", "password": "geheim",
        })
        self.assertEqual(err, "")
        self.assertEqual(spec["host"], "192.168.168.200")
        self.assertEqual(spec["share"], "video")
        self.assertEqual(spec["subpath"], "")

    def test_smb_url_can_carry_share_and_folder(self):
        spec, err = shares.normalize({
            "name": "NAS", "host": r"\\192.168.168.200\video\Filme",
            "username": "daniel", "password": "geheim",
        })
        self.assertEqual(err, "")
        self.assertEqual(spec["host"], "192.168.168.200")
        self.assertEqual(spec["share"], "video")
        self.assertEqual(spec["subpath"], "Filme")
        filled, err = shares.normalize({
            "name": "NAS", "host": "smb://nas.local:445/anders/weg",
            "share": "video", "username": "daniel", "password": "x",
        })
        self.assertEqual(err, "")
        self.assertEqual((filled["host"], filled["share"], filled["subpath"]), ("nas.local", "video", ""))

    def test_mount_errors_stay_short(self):
        self.assertEqual(shares._explain_mount("mount error(13): Permission denied"), "Anmeldung abgelehnt")
        self.assertEqual(shares._explain_mount("No route to host"), "Server nicht erreichbar")


if __name__ == "__main__":
    unittest.main()
