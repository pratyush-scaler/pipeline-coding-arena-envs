#!/usr/bin/env python3
"""Stage D2 -- classify surviving seeds into Arena's four faithfulness levels.

User, 2026-09-26: the categories are the spec's -- F3 Replay, F2 Scenario, F1
Pattern, F0 Inspired -- and every trace is classified into the level it can
reach BEFORE authoring starts. The level is a target: what the built bundle
actually preserved is measured afterwards (`ledger/levels.py`) and is what gets
delivered. No level blocks authoring; an F0-target seed is still built and
delivered at the level it measures.

  F3  Replay    liftable fixture AND a strong correction signal: fixture and
                answer key close to the trace files, grading from what the
                user actually corrected
  F2  Scenario  the trace's scenario, failure mode and grading criteria
                survive, the fixture is rebuilt -- strong corrections without
                a liftable workspace, or a liftable workspace with thin ones
  F1  Pattern   too little workspace to rebuild the scenario, but a recoverable
                correction pattern: a synthetic world that reproduces the
                trace's failure mode (spec: "only possible if the trace has a
                recoverable correction pattern")
  F0  Inspired  neither a liftable workspace nor any correction (spec:
                "nothing recoverable (e.g., a single document, no corrections)")

The two signals and their absolute boundaries are unchanged from the A-D
categories this replaced (A -> F3; B, C -> F2; D split into F1 and F0 by
whether any correction survives). What follows is that history.


Two properties of a seed decide what kind of environment it can carry, and they
are independent:

  replay_readiness  how much of the practitioner's workspace we can lift
                    verbatim -- the fixture we get for free
  corrections       how many times the user pushed back -- the failure modes
                    the task can be built around

A seed rich in both gives a task its documents AND its disputes. A seed with
corrections but a thin workspace still carries a real problem; the documents
have to be assembled around it. A seed with documents but no pushback can only
be led by what those documents support. A seed with neither usable workspace
nor pushback needs its fixture invented outright -- legitimate, but it must
stay grounded in the scenario the trace actually describes.

  A  Rich         rich fixture AND strong correction signal
  B  Strong       strong correction signal, fixture assembled around it
  C  Fixture-led  liftable fixture, thin correction signal
  D  Thin fixture fixture must be invented, grounded in the trace's scenario

Boundaries are fitted to the first 500-session corpus and are ABSOLUTE, for the
same reason the tier cuts are: recomputing them per corpus hides the change you
want to detect. Measured there: A 11%, B 35%, C 25%, D 29%. The published yield
table for the reference corpus gives A 11%, B 37%, C 51% with no D -- our C+D
(54%) is their C, split by whether the fixture can be lifted or must be built.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ARTIFACTS

#: Replay readiness at or above which a seed's workspace is a fixture already.
RR_RICH = 0.64
#: ...and below which there is too little to lift, so it must be authored.
RR_LIFTABLE = 0.52
#: Pushbacks at or above which the trace carries real disputes to build on.
CORRECTIONS_STRONG = 2

from ..policy import TARGET_LEVELS  # noqa: E402  the order and the names are policy's

CATEGORIES = dict(zip(TARGET_LEVELS, (
    "Replay -- liftable fixture and strong correction signal",
    "Scenario -- trace scenario and failure mode survive, fixture rebuilt",
    "Pattern -- thin workspace, recoverable correction pattern",
    "Inspired -- no liftable workspace and no correction",
)))

#: Legacy spellings still accepted where a human names a category (`--category`).
from ..policy import LEGACY_CATEGORY_LEVEL, target_level  # noqa: E402  re-exported


def categorise(row: dict) -> str:
    """The faithfulness level this seed can reach -- its target level."""
    readiness = row.get("replay_readiness", 0.0)
    corrections = row.get("corrections", 0)
    if readiness >= RR_RICH and corrections >= CORRECTIONS_STRONG:
        return "F3"
    if corrections >= CORRECTIONS_STRONG or readiness >= RR_LIFTABLE:
        return "F2"
    if corrections >= 1:
        return "F1"
    return "F0"


def annotate(rows: list[dict]) -> list[dict]:
    """Attach `category` to every row, in place, and return them."""
    for row in rows:
        row["category"] = categorise(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse
    from collections import Counter
    ap = argparse.ArgumentParser(description="Classify seeds into target faithfulness levels")
    ap.add_argument("-i", "--tiered", default=str(ARTIFACTS / "tiered.json"))
    ap.add_argument("-o", "--out", default=str(ARTIFACTS / "categorised.json"))
    ns = ap.parse_args(argv)

    doc = json.loads(Path(ns.tiered).read_text())
    rows = annotate(doc["rows"] if isinstance(doc, dict) else doc)
    counts = Counter(r["category"] for r in rows)
    total = sum(counts.values()) or 1
    print(f"{len(rows)} seeds\n")
    for key, description in CATEGORIES.items():
        n = counts.get(key, 0)
        print(f"  {key}  {n:4d}  {n/total:5.1%}   {description}")
    Path(ns.out).write_text(json.dumps(
        {"schema": "arena-pipeline/categorised/2",
         "boundaries": {"rr_rich": RR_RICH, "rr_liftable": RR_LIFTABLE,
                        "corrections_strong": CORRECTIONS_STRONG},
         "counts": dict(counts), "rows": rows}, indent=2))
    print(f"\n-> {ns.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
