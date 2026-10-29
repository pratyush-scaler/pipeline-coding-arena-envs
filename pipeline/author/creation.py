"""Inventory authored components without deciding whether the task is valid.

A saved draft is not necessarily a finished creation. Missing components guide
the author's remaining work; this module neither rejects submissions nor runs
validators, controls, Docker, reviewers or difficulty measurements.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..artifacts import atomic_json
from ..config import VENDORED_PARTS


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value) -> list:
    return value if isinstance(value, list) else []


REQUESTED = "verification/requested-traps.json"


def record_requested(bundle: Path, ideas: list[str]) -> None:
    """The operator's requested trap ideas for this session (harden --trap); [] clears them."""
    path = Path(bundle) / REQUESTED
    if ideas:
        atomic_json(path, {"ideas": [str(i) for i in ideas]})
    elif path.is_file():
        path.unlink()


def requested_trap_gaps(spec: dict, bundle: Path) -> list[str]:
    """Each requested idea needs a quality.requested_traps row {idea: <index>, status: built, traps: [ids]} or
    {idea: <index>, status: declined, reason: why the source cannot support it legitimately}."""
    try:
        ideas = json.loads((Path(bundle) / REQUESTED).read_text()).get("ideas") or []
    except (OSError, ValueError, AttributeError):
        return []
    rows = {}
    for row in _rows(_object(spec.get("quality")).get("requested_traps")):
        row = _object(row)
        if type(row.get("idea")) is int:
            rows[row["idea"]] = row
    trap_ids = {_object(t).get("id") for t in _rows(spec.get("traps"))}
    gaps = []
    for i, idea in enumerate(ideas):
        row = rows.get(i, {})
        built = row.get("status") == "built" and _rows(row.get("traps")) and set(row["traps"]) <= trap_ids
        declined = row.get("status") == "declined" and _text(row.get("reason")) and len(row["reason"].strip()) >= 40
        if not (built or declined):
            gaps.append(f"quality.requested_traps[idea={i}] for the operator's requested trap \"{idea[:90]}...\": "
                        "status 'built' with the trap ids that implement it, or 'declined' with the reason this "
                        "source cannot support it legitimately (40+ characters)")
    return gaps


