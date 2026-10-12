"""A later step's instruction must stand on its own.

Every calibration step runs in a fresh session: Codex never sees step 1's
prompt (a-034 in the parent programme), so a step-2 instruction that defines
what counts by pointing at "step 1" or "the rules from before" grades the
model on text it was never shown. The repository carries the earlier code;
the earlier prompt does not travel.
"""
import unittest

from pipeline.author import validate


class LaterStepsRestateWhatGoverns(unittest.TestCase):

    def test_a_reference_to_the_earlier_prompt_is_refused(self):
        for text in ("Same rules as before, but now refunds count too.",
                     "Keep everything from step 1 and add the CSV export.",
                     "As I said earlier, the totals must be per month; now also per client.",
                     "The requirements from above still hold; the export needs a header row now.",
                     "Do the same as the previous step for the weekly view."):
            with self.subTest(text=text):
                issues = validate.history_reference_problems(text, "step 'two' instruction", 2)
                self.assertEqual(len(issues), 1, issues)
                self.assertIn("fresh session", issues[0])

    def test_the_first_step_may_say_anything(self):
        self.assertEqual(validate.history_reference_problems("Same rules as before.", "step 'one'", 1), [])

    def test_a_self_contained_later_step_passes(self):
        text = ("ok so the monthly totals look right now but I forgot, refunds. a refund is a negative line "
                "and it has to come off the month it was issued in, not the month of the original sale. "
                "the export still needs one row per month, oldest first.")
        self.assertEqual(validate.history_reference_problems(text, "step 'two'", 2), [])

    def test_code_state_references_are_fine(self):
        # Pointing at the repository is fine; the code is there. Pointing at
        # the earlier prompt is not.
        text = "the export you added in reports/export.py needs a header row now, and refunds go negative."
        self.assertEqual(validate.history_reference_problems(text, "step 'two'", 2), [])

    def test_the_rule_runs_inside_validate_for_every_step_after_the_first(self):
        import tempfile
        from pathlib import Path
        from pipeline.tests.synthetic_code import spec, write_bundle
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            write_bundle(root, two_steps=True)
            doc = spec(two_steps=True)
            clean = validate.validate(doc, root)
            self.assertEqual([i for i in clean if "fresh session" in i], [], clean)
            doc["steps"][1]["instruction"] = "Same rules as before, plus refunds."
            leaning = validate.validate(doc, root)
            self.assertTrue(any("fresh session" in i for i in leaning), leaning)


if __name__ == "__main__":
    unittest.main()
