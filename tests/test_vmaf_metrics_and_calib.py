"""Tests für auswählbare VMAF-Zusatztests, Sub-Timings, Diagnose-Kalibrierung & Ordner-Erkennung."""
from __future__ import annotations

import time
import unittest
from unittest import mock
from fastapi.testclient import TestClient

import app as app_mod
from core import queue_manager as qm
from core import vmaf as vmaf_mod
from core import vmaf_calib
from core import works


class FolderFirstMediaTests(unittest.TestCase):
    def test_dedicated_movie_folder_beats_generic_or_noisy_filename(self):
        m1 = works.describe_media("movie.mkv", "Filme/Blade Runner 2049 (2017)", dedicated_folder=True)
        self.assertEqual(m1["kind"], "film")
        self.assertEqual(m1["title"], "Blade Runner 2049")
        self.assertEqual(m1["year"], 2017)

        m2 = works.describe_media("00001.m2ts", "UHD/Inception (2010)/BDMV/STREAM", dedicated_folder=True)
        self.assertEqual(m2["kind"], "film")
        self.assertEqual(m2["title"], "Inception")
        self.assertEqual(m2["year"], 2010)

        m3 = works.describe_media("dnp2-2160p-uhd-hdr-grp.mkv", "Filme/Dune Part Two (2024)", dedicated_folder=True)
        self.assertEqual(m3["kind"], "film")
        self.assertEqual(m3["title"], "Dune Part Two")
        self.assertEqual(m3["year"], 2024)

    def test_season_folder_recognizes_series_and_plain_episode_numbers(self):
        ep1 = works.describe_media("01 - Good News About Hell.mkv", "Serien/Severance (2022)/Season 01")
        self.assertEqual(ep1["kind"], "serie")
        self.assertEqual(ep1["show"], "Severance")
        self.assertEqual(ep1["show_year"], 2022)
        self.assertEqual(ep1["season"], 1)
        self.assertEqual(ep1["episode"], 1)

        ep2 = works.describe_media("Folge 04.mkv", "Serien/Dark/Staffel 2")
        self.assertEqual(ep2["kind"], "serie")
        self.assertEqual(ep2["show"], "Dark")
        self.assertEqual(ep2["season"], 2)
        self.assertEqual(ep2["episode"], 4)

    def test_bucket_directories_do_not_override_movie_stem(self):
        m = works.describe_media("Arrival.2016.2160p.UHD.BluRay.mkv", "Filme/2160p")
        self.assertEqual(m["kind"], "film")
        self.assertEqual(m["title"], "Arrival")
        self.assertEqual(m["year"], 2016)


class VmafMetricToggleTests(unittest.TestCase):
    def test_feature_attempts_respect_disabled_metrics(self):
        pure = vmaf_mod._build_vmaf_feature_attempts(
            with_psnr=False, with_ssim=False, with_ms_ssim=False, with_cambi=False
        )
        self.assertEqual(pure, [("", False)])

        psnr_only = vmaf_mod._build_vmaf_feature_attempts(
            with_psnr=True, with_ssim=False, with_ms_ssim=False, with_cambi=False
        )
        self.assertEqual(psnr_only[0], ("feature=name=psnr:", False))
        for feat_str, has_cambi in psnr_only:
            self.assertFalse(has_cambi)
            self.assertNotIn("cambi", feat_str)
            self.assertNotIn("float_ms_ssim", feat_str)
            self.assertNotIn("float_ssim", feat_str)

        cambi_ssim = vmaf_mod._build_vmaf_feature_attempts(
            with_psnr=False, with_ssim=True, with_ms_ssim=False, with_cambi=True
        )
        self.assertIn("name=float_ssim", cambi_ssim[0][0])
        self.assertIn("name=cambi", cambi_ssim[0][0])
        self.assertFalse(cambi_ssim[0][1])
        for feat_str, _ in cambi_ssim:
            self.assertNotIn("psnr", feat_str)
            self.assertNotIn("ms_ssim", feat_str)

    def test_job_settings_store_metric_flags(self):
        js = qm.build_job_settings({
            "platform": "cpu",
            "codec": "av1",
            "vmaf_check": True,
            "vmaf_psnr": True,
            "vmaf_ssim": False,
            "vmaf_ms_ssim": False,
            "vmaf_xpsnr": False,
            "vmaf_cambi": True,
            "ssimulacra2_frames": 0,
        })
        self.assertTrue(js.vmaf_psnr)
        self.assertFalse(js.vmaf_ssim)
        self.assertFalse(js.vmaf_ms_ssim)
        self.assertFalse(js.vmaf_xpsnr)
        self.assertTrue(js.vmaf_cambi)
        self.assertEqual(js.ssimulacra2_frames, 0)


