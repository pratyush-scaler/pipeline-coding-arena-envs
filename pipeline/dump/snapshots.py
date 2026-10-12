"""Read supplied workspace manifests and blobs without executing recovered files.

The export's hash is a lookup key and may predate upstream privacy redaction.
Evidence therefore binds the actual supplied manifest and blob bytes. A changed
declared digest is accepted only for explicitly upstream-redacted exports and
is recorded, never represented as a successful original-checksum verification.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from ..author.contracts import contained
from ..config import BINARY_EXT


def path_key(path: str) -> str:
    """Join tool absolute paths to manifest paths without guessing a cwd."""
    if not isinstance(path, str) or not path or '\\' in path or '\x00' in path:
        raise ValueError('invalid recovered path')
    value = PurePosixPath(path)
    if '..' in value.parts:
        raise ValueError('parent traversal in recovered path')
    canonical = value.as_posix().lstrip('/')
    if not canonical or canonical == '.':
        raise ValueError('empty recovered path')
    return canonical


def _reference(value: str, directory: str) -> str:
    if not isinstance(value, str) or path_key(value) != value:
        raise ValueError('noncanonical source reference')
    parts = PurePosixPath(value).parts
    if len(parts) != 2 or parts[0] != directory:
        raise ValueError('source reference outside its channel')
    return value


def read(trace: dict, root: Path) -> dict:
    """Return message-anchored post-states, plus omissions and byte evidence."""
    session = trace.get('session_id')
    if not isinstance(session, str) or '/' in session or '\\' in session or session in ('', '.', '..'):
        raise ValueError('session_id must be one directory name')
    folder = contained(root, session)
    result = {'snapshots': [], 'omissions': [], 'inputs': {}, 'declaration_differences': []}
    wsdir = contained(folder, 'workspaces')
    if not wsdir.exists():
        if any(m.get('workspace') for m in trace.get('messages', [])):
            result['omissions'].append({'reason': 'missing_workspace_directory'})
        return result
    bindings, node_bindings = {}, {}
    for i, message in enumerate(trace.get('messages', [])):
        nodes = list(message.get('node_ids') or [])
        if message.get('manifest_node_id'):
            nodes.append(message['manifest_node_id'])
        for order, node in enumerate(nodes):
            if isinstance(node, str):node_bindings.setdefault(node, []).append((i, order))
        value = message.get('workspace')
        if value is not None:
            try:ref = _reference(value, 'workspaces')
            except ValueError:
                result['omissions'].append({'reason': 'unsafe_workspace_reference', 'msg_index': i})
                continue
            bindings.setdefault(ref, []).append((i, len(nodes) + 1))
    upstream_redacted = trace.get('pii_tags', {}).get('redacted') is True
    # The dump may declare masking globally even if this trace's text needed no
    # edits. This flag does not prove any particular transformation.
    data = contained(folder, 'data.json')
    if data.exists():
        raw_data = data.read_bytes()
        result['inputs']['data.json'] = hashlib.sha256(raw_data).hexdigest()
        metadata = json.loads(raw_data).get('metadata') or {}
        upstream_redacted |= (metadata.get('pii_redaction') or {}).get('applied') is True
    cache = {}
    seen = set()
    for file in sorted(wsdir.glob('*.json')):
        rel = file.relative_to(folder).as_posix()
        source = contained(folder, rel)
        raw = source.read_bytes()
        result['inputs'][rel] = hashlib.sha256(raw).hexdigest()
        seen.add(rel)
        anchors = bindings.get(rel) or node_bindings.get(file.stem)
        if not anchors:
            result['omissions'].append({'reason': 'unmapped_workspace_snapshot', 'snapshot': rel})
            continue
        if rel not in bindings and len({i for i, _ in anchors}) != 1:
            result['omissions'].append({'reason': 'ambiguous_workspace_anchor', 'snapshot': rel})
            continue
        try:
            snapshot = json.loads(raw)
            files = snapshot['files']
            if not isinstance(files, list):raise ValueError('files must be a list')
        except (ValueError, KeyError, TypeError):
            result['omissions'].append({'reason': 'malformed_workspace_snapshot', 'snapshot': rel})
            continue
        records, paths, repeated = [], set(), set()
        for entry in files:
            if not isinstance(entry, dict):
                result['omissions'].append({'reason': 'malformed_snapshot_entry', 'snapshot': rel})
                continue
            path = entry.get('path')
            try:key = path_key(path)
            except ValueError:
                result['omissions'].append({'reason': 'unsafe_snapshot_path', 'snapshot': rel})
                continue
            if key in paths:repeated.add(key)
            paths.add(key)
            record = {'path': path, 'key': key, 'status': entry.get('status'), 'snapshot': rel}
            records.append(record)
            if entry.get('status') not in ('ok', 'redacted'):
                record['reason'] = 'snapshot_' + str(entry.get('status', 'missing_status'))
                continue
            digest = entry.get('hash')
            if not isinstance(digest, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', digest):
                record['reason'] = 'invalid_blob_identifier'
                continue
            inline = entry.get('inlineText')
            pointer = entry.get('file', 'files/' + digest)
            if not isinstance(inline, str):
                try:pointer = _reference(pointer, 'files')
                except (ValueError, TypeError):
                    record['reason'] = 'missing_or_unsafe_snapshot_body'
                    continue
            cache_key = ('inline', inline) if isinstance(inline, str) else ('blob', pointer)
            if cache_key not in cache:
                try:
                    if isinstance(inline, str):
                        payload, channel, reference = inline.encode('utf-8'), 'workspace-inline', rel
                    else:
                        reference = _reference(pointer, 'files')
                        blob = contained(folder, reference)
                        payload, channel = blob.read_bytes(), 'workspace-blob'
                    observed = hashlib.sha256(payload).hexdigest()
                    encoded = base64.urlsafe_b64encode(bytes.fromhex(observed)).decode().rstrip('=')
                    text = None
                    try:
                        text = payload.decode('utf-8')
                        if '\x00' in text:text = None
                    except UnicodeDecodeError:pass
                    cache[cache_key] = {'content': text, 'sha256': observed, 'hash': encoded,
                        'bytes': len(payload), 'source': channel, 'reference': reference}
                    if channel == 'workspace-blob':result['inputs'][reference] = observed
                except (OSError, ValueError, TypeError, UnicodeError):
                    record['reason'] = 'missing_or_unsafe_snapshot_body'
                    continue
            body = cache[cache_key]
            agrees = body['hash'] == digest
            size_agrees = body['bytes'] == entry.get('size')
            if not agrees or not size_agrees:
                result['declaration_differences'].append({'snapshot': rel, 'path': path,
                    'declared_hash': digest, 'observed_sha256': body['sha256'],
                    'hash_matches': agrees, 'size_matches': size_agrees,
                    'upstream_redaction_declared': upstream_redacted or entry.get('status') == 'redacted'})
                if not upstream_redacted and entry.get('status') != 'redacted':
                    record['reason'] = 'snapshot_integrity_mismatch'
                    continue
            record.update(body)
            if PurePosixPath(path).suffix.lower() in BINARY_EXT:record['content'] = None
        if repeated:
            for record in records:
                if record['key'] in repeated:
                    record.pop('content', None);record.pop('sha256', None)
                    record['reason'] = 'duplicate_snapshot_path'
        for index, order in sorted(set(anchors)):
            result['snapshots'].append({'msg_index': index, 'order': order, 'snapshot': rel,
                                       'paths': paths, 'files': records})
            for record in records:
                if record.get('reason'):
                    result['omissions'].append({'path': record['path'], 'msg_index': index,
                        'snapshot': rel, 'reason': record['reason']})
    for rel in sorted(set(bindings) - seen):
        result['omissions'].append({'reason': 'missing_workspace_snapshot', 'snapshot': rel})
    result['snapshots'].sort(key=lambda s: (s['msg_index'], s['order'], s['snapshot']))
    return result
