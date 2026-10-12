"""The shipped grader runtime: one reviewed file, hash-pinned into every bundle."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRADER = HERE / "grader_runtime" / "grade.py"
SHIPPED = "tests/grade.py"


def _source(bundle: Path | None = None) -> bytes:
    source = GRADER.read_bytes()
    if bundle is None or not (Path(bundle) / "environment/toolchain.json").is_file():
        return source
    from . import toolchain
    chain = toolchain.load(Path(bundle))
    if chain["name"] in {"node", "browser"}:
        from .grader_runtime.node_test import render
        return render(source)
    if chain["name"] == "go":
        from .grader_runtime.go_test import render
        return render(source)
    return source


def sha256(bundle: Path | None = None) -> str:
    return hashlib.sha256(_source(bundle)).hexdigest()


def stage(bundle: Path) -> Path:
    target = Path(bundle) / SHIPPED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_source(Path(bundle)))
    target.chmod(0o644)
    return target


def problems(bundle: Path) -> list[str]:
    target = Path(bundle) / SHIPPED
    if not target.is_file():
        return [f"{SHIPPED} is missing"]
    if hashlib.sha256(target.read_bytes()).hexdigest() != sha256(Path(bundle)):
        return [f"{SHIPPED} differs from the reviewed grader runtime; re-export the bundle"]
    stray = sorted(p.relative_to(bundle).as_posix() for p in (Path(bundle) / "tests").glob("*.py")
                   if p.name != "grade.py")
    if stray:
        return ["tests/ may hold no Python beyond grade.py and suites/: " + ", ".join(stray)]
    return []
