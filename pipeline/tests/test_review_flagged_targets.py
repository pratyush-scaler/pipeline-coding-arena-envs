"""A review repair may change the checks the review flagged (2026-10-14: seven review repairs were
refused as "revision changed an untargeted check" for changing exactly the guardrail the review had
marked 'revise'). Removing a check, or changing an unflagged guardrail, is still refused."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import harden, review


def _bundle(tmp: str, doc: dict) -> Path:
    bundle = Path(tmp)
    work = bundle / "verification/reviews/r1/attempt"
    (work / "deliverables").mkdir(parents=True)
    (work / "deliverables/review.json").write_text(json.dumps(doc))
    (bundle / "verification/semantic-review.json").write_text(
        json.dumps({"artifact_sha256": "SHA", "work_dir": "verification/reviews/r1/attempt"}))
    return bundle


def _guard(cid: str, runner: dict) -> dict:
    return {"id": cid, "kind": "guardrail", "weight": 0, "step": "01-fix", "runner": runner}


class FlaggedChecks(unittest.TestCase):
    DOC = {"verdict": "revise",
           "checks": [{"id": "g_grep", "verdict": "revise", "reason": "a raw grep, not behavioural"},
                      {"id": "o_ok", "verdict": "pass"}],
           "substantive_gaps": [{"id": "G1", "affected_checks": ["o_named"], "reason": "x"}]}

    def test_ids_come_from_non_passing_checks_and_affected_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _bundle(tmp, self.DOC)
            with mock.patch.object(review, "identity", return_value="SHA"):
                self.assertEqual(review.review_flagged_checks(bundle), {"g_grep", "o_named"})
            with mock.patch.object(review, "identity", return_value="OTHER"):     # reviewed other bytes
                self.assertEqual(review.review_flagged_checks(bundle), set())

    def test_flagged_guardrail_may_change_but_not_disappear(self):
        before = {"checks": [_guard("g_grep", {"type": "command", "cmd": "grep -r stack ."}),
                             _guard("g_keep", {"type": "command", "cmd": "test -f a"})]}
        retyped = {"checks": [_guard("g_grep", {"type": "pytest", "select": ["t::test_no_stack_access"]}),
                              _guard("g_keep", {"type": "command", "cmd": "test -f a"})]}
        self.assertTrue(any("untargeted" in p for p in harden.revision_problems(before, retyped, [])))
        self.assertEqual(harden.revision_problems(before, retyped, [{"id": "g_grep"}]), [])
        weakened_other = {"checks": [retyped["checks"][0], _guard("g_keep", {"type": "pytest"})]}
        self.assertTrue(any(p.startswith("g_keep:") for p in
                            harden.revision_problems(before, weakened_other, [{"id": "g_grep"}])))
        dropped = {"checks": [before["checks"][1]]}
        self.assertTrue(any("removes measured checks" in p for p in
                            harden.revision_problems(before, dropped, [{"id": "g_grep"}])))


if __name__ == "__main__":
    unittest.main()
