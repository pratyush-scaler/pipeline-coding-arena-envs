"""A lenient grader is an advisory, not a refusal (user, 2026-10-21 ~10:05 IST: "Ignore the small minor issues which
are not a blocker"). An executed coverage-critic gap or a pass-audit demonstration where a wrong solution still passes
is listed under the package's untested surfaces; so is an unsettled point the critic or the pass audit constructs (its wrong version still scores the check, so no reading of it is penalised)."""
import unittest

from pipeline import coverage_critic as cc
from pipeline import policy

GAP = {"step": "01-a", "index": 8, "clause": "read the number however it is typed", "violation": "U+2012 not read",
       "edits": [], "blocking": True, "source_gap": True, "reward": 1.0}
UNSETTLED = {**GAP, "index": 9, "blocking": False, "asserted_by": ["o_labels"], "route": "instruction",
             "judgement": {"why": "the instruction does not clearly settle it (gpt-6-astra)", "sentence": "x"}}


class LenientGraderIsAdvisory(unittest.TestCase):

    def test_the_default_does_not_block_on_leniency(self):
        self.assertFalse(policy.LENIENCY_FINDINGS_BLOCK)

    def test_coverage_gaps_and_unsettled_assertions_are_advisories(self):
        result = {"gaps": [GAP, UNSETTLED]}
        self.assertEqual(cc.findings(result), [])
        self.assertIn("a lenient grader", cc.advisory(result)[0]["detail"])
        self.assertFalse(policy.UNSETTLED_ASSERTION_BLOCK)

    def test_pass_gaps_and_pass_unsettled_are_advisories(self):
        record = {"gaps": [{"step": "01-a", "id": "p1", "route": "hidden", "check": "o_minus_sign", "clause": "c"},
                           {"step": "01-a", "id": "p2", "route": "instruction", "check": "o_x", "clause": "c",
                            "asserted_by": ["o_x"], "judgement": {"why": "unsettled", "sentence": "s"}}]}
        self.assertEqual(cc.pass_findings(record), [])


if __name__ == "__main__":
    unittest.main()
