"""The batch Arena receives, in the shape the spec asks for.

Spec, "Deliverables": environment, tasks, grader, **runs and results**,
**reference solution**, **batch report**. Of those six the workspace produced
the first three. The other three were computable from retained artefacts and
never assembled, so the batch's own acceptance criteria could not be evaluated
by us -- Arena would have been the first to compute them.

This module emits:

    BATCH_REPORT.md / batch-report.json   every batch- and task-level metric
    trace-accounting.json                 100% of shared sessions dispositioned
    <env>/REWARD-HACKING.md               what was tested, found and fixed
    <env>/runs/                           every calibration run and its score

and it computes the six batch acceptance criteria and the three yield metrics,
so a batch that would fail is refused here rather than on arrival.

One rule governs the whole module: a number that could not be computed is
reported as unknown and blocks the criterion it belongs to. A report that
silently omits a task it could not read is worse than no report, because it
looks complete.
"""
from __future__ import annotations

import json
import shutil
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import metrics, naming, policy
from .ledger import levels


# ---------------------------------------------------------------------------
# Trace accounting
# ---------------------------------------------------------------------------

def trace_accounting(sessions: list[str], converted: dict[str, list[str]],
                     excluded: dict[str, str], *,
                     expected_count: int | None = None,
                     expected_sessions: list[str] | None = None) -> dict:
    """Spec: "100% of shared traces dispositioned (converted -> env IDs, or
    excluded -> reason)".

    `sessions` is every session Arena shared -- the unfiltered denominator.
    `converted` maps a session to the environment IDs it produced. `excluded`
    maps a session to the reason it produced none.

    A session listed twice is one session: counting it twice inflates the
    denominator AND the dispositioned numerator. When the dump says how many
    sessions it holds (`expected_count`) or which ones it indexed
    (`expected_sessions`), the shared list has to agree with it -- a list that
    silently dropped sessions dispositions 100% of the wrong denominator.
    """
    listed = len(sessions)
    sessions = list(dict.fromkeys(sessions))
    rows, missing = [], []
    for session in sessions:
        if session in converted and converted[session]:
            rows.append({"session_id": session, "disposition": "converted",
                         "environments": sorted(converted[session])})
        elif session in excluded and str(excluded[session]).strip():
            rows.append({"session_id": session, "disposition": "excluded",
                         "reason": excluded[session]})
        else:
            missing.append(session)
            rows.append({"session_id": session, "disposition": "undispositioned"})

    share = (len(sessions) - len(missing)) / len(sessions) if sessions else None
    problems = []
    if listed != len(sessions):
        problems.append(
            f"the shared session list names {listed} entries but only "
            f"{len(sessions)} distinct sessions; duplicates are counted once")
    if expected_count is not None and expected_count != len(sessions):
        problems.append(
            f"the dump holds {expected_count} sessions but the shared list has "
            f"{len(sessions)}; the denominator must be every session shared")
    if not sessions:
        problems.append("the shared session list is empty; trace accounting over no "
                        "sessions is not 100% dispositioned, it is unmeasured")
    stray = sorted(s for s, envs in converted.items() if envs and s not in set(sessions))
    if stray:
        problems.append(
            f"{len(stray)} session(s) produced a task but are not in the shared list; "
            f"either the list is incomplete or the task names the wrong source. "
            f"First few: {', '.join(stray[:5])}")
    absent = sorted(set(expected_sessions or ()) - set(sessions))
    if absent:
        problems.append(
            f"{len(absent)} session(s) in the dump index are missing from the "
            f"shared list. First few: {', '.join(absent[:5])}")
    if missing:
        problems.append(
            f"{len(missing)} of {len(sessions)} shared sessions carry no "
            f"disposition. Every session must be either converted to named "
            f"environment IDs or excluded with a reason; silence is not a "
            f"disposition. First few: {', '.join(missing[:5])}")
    return {"schema": "arena-pipeline/trace-accounting/1",
            "sessions_shared": len(sessions),
            "sessions_listed": listed,
            "sessions_expected": expected_count,
            "converted": sum(1 for r in rows if r["disposition"] == "converted"),
            "excluded": sum(1 for r in rows if r["disposition"] == "excluded"),
            "undispositioned": len(missing),
            "dispositioned_share": share,
            "rows": rows, "ok": not problems, "problems": problems}


def dump_expectation(index: Path | None) -> dict:
    """What the corpus index says the dump holds, for `trace_accounting`."""
    if index is None or not Path(index).is_file():
        return {}
    document = json.loads(Path(index).read_text())
    manifest = (document.get("dump_inventory") or {}).get("cohort_manifest.json") or {}
    count = manifest.get("sessions")
    if count is None:
        count = document.get("sessions")
    return {"expected_count": count,
            "expected_sessions": sorted({r["session_id"] for r in document.get("rows") or []
                                         if r.get("session_id")})}


# ---------------------------------------------------------------------------
# Yield
# ---------------------------------------------------------------------------

def yield_metrics(sessions: list[str], tasks: list[dict]) -> dict:
    """Spec, "Monitoring metrics": three yields over an unfiltered denominator.

    "Use the total number of sessions shared by Arena in a batch as the
    denominator, without filtering."  Reporting a yield over the sessions we
    chose to attempt would make the number meaningless -- and flattering.
    """
    shared = set(sessions)
    total = len(shared)
    if not total:
        return {"sessions_shared": 0, "problems": ["no sessions were shared"]}

    any_env, difficulty, hard_f2 = set(), set(), set()
    for task in tasks:
        session = task.get("session_id")
        if session not in shared:
            continue                     # only sessions Arena shared count
        level = task.get("faithfulness_level")
        hard = bool((task.get("hardness") or {}).get("hard"))
        accepted = task.get("accepted") is True
        any_env.add(session)
        if accepted and hard and level in ("F1", "F2", "F3"):
            difficulty.add(session)
        if accepted and hard and level in ("F2", "F3"):
            hard_f2.add(session)

    return {"schema": "arena-pipeline/yield/1",
            "sessions_shared": total,
            "environment_yield": len(any_env) / total,
            "difficulty_yield": len(difficulty) / total,
            "hard_task_yield": len(hard_f2) / total,
            "definitions": {
                "environment_yield": "% of sessions shared that produce at least "
                    "one environment, at any faithfulness level",
                "difficulty_yield": "% of sessions shared that produce at least "
                    "one accepted environment containing a task at hard "
                    "difficulty (primary pass@1 <= 0.2), at F1+ faithfulness",
                "hard_task_yield": "% of sessions shared that produce at least "
                    "one accepted environment containing a hard task at F2+"}}


# ---------------------------------------------------------------------------
# Per-task record
# ---------------------------------------------------------------------------

