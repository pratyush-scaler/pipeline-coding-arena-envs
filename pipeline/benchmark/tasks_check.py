"""`harbor tasks-check`, which the spec requires and this workspace never ran.

Spec, "Deliverables/Requirements": "Deliver each environment in Harbor format.
Before sending it, run harbor tasks-check and run each task in the delivered
setup to confirm it works end to end."

Spec, "Batch-Level acceptance criteria", Reviewable: "100% of tasks: harbor
tasks-check pass, calibration runs + provenance included."

The pipeline used Harbor's Python API to execute trials but never invoked its
static validator, so the one check Arena names by command was the one nobody
ran. Bundles are in Harbor format and do parse -- `gates/bundle.py` checks
`schema_version`, and every native campaign loads the package -- but "our own
loader accepts it" is not the criterion Arena wrote down.

The command has been renamed twice across Harbor versions:

    harbor tasks-check <dir>     the spelling in the spec
    harbor tasks check <dir>     removed, with a message pointing at the next
    harbor check <dir>           0.22.0, 0.24.0

so this resolves the spelling rather than hard-coding one, and records which
one answered. A version that supports none of them is `unverified`, never a
pass: "could not check" is not "nothing wrong".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

#: Tried in order. The first that does not report an unknown/removed command
#: wins, and its spelling is recorded in the receipt.
SPELLINGS = (("check",), ("tasks", "check"), ("tasks-check",))

#: `harbor check` (0.22.0, 0.24.0) is NOT a static validator: it is an LLM agent trial
#: against a rubric, so it takes minutes, not seconds. 300s was set on the
#: assumption it was a lint and timed out the smallest bundle in this batch.
#: Measured: a 4KB-instruction task exceeded 280s without finishing.
TIMEOUT_SECONDS = int(os.environ.get("ARENA_TASKS_CHECK_TIMEOUT_S", "900"))

#: Harbor's default evaluator (`claude-code`) passes the trial instruction as an
#: ENVIRONMENT VARIABLE and shells it. Our packages carry dense graders -- one
#: `tests/checks.json` is 75KB -- and the exec then fails with
#:
#:     exec /usr/bin/bash: argument list too long
#:
#: which surfaces as a task defect rather than a harness limit. `swe-agent`
#: writes the instruction to a file via heredoc, so it has no such ceiling.
#: Override when a package is large enough to trip the default.
AGENT = os.environ.get("ARENA_TASKS_CHECK_AGENT") or None

RECEIPT = "verification/harbor-tasks-check.json"

#: Text that means the HARNESS failed, not the task: the evaluator could not
#: start, authenticate or exec. Such a run measured nothing about the task, so
#: it is `unverified` -- never `fail`, and never green.
_HARNESS = ("argument list too long", "cannot connect to the docker daemon",
            "is the docker daemon running", "agentauthenticationerror",
            "nonzeroagentexitcodeerror", "docker: command not found")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

#: Text Harbor emits when a spelling no longer exists. Matching this is how a
#: renamed command is told apart from a task that genuinely failed validation.
_REMOVED = ("has been removed", "no such command", "usage:", "invalid value",
            "unrecognized arguments")


def binary() -> str | None:
    """The harbor CLI, preferring the pinned native environment."""
    override = os.environ.get("ARENA_HARBOR_BIN")
    if override:
        # Absolute: Harbor runs from its own working directory, not ours.
        return str(Path(override).resolve()) if Path(override).is_file() else None
    root = Path(__file__).resolve().parents[2]
    pinned = root / ".venv-harbor/bin/harbor"
    if pinned.is_file():
        return str(pinned)
    return shutil.which("harbor")


#: Directories that are OUR measurement records, not the task Harbor validates.
#: Harbor embeds the package's complete file tree in the evaluator's prompt, and
#: that prompt reaches the agent as a single environment string. Calibration
#: writes a directory per episode under `verification/`, so a fully calibrated
#: bundle reached 32,068 files and a 3.9MB tree -- far past the 128KB a single
#: env string may hold. The exec then fails `[Errno 7] Argument list too long`,
#: which reads as a broken task rather than as our own artefacts crowding it out.
#: Measured across this batch: failing bundles median 267KB of tree, passing 113KB.
NOT_THE_TASK = ("verification", "jobs", "__pycache__")


def staged(task: Path) -> tuple[Path, str]:
    """A copy carrying only what Harbor is meant to validate, and its scratch root."""
    import shutil

    scratch = tempfile.mkdtemp(prefix="arena-taskscheck-")
    dest = Path(scratch) / task.name
    shutil.copytree(task, dest,
                    ignore=shutil.ignore_patterns(*NOT_THE_TASK, "*.pyc"))
    return dest, scratch


def workspace(task: Path) -> Path:
    """A fresh directory for Harbor's `jobs/` output, under the artifacts root.

    Harbor writes job results relative to its working directory; run from the
    repository, that put `jobs/` in the repository root.
    """
    from ..config import ARTIFACTS

    root = Path(ARTIFACTS) / "harbor-tasks-check"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{task.name}-", dir=root))


def _harness_errors(jobs: Path) -> list[str]:
    """Trials under `jobs` that ended in an exception rather than a verdict."""
    errors = []
    for path in sorted(Path(jobs).rglob("result.json")):
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        info = result.get("exception_info") if isinstance(result, dict) else None
        if info:
            message = str(info.get("exception_message") or "")
            errors.append(f"{info.get('exception_type') or 'exception'}: "
                          f"{message.strip().splitlines()[-1] if message.strip() else ''}"[:300])
    return errors


def _attempt(exe: str, words: tuple[str, ...], task: Path, work: Path) -> dict:
    try:
        argv = [exe, *words, str(task)]
        if words == ("check",):
            argv += ["--jobs-dir", str(work / "jobs")]
        if AGENT:
            argv += ["--agent", AGENT]
        proc = subprocess.run(argv, capture_output=True, cwd=work,
                              text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"spelling": " ".join(words), "status": "timeout",
                "detail": f"no result within {TIMEOUT_SECONDS}s"}
    except OSError as exc:
        return {"spelling": " ".join(words), "status": "unavailable",
                "detail": str(exc)}
    output = _ANSI.sub("", f"{proc.stdout}\n{proc.stderr}").strip()
    if proc.returncode != 0 and any(m in output.lower() for m in _REMOVED):
        return {"spelling": " ".join(words), "status": "unknown-command",
                "detail": output[:400]}
    harness = _harness_errors(work / "jobs")
    harness += [f"harness: {m}" for m in _HARNESS if m in output.lower()]
    if harness:
        # The evaluator never reached a verdict. Whatever the exit code says,
        # this measured the harness, not the task.
        return {"spelling": " ".join(words), "status": "unverified",
                "returncode": proc.returncode,
                "detail": "harbor check did not reach a verdict: " + "; ".join(harness[:3]),
                "output": output[-8000:]}
    return {"spelling": " ".join(words),
            "status": "pass" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode, "output": output[-8000:]}


def run(task: Path) -> dict:
    """Validate one task package. Writes a receipt beside the bundle."""
    from ..artifacts import atomic_json, identity

    task = Path(task).resolve()
    exe = binary()
    if exe is None:
        record = {"status": "unverified",
                  "detail": "no harbor CLI on PATH or at .venv-harbor/bin/harbor; "
                            "install the pinned native environment. Not having "
                            "run the check is not the same as passing it"}
    else:
        attempts = []
        record = None
        work = workspace(task)
        package, scratch = staged(task)
        for words in SPELLINGS:
            attempt = _attempt(exe, words, package, work)
            attempts.append(attempt)
            if attempt["status"] in ("pass", "fail", "timeout", "unverified"):
                # COPY, never alias: `record["attempts"] = attempts` below puts
                # the list back on the record, and if `record` IS one of its
                # elements the receipt refers to itself. `json.dump` then raises
                # "Circular reference detected" *after* the validator has
                # already run, so the work is done and no receipt is ever
                # written -- which is why this check read 0 of 42 despite
                # Harbor being installed and working.
                record = dict(attempt)
                break
        if record is None:
            record = {"status": "unverified",
                      "detail": "no supported spelling of the task validator; "
                                f"tried {[' '.join(w) for w in SPELLINGS]}"}
        import shutil as _shutil
        _shutil.rmtree(scratch, ignore_errors=True)
        record["checked"] = "staged copy excluding " + ", ".join(NOT_THE_TASK)
        record["attempts"] = attempts
        record["harbor_bin"] = str(exe)   # a Path is not JSON-serialisable
        record["jobs_dir"] = str(work / "jobs")

    record.update(schema="arena-pipeline/harbor-tasks-check/1",
                  task=task.name, artifact_sha256=identity(task))
    atomic_json(task / RECEIPT, record)
    return record


def retained(task: Path) -> dict | None:
    """A receipt bound to the current task version, or None."""
    from ..artifacts import identity
    from ..author.contracts import strict_json

    path = Path(task) / RECEIPT
    if not path.is_file():
        return None
    try:
        record = strict_json(path.read_text())
    except (OSError, ValueError):
        return None
    return record if record.get("artifact_sha256") == identity(Path(task)) else None


def problems(task: Path) -> list[str]:
    """Release blockers. An absent or stale receipt blocks; it does not pass."""
    record = retained(task)
    if record is None:
        return ["harbor tasks-check has not been run against this task version"]
    if record["status"] == "pass":
        return []
    if record["status"] == "fail":
        return [f"harbor tasks-check failed: {record.get('output', '')[:600]}"]
    return [f"harbor tasks-check could not be completed ({record['status']}): "
            f"{record.get('detail', '')[:300]}. SKIPPED is not green"]
