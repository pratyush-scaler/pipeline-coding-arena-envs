"""Concurrent measurement preserves controls, both models and the shared cap."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pipeline import checkpoint, release
from pipeline.artifacts import atomic_json
from pipeline.benchmark.capacity import NativeCapacity
from pipeline.tests.test_production import synthetic_bundle


class ParallelQualificationTests(unittest.TestCase):
    def test_capacity_is_shared_and_exception_releases_waiters(self):
        capacity = NativeCapacity(3)
        entered = threading.Event()
        def waiting():
            with capacity.reserve(10) as slots:
                self.assertEqual(slots, 3)
                entered.set()
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            with capacity.reserve(10) as slots:
                self.assertEqual(slots, 3)
                thread = threading.Thread(target=waiting)
                thread.start()
                with capacity.condition:
                    self.assertTrue(capacity.condition.wait_for(lambda: bool(capacity.waiting), timeout=2))
                self.assertFalse(entered.is_set())
                raise RuntimeError('failed')
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(entered.is_set())
        self.assertEqual(capacity.stats(), {'limit': 3, 'reserved': 0, 'peak_reserved': 3, 'waiting_campaigns': 0})

    def test_two_models_overlap_and_all_campaigns_are_retained(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            # Qualification behaviour under test; the screen gate is tested
            # in test_spec_acceptance.
            stack.enter_context(patch("pipeline.screen.shippable_problems", return_value=[]))
            root = Path(temporary)
            bundle = synthetic_bundle(root)
            spec = json.loads((bundle / 'authoring/spec.json').read_text())
            spec['_producer'] = {'model': 'author'}
            atomic_json(bundle / 'authoring/spec.json', spec)
            barrier = threading.Barrier(2)
            capacity = NativeCapacity(4)
            ran = {}
            def native(_bundle, output, *, agent, **kwargs):
                ran[str(output)] = (agent, kwargs.get('model'))
                if agent in ('claude-code', 'codex'):
                    self.assertEqual(kwargs['rollouts'], 10)
                    self.assertEqual(kwargs['workers'], 2)
                    barrier.wait(3)
                return {'ok': True}
            def evidence(_bundle, path, role, minimum=1):
                agent, model = ran[str(path)]
                return [], {'model': model, 'trials': [{'reward': 1 if agent == 'oracle' else 0}]}
            for target, value in (
                ('pipeline.run.run_bundle', {'verdict': 'green'}),
                ('pipeline.privacy.scan', []), ('pipeline.release._provenance_problems', []),
                ('pipeline.corpus.problems', []), ('pipeline.assurance.generated_problems', []),
                ('pipeline.assurance.controls', {'ok': True}),
                ('pipeline.author.controls.problems', []),
                ('pipeline.benchmark.harbor.doctor', {'ok': True}),
                ('pipeline.review.review_problems', ['missing review']),
                ('pipeline.review.review', {'ok': True}),
                ('pipeline.evidence.calibration_problems', []),
                ('pipeline.calibrate.run.campaign', {'campaign_id': 'blind', 'steps': ['first'],
                    'rollouts': [{'episode': 0, 'reward': 1}]}),
                ('pipeline.release._evaluate', {'release_ready': False, 'status': 'blocked'})):
                stack.enter_context(patch(target, return_value=value))
            stack.enter_context(patch('pipeline.benchmark.harbor.run_campaign', side_effect=native))
            stack.enter_context(patch('pipeline.evidence.native_evidence', side_effect=evidence))
            stack.enter_context(patch('pipeline.benchmark.harbor.resolve_model', side_effect=lambda model: model))
            stack.enter_context(patch('pipeline.benchmark.capacity.NATIVE_CAPACITY', capacity))
            stack.enter_context(patch('pipeline.policy.harness_for', side_effect=lambda model: {
                'first-model': 'claude-code', 'second-model': 'codex'}[model]))
            result = release._qualify(bundle, ['first-model', 'second-model'], 'reviewer', 'solver', root / 'native', workers=2)
            self.assertFalse(result['release_ready'])
            index = json.loads((bundle / 'verification/native-calibrations.json').read_text())
            self.assertEqual({ran[path][1] for path in index['campaigns']}, {'first-model', 'second-model'})
            # Each model's calibration ran in its assigned harness (user, 2026-09-26).
            self.assertEqual({ran[path] for path in index['campaigns']},
                             {('claude-code', 'first-model'), ('codex', 'second-model')})
            self.assertEqual(capacity.stats()['peak_reserved'], 4)
            self.assertEqual(capacity.stats()['reserved'], 0)

    def test_saved_revision_waits_for_manual_trigger(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            checkpoint.update(bundle, phase='qualification-repair', stage='authoring',
                              qualification_repairs_remaining=0, feedback=['Saved finding'])
            with patch.object(release, '_evaluate', return_value={'release_ready': False}) as evaluate, \
                    patch.object(release, '_qualify') as measure, patch('pipeline.repair.revise') as revise:
                result = release.qualify(bundle, ['one', 'two'], 'reviewer', 'solver', bundle / 'native')
            revise.assert_not_called()
            measure.assert_not_called()
            self.assertTrue(result['manual_trigger_required'])
            self.assertFalse(result['release_ready'])
            self.assertEqual(checkpoint.read(bundle)['phase'], 'qualification-repair')
