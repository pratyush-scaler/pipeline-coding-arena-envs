"""Failure modes: which DIFFERENT mistakes a task makes a model pay for, read from the grading surface.

User, 2026-10-27: "add more diverse traps ... there could be multiple traps belonging to a single family but
also there should be more diverse trap modes ... in all tasks the failure modes should be diverse and a lot of
diverse trap modes that defeats both the models."

Measured the same day on the cricket build (c-001-cricket-analytics-tool, screen 0.0 / 0.0): every point both
models lost came from ONE mistake -- not enumerating the consistency rules behind one instruction sentence ("if
the card can't be right ... raise ValueError"). Four refusal checks quoted exactly that sentence; their traps were
typed absence / temporal / numeric and listed as four quality.decisions, so the label-based diversity counts
(bites.biting_types, difficulty_gate's trap types and decisions) saw three types and four decisions. The six
other planted modes (proxy, joint feasibility, abstention, partner carry, exact last stand, typed overs) failed
in no episode of either model.

A failure mode is therefore computed from what grades, not from what the designer calls it. Objective checks are
joined into one mode when they share
  * a `quality.lever_plan` entry (one interlocked decision family),
  * a `quality.decisions` entry, or
  * the same instruction clause: the identical set of `rule_quote` sentences after the GENERIC sentences are
    removed (a sentence more than `policy.FAILURE_MODE_GENERIC_CLAUSE_SHARE` of the objective checks quote, such
    as "all of it has to work on any card that can be right", settles nothing on its own).
A mode's CLASS is the catalogued family of its levers (author/levers.py: scope, interaction, representation,
idiom, state, invariant, carry, verification, protocol, environment, adversarial), by majority; without a lever
plan, the class of its traps' types (`TYPE_CLASS`). Re-typing or splitting a trap creates no mode. Many traps
per mode are welcome; diversity is the number of modes and classes that bite.

Read by the creation hardness gate (author/difficulty_gate.py), the hidden-only round's gate, check_draft
advisories, the blind_solve and regrade_episodes hand-backs, and the screen report's census.
"""
from __future__ import annotations

import collections
import math

from .. import policy

#: Trap type -> failure-mode class, used when a mode has no catalogued lever. The classes are the lever
#: catalogue's families plus `restraint` (a stated prohibition), so a mode reads the same whichever way it
#: was declared.
TYPE_CLASS = {
    "symptom_patch": "scope", "spec_misread": "scope", "decoy_location": "carry", "semantic_decoy": "carry",
    "chained_defect": "carry", "architectural_carry": "carry", "premise_revision": "carry",
    "revert_residue": "state", "temporal_state": "state", "concurrency_semantics": "state",
    "regression_bait": "invariant", "distributed_invariant": "invariant",
    "exception_interaction": "interaction", "ordering_semantics": "interaction",
    "representation_collision": "representation", "text_semantics": "representation",
    "absence_semantics": "representation",
    "library_semantics": "idiom", "numeric_semantics": "idiom", "temporal_semantics": "idiom",
    "error_semantics": "idiom",
    "protocol_semantics": "protocol", "stale_authority": "verification", "unsatisfiable": "verification",
    "environment": "environment", "resource_semantics": "environment", "security_semantics": "adversarial",
    "constraint": "restraint", "scope_creep_bait": "restraint",
}


def _catalogue() -> dict[str, dict]:
    from . import levers
    out: dict[str, dict] = {}
    for name in ("LEVERS", "ASTRA_LEVERS", "PROXY_LEVERS", "RESEARCH_LEVERS", "BASH_LEVERS", "CORRECTION_LEVERS"):
        table = getattr(levers, name, None)
        if isinstance(table, dict):
            out.update({k: v for k, v in table.items() if isinstance(v, dict)})
    return out


def _quotes(check: dict) -> list[str]:
    raw = check.get("rule_quote")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    return [" ".join(str(q).split()) for q in items if str(q).strip()]


def _objectives(spec: dict) -> dict[str, dict]:
    return {c["id"]: c for c in spec.get("checks") or []
            if isinstance(c, dict) and c.get("id") and c.get("kind") == "objective"}


