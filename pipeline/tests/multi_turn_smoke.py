"""Opt-in: the multi-turn controls (Spec Appendix A) executed on the two-step
synthetic through the real `controls.run`. Needs Docker.

prefix_only:02-counts (step 1's reference, nothing at step 2) must pass no
step-2 objective; skip_prior:02-counts (step 2's reference on the untouched
fixture) must not score step 2 full credit. Prints the receipt's turn
results and every control error; exits non-zero on any error.
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
    ap.add_argument("--control-workers", type=int, choices=(1, 2, 3, 4), default=3)
    args = ap.parse_args()
    ok, why = sandbox.available()
    if not ok:
        print(why)
        return 2
    root = args.out or Path(tempfile.mkdtemp(prefix="arena-mt-smoke-"))
    bundle = write_bundle(root / "bundle", two_steps=True)
    print(f"bundle at {bundle}")
    receipt = controls.run(bundle, work=root / "controls", workers=args.control_workers)
    turns = (receipt.get("results") or {}).get("turns")
    print("turns:", json.dumps(turns, indent=2))
    print("ok:", receipt.get("ok"))
    for error in receipt.get("errors") or []:
        print("ERROR:", error)
    expected = {"prefix_only:02-counts", "skip_prior:02-counts"}
    if not turns or set(turns) != expected:
        print(f"FAIL: turn controls missing, expected {sorted(expected)}")
        return 1
    if turns["prefix_only:02-counts"]["passing"]:
        print("FAIL: step-2 objectives pass with step 1's reference alone")
        return 1
    if turns["skip_prior:02-counts"]["step_reward"] == 1.0:
        print("FAIL: step 2 is full credit without step 1")
        return 1
    # The synthetic's step-1 rule "Do not add files" is a zero-allowance tree-guard,
    # so benign_test (which adds a regression test file) fails on it by design; that
    # error is the synthetic's property, not a turn control's.
    others = [e for e in receipt.get("errors") or [] if not e.startswith("benign_test")]
    return 0 if not others else 1


if __name__ == "__main__":
    sys.exit(main())
