"""The independent-solver control: a different model fixes the task from the
instruction and the repository alone, and its fix is graded by the shipped tests.

Why this exists (2026-09-29). Three independent reviews of one frozen bundle
agreed on 17 of 18 check verdicts: the reviewer is consistent, and what it
rejects is real -- hidden tests shaped by the designer's own reference and
its own doubles, which a differently written correct fix fails. The designer
cannot see that from inside its own head, and its repairs remove the named
objection while keeping the shape. An executed second implementation makes
the shape visible: every check the independent fix fails is either a TRAP
(the solver adopted the misreading the task is built on -- that belongs in
`quality.difficulty_case`) or an OVER-FIT TEST (the solver's fix is correct
under the instruction and the test asserts the reference's convention). The
designer decides which, with the diff in hand; the reviewer later reads the
same record as evidence. Advisory throughout: nothing here blocks a draft.

The solver sees each step's instruction in sequence and `/app/repo` inside
one persistent agent container, with a shell and no network. This bounded
advisory probe differs from the unlimited native calibration harness; its
limits and cutoffs are recorded. It never sees tests, solution, traps or spec.
"""
from __future__ import annotations

import json
import shlex
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

from . import sandbox
from ..config import VENDORED_PARTS

SOLVER_SYSTEM = (
    "You are a careful senior engineer fixing a real repository. The repository is at /app/repo inside a "
    "sandbox with the project's toolchain installed and no network. Read the code, reproduce the problem, "
    "change the source so the described behaviour is correct everywhere it applies, and check your work by "
    "running it. Do not add dependencies. Do not rename or remove public modules or functions unless the "
    "instruction asks. When the fix is complete, reply with a short summary and stop calling tools.")

