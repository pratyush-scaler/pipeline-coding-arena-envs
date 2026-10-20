"""Spec Appendix A (multi-turn tasks) and the user's 2026-10-12 rule "at least 3 turns".

Covers: operator-pinned and policy step budgets, the turn citation rules
(`trace_turn` against the extraction's user-turn index), per-turn density
floors, the earlier-prompt phrases, /arena-history in suites, and the two
executed controls (prefix_only / skip_prior) as `_evaluate` judges them.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import orchestrate, policy
from pipeline.author import controls, validate
from pipeline.tests import synthetic_code


class StepBudgets(unittest.TestCase):
    def rows(self, n=20, later=0):
        return [{"classification": {"classes": {"spec_correction": later}, "usable_moments": 1}} for _ in range(n)]

    def test_operator_budgets_win_and_are_clamped(self):
        rows = self.rows(5)
        rows[0]["operator_step_budget"] = 3
        rows[1]["operator_step_budget"] = 9      # clamped to the policy maximum
        rows[2]["operator_step_budget"] = 2      # a multi-turn budget is never below the minimum
        self.assertEqual(orchestrate.assign_step_budgets(rows), 3)
        self.assertEqual([r["step_budget"] for r in rows],
                         [3, policy.MULTI_STEP_MAX_STEPS, policy.MULTI_STEP_MIN_STEPS, 1, 1])

    def test_policy_designation_needs_enough_later_changes_for_three_turns(self):
        one_change = self.rows(20, later=1)
        self.assertEqual(orchestrate.assign_step_budgets(one_change), 0)
        two_changes = self.rows(20, later=2)
        self.assertEqual(orchestrate.assign_step_budgets(two_changes), 2)
        budgets = sorted(r["step_budget"] for r in two_changes)
        self.assertEqual(budgets[-2:], [policy.MULTI_STEP_MIN_STEPS] * 2)
        self.assertEqual(set(budgets[:-2]), {1})


class TurnRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = synthetic_code.write_bundle(Path(self.tmp.name) / "b", two_steps=True)
        self.spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        self.checks = self.spec["checks"]

    def tearDown(self):
        self.tmp.cleanup()

    def problems(self, spec):
        return validate.multi_turn_problems(spec, self.bundle, spec["checks"])

    def test_the_synthetic_multi_turn_spec_is_clean(self):
        self.assertEqual(self.problems(self.spec), [])

    def test_a_turn_without_a_citation_is_refused(self):
        spec = copy.deepcopy(self.spec)
        del spec["steps"][1]["trace_turn"]
        self.assertTrue(any("trace_turn must be" in p for p in self.problems(spec)))

    def test_turns_must_be_sequential_user_messages_after_the_pivot(self):
        spec = copy.deepcopy(self.spec)
        spec["steps"][1]["trace_turn"] = 3            # same message as turn 1
        self.assertTrue(any("does not come after" in p for p in self.problems(spec)))
        spec["steps"][1]["trace_turn"] = 8            # not a user message
        self.assertTrue(any("not a user message" in p for p in self.problems(spec)))
        spec["steps"][0]["trace_turn"], spec["steps"][1]["trace_turn"] = 0, 1   # turn 2 before the pivot (2)
        found = self.problems(spec)
        self.assertTrue(any("precedes the fixture's pivot" in p for p in found), found)

    def test_missing_turn_index_is_refused(self):
        (self.bundle / "authoring/extraction/user-turns.json").unlink()
        self.assertTrue(any("user-turns.json" in p for p in self.problems(self.spec)))

    def test_a_thin_turn_is_refused(self):
        spec = copy.deepcopy(self.spec)
        spec["checks"] = [c for c in spec["checks"] if c["id"] not in ("o2_malformed_not_counted",)]
        self.assertTrue(any("objective(s); a multi-turn task needs at least" in p for p in self.problems(spec)))
        spec = copy.deepcopy(self.spec)
        spec["traps"] = [t for t in spec["traps"] if t["id"] != "t_step2_order"]
        self.assertTrue(any("trap(s) drive its checks" in p for p in self.problems(spec)))

    def test_suites_may_not_read_arena_history(self):
        spec = copy.deepcopy(self.spec)
        spec["suites"]["test_counts.py"] += "\nHIST = '/arena-history/01-fix/app'\n"
        self.assertTrue(any("arena-history" in p for p in self.problems(spec)))

    def test_designated_multi_turn_needs_three_steps(self):
        spec = copy.deepcopy(self.spec)
        spec["step_budget"] = 3
        issues = validate.validate(spec, self.bundle)
        self.assertTrue(any("needs at least 3 turns" in p for p in issues), issues)

    def test_earlier_prompt_phrases(self):
        for text in ("as I asked in my previous message", "do it like I said", "same rules as before",
                     "redo step 2 with this", "the second turn's export"):
            self.assertTrue(validate.history_reference_problems(text, "s", 2), text)
        for text in ("the export should also include totals", "keep everything else as it is now"):
            self.assertEqual(validate.history_reference_problems(text, "s", 2), [], text)


class TurnControls(unittest.TestCase):
    """`_evaluate` over synthetic records: the verdicts on prefix_only and skip_prior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = synthetic_code.write_bundle(Path(self.tmp.name) / "b", two_steps=True)
        self.spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        from pipeline.author.checks import load_shipped
        self.doc = load_shipped(self.bundle / "tests/checks.json")
        self.steps = [s["name"] for s in self.spec["steps"]]

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, flags_by_step, stdout=""):
        steps = {}
        for step in self.steps:
            flags = flags_by_step.get(step, {})
            objectives = [c for c in self.doc["checks"] if c["step"] == step and c["kind"] == "objective"]
            passed = sum(1 for c in objectives if flags.get(c["id"], False))
            reward = round(passed / len(objectives), 4) if objectives else 0.0
            steps[step] = {"valid": True, "reward": reward, "applied": {"stdout": stdout},
                           "grade": {"checks": {c["id"]: {"passed": flags.get(c["id"], c["kind"] == "guardrail")}
                                                for c in self.doc["checks"] if c["step"] == step}}}
        return {"valid": True, "reward": round(sum(s["reward"] for s in steps.values()) / len(steps), 4),
                "steps": steps}

    def verdicts(self, prefix_flags, skip_flags, skip_stdout):
        step2 = self.steps[1]
        everything = {c["id"]: True for c in self.doc["checks"]}
        nothing = {c["id"]: c["kind"] == "guardrail" for c in self.doc["checks"]}
        records = {"nop": self.record({s: nothing for s in self.steps}),
                   "golden-1": self.record({s: everything for s in self.steps}),
                   f"prefix_only:{step2}": self.record({self.steps[0]: everything, step2: prefix_flags}),
                   f"skip_prior:{step2}": self.record({self.steps[0]: nothing, step2: skip_flags}, skip_stdout)}
        results, errors, _ = controls._evaluate(self.bundle, self.spec, self.doc, self.steps,
                                                {"name": "python"}, 1, _complete(records, self.spec, self))
        return results, [e for e in errors if e.startswith(("prefix_only", "skip_prior"))]

    def test_a_turn_that_needs_new_work_and_earlier_state_passes(self):
        nothing = {c["id"]: c["kind"] == "guardrail" for c in self.doc["checks"]}
        partial = dict(nothing, o2_refund_counted=True)
        results, errors = self.verdicts(nothing, partial, controls.SKIP_PRIOR_APPLIED)
        self.assertEqual(errors, [])
        self.assertIn(f"skip_prior:{self.steps[1]}", results["turns"])

    def test_free_credit_at_a_later_turn_is_refused(self):
        nothing = {c["id"]: c["kind"] == "guardrail" for c in self.doc["checks"]}
        free = dict(nothing, o2_counts_two_years_separate=True)
        _, errors = self.verdicts(free, nothing, controls.SKIP_PRIOR_NOT_APPLIED)
        self.assertTrue(any(e.startswith("prefix_only") and "o2_counts_two_years_separate" in e for e in errors))

    def test_a_turn_independent_of_earlier_state_is_refused(self):
        nothing = {c["id"]: c["kind"] == "guardrail" for c in self.doc["checks"]}
        everything = {c["id"]: True for c in self.doc["checks"]}
        _, errors = self.verdicts(nothing, everything, controls.SKIP_PRIOR_APPLIED)
        self.assertTrue(any(e.startswith("skip_prior") for e in errors))

    def test_single_step_bundles_plan_no_turn_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = synthetic_code.write_bundle(Path(tmp) / "one")
            spec = json.loads((bundle / "authoring/spec.json").read_text())
            from pipeline.author.checks import load_shipped
            doc = load_shipped(bundle / "tests/checks.json")
            with unittest.mock.patch.object(controls.sandbox, "solve_variant",
                                            lambda b, s: controls.sandbox.Variant("nop")):
                plan = controls._trial_plan(bundle, spec, doc, [s["name"] for s in spec["steps"]],
                                            {"name": "python"}, 1)
            self.assertFalse(any(k.startswith(("prefix_only", "skip_prior")) for k in plan))


