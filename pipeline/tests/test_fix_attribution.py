"""Attribution, void handling and phase merging, against the policy they enforce.

Each class pins one defect that shipped: every test here fails on the code it
replaced.

  - an all-fail check could never earn its proof, because credit was read from
    keys nothing wrote and from an author field the schema does not have
  - `unattributed` was unreachable: an unclassifiable episode became void and
    silently left every denominator
  - one void trial rejected a whole native campaign
  - `analyse` merged models by union, so one model passing everything called a
    check dead that the other model failed
  - a skipped presolve and a missing ledger were both green
"""
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pipeline import attribution, evidence, phases
from pipeline.artifacts import atomic_json, identity


def _objective(cid, step="01-task"):
    return {"id": cid, "kind": "objective", "weight": 50, "step": step,
            "target": {"op": "rows"}}


def _task(root: Path, checks, steps=("01-task",)) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text("".join(
        f'[[steps]]\nname = "{s}"\n' for s in steps))
    atomic_json(root / "tests/checks.json", {"checks": checks})
    atomic_json(root / "tests/rubric.json",
                {s: [c["id"] for c in checks if c.get("step") == s] for s in steps})
    return root


def _grade(**checks):
    return {"reward": 1, "guardrails_violated": [], "contract_errors": [],
            "checks": {cid: {"id": cid, **rec} for cid, rec in checks.items()}}


def _all_fail(*cids, step="01-task"):
    return {"model": "m", "by_step": {step: {"checks": {
        cid: {"passed": 0, "failed": 10, "gated_out": 0} for cid in cids}}}}


