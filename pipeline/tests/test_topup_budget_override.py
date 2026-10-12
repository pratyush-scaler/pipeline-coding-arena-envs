"""A larger void top-up budget applies only to the run that sets ARENA_TOPUP_BUDGET (2026-10-08)."""
import os
import unittest
from unittest.mock import patch

from pipeline import policy, release


class TopUpBudget(unittest.TestCase):
    def test_default_is_the_policy_budget(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(release.topup_budget(), policy.MAX_TOPUP_ATTEMPTS)

    def test_one_run_may_raise_it(self):
        with patch.dict(os.environ, {"ARENA_TOPUP_BUDGET": "10"}, clear=True):
            self.assertEqual(release.topup_budget(), 10)

    def test_it_never_lowers_the_policy_budget_or_accepts_junk(self):
        for raw in ("1", "-3", "ten", ""):
            with patch.dict(os.environ, {"ARENA_TOPUP_BUDGET": raw}, clear=True):
                self.assertEqual(release.topup_budget(), policy.MAX_TOPUP_ATTEMPTS, raw)


if __name__ == "__main__":
    unittest.main()