def generic_clauses(spec: dict) -> set[str]:
    """Sentences too many objective checks quote to tell decisions apart."""
    objectives = _objectives(spec)
    if len(objectives) < 4:
        return set()
    counts = collections.Counter(q for c in objectives.values() for q in set(_quotes(c)))
    return {q for q, n in counts.items() if n / len(objectives) > policy.FAILURE_MODE_GENERIC_CLAUSE_SHARE}


def modes(spec: dict) -> list[dict]:
    """The task's failure modes: groups of objective checks one shared mistake explains."""
    from . import bites
    objectives = _objectives(spec)
    parent = {cid: cid for cid in objectives}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(ids):
        ids = [i for i in ids if i in parent]
        for other in ids[1:]:
            a, b = find(ids[0]), find(other)
            if a != b:
                parent[max(a, b)] = min(a, b)

    quality = spec.get("quality") or {}
    plan = [p for p in quality.get("lever_plan") or [] if isinstance(p, dict)]
    decisions = [d for d in quality.get("decisions") or [] if isinstance(d, dict)]
    for entry in plan + decisions:
        join([str(c) for c in entry.get("checks") or []])
    generic = generic_clauses(spec)
    by_clause: dict[tuple, list[str]] = collections.defaultdict(list)
    for cid, check in objectives.items():
        key = tuple(sorted(set(_quotes(check)) - generic))
        if key:
            by_clause[key].append(cid)
    for ids in by_clause.values():
        join(ids)

    groups: dict[str, list[str]] = collections.defaultdict(list)
    for cid in objectives:
        groups[find(cid)].append(cid)
    owned = bites.owned_checks(spec)
    types = {t.get("id"): t.get("type") for t in spec.get("traps") or [] if isinstance(t, dict)}
    catalogue = _catalogue()
    out = []
    for members in groups.values():
        checks = sorted(members)
        member_set = set(checks)
        entries = [p for p in plan if member_set & {str(c) for c in p.get("checks") or []}]
        traps = sorted({t for t, cids in owned.items() if cids & member_set}
                       | {str(t) for p in entries for t in p.get("traps") or []})
        lever_names = [str(lv) for p in entries for lv in p.get("levers") or []]
        families = [catalogue[lv]["family"] for lv in lever_names
                    if lv in catalogue and catalogue[lv].get("family") not in (None, "interlock")]
        if families:
            ranked = collections.Counter(families)
            top = max(ranked.values())
            klass = next(f for f in families if ranked[f] == top)
        else:
            kinds = [TYPE_CLASS.get(str(types.get(t))) for t in traps if TYPE_CLASS.get(str(types.get(t)))]
            klass = collections.Counter(kinds).most_common(1)[0][0] if kinds else "unclassified"
        ids = sorted(str(p.get("id")) for p in entries if p.get("id"))
        mode_decisions = sorted(str(d.get("id")) for d in decisions
                                if d.get("id") and member_set & {str(c) for c in d.get("checks") or []})
        mode_id = ("lp:" + "+".join(ids)) if ids else ("d:" + "+".join(mode_decisions)) if mode_decisions \
            else "c:" + checks[0]
        clause = sorted({q for c in checks for q in _quotes(objectives[c])} - generic)
        out.append({"id": mode_id, "class": klass, "checks": checks, "traps": traps,
                    "levers": sorted(set(lever_names)), "clause": clause[:3],
                    "weight": sum(objectives[c].get("weight") or 0 for c in checks)})
    return sorted(out, key=lambda m: m["id"])


def mode_of(spec: dict, table: list[dict] | None = None) -> dict[str, str]:
    return {cid: m["id"] for m in (table if table is not None else modes(spec)) for cid in m["checks"]}


def planted(spec: dict) -> dict:
    table = modes(spec)
    total = sum(m["weight"] for m in table) or 0
    return {"modes": len(table), "classes": sorted({m["class"] for m in table}),
            "largest_weight_share": round(max((m["weight"] for m in table), default=0) / total, 2) if total else None,
            "table": [{k: m[k] for k in ("id", "class", "checks", "traps")} for m in table]}


