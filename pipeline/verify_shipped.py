"""The grader as it ships: untouched fixture 0.0, reference 1.0, through the
package's own images. Nothing from this repository is on the path inside the
containers; what runs is `tests/grade.py` as Harbor will run it.

Exit codes: 0 every bundle verified, 1 a bundle failed, 2 could not verify.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .artifacts import atomic_json, identity
from .author import sandbox
from .config import bundle_paths
from .evaluate import exported_grade, golden_variants

RECEIPT = "verification/verify-shipped.json"


def run_reference(bundle: Path) -> dict:
    bundle = Path(bundle)
    nop = exported_grade(bundle)
    golden = exported_grade(bundle, golden_variants(bundle))
    problems = []
    if not nop["valid"]:
        problems.append("untouched fixture: " + "; ".join(nop["errors"] or ["no valid reward"]))
    elif nop["reward"] != 0.0:
        problems.append(f"untouched fixture scores {nop['reward']}, not 0")
    if not golden["valid"]:
        problems.append("reference: " + "; ".join(golden["errors"] or ["no valid reward"]))
    elif golden["reward"] != 1.0:
        failing = {s: [c for c, ok in v["flags"].items() if not ok] for s, v in golden["steps"].items()}
        problems.append(f"reference scores {golden['reward']}, not 1.0; failing {failing}")
    record = {"schema": "arena-coding/verify-shipped/1", "bundle": bundle.name,
              "artifact_sha256": identity(bundle), "nop": nop, "golden": golden,
              "problems": problems, "ok": not problems,
              "unverified": bool(nop["errors"] and not nop["valid"] or golden["errors"] and not golden["valid"])}
    atomic_json(bundle / RECEIPT, record)
    return record


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", nargs="?", default="envs")
    ap.add_argument("-o", "--out")
    args = ap.parse_args(argv)
    ok, why = sandbox.available()
    if not ok:
        print(f"UNVERIFIED: {why}")
        return 2
    root = Path(args.root)
    bundles = sorted(set(bundle_paths(root)) | ({root} if (root / "task.toml").is_file() else set()))
    reports, worst = [], 0
    for bundle in bundles:
        record = run_reference(bundle)
        reports.append(record)
        state = "UNVERIFIED" if record["unverified"] else ("PASS" if record["ok"] else "FAIL")
        worst = max(worst, 2 if record["unverified"] else (0 if record["ok"] else 1))
        print(f"{state:10s} {bundle.name}  nop={record['nop']['reward']} golden={record['golden']['reward']}")
        for p in record["problems"]:
            print(f"           {p}")
    if args.out:
        Path(args.out).write_text(json.dumps(reports, indent=2))
    return worst


if __name__ == "__main__":
    sys.exit(main())
