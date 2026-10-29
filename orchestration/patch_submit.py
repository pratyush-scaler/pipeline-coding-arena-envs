"""Patch a designed bundle from review findings, and refuse any patch that is not proven.

A finding that names one assertion does not need a designer session. An operator writes the
edits; this tool decides whether they are a fix, in the order that spends the least:

  1. PLAN (no Docker, no model): every edit matches exactly once in the spec; every edited
     Python suite still parses; the patch adds no authoring advisory the suite did not have
     (`validate.overfit_advisories`: timestamps as evidence, bytes required to differ, ...).
  2. REPRODUCE: every new probe -- an executable counterexample of a finding the patch fixes
     (pipeline/author/counterexamples.py) -- is run on the CURRENT version and must NOT hold:
     a probe that already holds does not encode its finding.
  3. CONFIRM: the edits are staged through the designer's own tools (the spec stays the
     source of truth, check_draft must be clean) and every new probe AND every probe kept from
     earlier patches of this bundle (authoring/patches/probes.json) must hold on the patched
     version: the fix works and no earlier fix is undone.
  4. SUBMIT: export, ledger, naming, the full control matrix, probes and gates
     (orchestration/manual_designer.submit).
  5. REVIEW: the gate's semantic review (one exhaustive round, with its sweep), and the
     review's own counterexamples executed and attached.

Only a patch that passes 1-4 reaches the review; its new probes join the bundle's regression set.

    ARENA_DUMP=... ARENA_PIPELINE_ARTIFACTS=... .venv/bin/python orchestration/patch_submit.py \
        <bundle> <patch.json> [--dry-run] [--measure] [--no-review] [--from-review] [--allow-advisory KIND] [--why TEXT]

patch.json: {"why": "...", "edits": [{"path": ["suites", "test_build.py"], "edit": {"old": "...", "new": "..."}},
                                     {"path": ["mutants"], "value": [...]}],
             "regression": [probes of findings fixed earlier: they must hold after, need not reproduce before],
             "probes": [{"id": "S1-page-break-before", "finding": "S1", "kind": "false_negative",
                         "base": "reference", "edits": [{"file": "make_doc.py", "old": "...", "new": "..."}]}]}
(a bare list is edits only). `--from-review` adds every counterexample the current review carries
that execution reproduced (verification/review-counterexamples.json) as a probe of this patch.
`--dry-run` stops after REPRODUCE; nothing is staged or exported.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.cli import load_keys  # noqa: E402
from orchestration import manual_designer  # noqa: E402

REGRESSION = "authoring/patches/probes.json"


def _seed_manual_session(bundle: Path) -> dict:
    """The designed bundle's spec as the manual draft, and a manual row from its source."""
    from pipeline.artifacts import atomic_json
    from pipeline.author import build as build_stage
    spec = build_stage.load_spec(bundle)
    source = spec.get("source") or {}
    try:
        ledger = json.loads((bundle / "provenance/ledger.json").read_text())
    except (OSError, ValueError):
        ledger = {}
    level = spec.get("target_level") or ledger.get("faithfulness_level") or "F2"
    if Path(bundle).parent.name == "hardened" and ledger.get("faithfulness_level"):
        # A hardening revision is validated at its MEASURED level (harden re-measures the ledger; scenario
        # hardening may report F2 for an F3 target), never at the original's authoring target (2026-10-28).
        level = ledger["faithfulness_level"]
    row = {"session_id": source["session_id"], "seed_index": int(source.get("seed_index", 0)),
           "tier": source.get("tier"), "authoring_target_level": level, "target_level": level,
           "manual": True, "patched_from": "designed bundle (orchestration/patch_submit.py)"}
    wave = bundle / "authoring/wave-row.json"
    if wave.is_file():
        try:
            row = {**json.loads(wave.read_text()), **row}
        except ValueError:
            pass
    else:
        # No row kept with the bundle (hardened revisions): the batch's classified row for this seed, so the
        # usable-correction count is the classifier's, as at the build -- never the every-moment fallback
        # (2026-10-28: 16 "usable" for a seed classified with 1 refused a patch on the trace floor).
        from pipeline.config import ARTIFACTS
        try:
            rows = json.loads((Path(ARTIFACTS) / "classified.json").read_text()).get("rows") or []
        except (OSError, ValueError, AttributeError):
            rows = []
        match = next((r for r in rows if r.get("session_id") == row["session_id"]
                      and int(r.get("seed_index", -1)) == row["seed_index"]), None)
        if match:
            row = {**match, **row}
    atomic_json(bundle / "authoring/manual-row.json", row)
    declared = (row.get("classification") or {}).get("usable_moments")
    if type(declared) is int and declared >= 0:
        # The draft carries the count manual_designer.submit will set from this row, so check_draft and the
        # gates judge the trace floor the same way.
        spec["usable_corrections"] = declared
    # A patch is a repair: the authoring floors of the round that built the bundle (researched and
    # trace-derived traps, requested-trap accounting) and its hardness gate do not apply to a fix
    # (2026-10-28: a patch of a hardened revision was refused for the round's own floors).
    from pipeline.author import creation, difficulty_gate, trap_research
    trap_research.begin(bundle, 0)
    creation.record_requested(bundle, [])
    difficulty_gate.arm(bundle, None)
    log = bundle / "verification/design-sessions/manual"
    log.mkdir(parents=True, exist_ok=True)
    atomic_json(log / "draft.json", spec)
    return spec