TOOL_SPECS = [
    {"name": "list_files", "description": "List files under a directory of the repository (relative to /app/repo).",
     "parameters": {"type": "object", "properties": {"dir": {"type": "string"}}, "required": []}},
    {"name": "read_file", "description": "Read a file of the repository (relative to /app/repo).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file of the repository (relative to /app/repo), creating it if needed.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"name": "run", "description": "Run a shell command in /app/repo (bash, no network, 300 s limit). Returns exit code, stdout and stderr.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]

MAX_TOOL_OUTPUT = 12000
EXCLUDES = tuple(sorted(VENDORED_PARTS))


def _checked_exec(container, command: str, **kwargs) -> dict:
    """A failed capture/replay operation is infrastructure failure, not model failure."""
    result = container.exec(command, **kwargs)
    if result.get("timed_out") or result.get("exit_code") != 0:
        raise sandbox.SandboxError("blind-solve capture/replay command failed: "
                                   + (result.get("stderr") or "no successful exit status")[-1500:])
    return result


class SandboxTools:
    """The four tools over one live agent container."""

    def __init__(self, container):
        self.container = container
        self.calls = 0
        # The pristine tree, diffed against at the end.
        _checked_exec(self.container, "cp -a /app/repo /tmp/pristine", user="agent", timeout=300, workdir="/app/repo")
        _checked_exec(self.container, "cp -a /app/repo /tmp/blind-original", user="agent", timeout=300, workdir="/app/repo")

    def _guest(self, rel: str) -> str:
        rel = (rel or ".").strip()
        if rel.startswith("/") or ".." in rel.split("/"):
            raise ValueError("paths are relative to /app/repo and may not escape it")
        return f"/app/repo/{rel}".rstrip("/")

    def call(self, name: str, args: dict) -> str:
        self.calls += 1
        if name == "list_files":
            guest = self._guest(args.get("dir") or ".")
            excl = " ".join(f"-not -path '*/{e}/*'" for e in EXCLUDES)
            out = self.container.exec(f"cd {shlex.quote(guest)} && find . -maxdepth 3 {excl} -not -name '*.pyc' | sort | head -400",
                                      user="agent", timeout=60)
            return (out["stdout"] or "")[:MAX_TOOL_OUTPUT] or "(empty)"
        if name == "read_file":
            out = self.container.exec(f"cat {shlex.quote(self._guest(args.get('path', '')))}", user="agent", timeout=60)
            if out["exit_code"]:
                return "error: " + (out["stderr"] or "")[-2000:]
            return (out["stdout"] or "")[:MAX_TOOL_OUTPUT]
        if name == "write_file":
            guest = self._guest(args.get("path", ""))
            _checked_exec(self.container, f"mkdir -p {shlex.quote(str(Path(guest).parent))}", user="agent", timeout=60)
            self.container.put_bytes(str(args.get("content", "")).encode("utf-8"), guest)
            return f"wrote {guest}"
        if name == "run":
            out = self.container.exec(str(args.get("command", "")), user="agent", timeout=300, workdir="/app/repo")
            return json.dumps({"exit_code": out["exit_code"], "timed_out": out.get("timed_out"),
                               "stdout": (out["stdout"] or "")[-MAX_TOOL_OUTPUT:],
                               "stderr": (out["stderr"] or "")[-4000:]})
        return f"error: no such tool {name!r}"

    def checkpoint(self) -> None:
        _checked_exec(self.container, "rm -rf /tmp/pristine && cp -a /app/repo /tmp/pristine",
                      user="agent", timeout=300, workdir="/app/repo")

    def diff(self, host_dir: Path, *, cumulative: bool = False) -> str:
        """The unified diff of the solver's changes, retrieved as a FILE.

        `Container.exec` keeps only the last 20,000 characters of stdout; the
        first two real solves (2026-09-29 03:19) came back as patches that began
        mid-hunk, applied partially without error, and produced failing checks
        that were artefacts. The diff is written inside the container and
        captured whole; a patch that does not start like one is refused.
        """
        excl = " ".join(f"--exclude={e}" for e in EXCLUDES) + " --exclude='*.pyc'"
        baseline = "/tmp/blind-original" if cumulative else "/tmp/pristine"
        _checked_exec(self.container,
            f"rm -rf /tmp/d && mkdir -p /tmp/d && cp -a {baseline} /tmp/d/a && cp -a /app/repo /tmp/d/b && "
            f"cd /tmp/d && (diff -ruN {excl} a b > /tmp/d/blind.patch; diff_status=$?; "
            'test "$diff_status" -le 1)', user="agent", timeout=300)
        host_dir = Path(host_dir)
        self.container.capture("/tmp/d", ["blind.patch"], [], host_dir)
        patch = (host_dir / "blind.patch").read_text(encoding="utf-8", errors="replace") if (host_dir / "blind.patch").is_file() else ""
        if not looks_like_unified_diff(patch):
            raise ValueError("the captured diff does not start like a unified diff; refusing to grade a partial patch")
        return patch


#: Lines `diff -ruN` writes before the first hunk when the solver touched a
#: binary or added a file the exclusions hide: they are not a sign of a patch
#: captured mid-hunk. 2026-10-26: a docx-generator task's solvers regenerated
#: the .docx and .png outputs, every capture began "Binary files a/... differ",
#: and both families' first solves were refused and their budget spent.
_DIFF_PREAMBLE = ("Binary files ", "Only in ", "File ")


def looks_like_unified_diff(patch: str) -> bool:
    """An empty capture, or one whose first real line opens a unified diff."""
    for line in patch.splitlines():
        if not line.strip() or line.startswith(_DIFF_PREAMBLE):
            continue
        return line.startswith(("diff ", "--- "))
    return True


class CapturedVariant(sandbox.Variant):
    """Replay a repository delta and the step's captured deliverable state."""
    def __init__(self, patch: str, deliverables: dict[str, bytes]):
        super().__init__("patch" if patch.strip() else "nop", patch=patch, label="blind")
        self.deliverables = deliverables

    def apply(self, container, step):
        result = super().apply(container, step)
        _checked_exec(container, "find /app/deliverables -mindepth 1 -maxdepth 1 -exec rm -rf {} +",
                      user="agent", timeout=300)
        for rel, data in self.deliverables.items():
            guest = f"/app/deliverables/{rel}"
            _checked_exec(container, f"mkdir -p {shlex.quote(str(Path(guest).parent))}", user="agent", timeout=60)
            container.put_bytes(data, guest)
        # The agent container drops every capability, so root cannot chown on a
        # Linux host: every blind solve on Coder came back ungraded (2026-10-03,
        # 157 solves). Ownership is best effort, as for `files` variants; what
        # grading needs is that the agent can read every replayed file.
        container.exec("chown -R agent:agent /app/deliverables 2>/dev/null || true", user="root", timeout=60)
        _checked_exec(container, "test -z \"$(find /app/deliverables ! -readable -print -quit)\"",
                      user="agent", timeout=60)
        return {**result, "deliverable_files": len(self.deliverables)}


def _capture_deliverables(container, destination: Path) -> dict[str, bytes]:
    container.capture("/app", ["deliverables"], [], destination)
    folder = destination / "deliverables"
    if folder.is_symlink():
        raise sandbox.SandboxError("the solver's deliverable directory is a symlink; cannot replay it faithfully")
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise sandbox.SandboxError("a solver deliverable is a symlink; the probe cannot replay it faithfully")
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def _openai_loop(instruction: str, model: str, tools: SandboxTools, max_steps: int, effort: str,
                 call: Callable) -> dict:
    items: list[dict[str, Any]] = [{"role": "system", "content": SOLVER_SYSTEM}, {"role": "user", "content": instruction}]
    specs = [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["parameters"]}
             for t in TOOL_SPECS]
    usage: dict[str, int] = {}
    steps = 0
    stop_reason = "max_steps"
    for steps in range(1, max_steps + 1):
        response = call({"model": model, "input": items, "tools": specs, "reasoning": {"effort": effort},
                         "max_output_tokens": 16000})
        for key, value in (response.get("usage") or {}).items():
            if type(value) is int:
                usage[key] = usage.get(key, 0) + value
        output = response.get("output") or []
        items.extend(output)
        calls = [o for o in output if o.get("type") == "function_call"]
        if response.get("status") not in (None, "completed"):
            stop_reason = "provider_incomplete"
            break
        if not calls:
            stop_reason = "completed"
            break
        for c in calls:
            try:
                args = json.loads(c.get("arguments") or "{}")
                result = tools.call(c.get("name", ""), args)
            except Exception as exc:  # noqa: BLE001  tool faults are the solver's to handle
                result = f"error: {type(exc).__name__}: {exc}"
            items.append({"type": "function_call_output", "call_id": c.get("call_id"), "output": result})
    return {"steps": steps, "usage": usage, "conversation": items,
            "stop_reason": stop_reason, "complete": stop_reason == "completed"}


