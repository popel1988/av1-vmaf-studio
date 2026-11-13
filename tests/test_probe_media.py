"""ffprobe-Metadaten ohne Decode: Bild, Ton, Kapitel, Anhänge."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import ffmpeg_utils as ff
from core import library


def _video(**extra):
    base = {
        "codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
        "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
        "level": "153", "avg_frame_rate": "24000/1001", "r_frame_rate": "24000/1001",
        "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "color_space": "bt2020nc", "color_range": "tv",
        "sample_aspect_ratio": "1:1", "display_aspect_ratio": "16:9",
        "field_order": "progressive", "bit_rate": "20000000",
        "tags": {"encoder": "libx265 - 3.5", "title": "Hauptfilm"},
    }
    base.update(extra)
    return base


class ProbeMediaTests(unittest.TestCase):
    def test_constant_hdr_file_keeps_square_pixels_and_splits_tools(self):
        info, err = ff.info_from_probe({
            "streams": [_video(), {
                "codec_type": "audio", "codec_name": "truehd", "profile": "Dolby TrueHD",
                "channels": 8, "channel_layout": "7.1", "sample_rate": "48000",
                "tags": {"language": "deu", "title": "Atmos"},
                "disposition": {"default": 1, "original": 1},
            }, {
                "codec_type": "audio", "codec_name": "aac", "profile": "LC",
                "channels": 2, "sample_rate": "48000", "bit_rate": "160000",
                "tags": {"language": "eng", "title": "Director commentary"},
                "disposition": {"comment": 1, "hearing_impaired": 1},
            }, {
                "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
                "tags": {"language": "deu"},
                "disposition": {"hearing_impaired": 1},
            }, {
                "codec_type": "attachment", "codec_name": "ttf",
                "tags": {"filename": "Font.ttf", "mimetype": "application/x-truetype-font"},
            }],
            "chapters": [
                {"start_time": "0", "tags": {"title": "Anfang"}},
                {"start_time": "63.5", "tags": {"title": "Zweiter Akt"}},
            ],
            "format": {"format_name": "matroska,webm", "bit_rate": "25000000",
                       "tags": {"encoder": "libebml", "title": "Hauptfilm"}},
        }, Path("film.mkv"), size_bytes=1000)
        self.assertIsNone(err)
        self.assertEqual(info.level_label, "5.1")
        self.assertEqual(info.frame_rate_mode, "cfr")
        self.assertFalse(info.anamorphic)
        self.assertEqual(info.encoder, "libx265 - 3.5")
        self.assertEqual(info.muxer, "libebml")
        self.assertEqual(info.container_title, "Hauptfilm")
        self.assertEqual(info.chapter_count, 2)
        self.assertEqual(info.chapters[1]["start"], 63.5)
        self.assertEqual(info.audio[0]["delivery"], "lossless")
        self.assertEqual(info.audio[1]["delivery"], "lossy")
        self.assertTrue(info.audio[1]["commentary"])
        self.assertTrue(info.audio[1]["hearing_impaired"])
        self.assertEqual(info.subtitles[0]["kind"], "image")
        self.assertEqual(info.attachments[0]["filename"], "Font.ttf")
        self.assertIs(info.dolby_vision, False)
        self.assertEqual(ff.extra_probe_fields(info)["probe_rev"], 4)

    def test_cover_does_not_replace_the_real_picture(self):
        info, err = ff.info_from_probe({"streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "width": 400, "height": 400,
             "disposition": {"attached_pic": 1}, "avg_frame_rate": "90000/1",
             "r_frame_rate": "90000/1"},
            _video(codec_name="h264", level="41", width=720, height=576,
                   sample_aspect_ratio="64:45", display_aspect_ratio="16:9",
                   field_order="tt", avg_frame_rate="24000/1001", r_frame_rate="25/1",
                   side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90},
                                   {"side_data_type": "Content light level metadata",
                                    "max_content": 1000, "max_average": 400},
                                   {"side_data_type": "Mastering display metadata",
                                    "max_luminance": "1000", "min_luminance": "0.005"}]),
        ], "format": {"format_name": "mov"}}, Path("cover.mp4"), size_bytes=10)
        self.assertIsNone(err)
        self.assertEqual((info.width, info.height, info.codec), (720, 576, "h264"))
        self.assertEqual(info.level_label, "4.1")
        self.assertTrue(info.anamorphic)
        self.assertEqual(info.dar, "16:9")
        self.assertEqual(info.rotation, -90)
        self.assertTrue(info.interlaced)
        self.assertEqual(info.frame_rate_mode, "vfr")
        self.assertEqual(info.fps_nominal, 25.0)
        self.assertEqual(info.max_cll, 1000)
        self.assertEqual(info.max_fall, 400)
        self.assertEqual(info.master_max, 1000)
        self.assertEqual(info.video_tracks[0]["attached_pic"], True)
        self.assertEqual(info.video_tracks[1]["codec"], "h264")

    def test_film_rate_rounding_is_not_reported_as_variable(self):
        info, _ = ff.info_from_probe({"streams": [_video(
            avg_frame_rate="24/1", r_frame_rate="24000/1001")],
            "format": {}}, Path("cfr.mkv"), size_bytes=10)
        self.assertEqual(info.frame_rate_mode, "cfr")
        self.assertEqual(info.fps_nominal, 0)

    def test_mkv_timebase_is_variable_without_a_fake_nominal_rate(self):
        info, _ = ff.info_from_probe({"streams": [_video(
            avg_frame_rate="24000/1001", r_frame_rate="1000/1")],
            "format": {}}, Path("vfr.mkv"), size_bytes=10)
        self.assertEqual(info.frame_rate_mode, "vfr")
        self.assertEqual(info.fps_nominal, 0)

    def test_dts_hd_ma_is_lossless_and_plain_dts_is_not(self):
        info, _ = ff.info_from_probe({"streams": [_video(),
            {"codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA", "channels": 6},
            {"codec_type": "audio", "codec_name": "dts", "profile": "DTS", "channels": 6}],
            "format": {}}, Path("dts.mkv"), size_bytes=10)
        self.assertEqual([a["delivery"] for a in info.audio], ["lossless", "lossy"])

    def test_missing_video_is_an_error_and_bad_chapters_are_skipped(self):
        info, err = ff.info_from_probe({"streams": [], "format": {}}, Path("x.mkv"), 1)
        self.assertIsNone(info)
        self.assertIn("Video", err)
        info, err = ff.info_from_probe({"streams": [_video()], "chapters": ["nein", {"start_time": "5"}]},
                                       Path("x.mkv"), 1)
        self.assertIsNone(err)
        self.assertEqual(info.chapter_count, 1)

    def test_levels_and_probe_command(self):
        self.assertEqual(ff.level_label("av1", "13"), "5.1")
        payload = json.dumps({"streams": [_video()], "format": {"format_name": "matroska"}})
        with patch.object(ff.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=payload, stderr="")) as run:
            info, err = ff.probe_with_error(Path("clip.mkv"), size_bytes=10)
        self.assertIsNone(err)
        self.assertEqual(info.codec, "hevc")
        self.assertIn("-show_chapters", run.call_args.args[0])

    def test_library_details_keep_the_probe_facts(self):
        info, _ = ff.info_from_probe({"streams": [_video(field_order="bb")],
                                      "format": {"format_name": "matroska"}},
                                     Path("movie.mkv"), 20)
        with patch.object(library.ff, "probe_with_error", return_value=(info, None)):
            details = library.file_details(Path("movie.mkv"), probe=True)
        self.assertEqual(details["probe_rev"], 4)
        self.assertIs(details["dolby_vision"], False)
        self.assertTrue(details["interlaced"])
        self.assertEqual(details["field_order"], "bb")
        self.assertEqual(details["level_label"], "5.1")

    def test_profile_8_keeps_the_integer_and_names_the_base(self):
        info, err = ff.info_from_probe({"streams": [_video(side_data_list=[{
            "side_data_type": "DOVI configuration record",
            "dv_version_major": 1, "dv_version_minor": 0,
            "dv_profile": 8, "dv_level": 6,
            "rpu_present_flag": 1, "el_present_flag": 0, "bl_present_flag": 1,
            "dv_bl_signal_compatibility_id": 1,
        }])], "format": {}}, Path("dv8.mkv"), 10)
        self.assertIsNone(err)
        self.assertEqual(info.dv_profile, 8)
        self.assertEqual(info.dv_profile_label, "8.1")
        self.assertEqual(info.hdr_type, "Dolby Vision 8.1 + HDR10")
        self.assertEqual(info.dv_level, 6)
        self.assertEqual(info.dv_version, "1.0")
        self.assertTrue(info.dv_rpu)
        self.assertFalse(info.dv_el)
        self.assertTrue(info.dv_bl)
        self.assertEqual(info.dv_compatibility_label, "HDR10")
        self.assertEqual(info.video_tracks[0]["role"], "base")
        self.assertEqual(info.video_tracks[0]["dv_profile_label"], "8.1")

    def test_profile_7_describes_an_embedded_layer_without_a_second_track(self):
        info, _ = ff.info_from_probe({"streams": [_video(side_data_list=[{
            "side_data_type": "DOVI configuration record",
            "dv_profile": 7, "dv_level": 6,
            "rpu_present_flag": 1, "el_present_flag": 1, "bl_present_flag": 1,
            "dv_bl_signal_compatibility_id": 6,
        }])], "format": {}}, Path("dv7.mkv"), 10)
        self.assertEqual(info.dv_profile, 7)
        self.assertEqual(info.dv_profile_label, "7")
        self.assertTrue(info.dv_el)
        self.assertTrue(info.dv_bl)
        self.assertEqual(info.dv_compatibility_label, "HDR10")
        self.assertEqual(len(info.video_tracks), 1)
        self.assertEqual(info.video_tracks[0]["role"], "base")

    def test_a_second_stream_without_a_base_layer_is_the_enhancement(self):
        info, _ = ff.info_from_probe({"streams": [
            _video(side_data_list=[{
                "side_data_type": "DOVI configuration record", "dv_profile": 7,
                "el_present_flag": 0, "bl_present_flag": 1, "rpu_present_flag": 1,
            }]),
            _video(width=1920, height=1080, side_data_list=[{
                "side_data_type": "DOVI configuration record", "dv_profile": 7,
                "el_present_flag": 1, "bl_present_flag": 0, "rpu_present_flag": 1,
                "dv_level": 6,
            }]),
        ], "format": {}}, Path("fel.mkv"), 10)
        self.assertEqual([track["role"] for track in info.video_tracks], ["base", "enhancement"])
        self.assertFalse(info.video_tracks[1]["el_inferred"])
        self.assertFalse(info.video_tracks[1]["dv_bl"])
        self.assertEqual(info.video_tracks[1]["hdr_type"], "Dolby Vision 7 + HDR10")

    def test_one_extra_hevc_track_is_inferred_only_for_profile_7(self):
        extra = _video(width=1920, height=1080, color_transfer="bt709",
                       color_primaries="bt709", color_space="bt709")
        info, _ = ff.info_from_probe({"streams": [
            _video(side_data_list=[{
                "side_data_type": "DOVI configuration record", "dv_profile": 7,
                "el_present_flag": 1, "bl_present_flag": 1,
            }]), extra,
        ], "format": {}}, Path("fel-inferred.mkv"), 10)
        self.assertEqual(info.video_tracks[1]["role"], "enhancement")
        self.assertTrue(info.video_tracks[1]["el_inferred"])

        info, _ = ff.info_from_probe({"streams": [
            _video(side_data_list=[{
                "side_data_type": "DOVI configuration record", "dv_profile": 7,
                "el_present_flag": 1, "bl_present_flag": 1,
            }]), extra, dict(extra),
        ], "format": {}}, Path("fel-ambiguous.mkv"), 10)
        self.assertEqual([track["role"] for track in info.video_tracks], ["base", "video", "video"])
        self.assertFalse(any(track["el_inferred"] for track in info.video_tracks))

    def test_missing_dovi_flags_stay_unknown_and_level_zero_is_kept(self):
        info, _ = ff.info_from_probe({"streams": [_video(codec_name="av1", side_data_list=[{
            "side_data_type": "DOVI configuration record",
            "dv_profile": 10, "dv_level": 0,
            "dv_bl_signal_compatibility_id": 1,
        }])], "format": {}}, Path("dv10.mkv"), 10)
        self.assertEqual(info.dv_profile, 10)
        self.assertEqual(info.dv_profile_label, "10.1")
        self.assertEqual(info.dv_level, 0)
        self.assertIsNone(info.dv_rpu)
        self.assertIsNone(info.dv_el)
        self.assertIsNone(info.dv_bl)
        self.assertEqual(info.dv_compatibility_label, "HDR10")

    def test_profile_filter_groups_minors_and_keeps_exact_matches(self):
        profile_8_1, _ = ff.info_from_probe({"streams": [_video(side_data_list=[{
            "side_data_type": "DOVI configuration record", "dv_profile": 8,
            "dv_bl_signal_compatibility_id": 1,
        }])], "format": {}}, Path("dv81.mkv"), 10)
        profile_8_2, _ = ff.info_from_probe({"streams": [_video(side_data_list=[{
            "side_data_type": "DOVI configuration record", "dv_profile": 8,
            "dv_bl_signal_compatibility_id": 2,
        }])], "format": {}}, Path("dv82.mkv"), 10)
        profile_10, _ = ff.info_from_probe({"streams": [_video(side_data_list=[{
            "side_data_type": "DOVI configuration record", "dv_profile": 10,
            "dv_bl_signal_compatibility_id": 1,
        }])], "format": {}}, Path("dv101.mkv"), 10)
        plain, _ = ff.info_from_probe({"streams": [_video(side_data_list=[{
            "side_data_type": "DOVI configuration record", "dv_profile": 8,
        }])], "format": {}}, Path("dv8plain.mkv"), 10)
        self.assertTrue(library._dynamic_match(profile_8_1, ["dv8"]))
        self.assertTrue(library._dynamic_match(profile_8_2, ["dv8"]))
        self.assertTrue(library._dynamic_match(plain, ["dv8"]))
        self.assertTrue(library._dynamic_match(profile_8_1, ["dv8.1"]))
        self.assertFalse(library._dynamic_match(profile_8_2, ["dv8.1"]))
        self.assertFalse(library._dynamic_match(plain, ["dv8.1"]))
        self.assertTrue(library._dynamic_match(profile_10, ["dv10"]))
        self.assertTrue(library._dynamic_match(profile_10, ["dv10.1"]))
        self.assertFalse(library._dynamic_match(profile_10, ["dv8"]))
        self.assertFalse(library._dynamic_match(profile_8_1, ["dv7"]))

    def test_1080p_sdr_is_never_false_positive_dolby_vision(self):
        info, err = ff.info_from_probe({"streams": [_video(
            codec_name="h264", width=1920, height=1080, pix_fmt="yuv420p",
            color_transfer="bt709", color_primaries="bt709", color_space="bt709",
        )], "format": {"format_name": "matroska"}}, Path("fhd_sdr.mkv"), 10)
        self.assertIsNone(err)
        self.assertIs(info.dolby_vision, False)
        self.assertFalse(info.is_hdr)
        self.assertEqual(info.hdr_type, "SDR")
        self.assertEqual(info.dv_profile, 0)
        as_dict = info.to_dict()
        self.assertIs(as_dict["dolby_vision"], False)
        self.assertIs(as_dict["is_hdr"], False)

    def test_empty_or_zeroed_dovi_side_data_is_ignored_on_sdr(self):
        info, err = ff.info_from_probe({"streams": [_video(
            codec_name="hevc", width=1920, height=1080, pix_fmt="yuv420p10le",
            color_transfer="bt709", color_primaries="bt709", color_space="bt709",
            side_data_list=[{
                "side_data_type": "DOVI configuration record",
                "dv_version_major": 1, "dv_version_minor": 0,
                "dv_profile": 0, "dv_level": 0,
                "rpu_present_flag": 0, "el_present_flag": 0, "bl_present_flag": 1,
            }],
        )], "format": {}}, Path("dummy_dovi.mkv"), 10)
        self.assertIsNone(err)
        self.assertIs(info.dolby_vision, False)
        self.assertFalse(info.is_hdr)
        self.assertEqual(info.hdr_type, "SDR")

    def test_sanitize_cached_matched_repairs_legacy_dict_dolby_vision(self):
        legacy = [{
            "path": "Movie.1080p.mkv",
            "name": "Movie.1080p.mkv",
            "codec": "h264",
            "height": 1080,
            "color_transfer": "bt709",
            "hdr_type": "SDR",
            "is_hdr": True,
            "dolby_vision": {
                "present": False, "profile": 0, "level": None, "version": "",
                "rpu": None, "el": None, "bl": None, "compatibility": None,
                "compatibility_label": "", "profile_label": "",
            },
            "dv_profile": 0,
            "hdr10_plus": False,
            "suggest": {"codec": "av1", "dv_mode": "preserve", "label": "AV1 · DV übernehmen"},
        }]
        cleaned = library._sanitize_cached_matched(legacy)
        self.assertEqual(len(cleaned), 1)
        self.assertIs(cleaned[0]["dolby_vision"], False)
        self.assertIs(cleaned[0]["is_hdr"], False)
        self.assertEqual(cleaned[0]["hdr_type"], "SDR")
        self.assertEqual(cleaned[0]["suggest"]["dv_mode"], "")
        self.assertEqual(cleaned[0]["suggest"]["label"], "AV1 · SDR")


if __name__ == "__main__":
    unittest.main()
