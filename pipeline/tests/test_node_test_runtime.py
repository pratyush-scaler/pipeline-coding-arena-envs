"""Real Node 22 reporter fixtures and reviewed runtime variant selection."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest

from pipeline.author import runtime, toolchain
from pipeline.author.grader_runtime import node_test
from pipeline.gates import code


FIXTURES = Path(__file__).parent / 'fixtures/node22-reports'


class RuntimeVariantTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.common = runtime.GRADER.read_bytes()

    def bundle(self, name):
        root = self.root / name
        (root / 'environment').mkdir(parents=True)
        (root / 'environment/toolchain.json').write_text(json.dumps(toolchain.record(name, language=name)))
        return root

    def test_non_node_staging_and_default_hash_are_exact_common_bytes(self):
        self.assertEqual(runtime.sha256(), hashlib.sha256(self.common).hexdigest())
        for name in ('python', 'c'):
            with self.subTest(toolchain=name):
                bundle = self.bundle(name)
                self.assertEqual(runtime.stage(bundle).read_bytes(), self.common)
                self.assertEqual(runtime.problems(bundle), [])

    def test_node_and_browser_stage_and_pin_the_reviewed_specialization(self):
        specialized = node_test.render(self.common)
        self.assertNotEqual(specialized, self.common)
        for name in ('node', 'browser'):
            with self.subTest(toolchain=name):
                bundle = self.bundle(name)
                self.assertEqual(runtime.stage(bundle).read_bytes(), specialized)
                self.assertEqual(runtime.sha256(bundle), hashlib.sha256(specialized).hexdigest())
                self.assertEqual(runtime.problems(bundle), [])
                findings, evidence = code._grader_runtime(SimpleNamespace(task_dir=bundle))
                self.assertEqual(findings, [])
                self.assertEqual(evidence['sha256'], runtime.sha256(bundle))

    def test_runtime_pins_reject_swapped_or_modified_variants(self):
        node = self.bundle('node')
        target = runtime.stage(node)
        target.write_bytes(self.common)
        self.assertTrue(runtime.problems(node))
        runtime.stage(node)
        target.write_bytes(target.read_bytes() + b'\n# altered\n')
        self.assertTrue(runtime.problems(node))
        python = self.bundle('python')
        runtime.stage(python).write_bytes(node_test.render(self.common))
        self.assertTrue(runtime.problems(python))

    def test_node_specialization_cannot_be_applied_twice(self):
        with self.assertRaises(ValueError):
            node_test.render(node_test.render(self.common))


class CapturedNativeReportTests(unittest.TestCase):
    def setUp(self):
        self.grade = ModuleType('reviewed_node_test_runtime')
        self.grade.__file__ = str(runtime.GRADER)
        source = node_test.render(runtime.GRADER.read_bytes())
        exec(compile(source, self.grade.__file__, 'exec'), self.grade.__dict__)
        # The native fixtures were produced with this private suite mount.
        self.grade.HERE = Path('/source')

    def test_genuine_native_completed_cases_are_counted_without_double_counting(self):
        for name, selection, expected in (
            ('flat_pass', ['pass.test.js'], 1),
            ('esm_pass', ['esm.test.mjs'], 1),
            ('nested_pass', ['nested.test.js'], 2),
        ):
            with self.subTest(case=name):
                proof = json.loads((FIXTURES / name / 'node-selection.json').read_text())
                counts = self.grade.test_counts('node-test', '', '', FIXTURES / name, Path('/source/suites'),
                                               nonce=proof['nonce'], select=selection)
                self.assertEqual(counts, {'passed': expected, 'failed': 0, 'skipped': 0})

    def test_native_failures_empty_missing_skipped_and_forged_stdout_do_not_pass(self):
        for name, selection in (
            ('assertion_fail', ['fail.test.js']), ('empty_file', ['empty.test.js']),
            ('missing_file', ['does-not-exist.test.js']),
            ('mixed_empty_selection', ['pass.test.js', 'empty.test.js']),
            ('skipped_case', ['skip.test.js']), ('early_exit_forged_stdout', ['early.test.js']),
        ):
            with self.subTest(case=name):
                proof_path = FIXTURES / name / 'node-selection.json'
                proof = json.loads(proof_path.read_text()) if proof_path.exists() else {}
                counts = self.grade.test_counts('node-test', '', '', FIXTURES / name, Path('/source/suites'),
                                               nonce=proof.get('nonce', 'missing'), select=selection)
                self.assertFalse(counts is not None and counts['passed'] > 0
                                 and counts['failed'] == 0 and counts['skipped'] == 0)

    def test_report_requires_matching_parent_nonce_selection_hash_and_receipt(self):
        for name, selection, nonce in (
            ('wrong_nonce', ['pass.test.js'], 'wrong-run-nonce'),
            ('wrong_selection', ['esm.test.mjs'], 'isolated-native-diagnostic-flat_pass'),
            ('wrong_digest', ['pass.test.js'], 'isolated-native-diagnostic-flat_pass'),
            ('missing_selection_proof', ['pass.test.js'], 'isolated-native-diagnostic-flat_pass'),
        ):
            with self.subTest(case=name):
                self.assertIsNone(self.grade.test_counts('node-test', '', '', FIXTURES / name,
                                                        Path('/source/suites'), nonce=nonce, select=selection))


if __name__ == '__main__':
    unittest.main()
