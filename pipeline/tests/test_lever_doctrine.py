"""The coding lever doctrine reaches every phase, and its audit is advisory and correct.

User, 2026-10-03: "introduce as many difficulty levers ... while authoring the tasks
and while hardening them ... relevant for the traces ... enhance the complete
pipeline with these learnings".
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from pipeline import policy
from pipeline.author import levers


def plan_spec(**overrides):
    spec = {
        "steps": [{"name": "01-a", "instruction": "keep the ids as text please"},
                  {"name": "02-b", "instruction": "now the export too"}],
        "checks": [{"id": "o_a", "kind": "objective", "step": "01-a"},
                   {"id": "o_b", "kind": "objective", "step": "02-b"},
                   {"id": "g_a", "kind": "guardrail", "step": "01-a"}],
        "traps": [{"id": "t_a"}, {"id": "t_b"}],
        "mutants": [{"id": "m_baseline_a", "fails": ["o_a"]}, {"id": "m_partial_a1", "fails": ["o_a"]},
                    {"id": "m_partial_a2", "fails": ["o_a"]},
                    {"id": "m_baseline_b", "fails": ["o_b"]}, {"id": "m_partial_b1", "fails": ["o_b"]},
                    {"id": "m_partial_b2", "fails": ["o_b"]}, {"id": "m_partial_b3", "fails": ["o_b"]}],
        "suites": {"data/hidden.txt": "a distinctive hidden line that nobody types\n"},
        "quality": {"lever_plan": [
            {"id": "lp_a", "step": "01-a", "levers": ["rule_scope_overreach", "format_variant_unseen"],
             "checks": ["o_a"], "traps": ["t_a"], "baseline_mutant": "m_baseline_a",
             "partial_mutants": ["m_partial_a1", "m_partial_a2"], "rule_quote": ["keep the ids as text"],
             "divergence": "a hidden id with a sibling placeholder inside a fence", "unlabelled": "no comment or sample shows it"},
            {"id": "lp_b", "step": "02-b", "levers": ["standing_rule_carry", "identity_collision", "sibling_path_omitted"],
             "checks": ["o_b"], "traps": ["t_b"], "baseline_mutant": "m_baseline_b",
             "partial_mutants": ["m_partial_b1", "m_partial_b2", "m_partial_b3"], "rule_quote": ["now the export too"],
             "divergence": "the new export path writes ids as numbers", "unlabelled": "nothing names the export"}]},
    }
    spec.update(overrides)
    return spec


class Catalogue(unittest.TestCase):
    def test_every_lever_is_complete_and_its_signals_compile(self):
        for name, meta in levers.LEVERS.items():
            with self.subTest(lever=name):
                self.assertIn(meta["targets"], levers.FAMILIES)
                self.assertIn(meta["status"], ("measured", "external", "hypothesis", "interlock"))
                self.assertGreater(len(meta["evidence"]), 40)
                self.assertGreater(len(meta["recipe"]), 60)
                for pattern in meta.get("signals") or []:
                    re.compile(pattern)

    def test_interlock_recipes_name_catalogued_parts(self):
        singles = {n for n, m in levers.LEVERS.items() if m["status"] != "interlock"}
        for name in levers.INTERLOCK_RECIPES:
            parts = {n for n in singles if n in levers.LEVERS[name]["recipe"]}
            self.assertGreaterEqual(len(parts), 2, name)

    def test_both_families_have_measured_levers(self):
        measured = {m["targets"] for m in levers.LEVERS.values() if m["status"] == "measured"}
        self.assertTrue({"fable", "astra"} <= measured)

    def test_the_doctrine_names_the_dead_levers_and_the_admission_test(self):
        for text in ("INTERLOCK", "IDIOMATIC BASELINE", "m_baseline_", "m_partial_", "UNLABELLED",
                     "DETERMINATE", "quality.lever_plan", "symptom vs cause"):
            self.assertIn(text, levers.PROMPT, text)
        self.assertIn(f"{policy.STEP_PASS_RATE_MAX:.0%}", levers.PROMPT)


class Audit(unittest.TestCase):
    def test_a_complete_plan_has_no_structural_advisory(self):
        out = levers.diagnostics(plan_spec())
        self.assertEqual(out["per_step"]["01-a"]["interlocks"], 1)
        self.assertEqual(out["per_step"]["02-b"]["interlocks"], 1)
        structural = [a for a in out["advisories"] if "lever_plan[" in a or "interlock(s)" in a]
        self.assertEqual(structural, [])
        self.assertGreaterEqual(len(out["distinct_levers"]), 5)

    def test_single_levers_missing_mutants_and_uncovered_steps_are_reported(self):
        spec = plan_spec()
        spec["quality"]["lever_plan"] = [{"id": "lp_x", "step": "01-a", "levers": ["rule_scope_overreach"],
                                          "checks": ["o_a", "g_a"], "baseline_mutant": "m_baseline_nope"}]
        text = "\n".join(levers.diagnostics(spec)["advisories"])
        self.assertIn("single-mechanism", text)
        self.assertIn("'g_a' is not a declared objective", text)
        self.assertIn("baseline mutant 'm_baseline_nope' is not in mutants", text)
        self.assertIn("step 02-b: 0 interlock(s)", text)
        self.assertIn("no lever aimed at astra", text)

    def test_missing_plan_is_advisory_and_never_raises(self):
        out = levers.diagnostics({"steps": [{"name": "01", "instruction": "x"}]})
        self.assertTrue(any("lever_plan is missing" in a for a in out["advisories"]))
        self.assertIn("advisory only", out["note"])

    def test_announcing_words_and_quoted_hidden_data_are_flagged(self):
        spec = plan_spec()
        spec["steps"][0]["instruction"] = ("note that there is a tricky edge case: "
                                           "a distinctive hidden line that nobody types")
        text = "\n".join(levers.diagnostics(spec)["advisories"])
        self.assertIn("announcing", text)
        self.assertIn("quotes hidden test data", text)

    def test_fixture_tells_and_agent_instruction_files_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "environment/repo"
            repo.mkdir(parents=True)
            (repo / "parse.py").write_text("# handle the hidden test edge case here\n")
            (repo / "AGENTS.md").write_text("plan: fix the parser\n")
            text = "\n".join(levers.diagnostics(plan_spec(), Path(tmp))["advisories"])
        self.assertIn("parse.py", text)
        self.assertIn("AGENTS.md", text)


class Relevance(unittest.TestCase):
    def test_the_trace_code_ranks_the_levers_it_can_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text(
                "import re, json\nfrom datetime import datetime\n"
                "def clean(s):\n    return re.sub(r'\\(Visual:.*?\\)', '', s)\n"
                "def total(rows):\n    return round(sum(float(r['amount']) for r in rows), 2)\n"
                "def save(x):\n    open('out.json','w').write(json.dumps(x))\n")
            ranked = levers.relevant({"moments": [{"cls": "spec_correction"}]}, repo)
        names = [r["lever"] for r in ranked]
        self.assertIn("rule_scope_overreach", names)
        self.assertIn("numeric_idiom_betrayal", names)
        self.assertLessEqual(len(ranked), policy.RELEVANT_LEVERS_SHOWN)
        pairs = levers.suggested_interlocks(ranked)
        self.assertTrue(pairs)
        text = levers.relevant_text({"moments": [{"cls": "spec_correction"}]}, repo)
        self.assertIn("LEVERS THIS TRACE SUPPORTS", text)

    def test_selection_reads_lever_support_without_a_model_call(self):
        from pipeline.select import mechanisms
        cls = {"failure_mode": "the regex strips every bracket", "checkable_rules": ["only (Visual: ...) goes"],
               "moments": [{"cls": "spec_correction"}]}
        supported = mechanisms.levers_supported(cls)
        self.assertIn("rule_scope_overreach", supported)
        self.assertGreater(mechanisms.bonus(cls), mechanisms.bonus({}))
        self.assertGreaterEqual(mechanisms.bonus(cls), mechanisms.bonus(cls, used=supported))


class EveryPhaseReadsIt(unittest.TestCase):
    def test_designer_prompt_leads_with_the_doctrine_and_drops_the_dead_advice(self):
        from pipeline.author import agent_designer as a
        self.assertTrue(a.SYSTEM.startswith(levers.PROMPT))
        self.assertNotIn("A DECOY LOCATION: the tempting place to patch", a.SYSTEM)
        self.assertIn("quality.lever_plan", a.SYSTEM)

    def test_hardening_brief_carries_interlocks_not_the_dead_list(self):
        from pipeline import harden
        self.assertIn("BUILD INTERLOCKS, NOT SINGLE TRAPS", harden.HARDENING_BRIEF)
        self.assertNotIn("- CHAINED DEFECTS: B is reachable only after A is fixed.", harden.HARDENING_BRIEF)

    def test_reviewer_reads_the_lever_plan_as_advisory(self):
        from pipeline import review
        self.assertIn("quality.lever_plan", review.PROMPT if hasattr(review, "PROMPT") else
                      open(review.__file__).read())


class PerStepBar(unittest.TestCase):
    def _record(self, tmp, rates):
        per_model = {}
        for model, steps in rates.items():
            camp = Path(tmp) / model.replace("/", "_")
            camp.mkdir()
            trials = []
            for i in range(5):
                trials.append({"valid": True, "steps": [
                    {"name": s, "valid": True, "reward": 1.0 if i < round(r * 5) else 0.5} for s, r in steps.items()]})
            (camp / "report.json").write_text(json.dumps({"trials": trials}))
            per_model[model] = {"campaign": str(camp), "pass_at_1": 0.0}
        return {"measurement_complete": True, "models": list(rates), "per_model": per_model, "index": {}}

    def test_a_whole_task_zero_carried_by_one_step_does_not_end_the_lineage(self):
        from pipeline import harden
        with tempfile.TemporaryDirectory() as tmp:
            record = self._record(tmp, {"a/fable": {"01": 0.2, "02": 0.0},
                                        "b/astra": {"01": 1.0, "02": 0.0}})
            self.assertTrue(harden.extreme_target_met(record))
            self.assertEqual(harden.steps_too_easy(record), {"a/fable": [], "b/astra": ["01"]})
            self.assertFalse(harden.lineage_target_met(record))

    def test_every_step_under_the_bar_meets_it(self):
        from pipeline import harden
        with tempfile.TemporaryDirectory() as tmp:
            record = self._record(tmp, {"a/fable": {"01": 0.2, "02": 0.0},
                                        "b/astra": {"01": 0.4, "02": 0.0}})
            self.assertTrue(harden.lineage_target_met(record))

    def test_unreadable_rates_never_meet_it(self):
        from pipeline import harden
        record = {"measurement_complete": True, "models": ["a", "b"],
                  "per_model": {"a": {"pass_at_1": 0.0, "campaign": "/nonexistent"},
                                "b": {"pass_at_1": 0.0, "campaign": "/nonexistent"}}}
        self.assertIsNone(harden.step_pass_rates(record))
        self.assertFalse(harden.lineage_target_met(record))


if __name__ == "__main__":
    unittest.main()
