"""Transport test credentials/state are private temporary files, never workspace keys."""
from types import SimpleNamespace
from pathlib import Path
import tempfile
from unittest.mock import patch

from pipeline import cli, openai_transport
from pipeline.calibrate import agent_openai


def isolated_openai_transport(testcase):
    directory = Path(testcase.enterContext(tempfile.TemporaryDirectory()))
    keys = directory / 'keys.env'
    keys.write_text('OPENAI_API_KEY=test-only\n')
    keys.chmod(0o600)
    testcase.enterContext(patch.object(cli, 'KEYS', keys))
    # A mocked sleep must advance the same clock used by the real cooldown;
    # otherwise a retry can keep polling forever during an offline test.
    clock = SimpleNamespace(now=1000.0)
    def sleep(seconds):
        clock.now += seconds
    sleeper = testcase.enterContext(patch.object(agent_openai.time, 'sleep', side_effect=sleep))
    real_pool = openai_transport.SharedCooldown
    testcase.enterContext(patch.object(openai_transport, 'SharedCooldown',
        side_effect=lambda *args, **kwargs: real_pool(directory / 'cooldown', clock=lambda: clock.now)))
    testcase.enterContext(patch.dict(agent_openai.os.environ, {'ARENA_TRANSPORT_OBSERVATIONS': ''}))
    testcase.enterContext(patch.object(agent_openai.urllib.request, 'urlopen',
        side_effect=AssertionError('Transport tests must supply an explicit mocked provider')))
    return SimpleNamespace(keys=keys, cooldown=directory / 'cooldown', clock=clock, sleep=sleeper)
