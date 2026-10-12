"""shipfix --release: the calibration of record certified as shipped (2026-10-07).

c-024's qualify was refused because a pipeline fix made the final's controls stale; c-007-html-date-form
was held at packaging because genuine.json certified the 5+5 screen, not the shipped 10+10 campaigns.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import shipfix
from pipeline.cli import build_parser


class CalibrationRecord(unittest.TestCase):
    def _version(self, root: Path, models=("claude-fable-5-1", "gpt-6-astra")) -> Path:
        version = root / "v5"
        (version / "verification").mkdir(parents=True)
        campaigns = []
        for i, model in enumerate(models):
            camp = root / f"camp{i}"
            camp.mkdir()
            (camp / "report.json").write_text(json.dumps({"requested_model": model}))
            campaigns.append(str(camp))
        (version / "verification/native-calibrations.json").write_text(json.dumps(
            {"artifact_sha256": "abc", "campaigns": campaigns, "topups": {campaigns[0]: [str(root / "topup")]}}))
        return version

    def test_both_campaigns_become_screen_shaped_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = self._version(Path(tmp))
            with mock.patch("pipeline.artifacts.identity", return_value="abc"):
                record = shipfix.calibration_record(version)
            self.assertEqual(sorted(record["per_model"]), ["anthropic/claude-fable-5-1", "openai/gpt-6-astra"])
            self.assertEqual(record["per_model"]["anthropic/claude-fable-5-1"]["topups"], [str(Path(tmp) / "topup")])

    def test_another_versions_calibration_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = self._version(Path(tmp))
            with mock.patch("pipeline.artifacts.identity", return_value="other"):
                with self.assertRaises(ValueError):
                    shipfix.calibration_record(version)

    def test_one_model_is_not_a_calibration_of_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = self._version(Path(tmp), models=("claude-fable-5-1",))
            with mock.patch("pipeline.artifacts.identity", return_value="abc"):
                with self.assertRaises(ValueError):
                    shipfix.calibration_record(version)


class StepRates(unittest.TestCase):
    def test_valid_episodes_only_and_full_score_counts(self):
        rows = [{"model": "m", "valid": True, "verdict": "capability", "steps": [{"name": "s1", "reward": 1.0},
                                                                                {"name": "s2", "reward": 0.5}]},
                {"model": "m", "valid": True, "verdict": "pass", "steps": [{"name": "s1", "reward": 1.0},
                                                                          {"name": "s2", "reward": 1.0}]},
                {"model": "m", "valid": False, "verdict": "void", "steps": [{"name": "s1", "reward": 1.0}]}]
        self.assertEqual(shipfix.step_rates({"episodes": rows}), {"m": {"s1": 1.0, "s2": 0.5}})


class CheckReplace(unittest.TestCase):
    def _spec(self):
        return {"checks": [{"id": "g_guard", "basis": "reads every file", "runner": {"type": "command", "shell": "grep -l X {} +"}},
                           {"id": "o_obj", "basis": "b", "runner": {"type": "pytest"}}]}

    def test_one_field_of_one_check_changes_exactly_once(self):
        spec = self._spec()
        done = shipfix.edit_spec(spec, {"check_replace": [
            {"id": "g_guard", "field": "runner.shell", "old": "grep -l", "new": "grep -lF"},
            {"id": "g_guard", "field": "basis", "old": "every file", "new": "runnable files"}]})
        self.assertEqual(spec["checks"][0]["runner"]["shell"], "grep -lF X {} +")
        self.assertEqual(spec["checks"][0]["basis"], "reads runnable files")
        self.assertEqual(spec["checks"][1], self._spec()["checks"][1])
        self.assertEqual(done, ["checks/g_guard.runner.shell", "checks/g_guard.basis"])

    def test_a_missing_check_or_text_is_refused(self):
        with self.assertRaises(ValueError):
            shipfix.edit_spec(self._spec(), {"check_replace": [{"id": "nope", "field": "basis", "old": "a", "new": "b"}]})
        with self.assertRaises(ValueError):
            shipfix.edit_spec(self._spec(), {"check_replace": [{"id": "g_guard", "field": "basis", "old": "absent", "new": "b"}]})


class CheckSet(unittest.TestCase):
    def _spec(self):
        return CheckReplace._spec(self)

    def test_the_named_field_is_replaced_whole(self):
        spec = self._spec()
        runner = {"type": "pytest", "select": ["test_x.py::test_guard"]}
        done = shipfix.edit_spec(spec, {"check_set": [{"id": "g_guard", "field": "runner", "value": runner}]})
        self.assertEqual(spec["checks"][0]["runner"], runner)
        self.assertEqual(spec["checks"][0]["basis"], "reads every file")
        self.assertEqual(spec["checks"][1], self._spec()["checks"][1])
        self.assertEqual(done, ["checks/g_guard.runner"])

    def test_a_field_the_check_lacks_is_refused(self):
        with self.assertRaises(ValueError):
            shipfix.edit_spec(self._spec(), {"check_set": [{"id": "g_guard", "field": "runer", "value": {}}]})
        with self.assertRaises(ValueError):
            shipfix.edit_spec(self._spec(), {"check_set": [{"id": "nope", "field": "runner", "value": {}}]})


class Cli(unittest.TestCase):
    def test_release_needs_no_edits(self):
        args = build_parser().parse_args(["shipfix", "/x/v5", "--release"])
        self.assertTrue(args.release)
        self.assertIsNone(args.edits)


if __name__ == "__main__":
    unittest.main()
