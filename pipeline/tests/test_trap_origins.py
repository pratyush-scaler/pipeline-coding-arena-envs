"""Two origins of new trap modes in a hardening round, and requested traps accounted for (user, 2026-10-28).

A round's researched and trace-derived traps are counted only when NEW (the traps it started with never count),
researched traps must drive several failure modes, and every operator-requested idea is built or declined."""
import json
from pathlib import Path
import tempfile
import unittest

from pipeline import policy
from pipeline.author import creation, trap_research

RESEARCH = {"research_id": "r_01", "sources": ["https://www.lords.org/mcc/the-laws"],
            "finding": "Law 16.6: the match ends as soon as the winning run is scored; a boundary counts in full.",
            "executed": "sandbox_run: reference refuses the card, baseline accepts it"}


def _check(cid, quote):
    return {"id": cid, "kind": "objective", "weight": 5, "rule_quote": [quote]}


def _spec(new_research_on=(), new_trace=(), requested=None):
    checks = [_check("o_old", "the old clause."), _check("o_a", "clause a."), _check("o_b", "clause b."),
              _check("o_c", "clause c.")]
    traps = [{"id": "t_old", "type": "numeric_semantics", "checks": ["o_old"], "research": dict(RESEARCH),
              "provenance": "trace_derived"}]
    for cid in ("o_a", "o_b", "o_c"):
        trap = {"id": f"t_{cid}", "type": "numeric_semantics", "checks": [cid], "provenance": "authored"}
        if cid in new_research_on:
            trap["research"] = dict(RESEARCH)
        if cid in new_trace:
            trap["provenance"] = "trace_derived"
        traps.append(trap)
    spec = {"checks": checks, "traps": traps}
    if requested is not None:
        spec["quality"] = {"requested_traps": requested}
    return spec


class Origins(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.bundle = Path(tmp.name); (self.bundle / "verification").mkdir()

    def missing(self, spec):
        return " | ".join(creation.inventory(spec, self.bundle)["missing_components"])

    def open_round(self, floor):
        trap_research.begin(self.bundle, floor, baseline=["t_old"], trace_required=policy.MIN_TRACE_TRAPS_HARDENING)
        doc = trap_research.load(self.bundle)
        doc["calls"] = [{"id": "r_01", "ok": True}]
        (self.bundle / trap_research.RECORD).write_text(json.dumps(doc))

    def test_traps_the_round_started_with_never_count(self):
        self.open_round(1)
        self.assertIn("researched traps (0/1 NEW in this round)", self.missing(_spec()))
        self.assertNotIn("researched traps (", self.missing(_spec(new_research_on=("o_a",))))

    def test_researched_traps_must_drive_several_modes(self):
        self.open_round(1)
        one = self.missing(_spec(new_research_on=("o_a",)))
        self.assertIn(f"researched failure modes (1/{policy.MIN_RESEARCHED_FAILURE_MODES})", one)
        spread = self.missing(_spec(new_research_on=("o_a", "o_b", "o_c")))
        self.assertNotIn("researched failure modes", spread)

    def test_a_hardening_round_needs_new_trace_derived_traps_too(self):
        self.open_round(1)
        self.assertIn(f"trace-derived traps (0/{policy.MIN_TRACE_TRAPS_HARDENING} NEW",
                      self.missing(_spec(new_research_on=("o_a",))))       # t_old is trace-derived but old
        enough = _spec(new_research_on=("o_a",), new_trace=("o_a", "o_b", "o_c")[:policy.MIN_TRACE_TRAPS_HARDENING])
        self.assertNotIn("trace-derived traps (", self.missing(enough))

    def test_a_fresh_design_has_no_trace_floor_from_this_rule(self):
        trap_research.begin(self.bundle, 1)                                  # no baseline: a fresh design
        doc = trap_research.load(self.bundle); doc["calls"] = [{"id": "r_01", "ok": True}]
        (self.bundle / trap_research.RECORD).write_text(json.dumps(doc))
        self.assertNotIn("trace-derived traps (", self.missing(_spec()))

    def test_a_fresh_design_plants_trace_traps_on_several_modes(self):
        """2026-10-28: both origins from the first design, so diversity is built in at authoring."""
        trap_research.begin(self.bundle, 1, trace_required=2)
        doc = trap_research.load(self.bundle); doc["calls"] = [{"id": "r_01", "ok": True}]
        (self.bundle / trap_research.RECORD).write_text(json.dumps(doc))
        missing = self.missing(_spec(new_research_on=("o_a",)))
        self.assertIn("trace-derived traps (1/2)", missing)                  # t_old counts: nothing to exclude
        self.assertIn("trace-derived failure modes (1/2)", missing)
        both = self.missing(_spec(new_research_on=("o_a",), new_trace=("o_a",)))
        self.assertNotIn("trace-derived traps (", both)
        self.assertNotIn("trace-derived failure modes (", both)                # o_old and o_a are two modes

    def test_the_trace_floor_holds_when_research_is_unavailable(self):
        trap_research.begin(self.bundle, 0, trace_required=2)
        missing = self.missing(_spec())
        self.assertNotIn("researched traps (", missing)
        self.assertIn("trace-derived traps (1/2)", missing)

    def test_the_fresh_design_opening_names_both_origins(self):
        trap_research.begin(self.bundle, policy.MIN_RESEARCHED_TRAPS, trace_required=3)
        text = trap_research.opening_text(self.bundle, None, policy.MIN_RESEARCHED_TRAPS)
        self.assertIn("FROM TWO ORIGINS", text)
        self.assertIn("at least 3 trace_derived traps", text)
        self.assertIn(f"at least {policy.MIN_SHARED_FAILURE_MODES} modes defeat BOTH families", text)

    def test_requested_ideas_are_built_or_declined_with_a_reason(self):
        creation.record_requested(self.bundle, ["retired batters", "penalty runs"])
        gaps = creation.requested_trap_gaps(_spec(), self.bundle)
        self.assertEqual(len(gaps), 2)
        ok = [{"idea": 0, "status": "built", "traps": ["t_o_a"]},
              {"idea": 1, "status": "declined", "reason": "The engine's card format has no field for extras by kind."}]
        self.assertEqual(creation.requested_trap_gaps(_spec(requested=ok), self.bundle), [])
        bad = [{"idea": 0, "status": "built", "traps": ["t_missing"]}, {"idea": 1, "status": "declined", "reason": "no"}]
        self.assertEqual(len(creation.requested_trap_gaps(_spec(requested=bad), self.bundle)), 2)
        self.assertIn("quality.requested_traps[idea=0]", self.missing(_spec()))
        creation.record_requested(self.bundle, [])
        self.assertEqual(creation.requested_trap_gaps(_spec(), self.bundle), [])


if __name__ == "__main__":
    unittest.main()
