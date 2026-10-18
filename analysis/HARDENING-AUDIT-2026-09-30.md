**Coding-task difficulty audit and proposed enhancements — 2026-09-30**

The [follow-up completeness report](HARDENING-COMPLETENESS-2026-09-30.md)
records the implemented enhancements, creation/validation fixes, evidence limits,
and latest verification (**857 tests passed**, both Docker sandbox smokes passed).
The [extreme-task workflow](HARDENING-WORKFLOW-2026-09-30.md) explains how to use
the new tools. The software enhancements are implemented; the full difficulty
objective remains unproven until fresh native campaigns measure the resulting
tasks. The findings and proposed changes below retain the original audit state.

Following the implementation request, authoring and hardening now aim at <=5%
full-task success per model. Draft submission defers all validation and controls;
incomplete candidates survive into validation. Optional causal profiles and
unregistered patch probes guide creation. Blind solving now carries one workspace
through all steps and replays incremental patches plus deliverable snapshots.
Scenario hardening permits a measured F3-to-F2 transition without arbitrary
authored trap/byte ceilings, while validation preserves genuine source provenance.
Screens distinguish the general target from the extreme observed target and report
uncertainty. The findings below describe the original audit state, not claims that
the enhanced pipeline has already produced a measured extreme task. No native
model campaign has been launched for these changes.

Implementation verification: `.venv/bin/python -m unittest discover -s pipeline/tests -t . -p 'test_*.py'`
passed all 804 tests. After small defensive fixes, 59 affected tests passed;
`git diff --check` was clean. New regressions cover deferred submission, retained
validation failures, unregistered shortcut probes, scenario provenance budgets,
extreme stopping targets, uncertainty and void exclusion, persistent multi-step
solving, incremental patch replay, deliverable snapshots, and probe cutoffs.

The highest-value change is to make hardening an experiment over competing, plausible implementations. More trap labels, mutants, or decoy files do not establish that a frontier agent will choose a wrong implementation. Construct a tempting hypothesis, give it credible support on ordinary inputs, expose its failure on an input permitted by the published contract, and measure whether agents actually adopt it.

This is a source-code and stored-evidence audit, with web research. It covers selection, authoring, trap validation, controls, blind solving, screen measurement, provenance budgets, and hardening. No task was redesigned and no model campaign was launched. Forty existing tests passed using `python3 -m unittest pipeline.tests.test_harden_traps pipeline.tests.test_fix_calibration_spec`; an existing unclosed-file ResourceWarning was emitted. The receipt/identity/budget observations are retained in [the audit snapshot](HARDENING-AUDIT-SNAPSHOT.json). Full historical trajectories were not independently re-adjudicated in this audit.

**What the available evidence establishes**

| Stored screen | Fable full successes | Astra full successes | Evidence status at audit time |
|---|---:|---:|---|
| Arabic assistant memory | 5/5 | 5/5 | Receipt identity differs from current bundle; historical result |
| Turbomachinery CAD | 5/5 | 5/5 | Receipt identity differs from current bundle; historical result |
| Next.js social app | 5/5 | 5/5 | Receipt identity differs; measurement incomplete with unresolved trajectory flags |
| Python text parsing | 2/5 | 5/5 | Receipt identity matches current bundle; stored measurement complete |

The corresponding receipts live under each task's `verification/screen.json`; the snapshot records their paths and hashes. These are full-task success counts, not averages over individual checks. The current matching text-parser screen also reports pass@4 = 1.0 for both models. Its Fable result is therefore a useful failure mechanism, but does not yet make the task resistant to multiple attempts.

[The existing failure analysis](FAILURE-MODES-CODING.md) identifies two concrete parser mistakes: expanding an explicit placeholder-removal rule to similar words, and preserving source option order when the published output contract requires letter order. It also discusses identity/format variants and nested exceptions. Its conclusions about other domains and the parent programme remain separate evidence; several referenced parent research files are absent from this workspace. The small coding sample does not establish that symptom traps, restraint, or premise revisions universally fail to work.

