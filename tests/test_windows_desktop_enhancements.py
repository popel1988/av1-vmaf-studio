"""Regression tests for Windows GPU monitoring, AMF/QSV support, taskbar/sleep
integration, post-queue power actions, folder management, and MOTW unblocking."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import config, ffmpeg_utils as ff, hardware, notify, output_report
from core.desktop_folders import DesktopMediaFolders
from core.desktop_host import DesktopBridge
from core.desktop_paths import DesktopPathGrants
from core.desktop_win32 import (
    TBPF_ERROR,
    TBPF_INDETERMINATE,
    TBPF_NOPROGRESS,
    TBPF_NORMAL,
    TBPF_PAUSED,
    compute_taskbar_status,
    should_prevent_sleep,
    unblock_zone_identifiers,
)


class WindowsGpuTests(unittest.TestCase):
    def test_nvml_reads_encoder_utilization_without_spawning_subprocess(self):
        fake_nvml = Mock()
        fake_nvml.available = True
        fake_nvml.read_gpus.return_value = [{
            "vendor": "nvidia",
            "name": "NVIDIA GeForce RTX 4090",
            "util": 88.0,
            "render_util": 9.0,
            "enc_util": 88.0,
            "dec_util": 34.0,
            "mem_used": 4096.0,
            "mem_total": 24564.0,
            "temperature": 61.0,
        }]
        mon = hardware.HardwareMonitor.__new__(hardware.HardwareMonitor)
        mon._nvml = fake_nvml
        mon._nvidia_smi = "nvidia-smi"
        mon._has_nvidia = True
        mon._drm_cards = []
        mon._dxgi_adapters = []
        mon._pdh = None
        with patch("core.hardware._run") as run_mock:
            gpus = mon._nvidia()
        run_mock.assert_not_called()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].util, 88.0)
        self.assertEqual(gpus[0].enc_util, 88.0)
        self.assertEqual(gpus[0].dec_util, 34.0)
        self.assertEqual(gpus[0].render_util, 9.0)

    def test_windows_dxgi_and_pdh_populate_amd_and_intel_metrics(self):
        fake_pdh = Mock()
        fake_pdh.sample_by_luid.return_value = {
            "0x00000000_0x0001524e": {
                "util": 72.5,
                "render_util": 12.0,
                "enc_util": 72.5,
                "dec_util": 15.0,
                "mem_used": 1520.0,
            },
            "0x00000000_0x00019999": {
                "util": 41.0,
                "render_util": 41.0,
                "enc_util": 0.0,
                "dec_util": 0.0,
                "mem_used": 640.0,
            },
        }
        mon = hardware.HardwareMonitor.__new__(hardware.HardwareMonitor)
        mon._dxgi_adapters = [
            {"vendor": "nvidia", "name": "RTX 4090", "vram_mb": 24564.0, "luid": "0x00000000_0x00013dd6"},
            {"vendor": "amd", "name": "AMD Radeon RX 7900 XTX", "vram_mb": 24576.0, "luid": "0x00000000_0x0001524e"},
            {"vendor": "intel", "name": "Intel Arc A380", "vram_mb": 6144.0, "luid": "0x00000000_0x00019999"},
        ]
        mon._pdh = fake_pdh
        mon._drm_cards = []
        mon._has_nvidia = True

        gpus = mon._windows_non_nvidia()
        self.assertEqual([g.vendor for g in gpus], ["amd", "intel"])
        self.assertEqual(gpus[0].name, "AMD Radeon RX 7900 XTX")
        self.assertEqual(gpus[0].enc_util, 72.5)
        self.assertEqual(gpus[0].mem_used, 1520.0)
        self.assertEqual(gpus[1].name, "Intel Arc A380")
        self.assertEqual(gpus[1].util, 41.0)

        # Platform priority must always keep NVIDIA ahead of secondary iGPUs
        self.assertEqual(mon.available_platforms(), ["nvidia", "intel", "amd", "cpu"])

    def test_nvenc_multi_engine_table_covers_ada_and_blackwell(self):
        self.assertEqual(hardware._nvenc_engines("NVIDIA GeForce RTX 5090"), 3)
        self.assertEqual(hardware._nvenc_engines("NVIDIA GeForce RTX 5080"), 2)
        self.assertEqual(hardware._nvenc_engines("NVIDIA GeForce RTX 4090"), 2)
        self.assertEqual(hardware._nvenc_engines("NVIDIA GeForce RTX 4070 Ti SUPER"), 2)
        self.assertEqual(hardware._nvenc_engines("NVIDIA GeForce RTX 4060"), 1)
        self.assertEqual(hardware._nvenc_engines("NVIDIA B200"), 0)


class WindowsAmfQsvTests(unittest.TestCase):
    def test_amf_presets_quality_and_report_codec(self):
        self.assertIn("amf", ff.speed_preset_catalog())
        self.assertEqual(ff.encoder_preset_args("av1_amf", "fast"), ["-quality", "speed"])
        self.assertEqual(ff.encoder_preset_args("hevc_amf", "balanced"), ["-quality", "balanced"])
        self.assertEqual(ff.encoder_preset_args("h264_amf", "slow"), ["-quality", "quality"])
        self.assertEqual(output_report._codec("av1_amf"), "av1")
        self.assertEqual(output_report._codec("hevc_amf"), "hevc")
        self.assertEqual(output_report._codec("h264_amf"), "h264")

        with patch.object(ff.os, "name", "nt"):
            self.assertEqual(ff.encoder_backend("amd"), "amf")
            self.assertEqual(ff.quality_args("amd", 24), ["-rc", "cqp", "-qp_i", "24", "-qp_p", "24"])
            self.assertEqual(ff.hwaccel_input_args("amd"), ["-hwaccel", "d3d11va"])
            self.assertEqual(ff.hwaccel_input_args("intel"), ["-hwaccel", "qsv"])


class TaskbarAndPowerTests(unittest.TestCase):
    def test_taskbar_progress_states(self):
        self.assertEqual(compute_taskbar_status({}), (TBPF_NOPROGRESS, 0, 1000))

        # Starting job (0% -> indeterminate)
        st = {
            "active_ids": ["j1"],
            "paused": False,
            "counts": {"running": 1, "waiting": 0, "awaiting": 0},
            "items": [{"id": "j1", "status": "in arbeit", "progress": {"pct": 0}}],
        }
        self.assertEqual(compute_taskbar_status(st), (TBPF_INDETERMINATE, 0, 1000))

        # Active encode at 42.5% -> green (425/1000)
        st["items"][0]["progress"]["pct"] = 42.5
        self.assertEqual(compute_taskbar_status(st), (TBPF_NORMAL, 425, 1000))

        # Suspended job -> yellow paused
        st["items"][0]["progress"]["suspended"] = True
        self.assertEqual(compute_taskbar_status(st), (TBPF_PAUSED, 425, 1000))
        self.assertFalse(should_prevent_sleep(st, enabled=True))

        # Active non-suspended -> prevents sleep
        st["items"][0]["progress"]["suspended"] = False
        self.assertTrue(should_prevent_sleep(st, enabled=True))
        self.assertFalse(should_prevent_sleep(st, enabled=False))

        # Latest finished job failed -> red error
        failed_state = {
            "active_ids": [],
            "paused": False,
            "counts": {"running": 0, "waiting": 0, "awaiting": 0, "failed": 1},
            "items": [
                {"id": "j0", "status": "fertig", "finished_at": 100.0},
                {"id": "j1", "status": "fehlgeschlagen", "finished_at": 200.0},
            ],
        }
        self.assertEqual(compute_taskbar_status(failed_state), (TBPF_ERROR, 1000, 1000))

    def test_post_queue_countdown_and_cancel(self):
        queue = Mock()
        bridge = DesktopBridge(DesktopPathGrants(), queue, Mock(), Path("."))
        bridge.set_post_queue_action("sleep")
        self.assertEqual(bridge.get_power_settings()["post_queue_action"], "sleep")

        # Tick 1: Queue is actively encoding
        queue.state.return_value = {
            "active_ids": ["j1"],
            "paused": False,
            "counts": {"running": 1, "waiting": 0, "awaiting": 0},
            "items": [{"id": "j1", "status": "in arbeit", "progress": {"pct": 50}}],
        }
        self.assertIsNone(bridge._monitor_tick(2.0))
        self.assertIsNone(bridge.get_power_settings()["countdown"])

        # Tick 2: Queue finishes -> 30s countdown starts, post_queue_action consumed
        queue.state.return_value = {
            "active_ids": [],
            "paused": False,
            "counts": {"running": 0, "waiting": 0, "awaiting": 0, "done": 1},
            "items": [{"id": "j1", "status": "fertig", "finished_at": 123.0}],
        }
        self.assertIsNone(bridge._monitor_tick(2.0))
        pwr = bridge.get_power_settings()
        self.assertEqual(pwr["post_queue_action"], "none")
        self.assertEqual(pwr["countdown"], {"action": "sleep", "seconds": 30})

        # Tick 3: Countdown advances by 15s -> 15s left
        self.assertIsNone(bridge._monitor_tick(15.0))
        self.assertEqual(bridge.get_power_settings()["countdown"]["seconds"], 15)

        # Cancel countdown -> no action triggered
        bridge.cancel_post_queue_action()
        self.assertIsNone(bridge.get_power_settings()["countdown"])
        self.assertIsNone(bridge._monitor_tick(20.0))

        # Arm again, run active -> idle -> full 30s expiry -> triggers "hibernate"
        bridge.set_post_queue_action("hibernate")
        queue.state.return_value["active_ids"] = ["j2"]
        bridge._monitor_tick(2.0)
        queue.state.return_value["active_ids"] = []
        bridge._monitor_tick(2.0)
        self.assertEqual(bridge._monitor_tick(30.0), "hibernate")
        self.assertIsNone(bridge.get_power_settings()["countdown"])


class FolderManagementAndToastTests(unittest.TestCase):
    def test_add_list_and_remove_custom_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vids = root / "Videos"
            extra = root / "Archiv"
            vids.mkdir()
            extra.mkdir()
            with patch.object(config, "MEDIA_ROOTS", [("Videos", vids)]), \
                 patch.object(config, "_SHARE_ROOTS", []), \
                 patch.object(config, "MULTI_MEDIA", False):
                folders = DesktopMediaFolders(config, root)
                added = folders.add_selected(str(extra))
                self.assertEqual(len(added["folders"]), 1)
                self.assertTrue(config.MULTI_MEDIA)

                bridge = DesktopBridge(DesktopPathGrants(), Mock(), Mock(), root, folders=folders)
                self.assertEqual(len(bridge.list_folders()["folders"]), 1)

                removed = bridge.remove_folder(str(extra))
                self.assertTrue(removed["ok"])
                self.assertEqual(removed["folders"], [])
                self.assertEqual(config.MEDIA_ROOTS, [("Videos", vids)])
                self.assertFalse(config.MULTI_MEDIA)

    def test_remove_matches_short_name_alias_of_resolved_folder(self):
        """8.3-Kurzpfad (RUNNER~1) und aufgelöster Langpfad sind derselbe Ordner."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vids = root / "Videos"
            extra = root / "Archiv"
            vids.mkdir()
            extra.mkdir()
            with patch.object(config, "MEDIA_ROOTS", [("Videos", vids)]), \
                 patch.object(config, "_SHARE_ROOTS", []), \
                 patch.object(config, "MULTI_MEDIA", False):
                folders = DesktopMediaFolders(config, root)
                folders.add_selected(str(extra))
                stored = folders.list_folders()[0]["path"]
                alias = r"C:\Users\RUNNER~1\AppData\Local\Temp\alias\Archiv"

                def fake_resolve(self, strict=False):
                    text = os.path.normcase(str(self))
                    if "runner~1" in text or os.path.normcase(stored) == os.path.normcase(
                            os.path.normpath(str(self))):
                        return Path(stored)
                    return Path(os.path.normpath(str(self)))

                with patch("core.desktop_folders.Path.resolve", fake_resolve):
                    removed = folders.remove_selected(alias)
                self.assertEqual(removed["folders"], [])
                self.assertEqual(config.MEDIA_ROOTS, [("Videos", vids)])

    def test_desktop_notifier_hook_receives_job_completion(self):
        delivered = []
        done_evt = threading.Event()

        def handler(title, msg):
            delivered.append((title, msg))
            done_evt.set()

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(config, "DATA_DIR", Path(tmp)):
            notify.register_desktop_notifier(handler)
            self.addCleanup(lambda: notify.register_desktop_notifier(None))
            item = SimpleNamespace(
                status="fertig",
                title="Matrix.mkv",
                output_size=1024 * 1024 * 500,
                saved_bytes=1024 * 1024 * 300,
                error="",
            )
            notify.notify_job(item)
            self.assertTrue(done_evt.wait(3.0))
            self.assertEqual(len(delivered), 1)
            self.assertIn("Matrix.mkv", delivered[0][1])

    @unittest.skipUnless(os.name == "nt", "NTFS Alternate Data Streams")
    def test_unblock_zone_identifiers_strips_motw_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            internal = root / "_internal"
            internal.mkdir()
            dll = internal / "Python.Runtime.dll"
            dll.write_bytes(b"MZ-fake-assembly")
            ads = Path(f"{dll}:Zone.Identifier")
            ads.write_text("[ZoneTransfer]\nZoneId=3\n", encoding="utf-8")
            self.assertTrue(ads.exists())
            removed = unblock_zone_identifiers(root)
            self.assertGreaterEqual(removed, 1)
            self.assertFalse(ads.exists())


if __name__ == "__main__":
    unittest.main()
