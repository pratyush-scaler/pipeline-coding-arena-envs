"""Calibration rollouts against OpenAI models, on the Responses API.

The same three tools, the same `_run_tool` handlers and the same `Rollout`
record as the Anthropic agent, so a bundle is graded by identical code whichever
model attempted it. Only the transport differs: endpoint, auth, tool schema and
the stop-reason vocabulary.

Two places where the shapes genuinely differ, and both matter for validity:

* **Stop reasons.** Anthropic reports `max_tokens`; the Responses API reports
  `status: "incomplete"` with `incomplete_details.reason == "max_output_tokens"`.
  Both are truncation and both must map to the same void class, or a truncated
  GPT run is recorded as a model failure -- the defect that made kw-a008 report
  ten model failures at mean 0.0 when nothing had been submitted.

* **Reasoning items must be echoed back.** The Responses API returns `reasoning`
  items alongside `function_call` items, and dropping them between turns loses
  the model's chain across tool results. They are appended verbatim, the same
  discipline the Anthropic agent follows for thinking blocks.
"""

from __future__ import annotations

import json
import http.client
import math
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import shutil
import time
import urllib.error
import urllib.request
import uuid
from ..provider import request_slot
from pathlib import Path
from typing import Any

from .agent import SYSTEM, Rollout, _run_tool, ROLLOUT_READ_TIMEOUT_S
from ..artifacts import atomic_json

API_URL = "https://api.openai.com/v1/responses"

#: The same three affordances the Anthropic agent has, in Responses-API shape.
#: Every extra affordance is a confound: an agent that can shell out measures
#: shell fluency as much as the task.
TOOLS = [
    {"type": "function", "name": "list_files",
     "description": "List every file in the workspace, with sizes.",
     "parameters": {"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False}},
    {"type": "function", "name": "read_file",
     "description": "Read a workspace file. Large files may be read in slices.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"},
         "offset": {"type": "integer"}, "limit": {"type": "integer"},
         "char_offset": {"type": "integer"}},
         "required": ["path"], "additionalProperties": False}},
    {"type": "function", "name": "write_file",
     "description": "Write a deliverable. Paths are relative to the "
                    "deliverables directory; the source workspace is read-only.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"], "additionalProperties": False}},
]


#: A provider error code is an enum-like constant (`rate_limit_exceeded`,
#: `context_length_exceeded`). Prompt material never matches this shape, so
#: echoing a code that does cannot leak private text.
_ERROR_CODE = re.compile(r'\A[a-z][a-z0-9_.-]{2,63}\Z')


def _error_token(event) -> str:
    """The provider's structured error code, or '' when there is no safe one.

    Without a code a void is indistinguishable from a task the model cannot do,
    and the house rule is that every zero is classified as model failure or
    void before it reaches a table. Only `code`/`type` are read, never
    `message`, and only when the value has the shape of a constant.
    """
    error = event.get('error') if isinstance(event.get('error'), dict) else event
    for key in ('code', 'type'):
        value = error.get(key) if isinstance(error, dict) else None
        if isinstance(value, str) and _ERROR_CODE.match(value) and value != 'error':
            return value
    return _error_shape(error)


def _error_shape(error) -> str:
    """A last-resort descriptor when the provider sends no usable code.

    A void whose cause we cannot name is the failure mode the house rule warns
    about: it is indistinguishable from a result, and `rate_limit_exceeded`
    (re-run the episode) and `context_length_exceeded` (a real model limit)
    then arrive at the table looking identical. When the provider gives us no
    code we can still say which FIELDS it sent, which is a fact about the
    provider's envelope and not about the prompt -- key names are part of the
    wire format, so reporting them cannot leak private text the way a `message`
    would. `no-code(message)` is a poor classification but an honest one, and it
    is what tells a reader to go and look rather than to trust the silence.
    """
    if not isinstance(error, dict):
        return 'no-error-object'
    fields = sorted(k for k in error if isinstance(k, str) and _ERROR_CODE.match(k))
    return f'no-code({",".join(fields)})' if fields else 'no-code'


class _StreamInterrupted(ConnectionError):
    """No authoritative terminal response arrived; partial tools are unusable."""


