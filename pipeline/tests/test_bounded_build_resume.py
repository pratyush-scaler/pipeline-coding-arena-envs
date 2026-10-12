"""A transport recovery cannot repeat a consumed repair or rewrite its fixture."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pipeline import orchestrate as m


class BoundedBuildResumeTests(unittest.TestCase):
    def test_consumed_repair_budget_is_not_replenished_and_workspace_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / 'rich/draft'
            (bundle / 'environment/repo').mkdir(parents=True)
            fixture = bundle / 'environment/repo/source.md'
            fixture.write_text('Previously verified public source transformation')
            (bundle / 'authoring/extraction').mkdir(parents=True)
            (bundle / 'authoring/extraction/extraction.json').write_text('{}')
            (bundle / 'authoring/traps.json').write_text('{}')
            designer = Mock(return_value={'source': {'session_id': 'source', 'seed_index': 0}})
            source = {'session_id': 'source', 'seed_index': 0, 'category': 'A'}
            with patch.object(m, 'GATE_REPAIRS', 1), \
                 patch.object(m, '_slug', return_value='draft'), \
                 patch.object(m.extract_stage, 'extract'), patch.object(m.extract_stage, 'write') as write, \
                 patch('pipeline.ledger.redaction.minimize') as minimize, \
                 patch.object(m.extract_stage, 'usable_correction_count', return_value=0), \
                 patch.object(m.build_stage, 'build'), \
                 patch.object(m.faithfulness, 'build', return_value={'faithfulness_by_bytes': 1}), \
                 patch.object(m.probe_stage, 'run', return_value={'results': {}}), \
                 patch.object(m.controls_stage, 'run', return_value={'ok': True, 'errors': [], 'results': {}, 'advisories': []}), \
                 patch.object(m, 'run_bundle', return_value={'verdict': 'blocked', 'results': [
                     {'gate': 'test', 'blockers': [{'code': 'unresolved', 'message': 'still invalid'}]}]}):
                result = m._build_one(source, 1, root, designer, nullcontext(),
                                      gate_repairs=0, preserve_workspace=True)
            self.assertEqual(result.status, 'blocked', result.error)
            self.assertEqual(result.gate_rounds, 1)
            designer.assert_called_once()
            write.assert_not_called()
            minimize.assert_not_called()
            self.assertEqual(fixture.read_text(), 'Previously verified public source transformation')

    def test_recovery_cannot_increase_repair_budget(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'GATE_REPAIRS', 1), \
             patch.object(m.extract_stage, 'extract') as extract:
            source = {'session_id': 'source', 'seed_index': 0, 'category': 'A'}
            result = m._build_one(source, 1, Path(tmp), Mock(), nullcontext(),
                                  gate_repairs=2, preserve_workspace=True)
            self.assertEqual(result.status, 'error')
            self.assertIn('original budget', result.error)
            extract.assert_not_called()


if __name__ == '__main__':
    unittest.main()
