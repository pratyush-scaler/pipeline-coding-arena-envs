"""Build a verifier-only copy of authoritative earlier Harbor /app captures.

This module is stdlib-only. The Harbor plugin is deliberately separate so that
packet/path validation can be tested without launching Harbor or Docker.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

SCHEMA = "arena-factory/prior-step-artifacts/1"
HISTORY_ROOT = "/arena-history"


class HistoryEvidenceError(ValueError):
    """The prior capture cannot establish what was actually collected."""


def _json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HistoryEvidenceError("Duplicate artifact manifest key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise HistoryEvidenceError("Invalid prior artifact manifest") from exc


def _path(value: str, *, absolute: bool) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise HistoryEvidenceError("Invalid artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() != absolute or ".." in path.parts or not path.parts:
        raise HistoryEvidenceError("Artifact path escapes its capture")
    return path


def _safe(root: Path, relative: PurePosixPath | str) -> Path:
    root = root.absolute()
    path = root.joinpath(*_path(str(relative), absolute=False).parts)
    if not path.is_relative_to(root):
        raise HistoryEvidenceError("History source leaves the trial")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise HistoryEvidenceError("History cannot follow symlinks")
    return path


def _regular(path: Path) -> bytes:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise HistoryEvidenceError("Expected a regular captured file")
        return path.read_bytes()
    except OSError as exc:
        raise HistoryEvidenceError("Expected capture evidence is missing or unreadable") from exc


def _declaration(value) -> dict:
    value = {"source": value} if isinstance(value, str) else value
    if not isinstance(value, dict) or value.get("service") not in {None, "main"}:
        raise HistoryEvidenceError("History only supports declared main-service artifacts")
    source = _path(value.get("source"), absolute=True)
    if len(source.parts) < 2 or source.parts[1] != "app":
        raise HistoryEvidenceError("History only supports declared /app artifacts")
    destination = value.get("destination")
    host = _path(destination, absolute=False) if destination else PurePosixPath(*source.parts[1:])
    return {"source": value["source"], "target": PurePosixPath(*source.parts[1:]),
            "destination": f"artifacts/{host.as_posix()}", "host": host}


def _copy_capture(source: Path, target: Path, root: Path, output: Path) -> list[dict]:
    """Copy regular captured bytes; no symlinks, special files or executable use."""
    entries = [source, *sorted(source.rglob("*"))] if source.is_dir() else [source]
    files = []
    for path in entries:
        _safe(root, path.relative_to(root).as_posix())
        mode = path.lstat().st_mode
        relative = path.relative_to(source) if path != source else Path()
        destination = target / relative
        if stat.S_ISDIR(mode):
            if destination.exists() and not destination.is_dir():
                raise HistoryEvidenceError("Overlapping captured artifact types")
            destination.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(mode):
            raw = _regular(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and (not destination.is_file() or destination.read_bytes() != raw):
                raise HistoryEvidenceError("Conflicting overlapping captured artifacts")
            destination.write_bytes(raw)
            if _regular(path) != raw:
                raise HistoryEvidenceError("Prior captured artifact changed while copying")
            files.append({"path": destination.relative_to(output).as_posix(), "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()})
        else:
            raise HistoryEvidenceError("History cannot contain symlinks or special files")
    return files


def build_history(task: dict, current_step: str | None, trial_root: Path, output: Path) -> dict:
    """Reconstruct only earlier declared captures; missing submissions stay missing.

    A missing/invalid manifest or a missing file advertised as captured is an
    evidence error. A native failed/skipped/empty collection stays explicit in
    the history manifest and supplies no substitute bytes.
    """
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise HistoryEvidenceError("History staging requires a fresh private directory")
    if any(path.is_symlink() for path in output.parents):
        raise HistoryEvidenceError("History staging cannot use symlinks")
    trial_root = trial_root.absolute()
    steps = task.get("steps") or []
    names = [step.get("name") for step in steps]
    if (any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in names)
            or len(set(names)) != len(names)):
        raise HistoryEvidenceError("Invalid or repeated task step")
    if current_step is None:
        if steps:
            raise HistoryEvidenceError("Multi-step verification needs its exact current step")
        earlier = []
    elif current_step not in names:
        raise HistoryEvidenceError("Verifier step is not in its pinned task")
    else:
        earlier = steps[:names.index(current_step)]
    output.mkdir(parents=True, mode=0o700)
    result = {"schema": SCHEMA, "current_step": current_step, "steps": []}
    for step in earlier:
        name = step["name"]
        captures = _safe(trial_root, f"steps/{name}/artifacts")
        manifest_path = _safe(captures, "manifest.json")
        raw_manifest = _regular(manifest_path)
        manifest = _json(raw_manifest)
        declarations = [_declaration(value) for value in [*task.get("artifacts", []), *step.get("artifacts", [])]]
        # Pinned Harbor 0.22 prepends its convention publish dir, then records
        # one collection attempt per declaration in the same main-service pass.
        if not isinstance(manifest, list) or len(manifest) != len(declarations) + 1:
            raise HistoryEvidenceError("Prior capture manifest differs from the declared artifact set")
        convention, *records = manifest
        if (not isinstance(convention, dict) or convention.get("source") != "/logs/artifacts"
                or convention.get("destination") != "artifacts/logs/artifacts"
                or convention.get("service") not in {None, "main"}):
            raise HistoryEvidenceError("Prior capture convention entry is missing or invalid")
        history = {"name": name, "capture_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(), "artifacts": []}
        for declared, record in zip(declarations, records):
            if (not isinstance(record, dict) or record.get("source") != declared["source"]
                    or record.get("destination") != declared["destination"]
                    or record.get("service") not in {None, "main"}
                    or record.get("status") not in {"ok", "failed", "empty", "skipped"}
                    or record.get("type") not in {"file", "directory"}):
                raise HistoryEvidenceError("Prior captured artifact mapping/status is inconsistent")
            captured = _safe(captures, declared["host"])
            entry = {key: record[key] for key in ("source", "destination", "type", "status")}
            entry["files"] = []
            if record["status"] == "ok":
                if not captured.exists() or (record["type"] == "directory") != captured.is_dir():
                    raise HistoryEvidenceError("Manifest advertised captured bytes that are missing or have changed type")
                target = output / name / declared["target"]
                entry["files"] = _copy_capture(captured, target, trial_root, output)
            history["artifacts"].append(entry)
        if _regular(manifest_path) != raw_manifest:
            raise HistoryEvidenceError("Prior capture manifest changed while staging history")
        result["steps"].append(history)
    (output / "manifest.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result
