"""Arena's faithfulness levels, F0-F3, assigned from measured evidence.

Spec, "Monitoring metrics", Faithfulness: "Tag each environment and task by how
much was preserved from the source trace."

This workspace had its own four-way taxonomy -- source tiers 1-4 and the folder
names rich/strong/fixture-led/thin -- built on how rich the *source trace* was.
That is a useful operational ordering (which traces to attempt first) but it is
not what Arena asked to be told, and two of Arena's six batch criteria are
uncomputable without F0-F3: the faithfulness mix, and two of the three yield
metrics. Worse, `F1`/`F2`/`F3` already meant *pipeline stages* in this codebase
(extract, design, build), so the same three tokens carried two unrelated
meanings in one repository.

The distinction that matters: source tier is an input property, measured before
authoring. Faithfulness level is an output property, measured after -- a rich
trace whose fixture the author chose to reconstruct is an F2 task, not an F3
one, and saying otherwise would be the ledger lying about where the material
came from. So levels are assigned from the built bundle, never from the tier.

Arena's definitions, and the two axes each one turns on:

    F3 Replay     enough session content survives to build the task directly;
                  fixture and answer key similar or identical to trace files;
                  grading from what the user actually corrected.
                  -> high fixture fidelity AND trace-derived grading.

    F2 Scenario   original files unavailable, so the fixture is reconstructed
                  or invented, but scenario, failure mode and grading criteria
                  still come from the trace.
                  -> fixture may be authored; grading still trace-derived.

    F1 Pattern    fully synthetic world; the only thing preserved is the
                  trace's failure mode, and calibration must show models
                  failing the same way.
                  -> neither axis survives, but the failure mode is reproduced,
                     and that reproduction is an empirical claim this module
                     refuses to grant without calibration evidence.

    F0 Inspired   nothing recoverable; invented whole. Delivered under
                  F0-inspired/ with its trace evidence.
"""
from __future__ import annotations

from .. import policy

#: Arena's level source names, for the report.
SOURCE_NAME = {level: meta["source"]
               for level, meta in policy.FAITHFULNESS_LEVELS.items()}

#: Delivery folder per level. The folder *is* the faithfulness claim, so a
#: reader sees the batch's mix from the directory listing without opening a
#: ledger -- which is the property the old category folders were reaching for.
DELIVERY_DIR = policy.DELIVERY_DIRS

#: The thresholds are policy's (POLICY.md, "Faithfulness"); kept as names here
#: because the level reasons read better with them.
REPLAY_FIDELITY = policy.FAITHFULNESS_FIDELITY_FLOOR["F3"]
SCENARIO_FIDELITY = policy.FAITHFULNESS_FIDELITY_FLOOR["F2"]
REPLAY_TRACE_DERIVED = policy.FAITHFULNESS_TRACE_DERIVED_FLOOR["F3"]
SCENARIO_TRACE_DERIVED = policy.FAITHFULNESS_TRACE_DERIVED_FLOOR["F2"]


def _pct(value) -> str:
    return "an unknown share of" if value is None else f"{value:.0%} of"


def _trap_provenance(bundle) -> dict:
    from ..author.contracts import strict_json

    path = bundle / "authoring/traps.json"
    if not path.is_file():
        return {"total": 0, "trace_derived": 0, "share": None}
    traps = strict_json(path.read_text()).get("traps", []) or []
    derived = sum(1 for t in traps if t.get("provenance") == "trace_derived")
    return {"total": len(traps), "trace_derived": derived,
            "share": (derived / len(traps)) if traps else None}


def _correction_grounding(bundle) -> dict:
    """Did anything in this task come from a moment the user objected to?"""
    from ..author.contracts import strict_json

    path = bundle / "authoring/spec.json"
    if not path.is_file():
        return {"corrections_cited": 0}
    spec = strict_json(path.read_text())
    cited = {t.get("derived_from") for t in (spec.get("traps") or [])
             if t.get("provenance") == "trace_derived" and t.get("derived_from")}
    moments = spec.get("source", {}).get("moments") or []
    return {"corrections_cited": len(cited), "corrections_available": len(moments)}


