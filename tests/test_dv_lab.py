"""Unit-Tests für das HDR / Dolby Vision Labor (Analyzer, 0-Disk-NALU-Scanner, Hybrid-Sync & RPU-Tresor)."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import config, dv_lab, export_preflight, ffmpeg_utils as ff, queue_manager as qm, rpu_vault

try:
    import quickjs
except ImportError:
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]


def _make_hevc_nalu(nal_type: int, layer_id: int = 0, payload_size: int = 100, first_slice: bool = True) -> bytes:
    """Baut eine synthetische HEVC-Annex-B-NAL-Unit mit 4-Byte-Startcode."""
    b0 = ((nal_type & 0x3F) << 1) | ((layer_id >> 5) & 0x01)
    b1 = ((layer_id & 0x1F) << 3) | 1  # temporal_id_plus1 = 1
    b2 = 0x80 if first_slice else 0x00
    body = bytes([b2]) + (b"\x55" * max(0, payload_size - 1))
    return b"\x00\x00\x00\x01" + bytes([b0, b1]) + body


class DvLabTests(unittest.TestCase):
    def test_zero_disk_hevc_nalu_layer_scanner_splits_bl_el_and_rpu(self):
        # 24 Frames @ 24 fps = 1.0 Sekunde
        stream_parts = []
        expected_bl = 0
        expected_el = 0
        expected_rpu = 0
        for _ in range(24):
            aud = _make_hevc_nalu(35, layer_id=0, payload_size=2)
            bl = _make_hevc_nalu(1, layer_id=0, payload_size=5000, first_slice=True)
            el = _make_hevc_nalu(63, layer_id=0, payload_size=1000)
            rpu = _make_hevc_nalu(62, layer_id=0, payload_size=250)
            stream_parts.extend([aud, bl, el, rpu])
            expected_bl += len(aud) + len(bl)
            expected_el += len(el)
            expected_rpu += len(rpu)

        raw = b"".join(stream_parts)
        # In kleinen, unregelmäßigen Chunks einspeisen (testet Startcode-Grenzen über Chunk-Kanten)
        chunks = [raw[i:i + 777] for i in range(0, len(raw), 777)]
        rpu_sink = io.BytesIO()
        stats = dv_lab.parse_hevc_annexb_layers_stream(
            chunks, fps=24.0, total_duration=1.0, rpu_sink_file=rpu_sink
        )

        self.assertEqual(stats["frames_counted"], 24)
        self.assertEqual(stats["bl_size_bytes"], expected_bl)
        self.assertEqual(stats["el_size_bytes"], expected_el)
        self.assertEqual(stats["rpu_size_bytes"], expected_rpu)
        self.assertEqual(len(rpu_sink.getvalue()), expected_rpu)
        self.assertGreater(stats["el_share_pct"], 15.0)
        self.assertLess(stats["el_share_pct"], 18.0)
        self.assertGreater(stats["bl_avg_mbps"], 0.9)
        self.assertGreater(stats["rpu_avg_kbps"], 40.0)

    def test_dovi_summary_and_exported_rpu_enrichment(self):
        summary = """
        Summary:
          Frames: 143856
          Profile: 7 (FEL)
          DM version: 2 (CM v4.0)
          RPU mastering display: 0.0050/4000 nits
          RPU content light level (L6): MaxCLL: 1555.18 nits, MaxFALL: 159.01 nits
          Scene cuts: 1420
        """
        parsed = rpu_vault.parse_dovi_summary_text(summary)
        self.assertEqual(parsed["frames"], 143856)
        self.assertEqual(parsed["profile"], 7)
        self.assertEqual(parsed["el_type"], "FEL")
        self.assertEqual(parsed["cm_version"], "CM v4.0")
        self.assertEqual(parsed["rpu_mastering_display"], "0.0050/4000 nits")
        self.assertAlmostEqual(parsed["max_cll_nits"], 1555.18, places=2)
        self.assertAlmostEqual(parsed["max_fall_nits"], 159.01, places=2)

        raw_json = [
            {
                "header": {"guessed_profile": 8, "el_type": "MEL"},
                "vdr_dm_data": {
                    "scene_refresh_flag": 1,
                    "cmv40_metadata": {
                        "ext_metadata_blocks": [
                            {"Level1": {"max_pq": 3079, "avg_pq": 1800, "min_pq": 20}},
                            {
                                "Level5": {
                                    "active_area_left_offset": 0,
                                    "active_area_right_offset": 0,
                                    "active_area_top_offset": 276,
                                    "active_area_bottom_offset": 276,
                                }
                            },
                        ]
                    },
                },
            }
        ]
        info: dict = {}
        rpu_vault._enrich_from_exported_rpu(raw_json, info)
        self.assertEqual(info["frames"], 1)
        self.assertEqual(info["cm_version"], "CM v4.0")
        self.assertGreater(info["peak_l1_nits"], 900.0)
        self.assertEqual(info["l5_offsets"]["top"], 276)
        self.assertEqual(info["scene_cut_frames"], [0])

    def test_scene_cut_offset_correlation_and_rpu_editor_config(self):
        donor_cuts = [0, 120, 295, 480, 710, 990, 1240]
        # Ziel-Release hat ein um +48 Frames längeres Studio-Intro (+/- 1 Frame Jitter)
        target_cuts = [0, 48, 168, 344, 528, 757, 1038, 1288]
        offset, conf = dv_lab.correlate_scene_cut_offset(donor_cuts, target_cuts)
        self.assertIn(offset, (47, 48, 49))
        self.assertGreaterEqual(conf, 0.7)

        # Spender hat 36 Intro-Frames zu viel (-36) + Ziel wurde gecroppt (L5 = 0)
        cfg_trim = dv_lab.build_rpu_editor_config(
            donor_frames=10036,
            target_frames=10000,
            frame_offset=-36,
            l5_offsets={"left": 0, "right": 0, "top": 0, "bottom": 0},
        )
        self.assertEqual(cfg_trim["remove"], ["0-35"])
        self.assertEqual(cfg_trim["active_area"], {"crop": True})

        # Ziel hat +48 Intro-Frames mehr + Letterbox 276px oben/unten
        cfg_pad = dv_lab.build_rpu_editor_config(
            donor_frames=10000,
            target_frames=10048,
            frame_offset=48,
            l5_offsets={"left": 0, "right": 0, "top": 276, "bottom": 276},
        )
        self.assertEqual(cfg_pad["duplicate"][0], {"source": 0, "offset": 0, "length": 48})
        self.assertFalse(cfg_pad["active_area"]["crop"])
        self.assertEqual(cfg_pad["active_area"]["presets"][0]["top"], 276)

    def test_five_format_report_export_integrates_dv_block(self):
        sample_analysis = {
            "filename": "Movie.2160p.UHD.mkv",
            "dolby_vision": True,
            "hdr_format": "Dolby Vision 7 + HDR10",
            "profile": 7,
            "profile_label": "7",
            "level": 6,
            "cm_version": "CM v2.9",
            "base_layer": True,
            "base_layer_type": "HDR10",
            "enhancement_layer": True,
            "enhancement_layer_type": "FEL",
            "rpu": True,
            "hdr10_plus": True,
            "video_codec": "HEVC Main 10",
            "resolution": "3840 x 2160",
            "rpu_mastering_display": "0.0050/4000 nits",
            "rpu_content_light_level": "MaxCLL: 1555.18 nits, MaxFALL: 159.01 nits",
            "mediainfo_text": "General\nComplete name : Movie.2160p.UHD.mkv\n\nVideo\nFormat : HEVC",
            "bitrate_scan": {
                "bl_size_human": "53.54 GB",
                "bl_avg_mbps": 66.07,
                "bl_max_mbps": 117.46,
                "el_size_bytes": 7043746365,
                "el_size_human": "6.56 GB",
                "el_avg_mbps": 8.09,
                "el_max_mbps": 25.02,
                "el_share_pct": 10.91,
                "rpu_size_human": "63.38 MB",
                "rpu_avg_kbps": 76.39,
                "timestamps": [0, 1, 2],
                "bl_mbps_series": [60.1, 68.4, 69.7],
                "el_mbps_series": [7.5, 8.2, 8.6],
                "rpu_kbps_series": [75.0, 76.5, 77.6],
            },
        }
        for fmt in ("txt", "md", "json", "csv", "html"):
            content, mime, fname = dv_lab.export_dv_report(sample_analysis, fmt)
            self.assertTrue(content)
            self.assertTrue(mime)
            self.assertTrue(fname.endswith(f".{fmt}"))
            if fmt == "txt":
                self.assertIn("Enhancement Layer Type                   : FEL", content)
                self.assertIn("EL Share                                 : 10.91 %", content)
                self.assertIn("Video\nFormat : HEVC", content)
            elif fmt == "csv":
                self.assertIn("second,bl_mbps,el_mbps,rpu_kbps", content)

    def test_rpu_vault_index_search_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_vault = Path(tmp)
            with mock.patch.object(config, "RPU_VAULT_DIR", tmp_vault):
                rpu_file = tmp_vault / "dune_abc123.rpu.bin"
                rpu_file.write_bytes(b"RPU_DATA_TEST")
                rpu_vault._save_index([
                    {
                        "id": "abc123",
                        "title": "Dune Part Two",
                        "source_name": "Dune2.HDR10Plus.mkv",
                        "filename": rpu_file.name,
                        "origin": "hdr10plus_converted",
                        "origin_label": "Generiert aus HDR10+",
                        "cm_version": "CM v2.9",
                        "created_at": 100.0,
                    }
                ])
                all_items = rpu_vault.list_entries()
                self.assertEqual(len(all_items), 1)
                self.assertTrue(all_items[0]["exists"])
                self.assertEqual(len(rpu_vault.list_entries("hdr10plus")), 1)
                self.assertEqual(len(rpu_vault.list_entries("nonexistent")), 0)
                entry = rpu_vault.get_entry("abc123")
                self.assertIsNotNone(entry)
                self.assertTrue(rpu_vault.delete_entry("abc123"))
                self.assertFalse(rpu_file.exists())
                self.assertEqual(rpu_vault.list_entries(), [])

    def test_preflight_warns_on_p7_el_drop_and_non_hevc_dual_hdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"00")
            info = ff.VideoInfo(
                str(src), 3840, 2160, 120.0, "hevc", "yuv420p10le",
                "smpte2084", "bt2020", "bt2020nc", 50_000_000, 1024,
                fps=24.0, hdr_type="Dolby Vision 7 + HDR10",
                dolby_vision=True, dv_profile=7, hdr10_plus=False,
            )
            s = qm.JobSettings(
                platform="nvidia", codec="hevc", preserve_hdr=True,
                preserve_dv=True, generate_dv_from_hdr10plus=True,
                out_mode="beside", vmaf_check=False,
            )
            res = export_preflight.inspect(src, s, info=info)
            codes = {w["code"] for w in res.get("warnings", [])}
            self.assertIn("dv_p7_el_dropped", codes)

            s_av1 = qm.JobSettings(
                platform="cpu", codec="av1", preserve_hdr=True,
                generate_dv_from_hdr10plus=True, out_mode="beside", vmaf_check=False,
            )
            res_av1 = export_preflight.inspect(src, s_av1, info=info)
            codes_av1 = {w["code"] for w in res_av1.get("warnings", [])}
            self.assertIn("hdr10plus_to_dv_hevc_only", codes_av1)

    @unittest.skipIf(quickjs is None, "quickjs nicht installiert")
    def test_dv_lab_js_syntax_valid(self):
        ctx = quickjs.Context()
        for js_name in ("dv_lab.js", "app.js", "i18n.js"):
            source = (ROOT / "static" / "js" / js_name).read_text(encoding="utf-8")
            self.assertTrue(ctx.eval("new Function(" + json.dumps(source) + "); true;"), js_name)


if __name__ == "__main__":
    unittest.main()
