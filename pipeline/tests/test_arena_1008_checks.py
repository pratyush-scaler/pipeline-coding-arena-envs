"""Ship-time checks added after Arena's feedback of 2026-10-08 (23 fixed coding tasks).

Each case is the shape Arena found in a delivered package: a Landlock fence denying cross-directory
renames, an empty coverage-critic run counted as coverage, a 'leave it' file nothing protected, and
[[steps]] that left Harbor's 600 s verifier default in force.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import shipcheck
from pipeline.author import build


class Arena1008Checks(unittest.TestCase):
    def setUp(self):
        self.b = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.b, True)
        (self.b / "tests/suites").mkdir(parents=True)
        (self.b / "verification").mkdir()
        (self.b / "steps/01-x").mkdir(parents=True)

    def test_a_fence_without_refer_is_a_finding_and_with_refer_is_not(self):
        fence = "CREATE, ADD, RESTRICT = 444, 445, 446\nattr = Ruleset(READ_FILE | READ_DIR)\n"
        (self.b / "tests/suites/m.py").write_text(fence)
        self.assertEqual([f["check"] for f in shipcheck.sandbox_refer(self.b)], ["sandbox.refer"])
        (self.b / "tests/suites/m.py").write_text(fence + "REFER = 1 << 13\n")
        self.assertEqual(shipcheck.sandbox_refer(self.b), [])

    def test_an_empty_critic_run_is_not_coverage(self):
        (self.b / "verification/coverage-critic.json").write_text(
            json.dumps({"steps": {"01-x": {"proposals": [], "raw_chars": 0}}, "gaps": []}))
        self.assertEqual([f["check"] for f in shipcheck.critic_empty(self.b)], ["coverage.critic_empty"])
        (self.b / "verification/coverage-critic.json").write_text(
            json.dumps({"steps": {"01-x": {"proposals": [{"applied": True, "reward": 0.4}], "raw_chars": 900}}, "gaps": []}))
        self.assertEqual(shipcheck.critic_empty(self.b), [])

    def test_a_critic_answer_that_executed_nothing_is_not_coverage(self):
        # c-024-vanilla-js-webapp v15 (2026-10-09): 2176 characters, zero proposals, recorded as "0 gaps".
        for row in ({"proposals": [], "raw_chars": 2176},
                    {"proposals": [{"applied": False, "error": "old text not found"}], "raw_chars": 3000}):
            (self.b / "verification/coverage-critic.json").write_text(json.dumps({"steps": {"01-x": row}, "gaps": []}))
            self.assertEqual([f["check"] for f in shipcheck.critic_empty(self.b)], ["coverage.critic_empty"])

    def test_a_leave_it_file_needs_a_hash_guard_or_a_suite_that_reads_it(self):
        (self.b / "steps/01-x/instruction.md").write_text("make the api work first, leave the html for now in index.html.\n")
        (self.b / "tests/fixture-manifest.json").write_text(json.dumps({"files": {"app/frontend/index.html": {}}}))
        (self.b / "tests/checks.json").write_text(json.dumps({"checks": []}))
        self.assertEqual([f["subject"] for f in shipcheck.preservation_unguarded(self.b)], ["app/frontend/index.html"])
        (self.b / "tests/checks.json").write_text(json.dumps({"checks": [
            {"id": "g", "kind": "guardrail", "runner": {"type": "hash-guard", "paths": ["app/frontend/index.html"]}}]}))
        self.assertEqual(shipcheck.preservation_unguarded(self.b), [])

    def test_every_step_names_a_verifier_timeout_covering_its_checks(self):
        checks = [{"id": f"o{i}", "step": "01-x", "runner": {"type": "pytest"}} for i in range(16)] + \
                 [{"id": "g", "step": "01-x", "runner": {"type": "hash-guard", "paths": ["a"]}}]
        (self.b / "tests/checks.json").write_text(json.dumps({"checks": checks}))
        (self.b / "task.toml").write_text('[[steps]]\nname = "01-x"\n[steps.verifier]\nenv = { ARENA_STEP = "01-x" }\n')
        self.assertEqual([f["check"] for f in shipcheck.step_timeouts(self.b)], ["harbor.step_timeout"])
        toml = build._steps_toml([{"name": "01-x"}], checks, 1800.0)
        self.assertIn("timeout_sec = 2520.0", toml)            # 16 x 120 s + the 600 s margin
        (self.b / "task.toml").write_text(toml)
        self.assertEqual(shipcheck.step_timeouts(self.b), [])
        self.assertNotIn("timeout_sec", build._steps_toml([{"name": "01-x"}]))

    def test_an_unrooted_hidden_tree_pattern_in_a_suite_is_a_finding(self):
        (self.b / "tests/suites/t.py").write_text('_R = re.compile(rb"ARENA_REPO|private-grader|/tests/(suites|data)")\n')
        self.assertEqual([f["check"] for f in shipcheck.suite_unrooted_scan(self.b)], ["suite.unrooted_scan"])
        (self.b / "tests/suites/t.py").write_text(
            '_R = re.compile(rb"ARENA_REPO|(?:\\A|[\\s\\"\'(=,:])(?:/tests/(?:suites|data)\\b|private-grader)")\n')
        self.assertEqual(shipcheck.suite_unrooted_scan(self.b), [])


if __name__ == "__main__":
    unittest.main()


class SuiteAdd(unittest.TestCase):
    """shipfix suite_add: a new hidden data file from a workspace file, pinned by sha256 (c-003-firebase, 2026-10-09)."""

    def test_adds_pinned_data_and_refuses_the_rest(self):
        import hashlib
        import tempfile
        from pipeline import shipfix
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "tw.js"
            f.write_text("(()=>{})();\n")
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            spec = {"suites": {"support/harness.js": "x"}}
            done = shipfix.edit_spec(spec, {"suite_add": [{"path": "data/tw.js", "from_file": str(f), "sha256": sha}]})
            self.assertEqual(spec["suites"]["data/tw.js"], "(()=>{})();\n")
            self.assertTrue(done and done[0].startswith("suites/data/tw.js"))
            for bad in ({"path": "support/tw.js", "from_file": str(f), "sha256": sha},
                        {"path": "data/tw.js", "from_file": str(f), "sha256": sha},
                        {"path": "data/other.js", "from_file": str(f), "sha256": "0" * 64}):
                with self.assertRaises(ValueError):
                    shipfix.edit_spec(spec, {"suite_add": [bad]})


class SpecTextReplace(unittest.TestCase):
    """shipfix spec_text_replace: a privacy redaction across every string of the spec (c-008-flask, 2026-10-09)."""

    def test_replaces_everywhere_and_refuses_a_missing_text(self):
        from pipeline import shipfix
        spec = {"suites": {"t.py": "x('send tips to desk@coindesk.com')"},
                "checks": [{"id": "o", "basis": "tips to desk@coindesk.com"}], "_producer": {"model": "m"}}
        done = shipfix.edit_spec(spec, {"spec_text_replace": [{"old": "desk@coindesk.com", "new": "desk@example.com"}]})
        self.assertNotIn("coindesk", json.dumps(spec))
        self.assertIn("(2x)", done[0])
        with self.assertRaises(ValueError):
            shipfix.edit_spec(spec, {"spec_text_replace": [{"old": "desk@coindesk.com", "new": "x"}]})


class DeliverablesRemove(unittest.TestCase):
    """shipfix deliverables_remove: a private-helper contract dropped (c-028 v22 review C1, 2026-10-09)."""

    def test_removes_named_entries_and_refuses_the_rest(self):
        from pipeline import shipfix
        spec = {"deliverables": [{"kind": "file", "path": "a/scoring.py"}, {"kind": "file", "path": "a/main.py"},
                                 {"kind": "file", "path": "a/accounts.py"}]}
        done = shipfix.edit_spec(spec, {"deliverables_remove": ["a/scoring.py", "a/accounts.py"]})
        self.assertEqual([d["path"] for d in spec["deliverables"]], ["a/main.py"])
        self.assertEqual(len(done), 2)
        with self.assertRaises(ValueError):
            shipfix.edit_spec(spec, {"deliverables_remove": ["a/nope.py"]})
        with self.assertRaises(ValueError):
            shipfix.edit_spec(spec, {"deliverables_remove": ["a/main.py"]})
