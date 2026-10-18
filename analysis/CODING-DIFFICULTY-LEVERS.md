# Coding difficulty levers: evidence, doctrine, and where the pipeline uses them (2026-10-03)

User, 2026-10-03: make every coding task and every step extremely hard for both
calibration models (pass@1 <= 0.5 on each, aiming near 0), with every failure a
genuine capability failure, no visible traps, and the levers chosen to fit the trace.

This file is the synthesis. The source of truth for the levers is
`pipeline/author/levers.py`. It holds 50 levers, the dead list, the admission test
and the audit. The raw research is in `analysis/web-research/W1-W4`, with a URL and
an evidence tag on every claim.

**Status.** Nothing here has been measured on a new task yet. Each lever is a
hypothesis until a 5x2 screen measures it, and only the levers marked *measured*
below have bitten these exact models in this harness.

## 1. What the evidence says

### 1.1 Measured here (Fable 5.1 in Claude Code, GPT-6 Astra in Codex, high effort, no limits)

**c-010, the six-step parser lineage.** Read from each episode's
`steps/<s>/verifier/arena-grade.json` in
`pipeline-artifacts/research-hardening-two-20261001/native/screen-f8a5…`.
Both models scored a whole-task 0/5, with all 10 episodes attributed to capability.

| step | what the step adds | Fable full pass | Astra full pass |
|---|---|---|---|
| 1 rebuild s10 | closed placeholder rule, markup, option order | **1/5** | 5/5 |
| 2 restart numbering | restart cut point within one dump | **2/5** | 5/5 |
| 3 newer export | fenced text, continuation lines, spelled answers | 4/5 | **3/5** |
| 4 batches + key | identity across batches, printed key | 4/5 | **3/5** |
| 5 rebuild as scans arrive | numbering holds across rebuilds, retraction | **1/5** | 5/5 |
| 6 the whole thing | manifest + cross-file agreement + atomic writes | 0/5 | 0/5 |

Three things follow:

- **The two models fail different steps.**
  - Fable fails on scope, on rule interaction, and on state held across rebuilds.
  - Astra fails where the same fact arrives in a new shape or identity.
  - Each model passed the other model's steps. A step aimed at one family is easy
    for the other. That is why the doctrine asks every step to aim at both.
- **The whole-task zero hides easy steps.** Steps 1, 2 and 5 were 5/5 for Astra.
  So `harden.lineage_target_met` now also requires every step to be at or under
  `policy.STEP_PASS_RATE_MAX` (0.5) on both models.
- **Step 6 is not yet a proven bite.** Its two all-fail checks (`o_manifest` and
  `o_cross_file`) still await the all-fail proof obligations, and three
  reward-hacking flags still await adjudication. Treat step 6 as unproven.

**The two-step c-010 (`analysis/FAILURE-MODES-CODING.md`).** Fable scored 0.40 here,
from two mechanisms:

- M1, rule-scope over-reach.
- M2, two rules interacting on a rare input.

Astra scored 1.00 on that version. Its only blind-verified bite in the programme is
M3, the same entity under a surface variant.

**Measured dead here as the puzzle (40 clean episodes):**
- symptom vs cause
- chained defects
- decoy fix location
- restraint (0 failures in 5,210 forbidden-edit attempts in the census)
- regression bait
- revert residue
- an announced premise revision
- hidden guard budgets
- breadth and volume
- stacked traps of one type

### 1.2 Measured in the sibling programme (`arena-envs-non-coding-v2`)

- **Lever lab L01-L29.** One micro-task per mechanism, 5 episodes per model, then
  combined, stacked and at scale. Every one was solved in every valid episode by both
  models. The one 0/5 (Fable on L20) was an ambiguity.
- **Wave 1 (R6).** 1,220 lever-episodes across 5 dense tasks produced 0 failures.
  The cause: **every lever was a noticing trap.** The deciding rule was a plain
  sentence that both models read in full and transcribed into one correct line.
- **R3 habits.** These describe what the idiomatic baseline actually does:
  - Astra uses first match via `re.search` in 49% of steps and `next()` in 27%.
  - Dict last-wins appears in 16% of Astra steps.
  - Astra uses Decimal with a single quantize at the end.
  - Fable uses float `round()`.
  - Both hard-code literal sets after reading a small population.
  - Verification is structural (schemas, counts, hashes), never an independent
    reading of the rule.

### 1.3 External (W1-W4; numbers are those published, tags in the source files)

