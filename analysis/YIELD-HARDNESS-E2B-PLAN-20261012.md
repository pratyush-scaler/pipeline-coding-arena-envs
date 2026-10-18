# More hard tasks from the same traces, built to run on Arena's E2B setup (2026-10-12)

This is the synthesis of four new research reports, our own screen data and Arena's QC overview.

- **Research reports** (all in `analysis/web-research/`; every claim there is tagged verified or inferred, with URLs):
  - W7: Harbor 0.20 @ cfc1bcb on E2B.
  - W8: 2026 hardening research and representation-neutral verification.
  - W9: turning traces into tasks.
  - W10: engine-bound environments.
- **Our own data:** the latest screens of 34 tasks.
- **Arena's QC overview:** `[Scaler] Arena RL QC Eval Overview.docx`.

Nothing in `pipeline/` was changed for this document.

---

## 0. The five things that matter most

1. **UPDATE (user, 2026-10-12): the shipped tasks' environments DO reproduce at Arena's end.** Arena's "Docker compatibility layer" therefore does not go through the stock `Template.from_dockerfile` parser. Most likely it builds images with Docker (W7 §3.6, reading b).
   - **What this means for R1 and R2.** They describe a path Arena does not use, so they are **not blockers**. Keep them only as hygiene for the day Arena changes integration.
   - **What it means for R3-R8.** They were inferred for the stock provider. They are unconfirmed on Arena's actual path and are now low priority. R8 is already mitigated.
   - **What this means for our validation.** The Harbor 0.20 Docker repro (`h020_repro.py`) is a fair proxy for Arena's setup.
   - The original finding is kept below for reference.

   *Original finding:* **Arena's E2B setup will very likely refuse to build our current packages.** This was verified by execution, but only for the stock E2B SDK path.
   - **The blocker.** Arena pins e2b SDK 2.25.0. When that SDK reads our agent `environment/Dockerfile` the way Harbor 0.20 @ cfc1bcb does, it raises `Multi-stage Dockerfiles are not supported`.
     - W7 found this on all 26 delivered agent images and 8 of the 26 verifier images.
     - I re-ran the probe myself on `a_f2_cli-tool` with SDK 2.25.0 and got the same error.
   - **Why we haven't seen it.** Our Harbor 0.20 repro (`h020_repro.py`) uses `--env docker`, which builds multi-stage images happily.
   - **Six more E2B differences**, all from W7 §7. The first three would also break builds or grading; the next two degrade them:

     | ID | Problem | Effect |
     |---|---|---|
     | R2 | SDK 2.25.0 ignores `!` lines in `.dockerignore` | `COPY deps/` finds nothing |
     | R3 | `useradd --uid 1000 agent` collides with E2B's own `user` account | Image build fails |
     | R4/R6/R7 | Every build step and command runs in a Debian login shell, which resets PATH; Dockerfile `ENV` is not present at runtime | Go and venv tools vanish; the agent's profile.d PATH is written after the reset |
     | R5 | The separate verifier gets E2B's default 1 GiB of RAM unless `[verifier.environment]` sets `memory_mb` | Chromium, Postgres and big Node suites can be OOM-killed, which looks like a false 0 |
     | R8 | 11 Node tasks have an absolute `node_modules` symlink inside `/app/repo` | Harbor's tar filter would stop at it. **Already mitigated:** every delivered Node task excludes `node_modules` from the artifact (checked 2026-10-12). |

   - **What would change the picture.** It depends on how Arena builds images (§3.4). If they `docker build` images themselves and launch from the result, R1 and R2 disappear but R3-R8 remain.
2. **Hardness is not what is holding back the tasks we've already built; validity is.**
   - All 34 tasks with a screen are under 0.5 on both models. Fable is at 0.0 on 27 of them and Astra on 29. Yield is lost in review and validity loops, not in hardness.
   - That hardness rests on brittle ground. About 36% of hidden checks are **never** passed by either model (145 of 405 for Fable, 143 of 405 for Astra). Another 25% (Fable) and 45% (Astra) are always passed.
   - The never-passed checks are exactly what a strict reviewer and Arena's *Trap Fairness* and *Solvability* checks scrutinise.
