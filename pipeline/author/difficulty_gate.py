"""The binding hardness bar: a design is not finished while the models it targets solve it.

User, 2026-10-17: "ensure the tasks are built extremely hard from the authoring phase
itself ... diverse traps which actually bite both the models". Measured the same evening:
of 31 designs that finished creation with blind solves, Astra fully passed 25 and Fable 18
of 24, and five of the first six screens were 1.0 / 1.0 -- the difficulty loop was advisory,
so designers submitted drafts the calibration models solve.

The gate is a CREATION component (`creation.inventory`): the session keeps working within its
budget until it holds, it never refuses validation, and it waives itself -- recorded, with the
reason -- when the turn or blind-solve budget runs out or the sandbox is unavailable, so a
session ends with its best draft instead of burning out.

Creation sessions (fresh designs and full redesign rounds), against the latest GRADED blind solve
of each family on the CURRENT grading surface (steps, checks, suites, toolchain, deps):
  * each family fails at least one step, and no step is fully passed by both;
  * at least `policy.MIN_BINDING_BITES_PER_FAMILY` objective-driving traps bite each family,
    counted only when a check the trap owns carries a determinate `determinacy_probe` (both
    families answer the case right when asked directly -- the failure is the code's, not the
    instruction's);
  * the biting traps span at least `policy.MIN_BINDING_BITING_TYPES` trap types.
Hidden-only hardening rounds (screen episodes captured), against the latest re-grade of the
current grading surface: every step fully passed by at most `policy.FAST_GATE_MAX_FULL_PASSES`
episodes per model, and at least `policy.MIN_BINDING_BITES_PER_FAMILY` traps fail in two or
more episodes of each model.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .. import policy
from ..artifacts import atomic_json
from . import bites

RECORD = "verification/difficulty-gate.json"
LINKS = "verification/determinacy-links.json"
FAMILIES = ("astra", "fable")


def grading_sha(spec: dict) -> str:
    """What a solver is graded against: a change here makes an earlier solve stale."""
    surface = {"steps": [{k: s.get(k) for k in ("name", "instruction", "new_information")}
                         for s in spec.get("steps") or [] if isinstance(s, dict)],
               "checks": spec.get("checks"), "suites": spec.get("suites"),
               "toolchain": spec.get("toolchain"), "deps": spec.get("deps")}
    return hashlib.sha256(json.dumps(surface, sort_keys=True, default=str).encode()).hexdigest()


def _load(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def arm(bundle: Path, mode: str | None) -> dict:
    """Open the gate for a session: mode 'creation', 'fast' or None (not armed)."""
    doc = {"mode": mode, "armed_at": time.time(), "waived": None}
    atomic_json(Path(bundle) / RECORD, doc)
    return doc


def waive(bundle: Path, reason: str) -> None:
    doc = _load(Path(bundle) / RECORD) or {}
    if doc.get("mode") and not doc.get("waived"):
        doc["waived"] = {"reason": reason, "at": time.time()}
        atomic_json(Path(bundle) / RECORD, doc)


def link_probe(bundle: Path, check: str, result: dict, case: str) -> None:
    path = Path(bundle) / LINKS
    doc = _load(path) or {}
    doc.setdefault(check, []).append({"determinate": bool(result.get("determinate")), "case": case[:300],
                                      "prompt_sha256": result.get("prompt_sha256"), "at": time.time()})
    atomic_json(path, doc)


def determinate_checks(bundle: Path) -> set[str]:
    return {cid for cid, rows in (_load(Path(bundle) / LINKS) or {}).items()
            if any(r.get("determinate") for r in rows or [])}


def _current_solves(spec: dict, bundle: Path) -> dict[str, dict]:
    current = grading_sha(spec)
    out = {}
    for family in FAMILIES:
        record = _load(Path(bundle) / f"verification/blind-solve-{family}.json")
        if record and bites.graded(record) and record.get("grading_sha256") == current:
            out[family] = record
    return out


def creation_status(spec: dict, bundle: Path) -> dict:
    steps = [s.get("name") for s in spec.get("steps") or [] if isinstance(s, dict) and s.get("name")]
    solves = _current_solves(spec, bundle)
    missing: list[str] = []
    for family in FAMILIES:
        if family not in solves:
            missing.append(f"a graded blind_solve(model='{family}') of the current tests and instruction")
    full = {f: {s for s, row in (r.get("by_step") or {}).items() if row.get("reward") == 1.0}
            for f, r in solves.items()}
    for family, passed in full.items():
        if steps and set(steps) <= passed:
            missing.append(f"{family}'s blind solve fully passes every step -- add a determinate interlocked case its "
                           "code gets wrong (THE BITE AUDIT / DIVERGENCE HUNT)")
    if len(full) == len(FAMILIES):
        both = set(steps) & full["astra"] & full["fable"]
        if both:
            missing.append(f"step(s) {sorted(both)} fully passed by BOTH families' blind solves")
    owned = bites.owned_checks(spec)
    objective = set(bites.objective_traps(spec))
    sure = determinate_checks(bundle)
    types = {t.get("id"): t.get("type") for t in spec.get("traps") or [] if isinstance(t, dict)}
    # Independent decisions, not checks: one upstream miss failing several checks is one bite
    # (W13: "8 decisions" in a release were 1-3). quality.decisions groups checks into decisions;
    # without it every trap is its own decision.
    decision_of = {}
    for row in ((spec.get("quality") or {}).get("decisions") or []):
        if isinstance(row, dict) and row.get("id"):
            for cid in row.get("checks") or []:
                decision_of.setdefault(cid, row["id"])
    biting: dict[str, set[str]] = {}
    for family, record in solves.items():
        bit = bites.from_blind(spec, record)
        failed = {cid for cid, ok in (record.get("checks") or {}).items() if ok is False}
        biting[family] = {t for t, v in bit.items() if v and t in objective and owned.get(t, set()) & sure}
        units = {decision_of.get(cid, f"trap:{t}") for t in biting[family] for cid in owned.get(t, set()) & sure & failed}
        if len(units) < policy.MIN_BINDING_BITES_PER_FAMILY:
            missing.append(f"{len(units)} independent determinate decision(s) bite {family} (need "
                           f"{policy.MIN_BINDING_BITES_PER_FAMILY}; a decision counts when a check of it, owned by an "
                           "objective-driving trap, fails in that solve AND carries a determinate "
                           "determinacy_probe(check=...); checks of one quality.decisions entry count once)")
    union = set().union(*biting.values()) if biting else set()
    kinds = {types.get(t) for t in union if types.get(t)}
    if solves and len(kinds) < policy.MIN_BINDING_BITING_TYPES:
        missing.append(f"the biting traps span {len(kinds)} type(s) {sorted(kinds)}; need "
                       f"{policy.MIN_BINDING_BITING_TYPES} (one principle learned must not pass every trap)")
    return {"missing": missing, "biting": {f: sorted(v) for f, v in biting.items()}, "types": sorted(kinds),
            "solved_by": sorted(solves)}


def fast_status(spec: dict, bundle: Path) -> dict:
    receipt = _load(Path(bundle) / "verification/fast-regrade.json")
    if not receipt or receipt.get("grading_sha256") != grading_sha(spec):
        return {"missing": ["a regrade_episodes of the current hidden tests"]}
    missing = []
    for model, row in (receipt.get("by_model") or {}).items():
        for step, cell in (row.get("steps") or {}).items():
            if (cell.get("full") or 0) > policy.FAST_GATE_MAX_FULL_PASSES:
                missing.append(f"{model.split('/')[-1]} still fully passes step {step} in {cell.get('full')} of "
                               f"{cell.get('graded')} episodes (bar {policy.FAST_GATE_MAX_FULL_PASSES})")
    rows = bites.from_regrade(spec, receipt)
    objective = set(bites.objective_traps(spec))
    for model in (receipt.get("by_model") or {}):
        name = model.split("/")[-1]
        n = sum(1 for t, r in rows.items() if t in objective and (r.get(name) or 0) >= 2)
        if n < policy.MIN_BINDING_BITES_PER_FAMILY:
            missing.append(f"{n} trap(s) fail in 2+ episodes of {name} (need {policy.MIN_BINDING_BITES_PER_FAMILY})")
    return {"missing": missing}


def requirement(spec: dict, bundle: Path) -> list[str]:
    """Missing creation components the armed gate still asks for ([] when unarmed or waived)."""
    doc = _load(Path(bundle) / RECORD) or {}
    if not doc.get("mode") or doc.get("waived") or not policy.DIFFICULTY_GATE_AT_CREATION:
        return []
    status = creation_status(spec, bundle) if doc["mode"] == "creation" else fast_status(spec, bundle)
    return [f"HARDNESS GATE: {m}" for m in status["missing"]]
