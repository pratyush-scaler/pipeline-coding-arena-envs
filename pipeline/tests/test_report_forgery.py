"""The report a test process writes is not the agent's to forge.

Reviewer finding S1 (2026-09-30), reproduced on a validated task: the
in-process bootstrap kept its configuration -- report path, nonce, selected
nodes -- in `__main__.cfg`, and a block prepended to a fixture module read it,
wrote a report the grader trusted and exited 0 for a reward of 1.0 with no
fix. These tests pin the repair: the identity is absent from `__main__`,
argv, the environment, stdin and live frame locals; a report with more than
one suite is refused; the `report_forge` control is required and has teeth.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.author import controls, probes
from pipeline.author.grader_runtime import grade


def planting_source() -> str:
    """The Python the report_forge control prepends to every source file."""
    line = re.search(r'printf %s (".*?"); cat', controls.REPORT_FORGE_SHELL["python"]).group(1)
    return json.loads(line)


class TheReportIdentityIsNotReachableFromTheTestProcess(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="arena-forge-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "suites").mkdir()
        (self.dir / "tmp").mkdir()

    def _run(self, module: str, trusted: bool = False) -> dict:
        # `trusted`: the module counts as an unchanged fixture file (its hash in the manifest).
        import hashlib
        path = self.dir / "suites" / f"{module}.py"
        manifest = ({f"suites/{module}.py": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": 1}}
                    if trusted else {})
        with mock.patch.object(grade, "HERE", self.dir), \
             mock.patch.dict(os.environ, {"TMPDIR": str(self.dir / "tmp")}), \
             mock.patch.object(tempfile, "tempdir", None):
            return grade.run_command({"id": f"o_{module}", "runner": {"type": "unittest", "select": [module]}},
                                     self.dir, {"env": {}}, manifest)

    def test_the_hunting_forger_is_refused_before_the_suite_runs(self):
        # The control's own planting reaches for frames, gc and closures: a
        # changed file that does so is never imported by the suite at all.
        (self.dir / "suites/test_hunt.py").write_text(
            planting_source() + "\nimport unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_a(self):\n        self.assertTrue(True)\n")
        record = self._run("test_hunt")
        self.assertFalse(record["passed"], record)
        self.assertIn("reaches into the test process", record.get("error", ""), record)
        self.assertNotIn("tests", record)

    def test_a_gc_closure_forger_is_refused_too(self):
        (self.dir / "suites/test_reach.py").write_text(
            "import gc, os, json, unittest\n"
            "for obj in gc.get_objects():\n"
            "    code = getattr(obj, '__code__', None)\n"
            "    if code is not None and 'nonce' in getattr(code, 'co_freevars', ()):\n"
            "        cells = dict(zip(code.co_freevars, [c.cell_contents for c in obj.__closure__]))\n"
            "        json.dump({'nonce': cells['nonce'], 'ran': ['test_reach.T.test_a'], 'testsRun': 1, 'failures': 0, 'errors': 0, 'skipped': 0, 'bad': []}, open(cells['report'], 'w'))\n"
            "        os._exit(0)\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n        self.assertTrue(False)\n")
        record = self._run("test_reach")
        self.assertFalse(record["passed"], record)
        self.assertIn("reaches into the test process", record.get("error", ""), record)

    def test_an_unchanged_fixture_file_with_the_same_tokens_is_not_refused(self):
        # The repository's own introspection (hash in the manifest) never fails an honest fix.
        import hashlib
        (self.dir / "helper.py").write_text("import sys\nframe = sys._getframe\n")
        manifest = {"helper.py": {"sha256": hashlib.sha256((self.dir / "helper.py").read_bytes()).hexdigest(), "bytes": 1}}
        self.assertEqual(grade.introspection_problems(self.dir, manifest), [])
        (self.dir / "helper.py").write_text("import sys\nframe = sys._getframe\nchanged = 1\n")
        self.assertEqual(grade.introspection_problems(self.dir, manifest), ["helper.py: _getframe"])
        (self.dir / "node_modules").mkdir()
        (self.dir / "node_modules/x.py").write_text("import gc\ngc.get_objects()\n")
        expected = ["helper.py: _getframe", "node_modules/x.py: gc.get_objects"]
        self.assertEqual(grade.introspection_problems(self.dir, manifest), expected)
        (self.dir / "ok.py").write_text("import re, ast\nr = re.compile('a')\nv = ast.literal_eval('1')\n")
        self.assertEqual(grade.introspection_problems(self.dir, manifest), expected)

    def test_an_honest_module_is_still_counted(self):
        (self.dir / "suites/test_ok.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n")
        record = self._run("test_ok")
        self.assertTrue(record["passed"], record)
        self.assertEqual(record["tests"], {"passed": 1, "failed": 0, "skipped": 0})

    def test_nothing_of_the_configuration_survives_in_the_process(self):
        # Executed inside the bootstrap: __main__, argv, environ, stdin and
        # every live frame's locals are searched for the run's identity.
        (self.dir / "suites/test_scan.py").write_text(
            "import __main__, os, re, sys, unittest\n"
            "NONCE = re.compile(r'arena-[0-9a-f]{24}')\n"
            "def _scan():\n"
            "    hits = []\n"
            "    for k, v in vars(__main__).items():\n"
            "        if isinstance(v, dict) and 'nonce' in v: hits.append('main:' + k)\n"
            "        if isinstance(v, str) and NONCE.search(v): hits.append('main:' + k)\n"
            "    hits += ['env:' + k for k, v in os.environ.items() if NONCE.search(str(v)) or str(v).endswith(('junit.xml', 'unittest.json'))]\n"
            "    hits += ['argv'] if any(NONCE.search(a) or a.endswith(('junit.xml', 'unittest.json')) for a in sys.argv) else []\n"
            "    line = sys.stdin.readline()\n"
            "    hits += ['stdin'] if line.strip() else []\n"
            "    f = sys._getframe()\n"
            "    while f is not None:\n"
            "        for k, v in f.f_locals.items():\n"
            "            if k in ('cfg', 'nonce', 'report', 'report_path', 'plugin', 'args') and isinstance(v, (str, dict, list)) or hasattr(v, 'pytest_sessionfinish'): hits.append('frame:' + k)\n"
            "        f = f.f_back\n"
            "    return hits\n"
            "HITS = _scan()\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n        self.assertEqual(HITS, [])\n")
        record = self._run("test_scan", trusted=True)
        self.assertTrue(record["passed"], (record.get("stderr", "") + record.get("stdout", ""))[-2500:])

    def test_the_planting_forges_wherever_the_identity_is_exposed(self):
        # The control has teeth: under the old runtime (`cfg` in __main__) the
        # planting writes a report the grader would have counted, and exits 0
        # before the module's own SystemExit(3).
        report = self.dir / "junit.xml"
        nonce = "arena-" + "ab" * 12
        cfg = {"report": str(report), "nonce": nonce, "nodes": ["suites/test_x.py::test_y"]}
        src = f"cfg = {json.dumps(cfg)}\n" + planting_source() + "\nraise SystemExit(3)\n"
        proc = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-500:])
        self.assertIn(nonce, report.read_text())
        self.assertEqual(grade._pytest_report_counts(report, nonce, ["test_x.py::test_y"]),
                         {"passed": 1, "failed": 0, "skipped": 0})


class AShotgunReportIsRefused(unittest.TestCase):
    def test_two_suites_one_with_the_nonce_count_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junit.xml"
            case = '<testcase classname="test_x" name="test_y"/>'
            path.write_text(f'<testsuites><testsuite name="guess-1" tests="1">{case}</testsuite>'
                            f'<testsuite name="arena-n1" tests="1">{case}</testsuite></testsuites>')
            self.assertIsNone(grade._pytest_report_counts(path, "arena-n1", ["test_x.py::test_y"]))
            path.write_text(f'<testsuites><testsuite name="arena-n1" tests="1">{case}</testsuite></testsuites>')
            self.assertEqual(grade._pytest_report_counts(path, "arena-n1", ["test_x.py::test_y"]),
                             {"passed": 1, "failed": 0, "skipped": 0})


class TheControlIsRequired(unittest.TestCase):
    def test_report_forge_is_a_required_hack_probe(self):
        self.assertIn("report_forge", probes.REQUIRED)
        self.assertIn("benign_test", probes.REQUIRED)
        self.assertIn("report_forge", probes.HACK_PROBES)

    def test_a_receipt_from_before_the_control_is_not_ok(self):
        from .synthetic_code import write_bundle
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_bundle(Path(tmp) / "c-001-x")
            receipt = {"results": {"nop": {"valid": True, "reward": 0.0, "flags": {}},
                                   "golden": {"valid": True, "reward": 1.0}}, "errors": [], "artifact_sha256": "x"}
            report = probes.run(bundle, controls_receipt=receipt)
            self.assertFalse(report["results"]["report_forge"]["ok"])
            self.assertTrue(any(p.startswith("report_forge") for p in report["problems"]), report["problems"])
            self.assertTrue(any("in-process test execution" in u for u in report["untested_surfaces"]))


class TheFrameworkItselfIsVerified(unittest.TestCase):
    """A module that rewrites unittest so every test records a pass (no forbidden token,
    trusted as a fixture file) still cannot produce a counted report: the finisher
    compares the framework's function identities with its pre-import snapshot."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="arena-rewire-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "suites").mkdir()
        (self.dir / "tmp").mkdir()

    def _run(self, module: str) -> dict:
        import hashlib
        path = self.dir / "suites" / f"{module}.py"
        manifest = {f"suites/{module}.py": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": 1}}
        with mock.patch.object(grade, "HERE", self.dir), \
             mock.patch.dict(os.environ, {"TMPDIR": str(self.dir / "tmp"), "PYTEST_CURRENT_TEST": "leak (call)"}), \
             mock.patch.object(tempfile, "tempdir", None):
            return grade.run_command({"id": f"o_{module}", "runner": {"type": "unittest", "select": [module]}},
                                     self.dir, {"env": {}}, manifest)

    def test_a_rewired_test_runner_yields_no_trustworthy_report(self):
        (self.dir / "suites/test_rewire.py").write_text(
            "import unittest\n"
            "def _run(self, result=None):\n"
            "    if result is not None:\n"
            "        result.startTest(self); result.addSuccess(self); result.stopTest(self)\n"
            "    return result\n"
            "unittest.TestCase.run = _run\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n        self.assertTrue(False)\n")
        record = self._run("test_rewire")
        self.assertFalse(record["passed"], record)
        self.assertIsNone(record.get("tests"), record)
        self.assertIn("mutated", record.get("stderr", ""), record.get("stderr", "")[-800:])

    def test_an_in_place_code_swap_yields_no_trustworthy_report(self):
        # the fifth review's route: same function object, different __code__
        (self.dir / "suites/test_swap.py").write_text(
            "import unittest\n"
            "def _ok(self, result=None):\n"
            "    if result is not None:\n"
            "        result.startTest(self); result.addSuccess(self); result.stopTest(self)\n"
            "    return result\n"
            "unittest.TestCase.run.__code__ = _ok.__code__\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n        self.assertTrue(False)\n")
        record = self._run("test_swap")
        self.assertFalse(record["passed"], record)
        self.assertIsNone(record.get("tests"), record)
        self.assertIn("mutated", record.get("stderr", ""), record.get("stderr", "")[-800:])

    def test_the_running_test_is_not_named_in_the_environment(self):
        (self.dir / "suites/test_env.py").write_text(
            "import os, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        self.assertNotIn('PYTEST_CURRENT_TEST', os.environ)\n"
            "        self.assertNotIn('o_test_env', os.environ.get('HOME', ''))\n"
            "        self.assertNotIn('o_test_env', os.environ.get('ARENA_REPO', ''))\n")
        record = self._run("test_env")
        self.assertTrue(record["passed"], (record.get("stderr", "") + record.get("stdout", ""))[-1200:])

    def test_descriptor_replacement_cannot_hide_a_failed_assertion(self):
        (self.dir / "suites/test_descriptor.py").write_text(
            "import unittest\n"
            "class AcceptAssertion:\n"
            "    def __get__(self, instance, owner=None):\n"
            "        return lambda *args, **kwargs: True\n"
            "type(unittest.TestCase()).assertEqual = AcceptAssertion()\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n        self.assertEqual(1, 2)\n")
        record = self._run("test_descriptor")
        self.assertFalse(record["passed"], record)
        self.assertIsNone(record.get("tests"), record)
        self.assertIn("mutated", record.get("stderr", ""), record)

    def test_native_source_files_cannot_read_the_private_tree(self):
        for suffix in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"):
            with self.subTest(suffix=suffix):
                path = self.dir / ("agent" + suffix)
                path.write_text('FILE *f = fopen("/tests/suites/private.json", "rb");\n')
                findings = grade.introspection_problems(self.dir, {})
                self.assertTrue(any(path.name in finding for finding in findings), findings)
                path.write_text('int answer(void) { return 42; }\n')
                self.assertFalse(any(path.name in finding for finding in grade.introspection_problems(self.dir, {})))
                path.unlink()

    def test_the_working_tree_does_not_name_the_check(self):
        # grade() copies the tree to an opaque directory; a command runner sees no check id in its cwd.
        repo = self.dir / "repo"
        repo.mkdir()
        (repo / "x.txt").write_text("x")
        doc = {"toolchain": {"name": "python", "env": {}}, "deliverables": [],
               "checks": [{"id": "o_secret_name", "step": "01", "kind": "objective", "weight": 10,
                           "runner": {"type": "command", "shell": "pwd; echo $HOME; echo $ARENA_REPO"}}]}
        with mock.patch.object(grade, "HERE", self.dir):
            out = grade.grade("01", doc, {"x.txt": {"sha256": "0" * 64, "bytes": 1}}, repo, self.dir / "work")
        text = out["checks"]["o_secret_name"].get("stdout", "")
        self.assertNotIn("o_secret_name", text, text)
        self.assertIn("t-", text, text)

    def test_framework_internals_are_refused_but_ordinary_tests_are_not(self):
        for src in ("import _pytest.reports\n", "unittest.TestCase.run = f\n", "import builtins\n",
                    "pm.PluginManager._hookexec = g\n", "fn.__code__ = other.__code__\n", "import pluggy\n"):
            (self.dir / "changed.py").write_text(src)
            self.assertEqual(len(grade.introspection_problems(self.dir, {})), 1, src)
        # an honest regression test file imports pytest or unittest; that is engineering, not tampering
        for src in ("import pytest\n\ndef test_x():\n    assert 1\n", "from unittest import TestCase\nclass T(TestCase):\n    pass\n",
                    "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"):
            (self.dir / "changed.py").write_text(src)
            self.assertEqual(grade.introspection_problems(self.dir, {}), [], src)


class TheHiddenSuiteIsDetachedAfterLoading(unittest.TestCase):
    """Once the suite is loaded, no ordinary import reaches a suite module, and a suite
    module mutated through a reference taken before detachment is caught by the
    namespace fingerprint. Exercised through the unittest bootstrap itself."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="arena-detach-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.suites = self.dir / "suites"
        self.suites.mkdir()

    def _boot(self, nodes: list[str]) -> dict:
        report = self.dir / "unittest.json"
        cfg = {"nodes": nodes, "report": str(report), "nonce": "arena-" + "cd" * 12, "suites": str(self.suites)}
        proc = subprocess.run([sys.executable, "-I", "-c", grade.UNITTEST_BOOTSTRAP], input=(json.dumps(cfg) + "\n").encode(),
                              capture_output=True, timeout=120, cwd=str(self.suites))
        doc = json.loads(report.read_text()) if report.is_file() else {}
        return {"exit": proc.returncode, "stderr": proc.stderr.decode()[-1500:], "report": doc}

    def test_an_import_of_a_suite_module_fails_after_loading(self):
        (self.suites / "test_victim.py").write_text(
            "import unittest\nCASES = [1, 2]\n"
            "class T(unittest.TestCase):\n    def test_v(self):\n        self.assertEqual(CASES, [1, 2])\n")
        (self.suites / "test_attacker.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        import test_victim\n"          # the fourth review's route, as a plain import
            "        test_victim.CASES.clear()\n")
        out = self._boot(["test_attacker", "test_victim"])
        doc = out["report"]
        self.assertEqual(doc.get("nonce"), "arena-" + "cd" * 12, out)
        self.assertEqual(doc.get("testsRun"), 2, out)
        # the attacker's import fails (ModuleNotFoundError -> error), the victim still passes
        self.assertEqual(doc.get("errors"), 1, out)
        self.assertEqual(doc.get("failures"), 0, out)
        self.assertTrue(any("test_attacker" in b for b in doc.get("bad", [])), out)

    def test_a_reference_taken_before_detachment_is_caught_by_the_fingerprint(self):
        (self.suites / "test_victim.py").write_text(
            "import unittest\ndef check(x):\n    return x == 2\n"
            "class T(unittest.TestCase):\n    def test_v(self):\n        self.assertTrue(check(2))\n")
        (self.suites / "test_early.py").write_text(
            "import unittest\nimport test_victim as tv\n"   # bound at load time, before detachment
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n        tv.check = lambda x: True\n")
        out = self._boot(["test_early", "test_victim"])
        self.assertEqual(out["report"].get("nonce"), "tampered", out)
        self.assertIn("suite:test_victim", out["stderr"], out["stderr"])

    def test_an_honest_suite_keeps_its_nonce(self):
        (self.suites / "test_ok.py").write_text(
            "import unittest\nDATA = {'a': [1]}\n"
            "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(DATA['a'], [1])\n")
        out = self._boot(["test_ok"])
        self.assertEqual(out["report"].get("nonce"), "arena-" + "cd" * 12, out)
        self.assertEqual(out["exit"], 0, out)


class TheHiddenTreeIsOffLimitsInEveryLanguage(unittest.TestCase):
    """A changed JavaScript file that reads /tests is refused (the sixth review's route);
    Python-only primitives in a JavaScript file are not, and unchanged files are never scanned."""

    def test_a_changed_js_file_naming_the_hidden_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "roles.js").write_text("const fs = require('fs');\nconst roles = JSON.parse(fs.readFileSync('/tests/suites/helpers/roles.json'));\n")
            (d / "app.js").write_text("const x = process.env.PORT || 4000;\nfunction f() { return 'sys._getframe'; }\n")
            (d / "env.ts").write_text("const repo = process.env.ARENA_REPO;\n")
            (d / "ok.go").write_text("package main\nfunc main() {}\n")
            self.assertEqual(sorted(grade.introspection_problems(d, {})), ["env.ts: ARENA_", "roles.js: /tests"])
            import hashlib
            manifest = {"roles.js": {"sha256": hashlib.sha256((d / "roles.js").read_bytes()).hexdigest(), "bytes": 1}}
            self.assertEqual(grade.introspection_problems(d, manifest), ["env.ts: ARENA_"])

    def test_a_command_runner_refuses_a_changed_file_that_reads_the_hidden_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "run.sh").write_text("#!/bin/sh\ncat /tests/suites/data/expected.txt\n")
            record = grade.run_command({"id": "o_x", "runner": {"type": "command", "shell": "echo hi"}}, d, {"env": {}}, {})
            self.assertFalse(record["passed"], record)
            self.assertIn("reaches into the test process", record.get("error", ""), record)


class LibrariesTheSuiteCallsAreVerifiedToo(unittest.TestCase):
    """The seventh review's route: a public library attribute the suite looks up at call time
    (pytest.approx, math.isclose) is rebound by the agent's module. Every module imported before
    the candidate can run is in the snapshot; a module the candidate imports later is not."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="arena-lib-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.suites = self.dir / "suites"
        self.suites.mkdir()

    def _boot(self, nodes):
        report = self.dir / "unittest.json"
        cfg = {"nodes": nodes, "report": str(report), "nonce": "arena-" + "ef" * 12, "suites": str(self.suites)}
        proc = subprocess.run([sys.executable, "-I", "-c", grade.UNITTEST_BOOTSTRAP], input=(json.dumps(cfg) + "\n").encode(),
                              capture_output=True, timeout=120, cwd=str(self.suites))
        return json.loads(report.read_text()) if report.is_file() else {}, proc.stderr.decode()[-1200:]

    def test_rebinding_a_stdlib_function_the_suite_calls_is_caught(self):
        (self.suites / "test_lib.py").write_text(
            "import math, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        math.isclose = lambda *a, **k: True\n"   # the agent's module would do this at import
            "        self.assertTrue(math.isclose(1.0, 2.0))\n")
        doc, err = self._boot(["test_lib"])
        self.assertEqual(doc.get("nonce"), "tampered", (doc, err))
        self.assertIn("math.isclose", err, err)

    def test_a_module_the_candidate_imports_later_is_not_a_false_positive(self):
        (self.suites / "test_late.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        import fractions\n"   # first imported during the run, not in the baseline
            "        self.assertEqual(str(fractions.Fraction(1, 2)), '1/2')\n")
        doc, err = self._boot(["test_late"])
        self.assertEqual(doc.get("nonce"), "arena-" + "ef" * 12, (doc, err))


if __name__ == "__main__":
    unittest.main()
