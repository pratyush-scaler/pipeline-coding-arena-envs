"""A minimal file-workspace agent, for calibration only.

Not a product. Its whole job is to give a model the same interface the original
Arena sessions had -- read files, write files, list the workspace -- so that a
rollout measures the task rather than the harness around it.

Deliberately small, because every affordance here is a confound. The original
sessions used bash, web_search and more; a calibration agent that can shell out
is measuring shell fluency as much as the task, and one that can reach the
network is not measuring a no-network environment at all. Three tools, no shell,
no network.

Two failure modes this is built to keep visible rather than absorb:

* **A stall is not a failure.** Two of three low scores in one measured corpus
  were adapter stalls -- 105 `wait` actions in 182 steps, six interactions in
  193. Authoring against that would mean authoring against a harness defect. So
  a rollout that makes no tool calls, or hits the step cap without submitting,
  is recorded as `void`, never as a zero.
* **A truncated run is indistinguishable from a wrong answer** once it reaches
  the score file. The stop reason is recorded on every rollout.
"""

from __future__ import annotations

import json
import http.client
import os
import shutil
import time
import urllib.error
import urllib.request
from ..provider import request_slot, _http_failure, _retry_delay, _transport_observer
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import policy
from ..credential import auth_headers as _shared_auth_headers
from ..author.contracts import contained
from ..artifacts import atomic_json

#: Output budget for one rollout turn. A run that stops on `max_tokens` wrote no
#: deliverable and is classified VOID, so too small a budget does not produce
#: low scores -- it produces no data at all. Measured: all three Opus rollouts
#: on a-003-renovation-project-scheduling hit 32000 and voided, while GPT
#: finished the same task at 0.80. Raise it per model or per task rather than
#: reading the voids as difficulty.
ROLLOUT_MAX_TOKENS = int(os.environ.get("ARENA_ROLLOUT_MAX_TOKENS",
                                        str(policy.SPEC_MAX_OUTPUT_TOKENS[policy.SPEC_PRIMARY_MODEL])))

#: 1M context. Requires the beta header; without it the request is capped at the
#: model's standard window and a long transcript is truncated rather than
#: refused, which reads as the model losing track rather than as a limit.
#: Socket read timeout for a rollout. Deliberately generous: a 128k-token reply
#: at xhigh takes a long time to produce, and a turn cut short is recorded VOID,
#: so a tight timeout destroys data rather than saving time. 0 disables it.
_raw_timeout = float(os.environ.get("ARENA_ROLLOUT_READ_TIMEOUT_S", "3600"))
ROLLOUT_READ_TIMEOUT_S = _raw_timeout if _raw_timeout > 0 else None

LONG_CONTEXT_BETA = os.environ.get("ARENA_CONTEXT_BETA", "context-1m-2025-08-07")

API_URL = "https://api.anthropic.com/v1/messages"

TOOLS = [
    {"name": "list_files",
     "description": "List every file in the workspace, with sizes.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "read_file",
     "description": "Read a workspace file. Large files may be read in slices.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "offset": {"type": "integer", "description": "first line, 1-based"},
         "limit": {"type": "integer", "description": "how many lines"},
         "char_offset": {"type": "integer", "description": "0-based character offset; overrides line offset"}},
         "required": ["path"]}},
    {"name": "write_file",
     "description": "Write a deliverable. Paths are relative to the deliverables "
                    "directory; the source workspace is read-only.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
]

SYSTEM = """\
You are completing a professional assignment in a file workspace.

The workspace holds the source material, which is READ-ONLY. Your deliverables go
to the deliverables directory via write_file.

Work from the material. Do not invent values you cannot support from it, and do
not leave a required field out because it was hard to determine -- if something
cannot be determined from the material, the assignment will say how to report
that. When you have written every required deliverable, stop and say you are
done. You can read your existing outputs with read_file using the prefix
deliverables/. Later instructions continue the same assignment."""


