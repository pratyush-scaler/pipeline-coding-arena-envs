"""The reference solution of a coding task: a patch that derives the fix.

Harbor's `hardcoded_solution` criterion fails a solution that "simply prints
or writes the final answer without computation". A coding reference applies a
source patch and runs the build the instruction names; the tests then compute
their verdict from the changed program. What it must never do is copy a
finished tree over the fixture, or write a test's expected output directly.

`problems()` is the static side; the executed side (reference scores 1.0,
base fails, alternative scores 1.0) is `controls.run`.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

FORBIDDEN = (re.compile(r"\bcp\s+-[a-zA-Z]*[rR]"), re.compile(r"\brsync\b"), re.compile(r"\btar\s+-?x"),
             re.compile(r"/logs/verifier"), re.compile(r"reward\.json"), re.compile(r"/tests\b"))
DIFF_HEADER = re.compile(r"^(--- |\+\+\+ |diff --git )", re.M)


def _steps(bundle: Path) -> list[str]:
    config = tomllib.loads((bundle / "task.toml").read_text())
    return [s["name"] for s in config.get("steps") or []]


def patch_touches(patch: str) -> list[str]:
    """Paths a unified diff modifies (the b/ side)."""
    paths = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if target.startswith("b/"):
                target = target[2:]
            if target != "/dev/null":
                paths.append(target)
    return paths


def problems(bundle: Path) -> list[str]:
    bundle = Path(bundle)
    issues = []
    steps = _steps(bundle)
    if not steps:
        return ["task.toml declares no steps"]
    for step in steps:
        folder = bundle / "steps" / step / "solution"
        solve, patch = folder / "solve.sh", folder / "fix.patch"
        if not solve.is_file() or not patch.is_file():
            issues.append(f"steps/{step}/solution needs solve.sh and fix.patch")
            continue
        script = solve.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if pattern.search(script):
                issues.append(f"steps/{step}/solution/solve.sh: {pattern.pattern!r} -- a reference copies "
                              "no finished tree and touches no grader path")
        text = patch.read_text(encoding="utf-8")
        if not DIFF_HEADER.search(text):
            issues.append(f"steps/{step}/solution/fix.patch is not a unified diff")
        touched = patch_touches(text)
        if not touched:
            issues.append(f"steps/{step}/solution/fix.patch changes no file")
        for rel in touched:
            if rel.startswith("tests/") or rel.startswith("/"):
                issues.append(f"steps/{step}/solution/fix.patch touches {rel}: outside the working tree")
    top = bundle / "solution"
    if not (top / "solve.sh").is_file() or not (top / "fix.patch").is_file():
        issues.append("solution/ needs solve.sh and fix.patch (Harbor's single-step oracle entry)")
    elif (top / "fix.patch").read_bytes() != (bundle / "steps" / steps[0] / "solution/fix.patch").read_bytes():
        issues.append("solution/fix.patch differs from the first step's patch")
    return issues


def alternative_problems(bundle: Path) -> list[str]:
    bundle = Path(bundle)
    issues = []
    for step in _steps(bundle):
        alt = bundle / "authoring/alt" / f"{step}.patch"
        ref = bundle / "steps" / step / "solution/fix.patch"
        if not alt.is_file():
            issues.append(f"authoring/alt/{step}.patch: every step needs an independently written second fix")
            continue
        if alt.read_text(encoding="utf-8").strip() == ref.read_text(encoding="utf-8").strip():
            issues.append(f"authoring/alt/{step}.patch is identical to the reference")
        if not DIFF_HEADER.search(alt.read_text(encoding="utf-8")):
            issues.append(f"authoring/alt/{step}.patch is not a unified diff")
    return issues
