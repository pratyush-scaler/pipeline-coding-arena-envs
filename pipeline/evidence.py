"""Validate measurement identity and recompute aggregates from retained attempts."""
from __future__ import annotations

import hashlib
import math
import tomllib
from collections import Counter
from pathlib import Path
from .artifacts import identity, harness_identity
from .author.contracts import contained, strict_json


def calibration_problems(bundle: Path, report: dict, minimum: int = 1,
                         replay: bool = False) -> list[str]:
    errors = []
    if report.get("schema") != "arena-pipeline/calibration/2":
        return ["legacy or unknown calibration schema cannot certify a release"]
    if report.get("bundle") != bundle.name or report.get("grader_fingerprint") != identity(bundle):
        errors.append("calibration belongs to another bundle or a stale task version")
    if report.get("grader_stable") is not True:
        errors.append("calibration did not measure one stable task version")
    if report.get("harness_sha256") != harness_identity() or report.get("harness_stable") is not True:
        errors.append("calibration transport is stale or changed during execution")
    names = [s["name"] for s in tomllib.loads((bundle / "task.toml").read_text())["steps"]]
    if report.get("steps") != names or report.get("complete_episode") is not True:
        errors.append("calibration must cover every declared step as complete episodes")
    if not report.get("model") or not report.get("effort") or report.get("protocol") != "file-workspace/2":
        errors.append("calibration model, effort or protocol is missing")
    if any(type(report.get(k)) is not int or report[k] < 1 for k in ("max_steps", "max_tokens")):
        errors.append("invalid calibration budgets")
    snapshot = contained(bundle, report.get("snapshot", ""))
    if identity(snapshot) != identity(bundle):
        errors.append("retained calibration snapshot differs from the task")
    rubric = strict_json((bundle / "tests/rubric.json").read_text())
    definitions = {c["id"]: c for c in strict_json((bundle / "tests/checks.json").read_text())["checks"]}
    episodes, ids = {}, set()
    rows = report.get("rollouts") or []
    for row in rows:
        rid, step = row.get("id"), row.get("step")
        if not isinstance(rid, str) or rid in ids:
            errors.append("missing or duplicate rollout ID")
        ids.add(rid)
        episodes.setdefault(row.get("episode"), []).append(row)
        reward = row.get("reward")
        work = contained(bundle, row.get("work_dir", ""))
        if row.get("zero_class") == "void" or row.get("error"):
            # A void measured nothing and leaves every denominator. It is not
            # release evidence, but it is not a reason to discard the valid
            # episodes beside it either; only its retained files are checked.
            for rel, sha in (row.get("artifacts") or {}).items():
                path = contained(work, rel)
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    errors.append(f"{rid}: missing or changed artifact {rel}")
            continue
        if (type(reward) not in (int, float) or not math.isfinite(reward) or not 0 <= reward <= 1
                or reward == 0 and row.get("zero_class") != "model_failure"):
            errors.append(f"{rid}: invalid or unclassified reward")
        if row.get("stop_reason") not in ("end_turn", "completed", "stop_sequence") or type(row.get("tool_calls")) is not int or row["tool_calls"] < 1:
            errors.append(f"{rid}: incomplete or stalled attempt")
        states = row.get("checks") or {}
        if set(states) != set(rubric.get(step, [])) or any(v not in {"passed", "failed", "gated_out"} for v in states.values()):
            errors.append(f"{rid}: criterion results do not match the declared step")
        artifacts = row.get("artifacts") or {}
        if not {"grade.json", "conversation.json", "request-config.json"} <= set(artifacts):
            errors.append(f"{rid}: missing retained grading, transcript or budget evidence")
        for rel, sha in artifacts.items():
            path = contained(work, rel)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                errors.append(f"{rid}: missing or changed artifact {rel}")
        if (work / "request-config.json").is_file():
            config = strict_json((work / "request-config.json").read_text())
            if any(config.get(k) != report.get(k) for k in ("model", "effort", "max_steps", "max_tokens")):
                errors.append(f"{rid}: actual execution budget differs from report")
        if (work / "grade.json").is_file():
            grade = strict_json((work / "grade.json").read_text())
            actual_states = {cid: "gated_out" if r.get("gated_out") else "passed" if r.get("passed") else "failed"
                             for cid, r in grade.get("checks", {}).items()}
            if grade.get("reward") != reward or actual_states != states:
                errors.append(f"{rid}: report disagrees with retained grade")
            if replay:
                from .evaluate import exported_grade
                if exported_grade(snapshot, work / "deliverables", step, work / "workspace") != grade:
                    errors.append(f"{rid}: replay differs from retained grade")
    if any(type(e) is not int or e < 1 for e in episodes):
        errors.append("invalid episode identifiers")

    def void(row):
        return row.get("zero_class") == "void" or bool(row.get("error"))
    complete = []
    for episode, rows_in_episode in episodes.items():
        steps = [r.get("step") for r in rows_in_episode]
        if not any(void(r) for r in rows_in_episode):
            if steps != names:
                errors.append(f"episode {episode}: missing, repeated or reordered steps")
            else:
                complete.append(rows_in_episode)
        # The runner stops an episode at its first void step, so a void
        # episode is a prefix of the declared steps ending in that void.
        elif (steps != names[:len(steps)] or not void(rows_in_episode[-1])
              or any(void(r) for r in rows_in_episode[:-1])):
            errors.append(f"episode {episode}: missing, repeated or reordered steps")
    if len(complete) < minimum:
        errors.append(f"need at least {minimum} valid complete episodes, found {len(complete)}")
    summary = report.get("summary") or {}
    if (summary.get("requested") != len(episodes) or summary.get("valid") != len(complete)
            or summary.get("void") != len(episodes) - len(complete)):
        errors.append(f"episode counts disagree with the retained attempts "
                      f"({len(complete)} valid, {len(episodes) - len(complete)} void)")
    for step in names:
        # Only complete episodes contribute, as the runner aggregates them.
        step_rows = [r for episode in complete for r in episode if r.get("step") == step]
        actual = report.get("by_step", {}).get(step, {})
        if set(actual.get("checks", {})) != set(rubric[step]) or actual.get("valid") != len(step_rows):
            errors.append(f"{step}: aggregate coverage or count is incorrect")
        for cid in rubric[step]:
            counts = Counter(r.get("checks", {}).get(cid) for r in step_rows)
            stored = actual.get("checks", {}).get(cid, {})
            if any(stored.get(k) != counts[k] for k in ("passed", "failed", "gated_out")):
                errors.append(f"{step}/{cid}: criterion counts disagree with attempts")
            check = definitions[cid]
            if stored.get("kind") != check["kind"] or stored.get("weight") != check["weight"]:
                errors.append(f"{step}/{cid}: criterion metadata differs from the task")
    return errors


