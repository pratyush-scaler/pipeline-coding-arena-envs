"""Bounded candidate revision from verified task defects, never provider failures."""
from __future__ import annotations

import uuid
from pathlib import Path

from . import policy
from .artifacts import atomic_json, freeze, identity
from .author.contracts import strict_json


def feedback(bundle: Path, result: dict) -> list[str]:
    """Return actionable findings without passing solver answers to the author."""
    if result.get("artifact_sha256") != identity(bundle) or result.get("release_ready"):
        return []
    findings = []
    from .run import run_bundle
    gates = run_bundle(bundle, Path(__file__).resolve().parents[1])
    # Infrastructure faults should be repaired by an operator, not by changing
    # the benchmark until an invalid run looks like model failure.
    if gates["verdict"] == "unverified":
        return []
    for gate in gates["results"]:
        findings.extend(f"{gate['gate']}: {row['code']}: {row.get('message', '')}"
                        for row in gate["blockers"])
    for error in result.get("errors", []):
        if error.startswith(("controls:", "provenance:")) and "unverified" not in error:
            findings.append(error)
    path = bundle / "verification/semantic-review.json"
    if path.is_file():
        report = strict_json(path.read_text())
        if report.get("artifact_sha256") == identity(bundle):
            from .review import review_problems, semantic_rejection
            problems = review_problems(bundle, report)
            # Only semantic rejection is repairable; stale/tampered execution
            # receipts must be rerun rather than used as design feedback.
            if semantic_rejection(problems):
                from .author.contracts import contained
                doc = strict_json(contained(bundle, report["work_dir"]).joinpath("deliverables/review.json").read_text())
                findings.extend(f"Semantic review {row.get('id')}: {row.get('reason')}"
                                for row in doc.get("checks", []) if row.get("verdict") != "pass")
                for key in ("ambiguities", "substantive_gaps", "shortcut_risks", "privacy_findings"):
                    if doc.get(key):
                        findings.append(f"Semantic review {key}: {doc[key]}")
    # Calibration results are deliberately absent: policy.REPAIR_FEEDBACK_EXCLUDES.
    # Feeding measured pass counts or band targets back to the author makes a
    # hard task easier because attempts failed it.
    return findings


def design_inputs(bundle: Path) -> tuple[dict, dict, dict]:
    """The retained spec, ledger and designer row for revising a built bundle."""
    spec = strict_json((bundle / "authoring/spec.json").read_text())
    ledger = strict_json((bundle / "provenance/ledger.json").read_text())
    row = {**spec["source"], "category": ledger.get("category", spec.get("category")),
           "classification": {"domain": spec.get("category", "professional"),
                              "usable_moments": strict_json((bundle / "authoring/traps.json").read_text()).get("usable_corrections", 0)}}
    return spec, ledger, row


def rebuild_ledger(bundle: Path, previous: dict) -> dict:
    """Re-measure fixture provenance after a revision, keeping the source tier."""
    from . import naming
    from .ledger import faithfulness
    source = strict_json((bundle / "authoring/spec.json").read_text())["source"]
    ledger = faithfulness.build(bundle, source["session_id"], source["seed_index"])
    for key in ("category", "category_inputs"):
        if key in previous:
            ledger[key] = previous[key]
    ledger = faithfulness.with_level(bundle, ledger)
    atomic_json(bundle / "provenance/ledger.json", ledger)
    naming.apply(bundle, ledger["faithfulness_level"])
    return ledger


