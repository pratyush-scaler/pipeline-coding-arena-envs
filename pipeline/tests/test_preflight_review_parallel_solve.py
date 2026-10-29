"""The submit preflight runs the independent reviewer beside the full controls, and one
blind_solve call solves with both calibration families at once (2026-10-26).

Time, not cost: pre_review was 12% of a design session's wall clock and blind_solve
10%, sequential per family. Neither changes the gate: its own review still decides,
and a green preflight review is never reused.
"""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import policy
from pipeline.author import agent_designer


def _fake_build(spec, dest):
    """A complete-enough export: artifacts.identity needs task.toml and tests/checks.json."""
    (dest / "tests/suites").mkdir(parents=True, exist_ok=True)
    (dest / "task.toml").write_text("[task]\nname='x'\n[[steps]]\nname='01-fix'\n")
    (dest / "tests/checks.json").write_text(json.dumps({"checks": spec.get("checks") or []}))
    (dest / "tests/suites/test_a.py").write_text("def test_a():\n    pass\n")
    (dest / "steps/01-fix").mkdir(parents=True, exist_ok=True)
    (dest / "steps/01-fix/instruction.md").write_text(spec.get("instruction") or "fix it\n")


def _review_doc(verdict="revise"):
    return {"verdict": verdict,
            "checks": [{"id": "o_a", "verdict": "pass", "reason": "fine"},
                       {"id": "o_b", "verdict": "revise" if verdict == "revise" else "pass",
                        "reason": "the double only answers the reference's flag order"}],
            "ambiguities": [], "substantive_gaps": [{"issue": "step 2 drops step 1 behaviour"}] if verdict == "revise" else [],
            "shortcut_risks": [], "privacy_findings": [],
            "difficulty_assessment": "hard enough", "novelty_assessment": "new"}


class _Session(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        (self.bundle / "environment/repo").mkdir(parents=True)
        (self.bundle / "environment/repo/app.py").write_text("print(1)\n")
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle, moments=[])
        self.log = self.bundle / "authoring/log"
        self.handlers = agent_designer._handlers(self.bundle, extraction, {"messages": []},
                                                 self.bundle / "draft.json", log=self.log)
        self.handlers["stage_spec"](path=["name"], value="x-task")
        self.handlers["stage_spec"](path=["instruction"], value="fix the flag order\n")

    def clean_draft(self, stack: ExitStack):
        stack.enter_context(patch.object(agent_designer.validate_mod, "validate", return_value=[]))
        stack.enter_context(patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]))
        stack.enter_context(patch("pipeline.ledger.faithfulness.build", return_value={"schema": "x"}))
        stack.enter_context(patch.object(agent_designer.build_mod, "build", _fake_build))
        stack.enter_context(patch.object(agent_designer.sandbox, "available", return_value=(True, "ok")))
        stack.enter_context(patch.object(agent_designer.sandbox, "build_image", return_value="tag"))
        stack.enter_context(patch.object(agent_designer, "MODEL", "claude-opus-5-5"))


