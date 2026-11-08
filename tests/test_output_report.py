"""Observational report contracts; no FFmpeg dependency for unit tests."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import output_report as report
from core.encoder import command_details


SOURCE = {
    "path": "source.mkv", "codec": "hevc", "width": 1920, "height": 1080,
    "duration": 100.0, "bit_depth": 10, "color_transfer": "smpte2084",
    "color_primaries": "bt2020", "color_space": "bt2020nc", "color_range": "tv",
    "dolby_vision": False, "hdr10_plus": True,
    "audio": [{"index": 0, "codec": "dts", "language": "deu", "title": "German",
               "default": True, "forced": False, "channels": 6}],
    "subtitles": [{"index": 0, "codec": "subrip", "language": "eng", "title": "English",
                   "default": False, "forced": True}],
}


def checks(result):
    return {check["key"]: check for check in result["checks"]}


class OutputReportTests(unittest.TestCase):
    def command(self, *args):
        return command_details(["ffmpeg", "-i", "source.mkv", *args, "out.mkv"], "out.mkv")

    def test_copy_plan_preserves_explicit_tracks_and_chapters(self):
        command = self.command("-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-c", "copy")
        chapters = [{"start": 0, "end": 100, "title": "One"}]
        plan = report.build_plan(SOURCE, {}, command, chapters=chapters)
        self.assertEqual(plan["video"]["codec"], "hevc")
        self.assertEqual(plan["video"]["resolution"], [1920, 1080])
        self.assertEqual(plan["audio"][0]["codec"], "dts")
        self.assertEqual(plan["subtitles"][0]["forced"], True)
        self.assertEqual(plan["chapters"], chapters)

    def test_encode_plan_records_transcode_metadata_channels_and_dispositions(self):
        command = self.command("-map", "0:v:0", "-map", "0:a:0", "-c:v", "libsvtav1",
                               "-c:a", "libopus", "-ac:a:0", "2", "-disposition:a:0", "0",
                               "-metadata:s:a:0", "language=spa", "-metadata:s:a:0", "title=Spanish",
                               "-vf", "crop=1920:800:0:140,scale=-2:400", "-pix_fmt", "yuv420p10le",
                               "-map_chapters", "-1")
        plan = report.build_plan(SOURCE, {}, command)
        self.assertEqual(plan["video"]["codec"], "av1")
        self.assertEqual(plan["video"]["resolution"], [960, 400])
        track = plan["audio"][0]
        self.assertEqual((track["codec"], track["channels"], track["language"], track["title"]),
                         ("opus", 2, "spa", "Spanish"))
        self.assertFalse(track["default"])
        self.assertFalse(track["forced"])
        self.assertEqual(plan["chapters"], [])

    def test_unknown_maps_inputs_and_defaults_are_not_guessed(self):
        for mapping, sources in [("0", {0: SOURCE}), ("[aout]", {0: SOURCE}),
                                 ("-0:a:0", {0: SOURCE}), ("1:a:0", {0: SOURCE})]:
            with self.subTest(mapping=mapping):
                command = self.command("-map", mapping, "-c", "copy")
                self.assertIsNone(report.build_plan(SOURCE, {}, command, sources=sources)["audio"])
        plan = report.build_plan(SOURCE, {}, self.command("-map", "0:a"))
        self.assertIsNone(plan["audio"][0]["codec"])
        self.assertIsNone(plan["video"]["codec"])
        self.assertIsNone(plan["video"]["resolution"])
        self.assertEqual(report.build_plan(SOURCE, {}, self.command("-an", "-sn"))["audio"], [])

    def test_complex_geometry_and_edited_timelines_are_unknown(self):
        for args in [("-filter_complex", "[0:v]scale=640:480[v]"),
                     ("-vf", "pad=2000:1200"), ("-vf", "scale=640:480")]:
            plan = report.build_plan(SOURCE, {}, self.command("-c:v", "libx264", *args))
            self.assertIsNone(plan["video"]["resolution"])
        plan = report.build_plan(SOURCE, {"video_mode": "editor"}, self.command("-c", "copy"), chapters=[])
        self.assertIsNone(plan["chapters"])
        self.assertIsNone(plan["duration"])

    def test_trim_duration_cannot_exceed_remaining_source(self):
        plan = report.build_plan(SOURCE, {}, self.command("-c", "copy", "-ss", "90", "-t", "30"))
        self.assertEqual(plan["duration"], 10)
        self.assertIsNone(plan["chapters"])

    def test_checked_and_not_checked_remain_distinct(self):
        result = report.build_report({}, dict(SOURCE, size_bytes=100), actual_chapters=[])
        by_key = checks(result)
        self.assertEqual(by_key["output.probe"]["status"], "ok")
        for key in ("integrity.samples", "quality.vmaf", "decode.full", "audio.payload",
                    "hdr.dynamic_payload", "output.size_estimate", "output.caps", "video.codec"):
            self.assertEqual(by_key[key]["status"], "not_checked", key)
            self.assertFalse(by_key[key]["checked"], key)
        summary = result["summary"]
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["checked"] + summary["not_checked"], len(result["checks"]))
        json.dumps(result, allow_nan=False)

    def test_mismatches_and_existing_failed_checks_are_visible(self):
        plan = report.build_plan(SOURCE, {}, self.command("-c", "copy"), chapters=[])
        actual = dict(SOURCE, codec="h264", width=1280, height=720, audio=[], duration=80, size_bytes=500)
        result = report.build_report(plan, actual, actual_chapters=[], integrity_ok=False,
                                     integrity_msg="decode failed", vmaf_target=95, vmaf_score=80,
                                     caps_enabled=True, caps_failed=True, operation_error="HDR reinjection failed")
        by_key = checks(result)
        self.assertEqual(result["summary"]["status"], "fail")
        self.assertEqual(by_key["video.codec"]["status"], "fail")
        self.assertEqual(by_key["video.resolution"]["status"], "fail")
        self.assertEqual(by_key["integrity.samples"]["status"], "fail")
        self.assertEqual(by_key["output.caps"]["status"], "fail")
        self.assertEqual(by_key["quality.vmaf"]["status"], "warn")
        self.assertEqual(by_key["operation.warning"]["actual"], "HDR reinjection failed")
        self.assertEqual(result["integrity_message"], "decode failed")

    def test_probe_failure_is_not_an_empty_success(self):
        result = report.build_report({}, None, probe_error="broken file", chapter_error="probe timeout")
        by_key = checks(result)
        self.assertEqual(by_key["output.probe"]["status"], "fail")
        self.assertEqual(by_key["chapters.probe"]["status"], "fail")
        self.assertEqual(by_key["chapters.count"]["status"], "not_checked")
        self.assertEqual(result["probe_error"], "broken file")
        self.assertEqual(len(result["errors"]), 2)

    def test_chapter_probe_failure_and_malformed_data(self):
        for mocked in [SimpleNamespace(returncode=1, stderr="bad chapters", stdout="{}"),
                       SimpleNamespace(returncode=0, stderr="", stdout="{}"),
                       SimpleNamespace(returncode=0, stderr="", stdout='{"chapters":[{"start_time":"NaN"}]}')]:
            with self.subTest(mocked=mocked), patch.object(report.subprocess, "run", return_value=mocked):
                chapters, error = report.probe_chapters(Path("out.mkv"))
                self.assertIsNone(chapters)
                self.assertTrue(error)
        with patch.object(report.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            self.assertIsNotNone(report.probe_chapters(Path("out.mkv"))[1])
        with patch.object(report.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="", stdout='{"chapters":[]}')):
            self.assertEqual(report.probe_chapters(Path("out.mkv")), ([], None))

    def test_unchecked_bitrate_cap_is_not_a_success(self):
        result = report.build_report({}, dict(SOURCE, size_bytes=100), caps_enabled=True,
                                     caps_checked=False, caps_failed=False)
        self.assertEqual(checks(result)["output.caps"]["status"], "not_checked")
        failed = report.build_report({}, dict(SOURCE, size_bytes=100), caps_enabled=True,
                                     caps_checked=False, caps_failed=True)
        self.assertEqual(checks(failed)["output.caps"]["status"], "fail")

    def test_nonfinite_quality_and_size_estimates_are_unchecked(self):
        result = report.build_report({"size_estimate": {"bytes": float("nan")}}, SOURCE,
                                     vmaf_target=95, vmaf_score=float("nan"))
        self.assertEqual(checks(result)["quality.vmaf"]["status"], "not_checked")
        self.assertEqual(checks(result)["output.size_estimate"]["status"], "not_checked")

    def test_final_path_and_postprocessing_failures_are_recorded(self):
        result = report.build_report({}, dict(SOURCE, size_bytes=100), output_path="temp.mkv")
        report.record_postprocessing(result, "inplace", "final.mkv")
        self.assertEqual(result["audited_path"], "temp.mkv")
        self.assertEqual(result["output_path"], "final.mkv")
        self.assertEqual(checks(result)["output.postprocess"]["status"], "ok")
        report.record_postprocessing(result, "inplace", "temp.mkv", error="blocked", blocked=True)
        self.assertEqual(checks(result)["output.postprocess"]["actual"], "blocked")
        self.assertEqual(result["summary"]["status"], "warn")
        self.assertEqual(len([c for c in result["checks"] if c["key"] == "output.postprocess"]), 1)
        self.assertEqual(result["errors"][-1]["error"], "blocked")

    def test_estimate_and_chapter_tolerance_are_explicit(self):
        plan = {"size_estimate": {"bytes": 1000, "basis": "sample"}, "size_target_bytes": 1200,
                "chapters": [{"start": 0, "end": 10, "title": "One"}]}
        result = report.build_report(plan, {"size_bytes": 1300},
                                     actual_chapters=[{"start": 0.05, "end": 10.05, "title": "One"}])
        by_key = checks(result)
        self.assertEqual(by_key["chapters.timeline"]["status"], "ok")
        self.assertEqual(by_key["output.size_estimate"]["status"], "warn")
        self.assertEqual(by_key["output.size_target"]["status"], "warn")
        self.assertEqual(result["size"]["deviation_pct"], 30)

    def test_editor_encode_preserves_10bit_hdr_overlays_and_smart_cut_plan(self):
        from core import editor, ffmpeg_utils as ff
        fake_info = ff.VideoInfo(
            path="source.mkv", width=3840, height=2160, duration=120.0,
            codec="hevc", pix_fmt="yuv420p10le", color_transfer="smpte2084",
            color_primaries="bt2020", color_space="bt2020nc", bit_rate=20000000,
            size_bytes=100000, fps=24.0, bit_depth=10, hdr_type="HDR10",
        )
        segs = [{
            "path": "source.mkv", "abs": Path("source.mkv"), "kind": "media",
            "start": 10.0, "end": 25.0, "title": "Szene 1",
            "audio_index": 0, "audio_indexes": [0], "mute": False,
            "sub_index": -1, "sub_indexes": [], "burn_subs": False,
            "fade_in": 1.0, "fade_out": 0.0, "speed": 1.0,
            "crop": "3840:1600:0:280", "scale": 0, "rotate": 90,
            "hflip": True, "vflip": False, "volume_db": 3.5, "loudnorm": True,
        }]
        ovs = [{
            "path": "broll.mkv", "abs": Path("broll.mkv"), "title": "PiP",
            "start": 0.0, "end": 5.0, "tl_start": 2.0,
            "position": "pip_br", "size_pct": 30, "opacity": 0.85, "fade": 0.5,
        }]
        with patch.object(ff, "probe_with_error", return_value=(fake_info, None)), \
             patch.object(editor, "_audio_tracks", return_value=[{"channels": 6}]), \
             patch.object(editor.remux, "write_chapter_meta", return_value=None):
            cmd, err = editor.build_editor_encode_cmd(
                segs, Path("out.mkv"), platform="cpu", codec="av1",
                crossfade=0.0, transition="wipeleft", preserve_hdr=True,
                overlays=ovs, add_chapters=False,
            )
            self.assertEqual(err, "")
            joined = " ".join(cmd)
            self.assertIn("yuv420p10le", joined)
            self.assertIn("transpose=1", joined)
            self.assertIn("hflip", joined)
            self.assertIn("loudnorm=", joined)
            self.assertIn("volume=3.50dB", joined)
            self.assertIn("overlay=", joined)
            self.assertIn("bt2020", joined)


if __name__ == "__main__":
    unittest.main()
