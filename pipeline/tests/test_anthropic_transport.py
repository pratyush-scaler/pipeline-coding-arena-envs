"""Protocol truncation, safe retry and no partial-tool execution regressions."""
from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from pipeline import provider
from pipeline.author import transport as designer
from pipeline.calibrate import agent


def stream(events):
    return io.BytesIO(''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode())


def response(reason='end_turn', tool=False):
    events = [{'type':'message_start', 'message':{
        'id':'synthetic', 'model':'claude-opus-5', 'content':[], 'usage':{'input_tokens':7}}}]
    if tool:
        block = {'type':'tool_use', 'id':'call-1', 'name':'write_file', 'input':{}}
        delta = {'type':'input_json_delta', 'partial_json':json.dumps({'path':'answer.txt','content':'private tool material'})}
    else:
        block = {'type':'text', 'text':''}
        delta = {'type':'text_delta', 'text':'done'}
    events += [{'type':'content_block_start','index':0,'content_block':block},
               {'type':'content_block_delta','index':0,'delta':delta},
               {'type':'content_block_stop','index':0},
               {'type':'message_delta','delta':{'stop_reason':reason},'usage':{'output_tokens':11}},
               {'type':'message_stop'}]
    return events


def http_error(code, headers=None):
    return urllib.error.HTTPError('https://provider.invalid', code, 'error', headers or {},
        io.BytesIO(b'{"error":{"message":"private provider material"}}'))