def assign(bundle, ledger: dict, calibration_reproduces_failure: bool | None = None) -> dict:
    """Assign one bundle's faithfulness level from its retained evidence.

    `calibration_reproduces_failure` is only consulted for an F1 candidate,
    where Arena's definition makes it a requirement rather than a nicety:
    "calibration runs must show models failing the same way". None means not yet
    measured, which keeps the task out of F1 rather than granting it.
    """
    fidelity = ledger.get("faithfulness_by_bytes")
    exact = ledger.get("exact_copy_by_bytes")
    traps = _trap_provenance(bundle)
    grounding = _correction_grounding(bundle)
    derived_share = traps["share"]

    reasons = []

    def level_f3():
        if fidelity is None or fidelity < REPLAY_FIDELITY:
            reasons.append(
                f"fixture fidelity {fidelity if fidelity is None else f'{fidelity:.0%}'} "
                f"is below the {REPLAY_FIDELITY:.0%} a replay claims")
            return False
        if derived_share is None or derived_share < REPLAY_TRACE_DERIVED:
            reasons.append(
                f"{'no traps' if derived_share is None else f'{derived_share:.0%} of traps'} "
                f"cite an actual correction; a replay grades from what the user "
                f"corrected, so it needs {REPLAY_TRACE_DERIVED:.0%}")
            return False
        if not grounding["corrections_cited"]:
            reasons.append("no user correction is cited anywhere in the task")
            return False
        return True

    def level_f2():
        # "the fixture is reconstructed or invented, but scenario, failure mode
        # and grading criteria still come from the trace."
        #
        # Two independent ways the trace survives into a scenario task, and
        # either is sufficient. An earlier version of this function required
        # trace-derived traps and nothing else, which classified a task whose
        # fixture was 96.7% lifted from the session as F0 -- "nothing
        # recoverable from the trace" -- because its traps were authored. The
        # documents were the trace's; the world was the trace's. Only the
        # difficulty had been invented, which the spec explicitly permits.
        #
        # The decisive check is against F1, which is defined as a *fully
        # synthetic world*. A fixture largely lifted from the session is not a
        # synthetic world, so it cannot sit below F2 on Arena's own ladder.
        # What separates F2 from F1 is the *scenario*, not the failure mode.
        # F1 preserves the failure mode alone, so a cited correction cannot by
        # itself lift a fully synthetic world to F2 -- that is precisely the
        # case Arena defines as F1, and treating it as F2 would both overstate
        # the task and make F1 unreachable.
        if fidelity is not None and fidelity >= SCENARIO_FIDELITY:
            return True                    # the world and its documents are the trace's
        if derived_share is not None and derived_share >= SCENARIO_TRACE_DERIVED:
            return True                    # the grading criteria are the trace's
        reasons.append(
            f"the fixture recovers {_pct(fidelity)} of its bytes from the session "
            f"(a scenario task needs {SCENARIO_FIDELITY:.0%}), "
            f"{'no traps are' if not derived_share else f'{derived_share:.0%} of traps are'} "
            f"trace-derived, and no user correction is cited. Nothing locates "
            "this task in the session's situation")
        return False

    def level_f1():
        # "Fully synthetic world. The only thing preserved is the trace's
        # failure mode, and calibration runs must show models failing the same
        # way." The empirical clause is a requirement, not a nicety.
        if not grounding["corrections_cited"] and not traps["trace_derived"]:
            reasons.append("no failure mode is traceable to the session")
            return False
        if calibration_reproduces_failure is not True:
            reasons.append(
                "F1 requires calibration runs showing models failing the same "
                "way as the trace; that evidence is "
                + ("absent" if calibration_reproduces_failure is None
                   else "present but does not reproduce the failure"))
            return False
        return True

    if level_f3():
        level = "F3"
    elif level_f2():
        level = "F2"
    elif level_f1():
        level = "F1"
    else:
        level = "F0"

    record = {
        "schema": "arena-pipeline/faithfulness-level/1",
        "level": level,
        "level_source": SOURCE_NAME[level],
        "definition": policy.FAITHFULNESS_LEVELS[level]["definition"],
        "delivery_dir": DELIVERY_DIR.get(level),
        "measured": {
            "fixture_fidelity_by_bytes": fidelity,
            "exact_copy_by_bytes": exact,
            "traps_total": traps["total"],
            "traps_trace_derived": traps["trace_derived"],
            "trace_derived_share": derived_share,
            "corrections_cited": grounding["corrections_cited"],
            "calibration_reproduces_failure": calibration_reproduces_failure,
        },
        # Why it is not the level above. Kept because "why is this F2 and not
        # F3" is the first question a reviewer asks, and rebuilding the answer
        # from four artefacts is how a reviewer stops asking.
        "why_not_higher": reasons,
        "source_tier": ledger.get("category"),
        "target_level": policy.target_level(ledger.get("category")),
    }
    return record


def is_primary_model(name, primary: str | None = None) -> bool:
    """Is a measured model id the primary calibration model? Release's rule."""
    primary = primary or policy.resolve_calibration_models()[0]
    return bool(name) and primary.split("/")[-1] in str(name)