**Same harness family (Harbor, Claude Code and Codex CLI, high effort).**
SWE-INTERACT gives requirements over several turns:
- Opus 4.8 falls from 50.7% to 26.7%.
- GPT 5.5 falls from 48.0% to 24.7%.
- About 34% of failure labels are a *forgotten* requirement the agent had already
  understood. This becomes `standing_rule_carry`: nothing in the later step mentions
  the earlier rule.

**Zero2Repo (2026; GPT-6 Astra tested directly).**
- 67% of Opus 5.5's failed hidden tests are long-tail branches of published rules.
- 62% of Astra's are single omissions of a format or convention.
- Both models tested from their own implementation, never from the spec. Every
  failure could have been detected from the public spec, and none was.

This independently reproduces our M1/M2 (Claude) and M3 (GPT) split.

**DeepSWE.** About two-thirds of Opus 4.7's missed requirements are "one branch
shipped" (`sibling_path_omitted`). GPT-5.5 misses the fewest.

**SpecBench.** Every frontier agent saturates the visible tests. Features that pass
alone fail when composed, with a 43-48 point gap between visible and held-out tests
for Claude Code (`feature_composition`).

**STING and PatchDiff.** 10-30% of patches that "pass" in public benchmarks are wrong.
- Input selection drives 54.5% of the rejections.
- End-to-end checks drive 40.9% (`real_entry_point`).
- Fallback branches hold 19.5% of the surviving variants (`long_tail_branch`).

**SWE-Touch.** On long tasks only, a user edit sitting in the workspace cuts:
- Opus 4.8 by 10 points.
- GPT 5.5 by 8 points.

Opus replaced the user's edit incorrectly in 37.9% of its failures
(`user_counter_edit`).

**BaxBench.** About half of functionally correct backends are exploitable. The rule
must be the user's own business rule: a generic "be secure" reminder removes the
difficulty (`adversarial_rule_inputs`).

**Performance tasks (SWE-fficiency, GSO).**
- 68% of the expert speedup lies in functions models never touch.
- 75% of runs stop early.
- Grade only with deterministic counters (`hidden_workload_counters`).

**Terminal-Bench 2.0 and 4.0 per-task rates (W1).** These were computed from 52,104
public trials. The TB maintainers now run trials with our two models.
- GPT stops at the locally sufficient fix. In mvcc-lsm-compaction, Sol and Terra
  scored 0/10 and Fable 5.1 5/5: the reported case is fixed and the generated family
  fails (`case_family_from_reproducer`).
- GPT also misses live invariants under kill and restart (`fault_atomicity`).
  Payments-pipeline scored Sol 1/5 vs Fable 5/5.
- GPT misses byte-exact error paths (`error_path_contract`). rs-archive-clone
  scored Sol 1/5.
- GPT fails performance budgets set against a hidden oracle. distributed-dedup
  scored GPT 0/10 vs Claude 12/15.
- Claude over-simplifies concurrency with a global lock (`concurrency_progress`).
- Claude misses a ported legacy quirk (`legacy_quirk_preservation`).
  vba-userform-port scored Fable 1/5.
- Claude misses the same bug in a sibling base class. vllm scored Claude 0/15.

The same report is the strongest validity warning:
- 14 of the 21 TB2 tasks under 0.35 were later modified, 13 with a documented
  defect.
- 11 of the 13 TB4 tasks that both families almost never solve have defect reports.
- The two largest Claude-only zeros were grader bugs in which Claude's fix was *more*
  correct.
- Family splits were often an ambiguity that each family read differently.

**Anti-levers:**
- A sub-task that looks impossible raises attempted reward hacking 3-6x, which
  produces voids rather than failures.
- Hiding a name or interface the tests need is a void (25.9% → 8.4%).
- A planted comment that contradicts the published rule is reported as a
  misleading-instruction defect.
- Edge rules listed out explicitly in the instruction score .95-1.00.
- A "may" that the tests enforce as "must" is a void.
- Ambiguity, wall-clock targets, and turn or context caps are not difficulty.
- Most public "all-fail" tasks are broken: 70% of Terminal-Bench 2 tasks under 0.3
  were later fixed.

## 2. The doctrine (in `levers.PROMPT`, leading the designer's system prompt)

1. **Publish the rule; let the evidence decide whom it reaches.**
   - Visible inputs are the ordinary cases, and they agree with the wrong reading.
   - Hidden inputs sit exactly where the published rule and the natural code
     diverge: scope siblings, unseen shapes, rare intersections, rebuild sequences,
     the other live path, the real entry point.
2. **Interlocks, never single levers.** Two or more levers act on ONE hidden item.
   - Resolving all but one lever still fails.
   - Each partial resolution fails differently.