def revise(bundle: Path, findings: list[str], max_steps: int | None = None) -> dict:
    """Called with the bundle lock held. Archive the prior version before writing."""
    from .author import agent_designer, build, extract, probes
    from . import checkpoint as progress
    state = progress.read(bundle)
    retained = progress.resumable(bundle) if state.get('phase') == 'qualification-repair' else None
    if state.get('phase') == 'qualification-repair' and not retained:
        raise ValueError('Interrupted qualification repair has no safe checkpoint')
    spec, ledger, row = design_inputs(bundle)
    source = spec["source"]
    if retained:
        from .author.contracts import contained
        archive = contained(bundle, state['repair_archive'])
        sha = state['repair_previous_sha256']
    else:
        archive = bundle / "verification/qualification-revisions" / uuid.uuid4().hex
        sha = freeze(bundle, archive / "task")
        atomic_json(archive / "feedback.json", {"artifact_sha256": sha, "findings": findings})
        for name in ("release.json", "qualification.json", "semantic-review.json", "blind-solve.json",
                     "native-controls.json", "native-calibrations.json", "probes.json"):
            path = bundle / "verification" / name
            if path.is_file():
                atomic_json(archive / name, strict_json(path.read_text()))
        progress.submitted(bundle, archive, spec)
        progress.update(bundle, stage='repair', phase='qualification-repair', feedback=findings,
            repair_archive=archive.relative_to(bundle).as_posix(), repair_previous_sha256=sha)
    extraction = extract.extract(source["session_id"], source["seed_index"])
    if retained and retained['stage'] in {'submitted', 'checking'}:
        candidate = progress.submitted_spec(bundle, retained)
    else:
        author = retained.get('authoring_session') if retained else None
        options = ({'resume_session': author, 'max_steps': author['remaining_steps']}
                   if author else {'initial_spec': spec,
                       'max_steps': max_steps if max_steps is not None else policy.REVISION_MAX_STEPS})
        candidate = agent_designer.design(row, extraction, bundle, **options, revision=True,
            feedback=[policy.PATCH_FIRST, *findings])
    build.build(candidate, bundle)
    traps = strict_json((bundle / "authoring/traps.json").read_text())
    traps["usable_corrections"] = row["classification"]["usable_moments"]
    atomic_json(bundle / "authoring/traps.json", traps)
    rebuild_ledger(bundle, ledger)
    progress.update(bundle, stage='checking',
        workspace_files=progress.evidence_inventory(bundle, progress.WORKSPACE))
    report = probes.run(bundle)
    atomic_json(bundle / "verification/probes.json", report)
    progress.update(bundle, stage='qualifying', phase=None)
    return {"previous_sha256": sha, "artifact_sha256": identity(bundle),
            "archive": archive.relative_to(bundle).as_posix()}


# ---------------------------------------------------------------------------
# Loop control
# ---------------------------------------------------------------------------
# `revise` above is the mechanism. This is the governor, and it is the half that
# was missing.
#
# Policy in the batch that motivated it was one design attempt and a default
# repair budget of zero. Of 86 candidates, 47 were discarded holding findings
# that named the exact file and the exact line -- "candidate-spec.json source
# case sc_activity_floor_50 is wrong: the unchanged 30% journaling ceiling makes
# the minimum 12, not 8". That is a fix, not a verdict, and throwing the task
# away burned a source family to avoid an edit.
#
# The opposite failure is the loop that never ends: an author and a reviewer
# disagreeing forever at real cost per round. So repair is bounded by a budget
# AND by an invariant, and the invariant is the part that matters -- a budget
# alone still permits two futile passes.
#
#   MONOTONE   the finding set must strictly shrink each pass, and no finding
#              may survive a pass that was told to fix it. A repeated finding
#              means the author cannot see what the reviewer sees, and another
#              pass will not change that.
#
# Both conditions are terminal, not advisory. Terminal is a disposition with
# evidence, which is different from the silent discard it replaces.

import re

LOG = "verification/repair-log.json"


def fingerprint(finding: str) -> str:
    """A stable identity for a finding, so 'the same one again' is decidable.

    Normalises whitespace and the volatile parts of a rendered message. Numbers
    are deliberately *kept*: "the minimum is 12, not 8" and "the minimum is 14,
    not 8" are different findings about the same check, and collapsing them
    would let an author cycle between two wrong answers forever.
    """
    text = re.sub(r"\s+", " ", (finding or "").strip().lower())
    text = re.sub(r"\b(work_dir|campaign|episode|rollout)[-_ ]?[0-9a-f]{6,}\b", "", text)
    return text[:400]


