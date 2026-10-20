"""Guardrail-zeroed judges see what failed even when the grader printed no assertion (2026-10-21).

Two shipped node guardrails (node-rest-dashboard g_page_intact, nextjs-fintech g_untouched_figures) reached the
judges as "exit 1": node-test checks keep no output in the grade, so both families answered UNCLEAR."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import shipcheck


class NodeGuardrailEvidence(unittest.TestCase):

    def test_test_source_and_certificate_detail_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp)
            (b / "tests/suites/node").mkdir(parents=True)
            (b / "verification").mkdir()
            (b / "tests/suites/node/page_served.test.js").write_text("test('page still served', () => { assert.ok(1) })\n")
            (b / "verification/genuine.json").write_text(json.dumps({"episodes": [
                {"id": "package__BvcyA6h", "failures": [{"check": "g_page_intact", "detail": "dashboard rendered no data"}]}]}))
            source = shipcheck._guardrail_source(b, {"type": "node-test", "select": ["page_served.test.js"]})
            detail = shipcheck._genuine_detail(b, "package__BvcyA6h", "g_page_intact")
        self.assertIn("page_served.test.js", source)
        self.assertIn("page still served", source)
        self.assertIn("dashboard rendered no data", detail)

    def test_nothing_recorded_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp)
            self.assertEqual(shipcheck._guardrail_source(b, {"select": ["missing.test.js"]}), "")
            self.assertEqual(shipcheck._genuine_detail(b, "t", "g"), "")

    def test_exit_status_alone_has_no_assertion_line(self):
        text, _ = shipcheck._failing_assertion({"stdout": "", "stderr": "", "reason": "exit 1"}, structural=False)
        self.assertIsNone(shipcheck._ASSERT_LINE.search(text))


if __name__ == "__main__":
    unittest.main()
