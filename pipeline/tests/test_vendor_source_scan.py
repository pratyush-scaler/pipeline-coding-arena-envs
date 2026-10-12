"""A dependency-like name alone never grants changed code a scanner exemption."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline.author.grader_runtime import grade


class VendorSourceScan(unittest.TestCase):
    def test_new_executable_sources_under_every_former_skip_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            names=['vendor','node_modules','.venv','venv','.git','__pycache__']
            for name in names:
                path=root/'app'/name/'pkg/helper.py';path.parent.mkdir(parents=True)
                path.write_text('import gc\ngc.get_objects()\n')
            self.assertEqual(grade.introspection_problems(root,{}),
                sorted('app/'+name+'/pkg/helper.py: gc.get_objects' for name in names))

    def test_declared_vendored_name_and_matching_basename_are_not_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for relative in ['deps/lib/helper.py','app/lib/helper.py','other/vendor/helper.js']:
                path=root/relative;path.parent.mkdir(parents=True)
                path.write_text('open("/tests/checks.json")\n')
            self.assertEqual(grade.introspection_problems(root,{},['deps/lib']),
                [p+': /tests' for p in ['app/lib/helper.py','deps/lib/helper.py','other/vendor/helper.js']])

    def test_exact_fixture_bytes_remain_trusted_and_changes_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'vendor/legacy.py';path.parent.mkdir()
            path.write_text('import gc\ngc.get_objects()\n')
            manifest={'vendor/legacy.py':{'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
            self.assertEqual(grade.introspection_problems(root,manifest,['vendor']),[])
            path.write_text(path.read_text()+'changed=True\n')
            self.assertEqual(grade.introspection_problems(root,manifest,['vendor']),
                ['vendor/legacy.py: gc.get_objects'])

    def test_pristine_dependency_directory_links_are_not_traversed(self):
        with tempfile.TemporaryDirectory() as tmp:
            top=Path(tmp);root=top/'repo';root.mkdir();pristine=top/'deps/package';pristine.mkdir(parents=True)
            (pristine/'trusted.py').write_text('import gc\ngc.get_objects()\n')
            (root/'node_modules').symlink_to(pristine,target_is_directory=True)
            # restore_tree/fresh_copy use controller-owned dependency links;
            # untrusted escaping links are independently rejected by the guard.
            self.assertEqual(grade.introspection_problems(root,{},['node_modules']),[])
            self.assertEqual(grade.escaping_symlinks(root),['node_modules'])

    def test_changed_vendor_code_is_refused_before_any_runner_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'vendor/new.py';path.parent.mkdir()
            path.write_text('import gc\ngc.get_objects()\n')
            with patch.object(grade.subprocess,'run') as spawn:
                result=grade.run_command({'id':'objective','runner':{'type':'command','command':'true'}},root,{'vendored':['vendor']},{})
            self.assertFalse(result['passed']);spawn.assert_not_called()
            self.assertIn('vendor/new.py: gc.get_objects',result['error'])


if __name__=='__main__':unittest.main()
