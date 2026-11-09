"""Export safety without real encoders, user media, or desktop side effects."""
from dataclasses import asdict
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from core import export_preflight as pf, queue_manager as qm
from core.encoder import build_encode_cmd
from core.ffmpeg_utils import VideoInfo


def media(path):
    return VideoInfo(str(path), 1920, 1080, 300, "hevc", "yuv420p10le", "", "", "",
                     1_000_000, 1024, fps=24)


def manager():
    result = qm.QueueManager.__new__(qm.QueueManager)
    result._lock = threading.RLock()
    result._items = []
    result._active = {}
    result._wake = threading.Event()
    result._cancel_ids = set()
    result._persist = Mock()
    result._refresh_global_msg = Mock()
    return result


class ExportPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "film.mkv"
        self.src.write_bytes(b"original")
        self.info = media(self.src)
        self.settings = qm.JobSettings(out_mode="beside", vmaf_check=False)

    def report(self, **kwargs):
        return pf.inspect(self.src, self.settings, info=self.info, **kwargs)

    def codes(self, report):
        return {x["code"] for x in report["blockers"]}

    def test_unresolved_vmaf_test_is_not_advertised_as_exact(self):
        self.settings.vmaf_check = True
        self.assertFalse(pf.test_support(self.settings, self.info.to_dict())["supported"])

    def test_shutdown_cancels_waits_and_persists_final_state(self):
        queue = manager()
        queue._thread = Mock()
        runner = Mock()
        queue._active = {"active": runner}
        runner.cancel.side_effect = lambda: queue._active.clear()
        queue.shutdown()
        queue._thread.join.assert_called_once()
        runner.cancel.assert_called_once()
        self.assertTrue(queue._stop)
        self.assertIn("active", queue._cancel_ids)
        queue._persist.assert_called_once()
        with patch.object(qm, "ffprobe", return_value=self.info):
            self.assertIsNone(queue.add_file(str(self.src), self.settings))

    def test_existing_requires_explicit_overwrite(self):
        (self.root / "film_av1.mkv").write_bytes(b"existing")
        self.assertIn("overwrite_consent_required", self.codes(self.report()))
        self.settings.on_duplicate = "overwrite"
        self.assertTrue(self.report()["ok"])
        self.assertEqual(self.src.read_bytes(), b"original")

    def test_same_source_target_always_blocked_even_with_overwrite(self):
        self.settings.suffix = ""
        self.settings.on_duplicate = "overwrite"
        self.assertIn("source_is_destination", self.codes(self.report()))

    def test_hardlink_to_source_is_blocked(self):
        try:
            (self.root / "film_av1.mkv").hardlink_to(self.src)
        except OSError:
            self.skipTest("hardlinks unavailable")
        self.settings.on_duplicate = "overwrite"
        self.assertIn("source_is_destination", self.codes(self.report()))

    def test_inplace_requires_separate_consent_and_reserves_final_path(self):
        self.settings.post_processing = "inplace"
        self.assertIn("inplace_consent_required", self.codes(self.report()))
        self.settings.approve_inplace = True
        report = self.report()
        self.assertTrue(report["ok"])
        self.assertEqual({x["role"] for x in report["outputs"]}, {"main", "replacement"})

    def test_inplace_extension_change_checks_other_existing_target(self):
        self.settings.post_processing = "inplace"
        self.settings.approve_inplace = True
        self.settings.container = "mp4"
        (self.root / "film.mp4").write_bytes(b"do not replace")
        self.assertIn("overwrite_consent_required", self.codes(self.report()))

    def test_mobile_output_has_collision_check(self):
        self.settings.mobile_copy = True
        (self.root / "film_av1_mobile.mp4").write_bytes(b"old mobile")
        self.assertIn("overwrite_consent_required", self.codes(self.report()))

    def test_split_family_collision_and_reservation(self):
        self.settings.video_mode = "split"
        (self.root / "film_001.mkv").write_bytes(b"part")
        self.assertIn("overwrite_consent_required", self.codes(self.report()))
        self.settings.on_duplicate = "overwrite"
        report = self.report(reservations=[{"path": str(self.root / "film_005.mkv")}])
        self.assertIn("destination_reserved", self.codes(report))

    def test_split_ranges_duplicate_names_blocked(self):
        self.settings.video_mode = "split"
        self.settings.edit_spec = {"split_mode": "range", "split_ranges": [
            {"start": 1, "end": 2, "title": "same"},
            {"start": 3, "end": 4, "title": "same"}]}
        self.assertIn("duplicate_destination", self.codes(self.report()))

    def test_archive_destination_needs_overwrite_consent(self):
        self.settings.post_processing = "archive"
        archive = self.root / qm.config.ARCHIVE_DIRNAME
        archive.mkdir()
        (archive / self.src.name).write_bytes(b"archive")
        self.assertIn("overwrite_consent_required", self.codes(self.report()))

    def test_disk_space_is_structured_and_blocks_when_insufficient(self):
        with patch.object(pf.shutil, "disk_usage", return_value=Mock(free=0)):
            report = self.report()
        self.assertIn("disk_space", self.codes(report))
        self.assertTrue(report["disk"][0]["estimate_only"])

    def test_parent_file_is_not_a_directory(self):
        self.settings.out_mode = "default"
        with patch.object(qm.config, "default_output_path", return_value=self.src), patch.object(
                qm.config, "rel_input", return_value="x.mkv"):
            self.assertIn("destination_parent_not_directory", self.codes(self.report()))

    def test_hdr_audio_subtitle_losses_are_reported(self):
        self.info.dolby_vision = True
        self.info.hdr10_plus = True
        self.info.audio = [{"index": 0, "codec": "truehd"}, {"index": 1, "codec": "aac"}]
        self.info.subtitles = [{"index": 0, "codec": "hdmv_pgs_subtitle"}]
        self.settings.container = "mp4"
        self.settings.tonemap = True
        self.settings.audio_tracks = [1]
        codes = {x["code"] for x in self.report()["warnings"]}
        self.assertTrue({"dv_loss", "hdr_loss", "hdr10plus_loss", "audio_removed",
                         "subtitle_container_loss"}.issubset(codes))

    def test_normalized_roundtrip_keeps_hdr(self):
        normalized = qm.build_job_settings({"hdr_mode": "preserve"})
        again = qm.build_job_settings(asdict(normalized))
        self.assertTrue(again.preserve_hdr)
        self.assertFalse(again.tonemap)

    def test_unsupported_dv_never_silently_falls_back(self):
        for raw in ({"dv_mode": "preserve", "codec": "av1", "platform": "nvidia"},
                    {"dv_mode": "preserve", "codec": "vp9"}):
            with self.assertRaises(pf.ExportRejected):
                qm.build_job_settings(raw)

    def test_unsupported_dv_filters_block_preflight(self):
        self.info.dolby_vision = True
        self.settings.preserve_dv = True
        self.settings.preserve_hdr = True
        self.settings.target_height = 720
        self.assertIn("unsupported_dv_filters", self.codes(self.report()))

    def test_queue_reserves_targets_and_protects_inflight_sources(self):
        queue = manager()
        with patch.object(qm, "ffprobe", return_value=self.info):
            first = queue.add_file(str(self.src), self.settings)
            with self.assertRaises(pf.ExportRejected):
                queue.add_file(str(self.src), qm.JobSettings(out_mode="beside", on_duplicate="overwrite"))
            # The reserved output cannot become another job's source.
            target = self.root / "film_av1.mkv"
            target.write_bytes(b"inflight")
            with self.assertRaises(pf.ExportRejected):
                queue.add_file(str(target), qm.JobSettings(out_mode="beside", suffix="_second"))
        self.assertEqual(len(queue._items), 1)
        self.assertEqual(queue._items[0].id, first.id)

    def test_actual_start_rechecks_destination_created_after_enqueue(self):
        queue = manager()
        with patch.object(qm, "ffprobe", return_value=self.info):
            item = queue.add_file(str(self.src), self.settings)
        (self.root / "film_av1.mkv").write_bytes(b"arrived later")
        with patch.object(queue, "_open_source", return_value=(self.info, None)), patch.object(
                queue, "_encode_to") as encode:
            with self.assertRaises(pf.ExportRejected):
                queue._process(item)
        encode.assert_not_called()

    def test_final_settings_rechecked_after_manual_changes(self):
        queue = manager()
        with patch.object(qm, "ffprobe", return_value=self.info):
            item = queue.add_file(str(self.src), self.settings)
        item._export_preflight = lambda: queue._check_export(item, self.info)
        item.settings.suffix = ""
        with self.assertRaises(pf.ExportRejected):
            qm._output_path(item)

    def test_fingerprint_changes_with_source_or_effective_settings(self):
        first = pf.fingerprint(self.src, self.settings)
        self.settings.quality += 1
        self.assertNotEqual(first, pf.fingerprint(self.src, self.settings))
        second = pf.fingerprint(self.src, self.settings)
        self.src.write_bytes(b"changed")
        self.assertNotEqual(second, pf.fingerprint(self.src, self.settings))

    def test_testexport_isolated_uses_same_encode_path_and_has_no_side_effects(self):
        queue = manager()
        self.settings.post_processing = "inplace"  # No approval needed for non-destructive scratch.
        self.settings.mobile_copy = True
        with patch.object(qm.config, "WORK_DIR", self.root / "scratch"), patch.object(
                qm, "ffprobe", return_value=self.info):
            item = queue.add_test_export(str(self.src), self.settings, 42.5, 15)
        def encode(job, settings, info, out, on_progress):
            out.write_bytes(b"sample")
            return 0, "", [], False
        with patch.object(queue, "_open_source", return_value=(self.info, None)), patch.object(
                queue, "_encode_to", side_effect=encode) as run, patch.object(
                queue, "_post_process") as cleanup, patch("core.media_servers.notify_output") as notify:
            queue._process(item)
            queue._record_history(item, 1)
        self.assertEqual(item.status, qm.STATUS_DONE)
        self.assertTrue(Path(item.output_path).is_relative_to(self.root / "scratch"))
        self.assertEqual(self.src.read_bytes(), b"original")
        self.assertEqual(run.call_count, 1)
        cleanup.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(self.settings.test_export, {})

    def test_testexport_rejects_changed_source_and_unsupported_modes(self):
        queue = manager()
        with patch.object(qm.config, "WORK_DIR", self.root / "scratch"), patch.object(
                qm, "ffprobe", return_value=self.info):
            item = queue.add_test_export(str(self.src), self.settings, 1, 10)
            for change in ({"chunked": True}, {"preserve_dv": True}, {"video_mode": "editor"},
                           {"verify_vmaf": True}, {"edit_spec": {"playlist_clips": ["a"]}}):
                candidate = qm.JobSettings(out_mode="beside", **change)
                with self.assertRaises(ValueError):
                    queue.add_test_export(str(self.src), candidate, 1, 10)
        self.src.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "geändert"):
            queue._process_test_export(item, self.info)

    def test_testexport_rejects_invalid_or_outside_ranges(self):
        queue = manager()
        with patch.object(qm, "ffprobe", return_value=self.info):
            for start, seconds in ((-1, 10), (0, 0), (299, 10), (0, 121), (float("nan"), 1)):
                with self.assertRaises(ValueError):
                    queue.add_test_export(str(self.src), self.settings, start, seconds)

    def test_testexport_does_not_inherit_final_skip_or_overwrite(self):
        queue = manager()
        (self.root / "film_av1.mkv").write_bytes(b"final")
        self.settings.on_duplicate = "skip"
        with patch.object(qm.config, "WORK_DIR", self.root / "scratch"), patch.object(
                qm, "ffprobe", return_value=self.info):
            self.assertIsNotNone(queue.add_test_export(str(self.src), self.settings, 0, 10))

    def test_encoder_limits_both_main_and_mobile_outputs(self):
        with patch("core.encoder.ff.available_encoders", return_value={"libsvtav1", "libx264"}):
            cmd = build_encode_cmd(self.info, self.root / "main.mkv", "cpu", "av1", 28,
                                   None, False, start_at=42, duration_limit=15, overwrite=False,
                                   mobile={"path": str(self.root / "mobile.mp4"), "height": 720})
        self.assertEqual(cmd.count("-t"), 2)
        self.assertIn("-n", cmd)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))

    def test_postprocess_never_deletes_original_before_failed_replace(self):
        out = self.root / "tmp.mkv"
        out.write_bytes(b"new")
        self.settings.post_processing = "inplace"
        self.settings.approve_inplace = True
        item = qm.QueueItem("x", "x", str(self.src), self.settings, "g")
        with patch.object(Path, "replace", side_effect=OSError("locked")):
            qm.QueueManager._post_process(item, out)
        self.assertEqual(self.src.read_bytes(), b"original")

    def test_editor_explicit_dv_preserve_rejected(self):
        from core.editor import build_editor_encode_cmd
        cmd, error = build_editor_encode_cmd([], self.root / "out.mkv", preserve_dv=True)
        self.assertEqual(cmd, [])
        self.assertIn("Dolby", error)


