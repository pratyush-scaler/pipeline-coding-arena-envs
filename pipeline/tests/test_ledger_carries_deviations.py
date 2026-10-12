"""A human-recorded deviation survives the ledger's recomputation (Arena C-034, 2026-10-08)."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.ledger import faithfulness


class CarriedDeclarations(unittest.TestCase):
    def test_deviations_from_the_previous_ledger_are_carried(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "provenance").mkdir()
            dev = [{"what": "uploads/s10.1.md Q71 keeps 'Correct Answer: D'", "why": "Arena C-034"}]
            (bundle / "provenance/ledger.json").write_text(json.dumps({"deviations_from_trace": dev, "counts": {}}))
            self.assertEqual(faithfulness.carried_declarations(bundle), {"deviations_from_trace": dev})

    def test_nothing_to_carry_and_unreadable_ledgers_carry_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            self.assertEqual(faithfulness.carried_declarations(bundle), {})
            (bundle / "provenance").mkdir()
            (bundle / "provenance/ledger.json").write_text("{not json")
            self.assertEqual(faithfulness.carried_declarations(bundle), {})

    def test_it_is_never_a_measured_field(self):
        self.assertNotIn("deviations_from_trace", faithfulness.measured_fields({"schema": faithfulness.SCHEMA}))


if __name__ == "__main__":
    unittest.main()
