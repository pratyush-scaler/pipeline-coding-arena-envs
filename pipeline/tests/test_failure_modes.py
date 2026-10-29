"""Failure modes from the grading surface (author/failure_modes.py, user 2026-10-27).

The fixture is the cricket build's shape: four refusal checks quote one instruction sentence and sit in one lever
plan entry, so their three trap types are one mode; a sentence most checks quote is generic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import policy
from pipeline.author import difficulty_gate as gate, failure_modes as fm

GENERIC = "all of it has to work on any card that can be right."
REFUSE = "if the card can't be right then raise ValueError."


def _check(cid, quotes, weight=5):
    return {"id": cid, "kind": "objective", "weight": weight, "step": "01-fix", "rule_quote": quotes}


def _spec(plan=True):
    checks = [_check("o_refuse_balls", [REFUSE, GENERIC], 9), _check("o_refuse_target", [REFUSE, GENERIC], 9),
              _check("o_refuse_total", [REFUSE, GENERIC], 7), _check("o_refuse_overs", [REFUSE, GENERIC], 6),
              _check("o_abstain", ["give None where the orders disagree.", GENERIC], 7),
              _check("o_partner", ["survivor_at() same thing for who was still in."], 7),
              _check("o_overs", ["overs are cricket overs however they're typed."], 8),
              _check("o_window", ["each wicket goes from the earliest to the latest any order gives."], 6),
              {"id": "g_docs", "kind": "guardrail", "weight": 0, "rule_quote": ["dont touch my documents."]}]
    traps = [{"id": "t_balls", "type": "absence_semantics", "checks": ["o_refuse_balls"]},
             {"id": "t_target", "type": "temporal_semantics", "checks": ["o_refuse_target"]},
             {"id": "t_total", "type": "numeric_semantics", "checks": ["o_refuse_total"]},
             {"id": "t_overs_match", "type": "numeric_semantics", "checks": ["o_refuse_overs"]},
             {"id": "t_abstain", "type": "absence_semantics", "checks": ["o_abstain"]},
             {"id": "t_partner", "type": "architectural_carry", "checks": ["o_partner"]},
             {"id": "t_overs", "type": "numeric_semantics", "checks": ["o_overs"]},
             {"id": "t_window", "type": "exception_interaction", "checks": ["o_window"]}]
    spec = {"steps": [{"name": "01-fix", "instruction": "fix it"}], "checks": checks, "traps": traps,
            "suites": {"test_x.py": "def test_a(): pass"}}
    if plan:
        spec["quality"] = {
            "lever_plan": [{"id": "lp_refuse", "levers": ["must_still_reject", "concept_vs_proxy", "error_path_contract"],
                            "traps": ["t_balls", "t_target", "t_total", "t_overs_match"],
                            "checks": ["o_refuse_balls", "o_refuse_target", "o_refuse_total", "o_refuse_overs"]},
                           {"id": "lp_abstain", "levers": ["abstain_or_resolve", "sibling_path_omitted"],
                            "traps": ["t_abstain", "t_partner"], "checks": ["o_abstain", "o_partner"]},
                           {"id": "lp_overs", "levers": ["format_variant_unseen", "numeric_idiom_betrayal"],
                            "traps": ["t_overs"], "checks": ["o_overs"]}],
            "decisions": [{"id": "d_balls", "checks": ["o_refuse_balls"]}, {"id": "d_target", "checks": ["o_refuse_target"]}]}
    return spec


class Modes(unittest.TestCase):
    def test_one_clause_is_one_mode_whatever_the_trap_labels(self):
        table = {m["id"]: m for m in fm.modes(_spec(plan=False))}
        refusal = [m for m in table.values() if "o_refuse_balls" in m["checks"]]
        self.assertEqual(len(refusal), 1)
        self.assertEqual(refusal[0]["checks"], ["o_refuse_balls", "o_refuse_overs", "o_refuse_target", "o_refuse_total"])
        # the generic sentence does not tie o_abstain to the refusals; guardrails are never modes
        self.assertTrue(any(m["checks"] == ["o_abstain"] for m in table.values()))
        self.assertNotIn("g_docs", fm.mode_of(_spec(plan=False)))
        self.assertIn(GENERIC, fm.generic_clauses(_spec()))

    def test_lever_plan_and_decisions_join_checks_and_give_the_class(self):
        table = {m["id"]: m for m in fm.modes(_spec())}
        self.assertEqual(sorted(table), ["c:o_window", "lp:lp_abstain", "lp:lp_overs", "lp:lp_refuse"])
        self.assertEqual(table["lp:lp_abstain"]["checks"], ["o_abstain", "o_partner"])
        self.assertEqual(table["lp:lp_refuse"]["class"], "scope")            # two scope levers, one idiom
        self.assertEqual(table["lp:lp_overs"]["class"], "representation")    # first of a tie
        self.assertEqual(table["c:o_window"]["class"], "interaction")        # no lever: its trap type's class
        self.assertEqual(table["lp:lp_refuse"]["weight"], 31)

    def test_planted_floors(self):
        missing = " ".join(fm.planted_missing(_spec()))
        self.assertIn("4 failure mode(s) planted (need 6)", missing)
        # scope (refusals), interaction (abstain; the window trap's type), representation (overs)
        self.assertIn("span 3 class(es) ['interaction', 'representation', 'scope'] (need 4)", missing)
        with patch.object(policy, "MIN_PLANTED_FAILURE_MODES", 4), patch.object(policy, "MIN_PLANTED_MODE_CLASSES", 3):
            self.assertEqual(fm.planted_missing(_spec()), [])

    def test_one_shared_mode_is_reported_however_many_checks_fail(self):
        failed = {"astra": {"o_refuse_balls", "o_refuse_target", "o_refuse_total"},
                  "fable": {"o_refuse_balls", "o_refuse_target", "o_refuse_total", "o_refuse_overs"}}
        out = fm.summary(_spec(), failed)
        self.assertEqual(out["shared"], ["lp:lp_refuse"])
        missing = " ".join(out["missing"])
        self.assertIn("1 failure mode(s) defeat EVERY family's blind solve (need 3)", missing)
        self.assertIn("1 failure mode(s) defeat astra's blind solve (need 4)", missing)

    def test_shared_modes_must_span_classes(self):
        spec = _spec()
        failed = {f: {"o_refuse_balls", "o_abstain", "o_overs", "o_window"} for f in ("astra", "fable")}
        self.assertEqual(fm.biting_missing(spec, fm.biting(spec, failed)), [])
        with patch.object(policy, "MIN_SHARED_FAILURE_MODES", 2), patch.object(policy, "MIN_SHARED_MODE_CLASSES", 3):
            narrow = {f: {"o_refuse_balls", "o_refuse_target"} | {"o_abstain"} for f in ("astra", "fable")}
            with patch.object(policy, "MIN_FAILURE_MODES_PER_FAMILY", 1):
                self.assertIn("span 2 class(es)", " ".join(fm.biting_missing(spec, fm.biting(spec, narrow))))

    def test_regrade_counts_an_episode_once_per_mode(self):
        def ep(model, failed):
            return {"model": model, "steps": {"01-fix": {"valid": True, "failed": {c: {} for c in failed}}}}
        receipt = {"episodes": [ep("openai/gpt-6-astra", ["o_refuse_balls", "o_refuse_target"]),
                                ep("openai/gpt-6-astra", ["o_refuse_total", "o_window"]),
                                ep("anthropic/claude-fable-5-1", ["o_window"]),
                                ep("anthropic/claude-fable-5-1", ["o_window", "o_overs"])]}
        out = fm.regrade_modes(_spec(), receipt)
        self.assertEqual(out["gpt-6-astra"], {"lp:lp_refuse"})       # two episodes, one mode
        self.assertEqual(out["claude-fable-5-1"], {"c:o_window"})

    def test_screen_census_names_the_concentration(self):
        per_model = {
            "openai/gpt-6-astra": {"episodes_valid": 5, "checks": {
                "01-fix/o_refuse_balls": {"passed": 0, "failed": 5}, "01-fix/o_refuse_target": {"passed": 0, "failed": 5},
                "01-fix/o_abstain": {"passed": 5, "failed": 0}}},
            "anthropic/claude-fable-5-1": {"episodes_valid": 5, "checks": {
                "01-fix/o_refuse_balls": {"passed": 0, "failed": 5}, "01-fix/o_window": {"passed": 3, "failed": 2},
                "01-fix/o_abstain": {"passed": 5, "failed": 0}}}}
        out = fm.census(_spec(), per_model)
        self.assertEqual(out["defeated_both"], ["lp:lp_refuse"])
        self.assertEqual(out["defeated"]["claude-fable-5-1"], ["c:o_window", "lp:lp_refuse"])
        self.assertEqual(out["loss_concentration"]["gpt-6-astra"], {"largest_mode": "lp:lp_refuse", "share": 1.0})
        reasons = " ".join(fm.census_reasons({"failure_modes": out}))
        self.assertIn("1 of 4 defeated both models", reasons)
        self.assertIn("gpt-6-astra lost 100% of its points to one mode", reasons)

    def test_advisories_and_brief_carry_the_policy_numbers(self):
        self.assertTrue(any("failure mode(s) planted" in a for a in fm.advisories(_spec())))
        text = fm.brief()
        for value in (policy.MIN_PLANTED_FAILURE_MODES, policy.MIN_SHARED_FAILURE_MODES,
                      policy.MIN_FAILURE_MODES_PER_FAMILY, policy.MIN_PLANTED_MODE_CLASSES):
            self.assertIn(str(value), text)


class GateReadsModes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.bundle = Path(tmp.name); (self.bundle / "verification").mkdir()

    def solve(self, family, failed, spec):
        checks = {c["id"]: c["id"] not in failed for c in spec["checks"] if c["kind"] == "objective"}
        record = {"reward": 0.5, "checks": checks, "by_step": {"01-fix": {"reward": 0.5}},
                  "grading_sha256": gate.grading_sha(spec)}
        (self.bundle / f"verification/blind-solve-{family}.json").write_text(json.dumps(record))

    def test_creation_gate_asks_for_modes_not_labels(self):
        spec = _spec()
        gate.arm(self.bundle, "creation")
        for cid in [c["id"] for c in spec["checks"]]:
            gate.link_probe(self.bundle, cid, {"determinate": True}, "case")
        refusals = {"o_refuse_balls", "o_refuse_target", "o_refuse_total", "o_refuse_overs"}
        self.solve("astra", refusals, spec)
        self.solve("fable", refusals, spec)
        status = gate.creation_status(spec, self.bundle)
        self.assertEqual(status["failure_modes"], {"astra": ["lp:lp_refuse"], "fable": ["lp:lp_refuse"]})
        missing = " ".join(gate.requirement(spec, self.bundle))
        self.assertIn("HARDNESS GATE: 1 failure mode(s) defeat EVERY family's blind solve (need 3)", missing)
        self.assertIn("failure mode(s) planted (need 6)", missing)

    def test_undeterminate_checks_do_not_count_as_bites(self):
        spec = _spec()
        gate.arm(self.bundle, "creation")
        wide = {"o_refuse_balls", "o_abstain", "o_overs", "o_window"}
        self.solve("astra", wide, spec)
        self.solve("fable", wide, spec)
        self.assertEqual(gate.creation_status(spec, self.bundle)["failure_modes"], {"astra": [], "fable": []})

    def test_fast_gate_reads_modes_from_the_episodes(self):
        spec = _spec()
        gate.arm(self.bundle, "fast")

        def ep(model, failed):
            return {"model": model, "steps": {"01-fix": {"valid": True, "full": False, "failed": {c: {} for c in failed}}}}
        episodes = [ep(m, ["o_refuse_balls"]) for m in ("openai/gpt-6-astra",) * 2 + ("anthropic/claude-fable-5-1",) * 2]
        receipt = {"grading_sha256": gate.grading_sha(spec), "episodes": episodes,
                   "by_model": {"openai/gpt-6-astra": {"steps": {"01-fix": {"graded": 2, "full": 0}},
                                                       "checks": {"01-fix/o_refuse_balls": {"failed": 2}}},
                                "anthropic/claude-fable-5-1": {"steps": {"01-fix": {"graded": 2, "full": 0}},
                                                               "checks": {"01-fix/o_refuse_balls": {"failed": 2}}}}}
        (self.bundle / "verification/fast-regrade.json").write_text(json.dumps(receipt))
        missing = " ".join(gate.requirement(spec, self.bundle))
        self.assertIn("defeat EVERY family's re-graded episodes (need 3)", missing)


class LineageTarget(unittest.TestCase):
    def test_one_shared_mode_does_not_end_hardening(self):
        from pipeline import harden
        record = {"failure_modes": {"defeated_both": ["lp:lp_refuse"], "defeated_both_classes": ["scope"]}}
        self.assertFalse(harden.diverse_modes_met(record))
        record["failure_modes"] = {"defeated_both": ["a", "b", "c"], "defeated_both_classes": ["scope"]}
        self.assertFalse(harden.diverse_modes_met(record))                      # one class
        record["failure_modes"]["defeated_both_classes"] = ["scope", "interaction"]
        self.assertTrue(harden.diverse_modes_met(record))
        self.assertFalse(harden.diverse_modes_met({}))                             # unreadable never meets it
        with patch.object(policy, "HARDENING_TARGET_NEEDS_DIVERSE_MODES", False):
            self.assertTrue(harden.diverse_modes_met({}))

    def test_a_screen_without_a_census_is_computed_from_its_bundle(self):
        from pipeline import harden
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "authoring").mkdir()
            (Path(tmp) / "authoring/spec.json").write_text(json.dumps(_spec()))
            per_model = {m: {"episodes_valid": 5, "checks": {"01-fix/o_refuse_balls": {"failed": 5},
                                                             "01-fix/o_window": {"failed": 5},
                                                             "01-fix/o_overs": {"failed": 4}}}
                         for m in ("x/astra", "y/fable")}
            census = harden.screen_failure_modes({"bundle_path": tmp, "per_model": per_model})
            self.assertEqual(census["defeated_both"], ["c:o_window", "lp:lp_overs", "lp:lp_refuse"])
            self.assertTrue(harden.diverse_modes_met({"bundle_path": tmp, "per_model": per_model}))

    def test_a_target_met_stop_under_the_older_rule_is_obsolete(self):
        from pipeline import harden
        older = {"status": "target-met", "target_pass_at_1": policy.EXTREME_TARGET_PASS_AT_1,
                 "step_pass_rate_max": policy.STEP_PASS_RATE_MAX}
        self.assertTrue(harden.obsolete_target_stop(older))                         # judged without the mode floor
        self.assertFalse(harden.obsolete_target_stop({**older, "shared_failure_modes_min": policy.MIN_SHARED_FAILURE_MODES}))
        with patch.object(policy, "HARDENING_TARGET_NEEDS_DIVERSE_MODES", False):
            self.assertFalse(harden.obsolete_target_stop(older))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "hardened").mkdir()
            harden._save(Path(tmp), dict(older))
            self.assertEqual(harden.read_ledger(Path(tmp))["shared_failure_modes_min"], policy.MIN_SHARED_FAILURE_MODES)


class IdleConcession(unittest.TestCase):
    GAP = ["HARDNESS GATE: 1 failure mode(s) defeat EVERY family's re-graded episodes (need 3): ['lp:lp_refuse']"]

    def test_identical_measured_holds_concede_on_the_policy_count(self):
        from pipeline.author.agent_designer import idle_concession
        state, spec = {}, _spec()
        results = [idle_concession(state, spec, self.GAP) for _ in range(policy.HARDNESS_GATE_IDLE_SUBMITS)]
        self.assertEqual(results, [False] * (policy.HARDNESS_GATE_IDLE_SUBMITS - 1) + [True])

    def test_a_changed_draft_or_gap_starts_the_count_again(self):
        from pipeline.author.agent_designer import idle_concession
        state, spec = {}, _spec()
        for _ in range(policy.HARDNESS_GATE_IDLE_SUBMITS - 1):
            idle_concession(state, spec, self.GAP)
        changed = _spec(); changed["checks"][0]["weight"] = 10
        self.assertFalse(idle_concession(state, changed, self.GAP))
        self.assertEqual(state["n"], 1)
        self.assertFalse(idle_concession(state, changed, self.GAP + ["HARDNESS GATE: something else"]))
        self.assertEqual(state["n"], 1)

    def test_a_missing_measurement_or_another_component_never_concedes(self):
        from pipeline.author.agent_designer import idle_concession
        state, spec = {}, _spec()
        unmeasured = ["HARDNESS GATE: a regrade_episodes of the current hidden tests"]
        other = self.GAP + ["a reference patch for step 01-fix"]
        for _ in range(policy.HARDNESS_GATE_IDLE_SUBMITS + 2):
            self.assertFalse(idle_concession(state, spec, unmeasured))
            self.assertFalse(idle_concession(state, spec, other))
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()
