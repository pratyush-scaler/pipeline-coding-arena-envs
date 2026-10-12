"""A cited, paginated view of trace evidence; never executes or fetches sources.

File state and contextual evidence are separate. A shell observation is not a
file, a fetched chunk is not a complete page, and a later draft is not truth.
Reasoning bodies are excluded from this interface, including fixture staging.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json
from pathlib import Path

from .snapshots import _reference, path_key
from ..author.contracts import contained
from ..privacy import redact_excerpt

SCHEMA = 'arena-pipeline/evidence/1'
PAGE_SIZE = 40
READ_CHARS = 24000
#: In-body search (`read_evidence(find=...)`): matches returned per call, and
#: characters of context either side of each.
FIND_MAX_MATCHES = 40
FIND_CONTEXT = 200


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def blocks(message):
    value = message.get('content') or []
    return [{'type': 'text', 'text': value}] if isinstance(value, str) else value


def read_channels(trace, root=None):
    """Read context and upload references, preserving failures and undo markers."""
    records, inputs, omissions = [], {}, []
    folder = contained(root, trace['session_id']) if root is not None else None
    metadata = {}
    if folder is not None and contained(folder, 'data.json').is_file():
        raw = contained(folder, 'data.json').read_bytes()
        inputs['data.json'] = sha(raw)
        metadata = json.loads(raw).get('metadata') or {}

    def record(i, j, msg, kind, text=None, **fields):
        item = dict(id=f'm{i}-b{j}', kind=kind, msg_index=i, block_index=j,
                    role=msg.get('role'), undone=msg.get('undone') is True,
                    complete=True, fixture_eligible=False, **fields)
        if text is not None:
            item.update(text=text, sha256=sha(text.encode()), bytes=len(text.encode()))
        records.append(item)
        return item

    for i, msg in enumerate(trace.get('messages', [])):
        for j, block in enumerate(blocks(msg)):
            if not isinstance(block, dict):
                continue
            kind = block.get('type', '')
            if kind == 'reasoning':
                record(i, j, msg, 'reasoning_metadata', bytes=len(str(block.get('text', '')).encode()),
                       status='body_excluded')
                continue
            if kind == 'text':
                item = record(i, j, msg, 'user_text' if msg.get('role') == 'user' else 'assistant_text',
                              str(block.get('text') or ''))
                item['fixture_eligible'] = msg.get('role') == 'user' and not item['undone']
            elif kind == 'file':
                item = record(i, j, msg, 'upload', filename=block.get('filename'),
                              media_type=block.get('mediaType'), reference=block.get('url'), status='unavailable')
                try:
                    ref = _reference(block.get('url'), 'files')
                    if folder is None:
                        raise ValueError('no dump root')
                    raw = contained(folder, ref).read_bytes()
                    inputs[ref] = sha(raw)
                    declaration = (metadata.get('files_index') or {}).get(ref.split('/')[-1]) or {}
                    if declaration.get('status') not in (None, 'ok', 'redacted'):
                        raise ValueError('Upload explicitly withheld by export metadata')
                    agrees = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=') == ref.split('/')[-1]
                    redacted = (declaration.get('status') == 'redacted'
                                or (metadata.get('pii_redaction') or {}).get('applied') is True
                                or (trace.get('pii_tags') or {}).get('redacted') is True)
                    item.update(export_hash_matches=agrees, upstream_redaction_declared=redacted)
                    if not agrees and not redacted:
                        raise ValueError('Unmasked upload does not match its export identifier')
                    item.update(sha256=sha(raw), bytes=len(raw), status='retained',
                                fixture_eligible=not item['undone'])
                except (ValueError, OSError) as exc:
                    omissions.append(dict(msg_index=i, block_index=j, reason='missing_or_unsafe_upload'))
            elif kind == 'tool-ask_user':
                questions = block.get('questions') or []
                item = record(i, j, msg, 'clarification', encoded({'skipped': block.get('skipped', False),
                                                               'questions': questions}))
                answered = any(isinstance(q, dict) and isinstance(q.get('answer'), dict)
                               and any(q['answer'].get(k) for k in ('selected_option', 'custom_response'))
                               for q in questions)
                item['answered'] = answered and not block.get('skipped', False)
                item['fixture_eligible'] = item['answered'] and not item['undone']
            elif kind.startswith('tool-'):
                inp = block.get('input') if isinstance(block.get('input'), dict) else {}
                out = block.get('output') if isinstance(block.get('output'), dict) else {}
                failed = (block.get('state') == 'output-error' or bool(block.get('errorText'))
                          or bool(out.get('error')) or out.get('success') is False
                          or out.get('exit_code') not in (None, 0)
                          or out.get('status') in ('error', 'failed'))
                partial = any(out.get(k) for k in ('truncated', 'is_truncated', 'content_truncated',
                                                   'stdout_truncated', 'stderr_truncated'))
                if kind == 'tool-fetch_page':
                    item = record(i, j, msg, 'fetched_page', out.get('content') if isinstance(out.get('content'), str) else None,
                                  url=out.get('url', inp.get('url')), title=out.get('title'),
                                  chunk_index=out.get('chunkIndex', inp.get('chunkIndex')),
                                  total_chunks=out.get('totalChunks'), has_more=out.get('hasMore'))
                    # Each chunk remains an observation. Never concatenate repeated
                    # fetches or imply that other chunks were actually supplied.
                    item['complete'] = not partial and out.get('totalChunks') == 1 and out.get('hasMore') is False
                    item['fixture_eligible'] = isinstance(item.get('text'), str) and not failed and not item['undone']
                else:
                    label = {'tool-present_file': 'delivery', 'tool-bash': 'shell_observation'}.get(kind, 'tool_observation')
                    item = record(i, j, msg, label, encoded({'tool': kind, 'input': block.get('input'),
                                  'output': block.get('output'), 'state': block.get('state'),
                                  'error': block.get('errorText')}), tool=kind,
                                  path=out.get('path', inp.get('path')))
                    item['complete'] = not partial
                    # Observations keep their wrapper, command and failure state;
                    # they are never put into the reconstructed file workspace.
                    item['fixture_eligible'] = label in ('delivery', 'shell_observation', 'tool_observation') and not failed and not item['undone']
                item['failed'] = bool(failed)
                item['truncated'] = bool(partial)

    if folder is not None:
        for name in ('data.json', 'manifest.json', 'outcomes.json'):
            path = contained(folder, name)
            if name not in inputs and path.is_file():
                inputs[name] = sha(path.read_bytes())
    return {'records': records, 'inputs': inputs, 'omissions': omissions}


def source_revisions(workspace):
    """Additional exact, source-bound text candidates for provenance replay."""
    from .recover import Revision
    result = []
    for x in workspace.evidence:
        if x.get('fixture_eligible') and isinstance(x.get('text'), str):
            result.append(Revision('@evidence/' + x['id'], x['msg_index'], x['text'], x['kind'], 'context'))
            result.append(Revision('@evidence/' + x['id'] + '.source.json', x['msg_index'],
                                   source_document(x), x['kind'], 'context'))
    return result


def source_document(item):
    keys = ('id', 'kind', 'msg_index', 'block_index', 'role', 'undone', 'complete',
            'answered', 'failed', 'truncated', 'url', 'title', 'chunk_index',
            'total_chunks', 'has_more', 'tool', 'path', 'text')
    return encoded({k:item[k] for k in keys if k in item}) + '\n'


class Catalog:
    def __init__(self, trace, workspace, root=None, span=None, pivot=None):
        self.trace, self.workspace, self.root = trace, workspace, root
        self.span = tuple(span or (0, len(trace.get('messages', [])) - 1))
        self.pivot = self.span[1] if pivot is None else pivot
        self.folder = contained(root, trace['session_id']) if root is not None else None
        self.records = {}
        for entry in workspace.evidence:
            self.records[entry['id']] = dict(entry)
        # One record per content version and observed message. Aliased paths and
        # duplicate bytes are visible rather than counted as independent sources.
        for n, revision in enumerate(workspace.revisions):
            item = dict(id=f'file-{n}', kind='file_version', path=revision.path,
                        msg_index=revision.msg_index, text=revision.content,
                        sha256=sha(revision.content.encode()), bytes=len(revision.content.encode()),
                        source=revision.source, complete=True, fixture_eligible=True)
            self.records[item['id']] = item
        for n, revision in enumerate(workspace.raw_revisions):
            if revision.get('text_recovered'):
                continue
            item = dict(revision, id=f'blob-{n}', kind='binary_file', complete=True, fixture_eligible=True)
            self.records[item['id']] = item
        for n, omission in enumerate(workspace.omissions):
            self.records[f'omission-{n}'] = dict(omission, id=f'omission-{n}', kind='recovery_gap', fixture_eligible=False)
        for item in self.records.values():
            i = item.get('msg_index')
            item['in_seed_span'] = i is not None and self.span[0] <= i <= self.span[1]
            item['later_than_pivot'] = i is not None and i > self.pivot
            if i is not None and i < len(trace.get('messages', [])):
                item['undone'] = trace['messages'][i].get('undone') is True
            if item.get('undone') or item['later_than_pivot']:
                item['fixture_eligible'] = False
        self._workspace_manifest_inventory()
        self._unmapped_inventory()
        self.inspected = set()
        self.inspected_members = set()

    def _workspace_manifest_inventory(self):
        """Expose supplied snapshot JSON as inspection-only source context."""
        if self.folder is None:
            return
        directory = contained(self.folder, 'workspaces')
        if not directory.is_dir():
            return
        anchors = {}
        for i, message in enumerate(self.trace.get('messages', [])):
            ref = message.get('workspace')
            if isinstance(ref, str):
                anchors.setdefault(ref, set()).add(i)
            nodes = list(message.get('node_ids') or [])
            if message.get('manifest_node_id'):
                nodes.append(message['manifest_node_id'])
            for node in nodes:
                if isinstance(node, str):
                    anchors.setdefault('workspaces/' + node + '.json', set()).add(i)
        for path in sorted(directory.glob('*.json')):
            ref = path.relative_to(self.folder).as_posix()
            _reference(ref, 'workspaces')
            raw = contained(self.folder, ref).read_bytes()
            eid = 'workspace-json-' + sha(ref.encode())[:20]
            self.records[eid] = dict(id=eid, kind='workspace_manifest', path=ref,
                reference=ref, sha256=sha(raw), bytes=len(raw), complete=True,
                msg_index=None, message_indices=sorted(anchors.get(ref, [])),
                fixture_eligible=False, status='retained',
                note='Raw supplied workspace JSON. Inspection context only; '
                     'use verified file versions for fixture provenance.')

    def _unmapped_inventory(self):
        if self.folder is None:
            return
        path = contained(self.folder, 'manifest.json')
        if not path.is_file():
            return
        try:
            manifest = json.loads(path.read_text())
            linked = ({r.get('reference') for r in self.records.values()}
                      | {r.get('reference') for r in self.workspace.raw_revisions})
            for ref in manifest.get('files', []):
                if not isinstance(ref, str) or not ref.startswith('files/') or ref in linked:
                    continue
                try:
                    _reference(ref, 'files')
                    p = contained(self.folder, ref)
                    raw = p.read_bytes() if p.is_file() else None
                    status, size = ('retained_unmapped', len(raw)) if raw is not None else ('missing', None)
                    digest = sha(raw) if raw is not None else None
                except (ValueError, OSError):
                    status, size, digest = 'unsafe_reference', None, None
                eid = 'unmapped-' + sha(ref.encode())[:20]
                self.records[eid] = dict(id=eid, kind='unmapped_blob', reference=ref,
                    status=status, bytes=size, sha256=digest, msg_index=None, fixture_eligible=False,
                    note='No verified path/time association. Retained bytes are inspectable '
                         'but cannot be staged or credited as fixture provenance.')
        except (ValueError, OSError):
            self.records['manifest-error'] = dict(id='manifest-error', kind='inventory_error',
                status='unreadable_manifest', fixture_eligible=False)

    def summary(self):
        return dict(schema=SCHEMA, channels=dict(Counter(x['kind'] for x in self.records.values())),
                    records=len(self.records), omission_counts=dict(Counter(x['reason'] for x in self.workspace.omissions)),
                    span=list(self.span), pivot=self.pivot,
                    note='Kinds/extensions are inventory, not scope, usefulness, acceptance or difficulty judgments.')

    def list(self, offset=0, limit=PAGE_SIZE, kind=None, query=None, in_seed_only=False):
        rows = [x for x in self.records.values() if (not kind or x['kind'] == kind)
                and (not in_seed_only or x.get('in_seed_span'))]
        if query:
            needle = str(query).casefold()
            rows = [x for x in rows if needle in encoded({k:v for k,v in x.items() if k != 'text'}).casefold()
                    or needle in x.get('text', '').casefold()]
        start = max(0, int(offset)); end = start + max(1, min(PAGE_SIZE, int(limit)))
        def metadata(x):
            return {k:v for k,v in x.items() if k != 'text'}
        # Metadata can include private filenames or URLs too.
        return json.loads(redact_excerpt(encoded(dict(total=len(rows), offset=start,
            next_offset=end if end < len(rows) else None, records=[metadata(x) for x in rows[start:end]]))))

    def _raw(self, item):
        if item['kind'] in ('reasoning_metadata', 'inventory_error'):
            raise ValueError('Evidence has no readable anchored body')
        if item['kind'] == 'unmapped_blob' and item.get('status') != 'retained_unmapped':
            raise ValueError('Unmapped evidence has no retained readable body')
        if 'text' in item:
            return item['text'].encode()
        ref = item.get('reference')
        if self.folder is None or not ref:
            raise ValueError('No retained body reference')
        _reference(ref, 'workspaces' if item['kind'] == 'workspace_manifest' else 'files')
        raw = contained(self.folder, ref).read_bytes()
        if sha(raw) != item.get('sha256'):
            raise ValueError('Source bytes changed after evidence inventory')
        return raw

    def selection_bytes(self, item, member=None, text_view=False):
        raw = self._raw(item)
        filename = item.get('path') or item.get('filename') or ''
        if member is not None:
            from .formats import member_bytes
            raw = member_bytes(raw, filename, member)
            filename = member
        if text_view:
            from .formats import inspect_body
            view = inspect_body(raw, filename)
            if 'text' not in view:
                raise ValueError('No supported text view to stage')
            return (encoded({'evidence_id':item['id'], 'msg_index':item['msg_index'],
                'source_sha256':item['sha256'], 'member':member,
                'representation':view.get('representation', 'extracted text'),
                'format':view['format'], 'text':view['text']}) + '\n').encode()
        return raw

    def read(self, evidence_id, char_offset=0, limit=READ_CHARS, member=None, find=None):
        item = self.records[evidence_id]
        raw = self._raw(item)
        self.inspected.add(evidence_id)
        result = {k:v for k,v in item.items() if k != 'text'}
        from .formats import inspect_body
        view = inspect_body(raw, item.get('path') or item.get('filename') or '', member=member)
        self.inspected_members.add((evidence_id, member))
        text = view.pop('text', None)
        result.update(view)
        if text is not None:
            # Redact before slicing: a credential crossing a page boundary must
            # never escape by being split into two individually innocuous pieces.
            text = redact_excerpt(text)
            if find:
                # Search inside the body. A workspace file can run to millions
                # of characters -- measured: a 7.1M-character JSON takes ~300
                # page reads, more than a whole design budget -- and nothing
                # else searched file bodies, so their depths were unreachable
                # in practice. Offsets are into the same redacted text, so a
                # match is one `char_offset` read away.
                # Matched on the text itself, case-insensitively: offsets into a
                # casefolded copy drift wherever casefolding changes a length
                # ("ß" -> "ss"), and a wrong offset lands the next read elsewhere.
                import re
                found = list(re.finditer(re.escape(str(find)), text, re.IGNORECASE)) if find else []
                matches = [{"char_offset": m.start(),
                            "excerpt": text[max(0, m.start() - FIND_CONTEXT):m.end() + FIND_CONTEXT]}
                           for m in found[:FIND_MAX_MATCHES]]
                result.update(find=str(find), matches=matches, total_characters=len(text),
                              match_count=len(found))
                return json.loads(redact_excerpt(encoded(result)))
            start = max(0, int(char_offset)); end = min(len(text), start + max(1, min(READ_CHARS, int(limit))))
            result.update(text=text[start:end], total_characters=len(text),
                          next_char_offset=end if end < len(text) else None)
        return json.loads(redact_excerpt(encoded(result)))

    def image(self, evidence_id):
        item = self.records[evidence_id]
        raw = self._raw(item)
        from .formats import image_type
        mime = image_type(raw)
        if not mime or len(raw) > 5_000_000:
            raise ValueError('Unsupported image encoding or image exceeds 5 MB inspection limit')
        self.inspected.add(evidence_id)
        return {'image_content': [{'type': 'text', 'text': redact_excerpt(encoded({k:v for k,v in item.items() if k != 'text'}))},
            {'type': 'image', 'source': {'type': 'base64', 'media_type': mime,
                                       'data': base64.b64encode(raw).decode()}}]}

    def stage(self, bundle, evidence_id, path, reason, member=None, text_view=False):
        """Explicitly promote selected earlier evidence; never alter source files."""
        item = self.records[evidence_id]
        if not item.get('fixture_eligible') or item.get('msg_index') is None:
            raise ValueError('Later, undone, unanswered, unanchored or private evidence cannot become a fixture')
        if evidence_id not in self.inspected:
            raise ValueError('Read this evidence before staging it')
        if member is not None and (evidence_id, member) not in self.inspected_members:
            raise ValueError('Read this archive member before staging it')
        if not isinstance(reason, str) or len(reason.strip()) < 20:
            raise ValueError('Explain the non-coding task dependency before staging evidence')
        context = item['kind'] not in ('file_version', 'binary_file', 'upload')
        # State the whole path rule at once: naming one rule per failure cost
        # a designer two failed turns to learn both.
        rule = ('a canonical path below sources/' + (
            f", ending in .json because {item['kind']} evidence is staged as a cited context "
            "record (e.g. sources/client_brief.json)" if context or text_view else ''))
        if not isinstance(path, str) or path_key(path) != path or not path.startswith('sources/'):
            raise ValueError(f'Recovered selections must use {rule}; got {path!r}')
        if context and (member is not None or text_view):
            raise ValueError('Archive/text-view options require a retained file')
        if (context or text_view) and not path.endswith('.json'):
            raise ValueError(f'Context must be staged as a .json source record with its completeness '
                             f'and citation metadata: use {rule}; got {path!r}')
        raw = source_document(item).encode() if context else self.selection_bytes(item, member, text_view)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('Source exceeds explicit staging limit')
        target = contained(bundle / 'environment/repo', path)
        if target.exists():
            raise ValueError('Cannot overwrite a fixture')
        from ..artifacts import atomic_json
        record = dict(evidence_id=evidence_id, path=path, reason=reason,
                      source_sha256=item.get('sha256'), staged_sha256=sha(raw),
                      msg_index=item['msg_index'], pivot=self.pivot,
                      kind=item['kind'], member=member, text_view=text_view,
                      representation='derived text view' if text_view else 'cited context record' if context else 'exact retained member bytes' if member else 'exact retained bytes')
        selection = bundle / 'provenance/evidence-selection.json'
        previous = json.loads(selection.read_text()) if selection.exists() else {'schema':SCHEMA,'selections':[]}
        from ..privacy import scan
        if scan(bundle / 'environment/repo'):
            raise ValueError('Minimize existing fixtures before adding another source')
        from ..ledger.redaction import MANIFEST
        privacy_path = contained(bundle, MANIFEST)
        privacy_before = privacy_path.read_bytes() if privacy_path.exists() else None
        selection_before = selection.read_bytes() if selection.exists() else None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            previous['selections'].append(record)
            atomic_json(selection, previous)
            # Reuse the existing typed, source-bound privacy transformation.
            from ..ledger.redaction import minimize
            minimize(bundle, self.trace['session_id'], root=self.root)
        except Exception:
            target.unlink(missing_ok=True)
            if privacy_before is None:
                privacy_path.unlink(missing_ok=True)
            else:
                privacy_path.write_bytes(privacy_before)
            if selection_before is None:selection.unlink(missing_ok=True)
            else:selection.write_bytes(selection_before)
            raise
        return record


def replay_selections(workspace, bundle, trace, root):
    """Recompute selected archive members/views; a private manifest is no proof."""
    path = contained(bundle, 'provenance/evidence-selection.json')
    if not path.exists():return
    from .recover import Revision
    document = json.loads(path.read_text())
    if document.get('schema') != SCHEMA or not isinstance(document.get('selections'), list):
        raise ValueError('Invalid evidence selection manifest')
    catalog = Catalog(trace, workspace, root)
    seen = set()
    for entry in document['selections']:
        rel=entry['path']
        if path_key(rel) != rel or not rel.startswith('sources/') or rel in seen:
            raise ValueError('Invalid or duplicate selected source path')
        seen.add(rel)
        item=catalog.records[entry['evidence_id']]
        if entry['source_sha256'] != item.get('sha256') or entry['msg_index'] != item.get('msg_index'):
            raise ValueError('Selected evidence changed since staging')
        if not entry.get('member') and not entry.get('text_view'):continue
        raw=catalog.selection_bytes(item,entry.get('member'),entry.get('text_view',False))
        if sha(raw) != entry['staged_sha256']:
            raise ValueError('Selected member/view does not replay from source')
        citation='@selection/' + rel
        origin='extracted_view' if entry.get('text_view') else 'archive_member'
        try:
            text=raw.decode('utf-8')
            if '\x00' in text:raise UnicodeError('binary')
            workspace.selected_revisions.append(Revision(citation,item['msg_index'],text,item['kind'],origin))
        except UnicodeError:
            workspace.raw_revisions.append(dict(path=citation,msg_index=item['msg_index'],sha256=sha(raw),
                bytes=len(raw),source='archive_member',reference=item.get('reference'),text_recovered=False))


TOOLS = [
    {'name': 'list_evidence', 'description': 'Search and paginate all source channels, versions and availability, including raw workspace_manifest JSON and inspection-only unmapped_blob records. No reasoning bodies. query searches full retained text, paths and metadata.',
     'input_schema': {'type':'object','properties': {'offset':{'type':'integer'}, 'limit':{'type':'integer'},
         'kind':{'type':'string'}, 'query':{'type':'string'}, 'in_seed_only':{'type':'boolean'}}, 'required':[]}},
    {'name':'read_evidence', 'description':'Read a cited source version or observation (including every workspace file version, by id), with continuation offsets. Pass `find` to search INSIDE the body instead: it returns up to 40 case-insensitive matches with their char_offset and context, so a deep passage in a large file is one further read away. For archives, list members then read one safe member. Never executes or fetches anything.',
     'input_schema': {'type':'object','properties': {'evidence_id':{'type':'string'}, 'char_offset':{'type':'integer'},
         'limit':{'type':'integer'}, 'member':{'type':'string'}, 'find':{'type':'string'}}, 'required':['evidence_id']}},
    {'name':'view_evidence_image', 'description':'Inspect an anchored retained PNG/JPEG/GIF/WebP upload or workspace image. Does not establish aesthetic correctness.',
     'input_schema':{'type':'object','properties':{'evidence_id':{'type':'string'}},'required':['evidence_id']}},
]


def tool_content(result):
    if isinstance(result, dict) and 'image_content' in result:
        return result['image_content']
    return redact_excerpt(encoded(result))