# One response may write a whole file (a single-file HTML app is often 60-70 KB); at 16000 output
# tokens Fable's write was cut off and the solve ended incomplete (c-006-html-canvas-3d-viewer and
# c-018-markdown-to-docx-cli, 2026-10-18). The transport streams, so the larger cap costs nothing
# unless used.
ANTHROPIC_MAX_TOKENS = 64000


def _anthropic_loop(instruction: str, model: str, tools: SandboxTools, max_steps: int, effort: str,
                    call: Callable) -> dict:
    from .prompt_cache import with_cache_breakpoints
    messages: list[dict[str, Any]] = [{"role": "user", "content": instruction}]
    specs = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOL_SPECS]
    usage: dict[str, int] = {}
    steps = 0
    stop_reason = "max_steps"
    for steps in range(1, max_steps + 1):
        response = call(with_cache_breakpoints({"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS, "system": SOLVER_SYSTEM,
                                                "output_config": {"effort": effort}, "tools": specs,
                                                "messages": messages}))
        for key, value in (response.get("usage") or {}).items():
            if type(value) is int:
                usage[key] = usage.get(key, 0) + value
        blocks = response.get("content") or []
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        messages.append({"role": "assistant", "content": blocks})
        if response.get("stop_reason") in ("max_tokens", "refusal", "pause_turn"):
            stop_reason = "provider_incomplete"
            break
        if not calls:
            stop_reason = "completed"
            break
        results = []
        for c in calls:
            try:
                result = tools.call(c.get("name", ""), c.get("input") or {})
            except Exception as exc:  # noqa: BLE001
                result = f"error: {type(exc).__name__}: {exc}"
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": result})
        messages.append({"role": "user", "content": results})
    return {"steps": steps, "usage": usage, "conversation": messages,
            "stop_reason": stop_reason, "complete": stop_reason == "completed"}


def solver_model_for(producer: str) -> str:
    """A different vendor from the author, so the second implementation is independent."""
    import os
    forced = os.environ.get("ARENA_BLIND_SOLVER_MODEL")
    if forced:
        return forced
    return "claude-fable-5-1" if producer.split("/")[-1].startswith("gpt-") else "gpt-6-astra"


