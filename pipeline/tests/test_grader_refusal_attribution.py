"""A grader refusal is a void, never a capability failure.

Measured on c-004-python-translation-engine (2026-10-01): seven Fable 5.1 and
one GPT-6 Astra zero in its calibration of record were the scanner refusing the
fixture's own `import ctypes` and an agent-added local test helper. No test ran
over those submissions (no exit code, 0 s, a `grader:` message), yet
`arena analyse` reported them as model failures and the batch table published
Fable 0.3 as difficulty. Re-graded with the scanner off, all eight passed.

The grade records below are verbatim shapes from that campaign's
`steps/02-offline-only-switch/verifier/arena-grade.json`.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import attribution, evidence

REFUSAL = ("grader: code the hidden tests would import reaches into the test process, so the "
           "suite was not run over it: LiveTranslator/app/main.py: import ctypes")


def _refused_grade() -> dict:
    refused = {"kind": "objective", "weight": 12, "passed": False, "gated_out": False,
               "reason": REFUSAL, "exit_code": None, "seconds": 0.0, "stdout": "",
               "stderr": "", "timed_out": False, "error": REFUSAL}
    return {"schema": "arena-coding/grade/1", "step": "02-offline-only-switch",
            "reward": 0.0, "earned": 0, "total": 50, "guardrail_violated": True,
            "contract_errors": [],
            "checks": {"g_default_online": {**refused, "kind": "guardrail", "weight": 0},
                       "g_shadow_step2": {"kind": "guardrail", "weight": 0, "passed": True,
                                          "gated_out": False, "reason": "no shadowing entry"},
                       "o_switch_no_network": refused}}


def _ran_and_failed_grade() -> dict:
    """A real failure: the suite ran (exit code 1, time spent) and an assertion failed."""
    failed = {"kind": "objective", "weight": 12, "passed": False, "gated_out": False,
              "reason": "1 failed, 4 passed", "exit_code": 1, "seconds": 2.4,
              "stdout": "E   AssertionError: expected offline dictionary hit", "stderr": "",
              "timed_out": False}
    return {"schema": "arena-coding/grade/1", "step": "02-offline-only-switch",
            "reward": 0.0, "earned": 38, "total": 50, "guardrail_violated": False,
            "contract_errors": [], "checks": {"o_switch_no_network": failed}}


def _trial(root: Path, tid: str, grade: dict) -> dict:
    folder = root / tid / "steps/02-offline-only-switch"
    (folder / "verifier").mkdir(parents=True)
    (folder / "verifier/arena-grade.json").write_text(json.dumps(grade))
    checks = {f"reward/{cid}": {"value": 1 if rec.get("passed") else 0, "gated_out": False}
              for cid, rec in grade["checks"].items()}
    step = {"name": "02-offline-only-switch", "valid": True, "reward": grade["reward"],
            "errors": [], "checks": checks, "evidence_dir": str(folder),
            "agent_result": {"metadata": {"n_episodes": 12}}}
    return {"id": tid, "valid": True, "reward": grade["reward"], "steps": [step], "errors": []}


class GraderRefusalIsVoid(unittest.TestCase):

    def rows(self, grade):
        with tempfile.TemporaryDirectory() as tmp:
            return evidence.rollouts([_trial(Path(tmp), "t", grade)], ["02-offline-only-switch"])

    def test_refusal_is_recorded_on_the_row(self):
        row = self.rows(_refused_grade())[0]
        self.assertEqual(len(row["grader_refusals"]), 2)
        self.assertTrue(all("import ctypes" in r for r in row["grader_refusals"]))

    def test_refused_episode_is_void_not_capability(self):
        verdict = attribution.episode_verdict(self.rows(_refused_grade()), set())
        self.assertEqual(verdict["verdict"], "void", verdict)
        self.assertIn("refused", verdict["reasons"][0])

    def test_a_suite_that_ran_and_failed_stays_capability(self):
        row = self.rows(_ran_and_failed_grade())[0]
        self.assertEqual(row["grader_refusals"], [])
        verdict = attribution.episode_verdict([row], set())
        self.assertEqual(verdict["verdict"], "capability", verdict)

    def test_a_grader_message_with_an_exit_code_is_not_a_refusal(self):
        record = {"passed": False, "exit_code": 2, "error": "grader: suite completed no tests"}
        self.assertFalse(evidence.grader_refusal(record))
        self.assertTrue(evidence.grader_refusal({"passed": False, "exit_code": None,
                                                 "error": REFUSAL}))


if __name__ == "__main__":
    unittest.main()
