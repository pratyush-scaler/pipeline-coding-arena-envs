"""The coding core, exercised without Docker.

Everything the executed smokes rest on -- the grader-as-data shape, weights,
structural guards, trap and quality validation, naming, the build's
reproducibility, the receipts the gates read, the selection floors -- has a
unit here that runs in milliseconds. The Docker and Harbor smokes
(`sandbox_smoke.py`, `native_smoke.py`) prove the same contract executes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import config, naming
from pipeline.author import checks as checks_mod
from pipeline.author import quality, traps, validate
from pipeline.author.grader_runtime import grade
from pipeline.tests import synthetic_code


def _objective(cid, step="01-fix", weight=10, band="core", **over):
    check = {"id": cid, "kind": "objective", "weight": weight, "band": band, "step": step,
             "basis": "the instruction states this rule in one full sentence",
             "runner": {"type": "pytest", "select": [f"test_x.py::{cid}"]}}
    check.update(over)
    return check


class WeightsAreABudget(unittest.TestCase):
    def test_every_task_sums_to_the_policy_total_with_equal_step_budgets(self):
        checks = [_objective("o_a", weight=3), _objective("o_b", weight=1),
                  _objective("o_c", step="02-more", weight=7),
                  {"id": "g_keep", "kind": "guardrail", "weight": 5, "band": "floor", "step": "01-fix",
                   "basis": "the instruction says the file must not change",
                   "runner": {"type": "hash-guard", "paths": ["a.py"]}}]
        checks_mod.normalise_weights(checks, steps=["01-fix", "02-more"])
        self.assertEqual(sum(c["weight"] for c in checks if c["kind"] == "objective"), checks_mod.WEIGHT_TOTAL)
        self.assertEqual({c["id"]: c["weight"] for c in checks}, {"o_a": 38, "o_b": 12, "o_c": 50, "g_keep": 0})
        self.assertEqual(checks_mod.weight_problems(checks, ["01-fix", "02-more"]), [])
        checks[0]["weight"] = 40
        self.assertTrue(any("not 100" in p for p in checks_mod.weight_problems(checks, ["01-fix", "02-more"])))

    def test_the_synthetic_checks_are_well_formed_and_a_bare_list_is_not(self):
        spec = synthetic_code.spec()
        steps = [s["name"] for s in spec["steps"]]
        self.assertEqual(checks_mod.check_problems(spec["checks"], steps, ["pytest", "command"]), [])
        broken = copy.deepcopy(spec["checks"])
        broken.append(copy.deepcopy(broken[0]))                       # duplicate id
        issues = checks_mod.check_problems(broken, steps, ["pytest", "command"])
        self.assertTrue(any("duplicate id" in p for p in issues), issues)
        objectives_only = [c for c in spec["checks"] if c["kind"] == "objective"]
        issues = checks_mod.check_problems(objectives_only, steps, ["pytest", "command"])
        self.assertTrue(any("at least one guardrail" in p for p in issues), issues)


class StructuralGuardsReadTheManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "keep.py").write_text("print(1)\n")
        (self.repo / "data.csv").write_text("a,b\n1,2\n")
        self.manifest = {rel: {"sha256": hashlib.sha256((self.repo / rel).read_bytes()).hexdigest(),
                               "bytes": (self.repo / rel).stat().st_size}
                         for rel in ("keep.py", "data.csv")}

    def guard(self, runner):
        return grade.structural({"runner": runner}, self.repo, self.manifest)

    def test_hash_guard_fails_on_any_byte_change_or_removal(self):
        self.assertTrue(self.guard({"type": "hash-guard", "paths": ["keep.py"]})[0])
        (self.repo / "keep.py").write_text("print(2)\n")
        ok, why = self.guard({"type": "hash-guard", "paths": ["keep.py"]})
        self.assertFalse(ok)
        self.assertIn("modified", why)
        (self.repo / "keep.py").unlink()
        self.assertFalse(self.guard({"type": "hash-guard", "paths": ["keep.py"]})[0])
        self.assertTrue(self.guard({"type": "hash-guard", "paths": ["keep.py"], "mode": "absent"})[0])
        self.assertFalse(self.guard({"type": "hash-guard", "paths": ["nope.py"]})[0])

    def test_size_guard_floor_from_the_fixture(self):
        self.assertTrue(self.guard({"type": "size-guard", "path": "data.csv", "min_bytes": "fixture"})[0])
        (self.repo / "data.csv").write_text("")
        ok, why = self.guard({"type": "size-guard", "path": "data.csv", "min_bytes": "fixture"})
        self.assertFalse(ok)
        self.assertIn("shrank", why)

    def test_tree_guard_counts_only_undeclared_new_files(self):
        self.assertTrue(self.guard({"type": "tree-guard", "allow": [], "max_new_files": 0})[0])
        (self.repo / "scratch.txt").write_text("x")
        self.assertFalse(self.guard({"type": "tree-guard", "allow": [], "max_new_files": 0})[0])
        self.assertTrue(self.guard({"type": "tree-guard", "allow": ["*.txt"], "max_new_files": 0})[0])
        self.assertTrue(self.guard({"type": "tree-guard", "allow": [], "max_new_files": 1})[0])


class CommandsAreIsolated(unittest.TestCase):
    def test_pytest_runs_isolated_from_the_agents_tree(self):
        argv, shell = grade.command_for({"type": "pytest", "select": ["test_x.py::test_y"]}, Path("/app/repo"))
        self.assertFalse(shell)
        self.assertIn("-I", argv)
        self.assertIn("--rootdir", argv)
        self.assertIn("--confcutdir", argv)
        self.assertTrue(argv[-1].endswith("suites/test_x.py::test_y"))
        self.assertNotIn("/app/repo", " ".join(argv))

    def test_command_runner_and_unknown_types(self):
        self.assertEqual(grade.command_for({"type": "command", "argv": ["ls", "-la"]}, Path("/x")), (["ls", "-la"], False))
        self.assertEqual(grade.command_for({"type": "command", "shell": "make test"}, Path("/x")), ("make test", True))
        with self.assertRaises(ValueError):
            grade.command_for({"type": "magic"}, Path("/x"))


class NamingFollowsTheLevel(unittest.TestCase):
    def test_working_and_delivered_names(self):
        self.assertEqual(naming.short_name("F2", "c-007-flask-api"), "a_f2_flask-api")
        self.assertEqual(naming.package_name("f3", "c007-flask-api"), "arena/a_f3_flask-api")
        self.assertEqual(naming.parse("arena/a_f1_cli-tool"), {"level": "F1", "task": "cli-tool", "short_name": "a_f1_cli-tool"})
        self.assertIsNone(naming.parse("arena/c-001-cli-tool"))
        with self.assertRaises(ValueError):
            naming.short_name("F4", "x")

    def test_colliding_names_are_disambiguated_by_working_number(self):
        items = [("F2", "c-002-brand"), ("F2", "c-003-brand"), ("F3", "c-004-other")]
        self.assertEqual(naming.unique_names(items),
                         {"c-002-brand": "a_f2_brand-002", "c-003-brand": "a_f2_brand-003", "c-004-other": "a_f3_other"})
        self.assertEqual(naming.collisions(items), {"a_f2_brand": ["c-002-brand", "c-003-brand"]})


class TrapsMustBeGroundedAndProved(unittest.TestCase):
    AUTHORED = {"id": "t_decoy", "type": "decoy_location", "surface": "ledger/report.py", "provenance": "authored",
                "checks": ["o_a"], "mutant": "m_decoy", "beatable_by": "read the parser before patching the report",
                "grounding": "the report is where the symptom shows; the parser drops the sign one module away",
                "grounded_in": "ledger/parse.py"}

    def test_an_authored_trap_needs_grounding_and_a_mutant(self):
        ok = traps.validate_all([self.AUTHORED], {"o_a"}, {"ledger/parse.py"}, {"m_decoy"})
        self.assertEqual(ok, [])
        for field, value, fragment in (("mutant", None, "mutant"), ("grounded_in", "nowhere.py", "existing fixture file"),
                                       ("grounding", "short", "30 characters"), ("checks", ["o_zzz"], "undeclared check"),
                                       ("type", "vibes", "unknown type")):
            broken = {**self.AUTHORED, field: value}
            issues = traps.validate_all([broken], {"o_a"}, {"ledger/parse.py"}, {"m_decoy"})
            self.assertTrue(any(fragment in p for p in issues), (field, issues))

    def test_a_trace_derived_trap_needs_the_users_words_and_a_compatible_class(self):
        derived = {**self.AUTHORED, "provenance": "trace_derived", "type": "symptom_patch",
                   "derived_from": "0" * 8 + "-0000-0000-0000-" + "0" * 12 + "::msg12",
                   "correction_class": "persistent_defect", "quote": "it still shows refunds as sales",
                   "grounding": None, "grounded_in": None}
        self.assertEqual(traps.validate_all([derived], {"o_a"}, set(), {"m_decoy"}), [])
        issues = traps.validate_all([{**derived, "correction_class": "taste"}], {"o_a"}, set(), {"m_decoy"})
        self.assertTrue(any("cannot be derived" in p for p in issues), issues)
        issues = traps.validate_all([{**derived, "derived_from": "somewhere"}], {"o_a"}, set(), {"m_decoy"})
        self.assertTrue(any("derived_from" in p for p in issues), issues)

    def test_the_trace_floor_follows_the_tier(self):
        self.assertGreater(traps.floor_for(1), traps.floor_for(4))
        self.assertEqual(traps.floor_for("nonsense"), traps.floor_for(4))
        self.assertEqual(traps.trace_derived_share([self.AUTHORED, {**self.AUTHORED, "provenance": "trace_derived"}]), 0.5)


class TheDifficultyCaseTiesMisreadingsToMutants(unittest.TestCase):
    def setUp(self):
        self.spec = synthetic_code.spec()
        self.quality = self.spec["quality"]
        self.checks, self.traps, self.mutants = self.spec["checks"], self.spec["traps"], self.spec["mutants"]

    def problems(self, quality=None):
        return quality_mod_problems(quality or self.quality, self.checks, self.traps, self.mutants)

    def test_the_synthetic_case_is_complete(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(quality.band_problems(self.checks), [])
        self.assertEqual(quality.quality_problems(self.spec), [])

    def test_every_objective_driving_trap_needs_its_own_misreading_with_its_own_mutant(self):
        q = copy.deepcopy(self.quality)
        dropped = q["difficulty_case"]["misreadings"].pop()
        issues = self.problems(q)
        self.assertTrue(any("has no misreading" in p for p in issues), issues)
        q = copy.deepcopy(self.quality)
        q["difficulty_case"]["misreadings"][0]["mutant"] = dropped["mutant"]
        issues = self.problems(q)
        self.assertTrue(any("belongs to trap" in p or "declares mutant" in p for p in issues), issues)

    def test_the_trap_hunt_must_cover_every_trap(self):
        q = copy.deepcopy(self.quality)
        q["difficulty_case"]["pitfalls_considered"] = [
            p for p in q["difficulty_case"]["pitfalls_considered"] if p.get("trap") != self.traps[0]["id"]]
        issues = self.problems(q)
        self.assertTrue(any("does not appear as a planted pitfall" in p for p in issues), issues)


def quality_mod_problems(q, checks, trap_list, mutants):
    return quality.difficulty_case_problems(q, checks, trap_list, mutants)


class TheBuildIsReproducibleAndComplete(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bundle = synthetic_code.write_bundle(Path(cls.tmp.name) / "task")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_package_has_every_surface_the_contract_names(self):
        for rel in ("task.toml", "environment/Dockerfile", "environment/toolchain.json", "environment/repo/ledger/parse.py",
                    "tests/Dockerfile", "tests/test.sh", "tests/grade.py", "tests/checks.json", "tests/rubric.json",
                    "tests/fixture-manifest.json", "solution/solve.sh", "solution/fix.patch",
                    "steps/01-fix/instruction.md", "authoring/spec.json", "authoring/traps.json",
                    "authoring/mutants/m_symptom_only.patch", "authoring/alt/01-fix.patch", "provenance/ledger.json"):
            self.assertTrue((self.bundle / rel).is_file(), rel)
        toml = (self.bundle / "task.toml").read_text()
        self.assertIn('schema_version = "1.4"', toml)
        self.assertIn("[[steps]]", toml)
        self.assertIn('name = "arena/a_f0_', toml)

    def test_the_shipped_checks_carry_no_authoring_fields_and_sum_to_the_total(self):
        doc = json.loads((self.bundle / "tests/checks.json").read_text())
        self.assertEqual(doc["schema"], checks_mod.SCHEMA)
        self.assertEqual(sum(c["weight"] for c in doc["checks"] if c["kind"] == "objective"), checks_mod.WEIGHT_TOTAL)
        for check in doc["checks"]:
            for key in checks_mod.AUTHORING_ONLY_CHECK_KEYS:
                self.assertNotIn(key, check, (check["id"], key))
        self.assertTrue(all(c["weight"] == 0 for c in doc["checks"] if c["kind"] == "guardrail"))

    def test_hidden_inputs_are_not_in_the_repository_and_the_grader_is_the_shipped_one(self):
        suites = self.bundle / "tests/suites"
        hidden = [p.relative_to(suites).as_posix() for p in suites.rglob("*") if p.is_file() and "data/" in p.as_posix()]
        self.assertTrue(hidden)
        for rel in hidden:
            self.assertFalse((self.bundle / "environment/repo" / Path(rel).name).exists(), rel)
        shipped = (self.bundle / "tests/grade.py").read_bytes()
        self.assertEqual(shipped, Path(grade.__file__).read_bytes())

    def test_validation_and_regeneration_are_clean(self):
        from pipeline import assurance
        from pipeline.author import toolchain
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        self.assertEqual(validate.validate(spec, self.bundle), [])
        self.assertEqual(assurance.generated_problems(self.bundle), [])
        # The gate regenerates both Dockerfiles from the RELOADED toolchain.json
        # (keys sorted on disk); the build wrote them from the in-memory record.
        # They diverged once on ENV line order (found by the gate, 2026-09-27).
        self.assertEqual(toolchain.dockerfile_problems(self.bundle), [])

    def test_the_ledger_records_an_authored_fixture_as_f0_and_receipts_are_demanded(self):
        from pipeline.author import controls, probes
        ledger = json.loads((self.bundle / "provenance/ledger.json").read_text())
        self.assertEqual(ledger["faithfulness_level"], "F0")
        self.assertTrue(all(e["origin"] == "authored" for e in ledger["entries"]))
        self.assertTrue(any("controls.json is missing" in p for p in controls.problems(self.bundle)))
        self.assertTrue(any("probes.json is missing" in p for p in probes.problems(self.bundle)))


class SelectionKeysOnWhatARepositoryIsMadeOf(unittest.TestCase):
    def test_runtime_class_follows_the_dominant_language(self):
        self.assertEqual(config.runtime_class({"python": 900, "html": 100}), "native")
        self.assertEqual(config.runtime_class({"html": 500, "javascript": 400}), "browser")
        self.assertEqual(config.runtime_class({"lua": 10, "python": 9}), "engine")
        self.assertEqual(config.runtime_class({}), "none")

    def test_vendored_and_test_paths(self):
        from pipeline.dump import index
        self.assertTrue(index.vendored("frontend/node_modules/react/index.js"))
        self.assertTrue(index.vendored(".venv/lib/python3.12/site-packages/x.py"))
        self.assertFalse(index.vendored("src/app.py"))
        self.assertTrue(index.is_test_path("tests/test_api.py"))
        self.assertTrue(index.is_test_path("src/app.test.ts"))
        self.assertTrue(index.is_test_path("pkg/handler_test.go"))
        self.assertFalse(index.is_test_path("src/attest.py"))

    def test_kill_filters_name_their_reason_code(self):
        from pipeline.select import score
        base = {"n_turns": 6, "corrections": 2, "categories": ["code_writing"], "src_paths": 9,
                "src_bytes": 40_000, "runtime_class": "native", "languages": {"python": 40_000},
                "evidence_schema": "arena-pipeline/evidence/1"}
        self.assertTrue(score.kill_filters(base).kept)
        self.assertEqual(score.kill_filters({**base, "n_turns": 1}).code, "kill_filter")
        self.assertEqual(score.kill_filters({**base, "runtime_class": "engine", "languages": {"lua": 9}}).code, "no_runtime")
        self.assertEqual(score.kill_filters({**base, "src_paths": 1, "src_bytes": 300}).code, "no_source_fixture")
        rr, parts = score.replay_readiness({**base, "src_versions": 2, "final_tree_recoverable_share": 0.9,
                                            "trail_paths": 0, "bash_total": 10, "bash_truncated": 0})
        self.assertTrue(0.0 <= rr <= 1.0)
        self.assertEqual(parts["runtime"], 1.0)
        self.assertLess(score.replay_readiness({**base, "runtime_class": "browser"})[0], rr)

    def test_extraction_helpers(self):
        from pipeline.author import extract
        self.assertEqual(extract.clean_path("/home/user/app/x.py"), "app/x.py")
        self.assertTrue(extract.vendored("app/node_modules/x.js"))
        self.assertEqual(extract.redaction_markers('key = [SECURITY_DATA]"K"; who = [PATH_USERNAME_1]'), 2)


if __name__ == "__main__":
    unittest.main()


class ExitStatusIsNotEvidence(unittest.TestCase):
    """Reviewer finding, 2026-09-27: a process that ends with status 0 before
    running any test must not pass. The grader counts what each runner reports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_pytest_runs_through_a_stdin_bootstrap_and_the_report_must_echo_the_nonce(self):
        argv, _ = grade.command_for({"type": "pytest", "select": ["test_x.py"]}, Path("/app/repo"), report_dir=self.dir)
        self.assertEqual(argv[:3], [grade.sys.executable, "-I", "-c"])
        self.assertNotIn("junit", " ".join(argv[3:]).replace(grade.PYTEST_BOOTSTRAP, ""))
        # 2026-09-30: the nonce is no longer handed to pytest at all (it was
        # readable from pytest's Config); the bootstrap disables the junitxml
        # plugin, writes the report itself and deletes its configuration.
        self.assertIn("no:junitxml", grade.PYTEST_BOOTSTRAP)
        self.assertNotIn("junit_suite_name", grade.PYTEST_BOOTSTRAP)
        self.assertNotIn("--junitxml", grade.PYTEST_BOOTSTRAP)
        self.assertIn("sys.exit(pytest.main(*_prepare()))", grade.PYTEST_BOOTSTRAP)
        self.assertNotIn("def _boot", grade.PYTEST_BOOTSTRAP)
        xml = ('<testsuites><testsuite name="{name}" tests="3" failures="1" errors="0" skipped="1">'
               '<testcase classname="test_x" name="test_a"/>'
               '<testcase classname="test_x" name="test_b"><failure/></testcase>'
               '<testcase classname="test_x" name="test_c"><skipped/></testcase></testsuite></testsuites>')
        (self.dir / "junit.xml").write_text(xml.format(name="arena-n1"))
        counts = grade.test_counts("pytest", "", "", self.dir, self.dir, nonce="arena-n1", select=["test_x.py::test_a"])
        self.assertEqual(counts, {"passed": 1, "failed": 1, "skipped": 1})
        # A forged report: wrong nonce, or one that does not cover the selection.
        self.assertIsNone(grade.test_counts("pytest", "", "", self.dir, self.dir, nonce="arena-other", select=["test_x.py"]))
        self.assertIsNone(grade.test_counts("pytest", "", "", self.dir, self.dir, nonce="arena-n1", select=["test_y.py::test_z"]))
        (self.dir / "junit.xml").unlink()
        self.assertIsNone(grade.test_counts("pytest", "", "", self.dir, self.dir, nonce="arena-n1", select=["test_x.py"]))

    def test_the_other_runners_report_completed_tests(self):
        (self.dir / "unittest.json").write_text(json.dumps({"nonce": "n", "ran": ["test_a.T.test_1", "test_a.T.test_2"],
                                                            "testsRun": 2, "failures": 1, "errors": 0, "skipped": 0}))
        self.assertEqual(grade.test_counts("unittest", "", "", self.dir, self.dir, nonce="n", select=["test_a"]),
                         {"passed": 1, "failed": 1, "skipped": 0})
        self.assertIsNone(grade.test_counts("unittest", "", "", self.dir, self.dir, nonce="other", select=["test_a"]))
        self.assertIsNone(grade.test_counts("unittest", "", "", self.dir, self.dir, nonce="n", select=["test_b"]))
        # A printed summary is not a report: only the bootstrap's file counts.
        self.assertIsNone(grade.test_counts("unittest", "", "Ran 3 tests\n\nOK\n", self.dir / "nowhere", self.dir))
        (self.dir / "junit.xml").write_text('<testsuites><testsuite name="/s/a.test.js" tests="3" failures="0" errors="0" '
                                            'skipped="1"/></testsuites>')
        self.assertEqual(grade.test_counts("node-test", "", "", self.dir, self.dir, select=["a.test.js"]),
                         {"passed": 2, "failed": 0, "skipped": 1})
        self.assertIsNone(grade.test_counts("node-test", "", "", self.dir, self.dir, select=["b.test.js"]))
        (self.dir / "junit.xml").unlink()
        self.assertIsNone(grade.test_counts("node-test", "# pass 3\n", "", self.dir, self.dir))
        cargo = "running 2 tests\ntest a ... ok\ntest b ... ok\n\ntest result: ok. 2 passed; 0 failed; 1 ignored; 0 measured\n"
        self.assertEqual(grade.test_counts("cargo", cargo, "", self.dir, self.dir), {"passed": 2, "failed": 0, "skipped": 1})
        go = "\n".join(json.dumps(e) for e in [{"Action": "run", "Test": "TestA"}, {"Action": "pass", "Test": "TestA"},
                                              {"Action": "fail", "Test": "TestB"}, {"Action": "pass", "Package": "p"}])
        self.assertEqual(grade.test_counts("go", go, "", self.dir, self.dir), {"passed": 1, "failed": 1, "skipped": 0})
        self.assertIsNone(grade.test_counts("go", '{"Action":"pass","Package":"p"}', "", self.dir, self.dir))
        mocha = 'noise\n{"stats": {"passes": 4, "failures": 0, "pending": 2}, "tests": []}\n'
        self.assertEqual(grade.test_counts("mocha", mocha, "", self.dir, self.dir), {"passed": 4, "failed": 0, "skipped": 2})
        (self.dir / "jest.json").write_text(json.dumps({"numTotalTests": 5, "numPassedTests": 5, "numFailedTests": 0,
                                                        "numPendingTests": 0, "numTodoTests": 0}))
        self.assertEqual(grade.test_counts("jest", "", "", self.dir, self.dir), {"passed": 5, "failed": 0, "skipped": 0})
        self.assertIsNone(grade.test_counts("command", "", "", self.dir, self.dir))

    def test_an_early_exit_with_status_zero_fails_the_check(self):
        # A real unittest run whose module exits 0 at import: status 0, no tests.
        suite = self.dir / "suites"
        suite.mkdir()
        (suite / "test_exit.py").write_text("import os\nos._exit(0)\n\nimport unittest\n\nclass T(unittest.TestCase):\n"
                                            "    def test_a(self):\n        self.assertTrue(True)\n")
        (suite / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n"
                                          "    def test_a(self):\n        self.assertTrue(True)\n")
        # ...and one that forges every report it can find before exiting. The
        # grader's private report directory is created under the process's
        # temp root; the forger searches exactly there.
        import os
        import tempfile as _tf
        reports_root = self.dir / "tmp"
        reports_root.mkdir()
        (suite / "test_forge.py").write_text(
            "import glob, os\n"
            f"for p in glob.glob({str(reports_root)!r} + '/**/*.json', recursive=True) + "
            f"glob.glob({str(reports_root)!r} + '/**/*.xml', recursive=True):\n"
            "    try:\n"
            "        open(p, 'w').write('{\"nonce\": \"x\", \"ran\": [\"test_forge\"], \"testsRun\": 1}')\n"
            "    except OSError:\n        pass\n"
            "os._exit(0)\n")
        import unittest.mock as mock
        with mock.patch.object(grade, "HERE", self.dir), mock.patch.dict(os.environ, {"TMPDIR": str(reports_root)}), \
                mock.patch.object(_tf, "tempdir", None):
            # These planted modules stand in for fixture files here (hashes in the
            # manifest), so the introspection refusal (2026-09-30) does not pre-empt
            # what this test measures: an early exit or a nonce-less forgery is not a pass.
            manifest = {f"suites/{m}.py": {"sha256": hashlib.sha256((suite / f"{m}.py").read_bytes()).hexdigest(), "bytes": 1}
                        for m in ("test_exit", "test_forge", "test_ok")}
            for module, expected in (("test_exit", False), ("test_forge", False), ("test_ok", True)):
                record = grade.run_command({"id": f"o_{module}", "runner": {"type": "unittest", "select": [module]}},
                                           self.dir, {"env": {}}, manifest)
                self.assertEqual(record["passed"], expected, record)
            self.assertEqual(record["tests"], {"passed": 1, "failed": 0, "skipped": 0})

    def test_a_planted_symlink_counts_against_the_tree_guard(self):
        repo = self.dir / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("x")
        manifest = {"a.py": {"sha256": hashlib.sha256(b"x").hexdigest(), "bytes": 1}}
        (repo / "link").symlink_to("a.py")
        ok, why = grade.structural({"runner": {"type": "tree-guard", "allow": [], "max_new_files": 0}}, repo, manifest)
        self.assertFalse(ok)
        self.assertIn("link", why)


