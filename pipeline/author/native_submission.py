"""Deliver an SDK-checked native Submit after a bounded CLI turn-limit terminal.

Native adapters retain the real receipt and raise NativeSubmissionCompleted only
once their subprocess has sealed. Ordinary provider errors are never converted.
The SDK rechecks the exact retained draft and refuses repeated event delivery.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _spec_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _file(path):
    path = Path(path)
    _require(not any(p.is_symlink() for p in [path, *path.parents]),
             'Native submission evidence must use real host-owned files')
    return path


def _read(root, name):
    path = _file(root / name)
    return json.loads(path.read_text())


def retain_submission(root, event, *, session_id, before, total_cap, accepted_calls,
                      draft, submit_check):
    """Called by a native bridge before acknowledging its sole genuine Submit."""
    root = _file(Path(root))
    _require(event.get('type') == 'tool_use' and event.get('name') == 'submit_draft'
             and event.get('input') == {} and isinstance(event.get('id'), str)
             and bool(event['id']), 'Only a real native submit_draft event can be retained')
    verdict = submit_check(deepcopy(draft))
    _require(isinstance(verdict, dict) and verdict.get('clean') is True,
             'SDK submit_check refused the native pending submission')
    value = {'schema': 'arena-native/sdk-checked-submission/2', 'event': deepcopy(event),
             'native_session_id': session_id, 'source': 'actual-native-mcp-request',
             'native_turns_before_call': before, 'original_native_total_cap': total_cap,
             'accepted_native_tool_calls': accepted_calls, 'sdk_submit_checked': True,
             'draft_spec_sha256': _spec_hash(draft), 'submit_check': deepcopy(verdict),
             'validation_deferred': True, 'official_terminal_required': True}
    path = _file(root / 'pending-submission.json')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False); handle.write('\n')
        handle.flush(); os.fsync(handle.fileno())
    return value


class NativeSubmissionCompleted(RuntimeError):
    """The real CLI failed at its turn limit after an accepted durable Submit."""
    def __init__(self, root):
        super().__init__('Native Submit retained; original CLI turn-limit receipt preserved')
        self.root = Path(root)

    def response(self, handlers):
        root = _file(self.root)
        pending, receipt, tools = [_read(root, name) for name in
                                  ['pending-submission.json', 'receipt.json', 'tools.json']]
        stdout = _file(root / 'stdout.json')
        rows = [json.loads(line) for line in stdout.read_text().splitlines() if line.strip()]
        terminal = next((row for row in reversed(rows) if row.get('type') == 'result'), None)
        _require(terminal is not None, 'The real native terminal has not sealed')
        event = pending.get('event', {})
        draft = handlers['draft_spec']()
        _require(pending.get('schema') == 'arena-native/sdk-checked-submission/2'
                 and pending.get('source') == 'actual-native-mcp-request'
                 and pending.get('sdk_submit_checked') is True
                 and pending.get('submit_check', {}).get('clean') is True
                 and pending.get('draft_spec_sha256') == _spec_hash(draft),
                 'Missing SDK validation or changed pending draft')
        _require(event.get('type') == 'tool_use' and event.get('name') == 'submit_draft'
                 and event.get('input') == {} and isinstance(event.get('id'), str),
                 'Only the exact native Submit event may be delivered')
        spent, before, cap = [receipt.get(k) for k in
                             ['native_turns', 'native_turns_before_call', 'native_total_cap']]
        _require(all(type(n) is int and n >= 0 for n in [spent, before, cap]) and spent > 0
                 and receipt.get('actual_lifetime_native_turns') == before + spent <= cap
                 and receipt.get('native_budget_overrun') == 0
                 and receipt.get('future_scope_budget_overrun') == 0
                 and type(receipt.get('maximum_this_invocation_official_turns')) is int
                 and spent <= receipt['maximum_this_invocation_official_turns'],
                 'Native lifetime/invocation accounting exceeded or is unsealed')
        _require(receipt.get('transport') == 'official-claude-cli-real-pipeline-mcp-tools'
                 and receipt.get('exit_code') == 1 and receipt.get('is_error') is True
                 and receipt.get('authoritative_submission_retained') is True
                 and terminal.get('is_error') is True and terminal.get('subtype') == 'error_max_turns'
                 and terminal.get('num_turns') == spent
                 and receipt.get('native_session_id') == terminal.get('session_id')
                 and (pending.get('native_session_id') == receipt.get('native_session_id')
                      or pending.get('native_session_id') is None and before == 0 and receipt.get('resumed') is False)
                 and bool(receipt.get('native_session_id'))
                 and pending.get('native_turns_before_call') == before
                 and pending.get('original_native_total_cap') == cap,
                 'Only an exact bounded native max-turns terminal can deliver retained Submit')
        accepted = [row for row in tools if row.get('name') == 'submit_draft'
                    and row.get('native_budget_accepted') is True]
        _require(len(accepted) == 1 and accepted[0].get('id') == event['id']
                 and accepted[0].get('arguments') == {} and accepted[0].get('result', {}).get('isError') is False,
                 'Missing, unchecked or duplicate accepted native Submit')
        acknowledgements = [json.loads(b['text']) for b in accepted[0]['result'].get('content', [])
                            if b.get('type') == 'text']
        _require(any(row.get('submitted') is True for row in acknowledgements),
                 'Native bridge did not acknowledge a successful Submit')
        verdict = handlers['submit_check'](deepcopy(draft))
        _require(isinstance(verdict, dict) and verdict.get('clean') is True,
                 'Current SDK submit_check refused the retained draft')
        bindings = {name: hashlib.sha256(_file(root / name).read_bytes()).hexdigest() for name in
                    ['pending-submission.json', 'receipt.json', 'tools.json', 'stdout.json']}
        delivery = {'schema': 'arena-native/sdk-submit-delivery/1', 'event': deepcopy(event),
                    'evidence_sha256': bindings, 'original_exit_code': receipt['exit_code'],
                    'original_is_error': receipt['is_error'], 'native_turns': spent,
                    'actual_lifetime_native_turns': before + spent, 'native_total_cap': cap,
                    'new_model_calls': 0, 'accounting_rewritten': False}
        path = _file(root / 'submission-delivered.json')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(delivery, handle, ensure_ascii=False); handle.write('\n')
            handle.flush(); os.fsync(handle.fileno())
        return {'content': [deepcopy(event)], 'stop_reason': 'tool_use',
                'usage': deepcopy(receipt.get('usage', {})),
                'native_submission_delivery': delivery}


def call_with_retained_submit(call, body, handlers, *, max_steps=None):
    try:
        return call(body)
    except NativeSubmissionCompleted as retained:
        return retained.response(handlers)