3. **Every step carries its own interlock aimed at BOTH families.** Each needs one
   lever Fable misses and one Astra misses.
   - Fable: scope over-reach, long-tail branch, one branch shipped, rule interaction,
     state across runs.
   - Astra: unseen format or identity variant, exact predicate, first match, a
     convention omitted.
4. **The admission test** (all three must hold):
   1. Default-wrong: an executed `m_baseline_*` mutant. It is the literal
      transcription a careful engineer writes with these agents' idioms, and it
      passes every visible input.
   2. Unlabelled: nothing the solver can read names the divergent case.
   3. Determinate: one instruction sentence settles it, and an independent
      alternative passes. Each interlocked lever also gets one `m_partial_*`
      mutant.
5. **At least `MIN_LEVER_MECHANISMS` distinct levers** per task, with different
   combinations on different items. Keep each affected population small and route
   several checks through each premise.
6. **Validity-safe construction** so the first draft passes review:
   - Every assertion traces to its rule_quote.
   - Tests observe only through the published interface, never through stdout,
     exit status or marker files.
   - Tests create every input they use.
   - A later step re-grades every earlier rule it says still holds.
   - Hidden data never appears in the repository or the instruction.

## 3. Turning near misses into counted failures (verifier hardening, W2 s.4)

- **Differential tests against the reference.** Run them on the public entry points
  and keep a divergence only when a published sentence decides it.
- **Mutation-guided augmentation.** Our trap mutants plus agent-made near misses
  (`blind_solve`, `probe_patch`). Keep a test only if:
  - it kills a variant;
  - it passes both the reference and the alternative;
  - it survives a rename or restructure.
- **Property-based tests over the trigger region** (with a fixed seed), and
  **metamorphic relations**, for example re-running changes nothing, or input order
  does not matter.
- **End-to-end checks through the real entry point**, and a full-suite guardrail.

## 4. Reward-hacking vectors (W4 s.4) against this pipeline

| vector | defence here |
|---|---|
| reading answers from git history | `.git` refused in the fixture (`validate.py`, `gates/code.py` leak sweep); keep fixtures without history |
| session leftovers: notes, `.orig`, readable reference lines | leak sweep, `reference-readable`; **new**: `AGENTS.md`/`CLAUDE.md` in the fixture is a lever-audit advisory |
| test tampering, conftest, early exit, report forging | hidden tests under root-owned `/tests`; `python -I -m pytest --rootdir /tests/suites`; `test_tamper`, `early_exit`, `report_forge` controls; introspection scan |
| special-casing visible inputs (GPT-5 did so in 76% of ImpossibleBench one-off tasks) | hidden inputs disjoint from visible ones (`hidden.disjoint`) and differently shaped |
| fetching the fix over the network | calibration only on Coder, with egress control |
| an impossible-looking sub-task | anti-lever, on the dead list |

## 5. Where each phase reads it

| phase | change |
|---|---|
| select | `select/mechanisms.levers_supported` scores catalogue levers from the classification's own text and correction classes. No model call, no classifier-schema change, cache intact. The shortlist uses it as a small ordering bonus and records `lever_opportunities`. |
| author | `levers.PROMPT` leads `agent_designer.SYSTEM`. The stale "WHAT DEFEATS" list, which recommended dead levers, is replaced. The opening message lists **LEVERS THIS TRACE SUPPORTS**, ranked from the fixture's code, the classification and the corrections, with interlock pairs that aim at both families. The self-audit adds `quality.lever_plan`. `check_draft` and `difficulty_profile` return the `lever_doctrine` audit. |
| grade / review | the reviewer reads `quality.lever_plan` and reports single-lever decisions, steps without their own interlock, labelled traps, divergences the visible inputs already exercise, and rule hubs as **difficulty advisories**. These never decide the verdict. |
| screen | the screen records `step_pass_rates` per model. Its outlook names steps above the bar, and the markdown report has a step-wise table. |
| harden | the brief's step 2 is the interlock doctrine. The brief adds the per-step goal with measured rates, the levers this trace supports, and the lever-plan audit of the measured version. A lineage meets its target only when every step is at or under the bar on both models. |

Nothing above blocks a task. `DIFFICULTY_GATES_RELEASE` stays False, and validity
still decides.

## 6. What is not established

- None of the new levers has been screened on a new task.
- The external numbers come from other tasks and mostly from older models. Opus 5.5
  is not Fable 5.1.
- The six-step bite rests on n=5 per model, with two checks still unadjudicated.
- Interlocks raise the designer's workload; their effect on repair rounds is
  unmeasured.
- The relevance ranking is a regex heuristic. It suggests which levers fit a trace;
  it cannot prove that one will bite.
