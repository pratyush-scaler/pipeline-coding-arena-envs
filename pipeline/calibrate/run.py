"""Stage H -- run a calibration campaign and emit the report the gates read.

Produces exactly the shape `calibration.bands` and `calibration.validity`
consume, which is the point: bands are measured here and enforced there, so a
task cannot ship on declared intent.

The two rules this runner exists to honour:

* **`gated_out` is not `failed`.** A check whose prerequisite failed was never
  attempted, and counting it as a failure is what makes an over-gated task look
  uniformly hard rather than broken. Pass rates are computed over reachable
  attempts only.
* **Every zero is classified before it reaches a table.** A rollout that made no
  tool calls, hit the step cap without submitting, or died in the harness did not
  score 0 -- it produced no score. Those are `void`, excluded from band
  estimates, and reported separately.

Rollouts run concurrently because they are independent; the fixture is copied per
rollout, so nothing shared can drift between them.
"""

from __future__ import annotations

import json
import re
import os
import sys
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from .. import policy

# The document expression grader is gone; the file-workspace protocol only
# serves the semantic reviewer's rollout now (`_rollout_for`).
Workspace = grade = load = None
from ..config import ROLLOUTS_PER_STEP
from .agent import Rollout, rollout


def _rollout_for(model: str):
    """Pick the transport by model name; the grading is identical either way.

    A calibration figure names a model, so the campaign must be able to run any
    model the programme cares to measure. What must NOT vary is what happens
    after the rollout: the same three tools, the same `_run_tool`, the same
    `Rollout` record, the same grader, the same `classify_zero`. Only the wire
    format differs.
    """
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        from .agent_openai import rollout as openai_rollout
        return openai_rollout
    return rollout


def _instruction(bundle: Path, step: str | None = None) -> tuple[str, str]:
    steps = sorted((bundle / "steps").glob("*/instruction.md"))
    if not steps:
        raise FileNotFoundError(f"no steps/*/instruction.md under {bundle}")
    if step is not None and step not in {p.parent.name for p in steps}:
        raise ValueError(f"unknown step {step!r}")
    chosen = next((p for p in steps if p.parent.name == step), steps[0])
    return chosen.read_text(), chosen.parent.name


def classify_zero(record: Rollout) -> str | None:
    """Was this zero the model's, or ours?

    A void is worse than a missing data point, because once it reaches a results
    table it is indistinguishable from a real result.
    """
    if record.error:
        return "void"
    if record.tool_calls == 0:
        return "void"                     # adapter stall: measures nothing
    # Truncation, in either of the two shapes it takes. `step_cap` is the
    # harness cutting the episode; `max_tokens` is the provider cutting the
    # model mid-generation, and the deliverable is then simply never written --
    # measured on a008, where all ten rollouts stopped on `max_tokens` with an
    # empty deliverables/ directory and were recorded as ten model failures at
    # mean 0.0. That is a void wearing a result's clothes, which this module's
    # own docstring calls the thing it exists to prevent: "a truncated run is
    # indistinguishable from a wrong answer once it reaches the score file".
    if record.stop_reason in ("step_cap", "max_tokens", "incomplete", "failed", "cancelled"):
        return "void"                     # truncation, not incapability
    if record.reward in (None,):
        return "void"
    if record.reward == 0.0:
        # A zero while every scored check PASSED needs its cause named before it
        # reaches a table. There are two very different ones:
        #
        #   contract   the submission broke the output contract -- an undeclared
        #              file, a missing one, a malformed schema. `grade()` zeroes
        #              the step outright. Measured on
        #              a_f2_engineering-thesis-writing-002: Fable 5.1 answered
        #              step 2 correctly (earned 14 of 14) and scored 0 for
        #              writing one extra file beside the two declared ones. That
        #              is a real result about the model's restraint, not a void.
        #
        #   incoherent no contract error, no guardrail violation, nothing
        #              failed -- the grader and the reward disagree. That is
        #              ours, and it must never be booked as the model's.
        #
        # The first briefly read as the only non-1.00 screen in eighteen and
        # nearly became the basis of a design strategy; it is difficulty of a
        # kind, but of the kind `attribution` caps as a share rather than
        # crediting as capability.
        states = (record.checks or {}).values()
        scored = [s for s in states if s in ("passed", "failed")]
        if scored and all(s == "passed" for s in scored):
            detail = getattr(record, "grade", None) or {}
            if detail.get("contract_errors") or detail.get("guardrails_violated"):
                return "model_failure"
            return "void"
        return "model_failure"
    return None