class DifficultyFloorsAreEnforced(unittest.TestCase):
    """User, 2026-09-27: extremely hard tasks with genuine traps, and
    instructions in the user's voice. What the prompt urges, the validator refuses without."""

    def setUp(self):
        self.spec = synthetic_code.spec()

    def test_the_synthetic_task_clears_the_floors(self):
        self.assertEqual(validate.trap_shape_problems(self.spec["traps"], self.spec["checks"]), [])
        for step in self.spec["steps"]:
            self.assertEqual(validate.voice_problems(step["instruction"], "x"), [])

    def test_too_few_types_premises_or_coupling_is_refused(self):
        from pipeline import policy
        traps_ = copy.deepcopy(self.spec["traps"])
        one_type = [{**t, "type": "constraint"} for t in traps_]
        issues = validate.trap_shape_problems(one_type, self.spec["checks"])
        self.assertTrue(any("distinct types" in p for p in issues), issues)
        self.assertTrue(any("premise trap" in p for p in issues), issues)
        uncoupled = [{**t, "checks": t["checks"][:1]} for t in traps_]
        issues = validate.trap_shape_problems(uncoupled, self.spec["checks"])
        self.assertTrue(any("two or more objectives" in p for p in issues), issues)
        issues = validate.trap_shape_problems(traps_[:policy.MIN_TRAPS_PER_TASK - 1], self.spec["checks"])
        self.assertTrue(any("genuine traps are required" in p for p in issues), issues)

    def test_the_first_pass_patch_must_be_an_executed_mutant(self):
        q = copy.deepcopy(self.spec["quality"])
        q["difficulty_case"].pop("first_try_mutant")
        issues = quality.difficulty_case_problems(q, self.spec["checks"], self.spec["traps"], self.spec["mutants"])
        self.assertTrue(any("first_try_mutant" in p for p in issues), issues)
        q["difficulty_case"]["first_try_mutant"] = "m_nope"
        issues = quality.difficulty_case_problems(q, self.spec["checks"], self.spec["traps"], self.spec["mutants"])
        self.assertTrue(any("first_try_mutant" in p for p in issues), issues)

    def test_an_assistant_written_instruction_is_refused_and_a_human_one_is_not(self):
        assistant = ("## Task\n\nYour task is to fix the ledger.\n\n### Requirements\n- Ensure that refunds are negative\n"
                     "- Ensure that months are keyed YYYY-MM\n- Additionally, keep the CLI unchanged\n- Add tests\n\n"
                     "Acceptance criteria: all tests pass.")
        issues = validate.voice_problems(assistant, "step")
        self.assertTrue(any("headings" in p for p in issues), issues)
        self.assertTrue(any("bullet" in p for p in issues), issues)
        self.assertTrue(any("assistant idioms" in p for p in issues), issues)
        human = ("the monthly report is wrong again. when the file has two years in it jan 2023 and jan 2024 get "
                 "added together, and refunds still show up as sales -- a refund should reduce the month it lands "
                 "in, the total can go negative if it has to. months come out as YYYY-MM in date order. don't touch "
                 "cli.py, another team owns it.\n\nheres the error I get:\n```\nKeyError: '01'\n```")
        self.assertEqual(validate.voice_problems(human, "step"), [])


