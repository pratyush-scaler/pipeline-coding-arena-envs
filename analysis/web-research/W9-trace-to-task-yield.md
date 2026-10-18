# W9: Turning more traces into hard tasks: funnel yields, task multiplication, triage, untestable sessions (web research, 2026-10-12)

Scope: how other groups turn real artifacts (commits, issues, PRs, chat logs, agent sessions) into verifiable agentic
coding tasks; where their funnels lose candidates and how they recovered them; how to get several distinct tasks out
of one source; how to predict difficulty before paying for authoring; how to grade work that has no tests (UI,
single-file HTML, data). The goal is to raise our yield of hard, faithful tasks from the 370 coding traces. It is
written to sit next to `analysis/SESSION-SELECTION-FOR-YIELD.md` (our own receipts) and W2 (`W2-swe-bench-family.md`,
which covers the benchmarks themselves). Where W2 already covers a source, this file points to it rather than repeating it.

---

## §0 Method and evidence tags

Generic web search, then the primary source. Four PDFs were pulled and their text extracted and read directly:
SWE-Mirror, GLM-5, Nemotron 3 Super and SWE-Hub. Everything else was read through an HTML fetch that summarises.

| tag | meaning |
|---|---|
| **[V]** | Read in the primary text itself (the four PDFs above, or a verbatim quote returned from the paper). |
| **[V~]** | Primary source read through a summarising fetch. Numbers very likely right, wording not checked word for word. |
| **[S]** | Search snippet or a secondary aggregator only. A pointer to check, not a fact. |
| **[I]** | My inference or proposal. Not measured anywhere. |

Caveats:

1. **Nearly every published funnel is built from commits, issues or PRs, not from chat sessions.** Only three
   sources convert real agent sessions into tasks: SWE-Together, SWE-INTERACT and CodeAssistBench (which uses issue
   threads). Transfer from the commit-mining funnels to our trace funnel is by analogy and is marked [I] where it
   matters.
2. **Their yield is our "valid" step, not our "hard" step.** Industrial funnels stop at "executable plus
   fail-to-pass". None of them filters for pass@1 <= 0.5 against two frontier agents. So their yields are upper
   bounds for the first half of our pipeline, and they say nothing about the second half.
3. **Their difficulty predictors were fitted on older or open models** (Qwen3-Coder, Claude 3.7/Sonnet 4). For Fable
   5.1 and GPT 6 Astra they are priors for ranking, not calibrated probabilities.
4. **The brief's numbers versus our receipts.** The brief says 220 coding sessions were never classified, which is
   what `CODING-TRACES.md` records. `SESSION-SELECTION-FOR-YIELD.md` (2026-10-05) instead reports C1 classifier
   output of 284 seeds over 220 sessions. One of the two is stale. Either way, recommendation R1 below is the same:
   run the code-aware classifier over all 370.

---

## §1 Bottom line: twelve recommendations, ranked by expected effect on yield

