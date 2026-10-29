"""One review round finds everything, and a patch is accepted only when it is proven (2026-10-27).

The first bash RLE build went through six review rounds after its design; each found one
new defect, and two of the hand patches between them were wrong (a clock slack the controls
caught, a timestamp rule the reviewer forged). This covers: executable counterexamples
(counterexamples.py), the reviewer's suite inventory, reference trees, helper audit and sweep
(review.py), the gate attaching executed counterexamples (orchestrate), and the patch tool's
plan, reproduce and confirm gates (orchestration/patch_submit.py).
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import policy, review as critic
from pipeline.author import counterexamples as cx
from pipeline.tests import synthetic_code

FIXED_LINE = '        key = t.date[:7]\n'              # only the reference has it
NEW_LINE = '        key = t.date[0:7]\n'


class _Bundle(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bundle = synthetic_code.write_bundle(self.root / "bundle")
        self.steps = ["01-fix"]


class Probes(_Bundle):
    def test_a_probe_is_checked_before_anything_runs(self):
        good = cx.normalize({"kind": "false_negative", "edits": [{"file": "README.md", "old": "# ledger", "new": "# Ledger"}]},
                            self.steps)
        self.assertEqual((good["step"], good["base"]), ("01-fix", "reference"))
        self.assertTrue(good["id"].startswith("cx-"))
        for bad, why in (({"kind": "maybe", "edits": [{"file": "a", "old": "x", "new": "y"}]}, "kind"),
                         ({"kind": "forgery", "edits": []}, "at least one edit"),
                         ({"kind": "forgery", "edits": [{"file": "../etc/passwd", "old": "x", "new": "y"}]}, "inside"),
                         ({"kind": "forgery", "edits": [{"file": "a", "old": "", "new": "y"}]}, "old not empty"),
                         ({"kind": "false_negative", "expect_fails": ["o_x"],
                           "edits": [{"file": "a", "old": "x", "new": "y"}]}, "no failing check"),
                         ({"kind": "forgery", "base": "mystery", "edits": [{"file": "a", "old": "x", "new": "y"}]}, "base")):
            with self.subTest(why=why), self.assertRaisesRegex(cx.ProbeError, why):
                cx.normalize(bad, self.steps)

    def test_edits_apply_to_the_tree_the_base_leaves(self):
        probe = cx.normalize({"kind": "false_negative", "edits": [
            {"file": "ledger/report.py", "old": FIXED_LINE, "new": NEW_LINE},
            {"file": "NOTES.md", "content": "made by a probe\n"}]}, self.steps)
        files = cx.edited_files(self.bundle, probe, self.steps)
        self.assertIn(NEW_LINE, files["ledger/report.py"].decode())
        self.assertEqual(files["NOTES.md"], b"made by a probe\n")
        on_fixture = dict(probe, base="fixture")
        with self.assertRaisesRegex(cx.ProbeError, "occurs 0 times"):
            cx.edited_files(self.bundle, on_fixture, self.steps)       # the fixture lacks the reference's line

    def test_the_variant_rebuilds_exactly_that_tree_when_executed(self):
        probe = cx.normalize({"kind": "false_negative", "edits": [
            {"file": "ledger/report.py", "old": FIXED_LINE, "new": NEW_LINE}]}, self.steps)
        container = self.root / "container"
        shutil.copytree(self.bundle / "environment/repo", container)
        shell = cx.variant(self.bundle, probe, self.steps, root=str(container)).shell
        done = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(cx.APPLIED, done.stdout)
        expected = cx.edited_files(self.bundle, probe, self.steps)["ledger/report.py"]
        self.assertEqual((container / "ledger/report.py").read_bytes(), expected)
        # the reference's other file is there too: the base patch was applied first
        self.assertIn("refund", (container / "ledger/parse.py").read_text().lower())

    def test_holding_means_full_credit_for_a_correct_fix_and_named_failures_for_a_wrong_one(self):
        applied = {"applied": "shell", "exit_code": 0, "stdout": cx.APPLIED}
        full = {"applied": applied, "valid": True, "reward": 1.0, "grade": {"checks": {"o_a": {"passed": True}}}}
        part = {"applied": applied, "valid": True, "reward": 0.5,
                "grade": {"checks": {"o_a": {"passed": True}, "o_b": {"passed": False}}}}
        fn = {"kind": "false_negative", "expect_fails": []}
        fp = {"kind": "forgery", "expect_fails": ["o_b"]}
        self.assertTrue(cx.verdict(fn, full)["holds"])
        self.assertFalse(cx.verdict(fn, part)["holds"])
        self.assertTrue(cx.verdict(fp, part)["holds"])
        self.assertFalse(cx.verdict(fp, full)["holds"])
        self.assertFalse(cx.verdict({"kind": "forgery", "expect_fails": ["o_a"]}, part)["holds"], "o_a must fail too")
        self.assertIsNone(cx.verdict(fn, {**full, "applied": {"stdout": "", "exit_code": 1}})["holds"])
        self.assertIsNone(cx.verdict(fn, {**full, "valid": False})["holds"])

    def test_run_grades_each_probe_and_records_an_unbuildable_one(self):
        def verify(dest, variants, **kw):
            shell = variants["01-fix"].shell
            reward = 1.0 if kw["work"].name == "probe-ok" and "ARENA_CX_B64_EOF" in shell else 0.0
            return {"steps": {"01-fix": {"applied": {"exit_code": 0, "stdout": cx.APPLIED}, "valid": True,
                                         "reward": reward, "grade": {"checks": {"o_a": {"passed": reward == 1.0}}}}}}
        probes = [{"id": "probe-ok", "kind": "false_negative", "edits": [
                      {"file": "ledger/report.py", "old": FIXED_LINE, "new": NEW_LINE}]},
                  {"id": "probe-bad", "kind": "false_negative", "edits": [{"file": "ledger/report.py", "old": "nope", "new": "x"}]}]
        with patch.object(cx.sandbox, "build_image", return_value="tag"), \
             patch.object(cx.sandbox, "verify", side_effect=verify):
            results = {r["id"]: r for r in cx.run(self.bundle, probes, work=self.root / "work", workers=2)}
        self.assertTrue(results["probe-ok"]["holds"])
        self.assertIsNone(results["probe-bad"]["holds"])
        self.assertIn("occurs 0 times", results["probe-bad"]["error"])

    def test_a_review_s_counterexamples_become_probes(self):
        edit = {"file": "make_doc.py", "old": "a", "new": "b"}
        doc = {"checks": [{"id": "o_a", "verdict": "revise", "counterexample": {"kind": "false_negative", "edits": [edit]}},
                          {"id": "o_b", "verdict": "pass", "counterexample": {"kind": "forgery", "edits": [edit]}}],
               "substantive_gaps": [{"id": "S1", "counterexample": {"kind": "false_negative", "edits": [edit]}},
                                    {"id": "S2", "counterexample_note": "not executable"}]}
        probes = cx.from_review(doc)
        self.assertEqual([p["finding"] for p in probes], ["o_a"], "a duplicate counterexample runs once; a pass has none")


class OneRoundReview(_Bundle):
    SUITE = (
        "import re\nLIT = re.compile(r'(\\d+)\\.')\n\n"
        "def _parse(t):\n    return LIT.match(t)\n\n"
        "class Model:\n    def walk(self, t):\n        return _parse(t)\n\n"
        "def test_one():\n    assert Model().walk('1. x')\n\n"
        "def test_two():\n    assert _parse('2. y')\n\n"
        "def test_alone():\n    assert True\n")

    def tests_dir(self):
        tests = self.root / "t/tests"
        (tests / "suites/data").mkdir(parents=True)
        (tests / "suites/test_x.py").write_text(self.SUITE)
        (tests / "suites/data/measure.py").write_text("import sys\nprint(sys.argv)\n")
        (tests / "suites/data/input.csv").write_text("a,b\n")
        (tests / "checks.json").write_text(json.dumps({"checks": [
            {"id": "o_one", "runner": {"select": ["test_x.py::test_one"]}},
            {"id": "o_two", "runner": {"select": ["test_x.py::test_two"]}},
            {"id": "o_alone", "runner": {"select": ["test_x.py::test_alone"]}}]}))
        return tests

    def test_the_inventory_names_every_function_and_the_checks_that_reach_it(self):
        inv = {e["name"]: e for e in critic.suite_inventory(self.tests_dir())["entries"]}
        for name in ("test_x.py::<module>", "test_x.py::_parse", "test_x.py::Model.walk", "test_x.py::test_one",
                     "data/measure.py::<module>", "data/measure.py", "data/input.csv"):
            if name == "data/measure.py":
                continue
            self.assertIn(name, inv, sorted(inv))
        self.assertEqual(inv["test_x.py::_parse"]["checks"], ["o_one", "o_two"])
        self.assertTrue(inv["test_x.py::_parse"]["shared"], "reached through Model.walk and directly")
        self.assertEqual(inv["test_x.py::Model.walk"]["checks"], ["o_one"])
        self.assertEqual(inv["data/input.csv"]["kind"], "data")

    def test_the_review_input_carries_the_inventory_and_the_reference_tree(self):
        from pipeline.artifacts import freeze
        snapshot = self.root / "frozen"
        freeze(self.bundle, snapshot)
        with patch.object(critic, "source_provenance", return_value={}), \
             patch("pipeline.corpus.review_context", return_value={}):
            public = critic.stage_review_input(self.bundle, snapshot, self.root / "review-input")
        inv = json.loads((public / "suite-inventory.json").read_text())
        self.assertTrue(any(e["name"] == "test_report.py::<module>" for e in inv["entries"]))
        self.assertIn(FIXED_LINE.strip(), (public / "reference/01-fix/ledger/report.py").read_text())
        self.assertNotIn(FIXED_LINE.strip(), (public / "fixture/ledger/report.py").read_text())
        self.assertFalse((public / "reference/_work").exists())

    def test_an_incomplete_helper_audit_is_a_receipt_defect_the_continuation_repairs(self):
        work = self.root / "attempt"
        (work / "workspace").mkdir(parents=True)
        (work / "workspace/suite-inventory.json").write_text(json.dumps(
            {"entries": [{"name": "a.py::f"}, {"name": "a.py::g"}]}))
        self.assertEqual(critic.audit_problems(work, {"helper_audit": [{"name": "a.py::f", "verdict": "pass"},
                                                                        {"name": "a.py::g", "verdict": "defect"}]}), [])
        problems = critic.audit_problems(work, {"helper_audit": [{"name": "a.py::f", "verdict": "pass"}]})
        self.assertEqual(len(problems), 1)
        self.assertIn("a.py::g", problems[0])
        self.assertTrue(critic.recitable(["semantic reviewer requested revision", *problems]))
        self.assertEqual(critic.citation_defects(problems), problems)
        self.assertIn("helper audit", critic.RECITE_PROMPT)

    def test_the_prompt_asks_for_one_exhaustive_round_with_executable_findings(self):
        for phrase in ("ONE ROUND -- EXHAUSTIVE", "helper_audit", "counterexample", "suite-inventory.json",
                       "reference/<step>/", "two parts of the grader", "metadata the agent's own process can set"):
            self.assertIn(phrase, critic.PROMPT, phrase)

    def test_the_sweep_continues_the_same_conversation_and_its_answer_stands(self):
        root = self.root / "review"
        work = root / "attempt"
        (work / "deliverables").mkdir(parents=True)
        (work / "deliverables/review.json").write_text(json.dumps(
            {"verdict": "revise", "checks": [{"id": "o_a", "verdict": "revise"}],
             "substantive_gaps": [{"id": "S1", "affected_checks": ["o_a", "o_b"]}]}))
        calls = []

        def rollout(instruction, public, attempt, model, rid, **kw):
            calls.append((instruction, attempt, rid, kw))
            (attempt / "deliverables").mkdir(parents=True)
            (attempt / "deliverables/review.json").write_text(json.dumps({"verdict": "revise", "checks": []}))
            return SimpleNamespace(error="", stop_reason="completed", steps=4, tool_calls=3), attempt / "deliverables"

        with patch("pipeline.calibrate.run._rollout_for", return_value=rollout):
            final = critic.sweep(root, root / "review-input", work, "gpt-6-astra")
        self.assertEqual(final, root / "attempt-sweep-1")
        instruction, attempt, rid, kw = calls[0]
        self.assertEqual((rid, kw["prior"], kw["max_steps"]), ("semantic-review-sweep", work, 60))
        self.assertIn("S1 (affects o_a, o_b)", instruction)
        self.assertIn("check o_a: revise", instruction)
        self.assertEqual(len(calls), policy.REVIEW_SWEEP_PASSES)

    def test_a_sweep_that_does_not_finish_leaves_the_first_pass_standing(self):
        root = self.root / "review2"
        work = root / "attempt"
        (work / "deliverables").mkdir(parents=True)
        (work / "deliverables/review.json").write_text(json.dumps({"verdict": "pass", "checks": []}))

        def rollout(instruction, public, attempt, model, rid, **kw):
            return SimpleNamespace(error="", stop_reason="step_cap", steps=60, tool_calls=40), attempt / "deliverables"

        with patch("pipeline.calibrate.run._rollout_for", return_value=rollout):
            self.assertEqual(critic.sweep(root, root / "review-input", work, "gpt-6-astra"), work)


class TheGateExecutesCounterexamples(_Bundle):
    def test_each_finding_says_whether_execution_reproduced_it(self):
        from pipeline import orchestrate
        doc = {"substantive_gaps": [{"id": "S1", "counterexample": {"kind": "false_negative", "edits": [
            {"file": "README.md", "old": "# ledger", "new": "# Ledger"}]}}],
               "shortcut_risks": [{"id": "F1", "counterexample": {"kind": "forgery", "edits": [
                   {"file": "README.md", "old": "Monthly", "new": "monthly"}]}}],
               "ambiguities": [{"id": "A1", "counterexample": {"kind": "false_negative", "edits": [
                   {"file": "README.md", "old": "absent", "new": "x"}]}}]}
        results = [{"id": "S1-cx", "finding": "S1", "kind": "false_negative", "holds": False, "reward": 0.4, "failing": ["o_a"]},
                   {"id": "F1-cx", "finding": "F1", "kind": "forgery", "holds": True, "reward": 0.0, "failing": ["o_a"]},
                   {"id": "A1-cx", "finding": "A1", "kind": "false_negative", "holds": None, "error": "occurs 0 times"}]
        with patch.object(cx.sandbox, "available", return_value=(True, "")), \
             patch.object(cx, "run", return_value=results) as run:
            lines = orchestrate._executed_counterexamples(self.bundle, doc)
        self.assertEqual(len(run.call_args.args[1]), 3)
        self.assertIn("S1 (false_negative): REPRODUCED", lines[0])
        self.assertIn("F1 (forgery): NOT reproduced", lines[1])
        self.assertIn("A1 (false_negative): not executable: occurs 0 times", lines[2])
        record = json.loads((self.bundle / "verification/review-counterexamples.json").read_text())
        self.assertEqual(len(record["results"]), 3)
        self.assertTrue(policy.REVIEW_EXECUTE_COUNTEREXAMPLES)


class PatchTool(_Bundle):
    def setUp(self):
        super().setUp()
        import importlib
        self.tool = importlib.import_module("orchestration.patch_submit")
        self.spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        self.suite = self.spec["suites"]["test_report.py"]
        self.line = next(l for l in self.suite.splitlines() if l.strip().startswith("def test_"))

    def edit(self, old, new):
        return {"path": ["suites", "test_report.py"], "edit": {"old": old, "new": new}}

    def test_a_clean_edit_plans_without_problems(self):
        patched, problems, report = self.tool.plan_patch(
            self.spec, [self.edit(self.line, self.line + "  # checked")], self.bundle)
        self.assertEqual(problems, [])
        self.assertIn("# checked", patched["suites"]["test_report.py"])
        self.assertEqual(self.spec["suites"]["test_report.py"], self.suite, "the input spec is never changed")
        self.assertEqual(report["edited"], [["suites", "test_report.py"]])

    def test_an_edit_that_does_not_match_once_or_breaks_the_syntax_is_refused(self):
        _, problems, _ = self.tool.plan_patch(self.spec, [self.edit("no such text", "x")], self.bundle)
        self.assertIn("occurs 0 times", problems[0])
        _, problems, _ = self.tool.plan_patch(self.spec, [self.edit(self.line, self.line + "  ((")], self.bundle)
        self.assertTrue(any("no longer parses" in p for p in problems), problems)

    def test_a_patch_that_adds_an_authoring_advisory_is_refused_unless_allowed(self):
        timestamp = self.line + "\n    _floor = __import__('os').stat('.').st_mtime"
        _, problems, report = self.tool.plan_patch(self.spec, [self.edit(self.line, timestamp)], self.bundle)
        self.assertTrue(any("forgeable-metadata" in p for p in problems), problems)
        self.assertTrue(report["advisories_added"])
        _, problems, _ = self.tool.plan_patch(self.spec, [self.edit(self.line, timestamp)], self.bundle,
                                              allow=("forgeable-metadata",))
        self.assertEqual(problems, [])

    def test_probes_must_reproduce_before_and_hold_after(self):
        reproduced = {"id": "p1", "kind": "false_negative", "holds": False, "reward": 0.5, "failing": ["o_a"]}
        fixed = dict(reproduced, holds=True, reward=1.0, failing=[])
        broken = {"id": "p2", "holds": None, "error": "occurs 0 times"}
        self.assertEqual(self.tool.gate_probes([reproduced], want=False), [])
        self.assertIn("does not encode its finding", self.tool.gate_probes([fixed], want=False)[0])
        self.assertEqual(self.tool.gate_probes([fixed], want=True), [])
        self.assertIn("still fails after the patch", self.tool.gate_probes([reproduced], want=True)[0])
        self.assertIn("could not run", self.tool.gate_probes([broken], want=True)[0])

    def test_a_whole_field_value_edit_replaces_the_field(self):
        """2026-10-28: removing a trap and its mutant, re-declaring what a mutant fails."""
        traps = [{"id": "t_kept"}]
        patched, problems, report = self.tool.plan_patch(self.spec, [{"path": ["traps"], "value": traps}], self.bundle)
        self.assertEqual(problems, [])
        self.assertEqual(patched["traps"], traps)
        self.assertEqual(report["edited"], [["traps"]])
        _, problems, _ = self.tool.plan_patch(
            self.spec, [{"path": ["traps"], "value": traps, "edit": {"old": "a", "new": "b"}}], self.bundle)
        self.assertIn("not both", problems[0])

    def test_a_patch_session_is_a_repair_with_the_classifier_count(self):
        """2026-10-28: a hardened revision's patch was refused for its round's authoring floors and for the
        every-moment usable-correction fallback (16 for a seed classified with 1)."""
        from unittest.mock import patch as mock_patch
        from pipeline.author import difficulty_gate, trap_research
        self.spec["source"] = {"session_id": "s-1", "seed_index": 4, "tier": 1}
        self.spec["usable_corrections"] = 16
        (self.bundle / "authoring/spec.json").write_text(json.dumps(self.spec))
        trap_research.begin(self.bundle, 6, baseline=["t_old"])
        difficulty_gate.arm(self.bundle, "creation")
        artifacts = self.bundle.parent / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "classified.json").write_text(json.dumps({"rows": [
            {"session_id": "s-1", "seed_index": 4, "tier": 1, "classification": {"usable_moments": 1}}]}))
        with mock_patch("pipeline.config.ARTIFACTS", artifacts):
            spec = self.tool._seed_manual_session(self.bundle)
        self.assertEqual(spec["usable_corrections"], 1)
        row = json.loads((self.bundle / "authoring/manual-row.json").read_text())
        self.assertEqual(row["classification"]["usable_moments"], 1)
        self.assertEqual(trap_research.requirement(self.bundle)[0], 0)
        self.assertEqual(difficulty_gate.requirement(spec, self.bundle), [])

    def test_proven_probes_join_the_bundle_s_regression_set(self):
        a = {"id": "p1", "kind": "false_negative"}
        self.assertEqual(self.tool.load_regression(self.bundle), [])
        self.tool.save_regression(self.bundle, [a])
        self.tool.save_regression(self.bundle, [dict(a, why="again"), {"id": "p2", "kind": "forgery"}])
        rows = self.tool.load_regression(self.bundle)
        self.assertEqual([r["id"] for r in rows], ["p1", "p2"])
        self.assertEqual(rows[0]["why"], "again")


if __name__ == "__main__":
    unittest.main()
