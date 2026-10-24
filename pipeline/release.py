"""The only acceptance decision. Missing evidence is a blocker, never a pass."""
from __future__ import annotations
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from .artifacts import atomic_json, identity, policy_identity, bundle_lock
from .author.contracts import contained, strict_json


def _provenance_problems(bundle: Path) -> list[str]:
    from .ledger.faithfulness import build, citation_problems, measured_fields
    from .dump.recover import load_trace
    stored = strict_json((bundle / "provenance/ledger.json").read_text())
    source = stored["source"]
    spec = strict_json((bundle / "authoring/spec.json").read_text())
    if any(spec.get("source", {}).get(key) != source.get(key) for key in ("session_id", "seed_index")):
        return ["authoring source and provenance ledger identify different source seeds"]
    actual = build(bundle, source["session_id"], source.get("seed_index"))

    # Files extraction excluded are declared from authoring/extraction/extraction.json,
    # which is not part of the artifact: a frozen copy (the semantic review's snapshot)
    # cannot recompute them, and comparing them refused every review of a task with
    # an excluded binary (2026-10-07, 8 tasks). They earn no fidelity credit; every
    # other measured field, host-path declarations included, is still recomputed.
    def comparable(ledger: dict, key: str):
        value = ledger.get(key)
        if key == "recovery_omissions" and isinstance(value, list):
            return [o for o in value if not (isinstance(o, dict)
                                             and str(o.get("reason", "")).startswith("excluded_at_extraction:"))]
        return value
    issues = [] if all(comparable(stored, k) == comparable(actual, k) for k in measured_fields(stored)) \
        else ["source trace, citations or measured fidelity differ from the retained ledger"]
    return issues + citation_problems(spec, load_trace(source["session_id"]), source["session_id"])


# ---------------------------------------------------------------------------
# Top-up merge
# ---------------------------------------------------------------------------
# A native campaign is one Harbor job directory, and Harbor cannot append trials
# to a finished job. So a top-up is a *separate* campaign of exactly as many
# episodes as the original lost to voids, and the measurement is the union of
# the valid trials of the original and its top-ups. Every scored episode of the
# original is retained; nothing is re-run.
#
# `native-calibrations.json` keeps `campaigns` as one primary path per model,
# which every other reader already understands, and records the void
# replacements under `topups: {primary: [extra, ...]}`. A reader that does not
# know about `topups` sees the primary campaign with its voids and rejects it --
# fail closed, never a silent acceptance.

#: The report error `run_campaign` adds when a trial was void. It is the only
#: report-level error a void may explain away.
_VOID_REPORT_ERROR = "One or more trials were incomplete or invalid"
_VOID_REPORT_ISSUE = "native report is incomplete, invalid or uses another protocol"


def _void_tolerant(bundle: Path, path) -> tuple[list[str], dict, int]:
    """One campaign's evidence with its void trials removed.

    Returns (issues, measured, voids). Only issues *caused by* a void trial are
    dropped: those naming a void trial's id, and the report-level "incomplete"
    issue when the report's own errors are exactly the void error and the rest
    of what that issue covers (protocol, agent, status, steps) is re-verified
    here. Anything else about the campaign still blocks.
    """
    import copy
    from .evidence import native_evidence, rollouts
    from .benchmark.harbor import PROTOCOL
    path = Path(path)
    # minimum=0: a part may be all void; the floor applies to the merge.
    issues, measured = native_evidence(bundle, path, "solver", 0)
    trials = measured.get("trials") or []
    void_ids = {t.get("id") for t in trials if t.get("valid") is False}
    if not void_ids:
        return issues, measured, 0
    measured = copy.deepcopy(measured)
    report = strict_json((path / "report.json").read_text())
    names = [s["name"] for s in tomllib.loads((bundle / "task.toml").read_text())["steps"]]
    benign = (set(report.get("errors") or []) <= {_VOID_REPORT_ERROR}
              and report.get("status") == "finished" and not report.get("dry_run")
              and report.get("protocol") == PROTOCOL and report.get("agent") == _harness_or_none(report)
              and report.get("step_names") == names)

    # A void trial's isolation proof is still integrity, never dropped here.
    def caused_by_void(issue: str) -> bool:
        if benign and issue == _VOID_REPORT_ISSUE:
            return True
        head, _, rest = issue.partition(": ")
        return (head.split("/", 1)[0] in void_ids
                and rest in ("incomplete/void native trial",
                             "stalled, capped or missing agent trace"))

    kept = [t for t in trials if t.get("valid") is not False]
    measured["trials"] = kept
    measured["rollouts"] = rollouts(kept, measured.get("step_names") or names)
    return [i for i in issues if not caused_by_void(i)], measured, len(void_ids)


