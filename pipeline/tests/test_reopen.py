"""A review-blocked build gets exactly ONE more repair pass, on the record (user, 2026-10-04:
"Reopen all, 1 extra pass")."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import checkpoint, policy, reopen, repair


def blocked_bundle(root: Path) -> Path:
    bundle = root / "c-013-static"
    (bundle / "verification/design-attempts").mkdir(parents=True)
    draft = bundle / "verification/design-attempts/1.json"
    draft.write_text(json.dumps({"checks": []}))
    (bundle / "verification/state.json").write_text(json.dumps({
        "status": "blocked", "blockers": ["Semantic review o_cap: an unpublished presentation restriction"]}))
    (bundle / repair.LOG).write_text(json.dumps({"schema": "arena-pipeline/repair-log/1", "passes": [
        {"index": 1, "findings": ["x"], "fingerprints": ["x"], "outcome": "charged"},
        {"index": 2, "findings": ["y"], "fingerprints": ["y"], "outcome": "charged"}]}))
    checkpoint.update(bundle, stage="blocked", gate_repairs_remaining=0, authoring_session=None,
                      submitted={"path": "verification/design-attempts/1.json",
                                 "sha256": hashlib.sha256(draft.read_bytes()).hexdigest()},
                      workspace_files=None)
    return bundle


class Reopen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch("pipeline.checkpoint.evidence_inventory", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_pass_is_granted_and_recorded(self):
        bundle = blocked_bundle(self.root)
        self.assertEqual(checkpoint.repairs_left(bundle), 0)
        self.assertTrue(reopen.reopen(bundle).startswith("reopened"))
        log = repair.history(bundle)
        self.assertEqual(repair.passes_granted(bundle), 1)
        self.assertEqual(log["passes"][-1]["outcome"], "granted")
        state = checkpoint.read(bundle)
        self.assertEqual(state["stage"], "repair")
        self.assertEqual(state["gate_repairs_remaining"], 0, "no pass beyond the granted one")
        self.assertIn("o_cap", " ".join(state["feedback"]))
        self.assertIn("careful human", state["feedback"][0])
        self.assertEqual(checkpoint.repairs_left(bundle), 0, "the granted pass is spent by the resume")
        self.assertIsNotNone(checkpoint.resumable(bundle))

    def test_it_never_reopens_twice_or_a_validated_task(self):
        bundle = blocked_bundle(self.root)
        reopen.reopen(bundle)
        self.assertEqual(reopen.reopen(bundle), "skip: already reopened")
        other = blocked_bundle(self.root / "b")
        (other / "verification/state.json").write_text(json.dumps({"status": "validated"}))
        self.assertEqual(reopen.reopen(other), "skip: status validated")

    def test_a_non_review_blocker_is_left_alone(self):
        bundle = blocked_bundle(self.root)
        (bundle / "verification/state.json").write_text(json.dumps({
            "status": "blocked", "blockers": ["repair refused: repair budget exhausted"]}))
        with mock.patch("pipeline.review.review_findings", return_value=[]):
            self.assertEqual(reopen.reopen(bundle), "skip: not blocked by the semantic review")

    def test_the_admission_check_honours_grants(self):
        bundle = blocked_bundle(self.root)
        self.assertEqual(policy.MAX_REPAIR_PASSES, 2)
        ok, why = repair.admissible(bundle, ["Semantic review o_new: z"])
        self.assertFalse(ok)
        log = repair.history(bundle)
        log["grants"] = [{"passes": 1}]
        (bundle / repair.LOG).write_text(json.dumps(log))
        ok, why = repair.admissible(bundle, ["Semantic review o_new: z"])
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
