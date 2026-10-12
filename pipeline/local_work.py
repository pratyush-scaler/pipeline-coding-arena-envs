"""Run CPU-heavy local checks in separate interpreters, with no model calls."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid


def enabled():
    return os.environ.get('ARENA_LOCAL_WORKERS', '0') == '1'


LIMIT = int(os.environ.get('ARENA_LOCAL_CONCURRENCY', str(min(32, os.cpu_count() or 1))))
if LIMIT < 1:
    raise ValueError('Local worker capacity must be positive')
_slots = threading.BoundedSemaphore(LIMIT)
_lock = threading.Lock()
_active = _peak = _waiting = 0


def stats():
    with _lock:
        return {'limit': LIMIT, 'active': _active, 'peak_active': _peak, 'waiting': _waiting}


@contextmanager
def slot():
    global _active, _peak, _waiting
    with _lock:
        _waiting += 1
    with _slots:
        with _lock:
            _waiting -= 1
            _active += 1
            _peak = max(_peak, _active)
        try:
            yield
        finally:
            with _lock:
                _active -= 1


def run(action, payload):
    """Only the fixed, trusted operations below can execute in a worker."""
    with slot(), tempfile.TemporaryDirectory(prefix='arena-local-') as temporary:
        root = Path(temporary)
        request, response = root / 'request.json', root / 'response.json'
        request.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        environment = dict(os.environ, ARENA_LOCAL_WORKERS='0')
        started = time.time()
        proc = subprocess.run([sys.executable, '-m', 'pipeline.local_work', action,
                               str(request), str(response)],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode or not response.is_file():
            raise RuntimeError(f'Local {action} worker failed ({proc.returncode}): {proc.stderr[-2000:]}')
        result = json.loads(response.read_text())
        from .artifacts import atomic_json
        atomic_json(Path(payload['bundle']) / 'verification/local-workers' / (uuid.uuid4().hex + '.json'),
                    {'action': action, 'pid': result['pid'], 'started_at': started,
                     'finished_at': time.time(), 'elapsed_seconds': time.time() - started})
        return result['result']


def execute(action, payload):
    bundle = Path(payload['bundle'])
    if action in {'check_spec'}:
        from types import SimpleNamespace
        from .author.agent_designer import _handlers
        handlers = _handlers(bundle, SimpleNamespace(**payload['extraction']),
                             payload.get('trace', {}), level=payload.get('level'),
                             row=payload.get('row') or {})
        return handlers[action](**payload['arguments'])
    if action == 'qualification-preflight':
        from .release import _preflight
        return _preflight(bundle)
    if action == 'release-evaluate':
        from .release import _evaluate
        return _evaluate(bundle, replay=payload.get('replay', True))
    raise ValueError('Unknown local worker operation')


if __name__ == '__main__':
    action, request, response = sys.argv[1:]
    result = execute(action, json.loads(Path(request).read_text()))
    Path(response).write_text(json.dumps({'pid': os.getpid(), 'result': result},
                                        ensure_ascii=False, allow_nan=False))
