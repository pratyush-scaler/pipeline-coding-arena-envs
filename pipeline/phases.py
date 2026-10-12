"""The pipeline as five phases, in the order they actually depend on each other.

    1  select      traces -> four source categories
    2  author      full trace visibility -> a complete environment, hard on the
                   FIRST pass: traps authored in, per the source category
    3  grade       validity only: gates, probes, controls, harbor tasks-check,
                   semantic review. >= policy.MIN_OBJECTIVES graded checks per
                   task, weights summing to policy.WEIGHT_TOTAL in every task
       -- barrier: the whole requested batch is authored and graded --
    4  calibrate   a SCREEN of every validated task: 5 episodes x 2 models ->
                   pass@1, pass@4, mean reward, attribution, hardening outlook.
                   The automatic pipeline STOPS here.
    5  harden      MANUAL ONLY -- never reached by any automatic path. Default
                   lifetime budget 3, explicit increases audited; each round ends in 5 x 2
                   screen. Any task whose screen measured cleanly may take the
                   10 x 2 calibration of record (`arena qualify`). pass@1 < 0.5
                   on both models is the strict target and blocks nothing

Nothing between phase 2 and phase 4 asks whether a task is hard. The design is
told to make it hard, the grade phase checks it is VALID, and difficulty is
measured once, for the whole batch, by the screen. The one-episode presolve that
used to end phase 2 -- and the in-build difficulty pilot -- fed difficulty back
into authoring one task at a time; both are gone.

    static gates, probes, controls   phase 3   ~$0    grader defects, leaks, weight drift
    semantic review                  phase 3   ~$3    incorrect graders
    harbor tasks-check               phase 3   ~$1    malformed packages
    screen (5 x 2)                   phase 4   ~$20   difficulty, attribution, outlook
    calibration of record (10 x 2)   manual    ~$39   what ships

Every phase is resumable, every phase records why it stopped, and no phase
treats "could not check" as "nothing wrong". A candidate that stops carries its
findings; it is never silently discarded, which is how 47 of 86 candidates were
lost in the batch that motivated this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import policy

#: Ordered, and the order is the dependency order. `harden` is in the tuple so
#: that it is named and documented, not so that anything runs it.
PHASES = ("select", "author", "grade", "calibrate", "harden")

#: Phases an automatic run may execute. Hardening is excluded by construction
#: rather than by a flag that could default wrong.
AUTOMATIC = ("select", "author", "grade", "calibrate")


@dataclass
class PhaseResult:
    phase: str
    ok: bool
    findings: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    # A stop that says nothing about the candidate's quality -- an outage, a
    # missing credential, an unreadable dump. Never counts as a rejection.
    unverified: bool = False

    def as_dict(self) -> dict:
        return {"phase": self.phase, "ok": self.ok, "findings": self.findings,
                "evidence": self.evidence, "unverified": self.unverified}


# ---------------------------------------------------------------------------
# 1. Select
# ---------------------------------------------------------------------------

def select(need: int, *, classified: Path | None = None,
           frontier_only: bool = False, exclude_sessions=()) -> PhaseResult:
    """Compose a batch from already-classified candidates, into four categories.

    Categories order *candidates* -- which trace to attempt first. They are a
    property of the source, and they are not what gets delivered: the delivery
    folder is the measured faithfulness level. See `ledger/levels.py`.

    Index/screen/tier/classify run ahead of this as their own modules; this
    phase is the composition step, which is where the four categories are
    actually assigned to slots.
    """
    import json

    from .config import ARTIFACTS
    from .select import shortlist

    path = Path(classified) if classified else Path(ARTIFACTS) / "classified.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return PhaseResult("select", ok=False, unverified=True,
                           findings=[f"no classified candidates at {path}: {exc}"])

    # An index built before the full evidence reader screened on extensions and
    # byte share, which is what collapsed the non-coding pool to 8 of 102.
    from .dump.evidence import SCHEMA as EVIDENCE_SCHEMA
    stale = [r for r in doc.get("rows", [])
             if r.get("classification", {}).get("survives")
             and r["classification"].get("evidence_schema") != EVIDENCE_SCHEMA]
    if stale:
        return PhaseResult("select", ok=False, unverified=True, findings=[
            f"{len(stale)} classifications predate the full evidence reader; "
            "rebuild the index and classify again before selection"])

    taken = {s.strip() for s in exclude_sessions if str(s).strip()}
    try:
        result = shortlist.compose(doc["rows"], size=need,
                                   frontier_only=frontier_only, taken=taken)
    except Exception as exc:                      # noqa: BLE001 -- recorded, not raised
        return PhaseResult("select", ok=False, unverified=True,
                           findings=[f"composition could not run: {exc}"])

    rows = result.get("batch", [])
    counts: dict = {}
    for row in rows:
        counts[row.get("category")] = counts.get(row.get("category"), 0) + 1
    unfilled = [s["slot"] for s in result.get("slots", []) if not s.get("filled")]
    return PhaseResult(
        "select", ok=bool(rows),
        evidence={"rows": rows, "by_category": counts, "requested": need,
                  "pool_size": result.get("pool_size"), "unfilled_slots": unfilled},
        # An unfilled slot is reported empty, never backfilled -- a batch that
        # cannot fill an axis is itself the finding.
        findings=[] if rows else ["no candidate survived selection"])


# ---------------------------------------------------------------------------
# 2. Author
# ---------------------------------------------------------------------------
# The build itself is `orchestrate._build_one`, and its exit condition is the
# grade phase below. There is deliberately no difficulty exit here: a draft is
# not screened on its own (user, 2026-09-25). The designer is told to make the
# task extremely hard on its first pass (`author.agent_designer.FIRST_PASS_TRAPS`)
# and the batch is measured together in phase 4.


# ---------------------------------------------------------------------------
# 3. Grade
# ---------------------------------------------------------------------------

def grade(bundle: Path, *, review_model: str | None = None) -> PhaseResult:
    """Everything that can be decided about the grader without spending rollouts.

    Static gates, the weight budget, the reward-hacking probes, the naming and
    faithfulness assignment, `harbor tasks-check`, and the independent semantic
    review. All of it is cheap relative to phase 4, so all of it runs first.
    """
    from .run import run_bundle
    from .author import solution
    from .benchmark import tasks_check
    from .ledger import levels
    from . import naming
    from .author.contracts import strict_json

    bundle = Path(bundle)
    findings, evidence = [], {}

    report = run_bundle(bundle, Path(__file__).resolve().parents[1])
    evidence["gates"] = report["verdict"]
    if report["verdict"] == "unverified":
        return PhaseResult("grade", ok=False, unverified=True, evidence=evidence,
                           findings=[f"{g['gate']}: {row['code']}: {row.get('message','')}"
                                     for g in report["results"] for row in g["blockers"]])
    findings += [f"{g['gate']}: {row['code']}: {row.get('message', '')}"
                 for g in report["results"] for row in g["blockers"]]

    # Faithfulness level, then the name that carries it. Both before any hash
    # is bound to the task: renaming later invalidates every later receipt.
    ledger_path = bundle / "provenance/ledger.json"
    if ledger_path.is_file():
        ledger = strict_json(ledger_path.read_text())
        level = ledger.get("faithfulness_level")
        if level:
            evidence["faithfulness_level"] = level
            findings += levels.problems({"level": level,
                                         "blocked": ledger.get("faithfulness_blocked", "")})
            findings += naming.problems(bundle, level)
        else:
            findings.append("the ledger carries no faithfulness level (F0-F3)")
    else:
        # No ledger, no level, no name to check. That is a finding, not a pass.
        findings.append("no provenance ledger (provenance/ledger.json); the "
                        "faithfulness level (F0-F3) and the name that carries "
                        "it cannot be assigned")

    findings += solution.problems(bundle)
    from .author import controls as controls_mod, probes as probes_mod
    findings += [f"controls: {p}" for p in controls_mod.problems(bundle)]
    findings += [f"probes: {p}" for p in probes_mod.problems(bundle)]

    check = tasks_check.run(bundle)
    evidence["harbor_tasks_check"] = check["status"]
    findings += tasks_check.problems(bundle)

    if review_model:
        from .review import review, review_problems
        try:
            result = review(bundle, review_model)
            evidence["semantic_review"] = result.get("ok")
            if not result.get("ok"):
                findings += review_problems(bundle, result)
        except Exception as exc:                  # noqa: BLE001
            return PhaseResult("grade", ok=False, unverified=True,
                               evidence=evidence,
                               findings=[f"semantic review unavailable: {exc}"])
    return PhaseResult("grade", ok=not findings, findings=findings, evidence=evidence)


# ---------------------------------------------------------------------------
# 4. Calibrate
# ---------------------------------------------------------------------------

def calibrate(bundles: list[Path], *, native_root: Path,
              task_workers: int = 8) -> PhaseResult:
    """The screen: `policy.SCREEN_EPISODES_PER_MODEL` episodes on each model.

    Run over the whole batch once it exists, and again by hand after every
    hardening round. Three questions, deliberately not blended:

      difficulty    pass@1 against the target (below 0.5 on BOTH models),
                    reported with pass@4 and mean reward
      attribution   is every failure the model's? Voids leave the denominator,
                    contract deaths are capped, all-fail checks carry proof
      outlook       how likely a hardening round is to bring the task under
                    the target -- a heuristic, reported, never a rejection

    A task above the target is a result, not a failure of this phase: `ok` is
    False only when a screen could not be taken or read cleanly.
    """
    from . import screen

    records = screen.run_many([Path(b) for b in bundles], native_root=Path(native_root),
                              task_workers=task_workers, label="batch")
    doc = screen.report(records)
    broken = [f"{r.get('bundle')}: {r.get('error') or '; '.join(r.get('shippable_problems', [])[:2])}"
              for r in records if r.get("error") or not r.get("measurement_complete")]
    return PhaseResult("calibrate", ok=not broken, findings=broken,
                       evidence={"report": doc})


def qualify(bundle: Path, *, native_root: Path, review_model: str,
            models: tuple[str, str] | None = None, workers: int = 2) -> PhaseResult:
    """The calibration of record: `policy.SPEC_EPISODES_PER_MODEL` x 2, after a clean screen.

    Not part of the automatic pipeline. `release._qualify` refuses to start the
    campaign unless the task's current screen measured cleanly -- valid
    episodes, every failure attributable, no unresolved reward hacking
    (`screen.shippable_problems`). Its pass@1 is never a condition.
    """
    from .release import qualify as release_qualify

    bundle = Path(bundle)
    primary, secondary = models or policy.resolve_calibration_models()
    try:
        result = release_qualify(bundle, models=[primary, secondary],
                                 review_model=review_model, blind_model=secondary,
                                 native_root=Path(native_root),
                                 n=policy.SPEC_EPISODES_PER_MODEL, workers=workers)
    except Exception as exc:                      # noqa: BLE001
        return PhaseResult("qualify", ok=False, unverified=True,
                           findings=[f"calibration of record could not run: {exc}"])
    return PhaseResult("qualify", ok=result.get("status") == "accepted",
                       evidence=result.get("evidence", {}),
                       findings=result.get("errors", []))


def analyse(bundle: Path) -> dict:
    """Phase 4's report: where the model failed, and which checks said nothing.

    Separate from the gate on purpose. A dead check is a defect in our authoring
    whether or not the task passed, and a task can pass with one.
    """
    from .author.contracts import strict_json
    from . import attribution
    from .measurement import objective_bands

    bundle = Path(bundle)
    index_path = bundle / "verification/native-calibrations.json"
    if not index_path.is_file():
        return {"available": False,
                "reason": "no native calibration index for this task version"}
    index = strict_json(index_path.read_text())

    # Per model first, then merged. A check is dead only if it failed on no
    # model, and unreachable only if no model attempted it: one model passing
    # everything says nothing about a check the other model failed.
    per_model, seen, attempted, failed = {}, set(), set(), set()
    for path in index.get("campaigns", []):
        from .release import calibration_measurement
        issues, measured = calibration_measurement(bundle, index, path,
                                                   policy.SPEC_EPISODES_PER_MODEL)
        model = measured.get("model")
        bands = objective_bands(
            {cid: rec for step in measured.get("by_step", {}).values()
             for cid, rec in step.get("checks", {}).items()})
        record = attribution.attribute(bundle, measured)
        per_model[model] = {"evidence_problems": issues, "bands": bands,
                            "attribution": record["counts"],
                            "all_fail": attribution.all_fail_problems(bundle, measured)}
        for step, data in (measured.get("by_step") or {}).items():
            for cid, rec in (data.get("checks") or {}).items():
                key = f"{step}/{cid}"
                seen.add(key)
                if rec.get("passed", 0) + rec.get("failed", 0):
                    attempted.add(key)
                if rec.get("failed", 0):
                    failed.add(key)
    unreachable = seen - attempted          # attempted on no model
    dead = attempted - failed               # failed on no model

    return {"available": True, "models": per_model,
            # A check every attempt passes on both models discriminates nothing.
            # It is not a reason to reject the task; it is a reason to fix it.
            "never_failed": sorted(dead),
            "never_attempted": sorted(unreachable),
            "note": "Checks that never failed cost weight without measuring "
                    "ability. Checks never attempted are gated out, not hard. "
                    "Neither is evidence the task is bad; both are authoring "
                    "defects worth repairing before hardening."}


# ---------------------------------------------------------------------------
# 5. Harden -- manual only
# ---------------------------------------------------------------------------

def harden(*_args, **_kwargs):
    """Never called by an automatic path. Use `arena harden <task>`.

    Kept as an explicit refusal rather than an absence, so that a future caller
    wiring hardening into a batch has to delete a statement that says not to.
    Hardening changes a task's difficulty after it has been measured; doing that
    automatically would tune tasks against their own calibration runs.
    """
    raise RuntimeError(
        "hardening is manual. Run `arena harden <task>` to see a report, and "
        "`arena harden <task> --apply` to write a revision. The revision "
        "requires full requalification.")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(bundle: Path, *, native_root: Path, review_model: str,
        repairs: int | None = None) -> dict:
    """Phases 3-4 over one built draft, with bounded validity repair between them.

    Selection and authoring are batch-level (`orchestrate`), and hardening is
    manual and never runs from here. The grade phase is validity only; the
    calibrate phase is the 5 x 2 screen. A screen that misses the difficulty
    target is a result, not a failure to repair -- only a grade failure, or a
    screen that could not measure cleanly, stops this run.
    """
    from . import repair

    bundle = Path(bundle)
    budget = policy.MAX_REPAIR_PASSES if repairs is None else repairs
    history: list[dict] = []

    for attempt in range(budget + 1):
        stages = [grade(bundle, review_model=review_model)]
        if stages[-1].ok:
            stages.append(calibrate([bundle], native_root=native_root))
        history.append({"attempt": attempt + 1,
                        "stages": [s.as_dict() for s in stages]})
        failed = next((s for s in stages if not s.ok), None)
        if failed is None:
            return {"ok": True, "attempts": history, "analysis": analyse(bundle)}

        # An unverified stop, or a screen that did not measure cleanly, is about
        # our ability to check -- never a reason to change the task.
        if failed.unverified or failed.phase == "calibrate":
            return {"ok": False, "unverified": True, "attempts": history,
                    "phase": failed.phase, "findings": failed.findings}

        if attempt >= budget:
            repair.disposition(bundle, failed.findings, "repair budget exhausted")
            return {"ok": False, "attempts": history, "phase": failed.phase,
                    "findings": failed.findings}

        allowed, why = repair.admissible(bundle, failed.findings)
        if not allowed:
            repair.disposition(bundle, failed.findings, why)
            return {"ok": False, "attempts": history, "phase": failed.phase,
                    "findings": failed.findings, "stopped_because": why}
        repair.record_pass(bundle, failed.findings, "revising", {"phase": failed.phase})
        try:
            repair.revise(bundle, failed.findings)
        except Exception as exc:                  # noqa: BLE001
            return {"ok": False, "unverified": True, "attempts": history,
                    "phase": failed.phase, "findings": [f"repair failed: {exc}"]}

    return {"ok": False, "attempts": history}
