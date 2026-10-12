"""Choose one file-authoritative key for a future GPT/Codex campaign.

Selection is local to the SSH request environment. The external Codex CLI
keeps that key throughout its campaign; this helper never restarts a trial.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import time

# A Root-admitted frozen-source overlay supplies the exact already verified
# transport engine here, without modifying files in that frozen release.
ENGINE = None


def campaign_environment(kwargs, inherited, *, keys_path=None, pool=None,
                         sleeper=None, max_wait_s=300.0):
    environment = dict(inherited)
    if kwargs.get('agent', 'terminus-2') != 'codex':
        return environment, None
    from .. import policy
    model = kwargs.get('model')
    if policy.harness_for(model) != 'codex':
        raise ValueError('The requested model must use its assigned native harness')
    if not str(model).split('/')[-1].lower().startswith('gpt-'):
        return environment, None
    if not math.isfinite(max_wait_s) or max_wait_s < 0:
        raise ValueError('Native credential admission wait must be finite and nonnegative')
    if ENGINE is None:
        from .. import openai_transport as engine
    else:
        engine = ENGINE
    if keys_path is None:
        from .. import cli
        keys_path = cli.KEYS
    keys_path = Path(keys_path)
    pool = pool or engine.SharedCooldown(keys_path.parent / 'pipeline-artifacts/provider-cooldowns')
    sleeper = sleeper or time.sleep
    waited = 0.0
    while True:
        choices = engine.credentials(keys_path)
        selected, delay = pool.choose(choices)
        if selected is not None:
            break
        if delay > max_wait_s - waited:
            raise RuntimeError('Native GPT credential admission wait exhausted; shared cooldown remains active')
        remaining = delay
        while remaining > 0:
            chunk = min(remaining, engine.WAIT_CHUNK_S)
            sleeper(chunk)
            waited += chunk
            remaining -= chunk
    # Do not mutate global os.environ: other concurrent campaigns may select
    # different keys, and Claude Code keeps its established credential routing.
    environment['OPENAI_API_KEY'] = selected.value
    for name in ('OPENAI_ORG_ID', 'OPENAI_PROJECT_ID'):
        environment.pop(name, None)
    if selected.organization:
        environment['OPENAI_ORG_ID'] = selected.organization
    if selected.project:
        environment['OPENAI_PROJECT_ID'] = selected.project
    metadata = {'schema': 'arena-pipeline/openai-native-campaign-selection/1',
        'requested_model': model, 'agent': 'codex', 'credential_name': selected.name,
        'credential_source': 'original-workspace-keys.env',
        'scope_policy': 'unknown credentials share the same conservative cooldown',
        'waited_seconds': waited, 'rotation_boundary': 'future-campaign-only',
        'midtrial_rotation': False, 'global_environment_mutated': False,
        'transport_engine_sha256': hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest(),
        'host_credential_helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return environment, metadata


def write_selection(output, metadata):
    if metadata is None:
        return None
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    path = output.with_name(output.name + '.openai-credential-selection.json')
    # An old failed launch remains evidence; never silently replace its owner.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(metadata, handle, indent=2)
        handle.write('\n')
    return {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
