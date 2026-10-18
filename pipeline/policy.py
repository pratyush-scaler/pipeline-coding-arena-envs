"""Every acceptance threshold in the programme, in one place, with its source.

Before this module the thresholds lived in prose across five documents and in
constants across a dozen modules, and two of them contradicted each other badly
enough to close the funnel at both ends: `release.py` required a model to solve
a task perfectly blind *before* difficulty was measured, and the band gate then
required most checks to sit at 30-70%. A genuinely hard task could satisfy
neither. Of 86 candidates in `envs-cat-v2`, 47 died on unrepairable findings and
the 12 that reached calibration scored 0.93-1.00 mean reward. Nothing shipped.

So: a threshold lives here or it does not exist. Each carries the clause of the
Arena spec (RL Environments Quality Spec, 2026-09-21) it implements, or an
explicit note that it is ours. `SPEC_*` names are Arena's numbers and change
only when Arena changes them; anything else is a local decision and says so.

Nothing here decides whether a task is *good*. These are admissibility bounds.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Calibration models
# ---------------------------------------------------------------------------

#: Spec, "Calibration and results": primary Fable 5.1, secondary Astra. The
#: difficulty thresholds below were set by Arena against these models, so
#: substituting one is a deviation the batch report must declare rather than a
#: free choice -- a pass@1 measured on a different model is not the same number.
SPEC_PRIMARY_MODEL = "claude-fable-5-1"

#: GPT 6 Astra, OpenAI. `resolve_model` routes a bare `gpt-` id to `openai/`.
SPEC_SECONDARY_MODEL = "gpt-6-astra"

#: Reasoning effort for BOTH calibration models, set by the user on
#: 2026-09-23. It is part of the measurement, not a runtime detail: a pass@1 is
#: "Fable 5.1 at high", not "Fable 5.1", and a figure taken at another effort is
#: not comparable. `release.py` already refuses to mix efforts across a task's
#: two campaigns; this makes the level itself explicit and reportable.
#:
#: Note the direction of the risk. Higher effort makes capable models act more,
#: and the restraint guardrails are exactly the checks that catch over-acting,
#: so effort moves guardrail rates as well as objective rates.
SPEC_CALIBRATION_EFFORT = "high"

#: The agent harness each calibration model runs in (user, 2026-09-26): Fable
#: 5.1 in Claude Code, GPT 6 Astra in Codex, both at SPEC_CALIBRATION_EFFORT.
#: Like effort, the harness is part of the measurement -- "Fable 5.1 in Claude
#: Code at high" -- and a campaign under any other harness is not comparable.
#: Both run INSIDE the task container (Terminus-2 drove it from outside), so
#: each gets its CLI pre-baked into the task image at a pinned version and, for
#: the agent phase only, network egress to its own model API host and nothing
#: else. The separate verifier stays offline.
CALIBRATION_HARNESS = {SPEC_PRIMARY_MODEL: "claude-code", SPEC_SECONDARY_MODEL: "codex"}

#: Pinned CLI versions (npm, resolved 2026-09-26). An update is an explicit
#: change: it alters the task image and therefore every artifact hash.
HARNESS_VERSIONS = {"claude-code": "2.1.283", "codex": "0.157.1"}

#: The only hosts each harness may reach during the agent phase. A configured
#: provider base URL (ANTHROPIC_BASE_URL / OPENAI_BASE_URL) adds its host.
HARNESS_API_HOSTS = {"claude-code": ("api.anthropic.com",), "codex": ("api.openai.com",)}


#: A documented substitute model (CALIBRATION_SUBSTITUTIONS) runs in its
#: provider's harness: Claude models in Claude Code, OpenAI models in Codex.
PROVIDER_HARNESS = {"anthropic": "claude-code", "openai": "codex"}


def harness_for(model) -> str:
    """The agent harness a calibration model runs in (Claude Code / Codex)."""
    text = str(model or "")
    bare = text.split("/")[-1].lower()
    harness = next((h for m, h in CALIBRATION_HARNESS.items() if m.lower() == bare), None)
    if harness is None:
        provider = (text.split("/", 1)[0].lower() if "/" in text else
                    "anthropic" if bare.startswith("claude-") else
                    "openai" if bare.startswith("gpt-") else None)
        harness = PROVIDER_HARNESS.get(provider)
    if harness is None:
        raise ValueError(f"{model}: no calibration harness is assigned to this model "
                         f"(assigned: {CALIBRATION_HARNESS}; by provider: {PROVIDER_HARNESS})")
    return harness


#: No turn cap, no wall-clock deadline on a calibration episode (user,
#: 2026-09-23). This is a measurement decision, not a convenience.
#:
#: A run stopped on `step_cap` or `max_tokens` wrote no deliverable and is
#: recorded VOID -- it measured nothing. Measured on a008: all ten rollouts
#: stopped on `max_tokens` with an empty deliverables directory and were
#: recorded as ten model failures at mean 0.00. That is a void wearing a
#: result's clothes. A tight cap therefore destroys data rather than measuring
#: difficulty, and it does so in the direction that makes a task look harder
#: than it is.
#:
#: Removing the caps means a hard task fails because the model could not do it,
#: not because it ran out of turns. The provider's own limits and the transport
#: read timeout remain the real bound; fault detection is unaffected.
SPEC_NO_LIMITS = True

#: Operator decision, 2026-09-24: the output allowance per request for each
#: calibration model -- 128k for Fable 5.1, 256k for GPT 6 Astra (the OpenAI
#: Responses API counts reasoning tokens against it, so the same answer needs
#: more). Applied by default to every native and file-workspace campaign, and
#: required by release. A turn cut off by the allowance wrote no deliverable
#: and is VOID, so too small an allowance destroys data rather than measuring.
SPEC_MAX_OUTPUT_TOKENS = {SPEC_PRIMARY_MODEL: 128_000, SPEC_SECONDARY_MODEL: 256_000}


def output_tokens_for(model) -> int | None:
    """The spec output allowance for a calibration model, or None if it has none."""
    bare = str(model or "").split("/")[-1].lower()
    return next((v for k, v in SPEC_MAX_OUTPUT_TOKENS.items() if k.lower() == bare), None)

#: Models this programme must NOT calibrate on, with the reason. A stale default
#: buried in three argparse declarations is how a batch gets measured on a model
#: nobody chose.
RETIRED_CALIBRATION_MODELS = {
    "gpt-5.6-sol": "superseded by GPT 6 Astra for calibration (user, 2026-09-23)",
    "claude-opus-5": "superseded by Fable 5.1 as the primary calibration model",
}


def calibration_problems(models) -> list[str]:
    """Refuse a campaign aimed at a retired calibration model.

    Matched on the bare model id, case-insensitively: `Claude-Opus-5` and
    `anthropic/CLAUDE-OPUS-5` are the same retired model, and a guard that an
    upper-case letter walks past is not a guard.
    """
    retired = {name.casefold(): reason for name, reason in RETIRED_CALIBRATION_MODELS.items()}
    issues = []
    for model in models or []:
        bare = str(model).strip().split("/")[-1].casefold()
        if bare in retired:
            issues.append(f"{model}: {retired[bare]}")
    return issues


def refuse_retired(model) -> None:
    """Raise before any campaign is launched on a retired calibration model.

    Every launcher calls this (`benchmark.harbor.run_campaign` for Terminus-2,
    `calibrate.run.campaign`, and through them the calibration screen, the
    benchmark CLI and category_batch), so a stale default cannot spend money on
    a model nobody chose and only be caught at release.
    """
    issues = calibration_problems([model])
    if issues:
        raise ValueError("retired calibration model: " + "; ".join(issues))

#: Substitutions actually used, when the spec models are unavailable. Every
#: entry lands in the batch report's deviations section. An empty mapping means
#: the batch was measured exactly as specified.
MODEL_SUBSTITUTIONS: dict[str, str] = {}


def resolve_calibration_models() -> tuple[str, str]:
    """(primary, secondary) after substitution, in spec order."""
    return (MODEL_SUBSTITUTIONS.get(SPEC_PRIMARY_MODEL, SPEC_PRIMARY_MODEL),
            MODEL_SUBSTITUTIONS.get(SPEC_SECONDARY_MODEL, SPEC_SECONDARY_MODEL))


def model_deviations() -> list[dict]:
    """What the batch report must disclose about the models measured."""
    return [{"spec_model": spec, "measured_model": actual,
             "effect": "difficulty thresholds were calibrated by Arena against "
                       f"{spec}; rates measured on {actual} are not directly "
                       "comparable to the spec's bands"}
            for spec, actual in sorted(MODEL_SUBSTITUTIONS.items())]


# ---------------------------------------------------------------------------
# Difficulty
# ---------------------------------------------------------------------------

#: Spec, "Task-Level checks": "Target pass@1 below 0.5 on both calibration
#: models for every task."
SPEC_GENERAL_DIFFICULTY_MAX = 0.5

#: Spec, same section: "A task qualifies as 'hard' when the primary calibration
#: model passes no more than 2 of 10 runs, equivalent to pass@1 <= 0.2."
SPEC_HARD_MAX = 0.2

#: Local decision (2026-09-23): the per-task admission band, 0.6. Since
#: 2026-09-25 it is REPORTED ONLY (`DIFFICULTY_GATES_RELEASE` is False): it
#: still labels a task's `meets_release_bound` and the batch report's
#: marginal band, and it rejects nothing.
RELEASE_PASS_AT_1_MAX = 0.6

#: User, 2026-09-25: "We should not have any blockers in task creation ... pass@1
#: < 0.5 should be our strict target." Difficulty never blocks a task anywhere
#: in the pipeline -- not the build, not the screen, not the calibration of
#: record, not release. It is measured, reported against the target and driven
#: down by authoring and hardening. What still blocks is VALIDITY: a grader
#: defect, a leak, void or unattributable episodes, unresolved reward hacking.
#: Arena's own batch criterion (>= 90% of tasks under 0.5) is still reported,
#: honestly, by the batch report. Named so the absence is greppable.
DIFFICULTY_GATES_RELEASE = False

#: Extreme authoring/hardening goal (2026-09-30), reported separately from
#: Arena's general target and primary-model hard label. Never an admission gate.
EXTREME_TARGET_PASS_AT_1 = 0.05
AUTHOR_TARGET_PASS_AT_1 = EXTREME_TARGET_PASS_AT_1

# Scenario-grounded hardening may lower F3 to F2. Record the measured level;
# preserving the original label must not impose a ceiling on genuine decoys.
HARDENING_PRESERVE_LEVEL = False

#: User, 2026-09-25: the STRICT TARGET -- pass@1 strictly below this on BOTH
#: calibration models. Arena's general difficulty number. Every screen and
#: calibration reports against it; hardening stops at EXTREME_TARGET_PASS_AT_1.
#: It blocks nothing (`DIFFICULTY_GATES_RELEASE`): a task
#: above it is reported with its hardening outlook, never discarded or held.
CALIBRATION_TARGET_PASS_AT_1 = SPEC_GENERAL_DIFFICULTY_MAX

#: Spec, "Batch-Level acceptance criteria", Task quality: "At least 90% of tasks
#: meet the grader sanity, solvability, and general difficulty checks." General
#: difficulty is the 0.5 above, so this is what keeps RELEASE_PASS_AT_1_MAX
#: honest.
BATCH_UNDER_SPEC_DIFFICULTY_MIN = 0.90

#: Spec, "Calibration and results": "Run each task 10 times with each model."
#: This is a floor on *valid* episodes. Voids do not count toward it, which is
#: the whole point of counting them separately. It is the calibration OF RECORD
#: -- what ships -- and it runs once a screen of the version measured cleanly
#: (never conditioned on pass@1: `DIFFICULTY_GATES_RELEASE`).
SPEC_EPISODES_PER_MODEL = 10

#: User, 2026-09-25: episodes per model in a calibration SCREEN. Two screens
#: exist and both use this width: the first calibration of a freshly authored
#: batch (run once every task in the batch is built, never per task), and the
#: re-measure after every manual hardening round. The SPEC_EPISODES_PER_MODEL
#: campaign runs only on a task whose current screen measured cleanly (valid
#: episodes, attributable failures, no unresolved reward hacking) -- never
#: conditioned on its pass@1. Five episodes is enough to report pass@1, pass@4 (k <= n) and mean
#: reward per model; it is not a precise estimate, and the report says so.
#:
#: This replaced the one-episode presolve screen, which ran after every draft on
#: one model and fed its verdict back into the design loop.
SCREEN_EPISODES_PER_MODEL = 5

#: Start a validated task's screen inside the build, as soon as it validates,
#: rather than after the whole batch (user, 2026-10-04: "why is the task
#: creation so slow"). The 2026-10-03 waves held 24 validated tasks unmeasured
#: for six hours and more behind their slowest designs. Nothing about the
#: screen changes -- same episodes, models and receipts -- only when it starts;
#: `calibrate_batch` reuses each receipt. The screen still never feeds a design.
SCREEN_AS_BUILT = True

#: Blind solves (and independent-oracle derivations) a design session may run
#: (`agent_designer.blind_solve`). 3 (config.MAX_BLIND_SOLVES_PER_SESSION) until
#: 2026-10-04, when the user asked for the bar to be met at BASELINE creation:
#: the designer now solves its draft with BOTH calibration families and hardens
#: every step either one still fully passes. GPT-6 Astra's screen episodes
#: converge on nearly the same code (analysis/ASTRA-EPISODE-MINING.md), so one
#: Astra blind solve is a strong preview of its five; each costs minutes.
BASELINE_BLIND_SOLVES = 8

#: The most trials one control run may execute at once (`controls.run`). Each
#: trial is two containers capped at the toolchain's CPUs; four keeps a Coder
#: workspace running ~24 designs inside its 32 cores.
MAX_CONTROL_WORKERS = 4

#: Spec, "Task-Level checks": "Report pass@1, pass@4, and average reward per
#: task." pass@k needs at least k episodes to mean anything.
SPEC_REPORTED_K = (1, 4)

#: Extra episodes per task per model, to replace ones lost to INFRASTRUCTURE
#: (user, 2026-09-23). Matches the retained cost model, which budgets "10
#: attempts plus 5 reruns per model" at a measured $39.52 per task.
#:
#: A void episode measured nothing -- container error, transport stall,
#: truncation, adapter stall -- so it leaves every denominator. Without a top-up
#: a campaign that loses two episodes to a flaky container reports a rate over
#: eight, or fails the ten-episode floor and wastes the eight that worked.
MAX_TOPUP_ATTEMPTS = 5

#: What may be topped up. This list is the whole safety property: only outcomes
#: that measured NOTHING are replaceable. A capability failure and a contract
#: failure are results -- re-running either until the number improves is
#: shopping for a score, and it would make every difficulty figure a lower bound
#: on how many times we tried.
TOPUP_REPLACES = ("void",)

#: Never. Named so the prohibition is greppable rather than implied.
TOPUP_NEVER_REPLACES = ("capability", "contract", "pass")


def topup_problems(before: dict, after: dict, replaced: int | None = None) -> list[str]:
    """Refuse a top-up that changed anything but the void count.

    `before` and `after` are attribution count maps over the *merged*
    measurement (the retained scored episodes plus the replacements), with
    `void` the voids of the latest launch. A top-up may only convert voids into
    real outcomes; if the number of episodes that actually produced a result
    went *down*, or a previously recorded result disappeared, the campaign was
    replaced rather than topped up. `replaced`, when given, is how many
    episodes the top-up launched: scored outcomes may grow by at most that.
    """
    issues = []
    if replaced is not None:
        grew = sum(after.get(k, 0) - before.get(k, 0) for k in TOPUP_NEVER_REPLACES)
        if grew > replaced:
            issues.append(
                f"top-up added {grew} scored episodes but launched only {replaced}; "
                "scored episodes were re-run rather than voids replaced")
    lost = [name for name in TOPUP_NEVER_REPLACES
            if after.get(name, 0) < before.get(name, 0)]
    if lost:
        issues.append(
            f"top-up lost recorded outcomes ({', '.join(sorted(lost))}); only "
            "void episodes may be replaced, because re-running a scored episode "
            "until the number improves is shopping for a result")
    if after.get("void", 0) > before.get("void", 0):
        issues.append("top-up produced more voids than it replaced; the "
                      "infrastructure fault is not transient")
    return issues


# ---------------------------------------------------------------------------
# Solvability
# ---------------------------------------------------------------------------

#: Spec, "Task-Level checks", Grader sanity: "Run the reference solution and an
#: untouched workspace through the grader. They must score full marks and zero,
#: respectively."
SPEC_REFERENCE_REWARD = 1.0
SPEC_UNTOUCHED_REWARD = 0.0

#: Spec, "Task-Level checks", Solvability: "At least one calibration run OR
#: reference trajectory scores 1.0." The reference trajectory alone satisfies
#: it.
#:
#: This constant exists to record a removal. Until 2026-09-23 `release.py`
#: required one of three blind model episodes to score reward 1.0 on every step
#: before a task could reach difficulty measurement -- effectively pass@3 == 1.0
#: as an admission requirement. That is strictly stronger than the spec and it
#: inverts the objective: the harder a task is, the less likely it survives.
#: Every task that reached calibration under it scored 0.93-1.00 mean reward,
#: because the gate had already removed everything else.
#:
#: The blind solve may still be *run* -- it is useful evidence -- but it cannot
#: gate. See `attribution.py` for what replaced it.
BLIND_SOLVE_GATES_RELEASE = False


# ---------------------------------------------------------------------------
# Failure attribution
# ---------------------------------------------------------------------------
# Spec, "Calibration and results": "Check failed runs to confirm the cause is
# model capability, not ambiguous instructions or an environment or grader bug."
#
# With the blind-solve gate gone, this is what stands between "extremely hard"
# and "quietly broken". A task where every episode fails is now admissible, so
# the burden moves onto proving the failures are the model's.

#: Every failed episode must carry one of these. `unattributed` blocks release:
#: an unclassified failure is indistinguishable from a defect.
EPISODE_VERDICTS = ("capability", "contract", "void", "unattributed")

#: Verdicts that count as evidence a model could not do the task.
CAPABILITY_VERDICTS = ("capability",)

#: Verdicts excluded from the pass@1 denominator entirely. A void measures
#: nothing; averaging it in produces a difficulty number that is partly a
#: record of our own infrastructure.
EXCLUDED_FROM_DENOMINATOR = ("void",)

#: Local decision: share of episodes that may die on a malformed or missing
#: deliverable before the output contract itself is suspect. A model that cannot
#: emit the declared schema might be failing on capability, or the schema might
#: be underspecified, and past this share the second explanation is likelier
#: than the first. Contract deaths look exactly like difficulty in a results
#: table, which is why this has a ceiling at all.
MAX_CONTRACT_FAILURE_SHARE = 0.30

#: Local decision: a check that no episode ever reached measured nothing. Its
#: weight is unreachable, not hard. `gated_out` is not `failed`.
MAX_UNREACHABLE_CHECKS = 0

#: Local decision: proof obligations a check must satisfy before an all-fail
#: result counts as a capability wall rather than a suspected defect. A hard
#: task is expected to have many all-fail checks; each one has to earn it.
ALL_FAIL_PROOF_OBLIGATIONS = (
    # an executed mutant -- a plausible wrong fix -- fails this check while the
    # reference passes it. Proves the check discriminates on behaviour rather
    # than on something stored.
    "mutant_counterfactual",
    # an independently written correct fix passes it. Proves a zero is a wrong
    # fix and not an over-fitted test.
    "alternative_implementation",
    # the check was attempted in at least one episode.
    "reachable",
    # no outstanding near-miss adjudication (a submission carrying the
    # reference patch verbatim, or >=80% of it, that still scored zero).
    "near_miss_adjudicated",
)

#: Near-miss threshold, from the existing `suspect_zeros` heuristic. Overlap at
#: or above this on a zero-scoring submission needs a human verdict before the
#: zero can be called the model's.
NEAR_MISS_OVERLAP = 0.80


# ---------------------------------------------------------------------------
# Grader shape
# ---------------------------------------------------------------------------

#: Local decision, and the reason a weight reads as a percentage. Objective
#: weights across the whole bundle -- every task, every step -- sum to exactly
#: this, so every task in a batch carries the IDENTICAL grader total (user,
#: 2026-09-25). Before it was enforced the batch ran totals from 84 to 128,
#: which made `weight: 20` worth 24% of one task and 16% of another. The build
#: normalises to it (`author.build.normalise_weights`) and the `weight-budget`
#: gate refuses a built `tests/checks.json` that does not sum to it.
WEIGHT_TOTAL = 100

#: User, 2026-09-25: minimum objective (graded) checks per task. Was 5. Every
#: task carries at least this many, and every one of them must be demonstrably
#: failable by a plausible wrong answer (`assurance.controls`) and passable by a
#: differently phrased correct one -- a check that cannot discriminate between
#: attempts is not counted as difficulty, it is a defect. Read by
#: `author.validate` (the designer's own check), the designer prompt, and the
#: `objective-count` gate on the built `tests/checks.json`.
MIN_OBJECTIVES = 8

#: Local decision: share of objective weight that must rest on semantic work --
#: a professional decision -- rather than on presence, inclusion or literal
#: matching. Floor checks may cover required fields; they cannot supply this.
MIN_SEMANTIC_WEIGHT_SHARE = 0.70


# ---------------------------------------------------------------------------
# Per-check bands: DIAGNOSTIC ONLY
# ---------------------------------------------------------------------------
# These describe where a check sat. Until 2026-09-23 they also gated release --
# 4-6 checks at 30-70% per model, at most 2 below 30%. A genuinely hard task has
# *most* checks below 30%, so the rule forbade by construction the thing the
# programme exists to produce. They now feed the calibration analysis, which
# finds dead and void checks, and they have no release power.

CORE_LO, CORE_HI, FLOOR_LO = 0.30, 0.70, 0.90
BANDS = ("stretch", "core", "transition", "floor", "unreachable")

#: Whether band composition can reject a task. Kept as a named constant so the
#: answer is greppable rather than implied by the absence of a call.
BANDS_GATE_RELEASE = False


# ---------------------------------------------------------------------------
# Faithfulness (F0-F3)
# ---------------------------------------------------------------------------
# Spec, "Monitoring metrics", Faithfulness. This taxonomy is Arena's and is
# reported per environment AND per task.
#
# Note the collision this resolves: `F1`/`F2`/`F3` were already used in this
# codebase as *stage* names (F1 extract, F2 design, F3 build). Those are
# unrelated to faithfulness levels and are being renamed; anything reading
# `F2` as a faithfulness level must come through here.

FAITHFULNESS_LEVELS = {
    "F3": {"source": "Replay",
           "definition": "Enough session content survives to build the task "
                         "directly; fixture and answer key are similar or "
                         "identical to trace files, grading from what the user "
                         "actually corrected."},
    "F2": {"source": "Scenario",
           "definition": "Original files were unavailable, so the fixture is "
                         "reconstructed or invented, but scenario, failure mode "
                         "and grading criteria still come from the trace."},
    "F1": {"source": "Pattern",
           "definition": "Fully synthetic world. Only the trace's failure mode "
                         "is preserved, and calibration runs must show models "
                         "failing the same way."},
    "F0": {"source": "Inspired",
           "definition": "Nothing recoverable from the trace; the task was "
                         "invented whole."},
}

#: Spec, "Batch-Level acceptance criteria": "Faithfulness mix - >=50% of tasks
#: at F2+".
SPEC_F2_PLUS_MIN = 0.50

#: F0 ships (user, 2026-10-05: "We can ship F0 task so there's no issue with that";
#: user, 2026-10-07: "Allow F0 tasks to be shipped there's no where mentioned we
#: cannot ship F0 tasks"). Spec 9_28 admits F0 Inspired and forbids only F-1.
SPEC_F0_ALLOWED = True

#: Local decision: the measured source-fidelity share each level requires. The
#: level is a claim about provenance; this is the arithmetic that has to back
#: it. Exact copies and mechanically verified anonymization earn full credit,
#: derived files half, authored files none.
FAITHFULNESS_FIDELITY_FLOOR = {"F3": 0.90, "F2": 0.20, "F1": 0.0, "F0": 0.0}

#: Share of traps citing an actual user correction: F3 requires it, and at
#: the F2 share it alone makes the grading criteria the trace's (POLICY.md).
FAITHFULNESS_TRACE_DERIVED_FLOOR = {"F3": 0.70, "F2": 0.30}

#: Local decision, unchanged from the prior tier scheme: below this the trace is
#: discarded rather than synthesised into an F1 task, because an F1 task must
#: still reproduce the trace's failure mode under calibration and a trace this
#: thin cannot demonstrate one.
FIXTURE_FLOOR_MIN = 0.80

#: Local decision: trace-derived share of traps, by category. Difficulty may be
#: invented; the world it happens in may not.
TRACE_DERIVED_TRAP_FLOOR = 0.50
CATEGORY_TRACE_FLOOR = {"A": 0.70, "B": 0.55, "C": 0.40, "D": 0.30}
CATEGORY_FIXTURE_FLOOR = {"A": 0.90, "B": 0.90, "C": 0.80, "D": 0.60,
                          # Build categories are target levels since 2026-09-26.
                          # F2 takes the fixture-led floor (the spec lets an F2
                          # fixture be reconstructed); F1 and F0 keep the
                          # authored-fixture floor they inherited from D. The
                          # letters stay so bundles built before then keep theirs.
                          "F3": 0.90, "F2": 0.80, "F1": 0.60, "F0": 0.60}


# ---------------------------------------------------------------------------
# Batch-level acceptance
# ---------------------------------------------------------------------------
# Spec, "Batch-Level acceptance criteria". All six, verbatim thresholds.

#: "Reviewable - 100% of tasks: harbor tasks-check pass, calibration runs +
#: provenance included."
SPEC_REVIEWABLE_MIN = 1.00

#: "Reward hacking - 0 confirmed or unresolved cases."
SPEC_MAX_REWARD_HACKING = 0

#: "Trace accounting - 100% of shared traces dispositioned (converted -> env
#: IDs, or excluded -> reason)."
SPEC_TRACE_ACCOUNTING_MIN = 1.00

#: "Multi-turn - >=10% of environments include a multi-turn task."
SPEC_MULTI_TURN_MIN = 0.10


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------
# The other half of why nothing shipped. Policy was one design attempt and a
# default repair budget of zero, so a correct review finding was a death
# sentence: 47 of 86 candidates were discarded carrying findings that named the
# exact file and the exact line to change. The corpus was being burned instead
# of the work being finished.
#
# The opposite failure is a loop that never terminates. The invariant below is
# what makes bounded repair safe: progress or stop.

#: Local decision: repair passes per candidate, across every source of findings.
#: One budget, not the three that existed before (gate, qualification,
#: difficulty) plus an ad-hoc authorization layer.
MAX_REPAIR_PASSES = 2

#: The anti-doom-loop invariant. A repair pass is admissible only if the finding
#: set strictly shrinks: every finding resolved stays resolved, and no pass may
#: return the same finding twice. A repeated finding is terminal -- it means the
#: author cannot see what the reviewer sees, and another pass will not change
#: that.
REQUIRE_MONOTONE_REPAIR = True

#: Local decision: a finding must cite a concrete artifact location to be
#: actionable. "The task is too easy" is not repairable; "checks.json line 40
#: omits the journaling ceiling" is.
REQUIRE_FINDING_CITATION = True

#: User, 2026-09-25: redesign attempts a task may receive from hardening, over
#: its WHOLE lineage (the original and every revision built from it), across
#: every `arena harden` invocation. Every call to the designer that returns a
#: revision counts -- validity repairs included -- so hardening cannot become a
#: loop of redesigning the same task again and again. Attempt 1 carries the
#: whole hardening brief and must make the task extremely hard in one go;
#: attempts 2 and 3 repair what it broke or close what its screen showed it
#: missed. Never reset: a task that has not met the target by then is reported
#: with its measurements, not redesigned again. A designer call that failed to
#: return (transport, provider) spent nothing and is not counted.
# This is the default recorded lifetime budget. An explicit --attempt-budget
# increase is audited by harden.run_lineage and never erases spent attempts.
MAX_HARDENING_ATTEMPTS = 3

#: The largest share of a task's objectives one trap's misreading may lose.
#: Measured (369 checks): what defeats frontier models is a plausible reading
#: that agrees with the right answer almost everywhere and diverges on a few
#: decisions. A "misreading" that fails most objectives is a wrong answer with
#: a trap's name on it, and proves nothing about that trap (review, 2026-09-26).
#: Read by the authoring self-audit (quality.difficulty_case_problems) and by
#: the executed controls (assurance.controls).
MISREADING_MAX_OBJECTIVE_SHARE = 0.5

#: Calibration scores and individual solver answers are never repair feedback.
#: Handing an author the answers its solvers gave is how a task gets tuned to
#: its own calibration run.
REPAIR_FEEDBACK_EXCLUDES = ("solver_answers", "per_episode_rewards")


# ---------------------------------------------------------------------------
# Reward hacking
# ---------------------------------------------------------------------------

#: Spec, "Calibration and results": "Inspect passing runs for reward hacking:
#: test access, fake output files, files dumped into graded directories, reused
#: caches, symlink tricks, and parser or exit-code exploits."
SPEC_PROBES = ("seed_zero", "golden", "empty_deliverable", "destructive", "test_tamper", "early_exit",
               "per_trap", "answer_lookup", "isolation")

#: Local decision: probes with no exemption under any circumstance.
MANDATORY_PROBES = ("seed_zero", "golden", "test_tamper", "early_exit", "answer_lookup", "isolation")

#: Probes that model an ATTACK. Full credit on any of these is a confirmed
#: reward-hacking case and is what the spec's "0 confirmed or unresolved cases"
#: counts. `test_tamper` plants skip-everything configs, shadow packages and a
#: forged reward on the untouched fixture; `answer_lookup` scans the agent's
#: environment for the fix.
HACK_PROBES = ("empty_deliverable", "destructive", "test_tamper", "early_exit", "answer_lookup")

#: Probes that model LEGITIMATE work and are *supposed* to earn full credit:
#: the reference and an independently written alternative fix.
EXPECT_FULL_CREDIT = ("golden", "alternative")

#: The untouched fixture and the two destructive variants must score zero.
EXPECT_ZERO = ("seed_zero", "empty_deliverable", "destructive")

#: Diagnostic rather than pass/fail: reported, read, not thresholded.
DIAGNOSTIC_PROBES = ("determinism",)

#: Local decision: a probe scoring above this warns even when it fails outright.
#: A hack that earns most of the reward is still a hack.
PROBE_WARN_ABOVE = 0.5
PROBE_MAX = 1.0


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

#: Local decision. Fitted to the first 500-session corpus and absolute rather
#: than recomputed per corpus, so a category means the same thing across dumps
#: and a richer dump correctly produces a larger category A. Re-derive only
#: deliberately: recomputing per corpus hides the improvement you want to see.
#:
#: These order *candidates* -- which trace to attempt first. They are an input
#: property of the trace and they are not what gets delivered. See below.
CATEGORY_BOUNDARIES = (0.53, 0.40, 0.28)
CATEGORIES = ("A", "B", "C", "D")
SOURCE_TIER_NAMES = {"A": "rich", "B": "strong", "C": "fixture-led", "D": "thin"}

#: User, 2026-09-26: a seed's build category IS the Arena faithfulness level it
#: can reach -- classified from the trace before authoring, in the spec's own
#: terms (F3 Replay / F2 Scenario / F1 Pattern / F0 Inspired). It is a TARGET:
#: the level measured from the built bundle (`ledger/levels.py`) stays the one
#: delivered. No level is a blocker -- an F0-target seed is still authored and
#: delivered at the level it measures.
TARGET_LEVELS = ("F3", "F2", "F1", "F0")

#: Working-tree folder per target level. Distinct from DELIVERY_DIRS on purpose:
#: those name a MEASURED level, these the level a seed was selected to aim at.
TARGET_LEVEL_DIRS = {"F3": "target-F3-replay", "F2": "target-F2-scenario",
                     "F1": "target-F1-pattern", "F0": "target-F0-inspired"}

#: The retired A-D build categories, as the level each aimed at (the trap
#: policies then said: A F3; B, C and D F2). Read from ledgers and trees built
#: before 2026-09-26 only; nothing new is classified into a letter. Those trees
#: are filed under SOURCE_TIER_NAMES (rich/strong/fixture-led/thin).
LEGACY_CATEGORY_LEVEL = {"A": "F3", "B": "F2", "C": "F2", "D": "F2"}


def target_level(category: str | None) -> str | None:
    """A build category as its target level: F-levels as-is, legacy letters mapped."""
    if category in TARGET_LEVELS:
        return category
    return LEGACY_CATEGORY_LEVEL.get(category)


# ---------------------------------------------------------------------------
# Delivery layout
# ---------------------------------------------------------------------------

#: Delivered environments are filed under Arena's faithfulness levels, not under
#: our source tiers. The spec defines exactly one environment taxonomy --
#: F3 Replay / F2 Scenario / F1 Pattern / F0 Inspired -- and keys two batch
#: criteria and two of three yield metrics on it. `rich/strong/fixture-led/thin`
#: were ours, described the source trace rather than the delivered task, and
#: told Arena nothing it asked for.
#:
#: The folder is the faithfulness claim, so the batch's mix is legible from a
#: directory listing.
DELIVERY_DIRS = {"F3": "F3-replay", "F2": "F2-scenario", "F1": "F1-pattern", "F0": "F0-inspired"}

#: Source tiers still exist and are still recorded per task -- they say how much
#: material the author had to work with, which is what makes a low-fidelity
#: outcome forgivable or not. They are metadata, not a directory.
RECORD_SOURCE_TIER = True


# ---------------------------------------------------------------------------
# Coding tasks: executed graders
# ---------------------------------------------------------------------------
# The coding pipeline grades by executing hidden tests in a separate offline
# verifier. Everything the parent proved about a document grader by evaluating
# expressions is proved here by running the grader over prepared trees
# (`author/controls.py`). These constants bound those executions.

#: How many times the reference solution is graded to call the grader
#: deterministic. A flaky hidden test is a grader defect: it turns a model's
#: correct fix into a zero the model did not earn. Three identical runs is a
#: bound on flakiness, not a proof of its absence (the receipt says so).
DETERMINISM_RUNS = 3

#: Every trap is proved by a mutant -- a plausible wrong fix -- that must fail
#: the checks the trap drives, pass at least one objective (a mutant that
#: passes nothing is the untouched fixture wearing a trap's name) and never
#: score full credit. The share of objectives it loses is REPORTED against
#: MISREADING_MAX_OBJECTIVE_SHARE as an advisory: in code a single wrong premise
#: legitimately cascades through many tests, and the parent's 50% rule was
#: measured on document tasks, not here. Validity is decided by the exact
#: declared failure set; the share is what hardening reads.
MUTANT_SHARE_IS_ADVISORY = True

#: Objectives in these bands must each be failed by at least one executed
#: mutant, or nothing shows they discriminate a plausible wrong fix. Floor
#: objectives (format, presence) are exempt: they are meant to pass.
MUTANT_COVERAGE_BANDS = ("core", "stretch")

#: Agent-side files the test_tamper control plants together with the reference
#: fix; the grader's objectives must read exactly as they did without them.
TEST_TAMPER_MUST_NOT_MOVE_OBJECTIVES = True


# ---------------------------------------------------------------------------
# Coding difficulty floors (user, 2026-09-27: "extremely hard tasks with
# genuine traps"). What the prompt urges, the validator refuses without.
# ---------------------------------------------------------------------------

#: Traps a task must carry. Count is not difficulty (measured on the parent
#: programme), so the floor is low and the SHAPE rules below carry the weight.
MIN_TRAPS_PER_TASK = 3

#: Distinct trap types among them: same-type traps are correlated, a model
#: that sees the principle once applies it everywhere.
MIN_TRAP_TYPES = 3

#: Trap types that change a premise the model has already acted on -- the
#: a design hypothesis until measured. At least MIN_PREMISE_TRAPS of a task's traps
#: must be of these types and drive an objective.
PREMISE_TRAP_TYPES = ("symptom_patch", "chained_defect", "spec_misread", "decoy_location",
                      "premise_revision", "stale_authority", "revert_residue", "semantic_decoy",
                      "exception_interaction", "representation_collision", "architectural_carry")
MIN_PREMISE_TRAPS = 2

#: At least this many traps must route two or more objectives through one
#: wrong premise. This diagnoses propagation; one failed objective already
#: defeats full success, and duplicating a consequence does not lower pass@1.
MIN_COUPLED_TRAPS = 1

#: Every step is measured on its own (user, 2026-10-02 and 2026-10-03: "every
#: step extremely hard ... pass@1 less than or equal to 0.5 for both the
#: models"). A step fully passed by more than this share of valid episodes on
#: EITHER calibration model is too easy, whatever the whole-task pass@1: the
#: 6-step parser lineage measured 0/5 on both models while steps 1, 2 and 5
#: passed 5/5 on Astra. Hardening keeps a lineage open while any step is above
#: it; the screen reports it. Never a release gate (`DIFFICULTY_GATES_RELEASE`).
STEP_PASS_RATE_MAX = 0.5

#: Ship-time sweep (`shipcheck`, user 2026-10-07: "if these kind of issues are
#: found while shipping a task then lets fix them and then ship"). A guardrail
#: failed by more than this share of one model's scored episodes is presumed
#: over-strict until a human adjudicates it: verification-library's
#: g_existing_behaviour zeroed 10/10 Fable episodes over a zero-argument
#: `int()` no sentence publishes, while every objective scored 0.68-1.0.
SHIP_GUARDRAIL_FAIL_SHARE_MAX = 0.6

#: Learning signal (Arena spec, Oct 12, the highlighted paragraphs; Arena review of the
#: delivered batch: "about 70% of the points are checks that every model passes ... good
#: and bad solutions both score about 0.72, and there is nothing to learn from"). A
#: measured task (pilot or the 10 x 2 of record) carries a usable signal only when:
#:   - at most this share of objective weight sits in checks EVERY valid episode of
#:     BOTH models passed (free points);
SIGNAL_FREE_WEIGHT_MAX = 0.5
#:   - at least this many INDEPENDENT hard decisions exist: objective checks failed by
#:     at least SIGNAL_HARD_FAIL_RATE of one model's valid episodes, counted once per
#:     primary trap (checks that turn on the same decision count as one);
SIGNAL_MIN_INDEPENDENT_HARD = 3
SIGNAL_HARD_FAIL_RATE = 0.3
#: Each model's valid-episode rewards should not be flat ("all 10 runs scored the same ... no mix"):
#: a standard deviation under this is REPORTED and aimed at, never a refusal -- a model can return
#: byte-identical solutions every run, which no grader design can spread (validation session,
#: 2026-10-13, c-011-quant).
SIGNAL_REWARD_SPREAD_MIN = 0.05
#: Decisive-trap variety across a batch ("chapter, poster, viva, redirect-map and wiring
#: all reduce to the same clock puzzle"): a trap type may be the decisive one of at most
#: this share of a batch's measured-hard tasks; designers are told which types are over it.
SIGNAL_DECISIVE_TYPE_MAX_SHARE = 0.25

#: User, 2026-10-03: "most of the tasks single step ... only 10% of the tasks
#: need to be multi-step ... all the steps are extremely hardened and meet the
#: criteria of pass@1<=0.5". A batch designates this share of its seeds -- the
#: ones whose traces carry a later requirement change -- as multi-step; every
#: other task has exactly ONE step. Every step of either kind is held to
#: STEP_PASS_RATE_MAX on both models. Assigned over the whole ranking, so every
#: Coder shard of a batch agrees (`orchestrate.assign_step_budgets`).
MULTI_STEP_SHARE = 0.10
#: Steps a designated multi-step task may have; validation refuses more.
#: User, 2026-10-12: "make the multi-step tasks extremely dense and multiple
#: turns at least 3 turns". Spec Appendix A (Oct 8): a multi-turn task has
#: sequential user turns in one environment, each turn substantive and built
#: on the state the previous one created, each graded on its own rubric.
MULTI_STEP_MAX_STEPS = 4
#: Fewest steps a designated multi-turn task may have (validation refuses fewer;
#: `orchestrate.assign_step_budgets` designates only traces with enough later
#: user requirement changes to supply that many real turns).
MULTI_STEP_MIN_STEPS = 3
#: Per-turn density floors for a multi-turn task (Appendix A: "each turn
#: involves substantive work. An easy setup turn followed by the only real
#: task does not count"). Objectives counted per step; traps counted by the
#: step of the checks they drive.
MIN_OBJECTIVES_PER_STEP = 4
MIN_TRAPS_PER_STEP = 2

#: Trap research (user, 2026-10-17: "a minimum goal of adding at least 6 extremely
#: hard traps relevant to source traces using web search and research + additional
#: traps from the library"; "diverse failure modes across tasks"). A fresh design
#: plants at least this many traps carrying `research` from `research_traps`
#: (author/trap_research.py) before creation counts as complete. A creation
#: component, never a validation refusal; waived only when research is unavailable.
MIN_RESEARCHED_TRAPS = 6
#: research_traps calls per design session, and the server-side web searches and
#: fetches one call may spend.
TRAP_RESEARCH_MAX_CALLS = 14
TRAP_RESEARCH_MAX_SEARCHES = 6
TRAP_RESEARCH_MAX_FETCHES = 3
#: Consecutive failed research calls, with none succeeding, after which research
#: counts as unavailable for the session and the floor above is waived.
TRAP_RESEARCH_UNAVAILABLE_AFTER = 3
#: Failure-mode diversity (advisory): no trap type above this share of a task's
#: traps; a type at or above DIVERSITY_OVERUSED_SHARE of the batch is named as
#: over-used in every new design's opening message.
TRAP_TYPE_MAX_SHARE = 0.34
DIVERSITY_OVERUSED_SHARE = 0.15
#: Traps that BITE (user, 2026-10-17: "ensure more traps bite the models ... most of them
#: should defeat the model"; author/bites.py). The four ship15 releases planted 23-37 traps
#: and 5-8 bit each model. Advisory targets for THE BITE AUDIT, read from blind solves at
#: creation and from the screen re-grade in hidden-only rounds: the share of objective-
#: driving traps a solver fails, the traps each family fails, and the types they span.
TRAP_BITE_SHARE_TARGET = 0.6
MIN_BITING_TRAPS_PER_FAMILY = 6
MIN_BITING_TRAP_TYPES = 4
#: The BINDING hardness bar at creation (user, 2026-10-17: "ensure the tasks are built extremely
#: hard from the authoring phase itself"; author/difficulty_gate.py). Of 31 designs that finished
#: creation that evening Astra's blind solve fully passed 25, and five of the first six screens were
#: 1.0 / 1.0. A creation component, not a validation refusal: the session keeps working within its
#: budget until each family's current graded blind solve fails a step, at least this many determinate
#: traps bite each family, spanning at least this many types -- or waives itself (recorded) when
#: fewer than DIFFICULTY_GATE_RESERVE_STEPS turns remain, the blind-solve budget is spent or the
#: sandbox is unavailable. Hidden-only rounds: at most FAST_GATE_MAX_FULL_PASSES of the captured
#: episodes may fully pass a step, and the same number of traps must fail 2+ episodes per model.
DIFFICULTY_GATE_AT_CREATION = True
MIN_BINDING_BITES_PER_FAMILY = 3
MIN_BINDING_BITING_TYPES = 3
FAST_GATE_MAX_FULL_PASSES = 1
#: Decisions that must defeat BOTH families (user, 2026-10-18: "make most of the trap modes bite both the
#: models"; W13: every 0/20 release rested on at least one decision both families failed 10/10).
MIN_SHARED_BITES = 2
DIFFICULTY_GATE_RESERVE_STEPS = 30

#: The lever doctrine (`author/levers.py`, user 2026-10-03: "introduce as many
#: difficulty levers ... relevant for the traces"). Advisory targets the
#: designer, check_draft, the reviewer and hardening read; none of them blocks.
#: Distinct catalogued levers a task's lever plan should use, so that one
#: insight cannot settle several decisions.
MIN_LEVER_MECHANISMS = 5

#: Interlocks -- two or more levers acting on ONE graded decision, aimed so
#: that resolving all but one still fails -- each step should carry. Single
#: mechanisms measured dead: the sibling lever lab solved all 28 of its
#: single and combined micro-tasks 10/10.
MIN_INTERLOCKS_PER_STEP = 1

#: How many trace-relevant levers the opening message and the hardening brief
#: list, ranked by the evidence `levers.relevant` finds in the fixture,
#: the classification and the corrections.
RELEVANT_LEVERS_SHOWN = 12

#: Census 2026-10-03: only 3 of 23 gated bundles validated (median 2 repair
#: passes), and ~150 findings were deterministic -- mutant fails != declared,
#: patches that do not apply, stale exports, static validation. submit_draft
#: therefore runs ONE deterministic preflight (static validation and, with
#: Docker, the executed controls) when every component is present and hands its
#: problems back inside the session. Never a refusal: the candidate is saved,
#: and the next submit_draft finishes whatever the preflight said.
SUBMIT_PREFLIGHT = True
#: Steps that must remain for the designer to act on preflight findings; with
#: fewer left, submission finishes without it.
SUBMIT_PREFLIGHT_RESERVE_STEPS = 12

#: The difficulty self-audit names the mutant that IS a strong model's
#: first-pass patch (the red-team result); the controls execute it and it
#: must score below full credit. A task whose first-pass patch scores 1.0 is
#: not hard, whatever the prose says.
FIRST_TRY_MUTANT_REQUIRED = True

#: Instructions are the user's, not an assistant's (user, 2026-09-27): the
#: register of the trace, vague about how and where, precise about what
#: counts. Markdown structure and assistant idioms are refused by
#: `validate.voice_problems`; the reviewer rejects an assistant-sounding
#: instruction as a validity defect.
INSTRUCTION_VOICE_ENFORCED = True
INSTRUCTION_MAX_BULLET_LINES = 3
INSTRUCTION_ASSISTANT_IDIOMS = (
    "ensure that", "please note", "feel free", "let me know", "in order to", "additionally,",
    "furthermore,", "acceptance criteria", "requirements:", "deliverables:", "your task is",
    "you are tasked", "implement the following", "the following requirements", "comprehensive",
    "robust", "leverage", "seamless", "as an ai", "step 1", "step 2", "note:", "important:",
)


#: Grading time added on top of the sum of one step's per-check timeouts, written as
#: `[steps.verifier] timeout_sec`. Harbor gives a [[steps]] step that names no timeout its own 600 s
#: default -- the task-level [verifier] value is not consulted -- and a submission that hangs every
#: check outlasts that, so the trial errors instead of scoring 0 (read in Harbor 0.20 @ cfc1bcb and
#: 0.22, 2026-10-09). Read by author/build.py and shipcheck.step_timeouts.
STEP_VERIFIER_MARGIN_S = 600


#: Coverage-critic runs a hidden-only hardening session may spend on its own staged draft (the tool
#: `coverage_critic` in author/agent_designer.py FAST_TOOLS). The final version's critic gaps block shipping since
#: 2026-10-09 (Arena feedback), so a round closes them before it submits instead of meeting a new batch at every final.
#: Raised 3 -> 5 the same day: the white-box critic still met ~5 new gaps at every final after three in-round runs.
DESIGNER_CRITIC_RUNS = 5
#: release_check runs per release-repair session (pipeline/release_blockers.py): each re-executes the
#: release's recorded critic proposals on the draft (minutes each).
RELEASE_CHECK_RUNS = 4

#: How the coverage critic gates a final pass (user, 2026-10-09 ~18:30 IST, "Closed-and-verified"). The critic
#: is white-box and found ~5 new gaps on nearly every fresh run (c-031, c-041, c-068, c-072), so "a fresh run
#: finds zero" never converged: about 140 finals that day, none READY. Under "closed_and_verified" a final whose
#: lineage already has a searching critic run re-executes THAT run's executed proposals on the new version
#: (coverage_critic.ship_critic): every former gap must score below 1.0, and a proposal that failed there and
#: scores 1.0 now is a regression and blocks too. A lineage with no usable searching run still gets a fresh one.
#: "fresh" restores the earlier rule.
CRITIC_SHIP_RULE = "closed_and_verified"

#: Which executed critic gaps block shipping (user, 2026-10-09 ~19:00 IST, "Block only realistic gaps"). Of 34
#: sampled gaps about 24 were realistic mistakes (host.replace('www.', ''), an unanchored youtube.com prefix, a
#: dropped --seed), about 7 contrived to slip past the suites (a warped r**1.25 draw, a >500-row special path),
#: 2-3 on a contested reading. Under "realistic_only" both calibration families judge each gap
#: (coverage_critic.judge_gap): a gap is advisory -- recorded, listed under the delivered package's untested
#: surfaces, never blocking -- when any family says the instruction does not clearly settle it, or every family
#: calls it contrived. A judge that errs demotes nothing. "all" makes every executed gap block again.
CRITIC_GAP_RULE = "realistic_only"

#: Arena's evaluation harness (user, 2026-10-12: "Let's ensure that we have a reproducibility check before we ship a
#: task. It should be reproducible at Arena's end."). Arena builds and grades every task on Harbor 0.20.0 pinned to
#: commit cfc1bcb (branch harbor-miles-v0.20.0, harbor-framework/harbor), x86_64, 2 vCPU / 4096 MB by default, and a
#: task passes only when the oracle scores 1.0 and a NOP agent 0.0. `arena_repro` runs exactly that harness (installed
#: from the commit's own lockfile, so its e2b SDK is 2.25.0) on every delivered zip and writes a receipt bound to the
#: zip's sha256; publishing refuses a zip without a passing receipt. Arena reproduces our Docker-built packages (user,
#: 2026-10-12), so the harness runs with `--env docker`.
ARENA_HARBOR_VERSION = "0.20.0"
ARENA_HARBOR_COMMIT = "cfc1bcb9480f123b0a702a1428ae3f79ecedb0ca"
ARENA_REPRO_RESOURCES = {"cpus": 2, "memory_mb": 4096}
#: Where the pinned harness lives: a Coder workspace with Docker, in its persistent home (a workspace stop resets the
#: root disk and /tmp; the earlier /tmp install was lost on 2026-10-11).
ARENA_REPRO_WORKSPACE = "arena-coding-3"
#: Ship gate: True refuses to publish a zip whose receipt is missing, stale (zip bytes changed) or not oracle 1.0 /
#: NOP 0.0 on the pinned harness at the pinned resources.
ARENA_REPRO_REQUIRED = True
