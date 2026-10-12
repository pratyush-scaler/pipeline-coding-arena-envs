"""Stage E -- rank survivors and compose a batch.

Batches are composed, not skimmed. Taking the top N by score concentrates a
batch on one regime, and then a systemic defect in that regime costs the whole
batch while teaching us about nothing else. Instead each batch fills distinct
slots, so three environments answer three different questions.

The slots come from what we do not yet know how to do reliably:

  persistent_defect  can we turn a defect the agent was told about twice into a
                     root-cause trap, and prove by mutant that the symptom patch
                     scores zero? (the corpus's dominant failure: 274 moments)
  multi_step         does a two-step environment survive its own step boundary?
                     (kw-003 shipped with rollback reachable during step 1)
  restraint          do the user's prohibitions become guardrails a capable
                     model actually trips? (where over-acting shows)

A slot with no qualifying candidate is reported empty. It is never backfilled
from another slot -- that would quietly turn a composed batch back into a
skimmed one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import ARTIFACTS, BATCH_SIZE

#: The one truth source a coding seed survives with. Kept as a tuple so the
#: rank bonus and the slot predicate read one definition.
DERIVED_TRUTH = ("executable_tests",)

#: Correction classes that become restraint guardrails.
RESTRAINT_CLASSES = ("constraint_assertion", "scope_correction")


def rank(row: dict) -> float:
    """Within-tier ordering. Deliberately multiplicative.

    This never crosses a tier boundary: `compose` fills from the highest tier
    that still has material, and only uses this to order inside it. A rich
    tier-2 trace must not outrank a thin tier-1 one, or the priority order is
    decorative.

    A seed must be recoverable AND disputed AND carry checkable defects. Any
    one of the three at zero should sink it, which a weighted sum would not do:
    a perfectly recoverable repository whose every correction is about taste
    scores well on two terms and is worthless. A located root cause earns a
    bonus: it is the trap the task will carry, already found.
    """
    c = row["classification"]
    truth_bonus = 1.15 if c["truth_source"] in DERIVED_TRUTH else 1.0
    cause_bonus = 1.0 + 0.1 * min(c.get("located_causes", 0), 3)
    from .mechanisms import bonus
    return round(row["replay_readiness"] * row["dispute"]
                 * c["usable_ratio"] * truth_bonus * cause_bonus * bonus(c), 4)


@dataclass
class Slot:
    name: str
    predicate: object
    rationale: str


#: Slots span the three difficulty axes rather than three domains. A batch of
#: three CRUD-API tasks that all load a spec misread teaches us about spec
#: misreads three times; one per axis tells us which axis our authoring
#: actually handles.
SLOTS = [
    Slot("persistent_defect",
         lambda r: (r["classification"].get("classes") or {}).get("persistent_defect", 0) >= 1
                   and r["classification"].get("located_causes", 0) >= 1,
         "root-cause axis: the user re-reported a defect after a claimed fix and "
         "the evidence locates its cause, so the symptom patch becomes a mutant "
         "that must score zero"),
    Slot("multi_step",
         lambda r: r.get("session_candidate_seeds", 0) >= 2,
         "execution axis: two graded steps, which exercises the step-isolation "
         "gate -- the defect kw-003 shipped with"),
    Slot("restraint",
         lambda r: sum((r["classification"].get("classes") or {}).get(k, 0)
                       for k in RESTRAINT_CLASSES) >= 2,
         "restraint axis: the user stated prohibitions the agent broke, which "
         "become guardrails -- where capable models over-act"),
]


def compose(rows: list[dict], size: int = BATCH_SIZE,
            frontier_only: bool = False, taken: set | None = None) -> dict:
    """Fill each slot from the highest tier that can fill it.

    Two orderings compose here and the precedence matters: **tier first, rank
    within tier.** A slot reaches into tier 2 only when no tier-1 candidate
    qualifies for it, and the report says which tier each pick came from, so a
    batch drawing deep is visible rather than silent.

    `taken` carries sessions already used by earlier batches, so batch two does
    not re-pick batch one's material.
    """
    pool = [r for r in rows if r["classification"].get("survives")]
    if frontier_only:
        pool = [r for r in pool if r["model_tier"] == "frontier"]
    for r in pool:
        r["rank_score"] = rank(r)
    pool.sort(key=lambda r: (r.get("tier", 4), -r["rank_score"]))

    chosen: list[dict] = []
    used_sessions: set[str] = set(taken or ())
    slots_report = []
    for slot in SLOTS[:size]:
        from .mechanisms import bonus, levers_supported, opportunities
        used_mechanisms = ({m["mechanism"] for row in chosen for m in opportunities(row["classification"])}
                           | {lever for row in chosen for lever in row.get("lever_opportunities") or []})
        eligible = [r for r in pool if slot.predicate(r) and r["session_id"] not in used_sessions]
        eligible.sort(key=lambda r: (r.get("tier", 4),
                                     -r["rank_score"] * bonus(r["classification"], used_mechanisms)
                                     / bonus(r["classification"])))
        pick = next(iter(eligible), None)
        if pick is None:
            slots_report.append({"slot": slot.name, "filled": False,
                                 "rationale": slot.rationale,
                                 "note": "no qualifying candidate in any tier -- "
                                         "slot left empty"})
            continue
        used_sessions.add(pick["session_id"])
        entry = dict(pick)
        entry["slot"] = slot.name
        entry["slot_rationale"] = slot.rationale
        entry["difficulty_opportunities"] = opportunities(pick["classification"])
        entry["lever_opportunities"] = levers_supported(pick["classification"])
        chosen.append(entry)
        slots_report.append({"slot": slot.name, "filled": True,
                             "session_id": pick["session_id"],
                             "seed_index": pick["seed_index"],
                             "tier": pick.get("tier"),
                             "trace_floor": pick.get("trace_floor"),
                             "rank_score": pick["rank_score"],
                             "rationale": slot.rationale})

    drawn_tiers = [r.get("tier", 4) for r in chosen]
    remaining = [r for r in pool if r["session_id"] not in used_sessions]
    by_tier: dict[int, int] = {}
    for r in remaining:
        by_tier[r.get("tier", 4)] = by_tier.get(r.get("tier", 4), 0) + 1

    return {
        "schema": "arena-pipeline/batch/2",
        "pool_size": len(pool),
        "frontier_only": frontier_only,
        "deepest_tier_drawn": max(drawn_tiers) if drawn_tiers else None,
        "remaining_by_tier": dict(sorted(by_tier.items())),
        "batches_remaining_estimate": len(remaining) // max(size, 1),
        "slots": slots_report,
        "batch": chosen,
        "reserve": remaining[:12],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    # Prints the composition only. It used to write `batch.json` and per-seed
    # briefs that no stage read: the build composes again from
    # `classified.json` (`phases.select`), so the files were a second, silently
    # divergent answer to "what is in this batch".
    ap = argparse.ArgumentParser(description="Stage E -- rank and compose a batch")
    ap.add_argument("-i", "--classified", default=str(ARTIFACTS / "classified.json"))
    ap.add_argument("-n", "--size", type=int, default=BATCH_SIZE)
    ap.add_argument("--frontier-only", action="store_true")
    ap.add_argument("--exclude-sessions", default="",
                    help="comma-separated session ids already used by an earlier "
                         "batch")
    args = ap.parse_args(argv)

    doc = json.loads(Path(args.classified).read_text())
    from ..dump.evidence import SCHEMA as EVIDENCE_SCHEMA
    if any(r.get('classification', {}).get('survives') and
           r['classification'].get('evidence_schema') != EVIDENCE_SCHEMA for r in doc['rows']):
        raise SystemExit('Classification predates the full evidence reader. Rebuild the index and classify again before selection.')
    taken = {s.strip() for s in args.exclude_sessions.split(",") if s.strip()}
    result = compose(doc["rows"], size=args.size,
                     frontier_only=args.frontier_only, taken=taken)

    print(f"pool {result['pool_size']} survivors -> batch of {len(result['batch'])}")
    for s in result["slots"]:
        if s["filled"]:
            print(f"  [{s['slot']:16s}] tier {s['tier']}  {s['session_id'][:8]} "
                  f"seed {s['seed_index']:<3} score={s['rank_score']}  "
                  f"trace-floor {s['trace_floor']:.0%}")
        else:
            print(f"  [{s['slot']:16s}] EMPTY -- {s['note']}")
    print(f"  deepest tier drawn: {result['deepest_tier_drawn']}")
    print(f"  remaining by tier : {result['remaining_by_tier']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
