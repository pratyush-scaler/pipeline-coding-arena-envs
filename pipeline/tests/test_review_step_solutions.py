"""The reviewer sees every step's reference, and never its own task as a peer.

Both defects surfaced in the streamlit pre-reviews of 2026-09-29: the
staging dropped `steps/<step>/solution` (12 "reference is only step 1"
findings over two two-step tasks), and a staged draft saw the live bundle
of the same seed as a competing peer ("duplicate task under new-task
metadata"). Neither could be repaired by a designer.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.artifacts import atomic_json, freeze
from pipeline.corpus import review_context
from pipeline.review import review_input_problems, stage_review_input

from .synthetic_code import write_bundle


def _two_step_bundle(root: Path) -> Path:
    return write_bundle(root / "c-001-two-step", two_steps=True)


PROVENANCE_STUB = {"schema": "stub", "verification": "not under test here"}


class ReviewStagingCarriesEveryStepsReference(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arena-review-steps-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = _two_step_bundle(self.tmp)
        self.steps = sorted(p.name for p in (self.bundle / "steps").iterdir() if p.is_dir())
        self.assertEqual(len(self.steps), 2, self.steps)
        for step in self.steps:
            self.assertTrue((self.bundle / "steps" / step / "solution/fix.patch").is_file(), step)

    def _stage(self) -> Path:
        root = self.tmp / "review"
        snapshot = root / "frozen"
        freeze(self.bundle, snapshot)
        with patch("pipeline.corpus.review_context", return_value={"peers": []}), \
             patch("pipeline.review.source_provenance", return_value=PROVENANCE_STUB):
            return stage_review_input(self.bundle, snapshot, root / "review-input")

    def test_each_steps_solution_is_staged_under_solution(self):
        public = self._stage()
        for step in self.steps:
            staged = public / "solution" / step / "fix.patch"
            self.assertTrue(staged.is_file(), f"{step} reference missing from the review input")
            self.assertEqual(staged.read_bytes(), (self.bundle / "steps" / step / "solution/fix.patch").read_bytes())
        # The root reference (what Harbor's oracle runs for the first step) is still there,
        # and the agent-visible steps/ tree still carries no solution.
        self.assertTrue((public / "solution/fix.patch").is_file())
        self.assertEqual(list((public / "steps").rglob("solution")), [])

    def test_the_receipt_binds_every_steps_solution(self):
        public = self._stage()
        work = self.tmp / "attempt"
        shutil.copytree(public, work / "workspace")
        patches = (patch("pipeline.corpus.review_context", return_value={"peers": []}),
                   patch("pipeline.review.source_provenance", return_value=PROVENANCE_STUB))
        with patches[0], patches[1]:
            errors, _ = review_input_problems(self.bundle, work)
        self.assertEqual([e for e in errors if "reviewer inputs differ" in e], [], errors)
        # A review that did not see the second step's reference is not this review.
        (work / "workspace/solution" / self.steps[1] / "fix.patch").unlink()
        with patches[0], patches[1]:
            errors, _ = review_input_problems(self.bundle, work)
        self.assertTrue(any("reviewer inputs differ" in e for e in errors), errors)


class SameTaskVersionsAreNotPeers(unittest.TestCase):
    """`review_context` scans `<workspace>/envs*`; the candidate's other exports are skipped."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="arena-corpus-"))
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.candidate = self._bundle("envs/target-F3-replay/c-002-thing", "session-x", 1)

    def _bundle(self, rel: str, session: str, seed: int, *, tweak: str = "") -> Path:
        bundle = write_bundle(self.ws / rel, two_steps=False)
        spec_path = bundle / "authoring/spec.json"
        spec = json.loads(spec_path.read_text())
        spec["source"] = {"session_id": session, "seed_index": seed, "tier": 1}
        if tweak:
            spec["novelty"] = tweak
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1))
        if tweak:
            # A different artifact hash, as a later export of the same task has.
            (bundle / "environment/repo" / f"NOTE-{tweak}.txt").write_text(tweak)
        return bundle

    def _peers(self):
        return review_context(self.candidate, workspace=self.ws)

    def test_a_staged_draft_does_not_see_its_own_live_bundle_as_a_peer(self):
        # `pre_review` reviews the scratch export (`.../scratch/019`), whose name is
        # not the task's; scanning envs* it finds the live bundle of the same
        # session and seed. That is this task, not a competitor.
        draft = self._bundle("envs/target-F3-replay/c-002-thing/verification/scratch/019", "session-x", 1,
                             tweak="draft")
        context = review_context(draft, workspace=self.ws)
        self.assertEqual(context["peers"], [])
        self.assertEqual(context["same_task_versions_skipped"], 1)
        self.assertEqual(context["design_mode"], "new_task")

    def test_the_same_name_in_a_retired_root_is_skipped(self):
        self._bundle("envs-retired/c-002-thing", "session-y", 7, tweak="retired")
        context = self._peers()
        self.assertEqual(context["peers"], [])
        self.assertEqual(context["same_task_versions_skipped"], 1)

    def test_a_different_task_from_the_same_session_is_still_a_peer(self):
        self._bundle("envs/target-F2-scenario/c-005-other", "session-x", 3, tweak="sibling")
        context = self._peers()
        self.assertEqual([p["name"] for p in context["peers"]], ["c-005-other"])
        self.assertTrue(context["peers"][0]["source_related"])
        self.assertEqual(context["same_task_versions_skipped"], 0)

    def test_a_declared_predecessor_of_the_same_task_stays_a_predecessor(self):
        from pipeline.artifacts import identity
        earlier = self._bundle("envs-retired/c-002-thing", "session-x", 1, tweak="attempt-1")
        atomic_json(self.candidate / "provenance/replacement.json", {
            "schema": "arena-pipeline/replacement/1", "reason": "attempt 1 retired: the reviewer held its tests",
            "predecessors": [identity(earlier)]})
        context = self._peers()
        self.assertEqual(context["design_mode"], "replacement")
        self.assertEqual([(p["name"], p["relationship"]) for p in context["peers"]],
                         [("c-002-thing", "predecessor")])
        self.assertEqual(context["same_task_versions_skipped"], 0)


