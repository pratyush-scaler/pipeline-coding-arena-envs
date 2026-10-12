"""Broken basic controls stop before the expensive wrong-fix matrix."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pipeline.author import control_cache, controls, sandbox
from pipeline.tests.test_parallel_controls import ParallelControls


class ControlPreflight(unittest.TestCase):
    setUp = ParallelControls.setUp
    record = ParallelControls.record

    def failed_run(self, label, mutate, workers=2):
        seen = []
        work = Path(self.tmp.name) / "failed-preflight"

        def verify(bundle, variants, **kwargs):
            name = kwargs["work"].name
            seen.append(name)
            record = self.record(bundle, variants, **kwargs)
            if name == label:
                mutate(record)
            return record

        with patch.object(sandbox, "build_image", side_effect=lambda b, role, **kw: role), \
                patch.object(sandbox, "verify", side_effect=verify), \
                patch.object(control_cache, "retain") as retain:
            with self.assertRaisesRegex(sandbox.SandboxError, "control preflight failed"):
                controls.run(self.bundle, work=work, workers=workers, reuse=False)
            retain.assert_not_called()
        self.assertEqual(set(seen), {"nop", "golden-1", "alternative"})
        self.assertEqual(len(seen), 3)
        self.assertFalse((self.bundle / controls.RECEIPT).exists())
        diagnostic = json.loads((work / "control-preflight-failure.json").read_text())
        self.assertFalse(diagnostic["ok"])
        self.assertFalse(diagnostic["reusable"])
        self.assertFalse(diagnostic["execution"]["complete"])
        self.assertEqual(diagnostic["execution"]["executed_new_trials"], 3)
        self.assertGreater(diagnostic["execution"]["planned_trials"], 3)
        return diagnostic

    def test_zero_reference_keeps_actual_failure_without_running_mutants(self):
        def fail(record):
            record["reward"] = 0.0
            record["steps"][self.steps[0]]["error"] = "libtk8.6.so missing during collection"
        diagnostic = self.failed_run("golden-1", fail)
        self.assertEqual(diagnostic["records"]["golden-1"]["reward"], 0.0)
        self.assertIn("libtk8.6.so", diagnostic["records"]["golden-1"]["steps"][self.steps[0]]["error"])

    def test_invalid_alternative_stops_serial_matrix(self):
        diagnostic = self.failed_run("alternative", lambda r: r.update(valid=False), workers=1)
        self.assertFalse(diagnostic["records"]["alternative"]["valid"])

    def test_reference_aggregate_cannot_hide_a_missing_checkpoint(self):
        diagnostic = self.failed_run("golden-1", lambda r: r["steps"].pop(self.steps[-1]))
        self.assertTrue(any("incomplete reference checkpoints" in e for e in diagnostic["errors"]))

    def test_contract_failure_stops_despite_full_reported_reward(self):
        def fail(record):
            record["steps"][self.steps[0]]["grade"]["contract_errors"] = ["missing delivery"]
        diagnostic = self.failed_run("alternative", fail)
        self.assertEqual(diagnostic["records"]["alternative"]["reward"], 1.0)
        self.assertTrue(any("reference checkpoint failed" in e for e in diagnostic["errors"]))

    def test_nop_receiving_credit_stops_before_more_grading(self):
        diagnostic = self.failed_run("nop", lambda r: r.update(reward=1.0))
        self.assertTrue(any("nop: expected valid reward 0.0" in e for e in diagnostic["errors"]))


if __name__ == "__main__":
    unittest.main()
