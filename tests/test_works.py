"""Erkennung von Filmen, Folgen, Sendungen und eigenen Sammlungen."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import works


class WorkTests(unittest.TestCase):
    def test_filename_alone_suggests_two_editions_without_merging(self):
        a = works.describe_work("Amelie (2001) - 1080p.mkv", "Filme/Amelie (2001)")
        b = works.describe_work("Amelie (2001) - 2160p.mkv", "Filme/Amelie (2001)")
        self.assertEqual(a["kind"], "film")
        self.assertNotEqual(a["group"], b["group"])
        self.assertEqual(a["title"], "Amelie")
        self.assertEqual(a["year"], 2001)
        self.assertEqual(a["edition"], "1080p")
        self.assertEqual(b["edition"], "2160p")

    def test_episodes_share_the_show_but_not_the_episode(self):
        one = works.describe_work("Kommissar (2019) S01E02.mkv", "Serien/Kommissar/Staffel 01")
        two = works.describe_work("Kommissar (2019) S01E03.mkv", "Serien/Kommissar/Staffel 01")
        self.assertEqual(one["kind"], "serie")
        self.assertEqual(one["episode"], "S01E02")
        self.assertNotEqual(one["group"], two["group"])
        self.assertEqual(one["title"], two["title"])

    def test_mediathek_date_groups_a_broadcast(self):
        item = works.describe_work("Die Sendung mit der Maus - 2024-11-10.mp4", "Mediathek")
        self.assertEqual(item["kind"], "sendung")
        self.assertEqual(item["title"], "Die Sendung mit der Maus")
        self.assertIn("2024-11-10", item["group"])

    def test_documentary_genre_is_labeled_without_a_separate_key_rule(self):
        item = works.describe_work(
            "Island (2020).mkv", "Dokus/Island (2020)",
            {"kind": "movie", "title": "Island", "year": "2020", "genres": ["Dokumentation"]},
        )
        self.assertEqual(item["kind"], "doku")
        self.assertTrue(item["group"].startswith("datei:"))

    def test_trailer_stays_beside_the_film(self):
        film = works.describe_work("Amelie (2001).mkv", "Filme/Amelie (2001)")
        trailer = works.describe_work("Amelie (2001) - trailer.mkv", "Filme/Amelie (2001)")
        self.assertNotEqual(film["group"], trailer["group"])
        self.assertTrue(trailer["extra"])
        self.assertNotEqual(film["key"], trailer["key"])

    def test_catalog_assigns_and_removes_recordings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library_catalog.json"
            with mock.patch.object(works, "_CATALOG_PATH", path):
                made = works.create_collection("Urlaub Island", "sammlung")
                works.assign_paths(["Aufnahmen/klippe.mp4", "Aufnahmen/hafen.mp4"], made["id"])
                catalog = works.list_catalog()
                self.assertEqual(catalog["assignments"]["Aufnahmen/klippe.mp4"]["collection_id"], made["id"])
                self.assertTrue(works.delete_collection(made["id"]))
                self.assertEqual(works.list_catalog()["assignments"], {})
                with self.assertRaises(ValueError):
                    works.create_collection("   ")


if __name__ == "__main__":
    unittest.main()
