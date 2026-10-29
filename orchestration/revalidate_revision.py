"""Re-validate a hardened revision of a hand-built bundle (bash-failure samples, 2026-10-24).

`harden` writes authoring/traps.json's usable_corrections from the classification row after building the
revision, but keeps the spec's own value. For a designed bundle both come from one classification; for a
hand-built one (orchestration/manual_designer.py) they can differ, and the screen then refuses the revision
as "inconsistent with the current generator". This aligns the spec with the classification's count, rebuilds,
and re-runs controls, probes, the gate chain and the export-drift check. Nothing graded changes.

    ARENA_DUMP=... ARENA_PIPELINE_ARTIFACTS=... .venv/bin/python orchestration/revalidate_revision.py <revision>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.cli import load_keys  # noqa: E402


def main(revision: Path) -> dict:
    from pipeline import assurance as assurance_stage
    from pipeline.artifacts import atomic_json
    from pipeline.author import build as build_stage
    from pipeline.author import controls as controls_stage
    from pipeline.author import probes as probe_stage
    from pipeline.run import run_bundle
    spec = build_stage.load_spec(revision)
    traps = json.loads((revision / "authoring/traps.json").read_text())
    out = {"usable_corrections": {"spec": spec.get("usable_corrections"), "traps": traps.get("usable_corrections")}}
    spec["usable_corrections"] = traps.get("usable_corrections", spec.get("usable_corrections", 0))
    build_stage.build(spec, revision)
    # The package name is rewritten after the build (orchestrate / harden do the same); without it qualification
    # refuses the version's faithfulness naming (c-003 v6 release, 2026-10-24).
    from pipeline import naming
    level = json.loads((revision / "provenance/ledger.json").read_text()).get("faithfulness_level")
    if level:
        out["naming"] = naming.apply(revision, level)
    try:
        receipt = controls_stage.run(revision)
    except controls_stage._ControlPreflightFailure as exc:
        receipt = {"errors": [str(exc)]}
    if receipt.get("results") is not None:
        probe_stage.run(revision, controls_receipt=receipt)
    gates = run_bundle(revision, ROOT)
    out["control_errors"] = receipt.get("errors")
    out["gate_verdict"] = gates["verdict"]
    out["gate_blockers"] = [{"gate": r.get("gate"), "blockers": r.get("blockers")}
                            for r in gates.get("results", []) if r.get("blockers")]
    if gates["verdict"] == "green" and not receipt.get("errors"):
        out["export_drift"] = assurance_stage.generated_problems(revision)
    atomic_json(revision / "verification/revalidate.json", out)
    return out


if __name__ == "__main__":
    load_keys(prompt_if_missing=False)
    print(json.dumps(main(Path(sys.argv[1]).resolve()), indent=1, default=str))
