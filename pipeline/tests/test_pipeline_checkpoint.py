"""Resume actual saved author boundaries without new attempts or plan review."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pipeline import checkpoint, orchestrate, release
from pipeline.artifacts import atomic_json
from pipeline.author import agent_designer as author
from pipeline.author.quality import QUALITY_PROMPT


class PipelineCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        (self.bundle / 'environment/repo').mkdir(parents=True)
        (self.bundle / 'environment/repo/source.txt').write_text('Scenario evidence')
        self.extraction = SimpleNamespace(session_id='source', seed_index=0)

    def session(self, tools=None, limit=5):
        session = self.bundle / 'verification/design-sessions/original'
        atomic_json(session / 'request-config.json', {
            'model': author.MODEL, 'effort': author.ARENA_EFFORT, 'max_steps': limit,
            'max_tokens': 48000, 'tools': tools or [], 'system': author.SYSTEM})
        atomic_json(session / 'conversation.json', [{'role': 'user', 'content': 'Retain original source instructions'}])
        return session

    def mock_author(self, stack, responses, tools=None, handlers=None):
        stack.enter_context(patch('pipeline.dump.recover.load_trace', return_value={'messages': []}))
        stack.enter_context(patch('pipeline.privacy.scan', return_value=[]))
        stack.enter_context(patch.object(author, '_opening', return_value='Read sources and build'))
        stack.enter_context(patch.object(author, '_tools', return_value=tools or []))
        stack.enter_context(patch.object(author, '_handlers', return_value=handlers or {}))
        return stack.enter_context(patch.object(author, '_call', side_effect=responses))

    def test_complete_draft_construction_has_no_plan_review_dependency(self):
        handlers = author._handlers(self.bundle, self.extraction, {'messages': []}, self.bundle / 'draft.json')
        handlers['stage_spec'](path=['checks'], value=[])
        self.assertEqual(handlers['draft_spec']()['checks'], [])
        self.assertNotIn('review_design', handlers)
        self.assertNotIn('review_design', [tool['name'] for tool in author._tools(0)])

    def test_parallel_tools_consume_one_response_and_interruption_preserves_history(self):
        response = {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': f'call-{i}', 'name': 'read_fixture', 'input': {'path': str(i)}}
            for i in range(2)]}
        with ExitStack() as stack:
            self.mock_author(stack, [response, TimeoutError('transport stopped')],
                handlers={'read_fixture': lambda path: {'source': path}})
            with self.assertRaises(TimeoutError):
                author.design({}, self.extraction, self.bundle, max_steps=5)
        state = checkpoint.resumable(self.bundle)
        self.assertEqual(state['stage'], 'authoring')
        saved = state['authoring_session']
        self.assertEqual((saved['response_count'], saved['remaining_steps']), (1, 4))
        history = json.loads((self.bundle / saved['session'] / 'conversation.json').read_text())
        self.assertEqual(len(history[-1]['content']), 2)
        with ExitStack() as stack:
            call = self.mock_author(stack, [TimeoutError('second interruption')])
            with self.assertRaises(TimeoutError):
                author.design({}, self.extraction, self.bundle, max_steps=4, resume_session=saved)
            # The request carries cache markers on a per-request copy; the
            # stored history never does (test_prompt_caching). Compare what
            # the model reads.
            def unmarked(node):
                if isinstance(node, dict):
                    return {k: unmarked(v) for k, v in node.items() if k != 'cache_control'}
                return [unmarked(v) for v in node] if isinstance(node, list) else node
            self.assertEqual(unmarked(call.call_args.args[0]['messages']), history)
        again = checkpoint.resumable(self.bundle)['authoring_session']
        self.assertEqual(again['remaining_steps'], 4)

    def test_the_mandate_follows_the_callers_intent_not_the_feedback(self):
        # Re-review: category_batch sends its design brief as feedback on every
        # FIRST design, which used to swap in the revision mandate.
        seen = {}
        for revision in (False, True):
            with ExitStack() as stack:
                call = self.mock_author(stack, [TimeoutError('observed request')])
                with self.assertRaises(TimeoutError):
                    author.design({}, self.extraction, self.bundle, max_steps=5,
                                  feedback=['a design brief line'], revision=revision)
                seen[revision] = call.call_args.args[0]['messages'][0]['content']
        self.assertIn(author.AUTHORING_MANDATE, seen[False])
        self.assertNotIn(author.REVISION_MANDATE, seen[False])
        self.assertIn(author.REVISION_MANDATE, seen[True])
        self.assertNotIn(author.AUTHORING_MANDATE, seen[True])

    def test_completed_submission_resumes_build_without_another_author_response(self):
        session = self.session(limit=1)
        spec = {'instruction': 'First', 'second_step': {'instruction': 'Second'}}
        checkpoint.submitted(self.bundle, session, spec)
        self.assertEqual(checkpoint.resumable(self.bundle)['stage'], 'submitted')
        self.assertEqual(checkpoint.submitted_spec(self.bundle), spec)
        (session / 'submitted.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'changed'):
            checkpoint.resumable(self.bundle)

    def test_changed_workspace_and_consumed_repairs_cannot_be_reset(self):
        session = self.session()
        checkpoint.author_boundary(self.bundle, session)
        self.assertEqual(checkpoint.repair_budget(self.bundle, 'gate', 1), 1)
        checkpoint.consume_repair(self.bundle, 'gate')
        self.assertEqual(checkpoint.repair_budget(self.bundle, 'gate', 1), 0)
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            checkpoint.consume_repair(self.bundle, 'gate')
        (self.bundle / 'environment/repo/source.txt').write_text('Changed source')
        with self.assertRaisesRegex(ValueError, 'workspace changed'):
            checkpoint.resumable(self.bundle)

    def test_disabling_auto_repair_preserves_manual_allowance(self):
        for kind in ('gate', 'qualification'):
            checkpoint.update(self.bundle, **{kind + '_repairs_remaining': 1})
            self.assertEqual(checkpoint.repair_budget(self.bundle, kind, 0), 0)
            self.assertEqual(checkpoint.read(self.bundle)[kind + '_repairs_remaining'], 1)
            self.assertEqual(checkpoint.repair_budget(self.bundle, kind, 1), 1)
            checkpoint.consume_repair(self.bundle, kind)
            self.assertEqual(checkpoint.repair_budget(self.bundle, kind, 0), 0)
            self.assertEqual(checkpoint.repair_budget(self.bundle, kind, 1), 0)

    def test_interrupted_mutation_cannot_start_a_fresh_attempt(self):
        row = {'session_id': 'source', 'seed_index': 0}
        bundle = self.bundle / 'draft'
        checkpoint.update(bundle, stage='authoring-tools', gate_repairs_remaining=0)
        with patch.object(orchestrate, '_slug', return_value='draft'), \
                patch.object(orchestrate.extract_stage, 'extract') as extract:
            result = orchestrate._build_one(row, 1, self.bundle, Mock(), nullcontext())
        self.assertEqual(result.status, 'error')
        self.assertIn('not resumable', result.error)
        extract.assert_not_called()
        self.assertEqual(checkpoint.read(bundle)['gate_repairs_remaining'], 0)

    def test_only_retired_plan_tool_can_be_removed_from_retained_configuration(self):
        session = self.session(tools=[{'name': 'review_design'}])
        saved = checkpoint.author_boundary(self.bundle, session)
        with ExitStack() as stack:
            call = self.mock_author(stack, [TimeoutError('observed request')])
            with self.assertRaises(TimeoutError):
                author.design({}, self.extraction, self.bundle, max_steps=5, resume_session=saved)
            messages = call.call_args.args[0]['messages']
            self.assertEqual(messages[0]['content'], 'Retain original source instructions')
            self.assertIn('removed', messages[-1]['content'])
        config = json.loads((session / 'request-config.json').read_text())
        config['model'] = 'another-author'
        atomic_json(session / 'request-config.json', config)
        saved = checkpoint.author_boundary(self.bundle, session)
        with ExitStack() as stack:
            call = self.mock_author(stack, [TimeoutError('observed request')])
            # A changed model, prompt or tool set no longer fails the task: the
            # design continues as a fresh session on the remaining budget,
            # never replaying the old conversation under new instructions.
            with self.assertRaises(TimeoutError):
                author.design({}, self.extraction, self.bundle, max_steps=5, resume_session=saved)
            messages = call.call_args.args[0]['messages']
            self.assertEqual(len(messages), 1)
            self.assertNotEqual(messages[0]['content'], 'Retain original source instructions')
        receipts = list(self.bundle.glob('verification/design-sessions/*/continuation-invalidated.json'))
        self.assertEqual(len(receipts), 1)
        record = json.loads(receipts[0].read_text())
        self.assertIn('model', record['changed'])
        self.assertEqual(record['remaining_steps'], 5, "the remaining budget carries over")
        self.assertFalse(record['budget_reset'])

    def test_qualification_repair_is_charged_before_a_transport_interruption(self):
        rejected = {'release_ready': False, 'status': 'blocked'}
        with patch.object(release, '_qualify', return_value=rejected), \
                patch('pipeline.repair.feedback', return_value=['checks.json chk_total: verified defect']), \
                patch('pipeline.repair.revise', side_effect=TimeoutError('interrupted')):
            with self.assertRaises(TimeoutError):
                release.qualify(self.bundle, ['one', 'two'], 'reviewer', 'solver', self.bundle / 'native', repair_attempts=1)
        self.assertEqual(checkpoint.read(self.bundle)['qualification_repairs_remaining'], 0)
        with patch.object(release, '_qualify', return_value=rejected), patch('pipeline.repair.revise') as revise:
            release.qualify(self.bundle, ['one', 'two'], 'reviewer', 'solver', self.bundle / 'native', repair_attempts=1)
            revise.assert_not_called()

    def test_interrupted_qualification_repair_resumes_before_remeasurement(self):
        checkpoint.update(self.bundle, phase='qualification-repair', stage='authoring',
                          qualification_repairs_remaining=0, feedback=['Retained defect'])
        events = []
        def finish(*args):
            events.append('resume')
            checkpoint.update(self.bundle, phase=None)
            return {'previous_sha256': 'old'}
        def measure(*args):
            events.append('measure')
            return {'release_ready': True, 'status': 'accepted'}
        with patch('pipeline.repair.revise', side_effect=finish) as revise, \
                patch.object(release, '_qualify', side_effect=measure):
            result = release.qualify(self.bundle, ['one', 'two'], 'reviewer', 'solver', self.bundle / 'native', repair_attempts=1)
        # The resumed revision is a new, unscreened version: it is screened
        # (5 x 2) before any calibration of record, never re-measured here.
        self.assertEqual(events, ['resume'])
        self.assertEqual(result['status'], 'awaiting-screen')
        self.assertEqual(len(result['qualification_revisions']), 1)
        self.assertEqual(checkpoint.read(self.bundle)['qualification_repairs_remaining'], 0)
