"""Shared descriptive measurement policy; validity is independent of difficulty.

An observed band is not proof of a sound check or of ability discrimination.
Never pool model rates to qualify a task. Uncertainty is reported, not hidden.
"""
from __future__ import annotations

import math
from collections import Counter

CORE_LO, CORE_HI, FLOOR_LO = .30, .70, .90
BANDS = ("stretch", "core", "transition", "floor", "unreachable")


def band(rate: float | None) -> str:
    if rate is None:
        return "unreachable"
    if not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("pass rate must be finite and between zero and one")
    return ("stretch" if rate < CORE_LO else "core" if rate <= CORE_HI
            else "transition" if rate < FLOOR_LO else "floor")


def observation(rec: dict) -> dict:
    counts = {k: rec.get(k, 0) for k in ("passed", "failed", "gated_out")}
    if any(type(v) is not int or v < 0 for v in counts.values()):
        raise ValueError("check counts must be nonnegative integers")
    n = counts["passed"] + counts["failed"]
    rate = counts["passed"] / n if n else None
    interval = None
    if n:
        z = 1.959963984540054
        denominator = 1 + z * z / n
        center = (rate + z * z / (2 * n)) / denominator
        half = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denominator
        interval = [max(0., center - half), min(1., center + half)]
    return {**counts, "attempted": n, "rate": rate, "band": band(rate),
            "wilson_95": interval, "provisional": n < 10,
            "boundary_uncertain": bool(interval and any(interval[0] < b < interval[1]
                                                        for b in (CORE_LO, CORE_HI, FLOOR_LO)))}


def objective_bands(checks: dict) -> dict:
    result = {name: [] for name in BANDS}
    for cid, rec in checks.items():
        if rec.get("kind") == "guardrail":
            continue
        measured = observation(rec)
        result[measured["band"]].append((cid, measured["rate"]))
    return result


def success_observation(passes: int, n: int, *, alpha: float = .05) -> dict:
    """Exact one-sided binomial upper bound for a fixed, independent cohort.

    This does not correct adaptive candidate selection or optional stopping.
    Confirmation must use fresh episodes on a frozen candidate.
    """
    if type(n) is not int or type(passes) is not int or not 0 <= passes <= n:
        raise ValueError("success counts must satisfy 0 <= passes <= n")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    measured = observation({"passed": passes, "failed": n - passes})
    upper = None
    if n:
        if passes == 0:
            upper = -math.expm1(math.log(alpha) / n)
        elif passes == n:
            upper = 1.
        else:
            coefficients = [math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
                            for k in range(passes + 1)]
            lo, hi = passes / n, 1.
            for _ in range(60):
                p = (lo + hi) / 2
                terms = [coef + k * math.log(p) + (n - k) * math.log1p(-p)
                         for k, coef in enumerate(coefficients)]
                peak = max(terms)
                cdf = math.exp(peak) * sum(math.exp(t - peak) for t in terms)
                if cdf > alpha:
                    lo = p
                else:
                    hi = p
            upper = (lo + hi) / 2
    return {**measured, "one_sided_upper": upper, "confidence": 1 - alpha,
            "assumption": "fixed independent cohort; no adaptive selection or optional stopping correction"}


def extreme_confirmation(cohorts: dict, target: float, *, held_out: bool = False) -> dict:
    """Simultaneous bounds across named models; screening cannot confirm itself."""
    if not 0 < target < 1:
        raise ValueError("target must be between zero and one")
    bounds = {}
    for model, row in cohorts.items():
        passes, n = row.get("passes"), row.get("episodes_valid")
        if passes is not None and n is not None:
            bounds[model] = success_observation(passes, n, alpha=.05 / max(1, len(cohorts)))
    sufficient = len(cohorts) >= 2 and len(bounds) == len(cohorts) and all(
        r["one_sided_upper"] is not None and r["one_sided_upper"] <= target for r in bounds.values())
    return {"target": target, "joint_confidence": .95, "by_model": bounds,
            "bounds_below_target": sufficient, "held_out": held_out,
            "confirmed": held_out and sufficient,
            "note": "confirm only with a predeclared fixed cohort of fresh episodes on the frozen final candidate"}


