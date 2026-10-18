# Extreme coding-task workflow

The pipeline now supports every software enhancement listed in the completeness
re-audit. These mechanisms are experimental until native campaigns demonstrate
legitimate failures. Task creation, case generation, variant drafting and draft
submission retain incomplete candidates. Export, controls, provenance, semantic
review and experiment consistency can reject them during validation.

Saving a partial draft no longer ends creation early. The author receives the
missing-component inventory and continues writing within its original session
budget. `creation_status` inventories the fixture and per-file provenance,
toolchain/dependencies, every step's instructions and checks, hidden suites,
reference and alternative patches, traps, mutant patches and self-audit entries.
It does not run acceptance checks or enforce difficulty thresholds. Completed
components are recorded in `verification/creation.json` and batch
`creation_counts`, with validity and difficulty still unverified. Exhausted or
interrupted work is retained as unfinished; source-capacity findings remain
advisory until validation rechecks them.

## Create the difficult behavior

Choose a source with inspected evidence for several interacting decisions.
Classification can record `mechanism_opportunities`: mechanism, tempting rule,
divergence case and inspected evidence IDs. Selection uses grounded opportunities
within the existing source tiers and batch slots, with a preference for mechanisms
the batch does not already cover. Missing annotations never exclude a seed.

The author has six mechanisms to compose: semantic decoys, exception interactions,
representation collisions, temporal state, distributed invariants and architectural
carry. Decoys should be useful adjacent code paths with different contracts and
accessible evidence distinguishing their authority. Several wrong algorithms
for the same mechanism are more useful than many names for the same wrong rule.
Preserve positive equivalences as well as negative collisions, allow/deny pairs,
ordinary cases and intersections. Publish the behavior that hidden inputs enforce.

`generate_cases` creates deterministic intersections and action histories, including
repeat/retry pairs and explicitly supplied critical histories. It reports missing
pair coverage when sampling. `read_generated_cases` pages private results.
`generate_cases(mode="checkpoints")` suggests six cumulative stages. These tools
generate inputs, never expected answers. Derive the rule model independently and
use deterministic clocks, explicit failure points and controlled restart barriers.

Optional `quality.difficulty_case.hypotheses` records the competing rules, a
distinguishing case, check/trap references, interacting hypotheses and multiple
registered mutants. Public evidence citations use `{file, quote}`, with
`step:<name>` for an instruction. Validation checks supplied citations against
actual public text. Profiles remain advisory and do not score difficulty.

`independent_oracle` has a different vendor derive an implementation from the
repository, sequential instructions and pinned toolchain, before grading. It
records input hashes, complete transcripts and incremental patch hashes. Stage
the returned patches as `alternative` and its reference as
`quality.independent_oracle`. Validation checks provenance and executed controls
still decide correctness. An incomplete or incorrect independent solve is not a
correctness oracle. The ordinary `blind_solve` and `probe_patch` tools remain
advisory too; bounded cutoffs and infrastructure failures are not model failures.

## Measure decoys and interactions

`stage_research_variants` can retain four experiment drafts and a plan directly
from an incomplete authoring draft, without exports, Docker or solver calls.
Alternatively, create variants from an existing task:

```sh
./arena research variants envs/c-NNN-task --out pipeline-artifacts/decoy-drafts --design design.json
```

`design.json` supplies `decoy_files` (fixture-relative paths to authored text),
`interaction_suites` (hidden-suite paths to text), optional per-cell
`spec_overrides`, and `treatment` describing the decoy, interaction and public
disambiguation. Every changed fixture file receives zero source-fidelity credit.
Include valid per-cell reference/alternative patches and mutant declarations
where the treatments require them. The factory retains all four raw candidates;
validation exports them, recomputes their ledgers and executes controls.

The cells are `00` (neither treatment), `10` (decoy), `01` (interaction) and
`11` (both). Their public instructions, check schema and weights stay identical.
Decoy contrasts keep hidden tests identical; interaction contrasts keep the
public repository identical. Interaction suites must exercise permitted inputs
and be selected by the tests. An unused data file does not establish a mechanism.

```sh
./arena research prepare pipeline-artifacts/decoy-study --purpose ablation \
  --variant 00=pipeline-artifacts/decoy-drafts/00 \
  --variant 10=pipeline-artifacts/decoy-drafts/10 \
  --variant 01=pipeline-artifacts/decoy-drafts/01 \
  --variant 11=pipeline-artifacts/decoy-drafts/11 \
  --episodes 10 --treatment treatment.json
./arena research validate pipeline-artifacts/decoy-study
./arena research run pipeline-artifacts/decoy-study --workers 4
./arena research report pipeline-artifacts/decoy-study
```

`treatment.json` contains `decoy`, `interaction` and `public_disambiguation`
descriptions. Validation freezes each valid variant and seals the models, native
harnesses, high effort, unlimited protocol, fixed horizon and randomized job
order. Native campaigns run on the existing Coder pool. Every scored episode
survives resume; only voids can be replaced within the existing top-up budget.
Missing, changed, foreign or omitted campaigns prevent a complete measurement.

The report keeps model rates separate and estimates decoy, interaction and joint
excess effects with conservative simultaneous intervals. An interval spanning
zero is inconclusive. Joint failure patterns expose correlated mistakes without
assuming every check is an independent trap.

To assess adoption, use `research adjudicate <plan> --claims claims.json`.
Each claim names variant, campaign, trial, hypothesis, verdict (`adopted`, `other`
or `unresolved`), reason and citations `{file, quote}` from that trial's retained
agent evidence. Quotes and file hashes are rechecked. Failed-check matches alone
are not adoption. Unadjudicated episodes remain unknown; coverage reports logical
rate bounds rather than treating them as evidence against adoption.

## Extend and confirm

Ordinary hardening preserves the step inventory. A separate checkpoint lineage
can append requirements while retaining prior steps, instructions, checks and
hidden suites:

```sh
./arena harden envs/c-NNN-task --extend-steps --out envs/extended-task
```

This creates a retained draft. Add `--apply` to validate after creation, or later
include the draft in a research plan: `research validate` exports and validates
it. The origin records the parent identity and prior search history. Measure a
fresh full-task baseline; a lower rate on a longer task is not a comparison of
the original task's difficulty.

Equal small screens no longer terminate a hardening lineage. Development
comparisons report uncertainty, and an explicit lifetime budget increase preserves
every spent attempt:

```sh
./arena harden envs/c-NNN-task --apply --attempt-budget 12
```

When hardening observes the extreme target it retains a confirmation plan.
Otherwise prepare one for the selected final candidate:

```sh
./arena research prepare pipeline-artifacts/final-confirmation --variant final=envs/c-NNN-task
./arena research validate pipeline-artifacts/final-confirmation
./arena research run pipeline-artifacts/final-confirmation --workers 4
./arena research report pipeline-artifacts/final-confirmation
```

The default is 72 valid fresh episodes per model. With zero full-task successes
on each of two models, the exact success-rate bounds are below 5% at joint 95%
confidence. Any successes may leave the result unconfirmed. The task remains
valid regardless of difficulty. Screening episodes cannot enter this cohort;
a registry permits one confirmation plan per frozen candidate and protocol,
preventing repeated confirmation until a favorable result appears. Every reserved
campaign must be present, with exactly the predeclared number of valid episodes.
Confirmation concerns this task and protocol, not simultaneous guarantees over
adaptive searches or unseen task families.

The software checks and Docker smokes do not establish model difficulty. No
new native model campaign was launched while implementing these enhancements.
