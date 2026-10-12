"""Retain substantive work; stop malformed-call loops without accepting a task."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pipeline.author import agent_designer
designer = agent_designer
from pipeline import orchestrate


class AuthoringProgress(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = self.root / 'candidate'
        (self.bundle / 'environment/repo').mkdir(parents=True)
        self.extraction = SimpleNamespace(session_id='s', seed_index=0,
                                         source_root=self.root, moments=[])
        self.draft = self.bundle / 'draft.json'

    def handlers(self):
        return agent_designer._handlers(self.bundle, self.extraction, {}, self.draft)

    def test_malformed_draft_section_does_not_replace_completed_work(self):
        handlers = self.handlers()
        handlers['stage_spec'](['quality'], {'decisions': []})
        before = self.draft.read_bytes()
        with self.assertRaisesRegex(ValueError, 'expected JSON object'):
            handlers['stage_spec'](['quality'], '{"decisions":[]}')
        self.assertEqual(self.draft.read_bytes(), before)
        self.assertEqual(handlers['read_draft'](['quality']), {'decisions': []})
        with self.assertRaisesRegex(ValueError, 'expected JSON array'):
            handlers['stage_spec'](['checks'], '[]')

    def test_partial_sections_and_literal_json_prose_remain_supported(self):
        handlers = self.handlers()
        handlers['stage_spec'](['instruction'], 'Return an object such as {"label":"value"}.')
        handlers['stage_spec'](['checks'], [{'id': 'first'}])
        handlers['stage_spec'](['checks'], [{'id': 'second'}], append=True)
        handlers['stage_spec'](['checks', 0, 'actual'], {'op': 'field'})
        self.assertEqual(handlers['read_draft'](['checks', 1, 'id']), 'second')
        with self.assertRaisesRegex(ValueError, 'expected JSON object'):
            handlers['stage_spec'](['checks', 0, 'actual'], '{"op":"field"}')
        self.assertEqual(handlers['read_draft'](['checks', 0, 'actual']), {'op': 'field'})

    def drive(self, responses, max_steps=20):
        with ExitStack() as stack:
            stack.enter_context(patch('pipeline.dump.recover.load_trace', return_value={'messages': []}))
            stack.enter_context(patch('pipeline.config.sessions_root', side_effect=AssertionError('synthetic root required')))
            stack.enter_context(patch.object(agent_designer, '_opening', return_value='Read the source.'))
            call = stack.enter_context(patch.object(agent_designer, '_call', side_effect=responses))
            from pipeline.author.outcomes import AuthoringStalled
            with self.assertRaises(AuthoringStalled) as raised:
                agent_designer.design({}, self.extraction, self.bundle, max_steps=max_steps,
                                      initial_spec={'instruction': 'Retain this work.'})
        return call, raised.exception.report

    def response(self, name, args, tag='call'):
        return {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': tag, 'name': name, 'input': args}]}

    def test_consecutive_errors_stop_with_retained_draft_and_receipt(self):
        bad = self.response('evaluate', {'projections': [], 'target': '{"op":"rows"}'})
        limit = agent_designer.MAX_CONSECUTIVE_TOOL_ERRORS
        call, report = self.drive([bad] * 20)
        self.assertEqual(call.call_count, limit)
        self.assertEqual(report['consecutive_tool_errors'], limit)
        session = self.bundle / report['session']
        self.assertEqual(report['draft_sha256'], hashlib.sha256((session / 'draft.json').read_bytes()).hexdigest())
        self.assertFalse(report['release_ready'])
        self.assertTrue((session / 'stalled.json').is_file())
        conversation = json.loads((session / 'conversation.json').read_text())
        self.assertTrue(conversation[-1]['content'][0]['is_error'])
        self.assertIn('Retain this work.', (session / 'draft.json').read_text())

    def test_successful_tool_calls_reset_error_streak(self):
        bad = self.response('evaluate', {'projections': [], 'target': '{}'})
        good = self.response('read_draft', {})
        responses = [bad] * 5 + [good] + [bad] * 5 + [good]
        call, report = self.drive(responses, max_steps=len(responses))
        self.assertEqual(call.call_count, len(responses))
        self.assertEqual(report['consecutive_tool_errors'], 0)
        self.assertIn('without a submitted candidate', report['reason'])

    def parallel(self, response, n):
        turn = deepcopy(response)
        turn['content'] = [dict(response['content'][0], id=f'call-{i}') for i in range(n)]
        return turn

    def test_parallel_failures_in_one_turn_count_as_one(self):
        # a-031 (2026-09-26): one misread path convention across four parallel
        # stage_evidence calls, then two more, stalled a design at step 8/140.
        bad = self.response('evaluate', {'projections': [], 'target': '{}'})
        limit = agent_designer.MAX_CONSECUTIVE_TOOL_ERRORS
        responses = [self.parallel(bad, limit)] * (limit - 1) + [self.response('read_draft', {})]
        call, report = self.drive(responses, max_steps=len(responses))
        self.assertEqual(call.call_count, len(responses))
        self.assertEqual(report['consecutive_tool_errors'], 0)
        self.assertIn('without a submitted candidate', report['reason'])

    def test_consecutive_failed_turns_stall_after_the_whole_turn(self):
        bad = self.response('evaluate', {'projections': [], 'target': '{}'})
        limit = agent_designer.MAX_CONSECUTIVE_TOOL_ERRORS
        call, report = self.drive([self.parallel(bad, 3)] * 20)
        self.assertEqual(call.call_count, limit)
        self.assertEqual(report['consecutive_tool_errors'], limit)
        conversation = json.loads((self.bundle / report['session'] / 'conversation.json').read_text())
        self.assertEqual(len(conversation[-1]['content']), 3)
        self.assertTrue(all(r['is_error'] for r in conversation[-1]['content']))

    def test_a_successful_call_resets_the_streak_within_its_turn(self):
        bad = self.response('evaluate', {'projections': [], 'target': '{}'})
        mixed = deepcopy(bad)
        mixed['content'].append({'type': 'tool_use', 'id': 'edit', 'name': 'stage_spec',
                                 'input': {'path': ['instruction'], 'value': 'Progress was made.'}})
        responses = [bad] * 5 + [mixed] + [bad] * 5
        _, report = self.drive(responses, max_steps=len(responses))
        self.assertEqual(report['consecutive_tool_errors'], 5)
        self.assertIn('without a submitted candidate', report['reason'])
        session = self.bundle / report['session']
        self.assertEqual(json.loads((session / 'draft.json').read_text())['instruction'],
                         'Progress was made.')

    def build(self, author, reviews=None):
        def exported(spec, bundle):
            path = bundle / 'authoring/traps.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"traps":[]}')
        with ExitStack() as stack:
            for target, value in [
                ('extract_stage.extract', Mock(return_value=self.extraction)),
                ('extract_stage.write', Mock()),
                ('extract_stage.usable_correction_count', Mock(return_value=0)),
                ('build_stage.build', Mock(side_effect=exported)),
                ('faithfulness.build', Mock(return_value={'faithfulness_by_bytes': 1.0})),
                ('probe_stage.run', Mock(return_value={'results': {}})),
                ('controls_stage.run', Mock(return_value={'ok': True, 'errors': [], 'results': {}, 'advisories': []})),
                ('run_bundle', Mock(return_value={'verdict': 'green', 'results': []})),
                ('assurance_stage.generated_problems', Mock(return_value=[])),
                ('assurance_stage.controls', Mock(return_value={'errors': []})),
            ]:
                stack.enter_context(patch('pipeline.orchestrate.' + target, value))
            stack.enter_context(patch('pipeline.ledger.redaction.minimize'))
            stack.enter_context(patch.object(orchestrate, 'GATE_REPAIRS', 1))
            stack.enter_context(patch.object(agent_designer, 'design', author))
            critic = stack.enter_context(patch.object(orchestrate, '_review_candidate', side_effect=reviews))
            campaign = stack.enter_context(patch('pipeline.calibrate.run.campaign'))
            result = orchestrate.build_one({'session_id': 's', 'seed_index': 0, 'tier': 1},
                1, self.root / 'batch', designer.design, orchestrate.Budget(2))
        return result, critic, campaign

    def test_semantic_repair_resumes_existing_spec_and_revalidates(self):
        first = {'name': 'Completed source work', 'instruction': 'Preserve all source rules.',
                 'forged_applicable': False, 'forged_reason': '', 'checks': [{'id': 'existing'}]}
        calls = []
        def author(*args, **kwargs):
            calls.append({key: deepcopy(value) for key, value in kwargs.items() if key != 'budget'})
            if len(calls) == 1:
                return deepcopy(first)
            self.assertEqual(kwargs['initial_spec'], first)
            self.assertIn('Semantic review o1_schedule: repair the schedule', kwargs['feedback'])
            amended = deepcopy(kwargs['initial_spec'])
            amended['instruction'] += ' Corrected schedule rule.'
            return amended
        result, critic, campaign = self.build(author, [
            {'verdict': 'blocked', 'findings': ['Semantic review o1_schedule: repair the schedule']},
            {'verdict': 'green', 'findings': []}])
        self.assertEqual(result.status, 'validated', result.error)
        self.assertIsNone(calls[0]['initial_spec'])
        self.assertEqual(critic.call_count, 2)
        campaign.assert_not_called()

    def test_stalled_author_is_not_restarted_or_sent_to_review(self):
        from pipeline.author.outcomes import AuthoringStalled
        author = Mock(side_effect=AuthoringStalled({'reason': 'Repeated malformed inputs',
                                                   'release_ready': False}))
        result, critic, campaign = self.build(author)
        self.assertEqual(result.status, 'unverified', result.error)
        self.assertEqual(result.stage_reached, 'authoring-progress')
        self.assertEqual(author.call_count, 1)
        critic.assert_not_called()
        campaign.assert_not_called()
        receipt = next((self.root / 'batch').glob('*/verification/authoring-stalled.json'))
        self.assertFalse(json.loads(receipt.read_text())['release_ready'])


if __name__ == '__main__':
    unittest.main()
