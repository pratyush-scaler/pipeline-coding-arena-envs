"""Traps for coding tasks: typed, provenanced, and proved by an executed mutant.

A trap declares where a strong model goes wrong for its own reasons. Each type
names the correction classes it may be derived from (a `constraint` trap
cannot be derived from a taste complaint), the difficulty axis it loads, and
the lever behind it. The taxonomy comes from the corpus analysis: what users
actually corrected in 370 coding sessions (`analysis/CODING-TRACES.md` §4).

Provenance is the parent's rule: `trace_derived` cites the moment and quotes
the user; `authored` names an existing fixture surface and why a practitioner
would meet the case. Every trap names the checks it drives and the mutant
that proves it -- a plausible wrong fix that fails exactly those checks
(`controls.py` executes it).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..policy import CATEGORY_TRACE_FLOOR

#: (allowed correction classes, axis, lever, one-line proof obligation)
TAXONOMY = {
    "symptom_patch": (("persistent_defect", "spec_correction"), "inference", "join",
                      "the visible repro passes and a hidden mirror case fails"),
    "chained_defect": (("persistent_defect",), "execution", "carry",
                       "defect B is reachable only after A is fixed; a mutant fixing A alone fails B's checks"),
    "spec_misread": (("spec_correction", "persistent_defect"), "inference", "arbitration",
                     "a plausible narrower or wider reading of a stated rule; hidden cases sit on the divergence"),
    "constraint": (("constraint_assertion", "scope_correction"), "restraint", "restraint",
                   "a stated prohibition, enforced by a guardrail; the mutant fixes by violating it"),
    "revert_residue": (("revert_request", "persistent_defect"), "inference", "arbitration",
                       "something reverted still leaves residue; the mutant removes the headline only"),
    "scope_creep_bait": (("scope_correction", "constraint_assertion"), "restraint", "restraint",
                         "an adjacent broken thing must be left alone; the mutant fixes it"),
    "regression_bait": (("persistent_defect", "spec_correction"), "execution", "carry",
                        "the obvious fix for A breaks B; a regression guardrail catches it"),
    "decoy_location": (("persistent_defect", "spec_correction"), "inference", "join",
                       "a tempting wrong place to fix (caller, not callee); the mutant patches the decoy"),
    "stale_authority": (("spec_correction", "revert_request"), "inference", "arbitration",
                        "docs or comments state a contract the tests contradict; the instruction says which governs"),
    "premise_revision": (("spec_correction", "constraint_assertion"), "inference", "arbitration",
                         "a later step revises a contract step-1 code already satisfies; earlier work must be re-derived"),
    "unsatisfiable": (("persistent_defect", "constraint_assertion"), "inference", "arbitration",
                      "part of the request cannot be met offline; the answer reports it and completes the rest"),
    "environment": (("infrastructure", "persistent_defect"), "execution", "carry",
                    "the fixture assumes a toolchain that is not the container's; the mutant hard-codes it"),
    "semantic_decoy": (("persistent_defect", "spec_correction"), "inference", "join",
                       "a near-equivalent live path has a different contract; the mutant changes the wrong path"),
    "exception_interaction": (("spec_correction", "persistent_defect"), "inference", "arbitration",
                              "the mutant handles individual exceptions but fails their published intersection"),
    "representation_collision": (("spec_correction", "persistent_defect"), "inference", "join",
                                 "the mutant merges distinct identities or splits equivalent ones after normalization"),
    "temporal_state": (("persistent_defect", "spec_correction"), "execution", "sequence",
                       "a deterministic action sequence reveals a state invariant missed by isolated examples"),
    "distributed_invariant": (("persistent_defect", "spec_correction"), "execution", "join",
                              "a plausible local repair violates an observable invariant across modules"),
    "architectural_carry": (("spec_correction", "constraint_assertion"), "inference", "sequence",
                            "later published demands invalidate an earlier assumption; a local extension loses prior behaviour"),
    # Failure modes by the SEMANTICS a researched trap turns on (user, 2026-10-17: diverse failure
    # modes across tasks; author/trap_research.py). The batch ledger had 30% spec_misread and 22%
    # semantic_decoy: generic labels hide which judgment failed, so tasks repeated one mode.
    "library_semantics": (("persistent_defect", "spec_correction", "infrastructure"), "inference", "join",
                          "the pinned library's documented behaviour differs from the idiom written from memory; "
                          "the mutant uses the idiom"),
    "protocol_semantics": (("persistent_defect", "spec_correction"), "inference", "join",
                           "a valid message of the format or protocol (HTTP, URL, CSV, JSON, SQL, MIME) that the "
                           "common shortcut mishandles; the mutant takes the shortcut"),
    "numeric_semantics": (("persistent_defect", "spec_correction"), "inference", "arbitration",
                          "precision, rounding stage, overflow or remainder allocation decides the value; the "
                          "mutant uses the default arithmetic"),
    "text_semantics": (("persistent_defect", "spec_correction"), "inference", "join",
                       "code points, graphemes, line boundaries, case mapping or encoding decide membership; the "
                       "mutant handles the ASCII member only or over-folds"),
    "temporal_semantics": (("persistent_defect", "spec_correction"), "inference", "arbitration",
                           "time zone, DST, calendar arithmetic or range inclusivity decides the result; the mutant "
                           "uses the naive clock"),
    "absence_semantics": (("persistent_defect", "spec_correction"), "inference", "arbitration",
                          "missing vs null vs empty vs zero (or SQL NULL logic) carry different published meanings; "
                          "the mutant collapses them"),
    "ordering_semantics": (("persistent_defect", "spec_correction"), "inference", "arbitration",
                           "ties, stability, precedence or first-vs-last decide the output; the mutant relies on a "
                           "default order"),
    "resource_semantics": (("persistent_defect", "constraint_assertion"), "execution", "carry",
                           "a partial failure, retry or rerun must leave the published state; the mutant writes "
                           "in place or repeats a side effect"),
    "concurrency_semantics": (("persistent_defect", "spec_correction"), "execution", "sequence",
                              "a deterministic interleaving or async ordering breaks the published rule; the mutant "
                              "serialises or assumes completion order"),
    "security_semantics": (("persistent_defect", "constraint_assertion"), "inference", "restraint",
                           "an encoded, indirect or other-owner member of a published safety rule; the mutant "
                           "blocks the obvious form only"),
    "error_semantics": (("persistent_defect", "spec_correction"), "inference", "arbitration",
                        "the published failure contract (what is refused, what it says, what stays on disk) on an "
                        "error path the visible cases never reach; the mutant fails generically"),
}
TYPES = tuple(TAXONOMY)
AXES = ("inference", "execution", "restraint")
CITATION = re.compile(r"^[0-9a-f-]{36}::msg\d+$")


@dataclass
class Trap:
    id: str
    type: str
    surface: str
    provenance: str
    checks: list[str] = field(default_factory=list)
    mutant: str | None = None
    derived_from: str | None = None
    correction_class: str | None = None
    quote: str | None = None
    grounding: str | None = None
    grounded_in: str | None = None
    beatable_by: str | None = None
    research: dict | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Trap":
        known = {k: raw.get(k) for k in cls.__dataclass_fields__}
        known["checks"] = list(raw.get("checks") or [])
        return cls(**known)

    def validate(self, check_ids: set[str], fixture_paths: set[str], mutant_ids: set[str]) -> list[str]:
        where = f"trap {self.id!r}"
        issues = []
        if not isinstance(self.id, str) or not re.match(r"^t_[a-z0-9_]{2,40}$", self.id):
            issues.append(f"{where}: id must match t_[a-z0-9_]+")
        if self.type not in TAXONOMY:
            issues.append(f"{where}: unknown type {self.type!r}; one of {', '.join(TYPES)}")
        if not isinstance(self.surface, str) or not self.surface:
            issues.append(f"{where}: surface must name the file or module the trap lives in")
        if not self.checks:
            issues.append(f"{where}: must drive at least one check")
        for cid in self.checks:
            if cid not in check_ids:
                issues.append(f"{where}: drives undeclared check {cid!r}")
        if not self.mutant or self.mutant not in mutant_ids:
            issues.append(f"{where}: needs a declared mutant that proves it (a plausible wrong fix)")
        if not isinstance(self.beatable_by, str) or len(self.beatable_by) < 15:
            issues.append(f"{where}: beatable_by must say how a careful engineer avoids it")
        if self.provenance == "trace_derived":
            if not self.derived_from or not CITATION.match(str(self.derived_from)):
                issues.append(f"{where}: trace_derived needs derived_from '<session>::msgN'")
            allowed = TAXONOMY.get(self.type, ((),))[0]
            if self.correction_class not in allowed:
                issues.append(f"{where}: a {self.type} trap cannot be derived from a "
                              f"{self.correction_class!r} correction (allowed: {', '.join(allowed)})")
            if not isinstance(self.quote, str) or len(self.quote.strip()) < 8:
                issues.append(f"{where}: trace_derived needs the user's words, verbatim")
        elif self.provenance == "authored":
            if not isinstance(self.grounding, str) or len(self.grounding) < 30:
                issues.append(f"{where}: authored needs a grounding of at least 30 characters")
            if self.grounded_in not in fixture_paths:
                issues.append(f"{where}: grounded_in must name an existing fixture file")
            if self.derived_from or self.quote:
                issues.append(f"{where}: authored traps carry no trace citation")
        else:
            issues.append(f"{where}: provenance must be trace_derived or authored")
        return issues


def validate_all(traps: list[dict], check_ids: set[str], fixture_paths: set[str],
                 mutant_ids: set[str]) -> list[str]:
    if not isinstance(traps, list) or not traps:
        return ["at least one trap is required"]
    issues, seen = [], set()
    for raw in traps:
        if not isinstance(raw, dict):
            issues.append("traps must be objects")
            continue
        trap = Trap.from_dict(raw)
        if trap.id in seen:
            issues.append(f"trap {trap.id!r}: duplicate id")
        seen.add(trap.id)
        issues.extend(trap.validate(check_ids, fixture_paths, mutant_ids))
    return issues


def trace_exploitation(traps: list[dict], usable_corrections: int) -> float:
    """Distinct cited corrections over usable ones, capped at 1."""
    if not usable_corrections:
        return 1.0
    cited = {t.get("derived_from") for t in traps if t.get("provenance") == "trace_derived" and t.get("derived_from")}
    return min(1.0, len(cited) / usable_corrections)


def trace_derived_share(traps: list[dict]) -> float:
    if not traps:
        return 0.0
    return sum(1 for t in traps if t.get("provenance") == "trace_derived") / len(traps)


def axes_loaded(traps: list[dict]) -> set[str]:
    return {TAXONOMY[t["type"]][1] for t in traps if t.get("type") in TAXONOMY}


def floor_for(tier) -> float:
    return CATEGORY_TRACE_FLOOR.get({1: "A", 2: "B", 3: "C", 4: "D"}.get(tier, "D"), 0.30)
