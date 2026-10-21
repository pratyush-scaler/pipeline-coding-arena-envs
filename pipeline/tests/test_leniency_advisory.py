"""A lenient grader is an advisory, not a refusal (user, 2026-10-21 ~10:05 IST: "Ignore the small minor issues which
are not a blocker"). An executed coverage-critic gap or a pass-audit demonstration where a wrong solution still passes
is listed under the package's untested surfaces; an unsettled point a check grades still blocks."""
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

    def test_a_coverage_gap_is_advisory_and_an_unsettled_assertion_still_blocks(self):
        result = {"gaps": [GAP, UNSETTLED]}
        self.assertEqual([f["check"] for f in cc.findings(result)], ["coverage.unsettled"])
        self.assertIn("a lenient grader", cc.advisory(result)[0]["detail"])

    def test_a_pass_gap_is_advisory_and_a_pass_unsettled_still_blocks(self):
        record = {"gaps": [{"step": "01-a", "id": "p1", "route": "hidden", "check": "o_minus_sign", "clause": "c"},
                           {"step": "01-a", "id": "p2", "route": "instruction", "check": "o_x", "clause": "c",
                            "asserted_by": ["o_x"], "judgement": {"why": "unsettled", "sentence": "s"}}]}
        self.assertEqual([f["check"] for f in cc.pass_findings(record)], ["pass.unsettled"])


if __name__ == "__main__":
    unittest.main()