class ParallelBlindSolve(_Session):
    def record(self, model, reward=0.4):
        return {"schema": "arena-coding/blind-solve/2", "model": model, "step": "01-fix", "step_names": ["01-fix"],
                "steps": 4, "tool_calls": 3, "usage": {}, "patch_bytes": 40, "seconds": 1.0, "reward": reward,
                "complete": True, "by_step": {"01-fix": {"reward": reward}},
                "checks": {"o_a": True, "o_b": reward == 1.0}, "failing": {} if reward == 1.0 else {"o_b": {"reason": "x"}}}

    def test_both_families_solve_at_once_over_one_export(self):
        seen, gate = [], threading.Barrier(2, timeout=10)

        def solve(package, model, work, **_):
            seen.append((model, package, work, threading.current_thread().name))
            gate.wait()  # both solvers must be inside solve() at the same time
            return self.record(model, reward=1.0 if model.startswith("claude") else 0.4)

        with ExitStack() as stack:
            self.clean_draft(stack)
            stack.enter_context(patch("pipeline.author.blind_solve.solve", side_effect=solve))
            out = self.handlers["blind_solve"]()
        self.assertEqual(sorted(m for m, *_ in seen), ["claude-fable-5-1", "gpt-6-astra"])
        self.assertEqual(len({package for _, package, *_ in seen}), 1, "one export is shared")
        self.assertEqual(len({work for _, _, work, _ in seen}), 2, "each family has its own work directory")
        self.assertTrue(all(name.startswith("blind-solve") for *_, name in seen), seen)
        self.assertEqual(sorted(out["families"]), ["astra", "fable"])
        self.assertEqual(out["families"]["astra"]["checks"], {"o_a": True, "o_b": False})
        self.assertEqual(out["too_easy_steps"], {"astra": [], "fable": ["01-fix"]})
        self.assertEqual(out["remaining"], agent_designer._blind_budget() - 2)
        self.assertIn("TRAP", out["how_to_read"])
        for family in ("astra", "fable"):
            record = json.loads((self.bundle / f"verification/blind-solve-{family}.json").read_text())
            self.assertEqual(record["model"], agent_designer.BLIND_FAMILIES[family])
            self.assertIn("draft_sha256", record)
        numbers = sorted(json.loads(p.read_text())["n"] for p in self.log.glob("blind-solve-00?.json"))
        self.assertEqual(numbers, [1, 2])
        self.assertTrue((self.bundle / "verification/blind-solve.json").is_file())

    def test_one_family_keeps_the_flat_shape(self):
        with ExitStack() as stack:
            self.clean_draft(stack)
            solve = stack.enter_context(patch("pipeline.author.blind_solve.solve",
                                              side_effect=lambda p, model, w, **_: self.record(model)))
            out = self.handlers["blind_solve"](model="fable")
        self.assertEqual(solve.call_count, 1)
        self.assertEqual(out["family"], "fable")
        self.assertEqual(out["checks"], {"o_a": True, "o_b": False})
        self.assertNotIn("families", out)
        self.assertEqual(out["remaining"], agent_designer._blind_budget() - 1)
        self.assertTrue((self.bundle / "verification/blind-solve-fable.json").is_file())
        self.assertFalse((self.bundle / "verification/blind-solve-astra.json").exists())

    def test_one_failing_family_does_not_lose_the_other(self):
        def solve(package, model, work, **_):
            if model.startswith("gpt-"):
                raise RuntimeError("astra transport down")
            return self.record(model)

        with ExitStack() as stack:
            self.clean_draft(stack)
            stack.enter_context(patch("pipeline.author.blind_solve.solve", side_effect=solve))
            out = self.handlers["blind_solve"]()
        self.assertIn("astra transport down", out["families"]["astra"]["error"])
        self.assertEqual(out["families"]["fable"]["checks"], {"o_a": True, "o_b": False})
        self.assertTrue((self.bundle / "verification/blind-solve-fable.json").is_file())
        self.assertFalse((self.bundle / "verification/blind-solve-astra.json").exists())
        latest = json.loads((self.bundle / "verification/blind-solve.json").read_text())
        self.assertEqual(latest["model"], "claude-fable-5-1")

    def test_the_budget_counts_each_family_and_names_the_choice(self):
        with ExitStack() as stack:
            self.clean_draft(stack)
            stack.enter_context(patch("pipeline.author.blind_solve.solve",
                                      side_effect=lambda p, model, w, **_: self.record(model)))
            budget = agent_designer._blind_budget()
            for _ in range(budget // 2):
                self.assertNotIn("error", self.handlers["blind_solve"]())
            if budget % 2:  # one solve left: both is refused, one family still runs
                refused = self.handlers["blind_solve"]()
                self.assertIn("choose model='astra' or model='fable'", refused["error"])
                self.assertNotIn("error", self.handlers["blind_solve"](model="astra"))
            spent = self.handlers["blind_solve"](model="fable")
        self.assertIn("budget spent", spent["error"])
        self.assertEqual(len(list(self.log.glob("blind-solve-0??.json"))), budget)

    def test_an_unknown_model_is_refused_before_anything_runs(self):
        with ExitStack() as stack:
            self.clean_draft(stack)
            solve = stack.enter_context(patch("pipeline.author.blind_solve.solve"))
            out = self.handlers["blind_solve"](model="gemini")
        self.assertIn("model must be one of", out["error"])
        solve.assert_not_called()

    def test_images_are_built_once_before_the_solvers_start(self):
        roles = []
        with ExitStack() as stack:
            self.clean_draft(stack)
            stack.enter_context(patch.object(agent_designer.sandbox, "build_image",
                                             side_effect=lambda dest, role, **_: roles.append(role) or role))
            stack.enter_context(patch("pipeline.author.blind_solve.solve",
                                      side_effect=lambda p, model, w, **_: self.record(model)))
            self.handlers["blind_solve"]()
        self.assertEqual(roles, ["agent", "verifier"])

    def test_the_tool_and_the_prompt_say_both(self):
        tool = next(t for t in agent_designer._tools(0) if t["name"] == "blind_solve")
        self.assertEqual(tool["input_schema"]["properties"]["model"]["enum"], ["both", "astra", "fable"])
        self.assertIn("at the same time", tool["description"])
        self.assertIn("blind_solve() runs", agent_designer.DIFFICULTY_LOOP)
        self.assertEqual(agent_designer._blind_families(None), ["astra", "fable"])
        self.assertEqual(agent_designer._blind_families("Fable"), ["fable"])


class PreflightReview(_Session):
    def fake_review(self, verdict="revise", errors=None, overlap=None):
        def review(dest, model):
            if overlap is not None:
                overlap["review_thread"] = threading.current_thread().name
                overlap["review_started"].set()
                overlap["controls_seen_by_review"] = overlap["controls_started"].wait(10)
            work = dest / "verification/reviews/x/attempt"
            (work / "deliverables").mkdir(parents=True)
            (work / "deliverables/review.json").write_text(json.dumps(_review_doc(verdict)))
            return {"work_dir": work.relative_to(dest).as_posix(), "artifact_sha256": "a" * 64,
                    "errors": errors if errors is not None else
                    (["semantic reviewer requested revision"] if verdict == "revise" else [])}
        return review

    def fake_controls(self, overlap=None, errors=()):
        def run(dest, **kwargs):
            if overlap is not None:
                overlap["controls_thread"] = threading.current_thread().name
                overlap["controls_started"].set()
                overlap["review_seen_by_controls"] = overlap["review_started"].wait(10)
            self.assertFalse(kwargs.get("incremental"), "the preflight executes the full matrix")
            return {"ok": not errors, "errors": list(errors), "advisories": [], "seconds": 1.0,
                    "results": {"nop": {"reward": 0.0, "valid": True}, "mutants": {}},
                    "execution": {"mode": "full", "trials_executed": 9, "trials_reused": []}}
        return run

    def preflight(self, stack: ExitStack, review=None, controls=None):
        self.clean_draft(stack)
        stack.enter_context(patch("pipeline.review.review", side_effect=review or self.fake_review()))
        stack.enter_context(patch.object(agent_designer.controls_mod, "run", side_effect=controls or self.fake_controls()))
        return self.handlers["submit_preflight"](self.handlers["draft_spec"]())

    def test_the_review_runs_beside_the_controls_and_its_findings_hold_the_submission(self):
        overlap = {"review_started": threading.Event(), "controls_started": threading.Event()}
        with ExitStack() as stack:
            problems = self.preflight(stack, review=self.fake_review(overlap=overlap),
                                      controls=self.fake_controls(overlap=overlap))
        self.assertTrue(overlap["controls_seen_by_review"] and overlap["review_seen_by_controls"],
                        "the review and the controls must be in flight at the same time")
        self.assertEqual(overlap["controls_thread"], threading.current_thread().name)
        self.assertTrue(overlap["review_thread"].startswith("preflight-review"), overlap)
        review_lines = [p for p in problems if p.startswith("review")]
        self.assertEqual(review_lines, [
            "review (revise): o_b: the double only answers the reference's flag order",
            "review (revise): substantive_gaps/1: " + json.dumps({"issue": "step 2 drops step 1 behaviour"}, ensure_ascii=False)])
        record = json.loads((self.log / "pre-review-001.json").read_text())
        self.assertEqual((record["origin"], record["verdict"], record["artifact_sha256"]), ("preflight", "revise", "a" * 64))
        summary = json.loads((self.log / "preflight-review.json").read_text())
        self.assertEqual((summary["n"], summary["verdict"], summary["findings"], summary["skipped"]), (1, "revise", 2, None))

    def test_a_pass_holds_nothing_and_harness_problems_still_surface(self):
        with ExitStack() as stack:
            problems = self.preflight(stack, review=self.fake_review(verdict="pass"))
        self.assertEqual([p for p in problems if p.startswith("review")], [])
        with ExitStack() as stack:
            problems = self.preflight(stack, review=self.fake_review(verdict="pass", errors=["review cited no source"]))
        self.assertEqual([p for p in problems if p.startswith("review")], ["review harness: review cited no source"])

    def test_a_review_that_cannot_run_never_holds_the_submission(self):
        def broken(dest, model):
            raise RuntimeError("reviewer transport down")
        with ExitStack() as stack:
            problems = self.preflight(stack, review=broken)
        self.assertEqual([p for p in problems if p.startswith("review")], [])
        summary = json.loads((self.log / "preflight-review.json").read_text())
        self.assertIn("reviewer transport down", summary["error"])
        self.assertEqual(summary["n"], 1, "the attempt still counts against the budget")

    def test_an_artifact_the_designer_already_pre_reviewed_is_not_reviewed_again(self):
        with ExitStack() as stack:
            self.clean_draft(stack)
            review = stack.enter_context(patch("pipeline.review.review", side_effect=self.fake_review()))
            stack.enter_context(patch.object(agent_designer.controls_mod, "run", side_effect=self.fake_controls()))
            stack.enter_context(patch("pipeline.artifacts.identity", return_value="a" * 64))
            first = self.handlers["pre_review"]()
            self.assertEqual(first["verdict"], "revise")
            problems = self.handlers["submit_preflight"](self.handlers["draft_spec"]())
        self.assertEqual(review.call_count, 1)
        self.assertEqual([p for p in problems if p.startswith("review")], [])
        summary = json.loads((self.log / "preflight-review.json").read_text())
        self.assertIn("pre-review 001 already read this exact artifact", summary["skipped"])

    def test_a_changed_artifact_is_reviewed_again(self):
        with ExitStack() as stack:
            self.clean_draft(stack)
            review = stack.enter_context(patch("pipeline.review.review", side_effect=self.fake_review()))
            stack.enter_context(patch.object(agent_designer.controls_mod, "run", side_effect=self.fake_controls()))
            self.handlers["pre_review"]()
            self.handlers["stage_spec"](path=["instruction"], value="fix the flag order, and the sort\n")
            self.handlers["submit_preflight"](self.handlers["draft_spec"]())
        self.assertEqual(review.call_count, 2)
        self.assertEqual(json.loads((self.log / "pre-review-002.json").read_text())["origin"], "preflight")

    def test_static_problems_skip_the_review(self):
        with ExitStack() as stack:
            self.clean_draft(stack)
            stack.enter_context(patch.object(agent_designer.validate_mod, "validate", return_value=["no checks"]))
            review = stack.enter_context(patch("pipeline.review.review", side_effect=self.fake_review()))
            stack.enter_context(patch.object(agent_designer.controls_mod, "run", side_effect=self.fake_controls()))
            problems = self.handlers["submit_preflight"](self.handlers["draft_spec"]())
        self.assertIn("static: no checks", problems)
        review.assert_not_called()
        self.assertFalse((self.log / "preflight-review.json").exists())

    def test_a_spent_budget_skips_the_review(self):
        from pipeline.config import MAX_PRE_REVIEWS_PER_SESSION
        with ExitStack() as stack:
            stack.enter_context(patch.object(policy, "SUBMIT_PREFLIGHT_RESERVED_REVIEWS", 0))  # no reserve: spend it all
            self.clean_draft(stack)
            review = stack.enter_context(patch("pipeline.review.review", side_effect=self.fake_review()))
            stack.enter_context(patch.object(agent_designer.controls_mod, "run", side_effect=self.fake_controls()))
            for n in range(MAX_PRE_REVIEWS_PER_SESSION):
                self.handlers["stage_spec"](path=["instruction"], value=f"fix the flag order, draft {n}\n")
                self.assertNotIn("error", self.handlers["pre_review"]())
            self.handlers["stage_spec"](path=["instruction"], value="fix the flag order, final\n")
            self.handlers["submit_preflight"](self.handlers["draft_spec"]())
        self.assertEqual(review.call_count, MAX_PRE_REVIEWS_PER_SESSION)
        summary = json.loads((self.log / "preflight-review.json").read_text())
        self.assertIn("budget spent", summary["skipped"])

    def test_the_policy_switch_turns_it_off(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(policy, "SUBMIT_PREFLIGHT_REVIEW", False))
            self.clean_draft(stack)
            review = stack.enter_context(patch("pipeline.review.review", side_effect=self.fake_review()))
            stack.enter_context(patch.object(agent_designer.controls_mod, "run", side_effect=self.fake_controls()))
            self.handlers["submit_preflight"](self.handlers["draft_spec"]())
        review.assert_not_called()

    def test_the_designer_is_told(self):
        self.assertIn("beside the full controls", agent_designer.SYSTEM)
        tool = next(t for t in agent_designer._tools(0) if t["name"] == "pre_review")
        self.assertIn("preflight", tool["description"])


class CapturedDiffShape(unittest.TestCase):
    """2026-10-26: both families' first solves of a docx generator were refused because
    `diff -ruN` reported the regenerated binaries before the first hunk."""

    def test_binary_and_only_in_lines_before_the_first_hunk_are_a_whole_diff(self):
        from pipeline.author.blind_solve import looks_like_unified_diff
        self.assertTrue(looks_like_unified_diff("Binary files a/out.docx and b/out.docx differ\n"
                                                "Only in b/assets: new.png\n"
                                                "diff -ruN a/make.py b/make.py\n--- a/make.py\n+++ b/make.py\n@@ -1 +1 @@\n-x\n+y\n"))
        self.assertTrue(looks_like_unified_diff("--- a/make.py\n+++ b/make.py\n@@ -1 +1 @@\n-x\n+y\n"))
        self.assertTrue(looks_like_unified_diff(""), "no change is not a partial patch")
        self.assertTrue(looks_like_unified_diff("Binary files a/out.docx and b/out.docx differ\n"),
                        "only binaries changed: nothing to apply, nothing partial")

    def test_a_capture_that_starts_mid_hunk_is_still_refused(self):
        from pipeline.author.blind_solve import looks_like_unified_diff
        self.assertFalse(looks_like_unified_diff("+    return value\n@@ -40,3 +41,4 @@\n"))
        self.assertFalse(looks_like_unified_diff("Binary files a/x and b/x differ\n+    tail of a hunk\n"))


if __name__ == "__main__":
    unittest.main()