class AnthropicTransport(unittest.TestCase):
    def test_only_terminal_messages_return_and_preserve_usage(self):
        events = response('tool_use', True)
        result = designer._consume_stream(stream(events))
        self.assertEqual(result['usage'], {'input_tokens':7,'output_tokens':11})
        self.assertEqual(result['content'][0]['input']['path'], 'answer.txt')
        for cutoff in range(len(events)):
            with self.subTest(cutoff=cutoff), self.assertRaises((ConnectionError, RuntimeError)):
                designer._consume_stream(stream(events[:cutoff]))

    def test_terminal_cannot_hide_unclosed_or_missing_blocks(self):
        events = response('tool_use', True)
        variants = [[e for e in events if e['type'] != 'content_block_stop'],
                    [e for e in events if e['type'] != 'message_start'],
                    [e for e in events if e['type'] != 'message_delta']]
        for changed in variants:
            with self.assertRaises((RuntimeError, ConnectionError)):
                designer._consume_stream(stream(changed))

    def test_invalid_tool_json_and_duplicate_blocks_are_rejected(self):
        events = response('tool_use', True)
        events[2]['delta']['partial_json'] = '[]'
        with self.assertRaises(RuntimeError):
            designer._consume_stream(stream(events))
        events = response()
        events.insert(2, events[1])
        with self.assertRaises(RuntimeError):
            designer._consume_stream(stream(events))
        with self.assertRaises(RuntimeError):
            designer._consume_stream(io.BytesIO(b'data: {private malformed JSON\n\n'))

    def test_unknown_events_and_reasoning_are_preserved_safely(self):
        events = response()
        events.insert(1, {'type':'future_protocol_event','unused':'private ignored data'})
        events[2]['content_block'] = {'type':'thinking','thinking':''}
        events[3]['delta'] = {'type':'thinking_delta','thinking':'private reasoning'}
        events.insert(4, {'type':'content_block_delta','index':0,
                          'delta':{'type':'signature_delta','signature':'signed'}})
        result = designer._consume_stream(stream(events))
        self.assertEqual(result['content'][0], {
            'type':'thinking','thinking':'private reasoning','signature':'signed'})

    def test_stream_error_does_not_echo_provider_prose(self):
        for code, expected in [('overloaded_error', ConnectionError), ('invalid_request_error', RuntimeError)]:
            with self.assertRaises(expected) as error:
                designer._consume_stream(stream([{'type':'error','error':{
                    'type':code,'message':'private provider material'}}]))
            self.assertNotIn('private', str(error.exception))

    def test_an_unclassified_stream_error_is_final_here_and_retried_by_the_designer(self):
        # 2026-10-03: an unlisted mid-stream error type ended a 72-response design.
        # transport.py is part of artifacts.harness_identity, so the retry lives in
        # agent_designer._designer_call (test_throughput_fixes); here it stays final.
        for code in ('timeout_error', None, 'request_too_large'):
            with self.assertRaises(RuntimeError, msg=code) as error:
                designer._consume_stream(stream([{'type':'error','error':{'type':code,'message':'private'}}]))
            self.assertNotIn('private', str(error.exception))

    def call_patches(self, caller, responses):
        stack = ExitStack()
        stack.enter_context(patch.object(caller, '_auth_headers', return_value={}))
        stack.enter_context(patch.object(caller, 'request_slot', side_effect=nullcontext))
        request = stack.enter_context(patch.object(caller.urllib.request, 'urlopen', side_effect=responses))
        sleep = stack.enter_context(patch.object(caller.time, 'sleep'))
        return stack, request, sleep

    def test_both_callers_retry_transient_failures_with_identical_requests(self):
        for caller in [designer, agent]:
            for code in [408,409,429,500,504,524,529]:
                stack, request, sleep = self.call_patches(caller, [http_error(code),stream(response())])
                with self.subTest(caller=caller.__name__,code=code), stack:
                    self.assertEqual(caller._call({'model':'synthetic'})['stop_reason'], 'end_turn')
                    self.assertEqual(request.call_count,2)
                    self.assertEqual(request.call_args_list[0].args[0].data, request.call_args_list[1].args[0].data)
                    self.assertEqual(sleep.call_count,1)

    def test_both_callers_fail_fast_without_leaking_auth_or_bad_request_errors(self):
        for caller in [designer, agent]:
            for code in [400,401,403]:
                stack, request, sleep = self.call_patches(caller,[http_error(code)])
                with self.subTest(caller=caller.__name__,code=code), stack:
                    with self.assertRaises(RuntimeError) as error:caller._call({})
                    self.assertNotIn('private',str(error.exception))
                    self.assertEqual(request.call_count,1)
                    sleep.assert_not_called()

    def test_retry_after_is_honored_and_cannot_extend_author_deadline(self):
        with patch.object(provider.random,'random',return_value=0):
            self.assertEqual(provider._retry_delay({'Retry-After':'45'},0),45)
            date = format_datetime(datetime.now(timezone.utc)+timedelta(seconds=60))
            self.assertGreater(provider._retry_delay({'Retry-After':date},0),58)
            self.assertEqual(provider._retry_delay({'Retry-After':'nan'},0),1)
        # An absurd Retry-After used to be stopped by the author call deadline,
        # which no longer exists. The bound now lives on the sleep itself, which
        # is the right place: waiting is capped, working is not. Eleven days of
        # sleep becomes two minutes, and the call proceeds to its next attempt.
        self.assertEqual(provider._retry_delay({'Retry-After':'999999'},0),
                         provider.MAX_RETRY_SLEEP_S)
        stack, request, sleep = self.call_patches(
            designer,[http_error(503,{'Retry-After':'999999'}),http_error(503,{})])
        with stack, self.assertRaises(RuntimeError):designer._call({},retries=2)
        self.assertEqual(request.call_count,2)
        self.assertEqual(sleep.call_count,1)
        self.assertLessEqual(sleep.call_args[0][0],provider.MAX_RETRY_SLEEP_S)

    def test_exhaustion_is_bounded_and_never_sleeps_after_last_try(self):
        for caller in [designer,agent]:
            stack, request, sleep = self.call_patches(caller,[http_error(524),http_error(524)])
            with stack, self.assertRaises(RuntimeError) as error:caller._call({},retries=2)
            self.assertNotIn('private',str(error.exception))
            self.assertEqual(request.call_count,2)
            self.assertEqual(sleep.call_count,1)

    def test_incomplete_stream_retry_executes_the_tool_exactly_once(self):
        stack, request, sleep = self.call_patches(agent,[stream(response('tool_use',True)[:-1]),
                                                       stream(response('tool_use',True)),stream(response())])
        with stack, tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture=root/'fixture';fixture.mkdir()
            with patch.object(agent,'_run_tool',wraps=agent._run_tool) as run_tool:
                record, output=agent.rollout('Synthetic prompt',fixture,root/'work','synthetic','episode',max_steps=3)
            self.assertEqual(run_tool.call_count,1)
            self.assertEqual(record.tool_calls,1)
            self.assertEqual(record.stop_reason,'end_turn')
            self.assertTrue((output/'answer.txt').exists())
            self.assertEqual(request.call_count,3)

    def test_truncated_terminal_response_cannot_execute_tools(self):
        stack, request, sleep = self.call_patches(agent,[stream(response('max_tokens',True))])
        with stack, tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture=root/'fixture';fixture.mkdir()
            with patch.object(agent,'_run_tool') as run_tool:
                record, output=agent.rollout('Synthetic prompt',fixture,root/'work','synthetic','episode',max_steps=3)
            run_tool.assert_not_called()
            self.assertEqual(record.stop_reason,'max_tokens')
            self.assertEqual(record.tool_calls,0)
            self.assertFalse((output/'answer.txt').exists())

    def test_telemetry_contains_only_protocol_metadata_and_failure_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'observations.jsonl'
            stack, request, sleep = self.call_patches(agent,[stream(response('tool_use',True))])
            with stack, patch.dict(os.environ,{'ARENA_TRANSPORT_OBSERVATIONS':str(path)}):
                agent._call({'messages':'private prompt material'})
            text=path.read_text();self.assertNotIn('private',text)
            self.assertNotIn('answer.txt',text)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(json.loads(text.splitlines()[-1])['stage'],'terminal')
            stack, request, sleep = self.call_patches(agent,[stream(response('private unexpected status'))])
            with stack, patch.dict(os.environ,{'ARENA_TRANSPORT_OBSERVATIONS':str(path)}):
                agent._call({})
            self.assertNotIn('private',path.read_text())
            self.assertEqual(json.loads(path.read_text().splitlines()[-1])['status'],'unknown')
            with patch.dict(os.environ,{'ARENA_TRANSPORT_OBSERVATIONS':directory}):
                provider._transport_observer()('observation-failure')


if __name__ == '__main__':
    unittest.main()
