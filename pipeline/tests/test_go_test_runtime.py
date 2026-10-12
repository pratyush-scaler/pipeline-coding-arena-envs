"""Private Go suite validation and exact runtime variant pinning."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import ModuleType
import tempfile
import unittest

from pipeline.author import checks, controls, runtime, toolchain, validate
from pipeline.author.grader_runtime import go_test


class PrivateGoRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PIPELINE_GO_ASSET_TEST_IMAGE"),
                         "set PIPELINE_GO_ASSET_TEST_IMAGE to the pinned Go verifier image")
    def test_private_runner_leaves_candidate_embedded_assets_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="go-asset-staging-test-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o777)
            (directory / "grade.py").write_bytes(go_test.render(runtime.GRADER.read_bytes()))
            fixture = Path(__file__).parent / "fixtures/go_private_assets_fixture.py"
            shutil.copy2(fixture, directory / "execute.py")
            command = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                       "--cap-add", "CHOWN", "--cap-add", "SETUID", "--cap-add", "SETGID",
                       "--cap-add", "DAC_READ_SEARCH", "-v", str(directory) + ":/source:ro",
                       "-v", str(directory) + ":/out", "--entrypoint", "python3",
                       os.environ["PIPELINE_GO_ASSET_TEST_IMAGE"], "/source/execute.py"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            report = json.loads((directory / "asset-staging-result.json").read_text())
            self.assertTrue(report["result"]["passed"], report["result"])
            self.assertEqual(report["result"]["tests"], {"passed": 1, "failed": 0, "skipped": 0})

    def test_go_runtime_is_pinned_and_other_toolchains_keep_their_variant(self):
        common = runtime.GRADER.read_bytes()
        reviewed = go_test.render(common)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "environment").mkdir()
            (root / "environment/toolchain.json").write_text(json.dumps(toolchain.record("go", language="go")))
            target = runtime.stage(root)
            self.assertEqual(target.read_bytes(), reviewed)
            self.assertEqual(runtime.sha256(root), hashlib.sha256(reviewed).hexdigest())
            self.assertEqual(runtime.problems(root), [])
            target.write_bytes(common)
            self.assertTrue(runtime.problems(root))
        with self.assertRaises(ValueError):
            go_test.render(reviewed)

    def test_private_suite_selection_accepts_declared_go_tests(self):
        self.assertEqual(checks.runner_problems({"type": "go-test", "cwd": "btmonit",
            "select": ["storage_test.go::TestRange"]}, ["go-test"], "o_range"), [])
        spec = {"suites": {"storage_test.go": "package hidden\nfunc TestRange() {}"}, "solution": {}}
        findings = validate._suite_problems(spec, [{"id": "o_range", "runner": {
            "type": "go-test", "select": ["storage_test.go::TestRange"]}}], {})
        self.assertEqual(findings, [])

    def test_legacy_stdout_only_go_checks_fail_closed(self):
        self.assertTrue(checks.runner_problems({"type": "go", "select": ["./..."]}, ["go"], "o_range"))
        module = ModuleType("private_go")
        module.__file__ = str(runtime.GRADER)
        exec(compile(go_test.render(runtime.GRADER.read_bytes()), module.__file__, "exec"), module.__dict__)
        record = module.run_command({"runner": {"type": "go"}}, Path("/missing"), {}, {})
        self.assertFalse(record["passed"])
        self.assertIn("go-test", record["error"])

    def test_same_package_overlay_is_private_and_cannot_escape_or_fall_back(self):
        valid = {"type": "go-test", "cwd": "btmonit", "package": "internal/storage",
                 "select": ["visible_storage_test.go::TestStorage"],
                 "overlay": {"visible_storage_test.go": "jsonl_test.go"}}
        self.assertEqual(checks.runner_problems(valid, ["go-test"], "g_visible"), [])
        for changed in ({"package": "../storage"}, {"package": "/tmp/storage"},
                        {"overlay": {"agent_test.go": "jsonl_test.go"}},
                        {"overlay": {"visible_storage_test.go": "../jsonl_test.go"}}):
            with self.subTest(changed=changed):
                self.assertTrue(checks.runner_problems({**valid, **changed}, ["go-test"], "g_visible"))

    def test_missing_or_escaping_private_go_suites_are_rejected_before_execution(self):
        for selection in ([], ["../storage_test.go"], ["/tmp/storage_test.go"],
                          ["storage.go"], ["storage_test.go::NoTest"],
                          ["storage_test.go::TestRange::extra"],
                          ["storage_test.go", "storage_test.go"]):
            with self.subTest(selection=selection):
                self.assertTrue(checks.runner_problems({"type": "go-test", "select": selection}, ["go-test"], "o_range"))
        for selection in (["missing_test.go"], ["storage_test.go::TestAbsent"]):
            with self.subTest(selection=selection):
                spec = {"suites": {"storage_test.go": "package hidden\nfunc TestRange() {}"}, "solution": {}}
                self.assertTrue(validate._suite_problems(spec, [{"id": "o_range", "runner": {
                    "type": "go-test", "select": selection}}], {}))

    def test_go_adversarial_controls_are_admitted_to_the_canonical_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "steps/01-fix/solution").mkdir(parents=True)
            (root / "steps/01-fix/solution/fix.patch").write_text("")
            (root / "steps/01-fix/solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
            plan = controls._trial_plan(root, {"traps": [], "mutants": []}, {"checks": []},
                                       ["01-fix"], {"name": "go"}, 1)
            self.assertIn("early_exit", plan)
            self.assertIn("report_forge", plan)
            self.assertEqual(plan["early_exit"]["01-fix"].shell, controls.EARLY_EXIT_SHELL["go"])
            self.assertEqual(plan["report_forge"]["01-fix"].shell, controls.REPORT_FORGE_SHELL["go"])


if __name__ == "__main__":
    unittest.main()