**Pipeline findings that directly affect difficulty**

| Finding | Location | Consequence and proposed change |
|---|---|---|
| Hardening stops on the general target below 0.5 on both models | [policy.py](../pipeline/policy.py), [harden.py](../pipeline/harden.py) | Add an explicit research target, initially at most 0.10 on each model. Keep the existing general target and validity acceptance separate. |
| The standard `hard` label checks only the primary model at at most 0.2 | [metrics.py](../pipeline/metrics.py) | Add a separate label requiring the extreme target on every designated evaluation model. Do not silently redefine the spec's existing label. |
| Trap floors and first-pass mutants measure author intent | [quality.py](../pipeline/author/quality.py), [traps.py](../pipeline/author/traps.py) | Add observed hypothesis adoption and ablation evidence; a hand-written wrong patch proves test discrimination, not model confusion. |
| New traps must drive at least two objectives | [harden.py](../pipeline/harden.py) | Prefer distinct behavioral consequences, but permit a strong trap with one decisive objective. One failed check already defeats full success; duplicating its consequence cannot reduce binary pass@1 further. |
| Five-run screens decide progress; one unchanged screen can end a lineage | [harden.py](../pipeline/harden.py) | Treat close comparisons as inconclusive and use a predeclared confirmation procedure. The loop's progress predicate needs a fall on one model, while its comparison acceptance asks for a fall on both; report these distinct meanings explicitly. |
| The blind solver handles only the first step and defaults to 60 steps | [blind_solve.py](../pipeline/author/blind_solve.py) | Extend the advisory probe to all steps and preserve solver changes. Label its budget/scaffold; it is not equivalent to the unlimited native calibration. |
| Alternative-fix controls enforce full credit and nonidentical patch text | [controls.py](../pipeline/author/controls.py) | Record independent authoring context and independently derive the semantic oracle. Nonidentical text alone cannot prove independent reasoning. |
| Selection emphasizes recoverability, disputes, located causes, and three familiar slots | [shortlist.py](../pipeline/select/shortlist.py) | Add experimental features for representation changes, interacting exceptions, state transitions, and distributed invariants. Use measured yield to rank them within the existing selection constraints. |
| Hardening cannot change step inventory | [harden.py](../pipeline/harden.py) | Longer iterative tasks need initial authoring or a deliberately new task lineage, not an ordinary existing-task hardening round. |

The present controls, separate verifier, version identities, failure attribution, and published-rule requirements are useful foundations. Preserve them: they let extremely low success rates mean that the implementation failed a legitimate requirement.

**The current pipeline does not permit arbitrary additional decoys**

Measured budgets at audit time:

| Task | Measured level | Further authored traps permitted | Further authored fixture bytes permitted |
|---|---|---:|---:|
| Next.js social app | F3 | 0 | 2,613 |
| Python text parsing | F3 | 0 | 33,270 |
| Turbomachinery CAD | F2 | 19 | 727 |

`faithfulness_budget()` preserves the original level, trace-derived trap share, category fidelity floor, and 128 KiB total authored-byte cap. For example, the social task already has seven trace-derived traps out of ten, exactly its F3 share floor. The parser has nine out of twelve; adding one authored trap would put it below that floor. CAD has considerable trap-count room but very little fixture-byte room.

If difficulty now takes priority over preserving F3, add an explicit scenario-hardening mode that permits a measured F3-to-F2 transition. Keep every authored addition declared and recompute the ledger and applicable category floors. This requires a policy change: merely increasing the global byte cap will not remove all the binding constraints. Never manufacture extra trace citations or split a single correction into many nominal traps to inflate the provenance ratio. A trace-grounded F1 experiment is another option, subject to its empirical failure-reproduction requirement.

**Recommended techniques, in priority order**

