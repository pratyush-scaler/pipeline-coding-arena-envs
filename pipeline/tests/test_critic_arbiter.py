"""A gap the families split on (one published, one doubting) is decided by policy.READING_ARBITER (2026-10-21).

On the repaired node-rest-dashboard a gap Astra called published, quoting the deciding sentence, was demoted on
Fable's UNDETERMINED alone and routed to an instruction rewrite."""
import unittest

from pipeline import coverage_critic as cc
from pipeline import policy

INSTRUCTION = "Whatever is in the file is shown by these same rules, and the order survives a restart."
GAP = {"step": "01-atlas", "clause": "the order survives a restart", "violation": "only app sections are sorted",
       "edits": []}


def asker(arbiter: str, calls: list):
    answers = {"claude-fable-5-1": "UNDETERMINED", "gpt-6-astra": "YES", policy.READING_ARBITER: arbiter}

    def ask(model, prompt):
        calls.append(model)
        if answers[model] == "raise":
            raise RuntimeError("HTTP 529")
        return (f"Reasoning.\nPUBLISHED: {answers[model]}\nREALISTIC: YES\n"
                "SENTENCE: Whatever is in the file is shown by these same rules")
    return ask


class GapArbiter(unittest.TestCase):

    def test_an_arbiter_that_finds_it_published_keeps_the_gap_blocking(self):
        calls = []
        judged = cc.judge_gap(INSTRUCTION, GAP, ask=asker("YES", calls))
        self.assertEqual(calls[-1], policy.READING_ARBITER)
        self.assertTrue(judged["blocking"])
        self.assertFalse(judged["unsettled"])

    def test_an_arbiter_that_doubts_demotes_it(self):
        judged = cc.judge_gap(INSTRUCTION, GAP, ask=asker("UNDETERMINED", []))
        self.assertTrue(judged["unsettled"])
        self.assertFalse(judged["blocking"])

    def test_an_erring_arbiter_leaves_the_doubt_standing(self):
        judged = cc.judge_gap(INSTRUCTION, GAP, ask=asker("raise", []))
        self.assertTrue(judged["unsettled"])
        self.assertIn("error", judged["arbiter"])

    def test_a_split_recorded_without_an_arbiter_is_judged_again(self):
        old = {"answers": {"claude-fable-5-1": {"published": "UNDETERMINED"}, "gpt-6-astra": {"published": "YES"}},
               "doubted": ["claude-fable-5-1"], "unsettled": True, "instruction_sha256": "d"}
        self.assertFalse(cc._reusable(old, "d"))
        self.assertTrue(cc._reusable({**old, "arbiter": {"published": "NO"}}, "d"))
        legacy = {k: v for k, v in old.items() if k not in ("doubted", "unsettled")}       # an older record's shape
        legacy["why"] = "the instruction does not clearly settle it (claude-fable-5-1)"
        self.assertFalse(cc._reusable(legacy, "d"))


class ReceiptReadjudicated(unittest.TestCase):

    def test_a_pre_arbiter_split_in_a_reused_receipt_is_judged_again(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp)
            (b / "steps/01-atlas").mkdir(parents=True)
            (b / "verification").mkdir()
            (b / "steps/01-atlas/instruction.md").write_text(INSTRUCTION)
            split = {"answers": {"claude-fable-5-1": {"published": "UNDETERMINED"}, "gpt-6-astra": {"published": "YES"}},
                     "doubted": ["claude-fable-5-1"], "unsettled": True, "blocking": False,
                     "instruction_sha256": cc._digest(b / "steps/01-atlas/instruction.md")}
            (b / cc.RECEIPT).write_text(json.dumps({"gaps": [{**GAP, "index": 5, "judgement": split}]}))
            calls = []
            receipt = cc.readjudicate(b, ask=asker("YES", calls))
        self.assertIn(policy.READING_ARBITER, calls)
        self.assertTrue(receipt["gaps"][0]["blocking"])
        self.assertNotIn("asserted_by", receipt["gaps"][0])

if __name__ == "__main__":
    unittest.main()
