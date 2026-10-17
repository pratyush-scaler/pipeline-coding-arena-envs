"""Which traps BITE: per trap, whether a solver's code fails the checks that trap owns.

User, 2026-10-17: "ensure more traps bite the models ... a lot of diverse traps are planted
and each of those traps are extremely hard ... most of them should defeat the model".
Measured on the four ship15 releases (10 x 2 calibration): 23-37 traps planted, 5-8 bit
each model, and on two tasks both models failed exactly the same traps. A trap no solver
fails is coverage, not difficulty.

A check is owned by the trap its `trap` field names; a check without one is owned by every
trap that lists it. Attribution to the owner keeps one failed check from counting as a bite
for every trap that merely routes through it (the over-count that showed 22 "biting" traps
where Astra failed 5 checks).

Two sources, both advisory (the author is strengthened, never blocked):
  * blind solves at creation (`verification/blind-solve-{astra,fable}.json`);
  * the re-grade of a screen's captured episodes in a hidden-only hardening round.
"""
from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path

from .. import policy

FAMILIES = ("astra", "fable")


def owned_checks(spec: dict) -> dict[str, set[str]]:
    checks = {c.get("id"): c for c in spec.get("checks") or [] if isinstance(c, dict) and c.get("id")}
    owned: dict[str, set[str]] = collections.defaultdict(set)
    for trap in spec.get("traps") or []:
        if not isinstance(trap, dict) or not trap.get("id"):
            continue
        for cid in trap.get("checks") or []:
            owner = (checks.get(cid) or {}).get("trap")
            if not owner or owner == trap["id"]:
                owned[trap["id"]].add(cid)
    for cid, check in checks.items():
        if check.get("trap"):
            owned[check["trap"]].add(cid)
    return dict(owned)


def objective_traps(spec: dict) -> list[str]:
    objectives = {c.get("id") for c in spec.get("checks") or [] if isinstance(c, dict) and c.get("kind") == "objective"}
    owned = owned_checks(spec)
    return [t.get("id") for t in spec.get("traps") or []
            if isinstance(t, dict) and owned.get(t.get("id"), set()) & objectives]


def graded(record: dict) -> bool:
    """A solve that never finished grading is no evidence: every check reads as failed."""
    return record.get("reward") is not None and record.get("complete") is not False and bool(record.get("checks"))


def from_blind(spec: dict, record: dict) -> dict[str, bool]:
    """trap -> did this solve fail one of its owned checks (all False for an ungraded solve)."""
    if not graded(record):
        return {trap: False for trap in owned_checks(spec)}
    passed = record.get("checks") or {}
    return {trap: any(passed.get(cid) is False for cid in cids) for trap, cids in owned_checks(spec).items()}


def from_regrade(spec: dict, summary: dict) -> dict[str, dict[str, int]]:
    """trap -> {model: episodes failing one of its owned checks (max over its checks)}."""
    out: dict[str, dict[str, int]] = {}
    owned = owned_checks(spec)
    for model, row in (summary.get("by_model") or {}).items():
        failed = {key.split("/", 1)[-1]: stats.get("failed", 0) for key, stats in (row.get("checks") or {}).items()}
        for trap, cids in owned.items():
            out.setdefault(trap, {})[model.split("/")[-1]] = max([failed.get(c, 0) for c in cids] or [0])
    return out


def audit(spec: dict, bundle: Path) -> dict:
    """The latest blind solve of each family against this draft's traps."""
    current = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    types = {t.get("id"): t.get("type") for t in spec.get("traps") or [] if isinstance(t, dict)}
    objective = objective_traps(spec)
    bites, stale, seen = {t: {} for t in objective}, [], []
    for family in FAMILIES:
        try:
            record = json.loads((Path(bundle) / f"verification/blind-solve-{family}.json").read_text())
        except (OSError, ValueError):
            continue
        if not graded(record):
            continue
        seen.append(family)
        if record.get("draft_sha256") != current:
            stale.append(family)
        for trap, bit in from_blind(spec, record).items():
            if trap in bites:
                bites[trap][family] = bit
    biting = [t for t, row in bites.items() if any(row.values())]
    per_family = {f: sum(1 for row in bites.values() if row.get(f)) for f in seen}
    return {"objective_traps": len(objective), "solved_by": seen, "stale": stale, "bites": bites,
            "biting": biting, "non_biting": [t for t in objective if t not in biting] if seen else [],
            "per_family": per_family, "share": round(len(biting) / len(objective), 2) if objective and seen else None,
            "biting_types": sorted({types.get(t) for t in biting if types.get(t)})}


def advisories(spec: dict, bundle: Path) -> list[str]:
    report = audit(spec, bundle)
    if not report["solved_by"] or not report["objective_traps"]:
        return []
    out = []
    stale = f" (the {', '.join(report['stale'])} solve graded an earlier draft)" if report["stale"] else ""
    if report["share"] is not None and report["share"] < policy.TRAP_BITE_SHARE_TARGET:
        out.append(f"ADVISORY trap bites{stale}: {len(report['biting'])}/{report['objective_traps']} objective-driving "
                   f"traps bite a blind solver (target {policy.TRAP_BITE_SHARE_TARGET:.0%}). Not biting: "
                   f"{report['non_biting'][:12]}. For each, read that solver's patch, find the input where ITS code "
                   "diverges from the published rule, and move or add the trap's hidden case there (THE BITE AUDIT).")
    for family, n in report["per_family"].items():
        if n < policy.MIN_BITING_TRAPS_PER_FAMILY:
            out.append(f"ADVISORY trap bites{stale}: only {n} trap(s) bite {family}'s blind solve (target "
                       f"{policy.MIN_BITING_TRAPS_PER_FAMILY}); aim new interlocks at that family's habits.")
    if report["biting"] and len(report["biting_types"]) < policy.MIN_BITING_TRAP_TYPES:
        out.append(f"ADVISORY trap bites: the traps that bite span {len(report['biting_types'])} type(s) "
                   f"{report['biting_types']}; the target is {policy.MIN_BITING_TRAP_TYPES} -- a model that learns one "
                   "principle passes every trap of that type.")
    return out


def summary_line(spec: dict, record: dict) -> dict:
    """What blind_solve hands back about traps."""
    objective = set(objective_traps(spec))
    bit = {t: v for t, v in from_blind(spec, record).items() if t in objective}
    return {"biting": sorted(t for t, v in bit.items() if v), "not_biting": sorted(t for t, v in bit.items() if not v),
            "share": round(sum(bit.values()) / len(bit), 2) if bit else None}