#: In-stream error codes that are infra loss, not a result: retried with a long
#: backoff. A stream error carries no Retry-After, and a plain RuntimeError is
#: not retried -- so one rate-limit burst voided 13 semantic reviews at once in
#: build 2026-09-26. Quota and spend codes are never here: retrying cannot help.
TRANSIENT_STREAM_CODES = frozenset({
    'rate_limit_exceeded', 'server_error', 'server_is_overloaded',
    'overloaded_error', 'service_unavailable', 'internal_error'})


class _TransientProviderError(_StreamInterrupted):
    """A transient provider error reported inside the stream."""


def _consume_stream(resp, progress=None) -> dict:
    """Return the full terminal Responses object, preserving all output items.

    Deltas keep the connection active but never authorize a tool call. The
    completed/incomplete/failed event carries the authoritative response,
    including reasoning items, tool arguments, usage and truncation details.
    """
    data, events = [], 0
    last_progress = time.monotonic()
    for raw in resp:
        line = raw.decode('utf-8').rstrip('\r\n')
        if line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
            continue
        if line or not data:
            continue
        payload, data = '\n'.join(data), []
        if payload == '[DONE]':
            break
        try:
            event = json.loads(payload)
        except ValueError as exc:
            raise RuntimeError('OpenAI stream contained malformed event JSON') from exc
        if not isinstance(event, dict):
            raise RuntimeError('OpenAI stream event must be an object')
        kind = event.get('type')
        events += 1
        now = time.monotonic()
        if progress is not None and (events == 1 or now - last_progress >= 10):
            # Only counts and protocol state, never event text or tool arguments.
            progress('stream-active', events=events)
            last_progress = now
        if kind == 'error':
            # Never echo provider messages, which can contain prompt material.
            # The structured code is a provider constant, not prompt text, and
            # it is what separates infra loss (re-run) from a real model limit.
            token = _error_token(event)
            error = _TransientProviderError if token in TRANSIENT_STREAM_CODES else RuntimeError
            raise error('OpenAI stream reported a provider error'
                        + (f' [{token}]' if token else ''))
        if kind in {'response.completed', 'response.incomplete', 'response.failed', 'response.cancelled'}:
            response = event.get('response')
            if (not isinstance(response, dict) or response.get('status') != kind.removeprefix('response.')
                    or not isinstance(response.get('output'), list)):
                raise RuntimeError('OpenAI terminal event has an invalid response contract')
            if progress is not None:
                progress('terminal', events=events, status=response['status'])
            return response
    raise _StreamInterrupted('OpenAI stream ended without a terminal response')


_NONRETRYABLE_QUOTA_CODES = frozenset({
    'insufficient_quota', 'billing_hard_limit_reached',
    'billing_not_active', 'organization_spend_limit_exceeded',
    'project_spend_limit_exceeded', 'organization_usage_limit_exceeded',
})


def _retry_delay(headers, attempt: int) -> float:
    """Honor a server delay; otherwise use bounded exponential backoff with jitter."""
    delay = min(2 ** attempt, 32) + random.random()
    value = (headers or {}).get('Retry-After')
    if value is None:
        return delay
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return delay
    return max(delay, seconds) if math.isfinite(seconds) and seconds >= 0 else delay


def _transient_delay(attempt: int) -> float:
    """15, 30, 60, 120 s plus jitter: a rate limit is a per-minute window, and
    the ordinary 1-32 s backoff spends all five attempts inside one window."""
    return min(15 * 2 ** attempt, 120) + 5 * random.random()


def _http_failure(exc, attempt: int):
    # Error bodies can quote prompts. Read only enough to classify known quota
    # codes, and never put provider prose into logs or retained trial errors.
    try:
        payload = json.loads(exc.read(8192))
    except (ValueError, TypeError, UnicodeDecodeError):
        payload = {}
    error = payload.get('error') if isinstance(payload, dict) else None
    code = error.get('code') if isinstance(error, dict) else None
    quota = isinstance(code, str) and code in _NONRETRYABLE_QUOTA_CODES
    message = f'HTTP {exc.code}: provider request failed'
    if quota:
        message += ' (quota or spend limit; retry cannot restore access)'
    retryable = not quota and (exc.code in (408, 409, 429) or 500 <= exc.code < 600)
    return RuntimeError(message), _retry_delay(exc.headers, attempt) if retryable else None


