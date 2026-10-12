"""Retry transient transport failures without leaking payloads or duplicating tools."""
import contextlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from pipeline.calibrate import agent_openai as agent
from pipeline.tests.test_openai_transport import stream, terminal
from pipeline.tests.openai_transport_fixture import isolated_openai_transport


def http_error(status, code=None, headers=None):
    return HTTPError(agent.API_URL, status, 'provider failure', headers or {},
        io.BytesIO(json.dumps({'error': {'code': code,
            'message': 'private prompt fragment must never enter a receipt'}}).encode()))


class OpenAIHTTPRetry(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(agent.os.environ, {'OPENAI_API_KEY': 'test-only'}))
        self.enterContext(patch.object(agent, 'request_slot', contextlib.nullcontext))
        self.transport = isolated_openai_transport(self)
        self.sleep = self.transport.sleep
        self.enterContext(patch.object(agent.random, 'random', return_value=0))

    def test_gateway_timeouts_retry_the_identical_request(self):
        body = {'model': 'gpt-5.6-sol', 'input': [], 'max_output_tokens': 256000}
        for status in (408, 409, 429, 500, 503, 504, 524, 529):
            with self.subTest(status=status), patch.object(agent.urllib.request, 'urlopen',
                    side_effect=[http_error(status), stream(terminal())]) as request:
                self.assertEqual(agent._call(body, retries=2), terminal()['response'])
                self.assertEqual(request.call_count, 2)
                first, second = [call.args[0].data for call in request.call_args_list]
                self.assertEqual(first, second)
                self.assertEqual(json.loads(first), {**body, 'stream': True})

    def test_auth_bad_requests_and_exhausted_quota_fail_without_retry_or_payload(self):
        for status, code in [(400, None), (401, None), (403, None),
                             (429, 'insufficient_quota'),
                             (429, 'project_spend_limit_exceeded')]:
            self.sleep.reset_mock()
            with self.subTest(status=status, code=code), patch.object(agent.urllib.request,
                    'urlopen', side_effect=http_error(status, code)) as request:
                with self.assertRaises(RuntimeError) as caught:
                    agent._call({'model': 'gpt-5.6-sol'}, retries=3)
                self.assertNotIn('private prompt', str(caught.exception))
                self.assertIn(f'HTTP {status}', str(caught.exception))
                self.assertEqual(request.call_count, 1)
                self.sleep.assert_not_called()

    def test_retry_after_seconds_and_date_are_respected(self):
        with patch.object(agent.urllib.request, 'urlopen', side_effect=[
                http_error(503, headers={'Retry-After': '45'}), stream(terminal())]):
            agent._call({'model': 'gpt-5.6-sol'}, retries=2)
        self.sleep.assert_called_once_with(45)
        future = datetime.now(timezone.utc) + timedelta(seconds=45)
        delay = agent._retry_delay({'Retry-After': format_datetime(future)}, 0)
        self.assertGreaterEqual(delay, 43)
        self.assertLessEqual(delay, 45)
        for invalid in ('bad date', 'NaN', 'Infinity', '-12'):
            self.assertEqual(agent._retry_delay({'Retry-After': invalid}, 0), 1)

    def test_exhaustion_is_bounded_and_does_not_sleep_after_the_final_request(self):
        with patch.object(agent.urllib.request, 'urlopen', side_effect=[
                http_error(524), http_error(524)]) as request:
            with self.assertRaisesRegex(RuntimeError, 'failed after 2 attempts: HTTP 524') as caught:
                agent._call({'model': 'gpt-5.6-sol'}, retries=2)
        self.assertNotIn('private prompt', str(caught.exception))
        self.assertEqual(request.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_retry_after_partial_stream_executes_only_the_terminal_tool_once(self):
        partial = {'type': 'response.output_item.done', 'item': {
            'type': 'function_call', 'call_id': 'partial', 'name': 'write_file',
            'arguments': json.dumps({'path': 'partial.json', 'content': '{}'})}}
        output = [{'type': 'function_call', 'call_id': 'valid', 'name': 'write_file',
                   'arguments': json.dumps({'path': 'answer.json', 'content': '{"ok":true}'})}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / 'fixture'
            fixture.mkdir()
            with patch.object(agent.urllib.request, 'urlopen', side_effect=[
                    stream(partial), http_error(524), stream(terminal(output=output)),
                    stream(terminal())]) as request:
                record, submitted = agent.rollout('test', fixture, root / 'attempt',
                    'gpt-5.6-sol', 'retry-test', max_steps=3)
            self.assertEqual(record.stop_reason, 'completed')
            self.assertEqual(record.tool_calls, 1)
            self.assertEqual(sorted(p.name for p in submitted.iterdir()), ['answer.json'])
            self.assertEqual(request.call_count, 4)
            items = json.loads((root / 'attempt/conversation.json').read_text())
            self.assertFalse(any(item.get('call_id') == 'partial' for item in items))

    def test_optional_observation_records_protocol_progress_without_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'transport.jsonl'
            with patch.dict(agent.os.environ, {'ARENA_TRANSPORT_OBSERVATIONS': str(log)}), \
                    patch.object(agent.urllib.request, 'urlopen', side_effect=[
                        http_error(524), stream(
                            {'type': 'response.created', 'message': 'private event text'},
                            terminal())]):
                agent._call({'model': 'gpt-5.6-sol', 'input': 'private input text'}, retries=2)
            text = log.read_text()
            self.assertNotIn('private', text)
            self.assertNotIn('test-only', text)
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(len({r['request_id'] for r in rows}), 1)
            self.assertEqual([r['stage'] for r in rows], ['request-start', 'http-error',
                'retry-wait', 'request-start', 'response-open', 'stream-active', 'terminal'])
            self.assertEqual(rows[-1]['status'], 'completed')
            self.assertEqual(rows[-1]['events'], 2)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_observation_write_failure_does_not_change_a_successful_response(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(agent.os.environ, {'ARENA_TRANSPORT_OBSERVATIONS': tmp}), \
                patch.object(agent.urllib.request, 'urlopen', return_value=stream(terminal())):
            self.assertEqual(agent._call({'model': 'gpt-5.6-sol'}), terminal()['response'])


if __name__ == '__main__':
    unittest.main()
