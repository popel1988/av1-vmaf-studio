"""Automatik behält die Quellauflösung, feste Profile skalieren weiter."""
from types import SimpleNamespace
import unittest

from core.player_hls import plan_playback


def _info(height, codec="hevc", hdr=True):
    return SimpleNamespace(
        codec=codec, height=height, is_hdr=hdr, container="matroska",
        audio=[{"codec": "truehd"}], duration=100,
    )


class AutoResolutionTests(unittest.TestCase):
    def test_auto_transcode_keeps_2160p_for_tonemap_and_unknown_codec(self):
        look = {"apply": True, "tonemap": True, "platform": "cpu"}
        tonemap = plan_playback(
            _info(2160), profile="auto", audio_codec="truehd",
            client_codecs=["h264", "hevc", "av1"], look=look,
        )
        unknown = plan_playback(
            _info(2160, codec="mpeg2video", hdr=False), profile="auto",
            audio_codec="aac", client_codecs=["h264"],
        )
        for plan in (tonemap, unknown):
            self.assertEqual(plan["profile"], "original")
            self.assertEqual(plan["height"], 0)
            self.assertTrue(plan["need_encode"])
            self.assertEqual(plan["v_bitrate"], 8000)

    def test_explicit_1080p_still_scales(self):
        plan = plan_playback(
            _info(2160), profile="1080p", audio_codec="truehd",
            client_codecs=["h264", "hevc"],
        )
        self.assertEqual(plan["profile"], "1080p")
        self.assertEqual(plan["height"], 1080)
        self.assertEqual(plan["v_bitrate"], 6000)


if __name__ == "__main__":
    unittest.main()