class PlantedExitsAreVisibleInTheTrajectory(unittest.TestCase):
    def test_markers_the_fixture_did_not_carry_are_flagged_only_when_added(self):
        from pipeline import attribution
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "b"
            (bundle / "environment/repo/pkg").mkdir(parents=True)
            (bundle / "environment/repo/pkg/__init__.py").write_text("VERSION = 1\n")
            (bundle / "environment/repo/pkg/cli.py").write_text("import sys\n\ndef main():\n    sys.exit(0)\n")
            evidence = Path(tmp) / "t/steps/01-fix"
            captured = evidence / "artifacts/app/repo/pkg"
            captured.mkdir(parents=True)
            (captured / "__init__.py").write_text("import os\nos._exit(0)\nVERSION = 1\n")
            (captured / "cli.py").write_text("import sys\n\ndef main():\n    sys.exit(0)\n")   # unchanged: sys.exit(0) was already there
            hits = attribution._planted_markers(bundle, evidence)
            self.assertEqual(hits, [("pkg/__init__.py", "os._exit(")])
            self.assertEqual(attribution._planted_markers(bundle, None), [])


class TwoStepTasksBuildAndBudget(unittest.TestCase):
    """The second step's patches apply on top of the first, earlier captures
    feed /arena-history, and the objective budget splits equally per step."""

    def test_the_two_step_synthetic_validates_regenerates_and_splits_the_budget(self):
        from pipeline import assurance
        with tempfile.TemporaryDirectory() as tmp:
            bundle = synthetic_code.write_bundle(Path(tmp) / "t", two_steps=True)
            spec = json.loads((bundle / "authoring/spec.json").read_text())
            self.assertEqual([s["name"] for s in spec["steps"]], ["01-fix", "02-counts"])
            self.assertEqual(validate.validate(spec, bundle), [])
            self.assertEqual(assurance.generated_problems(bundle), [])
            doc = json.loads((bundle / "tests/checks.json").read_text())
            by_step = {s: sum(c["weight"] for c in doc["checks"] if c["step"] == s and c["kind"] == "objective")
                       for s in ("01-fix", "02-counts")}
            self.assertEqual(by_step, {"01-fix": 50, "02-counts": 50})
            for rel in ("steps/02-counts/instruction.md", "steps/02-counts/solution/fix.patch",
                        "authoring/alt/02-counts.patch", "authoring/mutants/m_step2_skip_refunds.patch",
                        "tests/suites/test_counts.py"):
                self.assertTrue((bundle / rel).is_file(), rel)
            self.assertEqual(validate.voice_problems(spec["steps"][1]["instruction"], "step 2"), [])