#: What `benchmark.harbor.run_campaign` records when any trial is void.
VOID_TRIALS_ERROR = "One or more trials were incomplete or invalid"


def _isolation_ok(proof: dict, agent: str) -> bool:
    """One container's isolation receipt, against the protocol for `agent`.

    Offline agents: no network at all. A harness campaign: every container is
    unprivileged, publishes no port and has no NET_ADMIN/NET_RAW; a container
    reaching any network does so only through Harbor's egress control, and its
    agent-phase allowlist is within that harness's own model API host(s).
    Verifier containers carry no allowlist.
    """
    from .benchmark.harbor import HARNESS_AGENTS
    if proof.get("privileged") is not False or proof.get("port_bindings"):
        return False
    if agent not in HARNESS_AGENTS:
        return proof.get("network_mode") == "none" and proof.get("networks") == ["none"]
    from .benchmark.harness_docker import harness_hosts
    if proof.get("schema") != "arena-pipeline/harness-network/1":
        return False
    if not {"NET_ADMIN", "NET_RAW"} <= {str(c).upper().removeprefix("CAP_")
                                        for c in proof.get("cap_drop") or []}:
        return False
    if proof.get("baseline") != "no-network":
        return False
    allowlist = proof.get("agent_phase_allowlist") or []
    if proof.get("egress_control") is not True:
        return proof.get("network_mode") == "none" and not allowlist
    return set(allowlist) <= set(harness_hosts(agent))


