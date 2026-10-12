"""Delivery must not launder acceptance, levels, metrics or accounting.

Each class pins one verified defect:

  AcceptanceSurvivesDelivery   delivery rewrote two files inside the artifact
                               identity and `accepted` was read from the status
                               string alone, so a tampered or stale bundle
                               still reported accepted
  F1IsReachable                nothing ever passed calibration evidence to
                               `levels.assign`, so every sub-F2 task was F0
  MarkdownMetricsArePrimary    pass@4 and mean reward came from whichever model
                               finished first, beside the primary's pass@1
  TraceAccountingDenominator   duplicate session IDs inflated the denominator
                               and a short list passed at 100%
  TasksCheckHarnessFailure     a harness crash was recorded as the task failing,
                               and `harbor check` wrote jobs/ into the repo
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

POLICY = "0" * 64
PRIMARY = "anthropic/claude-fable-5-1"


def _bundle(root: Path, category: str, name: str, *, fidelity: float,
            traps: list[dict]) -> Path:
    b = root / category / name
    for rel in ("provenance", "authoring", "tests", "verification"):
        (b / rel).mkdir(parents=True, exist_ok=True)
    (b / "task.toml").write_text(
        f'schema_version = "1.4"\n\n[task]\nname = "arena/{name}"\n\n'
        '[metadata]\nsource_trace = "s1"\n\n[[steps]]\nname = "01-task"\n')
    (b / "tests/checks.json").write_text(json.dumps(
        {"checks": [{"id": "o1", "kind": "objective", "weight": 1},
                    {"id": "o2", "kind": "objective", "weight": 1}]}))
    (b / "provenance/ledger.json").write_text(json.dumps(
        {"faithfulness_by_bytes": fidelity, "entries": []}, indent=1) + "\n")
    for n in ("traps.json", "spec.json"):
        (b / "authoring" / n).write_text(json.dumps({"traps": traps}))
    return b


def _accept(bundle: Path, policy_sha: str = POLICY) -> None:
    from pipeline.artifacts import identity
    (bundle / "verification/release.json").write_text(json.dumps(
        {"schema": "arena-pipeline/release/1", "status": "accepted", "errors": [],
         "artifact_sha256": identity(bundle), "policy_sha256": policy_sha,
         "evidence": {}}))


AUTHORED = [{"id": f"a{i}", "provenance": "authored", "checks": ["o2"]} for i in range(4)]


class AcceptanceSurvivesDelivery(unittest.TestCase):

    def setUp(self):
        from pipeline import deliver, deliverable
        self.deliver, self.deliverable = deliver, deliverable
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch("pipeline.artifacts.policy_identity", return_value=POLICY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.source = _bundle(self.root / "work", "rich", "c-001-editorial-review",
                              fidelity=0.95, traps=AUTHORED)

    def _deliver(self, **kw):
        record = self.deliver.deliver(self.root / "work", self.root / "out", **kw)
        return record, self.root / "out/F2-scenario/a_f2_editorial-review"

    def test_an_untouched_delivered_bundle_is_accepted(self):
        _accept(self.source)
        record, out = self._deliver(accepted_only=True)
        self.assertEqual(len(record["delivered"]), 1, record["held"])
        self.assertIn("arena/a_f2_editorial-review", (out / "task.toml").read_text())
        self.assertEqual(self.deliverable.acceptance_problems(out), [])
        self.assertTrue(self.deliverable.task_record(out)["accepted"])

    def test_a_grader_edited_after_delivery_is_not_accepted(self):
        _accept(self.source)
        _, out = self._deliver()
        (out / "tests/checks.json").write_text(json.dumps({"checks": []}))
        record = self.deliverable.task_record(out)
        self.assertFalse(record["accepted"])
        self.assertTrue(any("tests/checks.json" in p for p in record["acceptance_problems"]))

    def test_a_ledger_edit_beyond_the_level_is_not_accepted(self):
        _accept(self.source)
        _, out = self._deliver()
        ledger = json.loads((out / "provenance/ledger.json").read_text())
        ledger["faithfulness_by_bytes"] = 1.0
        (out / "provenance/ledger.json").write_text(json.dumps(ledger))
        self.assertFalse(self.deliverable.task_record(out)["accepted"])

    def test_a_task_toml_edit_beyond_the_rename_is_not_accepted(self):
        _accept(self.source)
        _, out = self._deliver()
        path = out / "task.toml"
        path.write_text(path.read_text().replace('name = "01-task"', 'name = "01-other"'))
        self.assertFalse(self.deliverable.task_record(out)["accepted"])

    def test_a_forged_receipt_inventory_is_not_accepted(self):
        _accept(self.source)
        _, out = self._deliver()
        (out / "tests/checks.json").write_text(json.dumps({"checks": []}))
        from pipeline.artifacts import inventory
        receipt = json.loads((out / "verification/delivery.json").read_text())
        receipt["source_inventory"] = inventory(out)   # claim the edit was accepted
        (out / "verification/delivery.json").write_text(json.dumps(receipt))
        self.assertFalse(self.deliverable.task_record(out)["accepted"])

    def test_a_source_changed_after_release_is_held(self):
        _accept(self.source)
        (self.source / "tests/checks.json").write_text(json.dumps({"checks": []}))
        record, out = self._deliver(accepted_only=True)
        self.assertEqual(record["delivered"], [])
        self.assertIn("another task version", record["held"][0]["held_because"])

    def test_a_stale_source_delivered_anyway_reports_not_accepted(self):
        _accept(self.source)
        (self.source / "tests/checks.json").write_text(json.dumps({"checks": []}))
        record, out = self._deliver()
        self.assertFalse(record["delivered"][0]["accepted"])
        self.assertFalse(self.deliverable.task_record(out)["accepted"])

    def test_a_release_under_another_policy_is_not_accepted(self):
        _accept(self.source, policy_sha="1" * 64)
        record, out = self._deliver(accepted_only=True)
        self.assertEqual(record["delivered"], [])
        self.assertIn("policy", record["held"][0]["held_because"])

    def test_a_blocked_release_is_not_accepted(self):
        _accept(self.source)
        release = json.loads((self.source / "verification/release.json").read_text())
        release["status"] = "blocked"
        (self.source / "verification/release.json").write_text(json.dumps(release))
        _, out = self._deliver()
        self.assertFalse(self.deliverable.task_record(out)["accepted"])

    def test_the_accepted_view_reproduces_the_released_identity(self):
        from pipeline.artifacts import identity
        _accept(self.source)
        released = identity(self.source)
        _, out = self._deliver()
        self.assertNotEqual(identity(out), released)
        with self.deliverable.accepted_view(out) as view:
            self.assertEqual(identity(view), released)


class F1IsReachable(unittest.TestCase):
    """A synthetic world whose one trace-derived trap drives check `o1`."""

    TRAPS = [{"id": "t0", "provenance": "trace_derived", "derived_from": "s1::msg3",
              "checks": ["o1"]}] + AUTHORED

    def setUp(self):
        from pipeline import deliver
        from pipeline.artifacts import identity
        self.deliver = deliver
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = _bundle(self.root / "work", "thin", "c-001-pattern",
                              fidelity=0.05, traps=self.TRAPS)
        (self.bundle / "verification/native-calibrations.json").write_text(json.dumps(
            {"artifact_sha256": identity(self.bundle), "campaigns": ["/c/primary"]}))

    def _measured(self, *rows, model=PRIMARY, issues=()):
        measured = {"model": model, "rollouts": [
            {"episode": i, "step": "01-task", "zero_class": zc, "checks": checks}
            for i, (zc, checks) in enumerate(rows, 1)]}
        return mock.patch("pipeline.evidence.native_evidence",
                          return_value=(list(issues), measured))

    def _level(self):
        return self.deliver.plan(self.root / "work")["rows"][0]

    def test_a_failure_on_the_primary_model_reproduces_the_trace(self):
        with self._measured((None, {"o1": "passed"}), (None, {"o1": "failed"})):
            row = self._level()
        self.assertEqual(row["level"], "F1")
        self.assertIs(row["calibration_reproduces_failure"], True)
        self.assertEqual(row["destination"], "F1-pattern/a_f1_pattern")

    def test_delivery_writes_the_f1_level_and_name(self):
        with self._measured((None, {"o1": "failed"})):
            record = self.deliver.deliver(self.root / "work", self.root / "out")
        out = self.root / "out/F1-pattern/a_f1_pattern"
        self.assertEqual(record["by_level"]["F1"], 1)
        ledger = json.loads((out / "provenance/ledger.json").read_text())
        self.assertEqual(ledger["faithfulness_level"], "F1")
        self.assertNotIn("faithfulness_blocked", ledger)
        self.assertIn("arena/a_f1_pattern", (out / "task.toml").read_text())

    def test_every_episode_passing_is_measured_non_reproduction(self):
        with self._measured((None, {"o1": "passed"})):
            row = self._level()
        self.assertEqual(row["level"], "F0")
        self.assertIs(row["calibration_reproduces_failure"], False)

    def test_gated_out_and_void_measure_nothing(self):
        with self._measured((None, {"o1": "gated_out"}), ("void", {"o1": "failed"})):
            row = self._level()
        self.assertEqual(row["level"], "F0")
        self.assertIsNone(row["calibration_reproduces_failure"])

    def test_only_the_primary_model_counts(self):
        with self._measured((None, {"o1": "failed"}), model="openai/gpt-6-astra"):
            self.assertIsNone(self._level()["calibration_reproduces_failure"])

    def test_unverifiable_evidence_is_not_evidence(self):
        with self._measured((None, {"o1": "failed"}), issues=["evidence changed"]):
            self.assertIsNone(self._level()["calibration_reproduces_failure"])

    def test_a_stale_index_is_not_evidence(self):
        (self.bundle / "tests/checks.json").write_text(json.dumps({"checks": []}))
        with self._measured((None, {"o1": "failed"})):
            self.assertIsNone(self._level()["calibration_reproduces_failure"])


class MarkdownMetricsArePrimary(unittest.TestCase):

    def test_every_primary_column_comes_from_the_primary_model(self):
        from pipeline import deliverable
        report = {
            "generated_at": "now", "environments": 1,
            "calibration_models": {"primary": PRIMARY, "secondary": "openai/gpt-6-astra"},
            "acceptance": {"criteria": [], "problems": [], "ok": True},
            "yield": {}, "faithfulness": {"counts": {}}, "deviations": [],
            "trace_accounting": {"sessions_shared": 1, "converted": 1,
                                 "excluded": 0, "undispositioned": 0},
            "tasks": [{"bundle": "b", "short_name": "a_f2_x", "session_id": "s1",
                       "faithfulness_level": "F2", "accepted": True,
                       "hardness": {"primary_pass_at_1": 0.1, "hard": True},
                       # secondary FIRST, as as_completed order can leave it
                       "per_model": {
                           "openai/gpt-6-astra": {"pass_at_1": 0.9, "pass_at_4": 0.99,
                                                  "average_reward": 0.95},
                           PRIMARY: {"pass_at_1": 0.1, "pass_at_4": 0.35,
                                     "average_reward": 0.2}}}]}
        line = next(l for l in deliverable.render_markdown(report).splitlines()
                    if l.startswith("| a_f2_x "))
        cells = [c.strip() for c in line.strip("|").split("|")]
        self.assertEqual(cells[3:9], ["0.10", "0.35", "0.200", "0.90", "0.99", "0.950"])


class TraceAccountingDenominator(unittest.TestCase):

    def setUp(self):
        from pipeline import deliverable
        self.d = deliverable

    def test_duplicates_are_one_session_and_a_problem(self):
        r = self.d.trace_accounting(["s1", "s1", "s2"], {"s1": ["e"]}, {"s2": "x"})
        self.assertEqual(r["sessions_shared"], 2)
        self.assertFalse(r["ok"])

    def test_a_list_shorter_than_the_dump_fails(self):
        r = self.d.trace_accounting(["s1"], {"s1": ["e"]}, {}, expected_count=500)
        self.assertFalse(r["ok"])
        self.assertIn("500", r["problems"][0])

    def test_indexed_sessions_missing_from_the_list_fail(self):
        r = self.d.trace_accounting(["s1"], {"s1": ["e"]}, {},
                                    expected_sessions=["s1", "s9"])
        self.assertFalse(r["ok"])

    def test_a_matching_list_passes(self):
        r = self.d.trace_accounting(["s1", "s2"], {"s1": ["e"]}, {"s2": "x"},
                                    expected_count=2, expected_sessions=["s1"])
        self.assertTrue(r["ok"], r["problems"])

    def test_the_expectation_is_read_from_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.json"
            index.write_text(json.dumps({
                "sessions": 3, "rows": [{"session_id": "s1"}, {"session_id": "s1"}],
                "dump_inventory": {"cohort_manifest.json": {"sessions": 500}}}))
            self.assertEqual(self.d.dump_expectation(index),
                             {"expected_count": 500, "expected_sessions": ["s1"]})
            self.assertEqual(self.d.dump_expectation(Path(tmp) / "absent.json"), {})


FAKE_HARBOR = """#!/bin/sh
# emulate `harbor check <task> --jobs-dir <dir>`
jobs=jobs
while [ $# -gt 0 ]; do
  if [ "$1" = "--jobs-dir" ]; then jobs="$2"; shift; fi
  shift
done
mkdir -p "$jobs/run/trial"
case "$FAKE_MODE" in
  crash)
    printf '{"exception_info": {"exception_type": "NonZeroAgentExitCodeError", "exception_message": "stdout: exec /usr/bin/bash: argument list too long"}}' > "$jobs/run/trial/result.json"
    echo "Checking task quality..."; exit 1 ;;
  fail)
    printf '{"exception_info": null, "verifier_result": {"rewards": {"reward": 0.0}}}' > "$jobs/run/trial/result.json"
    echo "task_name is invalid"; exit 1 ;;
  *)
    printf '{"exception_info": null}' > "$jobs/run/trial/result.json"; exit 0 ;;
