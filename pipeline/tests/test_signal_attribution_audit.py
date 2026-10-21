"""User, 2026-10-21, after the external audit of Batch 3: lockstep collapse, the single-decision cap and failure
attribution from the assertion that actually failed.

Fixtures (pipeline/tests/fixtures/audit-1021/) are cut from the shipped Batch 3 packages: each campaign's report.json
trials (per-trial per-check values, verbatim), the failing runs' verifier/arena-grade.json (verbatim, the stdout of
passing checks dropped), genuine.json failures (published sentences cut), the spec's checks and traps, and the two
school-scheduling suite files the attribution searches. No model call, no network, no Docker.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import failure_report, learning_signal, policy

FIXTURES = Path(__file__).resolve().parent / "fixtures/audit-1021"
GAME, SCHOOL = FIXTURES / "a_f2_c-game-bot", FIXTURES / "a_f0_school-scheduling-spa"
MINDMAP, CANVAS = FIXTURES / "a_f1_static-html-mindmap", FIXTURES / "a_f2_canvas-web-app"
STEP = "01-audit-the-recorded-runs"
REPLAY = [f"{STEP}/{c}" for c in ("o_bogus_after", "o_bogus_dry", "o_clip", "o_crash_ledge")]


def spec_of(pkg: Path) -> dict:
    return json.loads((pkg / "authoring/spec.json").read_text())


def campaigns(pkg: Path) -> list[Path]:
    return [pkg / c["path"] for c in json.loads((pkg / "runs/index.json").read_text())["campaigns"]]


def signal_of(pkg: Path) -> dict:
    return learning_signal.from_campaigns(spec_of(pkg), campaigns(pkg))


class LockstepCollapse(unittest.TestCase):
    def test_c_game_bot_replay_checks_are_one_decision(self):
        s = signal_of(GAME)
        self.assertEqual(s["runs_compared"], {"anthropic/claude-fable-5-1": 10, "openai/gpt-6-astra": 10})
        self.assertEqual(s["lockstep_groups"], [REPLAY])         # 16 failing runs, 4 passing, all four together
        self.assertEqual(s["independent_hard_before_lockstep"], 5)  # what FAILURES.md said
        self.assertEqual(s["independent_hard"], 2)                # the replay group and o_zone_run (Fable 3/10)
        self.assertEqual(s["decisions_losing_reward"], 3)         # + o_bogus, 1 Fable run: the audit's "3"
        group = next(v for k, v in s["hard_decisions"].items() if v["lockstep"])
        self.assertEqual(group["checks"], REPLAY)
        self.assertEqual(group["weight"], 41.0)
        self.assertEqual(group["fail_rate"], {"anthropic/claude-fable-5-1": 1.0, "openai/gpt-6-astra": 0.6})
        found = learning_signal.problems(s)
        # 93% on one decision is past policy.SIGNAL_SINGLE_DECISION_SHARE_BLOCK: the audit's c-game-bot still blocks;
        # two independent decisions (aimed at 3) is an advisory since 2026-10-21 ~10:05 IST.
        self.assertTrue(any("carries 93%" in p for p in found), found)
        self.assertTrue(any(p.startswith("signal advisory: 2 independent hard decision(s)")
                            for p in learning_signal.advisories(s)))

    def test_lockstep_needs_every_run_identical(self):
        # One Astra run where o_clip alone passes splits the group: the three left still move together.
        rows = learning_signal.campaign_measurements(campaigns(GAME))
        astra = rows["openai/gpt-6-astra"]
        failing = next(run for run in astra["runs"] if run[f"{STEP}/o_clip"] == "failed")
        failing[f"{STEP}/o_clip"] = "passed"
        groups = learning_signal.summary(spec_of(GAME), rows)["lockstep_groups"]
        self.assertEqual(groups, [[r for r in REPLAY if not r.endswith("o_clip")]])

    def test_no_runs_no_collapse(self):
        rows = learning_signal.campaign_measurements(campaigns(GAME))
        for row in rows.values():
            row.pop("runs")
        s = learning_signal.summary(spec_of(GAME), rows)
        self.assertIsNone(s["lockstep_groups"])                  # unmeasured, not absent
        self.assertEqual(s["independent_hard"], 5)
        self.assertEqual(s["independent_hard"], s["independent_hard_before_lockstep"])

    def test_screen_reads_runs_from_its_campaigns(self):
        rows = learning_signal.campaign_measurements(campaigns(GAME))
        paths = {json.loads((p / "report.json").read_text())["model"]: p for p in campaigns(GAME)}
        screen = {"per_model": {m: {"checks": r["checks"], "episode_rewards": r["episode_rewards"],
                                    "campaign": str(paths[m]), "topups": []} for m, r in rows.items()}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "authoring").mkdir()
            shutil.copyfile(GAME / "authoring/spec.json", Path(tmp) / "authoring/spec.json")
            s = learning_signal.from_screen(Path(tmp), screen)
        self.assertEqual(s["lockstep_groups"], [REPLAY])
        self.assertEqual(s["independent_hard"], 2)

    def test_episode_checks_skip_voids(self):
        measured = {"rollouts": [
            {"episode": 1, "step": "a", "checks": {"o_x": "failed"}}, {"episode": 2, "step": "a", "zero_class": "void",
                                                                     "checks": {"o_x": "failed"}},
            {"episode": 3, "step": "a", "checks": {"o_x": "passed"}}]}
        self.assertEqual(learning_signal.episode_checks(measured), [{"a/o_x": "failed"}, {"a/o_x": "passed"}])


class SingleDecisionCap(unittest.TestCase):
    def cap_problems(self, s):
        return [p for p in learning_signal.problems(s) if "of all the reward the calibration lost" in p]

    def test_c_game_bot_replay_carries_93_percent(self):
        s = signal_of(GAME)
        self.assertEqual(s["single_decision"]["checks"], REPLAY)
        self.assertEqual(s["single_decision_share"], 0.934)      # 656 of 702 lost points (audit F3)
        self.assertEqual(s["single_decision_share_max"], policy.SIGNAL_SINGLE_DECISION_SHARE_MAX)
        found = self.cap_problems(s)
        self.assertEqual(len(found), 1, found)
        self.assertIn("carries 93%", found[0])
        self.assertNotIn("runs are flat", found[0])               # the pilot verdict and release blockers keep it

    def test_mindmap_one_fold_carries_everything(self):
        s = signal_of(MINDMAP)
        self.assertEqual(len(s["lockstep_groups"]), 1)
        self.assertEqual(len(s["lockstep_groups"][0]), 6)         # FAILURES.md counted these as 6 decisions
        self.assertEqual(s["independent_hard"], 1)
        self.assertEqual(s["single_decision_share"], 1.0)
        self.assertEqual(len(self.cap_problems(s)), 1)

    def test_healthy_spread_is_not_capped(self):
        # canvas-web-app: lockstep pairs exist, yet five decisions share the lost reward (the heaviest 28%).
        s = signal_of(CANVAS)
        self.assertTrue(s["lockstep_groups"])
        self.assertGreaterEqual(s["independent_hard"], policy.SIGNAL_MIN_INDEPENDENT_HARD)
        self.assertLess(s["single_decision_share"], policy.SIGNAL_SINGLE_DECISION_SHARE_MAX)
        self.assertEqual(learning_signal.problems(s), [])

    def test_the_cap_is_policy(self):
        s = signal_of(CANVAS)
        for name in ("SIGNAL_SINGLE_DECISION_SHARE_MAX", "SIGNAL_SINGLE_DECISION_SHARE_BLOCK"):
            self.addCleanup(setattr, policy, name, getattr(policy, name))
        policy.SIGNAL_SINGLE_DECISION_SHARE_MAX = 0.2
        self.assertEqual(self.cap_problems(s), [])                     # between the aim and the block: advisory
        self.assertTrue(any("carries" in a for a in learning_signal.advisories(s)))
        policy.SIGNAL_SINGLE_DECISION_SHARE_BLOCK = 0.2
        self.assertEqual(len(self.cap_problems(s)), 1)

    def test_a_gated_check_loses_nothing_to_its_own_decision(self):
        sp = {"checks": [{"id": "o_a", "kind": "objective", "weight": 50, "step": "s", "trap": "t_a"},
                         {"id": "o_b", "kind": "objective", "weight": 50, "step": "s", "trap": "t_b"}],
              "traps": [{"id": "t_a", "type": "x"}, {"id": "t_b", "type": "y"}]}
        rows = {"m": {"checks": {"s/o_a": {"passed": 0, "failed": 2, "gated_out": 8},
                                 "s/o_b": {"passed": 8, "failed": 2, "gated_out": 0}}, "episode_rewards": []}}
        s = learning_signal.summary(sp, rows)
        self.assertEqual(s["lost_reward_share"], {"trap:t_a": 0.5, "trap:t_b": 0.5})


class FailedTests(unittest.TestCase):
    def test_pytest_record_names_the_node_and_the_assertion(self):
        grade = next(GAME.glob("runs/*/*/jobs/run/package__KjN55Us/steps/*/verifier/arena-grade.json"))
        record = json.loads(grade.read_text())["checks"]["o_bogus"]
        found, complete = failure_report.failed_tests(record["stdout"])
        self.assertTrue(complete)
        self.assertEqual(found, [{"test": "test_corners.py::test_more_impossible_lines",
                                  "assertion": "AssertionError: assert ('BOGUS', 6, 887.0) == ('BOGUS', 6, 887.5)"}])

    def test_node_audit_detail_names_the_title(self):
        detail = next(f["detail"] for e in json.loads((SCHOOL / "verification/genuine.json").read_text())["episodes"]
                      for f in e["failures"] if f["check"] == "o_class_busy")
        found, complete = failure_report.failed_tests(failure_report._audit_text(detail))
        self.assertFalse(complete)                                 # a re-run excerpt may omit other failures
        self.assertEqual(found[0]["test"], "a double running on into it is refused too")
        self.assertTrue(found[0]["assertion"].startswith("AssertionError [ERR_ASSERTION]: the second half"))


class Attribution(unittest.TestCase):
    def rows(self, pkg: Path, spec: dict | None = None) -> dict:
        if spec is None:
            doc = failure_report.report(pkg, None, grades_from=[pkg])
        else:
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp) / pkg.name
                shutil.copytree(pkg, work)
                (work / "authoring/spec.json").write_text(json.dumps(spec))
                doc = failure_report.report(work, None, grades_from=[work])
        return {r["check"]: r for r in doc["checks"]}, doc

    def test_c_game_bot_o_bogus_is_fuel_formatting_not_the_thrust_step(self):
        rows, _ = self.rows(GAME)
        r = rows["o_bogus"]
        self.assertEqual(r["trap"], "t_thrust_step_limit")          # the label, kept as context
        self.assertEqual([x["test"] for x in r["failing_tests"]], ["test_corners.py::test_more_impossible_lines"])
        self.assertIn("('BOGUS', 6, 887.0) == ('BOGUS', 6, 887.5)", r["failing_tests"][0]["assertion"])
        self.assertIn("887.0", r["cause"])
        self.assertTrue(r["records_complete"])
        self.assertEqual(r["label"]["status"], "unconfirmed")
        self.assertIn("`test_audit.py::test_impossible_recordings` never failed", r["label"]["note"])

    def test_c_game_bot_replay_checks_each_fail_on_their_machine_test(self):
        rows, _ = self.rows(GAME)
        for cid, test in (("o_crash_ledge", "test_speeds_that_could_not_have_been"),
                          ("o_clip", "test_path_that_could_not_have_been"),
                          ("o_bogus_dry", "test_engine_dead_and_still_slowing"),
                          ("o_bogus_after", "test_a_line_taken_out_of_the_file")):
            only = rows[cid]["failing_tests"]
            self.assertEqual([x["test"] for x in only], [f"test_machine.py::{test}"], cid)
            self.assertEqual(only[0]["runs"], {"anthropic/claude-fable-5-1": 10, "openai/gpt-6-astra": 6})
            self.assertEqual(only[0]["source"], "grade")

    def test_school_o_class_busy_fails_on_the_rows_file(self):
        rows, _ = self.rows(SCHOOL)
        r = rows["o_class_busy"]
        self.assertEqual(len(r["failing_tests"]), 1)
        x = r["failing_tests"][0]
        self.assertEqual(x["file"], "test_class_busy_rows.js")
        self.assertEqual(x["runs"], {"anthropic/claude-fable-5-1": 10, "openai/gpt-6-astra": 10})
        self.assertEqual(x["source"], "audit")                      # node keeps no output in arena-grade.json
        self.assertFalse(any("test_class_busy.js" == (y["file"] or "") for y in r["failing_tests"]))
        self.assertEqual(r["label"]["status"], "unconfirmed")
        self.assertIn("`test_class_busy.js` was never named failing", r["label"]["note"])

    def test_a_test_another_trap_names_is_said_to_belong_to_it(self):
        spec = spec_of(GAME)
        variant = copy.deepcopy(spec)
        variant["traps"].append({"id": "t_fuel_as_printed", "type": "format_semantics", "checks": ["o_bogus"],
                                 "surface": "test_more_impossible_lines doctors the fuel column by a fraction"})
        rows, doc = self.rows(GAME, variant)
        label = rows["o_bogus"]["label"]
        self.assertEqual(label["status"], "different_trap")
        self.assertEqual(label["owner_traps"], ["t_fuel_as_printed"])
        self.assertIn("belongs to t_fuel_as_printed (format_semantics), not to the check's label t_thrust_step_limit",
                      label["note"])
        self.assertIn("format_semantics", doc["failure_trap_types"])

    def test_write_reports_the_collapse_the_cap_and_the_assertion(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / GAME.name
            shutil.copytree(GAME, pkg)
            doc = failure_report.write(pkg)                          # no release receipt: recomputed from runs/
            text = (pkg / failure_report.MARKDOWN).read_text()
            receipt = json.loads((pkg / failure_report.RECEIPT).read_text())
        self.assertEqual(receipt["schema"], failure_report.SCHEMA)
        self.assertIn("recomputed_from", doc["signal"])
        self.assertIn("**Independent hard decisions:** 2 (5 counted per trap", text)
        self.assertIn("carries 93% of all the reward the calibration lost (at most 60%)", text)
        self.assertIn("('BOGUS', 6, 887.0) == ('BOGUS', 6, 887.5)", text)
        row = failure_report.cover_row(doc)
        self.assertEqual(row["independent_hard_decisions"], 2)
        self.assertEqual(row["single_decision_share"], 0.934)
        self.assertTrue(row["why_models_failed"][0].startswith("o_crash_ledge [temporal_state; failed "))
        self.assertIn("test_machine.py::test_speeds_that_could_not_have_been", row["why_models_failed"][0])
        for key in ("check", "failed_runs", "failure_kinds", "trap", "trap_type", "what_the_task_rewards",
                    "why_it_misleads", "instruction_sentences"):
            self.assertIn(key, receipt["checks"][0])                 # every /1 reader's field is still there

    def test_a_schema_1_receipt_still_renders(self):
        _, doc = self.rows(GAME)
        old = {**doc, "checks": [{k: v for k, v in r.items() if k not in ("failing_tests", "unattributed_runs",
                                                                         "records_complete", "cause", "label")}
                                 for r in doc["checks"]], "signal": {"independent_hard": 5}}
        row = failure_report.cover_row(old)
        self.assertTrue(row["why_models_failed"][0].startswith("o_crash_ledge [temporal_state; failed "))
        self.assertIn("Taking 'could not have come from the line before it'", row["why_models_failed"][0])
        self.assertEqual(row["label_notes"], [])
        self.assertIn("| `o_bogus` |", failure_report.markdown(old))


if __name__ == "__main__":
    unittest.main()
