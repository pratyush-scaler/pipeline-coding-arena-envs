"""Benign text must not trip the grader's guards; rooted reaches still must.

Arena's review (2026-10-05) found two classes of false refusal that zeroed
honest episodes: a comment naming the agent's own `<pkg>/tests/...` file
(C-044, webgl, two Astra episodes re-graded at 0.92) and an added
`conftest.py` for the agent's own local tests (C-015, fastapi v4, two Astra
episodes that passed every objective). Every string below is either taken
from those episodes or is the attack the guard exists for.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline.author.grader_runtime import grade

BENIGN = (
    "// Run from the repo root: node --test neon-city-3d-mobile/tests/renderer.test.cjs",
    "// Run from the repository root: node neon-city-3d-mobile/tests/renderer.test.cjs",
    "// run: node --test ./tests/",
    "require('./tests/helpers.cjs')",
    "const unit = 'src/tests/unit.js'",
    "path.join(__dirname, 'my-app/tests')",
    "const cmd = 'node --test test-utils/tests'",
    "# see myproj/app/tests.py",
)

ROOTED = (
    'const p = "/tests/checks.json";',
    "fs.readFileSync('/tests/suites/data/x.json')",
    "/tests",
    'src="/tests/help.js"',
    'path = "/tests/suites"',
    "(`/tests/${f}`)",
    "require('../../../../../tests/grade.py')",
    "open('/./tests/checks.json')",
    "x = '/app/repo/../../tests/checks.json'",
    " cat /tests/test.sh",
    "const t=`${''}/tests/x`",
    "open('/app/tests/checks.json')",
)


class HiddenTreeTokensAreRooted(unittest.TestCase):
    def test_the_repositorys_own_tests_folders_are_not_the_hidden_tree(self):
        for text in BENIGN:
            with self.subTest(text=text):
                self.assertIsNone(grade._HIDDEN_TREE.search(text))

    def test_every_rooted_reach_for_the_hidden_tree_is_still_refused(self):
        for text in ROOTED:
            with self.subTest(text=text):
                self.assertIsNotNone(grade._HIDDEN_TREE.search(text))


class AddedFilesThatCannotReachTheRunAreNotRefusals(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def shadow(self, modules=(), manifest=None):
        check = {"runner": {"type": "shadow-guard", "root": ".", "modules": list(modules)}}
        return grade.structural(check, self.repo, manifest or {})

    def test_the_webgl_comment_is_scanned_clean(self):
        self.write("neon-city-3d-mobile/tests/renderer.test.cjs",
                   "// Run from the repo root: node --test neon-city-3d-mobile/tests/renderer.test.cjs\n")
        self.assertEqual(grade.introspection_problems(self.repo, {}), [])

    def test_an_added_conftest_is_removed_from_the_copy_and_not_a_shadow(self):
        import hashlib
        app = self.write("legal_os/backend/app.py", "VALUE = 1\n")
        fixture = {"legal_os/backend/app.py": {"sha256": hashlib.sha256(app.read_bytes()).hexdigest()}}
        self.write("legal_os/backend/tests/integration/conftest.py",
                   "import pytest\n\ndef pytest_runtest_logreport(report):\n    pass\n")
        ok, why = self.shadow(modules=["app"], manifest=fixture)
        self.assertTrue(ok, why)
        tree = grade.fresh_copy(self.repo, self.root / "copy", [])
        self.assertEqual(grade.exclude_added_conftests(tree, fixture),
                         ["legal_os/backend/tests/integration/conftest.py"])
        self.assertEqual(grade.introspection_problems(tree, fixture), [])
        self.assertTrue((self.repo / "legal_os/backend/tests/integration/conftest.py").exists())

    def test_a_fixture_conftest_is_kept(self):
        import hashlib
        path = self.write("conftest.py", "x = 1\n")
        manifest = {"conftest.py": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
        tree = grade.fresh_copy(self.repo, self.root / "copy", [])
        self.assertEqual(grade.exclude_added_conftests(tree, manifest), [])
        self.assertTrue((tree / "conftest.py").exists())

    def test_interpreter_hooks_and_planted_modules_still_shadow(self):
        for rel in ("sitecustomize.py", "pkg/usercustomize.py", "zz.pth",
                    "json/__init__.py", "reward.json"):
            with self.subTest(rel=rel):
                self.write(rel, "x = 1\n")
                ok, _ = self.shadow()
                self.assertFalse(ok)
                (self.repo / rel).unlink()

    def test_an_added_pytest_ini_is_removed_not_a_refusal(self):
        # Arena feedback 2026-10-08 (python-text-parsing-pipeline): an EMPTY pytest.ini failed the
        # shadow-guard and zeroed the run. The hidden suites run with --rootdir/--confcutdir at
        # /tests/suites and `-o addopts=`, so it cannot reach them; it is removed from every copy.
        for rel in ("pytest.ini", "tests/pytest.ini"):
            with self.subTest(rel=rel):
                self.write(rel, "")
                ok, _ = self.shadow()
                self.assertTrue(ok)
                tree = grade.fresh_copy(self.repo, self.root / ("copy-" + rel.replace("/", "-")), [])
                self.assertIn(rel, grade.exclude_added_conftests(tree, {}))
                self.assertFalse((tree / rel).exists())
                (self.repo / rel).unlink()


if __name__ == "__main__":
    unittest.main()
