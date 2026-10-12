"""Opt-in paid API interface check using only synthetic public ticket data."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from pipeline.artifacts import atomic_json
from pipeline.calibrate.run import campaign
from pipeline.cli import load_keys
from pipeline.evidence import calibration_problems
from pipeline.tests.test_production import synthetic_bundle


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--models", nargs="+", default=["claude-opus-5", "gpt-5.6-sol"])
    args = ap.parse_args()
    load_keys(prompt_if_missing=False)
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    bundle = synthetic_bundle(root, two_steps=True)
    results = {}
    for model in args.models:
        report = campaign(bundle, model, n=1, workers=1, max_steps=12)
        try:
            errors = calibration_problems(bundle, report, replay=True)
        except Exception as exc:
            errors = [f"{type(exc).__name__}: {exc}"]
        results[model] = {"summary": report["summary"], "errors": errors,
                          "attempt_errors": [r["error"] for r in report["rollouts"] if r["error"]]}
        print(json.dumps({model: results[model]}), flush=True)
    atomic_json(root / "validation.json", results)
    return 0 if all(not r["errors"] and r["summary"]["mean_reward"] == 1 for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