3. **The review loop doesn't converge because we loosen assertions instead of moving where the test observes.**
   - The field's answer (W8) is to observe only at declared interfaces and canonicalise in the grader.
   - Run a **variant gauntlet**: spec-equivalent variants of the reference that must all pass. That turns each reviewer objection into an executed check.
   - Run candidate code in a separate process and pass JSON only.
   - Pick inputs where right and wrong answers are far apart, so being lenient about representation costs nothing.
4. **Converting traces is 2-3× short of what the data supports.**
   - Only about 150 of the 370 coding sessions were classified with the code-aware rules.
   - Five conjunctive gates kill recall.
   - About 10 engine-bound kills are classifier mapping bugs, not missing runtimes: `java`, R, MATLAB, Groovy.
   - The hardest bug class published is regressions the agent introduced itself. Our persistent defects are exactly that, and we don't seek them out.
5. **Some engine-bound sessions can be recovered faithfully.**
   - Godot (headless), HDL (Icarus/GHDL/cocotb), Jenkins (JenkinsPipelineUnit), R/Octave, Flutter and Roblox logic (Lune) all run headless on Linux.
   - Unity Editor, MetaTrader 5, Garry's Mod and Minecraft can't be pinned or redistributed.
   - Five Lua sessions are game cheats and should be policy-killed.

---

## 1. Arena's QC, and where we stand on each group

Arena checks six groups (QC overview). For each, what we already enforce and what is missing:

