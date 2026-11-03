"""Queue detail/report integration with isolated filesystem and scheduler."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import queue_manager as qm
from core.encoder import EncodeRunner, command_details


def item(**kwargs):
    return qm.QueueItem(id="test", path="source.mkv", title="Source", settings=qm.JobSettings(),
                        group_id="group", **kwargs)


def manager():
    result = qm.QueueManager.__new__(qm.QueueManager)
    result._lock = threading.RLock()
    result._items = []
    result._wake = threading.Event()
    result._active = {}
    result._cancel_ids = set()
    result._status_msg = ""
    result._max_parallel = 1
    return result


def info(path="source.mkv"):
    data = {"path": str(path), "codec": "h264", "width": 1920, "height": 1080,
            "duration": 100.0, "size_bytes": 1000, "audio": [], "subtitles": [],
            "dolby_vision": False, "hdr10_plus": False}
    return SimpleNamespace(path=str(path), duration=100.0, to_dict=lambda: data)


class CommandDetailsTests(unittest.TestCase):
    def test_native_values_not_encoder_defaults(self):
        detail = command_details(["ffmpeg", "-i", "in.mkv", "-c:v", "av1_nvenc", "out.mkv"], "out.mkv")
        self.assertEqual(detail["encoder"], "av1_nvenc")
        self.assertEqual(detail["backend"], "nvidia")
        self.assertIsNone(detail["preset"])
        self.assertIsNone(detail["b_frames"])
        self.assertTrue(all(v is None for v in detail["rate_control"].values()))

    def test_primary_output_excludes_mobile_options(self):
        cmd = ["custom-ffmpeg", "-hwaccel", "cuda", "-i", "in.mkv", "-map", "0:v:0",
               "-c:v", "hevc_nvenc", "-preset", "p6", "-cq", "21", "-multipass", "fullres",
               "-vf", "tonemap_cuda=tonemap=hable,scale_cuda=-2:720", "-bf", "3", "-g", "240",
               "main.mkv", "-c:v", "libx264", "-preset", "fast", "mobile.mp4"]
        detail = command_details(cmd, "main.mkv")
        self.assertEqual(detail["argv"], cmd)
        self.assertEqual(detail["encoder"], "hevc_nvenc")
        self.assertEqual(detail["preset"], "p6")
        self.assertEqual(detail["rate_control"]["cq"], "21")
        self.assertEqual(detail["tonemap_engine"], "cuda")
        self.assertEqual(detail["hardware"]["decode"], "cuda")
        self.assertEqual(detail["multipass"], "fullres")
        self.assertEqual(detail["keyint"], "240")
        json.dumps(detail, allow_nan=False)

    def test_repeated_maps_negative_values_and_native_params(self):
        detail = command_details(["ffmpeg", "-i", "in.mkv", "-map", "0:a:1", "-map", "0:s?",
                                  "-map_chapters", "-1", "-c:v", "libsvtav1", "-crf", "24",
                                  "-svtav1-params", "film-grain=8:tune=0", "out.mkv"], "out.mkv")
        self.assertEqual(detail["maps"], ["0:a:1", "0:s?"])
        self.assertEqual(detail["options"]["-map_chapters"], ["-1"])
        self.assertEqual(detail["encoder_params"]["svtav1-params"], "film-grain=8:tune=0")

    def test_runner_reports_command_before_process(self):
        events = []
        runner = EncodeRunner(on_command=lambda cmd: events.append(cmd))
        with patch("core.encoder.subprocess.Popen", side_effect=OSError("missing binary")):
            with self.assertRaises(OSError):
                runner.run(["missing-ffmpeg", "out.mkv"], 1)
        self.assertEqual(events, [["missing-ffmpeg", "out.mkv"]])


class QueueDetailsTests(unittest.TestCase):
    def test_live_phase_totals_and_legacy_compatibility(self):
        job = item()
        with patch.object(qm.time, "time", return_value=100):
            qm._begin_step(job, "Zwei-Pass · Analyse", attempt=2)
        with patch.object(qm.time, "time", return_value=105):
            data = job.to_dict()
            self.assertEqual(data["phase"]["key"], "encode.pass1")
            self.assertEqual(data["phase_seconds"], {"encode.pass1": 5})
            self.assertTrue(data["phase"]["running"])
            qm._begin_step(job, "Zwei-Pass · Encode")
        with patch.object(qm.time, "time", return_value=110):
            qm._end_step(job)
        self.assertIsNone(job.to_dict()["phase"])
        self.assertEqual(job.to_dict()["phase_seconds"], {"encode.pass1": 5, "encode.pass2": 5})
        self.assertEqual(job.steps[0]["context"], {"attempt": 2})
        self.assertEqual(qm._steps_from_saved([{"name": "Old", "seconds": "2"}]),
                         [{"name": "Old", "seconds": 2.0}])

    def test_capture_attempts_fallbacks_and_bounded_history(self):
        job = item()
        job.details["requested"] = dict(asdict(job.settings), tonemap_engine="cuda")
        job.verify_attempts = 2
        qm._begin_step(job, "Encode", "encode.main", quality=24)
        cmd = ["ffmpeg", "-i", job.path, "-c:v", "libsvtav1", "-vf", "tonemap=tonemap=hable", "out.mkv"]
        for _ in range(65):
            qm._capture_command(job, cmd, "out.mkv")
        self.assertEqual(len(job.details["commands"]), 60)
        self.assertEqual(job.details["command_count"], 65)
        detail = job.details["command"]
        self.assertEqual(detail["attempt"], 2)
        self.assertEqual(detail["phase_key"], "encode.main")
        self.assertEqual(detail["context"]["quality"], 24)
        self.assertEqual(detail["fallbacks"][0]["actual"], "cpu")
        self.assertEqual(job._step_meta["context"]["command_index"], 65)

    def test_two_pass_analysis_reports_real_null_output(self):
        job = item()
        qm._begin_step(job, "Zwei-Pass · Analyse")
        qm._capture_command(job, ["ffmpeg", "-i", job.path, "-c:v", "libx264", "-pass", "1", "NUL"], "out.mkv")
        self.assertEqual(job.details["command"]["output"], "NUL")
        self.assertEqual(job.details["command"]["phase_key"], "encode.pass1")

    def test_command_capture_error_is_visible_and_does_not_abort_runner(self):
        job = item()
        runner = qm._queue_runner(job)
        with self.assertLogs(qm.logger, level="ERROR"), \
             patch.object(qm, "_capture_command", side_effect=ValueError("capture failed")):
            runner.on_command(["ffmpeg"])
        self.assertEqual(job.details["errors"][0]["key"], "command.capture_failed")

    def test_persist_restore_details_report_and_phase_metadata(self):
        job = item(status=qm.STATUS_DONE)
        job.details = {"schema_version": 1, "commands": [{"encoder": "libx264"}]}
        job.report = {"schema_version": 1, "summary": {"status": "warn"}}
        qm._begin_step(job, "Encode", "encode.main", attempt=1)
        qm._end_step(job)
        with tempfile.TemporaryDirectory() as temp:
            original = manager()
            original._items = [job]
            original._persist_path = Path(temp) / "queue.json"
            original._persist()
            saved = json.loads(original._persist_path.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["output_report"], job.report)
            restored = manager()
            restored._persist_path = original._persist_path
            self.assertEqual(restored.restore(), 1)
            copy = restored._items[0]
            self.assertEqual(copy.details, job.details)
            self.assertEqual(copy.report, job.report)
            self.assertEqual(copy.steps, job.steps)
            self.assertEqual(copy.to_dict()["output_report"], copy.to_dict()["report"])
            self.assertIsNone(copy.to_dict()["phase"])

    def test_restore_legacy_report_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            mgr = manager()
            mgr._persist_path = Path(temp) / "queue.json"
            mgr._persist_path.write_text(json.dumps([{"id": "old", "status": qm.STATUS_DONE,
                                                      "path": "gone.mkv", "report": {"checks": []}}]), encoding="utf-8")
            self.assertEqual(mgr.restore(), 1)
            self.assertEqual(mgr._items[0].report, {"checks": []})

    def test_chunk_status_uses_stable_keys_and_context(self):
        job = item()
        for text, key in [("Chunked: 3 Segmente werden analysiert …", "chunk.analysis"),
                          ("Chunked: Segment 2/3 (CQ 27) …", "chunk.encode"),
                          ("Chunked: Segmente werden zusammengefügt …", "chunk.concat"),
                          ("Chunked: Audio/Untertitel werden gemuxt …", "chunk.mux")]:
            qm._chunk_status(job, text)
            self.assertEqual(job.to_dict()["phase"]["key"], key)
        self.assertEqual(job.steps[1]["context"]["chunk"], 2)

    def test_report_generation_failure_is_a_failed_report(self):
        job = item()
        with self.assertLogs(qm.logger, level="ERROR"), \
             patch.object(qm.output_report, "build_plan", side_effect=ValueError("plan broken")):
            qm.QueueManager._run_output_report(job, info(), Path("out.mkv"))
        self.assertEqual(job.report["summary"]["status"], "fail")
        self.assertEqual(job.report["errors"][0]["key"], "report.generation")
        self.assertIn("plan broken", job.report["errors"][0]["error"])
        self.assertIsNone(job.to_dict()["phase"])

    def test_report_preserves_probe_errors(self):
        job = item()
        with patch.object(qm, "probe_with_error", return_value=(None, "cannot read output")), \
             patch.object(qm.output_report, "probe_chapters", return_value=(None, "chapter probe failed")):
            qm.QueueManager._run_output_report(job, info(), Path("out.mkv"))
        self.assertEqual(job.report["summary"]["status"], "fail")
        self.assertEqual(job.report["probe_error"], "cannot read output")
        self.assertEqual(job.report["chapter_error"], "chapter probe failed")

    def test_copy_job_builds_report_before_postprocessing(self):
        job = item()
        job.settings.video_mode = "edit"
        job.info = info().to_dict()
        mgr = manager()
        mgr._refresh_global_msg = Mock()
        mgr._copy_nfo_beside = Mock()
        events = []
        mgr._run_integrity = lambda *args: events.append("integrity")
        mgr._run_caps = lambda *args: events.append("caps")
        mgr._post_process = lambda *args: events.append("postprocess")
        actual = info("out.mkv")
        fake_runner = Mock(_cancel=False)
        fake_runner.run.return_value = (0, "")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.mkv"
            output.write_bytes(b"output")
            cmd = ["ffmpeg", "-i", job.path, "-c", "copy", str(output)]
            def runner(*args, **kwargs):
                qm._capture_command(job, cmd, output)
                return fake_runner
            original_report = mgr._run_output_report
            def generate(*args):
                events.append("report")
                original_report(*args)
            mgr._run_output_report = generate
            with patch.object(qm, "_queue_runner", side_effect=runner), \
                 patch.object(qm, "_log_job_start"), \
                 patch.object(qm.ff, "add_mkv_statistics_tags", side_effect=lambda _: events.append("statistics")), \
                 patch.object(qm, "probe_with_error", return_value=(actual, None)), \
                 patch.object(qm.output_report, "probe_chapters", return_value=([], None)):
                mgr._run_copy_job(job, info(), cmd, output, "Remux/Bearbeiten")
            self.assertEqual(events, ["integrity", "caps", "statistics", "report", "postprocess"])
            self.assertEqual(job.status, qm.STATUS_DONE)
            self.assertIsNotNone(job.report)
            self.assertEqual(job.details["command"]["phase_key"], "remux.edit")

    def test_split_aggregates_each_output_and_does_not_hide_failure(self):
        job = item()
        job.settings.video_mode = "split"
        def generate(target, source, path):
            target.report = qm.output_report.build_report(
                {}, info(path).to_dict() if str(path) == "first.mkv" else None,
                output_path=str(path), probe_error=None if str(path) == "first.mkv" else "broken second output")
        with patch.object(qm.QueueManager, "_run_output_report", side_effect=generate):
            qm.QueueManager._run_split_reports(job, info(), [Path("first.mkv"), Path("second.mkv")])
        self.assertEqual(job.output_path, "first.mkv")
        self.assertEqual(job.extra_outputs, ["second.mkv"])
        self.assertEqual(job.report["summary"]["status"], "fail")
        self.assertEqual(job.report["summary"]["outputs_audited"], 2)
        self.assertEqual(len(job.report["outputs"]), 2)
        self.assertIn("broken second output", json.dumps(job.report))
        self.assertTrue(all("output_path" in check for check in job.report["checks"]))

    def test_split_discovers_only_new_or_changed_outputs(self):
        from core import remux
        job = item()
        job.settings.video_mode = "split"
        job.settings.edit_spec = {"split_mode": "time", "split_value": 10}
        mgr = manager()
        mgr._refresh_global_msg = Mock()
        mgr._run_split_reports = Mock()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "source.mkv"
            old = Path(temp) / "source_999.mkv"
            old.write_bytes(b"old output")
            created = Path(temp) / "source_000.mkv"
            fake_runner = Mock(_cancel=False)
            def run(*args):
                created.write_bytes(b"new output")
                return 0, ""
            fake_runner.run.side_effect = run
            with patch.object(qm, "_output_path", return_value=base), \
                 patch.object(qm, "_queue_runner", return_value=fake_runner), \
                 patch.object(qm, "_log_job_start"), \
                 patch.object(remux, "build_split_cmd", return_value=(["ffmpeg", "-c", "copy", "pattern"], None)):
                mgr._process_split(job, info())
            self.assertEqual(mgr._run_split_reports.call_args.args[2], [created])

    def test_cap_probe_failure_is_not_reported_as_checked(self):
        job = item()
        job.settings.max_video_bitrate_kbps = 1000
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "out.mkv"
            path.write_bytes(b"output")
            with patch.object(qm.ff, "probe_with_error", return_value=(None, "bad bitrate probe")):
                manager()._run_caps(job, job.settings, path)
        self.assertFalse(job.caps_failed)  # Existing safety behavior was not reinterpreted.
        self.assertFalse(job.details["cap_check"]["checked"])
        self.assertEqual(job.details["errors"][-1]["key"], "caps.bitrate_probe")

    def test_failed_integrity_still_blocks_original_move(self):
        job = item()
        job.settings.post_processing = "archive"
        job.integrity_ok = False
        job.output_path = "out.mkv"
        job.report = qm.output_report.build_report({}, {}, output_path=job.output_path)
        with self.assertLogs(qm.logger, level="WARNING"), patch.object(Path, "replace") as move:
            qm.QueueManager._post_process(job, Path(job.output_path))
        move.assert_not_called()
        self.assertEqual(job.report["checks"][-1]["actual"], "blocked")

    def test_existing_cap_check_is_callable_without_changing_safety(self):
        job = item()
        job.settings.max_output_mb = 1
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "out.mkv"
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            manager()._run_caps(job, job.settings, path)
            self.assertTrue(job.caps_failed)
            self.assertIn("Cap-Warnung", job.error)


if __name__ == "__main__":
    unittest.main()
