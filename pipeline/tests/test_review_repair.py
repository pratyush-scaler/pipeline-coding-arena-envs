"""Hardening repairs what the independent review found, without trading it for difficulty.

User, 2026-10-04: "Fix all the tasks based on the review, and some ambiguity is
required in the task, as far as the task remains correct and is understandable and
performable by a human carefully, the task should be valid." -- and the hardness
must be maintained (every step at or under the bar on both models).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import harden, policy, review

WORK = "verification/reviews/r1/attempt"


def reviewed(bundle: Path, doc: dict, sha: str = "sha-current") -> None:
    (bundle / WORK / "deliverables").mkdir(parents=True)
    (bundle / WORK / "deliverables/review.json").write_text(json.dumps(doc))
    (bundle / "verification/semantic-review.json").write_text(
        json.dumps({"artifact_sha256": sha, "work_dir": WORK}))


REVISE = {
    "verdict": "revise",
    "checks": [{"id": "o_rows", "verdict": "revise", "reason": "tuple rows are never published"},
               {"id": "o_walls", "verdict": "pass", "reason": "published"}],
    "ambiguities": [{"id": "A1", "reason": "row container left open",
                     "citations": [{"file": "steps/01/instruction.md", "quote": "une liste de lignes"}]}],
    "substantive_gaps": [{"id": "G1", "finding": "an attack listed as a semantic mutant",
                          "citations": [{"file": "authoring/mutants/m_attack.patch", "quote": "x"}]}],
    "shortcut_risks": [], "privacy_findings": [],
}


class ReviewFindings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "c-029"
        (self.root / "verification").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_validity_findings_and_bookkeeping_are_told_apart(self):
        reviewed(self.root, REVISE)
        with mock.patch.object(review, "identity", return_value="sha-current"):
            found = review.review_findings(self.root)
        self.assertTrue(found[0].startswith("check o_rows:"))
        self.assertFalse(any("o_walls" in f for f in found))
        self.assertTrue(any(f.startswith("ambiguities:") for f in found))
        self.assertTrue(any(f.startswith("authoring bookkeeping:") for f in found))

    def test_a_review_of_other_bytes_says_nothing(self):
        reviewed(self.root, REVISE, sha="sha-old")
        with mock.patch.object(review, "identity", return_value="sha-current"):
            self.assertEqual(review.review_findings(self.root), [])

    def test_an_unreviewed_revision_inherits_its_reviewed_parents_findings(self):
        v2, v3 = self.root / "hardened/v2", self.root / "hardened/v3"
        for v in (v2, v3):
            (v / "verification").mkdir(parents=True)
        reviewed(v2, REVISE)
        (self.root / "hardened/hardening.json").write_text(json.dumps({
            "status": "open", "attempts": [{"from": ".", "revision": "hardened/v2", "outcome": "valid"},
                                           {"from": "hardened/v2", "revision": "hardened/v3", "outcome": "valid"}]}))
        with mock.patch.object(review, "identity", return_value="sha-current"):
            inherited = harden.review_repairs(v3)
            own = harden.review_repairs(v2)
        self.assertTrue(inherited and all(f.startswith("[on the earlier v2") for f in inherited))
        self.assertTrue(own and not own[0].startswith("[on the earlier"))

    def test_the_brief_keeps_resolvable_ambiguity_and_the_bar(self):
        text = harden.REVIEW_REPAIR.format(items="  - check o_rows: x", bar=policy.STEP_PASS_RATE_MAX)
        self.assertIn("careful human", text)
        self.assertIn("determinacy_probe", text)
        self.assertIn(f"at or under {policy.STEP_PASS_RATE_MAX} on BOTH models", text)
        self.assertIn("Never drop or weaken a check", text)


if __name__ == "__main__":
    unittest.main()
