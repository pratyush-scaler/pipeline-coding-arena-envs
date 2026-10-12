"""Concurrent batch driver -- the whole pipeline over many seeds at once.

The flow, and where it stops:

    1 select     the seeds, from the classified candidates (`arena select`)
    2 author     every seed, concurrently, to a complete and deliberately hard
                 environment: the designer sees the whole trace and builds the
                 traps in on the first pass
    3 grade      validity only -- gates, probes, controls, semantic review.
                 Nothing here asks whether a task is HARD; a draft is never
                 screened for difficulty on its own
       -- barrier: the whole requested set is authored and graded --
    4 calibrate  a screen of every validated task: `policy.SCREEN_EPISODES_PER_MODEL`
                 episodes on each of the two calibration models (`screen.py`)
       -- STOP: the pipeline prints pass@1, pass@4 and mean reward per task per
          model, and a hardening outlook per task, and ends --
    5 harden     manual only (`arena harden`, at most 3 attempts per task); every
                 round ends in another screen. Any task whose screen measured
                 cleanly may take the ten-episode calibration of record
                 (`arena qualify`); pass@1 < 0.5 is a target, never a blocker

At one or two environments a day the stages can be run by hand. At forty they
cannot, and the constraint that matters is not authoring effort but API
concurrency: a rollout averages 232 s, and forty tasks at ten rollouts each is
400 rollouts. Sequentially that is a 26-hour day. The arithmetic only works with
real concurrency, and concurrency is where a batch pipeline usually goes wrong.

Two separate limits, because they bind on different things:

  `tasks`    how many environments are being built at once. Bounded by local
             work -- extraction, projection, probes -- which is CPU and disk.
  `rollouts` how many model calls are in flight ACROSS ALL TASKS. Bounded by the
             provider. A per-task limit does not bound this: ten tasks each
             politely capped at five rollouts is fifty concurrent calls, and the
             rate limiter sees one number, not ten.

So the rollout budget is a single global semaphore that every task shares.

**A failing task must not fail the batch.** Each task is isolated: an exception
in one is recorded against that task and the rest continue. A batch that dies on
its third environment because the seventh had a malformed fixture has wasted the
other thirty-seven.

**Nothing is reported green that was not measured.** Per-task status is one of
`green`, `blocked` (a gate failed -- a finding about the task), `unverified` (a
gate errored or was skipped -- a finding about our ability to check it), or
`error`. The batch summary never collapses those into a pass count.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from .author.outcomes import AuthoringStalled, SeedRejected
from typing import Any, Callable

from .author import build as build_stage
from .author import extract as extract_stage
from .author import controls as controls_stage
from .author import probes as probe_stage
from .config import ARTIFACTS
from .ledger import faithfulness
from .run import run_bundle
from .artifacts import atomic_json, bundle_lock, identity
from . import assurance as assurance_stage
from . import policy as _policy

#: Global cap on concurrent model calls across every task in the batch.
from .config import MAX_QUALIFY_WORKERS, MAX_REVIEW_CONCURRENCY, MAX_ROLLOUT_BUDGET, MAX_TASK_WORKERS
DEFAULT_ROLLOUT_BUDGET = MAX_ROLLOUT_BUDGET
DEFAULT_TASK_WORKERS = MAX_TASK_WORKERS


class Budget:
    """A counting semaphore with visibility.

    A plain semaphore hides saturation, and saturation is the thing worth
    knowing: if the batch spends its time at the cap, the cap is the throughput,
    not the worker count.
    """

    def __init__(self, size: int) -> None:
        self._sem = threading.BoundedSemaphore(size)
        self._lock = threading.Lock()
        self.size = size
        self.in_flight = 0
        self.peak = 0
        self.waited_s = 0.0

    def acquire(self) -> None:
        started = time.monotonic()
        self._sem.acquire()
        waited = time.monotonic() - started
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.waited_s += waited

    def release(self) -> None:
        with self._lock:
            self.in_flight -= 1
        self._sem.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def stats(self) -> dict:
        return {"size": self.size, "peak_in_flight": self.peak,
                "total_wait_s": round(self.waited_s, 1)}


@dataclass
class TaskResult:
    slug: str
    session_id: str
    seed_index: int
    tier: int | None = None
    status: str = "pending"          # validated | accepted | reserve | blocked | unverified | error
    stage_reached: str = ""
    creation_status: str = "unrecorded"  # complete components | unfinished; never validity
    creation_missing_components: list[str] = field(default_factory=list)
    faithfulness: float | None = None
    #: Arena's F0-F3 tag. Distinct from `tier`, which describes the source
    #: trace; this describes what survived into the delivered task.
    faithfulness_level: str | None = None
    probe_rewards: dict = field(default_factory=dict)
    gate_verdict: str = ""
    blockers: list[str] = field(default_factory=list)
    gate_rounds: int = 0
    repair_feedback: list[str] = field(default_factory=list)
    selection: dict = field(default_factory=dict)
    #: The batch calibration screen, per model, once the whole batch is built.
    calibration: dict | None = None
    design_attempts: int = 1
    duration_s: float = 0.0
    error: str = ""


def _slug(row: dict, index: int) -> str:
    # Prefer the seed's rank over its position in this batch. A batch that does
    # not start at rank 1 otherwise renumbers from 1, which is how the rank-12
    # seed shipped as `kw-a004` -- a name that pointed at a different seed than
    # the one inside it. With the rank baked in, the name is checkable.
    index = row.get("_rank", index)
    domain = (row.get("classification", {}).get("domain") or "task")
    clean = "".join(c if c.isalnum() else "-" for c in domain.lower()).strip("-")
    clean = "-".join(p for p in clean.split("-") if p)[:36] or "task"
    return f"c-{index:03d}-{clean}"


#: Bounded repair, defaulting to the policy budget rather than to zero.
#:
#: This was `0`, and zero is how 47 of 86 candidates in `envs-cat-v2` were
#: discarded holding findings that named the exact file and the exact line.
#: Repair here is VALIDITY repair only -- gate, control and semantic-review
#: findings. Difficulty is never a repair finding in the build: it is authored
#: into the first draft and measured by the batch screen afterwards.
#:
#: The loop is still bounded, and by more than a count: `repair.admissible`
#: requires the finding set to strictly shrink and treats a repeated finding as
#: terminal. `ARENA_GATE_REPAIRS=0` restores the old behaviour for a run that
#: genuinely wants one shot.
_gate_repairs_env = os.environ.get("ARENA_GATE_REPAIRS", "").strip()
GATE_REPAIRS = (int(_gate_repairs_env) if _gate_repairs_env
                else _policy.MAX_REPAIR_PASSES)

#: One initial design. Verified findings use the separately bounded repair allowance.
DESIGN_ATTEMPTS = 1


def check_bands(campaign: dict) -> dict:
    """Sort every check into floor / core / stretch / unreachable by pass rate.

    `gated_out` is not `failed`: a check whose prerequisite failed was never
    attempted and says nothing about ability, so it is counted separately
    rather than folded into the failures.
    """
    from .measurement import objective_bands
    return objective_bands(campaign.get("checks") or {})


def suspect_zeros(campaign: dict, bands: dict) -> list[str]:
    """Zeros that are NOT evidence of difficulty, with the reason each is suspect.

    A task whose models all score 0.0 is acceptable and may be exactly what was
    wanted -- but only when the zero is the model's. This separates the two.
    Nothing here fires because a task is hard; every entry names a way the
    measurement itself is untrustworthy.
    """
    suspect = []
    summary = campaign.get("summary") or {}

    # 1. Void rollouts measure nothing: truncation, adapter stall, error.
    if summary.get("void"):
        suspect.append(
            f"{summary['void']} of {summary.get('requested', '?')} rollouts were "
            "VOID (truncation, adapter stall or error). A void is worse than a "
            "missing data point -- it is indistinguishable from a result. Nothing "
            "about difficulty can be read from this run until they are real.")

    # 2. `gated_out` is not `failed`. A check never attempted scored nothing and
    #    measured nothing, and its weight is unreachable rather than hard.
    if bands["unreachable"]:
        suspect.append(
            "NEVER ATTEMPTED -- a prerequisite failed, so these checks measured "
            "nothing and their weight could not be earned by any attempt. This is "
            "unreachable, not difficult. Shorten the `requires` chains: "
            + ", ".join(c for c, _ in bands["unreachable"]))

    # 3. Near-misses need adjudication. High overlap can still be a substantive
    # error; this heuristic must never certify a defect or a capability failure.
    near = {}
    for roll in campaign.get("rollouts") or []:
        for cid, shape in (roll.get("misses") or {}).items():
            if shape == "equal_normalised":
                near.setdefault(cid, []).append("the reference answer verbatim")
            elif isinstance(shape, str) and shape.startswith("overlap_"):
                try:
                    value = float(shape.split("_", 1)[1])
                except ValueError:
                    continue
                if value >= 0.80:
                    near.setdefault(cid, []).append(f"{value:.0%} of the answer")
    for cid, shapes in near.items():
        suspect.append(
            f"check {cid!r} scored zero on submissions carrying {shapes[0]} "
            f"({len(shapes)} rollout(s)). Review the public contract and actual "
            "decision before classifying it as a model, grader or specification "
            "error. Similarity alone cannot decide that classification.")
    return suspect


_REVIEW_SLOTS = threading.BoundedSemaphore(
    int(os.environ.get("ARENA_REVIEW_CONCURRENCY", str(MAX_REVIEW_CONCURRENCY))))


def _ship_readiness(bundle: Path) -> list[dict]:
    """The ship-time sweep, run while the designer can still fix what it finds.

    User, 2026-10-07: "add these learnings to the pipeline ... to ensure these
    issues are not repeated and tasks produced are clean and hard". Arena's
    review of the shipped batch found classes no build stage looked for; each
    is now a build blocker that goes back to the designer as repair feedback:

      static     declared packages not installed, a README requiring a newer
                 toolchain, an advertised script reading an unshipped path,
                 a toolchain whose binaries a login shell loses
      image      every import the repository's code makes resolves inside the
                 built agent image, every interpreter starts silently
      coverage   the coverage critic's EXECUTED gaps: a counter-implementation
                 of a published clause that scores 1.0 (C-006/C-013/C-024).
                 Run once per build -- a second critic run proposes different
                 clauses, and a repair loop chasing a moving target would
                 never shrink its findings; the ship-time sweep re-runs it on
                 the final version.

    Findings a designer cannot act on (ledger declarations, which the ledger
    build now writes itself; human waivers) are left to the ship-time sweep.
    """
    from . import shipcheck, coverage_critic
    from .author import sandbox
    from .credential import resolve_credential
    found = []
    # The login-shell layer is set on every newly staged toolchain
    # (agent_designer.stage_spec), so the build checks what a designer can get wrong.
    for detector in (shipcheck.deps_declared, shipcheck.docs_toolchain, shipcheck.fixture_outside_paths):
        found += [f for f in detector(bundle) if f["route"] in ("fixture", "regenerate")]
    if (bundle / "environment/Dockerfile").is_file() and sandbox.available()[0]:
        try:
            extra, _ = shipcheck.image_probe(bundle)
            found += [f for f in extra if f["check"] != "image.probe"]
        except Exception as exc:                      # noqa: BLE001 -- an unprobed image is not a pass
            found.append(shipcheck.finding("image.probe", "agent image", f"probe failed: {exc}"[:300],
                                           route="regenerate", measure="screen"))
    receipt = bundle / coverage_critic.RECEIPT
    if not receipt.is_file() and (bundle / "tests/checks.json").is_file() and resolve_credential():
        try:
            found += coverage_critic.findings(coverage_critic.critic(bundle, record_dir=bundle / "verification"))
        except Exception as exc:                      # noqa: BLE001 -- the ship-time sweep re-runs it
            (bundle / "verification").mkdir(parents=True, exist_ok=True)
            (bundle / "verification/coverage-critic-error.txt").write_text(f"{type(exc).__name__}: {exc}")
    granted = shipcheck.waivers(bundle)
    found = [f for f in found if not shipcheck._waived(f, granted)]
    advice = {
        "coverage.gap": "add a hidden case for exactly this clause only if determinacy_probe settles it, keep the "
                        "counter-implementation as an m_partial_ mutant, else record the clause under untested_surfaces",
        "image.imports": "install the package pinned in spec.deps (the repository's declared version when it has one)",
        "image.packages": "pin compatible versions in spec.deps",
        "image.quiet_start": "scope the post_install step that causes it to /opt/deps/venv",
        "image.shell_parity": "re-stage spec.toolchain with login_shell_path: true",
        "image.login_shell": "re-stage spec.toolchain with login_shell_path: true",
        "deps.declared": "install the declared packages, pinned, in spec.deps",
        "docs.toolchain": "correct the doc's version to the image's toolchain (write_fixture_file, declared as authored)",
        "fixture.outside_paths": "guard the advertised script so it reports NOT RUN when the unshipped file is absent "
                                 "(declare it authored with a reason starting 'guard:')",
    }
    def where(f: dict) -> str:
        if f["check"] == "coverage.gap":
            return "tests/suites"
        if f["check"] in ("docs.toolchain", "fixture.outside_paths"):
            return "environment/repo/" + str(f["subject"]).split(":")[0]
        return "authoring/spec.json"

    return [{"code": f["check"], "message": f"{f['subject']}: {f['detail']} -- fix: {advice.get(f['check'], 'see shipcheck')}",
             "where": where(f)} for f in found]


def _review_candidate(bundle: Path, budget: Budget) -> dict:
    """Review before measurement; retain an intact rejection until redesign."""
    from . import review as critic
    from .author.contracts import contained, strict_json
    try:
        prior = bundle / "verification/semantic-review.json"
        report = None
        issues = ["missing review"]
        if prior.is_file():
            try:
                report = strict_json(prior.read_text())
                issues = critic.review_problems(bundle, report)
            except (OSError, ValueError, KeyError, TypeError):
                issues = ["invalid review receipt"]
        if issues and not critic.semantic_rejection(issues):
            spec = strict_json((bundle / "authoring/spec.json").read_text())
            producer = spec.get("_producer", {}).get("model", "")
            model = os.environ.get("ARENA_REVIEW_MODEL") or (
                "claude-fable-5-1" if producer.startswith("gpt-") else "gpt-6-astra")
            if not producer or model == producer:
                return {"verdict": "unverified", "findings": [
                    "semantic review requires an identified author and a different reviewer model"]}
            # The review slot first, so a queued review holds no rollout slot.
            with _REVIEW_SLOTS, budget:
                report = critic.review(bundle, model)
            issues = critic.review_problems(bundle, report)
        if not issues:
            return {"verdict": "green", "findings": []}
        if not critic.semantic_rejection(issues):
            return {"verdict": "unverified", "findings": issues}
        doc, _ = critic.load_review_deliverable(contained(bundle, report["work_dir"]).joinpath("deliverables/review.json"))
        findings = [f"Semantic review {c.get('id')}: {c.get('reason')}"
                    for c in doc.get("checks", []) if c.get("verdict") != "pass"]
        for key in ("ambiguities", "substantive_gaps", "shortcut_risks", "privacy_findings"):
            findings.extend(f"Semantic review {key}: {item}" for item in doc.get(key, []))
        return {"verdict": "blocked", "findings": findings or issues}
    except Exception as exc:
        return {"verdict": "unverified", "findings": [
            f"semantic review could not be verified: {type(exc).__name__}: {exc}"]}


#: Checkpoint stages a finished attempt ends in. A re-run returns the retained
#: result instead of redoing -- or, as it used to, raising "not resumable" and
#: overwriting a finished task's record with an error.
TERMINAL_STAGES = frozenset({"validated", "blocked", "reserve"})

JOB_STATE = "verification/state.json"


def _retained_result(bundle: Path) -> TaskResult | None:
    """The finished result for a bundle whose checkpoint is terminal, else None.

    Raises when the checkpoint is terminal but the retained record is missing,
    disagrees with it, or describes a different artifact: that attempt needs
    inspection, and neither redoing it nor reporting it as finished is honest.
    """
    from . import checkpoint as progress
    from .author.contracts import strict_json
    stage = progress.read(bundle).get("stage")
    if stage not in TERMINAL_STAGES:
        return None
    try:
        job = strict_json((bundle / JOB_STATE).read_text())
    except (OSError, ValueError):
        job = None
    if not isinstance(job, dict) or job.get("status") != stage:
        raise ValueError(f"terminal checkpoint {stage!r} has no matching retained job "
                         "record; inspect this attempt instead of redoing it")
    try:
        current = identity(bundle)
    except (OSError, ValueError):
        current = None
    if job.get("artifact_sha256") != current:
        raise ValueError(f"retained {stage!r} result no longer matches the artifact on "
                         "disk; inspect this attempt instead of redoing it")
    names = {f.name for f in fields(TaskResult)}
    return TaskResult(**{k: v for k, v in job.items() if k in names})


def _repair_refused(result: TaskResult, findings: list[str], why: str) -> TaskResult:
    """Stop with the findings attached when the repair governor refuses a pass.

    `checkpoint.admit_repair` refuses a pass whose findings cite no file, check
    or step, one that does not strictly shrink the finding set, or one that
    repeats a finding. The candidate is blocked, never discarded: its findings
    stay on the record for a later, deliberate repair.
    """
    result.status = "blocked"
    result.blockers = [*findings, f"repair refused: {why}"]
    return result


def preflight_feedback(exc) -> list[str]:
    """A control preflight failure as findings the designer can act on.

    `reference checkpoint failed` alone names no cause; the grade records do:
    the contract errors and the first failing checks with their reasons.
    """
    out = []
    for label, record in sorted((exc.records or {}).items()):
        for step, result in sorted((record.get("steps") or {}).items()):
            grade = result.get("grade") or {}
            expected = 0.0 if label == "nop" else 1.0
            if result.get("reward") == expected and not grade.get("contract_errors"):
                continue
            failing = [f"{cid} ({str(c.get('reason') or c.get('error') or 'failed')[:160]})"
                       for cid, c in (grade.get("checks") or {}).items()
                       if isinstance(c, dict) and not c.get("passed") and label != "nop"][:4]
            parts = [f"control preflight {label}/{step}: reward {result.get('reward')} (expected {expected})"]
            if result.get("timed_out") or result.get("error"):
                parts.append(f"error: {str(result.get('error') or 'timed out')[:200]}")
            if grade.get("contract_errors"):
                parts.append("contract errors: " + "; ".join(dict.fromkeys(grade["contract_errors"]))[:400])
            if failing:
                parts.append("failing: " + "; ".join(failing))
            out.append(" -- ".join(parts))
    return out or [f"control preflight: {e}" for e in getattr(exc, "errors", [])]


def _build_one(row: dict, index: int, out_root: Path, designer: Callable,
              budget: Budget, *, gate_repairs: int | None = None,
              preserve_workspace: bool = False) -> TaskResult:
    """One environment, authored and graded. Never raises -- failures are recorded.

    This is phases 2 and 3 for one seed, and nothing more: no model is asked to
    solve the task here. A validated task waits for the batch screen, which
    runs once every task in the batch has been built (`calibrate_batch`).
    """
    started = time.monotonic()
    slug = _slug(row, index)
    result = TaskResult(slug=slug, session_id=row["session_id"],
                        seed_index=row["seed_index"], tier=row.get("tier"))
    # Group by target level on disk. A reader seeing
    # `target-F1-pattern/a-002-content-writing` knows the seed was classified to
    # aim at F1 before authoring, without opening a ledger. Uncategorised seeds
    # still land at the top level.
    bundle = _bundle_for(out_root, row, slug)
    # The bundle found for a re-ranked seed keeps its earlier name; the result
    # must report that name, or the batch screen looks for a task that is not
    # on disk (the third relaunch reported `c-005-...` for `c-002-...`).
    slug = result.slug = bundle.name
    try:
        from . import checkpoint as progress
        # A design that ran out of steps is never restarted automatically;
        # `--continue-stalled` asks for one fresh session seeded with its draft.
        stalled = (progress.stalled_design(bundle)
                   if os.environ.get("ARENA_CONTINUE_STALLED") == "1" else None)
        if stalled is None and (retained := _retained_result(bundle)) is not None:
            return retained
        resumed = ({**progress.read(bundle), "stage": "stalled"} if stalled
                   else progress.resumable(bundle))
        if progress.read(bundle) and not resumed:
            raise ValueError('Existing checkpoint is not resumable; retain this attempt instead of resetting it')
        if resumed:
            preserve_workspace = True
        repair_limit = GATE_REPAIRS if gate_repairs is None else gate_repairs
        if type(repair_limit) is not int or not 0 <= repair_limit <= GATE_REPAIRS:
            raise ValueError("retained gate repair budget cannot exceed the configured original budget")
        if type(preserve_workspace) is not bool:
            raise ValueError("preserve_workspace must be boolean")
        repair_limit = progress.repair_budget(bundle, 'gate', repair_limit)
        result.stage_reached = "extract"
        extraction = extract_stage.extract(row["session_id"], row["seed_index"])
        if preserve_workspace:
            if not (bundle / "environment/repo").is_dir() or not (
                    bundle / "authoring/extraction/extraction.json").is_file():
                raise ValueError("retained authoring requires an existing extracted workspace")
        else:
            extract_stage.write(extraction, bundle)
            result.stage_reached = "privacy"
            from .ledger.redaction import minimize
            try:
                minimize(bundle, row["session_id"])
            except ValueError as exc:
                # Retain the fixture for the author to repair; the privacy gate
                # still rejects residual findings during validation.
                atomic_json(bundle / "verification/creation-advisories.json",
                            {"privacy": str(exc), "validation_deferred": True})
            _declare_lineage(bundle, row, out_root)

        # A manually requested repair carries concrete gate findings into a
        # bounded revision. The default path retains findings without revising.
        gate_feedback: list[str] = resumed.get('feedback', []) if resumed and resumed['stage'] == 'repair' else []
        previous_spec = progress.submitted_spec(bundle, resumed) if gate_feedback else None
        if stalled:
            previous_spec = stalled["draft"]
            gate_feedback = [
                f"Continue the design that stopped after {stalled['steps']} steps. The draft "
                "is staged; keep its completed work and fix what the validator still reports, "
                "then submit. Remaining problems:"] + [str(p) for p in stalled["problems"]]
        gate_repairs_left = repair_limit
        for gate_round in range(repair_limit + 1):
            result.gate_rounds = gate_round + 1
            result.stage_reached = "design"
            spec = None
            design_errors: list[str] = []
            # A stalled session has no steps left; its continuation starts a
            # fresh session from the draft instead of resuming the old one.
            retained_author = (resumed.get('authoring_session')
                               if resumed and gate_round == 0 and not stalled else None)
            if resumed and gate_round == 0 and resumed['stage'] in {'submitted', 'checking'}:
                spec = progress.submitted_spec(bundle, resumed)
            for attempt in range(DESIGN_ATTEMPTS):
                if spec is not None:
                    break
                try:
                    # Design shares the batch semaphore. It used to sit outside it
                    # because a one-shot call was 1-12 requests; the agent designer
                    # is 20-60, and six of those in parallel is exactly the
                    # uncontrolled fan-out `Budget` exists to prevent -- the
                    # provider sees one number, not six.
                    options = ({'resume_session': retained_author,
                                'max_steps': retained_author['remaining_steps']}
                               if retained_author else {'initial_spec': previous_spec})
                    # A repair round revises a submitted draft; the first design
                    # and a stalled first design's continuation do not.
                    options['revision'] = bool(gate_round > 0 or (
                        resumed and resumed.get('stage') == 'repair'))
                    spec = designer(row, extraction, bundle, budget=budget,
                                    feedback=gate_feedback or None, **options)
                    break
                except (SeedRejected, AuthoringStalled):
                    raise
                except Exception as exc:
                    design_errors.append(f"attempt {attempt + 1}: {exc}")
            if spec is None:
                raise RuntimeError(
                    f"design failed {DESIGN_ATTEMPTS} times. " + design_errors[-1])
            result.design_attempts = len(design_errors) + 1
            from copy import deepcopy
            previous_spec = deepcopy(spec)

            result.stage_reached = "build"
            attempt_path = bundle / "verification" / "design-attempts" / f"{time.time_ns()}.json"
            atomic_json(attempt_path, spec)
            import hashlib
            progress.update(bundle, stage='submitted', authoring_session=None,
                submitted={'path': attempt_path.relative_to(bundle).as_posix(),
                           'sha256': hashlib.sha256(attempt_path.read_bytes()).hexdigest()},
                workspace_files=progress.evidence_inventory(bundle, progress.WORKSPACE))
            # The spec is the source of truth for everything the build writes;
            # it lands first so a build failure still leaves what was designed.
            # The usable-correction count is part of the spec, never patched
            # into the exported traps.json afterwards: a post-build edit is
            # export drift the next regeneration reports (repair round,
            # 2026-09-27) and the designer cannot repair.
            spec["usable_corrections"] = extract_stage.usable_correction_count(row, extraction)
            atomic_json(bundle / "authoring" / "spec.json", spec)
            from .author import creation
            created = creation.record(spec, bundle, reason="retained-before-validation")
            result.creation_status = created["status"]
            result.creation_missing_components = created["missing_components"]
            result.stage_reached = "export-validation"
            try:
                from .author.agent_designer import validate_source_capacity
                source_rejection = validate_source_capacity(spec, bundle,
                                                            source_root=getattr(extraction, "source_root", None))
                if source_rejection is not None:
                    result.stage_reached = "source-capacity-validation"
                    atomic_json(bundle / "verification/submission-validation.json",
                                {"verdict": "blocked", "phase": "validation", "kind": "source-capacity",
                                 "problems": [source_rejection["reason"]]})
                    raise SeedRejected(source_rejection)
                build_stage.build(spec, bundle)
            except SeedRejected:
                raise
            except (ValueError, KeyError, TypeError) as exc:
                # Creation has finished. An incomplete/malformed candidate is
                # a retained validation failure, never a failed designer call.
                gate_feedback = [f"export.validation: invalid-spec: {type(exc).__name__}: {exc} "
                                 "[authoring/spec.json]"]
                atomic_json(bundle / "verification/submission-validation.json",
                            {"verdict": "blocked", "phase": "validation", "problems": gate_feedback})
                result.gate_verdict = "blocked"
                result.blockers = result.repair_feedback = gate_feedback
                if gate_repairs_left == 0:
                    result.status = "blocked"
                    return result
                ok, why, gate_repairs_left = progress.admit_repair(bundle, 'gate', gate_feedback)
                if not ok:
                    return _repair_refused(result, gate_feedback, why)
                progress.update(bundle, stage='repair', feedback=gate_feedback)
                continue
            atomic_json(bundle / "verification/submission-validation.json",
                        {"verdict": "exported", "phase": "validation",
                         "note": "export succeeded; controls, gates and semantic review still pending"})

            result.stage_reached = "ledger"
            ledger = faithfulness.build(bundle, row["session_id"], row["seed_index"])
            # Which category this seed was built as, and the two measurements
            # that decided it. Without this a finished bundle cannot be traced
            # back to the bucket it was chosen for, which is the whole basis of
            # a category-wise batch.
            ledger["category"] = row.get("category")
            ledger["category_inputs"] = {
                "replay_readiness": row.get("replay_readiness"),
                "corrections": row.get("corrections")}
            # Arena's F0-F3 level, and then the task name that carries it.
            # Both before any hash is bound to this task: renaming after
            # qualification would invalidate every receipt bound to the old
            # identity. The category above says which bucket the *seed* came
            # from; the level says what actually survived into the task, and
            # they can legitimately differ.
            ledger = faithfulness.with_level(bundle, ledger)
            (bundle / "provenance").mkdir(parents=True, exist_ok=True)
            (bundle / "provenance" / "ledger.json").write_text(json.dumps(ledger, indent=2))
            result.faithfulness = ledger["faithfulness_by_bytes"]
            result.faithfulness_level = ledger["faithfulness_level"]
            from . import naming
            naming.apply(bundle, ledger["faithfulness_level"])
            progress.update(bundle, stage='checking',
                workspace_files=progress.evidence_inventory(bundle, progress.WORKSPACE))

            # Executed controls and probes need Docker. Without it nothing here
            # is measured, and "could not check" is never green.
            from .author import sandbox as sandbox_stage
            docker_ok, docker_why = sandbox_stage.available()
            if not docker_ok:
                result.status = "unverified"
                result.stage_reached = "controls"
                result.blockers = [f"controls: sandbox unavailable: {docker_why}"]
                return result
            result.stage_reached = "controls"
            try:
                controls = controls_stage.run(bundle)
            except controls_stage._ControlPreflightFailure as exc:
                # The reference or the alternative fails its own grader: a
                # defect the designer can repair, not infrastructure loss.
                # 2026-10-03 recorded three finished designs as errors here (a
                # directory declared as a deliverable twice; reference code the
                # grader refuses to import once).
                controls = {"errors": preflight_feedback(exc)}
            if controls.get("results") is not None:
                result.stage_reached = "probes"
                report = probe_stage.run(bundle, controls_receipt=controls)
                result.probe_rewards = {k: (v.get("reward") if isinstance(v, dict) else None)
                                        for k, v in report["results"].items()
                                        if isinstance(v, dict) and "reward" in v}

            result.stage_reached = "gate"
            gates = run_bundle(bundle, Path.cwd())
            if controls["errors"]:
                gates["verdict"] = "blocked" if gates["verdict"] != "unverified" else gates["verdict"]
                gates["results"].append({"gate": "exported.controls", "blockers": [
                    {"code": "control-failed", "message": e, "where": "verification/controls.json"}
                    for e in controls["errors"]]})
            if gates["verdict"] == "green":
                result.stage_reached = "reproducible"
                control_errors = assurance_stage.generated_problems(bundle)
                if control_errors:
                    gates["verdict"] = "blocked"
                    gates["results"].append({"gate": "exported.reproducible", "blockers": [
                        {"code": "export-drift", "message": e, "where": "authoring/spec.json"} for e in control_errors]})
            if gates["verdict"] == "green":
                result.stage_reached = "ship-readiness"
                readiness = _ship_readiness(bundle)
                if readiness:
                    gates["verdict"] = "blocked"
                    gates["results"].append({"gate": "ship.readiness", "blockers": readiness})
            result.gate_verdict = gates["verdict"]
            result.blockers = [f"{g['gate']}: {b['code']}"
                               for g in gates["results"] for b in g["blockers"]]
            if gates["verdict"] != "green":
                # `where` is the finding's location; the repair governor admits
                # only findings that cite one, so it must not be dropped here.
                gate_feedback = [
                    f"{g['gate']}: {b['code']}: {b.get('message', '')}"
                    + (f" [{b['where']}]" if b.get('where') else "")
                    for g in gates["results"] for b in g["blockers"]]
                result.repair_feedback = gate_feedback
                if gates["verdict"] == "unverified" or gate_repairs_left == 0:
                    result.status = gates["verdict"]
                    return result
                ok, why, gate_repairs_left = progress.admit_repair(bundle, 'gate', gate_feedback)
                if not ok:
                    return _repair_refused(result, gate_feedback, why)
                progress.update(bundle, stage='repair', feedback=gate_feedback)
                continue
            # No difficulty screen here, by design (user, 2026-09-25). A draft
            # is authored to be hard on its first pass and is measured with the
            # rest of its batch by the calibration screen once every task is
            # built. The one-episode presolve that used to run here fed "too
            # easy" back into the design loop one task at a time; it is gone.
            result.stage_reached = "semantic-review"
            reviewed = _review_candidate(bundle, budget)
            if reviewed["verdict"] != "green":
                result.gate_verdict = reviewed["verdict"]
                result.blockers = reviewed["findings"]
                result.repair_feedback = reviewed["findings"]
                if reviewed["verdict"] == "unverified" or gate_repairs_left == 0:
                    result.status = reviewed["verdict"]
                    return result
                ok, why, gate_repairs_left = progress.admit_repair(
                    bundle, 'gate', reviewed["findings"])
                if not ok:
                    return _repair_refused(result, reviewed["findings"], why)
                gate_feedback = reviewed["findings"]
                progress.update(bundle, stage='repair', feedback=gate_feedback)
                continue
            break
        else:
            result.status = "blocked"
            result.blockers.append("repair budget exhausted without a validated final revision")
            return result

        result.status = "validated"
    except AuthoringStalled as exc:
        result.status = 'unverified'
        result.stage_reached = 'authoring-progress'
        result.creation_status = 'unfinished'
        result.blockers = [exc.report['reason']]
        result.repair_feedback = result.blockers
        atomic_json(bundle / 'verification/authoring-stalled.json', exc.report)
    except SeedRejected as exc:
        result.status = "reserve"
        if result.stage_reached != "source-capacity-validation":
            result.stage_reached = "source-capacity"  # Compatibility with external legacy designers.
        result.selection = {"status": "source-rejected", "reason": exc.report['reason']}
        result.error = ""
        atomic_json(bundle / "verification/source-rejection.json", exc.report)
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        (bundle / "verification").mkdir(parents=True, exist_ok=True)
        (bundle / "verification" / "orchestrator-error.txt").write_text(
            traceback.format_exc())
    finally:
        result.duration_s = round(time.monotonic() - started, 1)
        # Keep a resumable author/build boundary on infrastructure errors.
        if result.status in {'validated', 'blocked', 'reserve'}:
            from .checkpoint import update
            update(bundle, stage=result.status)
    return result


#: Set when a task fails because provider access itself is gone (a spent quota,
#: a rejected credential). Tasks not yet started then return at once, untouched
#: and resumable, instead of each spending its design attempt on the same wall
#: (census 2026-10-03: 19 of 44 batch runs errored at design this way).
PROVIDER_STOP = threading.Event()
STOPPED = ("batch stopped before this task started: provider access failed (quota or credential); "
           "fix keys.env or wait for the limit to reset, then re-run the same command to resume")


def credential_preflight() -> dict:
    """One minimal authoring request before a batch spends anything.

    Fatal (quota, credential) stops the batch before any task starts; anything
    else is reported and the batch proceeds (a transient failure is retried
    where it happens).
    """
    from .author.transport import MODEL, _call
    from .provider import fatal_provider_error
    try:
        _call({"model": MODEL, "max_tokens": 1,
               "messages": [{"role": "user", "content": "Reply with OK."}]}, retries=2)
        return {"ok": True}
    except Exception as exc:                              # noqa: BLE001 -- classified below
        if fatal_provider_error(exc):
            raise SystemExit(f"provider preflight failed before any task started: {exc}") from None
        return {"ok": False, "transient": f"{type(exc).__name__}: {exc}"}


def build_one(row: dict, index: int, out_root: Path, designer: Callable,
              budget: Budget, *, gate_repairs: int | None = None,
              preserve_workspace: bool = False) -> TaskResult:
    """One durable, exclusive job. The state file is never a release certificate."""
    slug = _slug(row, index)
    if PROVIDER_STOP.is_set():
        return TaskResult(slug=slug, session_id=row["session_id"], seed_index=row["seed_index"],
                          tier=row.get("tier"), status="error", error=STOPPED)
    bundle = _bundle_for(out_root, row, slug)
    # A seed re-ranked since its bundle was built keeps that bundle's name:
    # every receipt, screen and report inside it is filed under it.
    slug = bundle.name
    state = bundle / JOB_STATE
    try:
        with bundle_lock(bundle):
            # Before the "running" record: that write would erase the result.
            if (retained := _retained_result(bundle)) is not None:
                return retained
            atomic_json(state, {"schema": "arena-pipeline/job/1", "status": "running",
                               "pid": os.getpid(), "started_at": time.time(),
                               "session_id": row["session_id"], "seed_index": row["seed_index"]})
            result = _build_one(row, index, out_root, designer, budget,
                                gate_repairs=gate_repairs, preserve_workspace=preserve_workspace)
            try:
                sha = identity(bundle)
            except (OSError, ValueError):
                sha = None
            atomic_json(state, {"schema": "arena-pipeline/job/1", **asdict(result),
                               "artifact_sha256": sha, "finished_at": time.time(),
                               "release_ready": False})
            return result
    except Exception as exc:
        return TaskResult(slug=slug, session_id=row["session_id"],
                          seed_index=row["seed_index"], status="error", error=str(exc))


def batch(rows: list[dict], out_root: Path, designer: Callable,
          task_workers: int = DEFAULT_TASK_WORKERS,
          rollout_budget: int = DEFAULT_ROLLOUT_BUDGET, screen_workers: int = 0) -> dict:
    """Author and grade every seed concurrently.

    Phases 2 and 3 for the whole batch. Difficulty is never asked of a draft.
    With `screen_workers` (`policy.SCREEN_AS_BUILT`) each task's calibration
    screen starts the moment the task validates, instead of after the slowest
    design in the batch: on 2026-10-03 validated tasks waited six hours and
    more for their wave's stragglers. `calibrate_batch` then reuses every
    receipt and screens only what is left.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    budget = Budget(rollout_budget)
    started = time.monotonic()
    results: list[TaskResult] = []

    from .provider import fatal_provider_error
    screens = ThreadPoolExecutor(max_workers=screen_workers) if screen_workers > 0 else None
    screening: list = []
    with ThreadPoolExecutor(max_workers=task_workers) as pool:
        futures = {pool.submit(build_one, row, i, out_root, designer, budget): i
                   for i, row in enumerate(rows, start=1)}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if fatal_provider_error(result.error) and not PROVIDER_STOP.is_set():
                PROVIDER_STOP.set()
                print(f"  PROVIDER ACCESS FAILED in {result.slug}: {result.error[:200]}\n"
                      "  no further task will start; running ones finish or fail on their own", flush=True)
            if screens is not None and result.status == "validated" and not PROVIDER_STOP.is_set():
                try:
                    bundle = _bundle_for(out_root, rows[futures[future] - 1], result.slug)
                except ValueError as exc:
                    print(f"  not screened as built: {exc}", flush=True)
                else:
                    screening.append(screens.submit(_screen_as_built, bundle))
    if screens is not None:
        for future in screening:
            future.result()
        screens.shutdown()

    results.sort(key=lambda r: r.slug)
    counts: dict[str, int] = {}
    creation_counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        creation_counts[r.creation_status] = creation_counts.get(r.creation_status, 0) + 1
    elapsed = time.monotonic() - started

    attempts = [r.design_attempts for r in results]
    from .provider import GLOBAL
    return {
        "schema": "arena-pipeline/batch-build/2",
        "pipeline": "arena-coding",
        "tasks": len(results),
        "release_ready": 0,
        "validated": counts.get("validated", 0),
        "design": {"mean_attempts": round(sum(attempts) / len(attempts), 2)
                                   if attempts else None,
                   "first_pass": sum(1 for a in attempts if a == 1),
                   "budget": DESIGN_ATTEMPTS},
        "status_counts": counts,
        "creation_counts": creation_counts,
        "wall_clock_s": round(elapsed, 1),
        "throughput_per_hour": round(len(results) / (elapsed / 3600), 1)
                               if elapsed > 0 else None,
        "rollout_budget": budget.stats(),
        "provider_budget": GLOBAL.stats(),
        "task_workers": task_workers,
        "results": [asdict(r) for r in results],
    }


