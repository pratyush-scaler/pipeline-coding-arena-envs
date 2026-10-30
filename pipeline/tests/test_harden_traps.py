"""Hardening may add traps -- the operator's and the designer's -- but never
at the cost of the task's faithfulness level or its trace-derived share."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import policy

from pipeline.harden import (HARDENING_BRIEF, budget_problems, faithfulness_budget,
                             propose, requested_traps)


def trap(i, provenance):
    row = {"id": f"t{i}", "type": "plausible_decoy", "provenance": provenance,
           "surface": "s", "beatable_by": "b", "probe": "plausible_wrong", "checks": ["c1"]}
    if provenance == "trace_derived":
        row.update(derived_from=f"sess1234::msg{i}", quote="the user's words")
    else:
        row.update(grounded_in="doc.md", grounding="a practitioner meets this")
    return row


def bundle_with(root, derived, authored, fidelity=1.0, size=90_000, category="A"):
    traps = [trap(i, "trace_derived") for i in range(derived)] + \
            [trap(100 + i, "authored") for i in range(authored)]
    (root / "authoring").mkdir(parents=True)
    (root / "authoring/traps.json").write_text(json.dumps({"traps": traps}))
    (root / "authoring/spec.json").write_text(json.dumps(
        {"traps": traps, "source": {"session_id": "sess1234", "moments": [1] * derived}}))
    return {"faithfulness_by_bytes": fidelity, "bytes": {"lifted": size}, "category": category}


class FaithfulnessBudget(unittest.TestCase):
    def budget(self, *args, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            return faithfulness_budget(root, bundle_with(root, *args, **kw))

    def test_f3_room_is_what_keeps_the_trace_derived_share_at_70_percent(self):
        b = self.budget(10, 1)                 # 10/11 today; 10/14 = 71% still F3
        self.assertEqual((b["level"], b["authored_traps_left"]), ("F3", 3))

    def test_f3_bytes_keep_fidelity_at_its_floor(self):
        b = self.budget(10, 1, size=90_000)    # 90,000 / (90,000 + 10,000) = 0.90
        self.assertEqual(b["authored_bytes_left"], 10_000)

    def test_f2_share_is_protected_even_when_fidelity_carries_the_level(self):
        b = self.budget(3, 7)                  # 30% already: no room for authored traps
        self.assertEqual((b["level"], b["authored_traps_left"]), ("F2", 0))

    def test_category_floor_caps_f2_bytes(self):
        loose = self.budget(3, 7, category="D")   # D floor 0.60
        tight = self.budget(3, 7, category="A")   # A floor 0.90
        self.assertLess(tight["authored_bytes_left"], loose["authored_bytes_left"])


class BudgetIsEnforced(unittest.TestCase):
    budget = {"level": "F3", "authored_traps_left": 1, "authored_bytes_left": 500}
    before = [trap(0, "trace_derived"), trap(1, "trace_derived")]

    def test_within_budget_passes(self):
        self.assertEqual(budget_problems(self.budget, self.before,
                                         self.before + [trap(100, "authored")], 400), [])

    def test_too_many_authored_traps_are_refused(self):
        after = self.before + [trap(100, "authored"), trap(101, "authored")]
        self.assertTrue(any("room for 1" in p for p in budget_problems(self.budget, self.before, after, 0)))

    def test_a_real_correction_cannot_be_dropped_or_relabelled(self):
        relabelled = [trap(0, "trace_derived"), {**trap(1, "authored"), "id": "t1"}]
        self.assertTrue(any("t1" in p for p in budget_problems(self.budget, self.before, relabelled, 0)))

    def test_too_many_bytes_are_refused(self):
        self.assertTrue(budget_problems(self.budget, self.before, self.before, 501))


class OperatorTraps(unittest.TestCase):
    def test_ideas_come_from_flags_and_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "traps.json"
            path.write_text(json.dumps(["a superseded quote", {"idea": "a decoy vendor", "grounding": "invoice.csv"}]))
            self.assertEqual(requested_traps([" a retracted figure "], str(path)),
                             ["a retracted figure", "a superseded quote",
                              "a decoy vendor (grounding: invoice.csv)"])
            path.write_text(json.dumps([{"no": "idea"}]))
            with self.assertRaises(ValueError):
                requested_traps([], str(path))

    def test_the_brief_carries_the_ideas_the_budget_and_the_legitimacy_rules(self):
        spec = {"source": {"session_id": "s", "seed_index": 0}, "checks": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = bundle_with(root, 10, 1)
            with patch("pipeline.repair.design_inputs", return_value=(spec, ledger, {})), \
                 patch("pipeline.author.extract.extract", return_value=object()), \
                 patch("pipeline.harden._breadth_summary", return_value="  -"), \
                 patch("pipeline.author.agent_designer.design", return_value=dict(spec)) as design:
                propose(root, [], requested=["a decoy that is correct under the superseded rule"])
        brief = design.call_args.kwargs["feedback"][0]
        self.assertIn("a decoy that is correct under the superseded rule", brief)
        self.assertIn("authored traps you may add: no ceiling", brief)
        for rule in ("grounded", "resolvable", "beatable", "No hidden rules", "as deceptive as the EVIDENCE",
                     "requirements themselves stay perfectly clear"):
            self.assertIn(rule, brief)

    def _propose(self, **kw):
        spec = {"source": {"session_id": "s", "seed_index": 0}, "checks": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = bundle_with(root, 10, 1)
            with patch("pipeline.repair.design_inputs", return_value=(spec, ledger, {})), \
                 patch("pipeline.author.extract.extract", return_value=object()), \
                 patch("pipeline.harden._breadth_summary", return_value="  -"), \
                 patch("pipeline.harden.review_repairs", return_value=[]), \
                 patch("pipeline.release_blockers.write_context"), \
                 patch("pipeline.release_blockers.brief", return_value="\nRELEASE BLOCKERS: [CRIT 1] ..."), \
                 patch("pipeline.author.agent_designer.design", return_value=dict(spec)) as design:
                propose(root, [], **kw)
        return design.call_args

    def test_a_release_repair_is_a_patch_round(self):
        from pipeline import harden
        call = self._propose(requested=[harden.RELEASE_PREFIX + json.dumps([{"id": f"CRIT {i}"} for i in range(28)])])
        feedback = call.kwargs["feedback"]
        self.assertEqual(feedback[0], policy.PATCH_FIRST)
        self.assertIn("RELEASE REPAIR, A PATCH ROUND", feedback[1])
        self.assertNotIn("Aim for pass@1", feedback[1])
        self.assertEqual(call.kwargs["max_steps"], policy.REVISION_MAX_STEPS + 28 * policy.RELEASE_REPAIR_STEPS_PER_BLOCKER)
        self.assertEqual(call.args[0]["_harden_kind"], "release_repair")

    def test_a_validity_repair_is_a_patch_round_and_a_hardening_round_is_not(self):
        call = self._propose(kind="repair")
        self.assertEqual(call.kwargs["feedback"][0], policy.PATCH_FIRST)
        self.assertEqual(call.kwargs["max_steps"], policy.REVISION_MAX_STEPS)
        call = self._propose(requested=["a decoy that is correct under the superseded rule"])
        self.assertNotEqual(call.kwargs["feedback"][0], policy.PATCH_FIRST)
        self.assertNotIn("max_steps", call.kwargs)
        self.assertEqual(call.args[0]["_harden_kind"], "harden")

    def test_without_requests_the_brief_has_no_empty_section(self):
        self.assertNotIn("{requested}", HARDENING_BRIEF.format(
            targets="", breadth="", requested="", budget=""))


class CliForwardsTrapOptions(unittest.TestCase):
    def test_every_harden_option_reaches_the_module(self):
        from pipeline import cli
        with patch.object(cli, "_run", return_value=0) as run, patch.object(cli, "load_keys"):
            import argparse
            args = argparse.Namespace(candidates=False, root=None, bundle="b", apply=True,
                                      compare=None, trap=["x", "y"], traps="t.json", no_screen=True)
            cli.cmd_harden(args)
        forwarded = run.call_args.args[1:]
        self.assertEqual(forwarded.count("--trap"), 2)
        self.assertIn("x", forwarded)
        self.assertIn("--traps", forwarded)
        self.assertIn("--no-screen", forwarded)


class NewTrapsAffectObjectives(unittest.TestCase):
    def test_one_genuine_objective_is_enough_but_guardrails_alone_are_not(self):
        from pipeline.harden import routing_problems
        checks = [{"id": "c1", "kind": "objective"}, {"id": "c2", "kind": "objective"},
                  {"id": "g1", "kind": "guardrail"}]
        old = [trap(0, "trace_derived")]
        weak = {**trap(100, "authored"), "checks": ["c1", "g1"]}      # one objective + a guardrail
        strong = {**trap(101, "authored"), "checks": ["c1", "c2"]}
        self.assertEqual(routing_problems(old, old + [weak], checks), [])
        self.assertTrue(routing_problems(old, old + [{**weak, "checks": ["g1"]}], checks))
        self.assertEqual(routing_problems(old, old + [strong], checks), [])
        self.assertEqual(routing_problems(old, old, checks), [])       # existing traps untouched