def task_record(bundle: Path) -> dict:
    """Everything the batch report needs about one environment.

    Reads only retained receipts. A receipt that is absent or bound to another
    task version yields `None` for its field and a problem, never a default.
    """
    from .author.contracts import strict_json
    from .benchmark import tasks_check

    bundle = Path(bundle)
    record: dict = {"bundle": bundle.name, "problems": []}

    def read(rel):
        path = bundle / rel
        if not path.is_file():
            record["problems"].append(f"missing {rel}")
            return None
        try:
            return strict_json(path.read_text())
        except (OSError, ValueError) as exc:
            record["problems"].append(f"unreadable {rel}: {exc}")
            return None

    import tomllib
    try:
        config = tomllib.loads((bundle / "task.toml").read_text())
    except (OSError, ValueError) as exc:
        record["problems"].append(f"unreadable task.toml: {exc}")
        config = {}
    meta = config.get("metadata", {})
    record["task_name"] = (config.get("task") or {}).get("name")
    record["short_name"] = (record["task_name"] or "").split("/")[-1]
    # Spec: "Include the source Arena session ID with each task."
    record["session_id"] = meta.get("source_trace")
    if not record["session_id"]:
        record["problems"].append("no source Arena session ID on the task")
    record["steps"] = [s.get("name") for s in config.get("steps", [])]
    # Spec B6 + "For multi-turn tasks, give each turn its own rubric": two steps
    # are a multi-turn task only when every step carries an objective check of
    # its own. A second step graded by nothing is a second prompt, not a turn.
    checks = read("tests/checks.json") or {}
    objectives = Counter(c.get("step") for c in checks.get("checks") or []
                         if isinstance(c, dict) and c.get("kind") == "objective")
    record["objectives_by_step"] = {step: objectives.get(step, 0) for step in record["steps"]}
    # Spec Appendix A + the user's 2026-10-12 rule ("at least 3 turns"): a
    # multi-turn task has MULTI_STEP_MIN_STEPS or more turns, each graded by an
    # objective of its own, and ships its per-turn contract (TURNS.md).
    record["multi_turn"] = (len(record["steps"]) >= policy.MULTI_STEP_MIN_STEPS
                            and all(record["objectives_by_step"].values())
                            and (bundle / "TURNS.md").is_file())

    ledger = read("provenance/ledger.json") or {}
    record["provenance_included"] = (bundle / "provenance/ledger.json").is_file()
    record["faithfulness_level"] = ledger.get("faithfulness_level")
    record["faithfulness_by_bytes"] = ledger.get("faithfulness_by_bytes")
    record["source_tier"] = ledger.get("category")
    # The level the seed was classified to aim at before authoring; the
    # delivered level is the measured one.
    record["target_level"] = policy.target_level(ledger.get("category"))
    if not record["faithfulness_level"]:
        record["problems"].append("no faithfulness level (F0-F3) on the ledger")

    release = read("verification/release.json") or {}
    record["release_status"] = release.get("status")
    record["artifact_sha256"] = release.get("artifact_sha256")
    record["acceptance_problems"] = acceptance_problems(bundle)
    record["accepted"] = not record["acceptance_problems"]
    if release.get("status") == "accepted" and record["acceptance_problems"]:
        record["problems"].extend(f"acceptance: {p}" for p in record["acceptance_problems"])
    record["release_errors"] = release.get("errors", [])

    evidence = release.get("evidence") or {}
    record["hardness"] = evidence.get("hardness")
    record["per_model"] = evidence.get("task_metrics") or {}
    record["deviations"] = evidence.get("deviations", [])
    record["calibration_models_missing"] = missing_models(record["per_model"])
    record["meets_spec_general_difficulty"] = general_difficulty(record)

    check = tasks_check.retained(bundle)
    record["harbor_tasks_check"] = check["status"] if check else None
    if record["harbor_tasks_check"] != "pass":
        record["problems"].append(
            f"harbor tasks-check: {record['harbor_tasks_check'] or 'not run'}")

    probes = read("verification/probes.json") or {}
    results = probes.get("results") or {}
    # Coding probes record `ok` (and a reward where one exists); an attack
    # probe reads as its reward (1.0 = exploitable), a static probe as 0/1 ok.
    record["probes"] = {name: ((r.get("reward") if r.get("reward") is not None else (1.0 if r.get("ok") else 0.0))
                               if isinstance(r, dict) else r)
                        for name, r in results.items() if name != "determinism"}
    # Only the attack probes count. `golden` and `alternative` are supposed
    # to score 1.0 -- they are the reference solution and a correct answer
    # written differently -- so counting their full credit as an exploit would
    # report every sound task as 100% compromised.
    record["reward_hacking_findings"] = sorted(
        name for name, value in record["probes"].items()
        if name in policy.HACK_PROBES
        and isinstance(value, (int, float)) and value >= policy.PROBE_MAX)
    # The mirror-image defect: a probe that must earn full credit and did not.
    record["reference_probe_failures"] = sorted(
        name for name, value in record["probes"].items()
        if name in policy.EXPECT_FULL_CREDIT
        and (not isinstance(value, (int, float)) or value < policy.PROBE_MAX))
    record["reward_hacking_warnings"] = sorted(
        name for name, value in record["probes"].items()
        if name in policy.HACK_PROBES
        and isinstance(value, (int, float))
        and policy.PROBE_WARN_ABOVE < value < policy.PROBE_MAX)
    # "0 confirmed OR UNRESOLVED": a mandatory probe that is missing or did not
    # score is unresolved, and so is an attack that earns most of the reward.
    record["reward_hacking_unresolved"] = sorted(
        {name for name in policy.MANDATORY_PROBES
         if not isinstance(record["probes"].get(name), (int, float))}
        | set(record["reward_hacking_warnings"]))
    record["untested_surfaces"] = list(probes.get("untested_surfaces", []))
    # Critic gaps the judges demoted (user, 2026-10-09: "Block only realistic gaps") ship listed, never hidden.
    critic = read("verification/coverage-critic.json") or {}
    for g in critic.get("gaps") or []:
        if isinstance(g, dict) and g.get("blocking", True) is False:
            record["untested_surfaces"].append(
                f"Coverage critic, advisory ({(g.get('judgement') or {}).get('why')}): a wrong implementation scores "
                f"1.0 while violating \"{str(g.get('clause'))[:200]}\" -- {str(g.get('violation'))[:240]}")
        elif isinstance(g, dict) and not policy.LENIENCY_FINDINGS_BLOCK:
            record["untested_surfaces"].append(
                f"Coverage critic, advisory (a lenient grader): a wrong implementation scores 1.0 while violating "
                f"\"{str(g.get('clause'))[:200]}\" -- {str(g.get('violation'))[:240]}")
    # Pass-audit demonstrations where a wrong calibration solution still passes (policy.LENIENCY_FINDINGS_BLOCK).
    passed = read("verification/pass-audit.json") or {}
    for g in passed.get("gaps") or [] if not policy.LENIENCY_FINDINGS_BLOCK else []:
        if isinstance(g, dict) and g.get("route") != "instruction":
            record["untested_surfaces"].append(
                f"Pass audit, advisory (a lenient grader): a calibration solution passed {g.get('check')} while "
                f"violating \"{str(g.get('clause'))[:200]}\" -- on {str(g.get('input'))[:200]}")
    return record


def measures(per_model: dict, model: str) -> bool:
    """Does `per_model` hold a measurement of `model`? Release's matching rule."""
    return any(levels.is_primary_model((m or {}).get("model") or name, model)
               for name, m in (per_model or {}).items() if isinstance(m, dict))


