"""Opt-in real Docker controls followed by a metadata-only export, with no model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from pipeline.artifacts import atomic_json, identity
from pipeline.author import controls, sandbox
from pipeline.naming import rename_text
from pipeline.tests.synthetic_code import write_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    bundle = write_bundle(args.out / "bundle", two_steps=True)
    before = controls.run(bundle, workers=2, reuse=False)
    if not before["ok"]:
        print(json.dumps(before["errors"], indent=2))
        return 1
    source_hash = identity(bundle)
    path = bundle / "task.toml"
    path.write_text(rename_text(path.read_text(), "arena/a_f2_exported-control-smoke"))
    atomic_json(bundle / "provenance/ledger.json", {"faithfulness_level": "F2", "cache_smoke": True})
    atomic_json(bundle / "authoring/extraction/fixture-authored.json", {"files": {}, "cache_smoke": True})
    # An actual cache miss raises: the export must make no Docker calls.
    with patch.object(sandbox, "build_image", side_effect=AssertionError("duplicate image build")), \
            patch.object(sandbox, "verify", side_effect=AssertionError("duplicate control execution")):
        after = controls.run(bundle, workers=2)
    failures = controls.problems(bundle)
    if (not after["execution"]["reused"] or after["artifact_sha256"] == source_hash
            or after["execution"]["source_artifact_sha256"] != source_hash
            or after["results"] != before["results"]):
        failures.append("incorrect execution lineage or changed control results")
    report = {"source_artifact_sha256": source_hash, "export_artifact_sha256": identity(bundle),
              "executed_trials": before["execution"]["executed_new_trials"], "steps": len(before["steps"]),
              "source_seconds": before["seconds"], "reuse_seconds": after["seconds"],
              "new_export_trials": after["execution"]["executed_new_trials"],
              "evidence_sha256": after["execution"]["evidence_sha256"], "failures": failures}
    atomic_json(args.out / "reuse-smoke.json", report)
    print(json.dumps(report, indent=2))
    print("PASS" if not failures else "FAIL")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
