#!/usr/bin/env python3
"""Stage H -- make a measured task genuinely harder, on purpose, by hand.

This is the one stage that never runs itself. `arena build` does not call it,
no environment variable turns it on, and it refuses a bundle that has not first
been calibrated. You pick the task; it revises a copy and proves the copy is
still legitimate. The original is never written to.

How a revision is made. The agent designer -- the same author that built the
task, with the whole fixture, the whole trace and its own evaluator in reach --
is handed the task's retained spec, the checks every model already passes, and
the hardening brief below. It adds traps grounded in the trace's scenario and
re-aims the free checks. New trap material can only enter as authored fixture
files (`write_fixture_file`, recorded in `authoring/extraction/fixture-
authored.json`), which earn no source credit and are bounded by the level's
fidelity allowance; the recovered source stays immutable.

Each attempt must then clear, in cost order:

  validation and the no-weakening rules   free
  scenario faithfulness at least F2       free (ledger re-measured)
  probes, gate chain, shipped grader      free

and a revision that clears them is then MEASURED: the calibration screen
(`screen.py`) -- `policy.SCREEN_EPISODES_PER_MODEL` episodes on each of the two
calibration models -- and a comparison against the version it revised. Only
its aggregate per-check pass rates reach the next attempt, never solver answers
or per-episode rewards. Any revision whose screen measured cleanly may take the
ten-episode calibration of record (`arena qualify`).

Faithfulness is budgeted, not hoped for. Before designing, the task's room is
computed from its ledger and traps (`faithfulness_budget`): how many authored
traps keep the trace-derived share at the level's floor, and how many authored
bytes keep fidelity at the level's and the category's floor. The brief states
it. Scenario hardening has no arbitrary authored trap/byte ceiling: validation
retains source provenance, real correction traps and at least F2 for F2/F3
sources. F3 may become F2 and the actual measured level is reported.

THE ATTEMPT BUDGET. A task defaults to `policy.MAX_HARDENING_ATTEMPTS` (3)
redesigns over its whole lineage -- the original and every revision built from
it, across every invocation -- recorded in `<original>/hardened/hardening.json`
and never reset. An explicit --attempt-budget increase is recorded without
erasing spend. Every returned revision counts, validity repairs included:

  attempt 1  HARDEN   the full brief, told it is the one attempt that must make
                      the task extremely hard; nothing held back for later
  then       validity checks; a valid revision is screened (5 x 2)
  attempt 2+ REPAIR   an invalid revision: fix exactly the cited findings
             TIGHTEN  a valid but still-easy revision: strengthen the existing
                      premises through the checks its screen shows still free

The lineage stops -- and `arena harden` then refuses it -- when a screen meets
the extreme observed target (pass@1 <= 0.05 on both models) with EVERY step at
or under `policy.STEP_PASS_RATE_MAX` on both models, when the budget is spent, when a validity finding repeats or the
finding set fails to shrink, when an informative comparison shows regression
on every model, or when source capacity is rejected during validation. Equal
small screens are inconclusive. Solver answers
and per-episode rewards are never fed back; per-check pass rates are, as they
always were.

What it may not change, each for a measured reason:

  the scenario or source  a fixture that drifts produces a task about nothing
  a check's existence     deleting a check raises the mean and teaches less
  `requires` chains       43% of kw-a010's weight sat behind checks nobody
                          reached; gating is not difficulty, it is unreachability

A built revision is a candidate, not a result. It is accepted only by the
comparison after its own two-model screen, and only when pass@1 falls with
every failure attributed to the model (`attribution.problems`).

Usage:
    ./arena harden --candidates          rank tasks by how much is hardenable
    ./arena harden <bundle>              report only; writes nothing
    ./arena harden <bundle> --apply      harden within the 3-attempt budget: revise,
                                         screen each valid revision (5 x 2), stop at
                                         target met / exhausted / no progress
                                         (--no-screen: build the revision only)
    ./arena harden <bundle> --apply --trap "IDEA" [--trap ...] [--traps ideas.json]
                                         ...also build the traps you ask for, each
                                         held to the same legitimacy rules and to
                                         the task's faithfulness budget (report
                                         mode prints that budget)
    ./arena harden <rev> --compare <bundle>   accept or reject a calibrated revision
"""
from __future__ import annotations

import contextlib
import json
import os
import re as _re_module
import shutil
import time
import uuid
from pathlib import Path

from . import policy
from .config import bundle_paths

ROOT = Path(__file__).resolve().parent.parent
LEVEL_RANK = {"F0": 0, "F1": 1, "F2": 2, "F3": 3}


def extreme_target_met(record: dict) -> bool:
    """Observed stopping target, not a statistically confirmed hardness claim."""
    if not record.get("measurement_complete"):
        return False
    models = record.get("models") or []
    rows = record.get("per_model") or {}
    return len(set(models)) >= 2 and all(
        m in rows and rows[m].get("pass_at_1") is not None
        and rows[m]["pass_at_1"] <= policy.EXTREME_TARGET_PASS_AT_1 for m in models)


def step_pass_rates(record: dict) -> dict[str, dict[str, float]] | None:
    """model -> step -> share of valid episodes that fully passed that step.

    Read from the screen's own campaign reports: the record keeps per-check
    marginals only, and a step can fail on a different check in every episode.
    None when a cited campaign cannot be read or a model has fewer valid
    episodes than the screen floor -- an unreadable rate never meets a target.
    """
    out = {}
    index_topups = (record.get("index") or {}).get("topups") or {}
    for model, result in (record.get("per_model") or {}).items():
        if not isinstance(result, dict) or not result.get("campaign"):
            return None
        cited = [result["campaign"], *(result.get("topups") or []),
                 *(index_topups.get(result["campaign"]) or [])]
        trials = []
        for campaign in dict.fromkeys(c for c in cited if isinstance(c, str)):
            try:
                report = json.loads((Path(campaign) / "report.json").read_text())
            except (OSError, ValueError):
                return None
            trials += [t for t in report.get("trials") or [] if isinstance(t, dict) and t.get("valid") is True]
        if len(trials) < policy.SCREEN_EPISODES_PER_MODEL:
            return None
        names = sorted({s.get("name") for t in trials for s in t.get("steps") or [] if s.get("name")})
        out[model] = {name: round(sum(1 for t in trials if any(
            s.get("name") == name and s.get("valid") is True and isinstance(s.get("reward"), (int, float))
            and s["reward"] >= 1.0 - 1e-9 for s in t.get("steps") or [])) / len(trials), 3) for name in names}
    return out or None


def steps_too_easy(record: dict) -> dict[str, list[str]] | None:
    """model -> steps fully passed by more than policy.STEP_PASS_RATE_MAX of its
    valid episodes; None when the per-step rates cannot be read."""
    rates = step_pass_rates(record)
    if rates is None:
        return None
    return {model: [step for step, rate in sorted(steps.items()) if rate > policy.STEP_PASS_RATE_MAX]
            for model, steps in rates.items()}


def lineage_target_met(record: dict) -> bool:
    """The lineage's stopping rule: the extreme whole-task target AND every step
    at or under the per-step bar on every model (user, 2026-10-03). A whole-task
    zero carried by one step does not end hardening; unreadable per-step rates
    never meet it."""
    if not extreme_target_met(record):
        return False
    easy = steps_too_easy(record)
    return easy is not None and not any(easy.values())


def _step_target_text(version: Path) -> str:
    """The per-step goal and the measured per-step rates of the version being revised."""
    from . import screen
    record, stale = screen.current(version), False
    if record is None:              # e.g. a provenance file changed after the screen
        try:
            record, stale = json.loads((Path(version) / "verification/screen.json").read_text()), True
        except (OSError, ValueError):
            record = {}
    rates = step_pass_rates(record) if record else None
    limit = policy.STEP_PASS_RATE_MAX
    lines = [f"\n\nPER-STEP GOAL (user, 2026-10-03): EVERY step must be fully passed by at most "
             f"{limit:.0%} of episodes on BOTH calibration models, and the aim is near zero. A whole-task "
             "pass@1 carried by one hard step does not count; each step is measured on its own."]
    if rates:
        if stale:
            lines.append("  (from the stored screen receipt; the bundle has changed since it was measured)")
        for step in sorted({s for model in rates.values() for s in model}):
            cells = ", ".join(f"{model.split('/')[-1]} {rates[model].get(step, 0):.0%}" for model in sorted(rates))
            easy = any(rates[model].get(step, 0) > limit for model in rates)
            lines.append(f"  {step}: {cells}{'   <-- TOO EASY: needs its own interlock' if easy else ''}")
    else:
        lines.append("  (no readable per-step rates for this version)")
    lines.append("For every step above the goal, route at least one INTERLOCK through checks OF THAT STEP, "
                 "aimed at BOTH models' measured failure modes (see the lever doctrine): a step-1 trap does "
                 "not make step 2 hard, and a step-2 trap does not make step 1 hard. Each later step runs in a "
                 "fresh session that rereads the workspace and its instruction, so its trap must bite a careful "
                 "fresh reader: a new published rule that silently re-governs code earlier steps wrote, an "
                 "input shape earlier code never met, a state the earlier steps left on disk. Every such trap "
                 "stays determinate: one published sentence settles it.")
    lines += _untouched_model_text(record or {})
    return "\n".join(lines)


#: Calibration model -> the lever catalogue's family tag (`levers.LEVERS[*]["targets"]`).
_FAMILY = {"openai": "astra", "anthropic": "fable"}


def _untouched_model_text(record: dict) -> list[str]:
    """For a model that failed no check at all, the catalogue's levers measured against it.

    The first 2026-10-04 screens: GPT-6 Astra passed every check of every
    episode on lever-doctrine designs while Fable already failed several. A
    revision that only widens the other model's live edges cannot move it;
    it needs a mechanism this model has actually been measured to miss.
    """
    from .author import levers
    # A check failed in EVERY episode is not a live edge, so live_edges alone
    # would call a model that fails one check always "untouched": read the
    # per-check counts when the receipt has them.
    failed_any = {model: any((c or {}).get("failed") for c in (row.get("checks") or {}).values())
                  for model, row in (record.get("per_model") or {}).items() if row.get("checks")}
    edges = ((record.get("outlook") or {}).get("signals") or {}).get("live_edges") or {}
    for model, failed in edges.items():
        failed_any.setdefault(model, bool(failed))
    out = []
    for model, failed in sorted(failed_any.items()):
        if failed:
            continue
        family = _FAMILY.get(model.split("/")[0])
        picks = [(name, entry) for name, entry in levers.LEVERS.items()
                 if entry.get("targets") == family and entry.get("status") in ("measured", "external")]
        if not picks:
            continue
        out.append(f"\n{model} FAILED NO CHECK IN ANY EPISODE: none of the current decisions bites it. Every step "
                   f"above the goal needs at least one interlock built on a lever {family.title()} has been "
                   "measured to miss (catalogue, measured or external evidence):")
        for name, entry in picks:
            out.append(f"  - {name}: {str(entry.get('recipe') or '')[:260]}  [evidence: "
                       f"{str(entry.get('evidence') or '')[:160]}]")
        out.append("  Prove it the doctrine's way: an m_baseline_ mutant that writes what this model's idiomatic "
                   "solution writes, and an m_partial_ per lever; both must fail while the reference passes.")
    return out


