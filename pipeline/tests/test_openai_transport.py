"""Long Responses calls must stream, and partial output must never execute."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.calibrate import agent_openai as agent
from pipeline.calibrate.run import classify_zero
from pipeline.tests.openai_transport_fixture import isolated_openai_transport


def stream(*events):
    return io.BytesIO(b''.join(b'event: ' + event['type'].encode() + b'\r\n'
        + b'data: ' + json.dumps(event, ensure_ascii=False).encode() + b'\r\n\r\n'
        for event in events))


def terminal(status='completed', output=None):
    return {'type':'response.'+status, 'response':{'id':'resp_test','status':status,
        'model':'gpt-5.6-sol','output':output or [],'usage':{'input_tokens':9,'output_tokens':12},
        'incomplete_details':{'reason':'max_output_tokens'} if status=='incomplete' else None}}


class OpenAIStreaming(unittest.TestCase):
    def setUp(self):
        self.transport = isolated_openai_transport(self)

    def test_terminal_object_preserves_reasoning_tools_usage_and_unicode(self):
        output = [{'type':'reasoning','id':'reason_1','encrypted_content':'retained-opaque-data'},
            {'type':'function_call','call_id':'call_1','name':'write_file',
             'arguments':json.dumps({'path':'answer.json','content':'{"label":"測定"}'},ensure_ascii=False)}]
        final = terminal(output=output)
        result = agent._consume_stream(stream(
            {'type':'response.created','response':{'status':'in_progress'}},
            {'type':'response.function_call_arguments.delta','delta':'incomplete fragment'},final))
        self.assertEqual(result,final['response'])

    def test_sse_multiline_data_and_keepalive_comments(self):
        body = json.dumps(terminal(),indent=2)
        lines = b': keepalive\r\nid: 4\r\n' + b''.join(b'data: '+line.encode()+b'\r\n'
            for line in body.splitlines()) + b'\r\n'
        self.assertEqual(agent._consume_stream(io.BytesIO(lines))['status'],'completed')

    def test_disconnect_or_done_without_terminal_rejects_partial_tools(self):
        partial = {'type':'response.output_item.done','item':{'type':'function_call',
            'name':'write_file','call_id':'partial','arguments':'{}'}}
        for ending in (b'',b'data: [DONE]\n\n'):
            with self.subTest(ending=ending), self.assertRaises(agent._StreamInterrupted):
                agent._consume_stream(io.BytesIO(stream(partial).getvalue()+ending))

    def test_malformed_events_and_inconsistent_terminal_status_fail_closed(self):
        invalid = [b'data: invalid-json\n\n',b'data: []\n\n',
            stream({'type':'response.completed','response':{'status':'in_progress','output':[]}}).getvalue(),
            stream({'type':'response.completed','response':{'status':'completed'}}).getvalue()]
        for body in invalid:
            with self.subTest(body=body),self.assertRaises(RuntimeError):
                agent._consume_stream(io.BytesIO(body))

    def test_provider_error_does_not_echo_private_prompt(self):
        with self.assertRaisesRegex(RuntimeError,'provider error') as caught:
            agent._consume_stream(stream({'type':'error','message':'private prompt fragment'}))
        self.assertNotIn('private prompt fragment',str(caught.exception))

    def test_provider_error_surfaces_the_code_but_never_prose(self):
        """A void must be classifiable, so the enum-like code is surfaced.

        Without it, `provider error` covers both a rate limit (infra loss, which
        must be re-run) and a context-length rejection (a real property of the
        task), and the two are indistinguishable in a receipt.
        """
        event = {'type':'error','error':{'code':'context_length_exceeded',
                                         'message':'private prompt fragment'}}
        with self.assertRaises(RuntimeError) as caught:
            agent._consume_stream(stream(event))
        self.assertIn('context_length_exceeded',str(caught.exception))
        self.assertNotIn('private prompt fragment',str(caught.exception))

    def test_provider_error_code_shaped_like_prose_is_withheld(self):
        """Only constant-shaped codes are echoed, so prose cannot ride in `code`.

        A value that is not constant-shaped is never echoed. The envelope's
        FIELD NAMES still are, because a void with no descriptor at all is
        indistinguishable from a result -- the failure this suite exists to
        prevent. `no-code(code)` says the provider sent a `code` we could not
        trust, which is a fact about the wire format, not about the prompt.
        """
        for bad in ('Your prompt about Acme Corp was too long',
                    'ERROR: leaked', '', 'error', 42, None, {'a':1}):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError) as caught:
                agent._consume_stream(stream({'type':'error','error':{'code':bad}}))
            self.assertEqual(str(caught.exception),
                             'OpenAI stream reported a provider error [no-code(code)]')
            self.assertNotIn('Acme', str(caught.exception))
            self.assertNotIn('leaked', str(caught.exception))

    def test_wire_request_streams_and_respects_shared_timeout(self):
        body = {'model':'gpt-5.6-sol','input':[],'reasoning':{'effort':'xhigh'},'max_output_tokens':256000}
        with patch.dict(agent.os.environ,{'OPENAI_API_KEY':'test-only-not-a-real-key'}), \
                patch.object(agent,'request_slot',contextlib.nullcontext), \
                patch.object(agent,'ROLLOUT_READ_TIMEOUT_S',None), \
                patch.object(agent.urllib.request,'urlopen',return_value=stream(terminal())) as request:
            agent._call(body)
        args,kwargs = request.call_args
        self.assertEqual(json.loads(args[0].data),{**body,'stream':True})
        self.assertIsNone(kwargs['timeout'])
        self.assertNotIn('stream',body)

    def test_interrupted_stream_retry_uses_only_the_complete_response(self):
        with patch.dict(agent.os.environ,{'OPENAI_API_KEY':'test-only-not-a-real-key'}), \
                patch.object(agent,'request_slot',contextlib.nullcontext), \
                patch.object(agent.time,'sleep'), \
                patch.object(agent.urllib.request,'urlopen',side_effect=[stream(
                    {'type':'response.output_item.added','item':{'type':'function_call'}}),
                    stream(terminal())]) as request:
            self.assertEqual(agent._call({'model':'gpt-5.6-sol'},retries=2),terminal()['response'])
            self.assertEqual(request.call_count,2)

    def test_incomplete_failed_and_cancelled_tools_are_never_executed(self):
        partial = [{'type':'function_call','call_id':'partial','name':'write_file',
                    'arguments':json.dumps({'path':'answer.json','content':'{"guess":true}'})}]
        for status in ('incomplete','failed','cancelled'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);fixture = root/'fixture';fixture.mkdir()
                response = agent._consume_stream(stream(terminal(status,partial)))
                with patch.object(agent,'_call',return_value=response), \
                        patch.object(agent,'_run_tool',side_effect=AssertionError('partial tool executed')) as tool:
                    record,submitted = agent.rollout('test',fixture,root/'attempt','gpt-5.6-sol','test',max_steps=2)
                tool.assert_not_called()
                self.assertFalse(list(submitted.iterdir()))
                self.assertEqual(record.stop_reason,'max_tokens' if status=='incomplete' else status)
                self.assertEqual(classify_zero(record),'void')

    def test_completed_tool_call_executes_once_and_reasoning_is_replayed(self):
        output = [{'type':'reasoning','id':'reason_1','encrypted_content':'opaque'},
            {'type':'function_call','call_id':'call_1','name':'write_file',
             'arguments':json.dumps({'path':'answer.json','content':'{"ok":true}'})}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);fixture=root/'fixture';fixture.mkdir()
            calls=[]
            def call(body):
                calls.append(json.loads(json.dumps(body)))
                return terminal(output=output if len(calls)==1 else [])['response']
            with patch.object(agent,'_call',side_effect=call):
                record,submitted=agent.rollout('test',fixture,root/'attempt','gpt-5.6-sol','test',max_steps=3)
            self.assertEqual(record.tool_calls,1)
            self.assertEqual(json.loads((submitted/'answer.json').read_text()),{'ok':True})
            self.assertIn(output[0],calls[1]['input'])
            self.assertEqual(sum(item.get('type')=='function_call_output' for item in calls[1]['input']),1)


if __name__ == '__main__':
    unittest.main()