def _miss_shape(target, actual) -> str | None:
    """How a failed check failed, when the two values are comparable.

    A `set_equals` failure is binary in the score and anything but binary in
    what it tells you. Measured 2026-09-17: four kw-a007 checks scored 0 on
    submissions that were the reference answer verbatim, differing only by the
    markdown `#` the parser strips; two kw-a003 checks missed one item of
    thirteen. All read as capability failures, and one of them was a defect in
    the fixture rather than the model.

    So every failed objective records the shape of its miss, and the validity
    gate reads it. `equal_normalised` is an outright grader or specification
    defect -- the answer was right. A high overlap is not proof of one, but it
    is the queue a human should read before publishing the score as difficulty.
    """
    def normalise(value):
        return " ".join(str(value).split()).casefold()
    if target is None or actual is None:
        return None
    if isinstance(target, dict) and isinstance(actual, dict):
        # A record (a scoreboard, a summary object) is compared leaf by leaf:
        # one wrong figure of nine is a near miss, not an unrelated answer.
        # Before this every record check was either equal or silent, so the
        # near-miss obligation could never fire on one.
        t, a = Counter(_record_leaves(target, normalise)), Counter(_record_leaves(actual, normalise))
        if not t and not a:
            return None
        if t == a:
            return "equal_normalised"
        union = sum((t | a).values()) or 1
        return f"overlap_{sum((t & a).values()) / union:.2f}"
    if isinstance(target, (list, tuple, set)) and isinstance(actual, (list, tuple, set)):
        # Preserve multiplicity: duplicate/missing records are substantive.
        t = Counter(str(normalise(v)) for v in target)
        a = Counter(str(normalise(v)) for v in actual)
        if not t and not a:
            return None
        if t == a:
            return "equal_normalised"
        union = sum((t | a).values()) or 1
        return f"overlap_{sum((t & a).values()) / union:.2f}"
    try:
        if normalise(target) == normalise(actual):
            return "equal_normalised"
    except Exception:
        return None
    return None