def _screen_as_built(bundle: Path) -> None:
    """One task's screen, started as soon as it validated; never raises."""
    from . import screen
    try:
        record = screen.run(bundle, native_root=ARTIFACTS / "native", label="batch")
        print(f"  screened as built {bundle.name}: {screen._one_line(record)}", flush=True)
    except Exception as exc:                     # noqa: BLE001 -- calibrate_batch retries it
        print(f"  screen as built {bundle.name} failed: {type(exc).__name__}: {exc}"[:400], flush=True)


def _bundle_for(out_root: Path, row: dict, slug: str) -> Path:
    """Where this seed's bundle lives: its target-level folder.

    A tree built before 2026-09-26 filed bundles under the retired A-D folders
    (rich/strong/fixture-led/thin). An existing bundle there is found and used
    -- a resumed build must continue the bundle it started, never begin a second
    one beside it.
    """
    from . import policy
    from .config import CATEGORY_DIRS
    level = policy.target_level(row.get("category"))
    folder = CATEGORY_DIRS.get(level)
    target = (Path(out_root) / folder / slug) if folder else Path(out_root) / slug
    if not target.exists():
        legacy = [Path(out_root) / name / slug for name in policy.SOURCE_TIER_NAMES.values()]
        found = [path for path in legacy if path.exists()]
        if len(found) == 1:
            return found[0]
        # The slug carries the seed's RANK in the surviving-candidate list, and
        # that list is re-ordered by every classification run and every change
        # to the hardness prior. A seed built as `c-003-x` in one batch and
        # ranked 7th in the next would otherwise be built a second time as
        # `c-007-x` beside its own earlier bundle. The seed's identity is the
        # (session, seed) pair the job record carries, so a bundle holding it is
        # this seed's bundle whatever it was called.
        same_seed = _bundles_for_seed(Path(out_root), row.get("session_id"), row.get("seed_index"))
        if len(same_seed) == 1:
            return same_seed[0]
        if len(same_seed) > 1:
            raise ValueError(f"seed {row.get('session_id')}/{row.get('seed_index')} already has "
                             f"{len(same_seed)} bundles under {out_root}: "
                             f"{', '.join(p.name for p in same_seed)}; retain one before building")
    return target


