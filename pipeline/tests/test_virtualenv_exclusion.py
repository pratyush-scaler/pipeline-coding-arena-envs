"""An agent's own virtualenv neither voids an honest submission nor forges one.

The name-based scanner skip (`vendor`, `.venv`, ...) let a planted
`vendor/forge.py` forge a pass (reviewer, 2026-10-01); scanning every directory
instead refuses an honest submission whose agent ran `python -m venv`, because
pip and pytest sources use the interpreter primitives the scanner refuses.
Agent-created environments are removed from the disposable grading copy, so
their code can neither be scanned nor imported.
"""
import tempfile
import unittest
from pathlib import Path

from pipeline.author.grader_runtime import grade


def _venv(root: Path, rel: str) -> Path:
    env = root / rel
    (env / "lib/python3.12/site-packages/pip").mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (env / "lib/python3.12/site-packages/pip/__init__.py").write_text(
        "import sys\nmods = sys.modules\n")
    return env


class AgentVirtualenvsAreRemoved(unittest.TestCase):

    def test_an_agent_venv_would_refuse_an_honest_tree_if_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _venv(root, ".venv")
            self.assertTrue(grade.introspection_problems(root, {}))

    def test_agent_venvs_are_removed_and_the_tree_then_scans_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _venv(root, ".venv")
            _venv(root, "tools/env")
            (root / "app.py").write_text("print('ok')\n")
            removed = grade.exclude_added_virtualenvs(root, {"app.py": {}})
            self.assertEqual(sorted(removed), [".venv", "tools/env"])
            self.assertFalse((root / ".venv").exists())
            self.assertEqual(grade.introspection_problems(root, {}), [])

    def test_a_planted_module_outside_a_venv_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _venv(root, ".venv")
            forge = root / "project/vendor/forge.py"
            forge.parent.mkdir(parents=True)
            forge.write_text("import sys\nsys.modules['pytest']\n")
            grade.exclude_added_virtualenvs(root, {})
            self.assertEqual(grade.introspection_problems(root, {}),
                             ["project/vendor/forge.py: sys.modules"])

    def test_a_directory_holding_fixture_files_is_never_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _venv(root, "app")
            (root / "app/main.py").write_text("print('fixture')\n")
            self.assertEqual(grade.exclude_added_virtualenvs(root, {"app/main.py": {}}), [])
            self.assertTrue((root / "app/pyvenv.cfg").exists())

    def test_the_tree_root_is_never_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
            (root / "app.py").write_text("print('ok')\n")
            self.assertEqual(grade.exclude_added_virtualenvs(root, {}), [])
            self.assertTrue((root / "app.py").exists())


if __name__ == "__main__":
    unittest.main()
