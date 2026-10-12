"""The numbers Arena asked for, computed the way Arena defined them.

Spec, "Task-Level checks": "Report pass@1, pass@4, and average reward per task."
None of these existed in this workspace. Difficulty was reported as per-check
band composition instead -- a different and much stricter construct, described
in `policy.py`. Bands stay, as diagnosis. These are what acceptance reads.

Two definitions do the work and both are load-bearing:

*Pass* is a complete episode scoring full marks. The spec fixes this when it
defines hard as "the primary calibration model passes no more than 2 of 10
runs" -- a run either passed or it did not, so partial reward is not a partial
pass. Average reward is reported alongside precisely because pass@1 throws that
detail away.

*Valid* excludes voids. A void episode measured nothing; averaging it in
produces a difficulty number that is partly a record of our own infrastructure
failing, and a task can be made to look arbitrarily hard by breaking its
container. `attribution.py` decides what is void; this module only refuses to
count it.
"""
from __future__ import annotations

import math
from collections import Counter

from .attribution import TRUNCATION_STOPS
from .measurement import success_observation

#: A complete episode must reach this to count as a pass.
PASS_REWARD = 1.0

#: Floating point: rewards are computed as sums of weights over 100, so exact
#: equality against 1.0 is unreliable. A reward within this of full marks is
#: full marks.
EPSILON = 1e-9


def episode_rewards(report: dict, strategy: str = "mean") -> dict[int, float | None]:
    """Reward per complete episode, keyed by episode number.

    A multi-step task scores per step; `task.toml` declares how they combine
    (`multi_step_reward_strategy`). An episode missing any step, or containing
    any void step, has no reward -- not a low one. Returning None rather than 0
    is the whole point: a truncated run is indistinguishable from a wrong answer
    once it reaches a score file.
    """
    steps = report.get("step_names") or [
        s for s in dict.fromkeys(r.get("step") for r in report.get("rollouts") or [])]
    grouped: dict[int, list[dict]] = {}
    for row in report.get("rollouts") or []:
        grouped.setdefault(row.get("episode"), []).append(row)

    out: dict[int, float | None] = {}
    for episode, rows in grouped.items():
        by_step = {r.get("step"): r for r in rows}
        if set(by_step) != set(steps) or len(rows) != len(steps):
            out[episode] = None                     # incomplete: measures nothing
            continue
        if any(r.get("zero_class") == "void" or r.get("error")
               or r.get("stop_reason") in TRUNCATION_STOPS for r in rows):
            out[episode] = None                     # a cut-off run is void, not a zero
            continue
        values = []
        for name in steps:
            reward = by_step[name].get("reward")
            if type(reward) not in (int, float) or not math.isfinite(reward):
                values = None
                break
            values.append(float(reward))
        if values is None:
            out[episode] = None
            continue
        out[episode] = combine(values, strategy)
    return out


def combine(values: list[float], strategy: str) -> float:
    """Harbor's `multi_step_reward_strategy`; anything else is refused, not summed."""
    if strategy == "mean":
        return sum(values) / len(values)
    if strategy == "final":
        return values[-1]
    if strategy == "min":
        return min(values)
    raise ValueError(f"unknown multi-step reward strategy {strategy!r}")


