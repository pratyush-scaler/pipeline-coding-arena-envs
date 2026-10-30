"""Phase 4 -- the calibration screen: five episodes on each of two models.

The pipeline is select -> author -> grade -> calibrate, and it stops there.
This module is the calibrate phase. It runs once the WHOLE requested batch has
been authored and graded -- never per task, and never inside the design loop --
and it runs again, by hand, after every hardening round. What it produces is a
table: pass@1, pass@4 and mean reward per task per model, the failure
attribution behind every zero, and a hardening outlook per task.

    screen                 5 episodes x 2 models   every new batch; every hardening round
    calibration of record  10 episodes x 2 models  `arena qualify`, only once shippable

It replaced two things. The one-episode presolve, which ran after every draft
on one model and sent "too easy" back into the design loop; and the in-build
difficulty pilot. Neither survives: difficulty is authored into the first draft
(the designer is told to build the traps in), and it is MEASURED here, after the
batch exists. Nothing in this module feeds back into authoring -- scores and
solver answers are never repair feedback (`policy.REPAIR_FEEDBACK_EXCLUDES`).

The measurement is the calibration of record's, narrower: the same native
Terminus-2 backend, the same two spec models, `high` effort, no turn or time
limits, the same evidence verification and the same void-only top-up
(`release._calibrate_model`). A void leaves every denominator. A scored episode
is never re-run: a screen retained for this exact task version is reused, and a
new one is launched only for a model the task version has not been screened on.

A screen is a MEASUREMENT, never a difficulty gate. pass@1 < 0.5 on both
models is the strict target (`policy.CALIBRATION_TARGET_PASS_AT_1`): reported
for every task, aimed at by hardening, and blocking nothing. The one thing a
screen decides is whether the ten-episode calibration of record may be spent,
and that only on VALIDITY -- enough valid episodes, every failure attributable,
no unresolved reward hacking (`shippable_problems`, read by `release._qualify`).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import policy

SCHEMA = "arena-pipeline/screen/1"

#: The current screen of this task version. Bound to `artifact_sha256`; a screen
#: of an earlier version describes a task that no longer exists.
RECEIPT = "verification/screen.json"

#: Every screen ever taken of this bundle, one file each, never overwritten --
#: the hardening history, so a round can be compared with the one before it.
HISTORY = "verification/screens"

#: Top-up receipt prefix, kept apart from the calibration of record's "topup".
TOPUP_RECEIPT = "screen-topup"

#: Outlook verdicts, strongest first. Descriptive; none of them rejects a task.
OUTLOOKS = ("meets-target", "strong-candidate", "weak-candidate",
            "poor-candidate", "not-assessable")


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def _runner_changed(campaign: Path) -> bool:
    """The campaign's report names a native runner other than this checkout's (evidence.native_evidence's test)."""
    import hashlib
    try:
        recorded = json.loads((campaign / "report.json").read_text()).get("runner_sha256")
    except (OSError, ValueError):
        return False  # missing evidence is native_evidence's finding, recorded with the campaign
    runner = Path(__file__).parent / "benchmark/harbor.py"
    return recorded != hashlib.sha256(runner.read_bytes()).hexdigest()


def current(bundle: Path) -> dict | None:
    """The screen receipt bound to this exact task version, or None."""
    from .artifacts import identity
    from .author.contracts import strict_json
    path = Path(bundle) / RECEIPT
    try:
        record = strict_json(path.read_text())
        return record if record.get("artifact_sha256") == identity(Path(bundle)) else None
    except (OSError, ValueError):
        return None


def shippable_problems(bundle: Path, models: list[str] | None = None) -> list[str]:
    """Why the ten-episode calibration of record may not start. [] = shippable.

    Validity only: pass@1 is a target, never a reason (policy.DIFFICULTY_GATES_RELEASE).

    Re-derived from the retained campaigns rather than read from the stored
    verdict, so a receipt edited by hand, or evidence deleted since, cannot
    authorise a campaign. The episode floor and the model set are POLICY's (or
    the caller's), never the receipt's own: a receipt claiming one episode or
    one model proves nothing. No screen of this version is a refusal, not a pass.
    """
    record = current(bundle)
    if record is None:
        return [f"no calibration screen of this task version ({RECEIPT}); run "
                f"`arena screen {bundle}` -- {policy.SCREEN_EPISODES_PER_MODEL} episodes "
                "per model -- first"]
    models = list(models or policy.resolve_calibration_models())
    episodes = max(int(record.get("episodes_per_model") or 0), policy.SCREEN_EPISODES_PER_MODEL)
    try:
        fresh = summarize(bundle, record.get("index") or {}, episodes, models)
    except Exception as exc:                         # noqa: BLE001 -- unreadable is not shippable
        return [f"the screen receipt could not be re-verified: {type(exc).__name__}: {exc}"]
    problems = fresh["shippable_problems"]
    if problems and record.get("remedy"):
        problems = [record["remedy"], *problems]
    return problems


#: Attribution's own shortfall: voids it excluded (a grader refusal of the tree among them).
_ATTRIBUTION_SHORT = "void episodes leave"


def _attribution_short(row: dict) -> bool:
    return any(_ATTRIBUTION_SHORT in str(p) for p in row.get("attribution_problems") or [])


def _short_only(row: dict) -> bool:
    """A verified campaign whose only problem is too few valid episodes."""
    problems = row.get("evidence_problems") or []
    if row.get("campaign") and not problems and _attribution_short(row):
        return True
    return bool(row.get("campaign") and problems) and all(
        str(p).split(": ", 1)[-1].startswith("need >= ") for p in problems)


def remedy(bundle: Path, models: list[str]) -> str:
    """The one operator step that completes a screen whose void budget is spent."""
    return (f"{', '.join(models)}: still short of valid episodes with the "
            f"{policy.MAX_TOPUP_ATTEMPTS}-episode void top-up budget spent, so re-screening "
            f"changes nothing; only `arena screen --relaunch-incomplete {bundle}` completes "
            "it (a fresh campaign that re-runs scored episodes; the receipt names the "
            "campaigns it discards)")


def rescreen_command(bundle: Path, record: dict | None) -> str:
    """How to re-measure `bundle`, given its current screen (or None)."""
    flag = "--relaunch-incomplete " if (record or {}).get("topup_exhausted") else ""
    return f"./arena screen {flag}{bundle}"


def _write(bundle: Path, record: dict) -> None:
    from .artifacts import atomic_json
    atomic_json(Path(bundle) / RECEIPT, record)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(record["created_at"]))
    atomic_json(Path(bundle) / HISTORY / f"{stamp}-{record['artifact_sha256'][:12]}.json", record)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def run(bundle: Path, *, native_root: Path, models: list[str] | None = None,
        episodes: int | None = None, workers: int | None = None,
        label: str = "", relaunch_incomplete: bool = False) -> dict:
    """Screen one task on both calibration models and write its receipt.

    Models run concurrently; the Coder pool (`NATIVE_CAPACITY`) bounds how many
    native episodes are in flight across every task in the batch. `label` names
    the occasion ("batch", "hardening round 2") in the receipt and history.

    A retained campaign whose evidence verified is kept whatever it scored. One
    still short of valid episodes is TOPPED UP with the missing episodes, within
    what is left of its void-replacement budget. `relaunch_incomplete` is the
    operator's override for a short campaign whose budget is spent: it is
    replaced by a fresh one, which re-runs scored episodes, so the receipt names
    the discarded campaign.
    """
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    from .artifacts import bundle_lock, freeze
    from .benchmark.harbor import resolve_model
    from .release import _calibrate_model, topup_spent

    bundle = Path(bundle).resolve()
    models = list(models or policy.resolve_calibration_models())
    episodes = episodes or policy.SCREEN_EPISODES_PER_MODEL
    workers = workers or episodes
    if len({resolve_model(m) for m in models}) < 2:
        raise ValueError("a screen measures two distinct calibration models")
    if retired := policy.calibration_problems(models):
        raise ValueError("; ".join(retired))
    with bundle_lock(bundle):
        prior = current(bundle)
        index = {"campaigns": [], "topups": {}}
        reused: dict[str, str] = {}
        short: dict[str, list[str]] = {}      # verified, short of episodes: top up
        discarded: dict[str, list[str]] = {}  # short, relaunched by the operator
        if prior and prior.get("episodes_per_model") == episodes:
            # Results are not re-run. A model this version was already screened
            # on keeps its campaign, whatever it SCORED -- but only if its
            # evidence verified. A campaign whose evidence is gone or changed
            # is replaced; one short of valid episodes is topped up.
            index["artifact_sha256"] = prior["artifact_sha256"]
            for model, row in (prior.get("per_model") or {}).items():
                # Reuse every campaign whose evidence verified, even one still
                # short of valid episodes after its void top-ups: relaunching it
                # would re-run episodes that were already scored. Only a campaign
                # whose evidence failed integrity (changed runner, deleted or
                # altered files) is replaced -- its scores cannot be trusted.
                integrity = [p for p in row.get("evidence_problems") or []
                             if not str(p).split(": ", 1)[-1].startswith("need >= ")]
                # The recorded problems were found by the runner of that time: a campaign measured by another
                # runner (Harbor 0.22 -> 0.24, 2026-10-24) is replaced, as qualification would refuse it.
                if row.get("campaign") and _runner_changed(Path(row["campaign"])):
                    integrity.append("native runner changed; remeasure with the current protocol")
                if (not integrity and row.get("campaign")
                        and model in {resolve_model(m) for m in models}):
                    extra = list((prior.get("index") or {}).get("topups", {}).get(row["campaign"]) or [])
                    parts = [row["campaign"], *extra]
                    incomplete = (len(integrity) < len(row.get("evidence_problems") or [])
                                  or (not integrity and _attribution_short(row)))
                    if incomplete and relaunch_incomplete:
                        discarded[model] = parts
                        continue
                    reused[model] = row["campaign"]
                    index["campaigns"].append(row["campaign"])
                    if extra:
                        index["topups"][row["campaign"]] = extra
                    if incomplete and topup_spent(parts) < policy.MAX_TOPUP_ATTEMPTS:
                        short[model] = parts
        pending = [m for m in models if resolve_model(m) not in reused]
        topping = [m for m in models if resolve_model(m) in short]
        failures: list[str] = []
        if pending or topping:
            root = Path(native_root).resolve() / ("screen-" + uuid.uuid4().hex)
            snapshot = root / "source"
            frozen = freeze(bundle, snapshot)
            if index.get("artifact_sha256") not in (None, frozen):
                raise RuntimeError("the task changed since its retained screen; screen it afresh")
            index["artifact_sha256"] = frozen
            with ThreadPoolExecutor(max_workers=len(pending) + len(topping)) as pool:
                futures = {pool.submit(_calibrate_model, bundle, snapshot, root, model,
                                       episodes, workers, TOPUP_RECEIPT): model for model in pending}
                futures.update({pool.submit(_calibrate_model, bundle, snapshot, root, model,
                                            episodes, workers, TOPUP_RECEIPT,
                                            existing=short[resolve_model(model)]): model
                                for model in topping})
                # Each model is collected on its own: one model's infrastructure
                # failure must not discard the other's finished, scored campaign.
                for future, model in futures.items():
                    try:
                        primary, extra, _ = future.result()
                    except Exception as exc:         # noqa: BLE001 -- recorded, then raised
                        failures.append(f"{model}: {type(exc).__name__}: {exc}")
                        continue
                    if primary not in index["campaigns"]:
                        index["campaigns"].append(primary)
                    if extra:
                        index["topups"][primary] = extra
        record = summarize(bundle, index, episodes, models)
        # A campaign short of valid episodes with its void budget spent cannot
        # be completed by re-running this command; say what can.
        stuck = sorted(model for model, row in (record.get("per_model") or {}).items()
                       if _short_only(row) and topup_spent(
                           [row["campaign"], *index["topups"].get(row["campaign"], [])])
                       >= policy.MAX_TOPUP_ATTEMPTS)
        record["topup_exhausted"] = stuck
        if stuck:
            record["remedy"] = remedy(bundle, stuck)
            record["shippable_problems"] = [record["remedy"], *record.get("shippable_problems", [])]
        record.update(label=label, reused_models=sorted(reused),
                      launched_models=[resolve_model(m) for m in pending],
                      topped_up_models=[resolve_model(m) for m in topping],
                      relaunched_incomplete=sorted(discarded),
                      discarded_campaigns=discarded,
                      launch_failures=failures)
        _write(bundle, record)
        if failures:
            raise RuntimeError("screen incomplete (finished campaigns were kept): "
                               + "; ".join(failures))
        return record