def failure_mode_checks(bundle) -> set[str]:
    """The checks the trace's failure mode drives.

    A trap is trace-derived when it cites the user correction it came from;
    the checks it names are where that failure shows up in a grade. Read from
    both the trap register and the spec, because either may carry the trap.
    """
    from pathlib import Path

    from ..author.contracts import strict_json

    checks: set[str] = set()
    for rel in ("authoring/traps.json", "authoring/spec.json"):
        path = Path(bundle) / rel
        if not path.is_file():
            continue
        for trap in strict_json(path.read_text()).get("traps") or []:
            if trap.get("provenance") == "trace_derived":
                checks.update(c for c in trap.get("checks") or [] if isinstance(c, str))
    return checks


def calibration_reproduces_failure(bundle, primary: str | None = None) -> bool | None:
    """F1's empirical clause, read from retained native calibration.

    True when, in a campaign on the primary model whose evidence re-verifies
    against this task version, at least one valid episode FAILS a check the
    trace's failure mode drives. False when such episodes exist and every one
    of them passed those checks. None when there is nothing to read: no
    trace-derived check, no index, an index bound to another task version, no
    verifying primary campaign, or no valid episode in which a failure-mode
    check was attempted -- `gated_out` is not `failed`, and a void is not an
    episode.

    Must be called on the bundle the calibration measured (the working tree,
    before delivery rewrites its name and level).
    """
    from pathlib import Path

    from ..artifacts import identity
    from ..author.contracts import strict_json

    bundle = Path(bundle)
    checks = failure_mode_checks(bundle)
    index_path = bundle / "verification/native-calibrations.json"
    if not checks or not index_path.is_file():
        return None
    try:
        index = strict_json(index_path.read_text())
        if index.get("artifact_sha256") != identity(bundle):
            return None
    except (OSError, ValueError):
        return None

    from ..evidence import native_evidence

    measured_any = False
    for path in index.get("campaigns") or []:
        try:
            issues, measured = native_evidence(bundle, Path(path), "solver")
        except Exception:                          # noqa: BLE001 -- unverifiable
            continue
        if issues or not is_primary_model(measured.get("model"), primary):
            continue
        for row in measured.get("rollouts") or []:
            if row.get("zero_class") == "void":
                continue
            states = [(row.get("checks") or {}).get(c) for c in checks]
            if "failed" in states:
                return True
            if "passed" in states:
                measured_any = True
    return False if measured_any else None


def assign_measured(bundle, ledger: dict) -> dict:
    """`assign`, reading calibration evidence when only F1 is left to decide.

    The reproduction check re-verifies native campaigns, so it runs only for a
    task that measured below F2 -- the one case where `assign` consults it.
    """
    record = assign(bundle, ledger)
    if record["level"] == "F0":
        record = assign(bundle, ledger, calibration_reproduces_failure(bundle))
    return record


def problems(record: dict) -> list[str]:
    """Release blockers arising from the faithfulness level alone."""
    if record["level"] == "F0" and not policy.SPEC_F0_ALLOWED:
        return ["policy.SPEC_F0_ALLOWED is off for F0"]
    return []


def batch_mix(records: list[dict]) -> dict:
    """Spec batch criterion: faithfulness mix, >=50% of tasks at F2+.

    A task whose level is missing or is not one of F0-F3 is `unlevelled`: it
    stays in the denominator (it is not F2+) and fails the criterion, because a
    mix computed over only the tasks that happen to carry a valid tag passes by
    omission.
    """
    if not records:
        return {"tasks": 0, "counts": {}, "f2_plus_share": None, "ok": False,
                "problems": ["no task carries a faithfulness level"]}
    known = ("F3", "F2", "F1", "F0")
    counts = {level: sum(1 for r in records if r.get("level") == level)
              for level in known}
    counts["unlevelled"] = sum(1 for r in records if r.get("level") not in known)
    total = len(records)
    f2_plus = counts["F3"] + counts["F2"]
    share = f2_plus / total
    issues = []
    if share < policy.SPEC_F2_PLUS_MIN:
        issues.append(
            f"{share:.0%} of tasks are F2 or better; the batch criterion is "
            f"{policy.SPEC_F2_PLUS_MIN:.0%}. Reconstructing a fixture is fine; "
            "inventing the grading criteria is what drops a task below F2")
    if counts["F0"] and not policy.SPEC_F0_ALLOWED:
        issues.append(f"{counts['F0']} F0 tasks while policy.SPEC_F0_ALLOWED is off")
    if counts["unlevelled"]:
        issues.append(f"{counts['unlevelled']} of {total} tasks carry no faithfulness "
                      "level in F0-F3 (missing or unrecognised); they count as unlevelled")
    return {"tasks": total, "counts": counts, "f2_plus": f2_plus,
            "f2_plus_share": share, "ok": not issues, "problems": issues}
