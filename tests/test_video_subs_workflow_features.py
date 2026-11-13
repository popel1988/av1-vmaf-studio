"""Tests für Punkt 1 (Auto-Filmkorn, AV1-HDR10+, VMAF-GPU Main/Legacy & Subsampling),
Punkt 2 (Sidecar-Untertitel-Muxing, interner Remux/Player-Versatz, PGS-OCR-Vorfilterung)
und Punkt 3 (Langzeit-Statistik-Dashboard, regelbasierte Spurauswahl, Profil-JSON-Bundle)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import unittest
from pathlib import Path
from unittest.mock import patch

from core import encoder, ffmpeg_utils as ff, hdr10plus, history, library, player_hls, profiles, remux, subtitle_ocr, vmaf


def _sample_info(tmp_path: Path, *, hdr10_plus: bool = False) -> ff.VideoInfo:
    p = tmp_path / "Movie.2024.mkv"
    p.write_bytes(b"\x00" * 1024)
    return ff.VideoInfo(
        path=str(p),
        duration=120.0,
        size_bytes=1024,
        container="mkv",
        codec="hevc",
        profile="Main 10",
        pix_fmt="yuv420p10le",
        bit_depth=10,
        width=3840,
        height=2160,
        fps=24.0,
        bit_rate=25_000_000,
        color_transfer="smpte2084" if hdr10_plus else "bt709",
        color_primaries="bt2020" if hdr10_plus else "bt709",
        color_space="bt2020nc" if hdr10_plus else "bt709",
        hdr_type="HDR10+" if hdr10_plus else "SDR",
        hdr10_plus=hdr10_plus,
        audio=[
            {"index": 0, "stream_index": 1, "codec": "truehd", "channels": 8, "language": "jpn", "title": "Original Japanese", "default": True, "forced": False},
            {"index": 1, "stream_index": 2, "codec": "eac3", "channels": 6, "language": "ger", "title": "Deutsch 5.1", "default": False, "forced": False},
            {"index": 2, "stream_index": 3, "codec": "ac3", "channels": 2, "language": "eng", "title": "Director's Commentary", "default": False, "forced": False},
            {"index": 3, "stream_index": 4, "codec": "eac3", "channels": 6, "language": "fra", "title": "French", "default": False, "forced": False},
        ],
        subtitles=[
            {"index": 0, "stream_index": 5, "codec": "subrip", "language": "ger", "title": "Deutsch Forced", "default": True, "forced": True},
            {"index": 1, "stream_index": 6, "codec": "subrip", "language": "ger", "title": "Deutsch SDH", "default": False, "forced": False},
            {"index": 2, "stream_index": 7, "codec": "subrip", "language": "eng", "title": "English Full", "default": False, "forced": False},
            {"index": 3, "stream_index": 8, "codec": "subrip", "language": "spa", "title": "Spanish", "default": False, "forced": False},
        ],
    )


# ---------------------------------------------------------------------------
# Punkt 1: Auto Film-Grain, AV1 HDR10+, VMAF GPU (Main vs. Legacy) & Subsample
# ---------------------------------------------------------------------------

def test_grain_mse_mapping():
    v0, cat0, _ = ff.grain_mse_to_svt_level(0.1)
    assert v0 == 0 and cat0 == "none"
    low, cat_l, _ = ff.grain_mse_to_svt_level(1.2)
    mid, cat_m, _ = ff.grain_mse_to_svt_level(5.5)
    high, cat_h, _ = ff.grain_mse_to_svt_level(40.0)
    assert 1 <= low < mid < high <= 50
    assert cat_l == "light" and cat_m == "medium" and cat_h == "heavy"
    # Anime deaktiviert/dämpft den Synthesewert
    v_anime, cat_anime, _ = ff.grain_mse_to_svt_level(5.5, anime=True)
    assert v_anime == 0 and cat_anime == "anime"


def test_av1_hdr10plus_native_passthrough(tmp_path: Path):
    info = _sample_info(tmp_path, hdr10_plus=True)
    out = tmp_path / "out.mkv"

    # SVT-AV1 (CPU) ohne aktive Videofilter darf KEIN -vf setparams einfügen,
    # damit AV_FRAME_DATA_DYNAMIC_HDR_PLUS als ITU-T T.35 OBU erhalten bleibt.
    cmd = encoder.build_encode_cmd(
        info, out, "cpu", "av1", 26, None, False, preserve_hdr=True,
    )
    assert "-vf" not in cmd
    assert "libsvtav1" in cmd

    assert hdr10plus.applicable(
        target_codec="av1", source_codec="hevc",
        preserve_hdr=True, tonemap=False, hdr10_plus=True,
        platform="cpu", av1_passthrough_blocked=False,
    ) == "nativ_av1"

    # GPU-AV1 kann HDR10+ nicht als T.35 OBU einbetten -> ehrlicher Grund
    reason_gpu = hdr10plus.applicable(
        target_codec="av1", source_codec="hevc",
        preserve_hdr=True, tonemap=False, hdr10_plus=True,
        platform="nvidia", av1_passthrough_blocked=False,
    )
    assert "SVT-AV1" in reason_gpu


def test_vmaf_subsample_defaults_to_1_and_validates():
    opts = vmaf.VmafOptions()
    assert opts.subsample == 1
    assert opts.gpu_accel is False
    assert vmaf.normalize_vmaf_subsample(None) == 1
    assert vmaf.normalize_vmaf_subsample(0) == 1
    assert vmaf.normalize_vmaf_subsample(3) == 3
    assert vmaf.normalize_vmaf_subsample(99) == 30


def test_vmaf_gpu_driver_gate_main_vs_legacy():
    # Main-Image (FFmpeg 9.0 / CUDA >= 610): Treiber 550 reicht NICHT -> Fallback auf CPU
    with patch("core.config.IMAGE_CHANNEL", "latest"), \
         patch("core.hardware.HardwareMonitor._find_nvidia_smi", return_value="nvidia-smi"), \
         patch("core.hardware._run", return_value="550.90.07\n"), \
         patch("core.vmaf.libvmaf_cuda_available", return_value=True):
        st_main = vmaf.vmaf_gpu_status()
        assert st_main["available"] is False
        assert st_main["min_driver"] == 610
        assert st_main["channel"] == "latest"

    # Legacy-Image (FFmpeg 8.1 / CUDA >= 525): derselbe Treiber 550 REICHT!
    with patch("core.config.IMAGE_CHANNEL", "legacy"), \
         patch("core.hardware.HardwareMonitor._find_nvidia_smi", return_value="nvidia-smi"), \
         patch("core.hardware._run", return_value="550.90.07\n"), \
         patch("core.vmaf.libvmaf_cuda_available", return_value=False):
        st_leg = vmaf.vmaf_gpu_status()
        assert st_leg["available"] is True
        assert st_leg["mode"] == "cuda_decode"
        assert st_leg["min_driver"] == 525
        assert st_leg["channel"] == "legacy"


# ---------------------------------------------------------------------------
# Punkt 2: Sidecar-Untertitel, Remux/Player-Versatz & PGS-OCR-Vorfilterung
# ---------------------------------------------------------------------------

def test_sidecar_discovery_and_encode_muxing(tmp_path: Path):
    info = _sample_info(tmp_path)
    s_de = tmp_path / "Movie.2024.de.forced.srt"
    s_en = tmp_path / "Movie.2024.en.sdh.srt"
    s_pgs = tmp_path / "Movie.2024.ja.sup"
    for f in (s_de, s_en, s_pgs):
        f.write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")

    found = library.find_sidecar_subtitles(info.path)
    assert len(found) == 3
    by_name = {x["name"]: x for x in found}
    assert by_name["Movie.2024.de.forced.srt"]["language"] == "deu"
    assert by_name["Movie.2024.de.forced.srt"]["forced"] is True
    assert by_name["Movie.2024.en.sdh.srt"]["language"] == "eng"
    assert by_name["Movie.2024.en.sdh.srt"]["sdh"] is True
    assert by_name["Movie.2024.ja.sup"]["bitmap"] is True

    # In MKV werden alle 3 (inkl. Bitmap .sup) hinter den 4 internen UT gemuxt
    cmd_mkv = encoder.build_encode_cmd(
        info, tmp_path / "out.mkv", "cpu", "av1", 28, None, False,
        external_subtitles=found,
    )
    assert str(s_de) in cmd_mkv
    assert str(s_pgs) in cmd_mkv
    assert "1:s:0" in cmd_mkv
    assert "language=deu" in cmd_mkv

    # In MP4 wird die Bitmap-.sup automatisch übersprungen, Text-UT -> mov_text
    cmd_mp4 = encoder.build_encode_cmd(
        info, tmp_path / "out.mp4", "cpu", "h264", 22, None, False,
        container="mp4", external_subtitles=found,
    )
    assert str(s_de) in cmd_mp4
    assert str(s_pgs) not in cmd_mp4
    assert "mov_text" in cmd_mp4


def test_remux_internal_track_delay_and_player_negative_delay(tmp_path: Path):
    info = _sample_info(tmp_path)
    spec = {
        "container": "mkv",
        "audio": [
            {"index": 0, "keep": True},
            {"index": 1, "keep": True, "delay_ms": -250},
        ],
        "subtitles": [
            {"index": 0, "keep": True, "delay_ms": 400},
        ],
    }
    cmd, err = remux.build_edit_cmd(info, tmp_path / "remuxed.mkv", spec)
    assert err == ""
    assert "-itsoffset" in cmd
    assert "-0.25" in cmd
    assert "0.4" in cmd
    assert "1:a:1?" in cmd
    assert "2:s:0?" in cmd

    # Player HLS mit negativem Audio-Versatz (-350 ms) legt zweiten Input mit -itsoffset an
    hls_cmd = player_hls._build_hls_cmd(
        info.path, tmp_path,
        audio_index=1, start_sec=10.0, audio_codec="eac3",
        platform="cpu", encoder="copy", codec="h264",
        height=0, v_bitrate=0, video_copy=True,
        audio_delay_ms=-350, source_codec="hevc",
    )
    assert "-itsoffset" in hls_cmd
    assert "-0.350" in hls_cmd
    assert "1:a:1?" in hls_cmd


def test_pgs_ocr_preprocessing_upscales_and_cleans_text(tmp_path: Path):
    # Kleines 40x20 Untertitel-Cue mit weißem Text (#1) und dunklem Outline (#2), Format: (luma, alpha)
    palette = {
        0: (0, 0),       # transparent
        1: (245, 255),   # Textkern (hell, deckend)
        2: (30, 255),    # schwarze Kontur -> muss unterdrückt werden!
    }
    bmp = bytearray(40 * 20)
    for y in range(5, 15):
        for x in range(8, 32):
            bmp[y * 40 + x] = 2
    for y in range(7, 13):
        for x in range(10, 30):
            bmp[y * 40 + x] = 1

    pgm = tmp_path / "cue.pgm"
    subtitle_ocr._write_pgm_for_ocr(bytes(bmp), palette, 40, 20, pgm)
    raw = pgm.read_bytes()
    header_end = raw.index(b"\n255\n") + 5
    header = raw[:header_end].decode("ascii").splitlines()
    assert header[0] == "P5"
    dims = [int(v) for v in header[1].split()]
    # 40x20 (<96px Höhe) -> 2x Upscale (80x40) + 2*24px Ruhe-Rand = 128 x 88
    assert dims == [80 + 48, 40 + 48]

    cleaned = subtitle_ocr._clean_ocr_text("  |ch bin bereit! \n\n\n Weiter geht's  ")
    assert cleaned == "Ich bin bereit!\nWeiter geht's"


# ---------------------------------------------------------------------------
# Punkt 3: Regelbasierte Spurauswahl, Statistik-Dashboard & Profil-Bundles
# ---------------------------------------------------------------------------

def test_rule_based_track_selection(tmp_path: Path):
    info = _sample_info(tmp_path)
    sidecars = [
        {"path": "a.de.srt", "name": "a.de.srt", "language": "deu", "forced": False, "sdh": False},
        {"path": "a.en.sdh.srt", "name": "a.en.sdh.srt", "language": "eng", "forced": False, "sdh": True},
        {"path": "a.fr.srt", "name": "a.fr.srt", "language": "fra", "forced": False, "sdh": False},
    ]
    settings = {
        "audio_languages": "de, en",
        "audio_keep_original": True,
        "audio_exclude_commentary": True,
        "subtitle_languages": "de, en",
        "sub_exclude_sdh": True,
        "sub_keep_forced": True,
    }
    res = ff.apply_track_rules(info, settings, sidecars=sidecars)
    assert res["audio_rules_active"] is True
    # Spur 0 (Original jpn) + Spur 1 (ger) bleiben; Spur 2 (eng Commentary) & Spur 3 (fra) entfallen!
    assert res["audio_tracks"] == [0, 1]

    assert res["sub_rules_active"] is True
    sub_indices = [s["index"] for s in res["subtitle_track_settings"]]
    # Spur 0 (ger Forced) + Spur 2 (eng Full) bleiben; Spur 1 (ger SDH) & Spur 3 (spa) entfallen!
    assert sub_indices == [0, 2]
    # Sidecar: nur a.de.srt bleibt (en ist SDH, fr nicht in Whitelist)
    assert [sc["name"] for sc in res["sidecars"]] == ["a.de.srt"]


def test_history_analytics_and_delete(tmp_path: Path):
    from types import SimpleNamespace
    from core.queue_manager import JobSettings

    old_conn = history._conn
    try:
        if history._conn is not None:
            try:
                history._conn.close()
            except Exception:
                pass
        history._conn = None
        with patch("core.config.DATA_DIR", tmp_path):
            history.init_db()
            item = SimpleNamespace(
                id="job-1",
                title="Blade.Runner.mkv",
                path="/media/Blade.Runner.mkv",
                status="fertig",
                original_size=20_000_000_000,
                output_size=8_000_000_000,
                saved_bytes=12_000_000_000,
                encode_seconds=1800.0,
                vmaf_verify=95.4,
                vmaf=None,
                created_at=1000.0,
                output_path="/media/Blade.Runner_av1.mkv",
                info={"codec": "hevc", "width": 3840, "height": 2160, "duration": 7200.0, "bit_rate": 22_000_000},
                settings=JobSettings(video_mode="encode", platform="cpu", codec="av1", quality=26, rate_mode="cq"),
                steps=[],
                details={},
                report={},
            )
            history.record_job(item, duration=1800.0)
            st = history.stats()
            assert st["count_done"] == 1
            assert st["saved_bytes"] == 12_000_000_000
            assert abs(st["realtime_factor"] - 4.0) < 0.1
            assert len(st["by_encoder"]) == 1
            assert abs(st["by_encoder"][0]["speed_x"] - 4.0) < 0.1
            assert st["by_resolution"][0]["resolution"] == "4K / UHD"
            assert len(st["timeline"]) == 1
            assert len(st["top_savers"]) == 1

            assert history.delete_job("job-1") is True
            assert history.delete_job("job-1") is False
            assert history.stats()["count_done"] == 0
            history._conn.close()
    finally:
        history._conn = old_conn


class VideoSubsWorkflowTestSuite(unittest.TestCase):
    """Unittest-Wrapper, damit `python -m unittest` alle neuen Tests direkt ausführt."""

    def test_all_features(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td1:
            t1 = Path(td1)
            test_grain_mse_mapping()
            test_av1_hdr10plus_native_passthrough(t1)
            test_vmaf_subsample_defaults_to_1_and_validates()
            test_vmaf_gpu_driver_gate_main_vs_legacy()
            test_sidecar_discovery_and_encode_muxing(t1)
            test_remux_internal_track_delay_and_player_negative_delay(t1)
            test_pgs_ocr_preprocessing_upscales_and_cleans_text(t1)
            test_rule_based_track_selection(t1)
        with tempfile.TemporaryDirectory() as td2:
            t2 = Path(td2)
            test_history_analytics_and_delete(t2 / "hist")
            test_profile_and_watch_bundle_export_import(t2 / "prof")

    def test_frontend_scripts_parse(self):
        try:
            import quickjs
        except ImportError:
            self.skipTest("quickjs nicht installiert")
        js_dir = Path(__file__).resolve().parents[1] / "static" / "js"
        for name in ("app.js", "player.js", "editor.js", "i18n.js"):
            src = (js_dir / name).read_text(encoding="utf-8")
            ctx = quickjs.Context()
            # Wrap in function body so QuickJS validates full JS syntax without executing DOM code
            ctx.eval(f"(function() {{\n{src}\n}});")


def test_profile_and_watch_bundle_export_import(tmp_path: Path):
    dir1 = tmp_path / "inst1"
    dir2 = tmp_path / "inst2"
    with patch("core.config.DATA_DIR", dir1):
        profiles.ensure_builtins()
        profiles.save_profile("Mein 4K Archiv", {"platform": "cpu", "codec": "av1", "quality": 24, "film_grain_auto": True})

        watch_rules = [{"name": "Serien-Ingest", "subfolder": "Serien", "profile": "Serie"}]
        bundle = profiles.export_bundle(include_watch=True, watch_rules=watch_rules)
        assert bundle["schema"] == "av1studio.profile_bundle"
        assert any(p["name"] == "Mein 4K Archiv" for p in bundle["profiles"])
        assert bundle["watch_rules"] == watch_rules

    # Auf frischer Instanz importieren
    with patch("core.config.DATA_DIR", dir2):
        profiles.ensure_builtins()
        res = profiles.import_bundle(bundle, overwrite=True)
        assert res["imported"] == 1
        assert res["watch_rules"] == watch_rules
        loaded = {p["name"]: p["settings"] for p in profiles.load_all()}
        assert loaded["Mein 4K Archiv"]["film_grain_auto"] is True
