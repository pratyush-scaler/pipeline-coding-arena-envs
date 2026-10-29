"""The Anthropic Messages transport the designer uses, kept byte-for-byte from
the parent pipeline's `designer.py` (streamed, deadline-aware, retried through
the shared provider budget). Nothing here knows what is being designed.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request

from ..config import ARENA_MODEL
from ..credential import auth_headers as _shared_auth_headers
from ..provider import _http_failure, _retry_delay, _transport_observer, request_slot

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ARENA_DESIGNER_MODEL", ARENA_MODEL)


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


#: Wall-clock ceiling for one model call, however slowly it is arriving. The
#: socket timeout is per-read, so a stream that trickles a byte every few
#: minutes resets it forever and the build sits at 0% CPU with an ESTABLISHED
#: connection. Measured: a-004 spent 70 minutes in one call after streaming was
#: enabled, having already been the task that wedged for 20 before it. A
#: request that cannot finish inside this is not going to.
#: **Unbounded by default** (user, 2026-09-23: "there should not be any turns or
#: time limit"). A wall-clock cap on an authoring call is an artificial limit,
#: not fault detection, and it destroys work rather than protecting anything:
#: measured on this workspace, a fresh build lost 1,578 seconds and 31 completed
#: designer responses to `retry delay exceeds the remaining call deadline` when
#: the provider slowed down. The draft was sound; only the clock had run out.
#:
#: What still protects against a genuine hang is READ_TIMEOUT_S below, which is
#: the right instrument: a socket that has delivered nothing for two minutes is
#: dead, whereas a call that is merely slow is still working. The a-004 incident
#: this deadline was added for -- 70 minutes in one call -- was a *silent*
#: connection, which the per-read timeout catches on its own.
#:
#: Set `ARENA_CALL_DEADLINE_S` to a positive number to reinstate a cap.
_deadline_env = os.environ.get("ARENA_CALL_DEADLINE_S", "").strip()
CALL_DEADLINE_S = float(_deadline_env) if _deadline_env else None

#: Per-read socket timeout. Lower than the deadline on purpose: a server that
#: has sent nothing for this long is not about to, and 300s meant a silent
#: connection burned five minutes per attempt before anything noticed.
READ_TIMEOUT_S = float(os.environ.get("ARENA_READ_TIMEOUT_S", "120"))


class _CallTimeout(TimeoutError):
    """One call exceeded its wall-clock deadline. Retryable."""


class _StreamInterrupted(ConnectionError):
    """A Messages response has no authoritative terminal event."""


def _consume_stream(resp, deadline: float | None = None, progress=None) -> dict:
    """Assemble complete Messages events; no partial response authorizes tools."""
    blocks, partial, closed, data = {}, {}, set(), []
    message, started, seen_delta, events = {}, False, False, 0
    last_progress = time.monotonic()
    for raw in resp:
        if deadline is not None and time.monotonic() > deadline:
            raise _CallTimeout('Anthropic stream exceeded its call deadline')
        try:
            line = raw.decode('utf-8').rstrip('\r\n')
        except UnicodeDecodeError:
            raise RuntimeError('Anthropic stream contained invalid UTF-8') from None
        if line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
            continue
        if line or not data:
            continue
        payload, data = '\n'.join(data), []
        try:
            event = json.loads(payload)
        except ValueError:
            raise RuntimeError('Anthropic stream contained malformed event JSON') from None
        if not isinstance(event, dict):
            raise RuntimeError('Anthropic stream event must be an object')
        kind = event.get('type')
        events += 1
        now = time.monotonic()
        if progress is not None and (events == 1 or now - last_progress >= 10):
            progress('stream-active', events=events)
            last_progress = now
        if kind == 'error':
            error = event.get('error')
            code = error.get('type') if isinstance(error, dict) else None
            if code in {'api_error', 'overloaded_error', 'rate_limit_error'}:
                raise _StreamInterrupted('Anthropic stream reported ' + code)
            raise RuntimeError('Anthropic stream reported a provider error')
        if kind == 'message_start':
            value = event.get('message')
            if started or not isinstance(value, dict) or value.get('content', []) != []:
                raise RuntimeError('Anthropic message_start violates the response contract')
            started, message = True, dict(value)
        elif kind in {'content_block_start', 'content_block_delta', 'content_block_stop'}:
            index = event.get('index')
            if not started or type(index) is not int or not 0 <= index < 4096:
                raise RuntimeError('Anthropic content block has an invalid index or message boundary')
            if kind == 'content_block_start':
                block = event.get('content_block')
                if index in blocks or not isinstance(block, dict) or not isinstance(block.get('type'), str):
                    raise RuntimeError('Anthropic content block start is invalid or duplicated')
                blocks[index], partial[index] = dict(block), []
                continue
            if index not in blocks or index in closed:
                raise RuntimeError('Anthropic delta or stop has no open content block')
            block = blocks[index]
            if kind == 'content_block_delta':
                delta = event.get('delta')
                if not isinstance(delta, dict):
                    raise RuntimeError('Anthropic block delta must be an object')
                delta_type = delta.get('type')
                if delta_type == 'input_json_delta':
                    fragment = delta.get('partial_json')
                    if block['type'] not in {'tool_use', 'server_tool_use'} or not isinstance(fragment, str):
                        raise RuntimeError('Anthropic tool input delta has an invalid contract')
                    partial[index].append(fragment)
                elif delta_type in {'text_delta', 'thinking_delta', 'signature_delta'}:
                    field = {'text_delta': 'text', 'thinking_delta': 'thinking', 'signature_delta': 'signature'}[delta_type]
                    value = delta.get(field)
                    if not isinstance(value, str) or not isinstance(block.get(field, ''), str):
                        raise RuntimeError('Anthropic text or reasoning delta must be a string')
                    block[field] = block.get(field, '') + value
                elif delta_type == 'citations_delta':
                    citation = delta.get('citation')
                    if not isinstance(citation, dict) or not isinstance(block.get('citations', []), list):
                        raise RuntimeError('Anthropic citation delta has an invalid contract')
                    block.setdefault('citations', []).append(citation)
                # Unknown event/delta types remain forward-compatible. They
                # never complete a known block or a message on their own.
            else:
                # A block can close because generation hit its output limit.
                # Only the message terminal tells us whether its JSON must be
                # complete; never execute or accept a block merely on closure.
                closed.add(index)
        elif kind == 'message_delta':
            delta, usage = event.get('delta'), event.get('usage', {})
            if not started or not isinstance(delta, dict) or not isinstance(usage, dict) or 'content' in delta:
                raise RuntimeError('Anthropic message delta has an invalid contract')
            seen_delta = True
            message.update(delta)
            if not isinstance(message.get('usage', {}), dict):
                raise RuntimeError('Anthropic usage must be an object')
            message.setdefault('usage', {}).update(usage)
        elif kind == 'message_stop':
            if (not started or not seen_delta or closed != set(blocks) or set(blocks) != set(range(len(blocks)))
                    or not isinstance(message.get('stop_reason'), str) or not message['stop_reason']):
                raise _StreamInterrupted('Anthropic terminal event arrived before the response was complete')
            content = []
            for index in range(len(blocks)):
                block = blocks[index]
                if block['type'] in {'tool_use', 'server_tool_use'}:
                    if message['stop_reason'] == 'max_tokens':
                        # Discard the entire tool batch, including completed
                        # calls preceding the cutoff. Authoring can request
                        # smaller edits; screening records its terminal limit.
                        continue
                    # A no-argument tool may send an empty delta after its
                    # initial input:{} block. Empty fragments add no input.
                    input_json = ''.join(partial[index])
                    if input_json.strip():
                        try:
                            block['input'] = json.loads(input_json)
                        except json.JSONDecodeError as exc:
                            if progress is not None:
                                reason = message['stop_reason']
                                progress('invalid-tool-input',
                                    status=reason if reason in {'tool_use', 'end_turn', 'pause_turn'} else 'unknown',
                                    characters=sum(map(len, partial[index])),
                                    line=exc.lineno, column=exc.colno)
                            raise RuntimeError('Anthropic tool input did not parse as JSON') from None
                    if (not isinstance(block.get('input'), dict)
                            or not isinstance(block.get('id'), str) or not block['id']
                            or not isinstance(block.get('name'), str) or not block['name']):
                        raise RuntimeError('Anthropic completed tool block has an invalid contract')
                content.append(block)
            message['content'] = content
            ids = [b['id'] for b in message['content'] if b['type'] in {'tool_use', 'server_tool_use'}]
            if len(ids) != len(set(ids)):
                raise RuntimeError('Anthropic response repeated a tool call identifier')
            if progress is not None:
                reason = message['stop_reason']
                status = reason if reason in {'end_turn', 'tool_use', 'max_tokens',
                    'stop_sequence', 'pause_turn', 'refusal', 'model_context_window_exceeded'} else 'unknown'
                progress('terminal', events=events, status=status)
            return message
    raise _StreamInterrupted('Anthropic stream ended without message_stop')

def _beta_header(headers: dict, betas) -> dict:
    """`headers` with the request's `betas` merged into `anthropic-beta`.

    A body key `betas` (a list of beta names) is lifted into the header here
    and never sent in the JSON body; an existing header value is kept and
    duplicates collapse.
    """
    if betas is None:
        return headers
    if not isinstance(betas, list) or not all(isinstance(b, str) and b for b in betas):
        raise ValueError("betas must be a list of non-empty strings")
    merged = [value for value in (headers.get("anthropic-beta") or "").split(",") if value]
    for beta in betas:
        if beta not in merged:
            merged.append(beta)
    return {**headers, "anthropic-beta": ",".join(merged)} if merged else headers


def _call(body: dict, retries: int = 5) -> dict:
    body = {**body, "stream": True}
    betas = body.pop("betas", None)
    data = json.dumps(body).encode()
    last: Exception | None = None
    observe = _transport_observer()
    deadline = (time.monotonic() + CALL_DEADLINE_S
                if CALL_DEADLINE_S is not None else None)

    def left() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    for attempt in range(retries):
        remaining = left()
        if remaining is not None and remaining <= 0:
            raise _CallTimeout('Anthropic call exceeded its deadline across attempts')
        req = urllib.request.Request(API_URL, data=data, method="POST", headers=_beta_header({
            "anthropic-version": "2023-06-01",
            "content-type": "application/json", **_auth_headers()}, betas))
        try:
            observe('request-start', attempt=attempt + 1)
            with request_slot():
                remaining = left()
                if remaining is not None and remaining <= 0:
                    raise _CallTimeout('Anthropic call deadline expired while awaiting admission')
                # The per-read timeout always applies: a socket silent this long
                # is dead. Only the overall cap is optional.
                socket_timeout = (READ_TIMEOUT_S if remaining is None
                                  else min(READ_TIMEOUT_S, remaining))
                with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                    observe('response-open', attempt=attempt + 1)
                    return _consume_stream(resp, deadline, observe)
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
            remaining = left()
            if remaining is not None and delay >= remaining:
                raise _CallTimeout('Anthropic retry delay exceeds the remaining call deadline') from None
            observe('retry-wait', attempt=attempt + 1, delay_s=round(delay, 3))
            time.sleep(delay)
    observe('exhausted', attempts=retries)
    raise RuntimeError(f'designer call failed after {retries} attempts: {last}') from None