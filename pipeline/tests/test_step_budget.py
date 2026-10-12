"""Most tasks single-step; ~10% multi-step; every step held to the per-step bar.

User, 2026-10-03: "most of the tasks single step ... only 10% of the tasks need to
be multi-step ... all the steps are extremely hardened and meet the criteria of
pass@1<=0.5".
"""
import unittest

from pipeline import harden, orchestrate, policy
from pipeline.author import validate


def row(i, later=0, usable=0):
    classes = {"spec_correction": later} if later else {}
    return {"session_id": f"s{i}", "seed_index": 0,
            "classification": {"classes": classes, "usable_moments": usable}}


class Assignment(unittest.TestCase):
    def test_about_ten_percent_multi_step_chosen_by_later_requirement_changes(self):
        rows = [row(i) for i in range(25)] + [row(100 + i, later=i + 1, usable=i) for i in range(5)]
        designated = orchestrate.assign_step_budgets(rows)
        self.assertEqual(designated, round(30 * policy.MULTI_STEP_SHARE))
        multi = [r["session_id"] for r in rows if r["step_budget"] > 1]
        self.assertEqual(sorted(multi), ["s102", "s103", "s104"])
        self.assertTrue(all(r["step_budget"] == 1 for r in rows if r["session_id"] not in multi))
        self.assertTrue(all(r["step_budget"] == policy.MULTI_STEP_MAX_STEPS for r in rows if r["session_id"] in multi))

    def test_a_trace_without_a_later_requirement_is_never_multi_step(self):
        rows = [row(i, usable=9) for i in range(20)]
        self.assertEqual(orchestrate.assign_step_budgets(rows), 0)
        self.assertTrue(all(r["step_budget"] == 1 for r in rows))


class Enforcement(unittest.TestCase):
    def test_validation_refuses_more_steps_than_the_budget(self):
        import hashlib, tempfile
        from pathlib import Path
        from pipeline.artifacts import atomic_json
        from pipeline.tests import synthetic_code
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provenance = {}
            for rel, text in synthetic_code.repo_files().items():
                path = root / "environment/repo" / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                provenance[rel] = {"reason": "Synthetic source for testing",
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            atomic_json(root / "authoring/extraction/fixture-authored.json", {"files": provenance})
            spec = synthetic_code.spec(two_steps=True)
            spec["step_budget"] = 1
            issues = validate.validate(spec, root)
            self.assertTrue(any("exceed this task's step budget of 1" in i for i in issues), issues)
            spec["step_budget"] = 2
            self.assertFalse(any("step budget" in i for i in validate.validate(spec, root)))
            del spec["step_budget"]
            self.assertFalse(any("step budget" in i for i in validate.validate(spec, root)))

    def test_a_hardening_revision_cannot_add_steps_past_the_budget(self):
        before = {"step_budget": 1, "steps": [{"name": "01"}], "checks": []}
        after = {"step_budget": 1, "steps": [{"name": "01"}, {"name": "02"}], "checks": []}
        self.assertTrue(any("step budget is 1" in e for e in harden.revision_problems(before, after, [], new_lineage=True)))

    def test_the_designer_is_told(self):
        from pipeline.author import agent_designer as a
        self.assertIn("STEP BUDGET", a.SYSTEM)
        self.assertIn("SINGLE-STEP", a.SYSTEM)
        self.assertIn("MOST TASKS HAVE ONE STEP", a.SYSTEM)


if __name__ == "__main__":
    unittest.main()
