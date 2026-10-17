# Hidden tests frontier coding agents fail, while each test stays settled by the instruction: research report (2026-10-17)

**Scope.** I ran 58 distinct searches and opened about 25 primary pages. "(secondary)" marks a figure I only saw in a news or aggregator summary. Several arXiv papers changed their numbers between versions; I give the version I read. Before writing, I read `analysis/CODING-DIFFICULTY-LEVERS.md`, so this report does not repeat the lever doctrine already there. It covers how to find failing inputs and how to prove them fair.

## 0. Why planted traps don't bite

- **Same blind spots.** SAGA (https://arxiv.org/html/2507.06920v1) found that tests written by an LLM mirror that LLM's own error patterns, which it calls a "homogenization trap":
  - LLM errors cluster tightly in PCA; human errors spread widely.
  - "LLM-generated solutions easily pass these self-generated tests."
  - An authoring agent from the same families as the solvers therefore tends to imagine the edge cases the solvers already handle by default.
- **Targeted exposure is weak without execution.** In TestCase-Eval (https://arxiv.org/html/2506.12278v1), the best model exposed a given buggy program 43.8% of the time; human experts managed 93.3%. Timeout (TLE) faults were hardest, 21.2% for Qwen3-32B.
- **Remedy: invert the loop.** Get the real wrong solutions first (blind-solve patches). Search by execution for inputs where they disagree with the reference. Then keep only the inputs the instruction settles. This is how competitive-programming suites reached near-100% TNR, the share of incorrect solutions a suite rejects (AetherCode, CodeContests+, CodeHacker below).

## 1. Differential and adversarial test generation against model-written solutions

### 1.1 Solver-differential mining (reference vs. agent patch)

- **What.** Generate tests that target the behavioural difference between an agent's passing patch and the reference patch. Then decide whether each difference is a bug.
- **Evidence.**
  - *Are "Solved Issues" in SWE-bench Really Solved Correctly?* (PatchDiff, https://arxiv.org/html/2503.15223v2):
    - 29.6% of plausible patches behave differently from the gold patch.
    - Of those divergences, 46.8% were "similar but divergent implementations" and 27.3% "change more behaviour than ground truth".
    - Only 28.6% of the divergent patches were certainly incorrect.
    - This inflated resolution rates by 6.2 points.
  - UTBoost (https://arxiv.org/abs/2506.09289) flags a task when the gold and agent patches differ on generated tests. It found 15.7% of "passing" SWE-bench Verified patches were wrong (v2 figures).
- **How it plugs in.**
  - `blind_solve` returns K patches per family.
  - In the sandbox, a case generator writes inputs targeting the diff (reference vs. patch).
  - Each divergence is shrunk and clustered by root cause, then sent to the determinacy probe.
  - Budget for most divergences being unsettled: the 28.6% figure says so.

### 1.2 Distinguishing-input synthesis

- **What.** For each pair of programs, prompt a model for inputs on which their outputs should differ, run them, and keep the inputs that do differ.
- **Evidence.** S* (https://arxiv.org/abs/2502.14382): pairwise "adaptive input synthesis" separated candidates well enough that GPT-4o-mini beat o1-preview by 3.7% on LiveCodeBench.
- **How it plugs in.**
  - Run it on reference vs. each blind patch.
  - Also run it on Fable-patch vs. Astra-patch. Where the two families disagree, either one family is wrong (a candidate check) or the instruction does not settle the behaviour (a candidate ambiguity). Send every such input to the determinacy probe.

### 1.3 Hack generation: enumerate wrong solutions, then attack each

- **What.**
  - The model first lists plausible wrong solutions in natural language.
  - It then writes one generator per wrong solution.
  - An LLM-written input validator encodes only the stated constraints.
  - The oracle (the reference) labels outputs.
- **Evidence.**
  - HardTests / HardTestGen (https://arxiv.org/html/2505.24098):
    - Three input types: direct, random, and "hacking".
    - Random inputs use one generator per rare output category.
    - Precision rose 11.3 points and recall 17.5; on hard problems the precision gain was up to about 40 points.
    - 30.0% of incorrect LLM programs failed by TLE, against 14.9% of incorrect human submissions.
  - CodeHacker (https://arxiv.org/abs/2602.20213):
    - A sandboxed code analyst runs probe inputs against the target submission.
    - Hack success was 64.83% (DeepSeek V3.2), falling to 23.76% with reasoning disabled.
    - Suite TNR rose from 85.72% to 96.31%.
  - TCGBench (https://arxiv.org/abs/2506.06821): o3-mini fell "significantly short of human performance" at writing targeted generators.
  - Differential Prompting (https://arxiv.org/abs/2304.11686): infer intent, synthesize a reference, then test differentially. Success rose to 75.0% from a 28.8% baseline.
- **How it plugs in.**
  - The case generator enumerates the measured idioms (first-match, last-wins, float `round`, literal sets) as wrong solutions.
  - It emits one generator per idiom, with a validator restricted to published preconditions.
  - In the sandbox, an input is kept only if it kills the matching `m_baseline_` mutant and the reference passes it.
  - Use a reasoning model, with execution access.
  - This pre-filter is cheap and runs before the costly blind solves.

### 1.4 A wrong-solution corpus, scored by TNR/TPR and selected by matrix rank

- **What.** Treat the hidden suite as a classifier over a corpus of known-correct and known-incorrect solutions.
- **Evidence.**
  - AetherCode (https://arxiv.org/html/2508.16402v1):
    - At least 5 correct and 20 incorrect solutions per problem.
    - 67 competitive programmers, most rated above 2000 on Codeforces, hand-wrote hacks for wrong solutions the automated tests missed.
    - Result: 100% TPR and 100% TNR on that corpus.
    - It also notes that older suites violated the problems' own explicit constraints.
  - CodeContests+ (https://arxiv.org/pdf/2506.05817): accuracy measured against 1.72M labelled submissions.
  - TC-Bench (https://arxiv.org/abs/2510.08720):
    - The rank of the wrong-code × test matrix equals the number of independent error patterns.
    - The best test-generation methods excluded only about 60% of wrong codes.
- **How it plugs in.**
  - Pool every blind patch, near miss, `m_baseline_` and `m_partial_` mutant across rounds.
  - Compute the check × patch matrix.
  - Pick checks that maximize rank (distinct mistakes), not count. This matches the CLAUDE.md caution that several checks can reveal one shared mistake.
  - TPR must be 100% on reference + alternative + the spotlight implementations (§5.1).

### 1.5 Mutation-guided generation

- **What.** LLM-made realistic mutants of the reference, then tests that kill them.
- **Evidence.**
  - Meta's ACH system (https://arxiv.org/pdf/2501.12862):
    - Engineers accepted 73% of its tests.
    - The equivalent-mutant detector reached precision 0.95 and recall 0.96 after preprocessing.
  - LLM mutants detected 87.98% of real faults vs. 41.64% for rule-based mutants, but more of them were equivalent or failed to compile (https://arxiv.org/html/2406.09843v5).
  - EvalPlus (https://arxiv.org/abs/2305.01210): type-aware input mutation cut pass@k by up to 28.9%.
  - Agent-introduced bugs are harder than LLM function rewrites (BugPilot, https://arxiv.org/abs/2510.19898).
- **How it plugs in.**
  - Use real blind-solve near misses as mutants, in addition to authored ones.
  - Filter out equivalent mutants before demanding a kill, otherwise the task carries unkillable obligations.

### 1.6 Coverage- and branch-diff-guided targeting

- **What.** Iterate an LLM on code that is still uncovered.
- **Evidence.** CoverUp (https://arxiv.org/abs/2403.16218): branch coverage 53% vs. 35%, with about 40% of its successes coming from the coverage loop. Panta (https://arxiv.org/abs/2503.13580): 23% more branch coverage.
- **How it plugs in.**
  - Diff the branch sets of the reference's rule code and the solver's patch.
  - Generate inputs that reach reference-only branches; these directly target "one branch shipped".
  - Also generate inputs that reach solver-only branches, where the solver's private assumptions live.
  - Keep a branch only if it maps to a `rule_quote`.

## 2. Property-based, metamorphic and exhaustive checking

- **PBT aimed at the trigger region.**
  - PBT-Bench (https://arxiv.org/abs/2605.15229):
    - Bugs were injected so that default Hypothesis strategies almost never trigger them.
    - Difficulty runs from L1 (single-constraint boundary) to L3 (stateful, cross-function protocol).
    - Frontier bug recall ranged 42–83%.
  - Anthropic's agentic PBT (https://arxiv.org/html/2510.09907v1):
    - 984 reports across 100 packages; 56% were valid bugs overall.
    - 86% of the top-scored reports were valid.
    - Its rule: only test properties the code explicitly claims.
  - **How it plugs in.** Write Hypothesis strategies, including stateful machines for action history, that concentrate inputs where the published rule and the natural code diverge. Use the reference as oracle and a fixed seed. Freeze the shrunk counterexamples as `tests/suites/data`, so the grader stays data.
- **Metamorphic relations (no stored expected value).**
  - Metamorphic prompt testing (https://arxiv.org/html/2406.06864v1) detected 75% of GPT-4's erroneous programs at 8.6% false positives.
  - **How it plugs in.** Use relations the instruction implies:
    - processing batches A then B equals processing A+B;
    - rebuilding is idempotent;
    - order invariance;
    - round-trip.
  - These satisfy ANTI_REWARD_HACKING (the answer comes from running code) and target state carried across runs.
- **Exhaustive interleavings for concurrency.**
  - CONCUR (https://arxiv.org/html/2603.03683v1):
    - With Java PathFinder exploring thread schedules, GPT-5 reached 77.39% pass@1 and Opus 4.1 67.83%.
    - Of 537 errors, about 14% were races and 13% deadlocks; ordinary unit tests cannot reach these schedules.
  - **How it plugs in.** If a step publishes a concurrency guarantee, grade it with a deterministic scheduler or model checker. Never use timing-based tests.

## 3. Lessons from hard agentic benchmark design

- **SWE-bench Pro** (https://arxiv.org/html/2509.16941v2):
  - Humans add a requirements list and an interface section to each task.
  - Without them, GPT-5 drops from 25.9% to 8.4%. Hiding names the tests need creates voids, not difficulty.
  - Every task changes at least 10 lines; the average is 107 lines across 4.1 files.
  - Pass rates fall sharply as the number of files touched rises.
  - Claude Opus 4.1's failures: 35.9% wrong solution.
  - Later audit: about 30% of the 731 public tasks were found broken, 27.4% by the automated check and 34.1% by humans. The OpenAI-attributed audit reported in the coverage below combined Codex investigator agents with 5 engineers, who agreed on 74% of flagged tasks. The flaw classes were over-strict tests, vague requirements, shallow tasks and misleading descriptions (https://tech-insider.org/openai-swe-bench-pro-retraction-2026/, secondary).
- **SWE-bench Verified.**
  - Underspecification and test validity were each rated 0–3; ratings of 2 or 3 discarded the task. Three experts reviewed each task, and 1,699 tasks became 500 (https://openai.com/index/introducing-swe-bench-verified/).
  - The 2026 re-audit of the 138 hardest tasks found 59.4% flawed (https://aiweekly.co/alerts/openai-drops-swe-bench-verified-backs-pro-after-flaw-audit, secondary):
    - 35.5% enforced implementation details ("narrow");
    - 18.8% tested functionality never asked for ("wide").
- **Terminal-Bench 2** (https://arxiv.org/html/2601.11868v1):
  - Criteria are specificity, solvability and integrity. "Specificity" means the instruction describes all correct end states.
  - Review uses LLM checks ("Behavior in Task Description" / "Behavior in Tests"), an adversarial exploit agent, and about 3 reviewer-hours per task.
  - Human-predicted difficulty correlated with measured difficulty at only r = 0.436, and 54.5% of tasks humans rated "medium" were measured hard. Difficulty has to be measured, not judged.
  - The task guideline (https://www.tbench.ai/news/writing-a-good-terminal-bench-task; https://arxiv.org/abs/2604.28093) lists these failure modes: AI-generated instructions, over-prescriptive specs, clerical difficulty, and tests validating the wrong things.
  - 16% of 1,968 terminal tasks were hackable.
- **DeepSWE** (https://arxiv.org/html/2607.07946):
  - Reviewers score "prompt-verifier bijection" (the verifier tests exactly what the prompt asks) and "acceptance breadth".
  - An LLM judge disagreed with DeepSWE's verifier on 1.4% of rollouts, against 32.4% on SWE-bench Pro.
  - About two-thirds of Claude's rollouts were tagged as missing a stated requirement ("one branch shipped").
- **SlopCodeBench** (https://arxiv.org/html/2603.24755):
  - Fairness process: an agent is run on each checkpoint to find ambiguity, and normalization rules are added only for arbitrary choices.
  - Hardness: the best agent passes 14.8% of checkpoints, and pass rates on core tests fall from 64.6% to 35.5% as checkpoints accumulate.
- **Warnings about selecting only what models fail.**
  - Humanity's Last Exam, built by keeping only questions models failed: FutureHouse estimated 29% of its chemistry/biology answers conflict with the literature; Scale's own figure is about 18%; Epoch found 46% in a 48-question sample (https://epoch.ai/benchmarks/hle/review).
  - Platinum benchmarks (https://arxiv.org/abs/2502.03461): label errors hide the real failures.
  - Keeping only what the models fail enriches for defects.

## 4. What remains hard in 2025–2026 (numbers)

| Region | Evidence |
|---|---|
| System-level, interacting functions | Breakpoint (https://arxiv.org/html/2506.00172): success falls from 55% to 0%. Each +1 SD of call-graph centrality cuts the odds by about 20%; each +1 SD of code length by about 55%. With 4 simultaneous corruptions, o4-mini scores 0%. Tasks in the top decile of both complexity and centrality lose about 40%. |
| Observation-heavy work and case analysis | LiveCodeBench Pro (https://arxiv.org/html/2506.11928): 0% on hard problems. Case work stays below 1500 rating. o3-mini made 34 more "idea" errors than humans and 25 fewer implementation errors. |
| Simple patches that need architecture or domain knowledge | 12 never-solved tasks had simple patches that annotators rated easy. Agents that share an LLM agree on far more tasks than agents that share a framework (https://arxiv.org/abs/2604.02547). |
| Iterative extension with carried rules | SlopCodeBench: best 14.8% of checkpoints. Lost in multi-turn (https://arxiv.org/abs/2505.06120): −39% when the instruction is split across turns, while the same content given at once keeps 95.1%. |
| Performance | 30% of wrong LLM programs fail by TLE (HardTests). SWE-fficiency (https://arxiv.org/abs/2511.06090): agents reach under 0.23× of expert speedup. |
| Version and convention specifics | GitChameleon 2.0 (https://arxiv.org/abs/2507.12367): 48–51%. EvoEval (https://arxiv.org/abs/2403.19114): −39.4% average on evolved variants of known problems. |
| Ambiguity (a void, not difficulty) | Orchid (https://arxiv.org/abs/2604.21505): the largest drops are in the strongest models, which write divergent code and rarely notice the ambiguity. |

## 5. Fairness: proving a hidden case is settled by the instruction

1. **Spotlight vs. in-context (two arms).** This is my synthesis; it is not a published protocol.
   - Accept a case only when:
     - **(a) Spotlight arm passes:** both families, given only the governing sentence(s) and the concrete input, answer correctly. They also implement that rule in isolation from the instruction alone, and the implementation matches the reference.
     - **(b) In-context arm fails:** the blind solve on the full task gets it wrong.
   - Supporting evidence:
     - SpecFix (https://arxiv.org/abs/2505.07270): asking an LLM directly to find ambiguity gives "irrelevant or inconsistent" clarifications. Semantic entropy over executed program behaviour works better, so the executable spotlight arm is the stronger signal.
     - ClarifyGPT (https://arxiv.org/abs/2310.10996): low output agreement among sampled solutions marks a requirement as ambiguous.
     - Ambig-SWE (https://arxiv.org/html/2502.13069v2): the best model detects underspecification 89% of the time, so a direct question alone is not enough.
     - *Measuring What a Specification Determines* (https://arxiv.org/abs/2608.19475) defines determinacy as agreement among conforming implementations. It also reports a 14.4-point noise floor, so require unanimity across repeats rather than a majority.
2. **Independent correct solutions (TPR = 100%).** Reference + an alternative + the spotlight implementations must all pass (AetherCode; HardTests kept oracle outputs only where two oracles agreed on over 90% of cases).
3. **Narrow and wide detectors (the OpenAI audit classes).**
   - Narrow: an alternative implementation fails the check.
   - Wide: no published sentence covers the asserted behaviour.
4. **Hacks as a fairness alarm.** ImpossibleBench (https://arxiv.org/html/2510.20270v1):
   - When tests contradict the spec, GPT-5 cheats on 54% of conflicting and 76% of one-off-change tasks.
   - Offering an "abort / flag for a human" option cut GPT-5's cheating from 54% to 9% and o3's from 49% to 12%.
   - So give blind solvers a sanctioned "the instruction does not settle X" channel. Any check they name, or that draws tampering or special-casing, goes back to adjudication.
5. **Hold-out confirmation.** Cases mined on episodes A count only after they fail fresh episodes B; otherwise the HLE and Terminal-Bench defect enrichment above applies.
6. **Item discrimination.** Use an IRT-style analysis of each check (Agent Psychometrics, https://arxiv.org/abs/2604.00594). Flag checks that stronger configurations fail more often than weaker ones; per W1, the largest Claude-only zeros were grader bugs where Claude's fix was more correct.

## 6. Ranked pipeline mechanisms to adopt

| # | Mechanism | Expected effect | Cost | Fairness safeguard |
|---|---|---|---|---|
| 1 | **Solver-first divergence mining.** Freeze the instruction, then: `blind_solve` (K≥3 per family) → distinguishing inputs (S*), hack generators (HardTests/CodeHacker) and reference-oracle fuzzing against each patch → shrink and cluster → adjudicate → add case → re-solve fresh episodes, iterating like the hacker-fixer loop (https://arxiv.org/abs/2606.08960: KernelBench attacks 62%→0%). | Every kept case is a measured failure, not an imagined trap. Hypothesis: the family determinism you measured makes mined cases transfer to fresh episodes. If no divergence survives adjudication, the premise is within default competence: kill or change it early. | 2K blind episodes per round plus sandbox fuzzing. | Determinacy probe + spotlight arm; alternative fix passes; hold-out. |
| 2 | **Spotlight-vs-in-context admission test (§5.1)** on every hidden case. | Separates "hard in situ" from "unsettled". Directly prevents ambiguity zeros. | Cheap (direct calls plus small isolated implementations). | It is the safeguard; require unanimity, both families. |
| 3 | **Hold-out confirmation plus an all-fail audit**: mined on set A, counted on fresh set B; any check under 0.2 gets the narrow/wide/discrimination audit. | Stops difficulty from being "achieved" through defects (HLE 18–46%; Verified 59.4% of hard tasks flawed; SWE-bench Pro about 30%). | One extra screen. | Pre-registered; selection and measurement separated. |
| 4 | **Per-task wrong-solution corpus** (blind patches + mutants + near misses), with TNR/TPR and rank-based check selection (AetherCode, TC-Bench). | Fewer checks catching one shared mistake; more independent failure modes per step; tells you when a suite is saturated. | Bookkeeping plus sandbox reruns. | TPR = 100% on reference, alternative and spotlight implementations. |
| 5 | **Hypothesis-driven hack generators before any blind solve**: enumerate measured idioms as wrong solutions, one generator plus a published-precondition validator each. | Cheap pre-filter, so blind-solve budget goes to drafts with live divergences. TestCase-Eval says expect only about 40% success without execution, so always execute. | Low. | Validator encodes only stated constraints; reference passes. |
| 6 | **Branch-diff targeting**: reference-only branches vs. solver patch, coverage-guided generation. | Hits "one branch shipped" (about two-thirds of Claude's DeepSWE misses) and long-tail branches. | Low–moderate (per-language coverage tooling). | Branch must map to a `rule_quote`. |
| 7 | **Stateful PBT plus metamorphic relations** over the trigger region, with frozen seeds and shrunk cases. | Reaches L3-style stateful/cross-function bugs that default strategies miss (PBT-Bench). Metamorphic checks need no stored answer. | Low. | Each relation derived from a published sentence; determinacy probe on the shrunk case. |
| 8 | **Pick premises in measured-hard regions**: multi-function interaction (Breakpoint), extension with regression carry (SlopCodeBench), insight-gated correctness under a published bound (LiveCodeBench Pro, TLE 30%), concurrency guarantees graded by exhaustive schedules (CONCUR). | Raises the base rate of live divergences for #1. | Selection-time only. | Bounds published; deterministic counters or schedulers, never wall-clock. |
| 9 | **Instruction-first freeze plus a distinguishing-feature leak scan**: check that no visible artefact (instruction, repo, visible tests, error text) shows the hidden case's distinguishing shape, not just its literal value. | Removes the "announced" failure. SWE-Bench+ (https://arxiv.org/html/2410.06992v2): removing leaked instances cut resolution from 12.47% to 3.97%. | Low. | The instruction still states the rule in the user's voice. |
| 10 | **"Not settled" channel and hack telemetry in blind solves** (ImpossibleBench). | Early detection of contradictory or unsettled checks. | Negligible. | Flagged checks are re-adjudicated before admission. |
| 11 | **Per-check discrimination audit** over all episodes and configurations. | Catches over-strict or quirky checks that pass all other gates. | Low. | Inverse discrimination blocks pending human review. |

**Not established.**
- No source measures the spotlight/in-context protocol on agentic tasks.
- How well mined cases transfer to fresh episodes is untested.
- Most of the external numbers come from competitive programming or older models. Expect most divergences to be unsettled, as in PatchDiff, where only 28.6% of divergent patches were certainly incorrect.

Nothing was written to the repository.