def _lever_text(bundle: Path, spec: dict, row: dict | None = None) -> str:
    """The levers this trace supports and the lever-plan audit of the measured version."""
    from .author import levers
    classification = (row or {}).get("classification") or {}
    corrections = [t.get("correction_class") for t in spec.get("traps") or []
                   if isinstance(t, dict) and t.get("correction_class")]
    lines = ["\n\n" + levers.relevant_text(classification, Path(bundle) / "environment/repo",
                                           corrections=corrections, steps=len(spec.get("steps") or []))]
    audit = levers.diagnostics(spec, Path(bundle))
    if audit["advisories"]:
        lines.append("LEVER-PLAN AUDIT OF THE MEASURED VERSION (advisory; each line is something the "
                     "revision should fix):")
        lines += [f"  - {a}" for a in audit["advisories"][:16]]
    return "\n".join(lines)


def _calibrations(bundle: Path) -> list[Path]:
    return sorted((bundle / "verification").glob("calibration-*.json"))


def baseline(bundle: Path) -> dict:
    """Verify current evidence and retain each model/step as a separate cohort."""
    from .artifacts import identity
    from .author.contracts import strict_json
    from .evidence import calibration_problems
    from .measurement import profile
    from .metrics import task_metrics
    reports, errors = [], []
    try:
        fingerprint = identity(bundle)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "models": [], "by_model": {},
                "reports": {}, "checks": {}, "means": []}
    index = bundle / "verification/native-calibrations.json"
    from . import screen
    screened = screen.current(bundle)
    if screened and not (index.is_file() and _index_current(index, fingerprint)):
        # The calibration screen of this exact version: what every freshly
        # built batch and every hardening round is measured by.
        from .release import calibration_measurement
        manifest = screened.get("index") or {}
        for path in manifest.get("campaigns", []):
            try:
                problems, measured = calibration_measurement(bundle, manifest, path, 1)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
                continue
            errors.extend(problems)
            reports.append(measured)
    elif index.is_file():
        try:
            manifest = strict_json(index.read_text())
            if manifest.get("artifact_sha256") != identity(bundle):
                raise ValueError("native calibration index belongs to a stale task version")
            from .release import calibration_measurement
            fallback = _screen_campaigns(bundle, screened)
            for path in manifest.get("campaigns", []):
                problems, measured = calibration_measurement(bundle, manifest, path, 1)
                model = measured.get("model")
                if any(p.startswith("need >= 1 valid native episodes") for p in problems) and model in fallback:
                    # Calibration plan of record (user, 2026-10-05): Fable from the
                    # ten-episode qualify, Astra from the 5-episode screen of this
                    # same version -- the qualify's Astra campaign was held empty
                    # on purpose, so its zero valid episodes are not a measurement.
                    problems, measured = fallback[model]
                errors.extend(problems)
                reports.append(measured)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    else:
        for file in _calibrations(bundle):
            try:
                report = strict_json(file.read_text())
                errors.extend(calibration_problems(bundle, report))
                reports.append(report)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{file.name}: {exc}")
    if not reports:
        errors.append("no calibration on record. Measure a frozen baseline first.")
    models, raw, checks, means = {}, {}, {}, []
    for report in reports:
        model = report.get("model")
        if not model or model in models:
            errors.append("missing or repeated model campaign; select one frozen campaign per model")
            continue
        steps = {step: profile(row["checks"]) for step, row in report.get("by_step", {}).items()}
        if not steps:
            errors.append(f"{model}: missing per-step counts")
        rates = task_metrics(report)
        models[model] = {"steps": steps, "protocol": report.get("protocol"),
                         "effort": report.get("effort"),
                         "limits": report.get("limits", {"max_steps": report.get("max_steps"),
                                                         "max_tokens": report.get("max_tokens")}),
                         "pass_at_1": rates["pass_at_1"],
                         "passes": rates["passes"],
                         "episodes_valid": rates["episodes_valid"]}
        raw[model] = report
        means.append(report.get("summary", {}).get("mean_reward"))
        for step, data in steps.items():
            for cid, rec in data["checks"].items():
                if rec.get("kind") == "guardrail":
                    continue
                key = f"{step}/{cid}"
                row = checks.setdefault(key, {"id": cid, "step": step, "weight": rec.get("weight", 0), "by_model": {}})
                row["by_model"][model] = rec
    return {"ok": not errors, "errors": errors, "models": list(models),
            "by_model": models, "reports": raw, "means": means, "checks": checks,
            "artifact_sha256": fingerprint}


def _screen_campaigns(bundle: Path, screened: dict | None) -> dict:
    """model -> (problems, measurement) for each campaign of this version's screen."""
    if not screened:
        return {}
    from .release import calibration_measurement
    manifest = screened.get("index") or {}
    out = {}
    for path in manifest.get("campaigns", []):
        try:
            problems, measured = calibration_measurement(bundle, manifest, path, 1)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if measured.get("model") and not problems:
            out[measured["model"]] = (problems, measured)
    return out


def _index_current(index: Path, fingerprint: str) -> bool:
    from .author.contracts import strict_json
    try:
        return strict_json(index.read_text()).get("artifact_sha256") == fingerprint
    except (OSError, ValueError):
        return False


def hardenable(bundle: Path) -> dict:
    """Select objectives easy on every measured model; never infer pooled core."""
    base = baseline(bundle)
    groups = {k: [] for k in ("floor", "already_hard", "in_band", "unreachable", "transition", "mixed_models")}
    for key, rec in sorted(base.get("checks", {}).items()):
        rows = rec["by_model"]
        bands = {r["band"] for r in rows.values()}
        if len(rows) != len(base["models"]) or "unreachable" in bands:
            groups["unreachable"].append(key)
        elif bands == {"floor"}:
            groups["floor"].append({"id": rec["id"], "step": rec["step"], "weight": rec["weight"],
                                    "rate": min(r["rate"] for r in rows.values())})
        else:
            group = ({"core": "in_band", "stretch": "already_hard", "transition": "transition"}
                     .get(next(iter(bands))) if len(bands) == 1 else "mixed_models")
            groups[group].append(key)
    return {**base, **groups, "free_weight": sum(c["weight"] for c in groups["floor"])}


def candidates(root: Path) -> list[dict]:
    """Rank built tasks by how much of their score is currently a free point."""
    rows = []
    for bundle in bundle_paths(root):
        report = hardenable(bundle)
        rows.append({"bundle": bundle.name,
                     "ok": report["ok"],
                     "errors": report["errors"],
                     "floor_checks": len(report["floor"]),
                     "free_weight": report["free_weight"],
                     "in_band": len(report.get("in_band", [])),
                     "already_hard": len(report.get("already_hard", [])),
                     "means": report.get("means", [])})
    rows.sort(key=lambda r: (-r["free_weight"], -r["floor_checks"]))
    return rows


HARDENING_BRIEF = """\
HARDEN THIS TASK. The staged draft is a working, calibrated coding task that
frontier models find too easy. Make it genuinely hard, so that a strong model
fails it for its own reasons and never for ours.

Checks every measured model already passes -- the free points you may re-aim:
{targets}

How each check behaved in calibration (the trap has to move these):
{breadth}

1. UNDERSTAND FIRST. Read the corrections, the rejected turns and the workspace
   history (list_corrections, read_trace, snapshot_boundaries, diff_workspace),
   and the fixture as it stands. Reproduce the current defect in sandbox_run.
   The traps must belong to this repository and this scenario.

2. BUILD INTERLOCKS, NOT SINGLE TRAPS (the CODING DIFFICULTY DOCTRINE at the top
   of your instructions). Noticing is free for these agents: a trap they can find
   and transcribe costs them nothing, and symptom-vs-cause, chained defects, decoy
   locations, restraint and announced revisions measured 1.00/1.00 here as the
   puzzle. What bit is a plainly published rule whose REALISATION goes wrong on a
   hidden input the visible material never showed: Fable over-reaches a closed
   rule's scope and misses rare rule interactions; Astra codes one exact predicate
   and misses a member in another shape. So re-aim every free check below through
   an INTERLOCK -- two or more catalogue levers on ONE hidden item, one aimed at
   each family, where resolving all but one lever still fails and each partial
   resolution fails differently -- and give EVERY step its own. Prove each with an
   idiomatic-baseline mutant (m_baseline_...) and one partial mutant per lever
   (m_partial_...), and record it in quality.lever_plan. Keep each trap's affected
   population small, and spread the difficulty over several INDEPENDENT decisions
   (LEARNING SIGNAL below) -- never several checks hanging off one premise as the
   whole difficulty. Vary the levers across items.

3. ROUTE GENUINE OBJECTIVES THROUGH EACH PREMISE. One failed objective already
   defeats full-task success. Re-aim the free checks above so
   they read through DIFFERENT traps, one decision each; never split one behaviour into several checks
   to multiply its weight. Every new trap needs its OWN MUTANT (a plausible
   wrong fix that fails exactly the checks the trap drives, passes at least one
   objective) and its misreading in quality.difficulty_case. Every rule a test
   enforces is a sentence in the instruction (rule_quote): a failure caused by
   an unpublished rule is not the model's.

4. EVERY TRAP GENUINE: grounded (trace_derived with the user's words, or
   authored with the fixture file it lives in), resolvable from the published
   instruction, beatable (the reference and the alternative both pass),
   graded (drives at least one check), proved (its mutant runs in the controls).
   No hidden rules: a trap is as deceptive as the EVIDENCE legitimately allows,
   never the instruction -- the requirements themselves stay perfectly clear.

{requested}5. KEEP FAITHFULNESS. Lifted files carry byte provenance; anything you write
   with write_fixture_file is authored (zero credit) and must not lower the
   source provenance. Record the measured level; scenario hardening may trade F3
   for F2. Prefer traps on code the repository already has.

   Your budget for this task, measured from its ledger and traps:
{budget}

6. DO NOT: delete or weaken a check or a test; add tolerance; add `requires`
   chains (unreachable is not hard); change the scenario or source; add
   meaningless volume; tune against solver answers. The band limits still hold
   (<<BANDS>>): add a check only where a band has room, otherwise pay for a
   root check by re-aiming a free one. Controls are useful advisory tools during
   creation; the validation phase runs them authoritatively after submission.

Submit the strongest candidate even if probes or difficulty advisories are weak.
Source rejection is reserved for evidence that no fair coding task is possible.
"""


def _signal_text(version) -> str:
    """The measured version's learning-signal problems (pipeline.learning_signal), for the brief."""
    from . import learning_signal
    try:
        screen = json.loads((Path(version) / "verification/screen.json").read_text())
        found = screen.get("signal_problems")
        if found is None:
            sig = learning_signal.from_screen(Path(version), screen)
            found = learning_signal.problems(sig) + learning_signal.advisories(sig)
        else:
            found = list(found) + list(screen.get("signal_advisories") or [])
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not found:
        return ""
    return ("  MEASURED ON THIS VERSION -- fix these without weakening any trap:\n"
            + "".join(f"    - {p}\n" for p in found))


def _bands_text() -> str:
    from .author.quality import BAND_LIMITS
    return ", ".join(f"{band} {low}-{high}" for band, (low, high) in BAND_LIMITS.items())


HARDENING_BRIEF = HARDENING_BRIEF.replace("<<BANDS>>", _bands_text())

#: What the designer is told about WHICH attempt this is. The first is the one
#: that must do the work (user, 2026-09-25): no incremental hardening.
FIRST_ATTEMPT = """\
ATTEMPT {n} OF {of} -- THE ONE THAT MUST MAKE THIS TASK EXTREMELY HARD.
There is no incremental hardening loop. This task gets at most {of} redesigns,
ever ({left} left after this one), and any after this exist only to repair what
this revision breaks or to tighten what its measurement shows it missed.
Hold nothing back for a later round: install every genuine premise trap this
source supports NOW, of varied
types, route the free checks through them -- each through its own independent decision -- and make each as deceptive as the
EVIDENCE legitimately allows -- never the requirements, which stay perfectly
clear -- while the task keeps meeting every requirement of
the ARENA QUALITY BAR (system prompt): every rule published, every check
failable, passable and reachable, nothing leaked, grounded in the trace's
situation. A revision that is merely harder than the original has wasted most of
the budget; one that trades quality for difficulty is broken. Run the TRAP HUNT again across every
pitfall family (system prompt) for this revision, and rewrite
quality.difficulty_case: one misreading per trap, each encoded by a
counterexample, and pitfalls_considered covering every trap.
"""