def _bundles_for_seed(out_root: Path, session_id: str | None, seed_index: int | None) -> list[Path]:
    """Every bundle under `out_root` whose job record names this (session, seed)."""
    from .config import bundle_paths
    if not session_id or seed_index is None:
        return []
    found = []
    for bundle in bundle_paths(out_root):
        try:
            job = json.loads((bundle / JOB_STATE).read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(job, dict) and job.get("session_id") == session_id
                and job.get("seed_index") == seed_index):
            found.append(bundle)
    return found


def multi_step_score(row: dict) -> tuple:
    """How well a seed's trace supports a later step: corrections that change a
    requirement after work was done, then usable moments and located causes."""
    cls = row.get("classification") or {}
    classes = cls.get("classes") or {}
    later = sum(classes.get(k, 0) for k in ("spec_correction", "revert_request",
                                             "constraint_assertion", "scope_correction"))
    return (later, cls.get("usable_moments", 0), len(cls.get("defect_pairs") or []), row.get("dispute", 0))


def assign_step_budgets(rows: list[dict]) -> int:
    """~MULTI_STEP_SHARE of the ranked batch gets MULTI_STEP_MAX_STEPS steps, the
    rest exactly one (user, 2026-10-03). Only a trace with at least one later
    requirement change can be multi-step. Returns how many were designated."""
    share = round(len(rows) * _policy.MULTI_STEP_SHARE)
    eligible = sorted((r for r in rows if multi_step_score(r)[0] >= 1), key=multi_step_score, reverse=True)
    chosen = {id(r) for r in eligible[:share]}
    for row in rows:
        row["step_budget"] = _policy.MULTI_STEP_MAX_STEPS if id(row) in chosen else 1
    return len(chosen)


