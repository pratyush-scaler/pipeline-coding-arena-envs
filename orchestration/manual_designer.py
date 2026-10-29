"""Hand authoring through the designer's own tool layer (bash-failure samples, 2026-10-22).

The pipeline's designer is a model session calling `agent_designer._handlers`. This CLI
calls the same handlers from a shell, so an operator (or an operator's agent) can author
a bundle by hand and still go through extraction, the ledger, stage_spec, check_draft,
run_controls, pre_review and blind_solve exactly as a designed bundle does.

    ARENA_DUMP=... ARENA_PIPELINE_ARTIFACTS=... .venv/bin/python orchestration/manual_designer.py \
        init <bundle> <session-id-or-prefix> <seed-index> <F0|F1|F2|F3>
    ... manual_designer.py call <bundle> <tool> '<json args>'    # or @args.json
    ... manual_designer.py tools <bundle>                        # list tool names
    ... manual_designer.py submit <bundle> [--no-controls]       # export, ledger, controls, probes, gates
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.cli import load_keys  # noqa: E402


#: The hand author (user ruling 2026-10-23: record claude-opus-5-5), so the semantic review picks the
#: other family (orchestrate._review_candidate).
PRODUCER = {"model": "claude-opus-5-5", "effort": "manual"}


def stamp_producer(bundle: Path) -> dict:
    """Name the author of an already-submitted bundle and re-export; nothing graded changes."""
    from pipeline.author import build as build_stage
    spec = build_stage.load_spec(bundle)
    if spec.get("_producer"):
        return {"unchanged": spec["_producer"]}
    spec["_producer"] = PRODUCER
    build_stage.build(spec, bundle)
    return {"stamped": PRODUCER}


def _sid(prefix: str) -> str:
    from pipeline.config import sessions_root
    hits = sorted(p.name for p in sessions_root().iterdir() if p.name.startswith(prefix))
    if len(hits) != 1:
        raise SystemExit(f"session prefix {prefix!r} matches {hits}")
    return hits[0]


def _row(bundle: Path) -> dict:
    return json.loads((bundle / "authoring/manual-row.json").read_text())


def init(bundle: Path, prefix: str, seed: int, level: str) -> dict:
    from pipeline.author import extract as extract_stage
    from pipeline.artifacts import atomic_json
    sid = _sid(prefix)
    if (bundle / "environment/repo").exists():
        raise SystemExit(f"{bundle} already initialised")
    extraction = extract_stage.extract(sid, seed)
    extract_stage.write(extraction, bundle)
    from pipeline.ledger.redaction import minimize
    try:
        minimize(bundle, sid)
    except ValueError as exc:
        atomic_json(bundle / "verification/creation-advisories.json",
                    {"privacy": str(exc), "validation_deferred": True})
    row = {"session_id": sid, "seed_index": seed, "authoring_target_level": level,
           "target_level": level, "manual": True}
    atomic_json(bundle / "authoring/manual-row.json", row)
    return {"bundle": str(bundle), "session_id": sid, "seed": seed, "span": list(extraction.span),
            "pivot": extraction.pivot, "stats": extraction.stats}


def handlers(bundle: Path) -> dict:
    from pipeline.author import agent_designer as ad
    from pipeline.author import extract as extract_stage
    from pipeline.dump import recover as rec
    row = _row(bundle)
    extraction = extract_stage.extract(row["session_id"], row["seed_index"])
    trace = rec.load_trace(extraction.session_id, getattr(extraction, "source_root", None))
    log = bundle / "verification/design-sessions/manual"
    return ad._handlers(bundle, extraction, trace, log / "draft.json",
                        level=row["authoring_target_level"], row=row, log=log)


def submit(bundle: Path, controls: bool = True) -> dict:
    """What orchestrate does after a designer returns its spec: export, ledger, controls, probes,
    gates and the export-drift check. The semantic review stays `./arena review <bundle>`."""
    import hashlib
    import time
    from pipeline import assurance as assurance_stage
    from pipeline import checkpoint as progress
    from pipeline import naming
    from pipeline.artifacts import atomic_json
    from pipeline.author import build as build_stage
    from pipeline.author import controls as controls_stage
    from pipeline.author import creation
    from pipeline.author import extract as extract_stage
    from pipeline.author import probes as probe_stage
    from pipeline.author.agent_designer import validate_source_capacity
    from pipeline.ledger import faithfulness
    from pipeline.run import run_bundle
    row = _row(bundle)
    spec = handlers(bundle)["draft_spec"]()
    spec.setdefault("_producer", PRODUCER)
    extraction = extract_stage.extract(row["session_id"], row["seed_index"])
    attempt_path = bundle / "verification" / "design-attempts" / f"{time.time_ns()}.json"
    atomic_json(attempt_path, spec)
    progress.update(bundle, stage="submitted", authoring_session=None,
                    submitted={"path": attempt_path.relative_to(bundle).as_posix(),
                               "sha256": hashlib.sha256(attempt_path.read_bytes()).hexdigest()},
                    workspace_files=progress.evidence_inventory(bundle, progress.WORKSPACE))
    spec["usable_corrections"] = extract_stage.usable_correction_count(row, extraction)
    atomic_json(bundle / "authoring" / "spec.json", spec)
    created = creation.record(spec, bundle, reason="retained-before-validation")
    rejection = validate_source_capacity(spec, bundle, source_root=getattr(extraction, "source_root", None))
    if rejection is not None:
        return {"stage": "source-capacity", "rejection": rejection}
    build_stage.build(spec, bundle)
    atomic_json(bundle / "verification/submission-validation.json",
                {"verdict": "exported", "phase": "validation",
                 "note": "export succeeded; controls, gates and semantic review still pending"})
    ledger = faithfulness.build(bundle, row["session_id"], row["seed_index"])
    ledger["category"] = row.get("category")
    ledger["category_inputs"] = {"replay_readiness": None, "corrections": None}
    ledger = faithfulness.with_level(bundle, ledger)
    (bundle / "provenance").mkdir(parents=True, exist_ok=True)
    (bundle / "provenance" / "ledger.json").write_text(json.dumps(ledger, indent=2))
    naming.apply(bundle, ledger["faithfulness_level"])
    progress.update(bundle, stage="checking",
                    workspace_files=progress.evidence_inventory(bundle, progress.WORKSPACE))
    out = {"creation": created["status"], "missing": created["missing_components"],
           "faithfulness": ledger["faithfulness_by_bytes"], "level": ledger["faithfulness_level"]}
    if not controls:
        return out
    try:
        receipt = controls_stage.run(bundle)
    except controls_stage._ControlPreflightFailure as exc:
        receipt = {"errors": [str(exc)]}
    if receipt.get("results") is not None:
        report = probe_stage.run(bundle, controls_receipt=receipt)
        out["probes"] = {k: (v.get("reward") if isinstance(v, dict) else None)
                         for k, v in report["results"].items() if isinstance(v, dict)}
    gates = run_bundle(bundle, ROOT)
    out["control_errors"] = receipt.get("errors")
    out["gate_verdict"] = gates["verdict"]
    out["gate_blockers"] = [{"gate": r.get("gate"), "blockers": r.get("blockers")}
                            for r in gates.get("results", []) if r.get("blockers")]
    if gates["verdict"] == "green" and not receipt.get("errors"):
        out["export_drift"] = assurance_stage.generated_problems(bundle)
    atomic_json(bundle / "verification/manual-submit.json", out)
    return out


def main(argv: list[str]) -> int:
    load_keys(prompt_if_missing=False)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "init":
        out = init(Path(argv[1]).resolve(), argv[2], int(argv[3]), argv[4])
    elif cmd == "submit":
        out = submit(Path(argv[1]).resolve(), controls="--no-controls" not in argv)
    elif cmd == "stamp-producer":
        out = stamp_producer(Path(argv[1]).resolve())
    elif cmd == "tools":
        out = sorted(handlers(Path(argv[1]).resolve()))
    elif cmd == "call":
        bundle, tool = Path(argv[1]).resolve(), argv[2]
        raw = argv[3] if len(argv) > 3 else "{}"
        args = json.loads(Path(raw[1:]).read_text() if raw.startswith("@") else raw)
        out = handlers(bundle)[tool](**args)
    else:
        raise SystemExit(f"unknown command {cmd!r}")
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
