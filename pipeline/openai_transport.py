"""File-authoritative OpenAI credentials and conservative, shared failover.

Keys with unknown quota ownership share one durable cooldown. Switching keys
never shortens Retry-After or a known shared delay. This module contains no
model/agent logic; partial streamed tools remain the caller's responsibility.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ALIAS = re.compile(r"OPEN_AI_API_KEY([1-9][0-9]*)\Z")
MAX_ATTEMPTS = 5
#: 2026-10-03: four Coder workspaces reviewing at once against shared OpenAI
#: quota exhausted a 300 s budget and left a valid task `unverified`. A
#: rate-limited review should wait rather than give up; the request budget,
#: not the clock, bounds spend. Override with ARENA_OPENAI_MAX_WAIT_S.
MAX_RETRY_WAIT_S = float(os.environ.get("ARENA_OPENAI_MAX_WAIT_S", "1800"))
WAIT_CHUNK_S = 60.0


def read_key_file(path: Path) -> dict[str, str]:
    values = {}
    if Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            name, _, value = line.partition('=')
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def key_names(values: dict[str, str]) -> list[str]:
    names = sorted((name for name, value in values.items() if value and ALIAS.fullmatch(name)),
                   key=lambda name: int(ALIAS.fullmatch(name).group(1)))
    if values.get('OPENAI_API_KEY'):
        names.insert(0, 'OPENAI_API_KEY')
    return names


def resolve_primary(values: dict[str, str]) -> str | None:
    names = key_names(values)
    return values[names[0]] if names else None


def apply_file_credentials(values, environment=None):
    """Replace inherited authentication from this file; never rewrite the file."""
    environment = os.environ if environment is None else environment
    resolved = dict(values)
    primary = resolve_primary(values)
    if primary:
        resolved['OPENAI_API_KEY'] = primary
    for name in list(environment):
        if name in ('OPENAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY') or ALIAS.fullmatch(name):
            if not resolved.get(name):
                environment.pop(name, None)
    for name, value in resolved.items():
        if value:
            environment[name] = value
    return resolved


def retry_after_minimum(headers):
    """A large valid minimum is refused by the wait budget, never truncated."""
    value = (headers or {}).get('Retry-After') or (headers or {}).get('retry-after')
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError, AttributeError):
            return 0.0
    return seconds if math.isfinite(seconds) and seconds >= 0 else 0.0


def route_headers(headers):
    headers = headers or {}
    return {request: headers.get(response) for request, response in (
        ('OpenAI-Organization', 'openai-organization'), ('OpenAI-Project', 'openai-project'))
        if headers.get(response)}


@dataclass(frozen=True)
class Credential:
    name: str
    value: str = field(repr=False)
    organization: str | None = field(default=None, repr=False)
    project: str | None = field(default=None, repr=False)

    def headers(self):
        result = {'Authorization': 'Bearer ' + self.value, 'Content-Type': 'application/json'}
        if self.organization:
            result['OpenAI-Organization'] = self.organization
        if self.project:
            result['OpenAI-Project'] = self.project
        return result


def credentials(path: Path) -> list[Credential]:
    values = read_key_file(path)
    found, seen = [], set()
    for name in key_names(values):
        value = values[name]
        if value in seen:
            continue
        seen.add(value)
        suffix = ALIAS.fullmatch(name).group(1) if ALIAS.fullmatch(name) else ''
        found.append(Credential(name, value,
            values.get('OPENAI_ORG_ID' + suffix) or values.get('OPENAI_ORG_ID'),
            values.get('OPENAI_PROJECT_ID' + suffix) or values.get('OPENAI_PROJECT_ID')))
    if not found:
        raise RuntimeError('No OpenAI credential is set in the authoritative keys.env')
    return found


class SharedCooldown:
    """One conservative rate-limit scope across processes and configured keys.

    Organization/project identity is unknown for these keys. Even when a
    response identifies one scope, unverified other keys remain in this group.
    State contains a delay and a numeric cursor, never credentials or payloads.
    """
    def __init__(self, directory: Path, clock=None):
        self.directory = Path(directory)
        self.clock = clock or time.time

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / 'openai.lock'
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state_path = self.directory / 'openai.json'
            state = json.loads(state_path.read_text()) if state_path.is_file() else {
                'schema': 'arena-pipeline/openai-shared-cooldown/1', 'not_before': 0.0, 'cursor': 0}
            if (state.get('schema') != 'arena-pipeline/openai-shared-cooldown/1'
                    or type(state.get('cursor')) is not int or state['cursor'] < 0
                    or type(state.get('not_before')) not in (int, float)
                    or not math.isfinite(state['not_before']) or state['not_before'] < 0):
                raise RuntimeError('OpenAI shared cooldown state is invalid; refusing an uncoordinated request')
            yield state
            fd, temporary = tempfile.mkstemp(prefix='.openai-', dir=self.directory)
            try:
                with os.fdopen(fd, 'w') as handle:
                    json.dump(state, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, state_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def choose(self, available, preferred=None, avoid=None):
        with self.locked() as state:
            # A key the provider reported out of credit is skipped by every process until its
            # mark expires (2026-10-05: one of three keys ran dry and, rotated in turn, failed
            # a third of all reviews; its error was never failed over).
            now = self.clock()
            spent = {name: until for name, until in (state.get('spent') or {}).items()
                     if type(until) in (int, float) and until > now}
            state['spent'] = spent
            usable = [key for key in available if key.name not in spent]
            if available and not usable:
                raise RuntimeError('every OpenAI key in keys.env reports no remaining credit')
            available = usable or available
            wait = max(0.0, state['not_before'] - self.clock())
            if wait:
                return None, wait
            by_name = {key.name: key for key in available}
            if preferred in by_name:
                return by_name[preferred], 0.0
            choices = [(state['cursor'] + offset) % len(available) for offset in range(len(available))]
            position = next((index for index in choices if available[index].name != avoid), choices[0])
            key = available[position]
            state['cursor'] = position + 1
            return key, 0.0

    def mark_spent(self, name, hours=24.0):
        """Record that a key has no remaining credit (shared by every process)."""
        with self.locked() as state:
            spent = dict(state.get('spent') or {})
            spent[str(name)] = max(float(spent.get(str(name), 0.0)), self.clock() + hours * 3600.0)
            state['spent'] = spent

    def defer(self, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise RuntimeError('Invalid provider cooldown')
        with self.locked() as state:
            state['not_before'] = max(state['not_before'], self.clock() + seconds)


#: Rate-limit windows one call may wait out without spending one of its attempts
#: (user-approved, 2026-10-06). A throttle is the provider's capacity, not this request:
#: KEY3's organisation throttled gpt-6-astra and every 170-turn semantic review died on its
#: fifth quick retry, stopping six hardening lineages. `max_wait_s`
#: (ARENA_OPENAI_MAX_WAIT_S, 1800 s) normally binds first; backoff grows 15 s -> 120 s.
MAX_RATE_LIMITED_WINDOWS = 20

#: A stream refusal that is a provider safety classification of the request, not of the key.
REFUSAL_RETRY = "[cyber_policy]"

#: Provider codes that mean THIS KEY cannot pay for requests: fail over, never retry it.
CREDIT_CODES = ('credit_balance_exhausted', 'insufficient_quota', 'billing_hard_limit_reached',
                'billing_not_active')


def _credit_failure(exc) -> bool:
    return any(code in str(exc) for code in CREDIT_CODES)


def call(body: dict, retries: int = 5, *, adapter=None, keys_path=None,
         pool=None, max_wait_s=MAX_RETRY_WAIT_S) -> dict:
    """Retry the same logical request, with no renewal of the provider budget."""
    if type(retries) is not int or not 1 <= retries <= MAX_ATTEMPTS:
        raise ValueError('OpenAI attempts must be between 1 and 5')
    if not math.isfinite(max_wait_s) or max_wait_s < 0:
        raise ValueError('OpenAI retry wait bound must be finite and nonnegative')
    if adapter is None:
        from .calibrate import agent_openai as adapter
    if keys_path is None:
        from . import cli
        keys_path = cli.KEYS
    keys_path = Path(keys_path)
    pool = pool or SharedCooldown(keys_path.parent / 'pipeline-artifacts/provider-cooldowns')
    credentials(keys_path)  # Refuse stale inherited environment credentials.
    data = json.dumps({**body, 'stream': True}).encode()
    observe = adapter._transport_observer()
    last, preferred, avoid, waited = None, None, None, 0.0
    observed_routes = {}

    def wait(seconds, attempt):
        nonlocal waited
        if seconds > max_wait_s - waited:
            raise RuntimeError('OpenAI retry waiting budget exhausted; provider cooldown remains active')
        remaining = seconds
        while remaining > 0:
            chunk = min(remaining, WAIT_CHUNK_S)
            observe('retry-wait', attempt=attempt, delay_s=chunk)
            adapter.time.sleep(chunk)
            waited += chunk
            remaining -= chunk

    # `consumed` counts failed attempts against `retries`; a rate-limit window does not spend
    # one (MAX_RATE_LIMITED_WINDOWS, bounded by `max_wait_s` through the shared cooldown).
    consumed, limited_tries = 0, 0
    while consumed < retries:
        attempt = consumed
        while True:
            available = credentials(keys_path)
            try:
                credential, delay = pool.choose(available, preferred, avoid)
            except RuntimeError as exc:
                if last is not None:                  # name the provider's own code first
                    raise RuntimeError(f'{last}; {exc}') from exc
                raise
            if credential is not None:
                break
            wait(delay, attempt + 1)
        headers = credential.headers()
        headers.update(observed_routes.get(credential.value, {}))
        req = urllib.request.Request(adapter.API_URL, data=data, method='POST', headers=headers)
        try:
            observe('request-start', attempt=attempt + 1, credential_name=credential.name)
            with adapter.request_slot(), adapter.urllib.request.urlopen(req, timeout=adapter.ROLLOUT_READ_TIMEOUT_S) as resp:
                observe('response-open', attempt=attempt + 1)
                observed_routes[credential.value] = route_headers(getattr(resp, 'headers', {}))
                response = adapter._consume_stream(resp, observe)
                if response.get('status') == 'failed':
                    token = adapter._error_token(response)
                    if token in adapter.TRANSIENT_STREAM_CODES:
                        raise adapter._TransientProviderError('OpenAI stream reported a provider error [' + token + ']')
                    if token in adapter._NONRETRYABLE_QUOTA_CODES:
                        raise RuntimeError('OpenAI provider quota or spend limit [' + token + ']')
                return response
        except urllib.error.HTTPError as exc:
            last, delay = adapter._http_failure(exc, attempt)
            observe('http-error', attempt=attempt + 1, status=exc.code, retryable=delay is not None)
            if delay is None and _credit_failure(last):
                pool.mark_spent(credential.name)
                observe('credential-spent', attempt=attempt + 1, credential_name=credential.name)
                preferred, avoid = None, credential.name
                consumed += 1
                continue
            if delay is None:
                raise last from exc
            observed_routes[credential.value] = route_headers(exc.headers)
            delay = max(delay, retry_after_minimum(exc.headers))
            limited = exc.code == 429
            if limited:
                # Server Retry-After is already a minimum in _http_failure.
                # Also retain the ordinary long rate-window backoff, growing per window.
                delay = max(delay, adapter._transient_delay(attempt),
                            min(15 * 2 ** (attempt + limited_tries), 120))
        except adapter._TransientProviderError as exc:
            last = RuntimeError(str(exc))
            limited = '[rate_limit_exceeded]' in str(exc)
            delay = max(adapter._transient_delay(attempt), min(15 * 2 ** (attempt + limited_tries), 120))
            observe('transient-provider-error', attempt=attempt + 1)
        except RuntimeError as exc:
            if REFUSAL_RETRY in str(exc):
                # A provider safety refusal of this request (2026-10-05: `cyber_policy` refused
                # 4 of ~10 kaizen semantic reviews -- the reviewer prompt describes the anti-tamper
                # threat model). Not a finding: retry, rotating keys, within the same attempts.
                last = RuntimeError(str(exc))
                observe('provider-refusal', attempt=attempt + 1, credential_name=credential.name)
                preferred, avoid = None, credential.name
                if attempt + 1 < retries:
                    wait(min(15 * 2 ** attempt, 120), attempt + 1)
                consumed += 1
                continue
            if not _credit_failure(exc):
                raise
            # A stream `error` event for an unfunded key: mark it and fail over at once.
            last = RuntimeError(str(exc))
            pool.mark_spent(credential.name)
            observe('credential-spent', attempt=attempt + 1, credential_name=credential.name)
            preferred, avoid = None, credential.name
            consumed += 1
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            last = RuntimeError('OpenAI transport interrupted (' + type(exc).__name__ + ')')
            delay = adapter._retry_delay(None, attempt)
            limited = False
            observe('transport-interrupted', attempt=attempt + 1)
        preferred = None if limited else credential.name
        avoid = credential.name if limited else None
        if limited:
            pool.defer(delay)  # Shared across keys, even on the last attempt.
        if attempt + 1 < retries and not limited:
            wait(delay, attempt + 1)
        if limited and limited_tries < MAX_RATE_LIMITED_WINDOWS:
            limited_tries += 1
            observe('rate-limit-window', attempt=attempt + 1, windows=limited_tries)
            continue
        consumed += 1
    observe('exhausted', attempts=retries)
    raise RuntimeError('OpenAI call failed after ' + str(retries) + ' attempts: ' + str(last)
                       + (f'; {limited_tries} rate-limit window(s) waited' if limited_tries else ''))
