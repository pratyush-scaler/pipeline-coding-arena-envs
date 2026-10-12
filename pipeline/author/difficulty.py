"""Advisory design feedback, never an authoring or admission gate.

Mechanisms are hypotheses until blind solving and held-out native campaigns
demonstrate them. Metadata is optional; trap counts are not difficulty scores.
"""
from __future__ import annotations

from pathlib import Path

MECHANISMS = {
    "semantic_decoy": "near-equivalent live code paths with different contracts",
    "exception_interaction": "individually simple exceptions whose intersection changes the answer",
    "representation_collision": "distinct identities collide after a tempting normalization",
    "temporal_state": "correctness depends on a deterministic sequence of prior actions",
    "distributed_invariant": "a local fix violates a contract across module boundaries",
    "architectural_carry": "later requirements invalidate an earlier architectural assumption",
}

EXTREME_PROMPT = """
EXTREME DIFFICULTY DESIGN -- advisory hypotheses, not claims of measured hardness.
Build several independent semantic failure mechanisms and make their interactions
matter. You may add as many scenario-grounded decoys as useful. A decoy must be
plausible code or documentation in this repository: same concepts and interface,
but a different version, authority, scope, identity or lifecycle. Give the solver
enough evidence to distinguish it. Merely adding files or renaming symbols is not
evidence of difficulty. Keep the published contract determinate.

Consider semantic_decoy, exception_interaction, representation_collision,
temporal_state, distributed_invariant, and architectural_carry. Examples: an
exact placeholder exception combined with a sorting rule; aliases that must
merge alongside similar identifiers that must stay distinct; retry/replay and
cache invalidation after a partial update; two plausible call paths where only
one preserves a downstream invariant. For multi-step tasks, reveal new demands
sequentially and test preservation at each checkpoint. Use deterministic action
sequences, intersection cases, and metamorphic transformations derived from
published rules; accept differently written correct implementations.

Optionally add quality.difficulty_case.hypotheses entries:
  {id, mechanism, traps: [ids], checks: [objective ids],
   tempting_rule, correct_rule, distinguishing_case,
   interaction_with: [other hypothesis ids]}
Use difficulty_profile during creation for advisory feedback. Exercise plausible
shortcuts with probe_patch before registering mutants, and inspect blind_solve
across ALL steps when useful. Test several distinct wrong algorithms rather than
only the one the author first imagined.
Use generate_cases to retain deterministic rule intersections and action histories;
include restart, partial failure, retry, retraction and boundary cases where the
scenario supports them. Its checkpoints template supports six cumulative stages.
Use independent_oracle before deriving hidden-test expectations: a different
vendor sees public inputs only and independently writes an alternative, with
hashed provenance. Stage quality.independent_oracle and its alternative patches
when useful; downstream controls still decide correctness. For decoy experiments,
retain four separately authored variants: neither decoy nor interaction, decoy
alone, interaction alone, and both. Keep the published contract and check schema
identical; only public decoys and hidden interaction inputs differ respectively.
stage_research_variants can retain these drafts and a deferred experiment plan
from your current draft. It never runs validation or native campaigns in creation.
Optionally record supporting_evidence, disambiguating_evidence, ordinary_cases,
divergence_generator and multiple mutants on each hypothesis. All empirical
adoption counts come from frozen experiments and cited solver behavior, not author
declarations. A shared check-failure signature alone does not prove adoption.
Evidence entries are {file, quote}: fixture-relative file or step:<name> for a
public instruction, and an exact excerpt. Private oracle/hidden-test files never
provide the solver's evidence. Multiple mutants entries name registered ids.
Missing hypotheses, a weak profile, a failed optional probe, or an easy draft
never prevents submission. Submit the candidate; only downstream validation can
reject it. Never treat hidden requirements, broken setup, or a solver cutoff as
evidence that the model failed on a valid hard task.
"""


