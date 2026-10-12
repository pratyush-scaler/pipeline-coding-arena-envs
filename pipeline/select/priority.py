"""Priority ordering: work the richest traces first, then descend.

Until now the pipeline drew one hard line and threw away everything below it.
That is the wrong shape for a batched programme. It is wasteful at the top (a
trace with 12 corrections and 27 retained files is ranked beside one with 2 and
0) and fatal at the bottom (the surviving pool held 6 sessions -- enough for one
batch and nothing after it).

So the thresholds split in two:

  HARD FLOORS       properties we cannot author around. A single-turn seed
                    cannot be multi-step; a seed with no recoverable source
                    tree has no fixture; an engine-bound language has nothing
                    to run on. These still kill.

  TIER BOUNDARIES   properties that make a trace *easier or harder to build
                    from*. These no longer kill -- they order. A seed with one
                    correction is a worse starting point than one with six, but
                    it is not worthless, and when the tier above is exhausted it
                    is what we have.

Three independent kinds of evidence decide the tier, deliberately not blended
into one number until the end, because a trace can be rich in one and empty in
another and the remedy differs:

  ATTACHMENTS   what survived -- the source tree, before/after pairs of the
                same file, tree completeness at the seed boundary. Governs how
                much of the fixture we lift versus author.
  CORRECTIONS   how much the user pushed back, and whether they pushed on the
                same defect repeatedly. Governs how many traps come from the
                trace rather than from us.
  SIGNALS       the dump's own evaluation outcomes. Governs triage confidence.

Tiers are assigned *before* classification, so the expensive stage runs top-down
and stops when a batch is full. Classifying tier 1 costs a fraction of
classifying everything, and the traces it skips are the ones we would not have
built from anyway.

What descending costs, stated rather than discovered later: lower tiers carry
fewer trace-derived corrections, so a larger share of their difficulty must be
authored. `TIER_TRACE_FLOOR` records the trace-derived trap share each tier can
honestly sustain, and the trap gate reads it. Yield per trace falls as we
descend; fidelity falls with it. Neither is hidden.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .. import policy
from ..config import ARTIFACTS

#: Source category letter for each tier: tier 1 is category A, tier 4 is D.
#: The per-category tables live in `pipeline.policy`; the tier-keyed views here
#: are derived from them, never retyped.
_TIER_CATEGORY = {tier: letter for tier, letter in enumerate(policy.CATEGORIES, start=1)}

#: Two different faithfulness questions, gated separately because they fail
#: separately and the remedies differ:
#:
#:   TIER_FIXTURE_FLOOR   how much of the agent's WORLD is lifted from the trace
#:                        rather than written by us (provenance ledger, by bytes)
#:   TIER_TRACE_FLOOR     how much of the user's own CORRECTION material became
#:                        difficulty (trap exploitation)
#:
#: A bundle can lift every document and ignore every complaint, or mine the
#: complaints into a world we invented. Both are failures, and neither shows up
#: in the other number.
#:
#: The band is 80-100% lifted, for every tier. Below 80% a bundle stops being a
#: trace-derived task and becomes an original task that borrows a setting, which
#: is the fidelity finding on the first delivery (~80% of its content came from
#: additions or omissions).
#:
#: Tier still moves the floor inside that band, because tiers differ in how much
#: they CAN supply: a tier-1 trace has the documents to lift and is held to 95%,
#: a tier-4 trace retains little and is held to 80% -- which now means a tier-4
#: trace must be discarded rather than heavily synthesised if it cannot reach it.
#: That is a real narrowing: measured on this dump, 67% of sessions retain no
#: files at all and all 848 retained files are images, so an 80% floor will
#: reject material that the earlier 35% floor would have admitted. The reject is
#: the point -- a bundle we cannot build faithfully is one we should not ship.
TIER_FIXTURE_FLOOR = {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.80}

#: How faithful a bundle's fixture must be to the trace, by BUILD CATEGORY.
#: Tier measures how rich a seed is; category decides how much of the fixture
#: may legitimately be authored, and they are different questions. A category D
#: seed has no liftable workspace by definition -- holding it to a tier-1 floor
#: of 95% would make the category impossible to build. The value is the floor
#: of each band; above it is always fine, because more lifted material is never
#: a defect.
#:
#:   A  Rich          90-100%   the workspace is there; use it
#:   B  Strong        90-100%   documents assembled around real disputes
#:   C  Fixture-led    80-90%   led by the documents, some connective authoring
#:   D  Thin fixture   60-80%   authored outright, grounded in the scenario
CATEGORY_FIXTURE_FLOOR = dict(policy.CATEGORY_FIXTURE_FLOOR)

#: Absolute floor, independent of tier. Nothing ships below this.
FIXTURE_FLOOR_MIN = policy.FIXTURE_FLOOR_MIN

#: Trace-derived trap exploitation each tier can honestly sustain. A tier-1 trace
#: has enough real corrections to carry its own difficulty; a tier-4 trace does
#: not, and pretending otherwise is how a bundle ends up mostly invented without
#: anyone deciding that.
TIER_TRACE_FLOOR = {tier: policy.CATEGORY_TRACE_FLOOR[letter]
                    for tier, letter in _TIER_CATEGORY.items()}

TIER_NOTE = {
    1: "replay-ready: 90%+ of the repository lifted verbatim, and most difficulty "
       "comes from defects the user actually re-reported",
    2: "largely lifted (75%+); some files reconstructed and some authored traps",
    3: "part-reconstructed (55%+): the scenario and key modules are real, the "
       "surrounding tree is built to make the defect testable",
    4: "inspiration-only (35%+): the situation is the trace's, most of the tree "
       "is ours, and the ledger says so per file",
}


@dataclass
class Evidence:
    attachments: float
    corrections: float
    signals: float

    @property
    def total(self) -> float:
        # Geometric-leaning: a trace empty on any one axis should not be lifted
        # into a high tier by the other two. A pure mean does exactly that.
        parts = [self.attachments, self.corrections, self.signals]
        floor = min(parts)
        return round((sum(parts) / 3.0) * 0.6 + floor * 0.4, 4)

    def as_dict(self) -> dict:
        return {"attachments": round(self.attachments, 3),
                "corrections": round(self.corrections, 3),
                "signals": round(self.signals, 3),
                "total": self.total}


def attachments(row: dict) -> float:
    """What survived of the repository, in [0, 1].

    Source text carries most of the weight: a coding fixture is the tree, and
    the share of it we hold the bytes of at the seed boundary is what decides
    whether the bundle lifts or authors. A retained binary is evidence of
    availability, not proof of text readability.
    """
    files = min(row.get("src_paths", 0) / 10.0, 1.0)
    volume = min(math.log10(max(row.get("src_bytes", 0), 1))
                 / math.log10(200_000), 1.0)
    versions = min(row.get("src_versions", 0) / 3.0, 1.0)
    tree = float(row.get("final_tree_recoverable_share", 0.0))
    retained = min(max(row.get("files_ok", 0), row.get('retained_snapshot_versions', 0)) / 6.0, 1.0)
    trail = row.get("trail_paths") or 0
    coverage = min(row.get("trail_covered", 0) / min(trail, 10), 1.0) if trail else 0.0
    return (0.22 * files + 0.18 * volume + 0.20 * versions
            + 0.22 * tree + 0.08 * retained + 0.10 * coverage)


def corrections(row: dict) -> float:
    """How much the user pushed back, in [0, 1].

    `longest_run` is weighted hardest on purpose. A user re-reporting the same
    defect over consecutive turns is a model being told exactly what is wrong
    and failing again; a scatter of one-off corrections across a long session is
    usually someone changing their mind.
    """
    volume = min(row.get("corrections", 0) / 6.0, 1.0)
    persistence = min(row.get("longest_run", 0) / 4.0, 1.0)
    undone = min(row.get("undone", 0) / 3.0, 1.0)
    return 0.42 * persistence + 0.38 * volume + 0.20 * undone


def signals(row: dict) -> float:
    """Strength of the dump's own evaluation outcomes, in [0, 1].

    Endorsements count: a session with both failures and successes is one the
    user stayed engaged with, which is where settled decisions come from. A
    session with failures and no successes is often someone giving up.
    """
    steer = min(row.get("steer_fail", 0) / 4.0, 1.0)
    complaint = min(row.get("complaint_fail", 0) / 2.0, 1.0)
    explicit = min(row.get("outcome_fail", 0) / 2.0, 1.0)
    engaged = min((row.get("steer_ok", 0) + row.get("outcome_ok", 0)) / 6.0, 1.0)
    tier_weight = 1.0 if row.get("model_tier") == "frontier" else 0.72
    return (0.34 * steer + 0.24 * complaint + 0.22 * explicit
            + 0.20 * engaged) * tier_weight


def evidence(row: dict) -> Evidence:
    return Evidence(attachments(row), corrections(row), signals(row))


#: Tier boundaries, derived from the observed distribution of the kaizen pool
#: (110 candidates) rather than chosen: 0.53 is its p95, 0.40 its p85, 0.28 its
#: p60. Set by intuition first, they put a single seed in tier 1 -- not a
#: priority order, just a hard cut with more steps.
#:
#: They are absolute, not recomputed per corpus, so a tier means the same thing
#: across dumps: tier 1 is "as rich as the top 5% of the kaizen dump". A better
#: dump should therefore produce a *larger* tier 1, and that is the correct
#: behaviour -- re-deriving quantiles per corpus would hide exactly the
#: improvement we want to detect.
TIER_CUTS = dict(zip((1, 2, 3), policy.CATEGORY_BOUNDARIES))

#: Tier 1 additionally requires no axis to be near-empty. A trace can reach a
#: high total on two strong axes and one bare one; that is a tier-2 trace with a
#: known gap, not a tier-1 trace.
TIER1_MIN_AXIS = 0.40


def tier_of(ev: Evidence) -> int:
    """Assign a tier from the evidence."""
    t = ev.total
    if t >= TIER_CUTS[1] and min(ev.attachments, ev.corrections,
                                 ev.signals) >= TIER1_MIN_AXIS:
        return 1
    if t >= TIER_CUTS[2]:
        return 2
    if t >= TIER_CUTS[3]:
        return 3
    return 4


@dataclass
class Tiering:
    rows: list[dict] = field(default_factory=list)

    def counts(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for r in self.rows:
            out[r["tier"]] = out.get(r["tier"], 0) + 1
        return dict(sorted(out.items()))

    def sessions_by_tier(self) -> dict[int, int]:
        seen: dict[int, set] = {}
        for r in self.rows:
            seen.setdefault(r["tier"], set()).add(r["session_id"])
        return {k: len(v) for k, v in sorted(seen.items())}


def assign(rows: list[dict]) -> Tiering:
    out = []
    for row in rows:
        ev = evidence(row)
        row = dict(row)
        row["evidence"] = ev.as_dict()
        row["tier"] = tier_of(ev)
        row["trace_floor"] = TIER_TRACE_FLOOR[row["tier"]]
        row["fixture_floor"] = TIER_FIXTURE_FLOOR[row["tier"]]
        row["tier_note"] = TIER_NOTE[row["tier"]]
        out.append(row)
    # Within a tier, richest first. Across tiers, tier always wins -- that is
    # the point of a priority order.
    out.sort(key=lambda r: (r["tier"], -r["evidence"]["total"]))
    return Tiering(out)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Assign priority tiers to candidates")
    ap.add_argument("-i", "--candidates", default=str(ARTIFACTS / "candidates.json"))
    ap.add_argument("-o", "--out", default=str(ARTIFACTS / "tiered.json"))
    args = ap.parse_args(argv)

    doc = json.loads(Path(args.candidates).read_text())
    tiering = assign(doc["candidates"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"schema": "arena-pipeline/tiered/1",
                               "counts": tiering.counts(),
                               "rows": tiering.rows}))

    seeds, sess = tiering.counts(), tiering.sessions_by_tier()
    print(f"{len(tiering.rows)} candidates tiered\n")
    for t in sorted(set(seeds) | set(sess)):
        print(f"  tier {t}:  {seeds.get(t,0):3d} seeds  {sess.get(t,0):3d} sessions  "
              f"fixture>={TIER_FIXTURE_FLOOR[t]:.0%} exploit>={TIER_TRACE_FLOOR[t]:.0%}"
              f"  -- {TIER_NOTE[t]}")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
