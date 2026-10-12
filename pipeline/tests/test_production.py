"""Compatibility shim: the coding synthetic bundle under the parent's helper name.

Tests written against the document pipeline imported `synthetic_bundle` from
here. The coding pipeline's synthetic task lives in `synthetic_code`; this
keeps those imports working while the bundle they get is the coding one.
"""
from __future__ import annotations

from pathlib import Path

from .synthetic_code import write_bundle


def synthetic_bundle(root: Path, two_steps: bool = False, **_ignored) -> Path:
    """Write the synthetic coding bundle under `root` and return its path."""
    return write_bundle(Path(root), two_steps=two_steps)