def exact_interval(passes: int, n: int, *, alpha: float = .05) -> list[float] | None:
    """Two-sided Clopper-Pearson interval; never turn a missing cohort into zero."""
    upper = success_observation(passes, n, alpha=alpha / 2)["one_sided_upper"]
    if upper is None:
        return None
    lower = 1 - success_observation(n - passes, n, alpha=alpha / 2)["one_sided_upper"]
    return [max(0., lower), min(1., upper)]


def compare_success(before: dict, after: dict, *, alpha: float = .05) -> dict:
    """Conservative simultaneous interval for independent success-rate change.

    Development comparisons are exploratory, never held-out confirmation.
    Equal five-run screens supply no evidence of a permanent lack of progress.
    """
    intervals = []
    for row in (before, after):
        try:
            intervals.append(exact_interval(row["passes"], row["episodes_valid"], alpha=alpha / 2))
        except (KeyError, TypeError, ValueError):
            intervals.append(None)
    if any(i is None for i in intervals):
        return {"status": "inconclusive", "change_interval": None, "exploratory": True}
    old, new = intervals
    change = [new[0] - old[1], new[1] - old[0]]
    status = "improved" if change[1] < 0 else "regressed" if change[0] > 0 else "inconclusive"
    return {"status": status, "change_interval": change, "confidence": 1 - alpha,
            "exploratory": True, "note": "independent fixed cohorts; adaptive search is not confirmation"}


def profile(checks: dict) -> dict:
    """Per-model descriptive profile, with guardrails outside capability totals."""
    rows = {cid: {**rec, **observation(rec)} for cid, rec in checks.items()}
    objectives = {cid: r for cid, r in rows.items() if r.get("kind") != "guardrail"}
    counts = dict(Counter(r["band"] for r in objectives.values()))
    if not objectives:
        status = "unmeasured"
    elif counts.get("unreachable"):
        status = "unreachable"
    elif all(r["rate"] < CORE_LO for r in objectives.values()):
        status = "stretch_only"
    elif all(r["rate"] > CORE_HI for r in objectives.values()):
        status = "easy"
    elif (4 <= counts.get("core", 0) <= 6 and counts.get("floor", 0) <= 2
          and 1 <= counts.get("stretch", 0) <= 2):
        status = "target_mix"
    else:
        status = "needs_redesign"
    return {"status": status, "objective_count": len(objectives),
            "guardrail_count": len(rows) - len(objectives), "bands": counts,
            "checks": rows, "qualification": "descriptive only; requires verified evidence and semantic review"}


def task_profile(steps: dict) -> dict:
    """Apply the task rubric budget without pooling rates or duplicating checks.

    Input is the step records for ONE model, either raw or already profiled.
    Each objective retains its own reachable observations. Guard outcomes remain
    in the step records; a shared integrity guard is counted once in the task.
    """
    objectives, guards, empty_steps = {}, set(), []
    for step, data in steps.items():
        informative = False
        for cid, row in data['checks'].items():
            if row.get('kind') == 'guardrail':
                guards.add(cid)
                continue
            if cid in objectives:
                raise ValueError(f'{cid}: an objective cannot be counted in multiple steps')
            objectives[cid] = row
            informative |= observation(row)['band'] in {'core', 'stretch'}
        if not informative:
            empty_steps.append(step)
    result = profile(objectives)
    if result['status'] == 'target_mix' and empty_steps:
        result['status'] = 'needs_redesign'
    return {**result, 'guardrail_count': len(guards), 'uninformative_steps': empty_steps,
            'scope': 'whole task for one model; criterion rates remain separate'}
