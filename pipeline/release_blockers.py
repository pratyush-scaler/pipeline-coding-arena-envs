"""Everything recorded against a hard version that release will check again, in one place.

User, 2026-10-18 ("repair these tasks properly ... the repair phase has the exact information of
what needs to be fixed ... so that these issues don't appear again and should fix the issues
properly and at once"). Measured the same morning: 32 oct13 lineages reached pass@1 <= 0.5 on
both models and 6 shipped. The rest cycled through release one blocker class at a time -- the
critic gate stopped a version, a hidden-only round closed some of its gaps, release stopped the
next version on the gap that survived, then the reviewer in qualify rejected a check, and so on
(c-015 v2 -> v3 -> v4, c-024 v2 -> v5, c-037 v3 -> v4 -> review). Each lap cost about an hour.

`collect(version)` lists every blocker on record with an id, what was measured, and the exact
way release re-checks it. `harden --release-repair` hands the whole list to ONE round, and the
round's `release_check` tool re-executes the release's own critic proposals on the staged draft
-- the closed-and-verified rule release applies -- so the round sees each gap close before it
submits. A creation component keeps the round working until that check is clean (or its turn
budget runs out, recorded), the way the hardness gate does.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import policy
from .artifacts import atomic_json

CONTEXT = "verification/release-repair.json"
CHECK = "verification/release-check.json"
PREREVIEW = "verification/release-prereview.json"


def _load(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def critic_source(version: Path) -> tuple[Path, dict] | None:
    """The searching critic run the NEXT version's release answers to: this version's own receipt when it
    searched, else the run its receipt verified (coverage_critic.source_record)."""
    from . import coverage_critic
    version = Path(version)
    own = _load(version / coverage_critic.RECEIPT)
    if own and own.get("mode") != "verify" and own.get("steps"):
        return version, own
    return coverage_critic.source_record(version)


def _critic(version: Path) -> list[dict]:
    from . import shipcheck
    receipt = _load(Path(version) / "verification/coverage-critic.json")
    survived = receipt.get("mode") == "verify"
    out = []
    for gap in shipcheck.coverage(Path(version)):
        if gap.get("check") != "coverage.gap":
            continue
        out.append({"id": f"CRIT {gap.get('subject')}", "source": "ship critic",
                    "finding": f"the hidden suite {gap.get('detail')}",
                    "evidence": " ".join(str(e) for e in gap.get("evidence") or [])[:1500],
                    "survived_a_round": survived,
                    "release_checks": "release re-executes this exact wrong implementation (the edits) on your "
                                      "version and blocks while it scores 1.0 -- release_check runs the same "
                                      "re-execution on your draft",
                    "agent_surface": False})
    return out


def _review(version: Path) -> list[dict]:
    from .review import review_findings
    out = []
    for n, text in enumerate(review_findings(Path(version)), 1):
        surface = text.startswith("ambiguities:")
        out.append({"id": f"REV {n}", "source": "independent semantic review (the one qualify runs)",
                    "finding": text[:2400],
                    "release_checks": "qualify runs the same reviewer on your version; any check it does not pass, "
                                      "any substantive gap, shortcut risk or unresolved ambiguity rejects the release "
                                      "before the 10 x 2 calibration -- pre_review runs that reviewer on your draft",
                    "agent_surface": surface})
    return out


def _signal(version: Path) -> list[dict]:
    screen = _load(Path(version) / "verification/screen.json")
    out = [{"id": f"SIG {n}", "source": "learning signal of the measured pilot", "finding": str(p)[:600],
            "release_checks": "release's hardness step reads the 10 x 2 calibration: at most 50% of the objective "
                              "weight may be passed by every run of both models and at least 3 independent hard "
                              "decisions (checks failed by >= 30% of a model's runs, one per trap) must fail -- "
                              "regrade_episodes on the captured episodes shows what the current tests would score",
            "agent_surface": False}
           for n, p in enumerate(screen.get("signal_problems") or [], 1) if "runs are flat" not in str(p)]
    bar = policy.STEP_PASS_RATE_MAX
    for model, steps in (screen.get("step_pass_rates") or {}).items():
        for step, rate in (steps or {}).items():
            if rate is not None and rate > bar:
                out.append({"id": f"STEP {step} {model.split('/')[-1]}", "source": "per-step bar",
                            "finding": f"{model.split('/')[-1]} fully passes step {step} in {rate:.0%} of runs "
                                       f"(bar {bar:.0%})",
                            "release_checks": "release's hardness step requires every step at or under the bar on "
                                              "both models",
                            "agent_surface": False})
    return out


def _release(version: Path) -> list[dict]:
    record = _load(Path(version) / "verification/shipfix-release.json")
    out = []
    for name, step in (record.get("steps") or {}).items():
        result = step.get("result") if isinstance(step.get("result"), dict) else {}
        if name == "shipcheck":
            for n, f in enumerate(result.get("findings") or [], 1):
                if isinstance(f, dict) and f.get("check") != "coverage.gap":
                    out.append({"id": f"SHIP {n}", "source": f"shipcheck {f.get('check')}",
                                "finding": f"{f.get('subject')}: {f.get('detail')}"[:1200],
                                "release_checks": "the packaging sweep runs the same detector and holds the zip",
                                "agent_surface": f.get("route") not in ("hidden", "regrade", None)})
        elif name == "genuine" and (result.get("problems") or not result.get("certified", True)):
            for n, p in enumerate(result.get("problems") or ["failures not certified as genuine"], 1):
                out.append({"id": f"GEN {n}", "source": "failure certification", "finding": str(p)[:1200],
                            "release_checks": "release certifies that every model failure is a real mistake, not a "
                                              "grader artefact", "agent_surface": False})
        elif name == "qualify":
            for n, e in enumerate(result.get("errors") or [], 1):
                e = str(e)
                if e.startswith("semantic review") or "native-calibrations.json" in e or "task name claims faithfulness" in e:
                    continue        # the review is collected from its own record; calibration never ran
                if any(k in e for k in ("OpenAI", "HTTP", "timed out", "Timeout", "rate limit", "could not be verified",
                                        "unavailable", "Connection", "could not be completed")):
                    continue        # infrastructure loss is re-run, not repaired
                out.append({"id": f"QUAL {n}", "source": "qualify", "finding": e[:1200],
                            "release_checks": "qualify repeats this check", "agent_surface": False})
    return out


def _build(version: Path) -> list[dict]:
    """A root build blocked before review: its ship-readiness findings (the last repair pass's list)."""
    version = Path(version)
    if version.parent.name == "hardened":
        return []
    log = _load(version / "verification/repair-log.json")
    passes = log.get("passes") or []
    out = []
    for n, f in enumerate((passes[-1].get("findings") if passes else []) or [], 1):
        f = str(f)
        if not f.startswith("ship.readiness") or "coverage.gap" in f:
            continue                # the build's critic gaps carry no edits here; the ship critic re-runs at release
        out.append({"id": f"READY {n}", "source": "build-time ship readiness", "finding": f[:1500],
                    "release_checks": "shipcheck runs the same detector at release and at packaging",
                    "agent_surface": any(k in f for k in ("deps.", "image.imports", "fixture.", "toolchain"))})
    return out


def collect(version: Path) -> list[dict]:
    version = Path(version)
    return _critic(version) + _review(version) + _signal(version) + _release(version) + _build(version)


def needs_full_round(blockers: list[dict]) -> bool:
    """A hidden-only round cannot change what the agent sees (instruction, fixture, toolchain, deps)."""
    return any(b.get("agent_surface") for b in blockers)


def write_context(bundle: Path, version: Path, blockers: list[dict]) -> None:
    """Where the round's release_check finds the release's critic run (the working copy, before design)."""
    found = critic_source(version)
    atomic_json(Path(bundle) / CONTEXT, {
        "version": str(Path(version).resolve()), "at": time.time(), "blockers": blockers,
        "critic_source": str(found[0]) if found else None, "waived": None})


def waive(bundle: Path, reason: str) -> None:
    doc = _load(Path(bundle) / CONTEXT)
    if doc and not doc.get("waived"):
        doc["waived"] = {"reason": reason, "at": time.time()}
        atomic_json(Path(bundle) / CONTEXT, doc)


def record_check(bundle: Path, grading_sha: str, rows: list[dict]) -> dict:
    doc = {"grading_sha256": grading_sha, "at": time.time(), "rows": rows,
           "open": [r for r in rows if r.get("still_scores_1") and r.get("blocking", True)]}
    atomic_json(Path(bundle) / CHECK, doc)
    return doc


def record_prereview(bundle: Path, grading_sha: str, findings: list[dict]) -> None:
    """The latest pre-review, and every finding any pre-review of this round raised (the reviewer is sampled: a
    finding one sample raises and a later one omits is not fixed -- 2026-10-18, c-007 v4)."""
    previous = _load(Path(bundle) / PREREVIEW)
    raised = {f["id"]: f for f in previous.get("raised") or [] if isinstance(f, dict) and f.get("id")}
    for f in findings:
        if isinstance(f, dict) and f.get("id"):
            raised.setdefault(str(f["id"]), {"id": str(f["id"]), "reason": str(f.get("reason"))[:400]})
    atomic_json(Path(bundle) / PREREVIEW, {"grading_sha256": grading_sha, "at": time.time(),
                                           "findings": findings, "raised": list(raised.values())})


def unresolved(raised: list[dict], notes: str) -> list[dict]:
    """Raised findings with no ledger line naming them FIXED or REFUTED in solution_notes."""
    lines = [l for l in str(notes or "").splitlines() if "FIXED" in l or "REFUTED" in l]
    return [f for f in raised if not any(str(f["id"]) in l for l in lines)]


def requirement(spec: dict, bundle: Path) -> list[str]:
    """Creation components a release repair still owes ([] when it is not one, or it was waived)."""
    context = _load(Path(bundle) / CONTEXT)
    if not context or context.get("waived"):
        return []
    from .author import difficulty_gate
    current = difficulty_gate.grading_sha(spec)
    missing = []
    if context.get("critic_source"):
        check = _load(Path(bundle) / CHECK)
        if check.get("grading_sha256") != current:
            missing.append("RELEASE CHECK: run release_check on the current draft (it re-executes the release's own "
                           "critic proposals; the tests or instruction changed since the last run)")
        else:
            for row in check.get("open") or []:
                missing.append(f"RELEASE CHECK: {row.get('step')}#{row.get('index')} still scores 1.0 while violating "
                               f"'{str(row.get('clause'))[:160]}' -- add the hidden case that makes it fail, or show "
                               "with determinacy_probe that the sentence does not settle it and record it under "
                               "quality.untested_surfaces")
    if any(b["id"].startswith("REV") for b in context.get("blockers") or []):
        review = _load(Path(bundle) / PREREVIEW)
        if review.get("grading_sha256") != current:
            missing.append("RELEASE CHECK: pre_review the current draft (qualify runs the same reviewer) and resolve "
                           "what it reports with THE REPAIR PROTOCOL")
        # Every finding ANY pre-review of this round raised ends FIXED or REFUTED in the ledger (2026-10-18:
        # c-029-static v8 submitted with exactly the findings its pre-review reported and qualify rejected them;
        # c-007 v4 took a clean 4th sample as a fix and qualify raised the 1st sample's o_twelve_weeks again).
        for f in unresolved(review.get("raised") or [], spec.get("solution_notes")):
            missing.append(f"RELEASE CHECK: pre_review raised {f['id']} ({f['reason'][:160]}) -- write a solution_notes "
                           f"ledger line '{f['id']}: FIXED -- <case>: before/after' or '{f['id']}: REFUTED -- <executed "
                           "evidence>'; a later review not repeating it is not a fix")
    return missing


def clear(bundle: Path) -> None:
    """A revision copies its parent's verification/: a release repair's context must not follow it into a later
    round that is not one."""
    for name in (CONTEXT, CHECK, PREREVIEW):
        try:
            (Path(bundle) / name).unlink()
        except FileNotFoundError:
            pass


def brief(blockers: list[dict], version: Path) -> str:
    """The round's whole assignment: every blocker, how release re-checks it, and the order of work."""
    lines = []
    for b in blockers:
        lines.append(f"  [{b['id']}] ({b['source']}){' -- SURVIVED AN EARLIER ROUND' if b.get('survived_a_round') else ''}\n"
                     f"      finding: {b['finding']}\n"
                     + (f"      the wrong implementation's edits: {b['evidence']}\n" if b.get("evidence") else "")
                     + f"      release re-checks it: {b['release_checks']}")
    return RELEASE_REPAIR.format(version=Path(version).name, n=len(blockers), items="\n".join(lines))


RELEASE_REPAIR = """

RELEASE REPAIR -- {version} is ALREADY HARD on both models. It did not ship because of the {n}
recorded blocker(s) below. This round fixes ALL of them, at once, and nothing else. Each was
recorded by the step of release that will check it again on your version; a blocker left open
sends the task round again and costs it an hour (2026-10-18: c-015, c-024 and c-037 each went
round three or four times, one blocker class per lap).

{items}

THE ORDER OF WORK
  1. Read every blocker first. Several often share one root cause (a check that reads one
     representation, a reference that handles one branch): fix the cause once.
  2. CRIT (ship critic): each lists the exact edits of a wrong implementation the hidden suite
     scores 1.0. Apply them to the reference as a variant (sandbox_grade) to reproduce the 1.0,
     add the hidden case the quoted published sentence settles (prove it with determinacy_probe
     when the reading is not obvious) so the variant fails while the reference and the
     alternative stay 1.0, and give it an m_partial_ mutant. When both families say the sentence
     does not settle it, record it under quality.untested_surfaces with the probe instead.
     Then run release_check: it re-executes the release's own proposals on your draft, exactly as
     release will. Do not submit while it lists an open CRIT.
  3. REV (the reviewer qualify runs): follow THE REPAIR PROTOCOL below -- reproduce, fix the root
     cause in the reference AND the alternative, sweep the same class, regression-gate. A check the
     reviewer calls over-strict is loosened only as far as the published sentence allows; a weak
     alternative is fixed so it really is correct. Then pre_review the final draft: it is the
     same reviewer. Do not submit while it reports something you have not FIXED or REFUTED with
     executed evidence. The reviewer is sampled: EVERY finding any pre_review of this round raises
     needs its own ledger line, even when a later pre_review does not repeat it (2026-10-18: c-007 v4
     passed its 4th pre_review and qualify rejected it on the check its 1st and 2nd had flagged).
  3b. THE REPRESENTATION SWEEP -- before pre_review, for EVERY objective check, not only the ones a
     finding names. The reviewer in qualify rejected c-037 three times, c-032, c-006 and c-013 twice
     each (2026-10-18), every time on a DIFFERENT check with the SAME defect: an oracle that accepts one
     representation of a correct answer and refuses an equivalent one the instruction never ruled out
     -- a DOM shape or nesting, how text is split into runs or spans, a colour written as srgbClr but
     not as a scheme or preset colour, a formula cell versus its cached value, a file left from an
     earlier build, an exact whitespace or ordering the sentence does not fix. For each check write
     down which forms a correct solution may legitimately take, grade one such equivalent form against
     it (sandbox_grade a variant of the reference), and widen any oracle that refuses it to the
     published behaviour -- keeping the wrong implementations failing (re-run release_check and the
     mutants). Record the sweep in solution_notes ("SWEEP <check>: forms tried, result").
  4. SIG / STEP (the learning signal and the per-step bar): make free checks discriminating with
     determinate cases the published sentences settle, and keep or add independent decisions.
     regrade_episodes shows what the captured episodes score under your tests.
  5. SHIP / READY / QUAL / GEN: fix exactly what the detector names.
  6. Keep it hard: never weaken a check that bites. After every change the reference (three runs)
     and the alternative stay at 1.0 and run_controls holds.
  7. Write the resolution ledger in solution_notes, one line per blocker id:
     "<id>: FIXED|REFUTED -- <case or file>: before <result>, after <result>". The reviewer reads it.
"""
