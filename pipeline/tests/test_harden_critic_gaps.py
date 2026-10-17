import unittest
from pathlib import Path
from unittest import mock

from pipeline import harden, shipcheck


class CriticGapFixes(unittest.TestCase):
    def test_each_blocking_gap_becomes_a_fix_naming_its_edits(self):
        found = [shipcheck.finding("coverage.gap", "01-step#0", "scores 1.0 while violating: keep X -- drops X",
                                   route="hidden", measure="regrade", evidence=['[{"path": "a.py"}]']),
                 shipcheck.finding("coverage.gap", "01-step#3", "scores 1.0 while violating: Y -- not Y",
                                   route="hidden", measure="regrade")]
        with mock.patch.object(shipcheck, "coverage", return_value=found):
            fixes = harden.critic_gap_fixes(Path("/nowhere"))
        self.assertEqual(len(fixes), 2)
        self.assertIn("01-step#0", fixes[0])
        self.assertIn("a.py", fixes[0])
        self.assertIn("untested_surfaces", fixes[1])

    def test_a_missing_receipt_is_not_a_gap(self):
        missing = [shipcheck.finding("coverage.critic", "verification/coverage-critic.json", "has not run",
                                     route="regenerate", measure="none")]
        with mock.patch.object(shipcheck, "coverage", return_value=missing):
            self.assertEqual(harden.critic_gap_fixes(Path("/nowhere")), [])


if __name__ == "__main__":
    unittest.main()