def inventory(spec: dict, bundle: Path) -> dict:
    """Report component presence, not correctness, thresholds or acceptance."""
    spec = _object(spec)
    bundle = Path(bundle)
    missing: list[str] = []

    def need(component, present):
        if not present and component not in missing:
            missing.append(component)

    for key in ("name", "description", "language", "intent", "difficulty", "novelty", "target_level"):
        need(key, _text(spec.get(key)))
    need("keywords", isinstance(spec.get("keywords"), list))
    need("deliverables", isinstance(spec.get("deliverables"), list))
    need("toolchain.name", _text(_object(spec.get("toolchain")).get("name")))
    need("deps", isinstance(spec.get("deps"), dict))
    source = _object(spec.get("source"))
    need("source.session_id", _text(source.get("session_id")))
    need("source.seed_index", type(source.get("seed_index")) is int)

    repo = bundle / "environment/repo"
    fixture = [p.relative_to(repo).as_posix() for p in sorted(repo.rglob("*"))
               if p.is_file() and not p.is_symlink()
               and not any(part in VENDORED_PARTS for part in p.relative_to(repo).parts)] if repo.is_dir() else []
    need("fixture.files", bool(fixture))
    provenance_files = [name for name in ("fixture-provenance.json", "fixture-authored.json")
                        if (bundle / "authoring/extraction" / name).is_file()]
    need("fixture.provenance", bool(provenance_files))
    covered = set()
    for name in provenance_files:
        try:
            files = _object(json.loads((bundle / "authoring/extraction" / name).read_text())).get("files")
            if isinstance(files, dict):
                covered.update(files)
            else:
                covered.update(row["path"] for row in _rows(files)
                               if isinstance(row, dict) and _text(row.get("path")))
        except (OSError, ValueError):
            need(f"fixture.provenance.{name}", False)
    for path in fixture:
        need(f"fixture.provenance[{path}]", path in covered)

    steps = _rows(spec.get("steps"))
    need("steps", bool(steps))
    names = []
    checks = _rows(spec.get("checks"))
    for i, raw in enumerate(steps):
        step = _object(raw)
        name = step.get("name")
        need(f"steps[{i}].name", _text(name))
        need(f"steps[{i}].instruction", _text(step.get("instruction")))
        if i:
            need(f"steps[{i}].new_information", _text(step.get("new_information")))
        if _text(name):
            names.append(name)
            need(f"checks for step {name}", any(_object(c).get("step") == name for c in checks))
            for label in ("solution", "alternative"):
                entry = _object(_object(spec.get(label)).get(name))
                need(f"{label}[{name}].patch", _text(entry.get("patch")))
                if label == "solution":
                    need(f"solution[{name}].provenance.origin", _text(_object(entry.get("provenance")).get("origin")))

    need("checks", bool(checks))
    for i, raw in enumerate(checks):
        check = _object(raw)
        label = check.get("id") if _text(check.get("id")) else str(i)
        for key in ("id", "kind", "step", "basis", "band"):
            need(f"checks[{label}].{key}", _text(check.get(key)))
        need(f"checks[{label}].weight", type(check.get("weight")) in (int, float))
        need(f"checks[{label}].rule_quote", bool(_rows(check.get("rule_quote"))))
        need(f"checks[{label}].runner.type", _text(_object(check.get("runner")).get("type")))
        if check.get("kind") == "objective":
            need(f"checks[{label}].weight_basis", _text(check.get("weight_basis")))
    suites = _object(spec.get("suites"))
    need("suites", bool(suites) and any(_text(text) for text in suites.values()))
    for path, text in suites.items():
        need(f"suites[{path}]", isinstance(text, str))

    traps = _rows(spec.get("traps"))
    mutants = _rows(spec.get("mutants"))
    need("traps", bool(traps))
    need("mutants", bool(mutants))
    for i, raw in enumerate(traps):
        trap = _object(raw)
        label = trap.get("id") if _text(trap.get("id")) else str(i)
        for key in ("id", "type", "surface", "provenance", "mutant", "beatable_by"):
            need(f"traps[{label}].{key}", _text(trap.get(key)))
        need(f"traps[{label}].checks", bool(_rows(trap.get("checks"))))
        keys = ("grounding", "grounded_in") if trap.get("provenance") == "authored" else ("derived_from", "quote")
        for key in keys:
            need(f"traps[{label}].{key}", _text(trap.get(key)))
        need(f"mutant for trap {label}", any(_object(m).get("id") == trap.get("mutant") for m in mutants))
    # A fresh design's researched-trap floor (policy.MIN_RESEARCHED_TRAPS, set per
    # session in verification/trap-research.json; 0 for revisions, waived when the
    # research tool was unavailable). Presence of well-formed, recorded research only.
    from . import trap_research
    from .. import policy
    floor, on_record = trap_research.requirement(bundle)
    if floor:
        started_with = trap_research.baseline(bundle)
        have = [t for t in trap_research.researched(traps, on_record) if t not in started_with]
        need(f"researched traps ({len(have)}/{floor}{' NEW in this round' if started_with else ''}): traps "
             "carrying research {research_id, sources, finding, executed} from a successful research_traps call, "
             "each verified by sandbox_run", len(have) >= floor)
        # Spread over failure modes (user, 2026-10-28: "a lot of diverse trap modes using web search and thorough
        # research"): researched traps piled on one clause are one mode however many there are.
        modes = trap_research.researched_modes(spec, have)
        need(f"researched failure modes ({len(modes)}/{policy.MIN_RESEARCHED_FAILURE_MODES}): the researched traps "
             f"drive {modes or 'no'} mode(s); each new mode is its own decision on its own published clause "
             "(author/failure_modes.py)", len(modes) >= policy.MIN_RESEARCHED_FAILURE_MODES)
    # The second origin, the source trace (user, 2026-10-28), at the first design and in every hardening round:
    # the session's own corrections, failed tool calls and retries, refined into harder cases of the same
    # mistake -- several of them, on more than one failure mode. Recorded per session, so it holds even when
    # web research is unavailable.
    traced_floor = trap_research.trace_requirement(bundle)
    if traced_floor:
        started_with = trap_research.baseline(bundle)
        traced = [t.get("id") for t in (_object(r) for r in traps)
                  if t.get("provenance") == "trace_derived" and t.get("id") not in started_with]
        need(f"trace-derived traps ({len(traced)}/{traced_floor}{' NEW in this round' if started_with else ''}): "
             "traps refined from the session's corrections, failed tool calls and retries (list_corrections, "
             "read_failures, read_signals), citing the moment and quoting the user", len(traced) >= traced_floor)
        mode_floor = min(policy.MIN_TRACE_FAILURE_MODES, traced_floor)
        traced_modes = trap_research.researched_modes(spec, traced)
        need(f"trace-derived failure modes ({len(traced_modes)}/{mode_floor}): the trace-derived traps drive "
             f"{traced_modes or 'no'} mode(s); refine different moments of the trace into different decisions",
             len(traced_modes) >= mode_floor)
    # Operator-requested traps (harden --trap; user, 2026-10-28): each is built or declined with a reason,
    # never dropped silently (a round built four requested decisions in scratch and shipped none of them).
    for gap in requested_trap_gaps(spec, bundle):
        need(gap, False)
    # The binding hardness bar (author/difficulty_gate.py): armed per session, waived when the
    # budget runs out; lists what the current blind solves or re-grade still fall short of.
    from . import difficulty_gate
    for gap in difficulty_gate.requirement(spec, bundle):
        need(gap, False)
    # A release repair (pipeline/release_blockers.py): the release's own critic re-execution and reviewer,
    # run on the draft, until clean or the round's budget waives them.
    from .. import release_blockers
    for gap in release_blockers.requirement(spec, bundle):
        need(gap, False)
    for i, raw in enumerate(mutants):
        mutant = _object(raw)
        label = mutant.get("id") if _text(mutant.get("id")) else str(i)
        for key in ("id", "trap", "reason", "patch"):
            need(f"mutants[{label}].{key}", _text(mutant.get(key)))
        need(f"mutants[{label}].fails", bool(_rows(mutant.get("fails"))))

    quality = _object(spec.get("quality"))
    for key in ("axes", "levers", "decisions"):
        need(f"quality.{key}", bool(_rows(quality.get(key))))
    need("quality.untested_surfaces", isinstance(quality.get("untested_surfaces"), list))
    for i, raw in enumerate(_rows(quality.get("decisions"))):
        decision = _object(raw)
        for key in ("id", "question", "shortcut"):
            need(f"quality.decisions[{i}].{key}", _text(decision.get(key)))
        for key in ("checks", "sources"):
            need(f"quality.decisions[{i}].{key}", bool(_rows(decision.get(key))))
    case = _object(quality.get("difficulty_case"))
    for key in ("hardest_decision", "first_try_verdict", "shortcut_test", "first_try_mutant"):
        need(f"quality.difficulty_case.{key}", _text(case.get(key)))
    for key in ("misreadings", "pitfalls_considered"):
        need(f"quality.difficulty_case.{key}", bool(_rows(case.get(key))))
    for i, raw in enumerate(_rows(case.get("misreadings"))):
        for key in ("trap", "misreading", "why_plausible", "diverges_on", "mutant"):
            need(f"quality.difficulty_case.misreadings[{i}].{key}", _text(_object(raw).get(key)))
    for i, raw in enumerate(_rows(case.get("pitfalls_considered"))):
        pitfall = _object(raw)
        for key in ("pitfall", "family", "where", "decision"):
            need(f"quality.difficulty_case.pitfalls_considered[{i}].{key}", _text(pitfall.get(key)))
        key = "trap" if pitfall.get("decision") == "planted" else "reason"
        need(f"quality.difficulty_case.pitfalls_considered[{i}].{key}", _text(pitfall.get(key)))

    return {"schema": "arena-coding/creation/1", "phase": "creation", "advisory_only": True,
            "status": "complete" if not missing else "unfinished", "components_present": not missing,
            "missing_components": missing, "validation_deferred": True,
            "inventory": {"fixture_files": fixture, "fixture_provenance_files": provenance_files,
                          "steps": names, "checks": len(checks), "suites": list(suites),
                          "traps": len(traps), "mutants": len(mutants)},
            "meaning": "complete means authored components are present; validity and difficulty are unverified"}


def record(spec: dict, bundle: Path, *, reason: str, finalized: bool = True) -> dict:
    report = inventory(spec, bundle)
    report.update(reason=reason, finalized=finalized,
                  spec_sha256=hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False,
                                                      allow_nan=False).encode()).hexdigest())
    if not finalized:
        report["status"] = "unfinished"
    atomic_json(Path(bundle) / "verification/creation.json", report)
    return report
