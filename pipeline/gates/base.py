"""Gate protocol: a named, ordered, fail-closed check over a task bundle.

Vendored from the AutomationBench authoring pipeline (benchmark-agnostic; no
changes). Kept verbatim so fixes flow in both directions.

Three rules this framework exists to enforce, each of which the repository has
already been bitten by:

1. **An error is a refusal, not a failure.** `run_evaluation_criteria` catches
   per-criterion exceptions and records them as 0.0, which silently converts a
   refusal into the thing it was written to prevent. The runner here never
   swallows an exception into FAIL: an exception is ERROR, and ERROR stops the
   pipeline rather than producing a verdict.
2. **Ordering is declared, not implied.** A gate names what it `requires`; a gate
   whose prerequisite did not pass reports SKIPPED, never FAILED, for the same
   reason `gated_out` is not `failed`.
3. **Every finding carries a location.** A blocker that does not say which file
   and which line is a blocker nobody can act on.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"     # a prerequisite did not pass
    ERROR = "error"         # the gate could not run -- refuse, do not score


class Severity(str, Enum):
    BLOCKER = "blocker"     # fails the gate
    WARN = "warn"           # reported; does not fail the gate
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    where: str = ""

    def __str__(self) -> str:
        loc = f" [{self.where}]" if self.where else ""
        return f"{self.severity.value.upper():7} {self.code}: {self.message}{loc}"


@dataclass
class GateResult:
    gate: str
    status: Status
    findings: list[Finding] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    error: str | None = None

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]


@dataclass
class GateContext:
    """Everything a gate may read. Paths are optional; a gate that needs one it
    was not given must report ERROR, never invent a verdict."""

    task_dir: Path
    repo_root: Path
    pristine_state: Path | None = None
    golden_state: Path | None = None
    image_digest: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def path(self, *parts: str) -> Path:
        return self.task_dir.joinpath(*parts)


class Gate(Protocol):
    name: str
    requires: tuple[str, ...]

    def run(self, ctx: GateContext) -> GateResult: ...


@dataclass
class FunctionGate:
    """Wrap a plain function as a gate.

    The function returns findings and optional evidence; the wrapper decides
    PASS/FAIL from the findings' severities, so a gate cannot accidentally
    return PASS while holding a blocker.
    """

    name: str
    fn: Callable[[GateContext], tuple[list[Finding], dict[str, Any]]]
    requires: tuple[str, ...] = ()

    def run(self, ctx: GateContext) -> GateResult:
        started = time.monotonic()
        try:
            findings, evidence = self.fn(ctx)
        except Exception as exc:
            return GateResult(
                gate=self.name, status=Status.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                evidence={"traceback": traceback.format_exc(limit=6)},
                duration_s=time.monotonic() - started,
            )
        blocked = any(f.severity is Severity.BLOCKER for f in findings)
        return GateResult(
            gate=self.name,
            status=Status.FAIL if blocked else Status.PASS,
            findings=findings, evidence=evidence,
            duration_s=time.monotonic() - started,
        )


def run_gates(gates: Iterable[Gate], ctx: GateContext) -> list[GateResult]:
    """Run gates in dependency order.

    A gate whose prerequisite did not PASS is SKIPPED. An ERROR anywhere leaves
    every dependent gate SKIPPED too -- the pipeline refuses rather than
    reporting a green bundle it could not actually verify.
    """
    gates = list(gates)
    by_name = {g.name: g for g in gates}
    results: dict[str, GateResult] = {}
    order: list[str] = []
    pending = [g.name for g in gates]

    while pending:
        progressed = False
        for name in list(pending):
            gate = by_name[name]
            deps = gate.requires
            if any(d in by_name and d not in results for d in deps):
                continue
            missing = [d for d in deps if d not in by_name]
            if missing:
                results[name] = GateResult(
                    gate=name, status=Status.ERROR,
                    error=f"unknown prerequisite(s): {', '.join(missing)}")
            elif any(results[d].status is not Status.PASS for d in deps):
                blockers = [d for d in deps if results[d].status is not Status.PASS]
                results[name] = GateResult(
                    gate=name, status=Status.SKIPPED,
                    evidence={"blocked_by": blockers})
            else:
                results[name] = gate.run(ctx)
            pending.remove(name)
            order.append(name)
            progressed = True
        if not progressed:
            for name in pending:
                results[name] = GateResult(gate=name, status=Status.ERROR,
                                           error="cyclic prerequisite chain")
                order.append(name)
            break
    return [results[n] for n in order]


def green(results: Iterable[GateResult]) -> bool:
    """A bundle is green only when every gate actually ran and passed.

    SKIPPED and ERROR are not green. A pipeline that treats "could not check" as
    "nothing wrong" is the whole problem.
    """
    results = list(results)
    return bool(results) and all(r.status is Status.PASS for r in results)