def missing_models(per_model: dict) -> list[str]:
    """The resolved calibration models (primary, secondary) with no measurement."""
    return [m for m in policy.resolve_calibration_models() if not measures(per_model, m)]


def general_difficulty(task: dict) -> bool | None:
    """Spec A3: pass@1 below 0.5 on BOTH calibration models.

    A task measured at or above the bar on either model fails it whatever its
    release evidence says; otherwise the release's `meets_spec_general_difficulty`
    decides; with neither, both models measured under the bar is a pass and
    anything less is unknown (None), never a pass.
    """
    per_model = task.get("per_model") or {}
    bar = policy.SPEC_GENERAL_DIFFICULTY_MAX
    values = [m.get("pass_at_1") for m in per_model.values() if isinstance(m, dict)]
    if any(type(v) in (int, float) and v >= bar for v in values):
        return False
    flag = (task.get("hardness") or {}).get("meets_spec_general_difficulty")
    if flag is not None:
        return bool(flag)
    if not missing_models(per_model) and values and all(type(v) in (int, float) for v in values):
        return True
    return None


def open_privacy(bundle: Path) -> list[dict]:
    """Spec P1: unresolved privacy findings anywhere in a bundle, as file + kind.

    The whole bundle as it ships -- fixture, grader, solution, receipts, runs and
    notes -- not only `environment/repo`, because a transcript or an extraction
    carries the same text the fixture was redacted of. Fixture findings are
    resolved exactly as release resolves them (a pinned visual review of an
    unscannable file); nothing outside the fixture has a way to be resolved.
    Never returns matched values.
    """
    from . import privacy

    bundle = Path(bundle)
    prefix = "environment/repo/"
    rows = privacy.scan(bundle)
    fixture = [{**r, "file": str(r.get("file"))[len(prefix):]} for r in rows
               if str(r.get("file", "")).startswith(prefix)]
    rest = [r for r in rows if not str(r.get("file", "")).startswith(prefix)]
    open_ = [{**r, "file": prefix + r["file"]} for r in privacy.unresolved(bundle, fixture)]
    return [{"file": f, "kind": k} for f, k in
            sorted({(str(r.get("file")), str(r.get("kind"))) for r in [*open_, *rest]})]


def privacy_summary(findings: list[dict], limit: int = 8) -> str:
    """Locations and kinds only; the values never leave the scan."""
    shown = ", ".join(f"{f['file']} ({f['kind']})" for f in findings[:limit])
    more = f", and {len(findings) - limit} more" if len(findings) > limit else ""
    return f"{len(findings)} unresolved privacy finding(s): {shown}{more}"


def tasks_per_environment(records: list[dict]) -> dict:
    """Spec T4, "multiple tasks per environment": reported, not gated (an aim).

    An environment is the source session's situation -- "a single long session
    can seed several distinct environment tasks" -- and each Harbor package is
    one task in it. A task with no source session is its own environment.
    """
    groups: dict[str, list[str]] = {}
    for r in records:
        key = r.get("session_id") or f"(no session) {r.get('bundle')}"
        groups.setdefault(key, []).append(r.get("short_name") or r.get("bundle"))
    sizes = [len(v) for v in groups.values()]
    return {"definition": "environment = source session; task = Harbor package",
            "environments": len(groups), "tasks": len(records),
            "mean": (sum(sizes) / len(sizes)) if sizes else None,
            "min": min(sizes) if sizes else None, "max": max(sizes) if sizes else None,
            "multi_task_environments": sum(1 for n in sizes if n > 1),
            "by_environment": {k: sorted(v) for k, v in sorted(groups.items())}}


# ---------------------------------------------------------------------------
# Acceptance that survives delivery
# ---------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _without_level(ledger: dict) -> dict:
    from .ledger.faithfulness import LEVEL_FIELDS
    return {k: v for k, v in ledger.items() if k not in LEVEL_FIELDS}


def acceptance_problems(bundle: Path) -> list[str]:
    """Why this bundle is NOT the version its release receipt accepted; [] if it is.

    A release receipt is bound to `artifacts.identity`, and delivery rewrites
    two files inside it (the package name and the ledger's level). So a
    delivered bundle is accepted only if its delivery receipt proves, from the
    files on disk, that it is the accepted version with exactly those two
    rewrites: the accepted inventory hashes to the release's artifact_sha256,
    every other file is byte-identical to it, the original task.toml and ledger
    hash to their accepted entries, the delivered task.toml is the original
    with only `[task] name` replaced, and the delivered ledger is the original
    with only the level fields replaced. A bundle with no delivery receipt must
    match its release receipt directly. Either way the release must be
    `accepted` and, when it records one, bound to the current policy.
    """
    from .artifacts import identity, inventory, policy_identity

    bundle = Path(bundle)
    release_path = bundle / "verification/release.json"
    if not release_path.is_file():
        return ["no release receipt"]
    try:
        release_raw = release_path.read_bytes()
        release = json.loads(release_raw)
    except (OSError, ValueError) as exc:
        return [f"unreadable release receipt: {exc}"]
    problems = []
    if release.get("status") != "accepted":
        problems.append(f"release status is {release.get('status')!r}")
    if release.get("policy_sha256") is not None and release["policy_sha256"] != policy_identity():
        problems.append("release was decided under another policy version; re-release")

    receipt_path = bundle / "verification/delivery.json"
    try:
        current = inventory(bundle)
    except (OSError, ValueError) as exc:
        return problems + [f"the bundle has no identity: {exc}"]
    if not receipt_path.is_file():
        if identity(bundle) != release.get("artifact_sha256"):
            problems.append("release receipt is bound to another task version")
        return problems

    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError) as exc:
        return problems + [f"unreadable delivery receipt: {exc}"]
    source = receipt.get("source_inventory")
    toml_text, ledger_text = receipt.get("source_task_toml"), receipt.get("source_ledger")
    if not isinstance(source, dict) or not isinstance(toml_text, str) or not isinstance(ledger_text, str):
        return problems + ["delivery receipt does not carry the accepted inventory"]
    if receipt.get("release_sha256") != _sha(release_raw):
        problems.append("release receipt differs from the one delivery verified")
    source_identity = _sha(json.dumps(source, sort_keys=True, separators=(",", ":")).encode())
    if not (source_identity == receipt.get("source_identity") == release.get("artifact_sha256")):
        problems.append("the delivered bundle's source is not the version the release accepted")
    if not receipt.get("accepted_at_delivery"):
        problems.append("the source was not accepted when it was delivered: "
                        + "; ".join(receipt.get("acceptance_problems") or ["unknown"]))

    rewritten = {"task.toml", "provenance/ledger.json"}
    changed = sorted(rel for rel in set(source) | set(current)
                     if rel not in rewritten and source.get(rel) != current.get(rel))
    if changed:
        problems.append(f"{len(changed)} file(s) differ from the accepted version: "
                        + ", ".join(changed[:5]))
    if _sha(toml_text.encode("utf-8")) != source.get("task.toml") or \
            _sha(ledger_text.encode("utf-8")) != source.get("provenance/ledger.json"):
        problems.append("delivery receipt's original task.toml or ledger is not the accepted one")
        return problems

    from . import naming
    rename = receipt.get("rename") or {}
    # A receipt that records a metadata stamp (spec M4: the task's level in
    # `[metadata]`) allows exactly that one key beyond the rename, and only
    # with the level delivery assigned.
    stamp = receipt.get("metadata")
    assigned = (receipt.get("level") or {}).get("assigned")
    try:
        expected_toml = naming.rename_text(toml_text, rename.get("name") or "")
        if isinstance(stamp, dict):
            if set(stamp) != {naming.LEVEL_KEY} or stamp[naming.LEVEL_KEY] != assigned:
                problems.append("the delivery receipt stamps metadata other than the assigned level")
            expected_toml = naming.set_metadata(expected_toml, naming.LEVEL_KEY,
                                                str(stamp.get(naming.LEVEL_KEY)))
        # A replacement package bumps `[task].version` (the task bank's contract); the receipt records the number
        # it replaced and the one it carries, and that is the only further difference allowed.
        version = receipt.get("version")
        if isinstance(version, dict):
            import tomllib
            previous = tomllib.loads(toml_text)["task"].get("version")
            if version.get("previous") != previous or version.get("assigned") != naming.next_version(previous):
                problems.append("the delivery receipt's version is not the accepted version's next minor")
            expected_toml = naming.set_version(expected_toml, str(version.get("assigned")))
    except ValueError as exc:
        expected_toml = None
        problems.append(f"accepted task.toml cannot be renamed: {exc}")
    delivered_toml = (bundle / "task.toml").read_bytes().decode("utf-8")
    if expected_toml is not None and delivered_toml != expected_toml \
            and delivered_toml != toml_text:
        problems.append("task.toml differs from the accepted version by more than "
                        "the rename, the level stamp and a recorded version bump")
    try:
        delivered_ledger = json.loads((bundle / "provenance/ledger.json").read_text())
        if _without_level(delivered_ledger) != _without_level(json.loads(ledger_text)):
            problems.append("the ledger differs from the accepted version by more than the level")
        elif delivered_ledger.get("faithfulness_level") != (receipt.get("level") or {}).get("assigned"):
            problems.append("the ledger's level is not the one delivery assigned")
    except (OSError, ValueError) as exc:
        problems.append(f"unreadable ledger: {exc}")
    return problems


