"""Exercise actual child interpreters and fail-closed worker errors."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline import local_work
from pipeline.author import agent_designer


class LocalWorkTests(unittest.TestCase):
    def test_author_validation_runs_in_another_process_without_weakening_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            (bundle / 'environment/repo').mkdir(parents=True)
            (bundle / 'environment/repo/source.txt').write_text('Recovered evidence.')
            extraction = SimpleNamespace(session_id='test-source', seed_index=0)
            handlers = agent_designer._handlers(bundle, extraction, {'messages': []})
            with patch.dict(os.environ, {'ARENA_LOCAL_WORKERS': '0'}):
                direct = handlers['check_spec']({})
            with patch.dict(os.environ, {'ARENA_LOCAL_WORKERS': '1'}):
                isolated = handlers['check_spec']({})
            self.assertEqual(isolated, direct)
            self.assertFalse(isolated['clean'])
            receipt = json.loads(next((bundle / 'verification/local-workers').glob('*.json')).read_text())
            self.assertNotEqual(receipt['pid'], os.getpid())
            self.assertEqual(receipt['action'], 'check_spec')
            self.assertEqual(local_work.stats()['active'], 0)

    def test_worker_failure_is_not_a_pass_and_releases_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'Unknown local worker operation'):
                local_work.run('untrusted-operation', {'bundle': temporary})
            self.assertEqual(local_work.stats()['active'], 0)
            self.assertEqual(local_work.stats()['waiting'], 0)