| # | recommendation | evidence (section) | expected effect |
|---|---|---|---|
| R1 | Run the code-aware classifier over **all 370** coding sessions, and score seeds instead of killing them on five conjunctive gates. Conjunctive LLM gates destroy recall: strict three-judge consensus in SWE-rebench V2 had recall **0.06**, against 0.31 for score averaging. | §2.4, §7.1 | Largest cheap gain in seed supply [I] |
| R2 | **Agent-introduced regressions get top priority as seeds.** These are moments where the trace's own assistant broke something that worked at an earlier snapshot. BugPilot's FeatAdd bugs come from exactly this situation and are the hardest bug class published: Sonnet 4 solves 41.4% of them, against 65.9% for SWE-smith bugs. They are also F3 by construction. | §3.4 | Raises P(hard) and faithfulness together |
| R3 | **Find the first failing snapshot by bisecting over turns.** Run the designer's reproduction across the per-turn snapshots instead of asking an LLM where the error happened (LLM failure attribution is below 10% accurate). This yields the cut point, the faithful fixture and the F3 "failure-mode reproduction" evidence in one deterministic step. | §3.5 | Cheaper, auditable F3 |
| R4 | Fix the reviewer bottleneck at the source. Hidden tests may only touch **observable or published interfaces**, and every hidden check is adjudicated against **2-3 independently written compliant fixes** from different vendors. 75 of the 102 defects in SWE-Bench Pro Verified were over-narrow tests. Removing the interface spec from SWE-Bench Pro cut GPT-5 from 25.9% to 8.4%, mostly through name and signature mismatches. | §7.3 | Raises validated/built from 0.23-0.49 toward ~0.5-0.6 [I] |
| R5 | Add a **reproduction-agent route** for "no reproduction" and "not gradable" kills that still carry a concrete symptom (an error text, a wrong output, "still broken: X"). The best published agents write a fail-to-pass reproduction test for **58-70%** of SWT-bench issues. | §2.4, §7.2 | Recovers about half of those seeds [I] |
| R6 | Open a **browser lane** with deterministic Playwright checks grounded in the prompt (no selectors, class names or hidden DOM, following HTMLBench-400's rules). Use VLM or agent judges only as advisories: the best LLM judge agrees with experts 70.3% of the time on web work, against 84.6% for humans. | §6.1 | HTML is in 198 sessions, the largest language pool |
| R7 | Mine **several distinct root causes per session**, one task per root cause. Deduplicate executably: if task A's reference patch passes task B's objectives, they are the same task. Expect about 1-2 hard tasks per productive session, 3 at most. | §4 | x1.2-1.5 tasks per session [I] |
| R8 | Turn "no runtime" from a kill into a **runtime-shim route** for the engines that have open headless runtimes: HDL (Verilator, Icarus, GHDL, cocotb), Godot (`--headless`), Flutter (`flutter test`), Jenkins Groovy (JenkinsPipelineUnit) and plain Lua/Luau. MQL5 and Roblox-API-bound code stay killed unless their logic can be extracted. | §6.5 | Recovers some of 197 (C1) and 50 (C2) [I] |
| R9 | Run a **replay probe** before full authoring. Give 1 Fable and 1 Astra episode the raw defect, with the user's own complaint as the instruction and the designer's reproduction as the grader. If both solve it, the hardness must come from authored interlocks. If either fails, it is an F3 candidate whose zero is then certified. | §5.4 | Cheap triage and better P(hard) estimates |
| R10 | Rank seeds by **expected hard-task yield**: P(runtime) x P(repro) x P(valid) x P(hard) x N_distinct. Use the features in §7.5 and refit the weights on our own ~185 built bundles. Static patch and repo features predict agent failure with AUC 0.86 for one agent, but prompt text alone is at chance. | §5.1, §7.5 | Spends authoring on the right seeds |
| R11 | **Certify every all-fail and adversarially selected task.** In Terminal-Bench 3, 47 of 125 all-fail tasks (38%) were broken, infrastructure-limited, exploitable or uncertifiable. Audits of Humanity's Last Exam put wrong answers at roughly 29-46%. Filtering for "hard" selects for defects. | §5.3 | Protects validity; already doctrine |
| R12 | For no-source seeds, a **mirror lane (F1/F2)** is possible but should stay opt-in. SWE-Mirror transplants an issue's failure mode into another working environment with 46% yield, and 88% of results were judged faithful. It weakens Artifact Provenance in QC, so it should never be the default. | §2.2, §7.2 | Optional extra supply |

---

## §2 Q1: Pipelines that turn real artifacts into verifiable tasks, with yields by stage

### 2.1 Funnel table

"Yield" is the share of the stage's input that survives. Where a paper gave only endpoints, the overall ratio is my
arithmetic.

| pipeline | input | output | overall | stage that loses most | recovery tricks | tag |
|---|---|---|---|---|---|---|
| **SWE-rebench** (Nebius, [2505.20411](https://arxiv.org/abs/2505.20411)) | ~450k issue-linked PRs from 30k+ repos | 21,336 tasks / 3,468 repos | ~4.7% of PRs; ~13.9% of 153.4k after preliminary filtering | Installation: "a working installation recipe for at least one task in **31%** of all repositories" | Version from `git tag` (~95% of tasks); LLM labels (clarity, complexity, test correctness) kept as metadata, not as filters | [V~] |
| **SWE-rebench V2** ([2602.23866](https://arxiv.org/abs/2602.23866)) | 29.5M PRs / 145k repos | 32,079 issue tasks / 3,617 repos (+120k PR-derived) | 0.11% of PRs | Setup + F2P: 583,809 -> 41,349 (**-92.9%**); "only around 20% of repositories succeed with a single setup attempt" | Agent setup pass@10 46.1-62.7% vs pass@1 17.4-27.1%; one setup per repo reused; per-language base images; log-parser regeneration (up to 5 iterations); relaxed thresholds for long-tail languages (10 stars); problem statements generated from PR text when no issue exists | [V~] |
| **SWE-bench-Live / RepoLaunch** ([2505.23419](https://arxiv.org/abs/2505.23419), [2603.05026](https://arxiv.org/abs/2603.05026)) | 8,577 repos -> 2,609 | 1,319 tasks / 93 repos (initial release) | n/a | Not reported; names "dependency version drift" | "Time-machine" versioning; RepoLaunch reports a **78%** build success rate across languages and OSes | [V~] |
| **SWE-Factory** ([2506.10954](https://arxiv.org/abs/2506.10954)) | 671 issues, 4 languages | 269 valid (GPT-4.1-mini) | **40%** | Env setup | Four-agent SWE-Builder with an environment memory pool; exit-code grading instead of hand-written log parsers; automated fail2pass (precision 0.92, recall 1.00); $0.024-0.045 per instance | [S]/[V~] |
| **Skywork-SWE** ([2506.19290](https://arxiv.org/abs/2506.19290)) | 146,568 PR instances | 10,169 / 2,531 repos | 6.9% | Installation: 123,179 of 146,568 fail (**84%**); execution validation then drops 56.5% of the rest | Not specified | [V~] |
| **SWE-Gym** ([2412.21139](https://arxiv.org/abs/2412.21139)) | 64,689 raw instances / 358 repos | 2,438 / **11 repos** | 3.8% | Env setup (semi-manual, ~200 annotator hours) | None (manual) | [V~] |
| **SWE-Next** ([2603.20691](https://arxiv.org/abs/2603.20691)) | 102,582 commit pairs / 3,971 repos | 2,308 / 311 repos | **2.2%** | "NewCommitNotBetter" **74.5%** (no test goes from failing to passing); test-run failures 20.8%; setup 2.5% | One environment per repo-quarter (1,273 profiles; 639 GB vs ~30.8 TB); per-commit fallback; file-level test matching after refactors | [V~] |
| **SWE-Universe** ([2602.02361](https://arxiv.org/abs/2602.02361)) | ~33.3M PRs -> ~1M after heuristics | 807,693 / 52,960 repos | ~2.4% of PRs | Build: **75.9%** succeed without hacking (production) | Iterative self-validation (build success 82.6% -> 94% held-out); `switch-to-bug` / `switch-to-resolved` toggles; hacking detection inside the loop; a cheap builder model trained by rejection sampling; base-model rollouts drop tasks that are "too difficult or too easy" | [V~] |
| **KAT-Coder-V2.5 AutoBuilder** ([2607.05471](https://arxiv.org/abs/2607.05471)) | n/a | >100k envs, 12 languages | n/a | Env construction | Success rose **16.5% -> 57.2%** with a preconfigured base environment, language templates and a library of reusable build recipes; accept only when >90% of expected tests are collected and outcomes reproduce | [V~] |
| **SWE-smith** ([2504.21798](https://arxiv.org/abs/2504.21798)) | synthetic bug candidates in 128 repos | 50,137 | 50.1% of candidates break >= 1 test | Low test coverage of the changed code | Yield by strategy: Combine **96.9%**, LM Modify 56.0%, Procedural 40.2%, LM Rewrite 35.0%, PR Mirror 33.8%; issue text generated from the diff plus a failing test (~2.5 cents each) | [V~] |
| **R2E-Gym / SWE-GEN** ([2504.07164](https://arxiv.org/abs/2504.07164)) | commits | 8,135 envs (4,578 subset) | n/a | Not reported | Problem statements back-translated from commit + F2P test output; tests generated for commits that lack them | [V~] |
| **SWE-Dev** (THUDM, [2506.07636](https://arxiv.org/abs/2506.07636)) | 88k instances / 10,416 repos | 38k training / 4,413 repos | 43% | Missing tests: of 26k instances given synthesized tests, only **2,097** fail-to-pass functions resulted | Gherkin scenario -> test, revised from tracebacks | [V~] |
| **SWE-Flow** ([2506.09003](https://arxiv.org/abs/2506.09003)) | 150 projects | 74 pass all their tests -> 16,061 train + 2,020 test instances | 49% of projects; ~260 tasks per project | Env (projects whose own suite fails are dropped) | One project yields many incremental TDD steps (see §4) | [V~] |
| **SWE-Synth** ([2504.14757](https://arxiv.org/abs/2504.14757)) | 21,804 re-implemented variants | 9,459 buggy (43%) | 43% | Variant passes all tests or does not compile | Coverage-weighted component sampling | [V~] |
| **BugPilot FeatAdd** ([2510.19898](https://arxiv.org/abs/2510.19898)) | agent feature attempts | 785 FeatAdd tasks | not reported | n/a | Bugs harvested from an agent's *unintentional* breaks while adding features (§3.4) | [V~] |
| **SWE-Mirror** ([2509.08724](https://arxiv.org/abs/2509.08724)) | issues mirrored into 40 existing gyms | 60,671 tasks | Mirroring yield **46%** (Python 68%, JS 52%, Go 36%, Rust 28%) | Semantic errors 36%; compile/syntax errors 18% (36% of Rust and 28% of Go attempts) | Reuses working environments instead of building new ones; LM filter precision 84.3%, recall 86.0%; 88.1% of audited tasks judged faithful (High or Moderate) | **[V]** |
| **Multi-SWE-bench** ([2504.02605](https://arxiv.org/abs/2504.02605)) | 2,456 auto-validated | 1,632 after 68 annotators | **66%** survive human verification | Human verification | Multi-SWE-RL then released 4,723 instances by skipping the manual step | [V~] |
| **CodeAssistBench** ([2507.10646](https://arxiv.org/abs/2507.10646)) | 25,656 issues from 770 repos | 3,286 / 214 repos | 12.8% | Regex + LLM filtering (25,656 -> 3,342); Docker builds succeeded for 238 of 294 that needed one (**81%**) | "Satisfaction conditions" extracted from the thread (§3.1) | [V~] |
| **SWE-Together** ([2606.29957](https://arxiv.org/abs/2606.29957)) | **11,260 real agent sessions** | **109 tasks** | **0.97%** | Deterministic filter, then an LLM viability screen (external state, PR management, deployment, credentials) | §3.1 | [V~] |

Industry reports state scale but give almost no funnel numbers:

- **DeepSeek-V3.2** ([2512.02556](https://arxiv.org/abs/2512.02556)): mined "millions of issue-PR pairs", filtered by
  rules and LLM judgment, then built environments with an environment-setup agent, giving **24,667 code-agent
  tasks** [S].
- **GLM-5** ([2602.15763](https://arxiv.org/abs/2602.15763)): built "over 10k verifiable environments across thousands
  of repositories spanning 9 programming languages" on top of RepoLaunch, with LLM-written log parsers. Its
  terminal-task synthesis reaches "Docker construction accuracy exceeding 90%" [V].
- **Kimi K2** ([2507.20534](https://arxiv.org/abs/2507.20534)): "a vast amount of pull requests and issues from GitHub",
  run in more than 10,000 concurrent sandboxes. Difficulty selection by SFT pass@k is described only for
  math/STEM/logic [V~].
- **Qwen3-Coder**: 20,000 parallel environments; no funnel published [S].
- **Nemotron 3 Super** ([2604.12374](https://arxiv.org/abs/2604.12374)): reuses SWE-Gym, R2E-Gym and SWE-rebench rather
  than building its own, and regenerates R2E-Gym problem statements. For conversational tool use it filters "by
  dropping scenarios that yield all-success or all-failure outcomes" [V].

### 2.2 Notes on the most transferable ones

- **SWE-Mirror is "the same failure mode, a different fixture".** [V] Each mirror has three steps: a validation test
  ported into the target repo, a `mirror.patch` that re-creates the symptom there, and a problem statement rewritten
  to "feel native". The fix patch is simply the reversed mirror patch. Three annotators compared each mirrored task
  with its original issue. Of the 177 tasks with a majority verdict, 156 (88.1%) were High or Moderate consistency and
  21 were Inconsistent. For us this is a template for an **F1/F2 lane** for seeds whose own tree is missing: the
  scenario and failure mode are kept, but the bytes come from elsewhere [I].
- **SWE-Universe's self-verification toggles** are the closest industrial analogue of our controls: the verifier must
  fail on the bug state and pass on the fixed state, and hacking is flagged inside the build loop. Iterative
  validation alone took build success from 82.6% to 94% on a held-out set [V~]. Our gates already do this. The
  transferable idea is **feeding gate failures back inside the designer's loop**, which matches our
  `_ship_readiness`-as-repair-feedback design.
- **SWE-rebench V2's diagnostic labels** ([2602.23866](https://arxiv.org/abs/2602.23866)) [V~]:
  - **B1 TEST_SUITE_COUPLING**: regressions in unrelated code caught by the pass-to-pass suite.
  - **B2 IMPLICIT_NAMING**: "Tests often expect specific implementation details not specified in the problem
    statement." The authors note it "can be mitigated by injecting naming hints".
  - **B3 EXTERNAL_DEPENDENCY.**

  These labels are kept as metadata, not used as filters. B2 is exactly our reviewer's "over-specified hidden test"
  rejection.
- **GLM-5 multi-step chains** [V] are mined from merged PRs with 3-15 commits. An LLM scores how related adjacent
  commits are, and dynamic programming partitions them into coherent task groups. Each group is split into a golden
  patch, a test patch and an auto-apply patch (configuration and fixtures). Problem statements are generated per
  group, and the whole chain is validated for zero regression. This is a recipe for turning **our per-turn snapshots
  into steps** (§3.6).

### 2.3 Environment-setup success rates (the analogue of our `runtime_feasible` gate)

| system / benchmark | success | note | tag |
|---|---|---|---|
| EnvBench ([2503.14443](https://arxiv.org/abs/2503.14443)) | best: 6.69% of Python repos, 29.47% of JVM repos | GPT-4o era | [S] |
| ExecutionAgent | 33 of 50 repos, 5 languages | not pinned to commits | [S] |
| SetupBench ([2507.09063](https://arxiv.org/abs/2507.09063)) | repo setup 38.9-57.4%; local DB setup 20.0-53.3% | OpenHands, bare sandbox | [S] |
| Repo2Run ([2502.13681](https://arxiv.org/abs/2502.13681)) | 86.0% on 420 Python repos | | [S] |
| RepoLaunch | 78% build success, any language/OS | | [V~] |
| Multi-Docker-Eval ([2512.06915](https://arxiv.org/abs/2512.06915)) | F2P 17-38% overall. By language: Go 54.5% avg; Python/JS/Ruby best ~49-52%; C++ 18.9%, Rust 20.8%, Java 14.3% | Docker build errors are 36% of failures; silent false passes 12.7%; model size "not decisive" | [V~] |
| SWE-rebench V2 | ~20% of repos with one attempt; pass@10 46-63% | retries are cheap yield | [V~] |
| KAT AutoBuilder | 16.5% -> 57.2% with templates and a recipe library | | [V~] |
| SWE-Universe | 82.6% -> 94% (held-out) with iterative validation | | [V~] |

**Takeaway for us [I].** Our fixtures are lifted from bytes, so recovery is high: 352 of 370 coding sessions have a
fully recoverable tree. Our repos are also small. So our "environment" problem is mostly re-pinning dependencies and
picking the toolchain, which is where the industrial pipelines get their biggest single-step gains from:
- templates and per-language base images (16.5% -> 57.2%);
- retries (pass@1 -> pass@10, roughly x2-3);
- a recipe memory (SWE-Factory, SWE-Builder).

Compiled and JVM languages are 2-4x harder than Python, JS and Go in every multi-language study. That is
consistent with our READY counts: JS 8 of 12 and Go 2 of 2.

### 2.4 Which steps lose the most, and what recovered them (synthesis)

| loss class (theirs) | typical loss | what recovered it | our analogue |
|---|---|---|---|
| No tests in the change | -71% (SWE-rebench V2) | Generate F2P tests (SWE-Dev, R2E-Gym), but yield is low: 2,097 F2P out of 26k attempts in SWE-Dev | Only 17 of our sessions ran a runner. We author tests by design, so this kill does not apply to us |
| No issue text | -91% (SWE-rebench V2) | Problem statement from PR description and patch (+120k tasks) | Our instruction is the user's own words, so this kill does not apply either |
| Environment / installation | -84% (Skywork), -69% of repos (SWE-rebench) | Agentic retries, base images, templates, recipe memory, env reuse | `runtime_infeasible` 95 (C1) / 152 (C2); `no_runtime` 197 / 50 |
| No strict fail-to-pass | -74.5% (SWE-Next) | File-level test matching; reproduction-test generation | `no_reproduction` 125 / 427 |
| Underspecified / unclear | -22% (SWE-rebench V2); 68.3% of SWE-bench Verified's sample removed by humans [S] | Human rewrite into problem + requirements + interface (SWE-Bench Pro) | Semantic review blocks (C1: 42 of 44 blocks) |
| Human verification | -34% (Multi-SWE-bench) | Skipped for RL data (Multi-SWE-RL) | Our semantic review |
| Session viability (external state, unrecoverable repo) | ~99% (SWE-Together) | High-precision subset only | `no_source_fixture` 517 (C1) / 319 (C2) |

**The most transferable single fact is how badly conjunctive LLM filters hurt recall.** SWE-rebench V2's issue-quality
judges, measured against human labels [V~]:

| combination | precision | recall | F1 |
|---|---|---|---|
| average of judge scores | 0.73 | 0.31 | 0.43 |
| strict consensus | 0.88 | **0.06** | 0.11 |

Our classifier keeps a seed only if five conditions all hold: executable tests, a coding request, runtime
feasibility, a named reproduction and a route. If each condition independently had 90% recall, the conjunction would
keep about 59% of good seeds; at 80% each it keeps about 33% [I].

---

## §3 Q2: Deriving tasks from interaction traces (not commits)

### 3.1 Direct analogues of what we do

**SWE-Together** ([2606.29957](https://arxiv.org/abs/2606.29957)) [V~] is the closest published pipeline to ours.

- **Funnel.** 11,260 recorded sessions -> **109 tasks (0.97%)**. By source:

  | source | sessions | tasks |
  |---|---|---|
  | DataClaw | 2,228 | 29 |
  | Pi-staging | 2,397 | 23 |
  | Hyperswitch | 784 | 9 |
  | SWE-chat | 5,851 | 48 |

- **Selection.** Sessions must have "recoverable repository commits, clear user intents, and concrete outcomes".
  The pipeline runs three steps:
  1. A deterministic filter: several genuine user messages, concrete agent edits, sessions where the human did not
     author the change.
  2. An LLM viability screen that rejects work that "depends on external state, such as pull-request management,
     issue triage, deployment operations, private credentials, or live-service state".
  3. A task-generation agent in a sandbox.
- **Grading.** Deterministic verifiers were judged insufficient ("misaligned with intent in both directions"). The
  primary signal is a **weighted rubric fixed offline before any candidate is seen**, with a success threshold
  τ = 0.85. Even the reference patches reach only ~78% pass (mean judge score 0.90). The shortfall is attributed to:
  - process requirements (~35% of unmet goals);
  - extraction noise from multi-commit work;
  - imperfect human solutions.
- **User simulator.** Grounded in the original user's objective, constraints and follow-up messages. Each turn it
  chooses one of: no-op, question, redirect, new requirement, check external. Annotators could tell simulated from
  real users no better than chance (46% Turing pass rate).
- **Results** (pass@1, opencode harness): Opus 4.8 63%, GPT-5.5 58%. A "User Correction" count correlates with
  capability at Pearson **-0.92**.
- **Lessons for us [I]:**
  - A ~1% session-to-task conversion is what a **high-precision** filter yields when sessions are not pre-selected.
    Our measured 10 shipped from ~370 coding sessions (~2.7%), and 23 READY from 500 in C2, are already above that,
    because our fixture is recoverable at every turn.
  - Their rubric-judge primary signal would fail our "executed test is the one truth source" rule. Their
    reference-pass rate (~78%) shows what happens when the grader is not executable.
  - Their simulator design (trajectory-conditioned, anchored to the original messages) is the right model if we ever
    build interactive steps.

**SWE-chat** ([2604.20779](https://arxiv.org/abs/2604.20779)) [V~]. About 18,000 real sessions logged through
Entire.io (~69% Claude Code).
- Users "interrupt or push back on **50.2%** of prompts". Soft pushback (corrections, rejections, failure reports)
  follows ~46% of prompts; hard interruptions are 3.5-5.2%.
- Only **59.4%** of agent-written code survives into commits. Human deletions account for 29.8% of agent lines;
  agent self-overwrites for 8.1%.
- SWE-Touch (W2 §1.13) adds that **59.0% of SWE-chat sessions contain user edits** to the repository.
- **Implication [I]:** correction moments are dense in long sessions. Our 274 persistent-defect and 193
  spec-correction moments over 303 seeds fit that. The scarce resource is not the correction; it is the correction
  that has an **executable, deterministic symptom**.

**SWE-INTERACT** (W2 §1.13; [2606.30573](https://arxiv.org/abs/2606.30573)) [V].
- Uses the same harness family as ours. Going from single-turn to multi-turn halves scores: GPT 5.5 48.0 -> 24.7,
  Opus 4.8 50.7 -> 26.7.
- The two largest failure labels are a forgotten earlier-turn requirement (~34%) and implementation bugs (~34%).
- Its "Expert Nitpicker" persona was built from real data.

**CodeAssistBench** ([2507.10646](https://arxiv.org/abs/2507.10646)) [V~].
- **Satisfaction conditions** are extracted from the full issue conversation. Each must come with an explanation that
  "cites or paraphrases thread evidence", and conditions that fail a verifier are discarded.
- Human audit of 663 conditions: **86.3% accurate, 65.7% complete**. The pipeline is precision-first.
- This is the published analogue of our "148 checkable rules from 95 seeds", and a way to run the **Dropped
  Requirements** QC check: list the conditions, then require each to map to a check or to a declared omission [I].

**Saving SWE-bench** ([2510.08996](https://arxiv.org/abs/2510.08996)) [V~] (W2 §1.13).
- Telemetry from about 6k users: real requests run 10-30 words and mostly mention file paths and stack traces.
- Rewriting benchmark issues into 11 user-style templates cuts resolve rates by **23-54% relative**.
- Our rule that instructions stay in the user's own register is supported by this.

**BigCodeArena** ([2510.08697](https://arxiv.org/abs/2510.08697)) and **BenchBuilder / Arena-Hard-Auto**
([2406.11939](https://arxiv.org/abs/2406.11939)) [V~].
- BigCodeArena: 14,123 conversations -> **4,731 (33.5%)** after dropping those with fewer than two turns or no
  execution.
- BenchBuilder scores crowd prompts on seven qualities (specificity, domain knowledge, complexity, problem-solving,
  creativity, technical accuracy, real-world application). It discards prompts scoring below 6 and clusters averaging
  below 5, going from 200k prompts and 4k clusters to 500 prompts.
- Both show that conversation logs support a **cheap LLM pre-score before costly authoring**. Neither checks
  executability.

**GLM-5 ToolCall-Badcase** [V]: "derived from failure cases in tool invocation scenarios reported by users in
production". It has 200 cases, each run through "multiple rounds of review, rewriting, and validation to remove
ambiguity". An industrial instance of turning **user-reported failures into tasks**.

### 3.2 Mining corrections and failure moments: what the feedback literature measured

- **Naturally Occurring Feedback** ([2407.10944](https://arxiv.org/abs/2407.10944)) [S]: feedback appears in 485 of
  1,000 multi-turn WildChat conversations and 454 of 1,000 LMSYS ones. Conversations with feedback average 5.5 turns.
- **User Feedback in Human-LLM Dialogues** ([2507.23158](https://arxiv.org/abs/2507.23158), EMNLP 2025) [V~]:
  - Feedback is "more than half of user utterances at later turns". By my tally, 72% (LMSYS) and 92% (WildChat) of
    annotated later turns carry it.
  - Refusals cause under 3% of it. Users react to unsatisfactory answers.
  - Automatic fine-grained detection reaches only **41.6%** accuracy.
- **Implication [I].** Detecting *that* a user complained is easy. Classifying *what* the defect is from text is
  unreliable. So our correction classes should be confirmed by **execution** (a reproduction across snapshots), not by
  an LLM label. This also explains our internal finding that correction-rich traces did not predict READY (9 of 33
  with corrections vs 19 of 42 without).

### 3.3 Relabeling trajectories as tasks (backward construction)

- **Learn-by-Interact** ([2501.10893](https://arxiv.org/abs/2501.10893)) [V~]:
  - Every contiguous sub-trajectory T[i:j] of a full trajectory is relabeled with an instruction it actually
    accomplishes, either a summary of the steps or an abstraction of their purpose. This gives "a quadratic increase
    in the number of synthesized examples".
  - An LLM committee (Gemini 1.5 Pro + Claude 3.5 Sonnet, unanimous) then filters: 114,769 examples -> 42,639
    (~37%). On SWE-bench, 41,237 -> 10,232 (24.8%).
  - Backward construction beats distilling the original instruction (Claude 3.5: SWE-bench 54.0 -> 60.0, OSWorld
    12.9 -> 22.5).
- **OS-Genesis** ([2412.19723](https://arxiv.org/abs/2412.19723)) [S] uses the same idea for GUI agents: explore, then
  turn the observed state-action pairs into low-level and high-level instructions, then filter with a reward model.
- **Transfer [I].** In our traces, a span between two snapshots where the user ended up satisfied is a candidate
  **feature task**: "make the repo at turn i do what it does at turn j", graded by tests derived from the turn-j
  behaviour. This is the `repo_task` route and can be mined automatically over every accepted span. Expect heavy
  filtering (the published keep rates are 25-60%), and keep only spans whose turn-j behaviour is executable and stable.

### 3.4 Agent-introduced bugs: the persistent-defect analogue (BugPilot)

**BugPilot** ([2510.19898](https://arxiv.org/abs/2510.19898)) [V~]: "Whenever the feature breaks an existing test, a
bug is created". The repository state at that moment becomes the task.

FeatAdd bugs compared with SWE-Smith bugs:

| measure | FeatAdd | SWE-Smith |
|---|---|---|
| files changed (avg) | 4.2 | 1.2 |
| lines of code | 206.5 | 8.8 |
| Claude Sonnet 4 resolve rate (4 attempts each) | **41.4%** | 65.9% (R2E-Gym 63.5%) |
| GPT-5 resolve rate | 53.4% | 77.5% |

- The bug categories are the most evenly spread of any dataset compared.
- Deliberate bug injection ("BugInstruct") gave "no visible gains" in training; unintentional bugs were "much more
  efficient training examples".

**Transfer [I].** Our `persistent_defect`, `revert_request` and `constraint_assertion` moments include many cases
where the trace's assistant **broke previously working behaviour while adding something**. The `019ef635` trace is one:
the agent rewrote `core.py`, dropped the timecode output and broke an import. That is the FeatAdd mechanism with a
real user attached, so it is F3 by construction. Detect it mechanically:

1. Behaviour X passes at snapshot t0 (run the reproduction).
2. X fails at a later snapshot t1 after an assistant edit.
3. The user complains between t1 and t2, or the accepted state restores X.

These seeds should rank first. W2 had already flagged FeatAdd as the hardest published bug class.

### 3.5 Locating the failure moment: counterfactual replay versus bisecting over turns

- **AgenTracer** ([2509.03312](https://arxiv.org/abs/2509.03312)) [S] builds training data with two techniques:
  programmatic fault injection on successful trajectories, and counterfactual replay on failed ones (replace an
  agent's action with oracle guidance to find the step after which failure is inevitable). That gives 2,000+
  trajectory/error-step pairs. It reports that frontier reasoning LLMs attribute failures with accuracy "generally
  below 10%".
- **Transfer [I], recommendation R3.** We do not need an LLM to attribute anything. Every assistant turn has a
  snapshot, so the designer's reproduction (which must exist before any test is written) can be **run across the
  snapshots like a `git bisect`**. That gives the first failing snapshot, the snapshot where the symptom is visible to
  the user, and the accepted snapshot where it is fixed.
  - The faithful fixture is the snapshot before the user's complaint.
  - "Failure-mode reproduction" for QC is then a receipt: reproduction fails at snapshot k, passes at the accepted
    snapshot, and the user's message at turn k quotes the symptom.
  - The cost is a few container runs per seed.

### 3.6 Multi-turn sessions as multi-step tasks

| source | how steps are built | result | tag |
|---|---|---|---|
| GLM-5 chained tasks | commits grouped by semantic DP; cumulative test patches; the agent continues from its own prior state | Opus 4.5 61.6% / GLM-5 52.3% pass@1 per task | [V] |
| SWE-Flow | runtime dependency graph of unit tests -> ordered TDD steps; partial codebase per step | 2,020 test steps from 12 projects (cryptography 474, pandas 223) | [V~] |
| Web-Bench ([2505.07473](https://arxiv.org/abs/2505.07473)) | 50 projects x 20 sequential tasks | best (Claude 3.7) 25.1% pass@1 | [S] |
| SlopCodeBench ([2603.24755](https://arxiv.org/abs/2603.24755)) | 36 problems, 196 checkpoints; specs evolve | best agent passes 14.8% of checkpoints; none solves a problem end-to-end | [V~] |
| SWE-INTERACT | simulator withholds requirements | scores halve; a forgotten earlier requirement is ~34% of failures | [V] |

**Tension with our receipts.** All 4 of our two-step coding tasks are stuck. The published multi-step difficulty
mostly comes from **carry-forward**: an earlier requirement that the later step's instruction does not restate (the
forgotten-requirement failure), or degradation that accumulates in the agent's own code. In our design each step's
instruction restates its premise.

**Proposal [I].** If multi-step is retried, step-2 hidden checks should re-assert step-1 obligations that the step-2
instruction never repeats but that the step-1 instruction did publish. This is fair, because the user said it
earlier, and it is the forgotten-requirement mechanism. The cut points come from the trace's own turn sequence,
grouped the way GLM-5 groups commits.

### 3.7 What this means for F3 and the customer's QC items

| QC item | published mechanism to reuse | tag |
|---|---|---|
| Failure-Mode Reproduction | Bisect over turns with the executed reproduction (§3.5); SWE-Universe-style verifier toggles (fails on bug state, passes on fixed state) | [I]/[V~] |
| Goal / Scenario Preserved | SWE-Together's "atomic original-session intents" and Intent Coverage (0.70 x recall + 0.30 x scope precision) as a diagnostic | [V~] |
| Dropped Requirements | CAB satisfaction conditions that cite thread evidence; each maps to a check or a declared omission | [V~]/[I] |
| Additions / Alterations | SWE-Mirror's three-annotator High/Moderate/Inconsistent audit, used as a rubric for authored bytes | [V] |
| Trap Fairness | Every hidden name is published (SWE-Bench Pro interface spec); B2 IMPLICIT_NAMING detector | [V]/[V~] |

---

## §4 Q3: Several distinct hard tasks from one source

### 4.1 Multiplication mechanisms

| mechanism | evidence | distinctness risk | fit for us |
|---|---|---|---|
| **Different root causes in one session** (one task per distinct reference hunk set) | 74 of 150 classified sessions have >= 2 usable corrections (`CODING-TRACES.md`). `019ef635` alone has three genuine defects | Low if the root causes touch different code | **Primary** [I] |
| **Sub-trajectory relabeling** (feature tasks from accepted spans) | Learn-by-Interact: quadratic candidates, 25-60% kept | High: overlapping spans share code | Pick non-overlapping spans only [I] |
| **Incremental steps from one codebase** | SWE-Flow ~260 steps per project; GLM-5 chains | Medium: later steps contain earlier ones | Multi-step tasks, not separate tasks |
| **Composition of bugs** | SWE-smith Combine: 2-4 bugs per instance, 96.9% yield, median F2P 15 vs ~4-7 for single bugs. Breakpoint ([2506.00172](https://arxiv.org/abs/2506.00172)): solve rate falls from **55% to 0%** as more interdependent functions are corrupted | Consumes two root causes per task | Matches our **interlock** doctrine: compose two located defects into one hidden item [I] |
| **Mirror into another fixture** | SWE-Mirror 46% yield, 88% faithful | Medium | F1/F2 only [I] |
| **Rewrite the instruction register** | Saving SWE-bench: same code, user-style query, 23-54% relative drop | **High**: same fix, so a near-duplicate | Use it to pick the *one* register, not to multiply |
| **Input perturbation of hidden tests** | EvalPlus ([2305.01210](https://arxiv.org/abs/2305.01210)): 80x tests, pass@k down 19.3-28.9% [S] | It is the same task | Hardens a task; does not multiply it |
| **Environment variants** (versions, OS, language ports) | SWE-bench-Live, Multi-SWE | Medium | Rarely faithful for traces |
| **Width/depth extension of an atomic task** | TaskCraft ([2506.10055](https://arxiv.org/abs/2506.10055)): ~36-41k tasks by chaining or combining atomic tasks [S] | High unless each new hop is verified | Only as an interlock source |

### 4.2 Guarding against near-duplicates

Published practice:

- **ContextBench** ([2602.05892](https://arxiv.org/abs/2602.05892)) [S]: exact metadata match, then cosine similarity of
  issue embeddings at a threshold of **0.9**, then manual review of borderline pairs.
- **Granite Code** ([2405.04324](https://arxiv.org/abs/2405.04324)) [S]: MinHash/LSH with a Jaccard threshold of **0.7**
  for code.
- **SWE-Bench-CL** ([2507.00014](https://arxiv.org/abs/2507.00014)) [S]: average patch Jaccard of **0.11** (TF-IDF cosine
  0.18) between tasks from the same repos. A baseline for what "independent" looks like.
- **SWE-Mirror** [V] deduplicates multiple mirror samples of one issue.

Proposal for trace-derived siblings [I]:

1. **Executable cross-solve (decisive).** Apply task A's reference patch to task B's fixture, or to the shared base
   snapshot, and run B's objectives. If B's objectives pass, A and B test the same thing. Ship one, or merge them
   into an interlock.
2. **Hunk overlap.** Jaccard of changed line ranges between reference patches above ~0.5 means one root cause.
3. **Instruction similarity.** Embedding cosine >= 0.9 means the same user ask.
4. **Shared hidden inputs.** Two tasks must not grade the same hidden data file.

Our gates already freeze each bundle independently. What is missing is a **cross-bundle** check, keyed on the shared
session.

### 4.3 How many tasks per trace are realistic

| source | tasks per source unit | bar | tag |
|---|---|---|---|
| SWE-smith | ~390 per repo (synthetic) | breaks >= 1 test | [V~] |
| SWE-Flow | ~260 per project | unit tests pass | [V~] |
| CodeAssistBench | ~15 per repo | conditions + container | [V~] |
| SWE-rebench V2 | ~9 per repo | F2P + clarity | [V~] |
| Learn-by-Interact | ~3.9 kept per raw trajectory | committee coherence | [V~] |
| SWE-Together | **~0.01 per session** | real session, rubric judge | [V~] |
| Ours (C2) | 23 READY / 91 surviving sessions ≈ 0.25 per surviving session | pass@1 <= 0.5 on both models, certified | internal |

**Estimate [I].** For a productive session (>= 2 corrections with distinct executable symptoms) the realistic bar is
1-2 hard tasks, and 3 for the richest (multi-file, three or more distinct defects). The hard bar, not the source, is
what binds. In the non-coding pipeline one session produced both a hard task (a-040) and an easy one (a-006), so
siblings from the same session must each be screened.

---

## §5 Q4: Predicting difficulty before expensive authoring

### 5.1 Feature-based predictors

| study | target | best result | what predicts | what does not | tag |
|---|---|---|---|---|---|
| "What Makes Software Issue Resolution Tasks Difficult for Agents?" ([2608.18280](https://arxiv.org/abs/2608.18280); ESEM 2026) | Qwen3-Coder-480B / OpenHands on 45,769 SWE-smith and SWE-rebench tasks | XGBoost AUC **0.863** (any success); pass-rate R² 0.41 | Patch fragmentation and repository scale: `patch_lines_deleted`, `patch_num_hunks`, `patch_hunk_gap_mean`, `repo_top_level_dir_count`; patch-only AUC 0.846, repo-only 0.839 | **Prompt features alone AUC 0.599**; adding them changes AUC by at most 0.002 (they matter only in the mid-band) | [V~] |
| Agent psychometrics ([2604.00594](https://arxiv.org/abs/2604.00594)) | IRT difficulty from LLM-judge rubric + embeddings | held-out-task AUC: SWE-bench Verified **0.842**, Pro 0.759, GSO 0.804, TB2 0.810 (oracle 0.92-0.95); held-out benchmarks only 0.70-0.74 | Adding the **gold solution** and **tests** to the inputs helps (GSO 0.725 -> 0.797) | Out-of-distribution transfer is weak; "treat predictions as screening, not calibration" | [V~] |
| SWE-rebench LLM labels | Task complexity (>1 h) | 81% accuracy (Qwen-72B fine-tuned on 3.8k Verified labels); issue clarity 79%; test correctness 67% | | | [V~] |
| SWE-smith difficulty model | easy/medium/hard | 75.3% accuracy; expert resolve 58.6% / 41.0% / 17.0% by bucket | | Difficulty "correlates with solvability but not with effectiveness as training data" | [V~] |
| Terminal-Bench 3 adjudication ([2609.26826](https://arxiv.org/abs/2609.26826)) | genuine vs tractable | output tokens to first honest pass: AUC 0.750 (median 24,829 vs 12,388) | Needs a solved run, so it cannot pre-screen all-fail tasks | Pass rate alone cannot separate genuine from fake hardness | [V~] |
| SWE-Bench Pro (W2) | resolve rate | falls sharply beyond 3 files | files, repo | | [V] |
| Issue-length study ([2509.13941](https://arxiv.org/abs/2509.13941)) | success vs issue length | non-monotonic | | Length is a weak predictor | [S] |

**Transfer [I].**

- The static predictors measure **patch** properties, and we have the patch: the diff between the defective and
  accepted snapshots is known before authoring. So `n_hunks`, `lines_deleted`, the gap between hunks and the number of
  top-level directories touched can be computed per correction moment at no cost.
- Their target, however, is a single open model on unmodified SWE tasks. Our receipts say our hardness came from the
  **authored mechanism**. The feature model should therefore rank seeds for **validity and supply**, not promise
  P(hard).

**Internal session features that separated READY from stuck** (`SESSION-SELECTION-FOR-YIELD.md` §5.2, thin
evidence):
- input in a published external format with many surface variants (12 of 13 READY in the comparison table);
- a single deliverable (23 of 50 vs 2 of 14);
- the trace's agent wrote no tests (23 READY / 13 stuck, vs 5 / 11 with tests);
- JS or Go.

### 5.2 Solve-rate filtering (keep 0 < k < n)

- **Online Difficulty Filtering** ([2504.03380](https://arxiv.org/abs/2504.03380)) [V~]: the learning signal is
  theoretically maximal at pass rate ~0.5. In practice prompts in a band such as **0.25 <= p <= 0.75** are kept, worth
  +10% on AIME over plain GRPO.
- **Nemotron 3 Super** [V] drops scenarios with all-success or all-failure outcomes. **SWE-Universe** [V~] drops
  "too difficult or too easy" queries using base-model rollouts. **Kimi K2** [V~] assesses difficulty by SFT pass@k.
- **KAT-Coder** [V~]: hints raise zero-pass tasks to roughly 20% pass rate. This is a hint ladder that separates
  "unsolvable as stated" from "hard".
- **Our target differs.** pass@1 <= 0.5 per step on both models is the target, with all-fail admissible after the
  proof obligations. So "keep 0 < k < n" is the wrong rule for us. The right rule is "keep k/n <= 0.5, with every
  zero certified". The KAT hint ladder and the TB3 strictness ladder are the published ways to certify all-fail
  tasks.

### 5.3 Adversarial selection concentrates defects

- **Terminal-Bench 3** [V~]: of 125 tasks no honest run passed, 78 survive as certified-unsolved. The rest were broken
  oracles (14), infrastructure-limited (8), passable only by exploit (4) and uncertified (21).
  - Mean honest pass rate of closed, unmerged task PRs by rejection reason:

    | rejection reason | mean honest pass rate |
    |---|---|
    | insufficient difficulty | 0.64 |
    | verifier-overfit / too hard | 0.10 |
    | ambiguous | 0.17 |
    | broken | 0.18 |

  - So low pass rates come from overfit verifiers, ambiguity and breakage just as much as from real difficulty.
- **Humanity's Last Exam**: its filter dropped questions frontier models answered. The FutureHouse audit then found
  ~29% of chemistry and biology answers wrong or misleading, and Epoch found substantial errors in 22 of 48 sampled
  questions [S] ([the-decoder](https://the-decoder.com/nearly-29-percent-of-humanitys-last-exam-questions-are-wrong-or-misleading/),
  [Epoch](https://epoch.ai/benchmarks/hle/review)).
- **SWE-Bench Pro Verified** (W2): 102 of 731 public instances (14%) were defective, and **75 of the 102** were
  over-narrow tests.
- **Implication.** This is our standing rule that every zero is classified, and the literature quantifies why. Of
  tasks selected for being failed, **roughly a third** are defective rather than hard. Our determinacy probe rejected
  36% of 2,415 candidate cases, which fits.

### 5.4 Cheap probes for our seeds

1. **Static features at zero cost** (§5.1): hunks, lines deleted, hunk gap, directories, files, language, manifest
   presence, single deliverable, external-format input, trace-agent tests. Use them to **rank** [I].
2. **Replay probe, after reproduction (R9) [I].** Once the designer has an executable reproduction (it must exist
   anyway), run one Fable and one Astra episode on the **raw defect**. The instruction is the user's verbatim complaint
   at the cut turn; the grader is the reproduction plus the accepted-state behaviour on the visible input.
   - **Both solve it:** the natural defect is easy. The task needs interlocked authored levers, and its expected
     yield should be cut.
   - **Either fails:** the zero is certified with the ladder, and the seed is an F3 candidate with natural hardness.
   - The cost is 2 episodes. It also feeds the doctrine "test difficulty before validity" with the cheapest possible
     draft.
   - The published bases: agent psychometrics shows execution artifacts (tests, solution) carry the signal text lacks;
     BugPilot shows natural agent-made defects are harder; SWE-Universe and Nemotron filter on actual rollouts, not
     predicted ones.
3. **Do not use a small-model probe as a stand-in for frontier difficulty.** Small models hit the floor on these
   tasks. Agent psychometrics generalises only to agents seen in training, and its accuracy falls to 0.70-0.74 on new
   benchmarks [V~]. Use the target models directly, at n = 1 each.
4. **The trace's own failed fix attempts are a weak prior.** The Debugging Decay Index
   ([2506.18403](https://arxiv.org/abs/2506.18403)) [S] reports that models lose 60-80% of their debugging ability
   within 2-3 attempts. So a defect the trace's agent failed on several times signals model-hard *for that older
   model*. Internally, correction richness did not predict READY, so give it low weight until refit.

---

## §6 Q5: Sessions with no tests: UI, single-file HTML, visual, data, engine-bound

### 6.1 Browser and single-file HTML apps (198 sessions contain HTML; 20 of 150 classified are browser-only)

| benchmark / system | oracle | reliability reported | tag |
|---|---|---|---|
| **HTMLCure / HTMLBench-400** ([2605.26807](https://arxiv.org/abs/2605.26807)) | 6,000 deterministic browser test cases (~15 per item; click, type, hover, key press, resize). Rules: "prompt grounded and observable in the browser"; "must not depend on class names, variable names, frameworks, hidden DOM layout" | No reliability study published; a validator checks schema and logic | [V~] |
| **FrontendBench** ([2506.13832](https://arxiv.org/abs/2506.13832)) | Puppeteer/Jest scripts in a sandbox, 148 prompt/test pairs | **90.54%** agreement with expert evaluation | [S] |
| **WebGen-Bench** ([2505.03733](https://arxiv.org/abs/2505.03733)) | 647 tests generated by GPT-4o, **then filtered and adjusted by humans**, run by a WebVoyager agent (YES/NO/PARTIAL) | 86-94% alignment with humans (aggregator) | [S] |
| **GLM-5 CC-Bench-V2 frontend** | Build check, then Claude Code + Playwright MCP judge per check item | 94% item agreement with experts (130 items); model-ranking Spearman 0.857; disagreements "concentrated on subjective visual-quality criteria" | [V] |
| **ArtifactsBench** ([2507.04952](https://arxiv.org/abs/2507.04952)) | MLLM judge over three-step screenshots + checklist | 94.4% ranking consistency with WebDev Arena; ~91% pairwise agreement with experts | [S] |
| **WebDevJudge** ([2510.18560](https://arxiv.org/abs/2510.18560), ICLR 2026) | meta-evaluation of LLM judges | best pairwise judge **70.34%** vs humans 84.56%; best single-answer 63.91%; failures in "functional equivalence" and "feasibility" | [V~] |
| **Vibe Code Bench** ([2603.04601](https://arxiv.org/abs/2603.04601)) | autonomous browser agent over 964 workflows | **31.8-93.6%** step-level agreement depending on the evaluator | [V~] |
| **Web-Bench** | sequential e2e tests | best 25.1% pass@1 | [S] |

**Conclusion.** Deterministic, prompt-grounded browser scripts are the only option that meets our "executed test"
rule, and they reach ~90% expert agreement when written well (FrontendBench). LLM, VLM and agent judges agree well on
*rankings* but poorly on *individual binary verdicts* (WebDevJudge 70%; Vibe Code Bench as low as 32%). They are fine
as advisory critics and unfit as the grader.

**Practice for our browser lane [I]:**

1. **Interact only through the browser.** Click, type and navigate by visible text or ARIA role (Playwright
   `getByRole` / `getByText`), never by class name, id or internal function name, unless the instruction publishes it
   (the SWE-Bench Pro interface lesson).
2. **Assert observable state:** rendered text, enabled/disabled state, `localStorage` content when the app exposes
   persistence, downloaded files, URL. Pixels are a last resort; if used, compare against the reference build's
   rendering at fixed viewport, fonts and time, with tolerance.
3. **Freeze stochastic inputs:** stub `Date.now` and `Math.random`, wait for network idle, disable animations. The
   HTMLCure paper is silent on this, so it is a known gap.
4. **Hidden inputs are data** (CSV/JSON loaded through the app's own upload or paste path). Format-variant inputs are
   what bit Astra on our READY tasks.
5. **Taste corrections (24) stay ungraded** unless they can be restated as invariants the trace itself states.
   Example: "every UI element present in the previous `index.html` must still be present" is already one of our 148
   rules.

### 6.2 Data, notebook and SQL work

- **ScienceAgentBench** ([2410.05080](https://arxiv.org/abs/2410.05080)) [S]: success criteria are tailored per task and
  derived **by running the annotated reference program**. Example: ROC-AUC >= 0.77 from five reference runs. This
  matches our "derived by running code, never stored" rule.
- **Spider 2.0** ([2411.07763](https://arxiv.org/abs/2411.07763)) [S]: a prediction is correct if every gold column
  appears, which reduces false negatives without adding false positives. Evaluation scripts use `condition_cols` and
  `ignore_order` options.
- **DA-Code** ([2410.07331](https://arxiv.org/abs/2410.07331)) [S]: 500 tasks with purpose-built evaluation scripts;
  best agent 30.5%.
- **Transfer [I].** Use golden outputs produced by the reference at build time, normalised for column order, row
  order where unspecified, float tolerance, whitespace and encoding. Add property checks (schema, row counts,
  conservation invariants). Never a stored literal.

### 6.3 Code with no tests at all (most of our sessions)

- **EvalPlus** [S]: mutation- and LLM-generated inputs (80x more tests) cut pass@k by up to 19.3-28.9% and re-ranked
  models. Input contracts filter out invalid inputs.
- **Property-based testing** [S]:
  - Property-Generated Solver ([2506.18315](https://arxiv.org/abs/2506.18315)): properties are "simpler to define and
    verify than directly predicting exhaustive test oracles".
  - LLM-written PBT alone detects 68.75% of bugs; combined with example tests, 81.25%
    ([2510.25297](https://arxiv.org/abs/2510.25297)).
  - X-PBT: 2.8-15.3% of programs that pass the unit tests fail a PBT.
- **Reproduction-test generation** [S]: ReProAgent ([2607.09123](https://arxiv.org/abs/2607.09123)) 58.43% on SWT-Lite
  and 70.30% on SWT-Verified; Echo ([2603.07326](https://arxiv.org/abs/2603.07326)) 66.28% on SWT-Verified. The
  original SWT-bench baseline was 19.2%.
- **Transfer [I].** Graders for our traces are authored anyway. These numbers say:
  - an agent can turn a described symptom into an executable reproduction for roughly 60-70% of real issues (route
    R5);
  - differential and property cases over hidden inputs measurably raise the share of wrong solutions caught, which
    serves both hardness (Astra's unseen shapes) and validity.

### 6.4 Engine-bound sessions (7 Lua engine, 13 "no runtime" among 150 classified)

**[I]** The tools below exist and are widely used, but none was tested here.

- **FPGA HDL:** Verilator, Icarus Verilog and GHDL are open-source simulators that run headless in Docker; cocotb
  writes testbenches in Python. HDL should arguably be **reclassified as native**.
- **Godot / GDScript:** the engine runs `--headless`; GUT is a test framework.
- **Flutter / Dart:** `flutter test` runs widget and unit tests headless.
- **Jenkins Groovy:** JenkinsPipelineUnit unit-tests pipeline scripts on the JVM.
- **Lua:** plain Lua is native. Roblox code is bound to the engine API. Roblox's Jest Lua "currently runs only inside
  Roblox" [S]
  ([yarn listing](https://classic.yarnpkg.com/en/package/@jsdotlua/jest-environment-roblox)), so Roblox seeds need
  either pure-logic extraction or authored API stubs (declared, zero fidelity credit).
- **Unity C#:** only logic that does not touch `UnityEngine` can be tested with `dotnet test`.
- **MQL5:** no open runtime. Keep killed.

**Expected value is low but positive.** Compiled-language environment success runs 2-4x below Python/JS/Go
(Multi-Docker-Eval, SWE-Mirror). These would be a **repair route with an explicit faithfulness note**, not a default.

### 6.5 What fraction becomes usable

- HTMLCure kept 63,703 "quality cleared" pages from a 97K prompt corpus (66%), but that is for training pages, not
  graded tasks [V~].
- WebGen-Bench needed human filtering of model-written tests [S].
- **Our estimate [I].** If the browser lane admits single-file apps with a stateful behaviour the user complained
  about (such as the `019fb770` booking dashboard), and excludes taste-only and canvas/animation-only work, expect
  about half of browser-only sessions to yield a gradable seed. After that, the same validity and hardness funnel as
  native seeds applies.

---

## §7 Q6: Recommendations for our funnel

### 7.1 Where filters can be relaxed safely

| current gate | change | evidence | risk and control |
|---|---|---|---|
| Five conjunctive classifier conditions | Score each condition; rank by expected yield (§7.5); send borderline seeds to a cheap executed check (reproduction attempt) instead of killing them | SWE-rebench V2 consensus recall 0.06 vs averaging 0.31; conjunction arithmetic | More authoring spent on dead seeds. Control: rank cut-off plus the replay probe |
| "Named reproduction" required at classification | Require a **symptom string** (error text, wrong output, user's "still X") plus a defective/accepted snapshot pair; the designer's reproduction agent decides | Reproduction agents 58-70% on SWT-bench | Some false starts, caught by the reproduce-first rule |
| Route = "two usable corrections incl. one located root cause" | Accept **one** correction with an executable symptom, or an agent-introduced regression found by snapshot bisection | Correction richness did not predict READY (9/33 vs 19/42); BugPilot shows one natural regression suffices | None new: validity gates are unchanged |
| `no_runtime` for HDL, Godot, Flutter, Groovy, plain Lua | Reclassify HDL as native; open a runtime-shim route for the others | §6.4 | Lower environment yield; faithfulness note for any authored stubs |
| `runtime_infeasible` caused by external APIs or keys | Record and replay HTTP fixtures (VCR-style cassettes) as declared authored bytes; DB through service containers | SWE-Together rejects external-state sessions outright [V~]; B3 EXTERNAL_DEPENDENCY [V~] | Additions vs Source in QC; declare it in the ledger |
| "Not a coding request" / "document" | Hand off to the sibling non-coding pipeline's intake list (do not edit the sibling) | 340 such kills in C2 | None |

**Do not relax:** engine-bound code with no open runtime (MQL5, Roblox API), taste-only corrections, sessions whose
deliverable is external state, and seeds with no recoverable bytes at all (except through the opt-in mirror lane).

### 7.2 Turning kill reasons into repair routes

| kill reason (C1 / C2 counts) | repair route | expected recovery [I] | faithfulness |
|---|---|---|---|
| `no_source_fixture` (517 / 319) | (a) **Rebuild the tree by replaying `write_file` / `edit_file` payloads** from the trace when the snapshot is missing but the tool calls carry the bytes. (b) Otherwise, the opt-in mirror lane | (a) depends on how many have payloads, so measure it first; (b) ~46% mirror yield | (a) F3/F2: the bytes are the trace's own; (b) F1/F2 |
| `no_runtime` (197 / 50) | Runtime-shim route (§6.4) | small share; compiled languages 2-4x lower | F3 if real code runs unmodified |
| `runtime_infeasible` (95 / 152) | Recorded HTTP fixtures, service containers, CPU fallback, base-image templates (KAT: 16.5% -> 57.2%) | about one-third [I] | declared authored bytes |
| `no_reproduction` (125 / 427) | Reproduction agent plus bisection over turns, when a symptom string exists | ~50-60% of those with a symptom | F3 |
| `not_gradable` (139 / 435) | Browser lane, golden-output lane, PBT lane; kill only taste or subjective work | about one-third [I] | F3/F2 |
| `human_judgement` (27 / 295) | Restate as invariants the trace states verbatim (guardrails); otherwise kill | small | F3 when quoted |
| Review block for an over-specified test | §7.3 | the largest downstream gain | n/a |

### 7.3 The reviewer bottleneck: over-specified hidden tests

The evidence that this is the dominant failure everywhere, not just for us:

- SWE-Bench Pro Verified: **75 of 102** defects were over-narrow tests [V].
- SWE-rebench V2 created a dedicated label for it, B2 IMPLICIT_NAMING [V~].
- SWE-Bench Pro needed an interface spec to stop name mismatches from turning into false negatives (GPT-5 25.9% vs
  8.4% without) [V].
- SWE-Together gave up on deterministic tests as its primary signal for the same reason [V~].

What to adopt [I]:

1. **Observable-interface rule, checked mechanically.** A hidden test may reach the repo only through:
   - a CLI or entry point the fixture already exposes;
   - an HTTP route;
   - files read or written;
   - browser-visible behaviour;
   - a module or function name that exists in the fixture **or** is quoted in the instruction.

   `check_draft` already advises on "a name only the reference introduces". The next step is a static import and
   symbol scan over `tests/suites/` against (fixture symbols ∪ instruction-quoted symbols), with any hit sent to the
   designer as a repair item.
2. **N-version adjudication.** Raise the alternative control from one independently written fix to **2-3 written by
   different vendors' models**, each given only the instruction and repo. Any objective check that fails a compliant
   alternative goes to adjudication: over-specified, or the alternative is wrong. This is the Self-Challenging rule:
   keep a verifier only if the example solution passes and none of the failure cases pass
   ([2506.01716](https://arxiv.org/abs/2506.01716)) [S]. It is also the published remedy for "functional equivalence"
   failures (WebDevJudge).
3. **Precision-first rule extraction with thread citations** (CAB: 86% accurate, 66% complete). Each check already
   quotes its sentence. Add the converse audit: every extracted user condition maps to a check or to a declared
   omission. That is the Dropped Requirements item.
4. **Pre-review only drafts already measured hard** (internal recommendation, `SESSION-SELECTION-FOR-YIELD.md` §6),
   because review is the scarcest step.

### 7.4 Tasks per trace

- Plan for **1 task per root cause**, and at most **2-3 per session**.
- Prefer composing two root causes from the same session into **one interlocked item**, because the interlock
  doctrine says single levers measured dead.
- A third separate task from the same session should pass the executable cross-solve check (§4.2) and be screened on
  its own.
- Budget assumption for planning [I]: x1.2-1.5 tasks per productive session over today's ~1.

### 7.5 Ranking seeds by expected hard-task yield

**EY(seed) = P_rt × P_rep × P_val × P_hard × N_distinct**

Feature sources and suggested starting priors [I]. Refit on our ~185 built bundles (C1 69 + C2 115) as
`SESSION-SELECTION-FOR-YIELD.md` recommends: logistic regression or gradient boosting, used for ranking only.

| factor | features (computable before authoring) | starting prior / direction | basis |
|---|---|---|---|
| P_rt (runtime) | language class; manifest present; repo size; external API, key or service dependency; engine API imports | Python/JS/Go native ~0.9; TS/PHP ~0.8; Java/C/C++ ~0.6; browser single-file ~0.8 with the lane; engine-bound ~0.2 with shims; external-state deliverable ~0 | §2.3; SWE-Mirror and Multi-Docker-Eval by language |
| P_rep (reproduction) | user quotes an error, wrong output or concrete symptom; defective/accepted snapshot pair exists; deterministic inputs; bisection finds a flip | with symptom + pair ~0.6; text-only complaint ~0.3 | SWT-bench agents 58-70%; SWE-Factory 40%; SWE-Mirror 46% |
| P_val (validity) | single deliverable; behaviour reachable through an observable interface; no taste or visual-only acceptance; user accepted the final state | C1 0.23, C2 0.49 today; higher when an observable interface exists | internal; SWE-Bench Pro interface effect |
| P_hard | external-format input with surface variants; single deliverable; 6-20 files; trace agent wrote no tests; JS/Go; **agent-introduced regression**; patch fragmentation (hunks, deletions, hunk gap); replay-probe failure | C1 0.31, C2 0.41 of validated; multiply up for regression and format-variant seeds, down when the replay probe is solved twice | internal §5.2; BugPilot; [2608.18280](https://arxiv.org/abs/2608.18280) |
| N_distinct | distinct root causes: non-overlapping reference hunks across correction moments that survive cross-solve | 1 for most, 2 for productive, 3 at most | §4 |

### 7.6 A yield model

**Measured today** (C1 = this checkout; C2 = sibling checkout on a different dump; brief):

| measure | C1 | C2 | recent batch |
|---|---|---|---|
| bundles built | 69 | 115 | ~70 |
| validated / built | 16 / 69 = 0.23 | 56 / 115 = 0.49 | |
| hard and certified / validated | 5 / 16 = 0.31 | 23 / 56 = 0.41 | |
| hard per coding session (370) | **0.014** | **0.062** (on its own 500-session dump) | |
| shipped | | | 10 of ~70 built (0.14 per built), ~0.027 per coding session |

Published comparison: SWE-Together converts **0.0097** tasks per raw session, with no hardness filter.

**Projection** [I], scenario arithmetic, not a forecast. Every factor is a hypothesis to be measured.

| lever | multiplier on built tasks | basis |
|---|---|---|
| R1: classify all 370 with scoring instead of conjunctive kills | x1.3-1.5 | recall arithmetic (§2.4) |
| R5/R6/R8 + API-stub route (repair routes) | x1.1-1.2 | §7.2 |
| R7: distinct root causes, interlocks, cross-solve dedupe | x1.2-1.4 | §4.3 |
| R4: observable-interface rule + N-version adjudication (P_val -> ~0.5-0.6) | applies to validated/built | SWE-Bench Pro interface effect; C2 already at 0.49 |
| R2/R9: regression-first ranking + replay probe | P_hard ~0.35-0.45 | internal C2 0.41; BugPilot |

Result: built ≈ 70 × 1.4 × 1.15 × 1.3 ≈ **145**; validated ≈ 145 × 0.5-0.6 ≈ 73-87; hard ≈ 73-87 × 0.35-0.45 ≈
**26-39**; shipped after QC and shipcheck ≈ **20-30 per 370 coding sessions**. That is roughly 2-3x today's 10.

The binding factors stay the ones `SESSION-SELECTION-FOR-YIELD.md` names: session supply and per-task hardness. No
literature supports a session-level hardness predictor strong enough to change that.

### 7.7 What the literature says not to do

- **Do not adopt a rubric or VLM judge as the grader** to make UI sessions "gradable". The best binary agreement is
  ~70% (WebDevJudge), and as low as 32% in Vibe Code Bench. SWE-Together's reference patches pass only 78% under its
  own rubric judge.
- **Do not multiply tasks by rewording the instruction or perturbing inputs.** Those give near-duplicates. Use them
  to harden a task, not to count it twice.
- **Do not treat LLM correction labels as ground truth.** Fine-grained feedback detection is 41.6% accurate; confirm
  each label by execution.
- **Do not keep tasks just because nobody solved them.** About a third of failed-by-everyone tasks are defective
  (TB3, HLE, SWE-Bench Pro Verified).
- **Do not use strict-consensus LLM filters for selection.** Recall collapses (0.06).

---

## §8 Open questions and what to measure next

1. **Byte replay for `no_source_fixture`:** what fraction of the 517 (C1) kills carry `write_file` / `edit_file`
   payloads complete enough to rebuild the tree? This is the largest single kill bucket.
2. **Regression seeds:** how many of the 274 persistent-defect moments are agent-introduced regressions (a behaviour
   passes at an earlier snapshot)? Bisection gives a mechanical count.
3. **Replay-probe calibration:** on already-screened bundles, does a raw-defect replay solved at n = 1 per model
   predict "stuck", and does a failure predict "READY"? This can be back-tested on existing receipts.
4. **Cross-solve dedupe:** run it over existing bundles that share a session to measure today's hidden overlap.
5. **Browser lane pilot:** ten single-file HTML seeds through Playwright-only graders with the observable-interface
   rule, then measure the validated rate and review outcomes.
6. **Refit the seed-ranking model** (§7.5) on ~185 bundles with trace-derived features; report AUC for validated and
   for READY separately.

---

## §9 Sources (primary unless noted)

**Funnels and environment construction**
- SWE-rebench https://arxiv.org/abs/2505.20411 ; SWE-rebench V2 https://arxiv.org/abs/2602.23866
- SWE-bench-Live https://arxiv.org/abs/2505.23419 ; RepoLaunch https://arxiv.org/abs/2603.05026 , https://github.com/microsoft/RepoLaunch
- SWE-Factory https://arxiv.org/abs/2506.10954 ; Skywork-SWE https://arxiv.org/abs/2506.19290 ; SWE-Gym https://arxiv.org/abs/2412.21139
- SWE-smith https://arxiv.org/abs/2504.21798 ; R2E-Gym https://arxiv.org/abs/2504.07164 ; SWE-Dev (THUDM) https://arxiv.org/abs/2506.07636
- SWE-Flow https://arxiv.org/abs/2506.09003 ; SWE-Synth https://arxiv.org/abs/2504.14757 ; BugPilot https://arxiv.org/abs/2510.19898
- SWE-Mirror https://arxiv.org/abs/2509.08724 ; SWE-Next https://arxiv.org/abs/2603.20691 ; SWE-Universe https://arxiv.org/abs/2602.02361
- SWE-Hub https://arxiv.org/abs/2603.00575 ; MEnvAgent https://arxiv.org/abs/2601.22859 ; KAT-Coder-V2.5 https://arxiv.org/abs/2607.05471
- Multi-SWE-bench / Multi-SWE-RL https://arxiv.org/abs/2504.02605 ; Multi-Docker-Eval https://arxiv.org/abs/2512.06915
- EnvBench https://arxiv.org/abs/2503.14443 ; SetupBench https://arxiv.org/abs/2507.09063 ; Repo2Run https://arxiv.org/abs/2502.13681
- DeepSeek-V3.2 https://arxiv.org/abs/2512.02556 ; GLM-5 https://arxiv.org/abs/2602.15763 ; Kimi K2 https://arxiv.org/abs/2507.20534
- Nemotron 3 Super https://arxiv.org/abs/2604.12374 ; Qwen3-Coder https://qwenlm.github.io/blog/qwen3-coder/ (secondary)
- CLI-Universe https://arxiv.org/abs/2606.22883 ; Meta-Task https://arxiv.org/abs/2607.27929

**From interaction traces and conversations**
- SWE-Together https://arxiv.org/abs/2606.29957 ; SWE-chat https://arxiv.org/abs/2604.20779 , https://huggingface.co/datasets/SALT-NLP/SWE-chat
- SWE-INTERACT https://arxiv.org/abs/2606.30573 ; SWE-Touch https://arxiv.org/abs/2608.02499 (via W2)
- CodeAssistBench https://arxiv.org/abs/2507.10646 ; Saving SWE-bench https://arxiv.org/abs/2510.08996
- BigCodeArena https://arxiv.org/abs/2510.08697 ; BenchBuilder / Arena-Hard-Auto https://arxiv.org/abs/2406.11939
- Naturally Occurring Feedback https://arxiv.org/abs/2407.10944 ; User Feedback in Human-LLM Dialogues https://arxiv.org/abs/2507.23158
- Learn-by-Interact https://arxiv.org/abs/2501.10893 ; OS-Genesis https://arxiv.org/abs/2412.19723
- AgenTracer https://arxiv.org/abs/2509.03312 ; TaskCraft https://arxiv.org/abs/2506.10055 ; Self-Challenging Agents https://arxiv.org/abs/2506.01716
- DataClaw https://github.com/peteromallet/dataclaw (tool, secondary)

**Multiplication, dedupe, difficulty**
- Breakpoint https://arxiv.org/abs/2506.00172 ; EvoEval https://arxiv.org/abs/2403.19114 ; EvalPlus https://arxiv.org/abs/2305.01210
- ContextBench https://arxiv.org/abs/2602.05892 ; SWE-Bench-CL https://arxiv.org/abs/2507.00014 ; Granite Code https://arxiv.org/abs/2405.04324
- What Makes Issue Resolution Tasks Difficult https://arxiv.org/abs/2608.18280 ; Agent psychometrics https://arxiv.org/abs/2604.00594
- What Makes a Terminal-Bench Task Hard https://arxiv.org/abs/2609.26826 ; Online Difficulty Filtering https://arxiv.org/abs/2504.03380
- Debugging Decay Index https://arxiv.org/abs/2506.18403 ; Failures in automated issue solving https://arxiv.org/abs/2509.13941
- HLE audits (secondary): https://the-decoder.com/nearly-29-percent-of-humanitys-last-exam-questions-are-wrong-or-misleading/ , https://epoch.ai/benchmarks/hle/review
- SWE-Bench Pro https://arxiv.org/abs/2509.16941 ; SWE-Bench Pro Verified https://arxiv.org/abs/2609.08149 (via W2)

**Untestable sessions**
- HTMLCure / HTMLBench-400 https://arxiv.org/abs/2605.26807 ; FrontendBench https://arxiv.org/abs/2506.13832 ; WebGen-Bench https://arxiv.org/abs/2505.03733
- ArtifactsBench https://arxiv.org/abs/2507.04952 ; WebDevJudge https://arxiv.org/abs/2510.18560 ; Vibe Code Bench https://arxiv.org/abs/2603.04601
- Web-Bench https://arxiv.org/abs/2505.07473 ; SlopCodeBench https://arxiv.org/abs/2603.24755 ; I-WebGenBench https://arxiv.org/abs/2606.00750
- ScienceAgentBench https://arxiv.org/abs/2410.05080 ; Spider 2.0 https://arxiv.org/abs/2411.07763 ; DA-Code https://arxiv.org/abs/2410.07331
- Property-Generated Solver https://arxiv.org/abs/2506.18315 ; LLM PBT edge cases https://arxiv.org/abs/2510.25297
- ReProAgent https://arxiv.org/abs/2607.09123 ; Echo https://arxiv.org/abs/2603.07326 ; AssertFlip https://arxiv.org/abs/2507.17542
- Vibe Checker https://arxiv.org/abs/2510.07315

**Internal cross-references:** `analysis/SESSION-SELECTION-FOR-YIELD.md`, `analysis/CODING-TRACES.md`,
`analysis/web-research/W2-swe-bench-family.md` (§1.2, §1.9, §1.13).

**Correction to W2 §1.9:** W2 says PR-mirror tasks were the hardest and LM-modify the easiest in SWE-smith's runs.
SWE-smith's Table 4 is in fact the SWE-bench Verified score of a student model fine-tuned on each strategy's
trajectories ("Comparison of training on 1000 SWE-smith instances created with different strategies"), so it measures
training usefulness, not difficulty. SWE-smith reports expert difficulty only by difficulty bucket (58.6% / 41.0% /
17.0%) and in aggregate (a 36% resolve rate over 17,906 attempts) [V~].