class QueueSubtimingTests(unittest.TestCase):
    def test_live_and_completed_subtiming_in_queue_steps(self):
        item = qm.QueueItem(id="t1", title="movie.mkv", path="movie.mkv", settings=qm.JobSettings(), group_id="g1")
        qm._begin_step(item, "AV1 · ABR 14000 kbit/s", "analysis.candidate")
        t_start = time.perf_counter() - 2.4
        qm._update_substep(item, {
            "encode_sec": 5.0,
            "vmaf_sec": 1.0,
            "s2_sec": 0.0,
            "post_sec": 0.0,
            "active": "vmaf",
            "active_t0": t_start,
            "metrics": ["VMAF", "PSNR"],
        })
        live_steps = qm._steps_public(item)
        self.assertEqual(len(live_steps), 1)
        st_live = live_steps[0]["context"]["subtiming"]
        self.assertEqual(st_live["active"], "vmaf")
        self.assertGreaterEqual(st_live["vmaf_sec"], 3.0)
        self.assertEqual(st_live["encode_sec"], 5.0)

        qm._end_step(item)
        done_steps = qm._steps_public(item)
        self.assertEqual(len(done_steps), 1)
        st_done = done_steps[0]["context"]["subtiming"]
        self.assertNotIn("active", st_done)
        self.assertNotIn("active_t0", st_done)
        self.assertGreaterEqual(st_done["vmaf_sec"], 3.0)


class VmafCalibrationPairTests(unittest.TestCase):
    def test_available_pairs_accept_boolean_capability_cache(self):
        cache = {
            "nvidia:av1": True,
            "nvidia:hevc": False,
            "cpu:av1": True,
            "cpu:vp9": True,
            "intel:av1": {"ok": True},
            "amd:h264": {"ok": False},
        }
        with mock.patch.object(vmaf_calib.caps_mod, "results_map", return_value=cache):
            pairs = vmaf_calib._available_pairs()
        self.assertIn(("nvidia", "av1"), pairs)
        self.assertIn(("cpu", "av1"), pairs)
        self.assertIn(("cpu", "vp9"), pairs)
        self.assertIn(("intel", "av1"), pairs)
        self.assertNotIn(("nvidia", "hevc"), pairs)
        self.assertNotIn(("amd", "h264"), pairs)
        self.assertNotIn(("nvidia", "vp9"), pairs)