TIGHTEN_ATTEMPT = """\
ATTEMPT {n} OF {of} -- TIGHTEN, DO NOT START OVER. The staged revision is valid
and was measured on both calibration models: it is still too easy. The per-check
pass rates below are REAL measurements of it -- use them. Keep every trap and
every check it has. You may change ONLY the checks listed below as free (passed
by every attempt), and add checks where a band has room; every other check stays
exactly as it is (a changed measured check is refused). Make the premises bite
harder through the fixture (write_fixture_file, within the budget) and by
re-aiming those free checks through them; add a trap only where a premise is
missing entirely. Controls are optional during creation and authoritative in
validation after submission. Attempts left after this one: {left}.
"""

REPAIR_ATTEMPT = """\
ATTEMPT {n} OF {of} -- REPAIR ONLY. The staged revision failed the validity
checks listed after this message. Fix exactly those findings. Do not redesign,
do not add traps, and do not remove or weaken a trap or a check that is valid:
the difficulty this revision installed is the point. A finding that comes back
unchanged ends hardening for this task. Attempts left after this one: {left}.
Your faithfulness budget, measured from the ORIGINAL task:
{budget}
"""


#: Explicit preserve-level mode only; default scenario hardening has no
#: arbitrary authored-byte cap and validates the measured final provenance.
MAX_AUTHORED_BYTES = 128 * 1024


def faithfulness_budget(bundle: Path, ledger: dict, *, preserve_level: bool = True) -> dict:
    """How many authored traps and authored bytes this task can take and keep its level.

    F3 needs fidelity >= the F3 floor AND >= the F3 share of traps citing a real
    correction; F2 holds on fidelity >= the F2 floor OR the F2 trap share. Each
    authored trap dilutes the trap share and each authored byte the fidelity, so
    both have a ceiling in preserve-level mode. Scenario mode returns no
    arbitrary ceilings; validation checks the actual resulting level.
    """
    import math
    from .ledger import levels
    record = levels.assign(bundle, ledger)
    level, measured = record["level"], record["measured"]
    total, derived = measured["traps_total"] or 0, measured["traps_trace_derived"] or 0
    fidelity = ledger.get("faithfulness_by_bytes") or 0.0
    size = sum((ledger.get("bytes") or {}).values())
    weighted = fidelity * size
    if not preserve_level:
        return {"level": level, "minimum_level": f"F{min(2, LEVEL_RANK[level])}",
                "preserve_level": False, "traps_total": total, "traps_trace_derived": derived,
                "fidelity": fidelity, "authored_traps_left": None,
                "authored_bytes_left": None,
                "scenario_bytes_used": (ledger.get("bytes") or {}).get("authored", 0)}
    # Fidelity may not fall below the level's floor nor the target's, so authored
    # bytes (write_fixture_file) are capped by the stricter of the two.
    floors = [f for f in (policy.FAITHFULNESS_FIDELITY_FLOOR.get(level),
                          policy.CATEGORY_FIXTURE_FLOOR.get(ledger.get("category"))) if f]
    bytes_ = min((math.floor(weighted / f - size) for f in floors), default=None)
    share_floor = policy.FAITHFULNESS_TRACE_DERIVED_FLOOR.get(level)
    traps = (max(0, math.floor(derived / share_floor + 1e-9) - total)
             if share_floor else None)
    used = (ledger.get("bytes") or {}).get("authored", 0)
    cap = MAX_AUTHORED_BYTES - used
    bytes_ = cap if bytes_ is None else min(bytes_, cap)
    return {"level": level, "traps_total": total, "traps_trace_derived": derived,
            "fidelity": fidelity, "authored_traps_left": traps,
            "authored_bytes_left": max(0, bytes_), "scenario_bytes_used": used}


def budget_problems(budget: dict, before: list[dict], after: list[dict],
                    bytes_added: int) -> list[str]:
    """Did a revision spend more faithfulness than the original task had to spare?"""
    def authored(traps):
        return sum(1 for t in traps if t.get("provenance") == "authored")
    problems = []
    added = authored(after) - authored(before)
    left = budget["authored_traps_left"]
    if left is not None and added > left:
        problems.append(f"the revision adds {added} authored trap(s); this {budget['level']} "
                        f"task has room for {left} before its trace-derived share falls "
                        "below the level's floor. Use real corrections instead")
    derived_before = {t.get("id") for t in before if t.get("provenance") == "trace_derived"}
    dropped = derived_before - {t.get("id") for t in after if t.get("provenance") == "trace_derived"}
    if dropped:
        problems.append(f"the revision removes or relabels trace-derived trap(s) {sorted(dropped)}; "
                        "a trap from a real correction is the task's faithfulness")
    if budget["authored_bytes_left"] is not None and bytes_added > budget["authored_bytes_left"]:
        problems.append(f"the revision adds {bytes_added} authored scenario bytes; the "
                        f"budget is {budget['authored_bytes_left']}")
    return problems


#: One genuine objective failure defeats full-task success. Multiple checks
#: can diagnose propagation but are not necessary evidence of a real trap.
MIN_CHECKS_PER_NEW_TRAP = 1


def routing_problems(before: list[dict], after: list[dict], checks: list[dict]) -> list[str]:
    """Every new trap must affect observable graded behaviour."""
    objectives = {c.get("id") for c in checks if c.get("kind") == "objective"}
    known = {t.get("id") for t in before}
    out = []
    for trap in after:
        if trap.get("id") in known:
            continue
        driven = [c for c in trap.get("checks") or [] if c in objectives]
        if len(driven) < MIN_CHECKS_PER_NEW_TRAP:
            out.append(f"new trap {trap.get('id')} drives {len(driven)} graded objective(s); route "
                       f"at least {MIN_CHECKS_PER_NEW_TRAP} through its genuine failure case")
    return out


def _budget_text(budget: dict) -> str:
    traps = budget["authored_traps_left"]
    lines = [f"     level {budget['level']}; {budget['traps_trace_derived']} of "
             f"{budget['traps_total']} traps cite a real correction; fixture fidelity "
             f"{budget['fidelity']:.0%}",
             "     authored traps you may add: "
             + ("no ceiling from this level" if traps is None else str(traps))
             + ". Traps citing a real user correction do not count against this -- "
               "prefer them.",
             "     authored scenario bytes you may add: "
             + ("no arbitrary ceiling" if budget["authored_bytes_left"] is None else str(budget["authored_bytes_left"]))]
    if budget.get("preserve_level") is False:
        lines.append(f"     Record the actual measured level; validation retains at least {budget['minimum_level']}. "
                     "F3 may become F2. Preserve real citations and ground every authored decoy in the scenario.")
    if traps == 0:
        lines.append("     You have NO room for authored traps: make the existing trace-"
                     "derived traps harder, or add traps that cite real corrections.")
    return "\n".join(lines)


#: User, 2026-10-04: "Fix all the tasks based on the review, and some ambiguity is
#: required in the task, as far as the task remains correct and is understandable
#: and performable by a human carefully, the task should be valid" -- and the
#: hardness must be maintained: every step at or under the bar on both models.
REVIEW_REPAIR = """

THE INDEPENDENT REVIEWER'S VALIDITY FINDINGS on the version you start from. Repair
EVERY one in this revision; each is a defect of the task, not of the models:
{items}

THE REPAIR PROTOCOL -- execution-gated, one finding at a time (2026-10-14: every hard
revision that reached release was rejected, and fixes made without reproducing a finding
or checking their own diff missed findings or created new ones):
  1. Number the findings R1..Rn as listed. Each ends FIXED or REFUTED; none is skipped
     and none is changed silently.
  2. Reproduce before fixing: write the finding's counterexample as a hidden case and
     sandbox_grade the CURRENT reference and alternative. A static claim the reviewer did
     not execute is a hypothesis (LLM reviewers over-flag correct code): if the case
     passes on the current reference, the finding is REFUTED -- change no code for it,
     keep the case, and put the executed evidence in the affected check's basis.
  3. Fix the root cause in the reference AND the alternative (every step's patch and
     solution/fix.patch), then sweep for the same class of defect: cover what the
     published instruction settles and record what it does not under untested_surfaces.
  4. Regression gate after every change: the reference (three runs) and the alternative
     stay at 1.0, and run_controls holds for every control and mutant exactly as declared.
  5. Scope: change only what a finding needs. Before submitting, read your diff against
     the starting version; every changed file, check, case, sentence and patch must trace
     to a numbered finding, and anything that does not is reverted.
  6. Write the resolution ledger in solution_notes, one line per finding:
     "R<k> <id>: FIXED|REFUTED -- case <name>: before <result>, after <reference and
     alternative results>; files <paths>". The reviewer reads it.
  7. pre_review the final draft and resolve whatever it raises with this same protocol;
     do not submit while it reports a substantive gap you have not FIXED or REFUTED with
     executed evidence.

How to judge each one (the user's standard): some ambiguity is required, and a task
is valid as long as it stays CORRECT and a careful human can understand and perform
it from the instruction and the repository.
  - An ambiguity a careful reader resolves stays. Prove it with determinacy_probe:
    both families, asked directly about that input, must give the reference's
    answer. Record the probe in the check's basis.
  - If a careful reader could defensibly reach a different answer, or the rule is
    nowhere stated, publish it in the user's own voice: one plain sentence, no
    heading, no list, and do not name the trap's mechanism.
  - Never drop or weaken a check or a hidden case to escape a finding.
  - Always repair these, then add the reviewer's counterexample as a hidden case:
      - an incorrect or crashing reference: fix the reference AND the alternative;
      - a shortcut that forges an observation: move it to the trusted side of the
        process boundary and prove it with a mutant that now fails;
      - a privacy finding.
  - Authoring bookkeeping, such as a tamper attack listed as a semantic mutant:
    reclassify it.
The hardness must not fall. Every step must stay at or under {bar} on BOTH models,
and every counted failure must be a capability failure. Where publishing a rule
makes a trap visible, put a new interlocked hidden case on that step: one lever
from each family (Fable: scope / long tail / one branch / interaction / state;
Astra: unseen shape or identity / exact predicate / convention).
Re-grade the captured episodes, or blind-solve with both families, to confirm that
the step still bites.
"""


def review_repairs(version: Path) -> list[str]:
    """The review's validity findings for `version`, or -- when `version` itself was
    never reviewed -- those on its nearest reviewed ancestor in the lineage, marked as
    such (c-029 / c-031, 2026-10-04: a v3 built on a flagged v2 carried no review)."""
    from .review import review_findings
    from .author.contracts import strict_json
    version = Path(version).resolve()
    found = review_findings(version)
    if found or (version / "verification/semantic-review.json").is_file():
        return found
    root = lineage_root(version)
    try:
        attempts = read_ledger(root).get("attempts", [])
    except (OSError, ValueError):
        return []
    seen, current = set(), version
    while current != root.resolve() and current not in seen:
        seen.add(current)
        rel = _rel(root, current)
        parent = next((a.get("from") for a in reversed(attempts) if a.get("revision") == rel), None)
        if parent is None:
            return []
        current = (root if parent in (".", "") else root / parent).resolve()
        try:
            strict_json((current / "verification/semantic-review.json").read_text())
        except (OSError, ValueError):
            continue
        return [f"[on the earlier {current.name if current != root.resolve() else 'original'}, which this "
                f"version descends from -- repair it if it still applies here] {text}"
                for text in review_findings(current)]
    return []


