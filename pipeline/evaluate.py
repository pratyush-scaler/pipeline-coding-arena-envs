"""Grade a prepared working tree with the shipped grader, exactly as Harbor would.

A thin wrapper over `author.sandbox.verify`: the agent image, a variant
applied to the tree, the capture, a fresh offline verifier, `/tests/test.sh`.
Every caller that used to run the expression grader in a subprocess now goes
through here, so there is one execution path and it is the shipped one.
"""
from __future__ import annotations

from pathlib import Path

from .author import sandbox

SUBMISSION_FAULTS = ("contract", "missing-working-tree", "verifier-timeout")


def exported_grade(bundle: Path, variants: dict | None = None, *, work: Path | None = None) -> dict:
    """{reward, valid, steps: {name: {reward, flags, contract_errors, error}}, errors}."""
    bundle = Path(bundle)
    ok, why = sandbox.available()
    if not ok:
        return {"reward": None, "valid": False, "steps": {}, "errors": [f"sandbox unavailable: {why}"]}
    try:
        record = sandbox.verify(bundle, variants or {}, work=work)
    except sandbox.SandboxError as exc:
        return {"reward": None, "valid": False, "steps": {}, "errors": [str(exc)]}
    steps = {}
    errors = []
    for name, entry in record["steps"].items():
        grade = entry.get("grade") or {}
        steps[name] = {"reward": entry.get("reward"), "valid": entry.get("valid"),
                       "flags": {cid: bool(r.get("passed")) for cid, r in (grade.get("checks") or {}).items()},
                       "contract_errors": grade.get("contract_errors") or [],
                       "error": entry.get("error")}
        if entry.get("error"):
            errors.append(f"{name}: {entry['error']}")
    return {"reward": record["reward"], "valid": record["valid"], "steps": steps, "errors": errors,
            "images": {"agent": record.get("agent_image"), "verifier": record.get("verifier_image")}}


def golden_variants(bundle: Path) -> dict:
    return {step: sandbox.solve_variant(bundle, step) for step in sandbox._steps(Path(bundle))}
