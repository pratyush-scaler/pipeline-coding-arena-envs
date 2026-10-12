"""Public GUI syntax and optional-dependency unit tests do not void coding trials."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from pipeline.author.build import fixture_manifest
from pipeline.author.grader_runtime import grade


GUI = '''import ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(2)
class App:
    def on_close(self):
        self.destroy()
        os._exit(0)
'''


class BenignSyntax(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root/'environment/repo'
        self.repo.mkdir(parents=True)
        self.main = self.repo/'main.py'
        self.main.write_text(GUI)
        self.manifest = fixture_manifest(self.root, [])

    def tearDown(self):
        self.temp.cleanup()

    def findings(self, text):
        self.main.write_text(text)
        return grade.introspection_problems(self.repo, self.manifest)

    def test_unrelated_edit_preserves_original_gui_statements(self):
        self.assertEqual(self.findings(GUI+'offline_only = False\n'), [])
        self.assertEqual(len(self.manifest['main.py']['benign_syntax']), 3)

    def test_original_statement_positions_can_move(self):
        self.assertEqual(self.findings('# فارسی\n\n'+GUI+'offline_only = False\n'), [])

    def test_utf8_ast_offsets_do_not_mask_following_code(self):
        self.assertTrue(self.findings(GUI.replace('import ctypes', 'عنوان = "فارسی"; import ctypes; sys._getframe()')))

    def test_unattested_import_is_refused(self):
        self.assertTrue(grade.introspection_problems(self.repo, {}))

    def test_new_ctypes_operation_is_refused(self):
        self.assertTrue(self.findings(GUI+'ctypes.pythonapi.Py_Exit(0)\n'))

    def test_benign_comment_does_not_void_an_honest_gui_edit(self):
        self.assertEqual(self.findings(GUI+'# Keep ctypes DPI setup intact.\noffline_only = False\n'), [])

    def test_inert_module_name_string_does_not_void_an_honest_edit(self):
        self.assertEqual(self.findings(GUI+'message = "ctypes DPI setup is preserved"\n'), [])

    def test_alias_of_original_ctypes_import_is_refused(self):
        self.assertTrue(self.findings(GUI+'native = ctypes\nnative.string_at(1)\n'))

    def test_changed_import_alias_is_refused(self):
        self.assertTrue(self.findings(GUI.replace('import ctypes', 'import ctypes as native')))

    def test_gui_exit_relocated_to_module_scope_is_refused(self):
        self.assertTrue(self.findings(GUI+'os._exit(0)\n'))

    def test_changed_gui_exit_argument_is_refused(self):
        self.assertTrue(self.findings(GUI.replace('os._exit(0)', 'os._exit(1)')))

    def test_changed_existing_frame_introspection_stays_refused(self):
        src='import sys\nframe = sys._getframe\n'
        self.main.write_text(src)
        self.manifest=fixture_manifest(self.root, [])
        self.assertTrue(self.findings(src+'changed = 1\n'))

    def test_hidden_paths_remain_refused(self):
        self.assertTrue(self.findings(GUI+'path = "/tests/suites"\n'))

    def mock_file(self, changes, target='sys.modules', method='test_optional', filename='tests/test_optional.py'):
        p=self.repo/filename;p.parent.mkdir(exist_ok=True)
        p.write_text('from unittest.mock import patch\n'
                     f'def {method}():\n'
                     f'    with patch.dict({target!r}, {changes}):\n'
                     '        pass\n')
        return grade.introspection_problems(self.repo, self.manifest)

    def test_literal_missing_dependency_unit_test_mock_is_allowed(self):
        self.assertEqual(self.mock_file('{"argostranslate": None}'), [])

    def test_private_framework_unit_test_mock_is_refused(self):
        for name in ('pytest', '_pytest', 'unittest', '__main__', 'package._internal'):
            with self.subTest(name=name):
                self.assertTrue(self.mock_file('{'+repr(name)+': None}'))

    def test_replacement_module_objects_stay_refused(self):
        self.assertTrue(self.mock_file('{"argostranslate": fake_module}'))

    def test_string_mock_in_application_code_stays_refused(self):
        self.assertTrue(self.mock_file('{"argostranslate": None}', filename='application.py'))

    def test_module_registry_expression_stays_refused(self):
        self.assertTrue(self.findings(GUI+'sys.modules["argostranslate"] = None\n'))

    def test_no_baseline_metadata_for_ordinary_source(self):
        self.main.write_text('import re\nanswer = 1\n')
        row=fixture_manifest(self.root, [])['main.py']
        self.assertEqual(set(row), {'sha256','bytes'})
        self.assertEqual(row['sha256'],hashlib.sha256(self.main.read_bytes()).hexdigest())


if __name__=='__main__':
    unittest.main()
