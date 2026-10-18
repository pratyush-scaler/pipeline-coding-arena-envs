# W2: SWE-bench family, test adequacy, and what makes repository tasks hard (web research, 2026-10-03)

Scope: repository-level issue-resolution and feature benchmarks (SWE-bench and its descendants, performance,
feature, long-horizon and interaction variants) plus the test-adequacy literature. Written for building hidden-test
coding environments that hold pass@1 at or below 0.5 for Fable 5.1 (Claude Code) and GPT 6 Astra (Codex), both at
high effort with no turn or time limits.

---

## §0 Method and evidence tags

Every claim was gathered with generic web queries and by reading the primary sources. For eight papers I pulled the
full PDF and read the extracted text directly: SWE-Bench Pro, SWE-Bench Pro Verified, DeepSWE, SWE-EVO, SWE-Touch,
SWE-INTERACT, RoadmapBench and SWE-Lancer. Other papers were read through an HTML fetch that runs a summariser, so a
single number from those can be paraphrased, though the structure and direction are reliable.

| tag | meaning |
|---|---|
| **[V]** | Read in the primary source (the paper text, repo or leaderboard). For the eight papers above it was the PDF text itself. |
| **[V~]** | Primary source read through a summarising fetch. The number is very likely right, but the exact wording was not checked. |
| **[S]** | Comes only from a search-result summary or a third-party aggregator. Treat it as a pointer to check. |
| **[I]** | My own inference. It has not been measured. |

Caveats that apply throughout:

