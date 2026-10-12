"""Independent implementation provenance, derived before exposing any hidden test."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from ..artifacts import atomic_json
from .contracts import contained, strict_json


def public_inventory(package: Path) -> dict:
    files = {}
    for folder in ("environment", "steps"):
        for path in sorted((package / folder).rglob("*")):
            if path.is_symlink():
                raise ValueError("independent oracle inputs cannot contain symlinks")
            if path.is_file() and (folder != "steps" or path.name == "instruction.md"):
                files[path.relative_to(package).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def public_digest(files: dict) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def vendor(model: str) -> str:
    bare = str(model).split("/")[-1]
    return "anthropic" if bare.startswith("claude-") else "openai" if bare.startswith("gpt-") else "unknown"


def derive(package: Path, model: str, producer: str, work: Path, **kwargs) -> dict:
    from .blind_solve import solve
    if vendor(model) == "unknown" or vendor(producer) == "unknown" or vendor(model) == vendor(producer):
        raise ValueError("independent derivation requires a known different vendor")
    if work.exists():
        raise ValueError("independent derivation needs a fresh evidence directory")
    inputs = public_inventory(package)
    result = solve(package, model, work, grade=False, **kwargs)
    if public_inventory(package) != inputs:
        raise ValueError("public contract changed during independent derivation")
    patches = {step: (work / "steps" / step / "blind.patch").read_text()
               for step in result["step_names"]}
    evidence = {p.relative_to(work).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in work.rglob("*") if p.is_file() and not p.is_symlink()}
    receipt = {"schema": "arena-coding/independent-oracle/1", "model": model, "producer": producer,
               "public_inputs": inputs, "public_sha256": public_digest(inputs),
               "patch_sha256": {s: hashlib.sha256(p.encode()).hexdigest() for s, p in patches.items()},
               "complete": result["complete"], "limits": result["limits"], "evidence": evidence,
               "input_scope": "repository and sequential public instructions only",
               "hidden_tests_exposed": False, "reference_exposed": False, "grading_deferred": True}
    atomic_json(work / "receipt.json", receipt)
    return {"advisory_only": True, "receipt": receipt, "patches": patches,
            "note": "independently derived candidate, not proof of correctness; grade as the alternative during validation"}


def problems(spec: dict, bundle: Path) -> list[str]:
    """Only supplied provenance is enforced, and only in downstream validation."""
    ref = spec.get("quality", {}).get("independent_oracle") if isinstance(spec.get("quality"), dict) else None
    if ref is None:
        return []
    try:
        work = contained(bundle, ref["path"])
        receipt = strict_json((work / "receipt.json").read_text())
        if not receipt.get("complete") or receipt.get("reference_exposed") is not False or receipt.get("hidden_tests_exposed") is not False:
            raise ValueError("derivation incomplete or not blind")
        if vendor(receipt["model"]) == vendor(receipt["producer"]) or vendor(receipt["model"]) == "unknown":
            raise ValueError("derivation lacks a different vendor")
        inputs = {k: v for k, v in public_inventory(bundle).items() if k.startswith("environment/repo/")}
        # Reconstruct the toolchain from the candidate, even before its first export.
        # This is pure file generation inside validation, with no Docker or execution.
        from .build import environment_only
        with tempfile.TemporaryDirectory(prefix="arena-oracle-inputs-") as tmp:
            scratch = Path(tmp)
            (scratch / "environment/repo").mkdir(parents=True)
            environment_only(spec, scratch)
            inputs.update({k: v for k, v in public_inventory(scratch).items() if not k.startswith("environment/repo/")})
        for step in spec.get("steps") or []:
            inputs[f"steps/{step['name']}/instruction.md"] = hashlib.sha256(
                (step["instruction"].strip() + "\n").encode()).hexdigest()
        if public_digest(inputs) != receipt["public_sha256"]:
            raise ValueError("independent oracle belongs to a different public contract")
        for rel, digest in receipt["evidence"].items():
            if hashlib.sha256(contained(work, rel).read_bytes()).hexdigest() != digest:
                raise ValueError(f"derivation evidence changed: {rel}")
        for step, digest in receipt["patch_sha256"].items():
            patch = spec.get("alternative", {}).get(step, {}).get("patch", "")
            if hashlib.sha256(patch.encode()).hexdigest() != digest:
                raise ValueError(f"{step}: alternative differs from the independently derived patch")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return [f"independent oracle provenance: {exc}"]
    return []