@contextmanager
def accepted_view(bundle: Path):
    """The accepted version of a delivered bundle, rebuilt in a temporary copy.

    Retained calibration evidence is bound to the identity the release saw, so
    a delivered bundle cannot be checked against it directly. When the
    delivery receipt verifies, restoring its two original files reproduces
    that identity exactly; otherwise the bundle is yielded as it is.
    """
    import tempfile

    bundle = Path(bundle)
    receipt_path = bundle / "verification/delivery.json"
    if not receipt_path.is_file() or acceptance_problems(bundle):
        yield bundle
        return
    receipt = json.loads(receipt_path.read_text())
    with tempfile.TemporaryDirectory(prefix="accepted-view-") as tmp:
        view = Path(tmp) / bundle.name
        shutil.copytree(bundle, view, ignore=shutil.ignore_patterns("runs", "__pycache__"))
        (view / "task.toml").write_bytes(receipt["source_task_toml"].encode("utf-8"))
        (view / "provenance/ledger.json").write_bytes(receipt["source_ledger"].encode("utf-8"))
        yield view


# ---------------------------------------------------------------------------
# Batch acceptance
# ---------------------------------------------------------------------------

def batch_acceptance(tasks: list[dict], accounting: dict) -> dict:
    """The six criteria from the spec's batch-level table, each with its share."""
    total = len(tasks)
    if not total:
        return {"ok": False, "criteria": [],
                "problems": ["the batch contains no tasks"]}

    # Spec B1, "harbor tasks-check pass, calibration runs + provenance
    # included": both resolved calibration models measured, the runs actually
    # exported beside the task, and its provenance (session ID + ledger) present.
    def not_reviewable(t) -> list[str]:
        why = []
        if t.get("harbor_tasks_check") != "pass":
            why.append(f"harbor tasks-check {t.get('harbor_tasks_check') or 'not run'}")
        why += [f"no calibration of {m}" for m in missing_models(t.get("per_model") or {})]
        if not (t.get("runs") or {}).get("exported"):
            why.append("calibration runs not exported")
        if not t.get("session_id"):
            why.append("no source session ID")
        if not t.get("provenance_included"):
            why.append("no provenance/ledger.json")
        return why
    unreviewable = {t.get("short_name") or t.get("bundle") or t.get("task_name"): why
                    for t in tasks if (why := not_reviewable(t))}
    reviewable = [t for t in tasks if not not_reviewable(t)]
    quality = [t for t in tasks
               if t.get("accepted") and general_difficulty(t)]
    hacking = sum(len(t.get("reward_hacking_findings") or [])
                  + len(t.get("reward_hacking_unresolved") or [])
                  + len(t.get("trajectory_unresolved") or []) for t in tasks)
    # An unstamped task is NOT excluded from the mix. Filtering it out shrinks
    # the denominator, so a batch where 29 of 42 carry no level reported "100%
    # at F2+" -- passing because tasks were absent, not because they qualified.
    # "Could not check" is never "nothing wrong", and least of all inside the
    # acceptance report that decides whether we ship.
    # A level outside F0-F3 is no level at all (spec B4): `batch_mix` counts
    # it as unlevelled, in the denominator and failing the criterion.
    unlevelled = [t.get("task_name") or t.get("bundle") for t in tasks
                  if t.get("faithfulness_level") not in ("F0", "F1", "F2", "F3")]
    mix = levels.batch_mix([{"level": t.get("faithfulness_level")} for t in tasks])
    if unlevelled:
        mix = dict(mix, ok=False, problems=[
            *(mix.get("problems") or []),
            "stamp unlevelled tasks with `levels.assign` before reading the mix. "
            "First few: " + ", ".join(str(n) for n in unlevelled[:5])])
    multi = [t for t in tasks if t.get("multi_turn")]

    criteria = [
        {"criterion": "Reviewable",
         "threshold": f"{policy.SPEC_REVIEWABLE_MIN:.0%} of tasks",
         "measured": len(reviewable) / total,
         "ok": len(reviewable) / total >= policy.SPEC_REVIEWABLE_MIN,
         "detail": "harbor tasks-check pass, calibration runs + provenance included"},
        {"criterion": "Task quality",
         "threshold": f"{policy.BATCH_UNDER_SPEC_DIFFICULTY_MIN:.0%} of tasks",
         "measured": len(quality) / total,
         "ok": len(quality) / total >= policy.BATCH_UNDER_SPEC_DIFFICULTY_MIN,
         "detail": "grader sanity, solvability and general difficulty (pass@1 "
                   f"< {policy.SPEC_GENERAL_DIFFICULTY_MAX})"},
        {"criterion": "Reward hacking", "threshold": "0 cases",
         "measured": hacking, "ok": hacking <= policy.SPEC_MAX_REWARD_HACKING,
         "detail": "confirmed or unresolved"},
        {"criterion": "Faithfulness mix",
         "threshold": f">={policy.SPEC_F2_PLUS_MIN:.0%} at F2+",
         "measured": mix.get("f2_plus_share"), "ok": bool(mix.get("ok")),
         "detail": f"counts {mix.get('counts')}"},
        {"criterion": "Trace accounting",
         "threshold": f"{policy.SPEC_TRACE_ACCOUNTING_MIN:.0%} dispositioned",
         "measured": accounting.get("dispositioned_share"),
         "ok": bool(accounting.get("ok")),
         "detail": f"{accounting.get('undispositioned')} undispositioned"},
        {"criterion": "Multi-turn",
         "threshold": f">={policy.SPEC_MULTI_TURN_MIN:.0%} of environments",
         "measured": len(multi) / total,
         "ok": len(multi) / total >= policy.SPEC_MULTI_TURN_MIN,
         "detail": f"{len(multi)} of {total} include a multi-turn task"},
    ]
    problems = [f"{c['criterion']}: {c['measured']} against {c['threshold']}"
                for c in criteria if not c["ok"]]
    problems += [f"not reviewable: {name}: {'; '.join(why)}"
                 for name, why in sorted(unreviewable.items(), key=lambda kv: str(kv[0]))]
    problems += mix.get("problems", []) + accounting.get("problems", [])
    # Spec P1. Not one of the six criteria, and it blocks the batch all the same.
    problems += [f"privacy: {t.get('short_name') or t.get('bundle')}: "
                 + privacy_summary(t["privacy_unresolved"])
                 for t in tasks if t.get("privacy_unresolved")]
    return {"criteria": criteria, "ok": not problems, "problems": problems}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _pct(value) -> str:
    # One decimal: 499/500 must not print as "100%" beside a FAIL.
    return "—" if value is None else f"{value:.1%}"


