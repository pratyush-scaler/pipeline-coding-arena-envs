"""Small filesystem and hashing helpers the benchmark runner needs.

Vendored from the upstream fork's `factory/common.py`, which is stdlib-only.
Taken rather than imported so the benchmark runner does not drag in the factory
campaign layer -- registry, planning, review staging -- none of which this
pipeline uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Status can be requested while another process finishes the same campaign.
    # Each writer needs its own temporary file before the atomic replacement.
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def contained(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Expected a relative path inside the workspace: {relative!r}")
    root = root.resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Path leaves the workspace: {relative!r}")
    return target

def files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in published packages: {path.relative_to(root)}")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
            raise ValueError(f"Only regular files and directories are allowed: {path.relative_to(root)}")
        if stat.S_ISREG(mode) and "__pycache__" not in path.parts:
            yield path

def task_paths(root: Path):
    """Published task surfaces and their parents, excluding mutable QA evidence."""
    from ..artifacts import ROOTS
    paths = set()
    for relative in ROOTS:
        target = root / relative
        if not target.exists() and not target.is_symlink():
            continue
        paths.add(target)
        if target.is_dir() and not target.is_symlink():
            paths.update(target.rglob("*"))
        parent = target.parent
        while parent != root:
            paths.add(parent)
            parent = parent.parent
    for path in sorted(paths):
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in published packages: {path.relative_to(root)}")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
            raise ValueError(f"Only regular files and directories are allowed: {path.relative_to(root)}")
        yield path


def fingerprint(root: Path, *, task_only: bool = False) -> dict:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Expected a real directory: {root}")
    selected = (p for p in task_paths(root) if p.is_file()) if task_only else files(root)
    entries = [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in selected]
    return {"sha256": digest(entries), "files": entries}

def copy_tree(source: Path, destination: Path, *, task_only: bool = False) -> None:
    # Validate before shutil follows anything. Copies never merge or overwrite.
    fingerprint(source, task_only=task_only)
    if task_only:
        destination.mkdir(parents=True, exist_ok=False)
        paths = list(task_paths(source))
        for path in paths:
            target = destination / path.relative_to(source)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        # Set read-only directory modes only after their contents are copied.
        for path in sorted((p for p in paths if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            shutil.copystat(path, destination / path.relative_to(source))
        return
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))

def redact(text: str) -> str:
    """Prevent accidentally echoed provider credentials from entering run logs."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        value = os.environ.get(name, "")
        if value:
            text = text.replace(value, "[REDACTED]")
    return text