def history(bundle: Path) -> dict:
    path = Path(bundle) / LOG
    if not path.is_file():
        return {"schema": "arena-pipeline/repair-log/1", "passes": []}
    # Fail closed. Returning an empty log here silently restored the whole
    # budget to any candidate whose log was damaged.
    try:
        log = strict_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"repair log is unreadable, so the repair budget cannot be counted: {exc}") from exc
    if (not isinstance(log, dict) or log.get("schema") != "arena-pipeline/repair-log/1"
            or not isinstance(log.get("passes"), list)):
        raise ValueError("repair log is malformed, so the repair budget cannot be counted")
    return log


def passes_used(bundle: Path) -> int:
    return len(history(bundle).get("passes", []))


def passes_granted(bundle: Path) -> int:
    """Extra passes the user granted explicitly, recorded beside the spent ones
    (`pipeline/reopen.py`, user 2026-10-04). Never inferred, never reset."""
    return sum(int(g.get("passes", 0)) for g in history(bundle).get("grants", []) or []
               if isinstance(g, dict) and type(g.get("passes")) is int and g["passes"] > 0)


#: What a finding may cite, per policy.REQUIRE_FINDING_CITATION ("a finding must
#: name a file, check or step"): a bundle path, the gate check that raised it
#: (`trap.beatable: ...`), the reviewed check (`Semantic review o2_record: ...`),
#: or a step (`01-task`, `step 2`).
_CITATION = re.compile(
    r"[\w.-]+/[\w./-]*"                               # a bundle path
    r"|[\w-]+\.(?:json|jsonl|md|py|toml|csv|txt|ts|tsx|js|jsx|mjs|go|rs|java|php|c|h|cpp|rb|kt|swift|lua|sh|sql|html|css|yaml|yml|patch)\b"      # a file
    r"|\b(?:o|g|t|m)_[a-z0-9_]+\b"                       # a check, trap or mutant id
    r"|^[a-z_]+\.[a-z_]+:"                              # a gate check
    r"|^Semantic review [\w.-]+:"                       # a reviewed check
    r"|\b\d{2}-[a-z][\w-]*|\bstep[- ]?\d|\bchk_", re.M)


def cites_location(finding: str) -> bool:
    return bool(_CITATION.search(str(finding)))


def admissible(bundle: Path, findings: list[str]) -> tuple[bool, str]:
    """May we spend another repair pass on these findings?

    Returns (ok, reason). A refusal is a disposition: it is recorded, and the
    candidate stops with its findings attached rather than vanishing.
    """
    from . import policy

    log = history(bundle)
    previous = log.get("passes", [])

    if not findings:
        return False, "nothing to repair"

    if policy.REQUIRE_FINDING_CITATION:
        vague = [f for f in findings if not cites_location(f)]
        if len(vague) == len(findings):
            return False, ("no finding cites a concrete artifact location, so "
                           "none of them is actionable. A repairable finding "
                           "names the file, check or step it is about")

    if len(previous) >= policy.MAX_REPAIR_PASSES + passes_granted(bundle):
        return False, (f"repair budget exhausted ({policy.MAX_REPAIR_PASSES} passes). "
                       "Remaining findings are recorded against this candidate")

    if not policy.REQUIRE_MONOTONE_REPAIR or not previous:
        return True, ""

    # A pass charged without its findings (see checkpoint.consume_repair)
    # counts against the budget but cannot anchor the comparison.
    anchored = [p for p in previous if isinstance(p.get("fingerprints"), list)]
    if not anchored:
        return True, ""
    now = {fingerprint(f) for f in findings}

    # A finding given to ANY earlier pass that comes back is terminal.
    seen = set().union(*(set(p["fingerprints"]) for p in anchored))
    repeated = sorted(now & seen)
    if repeated:
        return False, (
            f"{len(repeated)} finding(s) survived a repair pass that was given "
            f"them explicitly: {repeated[0][:160]!r}. The author is not seeing "
            "what the reviewer sees, and a further pass costs money to learn "
            "the same thing. Stopping.")

    # Shrinking is measured within one grading stage. The semantic review runs
    # only once the gate chain is green, so its first findings arrive after a
    # repair that cleared every gate finding -- complete progress, not churn.
    # Comparing its count with the gates' (12 gate findings, all fixed, then
    # 13 review findings) refused the budgeted repair of a candidate that had
    # just done everything asked of it (e2e build, 2026-09-26).
    stages = {finding_stage(f) for f in now}
    if not policy.REVIEW_FINDINGS_MUST_SHRINK:
        # a fresh exhaustive review is not a count of the last one (policy.REVIEW_FINDINGS_MUST_SHRINK)
        stages.discard("semantic-review")
        now = {f for f in now if finding_stage(f) != "semantic-review"}
        if not stages:
            return True, ""
    earlier = [p for p in anchored if stages & {finding_stage(f) for f in p["fingerprints"]}]
    if not earlier:
        return True, ""                    # this stage's first findings
    before = [f for f in earlier[-1]["fingerprints"] if finding_stage(f) in stages]
    if len(now) >= len(before):
        return False, (
            f"the finding set did not shrink ({len(before)} -> {len(now)} "
            f"{'/'.join(sorted(stages))} findings). Repair is replacing defects "
            "rather than removing them, which is churn and does not terminate. Stopping.")

    return True, ""


