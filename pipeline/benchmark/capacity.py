"""Share native trial capacity across simultaneous task/model campaigns."""
from collections import deque
from contextlib import contextmanager, ExitStack
import os
import threading
import time


class NativeCapacity:
    def __init__(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError('Native episode capacity must be positive')
        self.limit = limit
        self.active = self.peak = 0
        self.condition = threading.Condition()
        self.waiting = deque()

    @contextmanager
    def reserve(self, requested):
        if type(requested) is not int or requested < 1:
            raise ValueError('Requested episode slots must be positive')
        ticket = object()
        with self.condition:
            self.waiting.append(ticket)
            self.condition.notify_all()
            try:
                self.condition.wait_for(lambda: self.waiting[0] is ticket and self.active < self.limit)
                granted = min(requested, self.limit - self.active)
                self.active += granted
                self.peak = max(self.peak, self.active)
            finally:
                self.waiting.remove(ticket)
                self.condition.notify_all()
        try:
            yield granted
        finally:
            with self.condition:
                self.active -= granted
                self.condition.notify_all()

    def stats(self):
        with self.condition:
            return {'limit': self.limit, 'reserved': self.active, 'peak_reserved': self.peak,
                    'waiting_campaigns': len(self.waiting)}


NATIVE_CAPACITY = NativeCapacity(int(os.environ.get('ARENA_NATIVE_CONCURRENCY', '16')))


#: Concurrent Codex (GPT) calibration episodes on this machine, across EVERY
#: process: `ARENA_CODEX_SLOTS` (unset = unlimited). On 2026-10-04 GPT-6 Astra's
#: organisation-wide 2M tokens/minute cap voided 63 of 121 Astra episodes in an
#: hour: each Codex request carries ~33k tokens and every screen, hardening and
#: build process launched its own episodes. Codex gives up after five quick
#: reconnects, so an over-subscribed minute is a lost (void, never counted) episode.
#: Fable episodes are not limited by this.
CODEX_SLOTS_DIR = "/tmp/arena-codex-slots"


#: Concurrent native trials of ANY model on this machine (`ARENA_TRIAL_SLOTS`).
#: Every trial creates a compose network, and Docker's default address pools
#: hold about 31: on 2026-10-04 a busy workspace failed trials with "all
#: predefined address pools have been fully subnetted".
TRIAL_SLOTS_DIR = "/tmp/arena-trial-slots"


@contextmanager
def trial_slots(wanted: int, *, poll_s: float = 5.0):
    """Hold up to `wanted` machine-wide trial slots (at least one) while a campaign runs."""
    with _file_slots(TRIAL_SLOTS_DIR, os.environ.get("ARENA_TRIAL_SLOTS", ""), wanted, poll_s) as held:
        yield held


@contextmanager
def codex_slots(model: str, wanted: int, *, poll_s: float = 5.0):
    """Hold up to `wanted` machine-wide Codex slots (at least one) while a GPT campaign runs."""
    if not str(model).split("/")[-1].startswith("gpt-"):
        yield wanted
        return
    with _file_slots(CODEX_SLOTS_DIR, os.environ.get("ARENA_CODEX_SLOTS", ""), wanted, poll_s) as held:
        yield held


@contextmanager
def _file_slots(directory: str, raw: str, wanted: int, poll_s: float):
    import fcntl
    if not raw.isdigit() or int(raw) < 1:
        yield wanted
        return
    total = int(raw)
    CODEX_SLOTS_DIR_ = directory
    os.makedirs(CODEX_SLOTS_DIR_, exist_ok=True)
    with ExitStack() as stack:
        held = 0
        while held == 0:
            for i in range(total):
                if held >= wanted:
                    break
                handle = open(os.path.join(CODEX_SLOTS_DIR_, f"slot-{i}.lock"), "a+")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                stack.callback(handle.close)
                stack.callback(fcntl.flock, handle, fcntl.LOCK_UN)
                held += 1
            if held == 0:
                time.sleep(poll_s)
        yield held
