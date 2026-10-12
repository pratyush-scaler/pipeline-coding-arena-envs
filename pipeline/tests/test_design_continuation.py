import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline.artifacts import atomic_json, evidence_inventory
from pipeline.author.agent_designer import restore_design_session


class DesignContinuationTests(unittest.TestCase):
    def checkpoint(self, root, config=None):
        session = root / 'verification/design-sessions/original'
        atomic_json(session / 'request-config.json', config or {'max_steps': 140})
        atomic_json(session / 'conversation.json', [
            {'role': 'user', 'content': 'original source and instructions'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'one', 'name': 'read_fixture', 'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'one', 'content': 'retained source evidence'}]},
        ])
        for i in range(1, 3):
            atomic_json(session / f'response-{i:03d}.json', {'response': i})
        atomic_json(root / 'environment/repo/source.json', {'evidence': 'unchanged'})
        return {'session': str(session.relative_to(root)),
                'session_files': evidence_inventory(root, [str(session.relative_to(root))]),
                'workspace_files': evidence_inventory(root, ['environment/repo', 'provenance']),
                'remaining_steps': 138}

    def test_resume_preserves_exploration_without_a_staged_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = self.checkpoint(root)
            restored = restore_design_session(root, checkpoint)
            self.assertIsNone(restored['draft'])
            self.assertEqual(restored['remaining_steps'], 138)
            self.assertEqual(restored['messages'][-1]['content'][0]['content'], 'retained source evidence')

    def test_resume_rejects_changed_sources_conversation_and_inflated_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = self.checkpoint(root)
            invalid = copy.deepcopy(checkpoint)
            invalid['remaining_steps'] = 140
            with self.assertRaisesRegex(ValueError, 'budget'):
                restore_design_session(root, invalid)
            atomic_json(root / 'environment/repo/source.json', {'evidence': 'changed'})
            with self.assertRaisesRegex(ValueError, 'workspace changed'):
                restore_design_session(root, checkpoint)
            atomic_json(root / checkpoint['session'] / 'conversation.json', [])
            with self.assertRaisesRegex(ValueError, 'conversation changed'):
                restore_design_session(root, checkpoint)

    def test_first_resumed_request_uses_original_tool_history(self):
        from pipeline.author import agent_designer as agent
        from pipeline.author.quality import QUALITY_PROMPT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {'max_steps': 140, 'model': agent.MODEL, 'effort': agent.ARENA_EFFORT,
                      'max_tokens': 48000, 'tools': [], 'system': agent.SYSTEM}
            checkpoint = self.checkpoint(root, config)
            class RequestObserved(Exception):
                pass
            def observe(body):
                self.assertEqual(body['messages'][0]['content'], 'original source and instructions')
                self.assertEqual(body['messages'][-1]['content'][0]['tool_use_id'], 'one')
                raise RequestObserved()
            with patch('pipeline.dump.recover.load_trace', return_value={'messages': []}), \
                 patch.object(agent, '_handlers', return_value={}), \
                 patch.object(agent, '_tools', return_value=[]), \
                 patch.object(agent, '_opening', return_value='new opening must not replace history'), \
                 patch('pipeline.privacy.scan', return_value=[]), \
                 patch.object(agent, '_call', side_effect=observe):
                with self.assertRaises(RequestObserved):
                    agent.design({'category': 'A'}, SimpleNamespace(session_id='source', seed_index=0),
                                 root, max_steps=138, resume_session=checkpoint)


if __name__ == '__main__':
    unittest.main()
