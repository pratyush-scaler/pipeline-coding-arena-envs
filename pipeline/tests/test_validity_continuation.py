"""Explicit validity work preserves budgets and cannot become automatic hardening."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from category_batch import (Controller, prepare_validity_revision, source_key,
                            validity_authorization, validity_findings, validity_passes_left, completed_calibration)
from pipeline import checkpoint
from pipeline.artifacts import atomic_json, identity
from pipeline.tests.test_category_batch import row
from pipeline.tests.test_production import synthetic_bundle


def authorization(key='source-0', passes=2):
    return {'id': 'user-validity-round', 'instruction': 'Correct defects, then calibrate; hardening later.',
            'purpose': 'validity-before-calibration', 'targets': [key], 'max_passes': passes, 'max_steps': 100}


class ValidityContinuationTests(unittest.TestCase):
    def test_validated_task_uses_only_current_artifact_bound_review_findings(self):
        record = {'candidate': '/retained/task', 'build': {'status': 'validated',
            'stage_reached': 'semantic-review', 'repair_feedback': ['Old build rejection']},
            'validity_findings': ['Old qualification rejection']}
        with patch('category_batch.review_validity_findings', return_value=[]) as review:
            self.assertEqual(validity_findings(record), [])
            review.assert_called_once_with(Path('/retained/task'))
        with patch('category_batch.review_validity_findings', return_value=['Current public-rule defect']):
            self.assertEqual(validity_findings(record), ['Current public-rule defect'])

    def test_restart_does_not_reopen_calibration_hold_from_resolved_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = row('source', 'F3', 1)
            atomic_json(root / 'queue.json', {'rows': []})
            atomic_json(root / 'tasks/source-0.json', {'row': source, 'stage': 'awaiting-hardening',
                'candidate': str(root / 'task'), 'build': {'status': 'validated', 'stage_reached': 'semantic-review'},
                'validity_findings': ['Historical source mapping defect, since repaired'],
                'release': {'status': 'blocked', 'errors': ['calibration: one incomplete native episode']}})
            with patch('pipeline.artifacts.policy_identity', return_value='policy'), \
                    patch('category_batch.review_validity_findings', return_value=[]):
                controller = Controller({'work': str(root), 'out': str(root / 'out'), 'policy_sha256': 'policy',
                                         'validity_revision': authorization()}, 1, 1)
            self.assertEqual(controller.records['source-0']['stage'], 'awaiting-hardening')

    def test_completed_calibration_retains_easy_results_for_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = synthetic_bundle(Path(tmp))
            outputs = [Path(tmp) / name for name in ('one', 'two')]
            for output in outputs:
                atomic_json(output / 'report.json', {'summary': {'valid': 10, 'void': 0, 'mean_reward': 1.0}})
            atomic_json(bundle / 'verification/native-calibrations.json', {
                'artifact_sha256': identity(bundle), 'campaigns': [str(p) for p in outputs]})
            def measured(candidate, index, path, minimum):
                # Read through the index so top-up campaigns are counted.
                self.assertEqual(minimum, 10)
                self.assertIn('campaigns', index)
                return [], {'model': Path(path).name, 'by_step': {'step-1': {'checks': {
                    'decision': {'kind': 'objective', 'passed': 10, 'failed': 0, 'gated_out': 0}}}}}
            with patch('pipeline.release.calibration_measurement', side_effect=measured), \
                    patch('pipeline.benchmark.harbor.resolve_model', side_effect=lambda name: name):
                result = completed_calibration(bundle, ['one', 'two'])
                self.assertTrue(result['complete'])
                self.assertEqual(result['models']['one']['summary']['valid'], 10)
                self.assertEqual(result['models']['one']['difficulty']['status'], 'easy')
                self.assertIsNone(completed_calibration(bundle, ['one', 'missing']))

    def test_missing_or_unrelated_instruction_cannot_authorize_revision(self):
        self.assertIsNone(validity_authorization({}, 'source-0'))
        self.assertIsNone(validity_authorization({'validity_revision': authorization('other-0')}, 'source-0'))
        for change in ({'purpose': 'hardening'}, {'instruction': ''}, {'max_passes': True}, {'max_steps': 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validity_authorization({'validity_revision': {**authorization(), **change}}, 'source-0')

    def test_measurement_and_provider_failures_are_not_revision_feedback(self):
        record = {'build': {'stage_reached': 'semantic-review', 'repair_feedback': [
            'Semantic review C1: public rule conflicts with the grader',
            "calibration: measured bands {'floor': 10}",
            'semantic review could not be verified: provider unavailable']}}
        self.assertEqual(validity_findings(record), ['Semantic review C1: public rule conflicts with the grader'])
        self.assertEqual(validity_findings({'build': {'stage_reached': 'difficulty',
            'repair_feedback': ['Change easy checks']}}), [])

    def test_pass_is_archived_charged_once_and_does_not_reset_original_allowances(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = synthetic_bundle(Path(tmp))
            before = identity(bundle)
            checkpoint.update(bundle, stage='blocked', gate_repairs_remaining=0, qualification_repairs_remaining=0)
            config = authorization(passes=1)
            prepared = prepare_validity_revision(bundle, config, ['Clarify the governing source rule'])
            self.assertEqual(identity(bundle), before)
            self.assertEqual(prepared['gate_repairs_remaining'], 0)
            self.assertEqual(prepared['qualification_repairs_remaining'], 0)
            self.assertEqual(prepared['validity_revision']['passes_started'], 1)
            self.assertEqual(validity_passes_left(bundle, config), 0)
            self.assertEqual(checkpoint.resumable(bundle)['stage'], 'repair')
            self.assertEqual(identity(bundle / prepared['validity_revision']['archive'] / 'task'), before)
            resumed = prepare_validity_revision(bundle, config, ['Clarify the governing source rule'])
            self.assertEqual(resumed['validity_revision']['passes_started'], 1)
            checkpoint.update(bundle, stage='blocked', validity_revision={**resumed['validity_revision'], 'active': False})
            with self.assertRaisesRegex(ValueError, 'passes remain'):
                prepare_validity_revision(bundle, config, ['Still invalid'])

    def test_changed_author_source_is_not_silently_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            atomic_json(bundle / 'verification/design-sessions/retained/request-config.json', {'max_steps': 3})
            atomic_json(bundle / 'verification/design-sessions/retained/conversation.json', [])
            atomic_json(bundle / 'verification/design-sessions/retained/draft.json', {})
            source = bundle / 'environment/repo/source.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Original source')
            checkpoint.author_boundary(bundle, bundle / 'verification/design-sessions/retained', stage='stalled')
            source.write_text('Unverified mutation')
            with self.assertRaisesRegex(ValueError, 'saved author boundary'):
                prepare_validity_revision(bundle, authorization(), ['Finish the draft'])

    def test_explicit_instruction_reopens_a_defective_task_but_not_an_easy_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, easy = row('source', 'F3', 1), row('easy', 'F2', 2)
            atomic_json(root / 'queue.json', {'rows': []})
            for item, build in [(source, {'stage_reached': 'semantic-review', 'repair_feedback': ['Defective rule']}),
                                (easy, {'stage_reached': 'difficulty', 'repair_feedback': ['Easy scores']})]:
                atomic_json(root / 'tasks' / (source_key(item) + '.json'), {'row': item, 'stage': 'awaiting-hardening',
                    'candidate': str(root / item['session_id']), 'build': build})
            config = {**authorization(), 'targets': ['source-0', 'easy-0']}
            with patch('pipeline.artifacts.policy_identity', return_value='policy'):
                controller = Controller({'work': str(root), 'out': str(root / 'out'), 'policy_sha256': 'policy',
                                         'validity_revision': config}, 1, 1)
            self.assertEqual(controller.records['source-0']['stage'], 'validity-queued')
            self.assertEqual(controller.records['easy-0']['stage'], 'awaiting-hardening')

    def test_scheduler_runs_selected_validity_work_without_new_source_screening(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        controller.request = {'per_category': 1, 'selected_only': True, 'eager_selected': True}
        controller.task_workers = controller.classify_workers = controller.qualification_workers = 1
        source = row('source', 'F3', 1)
        controller.queue, controller.classified = [], {}
        controller.records = {'source-0': {'row': source, 'stage': 'validity-queued'}}
        controller.save = controller.import_prefetched = lambda: None
        attempted = []
        def build(item):
            attempted.append(item['session_id'])
            controller.records['source-0']['stage'] = 'validity-blocked'
        controller.build = build
        with patch.object(controller, 'classify') as classify:
            self.assertEqual(controller.drive(), 1)
        self.assertEqual(attempted, ['source'])
        classify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
