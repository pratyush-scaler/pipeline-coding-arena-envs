"""Require current reproducible exports unless legacy diagnostics are explicit."""
from pathlib import Path

from ..artifacts import identity, policy_identity
from ..author.contracts import strict_json


def validate_task(bundle: Path, *, legacy_diagnostic: bool = False) -> dict:
    sha = identity(bundle)
    if legacy_diagnostic:
        return {"artifact_sha256": sha, "mode": "legacy-diagnostic",
                "release_qualified": False, "current_generator_verified": False,
                "note": "Original task semantics retained; these results cannot qualify the current pipeline."}
    from ..assurance import generated_problems
    try:
        spec = strict_json((bundle / "authoring/spec.json").read_text())
        if not spec.get("checks") or not spec.get("suites"):
            raise ValueError("missing checks or hidden suites")
        errors = generated_problems(bundle)
        if errors:
            raise ValueError("; ".join(errors))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Task is legacy, stale, or inconsistent with the current generator: {exc}. "
                         "Rebuild and validate the task; use --legacy-diagnostic only for explicit historical measurements.") from exc
    if identity(bundle) != sha:
        raise ValueError("Task changed during benchmark preflight")
    return {"artifact_sha256": sha, "policy_sha256": policy_identity(),
            "mode": "current-draft", "release_qualified": False,
            "current_generator_verified": True,
            "note": "Current reproducible export; production acceptance requires complete release qualification."}