def _transport_observer():
    """Optional payload-free telemetry; observation failures cannot change a run."""
    filename = os.environ.get('ARENA_TRANSPORT_OBSERVATIONS')
    request_id, started = uuid.uuid4().hex, time.monotonic()
    def observe(stage, **metadata):
        if not filename:
            return
        row = {'request_id': request_id, 'stage': stage,
               'at': datetime.now(timezone.utc).isoformat(),
               'elapsed_s': round(time.monotonic() - started, 3), **metadata}
        try:
            fd = os.open(filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(row) + '\n').encode())
            finally:
                os.close(fd)
        except OSError:
            pass
    return observe


def _call(body: dict, retries: int = 5) -> dict:
    from .. import openai_transport
    return openai_transport.call(body, retries)


def _stop_reason(response: dict) -> str:
    """Map the Responses vocabulary onto the one `classify_zero` reads.

    `incomplete` + `max_output_tokens` is the same event as Anthropic's
    `max_tokens`: the model was cut mid-generation and the deliverable was never
    written. It must carry the same name or it is classified as a model failure.
    """
    status = response.get("status")
    if status == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason") or ""
        return "max_tokens" if "token" in reason else (reason or "incomplete")
    return status or ""


#: OpenAI output budget, separate from the Anthropic one: the Responses API
#: counts reasoning tokens against `max_output_tokens`, so the same task needs a
#: larger cap here than on the Messages API to reach the same visible output.
#: A run cut off by this is reported `incomplete` and classified VOID, not as a
#: low score.
from .. import policy as _policy
OPENAI_MAX_TOKENS = int(os.environ.get("ARENA_OPENAI_MAX_TOKENS",
                                       str(_policy.SPEC_MAX_OUTPUT_TOKENS[_policy.SPEC_SECONDARY_MODEL])))

def rollout(instruction: str, fixture: Path, work: Path, model: str,
            rollout_id: str, max_steps: int = 40,
            max_tokens: int | None = None, effort: str | None = None,
            prior: Path | None = None,
            withhold: list[str] | None = None
            ) -> tuple[Rollout, Path]:
    """One attempt. Same contract as `agent.rollout`."""
    from .. import policy
    effort = effort or policy.SPEC_CALIBRATION_EFFORT
    started = time.monotonic()
    source = work / "workspace"
    deliverables = work / "deliverables"
    if work.exists():
        raise FileExistsError(f"rollout workspace already exists: {work}")
    # Same withholding as the Anthropic transport: a step must see the same
    # workspace whichever model is measuring it, or the two rates are not
    # comparable.
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
    items: list[dict[str, Any]] = json.loads((prior / "conversation.json").read_text()) if prior else [{"role": "system", "content": SYSTEM}]
    items.append({"role": "user", "content": instruction})
    atomic_json(work / "request-config.json", {"model": model, "effort": effort,
                "max_steps": max_steps, "max_tokens": max_tokens, "system": SYSTEM, "tools": TOOLS,
                "transport": "responses-sse-terminal-v1", "read_timeout_s": ROLLOUT_READ_TIMEOUT_S})
    atomic_json(work / "conversation.json", items)

    for step in range(max_steps):
        record.steps = step + 1
        response = _call({
            "model": model, "input": items, "tools": TOOLS,
            "reasoning": {"effort": effort},
            "max_output_tokens": max_tokens or OPENAI_MAX_TOKENS,
        })
        record.stop_reason = _stop_reason(response)
        if response.get("model") and response["model"] not in record.served_models:
            record.served_models.append(response["model"])
        for key, value in (response.get("usage") or {}).items():
            if type(value) is int:
                record.usage[key] = record.usage.get(key, 0) + value
        output = response.get("output") or []

        # Reasoning items ride along with the function calls; dropping them
        # between turns restarts the model's chain on every tool result.
        items.extend(output)
        atomic_json(work / "conversation.json", items)

        if response.get('status') != 'completed':
            break  # Retain the void receipt; never execute partial tool calls.

        calls = [o for o in output if o.get("type") == "function_call"]
        if not calls:
            break

        for call in calls:
            record.tool_calls += 1
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                out = f"error: arguments were not valid JSON: {exc}"
            else:
                try:
                    out = _run_tool(call.get("name", ""), args,
                                    source, deliverables)
                except Exception as exc:          # tool faults are the agent's
                    out = f"error: {type(exc).__name__}: {exc}"
            items.append({"type": "function_call_output",
                          "call_id": call.get("call_id"),
                          "output": out})
        atomic_json(work / "conversation.json", items)
    else:
        record.stop_reason = "step_cap"

    record.duration_s = round(time.monotonic() - started, 1)
    return record, deliverables
