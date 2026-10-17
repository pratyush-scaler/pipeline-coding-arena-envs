"""The binding hardness bar: blind solves on the current grading surface, determinate bites, waivers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import policy
from pipeline.author import creation, difficulty_gate as gate


def _spec():
    return {"steps": [{"name": "01-fix", "instruction": "fix it"}],
            "checks": [{"id": "o_a", "kind": "objective", "trap": "t_a"},
                       {"id": "o_b", "kind": "objective", "trap": "t_b"},
                       {"id": "o_c", "kind": "objective", "trap": "t_c"},
                       {"id": "o_d", "kind": "objective", "trap": "t_d"}],
            "suites": {"test_x.py": "def test_a(): pass"},
            "traps": [{"id": "t_a", "type": "text_semantics", "checks": ["o_a"]},
                      {"id": "t_b", "type": "temporal_semantics", "checks": ["o_b"]},
                      {"id": "t_c", "type": "numeric_semantics", "checks": ["o_c"]},
                      {"id": "t_d", "type": "numeric_semantics", "checks": ["o_d"]}]}


class HardnessGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.bundle = Path(tmp.name); (self.bundle / "verification").mkdir()

    def solve(self, family, checks, reward, spec=None, by_step=None):
        record = {"reward": reward, "checks": checks, "by_step": by_step or {"01-fix": {"reward": reward}},
                  "grading_sha256": gate.grading_sha(spec or _spec())}
        (self.bundle / f"verification/blind-solve-{family}.json").write_text(json.dumps(record))

    def probe(self, *checks):
        for cid in checks:
            gate.link_probe(self.bundle, cid, {"determinate": True}, "case")

    def test_unarmed_or_waived_gate_asks_for_nothing(self):
        self.assertEqual(gate.requirement(_spec(), self.bundle), [])
        gate.arm(self.bundle, "creation")
        self.assertTrue(gate.requirement(_spec(), self.bundle))
        gate.waive(self.bundle, "turn budget")
        self.assertEqual(gate.requirement(_spec(), self.bundle), [])

    def test_a_solve_of_older_tests_does_not_count(self):
        gate.arm(self.bundle, "creation")
        old = _spec(); old["suites"] = {"test_x.py": "older"}
        self.solve("astra", {"o_a": False}, 0.5, spec=old)
        missing = " ".join(gate.requirement(_spec(), self.bundle))
        self.assertIn("blind_solve(model='astra')", missing)

    def test_full_passes_and_undeterminate_bites_keep_creation_open(self):
        gate.arm(self.bundle, "creation")
        self.solve("astra", {c: True for c in ("o_a", "o_b", "o_c", "o_d")}, 1.0)
        self.solve("fable", {"o_a": False, "o_b": False, "o_c": False, "o_d": True}, 0.4)
        missing = " ".join(gate.requirement(_spec(), self.bundle))
        self.assertIn("astra's blind solve fully passes every step", missing)
        self.assertIn("0 independent determinate decision(s) bite fable", missing)      # no probe linked yet
        self.probe("o_a", "o_b", "o_c")
        missing = " ".join(gate.requirement(_spec(), self.bundle))
        self.assertNotIn("bite fable", missing)

    def test_both_families_defeated_by_determinate_diverse_traps_closes_the_gate(self):
        gate.arm(self.bundle, "creation")
        self.solve("astra", {"o_a": False, "o_b": False, "o_c": False, "o_d": True}, 0.3)
        self.solve("fable", {"o_a": True, "o_b": False, "o_c": False, "o_d": False}, 0.4)
        self.probe("o_a", "o_b", "o_c", "o_d")
        self.assertEqual(gate.requirement(_spec(), self.bundle), [])
        with patch.object(policy, "MIN_BINDING_BITING_TYPES", 4):
            self.assertIn("span 3 type(s)", " ".join(gate.requirement(_spec(), self.bundle)))

    def test_checks_of_one_decision_count_once(self):
        gate.arm(self.bundle, "creation")
        spec = _spec()
        spec["quality"] = {"decisions": [{"id": "d_one", "checks": ["o_a", "o_b", "o_c"]}, {"id": "d_two", "checks": ["o_d"]}]}
        self.solve("astra", {"o_a": False, "o_b": False, "o_c": False, "o_d": True}, 0.3, spec=spec)
        self.solve("fable", {"o_a": False, "o_b": False, "o_c": False, "o_d": True}, 0.3, spec=spec)
        self.probe("o_a", "o_b", "o_c", "o_d")
        self.assertIn("1 independent determinate decision(s) bite astra", " ".join(gate.requirement(spec, self.bundle)))

    def test_creation_inventory_lists_the_gate(self):
        gate.arm(self.bundle, "creation")
        missing = creation.inventory(_spec(), self.bundle)["missing_components"]
        self.assertTrue(any(m.startswith("HARDNESS GATE") for m in missing))

    def test_fast_rounds_read_the_regrade_of_the_current_tests(self):
        gate.arm(self.bundle, "fast")
        receipt = {"grading_sha256": gate.grading_sha(_spec()),
                   "by_model": {"openai/gpt-6-astra": {"steps": {"01-fix": {"full": 3, "graded": 5}},
                                                        "checks": {"01-fix/o_a": {"failed": 2}}},
                                "anthropic/claude-fable-5-1": {"steps": {"01-fix": {"full": 0, "graded": 5}},
                                                               "checks": {f"01-fix/o_{c}": {"failed": 4} for c in "abc"}}}}
        (self.bundle / "verification/fast-regrade.json").write_text(json.dumps(receipt))
        missing = " ".join(gate.requirement(_spec(), self.bundle))
        self.assertIn("gpt-6-astra still fully passes step 01-fix in 3 of 5", missing)
        self.assertIn("1 trap(s) fail in 2+ episodes of gpt-6-astra", missing)
        self.assertNotIn("claude-fable-5-1", missing)


if __name__ == "__main__":
    unittest.main()


class Arming(unittest.TestCase):
    def test_the_gate_arms_for_designs_and_hardening_never_for_repairs(self):
        import inspect
        from pipeline.author import agent_designer
        src = inspect.getsource(agent_designer.design)
        self.assertIn('row.get("_harden_kind") not in (None, "repair")', src)
        from pipeline import harden
        self.assertIn("_harden_kind=kind", inspect.getsource(harden))
