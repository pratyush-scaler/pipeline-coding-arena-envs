import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import cli, harden, release_blockers, shipcheck
from pipeline.author import difficulty_gate


def _version(tmp: str, name: str = "v3") -> Path:
    v = Path(tmp) / "c-001-x" / "hardened" / name
    (v / "verification").mkdir(parents=True)
    return v


class Collect(unittest.TestCase):
    def test_every_recorded_source_becomes_a_blocker_with_an_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = _version(tmp)
            (v / "verification/coverage-critic.json").write_text(json.dumps({"mode": "verify"}))
            (v / "verification/screen.json").write_text(json.dumps({
                "signal_problems": ["signal: 61% of the objective weight is passed by every run of both models",
                                    "signal: reward spread: runs are flat"],
                "step_pass_rates": {"anthropic/claude-fable-5-1": {"01-a": 0.6, "02-b": 0.2}}}))
            (v / "verification/shipfix-release.json").write_text(json.dumps({"steps": {
                "qualify": {"ok": True, "result": {"errors": [
                    "calibration: unverified (FileNotFoundError: ... native-calibrations.json')",
                    "semantic review: semantic reviewer requested revision", "tasks-check: failed"]}},
                "shipcheck": {"ok": False, "result": {"findings": [
                    {"check": "deps.declared", "subject": "requirements.txt", "detail": "x missing", "route": "deps"}]}}}}))
            gap = shipcheck.finding("coverage.gap", "01-a#4", "scores 1.0 while violating: Z -- not Z",
                                    route="hidden", measure="regrade", evidence=['[{"path": "a.py"}]'])
            with mock.patch.object(shipcheck, "coverage", return_value=[gap]), \
                    mock.patch("pipeline.review.review_findings",
                               return_value=["check o_x: over-strict", "ambiguities: the month of day 540"]):
                got = release_blockers.collect(v)
        ids = [b["id"] for b in got]
        self.assertEqual(ids, ["CRIT 01-a#4", "REV 1", "REV 2", "SIG 1", "STEP 01-a claude-fable-5-1", "QUAL 3", "SHIP 1"])
        crit = got[0]
        self.assertTrue(crit["survived_a_round"])
        self.assertIn("a.py", crit["evidence"])
        self.assertTrue(release_blockers.needs_full_round(got))           # the ambiguity and the deps finding
        self.assertFalse(release_blockers.needs_full_round(got[:1] + got[3:5]))

    def test_a_root_build_contributes_its_readiness_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "c-005-card"
            (root / "verification").mkdir(parents=True)
            (root / "verification/repair-log.json").write_text(json.dumps({"passes": [{"findings": [
                "ship.readiness: image.imports: py:PIL: not in the image",
                "ship.readiness: coverage.gap: 01-a#0: scores 1.0", "trap.faithful: x"]}]}))
            with mock.patch.object(shipcheck, "coverage", return_value=[]), \
                    mock.patch("pipeline.review.review_findings", return_value=[]):
                got = release_blockers.collect(root)
        self.assertEqual([b["id"] for b in got], ["READY 1"])
        self.assertTrue(got[0]["agent_surface"])


class FailedAttempts(unittest.TestCase):
    def test_the_last_invalid_attempts_from_a_version_are_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = _version(tmp, "v7")
            (v.parent / "hardening.json").write_text(json.dumps({"attempts": [
                {"attempt": 6, "from": "hardened/v6", "revision": "hardened/v7", "outcome": "valid", "findings": []},
                {"attempt": 7, "from": "hardened/v7", "revision": "hardened/v8", "outcome": "invalid",
                 "findings": ["hidden-only revision changed `deliverables`, which the agent sees"]}]}))
            with mock.patch.object(shipcheck, "coverage", return_value=[]), \
                    mock.patch("pipeline.review.review_findings", return_value=[]):
                got = release_blockers.collect(v)
        self.assertEqual([b["id"] for b in got], ["ATTEMPT 1"])
        self.assertIn("deliverables", got[0]["finding"])
        self.assertTrue(release_blockers.needs_full_round(got))


