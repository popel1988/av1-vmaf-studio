"""Targeted range validation and persistence without an encoding dependency."""
import math
from dataclasses import asdict
import unittest

from core.queue_manager import JobSettings, build_job_settings, _validate_sample_ranges
from core import vmaf


class TargetedSceneTests(unittest.TestCase):
    def test_ranges_round_trip_in_job_settings_and_use_existing_vmaf_path(self):
        settings = build_job_settings({'sample_ranges':[[42.5,15.5]],'workflow':'compare_only'})
        restored = build_job_settings(asdict(settings))
        self.assertEqual(restored.sample_ranges, [[42.5,15.5]])
        self.assertEqual(vmaf._coerce_starts(restored.sample_ranges),[(42.5,15.5)])

    def test_old_jobs_keep_the_regular_scene_selection(self):
        self.assertEqual(JobSettings().sample_ranges,[])
        self.assertEqual(build_job_settings({}).sample_ranges,[])

    def test_bad_ranges_never_silently_fall_back_to_whole_film_or_middle(self):
        for value in ('invalid', [[0,2]], [[0,121]], [[-1,15]], [[math.inf,15]], [[0,math.nan]], [[0]], [[0,15]]*6):
            with self.subTest(value=value), self.assertRaises((TypeError,ValueError)):
                _validate_sample_ranges(value)

    def test_six_point_rule_remains_unchanged(self):
        self.assertEqual(vmaf.VMAF_P1_GAP,6.0)

    def test_source_lead_follows_the_file_clock(self):
        self.assertEqual(vmaf._lead_from_origin(4699.2, 4700.889, 0.819), 1.689)
        self.assertEqual(vmaf._lead_from_origin(0.0, 4700.889, 0.819), 0.819)
        self.assertEqual(vmaf._lead_from_origin(None, 10.0, 0.5), 0.5)
        self.assertTrue(vmaf.clip_uses_source_clock("source_s0.mkv"))
        self.assertFalse(vmaf.clip_uses_source_clock("test_nvidia_av1_s0.mkv"))


if __name__ == '__main__':
    unittest.main()
