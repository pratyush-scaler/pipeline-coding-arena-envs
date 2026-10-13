"""The learning signal a measured task carries (Arena spec, Oct 12, the highlighted paragraphs).

Arena's review of the delivered batch: "about 70% of the points are checks that every model
passes. The rest are two linked checks that both turn on the one trap ... good and bad
solutions both score about 0.72, and there is nothing to learn from"; "all 10 runs scored the
same (all failed the same way). No mix -> nothing to learn from"; and several tasks "reduce to
the same clock puzzle". The spec now asks for difficulty spread "across several checks that can
fail on their own, so partial progress earns partial reward", calibration runs that "produce a
range of scores", and a decisive trap that varies across a batch.

Everything here is derived from what a measurement already records -- per-check pass/fail
counts, per-episode rewards, and the spec's check -> primary trap -> trap type links. Nothing is
typed by hand. Thresholds live in `policy` (SIGNAL_*).

    summary(spec, per_model)   free-weight share, independent hard decisions, per-model reward
                               spread, the decisive trap types
    problems(summary)          what keeps the signal from being usable (empty when it is): free points, independent decisions
    advisories(summary)        reward spread: aimed at and reported, never a refusal
    from_screen(bundle, screen) / from_release(version)
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

from . import policy

SCHEMA = "arena-coding/signal/1"


def episode_rewards(measured: dict) -> list[float]:
    """Valid episodes' task rewards (the mean of their step rewards), in episode order."""
    episodes: dict = {}
    for row in measured.get("rollouts") or []:
        cell = episodes.setdefault(row.get("episode"), {"void": False, "rewards": []})
        if row.get("zero_class") == "void":
            cell["void"] = True
        reward = row.get("reward")
        cell["rewards"].append(float(reward) if isinstance(reward, (int, float)) else 0.0)
    return [round(sum(c["rewards"]) / len(c["rewards"]), 4)
            for _, c in sorted(episodes.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
            if not c["void"] and c["rewards"]]


def decisions(spec: dict) -> dict[tuple, dict]:
    """(step, objective check id) -> its decision: the primary trap it turns on (or itself)."""
    traps = {t.get("id"): t for t in spec.get("traps") or [] if isinstance(t, dict)}
    out = {}
    for check in spec.get("checks") or []:
        if not isinstance(check, dict) or check.get("kind") != "objective":
            continue
        trap = check.get("trap")
        if isinstance(trap, list):
            trap = trap[0] if trap else None
        out[(check.get("step"), check.get("id"))] = {
            "key": f"trap:{trap}" if trap else f"check:{check.get('id')}",
            "trap": trap, "type": (traps.get(trap) or {}).get("type") if trap else None,
            "weight": float(check.get("weight") or 0)}
    return out


def _lookup(table: dict, ref: str) -> dict | None:
    step, _, check = ref.rpartition("/")
    return table.get((step or None, check)) or next(
        (d for (s, c), d in table.items() if c == check and (not step or s == step)), None)


def summary(spec: dict, per_model: dict[str, dict]) -> dict:
    """per_model: {model: {"checks": {"<step>/<check>": {passed, failed, gated_out}}, "episode_rewards": [..]}}"""
    table = decisions(spec)
    total = sum(d["weight"] for d in table.values()) or 0.0
    passed_by_all: dict[tuple, bool] = {}
    hard: dict[str, dict] = {}
    for model, row in per_model.items():
        for ref, state in (row.get("checks") or {}).items():
            d = _lookup(table, ref)
            if d is None or not isinstance(state, dict):
                continue
            ok, bad = int(state.get("passed") or 0), int(state.get("failed") or 0) + int(state.get("gated_out") or 0)
            key = (ref.rpartition("/")[0] or None, ref.rpartition("/")[2])
            passed_by_all[key] = passed_by_all.get(key, True) and bad == 0 and ok > 0
            rate = bad / (ok + bad) if ok + bad else 0.0
            if rate >= policy.SIGNAL_HARD_FAIL_RATE:
                cell = hard.setdefault(d["key"], {"trap": d["trap"], "type": d["type"], "checks": set(),
                                                  "fail_rate": {}, "weight": 0.0})
                cell["checks"].add(ref)
                cell["fail_rate"][model] = max(cell["fail_rate"].get(model, 0.0), round(rate, 3))
    for cell in hard.values():
        cell["weight"] = sum((_lookup(table, ref) or {}).get("weight", 0.0) for ref in cell["checks"])
        cell["checks"] = sorted(cell["checks"])
    free = sum((_lookup(table, f"{s}/{c}" if s else c) or {}).get("weight", 0.0)
               for (s, c), all_ok in passed_by_all.items() if all_ok)
    spread = {}
    for model, row in per_model.items():
        rewards = [r for r in row.get("episode_rewards") or [] if isinstance(r, (int, float))]
        spread[model] = {"episodes": len(rewards), "stdev": round(statistics.pstdev(rewards), 4) if len(rewards) >= 2 else None,
                         "min": min(rewards) if rewards else None, "max": max(rewards) if rewards else None,
                         "distinct": len({round(r, 2) for r in rewards}),
                         "full_passes": sum(1 for r in rewards if r >= 1 - 1e-9)}
    types = Counter()
    for cell in hard.values():
        if cell["type"]:
            types[cell["type"]] += cell["weight"] * max(cell["fail_rate"].values() or [0])
    return {"schema": SCHEMA,
            "free_weight_share": round(free / total, 3) if total else None,
            "independent_hard": len(hard),
            "hard_decisions": {k: {**v} for k, v in sorted(hard.items(), key=lambda kv: -kv[1]["weight"])},
            "spread": spread,
            "decisive_types": [t for t, _ in types.most_common()]}


def problems(s: dict) -> list[str]:
    out = []
    if s.get("free_weight_share") is not None and s["free_weight_share"] > policy.SIGNAL_FREE_WEIGHT_MAX:
        out.append(f"signal: {s['free_weight_share']:.0%} of the objective weight is passed by every run of both models "
                   f"(at most {policy.SIGNAL_FREE_WEIGHT_MAX:.0%}): good and bad solutions score alike")
    if s.get("independent_hard", 0) < policy.SIGNAL_MIN_INDEPENDENT_HARD:
        out.append(f"signal: {s.get('independent_hard', 0)} independent hard decision(s) (checks failed by >= "
                   f"{policy.SIGNAL_HARD_FAIL_RATE:.0%} of a model's runs, one per trap); at least "
                   f"{policy.SIGNAL_MIN_INDEPENDENT_HARD} needed so partial progress earns partial reward")
    return out


def advisories(s: dict) -> list[str]:
    """Reward spread: aimed at and reported, never a refusal. A model can return byte-identical solutions in every
    run (validation session 2026-10-13, c-011-quant: 10/10 Astra repos identical on 300 fuzz frames), so a spread
    floor would refuse a task whose grader is sound; only fresh episodes can show spread there."""
    out = []
    for model, sp in (s.get("spread") or {}).items():
        if sp.get("stdev") is not None and sp["stdev"] < policy.SIGNAL_REWARD_SPREAD_MIN:
            out.append(f"signal advisory: {model.split('/')[-1]} runs are flat (rewards {sp['min']}-{sp['max']}, stdev "
                       f"{sp['stdev']}): every run scores about the same")
    return out


def from_screen(bundle: Path, screen: dict) -> dict:
    spec = json.loads((Path(bundle) / "authoring/spec.json").read_text())
    return summary(spec, {m: {"checks": r.get("checks") or {}, "episode_rewards": r.get("episode_rewards")}
                          for m, r in (screen.get("per_model") or {}).items()})


def from_release(version: Path) -> dict:
    """The signal of the calibration of record (verification/native-calibrations.json)."""
    from .release import calibration_measurement
    from .screen import _check_rows
    version = Path(version)
    spec = json.loads((version / "authoring/spec.json").read_text())
    index = json.loads((version / "verification/native-calibrations.json").read_text())
    per_model = {}
    for campaign in index.get("campaigns") or []:
        _, measured = calibration_measurement(version, index, campaign, 1)
        model = measured.get("model") or Path(campaign).name
        per_model[model] = {"checks": _check_rows(measured), "episode_rewards": episode_rewards(measured)}
    return summary(spec, per_model)


def batch_ledger(signals: dict[str, dict]) -> dict:
    """Decisive trap types over a batch's measured-hard tasks: {task: signal} -> shares and the over-used types."""
    first = Counter()
    for s in signals.values():
        if s.get("decisive_types"):
            first[s["decisive_types"][0]] += 1
    n = sum(first.values())
    shares = {t: round(c / n, 3) for t, c in first.most_common()} if n else {}
    return {"schema": SCHEMA + "/batch", "tasks": n, "decisive_type_share": shares,
            "over_used": [t for t, share in shares.items() if share > policy.SIGNAL_DECISIVE_TYPE_MAX_SHARE and first[t] > 1]}


def ledger_path() -> Path:
    from .config import ARTIFACTS
    return Path(ARTIFACTS) / "waves/decisive-traps.json"


def over_used_types() -> list[str]:
    """Trap types already decisive in more than SIGNAL_DECISIVE_TYPE_MAX_SHARE of this batch's hard tasks."""
    try:
        return list(json.loads(ledger_path().read_text()).get("over_used") or [])
    except (OSError, ValueError):
        return []


GUIDANCE = f"""\
LEARNING SIGNAL (Arena spec, Oct 12 -- binding; the task trains a model, so its failures must teach):
  - SPREAD THE DIFFICULTY over at least {policy.SIGNAL_MIN_INDEPENDENT_HARD} INDEPENDENT hard decisions: each its own \
trap, its own hidden item and its own checks, each able to fail while the others pass. Never let the difficulty sit in \
one premise with several linked checks hanging off it -- every run then scores the same ("good and bad solutions both \
score about 0.72, nothing to learn"). A full pass needs every decision right, so pass@1 stays low while partial \
progress earns partial reward.
  - BOUND THE FREE POINTS: checks every competent run passes may carry at most \
{policy.SIGNAL_FREE_WEIGHT_MAX:.0%} of the objective weight. Put weight on the decisions.
  - AIM FOR A MIX within ONE model's runs: some runs right, some wrong, partial scores in between -- not every run \
failing identically, and not one model passing everything while the other fails everything. Never manufacture the \
mix by weakening or removing a trap: split one blocker into separate decisions, or add an independent trap a model \
sometimes gets right.
  - VARY THE DECISIVE TRAP across the batch: the trap that decides pass or fail must test a different skill from the \
other tasks' (Arena found five tasks that all reduced to the same out-of-sync-clock puzzle).
  - DECOUPLE THE CHECKS: one decision per check, and grade a downstream check against the solution's OWN upstream \
value where the rule allows (consistency credit), so one early mistake does not zero every later check. This turns a \
flat all-fail into a spread without loosening any rule (validation-session pilot, 2026-10-13).
  - EVERY INFERENCE RESOLVABLE from the instruction and the repository: no fact the solver cannot obtain from the \
materials, however hard the task.
"""


def guidance() -> str:
    """GUIDANCE plus the trap types already over-used as decisive in this batch."""
    over = over_used_types()
    if not over:
        return GUIDANCE
    return GUIDANCE + (f"  - ALREADY OVER-USED AS THE DECISIVE TRAP IN THIS BATCH: {', '.join(over)}. Do not make any of "
                       "these the trap that decides pass or fail here; use a different type for the decisive decisions.\n")


def design_advisories(spec: dict) -> list[str]:
    """Before any measurement: the draft's grader shape against the learning-signal rules (advisory only)."""
    table = decisions(spec)
    if not table:
        return []
    bands = {(c.get("step"), c.get("id")): c.get("band") for c in spec.get("checks") or [] if isinstance(c, dict)}
    total = sum(d["weight"] for d in table.values()) or 1.0
    floor = sum(d["weight"] for k, d in table.items() if bands.get(k) == "floor")
    hard_keys = {d["key"] for k, d in table.items() if bands.get(k) in ("core", "stretch") and d["trap"]}
    out = []
    if len(hard_keys) < policy.SIGNAL_MIN_INDEPENDENT_HARD:
        out.append(f"ADVISORY learning signal: {len(hard_keys)} independent decision(s) carry the core/stretch checks "
                   f"(one per primary trap); spread the difficulty over at least {policy.SIGNAL_MIN_INDEPENDENT_HARD} "
                   "so a run can get some right and some wrong (Arena spec Oct 12)")
    if floor / total > policy.SIGNAL_FREE_WEIGHT_MAX:
        out.append(f"ADVISORY learning signal: floor-band checks carry {floor / total:.0%} of the objective weight "
                   f"(at most {policy.SIGNAL_FREE_WEIGHT_MAX:.0%}); move weight onto the hard decisions")
    over = set(over_used_types())
    heavy = sorted(table.values(), key=lambda d: -d["weight"])[:2]
    clash = sorted({d["type"] for d in heavy if d["type"] in over})
    if clash:
        out.append(f"ADVISORY learning signal: the heaviest decisions use trap type(s) {', '.join(clash)}, already the "
                   "decisive trap in too many tasks of this batch; make a different type decisive here")
    return out