class AReadableFixIsSeveralLinesTogether(unittest.TestCase):
    def test_one_shared_idiom_is_not_a_leak_but_a_copy_of_the_patch_is(self):
        from pipeline.author import probes
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            repo = bundle / "environment/repo"
            repo.mkdir(parents=True)
            patch = ("--- a/app/x.py\n+++ b/app/x.py\n@@ -1,3 +1,6 @@\n"
                     "+    for starter in question_starters_of_the_request:\n"
                     "+        if message.startswith(starter) and not seen_before:\n"
                     "+            plan.intent = 'family_event_memory_write'\n")
            spec = {"solution": {"01": {"patch": patch}}, "suites": {}}
            (repo / "app/x.py").parent.mkdir()
            (repo / "app/x.py").write_text("for starter in question_starters_of_the_request:\n    pass\n")
            self.assertEqual(probes.answer_lookup(bundle, spec)["reference_lines_found"], [])
            (repo / "notes.txt").write_text("for starter in question_starters_of_the_request:\n"
                                            "    if message.startswith(starter) and not seen_before:\n"
                                            "        plan.intent = 'family_event_memory_write'\n")
            hits = probes.answer_lookup(bundle, spec)["reference_lines_found"]
            self.assertEqual([h["file"] for h in hits], ["notes.txt"])


