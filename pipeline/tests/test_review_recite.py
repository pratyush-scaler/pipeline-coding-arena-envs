"""A review discarded only for its own citations is continued, not re-bought (2026-10-26).

The first bash RLE build's gate review requested revision with valid findings but
mis-quoted one check; the pipeline called the whole review unverified and paid for a
second one. `review.recite_review` continues the same reviewer from its retained
conversation to repair the citations, and `orchestrate._review_candidate` tries that
before a fresh review.
"""
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import policy, review as critic
from pipeline.artifacts import atomic_json

THIS_RUN = ["semantic reviewer requested revision", "unresolved review findings: substantive_gaps",
            "o_visible_fixes: check not independently justified",
            "o_step_restart_keeps: invalid review citation (quote absent or too short)"]


class Recitable(unittest.TestCase):
    def test_the_first_bash_rle_review_is_recitable(self):
        self.assertTrue(critic.recitable(THIS_RUN))
        self.assertEqual(critic.citation_defects(THIS_RUN), [THIS_RUN[-1]])

    def test_a_green_or_rejection_only_review_is_not(self):
        self.assertFalse(critic.recitable([]))
        self.assertFalse(critic.recitable(THIS_RUN[:3]), "nothing to re-cite: this is a plain rejection")
        self.assertTrue(critic.semantic_rejection(THIS_RUN[:3]))

    def test_any_other_defect_means_a_fresh_review(self):
        for extra in ("review transport changed; rerun independent review", "review must disclose limitations",
                      "reviewer inputs differ from the released task", "missing reasoned difficulty_assessment"):
            with self.subTest(extra=extra):
                self.assertFalse(critic.recitable(THIS_RUN + [extra]))

    def test_every_citation_defect_shape_counts(self):
        for defect in ("o_a: invalid review citation (quote absent or too short)",
                       "o_a: passing objective lacks a valid quotation from the fixture or the instruction",
                       "o_a: objective lacks evidence citations"):
            with self.subTest(defect=defect):
                self.assertTrue(critic.recitable(["semantic reviewer requested revision", defect]))
                self.assertTrue(critic.recitable([defect]), "a passing review with one bad quote is recitable too")


class ReciteReview(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name).resolve() / "c-001-x"   # the continuation resolves the bundle, as the gate does
        self.root = self.bundle / "verification/reviews/abc123"
        self.attempt = self.root / "attempt"
        (self.attempt / "deliverables").mkdir(parents=True)
        (self.root / "review-input/fixture").mkdir(parents=True)
        (self.root / "review-input/fixture/app.py").write_text("def build():\n    return 1\n")
        atomic_json(self.attempt / "conversation.json", [{"role": "system", "content": "review"},
                                                         {"role": "user", "content": "the prompt"}])
        atomic_json(self.attempt / "deliverables/review.json", {"verdict": "revise", "checks": []})
        self.report = {"schema": "arena-pipeline/semantic-review/1", "artifact_sha256": "sha-1", "harness_sha256": "h-1",
                       "model": "gpt-6-astra", "work_dir": "verification/reviews/abc123/attempt", "errors": list(THIS_RUN)}

    def patches(self, stack, rollout, finish=None):
        stack.enter_context(patch("pipeline.review.identity", return_value="sha-1"))
        stack.enter_context(patch("pipeline.review.harness_identity", return_value="h-1"))
        stack.enter_context(patch("pipeline.calibrate.run._rollout_for", return_value=rollout))
        stack.enter_context(patch("pipeline.review.finish_review", side_effect=finish or (
            lambda bundle, sha, work, model, transport: {"artifact_sha256": sha, "model": model,
                                                         "work_dir": work.relative_to(bundle).as_posix(),
                                                         "errors": THIS_RUN[:3], "ok": False})))

    def test_the_same_reviewer_continues_from_its_conversation_and_the_report_is_resealed(self):
        seen = {}

        def rollout(instruction, fixture, work, model, rollout_id, **kw):
            seen.update(instruction=instruction, fixture=fixture, work=work, model=model, rid=rollout_id, kw=kw)
            work.mkdir(parents=True)
            return SimpleNamespace(error="", stop_reason="completed", steps=3, tool_calls=2), work / "deliverables"

        from contextlib import ExitStack
        with ExitStack() as stack:
            self.patches(stack, rollout)
            out = critic.recite_review(self.bundle, self.report)
        self.assertEqual(seen["fixture"], self.root / "review-input")
        self.assertEqual(seen["work"], self.root / "attempt-recite-1")
        self.assertEqual(seen["kw"], {"max_steps": 60, "prior": self.attempt})
        self.assertEqual((seen["model"], seen["rid"]), ("gpt-6-astra", "semantic-review-recite"))
        self.assertIn("o_step_restart_keeps: invalid review citation", seen["instruction"])
        self.assertIn("keep every verdict", seen["instruction"])
        self.assertEqual(out["work_dir"], "verification/reviews/abc123/attempt-recite-1")
        self.assertEqual(out["errors"], THIS_RUN[:3], "after the re-cite only the verdict itself remains")
        record = json.loads((self.root / "recite.json").read_text())
        self.assertEqual(record["defects"], [THIS_RUN[-1]])
        self.assertEqual(record["from"], self.report["work_dir"])

    def test_the_budget_is_one_continuation_per_review(self):
        def rollout(instruction, fixture, work, model, rollout_id, **kw):
            work.mkdir(parents=True)
            return SimpleNamespace(error="", stop_reason="completed", steps=1, tool_calls=1), work / "deliverables"

        from contextlib import ExitStack
        with ExitStack() as stack:
            self.patches(stack, rollout)
            critic.recite_review(self.bundle, self.report)
            with self.assertRaisesRegex(ValueError, "budget spent"):
                critic.recite_review(self.bundle, self.report)
        self.assertEqual(policy.REVIEW_RECITE_ATTEMPTS, 1)

    def test_a_moved_artifact_or_harness_or_nothing_to_cite_is_refused_before_any_spend(self):
        from contextlib import ExitStack
        rollout = unittest.mock.MagicMock()
        with ExitStack() as stack:
            self.patches(stack, rollout)
            with self.assertRaisesRegex(ValueError, "another artifact"):
                critic.recite_review(self.bundle, {**self.report, "artifact_sha256": "sha-2"})
            with self.assertRaisesRegex(ValueError, "another artifact"):
                critic.recite_review(self.bundle, {**self.report, "harness_sha256": "h-2"})
            with self.assertRaisesRegex(ValueError, "nothing to re-cite"):
                critic.recite_review(self.bundle, {**self.report, "errors": THIS_RUN[:3]})
        rollout.assert_not_called()

    def test_an_unfinished_continuation_leaves_the_review_unverified(self):
        def rollout(instruction, fixture, work, model, rollout_id, **kw):
            work.mkdir(parents=True)
            return SimpleNamespace(error="", stop_reason="step_cap", steps=60, tool_calls=9), work / "deliverables"

        from contextlib import ExitStack
        with ExitStack() as stack:
            self.patches(stack, rollout)
            with self.assertRaisesRegex(RuntimeError, "did not finish"):
                critic.recite_review(self.bundle, self.report)


