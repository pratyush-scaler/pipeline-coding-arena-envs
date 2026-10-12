"""A grader refusal of the submitted tree is void, never a failure -- and, like a lost
container, it is replaced by a top-up (2026-10-04: four screens stayed one episode
short because top-ups counted only natively valid trials)."""
import unittest

from pipeline import release, screen


class AttributionVoids(unittest.TestCase):
    def test_a_refused_tree_does_not_count_as_a_scored_episode(self):
        self.assertEqual(release._scored({"trials": [{}] * 5}, {"capability": 4, "void": 1}), 4)

    def test_without_attribution_the_native_count_stands(self):
        self.assertEqual(release._scored({"trials": [{}] * 3}, {}), 3)

    def test_the_screen_tops_up_an_attribution_shortfall(self):
        row = {"campaign": "c", "evidence_problems": [],
               "attribution_problems": ["1 void episodes leave 4 valid (need 5). re-run them"]}
        self.assertTrue(screen._short_only(row))
        self.assertFalse(screen._short_only({**row, "attribution_problems": []}))
        self.assertFalse(screen._short_only({**row, "evidence_problems": ["evidence changed"]}))


if __name__ == "__main__":
    unittest.main()