def run_many(bundles: list[Path], *, native_root: Path, task_workers: int = 8,
             label: str = "batch", relaunch_incomplete: bool = False) -> list[dict]:
    """Screen a batch concurrently. One task failing to screen never stops the rest."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(bundle: Path) -> dict:
        try:
            return run(bundle, native_root=native_root, label=label,
                       relaunch_incomplete=relaunch_incomplete)
        except Exception as exc:                     # noqa: BLE001 -- recorded, not raised
            return {"schema": SCHEMA, "bundle": Path(bundle).name,
                    "bundle_path": str(bundle), "error": f"{type(exc).__name__}: {exc}",
                    "shippable": False, "meets_target": False,
                    "outlook": {"verdict": "not-assessable",
                                "reasons": [f"the screen could not run: {type(exc).__name__}: {exc}"]}}

    out: list[dict] = []
    if not bundles:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(task_workers, len(bundles)))) as pool:
        futures = {pool.submit(one, b): b for b in bundles}
        for future in as_completed(futures):
            record = future.result()
            print(f"  screened {record.get('bundle', futures[future].name)}: "
                  + _one_line(record), flush=True)
            out.append(record)
    return sorted(out, key=lambda r: r.get("bundle", ""))


# ---------------------------------------------------------------------------
# Reading a screen
# ---------------------------------------------------------------------------

def _review_advisories(bundle: Path) -> list[str]:
    from .review import review_advisories
    return review_advisories(bundle)


def _check_rows(measured: dict) -> dict[str, dict]:
    """Objective checks as `step/id` -> counts, weight."""
    rows = {}
    for step, data in (measured.get("by_step") or {}).items():
        for cid, rec in (data.get("checks") or {}).items():
            if rec.get("kind") == "guardrail":
                continue
            rows[f"{step}/{cid}"] = {k: rec.get(k, 0) for k in ("passed", "failed", "gated_out")}
            rows[f"{step}/{cid}"]["weight"] = rec.get("weight")
    return rows


def _capability_edges(measured: dict, verdicts: dict) -> dict[str, int]:
    """Per objective check: how many CLEAN capability episodes failed it.

    A check failing in a capability episode is a live reasoning edge -- the model
    produced a well-formed deliverable and got this decision wrong. Contract and
    void episodes are excluded: they say nothing about the decision.
    """
    capability = {e for e, v in verdicts.items() if (v or {}).get("verdict") == "capability"}
    edges: dict[str, int] = {}
    for row in measured.get("rollouts") or []:
        if row.get("episode") not in capability:
            continue
        for cid, state in (row.get("checks") or {}).items():
            if state == "failed":
                key = f"{row.get('step')}/{cid}"
                edges[key] = edges.get(key, 0) + 1
    return edges


def summarize(bundle: Path, index: dict, episodes: int | None, models: list[str]) -> dict:
    """Everything a screen says, re-derived from its campaigns."""
    from .artifacts import identity
    from .benchmark.harbor import resolve_model
    from .metrics import hardness, task_metrics
    from .release import calibration_measurement
    from . import attribution
    from . import learning_signal as _signal

    bundle = Path(bundle)
    episodes = episodes or policy.SCREEN_EPISODES_PER_MODEL
    wanted = [resolve_model(m) for m in models]
    per_model: dict[str, dict] = {}
    problems: list[str] = []
    guard_ids: set[str] = set()
    for path in index.get("campaigns") or []:
        issues, measured = calibration_measurement(bundle, index, path, episodes)
        model = measured.get("model") or f"unreadable ({Path(path).name})"
        if model in per_model:
            problems.append(f"{model}: more than one screen campaign for this model")
            continue
        verdicts = {}
        try:
            attributed = attribution.attribute(bundle, measured, episodes)
            verdicts = attributed.get("episodes") or {}
            attribution_problems = attributed["problems"] + attribution.all_fail_problems(bundle, measured)
            counts = attributed["counts"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            attribution_problems = [f"attribution could not run: {type(exc).__name__}: {exc}"]
            counts = {}
        try:
            hits = attribution.trajectory_hacks(bundle, measured)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            hits = [{"trial": "?", "step": None, "kind": "uninspectable",
                     "evidence": f"{type(exc).__name__}: {exc}"}]
        rates = task_metrics(measured)
        per_model[model] = {
            "model": model,
            "campaign": str(path),
            "topups": (index.get("topups") or {}).get(str(path), []),
            "effort": measured.get("effort"),
            "limits": measured.get("limits"),
            "protocol": measured.get("protocol"),
            "episodes_requested": episodes,
            "episodes_valid": rates["episodes_valid"],
            "voids_excluded": measured.get("voids_excluded", 0),
            "passes": rates["passes"],
            "pass_at_1_uncertainty": rates["pass_at_1_uncertainty"],
            "pass_at_1": rates["pass_at_1"],
            "pass_at_4": rates["pass_at_4"],
            "mean_reward": rates["average_reward"],
            "attribution": counts,
            "attribution_problems": attribution_problems,
            "evidence_problems": issues,
            "reward_hacking": hits,
            "checks": _check_rows(measured),
            "capability_edges": _capability_edges(measured, verdicts),
            # Per valid episode, for the reward-spread half of the learning signal (Arena spec Oct 12).
            "episode_rewards": _signal.episode_rewards(measured),
            # How long the agent worked (Arena feedback 2026-10-30: long, natural trajectories).
            "episode_length": episode_lengths(measured),
        }
    try:
        from .author.contracts import strict_json
        guard_ids = {c["id"] for c in strict_json((bundle / "tests/checks.json").read_text())["checks"]
                     if c.get("kind") == "guardrail"}
    except (OSError, ValueError, KeyError):
        pass

    primary, secondary = (per_model.get(m) for m in (wanted + [None, None])[:2])
    verdict = hardness(
        {"pass_at_1": primary["pass_at_1"]} if primary else None,
        {"pass_at_1": secondary["pass_at_1"]} if secondary else None)
    worst = verdict["worst_pass_at_1"]
    meets = bool(verdict["both_models_measured"] and worst is not None
                 and worst < policy.CALIBRATION_TARGET_PASS_AT_1)

    census = _census(per_model, guard_ids)
    # FAILURE-MODE DIVERSITY (author/failure_modes.py, 2026-10-27): which different mistakes each model paid for.
    try:
        from .author import failure_modes as _failure_modes
        from .author.contracts import strict_json as _strict_json
        failure_mode_census = _failure_modes.census(
            _strict_json((bundle / "authoring/spec.json").read_text()), per_model)
    except (OSError, ValueError, KeyError, TypeError):
        failure_mode_census = None

    shippable = list(problems)
    if len(set(wanted)) < 2:
        shippable.append(f"a screen must measure two calibration models; got {wanted}")
    for model in wanted:
        row = per_model.get(model)
        if row is None:
            shippable.append(f"{model}: not screened")
            continue
        shippable += [f"{model}: {p}" for p in row["evidence_problems"]]
        if row["episodes_valid"] < episodes:
            shippable.append(f"{model}: {row['episodes_valid']} valid episodes of "
                             f"{episodes} after {row['voids_excluded']} void(s)")
        if row["effort"] != policy.SPEC_CALIBRATION_EFFORT:
            shippable.append(f"{model}: effort {row['effort']!r}, policy requires "
                             f"{policy.SPEC_CALIBRATION_EFFORT!r}")
        if (row["limits"] or {}).get("no_limits") is not policy.SPEC_NO_LIMITS:
            shippable.append(f"{model}: calibration must run without turn or time limits")
        shippable += [f"{model}: {p}" for p in row["attribution_problems"]]
        shippable += [f"{model}: {p}" for p in attribution.hack_problems(row["reward_hacking"])]
    measurement_complete = not shippable
    from .measurement import extreme_confirmation
    extreme = bool(measurement_complete and verdict["meets_extreme_target"])
    confirmation = extreme_confirmation(
        {m: per_model[m] for m in wanted if m in per_model}, policy.EXTREME_TARGET_PASS_AT_1)
    # The target is strict but never a blocker (policy.DIFFICULTY_GATES_RELEASE):
    # a task above it is shippable to the calibration of record once measured
    # cleanly. The gap is reported, not gated.
    target_gap = []
    if not meets:
        rates = ", ".join(f"{m} {per_model[m]['pass_at_1']:.2f}" for m in wanted
                          if m in per_model and per_model[m]["pass_at_1"] is not None)
        target_gap.append(f"pass@1 is not below {policy.CALIBRATION_TARGET_PASS_AT_1} on both "
                          f"models ({rates or 'unmeasured'}) -- the strict target; hardening "
                          "can aim at it, nothing is blocked by it")

    try:
        learning_signal = _signal.from_screen(bundle, {"per_model": per_model})
    except (OSError, ValueError, KeyError, TypeError):
        learning_signal = None
    try:
        sha = identity(bundle)
    except (OSError, ValueError):
        sha = None
    record = {
        "schema": SCHEMA,
        "bundle": bundle.name,
        "bundle_path": str(bundle),
        "artifact_sha256": index.get("artifact_sha256") or sha,
        "created_at": time.time(),
        "models": wanted,
        "episodes_per_model": episodes,
        "effort": policy.SPEC_CALIBRATION_EFFORT,
        "no_limits": policy.SPEC_NO_LIMITS,
        "target_pass_at_1": policy.CALIBRATION_TARGET_PASS_AT_1,
        "index": {k: index.get(k) for k in ("artifact_sha256", "campaigns", "topups")},
        # The reviewer's difficulty concerns, advisory only: this screen is
        # what measures them, and hardening reads them.
        "review_advisories": _review_advisories(bundle)[:10],
        "per_model": per_model,
        "hardness": verdict,
        "meets_target": meets,
        "extreme_target_pass_at_1": policy.EXTREME_TARGET_PASS_AT_1,
        "meets_extreme_target": extreme,
        "extreme_confirmation": confirmation,
        "measurement_complete": measurement_complete,
        "census": census,
        "failure_modes": failure_mode_census,
        "signal": learning_signal,
        "signal_problems": _signal.problems(learning_signal) if learning_signal else [],
        "signal_advisories": ((_signal.advisories(learning_signal) if learning_signal else [])
                              + length_advisories(per_model)),
        "episode_minutes_target": policy.EPISODE_MINUTES_TARGET,
        "shippable": not shippable,
        "shippable_problems": shippable,
        "target_gap": target_gap,
        "note": (f"{episodes} episodes per model is a screen, not the calibration of "
                 f"record: pass@1 moves in steps of {1 / episodes:.2f}. Only a shippable "
                 f"task earns the {policy.SPEC_EPISODES_PER_MODEL}-episode campaign."),
    }
    # Every step is measured on its own (policy.STEP_PASS_RATE_MAX): a whole-
    # task zero carried by one step leaves the other steps' difficulty unread.
    from .harden import step_pass_rates
    record["step_pass_rates"] = step_pass_rates(record)
    record["step_pass_rate_max"] = policy.STEP_PASS_RATE_MAX
    record["faithfulness_level"] = _level(bundle)
    record["outlook"] = outlook(bundle, record)
    return record


def episode_lengths(measured: dict) -> dict:
    """Agent minutes and output tokens per step over the valid episodes, read from Harbor's own trial results.

    Measured 2026-10-30 over 6,241 coding episodes: median 6.8 agent minutes (Fable) and 5.6 (Astra), failed
    episodes no longer than passed ones. Reported, never a gate (`policy.EPISODE_MINUTES_TARGET`)."""
    import statistics
    from datetime import datetime

    def stamp(value):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    minutes: list[float] = []
    per_step: dict[str, list[float]] = {}
    tokens: list[int] = []
    for trial in measured.get("trials") or []:
        if not isinstance(trial, dict) or not trial.get("valid") or not trial.get("result_file"):
            continue
        try:
            data = json.loads(Path(trial["result_file"]).read_text())
        except (OSError, ValueError, TypeError):
            continue
        for step in data.get("step_results") or [data]:
            execution = (step or {}).get("agent_execution") or {}
            try:
                spent = (stamp(execution["finished_at"]) - stamp(execution["started_at"])).total_seconds() / 60
            except (KeyError, ValueError, TypeError):
                continue
            minutes.append(spent)
            per_step.setdefault(step.get("step_name") or "single", []).append(spent)
            out = (step.get("agent_result") or {}).get("n_output_tokens")
            if type(out) is int:
                tokens.append(out)
    if not minutes:
        return {}
    ordered = sorted(minutes)
    return {"steps_measured": len(minutes), "median_minutes": round(statistics.median(minutes), 1),
            "p90_minutes": round(ordered[int(0.9 * (len(ordered) - 1))], 1), "max_minutes": round(ordered[-1], 1),
            "per_step_median_minutes": {k: round(statistics.median(v), 1) for k, v in per_step.items()},
            "median_output_tokens": int(statistics.median(tokens)) if tokens else None}


def length_advisories(per_model: dict) -> list[str]:
    """A screen whose episodes end quickly says the world has little to discover (read by the hardening brief)."""
    out = []
    for model, row in per_model.items():
        median = (row.get("episode_length") or {}).get("median_minutes")
        if median is not None and median < policy.EPISODE_MINUTES_TARGET:
            out.append(f"{model}: the median episode ran {median} agent minutes per step (advisory target "
                       f"{policy.EPISODE_MINUTES_TARGET}) -- there is little to discover. Deepen the world, never pad "
                       "it: facts spread over code, data, docs and logs; a defect seen only by running the program "
                       "on the world; a next graded defect behind the first (THE ENVIRONMENT IS A WORLD).")
    return out


def _census(per_model: dict, guard_ids: set[str]) -> dict:
    """Checks that measured nothing in this screen, across BOTH models.

    never_failed     attempted, and no episode on any model failed it: it did not
                     discriminate between attempts here
    never_attempted  gated out in every episode on every model: void weight
    never_passed     attempted, passed by no episode: legitimate only with its
                     proof obligations (`attribution.all_fail_problems`)
    """
    keys = sorted({k for row in per_model.values() for k in row["checks"]
                   if k.split("/", 1)[-1] not in guard_ids})
    census = {"never_failed": [], "never_attempted": [], "never_passed": [], "weights": {}}
    for key in keys:
        rows = [row["checks"][key] for row in per_model.values() if key in row["checks"]]
        attempted = sum(r["passed"] + r["failed"] for r in rows)
        census["weights"][key] = next((r.get("weight") for r in rows if r.get("weight") is not None), None)
        if not attempted:
            census["never_attempted"].append(key)
        elif not any(r["failed"] for r in rows):
            census["never_failed"].append(key)
        elif not any(r["passed"] for r in rows):
            census["never_passed"].append(key)
    return census


def _level(bundle: Path) -> str | None:
    try:
        return json.loads((Path(bundle) / "provenance/ledger.json").read_text()).get("faithfulness_level")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Hardening outlook
# ---------------------------------------------------------------------------

def _trap_material(bundle: Path, level: str | None) -> dict:
    """Grounded material a hardening round could still build a trap from."""
    material = {"usable_corrections": None, "corrections_used": 0,
                "unused_corrections": None, "authored_traps_left": None,
                "authored_bytes_left": None, "objectives": None, "objective_room": None}
    bundle = Path(bundle)
    try:
        traps = json.loads((bundle / "authoring/traps.json").read_text())
        usable = traps.get("usable_corrections")
        used = {t.get("derived_from") for t in traps.get("traps", [])
                if t.get("provenance") == "trace_derived" and t.get("derived_from")}
        material["usable_corrections"] = usable
        material["corrections_used"] = len(used)
        if type(usable) is int:
            material["unused_corrections"] = max(0, usable - len(used))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        from .harden import faithfulness_budget
        ledger = json.loads((bundle / "provenance/ledger.json").read_text())
        budget = faithfulness_budget(bundle, ledger, preserve_level=policy.HARDENING_PRESERVE_LEVEL)
        material["authored_traps_left"] = budget["authored_traps_left"]
        material["authored_bytes_left"] = budget["authored_bytes_left"]
    except Exception:                                # noqa: BLE001 -- a missing input is reported as None
        pass
    try:
        from .author.quality import BAND_LIMITS
        checks = json.loads((bundle / "tests/checks.json").read_text())["checks"]
        count = sum(1 for c in checks if c.get("kind") == "objective")
        material["objectives"] = count
        material["objective_room"] = sum(high for _, high in BAND_LIMITS.values()) - count
    except (OSError, ValueError, KeyError):
        pass
    return material


def outlook(bundle: Path, record: dict) -> dict:
    """How confident a hardening round would make this task meet the target.

    A HEURISTIC over the screen, not a measurement, and not yet validated
    against hardening outcomes. It encodes what the calibration data so far
    says (rulesets/PLAYBOOK_GENERAL.md, the seventh lever):

      - what defeats a frontier model is a premise that changes, a governing
        conflict or a residue that invalidates an answer it already computed;
        breadth and volume do not;
      - one premise that poisons several checks drives pass@1 (FULL success)
        down, so hardening routes checks that never failed through a premise;
      - of five 1.00/1.00 bundles given premise revisions, four stayed at
        1.00/1.00. A task nobody failed at all is the weakest candidate.

    strong-candidate  every model above target already fails some check in a
                      clean capability episode (a live edge to widen), there
                      is grounded trap material left, and at least two checks
                      that never failed are free to route through a premise
    weak-candidate    grounded material and something to work with, but no
                      live edge on every model above target
    poor-candidate    no grounded material left to build a genuine trap from,
                      or nothing to route through one
    """
    from .harden import MIN_CHECKS_PER_NEW_TRAP

    per_model = record.get("per_model") or {}
    models = record.get("models") or []
    level = record.get("faithfulness_level")
    signals: dict = {"level": level}
    unmet = list(record.get("shippable_problems", []))
    # A reward-hacking flag is unresolved until a human adjudicates it, so it
    # always blocks shipping (a validity rule, not a difficulty one). It does not by itself void the difficulty
    # reading: it is carried as a caveat (a flagged pass may be inflated).
    hacks = [p for p in unmet if "reward hacking unresolved" in p]
    invalid = [p for p in unmet if p not in hacks]
    caveats = ([f"{len(hacks)} reward-hacking flag(s) awaiting adjudication -- the task "
                "cannot ship until each is resolved, and a flagged pass may be inflated:",
                *[h[:240] for h in hacks[:3]]] if hacks else [])
    if invalid:
        return {"verdict": "not-assessable", "signals": signals,
                "reasons": ["the screen did not measure cleanly, so difficulty cannot be "
                            "read from it yet -- fix these first:", *invalid[:6], *caveats]}
    rates = {m: per_model[m]["pass_at_1"] for m in models}
    from .harden import extreme_target_met
    if extreme_target_met(record):
        hard = (record.get("hardness") or {}).get("hard")
        return {"verdict": "meets-target", "signals": {**signals, "pass_at_1": rates},
                "caveats": caveats,
                "reasons": [f"pass@1 {', '.join(f'{m} {r:.2f}' for m, r in rates.items())}: "
                            f"at most {policy.EXTREME_TARGET_PASS_AT_1} on both models (provisional screen)"
                            + (f"; hard on the primary (<= {policy.SPEC_HARD_MAX})" if hard else "")
                            + f". Run the {policy.SPEC_EPISODES_PER_MODEL}-episode "
                              "calibration of record (`arena qualify`)."
                            if record.get("shippable") else
                            "meets the target but did not measure cleanly: "
                            + "; ".join(record.get("shippable_problems", [])[:3])]}

    above = [m for m in models if rates.get(m) is not None
             and rates[m] > policy.EXTREME_TARGET_PASS_AT_1]
    edges = {m: per_model[m]["capability_edges"] for m in above}
    live = bool(above) and all(edges[m] for m in above)
    split = len(above) < len(models)
    census = record.get("census") or {}
    free = census.get("never_failed", [])
    material = _trap_material(bundle, level)
    unused = material["unused_corrections"] or 0
    authored_room = material["authored_traps_left"]
    if level == "F3" and policy.HARDENING_PRESERVE_LEVEL:
        # A replay grades from what the user corrected: new traps must cite
        # real corrections, so authored material does not count.
        grounded = unused > 0
    else:
        grounded = unused > 0 or authored_room is None or authored_room > 0
    routable = len(free) >= MIN_CHECKS_PER_NEW_TRAP or (material["objective_room"] or 0) > 0
    signals.update({"pass_at_1": rates, "above_target": above,
                    "live_edges": {m: sorted(e) for m, e in edges.items()},
                    "one_model_already_below": split,
                    "never_failed_checks": free,
                    "never_failed_weight": sum(census.get("weights", {}).get(k) or 0 for k in free),
                    **material})

    reasons = []
    step_rates = record.get("step_pass_rates") or {}
    for m, steps in sorted(step_rates.items()):
        easy = [f"{s} {r:.0%}" for s, r in sorted(steps.items()) if r > policy.STEP_PASS_RATE_MAX]
        if easy:
            reasons.append(f"{m}: step(s) fully passed above the per-step bar "
                           f"({policy.STEP_PASS_RATE_MAX:.0%}): {', '.join(easy)} -- each needs its own interlock")
    for m in above:
        if edges[m]:
            worst = sorted(edges[m].items(), key=lambda kv: -kv[1])[:3]
            reasons.append(f"{m} already fails " + ", ".join(f"{k} ({n}x)" for k, n in worst)
                           + " in clean capability episodes: a premise routed through "
                             "those decisions has a live edge to widen")
        else:
            reasons.append(f"{m} failed no check in any clean capability episode "
                           f"(pass@1 {rates[m]:.2f}): no live edge to widen")
    if split:
        reasons.append("one model is already below the target, so the construct exists; "
                       "the question is whether it transfers")
    reasons.append(f"{len(free)} objective check(s) never failed on either model -- free to "
                   "route through a premise" if free else
                   "every objective failed at least once; room must come from new checks")
    reasons.append(
        (f"{unused} usable trace correction(s) not yet used as traps"
         if unused else "no unused trace corrections")
        + ("" if level == "F3" else
           "; authored-trap room: " + ("no ceiling at this level" if authored_room is None
                                       else str(authored_room))))
    if not grounded:
        reasons.append("an F3 task's new traps must cite real corrections, and none are left"
                       if level == "F3" else "no grounded trap material left within the "
                       "faithfulness budget")

    if grounded and routable and live:
        verdict = "strong-candidate"
    elif grounded and routable and (live or split or free):
        verdict = "weak-candidate"
    else:
        verdict = "poor-candidate"
    return {"verdict": verdict, "signals": signals, "reasons": reasons + caveats,
            "basis": "heuristic over this screen; unvalidated against hardening outcomes"}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(value) -> str:
    return "  -- " if value is None else f"{value:.2f}"


def _one_line(record: dict) -> str:
    if record.get("error"):
        return f"ERROR {record['error'][:100]}"
    bits = []
    for model, row in (record.get("per_model") or {}).items():
        bits.append(f"{model.split('/')[-1]} p@1 {_fmt(row['pass_at_1'])} "
                    f"p@4 {_fmt(row['pass_at_4'])} mean {_fmt(row['mean_reward'])}")
    return " | ".join(bits) + f" | {record.get('outlook', {}).get('verdict', '?')}"


def report(records: list[dict], not_screened: list[dict] | None = None) -> dict:
    """The batch table: every task, both models, and the hardening outlook."""
    primary, secondary = policy.resolve_calibration_models()
    rows = []
    for record in records:
        row = {"bundle": record.get("bundle"), "level": record.get("faithfulness_level"),
               "error": record.get("error"), "meets_target": record.get("meets_target"),
               "meets_extreme_target": record.get("meets_extreme_target", False),
               "extreme_confirmation": record.get("extreme_confirmation"),
               "shippable": record.get("shippable"),
               "outlook": (record.get("outlook") or {}).get("verdict"),
               "outlook_reasons": (record.get("outlook") or {}).get("reasons", []),
               "census": record.get("census"), "models": {},
               "failure_modes": record.get("failure_modes"),
               "step_pass_rates": record.get("step_pass_rates"),
               "review_advisories": record.get("review_advisories") or []}
        for model, data in (record.get("per_model") or {}).items():
            row["models"][model] = {k: data.get(k) for k in (
                "pass_at_1", "pass_at_4", "mean_reward", "episodes_valid",
                "voids_excluded", "attribution", "pass_at_1_uncertainty")}
        rows.append(row)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outlook"] or "?"] = counts.get(row["outlook"] or "?", 0) + 1
    return {
        "schema": "arena-pipeline/screen-report/1",
        "created_at": time.time(),
        "measurement": {"models": [primary, secondary],
                        "episodes_per_model": policy.SCREEN_EPISODES_PER_MODEL,
                        "effort": policy.SPEC_CALIBRATION_EFFORT,
                        "no_limits": policy.SPEC_NO_LIMITS,
                        "agent": "; ".join(
                            f"{m} in {h} {policy.HARNESS_VERSIONS[h]}"
                            for m, h in policy.CALIBRATION_HARNESS.items()),
                        "target": f"pass@1 < {policy.CALIBRATION_TARGET_PASS_AT_1} on both models",
                        "extreme_target": f"pass@1 <= {policy.EXTREME_TARGET_PASS_AT_1} on each model"},
        "tasks_screened": len(rows),
        "meets_target": sum(1 for r in rows if r["meets_target"]),
        "meets_extreme_target": sum(1 for r in rows if r["meets_extreme_target"]),
        "shippable": sum(1 for r in rows if r["shippable"]),
        "outlook_counts": counts,
        "rows": rows,
        "not_screened": not_screened or [],
    }


def markdown(doc: dict) -> str:
    """The report as a table a reviewer can read without the JSON."""
    m = doc["measurement"]
    models = m["models"]
    short = [x.split("/")[-1] for x in models]
    lines = [f"# Calibration screen -- {doc['tasks_screened']} task(s)", "",
             f"{m['episodes_per_model']} episodes per model on {', '.join(short)}; effort "
             f"{m['effort']}; {'no turn or time limits' if m['no_limits'] else 'LIMITED'}; "
             f"{m['agent']}. Target: {m['target']}. Rates are over valid episodes "
             "(voids excluded and topped up).", "",
             f"Extreme design goal: {m.get('extreme_target', '<= 0.05 on each model')}. "
             "Small screens are provisional; confirmation needs fresh fixed cohorts on a frozen candidate.", "",
             "| task | level | " + " | ".join(f"{s} p@1 | {s} p@4 | {s} mean" for s in short)
             + " | target | extreme observed | ready for 10x2 | outlook |",
             "|---|---|" + "---|" * (3 * len(short)) + "---|---|---|---|"]
    from .benchmark.harbor import resolve_model
    for row in doc["rows"]:
        cells = []
        for model in models:
            data = row["models"].get(resolve_model(model)) or {}
            cells += [_fmt(data.get("pass_at_1")), _fmt(data.get("pass_at_4")),
                      _fmt(data.get("mean_reward"))]
        target = "yes" if row["meets_target"] else ("error" if row["error"] else "no")
        ready = "yes" if row["shippable"] else "no"
        extreme = "yes (provisional)" if row.get("meets_extreme_target") else "no"
        lines.append(f"| {row['bundle']} | {row['level'] or '?'} | " + " | ".join(cells)
                     + f" | {target} | {extreme} | {ready} | {row['outlook']} |")
    lines += ["", f"## Step-wise pass@1 (share of valid episodes that fully passed the step; "
              f"bar <= {policy.STEP_PASS_RATE_MAX:.0%} on each model)", ""]
    for row in doc["rows"]:
        rates = row.get("step_pass_rates") or {}
        if not rates:
            lines.append(f"- {row['bundle']}: per-step rates unreadable")
            continue
        for step in sorted({s for steps in rates.values() for s in steps}):
            cells = ", ".join(f"{model.split('/')[-1]} {steps.get(step, 0):.0%}"
                              for model, steps in sorted(rates.items()))
            flag = (" **above the bar**" if any(steps.get(step, 0) > policy.STEP_PASS_RATE_MAX
                                                 for steps in rates.values()) else "")
            lines.append(f"- {row['bundle']} {step}: {cells}{flag}")
    lines += ["", "## Hardening outlook", ""]
    for verdict in OUTLOOKS:
        members = [r for r in doc["rows"] if r["outlook"] == verdict]
        if not members:
            continue
        lines.append(f"### {verdict} ({len(members)})")
        for row in members:
            lines.append(f"- **{row['bundle']}**")
            lines += [f"  - {reason}" for reason in row["outlook_reasons"]]
            from .author import failure_modes as _failure_modes
            lines += [f"  - {reason}" for reason in _failure_modes.census_reasons(row)]
            lines += [f"  - reviewer difficulty advisory: {text[:240]}"
                      for text in row.get("review_advisories", [])[:3]]
        lines.append("")
    lines += ["## Checks that measured nothing in this screen", ""]
    for row in doc["rows"]:
        census = row.get("census") or {}
        dead = census.get("never_failed") or []
        void = census.get("never_attempted") or []
        if dead or void:
            lines.append(f"- {row['bundle']}: never failed {dead or '-'}; never attempted {void or '-'}")
    if doc.get("not_screened"):
        lines += ["", "## Not screened", ""]
        lines += [f"- {r.get('slug')}: {r.get('status')} at {r.get('stage_reached')} -- "
                  f"{(r.get('error') or '; '.join(r.get('blockers') or []))[:200]}"
                  for r in doc["not_screened"]]
    lines += ["", "The outlook is a heuristic over this screen, not a measurement, and is "
              "not yet validated against hardening outcomes. Five episodes put pass@1 on a "
              "0.2 grid; the calibration of record is ten. The target is strict but blocks "
              "nothing: every task that measured cleanly may go to the calibration of record."]
    return "\n".join(lines) + "\n"


def print_table(doc: dict) -> None:
    print(markdown(doc))


def main(argv: list[str] | None = None) -> int:
    """`arena screen` -- screen built tasks, or report the screens already taken."""
    import argparse
    from .config import ARTIFACTS, bundle_paths
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="bundles, or trees of bundles")
    ap.add_argument("--report-only", action="store_true",
                    help="read the retained screens; launch nothing")
    ap.add_argument("--native-root", default=str(ARTIFACTS / "native"))
    ap.add_argument("--task-workers", type=int, default=8)
    ap.add_argument("--label", default="manual")
    ap.add_argument("--include-unvalidated", action="store_true",
                    help="screen bundles whose gate chain is not green (spends episodes "
                         "measuring a grader that failed its own checks)")
    ap.add_argument("--relaunch-incomplete", action="store_true",
                    help="replace a retained campaign that is still short of valid episodes "
                         "after its void top-ups with a fresh one. This RE-RUNS scored "
                         "episodes, so the receipt names every campaign it discarded")
    ap.add_argument("-o", "--out", help="where to write the JSON report (a .md is written beside it)")
    args = ap.parse_args(argv)

    bundles: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        bundles += [path] if (path / "task.toml").is_file() else list(bundle_paths(path))
    bundles = sorted(dict.fromkeys(b.resolve() for b in bundles))
    if not bundles:
        print("no bundles found")
        return 2
    skipped: list[dict] = []
    if not args.report_only and not args.include_unvalidated:
        # Only a VALID task is worth ten paid episodes: a grader that fails its
        # own gates would be measured wrong. The gate chain re-runs the probes
        # and the executed controls, so green here is the build's validity bar
        # minus the semantic review the build already required.
        from .run import run_bundle
        eligible = []
        for bundle in bundles:
            try:
                gates = run_bundle(bundle, Path(__file__).resolve().parents[1])
                verdict = gates["verdict"]
                why = [f"{g['gate']}: {b['code']}" for g in gates["results"] for b in g["blockers"]]
            except Exception as exc:                 # noqa: BLE001 -- could not check is not green
                verdict, why = "unverified", [f"{type(exc).__name__}: {exc}"]
            if verdict == "green":
                eligible.append(bundle)
            else:
                skipped.append({"slug": bundle.name, "status": verdict, "stage_reached": "gate",
                                "error": "", "blockers": why[:6]})
                print(f"  not screened {bundle.name}: gate chain {verdict}", flush=True)
        bundles = eligible
    if args.report_only:
        records = []
        for bundle in bundles:
            record = current(bundle)
            records.append(record or {"bundle": bundle.name, "error": "no current screen",
                                      "outlook": {"verdict": "not-assessable",
                                                  "reasons": ["not screened at this version"]}})
    else:
        print(f"screening {len(bundles)} task(s): {policy.SCREEN_EPISODES_PER_MODEL} episodes x "
              f"{len(policy.resolve_calibration_models())} models", flush=True)
        records = run_many(bundles, native_root=Path(args.native_root),
                           task_workers=args.task_workers, label=args.label,
                           relaunch_incomplete=args.relaunch_incomplete)
    doc = report(records, not_screened=skipped)
    out = Path(args.out) if args.out else ARTIFACTS / f"screen-report-{int(time.time())}.json"
    write_report(doc, out)
    print_table(doc)
    print(f"-> {out}")
    return 0 if all(not r.get("error") for r in records) else 1


def write_report(doc: dict, out: Path) -> None:
    from .artifacts import atomic_json
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(out, doc)
    out.with_suffix(".md").write_text(markdown(doc))


if __name__ == "__main__":
    raise SystemExit(main())