class TheGateTriesTheReciteFirst(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name) / "c-001-x"
        (self.bundle / "authoring").mkdir(parents=True)
        atomic_json(self.bundle / "authoring/spec.json", {"_producer": {"model": "claude-opus-5-5"}, "checks": []})
        work = self.bundle / "verification/reviews/abc123/attempt"
        (work / "deliverables").mkdir(parents=True)
        atomic_json(work / "deliverables/review.json", {
            "verdict": "revise", "checks": [{"id": "o_visible_fixes", "verdict": "revise", "reason": "an unpublished gate"}],
            "ambiguities": [], "substantive_gaps": ["the vocabulary regex"], "shortcut_risks": [], "privacy_findings": []})
        self.report = {"schema": "arena-pipeline/semantic-review/1", "artifact_sha256": "sha-1", "harness_sha256": "h-1",
                       "model": "gpt-6-astra", "work_dir": "verification/reviews/abc123/attempt", "errors": list(THIS_RUN)}
        atomic_json(self.bundle / "verification/semantic-review.json", self.report)

    def test_a_recited_review_becomes_the_blocking_review_without_a_fresh_one(self):
        from pipeline import orchestrate
        recited = {**self.report, "work_dir": "verification/reviews/abc123/attempt-recite-1", "errors": THIS_RUN[:3]}
        (self.bundle / "verification/reviews/abc123/attempt-recite-1/deliverables").mkdir(parents=True)
        atomic_json(self.bundle / "verification/reviews/abc123/attempt-recite-1/deliverables/review.json",
                    json.loads((self.bundle / "verification/reviews/abc123/attempt/deliverables/review.json").read_text()))
        problems = {json.dumps(self.report, sort_keys=True): list(THIS_RUN), json.dumps(recited, sort_keys=True): THIS_RUN[:3]}
        with patch("pipeline.review.review_problems", side_effect=lambda b, r: list(problems[json.dumps(r, sort_keys=True)])), \
             patch("pipeline.review.recite_review", return_value=recited) as recite, \
             patch("pipeline.review.review") as fresh:
            out = orchestrate._review_candidate(self.bundle, nullcontext())
        recite.assert_called_once()
        fresh.assert_not_called()
        self.assertEqual(out["verdict"], "blocked")
        self.assertTrue(any("o_visible_fixes" in f for f in out["findings"]), out)
        self.assertTrue(any("substantive_gaps" in f for f in out["findings"]), out)

    def test_a_continuation_that_cannot_run_falls_back_to_a_fresh_review(self):
        from pipeline import orchestrate
        fresh_report = {**self.report, "work_dir": "verification/reviews/def456/attempt", "errors": []}
        with patch("pipeline.review.review_problems", side_effect=[list(THIS_RUN), []]), \
             patch("pipeline.review.recite_review", side_effect=ValueError("re-cite budget spent")) as recite, \
             patch("pipeline.review.review", return_value=fresh_report) as fresh:
            out = orchestrate._review_candidate(self.bundle, nullcontext())
        recite.assert_called_once()
        fresh.assert_called_once()
        self.assertEqual(out["verdict"], "green")


if __name__ == "__main__":
    unittest.main()