#: A stop is infrastructure, not a finding, when the last attempt's findings are all a review
#: that could not run (2026-10-05: an OpenAI key out of credit made three fix rounds' reviews
#: "unverified", and the repeated "finding" stopped c-009-go and c-043). Infra loss is re-run.
_INFRA_FINDING = ("could not be verified", "provider error", "credit_balance", "insufficient_quota",
                  "transport interrupted", "semantic review unverified")


def _infra_stopped(ledger: dict) -> bool:
    last = next((a for a in reversed(ledger.get("attempts") or []) if a.get("findings")), None)
    findings = [str(f) for f in (last or {}).get("findings") or []]
    return bool(findings) and all(any(m in f for m in _INFRA_FINDING) for f in findings)


#: An operator-required fix (`arena harden --fix`), kept apart from requested traps.
FIX_PREFIX = "FIX: "


def _requested_text(requested: list[str]) -> str:
    if not requested:
        return ""
    fixes = [r[len(FIX_PREFIX):] for r in requested if r.startswith(FIX_PREFIX)]
    traps = [r for r in requested if not r.startswith(FIX_PREFIX)]
    out = ""
    if fixes:
        # 2026-10-05, c-002-express-glb: the pinned dependency set lacked packages the
        # repository declares, so an agent re-linked node_modules and was flagged.
        out += ("4a. FIXES THE OPERATOR REQUIRES -- defects of the task found in measured evidence.\n"
                "   Make every one. Change nothing else beyond what the fix needs: the instruction,\n"
                "   the hidden tests, the reference, the traps and the difficulty stay as they are, and\n"
                "   every step must stay at or under the per-step bar on both models. Never drop or\n"
                "   weaken a check.\n" + "\n".join(f"   - {fix}" for fix in fixes) + "\n\n")
    if traps:
        items = "\n".join(f"   - {idea}" for idea in traps)
        out += ("4b. TRAPS THE OPERATOR ASKED FOR. Build each one if this source genuinely\n"
                "   supports it, under every rule in 4 and within the budget in 5. If one\n"
                "   cannot be made legitimate here, leave it out; never bend a rule for it.\n"
                f"{items}\n\n")
    return out


def _breadth_summary(bundle: Path, checks: list[dict]) -> str:
    """Per-check calibration pass rates, so the designer sees what never fails.

    (Named for its history: it used to print the measurement-breadth screen,
    whose lever the per-check calibration data has since superseded.)
    """
    report = hardenable(bundle)
    lines = []
    for key, rec in sorted(report.get("checks", {}).items()):
        rates = [r.get("rate") for r in rec["by_model"].values() if r.get("rate") is not None]
        if rates:
            lines.append(f"  {key}: passed by {min(rates):.0%}-{max(rates):.0%} of attempts "
                         f"(weight {rec.get('weight')})")
    return "\n".join(lines) or "  no per-check calibration on record"


#: The last spec the designer returned for a working copy, kept whether or not
#: it validated. A REPAIR attempt continues from it: continuing from the copy's
#: authoring/spec.json -- the pre-revision spec when validation rejected the
#: revision before it was built -- threw the new traps away.
HARDENING_DRAFT = "verification/hardening-draft.json"


def propose(bundle: Path, targets: list[dict], findings: list[str] | None = None,
            requested: list[str] | None = None, *, number: int = 1,
            of: int = policy.MAX_HARDENING_ATTEMPTS, kind: str = "harden",
            measured: Path | None = None, left: int = 0, tighten: bool = False,
            fast: bool = False) -> dict:
    """Ask the agent designer for a harder version of THIS spec, inside `bundle`.

    `bundle` is the working copy: scenario supplements the designer writes land
    there, never in the original. The source and category are carried across
    verbatim rather than trusted to come back unchanged.

    `kind` decides the brief: "harden" (the full brief -- the extremely-hard
    redesign, or with `tighten` a tightening of a measured revision) or
    "repair" (fix the cited validity findings only, continuing from the last
    returned draft). `measured` is the version whose calibration the per-check
    table is read from -- the working copy has none of its own.
    """
    from .author import agent_designer, extract
    from .repair import design_inputs

    if not (bundle / "authoring" / "spec.json").is_file():
        raise FileNotFoundError(
            f"{bundle / 'authoring' / 'spec.json'} is absent, so there is no design "
            "to revise. Bundles built before the spec was retained cannot be "
            "hardened; rebuild the task first.")
    spec, ledger, row = design_inputs(bundle)
    staged = spec
    draft = Path(bundle) / HARDENING_DRAFT
    if kind == "repair" and draft.is_file():
        from .author.contracts import strict_json
        staged = strict_json(draft.read_text())
    if not policy.HARDENING_PRESERVE_LEVEL:
        import copy
        staged, row = copy.deepcopy(staged), dict(row)
        # This is an authoring target, not a claim about the measured ledger.
        if staged.get("target_level") == "F3":
            staged["target_level"] = "F2"
        row["authoring_target_level"] = staged.get("target_level") or "F2"
    if kind == "repair":
        brief = REPAIR_ATTEMPT.format(n=number, of=of, left=left,
                                      budget=_budget_text(faithfulness_budget(
                                          measured or bundle, design_inputs(measured or bundle)[1],
                                          preserve_level=policy.HARDENING_PRESERVE_LEVEL)))
    else:
        rendered = "\n".join(
            f"  {c['id']}  (weight {c['weight']}, passed by {c['rate']:.0%} of attempts)"
            for c in targets) or "  none"
        header = (TIGHTEN_ATTEMPT.format(n=number, of=of, left=left) if tighten
                  else FIRST_ATTEMPT.format(n=number, of=of, left=left))
        brief = header + "\n" + HARDENING_BRIEF.format(
            targets=rendered,
            breadth=_breadth_summary(measured or bundle, spec.get("checks", [])),
            requested=_requested_text(requested or []),
            budget=_budget_text(faithfulness_budget(bundle, ledger,
                                                  preserve_level=policy.HARDENING_PRESERVE_LEVEL)))
        from .author.difficulty import EXTREME_PROMPT
        brief += (f"\nAim for pass@1 <= {policy.EXTREME_TARGET_PASS_AT_1} on EACH model, with the same model's runs "
                  "differing (partial scores varied; an occasional full pass is fine, identical failures are not).\n"
                  + EXTREME_PROMPT)
        from . import learning_signal as _learning_signal
        brief += "\n" + _learning_signal.guidance() + _signal_text(measured or bundle)
        brief += _lever_text(measured or bundle, spec, row)
        if kind == "extend":
            from .author.cases import checkpoint_template
            brief += ("\nNEW CHECKPOINT LINEAGE: retain every existing step, public instruction, check and behavioral requirement. "
                      "Append meaningful checkpoints that expose earlier design assumptions. Step inventory may grow in this "
                      "new lineage; do not remove, reorder or weaken prior work. New objectives may have their own dependencies. "
                      "No old/full-task rate comparison is legitimate after extending the task. Template: "
                      + json.dumps(checkpoint_template()))
        # The independent reviewer's difficulty advisories are advisory for
        # admission but exactly the input a hardening redesign should use.
        from .review import review_advisories
        advisories = review_advisories(measured or bundle)
        if advisories:
            brief += ("\n\nTHE INDEPENDENT REVIEWER'S DIFFICULTY ADVISORIES -- where it judged "
                      "this task easier or less substantive than claimed; address these first:\n"
                      + "\n".join(f"  - {text[:500]}" for text in advisories[:8]))
    # The independent review's VALIDITY findings on the version being revised are
    # repaired in the same round, never traded for difficulty (user, 2026-10-04).
    repairs = review_repairs(measured or bundle)
    if repairs:
        brief += REVIEW_REPAIR.format(items="\n".join(f"  - {text[:2400]}" for text in repairs[:12]),
                                      bar=policy.STEP_PASS_RATE_MAX)
    if kind != "repair":
        brief += _step_target_text(measured or bundle)
    options = {}
    if fast:
        # A hidden-only round (fastharden): the designer re-grades its draft
        # against every captured episode of the measured version.
        from . import fastharden, screen as _screen
        episodes = fastharden.captured(measured or bundle)
        if not episodes:
            raise RuntimeError("no captured episodes of the measured version to harden against")
        row = dict(row, _captured_episodes=episodes)
        options["max_steps"] = fastharden.FAST_MAX_STEPS
        brief = (fastharden.FAST_BRIEF + "\n" + fastharden.episode_summary(
            episodes, _screen.current(measured or bundle)) + "\n\n" + brief)
    source = spec["source"]
    revised = agent_designer.design(
        row, extract.extract(source["session_id"], source["seed_index"]), bundle,
        initial_spec=staged, feedback=[brief, *(findings or [])], revision=True, **options)
    for field in ("source", "category"):
        if field in spec:
            revised[field] = spec[field]
    return revised


#: Fields of a check that only describe it. Rewording them when the published
#: instruction is reworded changes no verdict (2026-10-04: five hardening
#: lineages were stopped for a reworded guardrail `basis` or `rule_quote`).
_DESCRIPTIVE = ("basis", "rule_quote", "band", "weight_basis", "trap", "sentence")


def _guardrail_weakened(old: dict, new: dict) -> str | None:
    """Why a revision's change to an untargeted guardrail weakens it, or None.

    A guardrail may be reworded and may be STRENGTHENED -- a shadow guard over
    the modules new hidden tests import, more hash-guarded files, more selected
    tests, a tighter tree allowlist -- but never weakened, retyped or reweighted.
    """
    a = {k: v for k, v in old.items() if k not in _DESCRIPTIVE}
    b = {k: v for k, v in new.items() if k not in _DESCRIPTIVE}
    if a == b:
        return None
    for key in ("kind", "weight", "step", "requires", "id"):
        if a.get(key) != b.get(key):
            return f"{key} changed"
    ra, rb = a.get("runner") or {}, b.get("runner") or {}
    if ra.get("type") != rb.get("type"):
        return "runner type changed"
    rest_a = {k: v for k, v in ra.items() if k not in ("modules", "paths", "select", "allow", "allowlist")}
    rest_b = {k: v for k, v in rb.items() if k not in ("modules", "paths", "select", "allow", "allowlist")}
    if rest_a != rest_b:
        return "runner settings changed"
    for key in ("modules", "paths", "select"):
        if not set(ra.get(key) or []) <= set(rb.get(key) or []):
            return f"runner {key} lost entries"
    for key in ("allow", "allowlist"):
        if not set(rb.get(key) or []) <= set(ra.get(key) or []):
            return f"runner {key} admits more"
    other = {k for k in set(a) | set(b) if k != "runner" and a.get(k) != b.get(k)}
    return f"changed {sorted(other)}" if other else None