def _merged_measurement(bundle: Path, parts: list, minimum: int) -> tuple[list[str], dict]:
    """The valid trials of a campaign and its void replacements, as one report."""
    issues, merged, part_voids = [], None, []
    for path in parts:
        prefix = f"{Path(path).name}: " if len(parts) > 1 else ""
        try:
            found, measured, voids = _void_tolerant(bundle, path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # A campaign the index names but we cannot read is missing
            # evidence, reported as such -- never a crash, never a pass.
            issues.append(f"{prefix or Path(path).name + ': '}campaign unreadable "
                          f"({type(exc).__name__}: {exc})")
            part_voids.append(0)
            continue
        part_voids.append(voids)
        issues.extend(prefix + i for i in found)
        if merged is None:
            import copy
            merged = copy.deepcopy(measured) if len(parts) > 1 else measured
            continue
        if any(measured.get(k) != merged.get(k)
               for k in ("model", "effort", "protocol", "limits", "step_names")):
            issues.append(f"{prefix}top-up campaign differs from the original in "
                          "model, effort, protocol, limits or steps")
            continue
        merged["trials"] = merged.get("trials", []) + measured.get("trials", [])
        for step, row in measured.get("by_step", {}).items():
            for cid, stats in row.get("checks", {}).items():
                target = merged["by_step"][step]["checks"][cid]
                for state in ("passed", "failed", "gated_out"):
                    target[state] += stats[state]
    merged = {"trials": [], "by_step": {}} if merged is None else merged
    if len(parts) > 1:
        from .evidence import rollouts
        merged["rollouts"] = rollouts(merged["trials"], merged.get("step_names") or [])
    merged["campaign_parts"] = [str(p) for p in parts]
    merged["part_voids"] = part_voids
    merged["voids_excluded"] = sum(part_voids)
    if "trials" in merged:
        valid = len(merged["trials"])
    else:
        episodes = {}
        for row in merged.get("rollouts") or []:
            episodes.setdefault(row.get("episode"), []).append(row.get("zero_class") != "void")
        valid = sum(all(rows) for rows in episodes.values())
    if valid < minimum:
        issues.append(f"need >= {minimum} valid native episodes; {valid} valid after "
                      f"{sum(part_voids)} void episode(s) were excluded")
    return issues, merged


def calibration_measurement(bundle: Path, index: dict, primary, minimum: int) -> tuple[list[str], dict]:
    """The measurement for one index entry, top-ups included.

    Other readers of `native-calibrations.json` (deliverable, harden, phases,
    category_batch) should call this instead of `native_evidence` on the path.
    """
    extra = (index.get("topups") or {}).get(str(primary), [])
    if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
        return ["native calibration top-up record is malformed"], {}
    return _merged_measurement(bundle, [str(primary), *extra], minimum)



def topup_budget() -> int:
    """Void top-up episodes allowed per model campaign: policy.MAX_TOPUP_ATTEMPTS, or a larger
    budget the operator set for ONE run with `ARENA_TOPUP_BUDGET` (user decision 2026-10-08, c-019
    only: grader-scan refusals that re-grade as failures are counted void until the runtime fix)."""
    import os
    from . import policy
    raw = os.environ.get("ARENA_TOPUP_BUDGET", "")
    return max(policy.MAX_TOPUP_ATTEMPTS, int(raw)) if raw.isdigit() else policy.MAX_TOPUP_ATTEMPTS

def topup_spent(parts: list[str]) -> int:
    """Episodes the top-up campaigns in `parts[1:]` requested, from their own reports.

    A report that cannot be read counts as the whole budget: an unknown spend
    must never hand back episodes that may already have been run.
    """
    from . import policy
    total = 0
    for part in parts[1:]:
        try:
            requested = json.loads((Path(part) / "report.json").read_text())["summary"]["requested"]
            if type(requested) is not int or requested < 0:
                raise ValueError(requested)
        except (OSError, ValueError, KeyError, TypeError):
            return topup_budget()
        total += requested
    return total


def _harness_or_none(report: dict) -> str | None:
    """The harness a calibration report's model is assigned to, or None."""
    from . import policy
    try:
        return policy.harness_for(report.get("requested_model"))
    except ValueError:
        return None


def _scored(measured: dict, counts: dict) -> int:
    """Episodes that produced a result: natively valid AND not voided by attribution.
    A grader refusal of the submitted tree (agent code reaching into the test process)
    is void too -- not a failure -- and, like a lost container, is replaced by a top-up
    (c-028 / c-005-webgl / c-014 / c-016-html, 2026-10-04: native-valid counts left them
    one episode short forever)."""
    native = len(measured.get("trials", []))
    if not any(counts.get(k) for k in ("pass", "capability", "contract", "void")):
        return native                     # attribution classified nothing: the native count stands
    return min(native, sum(counts.get(k, 0) for k in ("pass", "capability", "contract")))


def _calibrate_model(bundle: Path, snapshot: Path, native_root: Path, model: str,
                     n: int, workers: int, receipt: str = "topup",
                     existing: list[str] | None = None) -> tuple[str, list[str], bool]:
    """Measure one model, replacing void episodes only.

    Returns (primary campaign, top-up campaigns, attributable). The first
    campaign requests `n`. Each top-up requests exactly the episodes still
    missing -- the voids -- bounded by `policy.MAX_TOPUP_ATTEMPTS` extra
    episodes in total, and is retried only while voids are the sole thing
    wrong. A low score is a result; nothing scored is ever re-run.

    `receipt` prefixes the top-up receipt's file name, so the five-episode
    screen (`screen.py`, "screen-topup") never overwrites the receipt of the
    ten-episode calibration of record ("topup").

    `existing` continues a retained campaign (primary first, then its top-ups)
    that verified but is still short of valid episodes: every scored episode
    stands, and only the missing ones are requested, within what is left of
    the same budget (`topup_spent`). Nothing is launched when the budget is
    spent or the retained evidence has an integrity problem.
    """
    import uuid
    from .benchmark.harbor import resolve_model
    from .benchmark.coder import native_runner
    from . import attribution, policy
    # Calibration runs only on the Coder pool; the pool bounds concurrency.
    run_campaign, NATIVE_CAPACITY = native_runner("calibration")
    parts, attempts, spent, requested, before = [], [], 0, n, None
    issues, measured = [], {}
    if existing:
        parts = [str(p) for p in existing]
        spent = topup_spent(parts)
        issues, measured = _merged_measurement(bundle, parts, n)
        counts = dict(attribution.attribute(bundle, measured)["counts"])
        valid = _scored(measured, counts)
        counts["void"] = measured.get("voids_excluded", 0)
        integrity = [i for i in issues if not i.split(": ", 1)[-1].startswith("need >= ")]
        attempts.append({"campaign": "retained", "parts": parts, "void": counts["void"],
                         "valid_total": valid, "counts": counts, "evidence_problems": issues})
        needed, remaining = n - valid, topup_budget() - spent
        if needed > 0 and not integrity and remaining > 0:
            before, requested = counts, min(needed, remaining)
        else:
            requested = 0
    while requested > 0:
        output = native_root / ("calibration-" + uuid.uuid4().hex)
        from .benchmark.capacity import codex_slots, trial_slots
        with NATIVE_CAPACITY.reserve(min(requested, workers)) as reserved, \
                codex_slots(model, reserved) as codex, trial_slots(codex) as slots:
            # No turn cap and no campaign deadline: a run cut short writes
            # no deliverable and is recorded VOID, which removes it from
            # every denominator rather than reporting it as a failure.
            # Capping is how a measurement becomes a record of our limits.
            report = run_campaign(snapshot, output, model=model,
                                  agent=policy.harness_for(model), rollouts=requested,
                                  workers=slots,
                                  effort=policy.SPEC_CALIBRATION_EFFORT,
                                  no_limits=policy.SPEC_NO_LIMITS)
        parts.append(str(output))
        if len(parts) > 1:
            spent += requested
        issues, measured = _merged_measurement(bundle, parts, n)
        counts = dict(attribution.attribute(bundle, measured)["counts"])
        valid = _scored(measured, counts)
        voids = measured["part_voids"][-1]
        counts["void"] = voids
        integrity = [i for i in issues if not i.startswith("need >= ")]
        attempts.append({"campaign": str(output), "requested": requested,
                         "void": voids, "valid_total": valid, "counts": counts,
                         "evidence_problems": issues})
        if before is not None:
            regression = policy.topup_problems(before, counts, replaced=attempts[-1]["requested"])
            if regression:
                attempts[-1]["refused"] = regression
                integrity += regression
        needed = n - valid
        remaining = topup_budget() - spent
        # Retry only when the *only* thing wrong was lost measurement.
        if needed <= 0 or integrity or voids == 0 or remaining <= 0:
            break
        before, requested = counts, min(needed, remaining)
    if not parts:
        raise ValueError("nothing to calibrate: no campaign was requested or retained")
    atomic_json(bundle / "verification" /
                f"{receipt}-{resolve_model(model).replace('/', '-')}.json",
                {"schema": "arena-pipeline/topup/1", "model": model,
                 "episodes_requested": n,
                 "topup_budget": topup_budget(),
                 "topup_spent": spent, "episodes_run": n + spent,
                 "valid_episodes": len(measured.get("trials", [])),
                 "void_episodes": measured.get("voids_excluded", 0),
                 "merge": "void replacements appended; every scored episode retained",
                 "campaigns": parts, "attempts": attempts})
    ok = not issues and not attribution.problems(bundle, measured, n)
    return parts[0], parts[1:], ok


#: The blind solve runs uncapped (policy.SPEC_NO_LIMITS). `calibrate.run.campaign`
#: records `max_steps` as a positive integer in its receipt, so "no cap" is the
#: largest one rather than its 400-step default.
_UNCAPPED_STEPS = sys.maxsize

#: Per-rollout findings from `evidence.calibration_problems` that describe what
#: an episode *did* -- void, stalled, capped, incomplete -- rather than whether
#: the receipt is genuine. For a diagnostic blind solve they are outcomes.
_BLIND_OUTCOME_ISSUES = (": void attempt cannot be release evidence",
                         ": incomplete or stalled attempt",
                         ": missing, repeated or reordered steps")


def _blind_integrity(bundle: Path, report: dict, replay: bool) -> list[str]:
    """Receipt integrity only. Voided, capped or failed rows are recorded outcomes."""
    from .evidence import calibration_problems
    return [i for i in calibration_problems(bundle, report, minimum=1, replay=replay)
            if not i.endswith(_BLIND_OUTCOME_ISSUES)
            and not i.startswith("need at least ")
            and i != "calibration must cover every declared step as complete episodes"]


def _blind_solve_problems(bundle: Path, replay: bool = True) -> tuple[list[str], dict]:
    """The blind solve is optional, diagnostic evidence (policy.BLIND_SOLVE_GATES_RELEASE).

    Missing: recorded as missing, not a blocker -- the spec's solvability is
    met by the reference trajectory, which `controls` proves. Present: only its
    integrity blocks, because a corrupt receipt is a different thing from a
    model that could not solve the task.
    """
    from . import policy
    path = bundle / "verification/blind-solve.json"
    if not path.is_file():
        return [], {"present": False, "gates_release": policy.BLIND_SOLVE_GATES_RELEASE}
    report = strict_json(path.read_text())
    issues = _blind_integrity(bundle, report, replay)
    episodes = {}
    for row in report.get("rollouts", []):
        episodes.setdefault(row.get("episode"), []).append(row)
    names = [s["name"] for s in tomllib.loads((bundle / "task.toml").read_text())["steps"]]
    solved = any(len(rows) == len(names)
                 and all(r.get("reward") == 1 and r.get("zero_class") != "void"
                         for r in rows)
                 for rows in episodes.values())
    voids = sum(1 for rows in episodes.values()
                if any(r.get("zero_class") == "void" or r.get("error") for r in rows))
    record = {"present": True, "model": report.get("model"), "solved": solved,
              "episodes": len(episodes), "void_episodes": voids,
              "max_steps": report.get("max_steps"),
              "gates_release": policy.BLIND_SOLVE_GATES_RELEASE}
    if policy.BLIND_SOLVE_GATES_RELEASE and not solved:
        issues.append("no independently solved complete episode scores 1 on every step")
    return issues, record


def _evaluate(bundle: Path, replay: bool = True) -> dict:
    from .local_work import enabled, run
    if enabled():
        return run('release-evaluate', {'bundle': str(bundle.resolve()), 'replay': replay})
    from .run import run_bundle
    from .assurance import controls, generated_problems
    from .evidence import calibration_problems
    from .privacy import scan
    from .review import review_problems
    from .benchmark.harbor import inspect_package, parse_trial, summarize, EXPECTED_VERSIONS
    bundle = bundle.resolve()
    # False until two spec-model campaigns prove otherwise.
    errors, evidence = [], {"meets_spec_general_difficulty": False}
    sha = identity(bundle)
    def check(label, fn):
        try:
            issues = fn()
            errors.extend(f"{label}: {e}" for e in issues)
        except Exception as exc:
            errors.append(f"{label}: unverified ({type(exc).__name__}: {exc})")

    def gates():
        report = run_bundle(bundle, Path(__file__).resolve().parents[1])
        evidence["pre_calibration"] = report["verdict"]
        return [] if report["verdict"] == "green" else [f"pre-calibration verdict {report['verdict']}"]
    check("gates", gates)
    from .privacy import unresolved
    check("privacy", lambda: [str(r) for r in unresolved(bundle, scan(bundle / "environment/repo"))])
    check("provenance", lambda: _provenance_problems(bundle))
    from .corpus import signature, problems, registry_root, register
    check("novelty", lambda: problems(signature(bundle), registry_root()))
    def runtime():
        inspect_package(bundle)
        from .author import runtime as grader_runtime
        return grader_runtime.problems(bundle)
    check("runtime", runtime)
    check("reproducible export", lambda: generated_problems(bundle))
    from .author import controls as controls_mod, probes as probes_mod
    check("controls", lambda: controls_mod.problems(bundle))
    check("probes", lambda: probes_mod.problems(bundle))

    def campaigns():
        from . import attribution, metrics, policy
        index = strict_json((bundle / "verification/native-calibrations.json").read_text())
        if index.get("artifact_sha256") != sha:
            return ["native calibration index is missing or stale"]
        problems, reports, hack_issues = [], [], []
        from .arena_v01_release import FORMAT, target, target_problems
        params = target(bundle)
        evidence["calibration_format"] = {"format": FORMAT if params else "pipeline bundle", "target": params}
        for path in index.get("campaigns", []):
            # Voids leave the denominator; what blocks is too few *valid*
            # episodes, counted over the campaign and its void replacements.
            issues, measured = calibration_measurement(
                bundle, index, path, policy.SPEC_EPISODES_PER_MODEL)
            # The calibration of record measures what the version releases on: with an Arena v0.1 target, the
            # derived package of exactly that target (user, 2026-10-24); never a mix.
            for part in [path, *((index.get("topups") or {}).get(str(path)) or [])]:
                issues = issues + target_problems(part, params)
            # Band composition no longer gates -- see policy.BANDS_GATE_RELEASE.
            # The old rule demanded 4-6 objectives at 30-70% and at most two
            # below 30%, which a genuinely hard task cannot satisfy: most of its
            # checks sit below 30% by construction. It is recorded as diagnosis
            # and read by `arena diagnose`; what decides acceptance is whether
            # the failures are the model's, below.
            issues += attribution.problems(bundle, measured)
            # Spec, "Inspect passing runs for reward hacking". Scanned on every
            # campaign read, rejected or not, so a finding names its trial even
            # when something else already blocks. Every hit is unresolved until
            # adjudicated; the batch criterion is zero confirmed or unresolved.
            hits = attribution.trajectory_hacks(bundle, measured or {})
            evidence.setdefault("reward_hacking", {})[str(path)] = {
                "model": (measured or {}).get("model"), "hits": hits}
            hack_issues.extend(attribution.hack_problems(hits))
            if issues:
                evidence.setdefault("rejected_campaigns", {})[str(path)] = issues
            else:
                reports.append(measured)
        models = {r["model"] for r in reports}
        evidence["calibrated_models"] = sorted(models)
        # Fail closed: a rejected campaign is not dropped in favour of another,
        # a retired model is refused, and one frozen campaign per model means a
        # better re-run cannot replace a worse one.
        # Every issue of a rejected campaign, not only its first (2026-10-21: a reading_settled problem listed
        # after another issue reached only evidence.rejected_campaigns and no release-repair round saw it).
        problems.extend(f"rejected campaign {Path(p).name}: {issue}"
                        for p, issues in evidence.get("rejected_campaigns", {}).items() for issue in issues)
        problems.extend(policy.calibration_problems(models))
        if len(reports) != len(models):
            problems.append("more than one campaign for a model; select one frozen campaign per model")

        # Spec, "Calibration and results": the models are Fable 5.1 (primary)
        # and Astra (secondary). The measured set must BE that set -- not
        # contain it, not overlap it. A missing primary used to become a
        # "deviation" that applied the primary's pass@1 thresholds to whichever
        # model sorted first; a pass@1 on another model is not the spec's number.
        from .benchmark.harbor import resolve_model
        primary_model, secondary_model = (resolve_model(m) for m in policy.resolve_calibration_models())
        required = {primary_model.casefold(), secondary_model.casefold()}
        measured_set = {str(m).casefold() for m in models}
        if measured_set != required:
            problems.append(
                f"calibration models {sorted(models) or 'none'} are not the spec's set "
                f"{sorted([primary_model, secondary_model])}"
                + (f"; missing {sorted(required - measured_set)}" if required - measured_set else "")
                + (f"; not calibration models {sorted(measured_set - required)}"
                   if measured_set - required else ""))
        for r in reports:
            # Effort and limits are part of the measurement (policy), on EVERY
            # campaign -- agreeing with each other is not agreeing with policy.
            if r.get("effort") != policy.SPEC_CALIBRATION_EFFORT:
                problems.append(f"{r['model']}: reasoning effort {r.get('effort')!r}, "
                                f"policy requires {policy.SPEC_CALIBRATION_EFFORT!r}")
            wanted = policy.output_tokens_for(r.get("model"))
            # A harness campaign (Claude Code / Codex inside the container) lets
            # the CLI manage output; the recorded allowance is None, by design.
            if (r.get("limits") or {}).get("max_output_tokens") not in (wanted, None):
                problems.append(f"{r['model']}: output allowance is "
                                f"{(r.get('limits') or {}).get('max_output_tokens')!r}, policy "
                                f"requires {wanted!r} (policy.SPEC_MAX_OUTPUT_TOKENS)")
            if (r.get("limits") or {}).get("no_limits") is not policy.SPEC_NO_LIMITS:
                problems.append(f"{r['model']}: limits.no_limits is "
                                f"{(r.get('limits') or {}).get('no_limits')!r}, policy "
                                f"requires {policy.SPEC_NO_LIMITS!r}")

        problems.extend(hack_issues)

        # Spec metrics. pass@1 per model, reported and gated; pass@4 and mean
        # reward reported alongside because pass@1 discards them.
        by_model = {r["model"]: metrics.task_metrics(r) for r in reports}
        evidence["task_metrics"] = by_model
        found = {str(name).casefold(): m for name, m in by_model.items()}
        primary = found.get(primary_model.casefold())
        secondary = found.get(secondary_model.casefold())
        evidence["hardness"] = metrics.hardness(primary, secondary)
        # Spec general difficulty (pass@1 < 0.5 on both models), exposed so the
        # batch report can label tasks admitted under the local 0.6 bound.
        evidence["meets_spec_general_difficulty"] = evidence["hardness"]["meets_spec_general_difficulty"]
        problems.extend(metrics.difficulty_problems(primary, secondary))
        if len({(r["effort"], r["protocol"]) for r in reports}) > 1:
            problems.append("model campaigns use different reasoning effort or native protocols")
        if len({(r.get("limits", {}).get("no_limits", False),
                 r.get("limits", {}).get("context_limit"),
                 r.get("limits", {}).get("campaign_timeout_sec")) for r in reports}) > 1:
            problems.append("model campaigns use different turn, context or time limits")
        return problems
    check("calibration", campaigns)

    # User, 2026-10-21 (external audit, class 3): the calibration solutions that PASSED a check are audited for a
    # published clause they break (coverage_critic.pass_audit, run by qualify after the campaigns). A demonstrated
    # violation blocks; a missing, stale or incomplete audit of this calibration is unverified, never a pass.
    def pass_audit():
        from . import coverage_critic
        record = None
        try:
            record = strict_json((bundle / coverage_critic.PASS_RECEIPT).read_text())
        except (OSError, ValueError):
            pass
        if isinstance(record, dict) and record.get("artifact_sha256") == sha:
            evidence["pass_audit"] = {"complete": record.get("complete"), "open": len(record.get("gaps") or []),
                                      "advisory": len(record.get("advisory") or []),
                                      "closed": len(record.get("closed") or []),
                                      "unaudited_checks": {s: e.get("unaudited_checks") for s, e in
                                                           (record.get("steps") or {}).items()
                                                           if e.get("unaudited_checks")}}
        return coverage_critic.pass_problems(bundle, sha)
    check("pass audit", pass_audit)

    # Solvability. The spec: "At least one calibration run OR reference
    # trajectory scores 1.0." The reference trajectory alone satisfies it, and
    # `controls` above already proves the reference scores 1 and an untouched
    # workspace scores 0 on every step -- the spec's grader-sanity check.
    #
    # What used to be here required one of three *blind model* episodes to score
    # 1.0 on every step before the task could reach difficulty measurement.
    # That is strictly stronger than the spec and it inverts the objective: the
    # harder a task is, the less likely it survives its own solvability gate.
    # Every task that got through it measured 0.93-1.00 mean reward, because the
    # gate had already removed everything else. See policy.BLIND_SOLVE_GATES_RELEASE.
    #
    # The blind solve still runs and is still retained -- it is useful evidence
    # and it goes in the batch report -- but a model failing it is now a fact
    # about the model.
    # No blind solve for coding tasks: the file-workspace protocol has no shell.
    # Solvability is the native oracle run (below) plus the reference trajectory.
    evidence["blind_solve"] = {"applicable": False, "reason": "coding tasks: solvability is the oracle run"}

    # Spec: "run harbor tasks-check" and, at batch level, 100% of tasks pass it.
    def harbor_static():
        from .benchmark import tasks_check
        issues = tasks_check.problems(bundle)
        record = tasks_check.retained(bundle)
        if record:
            evidence["harbor_tasks_check"] = record["status"]
        return issues
    check("harbor tasks-check", harbor_static)

    # Spec: tag each environment and task F0-F3. The level also has to
    # agree with the task's name, which carries it into every downstream table.
    def faithfulness_level():
        from .ledger import levels
        from . import naming
        stored = strict_json((bundle / "provenance/ledger.json").read_text())
        # Re-assigned from the evidence as it stands now. The stored level was
        # assigned before calibration, when F1's empirical clause ("models
        # failing the same way") could not yet be read, so an F1 task could
        # never be released; rewriting the ledger would invalidate the
        # calibration bound to its identity.
        record = levels.assign(bundle, stored, levels.calibration_reproduces_failure(bundle))
        level = record["level"]
        evidence["faithfulness"] = {"level": level,
                                    "source": levels.SOURCE_NAME.get(level),
                                    "ledger_level": stored.get("faithfulness_level")}
        # The working tree's name carries the level its ledger was built with; delivery re-assigns the level the
        # same way (deliver.plan -> levels.assign_measured) and renames the delivered copy to it. Holding the
        # working name to the RE-ASSIGNED level refused every task calibration lifted from F0 to F1 (2026-10-18:
        # c-015 v5/v6, c-023 v5/v6, c-038 v4/v5, all hard on both models: "task name claims faithfulness F0 but
        # the ledger measures F1"), and renaming here would change the identity the calibration is bound to.
        return faithfulness_problems(bundle, stored, record)
    check("faithfulness", faithfulness_level)

    # Spec: "a scripted solution included in the package that scores full marks."
    # Harbor's own rubric fails a solution that copies a stored answer.
    def reference_solution():
        from .author import solution
        return solution.problems(bundle)
    check("reference solution", reference_solution)
    check("semantic review", lambda: review_problems(bundle, strict_json((bundle / "verification/semantic-review.json").read_text())))

    def native():
        from .evidence import native_evidence
        index = strict_json((bundle / "verification/native-controls.json").read_text())
        if index.get("artifact_sha256") != sha:
            return ["native controls refer to another task version"]
        problems = []
        from .arena_v01_release import target, target_problems
        params = target(bundle)
        for role, expected in (("oracle", 1), ("nop", 0)):
            issues, measured = native_evidence(bundle, Path(index[role]), role)
            problems.extend(f"{role}: {e}" for e in issues + target_problems(index[role], params))
            if not measured["trials"] or any(t.get("reward") != expected for t in measured["trials"]):
                problems.append(f"native {role} expected complete reward {expected}")
        return problems
    check("native", native)
    if identity(bundle) != sha:
        errors.append("task changed during release verification")
    result = {"schema": "arena-pipeline/release/1", "bundle": bundle.name,
              "artifact_sha256": sha, "status": "accepted" if not errors else "blocked",
              "policy_sha256": policy_identity(),
              "release_ready": not errors, "errors": errors, "evidence": evidence}
    if result["release_ready"]:
        try:
            errors.extend(register(bundle, result))
        except Exception as exc:
            errors.append(f"release catalog unavailable: {exc}")
        if errors:
            result.update(status="blocked", release_ready=False)
    atomic_json(bundle / "verification/release.json", result)
    return result


def evaluate(bundle: Path, replay: bool = True) -> dict:
    with bundle_lock(bundle):
        return _evaluate(bundle, replay)


def _preflight(bundle: Path) -> bool:
    from .run import run_bundle
    from .privacy import scan
    from .corpus import signature, problems, registry_root
    from .assurance import generated_problems, controls
    from .benchmark.harbor import doctor
    if run_bundle(bundle, Path(__file__).resolve().parents[1])['verdict'] != 'green':
        return False
    from .privacy import unresolved
    if unresolved(bundle, scan(bundle / 'environment/repo')) or _provenance_problems(bundle):
        return False
    if problems(signature(bundle), registry_root()) or generated_problems(bundle):
        return False
    from .author import controls as controls_mod
    if controls_mod.problems(bundle):
        return False
    if not doctor()['ok']:
        raise RuntimeError('native runtime preflight failed; run arena benchmark --doctor')
    return True


def _calibrated(bundle: Path, models: list[str], n: int) -> bool:
    """Would `_qualify` reuse an existing calibration of record for EVERY model?

    The same test `_qualify.measure` applies before reusing a campaign: indexed
    for this exact task version, and `calibration_measurement(..., n)` finds no
    issue. Anything weaker lets a stale, void-short or evidence-less campaign
    skip the screen gate and then start a fresh ten-episode campaign anyway.
    """
    from .benchmark.harbor import resolve_model
    try:
        index = strict_json((Path(bundle) / "verification/native-calibrations.json").read_text())
        if index.get("artifact_sha256") != identity(Path(bundle)):
            return False
        reusable = set()
        from .arena_v01_release import target, target_problems
        params = target(Path(bundle))
        for path in index.get("campaigns", []):
            issues, measured = calibration_measurement(Path(bundle), index, path, n)
            if not issues and measured.get("model") and not target_problems(path, params):
                reusable.add(str(measured["model"]).casefold())
        return {resolve_model(m).casefold() for m in models} <= reusable
    except (OSError, ValueError, TypeError, KeyError):
        return False


#: Screen problems that only say the screen was measured by another native runner (evidence.native_evidence).
RUNNER_TRANSITION = ("native runner changed", "native runtime versions differ",
                     "native isolation/history controller changed",
                     "native harness or its pinned version differs")


def _qualify(bundle: Path, models: list[str], review_model: str, blind_model: str,
            native_root: Path, n: int = 10, workers: int = 2) -> dict:
    """Bounded production campaign. Stop spending as soon as a stage rejects it."""
    import uuid
    from .assurance import controls, generated_problems
    from .artifacts import freeze
    from .calibrate.run import campaign
    from .review import review
    from .benchmark.harbor import doctor
    from .benchmark.coder import native_runner
    from .benchmark.harbor import resolve_model
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .evidence import native_evidence
    from . import attribution, policy
    from .review import review_problems, semantic_rejection
    from .run import run_bundle
    if len(set(models)) < 2 or n < 10:
        raise ValueError("qualification requires at least two models and ten episodes per model")
    # A retired calibration model must not be measured on, however it got here
    # -- an old script, a stale snapshot, a hand-typed flag.
    retired = policy.calibration_problems(models)
    if retired:
        raise ValueError("; ".join(retired))
    bundle = bundle.resolve()
    # User, 2026-09-25: the ten-episode calibration of record follows a
    # five-episode screen of this exact version that measured cleanly -- valid
    # episodes, every failure attributable, no unresolved reward hacking. Its
    # pass@1 is never a condition: the target is strict but blocks nothing
    # (policy.DIFFICULTY_GATES_RELEASE). A task whose calibration of record
    # already exists for this version starts no new ten-episode campaign, so it
    # is not gated here; native controls, semantic review and the diagnostic
    # blind solve still run when their retained receipts are missing or stale.
    if not _calibrated(bundle, models, n):
        from .screen import shippable_problems
        refused = shippable_problems(bundle, models)
        from .arena_v01_release import target as _v01_target
        if refused and _v01_target(bundle):
            # Arena v0.1 release (user, 2026-10-24): the calibration of record below is measured fresh on this runner
            # and every gate reads it, so a clean screen measured by the previous runner (Harbor 0.22) still opens
            # the gate. Only the runner transition is forgiven; any other screen problem still refuses.
            refused = [p for p in refused if not str(p).split(": ", 1)[-1].startswith(RUNNER_TRANSITION)]
            # Opt-in (user, 2026-10-24: "complete all the tasks by 7 PM"): with no screen of this version at all,
            # the ten-episode calibration of record is the measurement and the gate. Every validity and hardness
            # gate below still reads it; only the five-episode spend guard is skipped, and the record says so.
            if refused and os.environ.get("ARENA_V01_RELEASE_WITHOUT_SCREEN") == "1" and all(
                    str(p).startswith("no calibration screen of this task version") for p in refused):
                print("  gate: no screen of this version; ARENA_V01_RELEASE_WITHOUT_SCREEN=1, so the 10 x 2 "
                      "calibration of record is the gate", flush=True)
                atomic_json(bundle / "verification/screen-gate-waived.json",
                            {"reason": "ARENA_V01_RELEASE_WITHOUT_SCREEN=1 (user, 2026-10-24: deadline)",
                             "refused": refused, "at": time.time()})
                refused = []
        if refused:
            raise ValueError("the task has no clean screen of this version, so the "
                             f"{policy.SPEC_EPISODES_PER_MODEL}-episode calibration of record "
                             "will not start: " + "; ".join(refused[:6]))
    producer = strict_json((bundle / "authoring/spec.json").read_text()).get("_producer", {}).get("model")
    if not producer or review_model == producer:
        raise ValueError("qualification requires an identified author and a different semantic review model")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    import time
    atomic_json(bundle / 'verification/qualification.json', {
        'stage': 'preflight', 'updated_at': time.time(), 'release_ready': False,
        'models': models, 'episodes_per_model': n})
    from .local_work import enabled, run
    ready = (run('qualification-preflight', {'bundle': str(bundle)}) if enabled()
             else _preflight(bundle))
    if not ready:
        return _evaluate(bundle)
    native_root = native_root.resolve() / uuid.uuid4().hex
    snapshot = native_root / "source"
    sha = freeze(bundle, snapshot)
    # User, 2026-10-24: "use the 0.24 calibration as release qualification". With an Arena v0.1 release target the
    # controls and the calibration of record run on the v0.1 package derived from this frozen snapshot, and those
    # campaigns are the package's calibration and Oracle evidence (pipeline/arena_v01_release.py).
    from .arena_v01_release import target, target_problems, derive
    params = target(bundle)
    run_on = Path(derive(snapshot, native_root / "arena-v01", params)["package"]) if params else snapshot
    def retained(name):
        try:
            value = strict_json((bundle / "verification" / name).read_text())
            return value if value.get("artifact_sha256", value.get("grader_fingerprint")) == sha else {}
        except (OSError, ValueError):
            return {}

    def reusable_native(path, role, minimum=1):
        try:
            issues, measured = native_evidence(bundle, Path(path), role, minimum)
            return measured if not issues and not target_problems(path, params) else None
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def progress(stage):
        import time
        atomic_json(bundle / "verification/qualification.json", {
            "artifact_sha256": sha, "stage": stage, "updated_at": time.time(),
            "models": models, "episodes_per_model": n, "release_ready": False})

    index = {"artifact_sha256": sha}
    prior = retained("native-controls.json")
    for role in ("oracle", "nop"):
        progress("native-" + role)
        existing = reusable_native(prior.get(role), role)
        if existing and all(t["reward"] == (1 if role == "oracle" else 0) for t in existing["trials"]):
            index[role] = prior[role]
            continue
        output = native_root / role
        # Controls run where calibration runs, so both measure one environment.
        run_campaign, capacity = native_runner("calibration")
        with capacity.reserve(1):
            report = run_campaign(run_on, output, agent=role)
        index[role] = str(output)
        atomic_json(bundle / "verification/native-controls.json", index)
        issues, measured = native_evidence(bundle, output, role)
        expected = 1 if role == "oracle" else 0
        if (not report["ok"] or issues or not measured["trials"]
                or any(t.get("reward") != expected for t in measured["trials"])):
            return _evaluate(bundle)
    atomic_json(bundle / "verification/native-controls.json", index)
    progress("semantic-review")
    previous_review = retained("semantic-review.json")
    try:
        review_issues = review_problems(bundle, previous_review)
    except (OSError, ValueError, KeyError, TypeError):
        review_issues = ["prior semantic review could not be validated"]
    if semantic_rejection(review_issues):
        # Repeated qualification must not shop for a passing verdict on an
        # unchanged task. Explicit repair still runs through qualify's loop.
        return _evaluate(bundle)
    reuse_review = previous_review.get("model") == review_model and not review_issues
    if not reuse_review and not review(bundle, review_model)["ok"]:
        return _evaluate(bundle)
    # Blind solve: evidence, not a gate. It used to stop qualification here
    # unless a model scored 1.0 on every step within three attempts, which
    # filtered out every hard task before difficulty was ever measured. The
    # spec's solvability requirement is satisfied by the reference trajectory,
    # and the native oracle control above has already proved it scores 1.
    # Voided, capped or failed blind episodes are outcomes, and an integrity
    # problem with the receipt is release's to report. Neither stops the
    # measurement that actually decides acceptance.
    index = {"artifact_sha256": sha, "campaigns": [], "topups": {}}
    prior_index = retained("native-calibrations.json")
    existing_campaigns = prior_index.get("campaigns", [])
    progress("native-calibration")

    def measure(model):
        for path in existing_campaigns:
            try:
                issues, measured = calibration_measurement(bundle, prior_index, path, n)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if not issues and measured.get("model") == resolve_model(model) and not any(
                    target_problems(p, params) for p in [path, *((prior_index.get("topups") or {}).get(str(path)) or [])]):
                extra = (prior_index.get("topups") or {}).get(str(path), [])
                if any("void episodes leave" in p for p in attribution.problems(bundle, measured, n)):
                    # A retained campaign short of valid episodes only because some were void:
                    # top up the voids, keeping every scored episode. Reusing it unchanged left
                    # c-014 blocked on 9/10 at every re-qualify (2026-10-08).
                    return _calibrate_model(bundle, run_on, native_root, model, n, workers,
                                            existing=[str(path), *extra])
                return path, extra, not attribution.problems(bundle, measured)
        # Top-up for infrastructure loss: `_calibrate_model` keeps every scored
        # episode and runs only as many more as were void. See its docstring
        # and `policy.topup_problems` (policy.MAX_TOPUP_ATTEMPTS bounds it).
        return _calibrate_model(bundle, run_on, native_root, model, n, workers)

    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = [pool.submit(measure, model) for model in models]
        for future in as_completed(futures):
            path, extra, _ = future.result()
            index["campaigns"].append(path)
            if extra:
                index["topups"][path] = extra
            atomic_json(bundle / "verification/native-calibrations.json", index)
    progress("pass-audit")
    _pass_audit(bundle, sha)
    return _evaluate(bundle)


def _pass_audit(bundle: Path, sha: str) -> dict:
    """The pass audit of the calibration of record, after the campaigns (user, 2026-10-21, external audit class 3:
    passing runs were never read). A complete receipt for this version and these campaigns is reused; an audit that
    cannot run is recorded as incomplete -- release reads it as unverified, never as clean."""
    from . import coverage_critic
    campaigns = [c["campaign"] for c in coverage_critic.audited_campaigns(bundle)]
    try:
        prior = strict_json((bundle / coverage_critic.PASS_RECEIPT).read_text())
    except (OSError, ValueError):
        prior = {}
    prior = prior if isinstance(prior, dict) else {}
    if prior.get("artifact_sha256") == sha and prior.get("campaigns") == campaigns and prior.get("complete"):
        return prior
    try:
        return coverage_critic.pass_audit(bundle, record_dir=bundle / "verification")
    except Exception as exc:                            # noqa: BLE001 -- recorded as unverified, never as clean
        record = {"schema": coverage_critic.PASS_SCHEMA, "artifact_sha256": sha, "campaigns": campaigns,
                  "complete": False, "incomplete": [f"the pass audit did not run: {type(exc).__name__}: {str(exc)[:600]}"],
                  "steps": {}, "gaps": [], "advisory": [], "closed": []}
        atomic_json(bundle / coverage_critic.PASS_RECEIPT, record)
        return record


def _awaiting_screen(bundle: Path, revisions: list) -> dict:
    """A qualification revision's outcome: the new version must be screened first."""
    from . import policy
    from .checkpoint import update
    try:
        sha = identity(Path(bundle))
    except (OSError, ValueError):
        sha = None                  # an incomplete revision is recorded, not hidden
    result = {"schema": "arena-pipeline/release/1", "bundle": Path(bundle).name,
              "artifact_sha256": sha, "status": "awaiting-screen",
              "release_ready": False, "qualification_revisions": revisions,
              "errors": [f"revised: screen this version ({policy.SCREEN_EPISODES_PER_MODEL} "
                         "episodes per model, `arena screen`) before the calibration of record"]}
    atomic_json(Path(bundle) / "verification/qualification.json", result)
    update(Path(bundle), stage="awaiting-screen")
    return result


def faithfulness_problems(bundle: Path, stored: dict, record: dict) -> list[str]:
    """The level's own blockers, and the working name checked against the level its ledger was built with
    (delivery renames to the re-assigned level; see qualify's faithfulness step)."""
    from . import naming
    from .ledger import levels
    return levels.problems(record) + naming.problems(bundle, stored.get("faithfulness_level") or record["level"])


def qualify(bundle: Path, models: list[str], review_model: str, blind_model: str,
            native_root: Path, n: int = 10, workers: int = 2,
            repair_attempts: int = 0) -> dict:
    from . import policy
    # One budget per candidate, shared with the gate loop (checkpoint.repairs_left).
    if (type(repair_attempts) is not int
            or not 0 <= repair_attempts <= policy.MAX_REPAIR_PASSES):
        raise ValueError(f"repair_attempts must be between zero and "
                         f"policy.MAX_REPAIR_PASSES ({policy.MAX_REPAIR_PASSES})")
    manual_revision_requested = repair_attempts > 0
    with bundle_lock(bundle):
        try:
            from .checkpoint import read as checkpoint, repair_budget, admit_repair, update
            repair_attempts = repair_budget(bundle, 'qualification', repair_attempts)
            revisions = []
            retained = checkpoint(bundle)
            if retained.get('phase') == 'qualification-repair':
                if not manual_revision_requested:
                    result = _evaluate(bundle)
                    result.update(status='awaiting-hardening', release_ready=False,
                                  qualification_revisions=[], manual_trigger_required=True)
                    atomic_json(bundle / "verification/qualification.json", result)
                    return result
                from .repair import revise
                revisions.append(revise(bundle, retained['feedback']))
                return _awaiting_screen(bundle, revisions)
            for attempt in range(repair_attempts + 1):
                result = _qualify(bundle, models, review_model, blind_model, native_root, n, workers)
                atomic_json(bundle / "verification/qualification.json", result)
                if result["release_ready"] or attempt == repair_attempts:
                    break
                from .repair import feedback, revise
                findings = feedback(bundle, result)
                if not findings:
                    break
                # The monotone governor: cited, strictly shrinking, never the
                # same finding twice. A refusal is a recorded disposition.
                from .repair import disposition
                allowed, why, _ = admit_repair(bundle, 'qualification', findings)
                if not allowed:
                    disposition(bundle, findings, why)
                    result.update(repair_stopped_because=why)
                    break
                from .author.outcomes import SeedRejected
                try:
                    revisions.append(revise(bundle, findings))
                except SeedRejected as exc:
                    result.update(status="reserve", release_ready=False, source_rejection=exc.report)
                    break
                # The revision is a new task version with no screen, and the
                # ten-episode campaign is refused without one. Stop here rather
                # than raise on the next pass.
                return _awaiting_screen(bundle, revisions)
            # A revised version always returns through the entire qualification
            # path. Exhaustion never accepts the last unmeasured author response.
            result["qualification_revisions"] = revisions
            atomic_json(bundle / "verification/qualification.json", result)
            update(bundle, stage='accepted' if result['release_ready'] else result['status'])
            return result
        except BaseException as exc:
            atomic_json(bundle / "verification/qualification.json", {
                "status": "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "error",
                "error": str(exc), "release_ready": False})
            raise


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--qualify", action="store_true")
    from . import policy as _policy
    _primary, _secondary = _policy.resolve_calibration_models()
    ap.add_argument("--models", nargs="+", default=[_primary, _secondary])
    # The reviewer must differ from the author, and is not a calibration model.
    ap.add_argument("--review-model", default=_secondary)
    ap.add_argument("--blind-model", default=_secondary)
    ap.add_argument("--native-root", type=Path, default=Path("pipeline/artifacts/native"))
    ap.add_argument("--rollouts", type=int, default=10)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--repair-attempts", type=int, default=0,
                    choices=range(_policy.MAX_REPAIR_PASSES + 1),
                    help="archive and revise rejected candidates, then requalify "
                         "(bounded by policy.MAX_REPAIR_PASSES, shared with gate repairs)")
    args = ap.parse_args(argv)
    if args.qualify:
        from .cli import load_keys
        load_keys(prompt_if_missing=False)
        try:
            report = qualify(args.bundle, args.models, args.review_model, args.blind_model,
                             args.native_root, args.rollouts, args.workers, args.repair_attempts)
        except ValueError as exc:
            # Every refusal names its reason; a traceback would bury it.
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
    else:
        report = evaluate(args.bundle)
    print(json.dumps(report, indent=2))
    return 0 if report["release_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
