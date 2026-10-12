"""One durable checkpoint per environment; a checkpoint never grants acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from .artifacts import atomic_json, evidence_inventory
from .author.contracts import contained, strict_json

PATH = 'verification/checkpoint.json'
WORKSPACE = ('environment/repo', 'environment/deps', 'provenance')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def read(bundle):
    path = Path(bundle) / PATH
    value = strict_json(path.read_text()) if path.is_file() else {}
    if value and value.get('schema') != 'arena-pipeline/checkpoint/1':
        raise ValueError('Unknown pipeline checkpoint')
    return value


def update(bundle, **fields):
    value = read(bundle)
    value.update(fields, schema='arena-pipeline/checkpoint/1', updated_at=time.time(), release_ready=False)
    atomic_json(Path(bundle) / PATH, value)
    return value


def author_boundary(bundle, session, stage='authoring'):
    bundle, session = Path(bundle), Path(session)
    relative = session.relative_to(bundle).as_posix()
    config = strict_json((session / 'request-config.json').read_text())
    responses = sorted(session.glob('response-*.json'))
    if [p.name for p in responses] != [f'response-{i:03d}.json' for i in range(1, len(responses) + 1)]:
        raise ValueError('Incomplete author response sequence')
    checkpoint = {'session': relative, 'session_files': evidence_inventory(bundle, [relative]),
        'workspace_files': evidence_inventory(bundle, WORKSPACE),
        'response_count': len(responses), 'remaining_steps': config['max_steps'] - len(responses)}
    update(bundle, stage=stage, authoring_session=checkpoint, submitted=None)
    return checkpoint


def submitted(bundle, session, spec):
    path = Path(session) / 'submitted.json'
    atomic_json(path, spec)
    update(bundle, stage='submitted', authoring_session=None,
        submitted={'path': path.relative_to(bundle).as_posix(),
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
        workspace_files=evidence_inventory(Path(bundle), WORKSPACE))


def submitted_spec(bundle, state=None):
    state = state or read(bundle)
    descriptor = state.get('submitted')
    if not descriptor:
        raise ValueError('Checkpoint has no submitted design')
    path = contained(bundle, descriptor['path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != descriptor['sha256']:
        raise ValueError('Submitted design changed after checkpoint')
    if evidence_inventory(Path(bundle), WORKSPACE) != state.get('workspace_files'):
        raise ValueError('Submitted design workspace changed after checkpoint')
    return strict_json(path.read_text())


# One repair budget per candidate, `policy.MAX_REPAIR_PASSES`, shared by every
# source of findings. The per-source `<name>_repairs_remaining` keys are kept
# because resumable callers read them, but each is capped by what the candidate
# has left, and that is counted from `repair.LOG` -- one ledger, so a candidate
# can no longer take the gate budget and then the qualification budget on top.
# An unreadable log raises: a budget that cannot be counted is spent.


def repairs_left(bundle):
    from . import policy, repair
    return max(0, policy.MAX_REPAIR_PASSES + repair.passes_granted(Path(bundle))
               - repair.passes_used(Path(bundle)))


def repair_budget(bundle, name, allowance):
    if type(allowance) is not int or allowance < 0:
        raise ValueError('Repair allowance must be a nonnegative integer')
    key = name + '_repairs_remaining'
    previous = read(bundle).get(key, allowance)
    if type(previous) is not int or previous < 0:
        raise ValueError('Invalid retained repair allowance')
    # Disabling automatic revision is not a consumed repair. Preserve any
    # retained allowance for a later explicit manual trigger.
    if allowance == 0:
        return 0
    remaining = min(previous, allowance, repairs_left(bundle))
    update(bundle, **{key: remaining})
    return remaining


def consume_repair(bundle, name, findings=None):
    """Charge one pass to the candidate's single budget and record it.

    `findings=None` records a pass whose findings the caller did not supply; it
    counts against the budget but cannot anchor the monotone comparison.
    """
    from . import repair
    key = name + '_repairs_remaining'
    remaining = read(bundle).get(key, 0)
    if type(remaining) is not int or remaining <= 0 or repairs_left(bundle) <= 0:
        raise ValueError('Original repair allowance exhausted')
    repair.record_pass(Path(bundle), findings, 'charged', {'phase': name})
    left = min(remaining - 1, repairs_left(bundle))
    update(bundle, **{key: left})
    return left


def admit_repair(bundle, name, findings):
    """The governor and the budget in one call: (ok, reason, remaining).

    Refuses unless `repair.admissible` accepts the findings (cited, within
    budget, strictly shrinking, nothing repeated); on acceptance the pass is
    charged and recorded before any revision starts. orchestrate's gate loop
    should call this in place of `consume_repair(bundle, 'gate')`.
    """
    from . import repair
    ok, reason = repair.admissible(Path(bundle), findings)
    if not ok:
        return False, reason, repairs_left(bundle)
    try:
        return True, '', consume_repair(bundle, name, findings)
    except ValueError as exc:
        return False, str(exc), 0


def stalled_design(bundle):
    """A design that ran out of steps: its retained draft and remaining problems.

    None unless the checkpoint is `stalled`. The draft is verified against the
    hash recorded when the session stopped, and the problems are the last
    `check_draft` result the designer saw -- what a continuation must fix.
    """
    import hashlib
    state = read(bundle)
    if state.get('stage') != 'stalled':
        return None
    session = Path(bundle) / state['authoring_session']['session']
    stalled = strict_json((session / 'stalled.json').read_text())
    draft_path = session / 'draft.json'
    if not draft_path.is_file():
        raise ValueError('stalled design retained no draft to continue from')
    if hashlib.sha256(draft_path.read_bytes()).hexdigest() != stalled.get('draft_sha256'):
        raise ValueError('stalled design draft changed after the session stopped')
    problems = []
    for message in strict_json((session / 'conversation.json').read_text()):
        if message.get('role') != 'user' or not isinstance(message.get('content'), list):
            continue
        for block in message['content']:
            text = block.get('content') if block.get('type') == 'tool_result' else None
            text = text if isinstance(text, str) else None
            if text and '"clean"' in text and '"problems"' in text:
                try:
                    problems = list(json.loads(text).get('problems') or [])
                except ValueError:
                    pass
    return {'draft': strict_json(draft_path.read_text()), 'problems': problems,
            'session': state['authoring_session']['session'], 'steps': stalled.get('steps')}


def resumable(bundle):
    """Verify the saved boundary. Incomplete local mutations require inspection."""
    state = read(bundle)
    if state.get('stage') == 'authoring':
        from .author.agent_designer import restore_design_session
        restore_design_session(Path(bundle), state['authoring_session'])
    elif state.get('stage') in {'submitted', 'checking'}:
        submitted_spec(Path(bundle), state)
    elif state.get('stage') == 'repair':
        if not state.get('feedback'):
            raise ValueError('Repair checkpoint has no verified findings')
        submitted_spec(Path(bundle), state)
    else:
        return None
    return state
