"""Closed-and-verified (user, 2026-10-09): a final re-executes the lineage's last searching critic run."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import coverage_critic as cc
from pipeline.author import sandbox


def _row(i, path, old, new, *, gap, applied=True):
    return {"index": i, "clause": f"clause {i}", "violation": f"wrong {i}", "applied": applied, "gap": gap,
            "reward": 1.0 if gap else 0.5, "edits": [{"path": path, "old": old, "new": new}]}


class ClosedAndVerified(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lineage = self.tmp / "c-999-x" / "hardened"
        for n in (3, 4, 5, 6):
            (self.lineage / f"v{n}" / "verification").mkdir(parents=True)
        patches = [mock.patch.object(cc, "_steps", lambda b: ["01-a"]),
                   mock.patch("pipeline.artifacts.identity", lambda b: f"sha-{Path(b).name}")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _record(self, n, rows, mode=None):
        doc = {"schema": cc.SCHEMA, "artifact_sha256": f"sha-v{n}", "steps": {"01-a": {"proposals": rows}},
               "gaps": [{"step": "01-a", **r} for r in rows if r.get("gap")]}
        if mode:
            doc["mode"] = mode
        (self.lineage / f"v{n}" / cc.RECEIPT).write_text(json.dumps(doc))

    def test_the_source_is_the_newest_earlier_searching_run(self):
        self._record(3, [_row(0, "a.py", "x", "y", gap=True)])
        self._record(4, [_row(0, "a.py", "x", "z", gap=True)])
        self._record(5, [_row(0, "a.py", "x", "z", gap=False)], mode="verify")
        found = cc.source_record(self.lineage / "v6")
        self.assertEqual(found[0].name, "v4")
        self.assertIsNone(cc.source_record(self.lineage / "v3"))

    def test_a_source_that_executed_nothing_on_a_step_means_a_fresh_search(self):
        self._record(4, [_row(0, "a.py", "x", "y", gap=True, applied=False)])
        self.assertIsNone(cc.source_record(self.lineage / "v6"))

    def _verify(self, rows, rewards, tree_text="x = 1\n", ported=None):
        self._record(4, rows)
        source_dir, source = cc.source_record(self.lineage / "v6")

        def reference_tree(bundle, step, dest):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "a.py").write_text(tree_text)
            return dest

        graded = []

        def fake_verify(bundle, variants, **kw):
            files = variants["01-a"].files
            graded.append(files)
            reward = rewards[files["a.py"]]
            return {"steps": {"01-a": {"valid": True, "reward": reward, "grade": {"checks": {
                "o_x": {"passed": reward == 1.0}}}}}}

        ask = mock.Mock(return_value=ported or "")
        with mock.patch.object(cc, "reference_tree", reference_tree), \
                mock.patch.object(sandbox, "build_image", lambda b, kind: kind), \
                mock.patch.object(sandbox, "solve_variant", lambda b, s: object()), \
                mock.patch.object(sandbox, "verify", fake_verify):
            out = cc.verify(self.lineage / "v6", source_dir, source, model="m", ask=ask, workers=1,
                            record_dir=self.lineage / "v6" / "verification")
        return out, graded, ask

    def test_closed_gaps_pass_and_nothing_new_is_searched(self):
        rows = [_row(0, "a.py", "x = 1", "x = 2", gap=True), _row(1, "a.py", "x = 1", "x = 3", gap=False)]
        out, graded, ask = self._verify(rows, {"x = 2\n": 0.5, "x = 3\n": 0.0})
        self.assertEqual(out["mode"], "verify")
        self.assertEqual(out["gaps"], [])
        self.assertEqual(len(graded), 2)
        ask.assert_not_called()
        written = json.loads((self.lineage / "v6" / cc.RECEIPT).read_text())
        self.assertEqual(written["artifact_sha256"], "sha-v6")
        self.assertEqual(written["source"], str(self.lineage / "v4"))

    def test_an_open_gap_and_a_regression_both_block(self):
        rows = [_row(0, "a.py", "x = 1", "x = 2", gap=True), _row(1, "a.py", "x = 1", "x = 3", gap=False)]
        out, _, _ = self._verify(rows, {"x = 2\n": 1.0, "x = 3\n": 1.0})
        self.assertEqual([g["index"] for g in out["gaps"]], [0, 1])
        texts = [f["detail"] if "detail" in f else json.dumps(f) for f in cc.findings(out)]
        self.assertIn("scores 1.0 while violating", texts[0])
        self.assertIn("although it failed on v4", texts[1])

    def test_edits_that_no_longer_apply_are_ported_or_stay_open(self):
        rows = [_row(0, "a.py", "x = 1", "x = 2", gap=True)]
        port = '```json\n{"proposals": [{"edits": [{"path": "a.py", "old": "y = 10", "new": "y = 20"}]}]}\n```'
        out, graded, ask = self._verify(rows, {"y = 20\n": 0.0}, tree_text="y = 10\n", ported=port)
        self.assertEqual(out["gaps"], [])
        self.assertTrue(out["steps"]["01-a"]["proposals"][0]["ported"])
        ask.assert_called_once()
        out, graded, _ = self._verify(rows, {}, tree_text="y = 10\n", ported="no json")
        self.assertEqual(len(out["gaps"]), 1)
        self.assertIn("not re-executed", json.dumps(cc.findings(out)))
        self.assertEqual(graded, [])

    def test_ship_critic_follows_policy(self):
        self._record(4, [_row(0, "a.py", "x", "y", gap=True)])
        with mock.patch.object(cc, "verify", return_value={"mode": "verify", "gaps": []}) as v, \
                mock.patch.object(cc, "critic", return_value={"gaps": []}) as c:
            cc.ship_critic(self.lineage / "v6")
            v.assert_called_once()
            c.assert_not_called()
            cc.ship_critic(self.lineage / "v3")
            c.assert_called_once()
            with mock.patch("pipeline.policy.CRITIC_SHIP_RULE", "fresh"):
                cc.ship_critic(self.lineage / "v6")
            self.assertEqual(c.call_count, 2)


class RealisticOnly(unittest.TestCase):
    """Block only realistic gaps (user, 2026-10-09)."""

    GAP = {"step": "01-a", "index": 0, "clause": "keep my list", "violation": "adds a word", "gap": True,
           "edits": [{"path": "a.py", "old": "x", "new": "y"}]}

    @staticmethod
    def _ask(replies):
        def ask(model, prompt):
            reply = replies[model]
            if isinstance(reply, Exception):
                raise reply
            return reply
        return ask

    def test_judges_demote_only_with_a_reason(self):
        yes = "fine\nPUBLISHED: YES\nREALISTIC: YES"
        cases = [
            ({"claude-fable-5-1": yes, "gpt-6-astra": yes}, True),
            ({"claude-fable-5-1": yes, "gpt-6-astra": "x\nPUBLISHED: UNDETERMINED\nREALISTIC: YES"}, False),
            ({"claude-fable-5-1": "PUBLISHED: YES\nREALISTIC: NO", "gpt-6-astra": "PUBLISHED: YES\nREALISTIC: NO"}, False),
            ({"claude-fable-5-1": "PUBLISHED: YES\nREALISTIC: NO", "gpt-6-astra": yes}, True),
            ({"claude-fable-5-1": "PUBLISHED: YES\nREALISTIC: NO", "gpt-6-astra": RuntimeError("401")}, True),
            ({"claude-fable-5-1": "no verdict lines", "gpt-6-astra": "**PUBLISHED:** NO\nREALISTIC: YES"}, False),
        ]
        for replies, blocking in cases:
            with self.subTest(replies=replies):
                self.assertIs(cc.judge_gap("instr", self.GAP, ask=self._ask(replies))["blocking"], blocking)

    def test_adjudicate_reuses_prior_judgements_and_splits_findings(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "steps/01-a").mkdir(parents=True)
        (tmp / "steps/01-a/instruction.md").write_text("keep my list")
        other = dict(self.GAP, index=1, violation="drops a word")
        result = {"steps": {"01-a": {"proposals": [dict(self.GAP), dict(other)]}},
                  "gaps": [dict(self.GAP, step="01-a"), dict(other, step="01-a")]}
        prior = {"gaps": [dict(self.GAP, judgement={"blocking": False, "why": "contrived"})]}
        asked = []

        def ask(model, prompt):
            asked.append(model)
            return "PUBLISHED: YES\nREALISTIC: YES"
        cc.adjudicate(tmp, result, prior=prior, ask=ask, workers=1)
        self.assertEqual(sorted(asked), sorted(cc.JUDGES))        # only the un-judged gap is asked about
        self.assertEqual([g["blocking"] for g in result["gaps"]], [False, True])
        self.assertEqual(result["steps"]["01-a"]["proposals"][0]["blocking"], False)
        self.assertEqual([f["subject"] for f in cc.findings(result)], ["01-a#1"])
        self.assertEqual([f["subject"] for f in cc.advisory(result)], ["01-a#0"])
        self.assertTrue(cc.findings(result)[0]["detail"].startswith("scores 1.0 while violating"))
        unjudged = {"gaps": [dict(self.GAP, step="01-a")]}
        self.assertEqual(len(cc.findings(unjudged)), 1)           # no judgement -> blocking
        self.assertEqual(cc.advisory(unjudged), [])


def setUpModule():
    # These tests pin the gap machinery in its blocking setting; the advisory default since 2026-10-21 ~10:05 IST
    # (policy.LENIENCY_FINDINGS_BLOCK = False) is tested in test_leniency_advisory.py.
    from pipeline import policy
    global _LENIENCY
    _LENIENCY, policy.LENIENCY_FINDINGS_BLOCK = policy.LENIENCY_FINDINGS_BLOCK, True


def tearDownModule():
    from pipeline import policy
    policy.LENIENCY_FINDINGS_BLOCK = _LENIENCY


if __name__ == "__main__":
    unittest.main()


class FirstJudgementHolds(unittest.TestCase):
    def test_earlier_verify_judgements_are_reused_first_wins(self):
        tmp = Path(tempfile.mkdtemp()) / "c-1" / "hardened"
        for n, blocking in ((3, False), (4, True)):
            (tmp / f"v{n}" / "verification").mkdir(parents=True)
            gap = {"step": "s", "index": 0, "clause": "c", "violation": "v", "judgement": {"blocking": blocking, "why": str(n)}}
            (tmp / f"v{n}" / cc.RECEIPT).write_text(json.dumps({"mode": "verify", "gaps": [gap]}))
        (tmp / "v5").mkdir()
        got = cc._judged_before(tmp / "v5")
        self.assertEqual([g["judgement"]["why"] for g in got], ["3", "4"])
        result = {"steps": {}, "gaps": [{"step": "s", "index": 0, "clause": "c", "violation": "v"}]}
        cc.adjudicate(tmp / "v5", result, prior={"gaps": got}, ask=lambda m, p: "PUBLISHED: YES\nREALISTIC: YES", workers=1)
        self.assertIs(result["gaps"][0]["blocking"], False)      # v3's first judgement holds