class VmafCalibrationAndEstimateTests(unittest.TestCase):
    def setUp(self):
        self.sample_calib = {
            "schema_version": 1,
            "calibrated_at": 1760000000.0,
            "encoders": {
                "nvidia:av1": {
                    "speeds_1080p": {"fastest": 260.0, "fast": 220.0, "balanced": 180.0, "slow": 110.0, "slowest": 60.0},
                    "fps_2160p_balanced": 54.0,
                    "scale_2160_to_1080": 0.3,
                },
                "cpu:av1": {
                    "speeds_1080p": {"fastest": 60.0, "fast": 35.0, "balanced": 18.0, "slow": 7.0, "slowest": 2.5},
                    "fps_2160p_balanced": 4.5,
                    "scale_2160_to_1080": 0.25,
                },
            },
            "vmaf": {
                "fps_1080p": 48.0,
                "fps_2160p": 13.0,
                "gpu_fps_1080p": 110.0,
                "gpu_fps_2160p": 34.0,
                "decode_share": 0.35,
                "overheads": {
                    "psnr": 0.03,
                    "ssim": 0.06,
                    "ms_ssim": 0.16,
                    "cambi": 0.24,
                    "xpsnr": 0.15,
                },
                "combo_scale": 0.85,
            },
            "ssimulacra2": {
                "available": True,
                "sec_per_pair_1080p": 0.45,
                "sec_per_pair_2160p": 1.4,
            },
            "reference": {"tonemap_fps_1080p": 45.0},
        }

    def test_estimate_reacts_to_metrics_resolution_and_s2(self):
        full = vmaf_calib.estimate_vmaf_run({
            "width": 1920, "height": 1080, "fps": 24,
            "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
            "test_values": [20, 24, 28, 32], "samples": 2, "clip_seconds": 20,
            "ssimulacra2_frames": 3,
            "vmaf_psnr": True, "vmaf_ssim": True, "vmaf_ms_ssim": True, "vmaf_xpsnr": True, "vmaf_cambi": True,
        }, calib=self.sample_calib)
        slim = vmaf_calib.estimate_vmaf_run({
            "width": 1920, "height": 1080, "fps": 24,
            "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
            "test_values": [20, 24, 28, 32], "samples": 2, "clip_seconds": 20,
            "ssimulacra2_frames": 0,
            "vmaf_psnr": False, "vmaf_ssim": False, "vmaf_ms_ssim": False, "vmaf_xpsnr": False, "vmaf_cambi": False,
        }, calib=self.sample_calib)
        uhd = vmaf_calib.estimate_vmaf_run({
            "width": 3840, "height": 2160, "fps": 24,
            "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
            "test_values": [20, 24, 28, 32], "samples": 2, "clip_seconds": 20,
            "ssimulacra2_frames": 3,
        }, calib=self.sample_calib)

        self.assertTrue(full["calibrated"])
        self.assertGreater(full["vmaf_sec"], slim["vmaf_sec"])
        self.assertGreater(full["s2_sec"], 0.0)
        self.assertEqual(slim["s2_sec"], 0.0)
        self.assertGreater(full["total_sec"], slim["total_sec"])
        self.assertGreater(uhd["total_sec"], full["total_sec"])

    def test_endpoints_and_ui_elements_present(self):
        client = TestClient(app_mod.app)
        r_html = client.get("/")
        self.assertEqual(r_html.status_code, 200)
        body = r_html.text
        for elem_id in (
            "opt-vmaf-psnr", "opt-vmaf-ms-ssim", "opt-vmaf-cambi", "opt-vmaf-xpsnr",
            "vt-psnr", "vt-ssim", "vt-ms-ssim", "vt-xpsnr", "vt-cambi",
            "st-psnr", "st-cambi", "eb-psnr", "btn-vc-start", "vt-vmaf-eta",
            "opt-encode-eta",
        ):
            self.assertIn(f'id="{elem_id}"', body)

        r_cal = client.get("/api/vmaf/calibration")
        self.assertEqual(r_cal.status_code, 200)
        self.assertIn("running", r_cal.json())
        self.assertIn("learned", r_cal.json())

        r_enc_est = client.post("/api/encode/estimate", json={"duration": 1800, "width": 1920, "height": 1080})
        self.assertEqual(r_enc_est.status_code, 200)
        self.assertIn("calibrated", r_enc_est.json())


