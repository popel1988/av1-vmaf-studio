"""Tests für auswählbare VMAF-Zusatztests, Sub-Timings, Diagnose-Kalibrierung & Ordner-Erkennung."""
from __future__ import annotations

import json
from pathlib import Path
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
            with_psnr=False, with_ssim=False, with_ms_ssim=False, with_cambi=False,
            with_xpsnr=False,
        )
        self.assertEqual(pure, [("", False)])

        psnr_only = vmaf_mod._build_vmaf_feature_attempts(
            with_psnr=True, with_ssim=False, with_ms_ssim=False, with_cambi=False,
            with_xpsnr=False,
        )
        self.assertEqual(psnr_only[0], ("feature=name=psnr:", False))
        for feat_str, has_cambi in psnr_only:
            self.assertFalse(has_cambi)
            self.assertNotIn("cambi", feat_str)
            self.assertNotIn("float_ms_ssim", feat_str)
            self.assertNotIn("float_ssim", feat_str)

        cambi_ssim = vmaf_mod._build_vmaf_feature_attempts(
            with_psnr=False, with_ssim=True, with_ms_ssim=False, with_cambi=True,
            with_xpsnr=False,
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

        r_qrank = client.get("/api/stats/quality-ranking")
        self.assertEqual(r_qrank.status_code, 200)
        self.assertIn("rankings", r_qrank.json())
        self.assertIn("head_to_head", r_qrank.json())


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
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                config.DATA_DIR = Path(td)
                history._conn = None
                history.init_db()
                item = SimpleNamespace(
                    id="job_perf_1",
                    title="Movie.mkv",
                    path="/media/Movie.mkv",
                    status="fertig",
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
                        "session": "sess_perf_1",
                        "sample_starts": [(60.0, 20.0), (420.0, 20.0)],
                        "results": [
                            {
                                "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
                                "rate_mode": "cq", "value": 28, "vmaf": 95.1,
                                "timing": {"encode_sec": 6.0, "vmaf_sec": 18.0, "s2_sec": 2.4, "metrics": ["PSNR", "SSIM"]},
                            }
                        ]
                    },
                )
                history.record_job(item, duration=230.0)
                # Doppelter Aufruf aus _save_session darf dank sample_key keine Duplikate erzeugen
                history.record_vmaf_analysis_perf(
                    "sess_perf_1", item.vmaf, src_info=item.info, params={"ssimulacra2_frames": 3},
                    source_path=item.path, source_title=item.title,
                )
                rows = history.list_perf_samples()
                kinds = {r["kind"] for r in rows}
                self.assertEqual(kinds, {"encode", "vmaf_metric", "s2"})
                enc_sources = {r["source_kind"] for r in rows if r["kind"] == "encode"}
                self.assertEqual(enc_sources, {"full_encode", "vmaf_test"})
                self.assertEqual(len(rows), 4)
        finally:
            if history._conn is not None and history._conn is not old_conn:
                history._conn.close()
            history._conn = old_conn
            config.DATA_DIR = old_dir

    def test_quality_ranking_and_head_to_head_duels(self):
        import tempfile
        from pathlib import Path
        from core import config, history

        old_dir = config.DATA_DIR
        old_conn = history._conn
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                config.DATA_DIR = Path(td)
                history._conn = None
                history.init_db()
                # Multi-Encoder-Session auf demselben Clip (Head-to-Head!)
                analysis = {
                    "session": "sess_h2h_1",
                    "sample_starts": [(60.0, 20.0), (300.0, 20.0)],
                    "results": [
                        {
                            "platform": "cpu", "codec": "av1", "encoder_speed": "balanced",
                            "rate_mode": "cq", "value": 28, "vmaf": 95.4, "vmaf_1pct": 89.2,
                            "ms_ssim": 0.992, "ssimulacra2": 82.0, "cambi": 1.1,
                            "video_kbps": 4200, "savings_percent": 62.0, "recommended": True,
                            "timing": {"encode_sec": 80.0, "vmaf_sec": 20.0, "s2_sec": 2.0},
                        },
                        {
                            "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
                            "rate_mode": "cq", "value": 28, "vmaf": 95.1, "vmaf_1pct": 88.0,
                            "ms_ssim": 0.990, "ssimulacra2": 79.5, "cambi": 1.8,
                            "video_kbps": 5000, "savings_percent": 54.0, "recommended": True,
                            "timing": {"encode_sec": 10.0, "vmaf_sec": 20.0, "s2_sec": 2.0},
                        },
                    ],
                }
                history.record_vmaf_analysis_perf(
                    "sess_h2h_1",
                    analysis,
                    src_info={"width": 3840, "height": 2160, "fps": 24.0, "duration": 3600.0, "is_hdr": True},
                    params={"ssimulacra2_frames": 3, "tonemap": False},
                )
                res_all = history.quality_ranking(height_filter=2160, hdr_filter="hdr", min_vmaf=95.0)
                self.assertEqual(res_all["total_samples"], 2)
                self.assertEqual(len(res_all["rankings"]), 2)
                self.assertEqual(len(res_all["head_to_head"]), 1)
                duel = res_all["head_to_head"][0]
                self.assertEqual(duel["a"], "cpu:av1")
                self.assertEqual(duel["b"], "nvidia:av1")
                self.assertLess(duel["bitrate_delta_pct"], -10.0)  # CPU AV1 spart Bitrate ggü. NVIDIA AV1
                self.assertLess(duel["speed_ratio"], 0.3)          # aber langsamer
                # SDR-Filter muss 0 liefern
                res_sdr = history.quality_ranking(hdr_filter="sdr")
                self.assertEqual(res_sdr["total_samples"], 0)
                analysis_cx = {
                    "session": "sess_cx",
                    "sample_windows": [
                        {"role": "peak", "kbps": 18000},
                        {"role": "quiet", "kbps": 2500},
                    ],
                    "results": [
                        {
                            "platform": "cpu", "codec": "av1", "encoder_speed": "balanced",
                            "rate_mode": "cq", "value": 28, "vmaf": 95.0, "recommended": True,
                            "timing": {"encode_sec": 40.0, "vmaf_sec": 10.0, "s2_sec": 1.0},
                            "scene_scores": [
                                {"scene": 0, "vmaf": 92.0, "p1": 84.0, "kbps": 9000},
                                {"scene": 1, "vmaf": 98.0, "p1": 96.0, "kbps": 1800},
                            ],
                        },
                        {
                            "platform": "nvidia", "codec": "hevc", "encoder_speed": "balanced",
                            "rate_mode": "cq", "value": 28, "vmaf": 94.5, "recommended": True,
                            "timing": {"encode_sec": 8.0, "vmaf_sec": 10.0, "s2_sec": 1.0},
                            "scene_scores": [
                                {"scene": 0, "vmaf": 91.5, "p1": 82.0, "kbps": 12000},
                                {"scene": 1, "vmaf": 97.5, "p1": 95.0, "kbps": 2200},
                            ],
                        },
                    ],
                }
                history.record_vmaf_analysis_perf(
                    "sess_cx",
                    analysis_cx,
                    src_info={"width": 3840, "height": 2160, "fps": 24.0, "duration": 3600.0,
                              "bit_rate": 40_000_000, "is_hdr": False},
                    params={"anime": True, "ssimulacra2_frames": 3},
                )
                res_cx = history.quality_ranking(complexity_filter="complex", anime_filter="anime")
                self.assertEqual(res_cx["total_samples"], 2)
                self.assertEqual(res_cx["head_to_head"][0]["complexity"], "complex")
                self.assertLess(res_cx["head_to_head"][0]["bitrate_delta_pct"], 0)
                res_quiet = history.quality_ranking(complexity_filter="quiet")
                self.assertEqual(res_quiet["total_samples"], 2)
                res_dense = history.quality_ranking(density_filter="dense")
                self.assertEqual(res_dense["total_samples"], 4)
                named = "Geheimer Film_2026-11-19_14-37_ab12cd"
                history.record_vmaf_analysis_perf(
                    named,
                    {"results": [{
                        "platform": "cpu", "codec": "av1", "encoder_speed": "balanced",
                        "rate_mode": "cq", "value": 28, "vmaf": 95.0,
                        "video_kbps": 4000, "timing": {"encode_sec": 6.0, "vmaf_sec": 4.0, "s2_sec": 1.0},
                    }]},
                    src_info={"width": 1920, "height": 1080, "fps": 24.0, "duration": 100.0},
                    source_path="/media/Geheimer Film.mkv",
                    source_title="Geheimer Film",
                )
                exported = json.dumps(history.export_quality_exchange(), ensure_ascii=False)
                self.assertNotIn("Geheimer", exported)
                self.assertNotIn("/media/", exported)
                rows = history.list_perf_samples()
                self.assertFalse(any("Geheimer" in json.dumps(r, ensure_ascii=False) for r in rows))
                alias = history._conn.execute(
                    "SELECT source_alias FROM quality_samples WHERE video_kbps=4000"
                ).fetchone()["source_alias"]
                resolved = history.resolve_source_alias(alias)
                self.assertEqual(resolved["title"], "Geheimer Film")
                self.assertEqual(resolved["path"], "/media/Geheimer Film.mkv")
                history.record_vmaf_analysis_perf(
                    "Geheimer Film_2026-11-19_15-01_cd34ef",
                    {"results": [{
                        "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
                        "rate_mode": "cq", "value": 30, "vmaf": 93.0,
                        "video_kbps": 3500, "timing": {"encode_sec": 2.0, "vmaf_sec": 4.0, "s2_sec": 1.0},
                    }]},
                    source_path="/media/Geheimer Film.mkv",
                    source_title="Geheimer Film",
                )
                again = history._conn.execute(
                    "SELECT source_alias FROM quality_samples WHERE video_kbps=3500"
                ).fetchone()["source_alias"]
                self.assertEqual(again, alias)
        finally:
            if history._conn is not None and history._conn is not old_conn:
                history._conn.close()
            history._conn = old_conn
            config.DATA_DIR = old_dir

    def test_encoder_version_grouping_and_upsert(self):
        import tempfile
        from pathlib import Path
        from core import config, history

        old_dir = config.DATA_DIR
        old_prev = config.PREVIEW_DIR
        old_conn = history._conn
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                config.DATA_DIR = Path(td)
                config.PREVIEW_DIR = Path(td) / "previews"
                history._conn = None
                history.init_db()
                a1 = {
                    "session": "sess_ver_1",
                    "results": [{
                        "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
                        "encoder_version": "NVENC RTX 4070 · drv 575.57",
                        "rate_mode": "cq", "value": 28, "vmaf": 94.5, "video_kbps": 4200,
                        "scene_scores": [{"scene": 0, "vmaf": 94.5, "kbps": 4200}],
                    }],
                }
                a2 = {
                    "session": "sess_ver_2",
                    "results": [{
                        "platform": "nvidia", "codec": "av1", "encoder_speed": "balanced",
                        "encoder_version": "NVENC RTX 5080 · drv 610.12",
                        "rate_mode": "cq", "value": 28, "vmaf": 95.8, "video_kbps": 3900,
                        "scene_scores": [{"scene": 0, "vmaf": 95.8, "kbps": 3900}],
                    }],
                }
                history.record_vmaf_analysis_perf("sess_ver_1", a1)
                history.record_vmaf_analysis_perf("sess_ver_2", a2)

                merged = history.quality_ranking(group_by_speed=True, group_by_version=False)
                self.assertEqual(len(merged["rankings"]), 1)
                self.assertEqual(len(merged["rankings"][0]["encoder_versions"]), 2)

                split = history.quality_ranking(group_by_speed=True, group_by_version=True)
                self.assertEqual(len(split["rankings"]), 2)
                vers = {r["encoder_version"] for r in split["rankings"]}
                self.assertEqual(vers, {"NVENC RTX 4070 · drv 575.57", "NVENC RTX 5080 · drv 610.12"})

                # Nachmessung via upsert_metrics ergänzt fehlende Spalten ohne Duplikat
                a1["results"][0]["scene_scores"][0]["ssimulacra2"] = 78.4
                a1["results"][0]["scene_scores"][0]["cambi"] = 1.8
                history.record_vmaf_analysis_perf("sess_ver_1", a1, upsert_metrics=True)
                ex = history.export_quality_exchange()
                self.assertEqual(len(ex), 2)
                r1 = next(x for x in ex if x["encoder_version"] == "NVENC RTX 4070 · drv 575.57")
                self.assertAlmostEqual(r1["ssimulacra2"], 78.4, places=1)
                self.assertAlmostEqual(r1["cambi"], 1.8, places=1)
        finally:
            if history._conn is not None and history._conn is not old_conn:
                history._conn.close()
            history._conn = old_conn
            config.DATA_DIR = old_dir
            config.PREVIEW_DIR = old_prev

    def test_library_incremental_scan(self):
        import os
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock
        from core import config, library
        from core.ffmpeg_utils import VideoInfo

        old_cache = library._CACHE_PATH
        old_by_root = dict(library._by_root)
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                td_p = Path(td)
                media = td_p / "media"
                media.mkdir()
                f_keep = media / "Keep.mkv"
                f_edit = media / "Edit.mkv"
                f_del = media / "Delete.mkv"
                f_keep.write_bytes(b"a" * 1000)
                f_edit.write_bytes(b"b" * 1000)
                f_del.write_bytes(b"c" * 1000)

                library._CACHE_PATH = td_p / "library_scan.json"
                library._by_root.clear()

                probed: list[str] = []

                def fake_probe(path: Path):
                    probed.append(path.name)
                    return VideoInfo(
                        path=str(path), width=1920, height=1080, fps=24.0,
                        duration=600.0, codec="h264", pix_fmt="yuv420p",
                        color_transfer="bt709", color_primaries="bt709",
                        color_space="bt709", bit_rate=8_000_000,
                        size_bytes=path.stat().st_size,
                    ), ""

                def fake_iter(_root, _exts):
                    return sorted(p for p in media.iterdir() if p.is_file())

                def fake_rel(p: Path):
                    return p.name

                with mock.patch("core.config.iter_input_files", side_effect=fake_iter), \
                     mock.patch("core.config.rel_input", side_effect=fake_rel), \
                     mock.patch("core.ffmpeg_utils.probe_with_error", side_effect=fake_probe), \
                     mock.patch("core.history.is_processed", return_value=False):
                    library._run("", {}, incremental=False)
                    self.assertEqual(sorted(probed), ["Delete.mkv", "Edit.mkv", "Keep.mkv"])

                    # 1 behalten, 1 ändern, 1 löschen, 1 neu anlegen
                    probed.clear()
                    f_del.unlink()
                    f_edit.write_bytes(b"b" * 2048)
                    os.utime(f_edit, (time.time() + 10, time.time() + 10))
                    f_new = media / "New.mkv"
                    f_new.write_bytes(b"d" * 1500)

                    library._run("", {}, incremental=True)
                    self.assertEqual(sorted(probed), ["Edit.mkv", "New.mkv"])
                    snap = library.get_cached("")
                    names = sorted(m["name"] for m in snap["matched"])
                    self.assertEqual(names, ["Edit.mkv", "Keep.mkv", "New.mkv"])
                    self.assertEqual(snap["delta"], {"added": 1, "updated": 1, "removed": 1, "reused": 1})
        finally:
            library._CACHE_PATH = old_cache
            library._by_root.clear()
            library._by_root.update(old_by_root)

    def test_vmaf_retest_info_and_run(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from core import config, history, vmaf
        from core.ffmpeg_utils import VideoInfo

        old_prev = config.PREVIEW_DIR
        old_sess = config.VMAF_SESSIONS_DIR
        old_data = config.DATA_DIR
        old_conn = history._conn
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                td_p = Path(td)
                config.DATA_DIR = td_p
                config.PREVIEW_DIR = td_p / "previews"
                config.VMAF_SESSIONS_DIR = td_p / "vmaf_sessions"
                history._conn = None
                history.init_db()

                sess = "test_retest_sess"
                meta_dir = config.PREVIEW_DIR / sess
                clip_dir = config.VMAF_SESSIONS_DIR / sess
                meta_dir.mkdir(parents=True)
                clip_dir.mkdir(parents=True)

                (clip_dir / "source_s0.mkv").write_bytes(b"src")
                (clip_dir / "test_nvidia_av1_28_s0.mkv").write_bytes(b"enc")
                payload = {
                    "session": sess,
                    "title": "Demo",
                    "created": 1700000000.0,
                    "source_path": "",
                    "params": {"target_vmaf": 93.0, "platform": "nvidia"},
                    "source_info": {"width": 1920, "height": 1080, "fps": 24.0, "is_hdr": False},
                    "analysis": {
                        "session": sess,
                        "clip_seconds": 10.0,
                        "source_clips": [{"scene": 0, "file": "source_s0.mkv", "lead": 0.5}],
                        "results": [{
                            "value": 28, "quality": 28, "rate_mode": "cq", "label": "CQ 28",
                            "codec": "av1", "platform": "nvidia", "encoder_speed": "balanced",
                            "vmaf": 94.0, "video_kbps": 3800,
                            "scene_scores": [{"scene": 0, "start": 60.0, "length": 10.0, "vmaf": 94.0, "kbps": 3800}],
                            "screenshots": [{"scene": 0, "clip": "test_nvidia_av1_28_s0.mkv"}],
                        }],
                    },
                }
                (meta_dir / "analysis.json").write_text(json.dumps(payload), encoding="utf-8")

                with mock.patch("core.vmaf._ssimulacra2_ok", return_value=True), \
                     mock.patch("core.vmaf.xpsnr_available", return_value=True), \
                     mock.patch("core.vmaf._measured_source_lead", return_value=0.5):
                    info = vmaf.retest_info(sess)
                    self.assertIsNotNone(info)
                    self.assertTrue(info["can_retest"])
                    self.assertEqual(info["source_mode"], "clips")
                    self.assertTrue(info["metrics_present"]["vmaf"])
                    self.assertIn("ssimulacra2", info["metrics_missing"])
                    self.assertIn("cambi", info["metrics_missing"])

                    fake_vi = VideoInfo(
                        path=str(clip_dir / "source_s0.mkv"), width=1920, height=1080,
                        fps=24.0, duration=10.5, codec="h264", pix_fmt="yuv420p",
                        color_transfer="bt709", color_primaries="bt709",
                        color_space="bt709", bit_rate=8_000_000, size_bytes=1024,
                    )

                    def fake_ref(src_info, tmp, *args, **kwargs):
                        p = tmp / "reference_0.mkv"
                        p.write_bytes(b"ref")
                        return p

                    with mock.patch("core.vmaf._butteraugli_ok", return_value=True), \
                         mock.patch("core.ffmpeg_utils.ffprobe", return_value=fake_vi), \
                         mock.patch("core.vmaf._extract_reference", side_effect=fake_ref), \
                         mock.patch("core.vmaf._vmaf_metrics", return_value={"vmaf": 94.0, "cambi": 1.4, "ms_ssim": 0.9912}), \
                         mock.patch("core.vmaf._image_metrics_clip", return_value=(
                             {"mean": 81.5, "min": 79.0, "n": 3, "scores": [81.5]},
                             {"mean": 0.742, "max": 0.891, "p3": 0.412, "n": 3, "scores": [0.742]},
                         )):
                        vmaf._run_retest(sess, {"cambi": True, "ms_ssim": True, "ssimulacra2": True, "butteraugli": True})

                st = vmaf.get_retest_state()
                self.assertFalse(st["running"])
                self.assertEqual(st["error"], "")
                self.assertEqual(st["updated_clips"], 1)
                reloaded = json.loads((meta_dir / "analysis.json").read_text(encoding="utf-8"))
                res0 = reloaded["analysis"]["results"][0]
                self.assertAlmostEqual(res0["cambi"], 1.4, places=2)
                self.assertAlmostEqual(res0["ms_ssim"], 0.9912, places=4)
                self.assertAlmostEqual(res0["ssimulacra2"], 81.5, places=1)
                self.assertAlmostEqual(res0["butteraugli"], 0.742, places=3)
                self.assertAlmostEqual(res0["butteraugli_max"], 0.891, places=3)
                self.assertAlmostEqual(res0["butteraugli_3norm"], 0.412, places=3)
                ex = history.export_quality_exchange()
                self.assertEqual(len(ex), 1)
                self.assertAlmostEqual(ex[0]["ssimulacra2"], 81.5, places=1)
                self.assertAlmostEqual(ex[0]["butteraugli"], 0.742, places=3)
        finally:
            if history._conn is not None and history._conn is not old_conn:
                history._conn.close()
            history._conn = old_conn
            config.DATA_DIR = old_data
            config.PREVIEW_DIR = old_prev
            config.VMAF_SESSIONS_DIR = old_sess


class ButteraugliAndDoviFastPathTests(unittest.TestCase):
    def test_butteraugli_pair_parsing_and_shared_frame_extraction(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ref = root / "ref.mkv"
            dist = root / "dist.mkv"
            ref.write_bytes(b"r")
            dist.write_bytes(b"d")

            fake_proc = mock.Mock(returncode=0, stdout="0.8421000000\n3-norm: 0.412300\n", stderr="")
            with mock.patch("subprocess.run", return_value=fake_proc):
                parsed = vmaf_mod._butteraugli_pair(ref, dist)
            self.assertIsNotNone(parsed)
            self.assertAlmostEqual(parsed[0], 0.8421, places=4)
            self.assertAlmostEqual(parsed[1], 0.4123, places=4)

            png_calls = []

            def fake_png(src, dest, at, fps):
                png_calls.append((src.name, round(at, 2)))
                dest.write_bytes(b"PNG")
                return True

            with mock.patch("core.vmaf._ssimulacra2_ok", return_value=True), \
                 mock.patch("core.vmaf._butteraugli_ok", return_value=True), \
                 mock.patch("core.vmaf._png_at", side_effect=fake_png), \
                 mock.patch("core.vmaf._ssimulacra2_pair", return_value=84.2), \
                 mock.patch("core.vmaf._butteraugli_pair", return_value=(0.65, 0.31)):
                s2, ba = vmaf_mod._image_metrics_clip(
                    ref, dist, root, "k", 10.0, 24.0, frames=2,
                    want_s2=True, want_ba=True,
                )
            # 2 Zeitpunkte × (1x ref + 1x dist) = 4 PNG-Extraktionen trotz 2 Metriken!
            self.assertEqual(len(png_calls), 4)
            self.assertAlmostEqual(s2["mean"], 84.2, places=2)
            self.assertAlmostEqual(ba["mean"], 0.65, places=3)
            self.assertAlmostEqual(ba["p3"], 0.31, places=3)

    def test_butteraugli_flag_in_settings_and_ui_template(self):
        from core.queue_manager import build_job_settings
        js = build_job_settings({"vmaf_butteraugli": True})
        self.assertTrue(js.vmaf_butteraugli)
        js_def = build_job_settings({})
        self.assertFalse(js_def.vmaf_butteraugli)
        html = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn("{{ prefix }}-butteraugli", html)

    def test_dovi_mkv_fast_path_extract_and_fallback(self):
        import tempfile
        from core import dolby_vision as dv
        from core.ffmpeg_utils import VideoInfo

        dv._dovi_version.cache_clear()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="dovi_tool 2.3.4\n", stderr="")):
            self.assertTrue(dv.supports_mkv_fast_path())

        dv._dovi_version.cache_clear()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="dovi_tool 2.3.3\n", stderr="")):
            self.assertFalse(dv.supports_mkv_fast_path())
        dv._dovi_version.cache_clear()

        dual_info = VideoInfo(
            path="movie.mkv", width=3840, height=2160, fps=23.976, duration=120.0,
            codec="hevc", pix_fmt="yuv420p10le", color_transfer="smpte2084",
            color_primaries="bt2020", color_space="bt2020nc", bit_rate=50_000_000, size_bytes=1000,
            video_tracks=[
                {"index": 0, "stream_index": 0, "role": "base", "dolby_vision": False},
                {"index": 1, "stream_index": 2, "role": "enhancement", "dolby_vision": True, "dv_rpu": True},
            ],
        )
        self.assertEqual(dv._mkv_rpu_track_number(dual_info), 3)

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "dual.mkv"
            rpu = Path(td) / "out.rpu.bin"
            src.write_bytes(b"mkv")
            seen_cmds = []

            def fake_run(cmd, **kw):
                seen_cmds.append(cmd)
                rpu.write_bytes(b"RPU_DATA")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("core.dolby_vision.supports_mkv_fast_path", return_value=True), \
                 mock.patch("core.ffmpeg_utils.ffprobe", return_value=dual_info), \
                 mock.patch("subprocess.run", side_effect=fake_run):
                ok = dv._extract_rpu(src, "hevc", rpu, mode=2)
            self.assertTrue(ok)
            self.assertEqual(len(seen_cmds), 1)
            self.assertIn("-t", seen_cmds[0])
            self.assertEqual(seen_cmds[0][seen_cmds[0].index("-t") + 1], "3")
            self.assertIn("-m", seen_cmds[0])

    def test_dv_lab_p7_to_81_uses_mkv_fast_path_and_falls_back(self):
        import tempfile
        from core import dv_lab
        from core.ffmpeg_utils import VideoInfo

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "movie.mkv"
            src.write_bytes(b"mkv")
            vi = VideoInfo(
                path=str(src), width=3840, height=2160, fps=23.976, duration=120.0,
                codec="hevc", pix_fmt="yuv420p10le", color_transfer="smpte2084",
                color_primaries="bt2020", color_space="bt2020nc", bit_rate=50_000_000, size_bytes=1000,
                dolby_vision=True, dv_profile=7,
                video_tracks=[{"index": 0, "stream_index": 0, "role": "base", "dolby_vision": True, "dv_rpu": True}],
            )

            def fake_fast(source, dest_es, *, discard_el=True):
                dest_es.write_bytes(b"HEVC81")
                return True, ""

            def fake_mux(injected, source_path, out_path, *a, **kw):
                out_path.write_bytes(b"OUT_MKV")
                return True, ""

            with mock.patch("core.ffmpeg_utils.ffprobe", return_value=vi), \
                 mock.patch("core.dolby_vision.available", return_value=True), \
                 mock.patch("core.dolby_vision.supports_mkv_fast_path", return_value=True), \
                 mock.patch("core.dolby_vision.convert_mkv_to_81_es", side_effect=fake_fast) as m_fast, \
                 mock.patch("core.dv_lab._demux_base_hevc") as m_demux, \
                 mock.patch("core.dv_lab._mux_injected_hevc", side_effect=fake_mux):
                res = dv_lab.make_profile7_tv_stream(src)
            self.assertTrue(res["ok"])
            self.assertTrue(res["fast_path"])
            m_fast.assert_called_once()
            m_demux.assert_not_called()

            # Fallback wenn MKV-Fast-Path fehlschlägt
            def fake_rpu(source, codec, rpu, mode=0, **kw):
                rpu.write_bytes(b"RPU")
                return True

            def fake_demux(source, dest, **kw):
                dest.write_bytes(b"\x00\x00\x00\x01\x02\x01abc")
                return True, ""

            def fake_inj(cmd, **kw):
                out_p = Path(cmd[cmd.index("-o") + 1])
                out_p.write_bytes(b"INJ")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("core.ffmpeg_utils.ffprobe", return_value=vi), \
                 mock.patch("core.dolby_vision.available", return_value=True), \
                 mock.patch("core.dolby_vision.supports_mkv_fast_path", return_value=True), \
                 mock.patch("core.dolby_vision.convert_mkv_to_81_es", return_value=(False, "unsupported")), \
                 mock.patch("core.dolby_vision._extract_rpu", side_effect=fake_rpu), \
                 mock.patch("core.dv_lab._demux_base_hevc", side_effect=fake_demux) as m_demux2, \
                 mock.patch("subprocess.run", side_effect=fake_inj), \
                 mock.patch("core.dv_lab._mux_injected_hevc", side_effect=fake_mux):
                res2 = dv_lab.make_profile7_tv_stream(src)
            self.assertTrue(res2["ok"])
            self.assertFalse(res2["fast_path"])
            m_demux2.assert_called_once()