def revision_problems(before: dict, after: dict, targets: list[dict], *, new_lineage: bool = False) -> list[str]:
    """Keep guardrails and dependency chains fixed while re-aiming objectives.

    Until 2026-10-04 only the floor checks (`targets`) could change. Every
    hardening attempt ever made here (8 attempts on 4 tasks) was refused for
    touching some other objective: an interlock re-aims several decisions at
    once, and the premise a revision changes runs through checks the screen
    measured in band. Whether a revision is harder is the next screen's
    question; this gate keeps what it can honestly keep -- no check removed,
    no guardrail changed unless targeted, no dependency chain rewired.
    """
    errors = []
    old = {c["id"]: c for c in before.get("checks", [])}
    new = {c["id"]: c for c in after.get("checks", [])}
    allowed = set() if new_lineage else (
        {c["id"] for c in targets} | {cid for cid, c in old.items() if c.get("kind") == "objective"})
    if not set(old) <= set(new):
        errors.append(f"revision removes measured checks {sorted(set(old) - set(new))}; "
                      "removing work raises the mean and measures less")
    for cid, check in new.items():
        if cid in old and cid not in allowed and check != old[cid]:
            why = _guardrail_weakened(old[cid], check)
            if why:
                errors.append(f"{cid}: revision changed an untargeted check ({why})")
        if cid in old and check.get("requires", []) != old[cid].get("requires", []):
            errors.append(f"{cid}: changing dependency chains cannot supply difficulty")
    old_steps, new_steps = before.get("steps", []), after.get("steps", [])
    budget = before.get("step_budget")
    if type(budget) is int and len(new_steps) > max(budget, len(old_steps)):
        errors.append(f"revision has {len(new_steps)} steps; this task's step budget is {budget} "
                      "(about 90% of tasks are single-step)")
    if new_lineage:
        if len(new_steps) <= len(old_steps) or new_steps[:len(old_steps)] != old_steps:
            errors.append("new checkpoint lineage must append steps and retain every prior instruction")
        for path, content in (before.get("suites") or {}).items():
            if (after.get("suites") or {}).get(path) != content:
                errors.append(f"new checkpoint lineage changed a prior hidden suite: {path}")
    elif [s.get("name") for s in old_steps] != [s.get("name") for s in new_steps]:
        errors.append("revision changed the step inventory")
    return errors


def level(bundle: Path, ledger: dict) -> str:
    """Faithfulness from fixture and traps alone, for comparing a revision.

    Calibration evidence is deliberately left out on both sides: the revision
    cannot have been calibrated yet, so an F1 original would always read as a
    drop. The revision's own measured level is assigned at delivery.
    """
    from .ledger import levels
    return levels.assign(bundle, ledger)["level"]


def diff_checks(before: dict, after: dict) -> dict:
    """What the revision actually did, so a human can review it in one screen."""
    old_by_id = {c["id"]: c for c in before.get("checks", [])}
    new_by_id = {c["id"]: c for c in after.get("checks", [])}
    changed = [cid for cid in old_by_id.keys() & new_by_id.keys()
               if json.dumps(old_by_id[cid], sort_keys=True)
               != json.dumps(new_by_id[cid], sort_keys=True)]
    return {"added": sorted(new_by_id.keys() - old_by_id.keys()),
            "removed": sorted(old_by_id.keys() - new_by_id.keys()),
            "changed": sorted(changed),
            "unchanged": sorted((old_by_id.keys() & new_by_id.keys())
                                - set(changed))}


class DesignerUnavailable(RuntimeError):
    """The designer returned no revision (transport, provider, deadline).

    Nothing was designed, so no hardening attempt was spent. The invocation
    stops rather than retrying: infrastructure loss is re-run by the operator.
    """


def attempt(original: Path, target: Path, targets: list[dict], findings: list[str],
            requested: list[str] | None = None, *, number: int = 1,
            of: int = policy.MAX_HARDENING_ATTEMPTS, kind: str = "harden",
            left: int = 0, tighten: bool = False, on_proposed=None, proposed: dict | None = None,
            fast: bool = False) -> list[str]:
    """One revision of `target`, then every free validity check. Returns findings.

    Validity only. Whether the revision is HARDER is measured afterwards by the
    round's calibration screen. Raises `DesignerUnavailable` when the designer
    returned nothing (no attempt spent); `SeedRejected` and `AuthoringStalled`
    pass through for the caller to record.
    """
    from .artifacts import atomic_json
    from .author import build as build_stage
    from .author import probes as probe_stage
    from .author.contracts import strict_json
    from .author.validate import validate
    from .repair import design_inputs, rebuild_ledger
    from .run import run_bundle
    from .verify_shipped import run_reference as verify_shipped

    from .author.outcomes import AuthoringStalled, SeedRejected
    before, before_ledger, row = design_inputs(original)
    try:
        revised = proposed if proposed is not None else propose(target, targets, findings, requested, number=number, of=of,
                                                               kind=kind, measured=original, left=left, tighten=tighten,
                                                               fast=fast)
    except AuthoringStalled as exc:
        # The stalled session's draft is the work this attempt did: keep it as
        # the draft the next REPAIR continues from, instead of the pre-revision
        # spec -- the new traps are in it, unfinished or not.
        stalled = target / str(exc.report.get("session") or "") / "draft.json"
        if exc.report.get("session") and stalled.is_file():
            try:
                atomic_json(target / HARDENING_DRAFT, strict_json(stalled.read_text()))
            except ValueError:
                pass  # an unreadable draft is no draft; the stall is still raised
        raise
    except SeedRejected:
        raise
    except Exception as exc:
        raise DesignerUnavailable(f"{type(exc).__name__}: {exc}") from exc
    # A revision now exists: the attempt is spent from here on, whatever
    # happens to the checks below -- including an interruption.
    atomic_json(target / HARDENING_DRAFT, revised)
    if on_proposed is not None:
        on_proposed()
    from .author.agent_designer import validate_source_capacity
    try:
        source_rejection = validate_source_capacity(revised, target)
    except ValueError as exc:
        return [f"source-capacity validation: {exc} [verification/hardening-draft.json]"]
    if source_rejection:
        raise SeedRejected(source_rejection)
    from . import fastharden
    hidden_only = fastharden.hidden_only_problems(before, revised) if fast else []
    # A check the independent review marked for revision is one this round must repair, so it
    # counts as targeted; no check may still be removed or have its dependency chain rewired.
    from .review import review_flagged_checks
    reviewed = [{"id": cid} for cid in sorted(review_flagged_checks(original))]
    if problems := validate(revised, target) + revision_problems(before, revised, targets + reviewed,
                                                                  new_lineage=kind == "extend") \
            + hidden_only:
        return problems
    build_stage.build(revised, target)
    if fast and (problems := fastharden.hidden_only_problems(before, revised, original, target)):
        return problems
    traps = strict_json((target / "authoring/traps.json").read_text())
    traps["usable_corrections"] = row["classification"]["usable_moments"]
    atomic_json(target / "authoring/traps.json", traps)
    ledger = rebuild_ledger(target, before_ledger)
    was, now = level(original, before_ledger), level(target, ledger)
    print(f"  faithfulness: {was} -> {now}, fidelity "
          f"{before_ledger.get('faithfulness_by_bytes')} -> {ledger.get('faithfulness_by_bytes')}")
    minimum = LEVEL_RANK[was] if policy.HARDENING_PRESERVE_LEVEL else min(2, LEVEL_RANK[was])
    if LEVEL_RANK[now] < minimum:
        return [f"faithfulness level fell from {was} to {now}; traps must come from the "
                "trace and the recovered documents, not replace them"]
    # The budget is the ORIGINAL task's: a second attempt may not spend what the
    # first attempt already spent, and nothing is measured against the copy.
    budget = faithfulness_budget(original, before_ledger, preserve_level=policy.HARDENING_PRESERVE_LEVEL)
    old_traps = strict_json((original / "authoring/traps.json").read_text()).get("traps", [])
    new_traps = traps.get("traps", [])
    added = faithfulness_budget(target, ledger)["scenario_bytes_used"] - budget["scenario_bytes_used"]
    known = {t.get("id") for t in old_traps}
    for t in new_traps:
        if t.get("id") not in known:
            print(f"  + trap {t.get('id')} [{t.get('type')}, {t.get('provenance')}] "
                  + (t.get("derived_from") or f"grounded in {t.get('grounded_in')}"))
    if problems := budget_problems(budget, old_traps, new_traps, max(0, added)) \
            + routing_problems(old_traps, new_traps, revised.get("checks", [])):
        return problems
    from .author import controls as controls_stage
    controls = controls_stage.run(target)
    probes = probe_stage.run(target, controls_receipt=controls)
    if controls["errors"]:
        return ["controls: " + e for e in controls["errors"]]
    gates, grader = run_bundle(target, Path.cwd()), verify_shipped(target)
    print(f"  gates: {gates['verdict']}   shipped grader: {'ok' if grader['ok'] else 'FAILED'}")
    problems = ([] if gates["verdict"] == "green" else [f"gate chain is {gates['verdict']}"]) \
        + ([] if grader["ok"] else ["the reference no longer scores 1.0; that is a defect, not difficulty"])
    if not problems and os.environ.get("ARENA_HARDEN_REVIEW") == "1":
        # The independent semantic review a build runs, opt-in (user, 2026-10-04: readiness
        # rests on the genuineness certificate; the review must not slow hardening).
        from .orchestrate import Budget, _review_candidate
        reviewed = _review_candidate(Path(target).resolve(), Budget(1))
        print(f"  semantic review: {reviewed['verdict']}")
        if reviewed["verdict"] != "green":
            problems += [f"semantic review {reviewed['verdict']}: {str(f)[:400]}"
                         for f in reviewed.get("findings") or ["no findings recorded"]]
    return problems


def extend_lineage(source: Path, target: Path, requested: list[str], *, validate: bool = False) -> int:
    """Create a new task scope without pretending its rate is an old-task improvement."""
    from .artifacts import atomic_json, identity
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.is_relative_to(source):
        raise ValueError("new checkpoint lineage needs a fresh destination outside its source")
    origin = {"schema": "arena-coding/checkpoint-lineage/1", "parent": str(source),
              "parent_artifact_sha256": identity(source), "created_at": time.time(),
              "parent_hardening": read_ledger(lineage_root(source)),
              "comparison_scope": "new task with appended checkpoints; fresh full-task baseline required"}
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("hardened", "verification", "__pycache__", "*.pyc"))
    atomic_json(target / "verification/lineage-origin.json", origin)
    revised = propose(target, [], requested=requested, kind="extend", measured=source)
    revised["_lineage_origin"] = {"parent_artifact_sha256": origin["parent_artifact_sha256"],
                                  "comparison_scope": origin["comparison_scope"]}
    atomic_json(target / HARDENING_DRAFT, revised)
    atomic_json(target / "authoring/spec.json", revised)
    receipt = {"phase": "creation", "validation_deferred": True, "origin": origin}
    atomic_json(target / "verification/lineage-validation.json", receipt)
    if not validate:
        print(f"retained checkpoint candidate at {target}; validation deferred")
        return 0
    findings = attempt(source, target, [], [], requested, kind="extend", proposed=revised)
    atomic_json(target / "verification/lineage-validation.json",
                {"phase": "validation", "validation_deferred": False,
                 "verdict": "blocked" if findings else "green", "findings": findings, "origin": origin})
    print(f"checkpoint lineage validation: {'blocked' if findings else 'green'}; candidate retained at {target}")
    return 1 if findings else 0