def solve(package: Path, model: str, work: Path, *, max_steps: int = 60, effort: str = "high",
          call: Callable | None = None, container_factory: Callable | None = None,
          grade: bool = True) -> dict:
    """Solve every step in order and grade incremental changes at each checkpoint.

    `package` is a complete export (task.toml, environment/, steps/, tests/,
    solution/). Returns the diff, the per-check results of the shipped grader
    over that diff, and the solver's usage. `call` and `container_factory`
    are injectable for tests; production uses the real transports and the
    agent image built from the package.
    """
    package, work = Path(package), Path(work)
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps must be a positive per-step turn budget")
    work.mkdir(parents=True, exist_ok=True)
    steps = sandbox._steps(package)
    if not steps:
        raise ValueError("the package has no steps")
    started = time.monotonic()
    if container_factory is None:
        tag = sandbox.build_image(package, "agent")
        chain = sandbox._toolchain(package)
        limits = chain.get("limits", {})

        def container_factory():
            return sandbox.Container(tag, user="agent", workdir="/app/repo",
                                     memory=f"{limits.get('memory_mb', 4096)}m", cpus=str(limits.get("cpus", 2)))
    if call is None:
        if model.startswith(("gpt-", "o1", "o3", "o4")):
            from ..calibrate.agent_openai import _call as call
        else:
            from .transport import _call as call
    loop = _openai_loop if model.startswith(("gpt-", "o1", "o3", "o4")) else _anthropic_loop
    sessions, variants, usage, by_step = {}, {}, {}, {}
    with container_factory() as container:
        tools = SandboxTools(container)
        for step in steps:
            instruction = (package / "steps" / step / "instruction.md").read_text()
            calls_before = tools.calls
            session = loop(instruction, model, tools, max_steps, effort, call)
            sessions[step] = session
            step_work = work / "steps" / step
            patch = tools.diff(step_work)
            deliverables = _capture_deliverables(container, step_work / "artifacts")
            variants[step] = CapturedVariant(patch, deliverables)
            by_step[step] = {"steps": session["steps"], "tool_calls": tools.calls - calls_before,
                             "stop_reason": session["stop_reason"], "complete": session["complete"],
                             "patch_bytes": len(patch.encode()), "deliverable_files": len(deliverables),
                             "reward": None, "checks": {}, "failing": {}}
            for key, value in session["usage"].items():
                usage[key] = usage.get(key, 0) + value
            step_work.mkdir(parents=True, exist_ok=True)
            (step_work / "conversation.json").write_text(json.dumps(session["conversation"], ensure_ascii=False, indent=1))
            tools.checkpoint()
        patch = tools.diff(work, cumulative=True)
    (work / "conversation.json").write_text(json.dumps(
        {s: rec["conversation"] for s, rec in sessions.items()}, ensure_ascii=False, indent=1))
    complete = all(r["complete"] for r in by_step.values())
    record = {"schema": "arena-coding/blind-solve/2", "model": model, "step": steps[0],
              "step_names": steps, "steps": sum(r["steps"] for r in by_step.values()),
              "complete": complete, "advisory_only": True, "by_step": by_step,
              "limits": {"max_turns_per_step": max_steps,
                         "max_output_tokens": 16000 if model.startswith("gpt-") else ANTHROPIC_MAX_TOKENS,
                         "shell_timeout_s": 300, "effort": effort, "native_harness": False},
              "tool_calls": tools.calls, "usage": usage, "patch_bytes": len(patch.encode()),
              "seconds": round(time.monotonic() - started, 1), "reward": None, "checks": {}, "failing": {}}
    if not complete:
        record["note"] = "bounded probe incomplete; diagnostic grades cannot establish a model capability failure"
    if not patch.strip() and not any(r["deliverable_files"] for r in by_step.values()):
        record["note"] = (record.get("note", "") + "; the solver changed no captured source or deliverable files").lstrip("; ")
    if not grade:
        record["grading_deferred"] = True
        return record
    try:
        graded = sandbox.verify(package, variants, work=work / "verify", keep_capture=False)
    except sandbox.SandboxError as exc:
        record["error"] = f"the solver's patch could not be graded: {exc}"[:2000]
        return record
    grading_valid = graded.get("valid") is True
    for step in steps:
        entry = (graded.get("steps") or {}).get(step) or {}
        grade = entry.get("grade") or {}
        row = by_step[step]
        row["reward"] = entry.get("reward")
        row["checks"] = {cid: bool(r.get("passed")) for cid, r in (grade.get("checks") or {}).items()}
        row["failing"] = {cid: {"reason": r.get("reason"), "stderr": (r.get("stderr") or "")[-1200:]}
                          for cid, r in (grade.get("checks") or {}).items() if not r.get("passed")}
        row["grading_valid"] = entry.get("valid", grading_valid) is True and row["reward"] is not None
        grading_valid &= row["grading_valid"]
        for field in ("checks", "failing"):
            record[field].update({(cid if len(steps) == 1 else f"{step}/{cid}"): value
                                  for cid, value in row[field].items()})
    record["grading_valid"] = grading_valid
    if grading_valid:
        from ..metrics import combine
        strategy = tomllib.loads((package / "task.toml").read_text()).get("multi_step_reward_strategy", "mean")
        record["diagnostic_reward"] = combine([r["reward"] for r in by_step.values()], strategy)
        record["reward"] = record["diagnostic_reward"] if complete else None
    else:
        record["error"] = f"invalid grading evidence: {graded.get('errors', [])}"[:2000]
    record["seconds"] = round(time.monotonic() - started, 1)
    return record
