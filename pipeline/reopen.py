"""Reopen a review-blocked build for ONE more repair pass.

User, 2026-10-04, asked to reach 30 extremely hard tasks and chose to "Reopen all, 1 extra
pass": every build blocked only by the semantic review after its two repair passes gets one
more pass, under the user's standard for ambiguity ("some ambiguity is required in the task,
as far as the task remains correct and is understandable and performable by a human
carefully, the task should be valid").

Nothing is erased. The grant is written to the repair log beside the spent passes, the
granted pass is recorded as a pass, and the checkpoint resumes at `repair` with the
review's findings and no further passes (`gate_repairs_remaining` 0): if the review still
blocks the revision, the build stops there.

usage: python -m pipeline.reopen <bundle or tree> ... [--why TEXT]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import checkpoint, repair
from .artifacts import atomic_json
from .author.contracts import strict_json

DECISION = "user, 2026-10-04: reopen every review-blocked build for 1 extra repair pass"

GUIDANCE = (
    "Semantic review (operator): this build was blocked by the independent review after its "
    "repair passes and has been granted ONE more (user decision). Repair every finding below. "
    "Judge ambiguity by the user's standard: some ambiguity is required, and a task is valid as "
    "long as it stays correct and a careful human can understand and perform it from the "
    "instruction and the repository. Keep an ambiguity a careful reader resolves and prove it "
    "with determinacy_probe; publish a rule in the user's own voice only where a careful reader "
    "could defensibly reach a different answer. Always fix an incorrect or crashing reference "
    "(and the alternative), move a forgeable observation to the trusted side of the process "
    "boundary, and fix privacy. Never drop or weaken a check. Keep the task extremely hard: every "
    "step must stay at or under the per-step bar on BOTH models -- where publishing a rule "
    "exposes a trap, add a new interlocked hidden case. Run pre_review before submitting.")

REOPENABLE = ("blocked", "unverified")


def _review_findings(bundle: Path, job: dict) -> list[str]:
    """The review's findings as the build renders them (`Semantic review <id>: ...`)."""
    found = [str(b) for b in job.get("blockers") or [] if str(b).startswith("Semantic review ")]
    if found:
        return found
    from .review import review_findings
    out = []
    for text in review_findings(bundle):
        label, _, rest = text.partition(": ")
        if label.startswith("check "):
            out.append(f"Semantic review {label[6:]}: {rest}")
        else:
            out.append(f"Semantic review {label.replace(' ', '_')}: {rest}")
    return out


def reopen(bundle: Path, why: str = DECISION) -> str:
    bundle = Path(bundle).resolve()
    try:
        job = strict_json((bundle / "verification/state.json").read_text())
    except (OSError, ValueError):
        return "skip: no job record"
    if job.get("status") not in REOPENABLE:
        return f"skip: status {job.get('status')}"
    state = checkpoint.read(bundle)
    if state.get("stage") == "repair":
        return "skip: already reopened"
    if not state.get("submitted"):
        return "skip: no submitted draft to revise"
    findings = _review_findings(bundle, job)
    if not findings:
        return "skip: not blocked by the semantic review"
    checkpoint.submitted_spec(bundle, state)          # the draft must verify before anything changes
    log = repair.history(bundle)
    log.setdefault("grants", []).append({"at": time.time(), "passes": 1, "by": why,
                                         "status_before": job.get("status"),
                                         "stage_before": state.get("stage")})
    atomic_json(bundle / repair.LOG, log)
    repair.record_pass(bundle, findings, "granted", {"phase": "gate", "granted_by": why})
    checkpoint.update(bundle, stage="repair", feedback=[GUIDANCE, *findings], gate_repairs_remaining=0)
    return f"reopened ({len(findings)} finding(s))"


def _bundles(paths: list[str]) -> list[Path]:
    out = []
    for raw in paths:
        p = Path(raw)
        if (p / "verification/state.json").is_file():
            out.append(p)
        else:
            out += sorted(q.parents[1] for q in p.glob("**/c-*/verification/state.json")
                          if "hardened" not in q.parts)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arena reopen")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--why", default=DECISION)
    ns = ap.parse_args(argv)
    for bundle in _bundles(ns.paths):
        try:
            result = reopen(bundle, ns.why)
        except Exception as exc:                          # noqa: BLE001 -- reported, never fatal
            result = f"error: {type(exc).__name__}: {exc}"[:300]
        print(json.dumps({"bundle": str(bundle), "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
