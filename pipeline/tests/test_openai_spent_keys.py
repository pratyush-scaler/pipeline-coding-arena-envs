"""A key the provider reports out of credit is failed over, never retried, and skipped by
every later selection (2026-10-05: one of three keys ran dry and, rotated in turn, failed
a third of all semantic reviews with `credit_balance_exhausted`)."""
import io
import json
import unittest
from unittest.mock import patch

from pipeline import openai_transport
from pipeline.calibrate import agent_openai
from pipeline.tests.openai_transport_fixture import isolated_openai_transport


def stream(*events):
    return io.BytesIO(b''.join(b'event: ' + event['type'].encode() + b'\r\n'
        + b'data: ' + json.dumps(event).encode() + b'\r\n\r\n' for event in events))


COMPLETED = {'type': 'response.completed', 'response': {'id': 'r', 'status': 'completed',
             'model': 'gpt-5.6', 'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}}
NO_CREDIT = {'type': 'error', 'error': {'code': 'credit_balance_exhausted'}}


class SpentKeys(unittest.TestCase):
    def setUp(self):
        self.transport = isolated_openai_transport(self)
        self.transport.keys.write_text('OPEN_AI_API_KEY1=k-one\nOPEN_AI_API_KEY2=k-two\nOPEN_AI_API_KEY3=k-three\n')
        self.used = []

        def urlopen(request, timeout=None):
            key = request.headers.get('Authorization', request.get_header('Authorization'))
            self.used.append(key)
            body = stream(NO_CREDIT) if key.endswith('k-two') else stream(COMPLETED)
            body.headers = {}
            body.__enter__ = lambda *a: body
            body.__exit__ = lambda *a: None
            return body

        class Context:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self.inner

            def __exit__(self, *exc):
                return False

        self.enterContext(patch.object(agent_openai.urllib.request, 'urlopen',
                                       side_effect=lambda request, timeout=None: Context(urlopen(request, timeout))))

    def pool(self):
        return openai_transport.SharedCooldown(self.transport.cooldown, clock=lambda: self.transport.clock.now)

    def test_an_unfunded_key_fails_over_and_is_marked_for_everyone(self):
        pool = self.pool()
        with pool.locked() as state:
            state['cursor'] = 1                      # the next pick is OPEN_AI_API_KEY2
        result = openai_transport.call({'model': 'gpt-5.6', 'input': 'x'}, 3, pool=pool,
                                       keys_path=self.transport.keys)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(self.used[0].endswith('k-two') and not self.used[-1].endswith('k-two'))
        picks = {pool.choose(openai_transport.credentials(self.transport.keys))[0].name for _ in range(6)}
        self.assertNotIn('OPEN_AI_API_KEY2', picks)

    def test_when_every_key_is_spent_the_call_says_so(self):
        pool = self.pool()
        for name in ('OPEN_AI_API_KEY1', 'OPEN_AI_API_KEY2', 'OPEN_AI_API_KEY3'):
            pool.mark_spent(name)
        with self.assertRaisesRegex(RuntimeError, 'no remaining credit'):
            pool.choose(openai_transport.credentials(self.transport.keys))

    def test_a_spent_mark_expires(self):
        pool = self.pool()
        pool.mark_spent('OPEN_AI_API_KEY2', hours=1)
        self.transport.clock.now += 3601
        names = {pool.choose(openai_transport.credentials(self.transport.keys))[0].name for _ in range(6)}
        self.assertIn('OPEN_AI_API_KEY2', names)


class SafetyRefusals(SpentKeys):
    """`cyber_policy` refused some semantic reviews (2026-10-05): retried, not fatal."""

    def test_a_refused_request_is_retried_and_can_succeed(self):
        calls = {'n': 0}
        refused = {'type': 'error', 'error': {'code': 'cyber_policy'}}

        def urlopen(request, timeout=None):
            calls['n'] += 1
            body = stream(refused) if calls['n'] == 1 else stream(COMPLETED)

            class Context:
                def __enter__(self_inner):
                    return body

                def __exit__(self_inner, *exc):
                    return False
            body.headers = {}
            return Context()

        with patch.object(agent_openai.urllib.request, 'urlopen', side_effect=urlopen):
            result = openai_transport.call({'model': 'gpt-5.6', 'input': 'x'}, 3, pool=self.pool(),
                                           keys_path=self.transport.keys)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(calls['n'], 2)


if __name__ == '__main__':
    unittest.main()
