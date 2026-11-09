"""Browser runtime assets must work without access to a public CDN."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if tag == "script" and values.get("src"):
            self.assets.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet":
            self.assets.append(values["href"])


class OfflineAssetTests(unittest.TestCase):
    def setUp(self):
        self.parser = AssetParser()
        self.parser.feed((ROOT / "templates" / "index.html").read_text(encoding="utf-8"))

    def test_all_runtime_assets_are_local_and_exist(self):
        self.assertTrue(self.parser.assets)
        for asset in self.parser.assets:
            with self.subTest(asset=asset):
                self.assertTrue(asset.startswith("/static/"), asset)
                path = ROOT / asset.split("?", 1)[0].lstrip("/")
                self.assertTrue(path.is_file(), str(path))
                self.assertGreater(path.stat().st_size, 0)

    def test_local_distributions_and_licenses_are_present(self):
        for name, version in (("chart.umd.min.js", "4.4.1"), ("hls.min.js", "1.5.17")):
            with self.subTest(name=name):
                path = ROOT / "static" / "vendor" / name
                source = path.read_text(encoding="utf-8")
                self.assertGreater(len(source), 100_000)
                self.assertIn(version, source)
                self.assertTrue(any(name in asset for asset in self.parser.assets))
        for name in ("LICENSE.chartjs.txt", "LICENSE.hlsjs.txt", "LICENSE.Apache-2.0.txt"):
            self.assertGreater((ROOT / "static" / "vendor" / name).stat().st_size, 1000)

    def test_template_has_unique_control_ids(self):
        # Template macro bodies have no IDs; duplicate controls break existing listeners.
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)))


if __name__ == "__main__":
    unittest.main()