def _complete(records: dict, spec: dict, case) -> dict:
    """Fill the labels `_evaluate` always reads (alternative, mutants, attacks) with neutral records."""
    everything = {c["id"]: True for c in case.doc["checks"]}
    nothing = {c["id"]: c["kind"] == "guardrail" for c in case.doc["checks"]}
    out = dict(records)
    out.setdefault("alternative", case.record({s: everything for s in case.steps}))
    for mutant in spec.get("mutants") or []:
        out.setdefault(f"mutant:{mutant['id']}", case.record({s: everything for s in case.steps}))
    for label in ("empty", "destructive", "test_tamper", "early_exit", "report_forge", "benign_test",
                  "neutral_additions"):
        out.setdefault(label, case.record({s: (everything if label in ("benign_test", "neutral_additions") else nothing)
                                           for s in case.steps}))
    return out


import unittest.mock  # noqa: E402  (used above through unittest.mock.patch)

if __name__ == "__main__":
    unittest.main()


class VariantApplyRouting(unittest.TestCase):
    """A patch variant that does not apply is a repairable design defect, not a terminal build error."""

    def test_unapplied_alternative_becomes_a_preflight_finding(self):
        from pipeline.author import sandbox
        with tempfile.TemporaryDirectory() as tmp:
            bundle = synthetic_code.write_bundle(Path(tmp) / "b", two_steps=True)
            ok = {"valid": True, "reward": 1.0, "steps": {"01-fix": {"reward": 1.0}, "02-counts": {"reward": 1.0}}}
            nop = {"valid": True, "reward": 0.0, "steps": {}}

            def fake_verify(bundle_, variants, **kw):
                label = kw["work"].name
                if label == "alternative":
                    raise sandbox.VariantApplyError("variant patch 'alt:02-counts' did not apply: error: patch failed")
                return nop if label == "nop" else ok

            plan = {"nop": {}, "golden-1": {}, "alternative": {}}
            with unittest.mock.patch.object(controls.sandbox, "verify", fake_verify):
                with self.assertRaises(controls._ControlPreflightFailure) as caught:
                    controls._execute_trials(bundle, plan, Path(tmp) / "work", "a", "v", 1)
            self.assertTrue(any("alternative" in e and "did not apply" in e for e in caught.exception.errors),
                            caught.exception.errors)