def _get(node, path):
    for key in path:
        node = node[key]
    return node


def _set(node, path, value):
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def plan_patch(spec: dict, edits: list, bundle: Path, allow: tuple[str, ...] = ()) -> tuple[dict, list[str], dict]:
    """(patched spec, problems, report): the edits applied in memory, the edited Python suites
    parsed, and the authoring advisories compared before and after. Nothing is written."""
    from pipeline.author import validate
    patched = copy.deepcopy(spec)
    problems: list[str] = []
    touched = set()
    for edit in edits:
        path, change = edit.get("path"), edit.get("edit") or {}
        where = "spec" + "".join(f"[{k!r}]" for k in path or [])
        try:
            current = _get(patched, path)
        except (KeyError, IndexError, TypeError):
            problems.append(f"{where}: no such field")
            continue
        if "value" in edit:
            # A whole-field edit (2026-10-28: removing a trap and its mutant, re-declaring what a mutant
            # fails): the new value replaces the field; check_draft and the controls judge it like any edit.
            if edit.get("edit") is not None:
                problems.append(f"{where}: an edit is either {{old, new}} or a value, not both")
                continue
            _set(patched, path, copy.deepcopy(edit["value"]))
            touched.add(tuple(path))
            continue
        old, new = change.get("old"), change.get("new")
        if not isinstance(current, str) or not isinstance(old, str) or not old or not isinstance(new, str):
            problems.append(f"{where}: an edit is {{old, new}} on a text field")
            continue
        count = current.count(old)
        if count != 1:
            problems.append(f"{where}: `old` occurs {count} times; it must occur exactly once")
            continue
        _set(patched, path, current.replace(old, new, 1))
        touched.add(tuple(path))
    for path in sorted(touched):
        if len(path) == 2 and path[0] == "suites" and str(path[1]).endswith(".py"):
            try:
                ast.parse(_get(patched, path))
            except SyntaxError as exc:
                problems.append(f"suites/{path[1]} no longer parses: line {exc.lineno}: {exc.msg}")
    before = set(validate.overfit_advisories(spec, bundle))
    after = set(validate.overfit_advisories(patched, bundle))
    added = sorted(a for a in after - before if not any(f"ADVISORY {kind}" in a for kind in allow))
    problems += [f"the patch introduces an authoring advisory: {a[:400]}" for a in added]
    return patched, problems, {"edited": [list(p) for p in sorted(touched)], "advisories_added": added,
                               "advisories_removed": sorted(before - after)}


def load_regression(bundle: Path) -> list[dict]:
    try:
        rows = json.loads((bundle / REGRESSION).read_text())
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def save_regression(bundle: Path, probes: list[dict]) -> list[dict]:
    from pipeline.artifacts import atomic_json
    kept = {p["id"]: p for p in load_regression(bundle) if p.get("id")}
    for probe in probes:
        kept[probe["id"]] = probe
    rows = sorted(kept.values(), key=lambda p: p["id"])
    atomic_json(bundle / REGRESSION, rows)
    return rows


def review_probes(bundle: Path) -> list[dict]:
    """The current review's counterexamples that execution reproduced."""
    try:
        record = json.loads((bundle / "verification/review-counterexamples.json").read_text())
    except (OSError, ValueError):
        return []
    return [r["probe"] for r in record.get("results") or [] if r.get("holds") is False and isinstance(r.get("probe"), dict)]


def gate_probes(results: list[dict], *, want: bool) -> list[str]:
    """Probes whose outcome is not `want` (True: must hold, False: must reproduce)."""
    out = []
    for r in results:
        if r.get("holds") is None:
            out.append(f"probe {r.get('id')} could not run: {r.get('error')}")
        elif r["holds"] is not want:
            out.append(f"probe {r.get('id')} ({r.get('kind')}, finding {r.get('finding') or '-'}) "
                       + ("still fails after the patch" if want else "already holds before the patch, so it does not "
                          "encode its finding") + f": reward {r.get('reward')}, failing {r.get('failing')}")
    return out


