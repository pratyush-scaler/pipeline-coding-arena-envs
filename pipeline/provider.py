"""Process-wide API admission and spend bounds, shared by authoring and solvers."""
from __future__ import annotations
import contextlib
import json
import math
import random
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import threading
import time


class ProviderBudgetExhausted(BaseException):
    """The process-wide request cap is spent: stop the run, loudly.

    A `BaseException`, deliberately, like `KeyboardInterrupt`. Every rollout and
    review loop catches `Exception` and records it as a void or an unverified
    verdict, so an ordinary exception here turned budget exhaustion into a
    stream of voids that looked like results. This one passes through those
    handlers and ends the run instead.
    """


EXHAUSTED = "provider request budget exhausted; run stopped, no evidence recorded"


class RequestBudget:
    """Concurrency and a request-count spend cap. No wall-clock deadline.

    There used to be one (`ARENA_API_WALL_SECONDS`, 4 hours), and past it every
    rollout became a void: every screening episode after hour four of a long
    batch was unverified. POLICY runs calibration with no turn or time limits, so time is
    not a budget here. Exhaustion of the request cap raises
    `ProviderBudgetExhausted`, which stops the run rather than voiding it.
    """

    def __init__(self, concurrency=8, max_requests=2500):
        if concurrency < 1 or max_requests < 1:
            raise ValueError("provider budget values must be positive")
        self.semaphore = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.max_requests = max_requests
        self.started = None
        self.requests = 0

    @contextlib.contextmanager
    def slot(self):
        with self.semaphore:
            with self.lock:
                if self.started is None:
                    self.started = time.monotonic()
                if self.requests >= self.max_requests:
                    raise ProviderBudgetExhausted(EXHAUSTED)
                self.requests += 1
            yield

    def stats(self):
        with self.lock:
            return {"requests": self.requests, "max_requests": self.max_requests,
                    "elapsed_seconds": round(time.monotonic() - self.started, 2) if self.started else 0}


from .config import MAX_API_REQUESTS, MAX_PROVIDER_CONCURRENCY
GLOBAL = RequestBudget(int(os.environ.get("ARENA_PROVIDER_CONCURRENCY", str(MAX_PROVIDER_CONCURRENCY))),
                       int(os.environ.get("ARENA_MAX_API_REQUESTS", str(MAX_API_REQUESTS))))
request_slot = GLOBAL.slot


_NONRETRYABLE_QUOTA_CODES = frozenset({
    'insufficient_quota', 'billing_hard_limit_reached',
    'billing_not_active', 'organization_spend_limit_exceeded',
    'project_spend_limit_exceeded', 'organization_usage_limit_exceeded',
})


#: The longest a single retry may sleep, regardless of what the server asks.
#:
#: There is no overall call deadline any more (see `designer.CALL_DEADLINE_S`):
#: a call that is slow but progressing is not a failure, and capping it destroyed
#: a 26-minute build that had already produced 31 good designer responses. What
#: that deadline *also* did, incidentally, was stop the pipeline honouring an
#: absurd `Retry-After` -- a server answering `Retry-After: 999999` would
#: otherwise put a worker to sleep for eleven days.
#:
#: So the bound moves to where it belongs. Waiting is capped; working is not.
MAX_RETRY_SLEEP_S = 120.0


def _retry_delay(headers, attempt: int) -> float:
    """Honor a server delay; otherwise use bounded exponential backoff with jitter.

    Capped at `MAX_RETRY_SLEEP_S`: a retry longer than that is indistinguishable
    from a hang, and the next attempt can ask again.
    """
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
    if math.isfinite(seconds) and seconds >= 0:
        return min(max(delay, seconds), MAX_RETRY_SLEEP_S)
    return delay


#: Anthropic phrasings of a limit that lasts hours or days (a subscription's
#: weekly or usage limit, a spent credit balance), not a per-minute rate limit.
#: Census 2026-10-03: a weekly-limit 429 was retried as transient and errored all
#: ten tasks of a batch. Matched to classify only; the prose is never logged.
_QUOTA_PHRASES = ("weekly", "usage limit", "monthly", "credit balance", "spend limit", "billing")

#: Markers a fatal provider failure carries in its message, so a batch can stop
#: launching tasks that would fail the same way (`fatal_provider_error`).
QUOTA_MARKER = "quota or spend limit; retry cannot restore access"
CREDENTIAL_MARKER = "credential rejected; check keys.env"


def fatal_provider_error(text) -> bool:
    """Does an error message say access itself is gone (quota or credential)?"""
    text = str(text or "")
    return QUOTA_MARKER in text or CREDENTIAL_MARKER in text


def _http_failure(exc, attempt: int):
    # Error bodies can quote prompts. Read only enough to classify known quota
    # codes, and never put provider prose into logs or retained trial errors.
    try:
        payload = json.loads(exc.read(8192))
    except (ValueError, TypeError, UnicodeDecodeError):
        payload = {}
    error = payload.get('error') if isinstance(payload, dict) else None
    code = error.get('code') if isinstance(error, dict) else None
    kind = error.get('type') if isinstance(error, dict) else None
    text = str(error.get('message') or '').lower() if isinstance(error, dict) else ''
    quota = ((isinstance(code, str) and code in _NONRETRYABLE_QUOTA_CODES) or kind == 'billing_error'
             or (exc.code in (400, 429) and any(phrase in text for phrase in _QUOTA_PHRASES)))
    credential = not quota and (exc.code in (401, 403)
                                or kind in ('authentication_error', 'permission_error'))
    message = f'HTTP {exc.code}: provider request failed'
    if quota:
        message += f' ({QUOTA_MARKER})'
    elif credential:
        message += f' ({CREDENTIAL_MARKER})'
    retryable = not quota and not credential and (exc.code in (408, 409, 429) or 500 <= exc.code < 600)
    return RuntimeError(message), _retry_delay(exc.headers, attempt) if retryable else None


def _transport_observer(provider="anthropic"):
    """Optional payload-free telemetry; observation failures cannot change a run."""
    filename = os.environ.get('ARENA_TRANSPORT_OBSERVATIONS')
    request_id, started = uuid.uuid4().hex, time.monotonic()
    def observe(stage, **metadata):
        if not filename:
            return
        row = {'provider': provider, 'request_id': request_id, 'stage': stage,
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

