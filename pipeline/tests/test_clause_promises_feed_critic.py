"""The clause map advises; the promises it flags are the coverage critic's first targets (2026-10-21).

Its first sweep of the 37 delivered packages judged ~29 clauses per task "in name only" and flagged framing
sentences as unenforced, so it no longer blocks on a text judgement. What it flags must still reach the critic,
whose executed counter-implementations are what block."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import coverage_critic


def _bundle(root: Path) -> Path:
    b = root / "task"
    (b / "steps/01-fix/solution").mkdir(parents=True)
    (b / "tests/suites").mkdir(parents=True)
    (b / "verification").mkdir()
    (b / "authoring").mkdir()
    (b / "steps/01-fix/instruction.md").write_text("python -m pytest tests is green right now and has to stay green.\n")
    (b / "steps/01-fix/solution/fix.patch").write_text("")
    (b / "tests/suites/test_x.py").write_text("def test_x():\n    assert True\n")
    (b / "tests/checks.json").write_text(json.dumps({"checks": [{"id": "o_fit", "kind": "objective", "step": "01-fix"}]}))
    (b / "authoring/spec.json").write_text(json.dumps({"checks": [{"id": "o_fit", "rule_quote": ["every column fits"]}]}))
    (b / "verification/clause-map.json").write_text(json.dumps({"steps": {"01-fix": {"clauses": [
        {"quote": "python -m pytest tests is green right now and has to stay green", "status": "unenforced"},
        {"quote": "every column fits", "status": "in_name_only",
         "judgement": {"gpt-6-astra": {"detects": "NO", "counter_example": "widen only text columns"}}},
        {"quote": "keep the CLI name", "status": "enforced"}]}}}))
    return b


class FlaggedPromisesReachTheCritic(unittest.TestCase):

    def test_the_prompt_lists_unenforced_and_in_name_only_promises_and_the_spec_sentences(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bundle(Path(tmp))
            prompt = coverage_critic.build_prompt(b, "01-fix", Path(tmp))
        self.assertIn("[unenforced] python -m pytest tests is green right now and has to stay green", prompt)
        self.assertIn("[in_name_only] every column fits -- judge's counter-example: widen only text columns", prompt)
        self.assertNotIn("keep the CLI name", prompt.split("PROMISES THE TESTS MAY NOT ENFORCE")[1].split("DECLARED")[0])
        self.assertIn("o_fit | objective | every column fits", prompt)        # the sentence from the spec

    def test_no_map_leaves_the_section_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bundle(Path(tmp))
            (b / "verification/clause-map.json").unlink()
            prompt = coverage_critic.build_prompt(b, "01-fix", Path(tmp))
        section = prompt.split("PROMISES THE TESTS MAY NOT ENFORCE")[1].split("DECLARED")[0]
        self.assertIn("(none)", section)


if __name__ == "__main__":
    unittest.main()
