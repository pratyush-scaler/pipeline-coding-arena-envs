"""The per-turn contract of a multi-turn task (Arena spec, Appendix A).

"What to include with a multi-turn submission:
  - The user prompt for each turn.
  - The environment state passed between turns.
  - The expected outcome for each turn.
  - Rollout results showing where the agent succeeds or struggles, per turn."

`report(package)` derives all four from what the package already carries --
task.toml and steps/<s>/instruction.md (prompts), the reference patches
(the canonical state between turns), tests/checks.json with the instruction
sentence each check enforces (expected outcome), verification/controls.json
(the executed prefix_only / skip_prior evidence that each turn needs new work
and builds on the previous one) and runs/index.json (per-trial, per-step
rubric scores) -- and `write(package)` puts TURNS.md and verification/turns.json
into it. Nothing is typed by hand; a package without calibration runs says so.

    python -m pipeline.turns <package-dir> [--check]
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path

from . import policy

SCHEMA = "arena-coding/turns/1"
MARKDOWN = "TURNS.md"
RECEIPT = "verification/turns.json"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _steps(package: Path) -> list[str]:
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    return [s.get("name") for s in config.get("steps") or [] if s.get("name")]


def _rollouts(package: Path, steps: list[str]) -> dict:
    """Per model, per step: episodes, full passes, mean score, the checks failed most."""
    index = _read_json(package / "runs/index.json") or {}
    out: dict = {}
    for campaign in index.get("campaigns") or []:
        label = campaign.get("label") or "?"
        model = out.setdefault(label, {s: {"episodes": 0, "full_passes": 0, "score_sum": 0.0,
                                           "failed": Counter()} for s in steps})
        for trial in campaign.get("trials") or []:
            if trial.get("exception"):
                continue
            rubric = trial.get("per_step_rubric") or {}
            for step in steps:
                row = rubric.get(step)
                if not isinstance(row, dict):
                    continue
                cell = model[step]
                cell["episodes"] += 1
                score = float(row.get("score") or 0.0)
                cell["score_sum"] += score
                cell["full_passes"] += int(score >= 1.0)
                for cid, value in (row.get("criteria") or {}).items():
                    if not value:
                        cell["failed"][cid] += 1
    final = {}
    for label, by_step in out.items():
        final[label] = {}
        for step, cell in by_step.items():
            n = cell["episodes"]
            final[label][step] = {
                "episodes": n, "full_passes": cell["full_passes"],
                "pass_at_1": round(cell["full_passes"] / n, 3) if n else None,
                "mean_score": round(cell["score_sum"] / n, 3) if n else None,
                "most_failed": [{"check": cid, "failed_in": k} for cid, k in cell["failed"].most_common(5)],
                "meets_step_bar": (cell["full_passes"] / n <= policy.STEP_PASS_RATE_MAX) if n else None}
    return final


def report(package: Path) -> dict:
    package = Path(package)
    steps = _steps(package)
    spec = _read_json(package / "authoring/spec.json") or {}
    spec_steps = {s.get("name"): s for s in spec.get("steps") or [] if isinstance(s, dict)}
    # The shipped checks carry runner, weight and step; the instruction sentence
    # each enforces and its basis live on the spec's checks (and tests/basis.toml).
    authored = {c.get("id"): c for c in spec.get("checks") or [] if isinstance(c, dict)}
    checks = [{**authored.get(c.get("id"), {}), **c} for c in
              (_read_json(package / "tests/checks.json") or {}).get("checks") or [] if isinstance(c, dict)]
    controls = (_read_json(package / "verification/controls.json") or {}).get("results") or {}
    turn_controls = controls.get("turns") or {}
    turns = []
    for position, step in enumerate(steps, 1):
        meta = spec_steps.get(step) or {}
        own = [c for c in checks if isinstance(c, dict) and c.get("step") == step]
        earlier = steps[:position - 1]
        turns.append({
            "turn": position, "step": step,
            "prompt": f"steps/{step}/instruction.md",
            "source_user_message": meta.get("trace_turn"),
            "new_information": meta.get("new_information"),
            "state_in": ({"agent": "the fixture: environment/repo as built into the agent image",
                          "reference": "environment/repo"} if position == 1 else
                         {"agent": f"the tree the agent left at the end of turn {position - 1}, in the same "
                                   "container (nothing is reset between turns; turn "
                                   f"{position - 1} was graded before this prompt was shown)",
                          "reference": "environment/repo + " + " + ".join(f"steps/{s}/solution/fix.patch"
                                                                         for s in earlier)}),
            "expected_outcome": {
                "reference_patch": f"steps/{step}/solution/fix.patch",
                "objectives": [{"id": c.get("id"), "weight": c.get("weight"), "band": c.get("band"),
                                "rule": c.get("rule_quote"), "basis": c.get("basis")}
                               for c in own if c.get("kind") == "objective"],
                "guardrails": [{"id": c.get("id"), "rule": c.get("rule_quote"), "basis": c.get("basis")}
                               for c in own if c.get("kind") == "guardrail"],
                "graded_before_next_turn": position < len(steps)},
            "dependence_evidence": {k: turn_controls.get(f"{k}:{step}") for k in ("prefix_only", "skip_prior")
                                    if position > 1},
        })
    rollouts = _rollouts(package, steps)
    return {"schema": SCHEMA, "multi_turn": len(steps) >= 2, "steps": len(steps), "turns": turns,
            "rollouts": rollouts,
            "rollouts_note": None if rollouts else "no calibration runs in this package (runs/index.json)"}


def markdown(doc: dict) -> str:
    lines = ["# Turns", "",
             f"This task has {doc['steps']} sequential user turn(s) in one environment. Each turn is graded on its "
             "own rubric before the next prompt is shown; the agent continues in the same container from whatever "
             "it left (a fresh agent session per turn, so each prompt stands on its own). Generated from the "
             "package by `pipeline/turns.py`; nothing here is typed by hand.", ""]
    for turn in doc["turns"]:
        lines += [f"## Turn {turn['turn']} — `{turn['step']}`", "",
                  f"- **User prompt:** `{turn['prompt']}`"
                  + (f" (the user's message {turn['source_user_message']} in the source session)"
                     if turn.get("source_user_message") is not None else ""),
                  f"- **State in (agent):** {turn['state_in']['agent']}",
                  f"- **State in (reference):** `{turn['state_in']['reference']}`"]
        if turn.get("new_information"):
            lines.append(f"- **New information this turn:** {turn['new_information']}")
        outcome = turn["expected_outcome"]
        lines += [f"- **Expected outcome:** the reference `{outcome['reference_patch']}` passes every check below.", ""]
        if outcome["objectives"]:
            lines += ["| objective | weight | band | rule (instruction sentence) |", "|---|---:|---|---|"]
            for o in outcome["objectives"]:
                rule = " / ".join(o.get("rule") or []).replace("|", "\\|").replace("\n", " ")
                lines.append(f"| `{o['id']}` | {o.get('weight')} | {o.get('band')} | {rule[:300]} |")
            lines.append("")
        if outcome["guardrails"]:
            lines.append("Guardrails (any failure scores the turn 0): "
                         + ", ".join(f"`{g['id']}`" for g in outcome["guardrails"]))
            lines.append("")
        evidence = {k: v for k, v in (turn.get("dependence_evidence") or {}).items() if v}
        if evidence:
            prefix, skip = evidence.get("prefix_only") or {}, evidence.get("skip_prior") or {}
            lines.append("Executed dependence controls: earlier turns' references with no new work pass "
                         f"{len(prefix.get('passing') or [])} objective(s) of this turn; this turn's reference "
                         f"without the earlier turns {'applied' if skip.get('applied') else 'did not apply'} and "
                         f"scored {skip.get('step_reward')} on this turn.")
            lines.append("")
    lines += ["## Rollouts per turn", ""]
    if not doc["rollouts"]:
        lines.append(doc.get("rollouts_note") or "No calibration runs.")
    else:
        lines += ["| model | turn | episodes | full passes | pass@1 | mean score | checks failed most |",
                  "|---|---|---:|---:|---:|---:|---|"]
        for model, by_step in doc["rollouts"].items():
            for step, cell in by_step.items():
                failed = ", ".join(f"`{m['check']}` ({m['failed_in']})" for m in cell["most_failed"][:3])
                lines.append(f"| {model} | `{step}` | {cell['episodes']} | {cell['full_passes']} | "
                             f"{cell['pass_at_1']} | {cell['mean_score']} | {failed} |")
    return "\n".join(lines) + "\n"


def write(package: Path) -> dict:
    package = Path(package)
    doc = report(package)
    (package / "verification").mkdir(exist_ok=True)
    (package / RECEIPT).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (package / MARKDOWN).write_text(markdown(doc), encoding="utf-8")
    return doc


def problems(package: Path) -> list[str]:
    """What keeps a multi-turn package from meeting Appendix A's submission list."""
    package = Path(package)
    steps = _steps(package)
    if len(steps) < 2:
        return []
    issues = []
    if len(steps) < policy.MULTI_STEP_MIN_STEPS:
        issues.append(f"multi-turn package has {len(steps)} turns; at least {policy.MULTI_STEP_MIN_STEPS} required")
    if not (package / MARKDOWN).is_file() or not (package / RECEIPT).is_file():
        issues.append(f"multi-turn package lacks {MARKDOWN} / {RECEIPT} (Appendix A submission contents)")
        return issues
    doc = report(package)
    if json.loads((package / RECEIPT).read_text(encoding="utf-8")) != json.loads(json.dumps(doc)):
        issues.append(f"{RECEIPT} is stale: regenerate it from the package (python -m pipeline.turns <package>)")
    for turn in doc["turns"]:
        if turn.get("source_user_message") is None:
            issues.append(f"turn {turn['turn']} cites no source user message (trace_turn)")
        if not turn["expected_outcome"]["objectives"]:
            issues.append(f"turn {turn['turn']} has no objective of its own")
    if not doc["rollouts"]:
        issues.append("no per-turn rollout results (runs/index.json)")
    return issues


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", type=Path)
    ap.add_argument("--check", action="store_true", help="report problems without writing")
    args = ap.parse_args(argv)
    if args.check:
        found = problems(args.package)
        print(json.dumps({"problems": found}, indent=2))
        return 1 if found else 0
    doc = write(args.package)
    print(json.dumps({"turns": doc["steps"], "rollout_models": sorted(doc["rollouts"])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
