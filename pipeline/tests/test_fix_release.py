"""Regression tests for three release defects.

1. Top-up re-ran scored episodes: each retry launched a fresh `n + spent`
   campaign and kept only the last.
2. The qualification repair budget ignored `policy.MAX_REPAIR_PASSES`, never
   consulted `repair.admissible`, was separate from the gate budget, and an
   unreadable repair log restored it.
3. The blind solve was capped at 80 steps and a void row stopped qualification.

Nothing here calls a model, Docker or the network: Harbor and the evidence
reader are replaced by a fake that behaves like `run_campaign` does on voids.
"""
import copy
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pipeline import checkpoint, policy, release, repair
from pipeline.artifacts import atomic_json
from pipeline.author.build import build
from pipeline.benchmark.harbor import PROTOCOL
from pipeline.tests.test_production import synthetic_bundle

SHA = "f" * 64
VOID_ERROR = "One or more trials were incomplete or invalid"


class FakeHarbor:
    """`run_campaign` + `native_evidence` + attribution, scripted per model.

    Like the real runner, a campaign with any void trial reports ok=False and
    native_evidence reports the void trials as issues.
    """

    def __init__(self, outcomes, names, report_ok_with_voids=False):
        self.outcomes = {m: iter(seq) for m, seq in outcomes.items()}
        self.names, self.ok_with_voids = names, report_ok_with_voids
        self.campaigns, self.launches = {}, {}

    def run_campaign(self, snapshot, output, model=None, agent="terminus-2", rollouts=1, **kw):
        output.mkdir(parents=True)
        trials = []
        for i in range(rollouts):
            outcome = next(self.outcomes[model])
            trials.append({"id": f"{output.name}-{i}", "valid": outcome != "void",
                           "reward": None if outcome == "void" else int(outcome == "pass"),
                           "steps": []})
        voids = any(not t["valid"] for t in trials)
        report = {"ok": self.ok_with_voids or not voids, "errors": [VOID_ERROR] if voids else [],
                  "status": "finished", "protocol": PROTOCOL, "agent": agent,
                  "requested_model": model,
                  "step_names": self.names, "dry_run": False}
        (output / "report.json").write_text(json.dumps(report))
        self.campaigns[str(output)] = (model, trials)
        self.launches.setdefault(model, []).append(rollouts)
        return report

    def native_evidence(self, bundle, output, role, minimum=1):
        if role in ("oracle", "nop"):
            return [], {"trials": [{"reward": 1 if role == "oracle" else 0}]}
        model, trials = copy.deepcopy(self.campaigns[str(output)])
        issues = [f"{t['id']}: incomplete/void native trial" for t in trials if not t["valid"]]
        if issues:
            issues.insert(0, "native report is incomplete, invalid or uses another protocol")
        if len(trials) < minimum:
            issues.append(f"need >= {minimum} complete native trials")
        return issues, {"model": "prov/" + model, "trials": trials, "by_step": {},
                        "step_names": self.names, "effort": "high",
                        "protocol": PROTOCOL, "limits": {}, "rollouts": []}

    @staticmethod
    def attribute(bundle, measured):
        counts = {"pass": 0, "capability": 0, "void": 0}
        for t in measured.get("trials", []):
            counts["void" if not t["valid"] else "pass" if t["reward"] == 1 else "capability"] += 1
        return {"counts": counts}

    @classmethod
    def problems(cls, bundle, measured, minimum=None):
        voids = cls.attribute(bundle, measured)["counts"]["void"]
        return [f"{voids} void episodes. re-run them"] if voids else []