class HdrVmafAndLibvaTests(unittest.TestCase):
    def _info(self, **kw):
        from core.ffmpeg_utils import VideoInfo
        base = dict(
            path="film.mkv", width=1920, height=1080, duration=60.0,
            codec="hevc", pix_fmt="yuv420p10le", color_transfer="smpte2084",
            color_primaries="bt2020", color_space="bt2020nc",
            bit_rate=8_000_000, size_bytes=60_000_000, color_range="tv",
        )
        base.update(kw)
        return VideoInfo(**base)

    def test_hdr_flag_defaults_on_and_template_exposes_it(self):
        from core.queue_manager import build_job_settings
        self.assertTrue(build_job_settings({}).vmaf_hdr)
        self.assertFalse(build_job_settings({"vmaf_hdr": False}).vmaf_hdr)
        html = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="{{ prefix }}-hdr-vmaf" checked', html)
        self.assertIn('id="faq-hdr-vmaf"', html)

    def test_model_selection_respects_channel_tonemap_and_zscale(self):
        from core import config
        info = self._info()
        info4k = self._info(width=3840, height=2160)
        sdr = self._info(color_transfer="bt709")
        with mock.patch.object(config, "IMAGE_CHANNEL", "latest"), \
             mock.patch.object(config, "VMAF_MODEL_HDR_1080P", "hdr_vmaf_v1.1.8_3d0h.json"), \
             mock.patch.object(config, "VMAF_MODEL_HDR_4K", "hdr_vmaf_v1.1.8_1d5h_2160.json"), \
             mock.patch.object(vmaf_mod, "_ffmpeg_has_zscale", return_value=True), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(Path, "exists", return_value=True):
            name, _path = vmaf_mod._model_for(info, True, tonemap=False, hdr_vmaf=True)
            self.assertEqual(name, "hdr_vmaf_v1.1.8_3d0h.json")
            name4, _path4 = vmaf_mod._model_for(info4k, True, tonemap=False, hdr_vmaf=True)
            self.assertEqual(name4, "hdr_vmaf_v1.1.8_1d5h_2160.json")
            mapped, _ = vmaf_mod._model_for(info, True, tonemap=True, hdr_vmaf=True)
            self.assertEqual(mapped, config.VMAF_MODEL_1080P_NEG)
            off, _ = vmaf_mod._model_for(info, True, tonemap=False, hdr_vmaf=False)
            self.assertEqual(off, config.VMAF_MODEL_1080P_NEG)
            sdr_name, _ = vmaf_mod._model_for(sdr, True, tonemap=False, hdr_vmaf=True)
            self.assertEqual(sdr_name, config.VMAF_MODEL_1080P_NEG)
        with mock.patch.object(config, "IMAGE_CHANNEL", "legacy"), \
             mock.patch.object(vmaf_mod, "_ffmpeg_has_zscale", return_value=True), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(Path, "exists", return_value=True):
            legacy_name, _ = vmaf_mod._model_for(info, True, tonemap=False, hdr_vmaf=True)
            self.assertFalse(str(legacy_name).lower().startswith("hdr_vmaf"))
        with mock.patch.object(config, "IMAGE_CHANNEL", "latest"), \
             mock.patch.object(config, "VMAF_MODEL_HDR_1080P", "hdr_vmaf_v1.1.8_3d0h.json"), \
             mock.patch.object(vmaf_mod, "_ffmpeg_has_zscale", return_value=False), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(Path, "exists", return_value=True):
            no_z, _ = vmaf_mod._model_for(info, True)
            self.assertEqual(no_z, config.VMAF_MODEL_1080P_NEG)

    def test_prepare_model_strips_conversion_target_only_on_the_copy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "hdr_vmaf_v1.1.8_3d0h.json"
            src.write_text(json.dumps({
                "model_dict": {"conversion_target": {"fmt": "yuv420p16le"}, "feature": "x"},
            }), encoding="utf-8")
            out, target = vmaf_mod._prepare_hdr_model_file(src, Path(td) / "work")
            self.assertEqual(target["fmt"], "yuv420p16le")
            stripped = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotIn("conversion_target", stripped["model_dict"])
            original = json.loads(src.read_text(encoding="utf-8"))
            self.assertIn("conversion_target", original["model_dict"])

    def test_zscale_filter_uses_source_transfer_and_ictcp_pq(self):
        pq = vmaf_mod._hdr_zscale_filter(self._info())
        self.assertIn("tin=smpte2084", pq)
        self.assertIn("m=ictcp", pq)
        self.assertTrue(pq.endswith("format=yuv420p16le"))
        hlg = vmaf_mod._hdr_zscale_filter(self._info(color_transfer="arib-std-b67"))
        self.assertIn("tin=arib-std-b67", hlg)
        self.assertIn("t=smpte2084", hlg)

    def test_status_legacy_disables_hdr_vmaf(self):
        from core import config
        with mock.patch.object(config, "IMAGE_CHANNEL", "legacy"):
            st = vmaf_mod.hdr_vmaf_status()
        self.assertFalse(st["available"])
        self.assertEqual(st["channel"], "legacy")
        self.assertIn("Legacy", st["reason"])

    def test_libva_diag_reports_api_and_package_version(self):
        from core import diagnostics as diag
        fake = mock.Mock(
            returncode=0,
            stdout="VA-API version: 1.24\nDriver version: Intel iHD driver 25.1\n",
            stderr="",
        )
        with mock.patch.dict("os.environ", {"LIBVA_VERSION": "2.24.1"}, clear=False), \
             mock.patch("core.diagnostics.shutil.which", return_value="vainfo"), \
             mock.patch("core.diagnostics.subprocess.run", return_value=fake), \
             mock.patch("pathlib.Path.exists", return_value=True):
            stat, detail = diag._libva_diag()
        self.assertEqual(stat, "ok")
        self.assertIn("VA-API 1.24", detail)
        self.assertIn("libva 2.24.1", detail)
        self.assertIn("Intel iHD", detail)

    def test_hdr_run_converts_with_zscale_and_skips_libvmaf_cuda(self):
        import tempfile
        from core import config
        info = self._info()
        captured = {}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "hdr_vmaf_v1.1.8_3d0h.json"
            model.write_text(json.dumps({
                "model_dict": {"conversion_target": {"color_space": "ictcp"}},
            }), encoding="utf-8")
            work = root / "job"
            work.mkdir()

            def fake_logged(cmd, label):
                captured["cmd"] = list(cmd)
                captured["label"] = label
                (work / "vmaf_k.json").write_text(json.dumps({
                    "pooled_metrics": {"vmaf": {"mean": 96.0}},
                    "frames": [{"metrics": {"vmaf": 96.0}}],
                }), encoding="utf-8")
                return mock.Mock(returncode=0)

            with mock.patch.object(config, "IMAGE_CHANNEL", "latest"), \
                 mock.patch.object(config, "VMAF_MODEL_DIR", root), \
                 mock.patch.object(config, "VMAF_MODEL_HDR_1080P", model.name), \
                 mock.patch.object(vmaf_mod, "_ffmpeg_has_zscale", return_value=True), \
                 mock.patch.object(vmaf_mod, "vmaf_gpu_status", return_value={"available": True, "mode": "libvmaf_cuda"}), \
                 mock.patch.object(vmaf_mod.ff, "_probe_duration", return_value=2.0), \
                 mock.patch.object(vmaf_mod, "_run_logged", side_effect=fake_logged):
                data = vmaf_mod._run_libvmaf(
                    root / "dist.mkv", root / "ref.mkv", info, work, "k",
                    True, None, features="", clip_len=2.0, with_xpsnr=False,
                    gpu_accel=True, tonemap=False, hdr_vmaf=True,
                )
            self.assertIsNotNone(data)
            cmd = captured["cmd"]
            fc = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("zscale=", fc)
            self.assertIn("m=ictcp", fc)
            self.assertIn("format=yuv420p16le", fc)
            self.assertIn("libvmaf=", fc)
            self.assertNotIn("libvmaf_cuda", fc)
            self.assertIn("-hwaccel", cmd)
            self.assertIn("hdr-vmaf", captured["label"])
            self.assertEqual(data["model_name"], model.name)
            copies = list(work.glob("_ff_*.json"))
            self.assertTrue(copies)
            self.assertNotIn(
                "conversion_target",
                json.loads(copies[0].read_text(encoding="utf-8"))["model_dict"],
            )


if __name__ == "__main__":
    unittest.main()