1. **Generate decoys that are almost correct implementations of adjacent contracts.**

   Start with a correct rule and enumerate believable wrong algorithms: a broader scope, a narrower identity predicate, the wrong precedence order, the wrong time at which to read a value, or an incorrect ownership boundary. Each candidate should pass ordinary visible examples. A genuine decoy remains useful somewhere in the repository: an old importer, a preview formatter, a client presentation helper, a bulk endpoint with different semantics, or an adapter for a different wire version.

   The deciding evidence must exist in the public task material. Runtime registration, imports, schema versions, or an explicit current-contract sentence should distinguish the required behavior. Avoid relying solely on filenames. If all decoys are unused files with obviously synthetic names, the model can discard them cheaply.

   For the parser, distinguish the exact removable tags from visually similar legitimate question content. For social-app tasks, distinguish stored roles from token snapshots, media position from database return order, and omission from null serialization. For CAD, distinguish display percentages from serialized fractions and physical angles from a helper's internal coordinates. These are candidate mechanisms; additions must match the task's published input domain and provenance constraints.

   Retrieval research supports measuring which context is found and actually used, rather than equating text similarity with relevance. [ContextBench](https://arxiv.org/abs/2602.05892) reports noisy retrieval and a gap between explored and utilized context. This motivates the decoy experiment; it does not prove these decoys will defeat the configured models.

2. **Make rule intersections the difficult cases.**

   Test conditions that are harmless individually but demand different behavior together. Examples include an out-of-order labeled option inside a restarted question run, a missing answer beside a plausible selection marker, or a role change combined with a stale token and a prohibited self-grant. For new scenario tasks, authorization can combine a base grant, a scoped exception, an exclusion, and a specific override, with explicit precedence.

   Generate paired cases: one that must be allowed only because an exception applies, and one that must be denied because the exclusion survives another permission. This defeats both overly permissive and overly restrictive shortcuts. Use an executable rule model or decision table derived independently from the implementation. Nested exception reasoning is an experimental candidate here, not a demonstrated failure of both calibration models.

3. **Combine representation equivalence with dangerous near-equivalence.**

   Require the implementation to recognize the same entity across documented encodings while keeping similar entities separate. Useful boundaries include percent versus fraction, UTC instant versus local calendar date, absent versus null versus empty, identifier versus display label, and Unicode variants within the declared grammar.

   Include positive equivalence pairs and negative collision pairs. An alias-normalization task should contain a genuine alias and a lookalike that refers to a different entity. Otherwise a model can solve every variant by indiscriminately broadening the normalization. Publish the accepted representations or make them inferable from accessible schemas and real fixture examples; novelty in a hidden sample cannot introduce a new requirement.

4. **Add failures that depend on action history.**

   A single request is often easy to validate. Test sequences such as create → read → update → reload, grant → cached read → revoke → read, or write → injected failure → retry → restart. The published contract can require transaction rollback, idempotency, current authorization, and preservation across restart, where the scenario supports them.

   Use deterministic clocks, explicit failure points, and controlled barriers for concurrency. Distinguish stale caches, stale token claims, and partial persistence with separate mutants. For numerical tasks, test combinations of updates and save/load operations rather than only isolated helper calls. [Hypothesis's stateful-testing documentation](https://hypothesis.readthedocs.io/en/latest/stateful.html) describes generating action sequences and comparing the real system with a simpler model. Discover cases during authoring, then retain a reproducible suite for grading.

5. **Make multi-step tasks accumulate architectural consequences.**

   Author a small set of experimental tasks with approximately five to seven meaningful checkpoints. Carry the agent's own code forward. Each checkpoint should expand a behavior that exposes an earlier design assumption: single format → multiple formats → incremental processing → correction/retraction → persistence and compatibility, for example. Keep prior published behavior tested unless a later requirement explicitly supersedes it.

   Measure final regression success as well as success over the whole sequence. Simply multiplying several easy steps can lower all-step completion without making the final engineering problem substantially harder. The desired mechanism is a design decision that makes later work difficult to implement correctly. Grade observable behavior, not an unpublished preferred architecture.

   [SlopCodeBench](https://arxiv.org/abs/2603.24755v2) studies agents extending their own implementations over successive checkpoints and reports no fully solved problem among its evaluated configurations. Its models, tasks, budgets, and scoring differ from this pipeline, so the result motivates a trial rather than predicting this pipeline's success rate.

6. **Compose actual defects across boundaries.**

   Favor tasks in which a local patch must satisfy an invariant across parsing, normalization, persistence, and presentation. Examples include keeping media order consistent across create and reload paths, or maintaining units consistently across import, geometry, and export. Use two to four interacting semantic faults where the source supports them; do not inflate difficulty with unrelated broken modules.

   [SWE-smith](https://arxiv.org/abs/2504.21798) demonstrates scalable generation of executable repair tasks by introducing changes that break existing repository tests. Its generation approach is useful infrastructure inspiration. It does not establish that random mutations, or more mutations, produce extreme difficulty. Rank candidate fault combinations by measured solver behavior and independently validate their contracts.

**What to measure for each decoy**

Retain a private design record alongside the existing mutant and misreading:

```json
{
  "tempting_hypothesis": "the specific wrong algorithm or premise",
  "supporting_evidence": ["public ordinary cases or adjacent implementation"],
  "disambiguating_evidence": ["public rule, schema, symbol, or runtime path"],
  "divergence_cases": ["contract-permitted counterexamples"],
  "wrong_fix_mutants": ["multiple structurally different wrong fixes"],
  "observed_adoption": "counts by model, scaffold, and frozen variant",
  "ablation": "with-decoy versus without-decoy evidence"
}
```

This is a proposed schema, not implemented support. Keep its answer-bearing content out of the agent image. Use several wrong implementations per important mechanism: the current one-mutant-per-trap requirement is a minimum, and killing that one patch may miss another equally plausible shortcut.

A useful four-way experiment runs the same legitimate contract with: neither added decoy nor added interaction, decoy alone, interaction alone, and both. Maintain comparable functionality and publish all variants. Compare actual errors and full-task success; any observed interaction needs enough independent runs to distinguish it from sampling noise. If removing the decoy does not help, it is decoration or its effect is unresolved. Discovered failure families may guide new development variants; exact final evaluation attempts must remain held out.

**A stronger hardening and evaluation workflow**

Use this sequence: choose a seed with plausible interacting decisions → construct competing implementations and divergence generators → validate reference, alternative, mutants, and grader controls → run advisory blind probes over the complete task → freeze task variants → conduct development screens → select a candidate → run a fresh confirmation campaign → adjudicate failures and report uncertainty.

Prefer a fixed, explicitly authorized research budget for candidate designs and confirmation runs. The present policy allows only three lifetime redesign attempts, includes validity repairs in that count, freezes previously difficult checks, and forbids step-inventory changes. Any broader search requires an explicit policy change. Do not silently bypass the existing lineage accounting or reinterpret repair attempts as free.

At first, target observed pass@1 at most 0.10 on every designated model, with an aspirational 0.05 target and pass@4 reported separately. Keep model rates separate. Maintain unlimited native calibration settings; shorter advisory probes may help select candidates, but their failures must not be presented as failures under unlimited calibration.

Five episodes are suitable for exploratory triage, not confirmation of a 10% success rate. Under independent Bernoulli attempts on a fixed task/model protocol, zero successes in five attempts has a one-sided exact 95% upper bound of approximately 45.1%; zero in ten has an upper bound of 25.9%. Zero in 29 gives an upper bound below 10% for one model. For a predeclared two-model claim, zero in 36 on each gives simultaneous bounds below 10% using a Bonferroni allocation of 2.5% per model. These figures are calculated from `1 - alpha ** (1 / n)` and assume a fixed candidate, independent attempts, and no adaptive early stopping. Task-family claims need independent family coverage too.

The existing [measurement.py](../pipeline/measurement.py) already computes per-check Wilson intervals. Extend uncertainty reporting to full-task success and comparison decisions. Fixed confirmation horizons are simpler than stopping as soon as a favorable rate appears. If sequential testing is desired, implement an appropriate predeclared sequential procedure.

Report failure mechanisms and their correlation. Several checks depending on one misunderstood rule are one latent decision. Repeating that rule across objectives changes partial credit and coverage, but does not create independent opportunities for failure. Similarly, a task with many nominal traps can still be solved once the model notices their shared principle. Prefer distinct measured mechanisms and inspect conditional failure patterns instead of assuming independent trap probabilities.

Use separate development and held-out evaluation variants and fresh confirmation episodes after candidate selection. Changes to public fixtures, hidden inputs, instructions, or solutions need their own identities and receipts. Current report-only commands should continue to refuse stale identities. Extend that discipline to research dashboards and analyst reports, including historical tables like the existing failure analysis.

**Research findings that limit the expected benefit of superficial confusion**

Identifier renaming, control-flow rewrites, and dead code can be useful robustness variants, but should be a lower priority than semantic mechanisms. [A Jagged Frontier](https://arxiv.org/abs/2608.18389) reports mostly small degradation from such transformations, with at most a 6.7 percentage-point mean drop in the most affected configurations and differences across scaffolds. Randomly adding files or replacing names is therefore an uncertain route to extreme difficulty.

Low scores also need deeper validity review. [OpenAI's July 2026 SWE-Bench Pro audit](https://openai.com/index/separating-signal-from-noise-coding-evaluations/) estimates roughly 30% of that benchmark's tasks are broken, citing overly strict tests, missing requirements, insufficient coverage, and misleading prompts. For this pipeline, preserve the semantic review and all-fail proof obligations, and strengthen independence of oracle construction. A clear published contract can support very deceptive repository evidence without requiring implementation-specific tests.

**Suggested implementation order**

| Priority | Change | Main files |
|---|---|---|
| P0 | Separate an extreme-difficulty research target from general acceptance; add task-level uncertainty and confirmation status | `pipeline/policy.py`, `pipeline/metrics.py`, `pipeline/measurement.py`, `pipeline/screen.py`, `pipeline/harden.py` |
| P0 | Permit explicit scenario-hardening with truthful level reassignment and recomputed budgets | `pipeline/harden.py`, `pipeline/ledger/levels.py`, `pipeline/author/validate.py`, policy documentation |
| P1 | Record competing hypotheses, divergence generators, multiple mutants, and decoy ablations; remove the requirement that every new trap must affect multiple objectives | `pipeline/author/quality.py`, `pipeline/author/traps.py`, `pipeline/author/controls.py`, `pipeline/harden.py` |
| P1 | Update authoring guidance from categorical parent-program claims to ranked, coding-specific experimental mechanisms | `pipeline/author/agent_designer.py`, `pipeline/harden.py`, `analysis/FAILURE-MODES-CODING.md` |
| P1 | Expand blind probes to complete multi-step tasks and retain independent contract/oracle evidence | `pipeline/author/blind_solve.py`, `pipeline/author/agent_designer.py`, controls and review receipts |
| P2 | Add stateful sequence generators, deeper iterative task templates, and mechanism-aware seed ranking | authoring tooling, `pipeline/select/classify.py`, `pipeline/select/shortlist.py` |

The first trials should compare parser scope × representation × restart interactions, a newly declared social-app scenario with authorization × stale state × rollback interactions, and CAD import/export tasks with units × schema variants × geometric invariants. Keep their provenance and experimental status explicit. Success means a low confirmed success rate accompanied by understandable, repeatable model errors on legitimate requirements.
