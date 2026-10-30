"""Classify a cut-off tool only after its authoritative message terminal."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline.author import transport as designer
from pipeline.calibrate import agent
from pipeline.tests import test_anthropic_transport as helpers
from pipeline.tests.test_anthropic_transport import response, stream


def truncated(reason='max_tokens'):
    events = response(reason, True)
    events[2]['delta']['partial_json'] = '{"path":"answer.txt","content":"unfinished'
    return events


class AnthropicCutoff(unittest.TestCase):
    def test_empty_delta_preserves_initial_zero_argument_object(self):
        for fragment in ('', '  \n\t'):
            events = response('tool_use', True)
            events[2]['delta']['partial_json'] = fragment
            self.assertEqual(designer._consume_stream(stream(events))['content'][0]['input'], {})

    def test_empty_fragments_do_not_change_real_input(self):
        events = response('tool_use', True)
        empty = copy.deepcopy(events[2]); empty['delta']['partial_json'] = ''
        events.insert(2, empty)
        events.insert(4, empty)
        result = designer._consume_stream(stream(events))
        self.assertEqual(result['content'][0]['input']['path'], 'answer.txt')

    def test_empty_delta_does_not_excuse_invalid_initial_input_type(self):
        for value in (None, [], 'invalid'):
            events = response('tool_use', True)
            events[1]['content_block']['input'] = value
            events[2]['delta']['partial_json'] = ''
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                designer._consume_stream(stream(events))

    def test_both_callers_accept_completed_zero_argument_tool_without_retry(self):
        events = response('tool_use', True)
        events[2]['delta']['partial_json'] = ''
        for caller in (designer, agent):
            stack, request, sleep = helpers.AnthropicTransport().call_patches(caller, [stream(events)])
            with self.subTest(caller=caller.__name__), stack:
                self.assertEqual(caller._call({})['content'][0]['input'], {})
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_cutoff_preserves_terminal_and_usage_without_executable_tools(self):
        observed = []
        result = designer._consume_stream(stream(truncated()), progress=lambda stage, **kw: observed.append((stage, kw)))
        self.assertEqual(result['stop_reason'], 'max_tokens')
        self.assertEqual(result['usage'], {'input_tokens': 7, 'output_tokens': 11})
        self.assertEqual(result['content'], [])
        self.assertEqual(observed[-1][0], 'terminal')
        self.assertEqual(observed[-1][1]['status'], 'max_tokens')
        self.assertNotIn('unfinished', json.dumps(observed))

    def test_cutoff_without_message_terminal_is_still_an_interrupted_stream(self):
        for cutoff in (3, 4, 5):
            with self.subTest(cutoff=cutoff), self.assertRaises(designer._StreamInterrupted):
                designer._consume_stream(stream(truncated()[:cutoff]))

    def test_non_cutoff_malformed_tool_remains_rejected(self):
        for reason in ('tool_use', 'end_turn', 'pause_turn'):
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeError, 'did not parse'):
                designer._consume_stream(stream(truncated(reason)))

    def test_malformed_terminal_diagnostics_do_not_include_tool_payload(self):
        observed = []
        with self.assertRaises(RuntimeError):
            designer._consume_stream(stream(truncated('tool_use')),
                progress=lambda stage, **kw: observed.append((stage, kw)))
        stage, row = observed[-1]
        self.assertEqual(stage, 'invalid-tool-input')
        self.assertEqual(row['status'], 'tool_use')
        self.assertGreater(row['characters'], 0)
        self.assertEqual(row['line'], 1)
        self.assertNotIn('answer.txt', json.dumps(observed))
        self.assertNotIn('unfinished', json.dumps(observed))

    def test_completed_earlier_calls_in_cutoff_batch_are_also_discarded(self):
        events = response('max_tokens', True)
        extra = copy.deepcopy(truncated()[1:4])
        for event in extra:
            event['index'] = 1
        extra[0]['content_block']['id'] = 'call-2'
        events[4:4] = extra
        self.assertEqual(designer._consume_stream(stream(events))['content'], [])

    def test_an_unclosed_block_at_max_tokens_is_the_cut_and_is_dropped(self):
        # 2026-10-30, reproduced against the API: a response cut at max_tokens ends with message_delta and
        # message_stop and never closes the block it cut. That is a complete response whose last block is cut,
        # not an interruption (reading it as one retried the same request until the design was lost).
        events = [event for event in truncated() if event['type'] != 'content_block_stop']
        message = designer._consume_stream(stream(events))
        self.assertEqual(message['stop_reason'], 'max_tokens')
        self.assertEqual(message['content'], [])
        other = [dict(e, delta={**e['delta'], 'stop_reason': 'tool_use'}) if e['type'] == 'message_delta' else e
                 for e in events]
        with self.assertRaises(designer._StreamInterrupted):
            designer._consume_stream(stream(other))

    def test_screening_records_cutoff_without_executing_or_retrying_it(self):
        helper = helpers.AnthropicTransport()
        stack, request, sleep = helper.call_patches(agent, [stream(truncated())])
        with stack, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'fixture'; fixture.mkdir()
            with patch.object(agent, '_run_tool', wraps=agent._run_tool) as run_tool:
                record, output = agent.rollout('Synthetic prompt', fixture, root / 'work',
                                               'synthetic', 'episode', max_steps=3)
            run_tool.assert_not_called()
            sleep.assert_not_called()
            self.assertEqual(request.call_count, 1)
            self.assertEqual(record.stop_reason, 'max_tokens')
            self.assertFalse((output / 'answer.txt').exists())

    def test_interrupted_malformed_tool_retries_then_executes_complete_call_once(self):
        helper = helpers.AnthropicTransport()
        stack, request, _ = helper.call_patches(agent, [stream(truncated()[:-1]),
            stream(response('tool_use', True)), stream(response())])
        with stack, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'fixture'; fixture.mkdir()
            with patch.object(agent, '_run_tool', wraps=agent._run_tool) as run_tool:
                record, output = agent.rollout('Synthetic prompt', fixture, root / 'work',
                                               'synthetic', 'episode', max_steps=3)
            self.assertEqual(run_tool.call_count, 1)
            self.assertEqual(record.stop_reason, 'end_turn')
            self.assertEqual(request.call_count, 3)
            self.assertTrue((output / 'answer.txt').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