def built_elsewhere(row: dict, workspace: Path | None = None) -> bool:
    """Does this (session, seed) already have a bundle in any envs* tree?"""
    workspace = workspace or Path(__file__).resolve().parents[1]
    return any(_bundles_for_seed(root, row.get("session_id"), row.get("seed_index"))
               for root in sorted(workspace.glob("envs*")) if root.is_dir())


def _declare_lineage(bundle: Path, row: dict, out_root: Path) -> list[str]:
    """A seed rebuilt after a retired attempt declares that attempt as its predecessor.

    The novelty check offers every bundle under the workspace's `envs*` trees
    to the reviewer as a peer. Without a lineage declaration the seed's own
    retired attempt is a competing peer, and the reviewer called c-006 a
    "semantic duplicate" of `envs-retired/c-003-...-attempt1` (2026-09-28).
    `provenance/replacement.json` is the mechanism that already exists for a
    redesign of the same source; this writes it for every built bundle of the
    same (session, seed) found outside `out_root`. Returns the predecessors.
    """
    from .artifacts import atomic_json
    from .author.contracts import strict_json
    workspace = Path(__file__).resolve().parents[1]
    out_root = Path(out_root).resolve()
    predecessors, reasons = [], []
    for root in sorted(p for p in workspace.glob("envs*") if p.is_dir() and p.resolve() != out_root):
        for earlier in _bundles_for_seed(root, row.get("session_id"), row.get("seed_index")):
            if earlier.resolve() == bundle.resolve():
                continue
            try:
                sha = identity(earlier)
            except (OSError, ValueError):
                continue                     # never built to a package; not a peer either
            predecessors.append(sha)
            note = earlier / "RETIRED-NOTE.md"
            reasons.append(f"{earlier.name}: " + (note.read_text().splitlines()[0].lstrip("# ").strip()
                                                  if note.is_file() else "earlier build attempt of this seed"))
    if not predecessors:
        return []
    path = bundle / "provenance/replacement.json"
    existing = strict_json(path.read_text()) if path.is_file() else {}
    declared = list(dict.fromkeys([*(existing.get("predecessors") or []), *predecessors]))[:8]
    atomic_json(path, {"schema": "arena-pipeline/replacement/1",
                       "reason": "Rebuilt from the same source after the pipeline changed; " + "; ".join(reasons),
                       "predecessors": declared})
    return declared