def never_started(trial_dir: Path) -> bool:
    """A trial whose environment never came up: the harness raised and no agent ran.

    2026-10-04: Docker ran out of network address pools on a busy workspace
    ("all predefined address pools have been fully subnetted"), so compose
    failed before any agent started. Such a trial has nothing to isolate and is
    a void like any other; requiring its isolation proof turned four voids into
    an integrity failure that discarded a whole revision's screen.
    """
    import json
    trial_dir = Path(trial_dir)
    try:
        result = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return False
    if not (result.get("exception_info") or {}).get("exception_type"):
        return False
    agent_files = [p for p in trial_dir.glob("steps/*/agent/**/*") if p.is_file()]
    agent_files += [p for p in trial_dir.glob("agent/**/*") if p.is_file()]
    return not agent_files


def native_evidence(bundle: Path, output: Path, agent: str, minimum: int = 1) -> tuple[list[str], dict]:
    """Re-read native trial files and isolation receipts; never trust aggregates alone."""
    from .benchmark.harbor import (parse_trial, summarize, EXPECTED_VERSIONS, PROTOCOL,
                                  _history_adapter, job_config, evidence_record,
                                  HARNESS_AGENTS, SOLVER_AGENTS, provider_for)
    from . import policy
    errors = []
    report = strict_json((output / "report.json").read_text())
    if agent == "solver":
        # A calibration campaign: the solver is the harness assigned to ITS
        # model (Fable 5.1 -> Claude Code, GPT 6 Astra -> Codex). A campaign
        # run in any other harness is not this protocol's measurement.
        try:
            agent = policy.harness_for(report.get("requested_model"))
        except ValueError as exc:
            return [f"native calibration campaign: {exc}"], {"trials": []}
    validation = report.get("task_validation", {})
    if (validation.get("mode") != "current-draft"
            or validation.get("current_generator_verified") is not True
            or validation.get("artifact_sha256") != identity(bundle)):
        errors.append("native task was not verified against the current generator; legacy diagnostics cannot certify release")
    # Symlinks are recorded, not refused: one planted by an agent inside its own
    # trial is that trial's reward-hacking finding (`attribution.
    # trajectory_hacks`), not a reason to lose every other trial's measurement.
    # One anywhere else in the evidence is ours, and blocks here.
    files, links = evidence_record(output)
    if (not files or report.get("evidence_files") != files
            or (report.get("evidence_symlinks") or {}) != links):
        errors.append("native execution evidence is missing, changed or has been deleted")
    stray = sorted(rel for rel in links if len(Path(rel).parts) < 4
                   or Path(rel).parts[:2] != ("jobs", "run"))
    if stray:
        errors.append("native evidence contains symlinks outside any trial: "
                      + ", ".join(stray[:5]))
    runner = Path(__file__).parent / "benchmark/harbor.py"
    if report.get("runner_sha256") != hashlib.sha256(runner.read_bytes()).hexdigest():
        errors.append("native runner changed; remeasure with the current protocol")
    names = [s["name"] for s in tomllib.loads((bundle / "task.toml").read_text())["steps"]]
    # The runner marks a campaign not-ok when any trial is void. That is lost
    # measurement, re-derived from the raw trials below, not a broken report.
    other_errors = [e for e in report.get("errors") or [] if e != VOID_TRIALS_ERROR]
    voided = (report.get("errors") or []) != other_errors
    if (report.get("protocol") != PROTOCOL or report.get("agent") != agent
            or (report.get("ok") is not True and not voided) or other_errors
            or report.get("dry_run")
            or report.get("status") != "finished" or report.get("step_names") != names):
        errors.append("native report is incomplete, invalid or uses another protocol")
    if identity(output / "package") != identity(bundle):
        errors.append("native snapshot differs from the task")
    if report.get("runtime", {}).get("versions") != EXPECTED_VERSIONS:
        errors.append("native runtime versions differ from the pinned protocol")
    adapter = report.get("step_history_adapter", {})
    current = _history_adapter()
    if {Path(f["path"]).name: f["sha256"] for f in adapter.get("files", [])} != {
            Path(f["path"]).name: f["sha256"] for f in current["files"]}:
        errors.append("native isolation/history controller changed")
    config = strict_json((output / "harbor-job.json").read_text())
    limits = report.get("limits", {})
    no_limits = limits.get("no_limits", False)
    if type(no_limits) is not bool:
        errors.append("native no_limits setting must be a boolean")
        no_limits = False
    expected_config = job_config(output / "package", output, model=report.get("requested_model"),
        agent=agent, rollouts=report.get("summary", {}).get("requested"),
        workers=config.get("n_concurrent_trials"), effort=report.get("effort"),
        no_limits=no_limits, max_output_tokens=limits.get("max_output_tokens"),
        context_limit=limits.get("context_limit"))
    if config != expected_config:
        errors.append("native job differs from the approved configuration and budgets")
    if config.get("environment", {}).get("import_path") != provider_for(agent):
        errors.append("native execution did not use the audited provider for this agent")
    declared_agent = (config.get("agents") or [{}])[0]
    if agent in SOLVER_AGENTS and declared_agent.get("model_name") != report.get("resolved_model"):
        errors.append("native report model/agent differs from the executed configuration")
    if agent in HARNESS_AGENTS and report.get("harness") != {
            "agent": agent, "version": policy.HARNESS_VERSIONS[agent]}:
        errors.append("native harness or its pinned version differs from the protocol")
    trials = []
    for path in sorted((output / "jobs/run").glob("*/result.json")):
        trial = parse_trial(path, names, oracle=agent == "oracle", strategy="mean",
                            solver=agent in SOLVER_AGENTS, harness=agent in HARNESS_AGENTS,
                            max_turns=None if no_limits else 40)
        proofs = [strict_json(p.read_text()) for p in (path.parent / "isolation").glob("*.json")]
        if (len(proofs) < len(names) + 1 or not all(_isolation_ok(p, agent) for p in proofs)) \
                and not (not trial["valid"] and not proofs and never_started(path.parent)):
            errors.append(f"{trial['id']}: incomplete native network-isolation proof")
        # An invalid trial is a void: it leaves every denominator and reaches
        # attribution as a void row through `rollouts()`, rather than
        # discarding the valid trials beside it. Its isolation proof above is
        # still required -- that is integrity, not measurement.
        if trial["valid"] and agent == "terminus-2":   # harness validity is parse_trial's transcript check
            for step in trial["steps"]:
                metadata = (step.get("agent_result") or {}).get("metadata") or {}
                turns = metadata.get("n_episodes")
                if (type(turns) is not int or turns <= 0 or (not no_limits and turns >= 40)
                        or not metadata.get("all_messages")):
                    errors.append(f"{trial['id']}/{step['name']}: stalled, capped or missing agent trace")
        trials.append(trial)
    requested = report.get("summary", {}).get("requested")
    valid = sum(1 for t in trials if t["valid"])
    if type(requested) is not int or len(trials) != requested:
        errors.append("native trial count differs from the requested rollouts")
    elif voided != (valid < len(trials)):
        errors.append("native report's void record disagrees with the raw trials")
    elif valid < minimum:
        errors.append(f"need >= {minimum} valid native trials after voids are "
                      f"excluded, found {valid} of {requested}")
    elif summarize(trials, requested) != report.get("summary"):
        errors.append("native aggregate differs from the raw trials")
    rubric = strict_json((bundle / "tests/rubric.json").read_text())
    definitions = {c["id"]: c for c in strict_json((bundle / "tests/checks.json").read_text())["checks"]}
    measured = {"by_step": {}, "model": report.get("resolved_model"), "trials": trials,
                "effort": report.get("effort"), "protocol": report.get("protocol"), "limits": limits}
    for name in names:
        stats = {cid: {"passed": 0, "failed": 0, "gated_out": 0} for cid in rubric[name]}
        for trial in trials:
            # A partial/void episode cannot contribute capability failures.
            if not trial.get("valid"):
                continue
            step = next((s for s in trial["steps"] if s["name"] == name), {})
            actual = step.get("checks", {})
            if set(actual) != {"reward/" + cid for cid in stats}:
                errors.append(f"{trial['id']}/{name}: native check inventory mismatch")
            for cid in stats:
                row = actual.get("reward/" + cid, {})
                value = row.get("value")
                if type(row.get("gated_out", False)) is not bool or type(value) not in (int, float) or value not in (0, 1):
                    errors.append(f"{trial['id']}/{cid}: invalid native check state")
                    continue
                state = "gated_out" if row.get("gated_out") else "passed" if value == 1 else "failed"
                stats[cid][state] += 1
        for cid in stats:
            stats[cid].update(kind=definitions[cid]["kind"], weight=definitions[cid]["weight"])
        measured["by_step"][name] = {"checks": stats}
    measured["step_names"] = list(names)
    measured["rollouts"] = rollouts(trials, names)
    return errors, measured


