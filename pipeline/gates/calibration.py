"""Post-calibration gates: what measured episodes say about the grader.

`calibration.bands` is DIAGNOSTIC (policy.BANDS_GATE_RELEASE is False): it
reports where each check sat and blocks only on a check no episode could
reach. `calibration.validity` is what carries the burden: every zero is
classified, the evidence is intact, the grader did not change mid-campaign.
An all-fail task is admissible (policy.DIFFICULTY_GATES_RELEASE is False), so
"no variance" is reported, never a blocker.
"""
from __future__ import annotations

from collections import Counter

from .. import policy
from .base import Finding, FunctionGate, GateContext, Severity

B, W = Severity.BLOCKER, Severity.WARN


def _bands(ctx: GateContext):
    report = ctx.extras.get("calibration")
    if report is None:
        raise RuntimeError("no calibration report supplied; run the campaign first")
    checks = report.get("checks") or {}
    if not checks:
        raise RuntimeError("calibration report contains no per-check results")
    from ..measurement import band as measured_band
    findings, bands, rates = [], {}, {}
    for check, row in checks.items():
        attempts = row.get("passed", 0) + row.get("failed", 0)
        if attempts == 0:
            bands[check] = "unreachable"
            if policy.MAX_UNREACHABLE_CHECKS == 0:
                findings.append(Finding(B, "dead-check", f"check '{check}' was never reachable in any episode", "tests/checks.json"))
            continue
        rate = row["passed"] / attempts
        rates[check] = round(rate, 3)
        bands[check] = measured_band(rate)
    counts = Counter(b for cid, b in bands.items() if checks[cid].get("kind") != "guardrail")
    if counts["core"] < 1:
        findings.append(Finding(W if not policy.BANDS_GATE_RELEASE else B, "no-core-checks",
                                "no objective measured in the 30-70% band (diagnostic)", "tests/checks.json"))
    never_failed = [cid for cid, b in bands.items() if b == "floor" and checks[cid].get("kind") == "objective"]
    if never_failed:
        findings.append(Finding(W, "never-failed", "objectives every attempt passed (they measure nothing yet): "
                                + ", ".join(never_failed), "tests/checks.json"))
    return findings, {"bands": bands, "rates": rates, "counts": dict(counts)}


def _validity(ctx: GateContext):
    report = ctx.extras.get("calibration")
    if report is None:
        raise RuntimeError("no calibration report supplied")
    rollouts = report.get("rollouts") or []
    if not rollouts:
        raise RuntimeError("calibration report lists no rollouts")
    findings = []
    if not ctx.extras.get("legacy"):
        from ..evidence import calibration_problems
        findings.extend(Finding(B, "invalid-evidence", p, "calibration") for p in calibration_problems(ctx.task_dir, report))
    unclassified = [r.get("id") for r in rollouts if r.get("reward") in (None, 0, 0.0) and not r.get("zero_class")]
    for rid in unclassified[:10]:
        findings.append(Finding(B, "unclassified-zero", f"rollout {rid} scored zero (or nothing) with no zero_class", "calibration"))
    voided = [r for r in rollouts if r.get("zero_class") == "void"]
    if len(voided) / len(rollouts) > 0.10:
        findings.append(Finding(W, "high-invalid-rate", f"{len(voided) / len(rollouts):.0%} of rollouts are void", "calibration"))
    if report.get("grader_stable") is False:
        findings.append(Finding(B, "grader-changed-mid-campaign", "tests/ was rewritten while this campaign ran", "calibration"))
    rewards = [r["reward"] for r in rollouts if isinstance(r.get("reward"), (int, float)) and r.get("zero_class") != "void"]
    if rewards and len(set(rewards)) == 1:
        findings.append(Finding(W, "no-variance", f"every valid rollout scored {rewards[0]}; an all-fail task is admissible "
                                "only with the all-fail proofs in place (attribution)", "calibration"))
    return findings, {"void": len(voided), "valid": len(rewards)}


bands_gate = FunctionGate("calibration.bands", _bands)
validity_gate = FunctionGate("calibration.validity", _validity)