def _num(value, places=3) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def split_models(per_model: dict, primary: str | None) -> tuple[tuple, tuple]:
    """((name, metrics), (name, metrics)) for the primary and secondary model.

    Release's rule: the primary is the entry naming the primary calibration
    model; if none does and two were measured, the first by name, which
    release declares as a deviation. Never "whichever finished first".
    """
    items = sorted((per_model or {}).items())
    first = next(((n, m) for n, m in items
                  if levels.is_primary_model(m.get("model") or n, primary)), None)
    if first is None and len(items) >= 2:
        first = items[0]
    rest = [(n, m) for n, m in items if first is None or n != first[0]]
    return first or (None, None), (rest[0] if rest else (None, None))


def _general_label(task: dict) -> str:
    """Spec A3 as a table cell: a task at pass@1 >= 0.5 on either model FAILS."""
    verdict = general_difficulty(task)
    if verdict is None:
        return "unknown"
    return "meets (<0.5 both)" if verdict else "**FAILS** (pass@1 >= 0.5)"


def render_markdown(report: dict) -> str:
    out = ["# Batch report", "",
           f"Generated {report['generated_at']}.",
           f"Environments: {report['environments']}. "
           f"Sessions shared: {report['trace_accounting']['sessions_shared']}.", ""]

    if report.get("deviations"):
        out += ["## Declared deviations from the spec", ""]
        out += [f"- {d}" for d in report["deviations"]] + [""]

    out += ["## Batch acceptance criteria", "",
            "| Criterion | Threshold | Measured | Status |",
            "| --- | --- | ---: | --- |"]
    for c in report["acceptance"]["criteria"]:
        measured = (_pct(c["measured"]) if isinstance(c["measured"], float)
                    else str(c["measured"]))
        out.append(f"| {c['criterion']} | {c['threshold']} | {measured} | "
                   f"{'PASS' if c['ok'] else 'FAIL'} |")

    y = report["yield"]
    out += ["", "## Yield", "",
            "Denominator is every session Arena shared, unfiltered.", "",
            "| Metric | Value |", "| --- | ---: |",
            f"| Environment yield | {_pct(y.get('environment_yield'))} |",
            f"| Difficulty yield | {_pct(y.get('difficulty_yield'))} |",
            f"| Hard-task yield | {_pct(y.get('hard_task_yield'))} |"]

    out += ["", "## Faithfulness mix", "",
            "| Level | Source | Tasks |", "| --- | --- | ---: |"]
    counts = report["faithfulness"]["counts"]
    for level in ("F3", "F2", "F1", "F0"):
        out.append(f"| {level} | {levels.SOURCE_NAME[level]} | "
                   f"{counts.get(level, 0)} |")
    out.append(f"| unlevelled | missing or not F0-F3 | {counts.get('unlevelled', 0)} |")

    per_env = report.get("tasks_per_environment") or {}
    if per_env:
        out += ["", "## Tasks per environment", "",
                f"Definition: {per_env.get('definition')}. "
                f"{per_env.get('tasks')} task(s) in {per_env.get('environments')} "
                f"environment(s); mean {_num(per_env.get('mean'), 2)}, "
                f"min {per_env.get('min')}, max {per_env.get('max')}; "
                f"{per_env.get('multi_task_environments')} environment(s) hold more "
                "than one task.", "",
                "| Environment (session) | Tasks | Names |", "| --- | ---: | --- |"]
        for env, names in (per_env.get("by_environment") or {}).items():
            out.append(f"| {str(env)[:8]} | {len(names)} | {', '.join(names)} |")

    models = report.get("calibration_models") or {}
    out += ["", "## Tasks", "",
            f"Primary (P): {models.get('primary') or '—'}. "
            f"Secondary (S): {models.get('secondary') or '—'}.", "",
            "| Task | Session | F | pass@1 (P) | pass@4 (P) | mean reward (P) | "
            "pass@1 (S) | pass@4 (S) | mean reward (S) | "
            "Hard | General difficulty | tasks-check | Accepted |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: "
            "| --- | --- | --- | --- |"]
    for t in report["tasks"]:
        (_, primary), (_, secondary) = split_models(t.get("per_model"),
                                                    models.get("primary"))
        primary, secondary = primary or {}, secondary or {}
        hardness = t.get("hardness") or {}
        pass_at_1 = (primary.get("pass_at_1") if primary
                     else hardness.get("primary_pass_at_1"))
        out.append(
            f"| {t.get('short_name') or t['bundle']} "
            f"| {(t.get('session_id') or '—')[:8]} "
            f"| {t.get('faithfulness_level') or '—'} "
            f"| {_num(pass_at_1, 2)} "
            f"| {_num(primary.get('pass_at_4'), 2)} "
            f"| {_num(primary.get('average_reward'))} "
            f"| {_num(secondary.get('pass_at_1'), 2)} "
            f"| {_num(secondary.get('pass_at_4'), 2)} "
            f"| {_num(secondary.get('average_reward'))} "
            f"| {'yes' if hardness.get('hard') else 'no'} "
            f"| {_general_label(t)} "
            f"| {t.get('harbor_tasks_check') or '—'} "
            f"| {'yes' if t.get('accepted') else 'no'} |")

    out += ["", "## Per-model detail", "",
            "| Task | Model | Valid | Void | pass@1 | pass@4 | Mean reward |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for t in report["tasks"]:
        for model, m in (t.get("per_model") or {}).items():
            out.append(f"| {t.get('short_name') or t['bundle']} | {model} "
                       f"| {m.get('episodes_valid')} | {m.get('episodes_void')} "
                       f"| {_num(m.get('pass_at_1'), 2)} "
                       f"| {_num(m.get('pass_at_4'), 2)} "
                       f"| {_num(m.get('average_reward'))} |")

    blocked = [t for t in report["tasks"] if not t.get("accepted")]
    if blocked:
        out += ["", "## Not accepted", ""]
        for t in blocked:
            reasons = (t.get("release_errors") or t.get("acceptance_problems")
                       or t.get("problems") or ["no receipt"])
            out.append(f"- **{t['bundle']}** — {reasons[0]}")

    if report["acceptance"]["problems"]:
        out += ["", "## Batch blockers", ""]
        out += [f"- {p}" for p in report["acceptance"]["problems"]]

    out += ["", "## Trace accounting", "",
            f"{report['trace_accounting']['converted']} converted, "
            f"{report['trace_accounting']['excluded']} excluded, "
            f"{report['trace_accounting']['undispositioned']} undispositioned "
            f"of {report['trace_accounting']['sessions_shared']} shared. "
            "Full rows in `trace-accounting.json`.", ""]
    return "\n".join(out)


def repair_history(bundle: Path) -> dict:
    """What was found and fixed before release, from the retained receipts.

    `verification/qualification-revisions/*/feedback.json` is the qualification
    feedback each revision was rebuilt from; `repair-log.json` the bounded repair
    passes; `repair-disposition.json` a repair that stopped. Unreadable receipts
    are reported, never skipped.
    """
    from .author.contracts import strict_json

    bundle = Path(bundle)
    history = {"revisions": [], "passes": [], "disposition": None, "problems": []}

    def load(path: Path):
        try:
            return strict_json(path.read_text())
        except (OSError, ValueError) as exc:
            history["problems"].append(f"unreadable {path.relative_to(bundle)}: {exc}")
            return None

    feedback = sorted(bundle.glob("verification/qualification-revisions/*/feedback.json"),
                      key=lambda f: (f.stat().st_mtime, f.parent.name))
    for path in feedback:
        doc = load(path)
        if isinstance(doc, dict):
            history["revisions"].append({"revision": path.parent.name,
                                         "findings": list(doc.get("findings") or [])})
    log = bundle / "verification/repair-log.json"
    if log.is_file() and isinstance(doc := load(log), dict):
        history["passes"] = [{"index": p.get("index"), "outcome": p.get("outcome"),
                              "findings": p.get("findings")}
                             for p in doc.get("passes") or [] if isinstance(p, dict)]
    disposition = bundle / "verification/repair-disposition.json"
    if disposition.is_file() and isinstance(doc := load(disposition), dict):
        history["disposition"] = {"reason": doc.get("reason"),
                                  "outstanding_findings": doc.get("outstanding_findings") or []}
    return history


def _clip(text, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def reward_hacking_note(record: dict, repairs: dict | None = None,
                        trajectories: dict | None = None) -> str:
    """Spec: "a short note per environment covering what you tested, found, and
    fixed".

    `repairs` is `repair_history(bundle)`; `trajectories` is the calibration
    trajectory scan `export_runs` performed (None: no scan ran, which the note
    says rather than implying the trajectories were clean).
    """
    out = [f"# Reward hacking — {record.get('short_name') or record['bundle']}", "",
           "What was tested, what it found, and what changed. Probes run through "
           "the exported grader exactly as the container runs it.", "",
           "| Probe | Score | Reading |", "| --- | ---: | --- |"]
    readings = {
        "seed_zero": "the untouched repository: every objective 0, every guardrail 1",
        "golden": "the reference fix applied by its patch",
        "alternative": "an independently written correct fix",
        "empty_deliverable": "the working tree emptied",
        "destructive": "the source deleted",
        "test_tamper": "skip-everything configs, shadow packages and a forged reward planted on the untouched fixture",
        "per_trap": "each mutant (a plausible wrong fix) fails exactly what its trap declares",
        "answer_lookup": "a scan of the agent's environment for the fix, a leftover patch or a git history",
        "isolation": "no tests or solution in the agent image; separate offline verifier",
        "determinism": "the reference graded three times, identically",
    }
    for name, score in sorted((record.get("probes") or {}).items()):
        note = readings.get(name, "")
        if isinstance(score, (int, float)):
            if name in policy.EXPECT_FULL_CREDIT:
                note += (" — expected 1.0" if score >= policy.PROBE_MAX
                         else " — **should score 1.0 and does not**")
            elif name in policy.HACK_PROBES:
                if score >= policy.PROBE_MAX:
                    note += " — **EXPLOITABLE**"
                elif score > policy.PROBE_WARN_ABOVE:
                    note += " — earns most of the reward; still a hack"
        out.append(f"| `{name}` | {_num(score, 2)} | {note} |")

    findings = record.get("reward_hacking_findings") or []
    out += ["", "## Findings", ""]
    unresolved = record.get("reward_hacking_unresolved") or []
    out += ([f"- `{name}` scored full marks. **Unresolved**: this task cannot ship until it is fixed and re-probed."
             for name in findings]
            + [f"- `{name}` is missing, unscored or earns most of the reward. **Unresolved**."
               for name in unresolved]
            or ["- None. Every mandatory probe ran and no attack earned full or most marks."])

    out += ["", "## Calibration trajectories", ""]
    status = (trajectories or {}).get("status")
    if trajectories is None:
        out.append("- Trajectories not inspected: no calibration trajectory scan ran for "
                   "this task, so nothing is claimed about the calibration runs.")
    elif status != "inspected":
        out.append(f"- Trajectories not inspected: {trajectories.get('reason') or status}. "
                   "**Unresolved**: could-not-check is not clean.")
    else:
        models = trajectories.get("models") or {}
        out.append(f"- Scanned every valid trial of {len(models)} calibration campaign(s) "
                   f"({', '.join(sorted(models)) or 'none'}) for test access, writes into "
                   "graded directories, symlinks, undeclared outputs and tampering.")
        for model, result in sorted(models.items()):
            if result.get("error"):
                out.append(f"- `{model}`: the scan failed ({_clip(result['error'])}). **Unresolved**.")
        hits = trajectories.get("hits") or []
        out += ([f"- `{h.get('model')}` trial `{h.get('trial')}`"
                 f"{'/' + str(h['step']) if h.get('step') else ''}: **{h.get('kind')}** — "
                 f"{_clip(h.get('evidence', ''))}. **Unresolved** until adjudicated."
                 for h in hits]
                or ["- No pattern matched in any valid trial. A pattern scan is evidence "
                    "of absence only for the techniques listed."])
        out.append("- Scope of the scan: each shell command is read as bash would run it "
                   "(quote-aware, the working directory followed part by part, `..` resolved, "
                   "a `cd` that may fail read both ways). It cannot see paths computed at "
                   "run time (inside `python -c`, variables, `$(...)`, encoded strings) or a "
                   "working directory left by an earlier command. `/tests` is uploaded only "
                   "for the verifier, so a harness read or write during the agent's run is an "
                   "attempt, never a working exploit; exploits that can work in-run are the "
                   "probes above, the fixture guardrails and the undeclared-output check.")

    out += ["", "## What was fixed", ""]
    repairs = repairs or {}
    fixed = []
    for n, rev in enumerate(repairs.get("revisions") or [], 1):
        found = rev.get("findings") or []
        fixed.append(f"- Qualification revision {n} (`{str(rev.get('revision'))[:8]}`) was "
                     f"rebuilt from {len(found)} finding(s)"
                     + (": " + "; ".join(_clip(f, 160) for f in found[:3]) if found else "")
                     + (f"; and {len(found) - 3} more" if len(found) > 3 else "") + ".")
    for p in repairs.get("passes") or []:
        found = p.get("findings")
        fixed.append(f"- Repair pass {p.get('index')}: {p.get('outcome')}"
                     + (f", {len(found)} finding(s) addressed" if isinstance(found, list) else "")
                     + ".")
    if repairs.get("disposition"):
        d = repairs["disposition"]
        fixed.append(f"- Repair stopped: {_clip(d.get('reason'))}. Outstanding: "
                     + ("; ".join(_clip(f, 160) for f in d["outstanding_findings"][:3]) or "none"))
    fixed += [f"- {p}. **Unresolved**: the repair record cannot be read."
              for p in repairs.get("problems") or []]
    out += fixed or ["- No repair or revision history is retained: nothing is recorded as "
                     "found and fixed before release."]

    surfaces = record.get("untested_surfaces") or []
    out += ["", "## Untested surfaces", ""]
    out += ([f"- {s}" for s in surfaces] if surfaces else
            ["- None declared. That is a claim about what we looked at, not a "
             "proof that nothing else exists."])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Runs and results
# ---------------------------------------------------------------------------

def export_runs(bundle: Path, out: Path) -> dict:
    """Spec, "Deliverables": "Runs and results - full calibration traces and
    scores, including at least one successful trajectory showing prompts,
    turns, tool calls, final artifacts, model name, and per-rubric rewards."

    The evidence exists -- native campaigns retain transcripts, grades and
    request configs -- but it lives outside the bundle under a campaign root,
    bound by path. Arena receives the bundle. So the runs are copied in beside
    it, with an index naming the model, the Harbor trial id and the per-rubric
    reward of each one.

    When no calibration episode scored full marks, the native oracle control
    (the reference solution run through the delivered grader) is exported to
    `runs/reference/` as the successful trajectory, and the index says so.

    A campaign whose retained files no longer verify is listed and NOT copied:
    shipping unverifiable evidence as if it were evidence is worse than
    shipping none, because it cannot be told apart on arrival.

    Each verified campaign is also scanned for reward hacking in its
    trajectories (`attribution.trajectory_hacks`) when that scan exists; when it
    does not, the record says the trajectories were not inspected.
    """
    from . import attribution
    from .author.contracts import strict_json

    bundle, out = Path(bundle), Path(out)
    record = {"schema": "arena-pipeline/runs-export/2", "campaigns": [],
              "rejected": [], "successful_trajectory": None,
              "successful_trajectories": [], "reference_trajectory": None,
              "trajectory_scan": None}
    if out.exists():
        shutil.rmtree(out)                  # produced here only; stale runs are not runs
    index_path = bundle / "verification/native-calibrations.json"
    if not index_path.is_file():
        record["rejected"].append("no native calibration index for this task version")
        return record

    try:
        index = strict_json(index_path.read_text())
    except (OSError, ValueError) as exc:
        record["rejected"].append(f"unreadable calibration index: {exc}")
        return record

    scanner = getattr(attribution, "trajectory_hacks", None)
    scan = ({"status": "inspected", "models": {}, "hits": []} if callable(scanner) else
            {"status": "not inspected", "models": {}, "hits": [],
             "reason": "attribution.trajectory_hacks is unavailable in this pipeline"})
    out.mkdir(parents=True, exist_ok=True)
    for path in index.get("campaigns", []) or []:
        source = Path(path)
        try:
            from .release import calibration_measurement
            with accepted_view(bundle) as view:
                issues, measured = calibration_measurement(
                    view, index, path, policy.SPEC_EPISODES_PER_MODEL)
        except Exception as exc:                  # noqa: BLE001
            record["rejected"].append(f"{source.name}: unverifiable ({exc})")
            continue
        if issues:
            record["rejected"].append(f"{source.name}: {issues[0]}")
            continue

        model = (measured.get("model") or "model").replace("/", "-")
        destination = out / model
        if destination.exists():
            shutil.rmtree(destination)
        # Every run is sent, top-ups included: the primary campaign, then each
        # top-up that replaced its voids, in order.
        parts = [source, *(Path(p) for p in (index.get("topups") or {}).get(str(path), []))]
        copied = []
        for n, part in enumerate(parts):
            target = destination if n == 0 else destination / f"topup-{n}"
            shutil.copytree(part, target,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "package"))
            copied.append((part, target))

        trials = measured.get("trials") or []

        def trial_of(episode):
            ok = isinstance(episode, int) and 0 < episode <= len(trials)
            return trials[episode - 1] if ok else {}

        def exported(trial) -> str | None:
            folder = Path(trial["result_file"]).parent if trial.get("result_file") else None
            for part, target in copied:
                try:
                    return (target / folder.relative_to(part)).relative_to(out).as_posix()
                except (ValueError, AttributeError, TypeError):
                    continue
            return None

        rows = metrics.episode_rewards(measured)
        ordered = sorted(rows.items(), key=lambda kv: (kv[0] is None, kv[0]))
        record["campaigns"].append({
            "model": measured.get("model"),
            "path": destination.name,
            "episodes": {str(k): v for k, v in ordered},
            "trials": {str(k): trial_of(k).get("id") for k, _ in ordered},
            "metrics": metrics.task_metrics(measured),
            "per_check": {step: data.get("checks", {})
                          for step, data in (measured.get("by_step") or {}).items()},
        })
        for episode, reward in ordered:
            if metrics.passed(reward):
                trial = trial_of(episode)
                record["successful_trajectories"].append({
                    "model": measured.get("model"), "trial_id": trial.get("id"),
                    "episode": episode, "reward": reward, "path": exported(trial),
                    "shows": "prompts, turns, tool calls, final artifacts and "
                             "per-rubric rewards, in the retained trial files"})
        if callable(scanner):
            name = measured.get("model") or model
            try:
                hits = list(scanner(bundle, measured) or [])
                scan["models"][name] = {"hits": len(hits), "error": None}
                scan["hits"] += [{**h, "model": name} for h in hits if isinstance(h, dict)]
            except Exception as exc:              # noqa: BLE001
                scan["models"][name] = {"hits": None, "error": f"{type(exc).__name__}: {exc}"}
                scan["status"] = "error"
                scan["reason"] = f"the scan of {name} failed"

    record["successful_trajectory"] = (record["successful_trajectories"] or [None])[0]
    if record["campaigns"]:
        record["trajectory_scan"] = scan
    if not record["successful_trajectories"]:
        record["reference_trajectory"] = _export_reference(bundle, out / "reference", record)
        record["note"] = (
            "No calibration episode scored full marks. runs/reference/ holds the native "
            "oracle control -- the reference solution run through the delivered grader -- "
            "which is the task's successful trajectory."
            if record["reference_trajectory"] else
            "No calibration episode scored full marks and the reference trajectory could "
            "not be exported (see `rejected`); this task ships no successful trajectory.")
    if not any(out.iterdir()):
        out.rmdir()
    return record


def _export_reference(bundle: Path, out: Path, record: dict) -> dict | None:
    """Copy the verified native oracle run into `out`; None (and why) otherwise."""
    from .artifacts import identity
    from .author.contracts import strict_json
    from .evidence import native_evidence

    controls = bundle / "verification/native-controls.json"
    if not controls.is_file():
        record["rejected"].append("reference: no native controls for this task version")
        return None
    try:
        index = strict_json(controls.read_text())
        with accepted_view(bundle) as view:
            if index.get("artifact_sha256") != identity(view):
                record["rejected"].append("reference: native controls refer to another task version")
                return None
            issues, measured = native_evidence(view, Path(index["oracle"]), "oracle")
    except Exception as exc:                      # noqa: BLE001
        record["rejected"].append(f"reference: unverifiable ({type(exc).__name__}: {exc})")
        return None
    trials = measured.get("trials") or []
    if issues or not trials or not all(t.get("valid") and metrics.passed(t.get("reward"))
                                        for t in trials):
        record["rejected"].append("reference: the oracle run does not verify at full marks"
                                  + (f" ({issues[0]})" if issues else ""))
        return None
    source = Path(index["oracle"])
    shutil.copytree(source, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "package"))
    trial = trials[0]
    folder = Path(trial["result_file"]).parent if trial.get("result_file") else None
    try:
        where = (Path(out.name) / folder.relative_to(source)).as_posix()
    except (ValueError, AttributeError, TypeError):
        where = out.name
    return {"model": "oracle (reference solution)", "trial_id": trial.get("id"),
            "reward": trial.get("reward"), "path": where,
            "shows": "the reference solution's commands, final artifacts and per-rubric "
                     "rewards through the delivered grader"}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build(root: Path, sessions: list[str], excluded: dict[str, str] | None = None,
          out: Path | None = None, index: Path | None | str = "auto") -> dict:
    """Assemble the batch report and write the per-environment notes.

    `index` is the corpus index whose session count the shared list must
    match; "auto" reads `config.ARTIFACTS/index.json` when it exists. With no
    readable index the comparison cannot be made, and trace accounting says so
    as a problem rather than passing.

    Order matters: runs are exported, trajectories scanned and the notes
    written BEFORE the batch criteria are computed, because Reviewable needs
    the exported runs and the privacy scan needs the bundle exactly as shipped.
    """
    from .artifacts import atomic_json
    from .config import ARTIFACTS, bundle_paths

    root = Path(root)
    out = Path(out) if out else root
    if index == "auto":
        index = ARTIFACTS / "index.json"
    bundles = bundle_paths(root)
    records = [task_record(b) for b in bundles]

    for bundle, record in zip(bundles, records):
        runs = export_runs(bundle, bundle / "runs")
        if runs["campaigns"] or runs["reference_trajectory"]:
            atomic_json(bundle / "runs/index.json", runs)
        exported_models = [c.get("model") for c in runs["campaigns"]]
        uncovered = [m for m in policy.resolve_calibration_models()
                     if not any(levels.is_primary_model(x, m) for x in exported_models)]
        record["runs"] = {
            "campaigns": len(runs["campaigns"]), "models": exported_models,
            "rejected": runs["rejected"],
            "successful_trajectory": bool(runs["successful_trajectories"]
                                          or runs["reference_trajectory"]),
            "successful_trials": [t["trial_id"] for t in runs["successful_trajectories"]],
            "reference_trajectory": bool(runs["reference_trajectory"]),
            "models_not_exported": uncovered,
            "exported": (bool(runs["campaigns"]) and not runs["rejected"] and not uncovered
                         and bool(runs["successful_trajectories"]
                                  or runs["reference_trajectory"]))}
        scan = runs["trajectory_scan"]
        record["trajectory_scan"] = scan
        record["trajectory_unresolved"] = (
            [] if scan is None else
            [f"{h.get('model')}: {h.get('trial')}{'/' + str(h['step']) if h.get('step') else ''}: "
             f"{h.get('kind')}" for h in scan["hits"]]
            + ([f"trajectory scan: {scan.get('reason') or scan['status']}"]
               if scan["status"] != "inspected" else []))
        (bundle / "REWARD-HACKING.md").write_text(
            reward_hacking_note(record, repair_history(bundle), scan), encoding="utf-8")
        # Last, over the whole bundle as it ships -- runs and notes included.
        record["privacy_unresolved"] = open_privacy(bundle)
        if record["privacy_unresolved"]:
            record["problems"].append("privacy: " + privacy_summary(record["privacy_unresolved"]))

    converted: dict[str, list[str]] = {}
    for record in records:
        session = record.get("session_id")
        if session:
            converted.setdefault(session, []).append(
                record.get("short_name") or record["bundle"])

    expectation = dump_expectation(Path(index) if index else None)
    accounting = trace_accounting(sessions, converted, excluded or {}, **expectation)
    accounting["dump_index"] = str(index) if expectation else None
    if expectation.get("expected_count") is None and not expectation.get("expected_sessions"):
        accounting["problems"].append(
            f"no usable dump index ({index or 'none given'}): the shared session list "
            "cannot be checked against what the dump holds, so 100% of it may be 100% "
            "of the wrong denominator")
        accounting["ok"] = False
    mix = levels.batch_mix([{"level": r.get("faithfulness_level")} for r in records])
    acceptance = batch_acceptance(records, accounting)
    difficulty = metrics.batch_difficulty(records)
    acceptance["problems"] = acceptance["problems"] + difficulty.get("problems", [])
    acceptance["ok"] = acceptance["ok"] and difficulty.get("ok", False)

    primary, secondary = policy.resolve_calibration_models()
    report = {
        "schema": "arena-pipeline/batch-report/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environments": len(records),
        "tasks_per_environment": tasks_per_environment(records),
        "calibration_models": {"primary": primary, "secondary": secondary},
        "deviations": policy.model_deviations() + sorted(
            {d for r in records for d in r.get("deviations", [])}),
        "acceptance": acceptance,
        "difficulty": difficulty,
        "faithfulness": mix,
        "yield": yield_metrics(sessions, records),
        "trace_accounting": accounting,
        "tasks": records,
    }

    atomic_json(out / "batch-report.json", report)
    atomic_json(out / "trace-accounting.json", accounting)
    (out / "BATCH_REPORT.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Assemble the batch deliverable")
    ap.add_argument("root", type=Path, help="delivery tree of environments")
    ap.add_argument("--sessions", type=Path, required=True,
                    help="JSON list of every session ID Arena shared")
    ap.add_argument("--excluded", type=Path,
                    help="JSON object mapping excluded session -> reason")
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--index", default="auto",
                    help="corpus index.json the shared list must match "
                         "(default: the artifacts root's; 'none' to skip)")
    args = ap.parse_args(argv)

    sessions = json.loads(args.sessions.read_text())
    excluded = json.loads(args.excluded.read_text()) if args.excluded else {}
    index = None if args.index == "none" else (
        "auto" if args.index == "auto" else Path(args.index))
    report = build(args.root, sessions, excluded, args.out, index)
    print(render_markdown(report))
    return 0 if report["acceptance"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