def rollouts(trials: list[dict], names: list[str]) -> list[dict]:
    """Per-step rows in the shape `metrics` and `attribution` read.

    Without these every native campaign measured 0 valid episodes: pass@1 was
    None, attribution counted nothing, and release blamed voids. Validity is the
    trial's own, established by `parse_trial` above; a step that failed it is a
    void, never a zero.
    """
    rows = []
    for episode, trial in enumerate(trials, 1):
        steps = {s.get("name"): s for s in trial.get("steps", [])}
        for name in names:
            step = steps.get(name)
            if step is None:
                continue                        # an incomplete episode stays incomplete
            ok = bool(trial.get("valid")) and bool(step.get("valid"))
            turns = ((step.get("agent_result") or {}).get("metadata") or {}).get("n_episodes")
            if type(turns) is not int:
                turns = step.get("agent_turns")     # harness: ATIF transcript turns
            problems = [*trial.get("errors", []), *step.get("errors", [])]
            rows.append({
                "episode": episode, "step": name, "id": f"{trial.get('id')}/{name}",
                "reward": step.get("reward"),
                "zero_class": None if ok else "void",
                "error": None if ok else (str(problems[0]) if problems else "invalid native trial"),
                "stop_reason": "completed" if ok else "incomplete",
                "tool_calls": turns if type(turns) is int else 0,
                "checks": {cid.removeprefix("reward/"): ("gated_out" if row.get("gated_out") else
                                                        "passed" if row.get("value") == 1 else "failed")
                           for cid, row in (step.get("checks") or {}).items()},
            })
            rows[-1].update(_verifier_detail(step, rows[-1]["checks"]))
    return rows