def acceptance(before: dict, after: dict, gates_green: bool,
               grader_ok: bool, suspect: list[str]) -> dict:
    """Accept a calibrated revision that is harder on the same cohorts, validly.

    Harder means pass@1 fell on every model not already at zero. An all-fail
    revision is admissible (POLICY.md): the burden sits on `suspect`, which
    carries every attribution problem, so a zero that is not the model's blocks.
    This is a candidate comparison, never release authorization.
    """
    invalid = list(suspect)
    if not gates_green:
        invalid.append("the gate chain no longer passes")
    if not grader_ok:
        invalid.append("the reference no longer scores 1.0; this is a defect, not difficulty")
    for label, report in (("baseline", before), ("revision", after)):
        if report.get("ok") is not True:
            invalid.append(f"{label} lacks verified current evidence: {report.get('errors', [])}")
    reasons = list(invalid)
    old, new = before.get("by_model", {}), after.get("by_model", {})
    if set(old) != set(new) or len(new) < 2:
        reasons.append("comparison needs the same two or more named model cohorts")
    rates = {}
    from .measurement import compare_success
    comparisons = {}
    for model, measured in new.items():
        previous = old.get(model, {})
        if any(previous.get(k) != measured.get(k) for k in ("protocol", "effort", "limits")):
            reasons.append(f"{model}: protocol, effort or budgets changed")
        if set(previous.get("steps", {})) != set(measured.get("steps", {})):
            reasons.append(f"{model}: step inventory changed")
        for step, current in measured.get("steps", {}).items():
            if not set(previous.get("steps", {}).get(step, {}).get("checks", {})) <= set(current["checks"]):
                reasons.append(f"{model}/{step}: removed checks cannot demonstrate improvement")
        if (measured.get("episodes_valid") or 0) < policy.SCREEN_EPISODES_PER_MODEL:
            reasons.append(f"{model}: {measured.get('episodes_valid')} valid episodes; a "
                           f"hardening round is compared on a screen of "
                           f"{policy.SCREEN_EPISODES_PER_MODEL} after voids")
        p0, p1 = previous.get("pass_at_1"), measured.get("pass_at_1")
        rates[model] = {"before": p0, "after": p1}
        comparisons[model] = compare_success(previous, measured, alpha=.05 / max(1, len(new)))
        if p0 is None or p1 is None:
            reasons.append(f"{model}: pass@1 unmeasured")
        elif p1 >= p0 and p0 > 0:
            reasons.append(f"{model}: pass@1 did not fall ({p0:.2f} -> {p1:.2f}); models are "
                           "never pooled, and a lower mean reward alone is not a harder task")
    harder = bool(rates) and all(r["after"] is not None and r["before"] is not None
                                 and (r["after"] < r["before"] or r["before"] == 0)
                                 for r in rates.values())
    worst = max((r["after"] for r in rates.values() if r["after"] is not None), default=None)
    disposition = ("invalid" if invalid else "inconclusive" if not harder and
                   any(r["status"] == "inconclusive" for r in comparisons.values()) else "not_harder" if not harder else
                   "unmeasured" if worst is None else
                   "extreme_target_observed" if worst <= policy.EXTREME_TARGET_PASS_AT_1 else
                   "hard" if worst <= policy.SPEC_HARD_MAX else
                   "meets_difficulty" if worst < policy.SPEC_GENERAL_DIFFICULTY_MAX else
                   "harder_but_easy")
    return {"accept": not reasons, "valid": not invalid, "disposition": disposition,
            "pass_at_1": rates, "reasons": reasons, "harder": harder,
            "uncertainty": comparisons, "comparison_scope": "exploratory development; fresh confirmation required",
            "release_ready": False}


def requested_traps(ideas: list[str], path: str | None) -> list[str]:
    """Operator trap ideas from --trap and --traps, as brief lines."""
    out = [i.strip() for i in ideas if i and i.strip()]
    if path:
        for item in json.loads(Path(path).read_text()):
            if isinstance(item, str):
                out.append(item.strip())
            elif isinstance(item, dict) and str(item.get("idea", "")).strip():
                grounding = str(item.get("grounding", "")).strip()
                out.append(item["idea"].strip() + (f" (grounding: {grounding})" if grounding else ""))
            else:
                raise ValueError(f"{path}: each trap is a string or {{'idea': ...}}")
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Raise a chosen task's difficulty, by hand, with evidence")
    ap.add_argument("bundle", nargs="?", help="the bundle to harden")
    ap.add_argument("--candidates", action="store_true",
                    help="rank built tasks by how much is hardenable")
    ap.add_argument("--root", default=str(ROOT / "envs"),
                    help="where to look for bundles (with --candidates)")
    ap.add_argument("--compare", help="compare this measured revision to its original bundle")
    ap.add_argument("--apply", action="store_true",
                    help="write the hardened version; default reports only")
    ap.add_argument("--trap", action="append", default=[], metavar="IDEA",
                    help="a trap you want added (repeatable); held to every legitimacy "
                         "and faithfulness rule, left out if the source cannot support it")
    ap.add_argument("--traps", metavar="FILE",
                    help="JSON list of trap ideas: strings, or objects with 'idea' "
                         "and optional 'grounding'")
    ap.add_argument("--no-screen", action="store_true",
                    help="build the revision only; skip the round's 5 x 2 calibration "
                         "screen (run `arena screen <revision>` later)")
    ap.add_argument("--attempt-budget", type=int, help="explicit lifetime budget increase; every spent attempt is retained")
    ap.add_argument("--extend-steps", action="store_true", help="author a separate lineage with appended checkpoints")
    ap.add_argument("--fast", action="store_true",
                    help="hidden-only round: instructions and fixture frozen, the designer re-grades its draft "
                         "against the measured version's captured episodes (fastharden); a fresh screen confirms")
    ap.add_argument("--reopen-stopped", action="store_true",
                    help="operator decision: reopen a lineage stopped by repeated findings (recorded)")
    ap.add_argument("--fix", action="append", metavar="DEFECT",
                    help="a defect of the task the round must fix (repeatable); nothing else changes")
    ap.add_argument("--review-repair", action="store_true",
                    help="repair the semantic review's validity findings on the latest measured revision")
    ap.add_argument("--out", help="fresh destination for --extend-steps")
    ns = ap.parse_args(argv)

    if ns.candidates:
        rows = candidates(Path(ns.root))
        if not rows:
            print(f"no bundles under {ns.root}")
            return 2
        print(f"{'bundle':44s} {'free wt':>8s} {'floor':>6s} {'in band':>8s} "
              f"{'hard':>5s}   means")
        for r in rows:
            if not r["ok"]:
                print(f"{r['bundle']:44s} {'--':>8s}   {r['errors'][0][:60]}")
                continue
            print(f"{r['bundle']:44s} {r['free_weight']:8} {r['floor_checks']:6} "
                  f"{r['in_band']:8} {r['already_hard']:5}   "
                  + ", ".join(f"{m:.2f}" for m in r["means"] if m is not None))
        print("\n'free wt' is score currently sitting in checks every attempt "
              "passes.\nPick from the top and run:\n  ./arena harden <bundle>")
        return 0

    if not ns.bundle:
        ap.error("give a bundle, or --candidates to see what is available")
    bundle = Path(ns.bundle)
    if not (bundle / "task.toml").is_file():
        print(f"{bundle} is not a built bundle")
        return 2

    if ns.extend_steps:
        if not ns.out:
            ap.error("--extend-steps needs a fresh --out directory")
        return extend_lineage(bundle, Path(ns.out), requested_traps(ns.trap, ns.traps), validate=ns.apply)

    report = hardenable(bundle)
    if ns.compare:
        from .artifacts import atomic_json
        from .attribution import problems as attribution_problems
        from .run import run_bundle
        from .verify_shipped import run_reference as verify
        suspect = list(report.get("unreachable", []))
        for model, measured in report.get("reports", {}).items():
            suspect += [f"{model}: {p}" for p in attribution_problems(
                bundle, measured, policy.SCREEN_EPISODES_PER_MODEL)]
        result = acceptance(hardenable(Path(ns.compare)), report,
                            run_bundle(bundle, Path.cwd())["verdict"] == "green",
                            verify(bundle)["ok"], suspect)
        atomic_json(bundle / "verification/hardening-comparison.json", result)
        print(json.dumps(result, indent=2))
        return 0 if result["accept"] else 1

    print(f"=== {bundle.name}")
    if not report["ok"]:
        for e in report["errors"]:
            print(f"  REFUSED: {e}")
        return 1
    print(f"  calibrated against: {', '.join(report['models'])}")
    print("  pass@1: " + ", ".join(f"{m} {r['pass_at_1']:.2f}" for m, r in report["by_model"].items()
                                   if r["pass_at_1"] is not None))
    print(f"\n  free points -- {len(report['floor'])} check(s) every attempt passes, "
          f"{report['free_weight']}% of the score:")
    for c in report["floor"]:
        print(f"    {c['id']:32s} weight {c['weight']:3}  pass {c['rate']:.0%}")
    print(f"\n  leave alone: {len(report['in_band'])} discriminating, "
          f"{len(report['already_hard'])} already hard")
    if report["unreachable"]:
        print(f"  unreachable (fix the `requires` chain first): {', '.join(report['unreachable'])}")
        return 1
    ledger_path = bundle / "provenance/ledger.json"
    if ledger_path.is_file():
        print("\n  faithfulness budget for new traps:")
        print(_budget_text(faithfulness_budget(bundle, json.loads(ledger_path.read_text()),
                                             preserve_level=policy.HARDENING_PRESERVE_LEVEL)))
    lineage = read_ledger(lineage_root(bundle))
    print(f"\n  hardening attempts left: {attempts_left(lineage)} of "
          f"{lineage.get('max_attempts', policy.MAX_HARDENING_ATTEMPTS)}; lineage {lineage.get('status', 'open')}"
          + (f" -- {TERMINAL[lineage['status']]}" if lineage.get("status") in TERMINAL else ""))
    if not ns.apply:
        print("\n  (report only -- pass --apply to build a harder version)")
        return 0

    requested = requested_traps(ns.trap, ns.traps) + [FIX_PREFIX + fix for fix in (ns.fix or [])]
    try:
        return run_lineage(bundle, report, requested, screen=not ns.no_screen, attempt_budget=ns.attempt_budget,
                           fast=ns.fast, review_repair=ns.review_repair, reopen_stopped=ns.reopen_stopped)
    except RuntimeError as exc:
        print(f"  REFUSED: {exc}")
        return 1


# ---------------------------------------------------------------------------
# The attempt budget
# ---------------------------------------------------------------------------

#: Where a lineage's attempts are recorded: inside `<original>/hardened/`,
#: beside the revisions, so the original task is never written to.
LEDGER = "hardened/hardening.json"

#: Lineage states that end hardening for good. `open` is the only live one.
TERMINAL = {"target-met": "a clean screen observed pass@1 <= 0.05 on each model with every step at or "
                          "under the per-step bar (provisional)",
            "exhausted": "its hardening attempts are spent",
            "no-progress": "an informative exploratory comparison showed regression on every model",
            "stopped": "a validity finding repeated or the finding set did not shrink",
            "source-rejected": "the designer found the source cannot support genuine hard traps"}


def lineage_root(bundle: Path) -> Path:
    """The original task a revision descends from (`<root>/hardened/vN/...`)."""
    root = Path(bundle).resolve()
    while root.parent.name == "hardened":
        root = root.parent.parent
    return root


def read_ledger(root: Path) -> dict:
    from .author.contracts import strict_json
    path = Path(root) / LEDGER
    try:
        return strict_json(path.read_text())
    except (OSError, ValueError):
        return {"schema": "arena-pipeline/hardening/1", "status": "open",
                "max_attempts": policy.MAX_HARDENING_ATTEMPTS, "attempts": [], "screens": [],
                "interruptions": []}


def attempts_left(ledger: dict) -> int:
    return max(0, ledger.get("max_attempts", policy.MAX_HARDENING_ATTEMPTS) - len(ledger.get("attempts", [])))


def _save(root: Path, ledger: dict) -> None:
    from .artifacts import atomic_json
    ledger["target_pass_at_1"] = policy.EXTREME_TARGET_PASS_AT_1
    ledger["step_pass_rate_max"] = policy.STEP_PASS_RATE_MAX
    atomic_json(Path(root) / LEDGER, ledger)