def planted_missing(spec: dict) -> list[str]:
    report = planted(spec)
    out = []
    if report["modes"] < policy.MIN_PLANTED_FAILURE_MODES:
        out.append(f"{report['modes']} failure mode(s) planted (need {policy.MIN_PLANTED_FAILURE_MODES}): checks of "
                   "one lever_plan entry, one quality.decisions entry or quoting the same instruction clause are ONE "
                   "mode however their traps are typed -- add decisions settled by OTHER published clauses")
    if len(report["classes"]) < policy.MIN_PLANTED_MODE_CLASSES:
        out.append(f"the planted failure modes span {len(report['classes'])} class(es) {report['classes']} (need "
                   f"{policy.MIN_PLANTED_MODE_CLASSES}): aim new modes at other lever families")
    return out


def biting(spec: dict, failed: dict[str, set[str]], table: list[dict] | None = None) -> dict[str, set[str]]:
    """family -> the modes with at least one failed check (check ids, with or without a `step/` prefix)."""
    owner = mode_of(spec, table)
    return {family: {owner[c.split("/", 1)[-1]] for c in cids if c.split("/", 1)[-1] in owner}
            for family, cids in failed.items()}


def biting_missing(spec: dict, bit: dict[str, set[str]], *, where: str = "blind solve",
                   table: list[dict] | None = None) -> list[str]:
    """What the measured bites still lack. Families with no entry in `bit` are not judged."""
    table = table if table is not None else modes(spec)
    klass = {m["id"]: m["class"] for m in table}
    out = []
    for family, ids in sorted(bit.items()):
        if len(ids) < policy.MIN_FAILURE_MODES_PER_FAMILY:
            out.append(f"{len(ids)} failure mode(s) defeat {family}'s {where} (need "
                       f"{policy.MIN_FAILURE_MODES_PER_FAMILY}); its failures are {sorted(ids) or 'none'}")
    if len(bit) >= 2:
        shared = set.intersection(*bit.values())
        classes = {klass.get(m) for m in shared}
        if len(shared) < policy.MIN_SHARED_FAILURE_MODES:
            out.append(f"{len(shared)} failure mode(s) defeat EVERY family's {where} (need "
                       f"{policy.MIN_SHARED_FAILURE_MODES}): {sorted(shared) or 'none'}; untouched modes: "
                       f"{sorted(m['id'] for m in table if not any(m['id'] in v for v in bit.values()))[:8]}")
        elif len(classes) < policy.MIN_SHARED_MODE_CLASSES:
            out.append(f"the modes defeating every family span {len(classes)} class(es) {sorted(classes)} (need "
                       f"{policy.MIN_SHARED_MODE_CLASSES}): one habit learned must not pass them all")
    return out


def summary(spec: dict, failed: dict[str, set[str]]) -> dict:
    """What a blind solve or re-grade hands back about failure modes."""
    table = modes(spec)
    bit = biting(spec, failed, table)
    shared = set.intersection(*bit.values()) if len(bit) >= 2 else set()
    klass = {m["id"]: m["class"] for m in table}
    return {"modes": [{k: m[k] for k in ("id", "class", "checks")} for m in table],
            "biting": {f: sorted(v) for f, v in bit.items()}, "shared": sorted(shared),
            "shared_classes": sorted({klass[m] for m in shared}),
            "not_biting": sorted(m["id"] for m in table if not any(m["id"] in v for v in bit.values())),
            "missing": planted_missing(spec) + biting_missing(spec, bit, table=table)}


def regrade_failing(receipt: dict) -> dict[str, collections.Counter]:
    """model -> Counter(check id -> episodes failing it), from a fast re-grade's per-episode rows."""
    out: dict[str, collections.Counter] = {}
    for row in receipt.get("episodes") or []:
        name = str(row.get("model") or "").split("/")[-1]
        counter = out.setdefault(name, collections.Counter())
        for step in (row.get("steps") or {}).values():
            if step.get("valid"):
                counter.update(set((step.get("failed") or {}).keys()))
    return out