esac
"""


class TasksCheckHarnessFailure(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        exe = self.root / "harbor"
        exe.write_text(FAKE_HARBOR)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.task = _bundle(self.root, "work", "c-001-x", fidelity=0.9, traps=AUTHORED)
        self.artifacts = self.root / "artifacts"
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        for p in (mock.patch.dict(os.environ, {"ARENA_HARBOR_BIN": str(exe)}),
                  mock.patch("pipeline.config.ARTIFACTS", self.artifacts)):
            p.start()
            self.addCleanup(p.stop)
        here = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, here)

    def _run(self, mode):
        from pipeline.benchmark import tasks_check
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode}):
            return tasks_check.run(self.task)

    def test_a_harness_crash_is_unverified_not_a_task_failure(self):
        from pipeline.benchmark import tasks_check
        record = self._run("crash")
        self.assertEqual(record["status"], "unverified")
        self.assertIn("argument list too long", record["detail"])
        self.assertTrue(tasks_check.problems(self.task), "unverified is never green")
        self.assertIn("SKIPPED is not green", tasks_check.problems(self.task)[0])

    def test_a_verdict_of_failure_is_still_a_failure(self):
        self.assertEqual(self._run("fail")["status"], "fail")

    def test_a_pass_is_a_pass(self):
        self.assertEqual(self._run("pass")["status"], "pass")

    def test_jobs_are_written_under_the_artifacts_root_not_the_cwd(self):
        record = self._run("pass")
        self.assertFalse((self.cwd / "jobs").exists())
        self.assertTrue(Path(record["jobs_dir"]).is_relative_to(self.artifacts))
        self.assertTrue(any(Path(record["jobs_dir"]).rglob("result.json")))


class OneBundleDiscovery(unittest.TestCase):

    def test_dead_duplicates_are_gone(self):
        from pipeline import deliver, deliverable, naming
        self.assertFalse(hasattr(naming, "delivery_path"))
        self.assertFalse(hasattr(deliver, "_rename_package"))
        self.assertFalse(hasattr(deliverable, "bundles"))

    def test_a_half_built_bundle_is_reported_not_skipped(self):
        from pipeline import deliver
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _bundle(root, "rich", "c-001-built", fidelity=0.95, traps=AUTHORED)
            (root / "rich/c-002-half").mkdir(parents=True)
            found, problems = deliver.working_bundles(root)
            self.assertEqual([p.name for p in found], ["c-001-built"])
            self.assertTrue(any("c-002-half" in p for p in problems))
            self.assertTrue(any("c-002-half" in p for p in deliver.plan(root)["problems"]))


if __name__ == "__main__":
    unittest.main()
