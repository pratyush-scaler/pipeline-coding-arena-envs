"""Guardrail misfires (user, 2026-10-21, after the external audit, class 6).

Three shipped tasks (a_f1_pptx-generator-script, a_f2_3d-scene-pipeline,
a_f1_vanilla-js-pwa) listed `conftest` in g_no_shadow's modules, which turns
off grade.py's exemption for an agent-added conftest.py: their own reference
scored 0 once a tests/conftest.py sat beside it, and every control passed.

Covers, without Docker: the `neutral_additions` control's plan and planting
(executed with `sh` over a real tree), its verdicts in `_evaluate`, the real
grader's shadow-guard over the planted tree with the shipped guardrails, a
full `controls.run` whose structural checks are graded by grade.py itself, the
grader.data gate and the check_draft advisory. That a mutant never breaks a
guardrail its trap does not declare is already the mutant control's exact
failure set; one test here shows it.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import policy
from pipeline.author import agent_designer, build, controls, sandbox
from pipeline.author.checks import load_shipped
from pipeline.author.grader_runtime import grade
from pipeline.gates import code as gates
from pipeline.gates.base import GateContext, Severity
from pipeline.tests import synthetic_code

#: The guardrails as shipped in Batch 3 (tests/checks.json), copied verbatim.
SHIPPED_CONFTEST_GUARDS = {
    "a_f1_pptx-generator-script": {"type": "shadow-guard",
                                   "modules": ["pptx", "markdown_it", "tourcheck", "handwork", "conftest"]},
    "a_f2_3d-scene-pipeline": {"type": "shadow-guard", "modules": ["json", "subprocess", "sitecustomize", "conftest"]},
    "a_f1_vanilla-js-pwa": {"type": "shadow-guard",
                            "modules": ["conftest", "playwright", "pytest", "http", "socketserver", "functools",
                                        "threading", "json", "re", "urllib"]},
}
SHIPPED_CLEAN_GUARDS = {
    "a_f2_csv-log-analysis": {"type": "shadow-guard", "modules": ["wcl_logs", "wcl_more", "wcl_edge", "json",
                                                                  "unicodedata", "subprocess", "re", "html", "pytest"]},
    "a_f2_python-trading-bot": {"type": "shadow-guard", "modules": ["json", "decimal", "urllib", "pytest", "server"]},
}


def _guard(cid: str, runner: dict) -> dict:
    return {"id": cid, "kind": "guardrail", "weight": 0, "band": "floor", "step": "01-fix", "runner": runner}


def _plant(tree: Path, planted: dict) -> None:
    subprocess.run(["sh", "-c", controls.neutral_shell(planted)], cwd=tree, check=True, capture_output=True)


def _with_conftest_shadow(spec: dict) -> dict:
    spec = copy.deepcopy(spec)
    for check in spec["checks"]:
        if check["runner"]["type"] == "shadow-guard":
            check["runner"]["modules"] = check["runner"]["modules"] + ["conftest"]
    return spec


class Planting(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def node_bundle(self, package: dict | None, dirs=()) -> Path:
        bundle = self.root / "node"
        repo = bundle / "environment/repo"
        repo.mkdir(parents=True)
        (repo / "app.js").write_text("module.exports = 1;\n")
        for name in dirs:
            (repo / name).mkdir()
        if package is not None:
            (repo / "web").mkdir()
            (repo / "web/package.json").write_text(json.dumps(package))
        return bundle

    def test_the_plan_runs_one_trial_with_the_reference_and_the_additions_on_every_step(self):
        bundle = synthetic_code.write_bundle(self.root / "b", two_steps=True)
        spec = json.loads((bundle / "authoring/spec.json").read_text())
        doc = load_shipped(bundle / "tests/checks.json")
        steps = [s["name"] for s in spec["steps"]]
        plan = controls._trial_plan(bundle, spec, doc, steps, {"name": "python"}, 1)
        variants = plan["neutral_additions"]
        self.assertEqual(list(variants), steps)
        for step in steps:
            shell = variants[step].shell
            reference = (bundle / "steps" / step / "solution/fix.patch").read_text().rstrip("\n")
            self.assertIn(reference, shell)
            for rel in ("tests/conftest.py", "tests/test_zz_notes.py", "NOTES.md", "__pycache__", "tests/__pycache__"):
                self.assertIn(rel, shell)
            # the planting runs only after the reference applied
            self.assertLess(shell.index(reference), shell.index("NOTES.md"))

    def test_planting_writes_real_files_and_never_overwrites_the_tree(self):
        bundle = synthetic_code.write_bundle(self.root / "b")
        planted = controls.neutral_additions(bundle, {"name": "python"})
        tree = self.root / "tree"
        shutil.copytree(bundle / "environment/repo", tree)
        (tree / "NOTES.md").write_text("the user's own notes\n")
        _plant(tree, planted)
        _plant(tree, planted)            # a later step plants again: idempotent
        self.assertEqual((tree / "NOTES.md").read_text(), "the user's own notes\n")
        self.assertEqual((tree / "tests/conftest.py").read_text(), controls.NEUTRAL_CONFTEST)
        test_file = (tree / "tests/test_zz_notes.py").read_text()
        compile(test_file, "test_zz_notes.py", "exec")
        compile(controls.NEUTRAL_CONFTEST, "conftest.py", "exec")
        self.assertTrue((tree / "__pycache__").is_dir() and not any((tree / "__pycache__").iterdir()))
        self.assertTrue((tree / "tests/__pycache__").is_dir())

    def test_the_extra_test_is_in_the_repository_runner(self):
        cases = [({"devDependencies": {"vitest": "2.1.8"}}, "test/zz_notes.test.mjs", "vitest"),
                 ({"scripts": {"test": "jest --ci"}}, "test/zz_notes.test.js", "jest"),
                 ({"devDependencies": {"mocha": "10"}}, "test/zz_notes.test.cjs", "mocha"),
                 ({"dependencies": {"ms": "2.1.3"}}, "test/zz_notes.test.mjs", "node:test"),
                 ({"devDependencies": {"eslint-plugin-jest": "28"}, "description": "not jest"},
                  "test/zz_notes.test.mjs", "node:test"),
                 (None, "test/zz_notes.test.mjs", "node:test")]
        for package, rel, runner in cases:
            with self.subTest(runner=runner, package=package):
                shutil.rmtree(self.root / "node", ignore_errors=True)
                planted = controls.neutral_additions(self.node_bundle(package), {"name": "node"})
                self.assertEqual(planted["test_runner"], runner)
                self.assertIn(rel, planted["files"])
                self.assertIn("test/conftest.py", planted["files"])
        shutil.rmtree(self.root / "node")
        planted = controls.neutral_additions(self.node_bundle(None, dirs=("tests",)), {"name": "browser"})
        self.assertIn("tests/zz_notes.test.mjs", planted["files"])
        shutil.rmtree(self.root / "node")
        planted = controls.neutral_additions(self.node_bundle(None), {"name": "go"})
        self.assertIsNone(planted["test_runner"])
        self.assertEqual(sorted(planted["files"]), ["NOTES.md", "tests/conftest.py"])

    def test_the_planted_python_test_passes_under_pytest_with_the_planted_conftest(self):
        bundle = synthetic_code.write_bundle(self.root / "b")
        tree = self.root / "tree"
        shutil.copytree(bundle / "environment/repo", tree)
        _plant(tree, controls.neutral_additions(bundle, {"name": "python"}))
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                              cwd=tree, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("1 passed", done.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_planted_node_test_passes_under_node_test(self):
        planted = controls.neutral_additions(self.node_bundle(None), {"name": "node"})
        tree = self.root / "node/environment/repo"
        _plant(tree, planted)
        done = subprocess.run(["node", "--test", "test/zz_notes.test.mjs"], cwd=tree, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("pass 1", done.stdout)

    def test_a_fixture_conftest_is_left_alone_and_another_is_added_beside_it(self):
        bundle = self.root / "py"
        (bundle / "environment/repo/tests").mkdir(parents=True)
        (bundle / "environment/repo/tests/conftest.py").write_text("# the fixture's own\n")
        planted = controls.neutral_additions(bundle, {"name": "python"})
        self.assertIn("tests/notes/conftest.py", planted["files"])
        self.assertNotIn("tests/conftest.py", planted["files"])


class RealGraderMisfire(unittest.TestCase):
    """grade.structural, the shipped runtime, over a tree the control's own shell planted."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bundle = Path(temp.name) / "b"
        repo = bundle / "environment/repo"
        repo.mkdir(parents=True)
        for rel in ("tourcheck.py", "tour_content.py", "handwork.py", "build_scene.py"):
            (repo / rel).write_text("VALUE = 1\n")
        self.manifest = {rel: {} for rel in ("tourcheck.py", "tour_content.py", "handwork.py", "build_scene.py")}
        self.tree = repo
        _plant(repo, controls.neutral_additions(bundle, {"name": "python"}))

    def test_each_shipped_conftest_guard_zeroes_the_planted_tree(self):
        for task, runner in SHIPPED_CONFTEST_GUARDS.items():
            with self.subTest(task=task):
                ok, why = grade.structural(_guard("g_no_shadow", runner), self.tree, self.manifest)
                self.assertFalse(ok, why)
                self.assertIn("tests/conftest.py", why)
                fixed = {**runner, "modules": [m for m in runner["modules"] if m != "conftest"]}
                ok, why = grade.structural(_guard("g_no_shadow", fixed), self.tree, self.manifest)
                self.assertTrue(ok, why)

    def test_guards_without_conftest_hold(self):
        for task, runner in SHIPPED_CLEAN_GUARDS.items():
            with self.subTest(task=task):
                ok, why = grade.structural(_guard("g_no_shadow", runner), self.tree, self.manifest)
                self.assertTrue(ok, why)