AVAILABLE = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx", "jinja2", "multipart", "psutil"))


@unittest.skipUnless(AVAILABLE, "optional API dependencies unavailable")
class ExportApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        from fastapi.testclient import TestClient
        cls.module = app
        cls.client = TestClient(app.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "source.mkv"
        self.src.write_bytes(b"fixture")
        self.info = media(self.src)
        self.resolve = patch.object(self.module, "_safe_resolve", return_value=self.src)
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def test_preflight_contract_and_explicit_dv_rejection(self):
        with patch.object(self.module.ff, "ffprobe", return_value=self.info):
            response = self.client.post("/api/export/preflight", json={
                "path": "source.mkv", "settings": {"out_mode": "beside", "hdr_mode": "preserve"}})
            self.assertEqual(response.status_code, 200)
            self.assertTrue({"settings", "fingerprint", "outputs", "warnings", "blockers",
                             "disk", "test_export"}.issubset(response.json()))
            invalid = self.client.post("/api/export/preflight", json={
                "path": "source.mkv", "settings": {"dv_mode": "preserve", "platform": "nvidia"}})
            self.assertFalse(invalid.json()["ok"])

    def test_enqueue_cannot_bypass_collision_check(self):
        (self.src.parent / "source_av1.mkv").write_bytes(b"old")
        isolated = manager()
        with patch.object(self.module, "queue", isolated), patch.object(qm, "ffprobe", return_value=self.info):
            response = self.client.post("/api/enqueue", json={"path": "source.mkv", "out_mode": "beside"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("preflight", response.json())

    def test_testexport_validation_and_contract(self):
        isolated = manager()
        with patch.object(self.module, "queue", isolated), patch.object(qm, "ffprobe", return_value=self.info):
            result = self.client.post("/api/export/test", json={"path": "source.mkv", "settings": {"vmaf_check": False},
                                                               "start": 42, "seconds": 15})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json()["test_export"])
            self.assertEqual(len(result.json()["fingerprint"]), 64)
            for seconds in (0, 121):
                bad = self.client.post("/api/export/test", json={"path": "source.mkv", "seconds": seconds})
                self.assertEqual(bad.status_code, 422)

    def test_open_folder_only_resolves_job_owned_output(self):
        opener = Mock()
        job = qm.QueueItem("abc", "Source", str(self.src), qm.JobSettings(), "g",
                           output_path=str(self.src))
        with patch.object(self.module.app.state, "desktop_open_folder", opener, create=True), patch.object(
                self.module.queue, "get_item", return_value=job) as lookup:
            response = self.client.post("/api/desktop/jobs/abc/open-folder?path=C:/arbitrary")
        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with("abc")
        opener.assert_called_once_with(str(self.src.parent.resolve()))

    def test_open_folder_unavailable_in_web_and_missing_output(self):
        with patch.object(self.module.app.state, "desktop_open_folder", None, create=True):
            self.assertEqual(self.client.post("/api/desktop/jobs/x/open-folder").status_code, 409)
        with patch.object(self.module.app.state, "desktop_open_folder", Mock(), create=True), patch.object(
                self.module.queue, "get_item", return_value=None):
            self.assertEqual(self.client.post("/api/desktop/jobs/missing/open-folder").status_code, 404)


if __name__ == "__main__":
    unittest.main()