def regrade_modes(spec: dict, receipt: dict, table: list[dict] | None = None) -> dict[str, set[str]]:
    """model -> modes failing in at least FAILURE_MODE_MIN_FAILING_EPISODES of its re-graded episodes
    (an episode counts once per mode however many of the mode's checks it fails)."""
    table = table if table is not None else modes(spec)
    owner = mode_of(spec, table)
    out: dict[str, set[str]] = {}
    for row in receipt.get("episodes") or []:
        name = str(row.get("model") or "").split("/")[-1]
        hit = set()
        for step in (row.get("steps") or {}).values():
            if step.get("valid"):
                hit |= {owner[c] for c in (step.get("failed") or {}) if c in owner}
        out.setdefault(name, collections.Counter()).update(hit)
    return {m: {mode for mode, n in counter.items() if n >= policy.FAILURE_MODE_MIN_FAILING_EPISODES}
            for m, counter in out.items()}


def census(spec: dict, per_model: dict) -> dict:
    """The screen's view: per mode and model, the episodes failing it (at least the worst check's count, the
    screen keeps per-check counts only), the modes that defeated each model and both, and how concentrated
    each model's lost points are."""
    table = modes(spec)
    owner = mode_of(spec, table)
    weights = {c["id"]: c.get("weight") or 0 for c in spec.get("checks") or [] if isinstance(c, dict)}
    rows, defeated, lost = [], {}, {}
    for m in table:
        rows.append({"id": m["id"], "class": m["class"], "checks": m["checks"], "failed_episodes": {}})
    index = {r["id"]: r for r in rows}
    for model, row in per_model.items():
        name = model.split("/")[-1]
        valid = row.get("episodes_valid") or 0
        need = max(policy.FAILURE_MODE_MIN_FAILING_EPISODES, math.ceil(valid * policy.SIGNAL_HARD_FAIL_RATE)) if valid \
            else policy.FAILURE_MODE_MIN_FAILING_EPISODES
        loss: collections.Counter = collections.Counter()
        for key, stats in (row.get("checks") or {}).items():
            cid = key.split("/", 1)[-1]
            mode = owner.get(cid)
            if not mode:
                continue
            failed = int(stats.get("failed") or 0)
            cell = index[mode]["failed_episodes"]
            cell[name] = max(cell.get(name, 0), failed)
            loss[mode] += failed * weights.get(cid, 0)
        defeated[name] = sorted(r["id"] for r in rows if r["failed_episodes"].get(name, 0) >= need)
        total = sum(loss.values())
        lost[name] = ({"largest_mode": loss.most_common(1)[0][0], "share": round(loss.most_common(1)[0][1] / total, 2)}
                      if total else None)
    both = sorted(set.intersection(*(set(v) for v in defeated.values()))) if len(defeated) >= 2 else []
    klass = {m["id"]: m["class"] for m in table}
    return {"modes": rows, "defeated": defeated, "defeated_both": both,
            "defeated_both_classes": sorted({klass[m] for m in both}), "loss_concentration": lost,
            "targets": {"shared_modes": policy.MIN_SHARED_FAILURE_MODES,
                        "shared_classes": policy.MIN_SHARED_MODE_CLASSES,
                        "per_model": policy.MIN_FAILURE_MODES_PER_FAMILY}}


def census_reasons(record: dict) -> list[str]:
    """Outlook lines for the screen report."""
    fm = record.get("failure_modes") or {}
    if not fm.get("modes"):
        return []
    out = [f"failure modes: {len(fm['defeated_both'])} of {len(fm['modes'])} defeated both models "
           f"({', '.join(fm['defeated_both']) or 'none'}; classes {fm['defeated_both_classes'] or '-'}), target "
           f"{policy.MIN_SHARED_FAILURE_MODES} across {policy.MIN_SHARED_MODE_CLASSES} classes; per model "
           + ", ".join(f"{m} {len(v)}" for m, v in sorted(fm["defeated"].items()))]
    for model, row in sorted((fm.get("loss_concentration") or {}).items()):
        if row and row["share"] > policy.FAILURE_MODE_LOSS_SHARE_MAX:
            out.append(f"{model} lost {row['share']:.0%} of its points to one mode ({row['largest_mode']}): the "
                       "next traps belong in modes nobody fails yet")
    return out


