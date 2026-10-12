"""Durable executed control evidence; export metadata does not change execution.

Only the published task name, the provenance ledger, the authored-fixture
declaration and known author-session bookkeeping are excluded. All generated
code, instructions, patches, control declarations and runtime settings remain
bound. This is an execution cache, never a provenance or acceptance receipt.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tomllib
import uuid

from ..artifacts import atomic_json, inventory, policy_identity
from ..naming import rename_text

SCHEMA = "arena-coding/control-evidence/1"
ROOT = "verification/control-cache"
_METADATA_FILES = {"provenance/ledger.json", "authoring/extraction/fixture-authored.json"}
_AGENT_FIELDS = {"steps", "self_checked", "creation_status", "validation_status", "controls_advisories"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _bytes_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _manifest(files: dict, texts: dict) -> dict:
    # Retain original bytes and inventory so a cache cannot hide unlisted
    # differences by merely claiming a normalized execution hash.
    result = {k: v for k, v in files.items() if k not in _METADATA_FILES}
    for rel in ("task.toml", "authoring/spec.json"):
        text = texts[rel]
        if _bytes_digest(text) != files[rel]:
            raise ValueError(f"control input evidence differs: {rel}")
    text = texts["task.toml"]
    before = tomllib.loads(text)
    normalized = rename_text(text, "arena/control-equivalence")
    expected = {**before, "task": {**before["task"], "name": "arena/control-equivalence"}}
    if tomllib.loads(normalized) != expected:
        raise ValueError("normalizing task name changed another setting")
    result["task.toml"] = _bytes_digest(normalized)
    spec = json.loads(texts["authoring/spec.json"])
    agent = spec.get("_agent")
    if isinstance(agent, dict) and set(agent) <= _AGENT_FIELDS:
        spec.pop("_agent")
    result["authoring/spec.json"] = digest(spec)
    return result


def snapshot(bundle: Path, runs: int) -> dict:
    files = inventory(bundle)
    texts = {rel: (bundle / rel).read_bytes().decode("utf-8")
             for rel in ("task.toml", "authoring/spec.json")}
    manifest = _manifest(files, texts)
    if inventory(bundle) != files:
        raise ValueError("bundle changed while binding control inputs")
    return {"artifact_sha256": _artifact_digest(files), "files": files, "texts": texts,
            "manifest": manifest, "policy_sha256": policy_identity(), "determinism_runs": runs}


def _artifact_digest(files: dict) -> str:
    # Must use artifacts.identity's exact encoding (including ensure_ascii).
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def key(inputs: dict) -> str:
    return digest({k: inputs[k] for k in ("manifest", "policy_sha256", "determinism_runs")})


def binding(inputs: dict) -> dict:
    return {"execution_sha256": key(inputs), "policy_sha256": inputs["policy_sha256"],
            "determinism_runs": inputs["determinism_runs"]}


def retain(root: Path, inputs: dict, receipt: dict, records: dict) -> tuple[Path, str]:
    """Full trials and their actual source receipt survive scratch pruning.

    Failed controls are also retained, but are never reusable as a success.
    """
    evidence = {"schema": SCHEMA, "inputs": inputs, "receipt": receipt, "records": records}
    sha = digest(evidence)
    path = Path(root) / key(inputs) / f"{sha}.json"
    if path.exists():
        try:
            intact = digest(json.loads(path.read_text())) == sha
        except (OSError, ValueError, TypeError):
            intact = False
        if not intact:
            # Keep damaged bytes for diagnosis, then restore only freshly
            # executed (or independently verified) content under this hash.
            path.rename(path.with_name(path.name + ".corrupt-" + uuid.uuid4().hex))
    if not path.exists():
        atomic_json(path, evidence)
    return path, sha


def read(path: Path, inputs: dict, labels, steps: list, checks: dict) -> dict:
    """Reject partial, stale, tampered or summary-only observations."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("control evidence must be a regular file")
    evidence = json.loads(path.read_text())
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA or path.stem != digest(evidence):
        raise ValueError("control evidence digest/schema differs")
    old = evidence["inputs"]
    if not isinstance(old, dict):
        raise ValueError("invalid control input binding")
    if (_artifact_digest(old["files"]) != old["artifact_sha256"]
            or _manifest(old["files"], old["texts"]) != old["manifest"]
            or key(old) != key(inputs)):
        raise ValueError("control execution inputs or policy differ")
    receipt, records = evidence["receipt"], evidence["records"]
    if not isinstance(receipt, dict) or not isinstance(records, dict):
        raise ValueError("invalid control evidence shape")
    if (receipt.get("schema") != "arena-coding/controls/1"
            or receipt.get("artifact_sha256") != old["artifact_sha256"]
            or receipt.get("input_binding") != binding(old)
            or receipt.get("steps") != steps
            or receipt.get("ok") is not True or receipt.get("errors") != []
            or receipt.get("execution", {}).get("reused") is not False
            or receipt.get("execution", {}).get("executed_new_trials") != len(records)
            or receipt.get("policy", {}).get("determinism_runs") != old["determinism_runs"]
            or set(records) != set(labels)):
        raise ValueError("no complete successful executed controls receipt")
    for record in records.values():
        if (not isinstance(record, dict) or record.get("schema") != "arena-coding/sandbox-trial/1"
                or record.get("valid") is not True or list(record.get("steps", {})) != steps
                or record.get("agent_image") != receipt["images"]["agent"]
                or record.get("verifier_image") != receipt["images"]["verifier"]
                or not _reward(record.get("reward"))):
            raise ValueError("incomplete control trial")
        for step, entry in record["steps"].items():
            if not isinstance(entry, dict):
                raise ValueError("invalid checkpoint evidence")
            grade = entry.get("grade") or {}
            if not isinstance(grade, dict):
                raise ValueError("invalid grader evidence")
            flags = grade.get("checks") or {}
            if (not isinstance(flags, dict) or entry.get("valid") is not True or entry.get("error") or entry.get("timed_out")
                    or set(flags) != set(checks[step])
                    or not _reward(entry.get("reward"))
                    or any(not isinstance(item, dict) or type(item.get("passed")) is not bool or item.get("timed_out")
                           for item in flags.values())):
                raise ValueError("invalid or incomplete per-check control evidence")
    return evidence


def _reward(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def candidates(root: Path, inputs: dict):
    return sorted((Path(root) / key(inputs)).glob("*.json"))


def changed_metadata(old: dict, current: dict) -> list[str]:
    return sorted(rel for rel in old["files"].keys() | current["files"].keys()
                  if old["files"].get(rel) != current["files"].get(rel))
