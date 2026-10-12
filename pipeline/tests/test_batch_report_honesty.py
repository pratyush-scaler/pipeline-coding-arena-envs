"""The batch report must not pass on missing evidence or a padded denominator."""
import unittest

from pipeline.deliverable import batch_acceptance, yield_metrics


class BatchReportHonesty(unittest.TestCase):
    def test_yield_counts_each_shared_session_once_and_only_shared_ones(self):
        tasks = [{"session_id": "X"}, {"session_id": "A"}]
        self.assertEqual(yield_metrics(["A", "B"], tasks[:1])["environment_yield"], 0.0)
        result = yield_metrics(["A", "A", "B", "B"], tasks)
        self.assertEqual((result["sessions_shared"], result["environment_yield"]), (2, 0.5))

    def test_missing_or_warning_probes_are_unresolved_hacking(self):
        task = {"task_name": "t", "faithfulness_level": "F2", "reward_hacking_findings": [],
                "reward_hacking_unresolved": ["forged_state"]}
        rows = {c["criterion"]: c for c in batch_acceptance([task], {"ok": True})["criteria"]}
        self.assertFalse(rows["Reward hacking"]["ok"])
        task["reward_hacking_unresolved"] = []
        rows = {c["criterion"]: c for c in batch_acceptance([task], {"ok": True})["criteria"]}
        self.assertTrue(rows["Reward hacking"]["ok"])