def advisories(spec: dict) -> list[str]:
    """check_draft: the planted floors and the concentration of weight, never a refusal."""
    report = planted(spec)
    if not report["modes"]:
        return []
    out = [f"ADVISORY failure modes: {m}" for m in planted_missing(spec)]
    share = report["largest_weight_share"]
    if share is not None and share > policy.FAILURE_MODE_LOSS_SHARE_MAX:
        biggest = max(modes(spec), key=lambda m: m["weight"])
        out.append(f"ADVISORY failure modes: {biggest['id']} ({biggest['class']}) carries {share:.0%} of the objective "
                   "weight; a model that learns that one decision collects most of the score")
    return out


def measured_text(bundle) -> str:
    """For a hardening brief: the failure modes of the version's latest screen (computed from its per-check
    counts when the screen predates this module)."""
    import json
    from pathlib import Path
    try:
        spec = json.loads((Path(bundle) / "authoring/spec.json").read_text())
        from .. import screen as _screen
        record = _screen.current(Path(bundle)) or {}
    except Exception:  # noqa: BLE001  advisory context only
        return ""
    if not record.get("per_model"):
        return ""
    fm = census(spec, record["per_model"])
    lines = ["", "FAILURE MODES OF THE MEASURED VERSION (its screen):"]
    for row in fm["modes"]:
        cells = ", ".join(f"{m} {n}" for m, n in sorted(row["failed_episodes"].items())) or "no episode failed it"
        lines.append(f"  {row['id']} [{row['class']}] checks {row['checks'][:6]}: {cells}")
    lines.append(f"  defeated both models: {fm['defeated_both'] or 'none'} (target {policy.MIN_SHARED_FAILURE_MODES} "
                 f"across {policy.MIN_SHARED_MODE_CLASSES} classes); per model: "
                 + ", ".join(f"{m} {len(v)}" for m, v in sorted(fm["defeated"].items())))
    for model, row in sorted(fm["loss_concentration"].items()):
        if row:
            lines.append(f"  {model} lost {row['share']:.0%} of its points to {row['largest_mode']}")
    lines.append("  Keep what bites. Add hidden cases to the modes no episode failed, and new modes on other "
                 "published clauses, until several different modes defeat BOTH models.")
    return "\n".join(lines)


def brief() -> str:
    return (
        "FAILURE-MODE DIVERSITY IS MEASURED (user, 2026-10-27: \"a lot of diverse trap modes that defeat both the "
        "models\"; several traps may share a family, but the modes must be diverse). Measured the same day: a build "
        "screened 0.0 / 0.0 lost every point of both models to ONE mode -- four refusal checks quoting the same "
        "instruction sentence, typed absence / temporal / numeric and listed as four decisions -- while its six "
        "other planted modes failed in no episode. Failure modes are now computed from the grading surface, not "
        "from labels: objective checks sharing a quality.lever_plan entry, a quality.decisions entry, or the same "
        "instruction clause(s) in rule_quote (a sentence most objective checks quote counts as generic and is "
        "ignored) are ONE mode; its class is the catalogued family of its levers (scope, interaction, "
        "representation, idiom, state, invariant, carry, verification, protocol, environment, adversarial, "
        "restraint). Re-typing or splitting traps creates no mode; a new mode is a DIFFERENT decision, settled by "
        "a DIFFERENT published clause, aimed at a different habit. Plant MANY traps -- several per mode is "
        f"welcome -- across at least {policy.MIN_PLANTED_FAILURE_MODES} modes in at least "
        f"{policy.MIN_PLANTED_MODE_CLASSES} classes. On the current tests at least "
        f"{policy.MIN_SHARED_FAILURE_MODES} modes must defeat BOTH families (spanning at least "
        f"{policy.MIN_SHARED_MODE_CLASSES} classes) and at least {policy.MIN_FAILURE_MODES_PER_FAMILY} must defeat "
        "each family -- determinate checks only, as everywhere in the hardness gate. blind_solve, regrade_episodes "
        "and check_draft return failure_modes {modes, biting per family, shared, not_biting, missing}. When one "
        "mode carries most of the lost points, the next hidden case goes to a mode nobody fails yet (read each "
        "solver's code at the clause that mode rests on: THE DIVERGENCE HUNT), never to another check on the same "
        "clause. Never make a mode bite by grading what no sentence states.")