class ControlReceiptsMayBeCited(unittest.TestCase):
    """A citation of `controls.json`, `probes.json` or `blind-solve.json` resolves at the workspace root.

    Elementor's gate review (2026-09-29 09:28) cited `controls.json` for its shadow-guard
    rows; the validator rewrote it to `fixture/controls.json`, the citation failed, and the
    review came back `unverified` with two repairs unspent.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arena-review-cite-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = write_bundle(self.tmp / "c-001-cite", two_steps=False)
        atomic_json(self.bundle / "verification/controls.json",
                    {"test_tamper": {"reward": 0.0, "note": "the untouched fixture scored nothing at all"}})

    def _errors_with(self, receipt_path: str):
        from types import SimpleNamespace
        from pipeline.review import review
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())

        def critic(_prompt, public, work, model, _tag, max_steps):
            shutil.copytree(public, work / "workspace")
            atomic_json(work / "conversation.json", [])
            atomic_json(work / "request-config.json", {"model": model, "max_steps": max_steps})
            step = sorted((work / "workspace/steps").glob("*/instruction.md"))[0]
            line = next(l for l in step.read_text().splitlines() if len(l.strip()) >= 20).strip()
            cites = [{"file": step.relative_to(work / "workspace").as_posix(), "quote": line},
                     {"file": receipt_path, "quote": "the untouched fixture scored nothing at all"}]
            atomic_json(work / "deliverables/review.json", {"verdict": "pass", "checks": [
                {"id": c["id"], "verdict": "pass",
                 "reason": "The published sentence and the executed test agree; the check grades behaviour, not wording.",
                 "citations": cites} for c in spec["checks"]],
                "ambiguities": [], "substantive_gaps": [], "shortcut_risks": [], "privacy_findings": [],
                "difficulty_assessment": "Synthetic verification only, nothing measured here.",
                "novelty_assessment": "Synthetic verification only, nothing measured here.",
                "untested_surfaces": ["No calibration."]})
            return SimpleNamespace(error=None, stop_reason="completed", tool_calls=1), None
        with patch("pipeline.calibrate.run._rollout_for", return_value=critic), \
             patch("pipeline.corpus.review_context", return_value={"peers": []}), \
             patch("pipeline.review.source_provenance", return_value=PROVENANCE_STUB):
            return review(self.bundle, "critic")["errors"]

    def test_controls_json_at_the_root_is_a_valid_citation(self):
        errors = self._errors_with("controls.json")
        self.assertEqual([e for e in errors if "invalid review citation" in e], [], errors)

    def test_the_fixture_spelling_of_a_receipt_is_still_invalid(self):
        errors = self._errors_with("fixture/controls.json")
        self.assertTrue(any("invalid review citation" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