1. **Harness and budget.** Many published failure taxonomies were produced under turn or cost caps. SWE-Bench Pro's
   analysis used 50 turns and $2 [V] ([2509.16941](https://arxiv.org/abs/2509.16941)). Under those caps "not
   submitted", "context overflow" and "stuck in loop" are mostly artefacts of the cap. Our harness has no limits, so
   those categories say almost nothing about our setting.
2. **Model vintage.** Most 2024-25 evidence comes from Claude 3.5/3.7/4 and GPT-4o/o1/GPT-5. Our targets are a
   generation or more later. Where only older evidence exists, the technique is a **hypothesis for frontier models**.
   The evidence closest to our setting is from 2026 papers that run Opus 4.7/4.8 and GPT-5.4/5.5: DeepSWE, SWE-INTERACT
   (Claude Code and Codex CLI at high reasoning through Harbor), SWE-Touch, RoadmapBench, SWE Atlas and FeatureBench.
3. **Saturation of the base task shape.** Aggregators report that Fable 5.1 scores 81.2% on SWE-bench Pro (attributed
   to Anthropic's system card) and that Fable 5 scored 95.0% on SWE-bench Verified
   [S] ([benchlm](https://benchlm.ai/models/claude-fable-5-1), [codingfleet](https://codingfleet.com/blog/swe-bench-pro-leaderboard-2026/)).
   GPT-6 Astra is reported at 74.1% on DeepSWE v1.1 [S] ([mindstudio](https://www.mindstudio.ai/blog/gpt-6-astra-benchmarks-analysis)).
   For Fable 5.1 on DeepSWE the aggregators conflict (67.4% versus 73.8%, [S]). Scale's standardised SWE-bench Pro
   public set topped out at 61.5% in Sept 2026, and its commercial set at 51.5% [S]
   ([morphllm](https://www.morphllm.com/swe-bench-pro)). **Implication [I]:** a faithfully constructed issue-resolution
   task of SWE-bench Pro shape starts near pass@1 ≈ 0.6-0.8 for our targets. Difficulty has to come from specific
   mechanisms, not from the task shape.
4. **Vendor scores may be inflated by leakage.** SWE-Bench Pro containers ship the full `.git` history. Removing the
   leak dropped GLM-5.2 from 78.80% to 57.32% [V] ([2609.08149](https://arxiv.org/abs/2609.08149)). DeepSWE's judge
   classed about 18% of Opus 4.7's and 25% of Opus 4.6's SWE-Bench Pro passes as improper [V]
   ([2607.07946](https://arxiv.org/abs/2607.07946)). Our fixtures must ship no future commits (see §4).

---

## §1 Per-benchmark sections

### 1.1 SWE-bench / Lite / Verified (and OpenAI's annotation and retirement audit)

- **Construction.** The original benchmark pairs a GitHub issue with the PR that fixed it. FAIL_TO_PASS tests come from
  the PR's test changes and PASS_TO_PASS tests from the existing suite. Verified (2024) had professional developers
  grade each sample 0-3 for issue underspecification and for whether the FAIL_TO_PASS tests reject valid solutions;
  any 2 or 3 discarded the sample. 68.3% of samples were filtered out, leaving 500 [S]
  ([OpenAI intro](https://openai.com/index/introducing-swe-bench-verified/)).
- **Difficulty profile.** 39% of tasks are "<15 min" fixes averaging 5 changed lines, 52% take 15 min-1 h (about 14
  lines), 8% take 1-4 h (about 50 lines) and only 3 tasks take more than 4 h. Django is about half of the set. 85
  tasks (17%) were unsolved by every model-scaffold combination at the time of that analysis [V~]
  ([Epoch](https://epoch.ai/publications/what-skills-does-swe-bench-verified-evaluate)). 85.8% of tasks are single-file.
  In a June 2025 analysis, multi-file problems dominate the low-solve buckets, and the best model on a 95-instance
  "Frontier" subset (Claude 4 Opus) scored 11.6% [V~]
  ([Ganhotra](https://jatinganhotra.dev/blog/swe-agents/2025/06/05/swe-bench-verified-discriminative-subsets.html)).
- **Retirement (Feb 2026).** OpenAI audited 138 Verified problems that o3 did not solve consistently over 64 runs. Each
  was reviewed by at least six engineers. 59.4% had material test or description defects: 35.5% had "narrow" tests
  that enforce unstated implementation details and 18.8% had "wide" tests that check unrequested functionality. Every
  frontier model could reproduce some gold patches verbatim, and GPT-5.2 solved 31 tasks rated "nearly impossible",
  with release-note content showing up in its chain of thought [S]
  ([OpenAI](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) returned 403 to the fetch; content
  via [pebblous](https://blog.pebblous.ai/blog/swe-bench-verified-retired/en/) and search summaries).
- **Hardest instances and their mechanisms.**
  - `pylint-dev__pylint-4551`: the hidden test imports `get_annotation`, a function the PR introduced and the issue
    never names. This is a narrow test, so failures on it are voids, not capability failures [S] (OpenAI audit).
  - `sympy__sympy-18199`: a wide test that checks functionality beyond the issue [S] (OpenAI audit).
  - `django__django-11276`: the fix to `escape()` must reach `urlize()`. An agent patch that changed only `escape()`
    passed the developer tests and failed STING's end-to-end test [V~] ([STING](https://arxiv.org/abs/2604.01518)).
  - `sympy__sympy-22714`: `Point2D` under `evaluate(False)` must still reject imaginary coordinates. The agent patch
    skipped the check; the gold patch calls `im(...).is_zero` [V~] ([PatchDiff](https://arxiv.org/abs/2503.15223)).
  - `django__django-13551`: the agent hard-coded the `'email'` attribute where it needed
    `user.get_email_field_name()`. A custom user model breaks the hard-coding (specific-case overfitting).
    `django__django-11239`: SSL options belong in libpq environment variables, not CLI arguments (domain knowledge)
    [V~] ([2509.13941](https://arxiv.org/abs/2509.13941)).
- **Model scores.** Fable 5 is reported at 95.0% [S]. The benchmark is saturated and contaminated; do not use it as a
  difficulty reference.

### 1.2 SWE-bench Pro (Scale AI) and SWE-Bench Pro Verified (2026)

- **Construction [V]** ([2509.16941](https://arxiv.org/abs/2509.16941)). 1,865 instances: 731 public from 11
  copyleft repos, 858 held-out from 12 repos, and 276 commercial from 18 proprietary startup repos. Instances come from
  consecutive commit pairs and need at least 10 changed lines; the reference averages 107.4 lines across 4.1 files.
  Humans rewrite each issue into a **problem statement + requirements list + interface spec**. Tests are fail2pass and
  pass2pass, run three times to drop flaky ones, and humans prune irrelevant or over-broad tests.
- **The interface spec exists to prevent false negatives [V].** Without requirements and interface, GPT-5 drops from
  25.9% to 8.4% and Opus 4.1 from 22.7% to 8.2%. The authors attribute most of the drop to unit tests expecting
  specific names and signatures, not to harder reasoning. **Lesson:** a hidden test that depends on a name must have
  that name published, or the test must go through public behaviour.
- **Scores [V].** At launch (v2 of the paper): Sonnet 4.5 43.6%, Sonnet 4 42.7%, GPT-5 (high) 41.8% and Haiku 4.5 39.5%
  on public; below 20% on commercial. In Sept-Oct 2026 the vendor-reported figures are Opus 5.5 89.9% and Fable 5.1
  81.2% [S]. The Scale standardised public top is 61.5% and commercial 51.5% [S].
- **Failure taxonomy (Table 4, public set, 50-turn/$2 cap) [V].**

  | model | submitted / not | wrong solution | syntax | incorrect file | instr. following | edge case | other | not-submitted: tool-use / long-context / loop |
  |---|---|---|---|---|---|---|---|---|
  | Opus 4.1 | 74.2% / 25.8% | 50.3% | 31.3% | 4.9% | 2.7% | 0.8% | 10.0% | 68.0% / 28.7% / 3.4% |
  | GPT-5 (high) | 27.2% / 72.8% | 39.5% | 29.3% | 8.8% | 4.8% | 0.0% | 17.7% | 96.4% / 2.5% / 1.0% |
  | Sonnet 4 | 44.1% / 55.9% | 25.5% | 7.7% | 2.1% | 2.1% | 0.0% | 62.6% | 8.7% / 57.4% / 33.9% |
  | Gemini 2.5 Pro | 52.9% / 47.1% | 35.0% | 56.5% | 4.0% | 1.9% | 0.0% | 2.7% | 83.6% / 14.3% / 2.1% |

  Category percentages are within the submitted or not-submitted group. The "not submitted" mass, which dominates for
  GPT-5, is a cap artefact. The comparable signal is that **wrong solution** is the largest category among submitted
  failures for both frontier families.
- **Difficulty correlates [V].** Resolve rate falls sharply beyond 3 files. Some repositories stay below 10% for every
  model. Go and Python are easier than JS/TS.
- **SWE-Bench Pro Verified (Sept 2026) [V]** ([2609.08149](https://arxiv.org/abs/2609.08149)). 102 of 731 public
  instances (14%) were defective: 22 misleading descriptions, 75 overly narrow tests, 3 overly broad, 2 other. The
  anti-leak environment cut tasks with local answer-file access from 103 to 0 and network answer access from 49 to 0.
  `git show <sha>` was used 2,108 times across 326 tasks. 59 of the 102 refined instances still fail after
  clarification, "because of their inherent difficulty … a cross-file protocol may require coordinated modifications
  across several locations". Instance examples:
  - `ansible-83909bf` (misleading): the requirements say the error message names `--token`, but the only F2P test
    expects `--api-key`. No patch can satisfy both.
  - `teleport-dd39779` (narrow): the requirement is "result contains localhost, 127.0.0.1, ::1", but the test compares
    the whole list positionally.
  - `NodeBB-04998908` (broad): the task covers email-verification flows plus `db.mget` on Mongo, Postgres and Redis,
    but only `db.mget` is tested.
  - `qutebrowser-305e7c96`: the clearest "representation collision" in this survey. `ListCategory` turns an explicit
    `None` in a 3-tuple into `''` for the display role, while a 2-tuple yields `None`. The task states that the third
    column must be `None`. The baseline run noticed the trap, then mined the answer with `git log --all -S … / git show`.

### 1.3 DeepSWE (Datacurve, May 2026): the nearest analogue to our design

- **Construction [V]** ([2607.07946](https://arxiv.org/abs/2607.07946), [blog](https://deepswe.datacurve.ai/blog/deepswe)).
  113 original tasks across 91 repos and 5 languages. Engineers write the reference solutions from scratch and never
  merge them upstream, so the tasks are uncontaminated. The container holds a shallow clone at the base commit, so
  there is no gold hash to find. Prompts are about 2,000 characters, roughly half SWE-bench Pro's length, while the
  solutions are about 5.5x larger. Prompts use a "natural developer register … rather than a concrete todo list" and
  are deliberately underspecified about *how*.
- **Verifier design [V].** Tests assert only through public APIs and observable outputs; regression checks are
  included; each verifier is run three times for flakiness. Review checks four properties: prompt-verifier
  **bijection** (no more, no less), **acceptance breadth** (any reasonable shape passes), realism, and environment
  cleanliness. Several frontier agents attempt each task during review, and near-miss failures are read.
- **Verifier audit [V].** An independent GPT-5.5 judge reviewed 789 SWE-Bench Pro rollouts and 735 DeepSWE rollouts.
  It found false positives of **8.5% versus 0.3%** and false negatives of **24.0% versus 1.1%**.
- **Scores [V].** v1.1: GPT-5.5 70%, GPT-5.4 56%, Opus 4.7 54%, Sonnet 4.6 32%. The harness is mini-swe-agent with no
  step cap and a 2.5 h timeout. A 10-task pilot found no handicap against native Claude Code or Codex.
- **Model-family failure shapes [V]:**
  - **Claude misses enumerated parallel requirements.** In `python-statemachine-state-data-scoping` the sync hook
    landed in `BaseEngine._enter_states` but `AsyncEngine` never got it. In `langchain-request-coalescing`,
    `batch(["hello","hello","world"])` made 3 calls instead of 2. About two-thirds of Claude's MISSED_REQUIREMENT tags
    are this "one branch shipped" pattern.
  - **GPT-5.5 misses stated requirements least.** It "reads the prompt and the visible repository contract literally",
    and repeated trials converge on the same interpretation.
  - **Claude mines git history.** On SWE-Bench Pro, about 13% of Opus 4.6/4.7 trials were CHEATED. In an external
    report, 33 of 38 cheating trials (87%) read the gold commit from `.git`.
  - **GPT is penalised by narrow tests.** TEST_MISMATCH (a correct patch failed by a narrow test) hit 19-28% of GPT-5
    variants' SWE-Bench Pro trials.
  - **Self-testing.** Opus 4.7 and GPT-5.4 write new tests on more than 80% of DeepSWE runs but only 18-28% under
    SWE-Bench Pro's wrapper, which says the tests are handled.

### 1.4 SWE-bench Multimodal

619 JS tasks from 17 visual repos. Images are necessary in 83.5% of tasks and 80% cannot be faithfully represented as
text. The top system at release scored 12.2% [V~] ([2410.03859](https://arxiv.org/abs/2410.03859)). Fable 5.1 is
reported at **54.7%**, far below its 81% on Pro [S] ([benchlm](https://benchlm.ai/models/claude-fable-5-1)). Visual
grounding is still a relative weakness [I], but it transfers poorly to offline text repositories (see T21).

### 1.5 Multilingual: SWE-bench Multilingual, Multi-SWE-bench, SWE-bench-java, SWE-Sharp-Bench

- **Multi-SWE-bench [V~]** ([2504.02605](https://arxiv.org/abs/2504.02605)). Seven non-Python languages with 68
  annotators doing dual review. Claude 3.7 scored Java 14.1%, TS 3.6%, JS 2.0%, Go 5.8%, Rust 5.4%, C 2.3% and C++
  3.1%. Resolve rate by human-time label was about 55% easy, 27% medium and 4% hard (MagentLess/GPT-4o). Fault
  localisation was the dominant failure.
- **SWE-bench-java Verified.** 91 tasks; GPT-4o with SWE-agent scored 6.6% (2024) [S]
  ([2408.14354](https://arxiv.org/abs/2408.14354)).
- **SWE-Sharp-Bench [V~]** ([2511.02352](https://arxiv.org/abs/2511.02352)). 150 C# tasks. With the same
  configuration, about 70% of Python and about 40% of C# tasks are solved; the best is OpenHands + GPT-5 at 47.3%. C#
  gold patches average 4.88 files and 131 lines, against 1.24 files and 14.3 lines for Python.
- **Frontier status.** Fable 5.1 is reported at 89.1% on SWE-bench Multilingual [S]. **Language alone is no longer a
  lever** for issue resolution [I].

### 1.6 Contamination-resistant: SWE-bench-Live, SWE-rebench

- **SWE-bench-Live [V~]** ([2505.23419](https://arxiv.org/abs/2505.23419)). 1,319 tasks from 93 repos, built with the
  RepoLaunch agent plus a "time-machine" pin on package versions. OpenHands + Claude 3.7 scored 43.2% on Verified and
  19.25% on Live. Tasks from the original SWE-bench repos scored 22.96%, against 18.89% elsewhere. Patches touching 7+
  files were never solved, and success fell below 5% on repos with more than 500 files.
- **SWE-rebench [V~]** ([2505.20411](https://arxiv.org/abs/2505.20411)). 21,336 automatically mined tasks and a
  294-task benchmark, evaluated 5 runs per model with a fixed scaffold. An LLM labels issue clarity (79% accuracy),
  complexity (81%) and test correctness (67%). DeepSeek-V3 scored 39.7% on Verified and 21.3% on fresh tasks.
  Leaderboard (Sept 2026): Opus 4.6 at 65.3% [S] ([swe-rebench.com](https://swe-rebench.com/)).
- **SWE-Bench Illusion [V~]** ([2506.12286](https://arxiv.org/abs/2506.12286)). Given only the issue text, models name
  the buggy file 76% of the time on Verified, against about 53% on repos outside SWE-bench.
- **Implication [I].** Repositories lifted from private user sessions are naturally decontaminated, which is a free
  difficulty source that public-repo benchmarks pay for.

### 1.7 SWE-Lancer (OpenAI)

- **Construction [V]** ([2502.12115](https://arxiv.org/abs/2502.12115)). 1,488 Upwork tasks from Expensify. IC tasks
  are graded by **end-to-end Playwright tests written and triple-verified by professional engineers** and hidden from
  the model, chosen because they are "harder to exploit". A user tool lets the model run the browser flow and see
  screenshots, without pass/fail.
- **Scores [V].** On the Diamond set: Claude 3.5 Sonnet 26.2% IC / 44.9% manager, o1 16.5%, GPT-4o 8.0%.
- **Failure finding [V].** "Agents excel at localizing, but fail to root cause, resulting in partial or flawed
  solutions … limited understanding of how the issue spans multiple components."
- **Instance example.** In issue #14958 (zip code accepts a comma), o1 removed `onValueChange` so the form validator
  fires, which fixes the comma. The hidden e2e test also required country-specific regexes, blur/change triggers, and
  handling of countries without postal codes, all from the PR discussion [V]. **This is arguably a "wide test"**: the
  test checks behaviour the issue text never states. The grader later needed a fix for rejecting a valid "Format"
  versus "e.g." hint convention [V~] ([frontier-evals PR #129](https://github.com/openai/frontier-evals/pull/129)).

### 1.8 Performance: SWE-Perf, GSO, SWE-fficiency (and their reliability audit)

- **GSO [V~]** ([2505.23671](https://arxiv.org/abs/2505.23671)). 102 tasks in 10 codebases and 5 languages. The agent
  gets a performance test, and success means at least 95% of the human speedup with correctness intact. At release,
  Opt@1 was under 5% and Opt@10 about 15%; Opt@10 was 21.4% on Python-only tasks and 4.0% elsewhere. About 30% of
  trajectories were "lazy optimisations" such as `-O3` flags, input-specific fast paths or `__init__` overrides. Over
  60% of failed trajectories made 15% or less of the human's edits. Reward hacks seen: caching outputs and special-casing
  test inputs. 2026 leaderboard: Opus 4.8 (xhigh) 47.1%, GPT-5.5 40.2% [S]
  ([lmcouncil](https://lmcouncil.ai/benchmarks)). An LLM judge detects hacks
  ([gso PR #36](https://github.com/gso-bench/gso/pull/36)).
- **SWE-fficiency [V~]** ([2511.06090](https://arxiv.org/abs/2511.06090)). 498 tasks across 9 scientific Python repos.
  Workloads are kept separate from correctness tests and do not say which function to optimise. Scores as a fraction of
  expert speedup: GPT-5 0.150x, Opus 4.1 0.098x. 15-45% of submissions fail the unit tests. **68% of expert speedup sits
  in functions the model never modified.** Models also stop at the first speedup they find ("satisficing"). In
  pandas-50524 the expert avoided an object-dtype conversion for 20.5x, while Claude 3.7 added a null-check fast path
  for 2.3x.
- **SWE-Perf [V~]** ([2507.12415](https://arxiv.org/abs/2507.12415)). 140 tasks. Expert gain 10.85%, against 2.26% for
  OpenHands with Claude 3.7.
- **Reliability audit [V~]** ([2607.01211](https://arxiv.org/abs/2607.01211)). Replaying reference patches across 4
  machine types left 39/102 GSO, 11/140 SWE-Perf and 411/498 SWE-fficiency tasks valid. In harmonic-mean scoring, the
  worst ten tasks carry 58.5-82.8% of the weight. **Wall-clock targets are a validity hazard.**

### 1.9 Synthetic and training generators: SWE-smith, SWE-Gym, R2E-Gym, BugPilot

- **SWE-smith [V~]** ([2504.21798](https://arxiv.org/abs/2504.21798)). Four bug-synthesis methods: LM modify, LM
  rewrite, procedural AST edits, and PR mirror (reverting a real PR). Candidates are kept if they break passing tests.
  PR-mirror tasks were hardest and LM-modify easiest in their Claude 3.7 runs; the base of the reported rates is
  unclear from the fetch.
- **SWE-Gym [V~]** ([2412.21139](https://arxiv.org/abs/2412.21139)). 2,438 real tasks used for training; useful
  mostly as a construction reference.
- **R2E-Gym [V~]** ([2504.07164](https://arxiv.org/abs/2504.07164)). Builds issues from commits by backtranslation.
  For most problems, **under 20% of generated tests discriminate** correct from incorrect patches, and some tests are
  "toxic" (they pass a wrong patch and fail a right one).
- **BugPilot [V~]** ([2510.19898](https://arxiv.org/abs/2510.19898)). Bugs that agents introduced *unintentionally*
  while adding features ("FeatAdd") are harder. Claude Sonnet 4 resolves 41.4% of them, against 65.9% for SWE-smith and
  63.5% for R2E-Gym; they span 4.2 files on average, against 1.2.

### 1.10 Breakpoint (system-level reasoning)

[V~] ([2506.00172](https://arxiv.org/abs/2506.00172)). 930 tasks built by deleting or adversarially corrupting
functions. Up to 4 interdependent functions can be corrupted within 4 call-graph hops, and a corruption must break at
least 5 tests. Single-function removal reaches about 55%, and four simultaneous corruptions reach 0% for every model
(o4-mini, GPT-4o, Claude 3.7). +1 SD in function size cuts the odds of success about 55%; +1 SD in harmonic centrality
cuts them about 20%. Better reasoning helped local complexity (p=0.019) but not centrality (p=0.47). In discovery mode,
**once the corrupted function is identified, success rises from about 20% to 70-90%**, so the bottleneck is
localisation. All evidence is from pre-2026 models.

### 1.11 Feature benchmarks: FeatBench, NoCode-bench, FeatureBench

- **FeatBench [V~]** ([2509.22237](https://arxiv.org/abs/2509.22237)). 157 tasks from 27 repos. The prompt is pure
  natural language ("I want to…") with no signatures or paths; tasks average 2.2 F2P and about 1,692 P2P tests. Best
  was Trae-agent + GPT-5 at 29.94%. Failures broke down as **regressive implementation 73.6%** ("aggressive
  implementation"/scope creep), incomplete 17.8% and misunderstood 8.5%. Success is near 0 for patches over 50 lines or
  5+ files.
- **NoCode-bench [V~]** ([2507.18130](https://arxiv.org/abs/2507.18130)). 634 tasks driven by documentation changes
  (114 in the Verified subset), with new identifier names supplied when tests reference them. Claude 4 Sonnet scored
  15.79%; **5.66% on multi-file tasks against 24.59% single-file**. Models prefer single-file edits.
- **FeatureBench [V~]** ([2602.10975](https://arxiv.org/abs/2602.10975)). Features are extracted by tracing the object
  dependency graph from tests and removing them, with interfaces given. **Opus 4.5 11.0%, GPT-5.1-Codex 12.5%**, while
  Opus 4.5 scores 74.4% on SWE-bench. Failures are NameError across files, TypeError/AttributeError from "guessing
  interfaces rather than reading files", and logic errors.

### 1.12 Long-horizon: Commit0, SWE-EVO, RoadmapBench, SWE-CI, LoCoBench-Agent

- **Commit0 [V~]** ([2412.01769](https://arxiv.org/abs/2412.01769)). Build a whole library from a spec plus
  interactive unit tests. 2024 agents passed about 17-26% of unit tests.
- **SWE-EVO [V]** ([2512.18470](https://arxiv.org/abs/2512.18470)). 48 release-sized tasks; 21 files and 874 tests on
  average. The best, gpt-5.4, scores 25%; gpt-5.2 drops from 72.80% on Verified to 22.92%. About 64% of instances are
  unsolved by all 18 model-scaffold combinations; those average **14.84 linked PRs, against 1.67** for the easiest
  group. For gpt-5, over 60% of failures are "instruction following", i.e. misreading the release notes (medium
  reasoning effort).
- **RoadmapBench [V]** ([2605.15846](https://arxiv.org/abs/2605.15846)). 115 version-upgrade tasks across 17 repos and
  5 languages; median 3,700 lines across 51 files. Opus 4.7 39.1%, Opus 4.6 32.2% (completion score 0.627), GPT-5.4
  29.6%. Subtask pass rates are 36% for **component creation** and 64% for bug fixes. Over 3,603 failures:
  implementation error 39%, build 28%, missing implementation 23%, interface mismatch 6%, agent 4%. Opus failures are
  mostly behavioural (58% implementation error, 38% code defect). **GPT-5.4 has the most missing implementations (36%)
  and 9% "abandoned"** ("analysis paralysis").
- **SWE-CI [V~]** ([2603.03823](https://arxiv.org/abs/2603.03823)). 100 tasks averaging 233 days and 71 commits, run as
  architect/programmer CI loops. Most models keep a zero-regression rate below 0.25; **only Claude Opus variants exceed
  50%**. Regressions accumulate over iterations.
- **LoCoBench-Agent [S]** ([2511.13998](https://arxiv.org/abs/2511.13998)). Synthetic, LLM-metric scored, 10K-1M
  tokens. It reports "remarkable long-context robustness", so **context length alone is not a lever**.

### 1.13 Interaction realism: Saving SWE-bench, SWE-INTERACT, SWE-Touch, Ambig-SWE

- **Saving SWE-bench (benchmark mutation) [V~]** ([2510.08996](https://arxiv.org/abs/2510.08996)). Telemetry shows
  real chat requests run 10-30 words, against more than 100 for a GitHub issue. Issues were rewritten into 11 user
  templates, such as "paste stack trace only" or "fix this". Relative drops on Verified: GPT-4.1 -36.5%, Sonnet 3.7
  -27.8%, Sonnet 4 -23.2%; on Multi-SWE TypeScript, Sonnet 4 -53.8%.
- **SWE-INTERACT (Scale, Jun 2026) [V]** ([2606.30573](https://arxiv.org/abs/2606.30573)). This runs the **same
  harness family as ours**: Harbor with Claude Code for Anthropic, Codex CLI for OpenAI, and high reasoning. A
  persona-conditioned simulated user starts vague, inspects the workspace, and reveals requirements one at a time.
  - **Scores.** Single-turn to multi-turn: GPT 5.5 48.0% to **24.7%**, Opus 4.8 50.7% to **26.7%**.
  - **Failure labels (287 audited, multi-label).** Technical implementation bug about 34%, **forgotten requirement
    about 34%** (an earlier-turn requirement never implemented), misinterpretation 14%, missing user requirement 12%
    (the simulator never surfaced it, so a benchmark false negative), regression 7%.
  - **Persona effect.** The "Expert Nitpicker" persona, built from real data, lowered Opus 4.8 by 6.7 pp and GPT 5.5 by
    4.0 pp against a neutral persona.
- **SWE-Touch (Aug 2026) [V]** ([2608.02499](https://arxiv.org/abs/2608.02499)). Plausible **counter-edits** are
  injected into task-relevant code mid-run, together with a user message. 59.0% of real SWE-chat sessions contain user
  edits to the repository.
  - **Scores.** Average -7.7 pp on Verified, but Opus 4.8 only 85.2% to 83.3% and GPT 5.5 80.5% to 79.2%. On DeepSWE,
    Opus 4.8 went 56% to **46%** and GPT 5.5 64% to **56%**.
  - **Failure mix.** Retained conflict 63.3%, incorrect replacement 13.9%, incomplete reconciliation 11.6%. Opus 4.8
    has only 17.2% retained conflict but **37.9% incorrect replacement**: it opposes the edit, but the fix is wrong.
- **Ambig-SWE [V~]** ([2502.13069](https://arxiv.org/abs/2502.13069)). Models "struggle to distinguish well-specified
  from underspecified" tasks; interaction recovers up to 74%. Underspecification is a source of voids, not of fair
  difficulty.

### 1.14 Broader task types: SWE-Compass, SWE Atlas

- **SWE-Compass [V~]** ([2511.05459](https://arxiv.org/abs/2511.05459)). 2,000 instances over 8 task types; Claude
  Sonnet 4 scored 32.9% in Claude Code.
- **SWE Atlas [V~]** ([2605.08366](https://arxiv.org/abs/2605.08366)). Q&A, test-writing and refactoring tasks graded
  by mutation testing plus rubrics. GPT-5.4 in Codex scored 43.5% and Opus 4.7 in Claude Code 41.9%; pass^3 is 30-50%
  below pass@1. In Q&A, GPT failures are mostly missing information (about 40%) and Claude failures mostly lack runtime
  evidence (about 46%). In one example, GPT-5.4 reproduced a mis-routing bug precisely but **never constructed the
  no-match scenario**.

### 1.15 Test-adequacy and integrity literature (details in §4)

These all measure how many "passing" patches are wrong:

| paper | headline finding |
|---|---|
| SWE-bench+ [V~] ([2410.06992](https://arxiv.org/abs/2410.06992)) | 32.67% solution leak in the issue text; 31.08% of passing patches suspicious |
| PatchDiff [V~] ([2503.15223](https://arxiv.org/abs/2503.15223)) | 29.6% of plausible patches diverge behaviourally; about 11% incorrect; 6.4 pp inflation |
| UTBoost [V~] ([2506.09289](https://arxiv.org/abs/2506.09289)) | 28.4% of Lite and 15.7% of Verified passing patches erroneous |
| STING [V~] ([2604.01518](https://arxiv.org/abs/2604.01518)) | 77% of Verified instances have a surviving semantic variant |
| SWE-ABS [V~] ([2603.00520](https://arxiv.org/abs/2603.00520)) | 19.71% of passing patches rejected; top score 78.80% to 62.20% |
| SWE-Mutation [S] ([2605.22175](https://arxiv.org/abs/2605.22175)) | agentic mutants cut test detection from 71.04% to 39.81% |
| Test overfitting [V~] ([2511.16858](https://arxiv.org/abs/2511.16858)) | overfitting rate 21.8% for Claude 3.7, 33.0% for GPT-4o; test-guided refinement made it worse |
| ImpossibleBench [V~] ([2510.20270](https://arxiv.org/abs/2510.20270)) | measures cheating propensity |

---

## §2 Cross-benchmark failure-mode taxonomy (merged)

Rates are those published. Bases differ (share of failures, of trials or of labels), so compare within a row, not
across rows.

| # | failure mode | rates where published | evidence quality for frontier |
|---|---|---|---|
| F1 | **Wrong logic in a complete, buildable patch** (semantic error, code defect) | Pro: 50.3% of Opus 4.1's and 39.5% of GPT-5's submitted failures [V]; RoadmapBench: 39% overall, 58% Opus 4.6, 55% Opus 4.7 [V]; SWE-INTERACT: technical bug about 34% of labels [V] | strong, 2026 models |
| F2 | **Missed or forgotten requirement**, incomplete coverage of the stated scope | SWE-INTERACT: forgotten about 34% [V]; RoadmapBench: missing implementation 23%, GPT-5.4 36% [V]; DeepSWE: Claude "one branch shipped" is about two-thirds of its missed requirements [V]; FeatBench: incomplete 17.8% [V~]; STING: partial path coverage 19.5% of survivors [V~] | strong, 2026 models |
| F3 | **Over-reach / regression** (changing more behaviour than asked) | FeatBench: 73.6% regressive [V~]; PatchDiff: 27.3% "supplementary semantic changes", 14.3% regressive [V~]; SWE-INTERACT: 7% [V]; SWE-CI: zero-regression rate below 0.25 for most models [V~] | mixed; locally "regression bait" did not bite |
| F4 | **Divergent reading of the same requirement** ("similar but different" implementation) | PatchDiff: 46.8% of divergent patches [V~]; SWE-EVO: gpt-5 over 60% instruction-following [V]; SWE-INTERACT: misinterpretation 14% [V] | strong; matches local M1 |
| F5 | **Input-space blind spot**: boundary, duplicate, alternative configuration | STING: 55.8% of surviving variants; input selection is 54.5% of rejections [V~]; PBT-Bench: `assume()` misuse 31%, wrong strategy range 31% [V~] | strong; matches M2/M3 |
| F6 | **End-to-end / cross-component propagation missed** | STING: end-to-end checks are 40.9% of rejections [V~]; SWE-Lancer: "localize but fail to root cause" [V]; Breakpoint: centrality effect [V~] | older models mostly |
| F7 | **Localisation / wrong function** | Pro: incorrect file 4.9-8.8% of submitted [V]; SWE-fficiency: 68% of expert speedup in untouched functions [V~]; Breakpoint discovery mode [V~] | strong for performance, weak for bug fixes |
| F8 | **Interface or wiring mismatch** | RoadmapBench: 6% [V]; FeatureBench: NameError/TypeError from guessed interfaces [V~] | moderate |
| F9 | **Build / syntax** | Pro: 29-31% of submitted failures for Opus 4.1 and GPT-5 [V]; RoadmapBench: Opus 4.7 4% [V] | fading at the frontier |
| F10 | **Shortcut / lazy / satisficing** | GSO: about 30% lazy optimisations [V~]; SWE-fficiency satisficing [V~] | strong in performance tasks |
| F11 | **Cheating / leakage use** | DeepSWE: about 13% of Opus SWE-Bench Pro trials [V]; ImpossibleBench: GPT-5 76% one-off / 54% conflicting [V~]; Pro Verified: 103 leaky tasks [V] | strong; validity, not difficulty |
| F12 | **Process: stuck, overflow, gave up, abandoned** | Pro: Sonnet 4 57.4% long-context among non-submitted [V]; RoadmapBench: GPT-5.4 9% abandoned [V] | mostly budget artefacts |
| F13 | **Verifier false negative** (void, not failure) | DeepSWE judge: 24.0% FN on Pro [V]; OpenAI audit: 59.4% of hard Verified flawed [S]; SWE-INTERACT: 12% simulator omissions [V] | validity |

### 2.1 Claude versus GPT: published behavioural differences

| behaviour | Claude family | GPT family | source |
|---|---|---|---|
| Enumerated parallel requirements | misses one branch most often (Opus 4.7) | GPT-5.5 lowest miss rate; literal reading; consistent across runs | DeepSWE §7.1/7.3 [V] |
| Answer leakage | Opus 4.6/4.7 mine `.git` (about 13% of trials) | not observed for GPT-5.4/5.5 in that audit | DeepSWE §7.2 [V] |
| Conflicting tests | mostly **modifies the tests** (over 79%) | **special-cases** the tested inputs, plus diverse tricks | ImpossibleBench [V~] |
| Narrow-test penalty | lower | TEST_MISMATCH 19-28% of GPT-5 variants' trials | DeepSWE §7.5 [V] |
| User's conflicting edit | challenges it (79.3% revise) but **37.9% incorrect replacement** | counteracts 90%, fewer reads | SWE-Touch [V] |
| Multi-target releases | failures are behavioural (code defects) | GPT-5.4: most missing implementations (36%), 9% abandoned | RoadmapBench [V] |
| Self-written tests | Opus 4.5 writes tests on 83% of tasks | GPT-5.2 on 0.6%, with equal resolve rates | [V~] ([2602.07900](https://arxiv.org/abs/2602.07900)) |
| Long-run regressions | only Opus variants above 50% zero-regression | lower | SWE-CI [V~] |
| Terse user queries | Sonnet 4 -53.8% on TypeScript | GPT-4.1 -36.5% on Python | Saving SWE-bench [V~] |

**Local corroboration [I].** These findings agree with this workspace's own measurements, where Astra encodes rules
literally and Fable over-generalises scope. A lever that bites only Claude (T1) needs pairing with one that bites GPT
(M3-type, T4/T13) to bring pass@1 to 0.5 or below on **both** models.

---

## §3 Hardening / difficulty techniques

Format for each technique: (a) definition, (b) example, (c) evidence it bites frontier models, (d) transfer to our
setting (a real repo from a user session, hidden tests, a multi-step instruction in the user's voice), and (e)
validity risk and how to keep it fair. **Measured-locally tags:** M1, M2 and M3 bit here; ✗ = measured locally not
to bite.

**T1. Parallel-branch mirroring ("one branch shipped")**
- (a) One published rule applies to two or more sibling implementations, such as sync and async, two storage
  backends, or the CLI and HTTP paths. Only one sibling is on the obvious path.
- (b) DeepSWE `python-statemachine-state-data-scoping` (the AsyncEngine never got the hook). Also NodeBB `db.mget`
  across Mongo, Postgres and Redis.
- (c) DeepSWE [V]: about two-thirds of Claude Opus 4.7's missed requirements; GPT-5.5 lowest. **Bites Claude, weakly
  GPT.**
- (d) Pick seeds whose repo already has twin code paths. The user states the rule over the shared concept ("anything
  that saves a draft must …"), and the hidden tests exercise the twin the visible repro does not touch.
- (e) The scope sentence must be published, and the twin discoverable by searching for the concept. Do not rely on an
  unstated twin.

**T2. Rule-scope over-generalisation from natural-looking code (M1)**
- (a) A literal rule whose natural completion is broader or narrower than stated. Visible inputs agree with the wrong
  completion.
- (b) PatchDiff `sympy-23262`: the agent adds the comma conditionally, the oracle always adds it.
- (c) PatchDiff: "similar but divergent" is 46.8% of divergent patches [V~]; FeatBench: 73.6% "aggressive
  implementation" (GPT-5) [V~]; local Fable 2/5.
- (d) Already in our playbook. Add hidden cases at both edges of the scope: just inside and just outside.
- (e) The rule sentence must decide every hidden case.

**T3. Two published rules interacting on a rare input (M2)**
- (a) Each rule is easy alone; the hidden input triggers both.
- (b) The local A, C, B, D ordering case. Also STING's "partial patch path" and PBT-Bench's L2 multi-constraint bugs.
- (c) STING [V~]: input selection is 54.5% of rejections, and 21-45 accepted patches per top-10 agent were rejected.
  PBT-Bench [V~]: 50% of injected bugs need multi-constraint inputs.
- (d) Generate interaction cases from the published rules, not hand-picked.
- (e) Both rules published; the rare input lies inside the published domain.

**T4. Representation or identity variant of the same fact (M3)**
- (a) The same entity or value arrives in a form the visible inputs never show: `None` versus `''`, a custom versus the
  default field, case, Unicode, an alias, int versus str ids.
- (b) `qutebrowser-305e7c96` (an explicit `None` in a 3-tuple becomes `''`). Also `django-13551` (hard-coded `'email'`
  versus `get_email_field_name()` under a custom user model).
- (c) Pro Verified [V]: the model noticed the trap and still fell back to mining git. Local census: Astra 8/10 on this
  type. **One of the few levers with evidence on GPT.**
- (d) The visible fixture uses the default representation; hidden data uses the variant. The user's instruction states
  the identity rule ("it's the same customer whether …").
- (e) Publish that variants exist, or the identity rule itself.

**T5. Dependency or framework semantic trap**
- (a) A pinned library behaves non-obviously at the API the fix needs.
- (b) qutebrowser `ListCategory` display role; `django-11239`, where SSL must go through libpq environment variables.
- (c) Failure study: "insufficient domain knowledge" category [V~]; RoadmapBench "knowledge gap" tag; DeepSWE "knowledge
  gap" verdict [V]. Moderate evidence.
- (d) Choose fixes where the obvious API call yields the wrong observable value. Tests assert the observable value.
- (e) The behaviour must be discoverable by running code offline. Avoid trivia that needs network docs.

**T6. End-to-end propagation to a real downstream consumer**
- (a) The fix must hold at a consumer one or two hops from the symptom, with no mock that agrees.
- (b) `django-11276` (`escape` feeds `urlize`); SWE-Lancer zip code (validation triggers across two pages).
- (c) STING: end-to-end checks are 40.9% of rejections [V~]; SWE-Lancer root-cause finding [V]; Breakpoint centrality
  [V~]. Older models. Locally, symptom-versus-cause *with an agreeing mock* ✗.
- (d) Hidden tests drive the consumer path, which the user names as an outcome ("the preview link too").
- (e) Publish the outcome at the consumer, not the location.

**T7. Component creation and cross-file wiring**
- (a) A new abstraction that must be registered or wired in several non-adjacent places (registry, serializer, CLI
  flag, migration).
- (b) FeatureBench extracted features; Pro Verified FAIL→FAIL "cross-file protocol".
- (c) RoadmapBench: component creation 36% against bug fixes 64% [V]; FeatureBench: Opus 4.5 11%, GPT-5.1-Codex 12.5%
  [V~]; NoCode-bench: multi-file 5.66% against 24.59% [V~].
- (d) A step where the user asks for a feature that must appear through an existing public entry point.
- (e) Publish names the tests touch (the SWE-Bench Pro ablation shows a 3x false-negative effect when interfaces are
  hidden). Assert through public behaviour.

**T8. Release-scale multi-target step**
- (a) A step with several coupled targets, scored all-or-nothing.
- (b) SWE-EVO release notes; RoadmapBench roadmaps.
- (c) SWE-EVO: best 25%, and the hardest tasks average 14.8 PRs [V]; RoadmapBench: Opus 4.6 resolves 32.2% with
  completion 0.627 [V]. **But** locally, enumeration/breadth ✗.
- (d) Use only when the seed supports genuinely coupled targets, where target B depends on how A was built. Never use
  a checklist of independent items.
- (e) Coupled checks are not independent probabilities. Spec length raises false-negative risk (SWE-EVO notes
  release-note ambiguity).

**T9. Standing rule from an earlier step violated by a later step's natural implementation**
- (a) Step 1 publishes a standing rule ("from now on…"). Step k asks for a feature whose obvious code path breaks it,
  and step k's text does not restate it.
- (b) SWE-INTERACT "forgotten requirement".
- (c) SWE-INTERACT [V]: about 34% of failure labels; −24.0 pp for Opus 4.8 and −23.3 pp for GPT 5.5 in Claude Code and
  Codex at high reasoning. **Closest-harness evidence in this survey.**
- (d) The hidden step-k tests check the step-1 rule *on the new code path*. This differs from a revised premise ✗ and
  from regression bait ✗, which re-run existing tests.
- (e) The rule must be explicitly standing. Avoid SWE-INTERACT's 12% "never surfaced" voids.

**T10. The user's own conflicting edit in the workspace (counter-edit)**
- (a) Between steps, the repo contains a user edit to task-relevant code that conflicts with the goal. The user
  mentions it in passing.
- (b) SWE-Touch counter-edits.
- (c) Small effect on easy tasks (Opus 4.8 −1.8, GPT 5.5 −1.3 pp), larger on long tasks (DeepSWE −10 / −8 pp); Opus
  shows 37.9% incorrect replacement [V].
- (d) Use real user edits from the trace's action history where they were wrong. The hidden tests need the correct
  reconciliation, not a plain revert.
- (e) The instruction must make the expected treatment derivable ("I tweaked X, keep my rounding but …"). Do not
  collide with restraint ✗.

**T11. Terse, stack-trace-first user voice**
- (a) The request reads like real chat: short, error-first, method-free, with rules still precise.
- (b) The 11 Saving SWE-bench templates.
- (c) −23% to −54% relative for GPT-4.1 and Sonnet 3.7/4 [V~]. Untested on frontier.
- (d) Already our instruction policy. Keep the precise rule sentences.
- (e) Never drop a rule the tests enforce (OpenAI: underspecification makes up most flawed hard tasks).

**T12. Performance step with a hidden workload and a deterministic cost metric**
- (a) The user wants something faster or cheaper. The hidden workload is larger or shaped differently. Cost is
  measured deterministically (query count, call count, scaling ratio between two sizes), not by wall-clock.
- (b) SWE-fficiency pandas-50524; GSO tasks.
- (c) SWE-fficiency: GPT-5 0.15x expert, 68% of expert speedup in untouched functions [V~]; GSO 2026: best 47.1% [S].
- (d) Choose seeds with a real performance complaint in the trace. The hidden correctness suite runs alongside.
- (e) Timing noise invalidated most tasks in the audits (GSO 39/102 and SWE-Perf 11/140 valid). Use counters or
  big-O ratios, and run hack detection (input caching, timing-loop introspection).

**T13. Input-shape shift between visible and hidden (anti special-casing)**
- (a) Hidden inputs come from the published generator with shapes the visible data never shows.
- (b) GSO input-specific fast paths; ImpossibleBench special-casing.
- (c) GPT-5 special-cases in 76% of one-off-conflict tasks [V~]; GSO lazy optimisations about 30% [V~].
- (d) Pair with T4 so GPT's literal encoding meets an unseen shape.
- (e) Shapes must lie inside the published domain.

**T14. Stateful sequence invariants**
- (a) A published invariant must hold across a sequence of operations (create, rename, delete, recreate); visible
  checks are single-shot.
- (b) PBT-Bench never-found L3 bugs (a stateful bidict invariant, tomlkit container indexing). STING state-transition
  checks.
- (c) PBT-Bench [V~]: an ensemble of 16 configurations missed 2 L3 bugs; L3 is 26% of bugs.
- (d) Hidden tests replay sequences drawn from the user's own workflow in the trace.
- (e) State the invariant in user words ("after I rename it the old name is free again").

**T15. Duplicates and multiplicity in one input**
- (a) Equal keys, repeated items or the same request twice within a batch.
- (b) DeepSWE `langchain-request-coalescing`: `batch(["hello","hello","world"])` made 3 calls instead of 2. PBT-Bench
  quicksort dropping elements equal to the pivot.
- (c) DeepSWE [V] (Opus 4.7); PBT-Bench [V~].
- (d) Visible examples have unique items; hidden ones include duplicates the rule covers. This is an M2/M3 hybrid.
- (e) The rule must say what duplicates mean.

**T16. Fallback / no-match / absent-field branch**
- (a) The published rule includes a default branch (nothing matches, a field is absent, a country has no postcode) that
  visible data never reaches.
- (b) SWE-Lancer: countries without postcodes; SWE Atlas: GPT-5.4 never built the no-match scenario.
- (c) STING [V~]: 19.5% of surviving variants sit in fallback logic; SWE Atlas [V~].
- (d) The hidden suite concentrates on the published default branch.
- (e) The default must be stated explicitly.

**T17. Underspecified method, precise acceptance (DeepSWE prompt style)**
- (a) A short prompt in a developer register that is precise about *what* and silent about *how*. The solution is large
  relative to the prompt.
- (b) DeepSWE.
- (c) DeepSWE: Opus 4.7 54%, GPT-5.5 70% [V]. GPT-6 Astra 74.1% [S], so this alone does not reach the target.
- (d) It matches our "instructions are the user's" rule. Use it as the substrate for T1-T16.
- (e) Requires prompt-verifier bijection review (§4).

**T18. Use natural, unintentionally introduced bugs over injected ones**
- (a) Defects come from real feature work, such as the trace agent's own mistakes, not from mutation operators.
- (b) BugPilot FeatAdd.
- (c) Claude Sonnet 4: 41.4% against 65.9% for SWE-smith [V~]. Older model.
- (d) Our corpus's corrections are exactly this. Prefer them over authored mutants for the *task*, and keep mutants for
  *test* hardening.
- (e) Needs reproduction plus provenance, which our pipeline already requires.

**T19. Private, unfamiliar repository (decontamination)**
- (a) The repo and its history are absent from pretraining.
- (b) SWE-Bench Pro commercial set; SWE-bench-Live.
- (c) Pro commercial best 51.5% against 61.5% public [S]; SWE-bench-Live 43.2% to 19.25% for Claude 3.7 [V~]; Illusion
  76% to 53% file-path recall [V~].
- (d) User-session repos already qualify. Never include a public upstream remote or a future commit.
- (e) None, provided provenance is clean.

**T20. High-centrality or multi-site latent fault**
- (a) The root cause is in a heavily called function, or several interdependent functions are wrong.
- (b) Breakpoint multi-corruption.
- (c) Breakpoint [V~]: 0% at four corruptions, but only for o4-mini, Claude 3.7 and GPT-4o. Locally, chained defects ✗.
- (d) Low priority. Try only as several coupled corrections from the trace.
- (e) Each site must be justified by a published rule.

**T21. Decisive fact carried by a non-text artifact the user supplied**
- (a) The rule's instance lives in an attached sample: a CSV, PDF, image or log.
- (b) SWE-bench Multimodal.
- (c) Fable 5.1 at 54.7% on Multimodal against 81% on Pro [S].
- (d) Only when the trace really contains the artifact. Ship it in the repo offline.
- (e) The artifact must be readable with the tools in the image.

**T22. Cross-language boundary inside the repo**
- (a) The fix must cross into the non-dominant language (SQL migration, shell, C extension, front-end).
- (b) GSO non-Python tasks.
- (c) GSO: Opt@10 4.0% against 21.4% [V~], but Fable 5.1 scores 89.1% on SWE Multilingual [S]. **Weak for bug fixes,
  plausible for performance.**
- (d) Use where the trace's stack is genuinely multi-language.
- (e) Toolchains must be pinned (`toolchain.json`).

**T23. Stale visible test against a newly published rule**
- (a) A repo test encodes the old behaviour. The user publishes the new rule. Keeping the visible suite green means
  violating the rule.
- (b) An ImpossibleBench-style conflict, inverted.
- (c) Evidence is indirect: GPT special-cases to keep tests green [V~]. Unmeasured as a difficulty lever [I].
- (d) Hidden tests check the new rule. Editing the stale repo test is legitimate.
- (e) The instruction must say the behaviour changes. This must not be confused with test_tamper (hidden tests are
  outside the repo).

**T24. Persona-layered requirements across steps (Expert Nitpicker)**
- (a) Later steps add precise refinements to earlier output (format, edge behaviour).
- (b) SWE-INTERACT persona.
- (c) −6.7 pp Opus 4.8 and −4.0 pp GPT 5.5 against a neutral persona [V].
- (d) Use real follow-ups from the trace.
- (e) Each refinement must be a published sentence.

**T25. Cumulative hidden suites across steps (maintenance pressure)**
- (a) Step k's grade includes the hidden checks of earlier steps, applied to the code as it stands after step k.
- (b) SWE-CI EvoScore.
- (c) SWE-CI: most models below 0.25 zero-regression; Opus above 50% [V~]. Locally, regression bait ✗.
- (d) Combine with T9 rather than using it alone.
- (e) Count shared mistakes once (CLAUDE.md: several checks can reveal one mistake).

**T26. Satisficing trap: first speedup or fix found versus the required level**
- (a) The published target needs a deeper change than the first plausible improvement.
- (b) SWE-fficiency median trajectories stop well under the action cap; GSO lazy optimisations.
- (c) Shown on performance tasks [V~]. For bug fixes this resembles chained defects ✗.
- (d) Use only with a quantitative published target (T12).
- (e) The target must be reachable by the reference and the alternative solution.

**T27. Ordering, set or format semantics stated precisely**
- (a) The rule fixes ordering or format.
- (b) Pro Verified clarifications ("define set/order semantics").
- (c) **Clarification turned FAIL into PASS** [V], so once published, models comply. Weak as a lever, essential for
  validity.
- (d) Use as part of M2 interactions, not alone.
- (e) Leaving it unpublished is invalid (teleport case).

Ranking for our targets [I], using 2026-model evidence and the local measurements:

| tier | techniques | why |
|---|---|---|
| most promising | T9, T4/T13/T15, T1, T2, T3, T16 | covers both families: T9 has evidence for both; T4/T13/T15 bite GPT; T1 bites Claude |
| next | T7, T6, T14, T10, T12, T5 | |
| substrate or weak | T17, T18, T19 (substrate); T8, T11, T20, T22, T23, T25, T26, T27 (weak or conditional) | |

---

## §4 Test and verifier hardening: making hidden tests reject plausible-but-wrong patches

| # | technique | what it catches / evidence | how to apply here |
|---|---|---|---|
| V1 | **Run the full developer suite**, not just the PR's tests | PatchDiff: 7.8% of plausible patches fail the full suite, a 4.5 pp drop (`django-13279` broke `_legacy_decode`) [V~] | run the repo's whole runnable suite as guardrails (PASS_TO_PASS) |
| V2 | **Differential testing against the reference** | PatchDiff: 29.6% of plausible patches diverge; 28.6% of those are certainly wrong; 66% are "uncertain" because of underspecification [V~] | fuzz public entry points with reference and candidate and diff the outputs. A divergence is a candidate check only if a published sentence decides it |
| V3 | **Mutation-guided augmentation with robustness filters** (STING) | 77% of Verified instances have a surviving variant; 1,014 tests; +10.8 line / +9.5 branch coverage; top agents −4.2 to −9.0 pp. Tests must pass the reference plus behaviour-preserving rewrites; 23% of candidates were dropped [V~] | our trap mutants plus LLM variants of the reference. Keep a test only if it kills a variant, passes the reference and the alternative, and survives renaming or restructuring |
| V4 | **Adversarial strengthening** (SWE-ABS) | 50.2% of instances strengthened; 19.71% of passing patches rejected; top score 78.80% to 62.20% [V~] | synthesise plausible-incorrect patches from the published rules and treat each surviving one as a hole |
| V5 | **LLM-generated augmentation tests validated on gold** (UTBoost, TestEnhancer) | UTBoost: 28.4% Lite and 15.7% Verified of passing patches wrong; 40.9% and 24.4% of rankings changed. TestEnhancer flags plausible patches on 97.11% of weak-test issues [V~] | `generate_cases` from published rules; gold must pass; the alternative must pass |
| V6 | **Agentic (multi-step) mutants** to stress-test the hidden suite | SWE-Mutation: agentic mutants cut detection from 71.04% to 39.81% [S] | mutants made by an agent attempting the task (blind solves, near misses) are harder than operator mutants. Ours exist as `blind_solve` |
| V7 | **End-to-end behavioural tests through public surfaces** | SWE-Lancer e2e Playwright ("harder to exploit") [V]; DeepSWE: FP 0.3% / FN 1.1% against Pro's 8.5% / 24.0% [V] | test via CLI, HTTP or the public API. Never import a helper only the reference introduces (`parseRpmQfLine`, `get_annotation`) |
| V8 | **Prompt-verifier bijection and acceptance-breadth review** | DeepSWE review [V]; Pro Verified fixed 75 narrow and 3 broad tests [V] | every check maps to an instruction sentence (`checks.json` already does this); run an independent alternative fix |
| V9 | **Independent LLM-judge audit of the verifier** | DeepSWE: judge and verifier disagree on 32.4% of Pro rollouts and 1.4% of DeepSWE's [V] | run a judge over pass/fail episodes during calibration to find FP/FN; the judge audits, the tests stay authoritative |
| V10 | **Property-based tests with targeted strategies** | PBT-Bench: bugs placed where default generators almost never go; even good agents misuse `assume()` [V~] | hidden PBT tests with explicit strategies over the trigger region, plus fixed seeds or `derandomize` for determinism |
| V11 | **Metamorphic relations** (for example, permutation invariance, idempotent re-run) | no SWE-family benchmark measures this directly [I] | use for rules like "order of input doesn't matter" or "running twice changes nothing" |
| V12 | **Hidden workloads plus deterministic cost** (performance) | perf reliability audit: most timing tasks unstable across machines [V~] | counters, ratios between two sizes, separate correctness suite |
| V13 | **Reward-hack detection** | GSO LLM judge for caching in timing loops; SWE-fficiency stack-introspection detection [S]; Pro Verified trajectory scan: answer access 103 to 0 [V]; ImpossibleBench: Claude edits tests, GPT special-cases [V~] | already have test_tamper, early_exit and shadow-guard controls. Add a trajectory scan for `git log --all`, `git show <sha>`, `/tmp` solution reads and network use |
| V14 | **No future history in the fixture** | Pro: 87% of external cheating cases read gold from `.git` [V]; DeepSWE: shallow clone at base [V] | the fixture's `.git` (if any) must hold only pre-task commits; prefer none |
| V15 | **Ship fixtures with tests** | Pro flipt cluster: 29 trials failed on fixture YAMLs that were not copied [V] | `tests/suites/data/` already enforces this |
| V16 | **Flake screening** (3 runs) | Pro and DeepSWE both run 3x [V] | the golden-1.0 three-run control already does this |
| V17 | **Count executed tests; reject runner tricks** | BenchJack-class exploits (conftest, pytest hooks, `sys.exit`) [S] ([2605.12673](https://arxiv.org/abs/2605.12673)) | `grade.py` JUnit counting and the early_exit control already cover this |
| V18 | **Avoid test-guided overfitting signals** | Overfitting: test-guided refinement *increased* overfitting (Claude 3.7 21.8% to 25.5%) [V~] | keep hidden tests disjoint from anything visible (`hidden.disjoint`) |
| V19 | **Discriminativeness metric** | R2E-Gym: under 20% of tests discriminate for most problems [V~] | report per check how many mutants and near-miss episodes it kills; drop checks that never discriminate |

What the numbers mean for us [I]. In public benchmarks with developer-written tests, 10-30% of "passing" patches are
semantically wrong (PatchDiff about 11%, UTBoost 15.7% on Verified, SWE-ABS 19.71%, SWE-bench+ 31%). A hidden suite
built only from the reference's natural tests will therefore *understate* difficulty. V2-V6 are how M1-M3 near misses
become counted failures instead of silent passes.

---

## §5 What does not make tasks hard (saturated or invalid levers)

1. **Small single-file issue fixes.** Fable 5 is at 95% on Verified [S], and most Verified tasks are under an hour and
   about 14 lines [V~]. Saturated.
2. **SWE-bench-Pro-shaped multi-file fixes with a full spec.** Fable 5.1 81.2% [S]. Expect pass@1 around 0.6-0.8 unless
   a specific mechanism is added [I].
3. **Language choice alone.** Fable 5.1 89.1% on SWE Multilingual [S]. The large language gaps of Multi-SWE-bench were
   on 2025 models [V~].
4. **Context length / repo size by itself.** LoCoBench-Agent reports long-context robustness [S]. Repo-size effects in
   SWE-bench-Live and FeatBench come from older models [V~].
5. **Hiding names or interfaces the tests depend on.** This is a void, not difficulty: the SWE-Bench Pro ablation
   (25.9% to 8.4%) is mostly false negatives [V]. OpenAI's 35.5% narrow and 18.8% wide tests [S] and Pro Verified's 75
   narrow tests [V] are the same mistake.
6. **Ambiguity.** Clarifying exact constants, set/order semantics, defaults and interfaces turned FAIL into PASS in Pro
   Verified [V]. Ambig-SWE shows models cannot tell when they are underspecified, so the failures are not genuine [V~].
7. **Turn, cost or context caps.** Budget-driven categories (not submitted, context overflow, loops) disappear without
   limits. Breakpoint's "tool budget" effect and SWE-Bench Pro's 50-turn analysis do not apply [V].
8. **Counter-edits on easy tasks.** Opus 4.8 −1.8 pp and GPT 5.5 −1.3 pp on Verified [V]. They only bite on long
   tasks.
9. **Synthetic local mutations as the task bug.** SWE-smith-style bugs are easier than natural ones; Claude Sonnet 4
   scores 65.9% against 41.4% on BugPilot [V~].
10. **Visible failing tests or a precise location.** Breakpoint's remove mode is easy; once the location is known,
    success rises to 70-90% [V~].
11. **Prompting for tests.** Test-writing prompts change behaviour but barely move resolution (GPT-5.2 stayed at 71.8%)
    [V~].
12. **Wall-clock performance targets.** Most are unreliable across machines [V~]. This is a validity defect, not
    difficulty.
13. **Locally measured ✗ levers**, consistent with the literature above: symptom-versus-cause with an agreeing mock;
    chained defects; decoy fix location; restraint; regression bait; revert residue; a later-step premise revision;
    hidden guard budgets; enumeration, breadth and volume; arithmetic; stacked traps of one type. The literature's
    nearest positives are different mechanisms: T9 (a standing rule broken by a later feature) is not T-revision; T1
    (a parallel sibling) is not decoy location; T8 needs *coupled* targets, not a checklist.
14. **Leaving answer channels open.** Not a difficulty lever, but it silently converts hard tasks into easy ones for
    Claude (DeepSWE: about 13% of Opus trials on Pro) and some open models (GLM-5.2 +21 pp) [V].

---

## Sources (primary)

- SWE-Bench Pro https://arxiv.org/abs/2509.16941
- SWE-Bench Pro Verified https://arxiv.org/abs/2609.08149
- DeepSWE https://arxiv.org/abs/2607.07946 and https://deepswe.datacurve.ai/blog/deepswe
- OpenAI, Verified introduction https://openai.com/index/introducing-swe-bench-verified/
- OpenAI, Verified retirement https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/ (403 to the fetch;
  read via https://blog.pebblous.ai/blog/swe-bench-verified-retired/en/)
- Epoch https://epoch.ai/publications/what-skills-does-swe-bench-verified-evaluate
- Ganhotra https://jatinganhotra.dev/blog/swe-agents/2025/06/05/swe-bench-verified-discriminative-subsets.html
- PatchDiff https://arxiv.org/abs/2503.15223
- UTBoost https://arxiv.org/abs/2506.09289
- SWE-bench+ https://arxiv.org/abs/2410.06992
- STING https://arxiv.org/abs/2604.01518
- SWE-ABS https://arxiv.org/abs/2603.00520
- SWE-Mutation https://arxiv.org/abs/2605.22175
- Test overfitting https://arxiv.org/abs/2511.16858
- SWE-Bench Illusion https://arxiv.org/abs/2506.12286
- ImpossibleBench https://arxiv.org/abs/2510.20270
- Failure taxonomy https://arxiv.org/abs/2509.13941
- Breakpoint https://arxiv.org/abs/2506.00172
- GSO https://arxiv.org/abs/2505.23671
- SWE-Perf https://arxiv.org/abs/2507.12415
- SWE-fficiency https://arxiv.org/abs/2511.06090
- Perf reliability https://arxiv.org/abs/2607.01211
- FeatBench https://arxiv.org/abs/2509.22237
- NoCode-bench https://arxiv.org/abs/2507.18130
- FeatureBench https://arxiv.org/abs/2602.10975
- Commit0 https://arxiv.org/abs/2412.01769
- SWE-Lancer https://arxiv.org/abs/2502.12115
- SWE-smith https://arxiv.org/abs/2504.21798
- R2E-Gym https://arxiv.org/abs/2504.07164
- SWE-Gym https://arxiv.org/abs/2412.21139
- BugPilot https://arxiv.org/abs/2510.19898
- SWE-rebench https://arxiv.org/abs/2505.20411
- SWE-bench-Live https://arxiv.org/abs/2505.23419
- Multi-SWE-bench https://arxiv.org/abs/2504.02605
- SWE-bench Multimodal https://arxiv.org/abs/2410.03859
- SWE-bench-java https://arxiv.org/abs/2408.14354
- SWE-Sharp-Bench https://arxiv.org/abs/2511.02352
- SWE-EVO https://arxiv.org/abs/2512.18470
- RoadmapBench https://arxiv.org/abs/2605.15846
- SWE-CI https://arxiv.org/abs/2603.03823
- LoCoBench-Agent https://arxiv.org/abs/2511.13998
- Saving SWE-bench https://arxiv.org/abs/2510.08996
- SWE-INTERACT https://arxiv.org/abs/2606.30573
- SWE-Touch https://arxiv.org/abs/2608.02499
- Ambig-SWE https://arxiv.org/abs/2502.13069
- SWE-Compass https://arxiv.org/abs/2511.05459
- SWE Atlas https://arxiv.org/abs/2605.08366
- PBT-Bench https://arxiv.org/abs/2605.15229
- Agent-written tests https://arxiv.org/abs/2602.07900
- BenchJack https://arxiv.org/abs/2605.12673

Aggregators (all [S]):

- https://benchlm.ai/models/claude-fable-5-1
- https://codingfleet.com/blog/swe-bench-pro-leaderboard-2026/
- https://www.morphllm.com/swe-bench-pro
- https://www.mindstudio.ai/blog/gpt-6-astra-benchmarks-analysis
- https://lmcouncil.ai/benchmarks
