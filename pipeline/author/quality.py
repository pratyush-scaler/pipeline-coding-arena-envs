"""Quality obligations on a coding spec: bands, decisions, the difficulty self-audit.

These are the parts of the parent's `quality.py` that do not depend on the
expression grader. Bands are wider on the core side than the parent's (4-8
instead of 4-6): hidden behaviour tests are the natural core of a coding
task, and a stretch objective is optional. The difficulty self-audit keeps
the parent's shape -- one misreading per objective-driving trap, each proved
by that trap's own executed mutant -- because that linkage is what made
dormant traps visible before calibration.
"""
from __future__ import annotations

from .. import policy

BAND_LIMITS = {"core": (4, 8), "floor": (0, 3), "stretch": (0, 4)}
MAX_OBJECTIVES = 12
MIN_DISTINCT_DECISIONS = 3
MIN_PITFALLS_CONSIDERED = 8
PITFALL_FAMILIES = ("symptom_vs_cause", "chained_defect", "spec_reading", "constraint", "revert_residue",
                    "scope", "regression", "decoy_location", "stale_docs", "premise_revision",
                    "unsatisfiable", "environment", "boundary", "unit_or_format", "identity", "concurrency",
                    "semantic_decoy", "exception_interaction", "representation_collision", "temporal_state",
                    "distributed_invariant", "architectural_carry")

QUALITY_PROMPT = """
`quality` (required):
  axes: >= 2 of inference | execution | restraint
  levers: >= 1 of join | arbitration | carry | restraint | sequence (not carry alone)
  untested_surfaces: [strings] -- what the hidden tests do NOT establish
  decisions: [{id, question, checks: [core/stretch objective ids], sources: [fixture files],
               shortcut: the tempting wrong move}] -- every core/stretch objective in exactly
               one decision; at least 3 distinct decisions
  difficulty_case: {
    hardest_decision: >= 60 chars -- the one judgment a strong model gets wrong first,
    first_try_verdict: >= 60 chars -- why a frontier model will NOT score 1.0 on its first attempt,
    shortcut_test: >= 60 chars -- the smallest patch that scores full marks; if it is one obvious
                   line a careful reader sees immediately, redesign,
    first_try_mutant: the id of the mutant that IS a strong model's first-pass patch -- fix what the
                   visible repro shows, run the visible case, ship. Declared like any mutant, executed
                   by the controls, and it must score below 1.0; if you cannot write such a patch that
                   scores below 1.0, the task is not hard yet,
    misreadings: one entry per trap that drives an objective:
      {trap, misreading (>= 40), why_plausible (>= 40), diverges_on (>= 10), mutant}
      -- `mutant` is that trap's own mutant; the controls execute it and it must fail exactly
         the trap's checks,
    pitfalls_considered: >= 8 of {pitfall (>= 30), family, where (a fixture file), decision:
      planted|rejected, trap (if planted) | reason (>= 20, if rejected)} -- every trap appears
      as planted
  }
"""


def band_problems(checks: list[dict]) -> list[str]:
    objectives = [c for c in checks if c.get("kind") == "objective"]
    counts = {band: sum(1 for c in objectives if c.get("band") == band) for band in BAND_LIMITS}
    issues = []
    for band, (lo, hi) in BAND_LIMITS.items():
        if not lo <= counts[band] <= hi:
            issues.append(f"{counts[band]} {band} objectives; the band allows {lo}-{hi}")
    if len(objectives) > MAX_OBJECTIVES:
        issues.append(f"{len(objectives)} objectives; at most {MAX_OBJECTIVES}")
    if len(objectives) < policy.MIN_OBJECTIVES:
        issues.append(f"{len(objectives)} objectives; at least {policy.MIN_OBJECTIVES}")
    return issues


def decision_problems(quality: dict, checks: list[dict]) -> list[str]:
    issues = []
    decisions = quality.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return ["quality.decisions is required"]
    semantic = {c["id"] for c in checks if c.get("kind") == "objective" and c.get("band") in ("core", "stretch")}
    seen_ids, covered = set(), {}
    for d in decisions:
        if not isinstance(d, dict):
            issues.append("decisions must be objects")
            continue
        did = d.get("id")
        if not isinstance(did, str) or did in seen_ids:
            issues.append(f"decision {did!r}: missing or duplicate id")
        seen_ids.add(did)
        if not isinstance(d.get("question"), str) or len(d["question"]) < 20:
            issues.append(f"decision {did!r}: question must be at least 20 characters")
        if not isinstance(d.get("shortcut"), str) or len(d["shortcut"]) < 15:
            issues.append(f"decision {did!r}: shortcut must name the tempting wrong move")
        for cid in d.get("checks") or []:
            if cid not in semantic:
                issues.append(f"decision {did!r}: {cid} is not a core/stretch objective")
            elif cid in covered:
                issues.append(f"decision {did!r}: {cid} already belongs to decision {covered[cid]!r}")
            covered[cid] = did
    for cid in sorted(semantic - set(covered)):
        issues.append(f"objective {cid} belongs to no decision")
    if len(seen_ids) < MIN_DISTINCT_DECISIONS:
        issues.append(f"{len(seen_ids)} decisions; at least {MIN_DISTINCT_DECISIONS} distinct professional decisions")
    return issues


