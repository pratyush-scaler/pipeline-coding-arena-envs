"""Opt-in native two-step oracle/no-op controls. Requires Docker and pinned Harbor."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from pipeline import policy
from pipeline.artifacts import atomic_json
from pipeline.benchmark.harbor import run_campaign
from pipeline.evidence import native_evidence
from pipeline.tests.test_production import synthetic_bundle


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--models", nargs="*", default=[])
    args = ap.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    bundle = synthetic_bundle(root / "source", two_steps=True)
    results = {}
    for agent, reward in (("oracle", 1), ("nop", 0)):
        report = run_campaign(bundle, root / agent, agent=agent, timeout_s=900)
        issues, _ = native_evidence(bundle, root / agent, agent)
        passed = report["ok"] and not issues and report["summary"]["mean_reward"] == reward
        results[agent] = {"passed": passed, "summary": report["summary"], "errors": report["errors"] + issues}
        print(json.dumps({agent: results[agent]}), flush=True)
        if not passed:
            atomic_json(root / "validation.json", results)
            return 1
    atomic_json(root / "validation.json", results)
    if args.models:
        from pipeline.cli import load_keys
        load_keys(prompt_if_missing=False)
    for model in args.models:
        # The calibration settings, not a private variant: the spec's effort,
        # no turn or time limits (a capped run would read as a failure).
        report = run_campaign(bundle, root / model, agent="terminus-2", model=model,
                              effort=policy.SPEC_CALIBRATION_EFFORT,
                              no_limits=policy.SPEC_NO_LIMITS)
        issues, _ = native_evidence(bundle, root / model, "terminus-2")
        results[model] = {"passed": report["ok"] and not issues and report["summary"]["mean_reward"] == 1,
                          "summary": report["summary"], "errors": report["errors"] + issues}
        print(json.dumps({model: results[model]}), flush=True)
    atomic_json(root / "validation.json", results)
    return 0 if len(results) == 2 + len(args.models) and all(r["passed"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
