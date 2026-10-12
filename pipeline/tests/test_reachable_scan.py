"""The interpreter-primitive scan covers the Python a hidden test process can execute -- no less.

Arena feedback 2026-10-08: an agent's own verification script (polya-hermes/verify_methods.py:
eval(, import test_methods) refused every check of python-verification-library, a helper importing
the agent's own test file refused c-019's Astra episodes, and an ordinary variable named
report_path refused cli-tool code. None of those files could run inside the test process. Every
case below that CAN reach it must still be refused.
"""
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline.author.grader_runtime import grade


class ReachableScan(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def scan(self, files, fixture=(), suites=None, runner=None):
        repo = self.root / "repo"
        shutil.rmtree(repo, True)
        repo.mkdir()
        sdir = self.root / "suites"
        shutil.rmtree(sdir, True)
        sdir.mkdir()
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        for rel, text in (suites or {}).items():
            (sdir / rel).write_text(text)
        manifest = {rel: {"sha256": hashlib.sha256((repo / rel).read_bytes()).hexdigest()} for rel in fixture}
        return grade.scan_problems(repo, manifest, [], runner or {"type": "pytest"}, sdir)

    def test_an_ordinary_report_path_variable_is_accepted(self):
        self.assertEqual(self.scan({"app/core.py": "def f():\n    report_path = 'x.txt'\n    return report_path\n"},
                                   suites={"t.py": "import app.core\n"}), [])

    def test_an_unreachable_helper_with_eval_and_a_test_import_is_accepted(self):
        self.assertEqual(self.scan({"app/core.py": "x = 1\n",
                                    "polya/verify_methods.py": "import test_methods\nprint(eval('1+1'))\n"},
                                   fixture=["app/core.py"], suites={"t.py": "from app import core\n"}), [])

    def test_an_added_helper_the_fixture_imports_is_still_refused(self):
        self.assertTrue(self.scan({"app/core.py": "from app import helper\n", "app/helper.py": "x = eval('1')\n"},
                                  fixture=["app/core.py"], suites={"t.py": "from app import core\n"}))

    def test_dynamic_loading_anywhere_reachable_restores_the_full_scan(self):
        self.assertTrue(self.scan({"app/core.py": "import importlib.util\nspec = importlib.util.spec_from_file_location(n, p)\n",
                                   "z.py": "eval('1')\n"},
                                  fixture=["app/core.py"], suites={"t.py": "from app import core\n"}))

    def test_a_fixture_evaluating_strings_keeps_the_reachability_rule(self):
        # python-verification-library's own library evaluates expressions; that is not loading a file
        self.assertEqual(self.scan({"lib/core.py": "def calc(e):\n    return eval(e, {})\n",
                                    "lib/verify_methods.py": "import test_methods\nx = __import__('os')\n"},
                                   fixture=["lib/core.py"], suites={"t.py": "from lib import core\n"}), [])

    def test_a_fixture_loading_by_name_or_file_contents_restores_the_full_scan(self):
        for src in ("import importlib\nm = importlib.import_module(name)\n", "exec(open(path).read())\n",
                    "import importlib.util\ns = importlib.util.find_spec(name)\n"):
            with self.subTest(src=src):
                self.assertTrue(self.scan({"lib/core.py": src, "z.py": "eval('1')\n"}, fixture=["lib/core.py"],
                                          suites={"t.py": "from lib import core\n"}))

    def test_a_constant_path_plugin_loader_is_an_import_not_a_route(self):
        loader = ("import importlib.util\nfrom pathlib import Path\n"
                  "PLUGIN_DIR = Path(__file__).resolve().parent / 'plugins' / 'polya-heuristic'\n"
                  "def load_plugin(name='polya_heuristic'):\n"
                  "    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / '__init__.py')\n"
                  "    plugin = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(plugin)\n")
        files = {"app/plugin_loader.py": loader, "app/plugins/polya-heuristic/__init__.py": "from . import tools\n",
                 "app/plugins/polya-heuristic/tools.py": "x = 1\n",
                 "app/verify_methods.py": "import test_methods\nprint(eval('1'))\n"}
        fixture = ["app/plugin_loader.py", "app/plugins/polya-heuristic/__init__.py", "app/plugins/polya-heuristic/tools.py"]
        suites = {"t.py": "spec = importlib.util.spec_from_file_location('l', repo / 'app/plugin_loader.py')\n"}
        self.assertEqual(self.scan(files, fixture=fixture, suites=suites), [])
        # the plugin package itself stays scanned: a changed plugin module with a frame walk is refused
        bad = dict(files, **{"app/plugins/polya-heuristic/tools.py": "import sys\nsys._getframe(1)\n"})
        self.assertTrue(self.scan(bad, fixture=fixture[:2], suites=suites))

    def test_a_data_driven_loader_restores_the_full_scan(self):
        loader = "import importlib.util\ndef load(p):\n    return importlib.util.spec_from_file_location('x', p)\n"
        self.assertTrue(self.scan({"app/loader.py": loader, "z.py": "eval('1')\n"}, fixture=["app/loader.py"],
                                  suites={"t.py": "from app import loader\n"}))

    def test_an_edited_fixture_script_nothing_runs_is_not_scanned(self):
        # the user's own oracle script, already holding eval(), edited by the agent; no hidden test imports it
        files = {"lib/core.py": "def f():\n    return 1\n",
                 "lib/verify_methods.py": "def check(t, ns):\n    return eval(compile(t, '<o>', 'eval'), ns)\n# edited\n"}
        self.assertEqual(self.scan(files, fixture=["lib/core.py"], suites={"t.py": "from lib import core\n"}), [])

    def test_an_edited_fixture_module_the_tests_import_is_scanned(self):
        files = {"lib/core.py": "import sys\nf = sys._getframe(1)\n"}
        self.assertTrue(self.scan(files, suites={"t.py": "from lib import core\n"}))

    def test_a_suite_loading_a_computed_module_restores_the_full_scan(self):
        self.assertTrue(self.scan({"z.py": "eval('1')\n"},
                                  suites={"t.py": "import importlib\nm = importlib.import_module(NAME)\n"}))

    def test_an_added_module_the_suites_import_is_scanned(self):
        self.assertTrue(self.scan({"newmod.py": "import sys\nsys._getframe(1)\n"}, suites={"t.py": "import newmod\n"}))

    def test_a_script_the_suites_name_is_scanned(self):
        self.assertTrue(self.scan({"tools/run.py": "import gc\ngc.get_objects()\n"},
                                  suites={"t.py": "subprocess.run(['python', 'tools/run.py'])\n"}))

    def test_a_module_the_runner_selects_is_scanned(self):
        self.assertTrue(self.scan({"test_reach.py": "import gc\ngc.get_objects()\n"}, suites={"t.py": ""},
                                  runner={"type": "unittest", "select": ["test_reach"]}))

    def test_a_command_runner_keeps_the_full_scan(self):
        self.assertTrue(self.scan({"z.py": "eval('1')\n"}, suites={"t.py": ""}, runner={"type": "command"}))

    def test_the_hidden_tree_is_refused_in_every_file(self):
        self.assertTrue(self.scan({"z.js": "require('/tests/checks.json')\n"}, suites={"t.py": ""}))

    def test_unreadable_suites_mean_the_full_scan(self):
        repo = self.root / "r"
        repo.mkdir()
        (repo / "z.py").write_text("eval('1')\n")
        self.assertTrue(grade.scan_problems(repo, {}, [], {"type": "pytest"}, self.root / "missing"))


if __name__ == "__main__":
    unittest.main()
