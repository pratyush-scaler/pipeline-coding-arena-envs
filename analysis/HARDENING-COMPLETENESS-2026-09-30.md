# Hardening implementation and evidence — 2026-09-30

The remaining software enhancements identified in the re-audit are now
implemented. Their usage is documented in [the extreme-task workflow](HARDENING-WORKFLOW-2026-09-30.md).
No new native model campaign has demonstrated the <=5% success target; implemented
mechanisms and passing software checks do not establish measured difficulty.

| Area | Implemented behavior and evidence limits |
|---|---|
| Creation and validation | Partial submissions retain candidates and continue writing missing components within the original session budget. The component inventory covers fixture provenance, all steps, checks/suites, per-step reference/alternative patches, traps/mutants and self-audit entries. A separate creation receipt and batch counts distinguish written components from unfinished work. Submission has no quality, controls or difficulty prerequisite. Export, source-capacity rejection and authoritative validity checks happen during validation. Transport, path protection and resource limits still exist. |
| Extreme target | <=5% full-task success per model, distinct from general acceptance and the primary-model hard label. Difficulty remains advisory. |
| Scenario decoys | No arbitrary authored trap/byte ceiling in scenario hardening. Validation recomputes actual provenance and preserves genuine trace traps and the applicable minimum level. |
| Semantic mechanisms | Six mechanisms, competing hypotheses, interactions, multiple registered mutants and validated optional public citations. Counts alone do not demonstrate confusion. |
| Deterministic cases | Intersection generation, pair-coverage diagnostics, stateful histories, retained critical prefixes and a six-checkpoint template. Expected answers must still come from an independent published-rule model. |
| Shortcut probes | Unregistered patches can be executed and their check failures inspected. Test discrimination does not prove solver adoption. |
| Blind solving | All steps share a workspace; incremental patches and per-step deliverables replay through the verifier. Cutoffs and capture errors cannot become model capability scores. Bounded probes differ from native calibration. |
| Independent oracle | A different vendor derives an implementation from public inputs and the pinned toolchain before grading. Hashed inputs, transcripts and patches are retained privately; supplied provenance is checked during validation. Executed alternative controls decide correctness. |
| Decoy experiments | Four retained variants and deferred plans; validation enforces matched public contracts, scoring schemas and treatment contrasts. Sealed native runs report separate model rates, uncertainty and factorial effects. No live ablation campaign was run during implementation. |
| Hypothesis adoption | Analyst claims must cite this trial's retained agent evidence. Quotes, hashes, trial identities and attribution are rechecked. Unadjudicated episodes stay unknown; failure signatures alone are consistency evidence. |
| Selection | Inspected mechanism opportunities influence ordering within source tiers and existing slots, including batch diversity. Missing or ungrounded opportunities do not exclude seeds or earn bonuses. |
| Longer tasks | Explicit new lineages append checkpoints while retaining prior steps, instructions, checks and hidden suites. Parent identity and search history are retained; a fresh full-task baseline is required. |
| Search uncertainty | Small unchanged screens are inconclusive rather than terminal. Explicit lifetime budget increases retain spent attempts and audit the change. Validity repair remains bounded. |
| Fresh confirmation | Frozen, sealed, predeclared native cohorts; exact horizon, complete campaign inventory, void-only replacement, no screening reuse, and one registered confirmation plan per candidate/protocol. The 95% claim concerns that candidate, not adaptive task-family searches. |

Latest verification, including the creation-completion follow-up, passed all
**857 tests** in 23.322 seconds using
`.venv/bin/python -m unittest discover -s pipeline/tests -t . -p 'test_*.py'`.
`git diff --check` was clean. Earlier verification passed 812 tests before these
features, then 834 and 842 tests while the implementation grew. The latest
focused creation checks passed 58 tests. They exercised complete two-step
creation and bundle export, completion after partial submissions, interruption
and resume without a budget reset, per-file provenance coverage, and deferred
quality rejection. Provider transport was simulated; no model calls were made.

Both real Docker sandbox smokes passed: single-step and two-step, with untouched
fixture 0, reference and alternative 1.0, intended mutant failures, and preserved
step history. A separate two-step Docker check passed public-image isolation and
ungraded independent-oracle capture using a simulated provider transport. It
made no model calls and supplies no difficulty evidence.

No existing production task bundle was redesigned and no live model campaign
was launched. Genuine native failure rates, treatment effects and observed
hypothesis adoption remain empirical work to perform with the new workflow.
