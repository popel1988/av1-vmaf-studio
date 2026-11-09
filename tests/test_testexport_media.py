"""Test media references never expose arbitrary work files."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from core import config


class TestExportMediaTests(unittest.TestCase):
    def test_generated_clip_can_be_played_but_no_other_work_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            clip = base / "test_exports" / ("a" * 32) / "test.mkv"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"fixture")
            (clip.parent / "private.json").write_bytes(b"private")
            with patch.object(config, "WORK_DIR", base):
                ref = "test-export:" + "a" * 32 + "/test.mkv"
                self.assertEqual(config.resolve_input(ref), clip.resolve())
                self.assertEqual(config.rel_input(clip), ref)
                for invalid in (
                    "test-export:../queue.json",
                    "test-export:" + "a" * 32 + "/private.json",
                    "test-export:" + "a" * 32 + "/../test.mkv",
                    "test-export:" + "a" * 32 + "\\test.mkv",
                    "test-export:wrong/test.mkv",
                ):
                    with self.subTest(invalid=invalid):
                        self.assertIsNone(config.resolve_input(invalid))


if __name__ == "__main__":
    unittest.main()