@dataclass
class Rollout:
    id: str
    reward: float | None = None
    steps: int = 0
    tool_calls: int = 0
    stop_reason: str = ""
    zero_class: str | None = None       # model_failure | void
    error: str = ""
    checks: dict = field(default_factory=dict)
    #: Per failed objective, HOW it failed -- `equal_normalised` when the
    #: submission was the reference answer in another form, `overlap_<jaccard>`
    #: otherwise. A binary score cannot distinguish a wrong answer from a right
    #: one the grader could not read, and that distinction is task validity.
    misses: dict = field(default_factory=dict)
    duration_s: float = 0.0
    served_models: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def _auth_headers() -> dict:
    """Anthropic auth, whichever credential the workspace carries.

    `keys.env` may hold either an API key or an OAuth token, and they are not
    interchangeable: an `sk-ant-...` key goes on `x-api-key`, an OAuth token
    goes on `Authorization: Bearer` and 401s on `x-api-key`. Reading only one
    variable name means a credential swap looks like an outage.
    """
    # Route on the CREDENTIAL, not on which variable happened to hold it. An
    # OAuth token pasted into ANTHROPIC_API_KEY is an ordinary mistake, and
    # keying on the variable name sends it as `x-api-key`, which 401s and reads
    # like an outage rather than a typo. Discriminator adopted from the upstream
    # fork's `anthropic_credential.py`.
    return _shared_auth_headers()


def _merge_beta(headers: dict, beta: str) -> dict:
    """Add a beta flag without dropping one the credential route already set."""
    if not beta:
        return headers
    merged = dict(headers)
    existing = merged.get("anthropic-beta")
    merged["anthropic-beta"] = f"{existing},{beta}" if existing else beta
    return merged


def _call(body: dict, retries: int = 5) -> dict:
    from ..author.transport import _consume_stream
    body = {**body, "stream": True}
    data = json.dumps(body).encode()
    last: Exception | None = None
    observe = _transport_observer()
    for attempt in range(retries):
        req = urllib.request.Request(API_URL, data=data, method="POST", headers={
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            **_merge_beta(_auth_headers(), LONG_CONTEXT_BETA)})
        try:
            observe('request-start', attempt=attempt + 1)
            with request_slot(), urllib.request.urlopen(req, timeout=ROLLOUT_READ_TIMEOUT_S) as resp:
                observe('response-open', attempt=attempt + 1)
                return _consume_stream(resp, progress=observe)
        except urllib.error.HTTPError as exc:
            last, delay = _http_failure(exc, attempt)
            observe('http-error', attempt=attempt + 1, status=exc.code, retryable=delay is not None)
            if delay is None:
                raise last from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            last = RuntimeError(f'Anthropic transport interrupted ({type(exc).__name__})')
            delay = _retry_delay(None, attempt)
            observe('transport-interrupted', attempt=attempt + 1)
        if attempt + 1 < retries:
            observe('retry-wait', attempt=attempt + 1, delay_s=round(delay, 3))
            time.sleep(delay)
    observe('exhausted', attempts=retries)
    raise RuntimeError(f'model call failed after {retries} attempts: {last}') from None

def _safe(root: Path, rel: str) -> Path:
    return contained(root, rel)


def _run_tool(name: str, args: dict, source: Path, deliverables: Path) -> str:
    if name == "list_files":
        rows = []
        for path in sorted(source.rglob("*")):
            if path.is_file():
                rows.append(f"{path.relative_to(source)}  ({path.stat().st_size:,} B)")
        for path in sorted(deliverables.rglob("*")):
            if path.is_file():
                rows.append(f"deliverables/{path.relative_to(deliverables)}  ({path.stat().st_size:,} B)")
        return "\n".join(rows) or "(empty)"

    if name == "read_file":
        rel = args["path"]
        target = (_safe(deliverables, rel.removeprefix("deliverables/"))
                  if rel.startswith("deliverables/") else _safe(source, rel))
        if not target.is_file():
            return f"error: no such file: {args['path']}"
        text = target.read_text()
        lines = text.splitlines(keepends=True)
        offset = max(int(args.get("offset", 1)), 1)
        limit = max(1, min(int(args.get("limit", 2000)), 2000))
        start = max(0, int(args["char_offset"])) if "char_offset" in args else sum(map(len, lines[:offset - 1]))
        end = min(len(text), start + 48000)
        if "char_offset" not in args:
            end = min(end, start + sum(map(len, lines[offset - 1:offset - 1 + limit])))
        more = f"\n[Continues: read_file path={rel!r}, char_offset={end}; total characters={len(text)}]" if end < len(text) else ""
        return text[start:end] + more

    if name == "write_file":
        target = _safe(deliverables, args["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"])
        return f"wrote {args['path']} ({len(args['content']):,} B)"

    return f"error: unknown tool {name!r}"