def difficulty_case_problems(quality: dict, checks: list[dict], traps: list[dict], mutants: list[dict]) -> list[str]:
    case = quality.get("difficulty_case")
    if not isinstance(case, dict):
        return ["quality.difficulty_case is required: the first pass is the only pass for difficulty"]
    issues = []
    for key in ("hardest_decision", "first_try_verdict", "shortcut_test"):
        value = case.get(key)
        if not isinstance(value, str) or len(value.strip()) < 60:
            issues.append(f"difficulty_case.{key} must be at least 60 characters")
    objectives = {c["id"] for c in checks if c.get("kind") == "objective"}
    driving = {t["id"]: t for t in traps if any(cid in objectives for cid in (t.get("checks") or []))}
    mutant_ids = {m.get("id"): m for m in mutants if isinstance(m, dict)}
    entries = case.get("misreadings")
    if not isinstance(entries, list) or not entries:
        issues.append("difficulty_case.misreadings must name one misreading per objective-driving trap")
        entries = []
    named = set()
    for entry in entries:
        if not isinstance(entry, dict):
            issues.append("misreadings must be objects")
            continue
        tid = entry.get("trap")
        if tid not in driving:
            issues.append(f"misreading names {tid!r}, which is not a trap driving an objective")
            continue
        if tid in named:
            issues.append(f"misreading for {tid!r} appears twice")
        named.add(tid)
        for key, floor in (("misreading", 40), ("why_plausible", 40), ("diverges_on", 10)):
            if not isinstance(entry.get(key), str) or len(entry[key].strip()) < floor:
                issues.append(f"misreading {tid!r}: {key} must be at least {floor} characters")
        mid = entry.get("mutant")
        if mid not in mutant_ids:
            issues.append(f"misreading {tid!r}: mutant {mid!r} is not declared")
        elif mutant_ids[mid].get("trap") != tid:
            issues.append(f"misreading {tid!r}: mutant {mid!r} belongs to trap {mutant_ids[mid].get('trap')!r}")
        elif driving[tid].get("mutant") != mid:
            issues.append(f"misreading {tid!r}: the trap declares mutant {driving[tid].get('mutant')!r}, not {mid!r}")
    for tid in sorted(set(driving) - named):
        issues.append(f"trap {tid!r} drives an objective but has no misreading")
    if policy.FIRST_TRY_MUTANT_REQUIRED:
        first = case.get("first_try_mutant")
        if first not in mutant_ids:
            issues.append("difficulty_case.first_try_mutant must name a declared mutant: the patch a strong "
                          "model ships on its first pass (fix the visible repro, run the visible case, stop); "
                          "the controls execute it and it must score below full credit")
    pitfalls = case.get("pitfalls_considered")
    if not isinstance(pitfalls, list) or len(pitfalls) < MIN_PITFALLS_CONSIDERED:
        issues.append(f"difficulty_case.pitfalls_considered needs at least {MIN_PITFALLS_CONSIDERED} entries")
        pitfalls = pitfalls if isinstance(pitfalls, list) else []
    planted = set()
    for p in pitfalls:
        if not isinstance(p, dict):
            issues.append("pitfalls_considered entries must be objects")
            continue
        if not isinstance(p.get("pitfall"), str) or len(p["pitfall"]) < 30:
            issues.append("a pitfall must be described in at least 30 characters")
        if p.get("family") not in PITFALL_FAMILIES:
            issues.append(f"pitfall family {p.get('family')!r} is not one of {', '.join(PITFALL_FAMILIES)}")
        if not isinstance(p.get("where"), str) or not p["where"]:
            issues.append("a pitfall must say where in the fixture it lives")
        if p.get("decision") == "planted":
            if p.get("trap") not in {t["id"] for t in traps}:
                issues.append(f"planted pitfall names unknown trap {p.get('trap')!r}")
            planted.add(p.get("trap"))
        elif p.get("decision") == "rejected":
            if not isinstance(p.get("reason"), str) or len(p["reason"]) < 20:
                issues.append("a rejected pitfall needs a reason of at least 20 characters")
        else:
            issues.append("a pitfall decision is planted or rejected")
    for t in traps:
        if t.get("id") not in planted:
            issues.append(f"trap {t.get('id')!r} does not appear as a planted pitfall")
    return issues


def quality_problems(spec: dict) -> list[str]:
    quality = spec.get("quality")
    if not isinstance(quality, dict):
        return ["quality is required"]
    issues = []
    axes = quality.get("axes")
    if not isinstance(axes, list) or len({a for a in axes if a in ("inference", "execution", "restraint")}) < 2:
        issues.append("quality.axes must load at least two of inference, execution, restraint")
    levers = quality.get("levers")
    if not isinstance(levers, list) or not any(l in ("join", "arbitration", "restraint", "sequence") for l in levers):
        issues.append("quality.levers must include a lever other than carry")
    if not isinstance(quality.get("untested_surfaces"), list) or not quality["untested_surfaces"]:
        issues.append("quality.untested_surfaces must say what the hidden tests do not establish")
    if not isinstance(spec.get("novelty"), str) or len(spec["novelty"]) < 30:
        issues.append("novelty must say, in at least 30 characters, what this asks that no existing task asks")
    checks = spec.get("checks") or []
    issues += band_problems(checks)
    issues += decision_problems(quality, checks)
    issues += difficulty_case_problems(quality, checks, spec.get("traps") or [], spec.get("mutants") or [])
    from .difficulty import hypothesis_problems
    issues += hypothesis_problems(spec)
    return issues