class Verdicts(unittest.TestCase):
    """`_evaluate` over synthetic records, as test_multi_turn judges the turn controls."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = synthetic_code.write_bundle(Path(temp.name) / "b", two_steps=True)
        self.spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        self.doc = load_shipped(self.bundle / "tests/checks.json")
        self.steps = [s["name"] for s in self.spec["steps"]]
        self.everything = {c["id"]: True for c in self.doc["checks"]}

    def record(self, flags):
        steps = {}
        for step in self.steps:
            checks = [c for c in self.doc["checks"] if c["step"] == step]
            objectives = [c for c in checks if c["kind"] == "objective"]
            guard_ok = all(flags.get(c["id"], True) for c in checks if c["kind"] == "guardrail")
            earned = sum(c["weight"] for c in objectives if flags.get(c["id"], True))
            reward = round(earned / sum(c["weight"] for c in objectives), 4) if guard_ok else 0.0
            steps[step] = {"valid": True, "reward": reward, "applied": {"stdout": ""},
                           "grade": {"checks": {c["id"]: {"passed": flags.get(c["id"], True)} for c in checks},
                                     "contract_errors": []}}
        return {"valid": True, "reward": round(sum(s["reward"] for s in steps.values()) / len(steps), 4), "steps": steps}

    def evaluate(self, neutral_flags, doc=None):
        doc = doc or self.doc
        nothing = {c["id"]: c["kind"] == "guardrail" for c in doc["checks"]}
        records = {"nop": self.record(nothing), "golden-1": self.record(self.everything),
                   "alternative": self.record(self.everything),
                   "neutral_additions": self.record(neutral_flags)}
        for mutant in self.spec["mutants"]:
            records[f"mutant:{mutant['id']}"] = self.record(dict(self.everything, **{c: False for c in mutant["fails"]}))
        for step in self.steps[1:]:
            records[f"prefix_only:{step}"] = self.record({c["id"]: c["kind"] == "guardrail" or c["step"] != step
                                                          for c in doc["checks"]})
            records[f"skip_prior:{step}"] = self.record(nothing)
        for label in ("empty", "destructive", "test_tamper", "early_exit", "report_forge"):
            records[label] = self.record(nothing)
        records["benign_test"] = self.record(self.everything)
        results, errors, advisories = controls._evaluate(self.bundle, self.spec, doc, self.steps,
                                                         {"name": "python"}, 1, records)
        return (results, [e for e in errors if e.startswith("neutral_additions")],
                [a for a in advisories if a.startswith("neutral_additions")])

    def conftest_doc(self):
        doc = copy.deepcopy(self.doc)
        for check in doc["checks"]:
            if check["runner"]["type"] == "shadow-guard":
                check["runner"]["modules"].append("conftest")
        return doc

    def test_the_reference_unmoved_by_harmless_files_holds(self):
        results, errors, advisories = self.evaluate(self.everything)
        self.assertEqual((errors, advisories), ([], []))
        entry = results["neutral_additions"]
        self.assertEqual(entry["test_runner"], "pytest")
        self.assertIn("tests/conftest.py", entry["planted"])
        self.assertIn("__pycache__/", entry["planted"])

    def test_a_shadow_guard_naming_conftest_is_a_control_error(self):
        _, errors, _ = self.evaluate(dict(self.everything, g_no_shadow=False), doc=self.conftest_doc())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("neutral_additions/01-fix", errors[0])
        self.assertIn("g_no_shadow", errors[0])
        self.assertIn("conftest", errors[0])

    def test_a_published_file_budget_is_an_advisory_not_a_misfire(self):
        _, errors, advisories = self.evaluate(dict(self.everything, g_no_new_files=False, g2_no_new_files=False))
        self.assertEqual(errors, [])
        self.assertEqual(len(advisories), 2, advisories)
        self.assertTrue(all("file budget" in a for a in advisories))

    def test_an_objective_moved_by_harmless_files_is_an_error(self):
        objective = next(c["id"] for c in self.doc["checks"] if c["kind"] == "objective")
        _, errors, _ = self.evaluate(dict(self.everything, **{objective: False}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("inventory", errors[0])

    def test_the_policy_switch_turns_the_error_into_an_advisory(self):
        with patch.object(policy, "NEUTRAL_ADDITIONS_MUST_MATCH_REFERENCE", False):
            _, errors, advisories = self.evaluate(dict(self.everything, g_api_name_kept=False))
        self.assertEqual(errors, [])
        self.assertTrue(any("g_api_name_kept" in a for a in advisories), advisories)

    def test_a_mutant_breaking_an_undeclared_guardrail_is_already_refused(self):
        mutant = self.spec["mutants"][0]
        mutant_step = next(c["step"] for c in self.doc["checks"] if c["id"] in mutant["fails"])
        guard = next(c["id"] for c in self.doc["checks"] if c["kind"] == "guardrail" and c["step"] == mutant_step
                     and c["runner"]["type"] == "shadow-guard")
        nothing = {c["id"]: c["kind"] == "guardrail" for c in self.doc["checks"]}
        records = {"nop": self.record(nothing), "golden-1": self.record(self.everything),
                   "alternative": self.record(self.everything), "neutral_additions": self.record(self.everything),
                   "benign_test": self.record(self.everything)}
        for m in self.spec["mutants"]:
            flags = dict(self.everything, **{c: False for c in m["fails"]})
            if m["id"] == mutant["id"]:
                flags[guard] = False
            records[f"mutant:{m['id']}"] = self.record(flags)
        for step in self.steps[1:]:
            records[f"prefix_only:{step}"] = self.record(nothing)
            records[f"skip_prior:{step}"] = self.record(nothing)
        for label in ("empty", "destructive", "test_tamper", "early_exit", "report_forge"):
            records[label] = self.record(nothing)
        _, errors, _ = controls._evaluate(self.bundle, self.spec, self.doc, self.steps, {"name": "python"}, 1, records)
        self.assertTrue(any(e.startswith(f"mutant {mutant['id']}: fails") and guard in e for e in errors), errors)
        self.assertTrue(any(f"mutant {mutant['id']}: fails checks its trap does not drive" in e for e in errors), errors)


class ExecutedThroughTheRealStructuralGrader(unittest.TestCase):
    """controls.run with every structural check graded by grade.py over a tree the variants' own shells
    built (reference patch applied, additions planted); test-runner checks follow the reference."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def bundle(self, spec: dict) -> Path:
        bundle = self.root / "bundle"
        for rel, text in synthetic_code.repo_files().items():
            (bundle / "environment/repo" / rel).parent.mkdir(parents=True, exist_ok=True)
            (bundle / "environment/repo" / rel).write_text(text)
        build.build(spec, bundle)
        return bundle

    def verify(self, bundle, variants, *, work, **kwargs):
        doc = load_shipped(bundle / "tests/checks.json")
        manifest = json.loads((bundle / "tests/fixture-manifest.json").read_text())
        label = work.name
        tree = self.root / "trees" / label
        shutil.copytree(bundle / "environment/repo", tree)
        scratch = self.root / "scratch" / label
        scratch.mkdir(parents=True)
        record = {"valid": True, "steps": {}}
        for step in sandbox._steps(bundle):
            variant = variants.get(step)
            correct = (label.startswith(("golden-", "mutant-"))
                       or label in ("alternative", "benign_test", "neutral_additions"))
            if label == "neutral_additions":
                shell = variant.shell.replace("/app/repo", str(tree)).replace("/tmp/", f"{scratch}/")
                done = subprocess.run(["sh", "-c", shell], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
            flags = {}
            for check in (c for c in doc["checks"] if c["step"] == step):
                if check["runner"]["type"] in grade.STRUCTURAL and label == "neutral_additions":
                    flags[check["id"]] = {"passed": grade.structural(check, tree, manifest)[0]}
                else:
                    flags[check["id"]] = {"passed": check["kind"] == "guardrail" or correct}
            guards = all(f["passed"] for cid, f in flags.items()
                         if next(c for c in doc["checks"] if c["id"] == cid)["kind"] == "guardrail")
            objectives = [c for c in doc["checks"] if c["step"] == step and c["kind"] == "objective"]
            earned = sum(c["weight"] for c in objectives if flags[c["id"]]["passed"])
            reward = round(earned / sum(c["weight"] for c in objectives), 4) if guards else 0.0
            record["steps"][step] = {"reward": reward, "grade": {"checks": flags, "contract_errors": []}}
        if label.startswith("mutant-"):
            mutant = next(m for m in json.loads((bundle / "authoring/spec.json").read_text())["mutants"]
                          if label == f"mutant-{m['id']}")
            for entry in record["steps"].values():
                for cid in mutant["fails"]:
                    if cid in entry["grade"]["checks"]:
                        entry["grade"]["checks"][cid]["passed"] = False
                checks = entry["grade"]["checks"]
                objectives = [c for c in doc["checks"] if c["id"] in checks and c["kind"] == "objective"]
                guards = all(f["passed"] for cid, f in checks.items() if cid not in {c["id"] for c in objectives})
                entry["reward"] = round(sum(c["weight"] for c in objectives if checks[c["id"]]["passed"])
                                        / sum(c["weight"] for c in objectives), 4) if guards else 0.0
        if label.startswith(("prefix_only", "skip_prior", "nop", "empty", "destructive", "test_tamper",
                             "early_exit", "report_forge")):
            for entry in record["steps"].values():
                for cid, flag in entry["grade"]["checks"].items():
                    flag["passed"] = next(c for c in doc["checks"] if c["id"] == cid)["kind"] == "guardrail"
                entry["reward"] = 0.0
        record["reward"] = round(sum(s["reward"] for s in record["steps"].values()) / len(record["steps"]), 4)
        return record

    def run_controls(self, bundle):
        with patch.object(sandbox, "build_image", side_effect=lambda b, role, **kw: role), \
                patch.object(sandbox, "verify", side_effect=self.verify):
            return controls.run(bundle, work=self.root / "work", reuse=False)

    def test_the_conftest_shadow_guard_is_caught_and_the_file_budget_is_reported(self):
        receipt = self.run_controls(self.bundle(_with_conftest_shadow(synthetic_code.spec())))
        self.assertEqual(len(receipt["errors"]), 1, receipt["errors"])
        neutral = [e for e in receipt["errors"] if e.startswith("neutral_additions/01-fix")]
        self.assertEqual(len(neutral), 1, receipt["errors"])
        self.assertIn("g_no_shadow", neutral[0])
        self.assertIn("conftest", neutral[0])
        self.assertTrue(any(a.startswith("neutral_additions") and "g_no_new_files" in a for a in receipt["advisories"]))
        self.assertFalse(receipt["ok"])

    def test_a_clean_guard_set_holds_with_only_the_published_budget_reported(self):
        receipt = self.run_controls(self.bundle(synthetic_code.spec()))
        self.assertTrue(receipt["ok"], receipt["errors"])
        flags = receipt["results"]["neutral_additions"]["flags"]["01-fix"]
        self.assertTrue(flags["g_no_shadow"])
        self.assertTrue(flags["g_cli_untouched"])       # the reference patch never touches cli.py
        self.assertFalse(flags["g_no_new_files"])        # the published zero-file budget saw the additions
        self.assertTrue(any(a.startswith("neutral_additions/01-fix") for a in receipt["advisories"]))

    def test_two_steps_plant_on_top_of_each_reference_in_one_tree(self):
        receipt = self.run_controls(self.bundle(synthetic_code.spec(two_steps=True)))
        self.assertTrue(receipt["ok"], receipt["errors"])
        flags = receipt["results"]["neutral_additions"]["flags"]
        self.assertTrue(flags["02-counts"]["g2_no_shadow"] and flags["02-counts"]["g2_cli_untouched"])
        self.assertEqual(sorted(a.split(":", 1)[0] for a in receipt["advisories"] if a.startswith("neutral_additions")),
                         ["neutral_additions/01-fix", "neutral_additions/02-counts"])
        report = (self.root / "trees/neutral_additions/ledger/report.py").read_text()
        self.assertIn("def monthly_counts", report)   # step 2's reference applied after step 1's additions


class GateAndAdvisory(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def bundle(self, spec: dict) -> Path:
        bundle = self.root / "bundle"
        for rel, text in synthetic_code.repo_files().items():
            (bundle / "environment/repo" / rel).parent.mkdir(parents=True, exist_ok=True)
            (bundle / "environment/repo" / rel).write_text(text)
        build.build(spec, bundle)
        return bundle

    def test_the_static_shape_matches_every_shipped_case_and_only_those(self):
        for task, runner in SHIPPED_CONFTEST_GUARDS.items():
            with self.subTest(task=task):
                found = controls.shadow_conftest_problems([_guard("g_no_shadow", runner)])
                self.assertEqual(len(found), 1)
                self.assertIn("g_no_shadow", found[0])
        for task, runner in SHIPPED_CLEAN_GUARDS.items():
            with self.subTest(task=task):
                self.assertEqual(controls.shadow_conftest_problems([_guard("g_no_shadow", runner)]), [])
        for name in ("tests/conftest", "conftest.py", "pkg/tests/conftest.py"):
            with self.subTest(name=name):
                self.assertTrue(controls.shadow_conftest_problems(
                    [_guard("g", {"type": "shadow-guard", "modules": [name]})]))
        for name in ("conftest_helpers", "myconftest", "tests.conftest_data"):
            with self.subTest(name=name):
                self.assertEqual(controls.shadow_conftest_problems(
                    [_guard("g", {"type": "shadow-guard", "modules": [name]})]), [])
        self.assertEqual(controls.shadow_conftest_problems(
            [_guard("g", {"type": "hash-guard", "paths": ["tests/conftest.py"]})]), [])

    def test_the_grader_data_gate_refuses_a_conftest_shadow_guard(self):
        clean = gates.grader_data_gate.run(GateContext(task_dir=self.bundle(synthetic_code.spec()),
                                                       repo_root=self.root))
        self.assertEqual([f for f in clean.findings if f.code == "shadow-conftest"], [], clean.findings)
        shutil.rmtree(self.root / "bundle")
        result = gates.grader_data_gate.run(GateContext(task_dir=self.bundle(_with_conftest_shadow(synthetic_code.spec())),
                                                        repo_root=self.root))
        refused = [f for f in result.findings if f.code == "shadow-conftest"]
        self.assertEqual(len(refused), 1, result.findings)
        self.assertIs(refused[0].severity, Severity.BLOCKER)
        self.assertIn("g_no_shadow", refused[0].message)
        self.assertEqual(refused[0].where, "tests/checks.json")

    def test_check_draft_advises_without_blocking(self):
        bundle = self.bundle(synthetic_code.spec())
        spec = _with_conftest_shadow(synthetic_code.spec())
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=bundle, moments=[])
        handlers = agent_designer._handlers(bundle, extraction, {"messages": []}, bundle / "draft.json")
        with patch.object(agent_designer.validate_mod, "validate", return_value=[]), \
             patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]):
            verdict = handlers["check_spec"](spec)
        self.assertTrue(verdict["clean"], verdict)
        hits = [a for a in verdict["advisories"] if a.startswith("ADVISORY guardrail misfire") and "g_no_shadow" in a]
        self.assertEqual(len(hits), 1, verdict["advisories"])


if __name__ == "__main__":
    unittest.main()