def _verifier_detail(step: dict, states: dict) -> dict:
    """What the step's own grader recorded beyond the per-check values.

    Read from the verifier's retained `arena-grade.json` (hashed into the
    campaign's evidence inventory), so neither field is our inference:

      contract_errors  the grader's own verdict that the deliverable is missing,
                       unparseable, undeclared or off the published schema
                       (`tests/outputs.json`). `attribution` reads it as the
                       contract verdict; the grader zeroes the reward on it.
      misses           per failed check, how close the submitted value came to
                       the recomputed target (`equal_normalised` or
                       `overlap_<share>`), which the all-fail near-miss
                       obligation reads. Before this, native rows never carried
                       it, so the obligation could not fire.

      grader_refusals  failed checks the grader refused before any test ran
                       (no exit code, a `grader:` message): the suite never
                       executed over the submission, so the failure measured
                       nothing about the model. Measured on c-004 (2026-10-01):
                       seven Fable and one Astra zero were scanner refusals of
                       the fixture's own `import ctypes` and of a local test
                       helper, and were reported as capability failures.

    A missing or unreadable grade leaves `contract_errors` None -- unknown, not
    empty -- and records no misses.
    """
    import json
    folder = step.get("evidence_dir")
    path = Path(folder) / "verifier/arena-grade.json" if folder else None
    try:
        grade = json.loads(path.read_text()) if path and path.is_file() and not path.is_symlink() else None
    except (OSError, ValueError):
        grade = None
    if not isinstance(grade, dict):
        return {"contract_errors": None, "misses": {}, "failed_output": {}, "grader_refusals": []}
    errors = grade.get("contract_errors")
    failed_output, refusals = {}, []
    for cid, record in (grade.get("checks") or {}).items():
        if states.get(cid) != "failed" or not isinstance(record, dict):
            continue
        tail = (record.get("stderr") or record.get("stdout") or record.get("reason") or "")
        failed_output[cid] = str(tail)[-600:]
        if grader_refusal(record):
            refusals.append(f"{cid}: {str(record.get('error') or record.get('reason'))[:200]}")
    return {"contract_errors": [str(e) for e in errors] if isinstance(errors, list) else None,
            "misses": {}, "failed_output": failed_output, "grader_refusals": refusals}


def grader_refusal(record: dict) -> bool:
    """A check the grader refused before running anything: no exit code and a
    `grader:` message. Not a test that ran and failed -- it measured nothing."""
    if record.get("exit_code") is not None:
        return False
    message = str(record.get("error") or record.get("reason") or "")
    return message.startswith("grader:")