def calibrate_batch(report: dict, rows: list[dict], out_root: Path, *,
                    native_root: Path = ARTIFACTS / "native",
                    task_workers: int = MAX_QUALIFY_WORKERS) -> dict:
    """Phase 4: screen every validated task in the batch, then stop.

    Runs only after `batch` has returned, so the whole requested set exists
    before a single solver episode is spent. Every validated task gets
    `policy.SCREEN_EPISODES_PER_MODEL` episodes on each calibration model; a
    task that did not validate is listed, with why, and is not calibrated -- a
    grader that failed its own gates would be measured wrong.
    """
    from . import screen

    # Keyed by the bundle's name on disk, which is the slug `build_one` reports:
    # a seed re-ranked since an earlier batch keeps its earlier bundle and name.
    by_slug = {}
    for i, row in enumerate(rows, 1):
        try:
            by_slug[_bundle_for(out_root, row, _slug(row, i)).name] = row
        except ValueError as exc:            # two bundles for one seed: reported, not screened
            print(f"  not screened: {exc}", flush=True)
    validated, skipped = [], []
    for item in report["results"]:
        if item["status"] == "validated" and item["slug"] in by_slug:
            validated.append(_bundle_for(out_root, by_slug[item["slug"]], item["slug"]))
        else:
            skipped.append({k: item.get(k) for k in
                            ("slug", "status", "stage_reached", "error", "blockers")})
    print(f"\ncalibration screen: {len(validated)} validated task(s) x "
          f"{len(_policy.resolve_calibration_models())} models x "
          f"{_policy.SCREEN_EPISODES_PER_MODEL} episodes "
          f"(effort {_policy.SPEC_CALIBRATION_EFFORT}, no turn or time limits)", flush=True)
    records = screen.run_many(validated, native_root=native_root,
                              task_workers=task_workers, label="batch")
    by_name = {r.get("bundle"): r for r in records}
    for item in report["results"]:
        record = by_name.get(item["slug"])
        if record:
            item["calibration"] = {m: {k: row.get(k) for k in
                                       ("pass_at_1", "pass_at_4", "mean_reward",
                                        "episodes_valid", "voids_excluded")}
                                   for m, row in (record.get("per_model") or {}).items()}
    return screen.report(records, not_screened=skipped)


