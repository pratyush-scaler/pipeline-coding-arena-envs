"""Host-owned labels for native resources; never grant cleanup by name alone."""
from __future__ import annotations
import os
import uuid
from pathlib import Path
from ..artifacts import atomic_json

ENV = "ARENA_NATIVE_OWNERSHIP"


def resource_labels(ownership: dict) -> dict[str, str]:
    return {"arena.owner": "pipeline", "arena.run": ownership["run_id"]}


def register_native_run(output: Path, snapshot: Path, steps: list[str]) -> dict:
    record = {"schema": "arena-pipeline/native-ownership/1", "run_id": uuid.uuid4().hex,
              "pid": os.getpid(), "output": str(output.resolve()),
              "snapshot": str(snapshot.resolve()), "steps": steps}
    atomic_json(output / "ownership.json", record)
    return record