class AMutantMustChangeBehaviour(unittest.TestCase):
    def test_a_comment_only_patch_is_not_a_plausible_wrong_fix(self):
        pure = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n+# just a comment\n+\n"
        self.assertFalse(validate._changes_behaviour(pure))
        js = "--- a/x.js\n+++ b/x.js\n@@ -1,1 +1,1 @@\n-// old\n+// new\n"
        self.assertFalse(validate._changes_behaviour(js))
        real = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-    return a + b\n+    return a - b\n"
        self.assertTrue(validate._changes_behaviour(real))
        spec = synthetic_code.spec(two_steps=True)
        for m in spec["mutants"]:
            self.assertTrue(validate._changes_behaviour(m["patch"]), m["id"])



class ExtractionPathCollisions(unittest.TestCase):
    """2026-10-03: a trace recorded `.journal_source` as a file and as a directory."""

    def test_the_directory_wins_and_the_colliding_file_is_recorded(self):
        import tempfile
        import unittest.mock
        from pathlib import Path
        from types import SimpleNamespace
        from pipeline.author import extract
        def doc(path, content):
            return extract.FixtureDoc(path=path, trace_path=path, sha256="x", bytes=len(content), msg_index=1,
                                      source="snapshot", snapshot="s", blob=None, content=content) \
                if hasattr(extract, "FixtureDoc") else SimpleNamespace(path=path, trace_path=path, sha256="x",
                                                                       bytes=len(content), msg_index=1, source="snapshot",
                                                                       snapshot="s", blob=None, content=content)
        docs = [doc(".journal_source", "a file"), doc(".journal_source/entry.json", "{}"), doc("app.py", "x = 1\n")]
        extraction = SimpleNamespace(before=docs, after=[], source_root=None, session_id="s", seed_index=0,
                                     pivot=1, span=(0, 2), changed_paths=[], stats={}, moments=[],
                                     evidence_summary={}, evidence_index=[])
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(extract, "asdict", side_effect=lambda d: dict(vars(d))):
            extract.write(extraction, Path(tmp))
            repo = Path(tmp) / "environment/repo"
            self.assertTrue((repo / ".journal_source/entry.json").is_file())
            self.assertTrue((repo / "app.py").is_file())
        self.assertEqual(extraction.stats["path_collisions"], [".journal_source"])
