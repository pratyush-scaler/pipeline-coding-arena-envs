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
    floor, on_record = trap_research.requirement(bundle)
    if floor:
        have = trap_research.researched(traps, on_record)
        need(f"researched traps ({len(have)}/{floor}): traps carrying research {{research_id, sources, finding, "
             "executed}} from a successful research_traps call, each verified by sandbox_run", len(have) >= floor)
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