def finding_stage(finding: str) -> str:
    """The grading stage that raised a finding (or its fingerprint).

    The semantic review's findings are rendered `Semantic review <id>: ...`
    (orchestrate._review_candidate, repair.feedback); the ship-time sweep's are
    `ship.readiness: ...` and, like the review, it runs only once the gate chain
    is green (orchestrate._build_one), so its first findings follow a repair that
    cleared every gate finding (ship15 2026-10-12: four builds went leak.sweep 1
    -> ship.readiness 5..15 and were refused as churn). Everything else comes from
    the gate chain -- static gates, probes and executed controls.
    """
    text = (finding or "").strip().lower()
    if text.startswith("semantic review "):
        return "semantic-review"
    return "ship-readiness" if text.startswith("ship.readiness:") else "gates"


def _identity_if_available(bundle: Path) -> str | None:
    """The task hash, or None mid-repair.

    A repair pass is recorded precisely when the bundle is being rewritten, so
    it can be momentarily incomplete and `identity` refuses to hash it. The
    governor must still record the pass: a log that cannot be written during a
    repair cannot bound one, and losing the pass would silently restore the
    budget. Recording `null` is honest about what was not known.
    """
    from .artifacts import identity

    try:
        return identity(Path(bundle))
    except (ValueError, OSError):
        return None


def record_pass(bundle: Path, findings: list[str] | None, outcome: str,
                detail: dict | None = None) -> dict:
    """Append a repair pass to the log. Called whether or not it succeeded."""
    from .artifacts import atomic_json

    log = history(bundle)
    log["passes"].append({
        "index": len(log["passes"]) + 1,
        "artifact_sha256": _identity_if_available(bundle),
        "findings": findings,
        "fingerprints": (None if findings is None
                         else sorted({fingerprint(f) for f in findings})),
        "outcome": outcome,
        **(detail or {}),
    })
    atomic_json(Path(bundle) / LOG, log)
    return log


def disposition(bundle: Path, findings: list[str], reason: str) -> dict:
    """Terminal state for a candidate repair cannot help.

    Deliberately not a deletion. `gated_out` is not `failed`, and a candidate
    stopped by the governor has said something about our authoring that a
    silently discarded one has not.
    """
    from .artifacts import atomic_json

    try:
        used = passes_used(bundle)
    except ValueError:
        used = None                         # the log itself is the problem
    record = {"schema": "arena-pipeline/repair-disposition/1",
              "artifact_sha256": _identity_if_available(bundle),
              "passes_used": used,
              "outstanding_findings": findings,
              "reason": reason,
              "repairable": False}
    atomic_json(Path(bundle) / "verification/repair-disposition.json", record)
    return record
