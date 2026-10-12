"""Artifact identity, atomic receipts and process-safe ownership (stdlib only)."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .author.contracts import contained

#: The release surfaces: what the artifact identity hashes, what a freeze
#: copies, what a reviewer may see. `authoring/extraction/` holds trace
#: material and stays out -- except the designer's declaration of authored
#: fixture bytes, which the provenance ledger is rebuilt from: without it a
#: frozen copy re-classifies every authored file and fails its own
#: provenance check (c-003, 2026-09-28, the first task whose repair session
#: authored fixture files).
ROOTS = ("task.toml", "steps", "environment", "tests", "solution", "provenance",
         "authoring/spec.json", "authoring/traps.json", "authoring/mutants", "authoring/alt",
         "authoring/oracles",
         "authoring/extraction/fixture-authored.json", ".dockerignore")


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def inventory(bundle: Path) -> dict[str, str]:
    bundle = Path(bundle).resolve()
    files = {}
    for rel in ROOTS:
        root = contained(bundle, rel)
        candidates = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in candidates:
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if path.is_symlink():
                raise ValueError(f"symlink in frozen artifact: {path.relative_to(bundle)}")
            if path.exists() and not path.is_dir():
                if not path.is_file():
                    raise ValueError("non-regular artifact")
                files[path.relative_to(bundle).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "task.toml" not in files or "tests/checks.json" not in files:
        raise ValueError("cannot identify an incomplete bundle")
    return files


def identity(bundle: Path) -> str:
    return hashlib.sha256(json.dumps(inventory(bundle), sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def harness_identity() -> str:
    root = Path(__file__).parent
    names = ("calibrate/agent.py", "calibrate/agent_openai.py", "calibrate/run.py",
             "provider.py", "config.py", "credential.py", "evaluate.py", "artifacts.py",
             "author/contracts.py", "author/transport.py", "author/runtime.py",
             "author/grader_runtime/grade.py", "author/grader_runtime/node_test.py",
             "author/grader_runtime/go_test.py")
    return hashlib.sha256(json.dumps({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                    for name in names}, sort_keys=True).encode()).hexdigest()


def evidence_inventory(root: Path, paths) -> dict[str, str]:
    root = root.resolve()
    result = {}
    for rel in paths:
        source = contained(root, rel)
        for file in sorted(source.rglob("*")) if source.is_dir() else [source]:
            if file.is_symlink():
                raise ValueError("evidence cannot contain symlinks")
            if file.is_file():
                result[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def policy_identity() -> str:
    """Invalidate cached acceptance whenever implementation or written rules change."""
    root = Path(__file__).resolve().parents[1]
    files = [*sorted((root / "pipeline").rglob("*.py")),
             *sorted((root / "rulesets").rglob("*.md")), root / "CLAUDE.md",
             root / "requirements-native.lock", root / 'requirements-evidence.lock',
             root / 'pyproject.toml', root / 'uv.lock', root / 'arena']
    payload = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in files if "tests" not in p.relative_to(root).parts}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def freeze(bundle: Path, destination: Path) -> str:
    """Copy just the release surfaces; reject a concurrent rewrite or overwrite."""
    before = inventory(bundle)
    destination.mkdir(parents=True, exist_ok=False)
    for rel in before:
        target = contained(destination, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(contained(bundle, rel), target)
    if inventory(destination) != before or inventory(bundle) != before:
        raise RuntimeError("bundle changed while freezing; discard this campaign")
    return identity(destination)


@contextlib.contextmanager
def bundle_lock(bundle: Path):
    """Nonblocking OS lock: survives thread/process contention; releases on crash."""
    path = Path(bundle) / "verification" / ".build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another process owns {bundle}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