class AllFailCreditComesFromExecutedControls(unittest.TestCase):
    """Obligations 1 and 2 are read from what `author.controls` executed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bundle = _task(Path(tmp.name) / "b", [_objective("a"), _objective("b")])
        atomic_json(self.bundle / "authoring/spec.json", {"quality": {}})
        flags = {"01-task": {"a": True, "b": True}}
        self.controls = {
            "schema": "arena-coding/controls/1", "ok": True, "errors": [],
            "results": {
                "golden": {"valid": True, "reward": 1.0, "flags": flags},
                # The independently written fix passes both checks.
                "alternative": {"valid": True, "reward": 1.0, "flags": flags},
                # A plausible wrong fix fails `a` and passes `b`: `b` has no
                # mutant that discriminates it.
                "mutants": {"m_symptom": {"valid": True, "reward": 0.5, "failed": ["a"]}}}}
        self.write()

    def write(self, sha=None):
        atomic_json(self.bundle / "verification/controls.json",
                    {**self.controls, "artifact_sha256": sha or identity(self.bundle)})

    def issues(self, *cids):
        return attribution.all_fail_problems(self.bundle, _all_fail(*cids))

    def test_an_all_fail_check_with_executed_proof_is_admissible(self):
        self.assertEqual(self.issues("a"), [])
        proof = attribution.check_evidence(self.bundle)["a"]
        self.assertEqual(proof, {"mutant_counterfactual": True, "alternative_implementation": True})

    def test_a_check_no_mutant_fails_has_no_counterfactual(self):
        issues = self.issues("b")
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("no executed mutant", issues[0])
        self.assertNotIn("independently written", issues[0])

    def test_an_alternative_that_failed_the_check_is_not_credit(self):
        self.controls["results"]["alternative"]["flags"]["01-task"]["a"] = False
        self.controls["results"]["alternative"]["reward"] = 0.5
        self.write()
        self.assertTrue(any("independently written correct fix" in i for i in self.issues("a")))

    def test_a_mutant_the_reference_also_fails_is_not_a_counterfactual(self):
        self.controls["results"]["golden"]["flags"]["01-task"]["a"] = False
        self.write()
        self.assertTrue(any("no executed mutant" in i for i in self.issues("a")))

    def test_an_invalid_mutant_run_is_not_credit(self):
        self.controls["results"]["mutants"]["m_symptom"]["valid"] = False
        self.write()
        self.assertTrue(any("no executed mutant" in i for i in self.issues("a")))

    def test_controls_for_another_task_version_prove_nothing(self):
        self.write(sha="0" * 64)
        issues = self.issues("a")
        self.assertTrue(any("another version of the task" in i for i in issues), issues)

    def test_failed_controls_prove_nothing(self):
        self.controls["ok"] = False
        self.write()
        self.assertTrue(any("executed controls failed" in i for i in self.issues("a")))

    def test_the_retired_receipt_keys_are_not_read(self):
        self.controls["results"].pop("alternative")
        self.controls["results"]["mutants"].clear()
        self.controls["alternative_passes"] = ["a"]
        self.controls["counterfactual_passes"] = ["a"]
        self.write()
        issues = self.issues("a")
        self.assertTrue(any("no executed mutant" in i and "independently written" in i
                            for i in issues), issues)

    def test_near_miss_still_blocks_with_full_proof(self):
        report = _all_fail("a")
        report["rollouts"] = [{"misses": {"a": "equal_normalised"}}]
        issues = attribution.all_fail_problems(self.bundle, report)
        self.assertTrue(any("near miss" in i for i in issues), issues)


def _row(**over):
    row = {"id": "t/01-task", "episode": 1, "step": "01-task", "reward": 1.0,
           "stop_reason": "end_turn", "tool_calls": 3, "checks": {"a": "passed"}}
    row.update(over)
    return row


class UnclassifiableIsUnattributedNotVoid(unittest.TestCase):

    def test_unrecognised_stop_on_a_clean_run_is_unattributed(self):
        verdict = attribution.episode_verdict([_row(stop_reason="refusal")], set())
        self.assertEqual(verdict["verdict"], "unattributed")

    def test_missing_reward_on_a_clean_run_is_unattributed(self):
        verdict = attribution.episode_verdict([_row(reward=None)], set())
        self.assertEqual(verdict["verdict"], "unattributed")

    def test_infrastructure_loss_stays_void(self):
        for row in (_row(error="DockerError"), _row(zero_class="void"),
                    _row(stop_reason="max_tokens"), _row(tool_calls=0),
                    _row(stop_reason="refusal", error="DockerError")):
            self.assertEqual(attribution.episode_verdict([row], set())["verdict"], "void", row)

    def test_an_unattributed_episode_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _task(Path(tmp), [_objective("a")])
            rows = [_row(episode=e, id=f"t{e}/01-task") for e in range(1, 11)]
            rows.append(_row(episode=11, id="t11/01-task", stop_reason="refusal"))
            record = attribution.attribute(bundle, {"rollouts": rows, "step_names": ["01-task"]})
        self.assertEqual(record["counts"].get("unattributed"), 1)
        self.assertTrue(any("could not be classified" in p for p in record["problems"]))

    def test_native_rows_still_classify(self):
        good = _trial("ok", True, value=0)
        bad = _trial("lost", False, value=1)
        rows = evidence.rollouts([good, bad], ["01-task"])
        self.assertEqual(attribution.episode_verdict([rows[0]], set())["verdict"], "capability")
        self.assertEqual(attribution.episode_verdict([rows[1]], set())["verdict"], "void")


def _trial(tid, valid, value=1):
    step = {"name": "01-task", "valid": valid, "reward": float(value) if valid else None,
            "errors": [] if valid else ["Step exception: DockerError"],
            "checks": {"reward/a": {"value": value, "gated_out": False}},
            "agent_result": {"metadata": {"n_episodes": 3,
                                          "all_messages": [{"role": "assistant"}]}}}
    return {"id": tid, "valid": valid, "reward": float(value) if valid else None,
            "steps": [step], "errors": [] if valid else ["Trial exception: DockerError"],
            "zero_class": None if valid else "void"}


class OneVoidDoesNotPoisonACampaign(unittest.TestCase):
    """POLICY: voids leave every denominator; the floor is >= 10 *valid*."""

    def campaign(self, root: Path, valid: int, void: int, isolation=True):
        from pipeline.benchmark import harbor
        bundle = _task(root / "bundle", [_objective("a")])
        out = root / "campaign"
        trials = [_trial(f"v{i}", True) for i in range(valid)]
        trials += [_trial(f"x{i}", False) for i in range(void)]
        for trial in trials:
            folder = out / "jobs/run" / trial["id"]
            atomic_json(folder / "result.json", trial)
            for n in range(2 if isolation or trial["valid"] else 1):
                atomic_json(folder / f"isolation/{n}.json", {
                    "network_mode": "none", "networks": ["none"],
                    "privileged": False, "port_bindings": None})
        config = {"environment": {"import_path":
                  "pipeline.benchmark.offline_docker:OfflineDockerEnvironment"},
                  "agents": [{"model_name": "m"}], "n_concurrent_trials": 2}
        atomic_json(out / "harbor-job.json", config)
        runner = Path(harbor.__file__).read_bytes()
        atomic_json(out / "report.json", {
            "task_validation": {"mode": "current-draft", "current_generator_verified": True,
                                "artifact_sha256": "sha"},
            "evidence_files": {"f": "h"},
            "runner_sha256": hashlib.sha256(runner).hexdigest(),
            "protocol": harbor.PROTOCOL, "agent": "terminus-2", "ok": not void,
            "errors": [evidence.VOID_TRIALS_ERROR] if void else [],
            "status": "finished", "step_names": ["01-task"],
            "runtime": {"versions": harbor.EXPECTED_VERSIONS},
            "step_history_adapter": {"files": []}, "limits": {"no_limits": True},
            "resolved_model": "m", "effort": "high",
            "summary": harbor.summarize(trials, len(trials))})
        return bundle, out, config

    def measure(self, valid, void, minimum=10, **kw):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            bundle, out, config = self.campaign(Path(tmp), valid, void, **kw)
            for target, value in (
                    ("pipeline.evidence.identity", lambda _p: "sha"),
                    ("pipeline.benchmark.harbor.evidence_record", lambda *_a: ({"f": "h"}, {})),
                    ("pipeline.benchmark.harbor._history_adapter", lambda: {"files": []}),
                    ("pipeline.benchmark.harbor.job_config", lambda *_a, **_k: config),
                    ("pipeline.benchmark.harbor.parse_trial",
                     lambda path, *_a, **_k: json.loads(path.read_text()))):
                stack.enter_context(patch(target, value))
            issues, measured = evidence.native_evidence(bundle, out, "terminus-2", minimum)
            record = attribution.attribute(bundle, measured)
        return issues, measured, record

    def test_ten_valid_and_one_void_is_evidence(self):
        issues, measured, record = self.measure(10, 1)
        self.assertEqual(issues, [])
        self.assertEqual(record["counts"], {"pass": 10, "void": 1})
        self.assertEqual(record["problems"], [])
        self.assertEqual(measured["by_step"]["01-task"]["checks"]["a"]["passed"], 10)

    def test_too_few_valid_after_voids_is_refused(self):
        issues, _, record = self.measure(9, 2)
        self.assertTrue(any("need >= 10 valid" in i for i in issues), issues)
        self.assertTrue(any("void episodes leave 9 valid" in p for p in record["problems"]))

    def test_a_void_trial_missing_its_isolation_proof_is_still_an_error(self):
        issues, _, _ = self.measure(10, 1, isolation=False)
        self.assertTrue(any("x0: incomplete native network-isolation proof" in i
                            for i in issues), issues)

    def test_a_report_claiming_voids_the_trials_do_not_show_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            bundle, out, config = self.campaign(Path(tmp), 10, 0)
            report = json.loads((out / "report.json").read_text())
            report.update(ok=False, errors=[evidence.VOID_TRIALS_ERROR])
            atomic_json(out / "report.json", report)
            for target, value in (
                    ("pipeline.evidence.identity", lambda _p: "sha"),
                    ("pipeline.benchmark.harbor.evidence_record", lambda *_a: ({"f": "h"}, {})),
                    ("pipeline.benchmark.harbor._history_adapter", lambda: {"files": []}),
                    ("pipeline.benchmark.harbor.job_config", lambda *_a, **_k: config),
                    ("pipeline.benchmark.harbor.parse_trial",
                     lambda path, *_a, **_k: json.loads(path.read_text()))):
                stack.enter_context(patch(target, value))
            issues, _ = evidence.native_evidence(bundle, out, "terminus-2", 10)
        self.assertTrue(any("void record disagrees" in i for i in issues), issues)


class FileWorkspaceVoidsLeaveTheDenominator(unittest.TestCase):

    def test_a_void_episode_beside_valid_ones_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            bundle = _task(Path(tmp) / "b", [_objective("a")])
            (bundle / "snap").mkdir()
            rows = []
            for episode in (1, 2, 3):
                work = bundle / f"work/{episode}"
                files = {"conversation.json": {}, "request-config.json": {
                    "model": "m", "effort": "high", "max_steps": 5, "max_tokens": 9}}
                if episode != 3:
                    files["grade.json"] = {"reward": 1, "checks": {"a": {"passed": True}}}
                for name, value in files.items():
                    atomic_json(work / name, value)
                row = {"id": f"e{episode}", "episode": episode, "step": "01-task",
                       "work_dir": f"work/{episode}",
                       "artifacts": {n: hashlib.sha256((work / n).read_bytes()).hexdigest()
                                     for n in files}}
                if episode == 3:
                    row.update(reward=None, checks={}, zero_class="void", error="DockerError")
                else:
                    row.update(reward=1, checks={"a": "passed"}, stop_reason="end_turn",
                               tool_calls=2, zero_class=None)
                rows.append(row)
            report = {"schema": "arena-pipeline/calibration/2", "bundle": "b",
                      "grader_fingerprint": "sha", "grader_stable": True,
                      "harness_sha256": "h", "harness_stable": True,
                      "steps": ["01-task"], "complete_episode": True, "model": "m",
                      "effort": "high", "protocol": "file-workspace/2",
                      "max_steps": 5, "max_tokens": 9, "snapshot": "snap",
                      "rollouts": rows,
                      "by_step": {"01-task": {"valid": 2, "checks": {"a": {
                          "passed": 2, "failed": 0, "gated_out": 0,
                          "kind": "objective", "weight": 50}}}},
                      "summary": {"requested": 3, "valid": 2, "void": 1}}
            stack.enter_context(patch("pipeline.evidence.identity", lambda _p: "sha"))
            stack.enter_context(patch("pipeline.evidence.harness_identity", lambda: "h"))
            self.assertEqual(evidence.calibration_problems(bundle, report, minimum=2), [])
            self.assertTrue(any("need at least 3 valid" in e for e in
                                evidence.calibration_problems(bundle, report, minimum=3)))
            (bundle / "work/3/conversation.json").write_text("tampered")
            self.assertTrue(any("e3: missing or changed artifact" in e for e in
                                evidence.calibration_problems(bundle, report, minimum=2)))


class AnalyseMergesModelsByIntersection(unittest.TestCase):

    def test_dead_and_unreachable_need_every_model(self):
        per = {"A": {"x": (10, 0, 0), "y": (0, 0, 10), "z": (10, 0, 0), "w": (0, 0, 10)},
               "B": {"x": (5, 5, 0), "y": (0, 3, 7), "z": (8, 0, 2), "w": (0, 0, 10)}}

        def native(_bundle, path, _agent, minimum=1):
            return [], {"model": str(path), "rollouts": [], "by_step": {"s": {"checks": {
                cid: dict(zip(("passed", "failed", "gated_out"), counts))
                for cid, counts in per[str(path)].items()}}}}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            bundle = Path(tmp)
            atomic_json(bundle / "verification/native-calibrations.json",
                        {"campaigns": ["A", "B"]})
            stack.enter_context(patch("pipeline.evidence.native_evidence", native))
            stack.enter_context(patch("pipeline.attribution.attribute",
                                      return_value={"counts": {}}))
            stack.enter_context(patch("pipeline.attribution.all_fail_problems",
                                      return_value=[]))
            result = phases.analyse(bundle)
        self.assertEqual(result["never_failed"], ["s/z"])
        self.assertEqual(result["never_attempted"], ["s/w"])


class SkippedIsNeverGreen(unittest.TestCase):

    def test_a_screen_that_could_not_run_is_never_green(self):
        # The screen replaced presolve; "could not screen" must read as
        # not-assessable, never as meeting the target or as shippable.
        from pipeline import screen
        with tempfile.TemporaryDirectory() as tmp, \
                patch("pipeline.screen.run", side_effect=RuntimeError("pool down")):
            records = screen.run_many([Path(tmp) / "c-001-x"], native_root=Path(tmp))
        self.assertFalse(records[0]["shippable"])
        self.assertFalse(records[0]["meets_target"])
        self.assertEqual(records[0]["outlook"]["verdict"], "not-assessable")
        doc = screen.report(records)
        self.assertEqual((doc["meets_target"], doc["shippable"]), (0, 0))

    def test_grade_without_a_ledger_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for target, value in (
                    ("pipeline.run.run_bundle", {"verdict": "green", "results": []}),
                    ("pipeline.author.solution.problems", []),
                    ("pipeline.benchmark.tasks_check.run", {"status": "passed"}),
                    ("pipeline.benchmark.tasks_check.problems", [])):
                stack.enter_context(patch(target, return_value=value))
            result = phases.grade(Path(tmp))
        self.assertFalse(result.ok)
        self.assertTrue(any("no provenance ledger" in f for f in result.findings))


class RemovedBandRule(unittest.TestCase):

    def test_the_band_release_rule_has_no_implementation_left(self):
        self.assertFalse(hasattr(evidence, "band_problems"))


if __name__ == "__main__":
    unittest.main()
