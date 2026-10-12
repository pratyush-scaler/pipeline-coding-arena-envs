import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from pipeline.author.grader_runtime import grade


class LocalTestsCannotEnterThePrivateRunner(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / 'repo'
        self.repo.mkdir()

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_unused_helper_imports_do_not_refuse_production_code(self):
        self.write('viewer.py', 'def answer():\n    return 42\n')
        self.write('tests/test_geometry.py', 'import os\nos._exit(0)\n')
        self.write('tests/test_controls.py', 'from test_geometry import viewer\n')
        tree = grade.fresh_copy(self.repo, self.root / 'copy', [])
        excluded = grade.exclude_added_python_tests(tree, {})
        self.assertEqual(excluded, ['tests/test_controls.py', 'tests/test_geometry.py'])
        self.assertEqual(grade.introspection_problems(tree, {}), [])
        self.assertTrue((self.repo / 'tests/test_geometry.py').exists())
        self.assertFalse((tree / 'tests/test_geometry.py').exists())

    def test_existing_changed_tests_keep_the_source_protection(self):
        path = self.write('test_old.py', 'old = True\n')
        manifest = {'test_old.py': {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
        path.write_text('import gc\ngc.get_objects()\n')
        tree = grade.fresh_copy(self.repo, self.root / 'copy', [])
        self.assertEqual(grade.exclude_added_python_tests(tree, manifest), [])
        self.assertEqual(grade.introspection_problems(tree, manifest), ['test_old.py: gc.get_objects'])

    def test_a_production_import_cannot_reach_an_excluded_test(self):
        self.write('viewer.py', 'import test_helper\n')
        self.write('test_helper.py', 'import gc\ngc.get_objects()\n')
        tree = grade.fresh_copy(self.repo, self.root / 'copy', [])
        grade.exclude_added_python_tests(tree, {})
        self.assertFalse((tree / 'test_helper.py').exists())
        self.assertEqual(grade.introspection_problems(tree, {}), ['viewer.py: import test_helper'])

    def test_production_introspection_and_hidden_reads_still_fail(self):
        self.write('viewer.py', 'import sys\nsys._getframe()\n')
        self.write('export.py', "path = '/tests/suites/secret.json'\n")
        self.write('test_unused.py', 'from test_other import data\n')
        tree = grade.fresh_copy(self.repo, self.root / 'copy', [])
        grade.exclude_added_python_tests(tree, {})
        self.assertEqual(grade.introspection_problems(tree, {}),
                         ['export.py: /tests', 'viewer.py: _getframe'])

    def test_the_old_callable_instance_approx_bypass_is_rejected_by_execution(self):
        self.write('viewer.py',
            'import pytest\n'
            'class _AcceptComparison:\n'
            '    def __call__(self, *args, **kwargs): return self\n'
            '    def __eq__(self, other): return True\n'
            'pytest.approx = _AcceptComparison()\n'
            'def value(): return 1.0\n')
        private = self.root / 'private'
        suites = private / 'suites'
        suites.mkdir(parents=True)
        (suites / 'test_comparison.py').write_text(
            'import importlib.util, os, pytest\n'
            'from pathlib import Path\n'
            'def test_value():\n'
            "    spec = importlib.util.spec_from_file_location('viewer', Path(os.environ['ARENA_REPO']) / 'viewer.py')\n"
            '    mod = importlib.util.module_from_spec(spec)\n'
            '    spec.loader.exec_module(mod)\n'
            '    assert mod.value() == pytest.approx(2.0)\n')
        with mock.patch.object(grade, 'HERE', private):
            record = grade.run_command({'id': 'o_comparison', 'runner': {
                'type': 'pytest', 'select': ['test_comparison.py::test_value']}}, self.repo,
                {'env': {'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']}}, {})
        self.assertFalse(record['passed'], record)
        self.assertIn('pytest.approx', record['stderr'], record)
        self.assertIn('framework was mutated', record['stderr'], record)
