import tempfile
import unittest
from pathlib import Path

from pipeline import release


class NameAgainstTheBuiltLevel(unittest.TestCase):
    def _bundle(self, tmp, level):
        b = Path(tmp) / "v5"
        b.mkdir()
        (b / "task.toml").write_text(f'[task]\nname = "arena/a_{level.lower()}_v5"\n')
        return b

    def test_a_calibration_lift_from_f0_to_f1_is_not_a_naming_defect(self):
        # 2026-10-18: c-015 v5/v6, c-023 v5/v6 and c-038 v4/v5 were refused at qualify with "task name claims
        # faithfulness F0 but the ledger measures F1"; delivery renames the copy to the re-assigned level.
        with tempfile.TemporaryDirectory() as tmp:
            b = self._bundle(tmp, "F0")
            self.assertEqual(release.faithfulness_problems(b, {"faithfulness_level": "F0"}, {"level": "F1"}), [])

    def test_a_name_that_disagrees_with_its_own_ledger_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._bundle(tmp, "F0")
            issues = release.faithfulness_problems(b, {"faithfulness_level": "F2"}, {"level": "F2"})
        self.assertTrue(any("claims faithfulness" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
