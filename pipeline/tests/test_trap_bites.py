"""Which traps bite: attribution to the owning trap, blind-solve and re-grade views, advisories."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pipeline import policy
from pipeline.author import bites


def _spec():
    return {"checks": [{"id": "o_a", "kind": "objective", "trap": "t_a"},
                       {"id": "o_b", "kind": "objective", "trap": "t_b"},
                       {"id": "o_shared", "kind": "objective"},
                       {"id": "g_keep", "kind": "guardrail", "trap": "t_g"}],
            "traps": [{"id": "t_a", "type": "text_semantics", "checks": ["o_a", "o_b"]},
                      {"id": "t_b", "type": "temporal_semantics", "checks": ["o_b"]},
                      {"id": "t_c", "type": "numeric_semantics", "checks": ["o_shared"]},
                      {"id": "t_g", "type": "constraint", "checks": ["g_keep"]}]}


class TrapBites(unittest.TestCase):
    def test_a_check_with_an_owner_counts_only_for_its_owner(self):
        owned = bites.owned_checks(_spec())
        self.assertEqual(owned["t_a"], {"o_a"})          # o_b belongs to t_b, not to every trap listing it
        self.assertEqual(owned["t_b"], {"o_b"})
        self.assertEqual(owned["t_c"], {"o_shared"})
        self.assertEqual(bites.objective_traps(_spec()), ["t_a", "t_b", "t_c"])

    def test_blind_solve_and_regrade_views(self):
        record = {"checks": {"o_a": True, "o_b": False, "o_shared": True, "g_keep": True}}
        self.assertEqual(bites.from_blind(_spec(), record), {"t_a": False, "t_b": True, "t_c": False, "t_g": False})
        self.assertEqual(bites.summary_line(_spec(), record), {"biting": ["t_b"], "not_biting": ["t_a", "t_c"],
                                                               "share": 0.33})
        summary = {"by_model": {"openai/gpt-6-astra": {"checks": {"01-x/o_a": {"failed": 4}}},
                                "anthropic/claude-fable-5-1": {"checks": {"01-x/o_shared": {"failed": 5}}}}}
        rows = bites.from_regrade(_spec(), summary)
        self.assertEqual(rows["t_a"], {"gpt-6-astra": 4, "claude-fable-5-1": 0})
        self.assertEqual(rows["t_b"], {"gpt-6-astra": 0, "claude-fable-5-1": 0})

    def test_the_audit_names_non_biting_traps_and_thin_families(self):
        spec = _spec()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "verification").mkdir()
            sha = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            for family, checks in (("astra", {"o_a": False, "o_b": True, "o_shared": True}),
                                   ("fable", {"o_a": True, "o_b": True, "o_shared": True})):
                (bundle / f"verification/blind-solve-{family}.json").write_text(
                    json.dumps({"checks": checks, "draft_sha256": sha}))
            report = bites.audit(spec, bundle)
            self.assertEqual(report["biting"], ["t_a"])
            self.assertEqual(report["non_biting"], ["t_b", "t_c"])
            self.assertEqual(report["per_family"], {"astra": 1, "fable": 0})
            text = " ".join(bites.advisories(spec, bundle))
            self.assertIn("1/3 objective-driving traps bite", text)
            self.assertIn(f"only 0 trap(s) bite fable", text)
            self.assertIn("span 1 type(s)", text)

    def test_no_solve_means_no_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bites.advisories(_spec(), Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