@contextlib.contextmanager
def _lineage_lock(root: Path):
    """One hardening process per lineage: two would both spend the budget."""
    import fcntl
    path = Path(root) / "hardened" / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another hardening run owns {root.name}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _next_revision(root: Path, source: Path) -> Path:
    """A fresh sibling under the ORIGINAL's `hardened/`, copied from `source`.

    Numbered past every revision on disk AND every one the ledger recorded: a
    recorded revision whose directory was deleted must never lend its name --
    and its "valid" row -- to a new, unscreened copy.
    """
    root = Path(root)
    taken = [int(p.name[1:]) for p in (root / "hardened").glob("v*") if p.name[1:].isdigit()]
    ledger = read_ledger(root)
    for row in ledger.get("attempts", []) + ledger.get("screens", []):
        name = Path(str(row.get("revision") or "")).name
        if name[:1] == "v" and name[1:].isdigit():
            taken.append(int(name[1:]))
    target = root / "hardened" / f"v{max(taken, default=1) + 1}"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        "hardened", "verification", "__pycache__", "*.pyc"))
    return target


def _rel(root: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix() or "."
    except ValueError:
        return str(path)


def measured_progress(before: dict, after: dict) -> bool | None:
    """Inconclusive exploratory comparisons keep searching within the recorded budget."""
    from .measurement import compare_success
    if set(before) != set(after) or len(after) < 2:
        return None
    rows = [compare_success(before[m], after[m], alpha=.05 / len(after)) for m in after]
    if any(row["status"] == "improved" for row in rows):
        return True
    return False if all(row["status"] == "regressed" for row in rows) else None


def _resume_point(root: Path, ledger: dict) -> Path | None:
    """The lineage's latest valid revision, when hardening is asked of the original.

    None when no attempt produced a valid revision (the original is then the
    right starting point). Raises when the latest valid revision has no clean
    screen: continuing from an older one, or from the original, would discard
    it, so it has to be measured first.
    """
    from . import screen
    latest = next((a for a in reversed(ledger.get("attempts", []))
                   if a.get("outcome") == "valid" and (Path(root) / a.get("revision", "")).is_dir()
                   and a.get("revision") not in (None, ".")), None)
    if latest is None:
        return None
    revision = Path(root) / latest["revision"]
    record = screen.current(revision)
    if record is None or not record.get("measurement_complete"):
        raise RuntimeError(
            f"{latest['revision']} is this lineage's latest valid revision and has no clean "
            f"screen; measure it before hardening further:\n"
            f"    {screen.rescreen_command(revision, record)}\n    ./arena harden {revision} --apply")
    return revision


def _judge_revision(root: Path, revision: Path, ledger: dict) -> str | None:
    """Apply the stop rules to a revision the operator resumes from.

    Resuming with `arena harden <vN> --apply` used to spend the next attempt
    without ever reading vN's screen: a revision that had already met the target
    was redesigned, and one that made no progress was tightened anyway. Returns
    a terminal status, "open", or None when vN has no screen to judge (its
    measurement then comes from its calibration index, as for an original).
    Raises when vN's screen exists but did not measure cleanly.
    """
    from . import screen
    record = screen.current(revision)
    if record is None:
        return None
    if not record.get("measurement_complete"):
        raise RuntimeError(f"{revision.name}'s screen did not measure cleanly "
                           f"({'; '.join(record.get('shippable_problems', [])[:2])}); "
                           f"re-screen it (`{screen.rescreen_command(revision, record)}`) "
                           "before hardening further")
    rel = _rel(root, revision)
    parent = next((a.get("from") for a in reversed(ledger.get("attempts", []))
                   if a.get("revision") == rel), None)
    progress = (measured_progress(hardenable(root if parent in (None, ".") else root / parent).get("by_model", {}),
                                  hardenable(revision).get("by_model", {})) if parent is not None else None)
    if not any(sc.get("revision") == rel for sc in ledger.get("screens", [])):
        ledger["screens"].append({"revision": rel, "meets_target": bool(record.get("meets_target")),
                                  "shippable": bool(record.get("shippable")), "progress": progress,
                                  "outlook": (record.get("outlook") or {}).get("verdict")})
    if lineage_target_met(record):
        return "target-met"
    if progress is False:
        return "no-progress"
    return "open"                   # progressed, or not comparable (None)


def _certify(revision: Path) -> dict:
    """`genuine.audit` of a revision's fresh screen, re-grading every counted failure."""
    from . import genuine
    try:
        doc = genuine.audit(revision, regrade=True)
    except Exception as exc:                              # noqa: BLE001 -- uncertified, never certified
        return {"certified": False, "problems": [f"audit failed: {type(exc).__name__}: {exc}"[:300]]}
    return {"certified": doc["certified"], "kinds": doc["kinds"], "counted_failures": doc["counted_failures"],
            "problems": doc["problems"][:12]}


_RETIRED_GUARDRAIL_FINDING = _re_module.compile(r"^\S+: revision changed an untargeted check$")


def _rule_stopped_revision(root: Path, ledger: dict) -> Path | None:
    """The retained revision of a lineage stopped ONLY by the retired guardrail rule, if it now passes."""
    invalid = [a for a in ledger.get("attempts") or [] if a.get("outcome") == "invalid"]
    if not invalid or not attempts_left(ledger):
        return None
    last = invalid[-1]
    findings = last.get("findings") or []
    if not findings or not all(_RETIRED_GUARDRAIL_FINDING.match(f) for f in findings):
        return None
    revision = Path(root) / str(last.get("revision") or "")
    draft = revision / HARDENING_DRAFT
    if not draft.is_file():
        return None
    from .author.contracts import strict_json
    try:
        before = strict_json((Path(root) / "authoring/spec.json").read_text())
        after = strict_json(draft.read_text())
    except (OSError, ValueError):
        return None
    if any("untargeted check" in p for p in revision_problems(before, after, [])):
        return None
    return revision


def run_lineage(bundle: Path, report: dict, requested: list[str], screen: bool = True,
                *, attempt_budget: int | None = None, fast: bool = False,
                review_repair: bool = False, reopen_stopped: bool = False) -> int:
    """Harden `bundle` within its lineage's attempt budget. The one loop.

    Each iteration is one designer call. An attempt is recorded the moment the
    designer returns a revision -- before building or checking it -- so an
    interruption can never hand back an attempt that was spent. The loop cannot
    outlive the budget, stops on anything that says another redesign would
    repeat the last one, and never ends a lineage on a measurement that did not
    measure cleanly.
    """
    from .author.outcomes import AuthoringStalled, SeedRejected
    root = lineage_root(bundle)
    with _lineage_lock(root):
        ledger = read_ledger(root)
        if attempt_budget is not None:
            previous = ledger.get("max_attempts", policy.MAX_HARDENING_ATTEMPTS)
            if type(attempt_budget) is not int or attempt_budget < previous or attempt_budget < len(ledger["attempts"]):
                raise ValueError("attempt budget may only increase; spent attempts cannot be erased")
            ledger.setdefault("budget_changes", []).append({"before": previous, "after": attempt_budget, "at": time.time()})
            ledger["max_attempts"] = attempt_budget
            if ledger.get("status") == "exhausted" and attempts_left(ledger):
                ledger["status"] = "open"
            _save(root, ledger)
        budget = ledger.get("max_attempts", policy.MAX_HARDENING_ATTEMPTS)
        if ledger.get("status") == "no-progress":
            # Historical equal small screens did not establish futility.
            ledger["previous_progress_status"] = "no-progress"
            ledger["status"] = "open"
        if ledger.get("status") == "target-met" and (
                ledger.get("target_pass_at_1", 1.) > policy.EXTREME_TARGET_PASS_AT_1
                or ledger.get("step_pass_rate_max", 1.) > policy.STEP_PASS_RATE_MAX):
            # An old general-target stop, or one judged without the per-step
            # bar, does not certify the current goal.
            # Keep every spent attempt; only reconsider the obsolete stop label.
            ledger["previous_target_status"] = {"status": "target-met",
                                                "target": ledger.get("target_pass_at_1", policy.CALIBRATION_TARGET_PASS_AT_1)}
            ledger["status"] = "open"
        retained = _rule_stopped_revision(root, ledger) if ledger.get("status") == "stopped" else None
        if retained is not None:
            # Stopped only by the pre-2026-10-04 guardrail rule (a reworded or
            # strengthened guardrail read as "changed"): the design was never
            # judged. Its retained revision is re-validated, not redesigned.
            ledger.setdefault("reopened", []).append(
                {"at": time.time(), "from": "stopped", "revision": _rel(root, retained),
                 "why": "every finding was the retired guardrail rule; re-validating the retained revision"})
            ledger["status"] = "open"
            _save(root, ledger)
        if reopen_stopped and ledger.get("status") == "stopped":
            # An explicit operator decision (user, 2026-10-05: "Can we fix these tasks" for four
            # named review-flagged tasks), recorded beside the stop it overrides.
            ledger.setdefault("reopened", []).append(
                {"at": time.time(), "from": "stopped", "revision": _rel(root, Path(bundle).resolve()),
                 "why": "operator reopened a stopped lineage (user decision, 2026-10-05)"})
            ledger["status"] = "exhausted" if not attempts_left(ledger) else "open"
            _save(root, ledger)
        review_resume = None
        fixing = any(str(r).startswith(FIX_PREFIX) for r in requested or [])
        infra_stop = (ledger.get("status") == "stopped" and _infra_stopped(ledger))
        if fixing and not review_repair and (ledger.get("status") in ("target-met", "exhausted", "open") or infra_stop):
            # An operator-required fix of a defect found in evidence (2026-10-05: incomplete
            # pinned dependencies) reopens the lineage at the NAMED version; the fixed revision
            # is held to the same bar by a fresh screen.
            named = Path(bundle).resolve()
            review_resume = (named if named != root.resolve()
                             else _resume_point(root, ledger) or root.resolve())
            if ledger.get("status") != "open":
                ledger.setdefault("reopened", []).append(
                    {"at": time.time(), "from": ledger.get("status"), "revision": _rel(root, review_resume),
                     "why": "operator-required fix: " + "; ".join(
                         str(r)[len(FIX_PREFIX):][:200] for r in requested if str(r).startswith(FIX_PREFIX))})
                ledger["status"] = "open"
                _save(root, ledger)
        if review_repair and ledger.get("status") in ("target-met", "exhausted", "open"):
            # Met the bar, but the independent review found the task itself defective
            # (user, 2026-10-04: "Fix all the tasks based on the review"). Reopen to repair
            # that version; the repair is held to the same bar by a fresh screen.
            # A named revision is repaired as named (c-029, 2026-10-04: its reviewed v2 met
            # the bar while a later, never-measured v3 would otherwise be the resume point).
            named = Path(bundle).resolve()
            review_resume = (named if named != root.resolve()
                             else _resume_point(root, ledger) or root.resolve())
            if review_resume is None or not review_repairs(review_resume):
                raise RuntimeError(f"{root.name}: --review-repair needs a measured revision "
                                   "with current review findings")
            if ledger.get("status") != "open":
                ledger.setdefault("reopened", []).append(
                    {"at": time.time(), "from": ledger.get("status"), "revision": _rel(root, review_resume),
                     "why": "the independent review found validity defects in this revision"})
                ledger["status"] = "open"
                _save(root, ledger)
        if ledger.get("status", "open") != "open":
            raise RuntimeError(f"{root.name}: hardening has ended -- "
                               f"{TERMINAL.get(ledger['status'], ledger['status'])}")
        current, targets = Path(bundle).resolve(), report["floor"]
        if current == root and (resume := _resume_point(root, ledger)) is not None:
            # `arena harden <original> --apply` on a lineage that already has a
            # measured revision continues from that revision. Starting again
            # from the original threw the measured progress away and spent an
            # attempt redoing the extremely-hard redesign.
            measured = hardenable(resume)
            if not measured.get("ok"):
                raise RuntimeError(f"{_rel(root, resume)} is the lineage's latest measured "
                                   f"revision but its screen cannot be read: "
                                   f"{'; '.join(measured.get('errors', [])[:2])}")
            print(f"  continuing from {_rel(root, resume)}, the lineage's latest measured "
                  f"revision (not from the original)")
            current, targets = resume.resolve(), measured["floor"]
        # A resumed revision is judged by the same rules as one screened inside
        # the loop, before any budget is spent on it.
        verdict = _judge_revision(root, current, ledger) if current != root else None
        if review_resume is not None and current == review_resume.resolve():
            verdict = None          # met the bar; the repair, not the rate, is the point
        elif reopen_stopped and current != root:
            verdict = None          # the operator named the version to continue from
        if verdict not in (None, "open"):
            ledger["status"] = verdict
            _save(root, ledger)
            _final_words(root, current, ledger)
            return 0 if verdict == "target-met" else 1
        if not attempts_left(ledger):
            ledger["status"] = "exhausted"
            _save(root, ledger)
            raise RuntimeError(f"{root.name}: all {budget} hardening "
                               "attempts are spent")
        # TIGHTEN only when the version being revised was actually measured as
        # a revision; otherwise this is the full extremely-hard redesign.
        tighten = current != root
        target = _next_revision(root, current) if retained is None else retained
        fresh = retained is None        # no attempt has touched this copy yet
        from .author.contracts import strict_json as _strict
        first_proposal = _strict((retained / HARDENING_DRAFT).read_text()) if retained is not None else None
        kind, findings, seen = "harden", [], set()
        print(f"\n  {attempts_left(ledger)} of {budget} hardening "
              f"attempts left for {root.name}")
        while attempts_left(ledger):
            number = len(ledger["attempts"]) + 1
            left = attempts_left(ledger) - 1
            print(f"\n  attempt {number}/{budget} "
                  f"[{'tighten' if kind == 'harden' and tighten else kind}] "
                  f"{_rel(root, current)} -> {_rel(root, target)}", flush=True)
            row = {"attempt": number, "kind": kind, "tighten": tighten and kind == "harden", "fast": fast,
                   "from": _rel(root, current), "revision": _rel(root, target),
                   "started_at": time.time()}

            def spent(row=row):
                # Called the moment the designer returns a revision.
                ledger["attempts"].append({**row, "outcome": "in-progress"})
                _save(root, ledger)

            try:
                new = attempt(current, target, targets, findings, requested,
                              number=number, of=budget,
                              kind=kind, left=left, tighten=tighten, on_proposed=spent, fast=fast,
                              proposed=first_proposal)
                first_proposal = None
            except DesignerUnavailable as exc:
                # Nothing was designed: record it, spend nothing, stop. A copy
                # made for this attempt and never revised is ours to remove.
                ledger.setdefault("interruptions", []).append({**row, "error": str(exc)[:500]})
                _save(root, ledger)
                if fresh:
                    shutil.rmtree(target, ignore_errors=True)
                print(f"  INTERRUPTED (no attempt spent): {str(exc)[:200]}\n"
                      "  Re-run the same command once the designer is reachable.")
                return 2
            except SeedRejected as exc:
                ledger["attempts"].append({**row, "outcome": "source-rejected",
                                           "reason": str(exc.report.get("reason", ""))[:500]})
                ledger["status"] = "source-rejected"
                _save(root, ledger)
                print(f"  STOPPED: the designer rejected the source -- {exc.report.get('reason', '')[:200]}")
                return 1
            except AuthoringStalled as exc:
                new = [f"the design stalled before submitting: {exc.report.get('reason', '')}"]
            except Exception as exc:                             # noqa: BLE001
                # A revision was proposed and checking it failed: that is a finding.
                new = [f"{type(exc).__name__}: {exc}"]
            outcome_row = {**row, "outcome": "invalid" if new else "valid",
                           "findings": [f[:300] for f in new[:12]]}
            pending = next((i for i, a in enumerate(ledger["attempts"])
                            if a.get("attempt") == number and a.get("outcome") == "in-progress"), None)
            if pending is None:
                ledger["attempts"].append(outcome_row)
            else:
                ledger["attempts"][pending] = outcome_row
            _save(root, ledger)
            fresh = False

            if new:
                print(f"  {len(new)} validity finding(s):")
                for line in new[:6]:
                    print(f"    - {line[:160]}")
                repeated = seen & set(new)
                if repeated or (findings and len(new) >= len(findings)):
                    ledger["status"] = "stopped"
                    _save(root, ledger)
                    print(f"  STOPPED: {'a finding repeated' if repeated else 'the finding set did not shrink'}"
                          f". The revision is kept at {target} for review; the original is untouched.")
                    return 1
                seen |= set(new)
                findings, kind = new, "repair"
                continue

            print(f"  valid revision at {target}. The original is untouched.")
            print("  NOT yet accepted: a revision is a candidate until its own screen says otherwise.")
            if not screen:
                print("  NOT yet measured (--no-screen). Screen it, then continue hardening:")
                print(f"    ./arena screen {target}")
                print(f"    ./arena harden {target} --apply")
                return 0
            try:
                outcome = screen_round(current, target)
            except Exception as exc:                             # noqa: BLE001
                # The revision is valid and its attempt is spent; only its
                # measurement failed. Measure THAT revision -- re-running harden
                # on the original would spend another attempt on a new design.
                ledger.setdefault("interruptions", []).append(
                    {"revision": _rel(root, target), "error": f"screen: {exc}"[:500]})
                _save(root, ledger)
                from . import screen as _screen
                print(f"  SCREEN FAILED (the valid revision is kept): {str(exc)[:200]}\n"
                      f"    {_screen.rescreen_command(target, _screen.current(target))}\n"
                      f"    ./arena harden {target} --apply     # continues from it")
                return 2
            if not outcome["record"].get("measurement_complete"):
                # Voids, unattributable failures or an unresolved hack flag: the
                # screen says nothing reliable about difficulty, so it may end
                # nothing. The lineage stays open on this revision.
                ledger.setdefault("interruptions", []).append(
                    {"revision": _rel(root, target), "error": "screen did not measure cleanly: "
                     + "; ".join(outcome["record"].get("shippable_problems", [])[:3])[:450]})
                _save(root, ledger)
                print("  SCREEN DID NOT MEASURE CLEANLY -- nothing is decided from it:")
                for line in outcome["record"].get("shippable_problems", [])[:4]:
                    print(f"    - {line[:300]}")
                from .screen import rescreen_command
                print(f"    {rescreen_command(target, outcome['record'])}\n"
                      f"    ./arena harden {target} --apply     # continues from it")
                return 2
            certificate = _certify(target)
            ledger["screens"].append({"revision": _rel(root, target), **outcome["summary"],
                                      "genuine": certificate})
            if lineage_target_met(outcome["record"]) and certificate.get("certified"):
                ledger["status"] = "target-met"
            elif lineage_target_met(outcome["record"]):
                # Under the bar, but a counted failure is not certified as the
                # model's (user, 2026-10-04): nothing is claimed; it is listed.
                print("  UNDER THE BAR, NOT CERTIFIED -- these failures are not yet shown to be the model's:")
                for line in certificate.get("problems", [])[:6]:
                    print(f"    - {line[:240]}")
            elif outcome["progress"] is False:
                # Only a measured "no" ends the lineage; None (nothing to compare
                # against) keeps it open on the budget.
                ledger["status"] = "no-progress"
            elif not attempts_left(ledger):
                ledger["status"] = "exhausted"
            _save(root, ledger)
            if ledger["status"] != "open":
                _final_words(root, target, ledger)
                return 0 if ledger["status"] == "target-met" else 1
            # Still too easy, measurably better, and budget left: tighten the
            # measured revision. Its own screen says which checks are still free.
            current = target
            measured = hardenable(current)
            if not measured["ok"]:
                ledger["status"] = "stopped"
                _save(root, ledger)
                print(f"  STOPPED: the revision's screen cannot be read: {measured['errors'][:2]}")
                return 1
            targets = measured["floor"]
            target = _next_revision(root, current)
            fresh, kind, findings, seen, tighten = True, "harden", [], set(), True
        # Budget spent on validity repairs without a valid revision.
        ledger["status"] = "exhausted"
        _save(root, ledger)
        print(f"  STOPPED: all {budget} attempts spent without a valid, "
              f"measured revision. The last is kept at {target} for review.")
        return 1


def _final_words(root: Path, revision: Path, ledger: dict) -> None:
    status = ledger["status"]
    spent = len(ledger.get("attempts", []))
    print(f"\n  hardening of {root.name} ended after {spent}/{ledger.get('max_attempts', policy.MAX_HARDENING_ATTEMPTS)} "
          f"attempt(s): {TERMINAL[status]}")
    if status != "target-met":
        print("  Its measurements stand. An explicit --attempt-budget increase can extend an exhausted search.")
    else:
        from .config import ARTIFACTS
        from .research import create_plan
        destination = ARTIFACTS / "research" / f"confirmation-{uuid.uuid4().hex}"
        plan = create_plan(destination, {"final": revision})
        ledger["confirmation_plan"] = str(destination)
        _save(root, ledger)
        print(f"  extreme target is provisional. Fresh confirmation plan retained ({plan['episodes']} episodes/model):\n"
              f"    ./arena research validate {destination}\n    ./arena research run {destination}")
    # The target never blocks: any revision whose screen measured cleanly may
    # take the calibration of record, met or not.
    print(f"  calibration of record (runs if the last screen measured cleanly):\n"
          f"    ./arena qualify {revision}")


def screen_round(original: Path, revision: Path) -> dict:
    """Measure a valid revision: screen it and compare it with what it revised.

    The screen is `policy.SCREEN_EPISODES_PER_MODEL` episodes on each model;
    the comparison is `acceptance` against the version it was revised from.
    `progress` uses conservative uncertainty intervals. Small unchanged screens
    are inconclusive and do not establish futility.
    """
    from . import screen
    from .artifacts import atomic_json
    from .attribution import problems as attribution_problems
    from .config import ARTIFACTS
    from .run import run_bundle
    from .verify_shipped import run_reference as verify
    print(f"\n  screening the revision: {policy.SCREEN_EPISODES_PER_MODEL} episodes x "
          f"{len(policy.resolve_calibration_models())} models", flush=True)
    record = screen.run(revision, native_root=ARTIFACTS / "native",
                        label=f"hardening {revision.name}")
    report = hardenable(revision)
    suspect = list(report.get("unreachable", []))
    for model, measured in report.get("reports", {}).items():
        suspect += [f"{model}: {p}" for p in
                    attribution_problems(revision, measured, policy.SCREEN_EPISODES_PER_MODEL)]
    result = acceptance(hardenable(original), report,
                        run_bundle(revision, Path.cwd())["verdict"] == "green",
                        verify(revision)["ok"], suspect)
    atomic_json(revision / "verification/hardening-comparison.json", result)
    screen.print_table(screen.report([record]))
    progress = measured_progress(hardenable(original).get("by_model", {}), report.get("by_model", {}))
    print(f"  comparison: {result['disposition']}; progress on at least one model: "
          f"{ {True: 'yes', False: 'regressed on every model', None: 'inconclusive'}[progress] }"
          + ("" if result["accept"] else f" -- {'; '.join(result['reasons'][:3])}"))
    summary = {"pass_at_1": result["pass_at_1"], "meets_target": bool(record.get("meets_target")),
               "meets_extreme_target": extreme_target_met(record),
               "steps_too_easy": steps_too_easy(record),
               "measurement_complete": bool(record.get("measurement_complete")),
               "shippable": bool(record.get("shippable")),
               "outlook": (record.get("outlook") or {}).get("verdict"),
               "disposition": result["disposition"], "progress": progress}
    return {"record": record, "comparison": result, "progress": progress, "summary": summary}


if __name__ == "__main__":
    raise SystemExit(main())