def profile(spec: dict) -> dict:
    """A robust, pure summary even for an incomplete staged draft."""
    def objects(value):
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    traps, mutants = objects(spec.get("traps")), objects(spec.get("mutants"))
    quality = spec.get("quality") if isinstance(spec.get("quality"), dict) else {}
    case = quality.get("difficulty_case") if isinstance(quality.get("difficulty_case"), dict) else {}
    hypotheses = objects(case.get("hypotheses"))
    mechanisms = sorted({h["mechanism"] for h in hypotheses
                         if isinstance(h.get("mechanism"), str) and h["mechanism"] in MECHANISMS})
    signatures = {}
    for mutant in mutants:
        fails = mutant.get("fails")
        if isinstance(fails, list) and fails and all(isinstance(c, str) for c in fails):
            signatures.setdefault(tuple(sorted(set(fails))), []).append(mutant.get("id"))
    duplicates = [ids for ids in signatures.values() if len(ids) > 1]
    advisories = []
    if not hypotheses:
        advisories.append("ADVISORY: describe optional causal hypotheses; trap count alone does not establish difficulty")
    if len(mechanisms) < 3:
        advisories.append("ADVISORY: consider several independent mechanisms and a case where they interact")
    if hypotheses and not any(h.get("interaction_with") for h in hypotheses):
        advisories.append("ADVISORY: hypotheses have no declared interactions; isolated rules may be easy to patch independently")
    if duplicates:
        advisories.append("ADVISORY: several mutants share a failure signature; inspect whether they exercise different reasoning")
    if len(objects(spec.get("steps"))) < 2:
        advisories.append("ADVISORY: consider sequential requirements when the scenario supports architectural carry")
    return {"advisory_only": True, "measured_hardness": False,
            "trap_count": len(traps), "hypothesis_count": len(hypotheses),
            "mechanisms": mechanisms, "duplicate_mutant_signatures": duplicates,
            "advisories": advisories}


def hypothesis_problems(spec: dict) -> list[str]:
    """Validate supplied references during validation; absence is always fine."""
    quality = spec.get("quality")
    case = quality.get("difficulty_case") if isinstance(quality, dict) else None
    hypotheses = case.get("hypotheses") if isinstance(case, dict) else None
    if hypotheses is None:
        return []
    if not isinstance(hypotheses, list):
        return ["difficulty_case.hypotheses must be an array when supplied"]
    traps = {t["id"] for t in spec.get("traps") or [] if isinstance(t, dict) and isinstance(t.get("id"), str)}
    checks = {c["id"] for c in spec.get("checks") or [] if isinstance(c, dict)
              and isinstance(c.get("id"), str) and c.get("kind") == "objective"}
    ids = [h.get("id") for h in hypotheses if isinstance(h, dict)]
    mutants = {m.get("id") for m in spec.get("mutants") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)}
    problems = []
    for h in hypotheses:
        if not isinstance(h, dict):
            problems.append("difficulty hypotheses must be objects")
            continue
        hid = h.get("id")
        if not isinstance(hid, str) or not hid or ids.count(hid) != 1:
            problems.append("difficulty hypotheses need unique nonempty string ids")
        if not isinstance(h.get("mechanism"), str) or h["mechanism"] not in MECHANISMS:
            problems.append(f"hypothesis {hid!r}: unknown mechanism")
        for key, valid in (("traps", traps), ("checks", checks), ("interaction_with", ids)):
            values = h.get(key, [] if key == "interaction_with" else None)
            if (not isinstance(values, list) or
                    (key != "interaction_with" and not values) or
                    any(not isinstance(v, str) or v not in valid or
                        (key == "interaction_with" and v == hid) for v in values)):
                problems.append(f"hypothesis {hid!r}: {key} must contain valid references")
        for key in ("tempting_rule", "correct_rule", "distinguishing_case"):
            if not isinstance(h.get(key), str) or not h[key].strip():
                problems.append(f"hypothesis {hid!r}: {key} is required when a hypothesis is supplied")
        if "mutants" in h and (not isinstance(h["mutants"], list) or not h["mutants"] or
                               any(not isinstance(m, str) or m not in mutants for m in h["mutants"])):
            problems.append(f"hypothesis {hid!r}: mutants must contain registered mutant ids")
    return problems


def hypothesis_evidence_problems(spec: dict, bundle: Path) -> list[str]:
    """Verify supplied public citations during validation; drafts never need them."""
    from .contracts import contained
    quality = spec.get("quality")
    case = quality.get("difficulty_case") if isinstance(quality, dict) else None
    hypotheses = case.get("hypotheses") if isinstance(case, dict) else None
    if not isinstance(hypotheses, list):
        return []
    instructions = {s.get("name"): s.get("instruction", "") for s in spec.get("steps") or [] if isinstance(s, dict)}
    issues = []
    for h in hypotheses:
        if not isinstance(h, dict):
            continue
        for field in ("supporting_evidence", "disambiguating_evidence"):
            if field not in h:
                continue
            try:
                if not isinstance(h[field], list) or not h[field]:
                    raise ValueError("evidence must be a nonempty array of {file, quote}")
                for row in h[field]:
                    file, quote = row["file"], row["quote"]
                    text = instructions[file[5:]] if file.startswith("step:") else contained(bundle / "environment/repo", file).read_text()
                    if not isinstance(quote, str) or not quote.strip() or quote not in text:
                        raise ValueError("quote is absent from the public source")
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                issues.append(f"hypothesis {h.get('id')!r}: {field}: {exc}")
    return issues
