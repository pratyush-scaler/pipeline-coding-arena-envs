"""Opt-in: the synthetic coding task through the local sandbox. Needs Docker.

Builds both images, then runs the shipped grader over the untouched fixture
(must score 0 with every guardrail intact), the reference (1.0), the
alternative fix (1.0), every mutant (fails exactly its trap's checks) and the
reference plus harmless additions (no check moves but a published file budget).
This is the contract the whole pipeline rests on, executed.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from pipeline.author import controls, sandbox
from pipeline.tests.synthetic_code import write_bundle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--two-steps", action="store_true", help="the two-step variant (history upload, per-step budgets)")
    ap.add_argument("--control-workers", type=int, choices=(1, 2, 3, 4), default=1,
                    help="independent variants to execute concurrently")
    args = ap.parse_args()
    ok, why = sandbox.available()
    if not ok:
        print(why)
        return 2
    root = args.out or Path(tempfile.mkdtemp(prefix="arena-code-smoke-"))
    bundle = write_bundle(root / "bundle", two_steps=args.two_steps)
    print(f"bundle at {bundle}")
    agent = sandbox.build_image(bundle, "agent", log=root / "build-agent.log")
    verifier = sandbox.build_image(bundle, "verifier", log=root / "build-verifier.log")
    print(f"images {agent} {verifier}")
    spec = json.loads((bundle / "authoring/spec.json").read_text())
    steps = [s["name"] for s in spec["steps"]]
    step_of = {c["id"]: c["step"] for c in spec["checks"]}
    traps = {tr["id"]: tr for tr in spec["traps"]}

    def patch(rel, label):
        return sandbox.Variant("patch", patch=(bundle / rel).read_text(), label=label)

    golden = {s: sandbox.solve_variant(bundle, s) for s in steps}
    runs = {"nop": {}, "golden": golden,
            "alt": {s: patch(f"authoring/alt/{s}.patch", f"alt:{s}") for s in steps}}
    mutant_step = {}
    for mutant in spec["mutants"]:
        driven = traps[mutant["trap"]]["checks"]
        step = step_of[driven[0]]
        mutant_step[mutant["id"]] = step
        # Earlier steps keep the reference so the mutant's step is reachable;
        # later steps are left untouched, exactly as the controls compose it.
        variants = {s: golden[s] for s in steps[:steps.index(step)]}
        variants[step] = patch(f"authoring/mutants/{mutant['id']}.patch", mutant["id"])
        runs[f"mutant:{mutant['id']}"] = variants
    # The reference plus what an honest agent leaves beside it, on every step (2026-10-21 audit).
    runs["neutral_additions"] = controls.neutral_variants(bundle, golden, steps, sandbox._toolchain(bundle))
    results, failures = {}, []
    records = controls._execute_trials(bundle, runs, root / "runs", agent, verifier,
                                       args.control_workers, keep_capture=args.keep)
    for label, record in records.items():
        per_step = {}
        for step, entry in record["steps"].items():
            grade = entry.get("grade") or {}
            per_step[step] = {cid: bool(rec.get("passed")) for cid, rec in (grade.get("checks") or {}).items()}
            if not entry.get("valid"):
                failures.append(f"{label}/{step}: {entry.get('error')} {entry.get('verifier_stderr', '')[-500:]}")
        results[label] = {"reward": record["reward"], "valid": record["valid"], "checks": per_step}
        flat = " ".join(f"{cid}={'P' if ok else 'F'}" for flags in per_step.values() for cid, ok in flags.items())
        print(f"{label:28s} reward={record['reward']} valid={record['valid']} {flat}")
    expect = {"nop": 0.0, "golden": 1.0, "alt": 1.0}
    for label, want in expect.items():
        if results[label]["reward"] != want:
            failures.append(f"{label}: reward {results[label]['reward']}, expected {want}")
    nop_flags = {cid: ok for flags in results["nop"]["checks"].values() for cid, ok in flags.items()}
    nop_guards = [cid for cid, ok in nop_flags.items() if cid.startswith("g") and not ok]
    if nop_guards:
        failures.append(f"nop: guardrails failed on the untouched fixture: {nop_guards}")
    nop_objectives = [cid for cid, ok in nop_flags.items() if cid.startswith("o") and ok]
    if nop_objectives:
        failures.append(f"nop: objectives passed on the untouched fixture: {nop_objectives}")
    for mutant in spec["mutants"]:
        got = results[f"mutant:{mutant['id']}"]["checks"].get(mutant_step[mutant["id"]], {})
        failed = sorted(cid for cid, ok in got.items() if not ok)
        if failed != sorted(mutant["fails"]):
            failures.append(f"mutant {mutant['id']}: failed {failed}, declared {sorted(mutant['fails'])}")
    doc = json.loads((bundle / "tests/checks.json").read_text())
    misfires, budgets = controls.neutral_moves(doc, results["golden"]["checks"], results["neutral_additions"]["checks"])
    for step, ids in misfires.items():
        failures.append(f"neutral_additions/{step}: the reference plus harmless additions fails {ids}")
    # Every step's zero-allowance tree-guard ("Do not add files") is a published budget: it must count the
    # planted files, which proves the variant planted them and the capture carried them to the verifier.
    if sorted(budgets) != sorted(steps):
        failures.append(f"neutral_additions: the file budgets did not see the planted files on every step: {budgets}")
    (root / "smoke.json").write_text(json.dumps({"results": results, "failures": failures}, indent=2))
    for line in failures:
        print("FAIL", line)
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
