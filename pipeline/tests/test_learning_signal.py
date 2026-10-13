"""Arena spec, Oct 12: a measured task's learning signal, and the failure-cause columns of the cover sheet."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import failure_report, learning_signal, policy

S = "01-fix"


def spec(floor_weight=10):
    checks = [
        {"id": "o_free", "kind": "objective", "weight": floor_weight, "band": "floor", "step": S, "trap": None},
        {"id": "o_a", "kind": "objective", "weight": 20, "band": "core", "step": S, "trap": "t_a"},
        {"id": "o_a2", "kind": "objective", "weight": 10, "band": "core", "step": S, "trap": "t_a"},
        {"id": "o_b", "kind": "objective", "weight": 20, "band": "core", "step": S, "trap": "t_b"},
        {"id": "o_c", "kind": "objective", "weight": 20, "band": "stretch", "step": S, "trap": "t_c"},
        {"id": "g_keep", "kind": "guardrail", "weight": 0, "step": S},
    ]
    traps = [{"id": "t_a", "type": "temporal_state", "beatable_by": "Read the stated clock rule."},
             {"id": "t_b", "type": "representation_collision", "beatable_by": "Merge the aliases."},
             {"id": "t_c", "type": "semantic_decoy", "beatable_by": "Use the live module."}]
    return {"checks": checks, "traps": traps}


def counts(passed, failed):
    return {"passed": passed, "failed": failed, "gated_out": 0}


class Summary(unittest.TestCase):
    def per_model(self, rewards_f, rewards_a, fails):
        rows = {}
        for model, rewards in (("anthropic/fable", rewards_f), ("openai/astra", rewards_a)):
            rows[model] = {"episode_rewards": rewards, "checks": {
                f"{S}/o_free": counts(10, 0), f"{S}/o_a": counts(10 - fails[0], fails[0]),
                f"{S}/o_a2": counts(10 - fails[0], fails[0]), f"{S}/o_b": counts(10 - fails[1], fails[1]),
                f"{S}/o_c": counts(10 - fails[2], fails[2])}}
        return rows

    def test_spread_independent_decisions_pass(self):
        s = learning_signal.summary(spec(), self.per_model([0.4, 0.8, 1.0, 0.6] * 2 + [0.2, 1.0],
                                                           [0.2, 0.6, 0.4, 0.8] * 2 + [0.2, 0.4], (5, 4, 6)))
        self.assertEqual(s["independent_hard"], 3)           # o_a and o_a2 share t_a: one decision
        self.assertAlmostEqual(s["free_weight_share"], 10 / 80, places=3)
        self.assertEqual(learning_signal.problems(s), [])
        self.assertEqual(s["decisive_types"][0], "temporal_state")

    def test_the_arena_shape_is_refused(self):
        # 70% free, one trap with two linked checks, every run 0.72: Arena's "nothing to learn from".
        sp = {"checks": [
            {"id": "o_free", "kind": "objective", "weight": 70, "band": "floor", "step": S, "trap": None},
            {"id": "o_clock", "kind": "objective", "weight": 15, "band": "core", "step": S, "trap": "t_clock"},
            {"id": "o_clock2", "kind": "objective", "weight": 15, "band": "core", "step": S, "trap": "t_clock"}],
            "traps": [{"id": "t_clock", "type": "temporal_state"}]}
        rows = {m: {"episode_rewards": [0.72] * 10, "checks": {f"{S}/o_free": counts(10, 0),
                                                               f"{S}/o_clock": counts(0, 10), f"{S}/o_clock2": counts(0, 10)}}
                for m in ("anthropic/fable", "openai/astra")}
        s = learning_signal.summary(sp, rows)
        found = learning_signal.problems(s)
        self.assertTrue(any("passed by every run" in p for p in found), found)
        self.assertTrue(any("independent hard decision" in p for p in found), found)
        self.assertFalse(any("flat" in p for p in found), found)            # spread is never a refusal
        self.assertEqual(sum("flat" in a for a in learning_signal.advisories(s)), 2)

    def test_all_fail_with_varied_partial_scores_is_not_flat(self):
        s = learning_signal.summary(spec(), self.per_model([0.2, 0.5, 0.7, 0.4, 0.6], [0.1, 0.3, 0.6, 0.2, 0.5], (3, 3, 3)))
        self.assertFalse(any("flat" in p for p in learning_signal.problems(s) + learning_signal.advisories(s)))
        self.assertEqual(s["spread"]["anthropic/fable"]["full_passes"], 0)

    def test_episode_rewards_skip_voids_and_average_steps(self):
        measured = {"rollouts": [
            {"episode": 1, "step": "a", "reward": 1.0}, {"episode": 1, "step": "b", "reward": 0.5},
            {"episode": 2, "step": "a", "reward": 0.0, "zero_class": "void"}, {"episode": 3, "step": "a", "reward": 0.2},
            {"episode": 3, "step": "b", "reward": 0.4}]}
        self.assertEqual(learning_signal.episode_rewards(measured), [0.75, 0.3])


class BatchAndDesign(unittest.TestCase):
    def test_over_used_decisive_types(self):
        sig = {f"t{i}": {"decisive_types": ["temporal_state"]} for i in range(3)}
        sig.update({f"u{i}": {"decisive_types": [f"type_{i}"]} for i in range(5)})
        ledger = learning_signal.batch_ledger(sig)
        self.assertEqual(ledger["over_used"], ["temporal_state"])     # 3/8 > 25%
        self.assertEqual(ledger["tasks"], 8)

    def test_design_advisories(self):
        self.assertEqual(learning_signal.design_advisories(spec()), [])
        heavy = spec(floor_weight=200)
        found = learning_signal.design_advisories(heavy)
        self.assertTrue(any("floor-band" in a for a in found), found)
        thin = spec()
        for c in thin["checks"]:
            if c.get("trap") in ("t_b", "t_c"):
                c["trap"] = "t_a"
        self.assertTrue(any("independent decision" in a for a in learning_signal.design_advisories(thin)))

    def test_guidance_names_over_used_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "waves/decisive-traps.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"over_used": ["temporal_state"]}))
            orig = learning_signal.ledger_path
            learning_signal.ledger_path = lambda: path
            try:
                self.assertIn("temporal_state", learning_signal.guidance())
            finally:
                learning_signal.ledger_path = orig
        self.assertIn(str(policy.SIGNAL_MIN_INDEPENDENT_HARD), learning_signal.GUIDANCE)


class FailureCauses(unittest.TestCase):
    def test_cover_row_from_the_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "task"
            (pkg / "authoring").mkdir(parents=True)
            (pkg / "verification").mkdir()
            (pkg / "authoring/spec.json").write_text(json.dumps(spec()))
            episodes = [{"model": "anthropic/fable", "valid": True, "verdict": "capability",
                         "failures": [{"step": S, "check": "o_a", "kind": "assertion", "published": ["Use the stated clock."]}]},
                        {"model": "openai/astra", "valid": True, "verdict": "capability",
                         "failures": [{"step": S, "check": "o_a", "kind": "assertion"},
                                      {"step": S, "check": "o_b", "kind": "repo_crash"}]},
                        {"model": "openai/astra", "valid": False, "verdict": "void", "failures": []}]
            (pkg / "verification/genuine.json").write_text(json.dumps({"episodes": episodes}))
            doc = failure_report.write(pkg, signal={"independent_hard": 3, "free_weight_share": 0.12, "spread": {}})
            row = failure_report.cover_row(doc)
            self.assertEqual(row["decisive_trap_types"][0], "temporal_state")
            self.assertTrue(row["why_models_failed"][0].startswith("o_a [temporal_state; failed fable 1, astra 1]"))
            self.assertEqual(row["failure_kinds"], {"assertion": 2, "repo_crash": 1})
            self.assertEqual(doc["models"]["openai/astra"]["valid_episodes"], 1)      # the void is not counted
            self.assertTrue((pkg / "FAILURES.md").is_file())


if __name__ == "__main__":
    unittest.main()
