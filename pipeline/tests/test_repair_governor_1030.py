"""The repair governor after the 2026-10-30 audits: a reviewer's probe lines belong to the review, a fresh exhaustive
review is not required to be smaller than the last one, a finding that comes back is still terminal, and an account
loss is never retried."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import orchestrate, policy, repair


class Governor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bundle = Path(tmp.name)
        (self.bundle / "verification").mkdir()

    def passes(self, *finding_lists):
        log = {"schema": "arena-pipeline/repair-log/1",
               "passes": [{"fingerprints": [repair.fingerprint(f) for f in fs]} for fs in finding_lists]}
        (self.bundle / "verification/repair-log.json").write_text(json.dumps(log))

    def test_probe_lines_are_review_findings(self):
        line = "Semantic review counterexample G1 (false_positive): REPRODUCED by the shipped grader"
        self.assertEqual(repair.finding_stage(line), "semantic-review")

    def test_a_larger_fresh_review_is_admitted_and_a_repeated_finding_still_stops(self):
        self.assertFalse(policy.REVIEW_FINDINGS_MUST_SHRINK)
        first = ["Semantic review c1: unsupported answer [tests/checks.json]",
                 "Semantic review c2: hidden rule [tests/checks.json]"]
        self.passes(first)
        second = [f"Semantic review c{i}: new defect {i} [tests/checks.json]" for i in range(3, 8)]
        ok, why = repair.admissible(self.bundle, second)
        self.assertTrue(ok, why)
        ok, why = repair.admissible(self.bundle, [first[0], *second])
        self.assertFalse(ok)
        self.assertIn("survived a repair pass", why)


class AccountLoss(unittest.TestCase):
    def test_credit_and_auth_losses_are_not_retried_but_a_dropped_stream_is(self):
        self.assertFalse(orchestrate._provider_loss(
            "semantic review could not be verified: RuntimeError: OpenAI stream reported a provider error "
            "[credit_balance_exhausted]"))
        self.assertFalse(orchestrate._provider_loss(
            "semantic review could not be verified: _HTTPProviderError: HTTP 401 Unauthorized"))
        self.assertTrue(orchestrate._provider_loss(
            "semantic review could not be verified: RuntimeError: OpenAI stream reported a provider error [server_error]"))


if __name__ == "__main__":
    unittest.main()