| Group, dimensions | What we enforce | Gaps found in this research |
|---|---|---|
| **Construction**: Task Clarity, Additions/Alterations vs Source, Dropped Requirements, Trap Fairness | voice rules; checks quote instruction sentences; determinacy probes; semantic review | **Trap Fairness**: never-passed checks need stronger fairness evidence (§4.3). **Dropped Requirements**: no converse audit that every user condition in the trace maps to a check or a declared omission (W9 §7.3). |
| **Source fidelity**: Goal/Failure-Mode/Scenario Preserved, Artifact Provenance | F-levels and ledger; recovered bytes; trap citations | Arena *"discounts traps"* when judging faithfulness, so authored traps earn no fidelity credit. Traps grounded in the trace's own failure moments (persistent defects, agent-introduced regressions) are better on both hardness and fidelity (§2.2). **Failure-Mode Reproduction**: bisect over turn snapshots to prove where the failure appears (W9 R3). |
| **Environment**: Workspace Fidelity, Dependency Completeness, Reproducible Execution, Isolated Evaluation, File-Change Rate | pinned digests, snapshot apt, /opt/deps, separate verifier, shipcheck, Harbor 0.20 Docker repro | **The E2B semantics gap (§3) is the largest open risk.** Build-time network may also be limited (W7 R12); vendoring wheels, npm tarballs and Go modules removes that dependency. |
| **Reference**: Reference Correctness, Reference Breadth | golden ×3; one alternative; mutants | **Breadth** is exactly the reviewer's "third correct shape" complaint. Add the variant gauntlet plus 2-3 independent alternatives from different vendors (W8 #10, W9 R4). |
| **Rubrics**: Soundness, Completeness, Verifiability, Reward Gradient, Reward Proportionality | data-only grader; weights; guardrails; critic | **Proportionality**: each item should be one atomic decision, guardrail gate × weighted sum (W8 #16). **Completeness**: the critic catches gaps, but closing them as instances keeps reopening; close them as classes. |
| **Calibration**: Solvability, Discrimination, Failure-Mode Reproduction | 5×2 screen, 10×2 release, genuine-failure certificate | **Solvability** for 0/20 tasks: Anthropic's guidance is that 0% over many trials usually means a broken task. Our 0/20 tasks need near-miss and alternative evidence. **Discrimination** is healthy (mean rewards spread 0.1-0.9; Fable average 0.45, Astra 0.54). |

---

## 2. Converting more traces into hard tasks (yield)

### 2.1 Where the funnel loses seeds today

These numbers come from `analysis/CODING-TRACES.md`, W9 §7.6 and W10.

- **Supply.** 500 traces, of which 370 are coding sessions. Only about 150 were classified with the code-aware rules at first. W9 notes that `SESSION-SELECTION-FOR-YIELD.md` counts 284 seeds over 220 sessions surviving C1 classification; reconcile the two figures before relying on either.
- **Runtime of the 150.** 110 native, 20 browser-only, 7 engine-bound Lua, 13 "no runtime".
- **Classifier mapping bugs** (verified in `pipeline/config.py`):
  - `java` is in `ENGINE_LANGUAGES` (l.417).
  - `.m` maps to `objc`, so MATLAB files are treated as engine-bound (l.404).
  - Every language not explicitly native or browser falls through to `"engine"` (l.474), including R, Groovy, Ruby, Scala, Elixir and Perl.
  - There is no JVM toolchain, although 13 sessions use Java and 12 have a `build.gradle`.
- **Yield after the screen** (W9 §7.6): hard and certified per validated task is 0.31 (C1) and 0.41 (C2); validated per built is 0.23 (C1) and 0.49 (C2). The recent batch shipped 10 of about 70 built.
- **Published comparison.** SWE-Together, the only published session-to-task pipeline, keeps 0.97% of raw sessions with no hardness filter. We are already above that because the repo state is recoverable at every turn.

### 2.2 Twelve changes, ranked by expected effect on yield

Effect estimates are W9's projection [I] unless noted otherwise.

| # | Change | Why it should work (evidence) | Effect |
|---|---|---|---|
| Y1 | **Classify all 370 coding sessions and rank seeds by expected yield** instead of five conjunctive kill gates: EY = P_runtime × P_repro × P_valid × P_hard × N_distinct. Send borderline seeds to an executed reproduction attempt. | Conjunctive LLM gates destroy recall: SWE-rebench V2's three-judge consensus had recall 0.06, against 0.31 when scores were averaged (W9 §2.4) | ×1.3-1.5 seeds |
| Y2 | **Agent-introduced regressions first.** These are moments where the trace's own assistant broke something that worked at an earlier snapshot. | BugPilot: agent-introduced bugs are the hardest published class (Sonnet 4 solves 41.4%, against 65.9% for injected SWE-smith bugs). They are F3 by construction, and they are *exactly* the failure mode Arena's "Failure-Mode Reproduction" checks for. | Raises P_hard and fidelity together |
| Y3 | **Locate the failure by bisecting over turn snapshots.** Run the designer's reproduction on every turn snapshot and find the first one where it fails, instead of asking an LLM where the error happened. | LLM failure attribution is under 10% accurate (W9 §3.5). Bisecting is deterministic and leaves an audit trail. | Cheaper F3, a fidelity receipt for QC |
| Y4 | **Replay probe before full authoring.** Run 1 Fable and 1 Astra episode on the raw defect, using the user's own complaint as the instruction. | Cheap triage. If both models solve it, hardness has to come from authored interlocks. If either fails, it's an F3 candidate whose zero is then certified. | Better P_hard estimates, less wasted authoring |
| Y5 | **Fix the classifier mappings** (`java`, R, MATLAB, Groovy, unknown → engine) and **policy-kill game cheats and exploits** (5 Lua sessions, W10). | Code facts, verified | ~10 seeds back; removes a QC and policy risk |
| Y6 | **New toolchains:** JVM (Java and Kotlin JVM tests, JenkinsPipelineUnit, MockBukkit), Godot headless, HDL (Icarus, GHDL, Yosys, cocotb), plain Lua and Luau via Lune, R and Octave. | W10 tables: all have pinned, redistributable, headless Linux runtimes | W10 counts about 9 seeds at P0 and 9 more at P1, plus up to 3 Paper/Velocity plugins |
| Y7 | **Browser lane.** Deterministic Playwright checks grounded in the prompt, using roles and visible text, never selectors or hidden DOM. VLM or agent judges are advisory only. | HTML appears in 198 sessions, the largest pool. The best LLM judge agrees with experts 70.3% of the time on web work, against 84.6% for humans (W9 §6.1). | Opens the biggest language pool |
| Y8 | **Reproduction-agent route** for seeds killed as "no reproduction" that still quote a concrete symptom | Published agents write a fail-to-pass reproduction for 58-70% of SWT-bench issues (W9 R5) | Recovers about half of those seeds |
| Y9 | **One task per distinct root cause, 1-2 per productive session, 3 at most.** Prefer combining two root causes into one interlocked task. Deduplicate by executing: if task A's reference passes task B's objectives, they are the same task. | W9 §4 | ×1.2-1.5 tasks per session |
| Y10 | **Recorded HTTP fixtures** for seeds that depend on an external API, declared as authored bytes | Removes the "external service" kill without network | Small but cheap |
| Y11 | **Rebuild the repo for no-source seeds** by replaying the `write_file`/`edit_file` contents | This was the largest kill bucket in C1 (517). It needs a measurement first. | Potentially large, unmeasured |
| Y12 | **Mirror lane** (move the failure mode into another repo), opt-in only | SWE-Mirror: 46% yield, 88% judged faithful, but it weakens Arena's *Artifact Provenance* | Optional |

**Projection (W9 §7.6, inference).** About 145 built, 73-87 validated, 26-39 hard and **~20-30 shipped per 370 coding sessions**, roughly 2-3× today. Session supply and per-task hardness remain the binding constraints.

### 2.3 Getting the most out of one trace (extraction checklist)

For every seed the designer should pull out, in this order:

1. **Every correction moment, classified** (persistent defect, spec correction, constraint, revert, scope) and tied to the turn snapshot before and after it. Persistent defects, where the original model *failed in real life*, are the strongest hidden-test targets, because the trace proves the failure happened.
2. **The first snapshot where the defect appears**, found by bisecting with the reproduction (Y3). That gives the fixture, the cut point and the evidence of where the failure began.
3. **Rules the user stated outright** (W9 §7.3: precision-first extraction where every rule cites the thread). Each one maps to a check or a declared omission, which covers Arena's *Dropped Requirements* check.
4. **Constraints and reverts** ("don't change X", "put it back") become guardrails. They are faithful, but are not difficulty by themselves; restraint is a dead lever.
5. **Data shapes the user actually had**: real file encodings, platforms (Windows paths, CRLF, BOM), locales and odd records. These are where *format-variant* and *Unicode-class* items bite, and they are faithful rather than invented.
6. **Places where one rule meets another rule, or meets saved state**, for example a later step that re-governs earlier code. These are interlock candidates aimed at both models.
7. **Several distinct root causes** (Y9). One task each, or two combined into an interlock.

---

## 3. Running on Arena's setup (Harbor 0.20 @ cfc1bcb on self-hosted E2B, 2 vCPU / 4 GiB)

### 3.1 What W7 found in the code (verified)

- cfc1bcb is v0.20.0 plus 43 commits on branch `harbor-miles-v0.20.0`; the repo is now `harbor-framework/harbor`. Several E2B fixes landed on that branch after the pin, so Arena doesn't have them.
- 0.20 **accepts** our `task.toml`: steps, artifacts with `exclude`, `agent.user`, `no-network`, per-step verifier timeout and env, and a separate verifier (built as a second E2B sandbox from `tests/`). Unknown keys are silently ignored; `schema_version` is never read.
- On E2B, Harbor never runs `docker build`. The e2b SDK converts the Dockerfile itself, and that conversion is where R1-R4 come from.

### 3.2 Risk register and author-side fixes

| ID | Severity | Affects | Fix (generator side: `author/toolchain.py`, `author/build.py`) |
|---|---|---|---|
| R1 | Blocker | 26 agent images, 8 verifiers | Single-stage Dockerfiles. Install Node from a vendored, sha256-checked tarball in `deps/`, or use one base image. Vendor the CA bundle. |
| R2 | Blocker (SDK 2.25.0) | 26 | No `!` lines in `.dockerignore`, or no `.dockerignore` at all |
| R3 | Blocker | 19 (python and go bases) | Don't pin UID 1000: `useradd -m agent`, or guard it with `getent` |
| R4 | Blocker | go-desktop-app | Absolute tool paths inside `RUN` (e.g. `/usr/local/go/bin/go`) |
| R5 | High | all verifiers | Set `cpus = 2` and `memory_mb = 4096` in `[verifier.environment]` |
| R6 | High | go-desktop-app | Full `PATH` in `checks.json` `toolchain.env` for **every** toolchain, not just Python and Node |
| R7 | Medium | all agent images | Write a **literal** PATH (and `VIRTUAL_ENV` and the `GO*` variables) into `/etc/profile.d`; never `"$PATH"` at build time |
| R8 | **Already mitigated** (checked 2026-10-12) | Node tasks | Every delivered Node `task.toml` already lists `node_modules` in the `/app/repo` artifact `exclude`. Keep that rule for future toolchains. |
| R9 | Infra | all | Pre-warm one trial per task at `-n 1`; `--force-build` after any resource change; confirm the tier's maximum sandbox length (Harbor at the pin requests 24 h) |
| R10 | Low | all | Graders never rely on `+x`, symlinks or empty directories in uploaded artifacts; already true today |
| R12 | Arena-side | all | Confirm the build-time allowlist (PyPI, npm, Go proxy, Docker Hub, MCR, Playwright CDN). Vendoring dependencies into `deps/` removes the need for any of it. |

**None of these changes difficulty or grading semantics.** All are environment plumbing. They do, however, change every package's identity, so every released and shipped task would need regenerating and re-validating, including the 10 already in the Final Batch.

**Inventory run on all 26 delivered zips (2026-10-12, e2b SDK 2.25.0, `pipeline-artifacts/shipfix/tools/e2b_parse_probe.py`):**

| Finding | Zips affected |
|---|---|
| Agent image fails the parse (multi-stage) | 26 of 26 |
| Verifier image fails the parse | 8 of 26, all with Node-based verifiers |
| `!` lines in `.dockerignore` | 26 of 26 |
| UID 1000 pinned | 17 of 26 |
| `[verifier.environment]` memory unset | 26 of 26 |
| `node_modules` excluded where present | all Node tasks |

### 3.3 How to validate against E2B semantics (cheapest first)

1. **Offline exact-parser check (zero cost, verified to work).** Run every Dockerfile through `e2b.Template(...).from_dockerfile()` plus `to_json()` under SDK 2.25.0 and the latest SDK. The script is W7 Appendix A, currently at `/tmp/w7r/scripts/e2b_parse_probe.py`; move it into the repo before `/tmp` is wiped again. Add greps for R3-R8. This belongs in shipcheck.
2. **Real Harbor @ cfc1bcb with `-e e2b` on E2B cloud** (needs a **Pro** key; Hobby's 1-hour cap rejects the hard-coded 24-hour timeout):
   ```
   harbor run -p <task> -a oracle -e e2b --override-cpus 2 --override-memory-mb 4096 -k 1 -n 1 --force-build
   ```
   Run it for nop as well, plus one run without overrides to see what Arena gets at default resources. **We don't have an E2B key** (`keys.env` has none).
3. **Self-hosted E2B** needs a host with `/dev/kvm`. **Our Coder workspaces have no KVM** (checked on arena-coding-3).
4. **Docker approximation.** A login shell with an empty environment for every command, E2B's `user` account injected, uploads flattened, `--shm-size`, `--network none`, `--cpus 2 --memory 4g` (W7 §8.2D). This catches R3-R8 and R10 but not the parser errors.

### 3.4 Questions for Arena (W7 §8.3)

1. Do you run the stock `harbor run -e e2b` (`Template.from_dockerfile`), or build images with Docker and launch from `docker_image` or a prebuilt template? What exactly is the "Docker compatibility layer"?
2. Which e2b SDK version (the lock pins 2.25.0) and which E2B infra/envd version do you run?
3. Do you pass `--override-cpus 2 --override-memory-mb 4096`? Without them our separate verifier gets 1 GiB.
4. What does the allowlist cover **during template builds**, as opposed to runtime?
5. What are your tier's maximum sandbox length and disk size?
6. Could you share `result.json` or `exception.txt` from one of our failing trials, if any failed?

---

## 4. Hardening: what actually beat Fable and Astra, and what to add

### 4.1 Our own measurement

This is the latest screen of each of 34 tasks: per-check pass rates, with each check mapped to the levers its `quality.lever_plan` names.

- Checks carry several levers each, so the effects are confounded. Treat the table as directional.
- These are rates per check, not per task. Task pass@1 is low because one episode has to pass every check.

| Lever (≥ 4 tasks) | Tasks | Fable passes | Astra passes | Reading |
|---|---:|---:|---:|---|
| unicode_class_member | 14 | **0.32** | **0.34** | strong for both |
| rules_stack_not_first_match | 4 | 0.31 | **0.11** | strong, small sample |
| first_green_stop | 5 | 0.32 | **0.26** | strong, used as an amplifier |
| numeric_idiom_betrayal | 9 | **0.32** | 0.48 | Fable-leaning |
| rule_reaches_existing_code | 17 | **0.34** | 0.53 | Fable-leaning |
| abstain_or_resolve | 8 | **0.35** | 0.56 | Fable-leaning |
| real_entry_point | 16 | 0.42 | **0.36** | Astra-leaning |
| field_not_line | 7 | 0.41 | **0.35** | Astra-leaning |
| ordering_idiom_betrayal | 20 | **0.38** | 0.49 | |
| masking_defect | 11 | 0.39 | 0.46 | |
| duplicate_members | 13 | 0.40 | 0.39 | |
| validator_admits_variants | 4 | **0.24** | 0.62 | Fable only |
| nested_exception_chain | 3 | **0.20** | 0.60 | Fable only, small sample |
| fault_atomicity | 2 | 0.57 | **0.23** | Astra only, small sample |
| cross_artifact_invariant | 11 | 0.54 | **0.41** | Astra-leaning |
| *most-used four* (rule_interaction_rare_input, scope over- and under-reach, format_variant_unseen) | 32-33 | 0.42-0.46 | 0.49-0.56 | workhorses; each check passes about half the time |
| **weak:** revision_reach | 8 | 0.74 | 0.60 | rarely bites |
| **weak:** local_convention_over_default | 14 | 0.55 | 0.65 | rarely bites |
| **weak:** error_path_contract | 13 | 0.61 | 0.59 | rarely bites |
| **weak for Astra:** feature_composition, input_population_by_role, value_distribution_shift, sibling_path_omitted | 4-23 | 0.34-0.47 | **0.58-0.72** | Astra handles these |

**Implications:**

- To aim at both models, interlock a Unicode-class or rule-stack item with one Fable-leaning lever (numeric idiom, rule reaching existing code, abstain-or-resolve) and one Astra-leaning lever (real entry point, field not line, cross-artifact, fault atomicity).
- Stop relying on revision reach, local convention and error-path contracts as primary levers.

### 4.2 New mechanisms from the 2026 literature (W8 §7a), in priority order for us

1. **Counterexample-backed differential checks at the observable interface.** In SWE Refactor Bench, adversarial verifiers broke 68.2% of the submissions that had passed every fixed check. Inputs come from generators, the reference runs inside the verifier, the outputs are put in canonical form, and a case is kept only if every alternative passes it. *This is both harder and representation-neutral.*
2. **Re-assert every still-active rule inside each step, on fresh inputs.** In EvoCode, 87-90% of failures were a missed active requirement, and multi-round scores ran 22-40 points below single-round. This is our `long_horizon_accumulation`, applied systematically rather than occasionally.
3. **Hidden-only requirement audit of captured passing episodes** (TestJack: resolution fell from 50.6% to 33.2%). Generate requirement tests from passing episodes and keep only those that the reference and alternatives pass and the reviewer accepts. This fits our fast-harden.
4. **Adversarial verifiers from both model families, strongest first.** Which model runs the verifier moves the break rate about 30 points. Point our critic at mutants and blind solves using both a Claude-family and a GPT-family verifier, accepting only executable counterexamples.
5. **A standing rule against an in-place request.** A published user or repo rule conflicts with a plausible in-repo request (an issue, TODO or teammate comment), and hidden items enforce the rule where the request's path looks right. The HANDBOOK benchmark measured 23-36% strict pass. Interlock it with an exact-predicate item for Astra.
6. **Mutated published parameters.** The trace's own thresholds or windows differ from the library default, and hidden inputs sit between the two values.
7. **Check-then-act consistency.** The code must act on the result of a validation it already performs.
8. **Propagation to peripheral consumers.** A changed contract has three or more consumers; exercise the peripheral ones through their real entry points.
9. **Model-based checks for stateful code.** Run generated operation sequences against a simple model (Hypothesis `RuleBasedStateMachine`) with fixed seeds.

**Anti-levers to avoid:** unfamiliar languages (agents write generators), permission or resource restrictions, labelled scope or consent traps, partial credit computed as raw test fractions.

### 4.3 Making hardness robust instead of brittle (our data and QC)

- **Today:** about 36% of checks are never passed, and those carry most of the hardness. Each is a Trap Fairness and Solvability liability, and they draw the reviewer's "not independently justified" findings.
- **Target:** more checks that are each passed **30-70%** of the time, so the task-level pass rate is low because few episodes pass them all, while every check is visibly fair and solvable.
- **Keep never-passed checks only with the full proof:** an executed mutant fails, the reference and ≥ 2 alternatives pass, a near miss is adjudicated, and a "should this pass?" determinacy probe agrees on both model families.
- **Reward shape** (W8 #16):
  - one atomic decision per item;
  - guardrail gate × weighted sum, with a capped core band;
  - executed controls showing nop < baseline mutant < partial mutants < reference;
  - an item-level point-biserial audit, where an item that better episodes fail more often signals over-specification.

### 4.4 Tests that are strict, neutral and hack-proof (fixes the review loop; W8 §7b)

1. **Observe only at a declared interface:** exit code and parsed output, files read with standard parsers, HTTP responses, DB rows, the DOM through roles and visible text, or a public callable's JSON-marshalled return value. Never source text, AST, declaration form, private helpers or framework internals.
2. Every graded axis is pinned by an instruction sentence, the repo's normative contract or the real consumer. **Canonicalise everything else in the grader, then compare exactly.**
3. **Margins beat tolerances.** Pick inputs where right and plausible-wrong outputs differ far beyond any tolerance.
4. **Expected values are executed, never typed.** Run the reference, and the original fixture for preserved behaviour, inside the verifier.
5. **Variant gauntlet before review.** Spec-equivalent variants of the reference, one axis each (declaration form, CJS/ESM export, container type, key order, float format, error wording), must all pass. **Every reviewer objection becomes a variant and is executed.** That is how the loop converges.
6. **Candidate code never shares a realm with a verdict.** Run it in a child process as the unprivileged `grader` user with a scrubbed environment, and pass JSON over a file. In browsers, assert through Playwright's utility-world locators. `node:vm` and jsdom are not security boundaries, as we saw directly with prototype monkeypatching in c-068.
7. **Real engines, pinned.** No homemade Vue or DOM simulators; c-004 went 20 repairs on one.
8. **Pair every leniency with a must-reject case** that differs in meaning, not representation.
9. **Re-grade every captured episode after each hidden-suite edit.** An episode that newly passes gets a hack review; one that newly fails gets a validity review (Terminal-Bench 3's `/regrade`).
10. **Mechanical interface rule (W9 §7.3).** Hidden tests may touch only fixture symbols, entry points or names quoted in the instruction. Statically scan `tests/suites/` for violations.

---

## 5. Engine-bound environments (W10)

| Ecosystem | Verdict | Approach |
|---|---|---|
| Godot | Recover now (5 seeds) | Official MIT binary; `--headless --import`, then GUT or gdUnit4 with JUnit output. The real engine runs. |
| HDL (FPGA) | Recover now | Icarus, GHDL and Yosys from Debian, plus cocotb 2.1 (JUnit `results.xml`). Verilator 5.036+ has to be built from source. |
| Jenkins Groovy | Recover now (1 seed) | JenkinsPipelineUnit 1.31 on a new JVM toolchain |
| R, Octave | Recover now (3 seeds) | Classifier bugs only |
| Flutter/Dart | Recover now, watch size (2 seeds) | `flutter test` needs no device; prune the SDK; pre-fill the pub cache; grade with `--offline` |
| Android/Kotlin | With effort (4 seeds) | JVM tests plus Robolectric 4.17. The SDK can't be redistributed, so download it at image build. Tight at 4 GiB. |
| Roblox | With effort, logic layer only | Rojo plus Lune `@lune/roblox`: real place files, about 23 Instance methods and the real datatypes. No events, physics or services. Put the first one through review to confirm it isn't judged a "fake runtime". |
| Paper/Velocity plugins | With effort | MockBukkit on the JVM toolchain |
| Unity | Editor not feasible; pure logic with effort | Login-only licence activation and a 5.4 GB image rule out the editor. Use .NET + NUnit on code that never touches `UnityEngine`. |
| MQL5 | Not faithfully | MetaTrader 5's installer can't be pinned and its licence is hostile. Only a Python port is possible, which makes it F1/F0. |
| Garry's Mod, Minecraft client mods, GoreBox | Not feasible | Game binaries can't be redistributed |
| Cheat/exploit Lua (5 sessions) | **Policy kill** | Regardless of runtime |

Runtime limits that bite: no KVM (so no Android emulator), a 1-hour E2B template build limit, disk tiers of 10-20 GiB, and slow compilers (Gradle, Flutter, Verilator), which call for fewer, wider checks because `grade.py` runs the runner once per check.

---

## 6. Recommended next steps

**Needs your decision or input:**

1. ~~Ask Arena which E2B integration they use~~ **Resolved (user, 2026-10-12): the shipped environments reproduce at Arena's end.**
   - No E2B-driven generator change is needed.
   - The Harbor 0.20 Docker repro remains our acceptance gate.
   - The single-stage and no-`!` dockerignore rules are optional hygiene. Batch them with other generator work if ever, since they change every package's identity.
   - Optional low-cost item: setting `cpus`/`memory_mb` in `[verifier.environment]` makes verifier resources explicit whatever Arena overrides.
4. **Operations:**
   - The repair and package drivers have been **down since 2026-10-11 14:15**. The disk filled ("No space left on device" writing `repair.json`).
   - State is intact and backed up to `pipeline-artifacts/shipfix/state-backups/`.
   - Docker Desktop holds 134 GB locally.
   - Coder workspaces auto-stopped, and their root disks reset. Data is safe in `~/arena-mirror` and is re-bound on the next run.
   - The drivers write state non-atomically. A temp-file-and-rename write would remove the truncation risk.
   - **The drivers should not be restarted until there is disk headroom.**

**Can start now, without touching `pipeline/`:**

5. Move W7's parser probe into the repo, outside `pipeline/`, and run it plus the R3-R8 greps across every package and final version, producing a per-task E2B inventory.
6. Recompute the per-lever table (§4.1) at task level, with partial correlations, once more tasks are screened.

**Pipeline changes to schedule** (each changes `policy_identity`, so batch them while no releases are in flight):

7. Selection: classify all 370 sessions; expected-yield ranking; fix the classifier mappings; policy-kill cheats; bisect over turns; replay probe; distinct root causes.
8. Toolchains: JVM, Godot, HDL, Lua/Luau-Lune, R/Octave, then Flutter and Robolectric.
9. Author and review: variant gauntlet and executable objections; mechanical interface scan; 2-3 cross-vendor alternatives; process-boundary transport for every JS and Python runner; re-grade on every edit; never-passed-check proof obligations; atomic-item reward shape.
10. Hardening: per-step re-assertion of active rules; requirement audits of captured passing episodes; two-family adversarial verifiers; the standing-rule-versus-request lever.

### What is not established

- None of the new mechanisms (variant gauntlet, standing-rule conflict, re-assertion) has been measured on *our* tasks.
- The yield projection is arithmetic, not a forecast.
- Whether Arena uses stock `from_dockerfile` is unknown until they answer. The parser failure is proven only for that path.
- The per-lever table is confounded: one check carries several levers.
