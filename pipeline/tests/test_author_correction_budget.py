"""Empty outcome signals must not force authors to invent user corrections."""
import tempfile
import unittest
from pathlib import Path

from pipeline.author.extract import Extraction, usable_correction_count


class CorrectionBudget(unittest.TestCase):
    def material(self, users):
        return Extraction('session-id', 0, (0, len(users)), 0, moments=[
            {'msg': i, 'signal': 'task_outcome_explicit', 'user': user}
            for i, user in enumerate(users)])

    def test_explicit_classified_zero_overrides_raw_signals(self):
        material = self.material(['', 'Looks good'])
        self.assertEqual(usable_correction_count(
            {'classification': {'usable_moments': 0}}, material), 0)
        self.assertEqual([m['user'] for m in material.moments], ['', 'Looks good'])

    def test_unclassified_empty_outcomes_supply_no_quote_capacity(self):
        material = self.material(['', '   ', None, 'Keep the original identifier.'])
        self.assertEqual(usable_correction_count({}, material), 1)
        self.assertEqual(usable_correction_count({}, self.material([''])), 0)

    def test_invalid_classification_counts_fail_before_authoring(self):
        for count in (True, -1, '2', 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                usable_correction_count({'classification': {'usable_moments': count}}, self.material(['x']))

    def test_the_author_route_uses_the_classified_budget(self):
        from pipeline.author.agent_designer import _opening
        row = {'session_id': 'session-id', 'seed_index': 0, 'tier': 4,
               'classification': {'usable_moments': 0}}
        material = self.material([''])
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            fixture = bundle / 'environment/repo'
            fixture.mkdir(parents=True)
            (fixture / 'case.md').write_text('A recovered procedure, without a user correction.\n')
            for prompt in (_opening(row, material, bundle),):
                self.assertIn('0 usable corrections', prompt)
                self.assertIn('cite at least 0', prompt)
                self.assertIn('no trace quotation is required', prompt)


if __name__ == '__main__':
    unittest.main()