def _record_leaves(value, normalise, path: str = ""):
    """(path, normalised leaf) pairs of a nested record; lists keep multiplicity."""
    if isinstance(value, dict):
        for key in sorted(value, key=str):
            yield from _record_leaves(value[key], normalise, f"{path}/{key}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield (f"{path}[]", str(normalise(item)))
    else:
        yield (path, str(normalise(value)))


def _grader_fingerprint(bundle: Path) -> str:
    from ..artifacts import identity
    return identity(bundle)


def _output_budget(model: str) -> int:
    """Output cap for one turn, by provider.

    They are not comparable numbers. The OpenAI Responses API counts reasoning
    tokens against `max_output_tokens`, so the same task needs a larger cap
    there to produce the same visible answer. A turn cut off by either limit
    wrote no deliverable and is classified VOID, so too small a budget destroys
    data rather than measuring difficulty -- measured on
    a-003-renovation-project-scheduling, where three Opus rollouts voided at
    32k on a task it completes at 0.80.
    """
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        from .agent_openai import OPENAI_MAX_TOKENS
        return OPENAI_MAX_TOKENS
    from .agent import ROLLOUT_MAX_TOKENS
    return ROLLOUT_MAX_TOKENS


def campaign(bundle: Path, model: str, n: int = ROLLOUTS_PER_STEP,
             step: str | None = None, workers: int = 4,
             max_steps: int | None = None, rollout_budget=None) -> dict:
    """Freeze once; run complete sequential episodes with immutable step outputs.

    A step-specific diagnostic still runs every preceding step to obtain genuine
    state and conversation history. It is not a complete release campaign.
    """
    raise NotImplementedError("the file-workspace campaign grades document deliverables; coding tasks are "
                              "measured through the Harbor harness campaigns (benchmark.harbor.run_campaign)")
    import hashlib
    import uuid
    from ..artifacts import freeze, identity, atomic_json, harness_identity
    from ..evaluate import exported_grade
    step_files = None
    from .agent import ROLLOUT_MAX_TOKENS, TOOLS
    from ..tool_surface import reconcile
    # Which agent produced these numbers. A screening surface and the shipped
    # Terminus-2 surface are not comparable, and only this says which ran.
    surface = reconcile(bundle, TOOLS, agent="pipeline.calibrate.agent")
    # No turn limit by default. The cap exists so a looping agent cannot run
    # forever, not to bound a legitimate task: a run stopped on `step_cap`
    # wrote no deliverable and is recorded VOID, so a tight limit destroys
    # data rather than measuring difficulty. Set it very high and let the
    # provider's own limits and the read timeout be the real bound.
    # Refuse a retired calibration model before anything is frozen or paid
    # for. Presolve and every other caller reach the solver through here.
    policy.refuse_retired(model)
    if max_steps is None:
        # No turn limit (policy.SPEC_NO_LIMITS); ARENA_MAX_STEPS exists only for
        # an explicit diagnostic bound.
        max_steps = (int(os.environ["ARENA_MAX_STEPS"]) if os.environ.get("ARENA_MAX_STEPS")
                     else sys.maxsize)
    if n < 1 or workers < 1 or max_steps < 1:
        raise ValueError("rollouts, workers and max_steps must be positive")
    bundle = bundle.resolve()
    harness_before = harness_identity()
    run_id = uuid.uuid4().hex
    root = bundle / "verification/campaigns" / run_id
    snapshot = root / "frozen"
    fingerprint = freeze(bundle, snapshot)
    config = tomllib.loads((snapshot / "task.toml").read_text())
    names = [s["name"] for s in config["steps"]]
    if step is not None:
        if step not in names:
            raise ValueError(f"unknown step {step!r}")
        names = names[:names.index(step) + 1]
    fixture = snapshot / "environment/repo"
    spec_path = snapshot / "authoring/spec.json"
    spec_for_steps = json.loads(spec_path.read_text()) if spec_path.is_file() else {}
    checks = load(snapshot / "tests/checks.json")
    rubric = json.loads((snapshot / "tests/rubric.json").read_text())
    # Catch stale or broken exports before paying for even one solver attempt.
    # The previous path discovered this only after the model had submitted.
    for name in names:
        control = exported_grade(snapshot, snapshot / "solution/submission", name)
        if control["reward"] != 1:
            raise RuntimeError(f"{name}: calibration preflight reference must score 1")
    run = _rollout_for(model)

    def one(index: int) -> list[dict]:
        records, prior = [], None
        for name in names:
            rid = f"episode-{index:03d}/{name}"
            work = root / rid
            instruction = (snapshot / "steps" / name / "instruction.md").read_text()
            record = None
            try:
                kwargs = {"max_steps": max_steps,
                          "max_tokens": _output_budget(model),
                          }
                # Hide whatever a LATER step releases: without this a two-step
                # task hands the agent step 2's documents during step 1 and the
                # revision is answerable before it is asked. Passed only when
                # non-empty, so a rollout implementation that does not know the
                # parameter is unaffected by a feature the task never uses.
                hidden = step_files.withheld_from(spec_for_steps, name, names)
                if hidden:
                    kwargs["withhold"] = hidden
                if prior is not None:
                    kwargs["prior"] = prior
                if rollout_budget is not None:
                    with rollout_budget:
                        record, submitted = run(instruction, fixture, work, model, rid, **kwargs)
                else:
                    record, submitted = run(instruction, fixture, work, model, rid, **kwargs)
                # Score the exact exported code against the attempt's fixture,
                # before the next instruction can change a previous deliverable.
                result = exported_grade(snapshot, submitted, name, work / "workspace")
                atomic_json(work / "grade.json", result)
                record.reward = result["reward"]
                record.checks = {cid: ("gated_out" if r.get("gated_out") else
                                      "passed" if r.get("passed") else "failed")
                                 for cid, r in result["checks"].items()}
                record.misses = {cid: shape for cid, r in result["checks"].items()
                                 if not r.get("passed") and not r.get("gated_out")
                                 and (shape := _miss_shape(r.get("target"), r.get("actual")))}
                record.zero_class = classify_zero(record)
            except Exception as exc:
                record = record or Rollout(id=rid)
                record.error = f"{type(exc).__name__}: {exc}"
                record.reward, record.checks, record.zero_class = None, {}, "void"
            rec = {**asdict(record), "step": name, "episode": index,
                   "work_dir": work.relative_to(bundle).as_posix(),
                   # Which agent produced this number. A screening rate and a
                   # calibration rate are not comparable, and only this field
                   # says which one you are holding.
                   "tool_surface": surface}
            rec["artifacts"] = {p.relative_to(work).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(work.rglob("*")) if p.is_file() and not p.is_symlink()}
            atomic_json(work / "record.json", rec)
            records.append(rec)
            if record.zero_class == "void":
                break
            prior = work
        return records

    with ThreadPoolExecutor(max_workers=min(n, workers)) as pool:
        episodes = list(pool.map(one, range(1, n + 1)))
    records = [r for episode in episodes for r in episode]
    complete = [episode for episode in episodes if len(episode) == len(names)
                and all(r["zero_class"] != "void" for r in episode)]
    per_check, by_step = {}, {}
    for name in names:
        # Use only complete episodes for every step, avoiding survivorship bias.
        valid = [next(r for r in episode if r["step"] == name) for episode in complete]
        step_checks = {}
        for check in checks:
            if check.id not in rubric[name]:
                continue
            counts = Counter(r["checks"].get(check.id) for r in valid)
            step_checks[check.id] = {"kind": check.kind, "band_declared": check.band,
                "weight": check.weight, "passed": counts["passed"], "failed": counts["failed"],
                "gated_out": counts["gated_out"]}
        by_step[name] = {"checks": step_checks, "valid": len(valid),
                         "rewards": [r["reward"] for r in valid]}
        # IDs are globally unique except the shared integrity guardrail.
        per_check.update(step_checks)
    rewards = [sum(r["reward"] for r in episode) / len(names) for episode in complete]
    stable = identity(bundle) == fingerprint and identity(snapshot) == fingerprint
    report = {"schema": "arena-pipeline/calibration/2", "campaign_id": run_id,
        "bundle": bundle.name, "step": step or (names[0] if len(names) == 1 else "episode"),
        "steps": names, "complete_episode": step is None, "model": model,
        "protocol": "file-workspace/2", "effort": policy.SPEC_CALIBRATION_EFFORT,
        "max_steps": max_steps, "max_tokens": _output_budget(model),
        "snapshot": snapshot.relative_to(bundle).as_posix(),
        "grader_fingerprint": fingerprint, "grader_stable": stable,
        "harness_sha256": harness_before, "harness_stable": harness_identity() == harness_before,
        "rollouts": records, "checks": per_check, "by_step": by_step,
        "gates": {c.id: c.requires for c in checks if c.requires},
        "summary": {"requested": n, "valid": len(complete), "void": n - len(complete),
            "mean_reward": round(sum(rewards) / len(rewards), 4) if rewards else None,
            "distinct_rewards": len(set(rewards)), "min": min(rewards) if rewards else None,
            "max": max(rewards) if rewards else None}}
    atomic_json(root / "report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Stage H -- run a calibration campaign")
    ap.add_argument("bundle")
    ap.add_argument("-m", "--model", default=policy.resolve_calibration_models()[0])
    ap.add_argument("-n", "--rollouts", type=int, default=ROLLOUTS_PER_STEP)
    ap.add_argument("--step", default=None)
    ap.add_argument("-j", "--workers", type=int, default=4)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args(argv)

    bundle = Path(args.bundle).resolve()
    report = campaign(bundle, args.model, args.rollouts, args.step,
                      args.workers, args.max_steps)
    out = Path(args.out) if args.out else (
        bundle / "verification" / f"calibration-{args.model}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))

    s = report["summary"]
    print(f"{bundle.name} [{report['step']}] {args.model}")
    print(f"  {s['valid']}/{s['requested']} valid ({s['void']} void)  "
          f"mean {s['mean_reward']}  range {s['min']}-{s['max']}  "
          f"distinct {s['distinct_rewards']}")
    print(f"  {'check':28s}{'kind':11s}{'pass':>5s}{'fail':>5s}{'gated':>6s}{'rate':>7s}  band")
    for cid, row in report["checks"].items():
        attempts = row["passed"] + row["failed"]
        rate = row["passed"] / attempts if attempts else None
        from ..measurement import band as measured_band
        band = measured_band(rate)
        flag = "" if band == row["band_declared"] or rate is None else \
               f"  (declared {row['band_declared']})"
        print(f"  {cid:28s}{row['kind']:11s}{row['passed']:5d}{row['failed']:5d}"
              f"{row['gated_out']:6d}{'    n/a' if rate is None else f'{rate:7.2f}'}"
              f"  {band}{flag}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
