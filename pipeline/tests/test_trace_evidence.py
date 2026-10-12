"""Cross-channel recovery, author visibility and selection must agree on evidence."""
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pipeline.dump import recover, index
from pipeline.dump.evidence import Catalog, SCHEMA, source_document, tool_content
from pipeline.dump.formats import inspect_body
from pipeline.author import extract, agent_designer
designer = agent_designer
from pipeline.ledger import faithfulness
from pipeline.select import classify, score


class TraceEvidence(unittest.TestCase):
    def test_scalar_draft_fields_are_serialized_without_image_membership_checks(self):
        for value in (0, 17, 1.25, False, None, 'image_content', [], [1, False], {'weight': 17}):
            with self.subTest(value=value):
                self.assertEqual(json.loads(tool_content(value)), value)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'sessions'; self.folder = self.root / 's'
        (self.folder/'files').mkdir(parents=True)
        self.trace = {'session_id':'s','model_name':'model','messages':[],
                      'tasks':[{'task_index':0,'task_key':'k','n_turns':4,'turn_ids':['n0','n1','n2','n3']}]}
        self.bundle = self.root.parent/'bundle'
        (self.bundle/'environment/repo').mkdir(parents=True)

    def message(self, role, *blocks, **fields):
        i=len(self.trace['messages'])
        self.trace['messages'].append(dict(role=role,node_ids=[f'n{i}'],content=list(blocks),**fields))
        return i

    def save(self):
        (self.folder/'data.json').write_text(json.dumps({'traces':[self.trace]}))
        (self.folder/'outcomes.json').write_text('{"outcomes":[]}')

    def blob(self, raw):
        h=base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
        ref='files/'+h;(self.folder/ref).write_bytes(raw);return ref

    def catalog(self, pivot=None):
        self.save();ws=recover.recover(self.trace,self.root)
        return Catalog(self.trace,ws,self.root,pivot=pivot)

    def test_user_requests_and_structured_answers_are_not_limited_to_corrections(self):
        self.message('user',{'type':'text','text':'Keep the reserve at 20.'})
        self.message('assistant',{'type':'tool-ask_user','questions':[{'question':'Which edition?',
            'options':[{'label':'B'}],'answer':{'selected_option':'B','custom_response':None}}]})
        c=self.catalog()
        self.assertEqual(c.read('m0-b0')['text'],'Keep the reserve at 20.')
        self.assertTrue(c.records['m1-b0']['answered'])
        self.assertTrue(c.records['m1-b0']['fixture_eligible'])
        self.assertIn('Which edition?',c.read('m1-b0')['text'])

    def test_unanswered_and_skipped_questions_are_not_rules(self):
        self.message('assistant',{'type':'tool-ask_user','skipped':True,'questions':[{'question':'Q','answer':{'selected_option':'A'}}]},
                     {'type':'tool-ask_user','questions':[{'question':'Q'}]})
        c=self.catalog()
        self.assertFalse(c.records['m0-b0']['fixture_eligible'])
        self.assertFalse(c.records['m0-b1']['fixture_eligible'])

    def test_reasoning_and_assistant_assertions_never_become_fixtures(self):
        self.message('assistant',{'type':'reasoning','text':'PRIVATE_REASONING_SENTINEL'}, {'type':'text','text':'The answer is 42.'})
        c=self.catalog()
        self.assertNotIn('PRIVATE_REASONING_SENTINEL',json.dumps(c.list()))
        self.assertNotIn('PRIVATE_REASONING_SENTINEL',agent_designer._render_message(self.trace['messages'][0]))
        with self.assertRaises(ValueError):c.read('m0-b0')
        c.read('m0-b1')
        with self.assertRaises(ValueError):c.stage(self.bundle,'m0-b1','sources/answer.json','This is claimed historical evidence.')

    def test_fetch_chunks_preserve_url_chunk_metadata_and_partiality(self):
        for chunk in (0,2):
            self.message('assistant',{'type':'tool-fetch_page','input':{'url':'https://example.org/source'},
                'output':{'content':f'chunk {chunk}','title':'Report','url':'https://example.org/source',
                          'chunkIndex':chunk,'totalChunks':3,'hasMore':chunk<2},'state':'output-available'})
        c=self.catalog();v=c.read('m1-b0')
        self.assertFalse(v['complete']);self.assertEqual(v['chunk_index'],2)
        self.assertEqual(v['text'],'chunk 2')
        # The first refusal names the whole path rule, not one clause of it.
        for bad in ('page.txt','sources/page.txt'):
            with self.assertRaises(ValueError) as refused:
                c.stage(self.bundle,'m1-b0',bad,'This captured chunk supplies the published threshold.')
            self.assertIn('below sources/',str(refused.exception));self.assertIn('ending in .json',str(refused.exception))
            self.assertIn(repr(bad),str(refused.exception))
        c.stage(self.bundle,'m1-b0','sources/page.json','This captured chunk supplies the published threshold.')
        public=json.loads((self.bundle/'environment/repo/sources/page.json').read_text())
        self.assertFalse(public['complete']);self.assertEqual(public['total_chunks'],3)
        ledger=faithfulness.build(self.bundle,'s',root=self.root)
        self.assertEqual(ledger['entries'][0]['origin'],'lifted')

    def test_shell_observations_keep_failure_truncation_and_command_without_inventing_file(self):
        self.message('assistant',{'type':'tool-bash','input':{'command':'head -n 2 draft.md'},
            'output':{'stdout':'two lines','stderr':'','exit_code':0,'stdout_truncated':False}},
            {'type':'tool-bash','input':{'command':'cat absent.md'},'output':{'stdout':'partial','exit_code':1,'stdout_truncated':True}})
        c=self.catalog();self.assertFalse(c.workspace.paths())
        self.assertIn('head -n 2',c.read('m0-b0')['text'])
        self.assertTrue(c.read('m0-b1')['failed']);self.assertTrue(c.read('m0-b1')['truncated'])
        self.assertFalse(c.records['m0-b1']['fixture_eligible'])

    def test_delivery_is_a_path_reference_not_a_body_or_acceptance(self):
        self.message('assistant',{'type':'tool-present_file','input':{'path':'report.pdf'},'output':{'path':'report.pdf','status':'success'}})
        c=self.catalog();self.assertFalse(c.workspace.paths())
        self.assertEqual(c.records['m0-b0']['path'],'report.pdf')
        self.assertNotIn('accepted',c.records['m0-b0'])

    def test_upload_body_and_source_hash_are_available_and_creditable(self):
        raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=')
        ref=self.blob(raw)
        self.message('user',{'type':'file','filename':'reference.png','mediaType':'image/png','url':ref})
        c=self.catalog();self.assertEqual(c.read('m0-b0')['format'],'image')
        content=tool_content(c.image('m0-b0'));self.assertEqual(content[1]['type'],'image')
        self.assertEqual(base64.b64decode(content[1]['source']['data']),raw)
        c.stage(self.bundle,'m0-b0','sources/reference.png','The image is the source asset being inventoried.')
        ledger=faithfulness.build(self.bundle,'s',root=self.root)
        self.assertEqual(ledger['entries'][0]['origin'],'lifted')
        self.assertIn(ref,ledger['source']['recovery_inputs'])

    def test_upload_remote_url_traversal_and_missing_reference_fail_closed(self):
        for ref in ('https://example.org/a.png','../secret','files/missing'):
            self.message('user',{'type':'file','filename':'a.png','url':ref})
        c=self.catalog();self.assertEqual(len(c.workspace.omissions),3)
        self.assertTrue(all(c.records[f'm{i}-b0']['status']=='unavailable' for i in range(3)))

    def test_blob_tampering_after_inventory_is_refused(self):
        ref=self.blob(b'retained body');self.message('user',{'type':'file','filename':'a.txt','url':ref})
        c=self.catalog();(self.folder/ref).write_bytes(b'tampered')
        with self.assertRaises(ValueError):c.read('m0-b0')

    def test_withheld_upload_is_not_recovered_even_when_stale_bytes_exist(self):
        ref=self.blob(b'withheld');self.message('user',{'type':'file','filename':'a.txt','url':ref});self.save()
        doc=json.loads((self.folder/'data.json').read_text());doc['metadata']={'files_index':{ref.split('/')[-1]:{'status':'dropped:pii'}}}
        (self.folder/'data.json').write_text(json.dumps(doc))
        ws=recover.recover(self.trace,self.root)
        self.assertFalse(ws.raw_revisions);self.assertEqual(ws.evidence[0]['status'],'unavailable')

    def test_upstream_masked_upload_binds_supplied_bytes_without_false_digest_claim(self):
        ref=self.blob(b'original');(self.folder/ref).write_bytes(b'masked')
        self.message('user',{'type':'file','filename':'a.txt','url':ref});self.save()
        self.assertFalse(recover.recover(self.trace,self.root).raw_revisions)
        doc=json.loads((self.folder/'data.json').read_text());doc['metadata']={'files_index':{ref.split('/')[-1]:{'status':'redacted'}}}
        (self.folder/'data.json').write_text(json.dumps(doc))
        ws=recover.recover(self.trace,self.root)
        self.assertEqual(ws.evidence[0]['status'],'retained');self.assertFalse(ws.evidence[0]['export_hash_matches'])
        self.assertEqual(ws.raw_revisions[0]['sha256'],hashlib.sha256(b'masked').hexdigest())

    def test_failed_privacy_minimization_rolls_back_source_and_selection_manifest(self):
        self.message('user',{'type':'text','text':'Write to person@private-company.test'})
        c=self.catalog();c.read('m0-b0')
        with self.assertRaises(ValueError):c.stage(self.bundle,'m0-b0','sources/rule.json','This would need a supported privacy transformation.')
        self.assertFalse((self.bundle/'environment/repo/sources/rule.json').exists())
        self.assertFalse((self.bundle/'provenance/evidence-selection.json').exists())

    def test_unmapped_manifest_blob_is_visible_but_not_given_a_time_or_fixture(self):
        ref=self.blob(b'unanchored')
        (self.folder/'manifest.json').write_text(json.dumps({'files':[ref]}))
        c=self.catalog();item=next(x for x in c.records.values() if x['kind']=='unmapped_blob')
        self.assertEqual(item['status'],'retained_unmapped');self.assertIsNone(item['msg_index'])
        self.assertEqual(c.read(item['id'])['text'],'unanchored')
        self.assertFalse(item['fixture_eligible'])
        with self.assertRaises(ValueError):
            c.stage(self.bundle,item['id'],'sources/unanchored.txt','This blob has no verified path or time.')
        (self.folder/ref).write_bytes(b'changed after inventory')
        with self.assertRaisesRegex(ValueError,'changed after evidence inventory'):
            c.read(item['id'])
        self.assertIn('manifest.json',c.workspace.recovery_inputs)

    def test_raw_workspace_json_is_readable_and_searchable_but_not_a_fixture(self):
        self.message('assistant',workspace='workspaces/n0.json')
        (self.folder/'workspaces').mkdir()
        body=json.dumps({'files':[{'path':'deep.txt','hash':'x'*43,'status':'ok',
                                  'size':9000,'inlineText':'a'*9000+'DEEP_WORKSPACE_VALUE'}]})
        (self.folder/'workspaces/n0.json').write_text(body)
        c=self.catalog();item=next(x for x in c.records.values() if x['kind']=='workspace_manifest')
        self.assertEqual(item['message_indices'],[0])
        self.assertFalse(item['fixture_eligible'])
        first=c.read(item['id'],limit=100)
        self.assertIsNotNone(first['next_char_offset'])
        found=c.read(item['id'],find='DEEP_WORKSPACE_VALUE')
        self.assertEqual(found['match_count'],1)
        page=c.read(item['id'],char_offset=found['matches'][0]['char_offset'],limit=100)
        self.assertIn('DEEP_WORKSPACE_VALUE',page['text'])
        with self.assertRaises(ValueError):
            c.stage(self.bundle,item['id'],'sources/workspace.json','Raw snapshot metadata is context only.')
        (self.folder/'workspaces/n0.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'changed after evidence inventory'):
            c.read(item['id'])

    def test_unanchored_workspace_manifest_remains_available_without_invented_time(self):
        (self.folder/'workspaces').mkdir()
        (self.folder/'workspaces/extra.json').write_text('{"files":[]}')
        c=self.catalog();item=next(x for x in c.records.values() if x['kind']=='workspace_manifest')
        self.assertEqual(item['message_indices'],[])
        self.assertIsNone(item['msg_index'])
        self.assertIn('files',c.read(item['id'])['text'])

    def test_missing_unmapped_blob_cannot_be_read_or_staged(self):
        (self.folder/'manifest.json').write_text(json.dumps({'files':['files/'+'x'*43]}))
        c=self.catalog();item=next(x for x in c.records.values() if x['kind']=='unmapped_blob')
        self.assertEqual(item['status'],'missing')
        with self.assertRaises(ValueError):c.read(item['id'])
        with self.assertRaises(ValueError):
            c.stage(self.bundle,item['id'],'sources/missing.txt','Missing bytes are not source evidence.')

    def test_anchored_text_blob_is_not_reported_again_as_unmapped(self):
        ref=self.blob(b'actual document')
        self.message('assistant',workspace='workspaces/n0.json')
        (self.folder/'workspaces').mkdir()
        (self.folder/'workspaces/n0.json').write_text(json.dumps({'files':[{
            'path':'draft.txt','hash':ref.split('/')[-1],'file':ref,'status':'ok','size':15}]}))
        (self.folder/'manifest.json').write_text(json.dumps({'files':[ref,'workspaces/n0.json']}))
        c=self.catalog()
        self.assertEqual(c.summary()['channels'].get('file_version'),1)
        self.assertNotIn('unmapped_blob',c.summary()['channels'])

    def test_complete_pagination_and_search_reach_beyond_first_six_files(self):
        for i in range(55):
            self.message('assistant',{'type':'tool-write_file','input':{'path':f'draft-{i}.txt','content':'x'*30000+f'needle-{i}'}})
        c=self.catalog();ids=[];offset=0
        while True:
            page=c.list(kind='file_version',offset=offset,limit=7);ids.extend(x['id'] for x in page['records'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual(len(ids),55)
        found=c.list(kind='file_version',query='needle-54')['records'][0]
        self.assertIn('needle-54',c.read(found['id'],char_offset=30000)['text'])

    def test_later_and_undone_versions_remain_readable_but_cannot_be_staged(self):
        for i in range(3):
            self.message('assistant',{'type':'tool-write_file','input':{'path':'draft.txt','content':str(i)}},undone=i==1)
        c=self.catalog(pivot=1)
        c.read('file-0');c.stage(self.bundle,'file-0','sources/earlier.txt','An earlier version supplies a real comparison baseline.')
        for eid in ('file-1','file-2'):
            c.read(eid)
            with self.assertRaises(ValueError):c.stage(self.bundle,eid,'sources/later.txt','This must not leak into the historical fixture.')
        self.assertEqual((self.bundle/'environment/repo/sources/earlier.txt').read_text(),'0')

    def test_staging_cannot_overwrite_or_escape_and_requires_inspection(self):
        self.message('user',{'type':'text','text':'Keep the reserve.'});c=self.catalog()
        reason='This is an explicit retained user constraint.'
        with self.assertRaises(ValueError):c.stage(self.bundle,'m0-b0','sources/rule.json',reason)
        c.read('m0-b0')
        for p in ('../escape.json','/sources/rule.json','sources/../rule.json','sources/rule.txt'):
            with self.assertRaises(ValueError):c.stage(self.bundle,'m0-b0',p,reason)
        c.stage(self.bundle,'m0-b0','sources/rule.json',reason)
        with self.assertRaises(ValueError):c.stage(self.bundle,'m0-b0','sources/rule.json',reason)

    def test_redaction_happens_before_pagination(self):
        self.message('user',{'type':'text','text':'12345 person@private-company.test end'})
        c=self.catalog();parts=[];offset=0
        while True:
            result=c.read('m0-b0',char_offset=offset,limit=9);parts.append(result['text'])
            if result['next_char_offset'] is None:break
            offset=result['next_char_offset']
        self.assertNotIn('person@private-company.test',''.join(parts))

    def test_index_populates_undo_channels_and_delivery_paths(self):
        self.message('user',{'type':'text','text':'Prepare the report.'})
        self.message('assistant',{'type':'tool-present_file','input':{'path':'report.docx'},'output':{'status':'success'}},undone=True)
        self.message('user',{'type':'text','text':'Keep the dates.'})
        self.message('assistant',{'type':'tool-present_file','input':{'path':'final.docx'},'output':{'status':'success'}})
        self.save();r=index.index_session('s',self.root)[0]
        self.assertEqual(r['undone'],1);self.assertEqual(r['evidence_schema'],SCHEMA)
        self.assertEqual(r['evidence_channels']['user_text'],2)
        self.assertEqual(r['delivery_paths'],['final.docx'])

    def test_production_extraction_writes_private_inventory_and_both_author_routes_have_tools(self):
        self.message('user',{'type':'text','text':'Prepare a non-coding comparison.'})
        self.message('assistant',{'type':'tool-write_file','input':{'path':'a.md','content':'source'}})
        self.save();material=extract.extract('s',0,self.root);extract.write(material,self.bundle)
        self.assertTrue((self.bundle/'authoring/extraction/evidence-index.json').is_file())
        handlers=agent_designer._handlers(self.bundle,material,self.trace)
        self.assertIn('user_text',{x['kind'] for x in handlers['list_evidence']()['records']})
        self.assertEqual(handlers['read_evidence'](evidence_id='m0-b0')['text'],'Prepare a non-coding comparison.')
        with patch.object(agent_designer,'design',return_value={'delegated':True}) as run:
            self.assertEqual(designer.design({},material,self.bundle),{'delegated':True})
            run.assert_called_once()

    def row(self):
        return dict(session_id='s',seed_index=0,model='model',model_tier='other',span=[0,3],
                    evidence_schema=SCHEMA,src_paths=3,src_bytes=5000,runtime_class='native',
                    moments=[],corrections=0,categories=['code_writing'],n_turns=4)

    def test_file_types_defer_to_real_coding_scope_judgment(self):
        row=self.row();self.assertTrue(score.kill_filters(row).kept)
        verdict=dict(gradable=True,coding_request=False,truth_source='executable_tests',
                     runtime_feasible=True,reproduction='run it',deliverable_kind='code',
                     repo_task='Build the app',defect_pairs=[],moments=[],_evidence_verified=2)
        self.assertFalse(classify.summarise(row,verdict)['survives'])
        verdict.update(coding_request=True)
        self.assertTrue(classify.summarise(row,verdict)['survives'])
        verdict.update(runtime_feasible=False)
        self.assertFalse(classify.summarise(row,verdict)['survives'])

    def test_classifier_reads_real_evidence_then_caches_a_bound_verdict(self):
        self.message('user',{'type':'text','text':'Compare the registered quantities.'})
        self.message('assistant',{'type':'tool-write_file','input':{'path':'source.csv','content':'id,value\na,4'}})
        self.save();calls=[]
        def response(body):
            calls.append(body)
            if len(calls)%2==1:
                return {'stop_reason':'tool_use','content':[{'type':'tool_use','id':'a','name':'read_evidence','input':{'evidence_id':'m0-b0'}},
                    {'type':'tool_use','id':'b','name':'read_evidence','input':{'evidence_id':'file-0'}}]}
            self.assertIn('id,value',json.dumps(body['messages'][-1]))
            return {'stop_reason':'tool_use','content':[{'type':'tool_use','id':'c','name':'record','input':{
                'moments':[],'defect_pairs':[],'failure_mode':'','gradable':True,'coding_request':True,
                'deliverable_kind':'code','truth_source':'executable_tests','runtime_feasible':True,
                'repo_task':'Reconcile quantities','reproduction':'load source.csv and assert the sum',
                'domain':'cli tool','why_not':None,'supporting_evidence':['m0-b0','file-0']}}]}
        with patch('pipeline.config.sessions_root',return_value=self.root), patch('pipeline.dump.recover.sessions_root',return_value=self.root), \
             patch.object(classify,'CACHE',self.root.parent/'cache'),patch.object(classify,'_call',side_effect=response):
            a=classify.classify_seed(self.row());b=classify.classify_seed(self.row())
            self.assertTrue(b['cached']);self.assertEqual(len(calls),2)
            privacy_file=Path(classify.__file__).parents[1]/'privacy.py'
            read_bytes=Path.read_bytes
            def changed_privacy_policy(path):
                raw=read_bytes(path)
                return raw+b'\n# changed privacy reader\n' if path==privacy_file else raw
            with patch.object(Path,'read_bytes',changed_privacy_policy):
                c=classify.classify_seed(self.row());d=classify.classify_seed(self.row())
            self.assertEqual(len(calls),4)
            self.assertFalse(c.get('cached',False));self.assertTrue(d['cached'])
        self.assertEqual(a['_evidence_verified'],2)

    def test_classifier_refuses_unread_citations_and_cutoff_without_caching(self):
        self.message('user',{'type':'text','text':'Prepare a report.'});self.save()
        for stop in ('max_tokens','tool_use'):
            with self.subTest(stop=stop), patch('pipeline.config.sessions_root',return_value=self.root), \
                 patch('pipeline.dump.recover.sessions_root',return_value=self.root), \
                 patch.object(classify,'CACHE',self.root.parent/'cache'),patch.object(classify,'_call',return_value={
                     'stop_reason':stop,'content':[{'type':'tool_use','id':'x','name':'record','input':{'supporting_evidence':['m0-b0']}}]}):
                with self.assertRaises(RuntimeError):classify.classify_seed(self.row())
        self.assertFalse(list((self.root.parent/'cache').glob('*.json')))

    def test_stale_index_fails_before_any_classifier_request(self):
        self.message('user',{'type':'text','text':'Prepare a report.'});self.save()
        row=self.row();row['source_evidence_sha256']='0'*64
        with patch('pipeline.config.sessions_root',return_value=self.root), \
             patch('pipeline.dump.recover.sessions_root',return_value=self.root),patch.object(classify,'_call') as call:
            with self.assertRaises(ValueError):classify.classify_seed(row)
            call.assert_not_called()

    def test_classifier_feedback_requires_missing_reads_and_accepts_real_file_tool_material(self):
        self.message('user',{'type':'text','text':'Compare the registered quantities.'})
        self.message('assistant',{'type':'tool-read_file','input':{'path':'quote.csv'},'output':{'content':'id,value\na,4'}})
        self.save()
        verdict={'moments':[],'defect_pairs':[],'failure_mode':'','gradable':True,'coding_request':True,
                 'deliverable_kind':'code','truth_source':'executable_tests','runtime_feasible':True,
                 'repo_task':'Compare quantities','reproduction':'load quote.csv and assert the sum',
                 'domain':'cli tool','why_not':None,'supporting_evidence':['m0-b0','m1-b0']}
        record={'stop_reason':'tool_use','content':[{'type':'tool_use','id':'r','name':'record','input':verdict}]}
        read={'stop_reason':'tool_use','content':[{'type':'tool_use','id':'a','name':'read_evidence','input':{'evidence_id':'m0-b0'}},
             {'type':'tool_use','id':'b','name':'read_evidence','input':{'evidence_id':'m1-b0'}}]}
        with patch('pipeline.config.sessions_root',return_value=self.root),patch('pipeline.dump.recover.sessions_root',return_value=self.root), \
             patch.object(classify,'CACHE',self.root.parent/'cache'),patch.object(classify,'_call',side_effect=[record,read,record]) as call:
            result=classify.classify_seed(self.row())
        self.assertEqual(result['response_turns'],3)
        self.assertEqual(len(result['verification_feedback']),1)
        self.assertTrue(call.call_args_list[1].args[0]['messages'][2]['content'][0]['is_error'])


class Formats(unittest.TestCase):
    def archive(self, entries):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for name,body in entries:z.writestr(name,body)
        return out.getvalue()

    def test_archive_members_are_visible_and_read_without_extracting(self):
        raw=self.archive([('data/quote.csv','item,amount\na,12'),('../escape','secret')])
        view=inspect_body(raw,'pack.zip');self.assertTrue(view['members'][0]['readable'])
        self.assertFalse(view['members'][1]['readable'])
        self.assertEqual(inspect_body(raw,'pack.zip',member='data/quote.csv')['text'],'item,amount\na,12')
        with self.assertRaises(ValueError):inspect_body(raw,'pack.zip',member='../escape')

    def test_duplicate_and_symlink_archive_members_fail_closed(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            symlink=zipfile.ZipInfo('link');symlink.external_attr=(stat.S_IFLNK|0o777)<<16
            z.writestr(symlink,'../outside')
        self.assertFalse(inspect_body(out.getvalue(),'pack.zip')['members'][0]['readable'])
        with self.assertRaises(ValueError):inspect_body(out.getvalue(),'pack.zip',member='link')

    def test_office_text_is_labelled_as_a_view_not_a_new_original(self):
        raw=self.archive([('word/document.xml','<w:document xmlns:w="urn:w"><w:p><w:t>Term A</w:t></w:p></w:document>')])
        view=inspect_body(raw,'contract.docx');self.assertIn('Term A',view['text'])
        self.assertIn('source bytes unchanged',view['representation'])

    def test_spreadsheet_view_preserves_formula_and_cached_value_distinction(self):
        raw=self.archive([('xl/worksheets/sheet1.xml','<worksheet><c r="B2"><f>A2*2</f><v>8</v></c></worksheet>')])
        view=inspect_body(raw,'book.xlsx');self.assertIn('<f>A2*2</f>',view['text']);self.assertIn('<v>8</v>',view['text'])

    def test_gzip_and_tar_members_have_bounded_nonexecuting_access(self):
        self.assertEqual(inspect_body(gzip.compress(b'body'),'a.gz')['text'],'body')
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w') as archive:
            member=tarfile.TarInfo('notes.txt');member.size=4;archive.addfile(member,io.BytesIO(b'body'))
        self.assertEqual(inspect_body(data.getvalue(),'a.tar',member='notes.txt')['text'],'body')

    def test_unsupported_binary_is_retained_not_empty_or_missing(self):
        view=inspect_body(b'\x00\xffnot text','asset.woff')
        self.assertNotIn('text',view);self.assertIn('retained',view['readability'])

    def test_pdf_text_is_extracted_in_a_bounded_process(self):
        from pypdf import PdfWriter
        from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
        writer=PdfWriter();page=writer.add_blank_page(width=200,height=200)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 10 100 Td (Retained source text) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(content)
        raw=io.BytesIO();writer.write(raw)
        view=inspect_body(raw.getvalue(),'report.pdf')
        self.assertIn('Retained source text',view['text']);self.assertEqual(view['pages'],1)

    def test_blank_pdf_does_not_report_page_labels_as_extracted_content(self):
        from pypdf import PdfWriter
        writer=PdfWriter();writer.add_blank_page(width=200,height=200)
        raw=io.BytesIO();writer.write(raw)
        self.assertIn('no extractable text',inspect_body(raw.getvalue(),'blank.pdf')['readability'])


if __name__=='__main__':unittest.main()
