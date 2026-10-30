import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_r4_quota_checkpoint.py"
SPEC = importlib.util.spec_from_file_location("reconcile_r4_quota_checkpoint", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NoProjectUsageCapTests(unittest.TestCase):
    def test_snapshot_is_recorded_without_restoring_stale_stop_gate(self):
        manifest = {
            "halt_no_more_model_requests": True,
            "halt_status": "paused_for_live_quota_checkpoint_owner",
            "usage_stop_percent": 100,
            "authorized_stop_threshold_percent": 100,
            "usage_guard": {"stop_threshold_percent": 100},
            "quota_control_checkpoint": {
                "halt_no_more_model_requests": True,
                "stop_threshold_percent": 100,
            },
        }

        MODULE.apply_no_project_cap(manifest, 3, "2026-10-28T07:00:00+00:00")

        self.assertFalse(manifest["halt_no_more_model_requests"])
        self.assertIsNone(manifest["halt_status"])
        self.assertIsNone(manifest["usage_stop_percent"])
        self.assertIsNone(manifest["authorized_stop_threshold_percent"])
        self.assertIsNone(manifest["usage_guard"]["stop_threshold_percent"])
        self.assertEqual(manifest["usage_guard"]["last_snapshot_percent"], 3)
        self.assertFalse(manifest["quota_control_checkpoint"]["halt_no_more_model_requests"])
        self.assertIsNone(manifest["quota_control_checkpoint"]["stop_threshold_percent"])
        self.assertTrue(manifest["events"][-1]["usage_snapshot_is_observation_only"])
        self.assertTrue(manifest["events"][-1]["unknown_in_flight_deferred_without_replay"])


if __name__ == "__main__":
    unittest.main()
