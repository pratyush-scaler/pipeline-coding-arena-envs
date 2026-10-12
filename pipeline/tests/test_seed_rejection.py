"""Creation retains weak-source findings; validation decides source rejection."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from pipeline.author.agent_designer import _rejection, design
from pipeline.author.outcomes import SeedRejected


class SeedRejection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = self.root / 'candidate'
        fixture = self.bundle / 'environment/repo'
        fixture.mkdir(parents=True)
        (fixture / 'brief.md').write_text('Only the final total is available; component records are absent.\n')
        self.reason = {'reason': ('The source supplies a copied total but no component records, so no '
                                  'independent decision can be graded: every candidate check would restate '
                                  'the printed total, and a fair two-step task needs component-level evidence '
                                  'that the brief says is absent.'),
            'source_evidence': [{'file': 'brief.md', 'quote': 'component records are absent.'}]}

    def test_rejection_requires_actual_public_evidence(self):
        receipt = _rejection(self.bundle, self.reason)
        self.assertFalse(receipt['release_ready'])
        self.assertEqual(receipt['status'], 'source-rejected')
        for row in ({'file': 'brief.md', 'quote': 'invented fact'},
                    {'file': '../private.md', 'quote': 'private'},
                    {'file': 'brief.md', 'quote': ''}):
            with self.subTest(row=row), self.assertRaises((ValueError, OSError)):
                _rejection(self.bundle, {**self.reason, 'source_evidence': [row]})

    def test_an_incomplete_receipt_says_exactly_what_is_missing(self):
        # e2e build, 2026-09-26: four rejections carried the evidence inside
        # `reason`; the refusal never said `source_evidence` was the missing key.
        with self.assertRaises(ValueError) as caught:
            _rejection(self.bundle, {'reason': self.reason['reason']})
        message = str(caught.exception)
        self.assertIn("missing ['source_evidence']", message)
        self.assertIn('"quote"', message, "the required shape is quoted")
        self.assertIn('Nothing has been rejected yet', message)
        with self.assertRaises(ValueError) as caught:
            _rejection(self.bundle, {**self.reason, 'notes': 'x'})
        self.assertIn("unexpected ['notes']", str(caught.exception))

    def test_a_trial_call_cannot_end_the_design(self):
        # ...and a 119-character "Test of receipt format" call became the record.
        probe = {'reason': 'Test of receipt format: the pack contradicts the rule.',
                 'source_evidence': self.reason['source_evidence']}
        with self.assertRaises(ValueError) as caught:
            _rejection(self.bundle, probe)
        self.assertIn('never send a trial', str(caught.exception))

    def test_only_the_argued_receipt_becomes_the_record(self):
        extraction = SimpleNamespace(session_id='source', seed_index=0, source_root=self.root, moments=[])
        def reject(i, payload):
            return {'stop_reason': 'tool_use', 'content': [
                {'type': 'tool_use', 'id': f'reject-{i}', 'name': 'reject_seed', 'input': payload}]}
        responses = [reject(0, {'reason': self.reason['reason']}),
                     reject(1, {'reason': 'Test of receipt format.', 'source_evidence': self.reason['source_evidence']}),
                     reject(2, self.reason)]
        with patch('pipeline.dump.recover.load_trace', return_value={'messages': []}), \
             patch('pipeline.author.agent_designer._handlers', return_value={
                 'draft_spec': lambda: {'name': 'Retained candidate'}, 'submit_check': lambda spec: {'clean': True}}), \
             patch('pipeline.author.agent_designer._opening', return_value='Read the source.'), \
             patch('pipeline.author.agent_designer._call', side_effect=responses) as call:
            spec = design({}, extraction, self.bundle, max_steps=10)
        self.assertEqual(call.call_count, 3)
        receipt = next((self.bundle / 'verification/design-sessions').glob('*/source-rejection.json'))
        self.assertEqual(json.loads(receipt.read_text())['reason'], self.reason['reason'])
        self.assertTrue(spec['_source_capacity_report']['validation_deferred'])

    def test_terminal_rejection_retains_receipt_and_stops_api_calls(self):
        extraction = SimpleNamespace(session_id='source', seed_index=0, source_root=self.root, moments=[])
        response = {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': 'reject', 'name': 'reject_seed', 'input': self.reason}]}
        with patch('pipeline.dump.recover.load_trace', return_value={'messages': []}) as load_trace, \
             patch('pipeline.config.sessions_root', side_effect=AssertionError('test must use its synthetic source root')), \
             patch('pipeline.author.agent_designer._handlers', return_value={
                 'draft_spec': lambda: {'name': 'Retained candidate'}, 'submit_check': lambda spec: {'clean': True}}), \
             patch('pipeline.author.agent_designer._opening', return_value='Read the source.'), \
             patch('pipeline.author.agent_designer._call', return_value=response) as call:
            spec = design({}, extraction, self.bundle, max_steps=10)
            self.assertEqual(call.call_count, 1)
            load_trace.assert_called_once_with('source', self.root)
        receipt = next((self.bundle / 'verification/design-sessions').glob('*/source-rejection.json'))
        self.assertEqual(json.loads(receipt.read_text())['source_evidence'], self.reason['source_evidence'])
        self.assertFalse((self.bundle / 'authoring/spec.json').exists())
        from pipeline import checkpoint
        self.assertEqual(checkpoint.read(self.bundle)['stage'], 'submitted')
        self.assertEqual(spec['_agent']['validation_status'], 'deferred')

    def test_orchestrator_does_not_retry_rejected_source_or_run_calibration(self):
        from pipeline.orchestrate import build_one, Budget
        reject = Mock(side_effect=SeedRejected(_rejection(self.bundle, self.reason)))
        with patch.dict(os.environ, {'ARENA_AGENT_DESIGN': '0'}), \
             patch('pipeline.orchestrate.extract_stage.extract', return_value=SimpleNamespace(moments=[])), \
             patch('pipeline.orchestrate.extract_stage.write'), \
             patch('pipeline.ledger.redaction.minimize'), \
             patch('pipeline.orchestrate.build_stage.build') as export, \
             patch('pipeline.calibrate.run.campaign') as campaign:
            result = build_one({'session_id': 's', 'seed_index': 0, 'tier': 1}, 1,
                self.root / 'batch', reject, Budget(2))
        self.assertEqual(result.status, 'reserve')
        self.assertEqual(result.stage_reached, 'source-capacity')
        self.assertEqual(result.error, '')
        self.assertEqual(reject.call_count, 1)
        export.assert_not_called()
        campaign.assert_not_called()


if __name__ == '__main__':
    unittest.main()
