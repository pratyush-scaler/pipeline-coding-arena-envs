"""The learning signal a measured task carries (Arena spec, Oct 12, the highlighted paragraphs).

Arena's review of the delivered batch: "about 70% of the points are checks that every model
passes. The rest are two linked checks that both turn on the one trap ... good and bad
solutions both score about 0.72, and there is nothing to learn from"; "all 10 runs scored the
same (all failed the same way). No mix -> nothing to learn from"; and several tasks "reduce to
the same clock puzzle". The spec now asks for difficulty spread "across several checks that can
fail on their own, so partial progress earns partial reward", calibration runs that "produce a
range of scores", and a decisive trap that varies across a batch.

Everything here is derived from what a measurement already records -- per-check pass/fail
counts, per-episode check states and rewards, and the spec's check -> primary trap -> trap type
links. Nothing is typed by hand. Thresholds live in `policy` (SIGNAL_*).

User, 2026-10-21, after the external audit of Batch 3: checks that fail together in every run were
counted as independent hard decisions (c-game-bot's FAILURES.md said 5; o_crash_ledge, o_clip,
o_bogus_dry and o_bogus_after failed in the same 16 runs and passed in the same 4), and one decision
carried 93% of all the reward the calibration lost. A decision is now a primary trap OR a lockstep
group -- checks whose pass/fail vectors are identical across every valid run of both models -- and
no single decision may carry more than policy.SIGNAL_SINGLE_DECISION_SHARE_MAX of the lost reward.

    summary(spec, per_model)   free-weight share, independent hard decisions (lockstep collapsed),
                               the lost-reward share of each decision, per-model reward spread,
                               the decisive trap types
    problems(summary)          what keeps the signal from being usable (empty when it is): free points,
                               independent decisions, one decision carrying the lost reward
    advisories(summary)        reward spread: aimed at and reported, never a refusal
    from_screen(bundle, screen) / from_release(version) / from_campaigns(spec, paths)
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

from . import policy

#: /2 (2026-10-21): independent_hard counts lockstep groups once; lost_reward_share and
#: single_decision_share added. A /1 record's independent_hard can overcount.
SCHEMA = "arena-coding/signal/2"


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


def episode_checks(measured: dict) -> list[dict[str, str]]:
    """Valid episodes' check states, {"<step>/<check>": "passed" | "failed" | "gated_out"}, in episode order: the
    per-run vectors lockstep collapse compares (user, 2026-10-21). Voids are skipped exactly as episode_rewards does."""
    episodes: dict = {}
    for row in measured.get("rollouts") or []:
        cell = episodes.setdefault(row.get("episode"), {"void": False, "states": {}})
        if row.get("zero_class") == "void":
            cell["void"] = True
        for cid, state in (row.get("checks") or {}).items():
            cell["states"][f"{row.get('step')}/{cid}"] = state
    return [c["states"] for _, c in sorted(episodes.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
            if not c["void"] and c["states"]]


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


def _resolve(table: dict, ref: str) -> tuple | None:
    """The (step, check) key a `<step>/<check>` (or bare `<check>`) reference names in the decisions table."""
    step, _, check = ref.rpartition("/")
    if (step or None, check) in table:
        return (step or None, check)
    return next(((s, c) for (s, c) in table if c == check and (not step or s == step)), None)


def _ref(key: tuple) -> str:
    return f"{key[0]}/{key[1]}" if key[0] else str(key[1])


def _order(key: tuple) -> tuple:
    return (key[0] or "", key[1] or "")


def lockstep_groups(table: dict, per_model: dict[str, dict]) -> list[list[tuple]] | None:
    """Objective checks whose pass/fail vectors are identical across every valid run of every model, with at least
    one failure and at least two checks per group: one decision, however many checks and traps it names (user,
    2026-10-21: c-game-bot's o_crash_ledge, o_clip, o_bogus_dry and o_bogus_after failed in the same 16 of 20 runs
    on one motion-replay test each). A check every run passes is free weight, never a group. None when any model's
    per-run states are missing: an unmeasured lockstep is not an absent one."""
    if not per_model or any(not row.get("runs") for row in per_model.values()):
        return None
    vectors: dict[tuple, list] = {key: [] for key in table}
    for model in sorted(per_model):
        for run in per_model[model]["runs"]:
            states = {}
            for ref, state in (run or {}).items():
                key = _resolve(table, ref)
                if key is not None:
                    states[key] = state
            for key in table:
                vectors[key].append(states.get(key, "missing"))
    same: dict[tuple, list] = {}
    for key, vector in vectors.items():
        if "failed" in vector:
            same.setdefault(tuple(vector), []).append(key)
    return sorted((sorted(keys, key=_order) for keys in same.values() if len(keys) > 1), key=lambda g: _order(g[0]))


def summary(spec: dict, per_model: dict[str, dict]) -> dict:
    """per_model: {model: {"checks": {"<step>/<check>": {passed, failed, gated_out}}, "episode_rewards": [..],
    "runs": [{"<step>/<check>": state}, ..]}}. `runs` (one entry per valid episode, episode_checks) is what lockstep
    collapse reads; without it decisions are counted per primary trap only and `lockstep_groups` is None."""
    table = decisions(spec)
    total = sum(d["weight"] for d in table.values()) or 0.0
    step_weight: Counter = Counter()
    for (step, _), d in table.items():
        step_weight[step] += d["weight"]
    passed_by_all: dict[tuple, bool] = {}
    rates: dict[tuple, dict] = {}
    # Reward lost on each objective check that ran and failed, in step-reward units summed over every valid run of
    # both models (a gated-out check lost its points to the guardrail, not to its own decision). Read from the
    # per-run states where they exist, which hold valid episodes only; from the counts otherwise.
    lost: Counter = Counter()
    for model, row in per_model.items():
        failures: Counter = Counter()
        for ref, state in (row.get("checks") or {}).items():
            key = _resolve(table, ref)
            if key is None or not isinstance(state, dict):
                continue
            ok, failed = int(state.get("passed") or 0), int(state.get("failed") or 0)
            bad = failed + int(state.get("gated_out") or 0)
            passed_by_all[key] = passed_by_all.get(key, True) and bad == 0 and ok > 0
            rate = bad / (ok + bad) if ok + bad else 0.0
            rates.setdefault(key, {})[model] = max(rates.get(key, {}).get(model, 0.0), round(rate, 3))
            failures[key] += failed
        if row.get("runs"):
            failures = Counter(key for run in row["runs"] for ref, state in (run or {}).items()
                               if state == "failed" and (key := _resolve(table, ref)) is not None)
        for key, n in failures.items():
            if step_weight[key[0]]:
                lost[key] += n * table[key]["weight"] / step_weight[key[0]]
    hard_keys = {key for key, by_model in rates.items() if max(by_model.values() or [0]) >= policy.SIGNAL_HARD_FAIL_RATE}
    # One decision = one primary trap, joined with every check it passes and fails in lockstep with.
    groups = lockstep_groups(table, per_model)
    parent = {key: key for key in table}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    first: dict[str, tuple] = {}
    for key, d in table.items():
        if d["key"] in first:
            parent[root(key)] = root(first[d["key"]])
        else:
            first[d["key"]] = key
    for group in groups or []:
        for key in group[1:]:
            parent[root(key)] = root(group[0])
    components: dict[tuple, list] = {}
    for key in sorted(table, key=_order):
        components.setdefault(root(key), []).append(key)
    in_lockstep = {key for group in groups or [] for key in group}
    lost_total = sum(lost.values())
    hard: dict[str, dict] = {}
    shares: dict[str, dict] = {}
    for keys in components.values():
        name = "+".join(sorted({table[k]["key"] for k in keys}))
        amount = sum(lost[k] for k in keys)
        share = round(amount / lost_total, 3) if lost_total else None
        if amount > 0:
            shares[name] = {"checks": [_ref(k) for k in keys if lost[k] > 0], "share": share}
        hit = [k for k in keys if k in hard_keys]
        if not hit:
            continue
        heaviest = max(hit, key=lambda k: table[k]["weight"])
        fail_rate: dict[str, float] = {}
        for k in hit:
            for model, rate in rates[k].items():
                fail_rate[model] = max(fail_rate.get(model, 0.0), rate)
        hard[name] = {"trap": table[heaviest]["trap"], "type": table[heaviest]["type"],
                      "traps": sorted({table[k]["trap"] for k in hit if table[k]["trap"]}),
                      "types": sorted({table[k]["type"] for k in hit if table[k]["type"]}),
                      "checks": [_ref(k) for k in hit], "fail_rate": fail_rate,
                      "weight": sum(table[k]["weight"] for k in hit),
                      "lockstep": any(k in in_lockstep for k in keys), "lost_share": share or 0.0}
    free = sum(table[key]["weight"] for key, all_ok in passed_by_all.items() if all_ok)
    spread = {}
    for model, row in per_model.items():
        rewards = [r for r in row.get("episode_rewards") or [] if isinstance(r, (int, float))]
        spread[model] = {"episodes": len(rewards), "stdev": round(statistics.pstdev(rewards), 4) if len(rewards) >= 2 else None,
                         "min": min(rewards) if rewards else None, "max": max(rewards) if rewards else None,
                         "distinct": len({round(r, 2) for r in rewards}),
                         "full_passes": sum(1 for r in rewards if r >= 1 - 1e-9)}
    # Decisive types stay per primary trap, as the batch ledger has always read them.
    by_trap: dict[str, dict] = {}
    for key in hard_keys:
        cell = by_trap.setdefault(table[key]["key"], {"type": table[key]["type"], "weight": 0.0, "rate": 0.0})
        cell["weight"] += table[key]["weight"]
        cell["rate"] = max([cell["rate"], *rates[key].values()])
    types = Counter()
    for cell in by_trap.values():
        if cell["type"]:
            types[cell["type"]] += cell["weight"] * cell["rate"]
    ranked = sorted(shares.items(), key=lambda kv: -kv[1]["share"])
    top = ranked[0] if ranked else None
    return {"schema": SCHEMA,
            "free_weight_share": round(free / total, 3) if total else None,
            "independent_hard": len(hard),
            "independent_hard_before_lockstep": len(by_trap),
            "hard_decisions": {k: {**v} for k, v in sorted(hard.items(), key=lambda kv: -kv[1]["weight"])},
            "lockstep_groups": [[_ref(k) for k in g] for g in groups] if groups is not None else None,
            "runs_compared": {m: len(r.get("runs") or []) for m, r in per_model.items()},
            "lost_reward_share": {name: row["share"] for name, row in ranked},
            "decisions_losing_reward": len(ranked),
            "single_decision": {"decision": top[0], **top[1]} if top else None,
            "single_decision_share": top[1]["share"] if top else None,
            "single_decision_share_max": policy.SIGNAL_SINGLE_DECISION_SHARE_MAX,
            "spread": spread,
            "decisive_types": [t for t, _ in types.most_common()]}


def problems(s: dict) -> list[str]:
    out = []
    if s.get("free_weight_share") is not None and s["free_weight_share"] > policy.SIGNAL_FREE_WEIGHT_MAX:
        out.append(f"signal: {s['free_weight_share']:.0%} of the objective weight is passed by every run of both models "
                   f"(at most {policy.SIGNAL_FREE_WEIGHT_MAX:.0%}): good and bad solutions score alike")
    # Below SIGNAL_MIN_INDEPENDENT_HARD_BLOCK / above SIGNAL_SINGLE_DECISION_SHARE_BLOCK it refuses; between those and
    # the aims it is an advisory (user, 2026-10-21 ~10:05 IST: "Ignore the small minor issues which are not a blocker").
    if s.get("independent_hard", 0) < policy.SIGNAL_MIN_INDEPENDENT_HARD_BLOCK:
        out.append(_independent_text(s))
    # User, 2026-10-21: c-game-bot lost 93% of its reward on one decision, static-html-mindmap 100% on one fold.
    share = s.get("single_decision_share")
    if share is not None and share > policy.SIGNAL_SINGLE_DECISION_SHARE_BLOCK:
        out.append(_share_text(s))
    return out


def _independent_text(s: dict, prefix: str = "signal") -> str:
    return (f"{prefix}: {s.get('independent_hard', 0)} independent hard decision(s) (checks failed by >= "
            f"{policy.SIGNAL_HARD_FAIL_RATE:.0%} of a model's runs, one per trap, and checks that pass and fail "
            f"together in every run count once); at least {policy.SIGNAL_MIN_INDEPENDENT_HARD} needed so partial "
            "progress earns partial reward")


def _share_text(s: dict, prefix: str = "signal") -> str:
    one = s.get("single_decision") or {}
    names = ", ".join(ref.rpartition("/")[2] for ref in one.get("checks") or []) or one.get("decision")
    return (f"{prefix}: one decision ({names}) carries {s['single_decision_share']:.0%} of all the reward the calibration "
            f"lost (at most {policy.SIGNAL_SINGLE_DECISION_SHARE_MAX:.0%}): one reading of one decision sets almost "
            "every score")


def advisories(s: dict) -> list[str]:
    """Reward spread: aimed at and reported, never a refusal. A model can return byte-identical solutions in every
    run (validation session 2026-10-13, c-011-quant: 10/10 Astra repos identical on 300 fuzz frames), so a spread
    floor would refuse a task whose grader is sound; only fresh episodes can show spread there."""
    out = []
    if policy.SIGNAL_MIN_INDEPENDENT_HARD_BLOCK <= s.get("independent_hard", 0) < policy.SIGNAL_MIN_INDEPENDENT_HARD:
        out.append(_independent_text(s, "signal advisory"))
    share = s.get("single_decision_share")
    if share is not None and policy.SIGNAL_SINGLE_DECISION_SHARE_MAX < share <= policy.SIGNAL_SINGLE_DECISION_SHARE_BLOCK:
        out.append(_share_text(s, "signal advisory"))
    for model, sp in (s.get("spread") or {}).items():
        if sp.get("stdev") is not None and sp["stdev"] < policy.SIGNAL_REWARD_SPREAD_MIN:
            out.append(f"signal advisory: {model.split('/')[-1]} runs are flat (rewards {sp['min']}-{sp['max']}, stdev "
                       f"{sp['stdev']}): every run scores about the same")
    return out


def campaign_measurements(paths) -> dict[str, dict]:
    """{model: {"checks", "episode_rewards", "runs"}} from campaigns' own report.json trials, void trials dropped as
    release._void_tolerant drops them. A derived reading for the signal only: the evidence itself was verified by the
    screen or release that measured it."""
    from .evidence import rollouts
    trials: dict[str, list] = {}
    names: dict[str, list] = {}
    for path in paths:
        report = json.loads((Path(path) / "report.json").read_text())
        model = report.get("model") or report.get("requested_model") or Path(path).name
        trials.setdefault(model, []).extend(t for t in report.get("trials") or [] if t.get("valid") is not False)
        names[model] = names.get(model) or list(report.get("step_names") or [])
    out = {}
    for model, rows in trials.items():
        measured = {"rollouts": rollouts(rows, names[model])}
        runs = episode_checks(measured)
        counts: dict[str, dict] = {}
        for run in runs:
            for ref, state in run.items():
                cell = counts.setdefault(ref, {"passed": 0, "failed": 0, "gated_out": 0})
                cell[state if state in cell else "failed"] += 1
        out[model] = {"checks": counts, "episode_rewards": episode_rewards(measured), "runs": runs}
    return out


def from_campaigns(spec: dict, paths) -> dict:
    """The signal of a set of calibration campaigns (a delivered package's runs/, read by failure_report when the
    release's own signal predates lockstep collapse)."""
    return summary(spec, campaign_measurements(paths))


def from_screen(bundle: Path, screen: dict) -> dict:
    spec = json.loads((Path(bundle) / "authoring/spec.json").read_text())
    per_model = {}
    for m, r in (screen.get("per_model") or {}).items():
        runs = r.get("runs")
        if runs is None and r.get("campaign"):
            # Screens record counts, not per-run states: read them from the campaign's own report (user, 2026-10-21).
            try:
                found = campaign_measurements([r["campaign"], *(r.get("topups") or [])])
                row = found.get(m) or (next(iter(found.values())) if len(found) == 1 else {})
                runs = row.get("runs")
            except (OSError, ValueError, KeyError, TypeError):
                runs = None
        per_model[m] = {"checks": r.get("checks") or {}, "episode_rewards": r.get("episode_rewards"), "runs": runs}
    return summary(spec, per_model)


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
        per_model[model] = {"checks": _check_rows(measured), "episode_rewards": episode_rewards(measured),
                            "runs": episode_checks(measured)}
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
  - NO ONE DECISION CARRIES THE GRADE: checks that pass and fail together in every calibration run count as ONE \
decision however many traps they name, and no single decision may carry more than \
{policy.SIGNAL_SINGLE_DECISION_SHARE_MAX:.0%} of all the reward the calibration loses (c-game-bot lost 93% of it on one \
motion replay spread over four checks; the external audit, 2026-10-21).
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