class PerfLearningAndEncodeEtaTests(unittest.TestCase):
    def setUp(self):
        self.base_calib = {
            "schema_version": 1,
            "calibrated_at": 1700000000,
            "encoders": {
                "nvidia:av1": {
                    "speeds_1080p": {"fastest": 260, "fast": 210, "balanced": 150, "slow": 95, "slowest": 55},
                    "fps_2160p_balanced": 42,
                    "scale_2160_to_1080": 0.28,
                },
            },
            "vmaf": {
                "fps_1080p": 50.0,
                "fps_2160p": 14.0,
                "gpu_fps_1080p": 0.0,
                "gpu_fps_2160p": 0.0,
                "decode_share": 0.35,
                "overheads": {"psnr": 0.04, "ssim": 0.08, "ms_ssim": 0.16, "cambi": 0.25, "xpsnr": 0.14},
                "combo_scale": 0.85,
            },
            "ssimulacra2": {"available": True, "sec_per_pair_1080p": 0.45, "sec_per_pair_2160p": 1.35},
            "reference": {"tonemap_fps_1080p": 45.0},
        }

    def test_effective_profile_blends_history_and_works_without_raw_calib(self):
        import time as _t
        now = _t.time()
        samples = [
            {"ts": now, "kind": "encode", "source_kind": "full_encode", "platform": "nvidia", "codec": "av1",
             "speed": "balanced", "width": 1920, "height": 1080, "fps": 24.0, "media_sec": 3600.0,
             "elapsed_sec": 432.0, "fps_measured": 200.0},
            {"ts": now, "kind": "encode", "source_kind": "vmaf_test", "platform": "nvidia", "codec": "av1",
             "speed": "balanced", "width": 1920, "height": 1080, "fps": 24.0, "media_sec": 40.0,
             "elapsed_sec": 4.6, "fps_measured": 208.0},
            {"ts": now, "kind": "vmaf_metric", "source_kind": "vmaf_test", "width": 1920, "height": 1080,
             "fps": 24.0, "media_sec": 40.0, "elapsed_sec": 12.0, "fps_measured": 80.0,
             "metrics_mask": "psnr,ssim", "subsample": 1, "vmaf_gpu": 0},
            {"ts": now, "kind": "s2", "source_kind": "vmaf_test", "width": 1920, "height": 1080,
             "fps": 24.0, "media_sec": 6.0, "elapsed_sec": 1.8, "fps_measured": 0.30},
        ]
        # 1) Fusion mit Grundtest bewegt balanced FPS nach oben (zwischen 150 und ~200)
        blended = vmaf_calib.effective_profile(calib=self.base_calib, samples=samples)
        self.assertEqual(blended["basis"], "calibration+history")
        self.assertEqual(blended["learned"]["full_encodes"], 1)
        self.assertEqual(blended["learned"]["vmaf_tests"], 1)
        bal = blended["encoders"]["nvidia:av1"]["speeds_1080p"]["balanced"]
        self.assertGreater(bal, 165.0)
        self.assertLess(bal, 205.0)
        self.assertGreater(blended["vmaf"]["fps_1080p"], 50.0)
        self.assertLess(blended["ssimulacra2"]["sec_per_pair_1080p"], 0.45)

        # 2) Funktioniert auch komplett ohne Grundtest nur aus Praxis-Samples
        hist_only = vmaf_calib.effective_profile(calib=False, samples=samples)
        self.assertEqual(hist_only["basis"], "history")
        self.assertIn("nvidia:av1", hist_only["encoders"])
        self.assertGreater(hist_only["encoders"]["nvidia:av1"]["speeds_1080p"]["balanced"], 180.0)

    def test_estimate_encode_run_scales_resolution_and_two_pass(self):
        hd = vmaf_calib.estimate_encode_run({
            "duration": 3600, "width": 1920, "height": 1080, "fps": 24,
            "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
        }, calib=self.base_calib)
        uhd = vmaf_calib.estimate_encode_run({
            "duration": 3600, "width": 3840, "height": 2160, "fps": 24,
            "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
        }, calib=self.base_calib)
        tp = vmaf_calib.estimate_encode_run({
            "duration": 3600, "width": 1920, "height": 1080, "fps": 24,
            "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
            "rate_mode": "abr", "two_pass": True,
        }, calib=self.base_calib)
        self.assertTrue(hd["calibrated"])
        self.assertGreater(hd["speed_x"], 4.0)
        self.assertGreater(uhd["encode_sec"], hd["encode_sec"] * 2.5)
        self.assertGreater(tp["encode_sec"], hd["encode_sec"])

    def test_history_records_perf_samples_from_encode_and_vmaf(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from core import config, history

        old_dir = config.DATA_DIR
        old_conn = history._conn
        try:
            with tempfile.TemporaryDirectory() as td:
                config.DATA_DIR = Path(td)
                history._conn = None
                history.init_db()
                item = SimpleNamespace(
                    id="job_perf_1",
                    title="Movie.mkv",
                    path="/media/Movie.mkv",
                    status="done",
                    settings=SimpleNamespace(
                        platform="nvidia", codec="av1", rate_mode="vmaf",
                        quality=28, encoder_speed="balanced", video_mode="encode",
                        remux_only=False, samples=2, clip_seconds=20,
                        ssimulacra2_frames=3, vmaf_psnr=True, vmaf_ssim=True,
                        vmaf_ms_ssim=False, vmaf_xpsnr=False, vmaf_cambi=False,
                        vmaf_subsample=1, vmaf_gpu=False, tonemap=False,
                    ),
                    info={"width": 1920, "height": 1080, "fps": 24.0, "duration": 1200.0},
                    encode_seconds=200.0,
                    original_size=2_000_000_000,
                    output_size=800_000_000,
                    saved_bytes=1_200_000_000,
                    created_at=1700000000.0,
                    output_path="/out/Movie.mkv",
                    steps=[],
                    details={},
                    report={},
                    vmaf={
                        "results": [
                            {
                                "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
                                "rate_mode": "cq", "value": 28, "vmaf": 95.1,
                                "timing": {"encode_sec": 6.0, "vmaf_sec": 18.0, "s2_sec": 2.4},
                            }
                        ]
                    },
                )
                history.record_job(item, duration=230.0)
                rows = history.list_perf_samples()
                kinds = {r["kind"] for r in rows}
                self.assertEqual(kinds, {"encode", "vmaf_metric", "s2"})
                enc_sources = {r["source_kind"] for r in rows if r["kind"] == "encode"}
                self.assertEqual(enc_sources, {"full_encode", "vmaf_test"})
                history._conn.close()
        finally:
            history._conn = old_conn
            config.DATA_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