class Qualification(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # The calibration of record is gated on a shippable screen; that gate
        # has its own tests (test_spec_acceptance). These test qualification.
        screened = patch("pipeline.screen.shippable_problems", return_value=[])
        screened.start()
        self.addCleanup(screened.stop)
        # Placeholder models stand for the two calibration models; each runs
        # in its assigned harness, as Fable 5.1 and GPT 6 Astra do.
        harness = patch("pipeline.policy.harness_for",
                        side_effect=lambda model: "claude-code" if model == "one" else "codex")
        harness.start()
        self.addCleanup(harness.stop)
        self.root = Path(temp.name).resolve()
        self.bundle = synthetic_bundle(self.root)
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec["_producer"] = {"model": "author"}
        build(spec, self.bundle)
        import tomllib
        self.names = [s["name"] for s in tomllib.loads((self.bundle / "task.toml").read_text())["steps"]]
        v = self.bundle / "verification"
        atomic_json(v / "native-controls.json", {"artifact_sha256": SHA, "oracle": "o", "nop": "n"})
        atomic_json(v / "semantic-review.json", {"artifact_sha256": SHA, "model": "critic"})
        atomic_json(v / "blind-solve.json", {"artifact_sha256": SHA, "model": "solver",
                                             "max_steps": sys.maxsize})

    def qualify(self, fake, blind_issues=(), blind_run=None):
        with ExitStack() as stack:
            for name, value in {
                "pipeline.local_work.enabled": False,
                "pipeline.release._preflight": True,
                "pipeline.artifacts.freeze": SHA,
                "pipeline.review.review_problems": [],
                "pipeline.review.semantic_rejection": False,
                "pipeline.evidence.calibration_problems": list(blind_issues),
                "pipeline.release._evaluate": {"release_ready": False},
            }.items():
                stack.enter_context(patch(name, return_value=value))
            stack.enter_context(patch("pipeline.review.review"))
            solver = stack.enter_context(patch("pipeline.calibrate.run.campaign",
                                               return_value=blind_run or {"campaign_id": "c1"}))
            for name, fn in (("pipeline.evidence.native_evidence", fake.native_evidence),
                             ("pipeline.benchmark.harbor.run_campaign", fake.run_campaign),
                             ("pipeline.benchmark.harbor.resolve_model", lambda m: "prov/" + m),
                             ("pipeline.attribution.attribute", fake.attribute),
                             ("pipeline.attribution.problems", fake.problems)):
                stack.enter_context(patch(name, side_effect=fn))
            release._qualify(self.bundle, ["one", "two"], "critic", "solver", self.root / "native")
        return solver

    def receipt(self, model):
        return json.loads((self.bundle / f"verification/topup-prov-{model}.json").read_text())

    # -- 1. top-up --------------------------------------------------------

    FIRST = ["pass"] * 2 + ["fail"] * 6 + ["void"] * 2

    def test_topup_runs_only_the_voids_and_keeps_every_scored_episode(self):
        for ok_with_voids in (False, True):
            with self.subTest(report_ok_with_voids=ok_with_voids):
                self.setUp()
                fake = FakeHarbor({m: self.FIRST + ["pass"] * 20 for m in ("one", "two")},
                                  self.names, ok_with_voids)
                self.qualify(fake)
                self.assertEqual(fake.launches["one"], [10, 2])
                receipt = self.receipt("one")
                self.assertEqual(receipt["topup_spent"], 2)
                self.assertEqual(receipt["episodes_run"], 12)
                self.assertEqual(receipt["valid_episodes"], 10)
                index = json.loads((self.bundle / "verification/native-calibrations.json").read_text())
                primary = next(p for p in index["campaigns"] if fake.campaigns[p][0] == "one")
                with patch("pipeline.evidence.native_evidence", side_effect=fake.native_evidence):
                    issues, measured = release.calibration_measurement(self.bundle, index, primary, 10)
                self.assertEqual(issues, [])
                # 2 retained passes + 2 replacements; the 6 scored failures stand.
                self.assertEqual(FakeHarbor.attribute(None, measured)["counts"],
                                 {"pass": 4, "capability": 6, "void": 0})

    def test_a_low_score_with_no_voids_is_never_re_run(self):
        fake = FakeHarbor({m: ["fail"] * 10 + ["pass"] * 20 for m in ("one", "two")}, self.names)
        self.qualify(fake)
        self.assertEqual(fake.launches["one"], [10])
        self.assertEqual(self.receipt("one")["topup_spent"], 0)

    def test_persistent_voids_stop_at_the_budget_and_block_on_valid_count(self):
        fake = FakeHarbor({m: ["fail"] * 7 + ["void"] * 30 for m in ("one", "two")}, self.names)
        self.qualify(fake)
        self.assertEqual(sum(fake.launches["one"][1:]), policy.MAX_TOPUP_ATTEMPTS)
        receipt = self.receipt("one")
        self.assertEqual(receipt["topup_spent"], policy.MAX_TOPUP_ATTEMPTS)
        self.assertEqual(receipt["valid_episodes"], 7)
        index = json.loads((self.bundle / "verification/native-calibrations.json").read_text())
        primary = next(p for p in index["campaigns"] if fake.campaigns[p][0] == "one")
        with patch("pipeline.evidence.native_evidence", side_effect=fake.native_evidence):
            issues, _ = release.calibration_measurement(self.bundle, index, primary, 10)
        self.assertTrue(any("need >= 10 valid native episodes" in i for i in issues))

    def test_topup_problems_refuses_more_scored_episodes_than_launched(self):
        self.assertTrue(policy.topup_problems({"pass": 2, "capability": 6, "void": 2},
                                              {"pass": 7, "capability": 6, "void": 0}, replaced=2))
        self.assertEqual(policy.topup_problems({"pass": 2, "capability": 6, "void": 2},
                                               {"pass": 4, "capability": 6, "void": 0}, replaced=2), [])

    # -- 3. blind solve ---------------------------------------------------

    def test_blind_solve_outcomes_are_recorded_and_only_integrity_blocks(self):
        atomic_json(self.bundle / "verification/blind-solve.json",
                    {"model": "solver", "max_steps": sys.maxsize, "rollouts": [
                        {"episode": 1, "step": self.names[0], "zero_class": "void", "error": "stall"}]})
        outcome = ["r1: void attempt cannot be release evidence",
                   "r1: incomplete or stalled attempt",
                   "calibration must cover every declared step as complete episodes"]
        with patch("pipeline.evidence.calibration_problems", return_value=outcome):
            issues, record = release._blind_solve_problems(self.bundle)
        self.assertEqual(issues, [])
        self.assertEqual(record["void_episodes"], 1)
        with patch("pipeline.evidence.calibration_problems",
                   return_value=outcome + ["r1: missing or changed artifact grade.json"]):
            issues, _ = release._blind_solve_problems(self.bundle)
        self.assertEqual(issues, ["r1: missing or changed artifact grade.json"])
        (self.bundle / "verification/blind-solve.json").unlink()
        self.assertEqual(release._blind_solve_problems(self.bundle)[0], [])


class FaithfulnessIsMeasuredAfterCalibration(unittest.TestCase):
    """The ledger's level was assigned before calibration, so release re-assigns
    it with the calibration's F1 reproduction evidence instead of reading it."""

    def test_release_uses_the_level_calibration_supports(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = synthetic_bundle(Path(tmp).resolve())
            ledger = {"faithfulness_level": "F0", "source": {"session_id": "s", "seed_index": 0}}
            atomic_json(bundle / "provenance/ledger.json", ledger)
            with patch("pipeline.ledger.levels.calibration_reproduces_failure",
                       return_value=True) as reproduces, \
                    patch("pipeline.ledger.levels.assign",
                          return_value={"level": "F1", "reasons": []}) as assign, \
                    patch("pipeline.naming.problems", return_value=[]):
                report = release._evaluate(bundle)
        reproduces.assert_called_once()
        self.assertEqual(assign.call_args.args[1:], (ledger, True))
        self.assertEqual(report["evidence"]["faithfulness"]["level"], "F1")
        self.assertEqual(report["evidence"]["faithfulness"]["ledger_level"], "F0")
        self.assertFalse(any(e.startswith("faithfulness:") for e in report["errors"]), report["errors"])


class RepairBudget(unittest.TestCase):
    FINDING = "checks.json chk_total: the minimum is 12, not 8"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = synthetic_bundle(Path(temp.name).resolve())

    def test_rebuilt_ledger_remeasures_level_and_package_name(self):
        previous = {"category": "F3-replay", "category_inputs": {"replay_readiness": True},
                    "faithfulness_level": "F3"}
        measured = {"source": {"session_id": "synthetic", "seed_index": 0},
                    "faithfulness_by_bytes": 1.0, "exact_copy_by_bytes": 1.0}
        with patch("pipeline.ledger.faithfulness.build", return_value=measured):
            result = repair.rebuild_ledger(self.bundle, previous)
        persisted = json.loads((self.bundle / "provenance/ledger.json").read_text())
        self.assertEqual(persisted, result)
        self.assertEqual(result["faithfulness_level"], "F2")
        self.assertEqual(result["faithfulness_level_record"]["level"], "F2")
        self.assertEqual(result["category"], previous["category"])
        self.assertEqual(result["category_inputs"], previous["category_inputs"])
        from pipeline import naming
        self.assertEqual(naming.problems(self.bundle, result["faithfulness_level"]), [])

    def test_revision_finishes_through_current_probe_interface(self):
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec["source"] = {"session_id": "synthetic", "seed_index": 0, "tier": 4}
        build(spec, self.bundle)
        with patch("pipeline.author.extract.extract"), \
                patch("pipeline.author.agent_designer.design", return_value=spec), \
                patch("pipeline.repair.rebuild_ledger"), \
                patch("pipeline.author.probes.run", autospec=True, return_value={"results": {}}) as probe:
            result = repair.revise(self.bundle, [self.FINDING])
        probe.assert_called_once_with(self.bundle)
        self.assertTrue((self.bundle / result["archive"] / "task/task.toml").is_file())
        self.assertEqual(checkpoint.read(self.bundle)["stage"], "qualifying")
        self.assertIsNone(checkpoint.read(self.bundle)["phase"])

    def test_unreadable_repair_log_fails_closed(self):
        path = self.bundle / repair.LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        with self.assertRaises(ValueError):
            repair.passes_used(self.bundle)
        with self.assertRaises(ValueError):
            checkpoint.repair_budget(self.bundle, "qualification", 1)

    def test_gate_and_qualification_share_one_budget(self):
        self.assertEqual(checkpoint.repair_budget(self.bundle, "gate", policy.MAX_REPAIR_PASSES),
                         policy.MAX_REPAIR_PASSES)
        for _ in range(policy.MAX_REPAIR_PASSES):
            checkpoint.consume_repair(self.bundle, "gate")
        self.assertEqual(checkpoint.repair_budget(self.bundle, "qualification", 1), 0)
        self.assertEqual(repair.passes_used(self.bundle), policy.MAX_REPAIR_PASSES)

    def test_qualify_refuses_a_budget_above_policy(self):
        with patch.object(release, "_qualify", return_value={"release_ready": False}), \
                self.assertRaisesRegex(ValueError, "MAX_REPAIR_PASSES"):
            release.qualify(self.bundle, ["one", "two"], "critic", "solver", self.bundle / "n",
                            repair_attempts=policy.MAX_REPAIR_PASSES + 1)

    def test_qualify_stops_on_a_repeated_finding(self):
        # A revision now stops at `awaiting-screen`; the governor's memory of
        # admitted findings persists across calls, so the same finding on the
        # next qualification (after the revision was screened) is terminal.
        blocked = {"release_ready": False, "status": "blocked"}
        with patch.object(release, "_qualify", return_value=blocked), \
                patch("pipeline.repair.feedback", return_value=[self.FINDING]), \
                patch("pipeline.repair.revise", return_value={"archive": "a"}) as revise:
            first = release.qualify(self.bundle, ["one", "two"], "critic", "solver",
                                    self.bundle / "n", repair_attempts=policy.MAX_REPAIR_PASSES)
            self.assertEqual(first["status"], "awaiting-screen")
            result = release.qualify(self.bundle, ["one", "two"], "critic", "solver",
                                     self.bundle / "n", repair_attempts=1)
        self.assertEqual(revise.call_count, 1)
        self.assertIn("survived a repair pass", result["repair_stopped_because"])
        self.assertTrue((self.bundle / "verification/repair-disposition.json").is_file())

    def test_qualify_does_not_repair_an_uncited_finding(self):
        blocked = {"release_ready": False, "status": "blocked"}
        with patch.object(release, "_qualify", return_value=blocked), \
                patch("pipeline.repair.feedback", return_value=["the task is too easy"]), \
                patch("pipeline.repair.revise") as revise:
            release.qualify(self.bundle, ["one", "two"], "critic", "solver",
                            self.bundle / "n", repair_attempts=1)
        revise.assert_not_called()


if __name__ == "__main__":
    unittest.main()
