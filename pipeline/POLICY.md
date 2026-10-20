# Acceptance policy — coding tasks

**Authoritative.** Where this file and any other document disagree about a
threshold, this one is right and the other is stale. Every number here is a
named constant in [`pipeline/policy.py`](policy.py) — prose does not hold
numbers, it names the constant that does. `EveryPolicyConstantIsRead` fails the
build on a constant nothing reads.

What is being accepted: a Harbor 0.22 package (`schema_version = "1.4"`,
`[[steps]]`, separate offline verifier) in which the agent starts inside a
repository lifted from an Arena session (`/app/repo`) and the verifier executes
HIDDEN TESTS against what the agent left behind. The one truth source is an
executed test. Nothing here scores a document, a wording or a layout.

## The five phases

```
1 select    dump/index.py -> select/score.py -> select/priority.py
            -> select/classify.py -> select/categories.py -> select/shortlist.py
2 author    author/agent_designer.py with a Docker sandbox over the fixture
3 grade     gates/code.py (14 gates) + the semantic review
   -- barrier: the whole requested batch is built --
4 calibrate screen: SCREEN_EPISODES_PER_MODEL (5) x 2 models through Harbor
            THE PIPELINE STOPS HERE
5 harden    manual only; default lifetime budget MAX_HARDENING_ATTEMPTS (3)
```

### 1. Select

A seed is killed only for a property nothing can author around
(`select/score.kill_filters`):

| floor | constant | reason code |
|---|---|---|
| one-turn seed | `config.MIN_TURNS` | `kill_filter` |
| aesthetic/meta-only session | `config.UNGRADABLE_ONLY` | `kill_filter` |
| dominant language is engine-bound | `config.ENGINE_LANGUAGES` via `runtime_class` | `no_runtime` |
| no source tree to lift | `config.MIN_SOURCE_FILES`, `MIN_SOURCE_BYTES`, `BUILDABLE_RUNTIMES` | `no_source_fixture` |

Vendored material (`config.VENDORED_PARTS`) is excluded from every count.
Authored fixtures (target F1/F0) are admitted only with
`ARENA_AUTHORED_FIXTURES=1`; fixture-led seeds (a repository with no
correction) with `ARENA_FIXTURE_LED` (default on).

The classifier (`select/classify.py`) survives a seed only when ALL of:
`gradable`, `coding_request`, `truth_source == "executable_tests"`
(`config.CODING_TRUTH`), `runtime_feasible`, a named `reproduction`,
`deliverable_kind in {code, mixed}`, `runtime_class in BUILDABLE_RUNTIMES`, and
a route: two usable corrections, or one whose `defect_pairs` locate a root
cause, or a `repo_task` (a testable gap already in the tree) with two verified
evidence reads. Every gradable verdict must cite inspected user text AND
inspected source material; a verdict with a non-executable truth source is
refused and sent back.

Tiers (`select/priority.py`) order candidates; boundaries
`policy.CATEGORY_BOUNDARIES` (0.53 / 0.40 / 0.28) are absolute. Target levels
(`select/categories.py`) file the working tree under
`policy.TARGET_LEVEL_DIRS`; the level that ships is measured after the build.

### 2. Author

`author/agent_designer.py` designs with the whole trace, the working tree at
every turn, and an optional Docker sandbox over the fixture. Submission retains
the candidate without running static validation, controls or models. Incomplete
drafts are retained too, and the author continues writing their missing components
within the original session budget. `creation_status` reports component presence
without thresholds, acceptance verdicts, Docker or models; all fixture files need
provenance records and every step needs instruction, checks, reference and alternative
patches. Traps, mutants and self-audit entries must be written too. A component-complete
draft is not yet a validated task. Budget exhaustion retains unfinished work with
an explicit creation receipt, rather than reporting it complete.
`check_draft`, `run_controls`, `pre_review`, `blind_solve`,
`probe_patch` and `difficulty_profile` guide creation and never gate submission.
`reject_seed` submits the retained candidate with a source-capacity finding;
phase 3 rechecks its evidence before deciding source rejection. It does not
reject the candidate during creation.
The following controls are authoritative only during phase 3 validation:

| control | must | constant |
|---|---|---|
| nop (untouched fixture) | reward `SPEC_UNTOUCHED_REWARD` (0), every guardrail passes, every objective fails | `EXPECT_ZERO` |
| golden (reference patch) | reward `SPEC_REFERENCE_REWARD` (1.0), `DETERMINISM_RUNS` (3) identical runs | `EXPECT_FULL_CREDIT` |
| alternative (independent fix) | reward 1.0 | `EXPECT_FULL_CREDIT` |
| every mutant | fails exactly the checks its trap declares, passes ≥ 1 objective, never 1.0 | `MUTANT_COVERAGE_BANDS` |
| mutant objective share | ≤ `MISREADING_MAX_OBJECTIVE_SHARE` (0.5) of objectives | advisory: `MUTANT_SHARE_IS_ADVISORY` |
| empty repository | 0 | `EXPECT_ZERO` |
| destructive (sources deleted) | 0 | `EXPECT_ZERO` |
| test_tamper on the UNTOUCHED fixture | 0, no objective passes | `TEST_TAMPER_MUST_NOT_MOVE_OBJECTIVES` |
| early_exit (every source file exits 0 before running, untouched fixture) | 0, no objective passes; compiled toolchains record it as an untested surface | `MANDATORY_PROBES` |
| neutral_additions (the reference on every step plus a local conftest.py, one passing test in the repo's runner, NOTES.md, an empty `__pycache__/`) | every check as the reference reads it; a moved check is an error, a tree-guard's published file budget an advisory | `NEUTRAL_ADDITIONS_MUST_MATCH_REFERENCE`, `NEUTRAL_ADDITIONS_EXEMPT_RUNNERS` |
| shadow-guard modules (static, `grader.data`) | never a module whose last path component is `conftest` | `SHADOW_GUARD_REFUSED_MODULES` |

The grader is data: `tests/checks.json` names runner, selected tests, weight,
band and the verbatim instruction sentence each check enforces
(`rule_quote`); `tests/grade.py` is the one shipped runtime. A test-runner
check passes only when the runner itself reports at least one completed test
and no failure, error or skip (`grade.test_counts`); an exit status alone is
never evidence. For pytest and unittest the selection, the report path and a
per-run nonce reach a grader-owned bootstrap over stdin, and the report must
echo the nonce and cover every selected test; the `early_exit` control forges
every reachable report before exiting and must still score 0. In-process
introspection of the runner's configuration remains a declared residual; the
trajectory scan flags source edits that add exit or report markers. Objective weights
sum to `WEIGHT_TOTAL` (100) in every task; at least `MIN_OBJECTIVES` (8)
objectives and at least one guardrail per task. There is nowhere to type an
expected value. Every step carries a tree-guard guardrail (a planted file must
never shadow the module under test) and every mutant changes behaviour, not
only comments (`checks.check_problems`, `validate._changes_behaviour`; both from
the first real task's review, 2026-09-27).

Difficulty floors the validator refuses without (`validate.trap_shape_problems`,
`quality.difficulty_case_problems`): `MIN_TRAPS_PER_TASK` (3) traps of
`MIN_TRAP_TYPES` (3) distinct types, `MIN_PREMISE_TRAPS` (2) objective-driving
premise traps (`PREMISE_TRAP_TYPES`: symptom_patch, chained_defect,
spec_misread, decoy_location, premise_revision, stale_authority,
revert_residue), `MIN_COUPLED_TRAPS` (1) trap routing two or more objectives,
and `difficulty_case.first_try_mutant` -- the strong model's first-pass patch,
declared as a mutant the controls execute and refuse full credit
(`FIRST_TRY_MUTANT_REQUIRED`). The floors are floors: a task that merely clears
them is not hard; only the screen says so.

Instructions are the user's, not an assistant's (`INSTRUCTION_VOICE_ENFORCED`):
the trace's register and words, vague about how and where, precise about what
counts. `validate.voice_problems` refuses markdown headings, more than
`INSTRUCTION_MAX_BULLET_LINES` (3) bullet lines, tables and
`INSTRUCTION_ASSISTANT_IDIOMS`; the reviewer rejects an assistant-sounding
instruction as a validity defect.

Difficulty is authored on the first pass, never gated: the strict target is
pass@1 < `SPEC_GENERAL_DIFFICULTY_MAX` (0.5) on both models, while authoring and
hardening aim at `EXTREME_TARGET_PASS_AT_1` (0.05) on each model.
`SPEC_HARD_MAX` (0.2) retains its original primary-model meaning.
`DIFFICULTY_GATES_RELEASE = False`: nothing rejects a
task for being easy.

Optional causal hypotheses in `quality.difficulty_case.hypotheses` identify a
tempting rule, correct rule, distinguishing case, trap/check references and
interactions. Absence is fine; validation checks the references when supplied.
Mechanism and mutant diversity feedback is advisory, never measured hardness.
Trap types include semantic decoys, exception interactions, representation
collisions, temporal state, distributed invariants and architectural carry.

### 3. Grade

Export failures are retained as `verification/submission-validation.json` in
phase 3; they are not designer failures. `gates/code.py`, validity only:
`bundle.structure`, `bundle.ledger`,
`grader.runtime`, `grader.data`, `toolchain.pinned`, `hidden.disjoint`,
`leak.sweep`, `solution.derived`, `trap.faithful`, `trap.beatable`,
`quality.controls`, `quality.spec`, `hacking.resistance`,
`contract.published`. `SKIPPED` and `ERROR` are never green
(`run.verdict`). The semantic review blocks on validity and novelty; its
difficulty judgments are `difficulty_advisories`.

### 4. Calibrate

`SCREEN_EPISODES_PER_MODEL` (5) episodes per model on `SPEC_PRIMARY_MODEL`
(Fable 5.1 in Claude Code) and `SPEC_SECONDARY_MODEL` (GPT 6 Astra in Codex),
`SPEC_CALIBRATION_EFFORT` (high), `SPEC_NO_LIMITS`, harnesses pinned at
`HARNESS_VERSIONS`. `RETIRED_CALIBRATION_MODELS` are refused at launch.
Harness, effort and limits are part of the measurement and are reported with
every rate. pass@k is reported for `SPEC_REPORTED_K` (1, 4).

## Task acceptance

An exact task version is accepted only by `release.evaluate`. Beyond the gate
chain it requires: a reproducible export (`assurance.generated_problems` is
empty), a controls receipt for this identity, probes with every
`MANDATORY_PROBES` present and every `HACK_PROBES` clean, no unresolved
privacy finding, a verified provenance ledger, a semantic review by a model
other than the author, and the calibration of record.

## Failure attribution

Every episode is one of `EPISODE_VERDICTS`: `capability`, `contract`, `void`,
`unattributed`. Voids leave every denominator (`EXCLUDED_FROM_DENOMINATOR`);
an unattributed episode blocks. Contract failures (the grader's own
`contract_errors`, or a check tagged `contract: true` / floor+present failing)
may not exceed `MAX_CONTRACT_FAILURE_SHARE` (0.30) of episodes. No check may be
unreachable in every episode (`MAX_UNREACHABLE_CHECKS` = 0).

A check no episode passed is admissible only with `ALL_FAIL_PROOF_OBLIGATIONS`:
`mutant_counterfactual` (an executed mutant failed it while the reference
passed), `alternative_implementation` (the executed alternative passed it),
`reachable`, `near_miss_adjudicated` (no zero-scoring submission carried
≥ `NEAR_MISS_OVERLAP` of the reference patch without a human verdict).

Reward hacking: every retained trajectory is scanned (`attribution.
trajectory_hacks`) for test access (`/tests`, `/app/tests`, checks.json,
grade.py, rubric.json, reward files), graded writes, symlinks into graded
directories, harness tampering (killing the verifier, .pth/sitecustomize,
conftest skips, trapping exit), and undeclared output. The agent's working
directory is the repository, so a relative `test.sh` is its own file; an
unknown directory after a subshell is read as written. Every hit blocks
release until adjudicated (`SPEC_MAX_REWARD_HACKING` = 0).

## Batch acceptance

`SPEC_REVIEWABLE_MIN` (100%) of delivered tasks reviewable, `SPEC_TRACE_
ACCOUNTING_MIN` (100%) of shared sessions dispositioned with derived reason
codes (`accounting.py`: `kill_filter`, `no_runtime`, `no_source_fixture`,
`structural_assertion`, `requires_human_judgement`, `none`,
`qualified_not_built`, `below_classification_depth`), at least
`SPEC_MULTI_TURN_MIN` (10%) multi-step tasks, and `BATCH_UNDER_SPEC_
DIFFICULTY_MIN` (90%) of tasks under the difficulty ceiling — reported, never
blocking (`DIFFICULTY_GATES_RELEASE = False`).

## Faithfulness (F0–F3)

Measured after the build from `provenance/ledger.json` (`ledger/faithfulness.py`,
schema `ledger/4`): every fixture file is `lifted` (exact trace bytes),
`anonymized` (a verified typed privacy transformation), `derived` (a diff
against a trace revision) or `authored` (written by the designer, recorded in
`authoring/extraction/fixture-authored.json`). Vendored paths never enter the
ledger. Levels: `FAITHFULNESS_FIDELITY_FLOOR` F3 ≥ 0.90, F2 ≥ 0.20;
`FAITHFULNESS_TRACE_DERIVED_FLOOR` F3 ≥ 0.70, F2 ≥ 0.30 of traps trace-derived.
`DELIVERY_DIRS` names F3/F2/F1; F0 is never delivered.

## Repair

`MAX_REPAIR_PASSES` (2) within a grading stage; the finding set must strictly
shrink (`REQUIRE_MONOTONE_REPAIR`); every finding cites its evidence
(`REQUIRE_FINDING_CITATION`); a repeated finding is terminal.

## Hardening

Manual only. The default is `MAX_HARDENING_ATTEMPTS` (3) redesigns over the
whole lineage; an explicit budget increase retains spend. Attempt 1 carries
the full difficulty brief. Each valid revision is screened 5 x 2; hardening
stops on target observed, budget spent, an informative comparison showing
regression on every model, or a repeated validity finding. Small unchanged
screens are inconclusive. Its observed stop
target is `EXTREME_TARGET_PASS_AT_1`, not the general 0.5 target. Legacy stops
are reconsidered without resetting spent attempts.

`HARDENING_PRESERVE_LEVEL = False`: scenario-grounded authoring/hardening has
no arbitrary authored trap or byte ceiling; F3 may become F2. Validation
retains real correction traps, truthful byte provenance and at least F2 for
F2/F3 sources. Preserve-level budget calculation remains available explicitly.
One genuinely failed objective suffices for a new trap; splitting a behaviour
into duplicate weighted objectives does not improve binary pass@1.

Blind solving uses one persistent workspace across all steps and replays
incremental patches and each step's deliverable snapshot through the shipped
verifier, including earlier captures.
Its per-step turn budget and provider cutoffs are recorded. A truncated probe
has diagnostic grades but no capability reward. Native measurement remains
unlimited and separate.

Screens report Wilson intervals and exact one-sided success-rate upper bounds.
An extreme observed rate is provisional; screening/adaptive search cannot
confirm its own candidate. Confirmation requires fresh, predeclared fixed
cohorts on a frozen candidate. With two models and zero successes, 72 valid
episodes per model put both success-rate bounds below 5% at simultaneous 95%
confidence (Bonferroni). Voids never count as failures.

`arena research` implements separate creation, validation, measurement and
reporting. Factorial variants keep identical public contracts and check schemas;
only declared public decoys and hidden interaction inputs differ on their
respective axes. Supplied public hypothesis citations and independent oracle
provenance are checked during validation. Adoption requires cited agent behavior;
unadjudicated episodes stay unknown. All research results are advisory for task
acceptance.

The confirmation manifest is predeclared and sealed before launching its fresh
native jobs. Every reserved campaign must be retained and every cohort must
match the fixed horizon exactly. The registry permits one confirmation plan per
candidate/protocol; scored outcomes cannot be retried to obtain a favorable
result. Confidence concerns one frozen candidate, not a simultaneous guarantee
over adaptive task-family searches.

Hardening treats small unchanged screens as statistically inconclusive. An
explicit `--attempt-budget` increase records the old and new budgets without
resetting spend. `--extend-steps --out` creates a separate checkpoint lineage,
retaining prior steps, instructions, checks and suites; a fresh baseline is
required before interpreting its full-task difficulty.

## Top-up

`MAX_TOPUP_ATTEMPTS` (5) per task per model, replacing `TOPUP_REPLACES`
(void) only; `TOPUP_NEVER_REPLACES` a capability, contract or pass episode.

## Commands

```
./arena select --need N          index, screen, tier, classify (top-down), categorise
./arena build -n N [--no-calibrate]   phases 1-4
./arena sandbox <bundle> [--golden | --shell CMD]   controls without a model
./arena gate <bundle>            the 14-gate chain
./arena screen <tree>            5 x 2 screen (Coder pool)
./arena qualify <bundle>         the 10 x 2 calibration of record
./arena harden <bundle> --apply  manual hardening
./arena deliver <tree> <out>     F3-replay/ F2-scenario/ F1-pattern/, a_f<level>_<slug>
```

## What this policy cannot do

It cannot prove a task is hard: only the screen measures that, and it never
blocks. It cannot prove the hidden tests are *right*: it proves they fail the
untouched repository, pass two independent fixes, fail every declared wrong
fix, and survive tampering. Whether the rule they enforce is the rule the
instruction publishes is what the semantic review and `rule_quote` are for,
and a human reads both.
