"""SDK native Submit delivery keeps genuine failures/spend and refuses replay."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from pipeline.author.native_submission import NativeSubmissionCompleted, call_with_retained_submit, retain_submission

class DurableNativeSubmit(unittest.TestCase):
    def fixture(self,root):
        self.draft={'suites':{'observer.py':'assert actual == expected\n'},'steps':[{'name':'checkpoint'}]}
        self.event={'type':'tool_use','id':'native-1-2','name':'submit_draft','input':{}}
        self.handlers={'draft_spec':lambda:deepcopy(self.draft),'submit_check':lambda draft:{'clean':True,'validation_deferred':True}}
        retain_submission(root,self.event,session_id='retained-SID',before=278,total_cap=285,accepted_calls=2,
                          draft=self.draft,submit_check=self.handlers['submit_check'])
        receipt={'transport':'official-claude-cli-real-pipeline-mcp-tools','exit_code':1,'is_error':True,
          'native_turns':4,'native_turns_before_call':278,'native_total_cap':285,
          'actual_lifetime_native_turns':282,'native_budget_overrun':0,'future_scope_budget_overrun':0,
          'maximum_this_invocation_official_turns':4,'authoritative_submission_retained':True,
          'native_session_id':'retained-SID','usage':{'output_tokens':321}}
        tools=[{'id':'native-1-1','name':'stage_spec','native_budget_accepted':True,'arguments':{},'result':{'isError':False}},
          {'id':self.event['id'],'name':'submit_draft','native_budget_accepted':True,'arguments':{},
           'result':{'isError':False,'content':[{'type':'text','text':json.dumps({'submitted':True})}]}}]
        terminal={'type':'result','subtype':'error_max_turns','is_error':True,'num_turns':4,'session_id':'retained-SID'}
        for name,value in [('receipt.json',receipt),('tools.json',tools),('stdout.json',terminal)]:
            (root/name).write_text(json.dumps(value)+'\n')
        return receipt,tools,terminal
    def test_genuine_checked_event_delivered_once_and_raw_failure_and_spend_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();receipt,tools,terminal=self.fixture(root)
            before={p.name:p.read_bytes() for p in root.iterdir()}
            calls=[]
            def provider(body):calls.append(body);raise NativeSubmissionCompleted(root)
            body={'model':'unchanged-model','messages':[{'role':'user','content':'unchanged input'}]}
            response=call_with_retained_submit(provider,body,self.handlers)
            self.assertEqual(calls,[body]);self.assertEqual(response['content'],[self.event])
            self.assertEqual(response['usage'],receipt['usage'])
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir() if p.name in before})
            claim=json.loads((root/'submission-delivered.json').read_text())
            self.assertEqual((claim['original_exit_code'],claim['original_is_error'],claim['actual_lifetime_native_turns']),(1,True,282))
            with self.assertRaises(FileExistsError):NativeSubmissionCompleted(root).response(self.handlers)
    def test_fresh_session_binds_genuine_terminal_and_cannot_reset_retained_spend(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();receipt,tools,terminal=self.fixture(root)
            pending=json.loads((root/'pending-submission.json').read_text())
            pending.update(native_session_id=None,native_turns_before_call=0,original_native_total_cap=4)
            receipt.update(native_turns_before_call=0,native_total_cap=4,actual_lifetime_native_turns=4,resumed=False)
            (root/'pending-submission.json').write_text(json.dumps(pending));(root/'receipt.json').write_text(json.dumps(receipt))
            self.assertEqual(NativeSubmissionCompleted(root).response(self.handlers)['content'],[self.event])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();self.fixture(root)
            pending=json.loads((root/'pending-submission.json').read_text());pending['native_session_id']=None
            (root/'pending-submission.json').write_text(json.dumps(pending))
            with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)
    def test_unchecked_rejected_capture_never_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            with self.assertRaises(RuntimeError):
                retain_submission(root,{'type':'tool_use','name':'submit_draft','id':'native-1-1','input':{}},
                  session_id='SID',before=0,total_cap=4,accepted_calls=1,draft={},submit_check=lambda _: {'clean':False})
            self.assertFalse((root/'pending-submission.json').exists())
    def test_duplicate_capture_refused_without_overwriting_first_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();self.fixture(root);before=(root/'pending-submission.json').read_bytes()
            with self.assertRaises(FileExistsError):
                retain_submission(root,self.event,session_id='retained-SID',before=278,total_cap=285,accepted_calls=2,
                                  draft=self.draft,submit_check=self.handlers['submit_check'])
            self.assertEqual(before,(root/'pending-submission.json').read_bytes())
    def test_missing_changed_or_unchecked_pending_refused_before_delivery(self):
        for key,value in [('schema','arena-native/retained-authoritative-submission/1'),('sdk_submit_checked',False),
                          ('draft_spec_sha256','changed'),('submit_check',{'clean':False}),('source','invented')]:
            with self.subTest(key=key),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();self.fixture(root);p=root/'pending-submission.json';v=json.loads(p.read_text());v[key]=value;p.write_text(json.dumps(v))
                with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)
                self.assertFalse((root/'submission-delivered.json').exists())
    def test_draft_changed_after_genuine_Submit_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();self.fixture(root);self.draft['steps'][0]['name']='changed'
            with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)
            self.assertFalse((root/'submission-delivered.json').exists())
    def test_provider_other_failures_missing_terminal_and_overruns_refused(self):
        for key,value in [('native_turns',False),('native_total_cap',281),('native_budget_overrun',1),
          ('future_scope_budget_overrun',1),('maximum_this_invocation_official_turns',3),('exit_code',0),
          ('is_error',False),('native_session_id','different'),('actual_lifetime_native_turns',281)]:
            with self.subTest(key=key),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();receipt,_,_=self.fixture(root);receipt[key]=value;(root/'receipt.json').write_text(json.dumps(receipt))
                with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)
                self.assertFalse((root/'submission-delivered.json').exists())
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();self.fixture(root);(root/'stdout.json').write_text('')
            with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)
    def test_false_or_duplicate_native_acknowledgements_refused(self):
        for kind in ['unchecked','failed','duplicate','not-submitted']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();_,tools,_=self.fixture(root)
                if kind=='unchecked':tools[-1]['native_budget_accepted']=False
                elif kind=='failed':tools[-1]['result']['isError']=True
                elif kind=='duplicate':tools.append(deepcopy(tools[-1]))
                else:tools[-1]['result']['content'][0]['text']=json.dumps({'submitted':False})
                (root/'tools.json').write_text(json.dumps(tools))
                with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)
                self.assertFalse((root/'submission-delivered.json').exists())
    def test_current_SDK_rejection_cannot_create_delivery_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();self.fixture(root);self.handlers['submit_check']=lambda _: {'clean':False}
            with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)
            self.assertFalse((root/'submission-delivered.json').exists())
    def test_ordinary_provider_success_and_error_unchanged(self):
        body={'model':'model','messages':['full unchanged input']};response={'content':[]};calls=[]
        def provider(value):calls.append(value);return response
        self.assertIs(call_with_retained_submit(provider,body,{}),response);self.assertEqual(calls,[body])
        def fail(_):raise ValueError('provider failure')
        with self.assertRaisesRegex(ValueError,'provider failure'):call_with_retained_submit(fail,body,{})
    def test_symlink_evidence_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();self.fixture(root);p=root/'receipt.json';data=p.read_bytes();p.unlink();other=root/'other.json';other.write_bytes(data);p.symlink_to(other)
            with self.assertRaises(RuntimeError):NativeSubmissionCompleted(root).response(self.handlers)

if __name__=='__main__':unittest.main()
