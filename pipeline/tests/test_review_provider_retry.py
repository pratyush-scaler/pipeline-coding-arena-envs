"""A review the provider dropped is run once more (2026-10-30 audit: 15% of non-coding build attempts ended
"unverified" on "OpenAI stream reported a provider error"). A verdict, or any other failure, is never re-run."""
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from pipeline import orchestrate

LOST = {"verdict": "unverified", "findings": [
    "semantic review could not be verified: RuntimeError: OpenAI stream reported a provider error [server_error]"]}
GREEN = {"verdict": "green", "findings": []}


class ProviderLossRetry(unittest.TestCase):
    def run_with(self, outcomes):
        calls = []

        def once(bundle, budget, *args, **kwargs):
            calls.append(1)
            return outcomes[len(calls) - 1]
        with patch.object(orchestrate, "_review_candidate_once", side_effect=once):
            out = orchestrate._review_candidate(Path("/nonexistent"), nullcontext())
        return out, len(calls)

    def test_a_dropped_review_runs_once_more(self):
        self.assertEqual(self.run_with([LOST, GREEN]), (GREEN, 2))

    def test_a_second_loss_is_reported_not_retried_again(self):
        self.assertEqual(self.run_with([LOST, LOST]), (LOST, 2))

    def test_a_verdict_or_another_failure_is_never_re_run(self):
        other = {"verdict": "unverified", "findings": ["semantic review requires an identified author and a "
                                                       "different reviewer model"]}
        blocked = {"verdict": "blocked", "findings": ["Semantic review c1: unsupported"]}
        self.assertEqual(self.run_with([other]), (other, 1))
        self.assertEqual(self.run_with([blocked]), (blocked, 1))


if __name__ == "__main__":
    unittest.main()
