"""Capture each step's agent workspace immediately before the agent starts and after it stops.

Arena delivery format v0.1 §12.4 requires, for every graded step, a `before` inventory captured after
step setup and immediately before the agent starts, and an `after` inventory captured after the agent
stops and before the verifier runs, both complete over the declared scope (no implicit exclusions) and
carrying every regular file's exact bytes. Harbor collects only the post-agent `artifacts`, with their
`exclude` lists, so this process-local hook wraps `Trial._run_agent_phase` in the same container:

    trial_dir/arena-capture/<step>/<phase>.tar    the scope as one tar (modes, links as links)
    trial_dir/arena-capture/<step>/<phase>.json   when, what scope, what was present, exit status

The packager turns the two tars into `changes.json` / `changes.patch`. A capture that fails is recorded,
never raised: the agent run itself is unaffected, and the run simply has no usable workspace evidence.

Runs in the native Harbor interpreter: stdlib and Harbor only.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "arena-pipeline/workspace-capture/1"
PHASES = ("before-agent", "after-agent-before-verifier")
FOLDER = "arena-capture"
REMOTE = "/tmp/.arena-workspace-capture.tar"
TIMEOUT_SEC = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(path) -> str | None:
    if not isinstance(path, str) or not path.startswith("/") or "\\" in path or "\0" in path:
        return None
    return posixpath.normpath(path)


def scope_of(metadata: dict | None) -> list[str]:
    """Sorted, unique, non-overlapping absolute paths covering the workspace, deliverables and protected paths."""
    metadata = metadata or {}
    wanted = [metadata.get("agent_workspace")]
    for key in ("deliverables", "protected_paths"):
        value = metadata.get(key) or []
        wanted.extend(value if isinstance(value, list) else [value])
    paths = sorted({p for p in (_canonical(w) for w in wanted) if p})
    out: list[str] = []
    for p in paths:
        if not any(p == q or p.startswith(q.rstrip("/") + "/") for q in out):
            out.append(p)
    return out


def _metadata(trial) -> dict:
    config = getattr(getattr(trial, "task", None), "config", None)
    meta = getattr(config, "metadata", None)
    return dict(meta) if isinstance(meta, dict) else {}


def _command(scope: list[str]) -> str:
    quoted = " ".join("'" + p.replace("'", "'\\''") + "'" for p in scope)
    return ("set -e; rm -f " + REMOTE + "; present=''; for p in " + quoted + "; do "
            "if [ -e \"$p\" ] || [ -L \"$p\" ]; then present=\"$present ${p#/}\"; echo \"$p\"; fi; done; "
            "if [ -n \"$present\" ]; then tar --numeric-owner -cpf " + REMOTE + " -C / $present; "
            "else tar -cpf " + REMOTE + " --files-from /dev/null; fi")


async def capture(trial, step: str | None, phase: str) -> dict:
    """Tar the scope as root inside the agent container and bring it into the trial directory."""
    folder = Path(trial.paths.trial_dir) / FOLDER / (step or "_task")
    folder.mkdir(parents=True, exist_ok=True)
    scope = scope_of(_metadata(trial))
    record = {"schema": SCHEMA, "step": step, "phase": phase, "scope": scope, "present": [],
              "tar": f"{phase}.tar", "ok": False}
    env = trial.agent_environment
    try:
        if not scope:
            raise ValueError("task metadata names no agent_workspace, deliverables or protected_paths")
        result = await env.exec(_command(scope), timeout_sec=TIMEOUT_SEC, user="root")
        record["captured_at"] = _now()
        record["return_code"] = result.return_code
        record["present"] = [line for line in (result.stdout or "").splitlines() if line.startswith("/")]
        if result.return_code != 0:
            record["stderr"] = (result.stderr or "")[-2000:]
            raise RuntimeError(f"capture command exited {result.return_code}")
        target = folder / record["tar"]
        await env.download_file(REMOTE, target)
        data = target.read_bytes()
        record.update(ok=True, bytes=len(data), tar_sha256=hashlib.sha256(data).hexdigest())
    except Exception as exc:  # recorded, never raised: the run proceeds without workspace evidence
        record["error"] = f"{type(exc).__name__}: {exc}"[:2000]
    finally:
        try:
            await env.exec("rm -f " + REMOTE, timeout_sec=60, user="root")
        except Exception as exc:
            record.setdefault("error", f"cleanup failed: {exc}"[:500])
    (folder / f"{phase}.json").write_text(json.dumps(record, indent=1) + "\n")
    return record


def install() -> None:
    """Wrap Harbor's per-step agent phase with a before/after workspace capture (idempotent)."""
    from harbor.trial.trial import Trial

    original = Trial._run_agent_phase
    if getattr(original, "_arena_workspace_capture", False):
        return

    async def wrapped(self, *args, **kwargs):
        step_cfg = kwargs.get("step_cfg")
        step = getattr(step_cfg, "name", None)
        await capture(self, step, PHASES[0])
        try:
            return await original(self, *args, **kwargs)
        finally:
            await capture(self, step, PHASES[1])

    wrapped._arena_workspace_capture = True
    Trial._run_agent_phase = wrapped