def main(argv: list[str]) -> int:
    load_keys(prompt_if_missing=False)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    bundle = Path(argv[0]).resolve()
    doc = json.loads(Path(argv[1]).read_text())
    doc = {"edits": doc} if isinstance(doc, list) else doc
    edits, new_probes = doc.get("edits") or [], list(doc.get("probes") or [])
    # probes of findings fixed in earlier rounds: they must hold after this patch and join the set,
    # without having to reproduce on the current version
    carried = list(doc.get("regression") or [])
    dry = "--dry-run" in argv
    review = "--no-review" not in argv
    why = argv[argv.index("--why") + 1] if "--why" in argv else str(doc.get("why") or "")
    allow = tuple(argv[i + 1] for i, a in enumerate(argv) if a == "--allow-advisory" and i + 1 < len(argv))
    if "--from-review" in argv:
        new_probes += review_probes(bundle)
    from pipeline.artifacts import atomic_json
    record: dict = {"schema": "arena-coding/manual-patch/2", "why": why, "started_at": time.time(),
                    "patch_sha256": hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()}

    def stop(stage: str, problems: list[str], code: int = 1) -> int:
        record.update(stage=stage, refused=problems, finished_at=time.time())
        atomic_json(bundle / "verification/manual-patch.json", record)
        print(json.dumps({"stage": stage, "refused": problems}, indent=1, ensure_ascii=False)[:6000], flush=True)
        return code

    spec = _seed_manual_session(bundle)
    _, problems, record["plan"] = plan_patch(spec, edits, bundle, allow)
    print("plan:", json.dumps(record["plan"], ensure_ascii=False)[:2000], flush=True)
    if problems:
        return stop("plan", problems)
    handlers = manual_designer.handlers(bundle)
    regression = load_regression(bundle)
    if new_probes:
        before = handlers["run_probes"](probes=new_probes)
        record["reproduce"] = before
        print("reproduce:", json.dumps(before, ensure_ascii=False)[:3000], flush=True)
        if before.get("error"):
            return stop("reproduce", [before["error"]])
        problems = gate_probes(before["results"], want=False)
        if problems:
            return stop("reproduce", problems)
    if dry:
        return stop("dry-run", [], 0)
    for edit in edits:
        if "value" in edit:
            out = handlers["stage_spec"](path=edit["path"], value=edit["value"])
        else:
            out = handlers["stage_spec"](path=edit["path"], edit=edit["edit"])
        if out.get("error"):
            return stop("stage", [f"{edit['path']}: {out['error']}"])
        print("staged", edit["path"], out.get("sha256"), flush=True)
    verdict = handlers["check_draft"]()
    record["check_draft"] = {"clean": verdict.get("clean"), "problems": verdict.get("problems")}
    if "--measure" in argv:
        # Measure only (2026-10-28): what the staged edits do to every control, so the mutants' declared
        # failures can be set from execution before the real patch. Nothing is submitted or reviewed.
        measured = handlers["run_controls"]()
        record["measure"] = measured
        print("measure:", json.dumps(measured, ensure_ascii=False, default=str)[:12000], flush=True)
        return stop("measured", list(verdict.get("problems") or []), 0)
    if not verdict.get("clean"):
        return stop("check_draft", list(verdict.get("problems") or []))
    fresh = {q.get("id") for q in new_probes + carried}
    probes = new_probes + carried + [p for p in regression if p.get("id") not in fresh]
    if probes:
        after = handlers["run_probes"](probes=probes)
        record["confirm"] = after
        print("confirm:", json.dumps(after, ensure_ascii=False)[:3000], flush=True)
        if after.get("error"):
            return stop("confirm", [after["error"]])
        problems = gate_probes(after["results"], want=True)
        if problems:
            return stop("confirm", problems)
    submitted = manual_designer.submit(bundle)
    record["submit"] = {k: v for k, v in submitted.items() if k != "probes"}
    print("submit:", json.dumps(record["submit"], ensure_ascii=False)[:3000], flush=True)
    if submitted.get("control_errors") or submitted.get("gate_verdict") != "green":
        return stop("submit", [str(e) for e in submitted.get("control_errors") or []]
                    + [json.dumps(b, ensure_ascii=False)[:600] for b in submitted.get("gate_blockers") or []])
    if new_probes or carried:
        from pipeline.author import counterexamples, sandbox
        steps = sandbox._steps(bundle)
        record["regression"] = len(save_regression(bundle, [counterexamples.normalize(p, steps)
                                                            for p in new_probes + carried]))
    if review:
        from contextlib import nullcontext
        from pipeline import orchestrate
        reviewed = orchestrate._review_candidate(bundle, nullcontext())
        record["review"] = reviewed
        print("review:", reviewed.get("verdict"), json.dumps(reviewed.get("findings"), ensure_ascii=False)[:6000], flush=True)
    record.update(stage="reviewed" if review else "submitted", finished_at=time.time())
    atomic_json(bundle / "verification/manual-patch.json", record)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
