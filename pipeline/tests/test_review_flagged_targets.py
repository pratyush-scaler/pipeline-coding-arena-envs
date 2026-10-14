"""A review repair may change the checks the review flagged (2026-10-14: seven review repairs were
refused as "revision changed an untargeted check" for changing exactly the guardrail the review had
marked 'revise'). Removing a check, or changing an unflagged guardrail, is still refused."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import harden, review


def _bundle(tmp: str, doc: dict) -> Path:
    bundle = Path(tmp)
    work = bundle / "verification/reviews/r1/attempt"
    (work / "deliverables").mkdir(parents=True)
    (work / "deliverables/review.json").write_text(json.dumps(doc))
    (bundle / "verification/semantic-review.json").write_text(
        json.dumps({"artifact_sha256": "SHA", "work_dir": "verification/reviews/r1/attempt"}))
    return bundle


def _guard(cid: str, runner: dict) -> dict:
    return {"id": cid, "kind": "guardrail", "weight": 0, "step": "01-fix", "runner": runner}


class FlaggedChecks(unittest.TestCase):
    DOC = {"verdict": "revise",
           "checks": [{"id": "g_grep", "verdict": "revise", "reason": "a raw grep, not behavioural"},
                      {"id": "o_ok", "verdict": "pass"}],
           "substantive_gaps": [{"id": "G1", "affected_checks": ["o_named"], "reason": "x"}]}

    def test_ids_come_from_non_passing_checks_and_affected_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _bundle(tmp, self.DOC)
            with mock.patch.object(review, "identity", return_value="SHA"):
                self.assertEqual(review.review_flagged_checks(bundle), {"g_grep", "o_named"})
            with mock.patch.object(review, "identity", return_value="OTHER"):     # reviewed other bytes
                self.assertEqual(review.review_flagged_checks(bundle), set())

    def test_flagged_guardrail_may_change_but_not_disappear(self):
        before = {"checks": [_guard("g_grep", {"type": "command", "cmd": "grep -r stack ."}),
                             _guard("g_keep", {"type": "command", "cmd": "test -f a"})]}
        retyped = {"checks": [_guard("g_grep", {"type": "pytest", "select": ["t::test_no_stack_access"]}),
                              _guard("g_keep", {"type": "command", "cmd": "test -f a"})]}
        self.assertTrue(any("untargeted" in p for p in harden.revision_problems(before, retyped, [])))
        self.assertEqual(harden.revision_problems(before, retyped, [{"id": "g_grep"}]), [])
        weakened_other = {"checks": [retyped["checks"][0], _guard("g_keep", {"type": "pytest"})]}
        self.assertTrue(any(p.startswith("g_keep:") for p in
                            harden.revision_problems(before, weakened_other, [{"id": "g_grep"}])))
        dropped = {"checks": [before["checks"][1]]}
        self.assertTrue(any("removes measured checks" in p for p in
                            harden.revision_problems(before, dropped, [{"id": "g_grep"}])))


class RevisionInvariantsInCheckDraft(unittest.TestCase):
    """check_draft in a revision session reports a removed check before submission (2026-10-14, nfse)."""

    def test_removed_check_is_reported_and_unchanged_draft_stays_clean(self):
        from pipeline.author import agent_designer
        base = {"checks": [{"id": "o_a", "kind": "objective", "weight": 5, "step": "01-fix"},
                           {"id": "o_b", "kind": "objective", "weight": 5, "step": "01-fix"}],
                "steps": [{"name": "01-fix"}]}
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "draft.json"
            check = agent_designer._with_revision_invariants(lambda **_: {"clean": True, "problems": []}, base, draft)
            draft.write_text(json.dumps(base))
            self.assertEqual(check(), {"clean": True, "problems": []})
            draft.write_text(json.dumps(dict(base, checks=base["checks"][:1])))
            out = check()
            self.assertFalse(out["clean"])
            self.assertTrue(any(p.startswith("REVISION: revision removes measured checks ['o_b']") for p in out["problems"]))


    def test_unrouted_new_trap_and_dropped_trace_trap_are_reported(self):
        from pipeline.author import agent_designer
        base = {"checks": [{"id": "o_a", "kind": "objective", "weight": 5, "step": "01-fix"}],
                "steps": [{"name": "01-fix"}],
                "traps": [{"id": "t_real", "provenance": "trace_derived", "checks": ["o_a"]}]}
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "draft.json"
            check = agent_designer._with_revision_invariants(lambda **_: {"clean": True, "problems": []}, base, draft)
            draft.write_text(json.dumps(dict(base, traps=[{"id": "t_new", "provenance": "authored", "checks": []}])))
            problems = check()["problems"]
            self.assertTrue(any("new trap t_new drives 0 graded objective" in p for p in problems), problems)
            self.assertTrue(any("trace-derived trap(s) ['t_real']" in p for p in problems), problems)


class NeverStartedCheckpoint(unittest.TestCase):
    def test_only_a_stageless_checkpoint_may_start_fresh(self):
        from pipeline import orchestrate
        self.assertTrue(orchestrate._never_started({"schema": "arena-pipeline/checkpoint/1", "gate_repairs_remaining": 2}))
        self.assertFalse(orchestrate._never_started({"stage": "authoring", "authoring_session": {"id": "x"}}))
        self.assertFalse(orchestrate._never_started({"stage": "validated"}))


class SubmittedDesignAfterBuild(unittest.TestCase):
    """A build that ran before an interrupted check must not make the submitted design unresumable
    (c-025-python-rules-engine, 2026-10-14); a changed repository still is refused."""

    def test_regenerated_deps_are_excused_and_repo_changes_are_not(self):
        from pipeline import checkpoint
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "environment/repo").mkdir(parents=True)
            (bundle / "environment/repo/app.py").write_text("x = 1\n")
            (bundle / "provenance").mkdir()
            session = bundle / "verification/session"
            session.mkdir(parents=True)
            checkpoint.submitted(bundle, session, {"deps": {"requirements.txt": "rich==13.7.1\n"}})
            (bundle / "environment/deps").mkdir()
            (bundle / "environment/deps/requirements.txt").write_text("rich==13.7.1\n")
            self.assertEqual(checkpoint.submitted_spec(bundle)["deps"], {"requirements.txt": "rich==13.7.1\n"})
            (bundle / "environment/repo/app.py").write_text("x = 2\n")
            with self.assertRaisesRegex(ValueError, "workspace changed"):
                checkpoint.submitted_spec(bundle)


class ComputedSuiteLoads(unittest.TestCase):
    """check_draft advises when a hidden suite loads a module by a computed name (2026-10-15, ai-agent v9)."""

    def test_computed_import_is_advised_and_literal_imports_are_not(self):
        from pipeline.author.agent_designer import computed_load_advisories
        computed = "import importlib\ndef make(name):\n    return importlib.import_module(name)\n"
        built = ("import sys\nfrom types import ModuleType\ndef make(name, path, source):\n"
                 "    m = ModuleType(name); sys.modules[name] = m\n"
                 "    exec(compile(source, str(path), 'exec'), m.__dict__)\n    return m\n"
                 "import deepseek_agent.agent.core\n")
        spec = {"suites": {"conftest.py": computed, "test_ok.py": built, "data/x.json": "{}"}}
        out = computed_load_advisories(spec)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("ADVISORY suites/conftest.py:"))

    def test_command_check_in_a_python_task_is_advised(self):
        from pipeline.author.agent_designer import computed_load_advisories
        checks = [{"id": "g_scan", "runner": {"type": "command", "shell": "grep -r x ."}},
                  {"id": "o_a", "runner": {"type": "pytest", "select": ["t.py::t"]}}]
        self.assertEqual([a.split(":")[0] for a in computed_load_advisories(
            {"toolchain": {"name": "python"}, "checks": checks})], ["ADVISORY checks/g_scan"])
        self.assertEqual(computed_load_advisories({"toolchain": {"name": "node"}, "checks": checks}), [])


if __name__ == "__main__":
    unittest.main()