class Requirement(unittest.TestCase):
    SPEC = {"steps": [{"name": "01-a", "instruction": "do it"}], "checks": [], "suites": {}}

    def test_unarmed_without_a_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(release_blockers.requirement(self.SPEC, Path(tmp)), [])

    def test_open_rows_on_the_current_draft_are_owed_until_waived(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp)
            (b / "verification").mkdir()
            (b / release_blockers.CONTEXT).write_text(json.dumps({
                "critic_source": "/x/v2", "blockers": [{"id": "CRIT 01-a#4"}, {"id": "REV 1"}], "waived": None}))
            owed = release_blockers.requirement(self.SPEC, b)
            self.assertEqual(len(owed), 2)                                  # no release_check, no pre_review yet
            sha = difficulty_gate.grading_sha(self.SPEC)
            release_blockers.record_check(b, sha, [
                {"step": "01-a", "index": 4, "clause": "Z", "still_scores_1": True, "blocking": True},
                {"step": "01-a", "index": 5, "clause": "W", "still_scores_1": True, "blocking": False},
                {"step": "01-a", "index": 6, "clause": "V", "still_scores_1": False, "blocking": True}])
            release_blockers.record_prereview(b, sha, [{"id": "o_x", "reason": "over-strict"}])
            owed = release_blockers.requirement(self.SPEC, b)
            self.assertEqual(len(owed), 2)                                  # the open CRIT row and o_x's ledger line
            self.assertIn("01-a#4", owed[0])
            self.assertIn("o_x", owed[1])
            stale = dict(self.SPEC, checks=[{"id": "o_new"}], solution_notes="o_x: REFUTED -- passes on the reference")
            self.assertEqual(len(release_blockers.requirement(stale, b)), 2)   # tests changed: both re-run
            release_blockers.waive(b, "turn budget: 20 turns left")
            self.assertEqual(release_blockers.requirement(self.SPEC, b), [])

    def test_every_raised_finding_needs_a_ledger_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp)
            (b / "verification").mkdir()
            (b / release_blockers.CONTEXT).write_text(json.dumps({"critic_source": None, "blockers": [{"id": "REV 1"}],
                                                                  "waived": None}))
            sha = difficulty_gate.grading_sha(self.SPEC)
            release_blockers.record_prereview(b, sha, [{"id": "o_twelve_weeks", "reason": "raw title"}])
            release_blockers.record_prereview(b, sha, [])                   # a later, clean sample
            owed = release_blockers.requirement(self.SPEC, b)
            self.assertEqual(len(owed), 1)
            self.assertIn("o_twelve_weeks", owed[0])
            done = dict(self.SPEC, solution_notes="o_twelve_weeks: FIXED -- case raw_title: before 0.0, after 1.0")
            self.assertEqual(release_blockers.requirement(done, b), [])

    def test_clear_removes_a_parents_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp)
            (b / "verification").mkdir()
            for name in (release_blockers.CONTEXT, release_blockers.CHECK, release_blockers.PREREVIEW):
                (b / name).write_text("{}")
            release_blockers.clear(b)
            self.assertFalse(any((b / n).exists() for n in (release_blockers.CONTEXT, release_blockers.CHECK)))


class Briefing(unittest.TestCase):
    def test_the_brief_lists_every_blocker_and_how_release_rechecks_it(self):
        blockers = [{"id": "CRIT 01-a#4", "source": "ship critic", "finding": "scores 1.0", "evidence": "[edits]",
                     "survived_a_round": True, "release_checks": "re-executes", "agent_surface": False},
                    {"id": "REV 1", "source": "review", "finding": "check o_x over-strict",
                     "release_checks": "qualify reviews", "agent_surface": False}]
        text = release_blockers.brief(blockers, Path("/x/hardened/v4"))
        for needle in ("v4 is ALREADY HARD", "[CRIT 01-a#4]", "SURVIVED AN EARLIER ROUND", "[edits]", "[REV 1]",
                       "release_check", "pre_review", "resolution ledger"):
            self.assertIn(needle, text)

    def test_release_items_are_not_rendered_as_operator_traps(self):
        text = harden._requested_text([harden.RELEASE_PREFIX + "[]", "a real trap idea"])
        self.assertIn("a real trap idea", text)
        self.assertNotIn(harden.RELEASE_PREFIX, text)

    def test_the_arena_cli_forwards_release_repair(self):
        args = cli.build_parser().parse_args(["harden", "/x/v2", "--apply", "--release-repair"])
        with mock.patch.object(cli, "_run", return_value=0) as run:
            args.fn(args)
        self.assertIn("--release-repair", run.call_args.args)


if __name__ == "__main__":
    unittest.main()