def _apply_build_flags(args) -> None:
    """Build-wide switches that are not per-task arguments."""
    if getattr(args, "continue_stalled", False):
        os.environ["ARENA_CONTINUE_STALLED"] = "1"


def hardness_prior(row: dict) -> float:
    """What the trace offers to make a task hard, before anything is authored.

    Usable corrections and located root causes are traps waiting to be
    written; a session with several surviving seeds carries a later
    requirement that can re-govern earlier work. None of it is difficulty
    until the screen says so.
    """
    c = row.get("classification") or {}
    return (c.get("usable_moments", 0) + 2 * c.get("located_causes", 0)
            + (3 if row.get("session_candidate_seeds", 0) >= 2 else 0)
            + (c.get("classes") or {}).get("spec_correction", 0))


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .author.agent_designer import design
    ap = argparse.ArgumentParser(
        description="Author a batch of environments concurrently, then screen the whole "
                    "batch on both calibration models and stop")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="stop after authoring and grading; spend no solver episodes "
                         "(screen later with `arena screen <tree>`)")
    ap.add_argument("--screen-workers", type=int, default=MAX_QUALIFY_WORKERS,
                    help="tasks screened concurrently; native episodes are separately "
                         "bounded by the Coder pool")
    ap.add_argument("--continue-stalled", action="store_true",
                    help="continue a design that ran out of steps from its retained draft "
                         "(one fresh design session; never done automatically)")
    ap.add_argument("-i", "--tiered", default=str(ARTIFACTS / "tiered.json"))
    ap.add_argument("-c", "--classified", default=str(ARTIFACTS / "classified.json"))
    ap.add_argument("-n", "--count", type=int, default=3)
    ap.add_argument("-o", "--out", default="envs")
    ap.add_argument("--task-workers", type=int, default=DEFAULT_TASK_WORKERS)
    ap.add_argument("--rollout-budget", type=int, default=DEFAULT_ROLLOUT_BUDGET)
    ap.add_argument("--max-tier", type=int, default=4)
    ap.add_argument("--category", default=None,
                    help="build only seeds in these categories, e.g. A or A,B")
    ap.add_argument("--ranks", default=None,
                    help="comma-separated 1-based ranks to build instead of the top -n")
    ap.add_argument("--skip-built", action="store_true",
                    help="drop seeds that already have a bundle in any envs* tree BEFORE ranking, "
                         "so a new batch (and every Coder shard of it) builds only new material")
    args = ap.parse_args(argv)

    doc = json.loads(Path(args.classified).read_text())
    from .dump.evidence import SCHEMA as EVIDENCE_SCHEMA
    if any(r.get('classification', {}).get('survives') and
           r['classification'].get('evidence_schema') != EVIDENCE_SCHEMA for r in doc['rows']):
        raise SystemExit('Classification predates the full evidence reader. Rebuild the index and classify again before authoring.')
    rows = [r for r in doc["rows"]
            if r["classification"].get("survives") and r.get("tier", 4) <= args.max_tier]
    # Category is a property of the seed, not of the classification, so it is
    # derived here rather than stored: the boundaries are absolute and the
    # inputs (replay readiness, corrections) are already on the row.
    from .select.categories import CATEGORIES, annotate, target_level
    # Phase 1 before phase 2: every surviving trace is classified into the
    # faithfulness level it can reach BEFORE any authoring starts (user,
    # 2026-09-26). No level blocks authoring.
    annotate(rows)
    from collections import Counter
    classified = Counter(r["category"] for r in rows)
    print("classified before authoring (target faithfulness level): "
          + ", ".join(f"{level} {classified.get(level, 0)}" for level in CATEGORIES))
    if args.category:
        named = {c.strip().upper() for c in args.category.split(",") if c.strip()}
        unknown = {c for c in named if target_level(c) is None}
        if unknown:
            print(f"unknown categor(y/ies) {sorted(unknown)}; "
                  f"known target levels: {list(CATEGORIES)}")
            return 2
        # A retired letter (A-D) is accepted as the level it aimed at.
        wanted = {target_level(c) for c in named}
        rows = [r for r in rows if r["category"] in wanted]
        if not rows:
            print(f"no surviving seeds in categor{'y' if len(wanted)==1 else 'ies'} "
                  f"{sorted(wanted)} at tier <= {args.max_tier}")
            return 2
    if args.skip_built:
        before = len(rows)
        rows = [r for r in rows if not built_elsewhere(r)]
        print(f"--skip-built: {before - len(rows)} seed(s) already have a bundle; {len(rows)} remain")
    # Within a tier, the seeds with the most to build difficulty from come
    # first: several usable corrections, causes the evidence located, a
    # second seed in the same session (a later requirement to revise a
    # premise with). A PRIOR, not a measurement: the first real task cleared
    # every floor from one usable correction and screened at pass@1 1.00.
    rows.sort(key=lambda r: (r.get("tier", 4), -hardness_prior(r), -r.get("dispute", 0)))
    # Rank is 1-based over the sorted survivors and is the only stable handle a
    # human has on a seed. Taking a prefix is fine for a first batch, but it
    # cannot express "build the ones we skipped" -- which is how rank 4 went
    # unbuilt while rank 12 was published as a004. --ranks makes the selection
    # explicit and prints it, so the mapping is auditable rather than implied.
    ranked = list(enumerate(rows, 1))
    # Over the WHOLE ranking, before --ranks: every shard designates the same seeds.
    multi = assign_step_budgets([row for _, row in ranked])
    print(f"step budgets: {multi} of {len(ranked)} seed(s) multi-step (<= {_policy.MULTI_STEP_MAX_STEPS} steps), "
          f"the rest single-step")
    if args.ranks:
        wanted = []
        for piece in args.ranks.replace(" ", "").split(","):
            if not piece:
                continue
            if not piece.isdigit():
                print(f"--ranks takes comma-separated integers; got {piece!r}")
                return 2
            wanted.append(int(piece))
        unknown = [n for n in wanted if not 1 <= n <= len(ranked)]
        if unknown:
            print(f"rank(s) {unknown} outside 1..{len(ranked)} surviving candidates")
            return 2
        ranked = [ranked[n - 1] for n in wanted]
    else:
        ranked = ranked[:args.count]
    if not ranked:
        print("no surviving candidates at this tier")
        return 2
    print("building:")
    for rank, row in ranked:
        print(f"  rank {rank:2d}  [{row.get('category', '?')}]  {row.get('step_budget', 1)}-step  "
              f"t{row.get('tier', 4)}  prior {hardness_prior(row):.0f}  dispute {row.get('dispute', 0):.2f}  "
              f"rr {row.get('replay_readiness', 0):.2f}  "
              f"{row['classification'].get('domain', '?')}")
    for rank, row in ranked:
        row["_rank"] = rank
    rows = [row for _, row in ranked]

    _apply_build_flags(args)
    import sys
    if os.environ.get("ARENA_SKIP_PREFLIGHT") != "1" and "unittest" not in sys.modules:
        checked = credential_preflight()
        print("provider preflight: " + ("ok" if checked["ok"] else f"transient failure ({checked['transient'][:160]}); proceeding"))
    report = batch(rows, Path(args.out), design,
                   task_workers=args.task_workers,
                   rollout_budget=args.rollout_budget,
                   screen_workers=(args.screen_workers if _policy.SCREEN_AS_BUILT and not args.no_calibrate
                                   else 0))
    screened = None
    if not args.no_calibrate:
        screened = calibrate_batch(report, rows, Path(args.out),
                                   task_workers=args.screen_workers)
        report["calibration_screen"] = {k: screened[k] for k in
                                        ("measurement", "tasks_screened", "meets_target",
                                         "shippable", "outlook_counts")}
    # One report per output tree. Two concurrent builds used to write the same
    # file, so whichever finished second silently erased the other's record --
    # the same shape as the calibration work-dir race that produced 66
    # worthless scores with no error. `envs/` keeps the canonical name so
    # `arena status` is unchanged; any other tree gets its own.
    # A Coder shard (`coder_run.shard`) names itself, so shards of one tree
    # pulled back to this checkout keep one report each.
    shard = os.environ.get("ARENA_SHARD", "").strip()
    suffix = f"--{shard}" if shard else ""
    out = ARTIFACTS / ("batch-build.json" if Path(args.out).name == "envs" and not shard
                       else f"batch-build-{Path(args.out).name}{suffix}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    import uuid
    atomic_json(out.with_name(out.stem + "-" + uuid.uuid4().hex + ".json"), report)
    atomic_json(out, report)

    print(f"{report['tasks']} tasks in {report['wall_clock_s']}s "
          f"({report['throughput_per_hour']}/h)")
    print(f"  status: {report['status_counts']}")
    print(f"  creation: {report['creation_counts']}")
    print(f"  rollout budget: {report['rollout_budget']}")
    for r in report["results"]:
        mark = {"validated": "VAL ", "blocked": "BLOK", "unverified": "UNVR",
                "reserve": "RSRV", "error": "ERR "}.get(r["status"], "?   ")
        extra = r["error"] or (r["blockers"][0] if r["blockers"] else "")
        print(f"  {mark} {r['slug']:40s} t{r['tier']} "
              f"{r['stage_reached']:10s} {extra[:60]}")
    print(f"-> {out}")
    if screened is not None:
        from . import screen
        report_path = ARTIFACTS / f"screen-report-{Path(args.out).name}{suffix}.json"
        screen.write_report(screened, report_path)
        print()
        screen.print_table(screened)
        print(f"-> {report_path}")
        print("The pipeline stops here. pass@1 < 0.5 on both models is the strict "
              "target; it blocks nothing. Harden by hand (`arena harden <task> --apply`, "
              f"at most {_policy.MAX_HARDENING_ATTEMPTS} attempts per task); any task whose "
              f"screen measured cleanly may take the {_policy.SPEC_EPISODES_PER_MODEL}-episode "
              "calibration of record (`arena qualify`).")
    validated = report["status_counts"].get("validated", 0) == report["tasks"]
    screened_ok = screened is None or all(not r.get("error") for r in screened["rows"])
    return 0 if validated and screened_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