def passed(reward: float | None) -> bool:
    return reward is not None and reward >= PASS_REWARD - EPSILON


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Unbiased pass@k: the chance that k episodes drawn from n contain a pass.

    The estimator from the Codex paper. Computing it as `1 - (1 - c/n)**k`
    instead -- the obvious closed form -- assumes sampling with replacement and
    reports an optimistic number on the small n the spec asks for (10). At n=10,
    c=1, that error is about four points of pass@4, which straddles a band.
    """
    if n <= 0 or k <= 0 or c < 0 or c > n:
        return None
    if k > n:
        return None                                 # undefined, not 1.0
    if c == 0:
        return 0.0
    if n - c < k:
        return 1.0
    # product form avoids the overflow a factorial ratio invites
    value = 1.0
    for i in range(k):
        value *= (n - c - i) / (n - i)
    return 1.0 - value


def task_metrics(report: dict, strategy: str = "mean") -> dict:
    """The per-task row of the batch report, for one model.

    `report` is a native campaign report (`native_evidence`'s second return) or
    any record carrying `rollouts` with `episode`, `step`, `reward` and
    `zero_class`.
    """
    rewards = episode_rewards(report, strategy)
    valid = {e: r for e, r in rewards.items() if r is not None}
    void = len(rewards) - len(valid)
    n = len(valid)
    passes = sum(1 for r in valid.values() if passed(r))

    out = {
        "model": report.get("model"),
        "episodes_requested": len(rewards),
        "episodes_valid": n,
        "episodes_void": void,
        "passes": passes,
        "pass_at_1_uncertainty": success_observation(passes, n),
        "average_reward": (sum(valid.values()) / n) if n else None,
        "min_reward": min(valid.values()) if n else None,
        "max_reward": max(valid.values()) if n else None,
        "distinct_rewards": len({round(r, 6) for r in valid.values()}),
    }
    for k in (1, 4):
        out[f"pass_at_{k}"] = pass_at_k(n, passes, k)
    return out


def hardness(primary: dict | None, secondary: dict | None = None) -> dict:
    """Where this task sits against the spec's two difficulty thresholds.

    `primary` and `secondary` are `task_metrics` results. The hard label is
    defined only on the primary model -- the spec is explicit that it is the
    primary calibration model that must pass no more than 2 of 10.

    General difficulty is "pass@1 below 0.5 on BOTH calibration models", so it
    needs both measured. A missing model is not a model that passed the bar:
    `hardness(P, None)` used to report the spec met on the primary alone,
    because the missing secondary was dropped from the list it then checked
    `all()` over. The release bound is read the same way.
    """
    from . import policy

    p1 = (primary or {}).get("pass_at_1")
    s1 = (secondary or {}).get("pass_at_1")
    both = p1 is not None and s1 is not None
    measured = [r for r in (p1, s1) if r is not None]

    return {
        "primary_pass_at_1": p1,
        "secondary_pass_at_1": s1,
        "both_models_measured": both,
        "worst_pass_at_1": max(measured) if measured else None,
        # spec: "A task qualifies as hard when the primary calibration model
        # passes no more than 2 of 10 runs, equivalent to pass@1 <= 0.2"
        "hard": p1 is not None and p1 <= policy.SPEC_HARD_MAX,
        "meets_extreme_target": both and max(p1, s1) <= policy.EXTREME_TARGET_PASS_AT_1,
        # spec: "Target pass@1 below 0.5 on both calibration models"
        "meets_spec_general_difficulty": both and max(p1, s1) < policy.SPEC_GENERAL_DIFFICULTY_MAX,
        # local, wider admission bound -- see policy.RELEASE_PASS_AT_1_MAX
        "meets_release_bound": both and max(p1, s1) < policy.RELEASE_PASS_AT_1_MAX,
    }


def difficulty_problems(primary: dict, secondary: dict | None) -> list[str]:
    """Release blockers from the difficulty MEASUREMENT: both models measured
    with enough valid episodes. pass@1 itself blocks only while
    `policy.DIFFICULTY_GATES_RELEASE` is True, which it is not."""
    from . import policy

    errors = []
    for name, m in (("primary", primary), ("secondary", secondary)):
        if not m:
            errors.append(f"{name} calibration model was not measured")
            continue
        if m["episodes_valid"] < policy.SPEC_EPISODES_PER_MODEL:
            errors.append(
                f"{name} model {m.get('model')}: {m['episodes_valid']} valid "
                f"episodes, need {policy.SPEC_EPISODES_PER_MODEL} "
                f"({m['episodes_void']} void). A void is not a low score; "
                "re-run them rather than reporting a rate over what survived")
    if errors:
        return errors

    # Difficulty is a strict target, not a blocker (policy.DIFFICULTY_GATES_RELEASE):
    # the measured pass@1 is reported in the release evidence (`hardness`), and
    # only the measurement's validity above can block.
    verdict = hardness(primary, secondary)
    if policy.DIFFICULTY_GATES_RELEASE and not verdict["meets_release_bound"]:
        errors.append(
            f"pass@1 is {verdict['worst_pass_at_1']:.2f} on at least one model; "
            f"release requires below {policy.RELEASE_PASS_AT_1_MAX}. This task is "
            "too easy -- it is not a grader defect")
    return errors


def batch_difficulty(rows: list[dict]) -> dict:
    """Spec batch criterion: >=90% of tasks under the 0.5 general-difficulty bar.

    This is what keeps the locally widened per-task bound honest. Individual
    tasks are admitted up to 0.6; the delivered batch still has to satisfy
    Arena's stated criterion, and this is where that is checked rather than
    discovered by Arena.
    """
    from . import policy

    graded = [r for r in rows if (r.get("hardness") or {}).get("worst_pass_at_1") is not None]
    if not graded:
        return {"tasks": 0, "under_spec": 0, "share": None, "ok": False,
                "problems": ["no task carries a measured pass@1"]}
    under = [r for r in graded
             if (r.get("hardness") or {}).get("meets_spec_general_difficulty")]
    hard = [r for r in graded if (r.get("hardness") or {}).get("hard")]
    share = len(under) / len(graded)
    problems = []
    if share < policy.BATCH_UNDER_SPEC_DIFFICULTY_MIN:
        problems.append(
            f"{share:.0%} of tasks are under the spec's pass@1 {policy.SPEC_GENERAL_DIFFICULTY_MAX} "
            f"bar; the batch criterion is {policy.BATCH_UNDER_SPEC_DIFFICULTY_MIN:.0%}. "
            f"Tasks between {policy.SPEC_GENERAL_DIFFICULTY_MAX} and "
            f"{policy.RELEASE_PASS_AT_1_MAX} are individually admissible but "
            "cannot make up more than a tenth of the batch")
    if not hard:
        problems.append(
            "no task in this batch is hard (primary pass@1 <= "
            f"{policy.SPEC_HARD_MAX}); the spec asks for some in each batch")
    return {"tasks": len(graded), "under_spec": len(under), "share": share,
            "hard_tasks": len(hard),
            "hard_share": len(hard) / len(graded),
            "ok": not problems, "problems": problems}


def reward_distribution(rows: list[dict]) -> dict:
    """Coarse shape of the batch, for the report. Not a gate."""
    counter = Counter()
    for row in rows:
        verdict = (row.get("hardness") or {})
        rate = verdict.get("primary_pass_at_1")
        if rate is None:
            counter["unmeasured"] += 1
        elif verdict.get("hard"):
            counter["hard (<=0.2)"] += 1
        elif rate < 0.5:
            counter["general (<0.5)"] += 1
        elif rate < 0.6:
            counter["marginal (0.5-0.6)"] += 1
        else:
            counter["too easy (>=0.6)"] += 1
    return dict(counter)