def rollout(instruction: str, fixture: Path, work: Path, model: str,
            rollout_id: str, max_steps: int = 40,
            max_tokens: int | None = None, prior: Path | None = None,
            withhold: list[str] | None = None) -> tuple[Rollout, Path]:
    """One attempt. Returns the record and the deliverables directory.

    `max_tokens` was 16000, and that was measuring the budget rather than the
    task. A register covering a hundred items is ordinary professional work, but
    written as one `write_file` call after any reasoning it overruns 16k, the
    response is cut mid-argument and the deliverable is never written at all.
    Measured on kw-a008: ten rollouts from ten, every one stopping on
    `max_tokens` with an empty deliverables/ directory. Those are void trials --
    see `classify_zero` -- but a void is a measurement we did not get, so the
    budget is raised to where the deliverable fits. It is an output allowance,
    not a hint: nothing about it tells the agent what to write.
    """
    started = time.monotonic()
    source = work / "workspace"
    deliverables = work / "deliverables"
    if work.exists():
        raise FileExistsError(f"rollout workspace already exists: {work}")
    # The fixture minus whatever a later step releases. A task that withholds
    # nothing copies exactly as it always did.
    if withhold:
        # The coding contract has no per-step file withholding: every step sees
        # the same repository and differs only in its instruction.
        raise ValueError("per-step file withholding is not part of the coding contract")
    shutil.copytree(fixture, source)
    if prior is not None:
        shutil.copytree(prior / "deliverables", deliverables)
    else:
        deliverables.mkdir(parents=True)

    record = Rollout(id=rollout_id)
    messages: list[dict[str, Any]] = json.loads((prior / "conversation.json").read_text()) if prior else []
    messages.append({"role": "user", "content": instruction})
    atomic_json(work / "request-config.json", {"model": model, "effort": policy.SPEC_CALIBRATION_EFFORT,
                "max_steps": max_steps, "max_tokens": max_tokens or ROLLOUT_MAX_TOKENS,
                "system": SYSTEM, "tools": TOOLS,
                "transport": "messages-sse-terminal-v1"})
    atomic_json(work / "conversation.json", messages)

    for step in range(max_steps):
        record.steps = step + 1
        # Effort applies to the model under test as well, so a calibration
        # figure is "opus-5 at xhigh" and not "opus-5". Reports must say which.
        response = _call({"model": model, "max_tokens": max_tokens or ROLLOUT_MAX_TOKENS,
                          "output_config": {"effort": policy.SPEC_CALIBRATION_EFFORT},
                          "system": SYSTEM, "tools": TOOLS, "messages": messages})
        record.stop_reason = response.get("stop_reason", "")
        if response.get("model") and response["model"] not in record.served_models:
            record.served_models.append(response["model"])
        for key, value in (response.get("usage") or {}).items():
            if type(value) is int:
                record.usage[key] = record.usage.get(key, 0) + value
        blocks = response.get("content", [])
        messages.append({"role": "assistant", "content": blocks})
        atomic_json(work / "conversation.json", messages)

        if record.stop_reason != "tool_use":
            break  # Keep terminal/truncation evidence; never execute incomplete tools.

        calls = [b for b in blocks if b.get("type") == "tool_use"]
        if not calls:
            break

        results = []
        for call in calls:
            record.tool_calls += 1
            try:
                out = _run_tool(call["name"], call.get("input") or {},
                                source, deliverables)
            except Exception as exc:                  # tool faults are the agent's
                out = f"error: {type(exc).__name__}: {exc}"
            results.append({"type": "tool_result", "tool_use_id": call["id"],
                            "content": out})
        messages.append({"role": "user", "content": results})
        atomic_json(work / "conversation.json", messages)
    else:
        record.stop_reason = "step_cap"

    record.duration_s = round(time.monotonic() - started, 1)
    return record, deliverables
