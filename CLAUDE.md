# arena-envs-codingtasks-large-dump

Building CODING RL environments from Arena agent traces, at batch scale. The
agent is dropped into a repository lifted from a real session and must fix,
extend or finish it; a separate offline verifier executes HIDDEN TESTS against
what it leaves behind. The one truth source is an executed test.

This workspace is the coding sibling of `arena-envs-tasks-large-dump` (the
non-coding, document pipeline). It shares that pipeline's shape -- select,
author, grade, calibrate, harden -- and none of its graders. Never edit the
sibling from here.

## Before designing anything

| doc | when |
|---|---|
| `DESIGN.md` | **authoritative on the bundle contract**: layout, grader as data, controls, traps, faithfulness, toolchains |
| `analysis/CODING-TRACES.md` | what the corpus actually carries: 370 coding sessions, 352 with a fully recoverable tree, 17 that ever ran a test runner; the correction classes and what they are good for |
| `rulesets/ANTI_REWARD_HACKING.md` | before writing any hidden test -- the answer must be derived by running code, never stored |
| `rulesets/PLAYBOOK_GENERAL.md` | difficulty levers; the seventh (a premise that changes) is the one that measured |
| `analysis/CODING-DIFFICULTY-LEVERS.md` | **the coding lever doctrine**: what bit here per step and per model, the sibling lever lab, external evidence (W1-W4), interlocks, the admission test; catalogue in `pipeline/author/levers.py` |
| `ref_files/[Scaler__Arena] RL Environments Spec - Updated 9_21.docx` | Arena's requirements. Harbor 0.22 package, schema 1.4, `[[steps]]`, offline verifier, `harbor check` |
| `pipeline/policy.py` | every threshold. Prose never carries a number policy owns |

## The five phases

```
1 select    dump/index.py -> select/score.py -> select/priority.py -> select/classify.py
            -> select/categories.py -> select/shortlist.py
            keyed on SOURCE: recoverable non-vendored tree, dominant-language
            runtime class (native / browser / engine / none), test runners the
            agent ran, before/after pairs. Engine-bound seeds (Roblox, Unity,
            Flutter, MQL5, HDL) are killed as no_runtime; a seed with no source
            tree is no_source_fixture (authored fixtures off by default).
            The classifier reads the corrections and the code and survives a
            seed only with truth_source executable_tests, a coding_request,
            runtime_feasible, a named reproduction and a route: two usable
            corrections, one with a located root cause, or a repo_task.
2 author    author/agent_designer.py -- an agent with the whole trace, the
            working tree at every turn, a Docker sandbox over the fixture
            (sandbox_run / sandbox_grade / run_controls) and the draft
            protocol. It must REPRODUCE the defect before writing a test,
            prove every trap with a mutant. Submit candidates without quality
            rejection; controls are authoritative in validation. Aim for
            pass@1 <= policy.EXTREME_TARGET_PASS_AT_1 on each model.
3 grade     validity only. gates/code.py: bundle.structure, bundle.ledger,
            grader.runtime, grader.data, toolchain.pinned, hidden.disjoint,
            privacy.clean, leak.sweep, solution.derived, trap.faithful, trap.beatable,
            quality.controls, quality.spec, hacking.resistance,
            contract.published; then the semantic review (validity and
            novelty block; difficulty is advisory).
   -- each task is screened the moment it validates (policy.SCREEN_AS_BUILT;
      the batch barrier held validated tasks unmeasured for 6 h on 2026-10-03) --
4 calibrate screen: 5 episodes x 2 models per task through Harbor (Fable 5.1
            in Claude Code, GPT 6 Astra in Codex, high effort, no limits).
            THE PIPELINE STOPS HERE.
5 harden    MANUAL ONLY. Three attempts by default; explicit budget increases
            retain every spent attempt. Separate lineages may append checkpoints.
```

pass@1 < 0.5 on both models is the STRICT TARGET and never a blocker. Validity
(grader defects, leaks, voids, attribution, reward hacking) still blocks.
Every STEP is measured on its own: `policy.STEP_PASS_RATE_MAX` (0.5) per step on
both models; a lineage meets its target only when every step is under it.

Everything that takes time runs on Coder (user, 2026-10-03): `./arena coder
shard --workspaces a,b --ranks 1-100 -- build -o envs-x ...` runs the whole
pipeline (authoring, repairs, controls, review, calibration) as one detached
orchestrator per workspace; `./arena coder watch|pull|runs` follow it and bring
results back to the same absolute paths (`pipeline/benchmark/coder_run.py`).
`coder run <ws> --staged -- ...` runs this checkout's code from its own snapshot
(`ROOT/.staged/<sha>`) beside live runs that hold older code; pass task trees as
absolute paths. Never edit the files `artifacts.harness_identity` hashes
(transport, provider, config, grader runtimes) to fix a run: every recorded
semantic review goes stale.

## The bundle (see DESIGN.md §2)

```
environment/{Dockerfile,toolchain.json,deps/,repo/}      the agent image and fixture
tests/{Dockerfile,test.sh,grade.py,checks.json,rubric.json,suites/,deps/}
                                                          the verifier, built from tests/ alone
solution/{solve.sh,fix.patch}  steps/<s>/{instruction.md,solution/}
authoring/{spec.json,traps.json,mutants/*.patch,alt/<step>.patch,extraction/}
provenance/ledger.json         verification/{controls.json,probes.json,...}
```

Working names are `c-NNN-<slug>`; delivery renames to `a_f<level>_<slug>`.

## Executed controls (every one must hold during validation)

nop 0 (objectives fail, guardrails hold) · golden 1.0, three identical runs ·
alternative 1.0 (an independently written fix) · every mutant fails exactly
the checks its trap declares, passes at least one objective, never scores 1.0
· empty 0 · destructive 0 · test_tamper on the UNTOUCHED fixture stays 0 with
no objective passing · neutral_additions (user, 2026-10-21, external audit):
the reference plus a local conftest.py, one passing test of the agent's own,
NOTES.md and an empty __pycache__/ reads check for check as the reference on
every step, no guardrail failing (a tree-guard's published file budget is an
advisory). `./arena sandbox <bundle>` runs them without a model.
The designer's in-session `run_controls` is INCREMENTAL (2026-10-26): every
executed trial is recorded under the digest of its inputs
(`verification/control-cache/trials/`), and a re-run executes only the trials
an edit touched. Such a receipt is never whole-bundle evidence: the submit
preflight and validation execute the full matrix, and `binding_problems`
refuses an incremental receipt. Workers derive from the machine
(`controls.default_workers`, cap `policy.MAX_CONTROL_WORKERS`).

Multi-turn (2+ steps; Spec Appendix A, user 2026-10-12 "at least 3 turns"): prefix_only:<s> -- every
earlier turn's reference and nothing new passes no objective of s · skip_prior:<s> -- s's reference on the
untouched fixture without the earlier turns is not full credit. Each turn cites its user message
(`trace_turn`, checked against authoring/extraction/user-turns.json), carries >= MIN_OBJECTIVES_PER_STEP
objectives and MIN_TRAPS_PER_STEP traps, never reads /arena-history, and the package ships TURNS.md +
verification/turns.json (`pipeline/turns.py`: prompt, state in, expected outcome, rollouts per turn).

## Standing rules

- **Nothing ships unswept; what the sweep finds is fixed, then shipped**
  (user, 2026-10-07, after Arena's review of the delivered batch, C-001..C-044).
  `arena shipcheck <bundle> --image --repro` (pipeline/shipcheck.py) checks the
  classes that reached a delivered zip: stale `tests/grade.py`, a toolchain a
  login shell loses (Codex runs `/bin/sh -lc`; Debian `/etc/profile` resets
  PATH), declared or imported packages missing in the agent image, noisy
  interpreters, docs requiring a newer toolchain, advertised scripts reading
  unshipped paths, undeclared omissions, false guard refusals, guardrails most
  of one model's episodes fail, certificates not covering the shipped
  campaigns, coverage gaps, and that both images rebuild from the package alone
  (`--no-cache`) with the reference at 1.0 and the fixture at 0. Packaging
  (`waves/package_remote.py`) holds any task with an open finding. The same
  detectors run at build time (`orchestrate._ship_readiness`, before the
  semantic review) as repair feedback. Fixes: `arena shipfix` (deterministic
  edits, no model, no redesign) or a hidden-only `harden --fast` round;
  waivers are human only (`verification/shipcheck-waivers.json`).
- **Arena v0.1 is the delivery format; Arena's own checker is the gate** (user,
  2026-10-24). A released package is converted, recalibrated on Harbor 0.24.0
  (10+10, Oracle, nop; evidence is rerun, never relabelled) and zipped by
  `orchestration/arena_v01_ship.py convert|launch|status|finish`; `finish`
  runs lmarena/arena-rle-formatcheck-external at its pinned commit and a zip
  ships only with no FAIL, every hack flag reviewed and none uninspectable.
  Provenance cites the export archive by its supplier-free name (Q37), a hard
  link in `arena-v01-archives/`. Waivers are human only
  (`orchestration/arena_v01_waivers.json`) and the README states each one.
- **Hidden suites must enforce every published clause** (Arena C-006, C-013,
  C-024; the critic found executed 1.0 counter-implementations in all 31
  shipped tasks). `arena critic` (pipeline/coverage_critic.py) has a model
  write wrong implementations of published clauses on purpose; only one the
  shipped grader scores 1.0 is a gap. A gap is closed with a hidden case only
  when the determinacy probe settles it, else recorded under untested_surfaces.
  **Closed-and-verified** (user, 2026-10-09): a fresh white-box critic run finds
  ~5 new gaps almost every time, so a final no longer searches again when its
  lineage has a usable searching run: it re-executes that run's proposals on
  the new version (`policy.CRITIC_SHIP_RULE`, `coverage_critic.ship_critic`).
  Every former gap must score below 1.0; a former miss now at 1.0 is a
  regression and blocks too. **Only realistic gaps block** (user, same day,
  `policy.CRITIC_GAP_RULE`): both calibration families judge each gap; it is
  advisory -- listed under the delivered package's untested surfaces, never
  blocking -- when any family says the instruction does not clearly settle
  it or every family calls it contrived. A judge that errs demotes nothing.
  2026-10-21: a final whose instruction or suites changed since its source run
  also gets a fresh bounded search, and an unsettled point a hidden check DOES
  assert blocks with route instruction (`coverage.unsettled`,
  `CRITIC_GAP_RULE = "realistic_asserted"`); unasserted ones stay advisory.
- **Every gate tests the designer's reading; these test the reading itself**
  (user, 2026-10-21, after an external audit found 22 grading defects in 11
  shipped tasks that every gate above had passed). Release blocks unless:
  every decisive check (failed by >= `SIGNAL_HARD_FAIL_RATE` of a model's runs)
  is judged settled by both families with the deciding sentence quoted (a
  split is decided by `READING_ARBITER`)
  (`reading_settled`, verification/reading-settled.json) -- settled means the
  words decide the correct outcome in the tested case: a precision slip or an
  implementation shortcut is not a second reading, and an input form nothing
  public shows is settled only by words that say how to treat it (the first
  wording flagged traps the audit ruled fair); every failing run
  that asked about or chose on the decision it lost is judged settled, else
  `ambiguity_suspect` withholds the genuine certificate
  (verification/genuine-ambiguity.json); every promise the clause map flags
  (`pipeline/clause_map.py`: both families quote the instruction's promises in
  its own language, verification/clause-map.json -- an ADVISORY text judgement)
  is a first target of the coverage critic, whose executed 1.0 counter-
  implementation blocks; every calibration run a guardrail zeroed is
  adjudicated inside that guardrail's sentence (verification/guardrail-zeroed.json);
  the pass audit finds no passing calibration solution that demonstrably breaks a
  published clause (release step `pass-audit`); checks failing in lockstep count
  as one decision and no one decision carries more than
  `SIGNAL_SINGLE_DECISION_SHARE_MAX` of the lost reward. FAILURES.md names the
  assertion that actually failed. An erring judge never passes anything.
- **F0 ships** (user, 2026-10-05 and 2026-10-07). No pipeline or package text
  may say otherwise; `policy.SPEC_F0_ALLOWED` is True, folder `F0-inspired/`.
- **Use the pipeline and `keys.env` for task creation** (user, 2026-09-30).
  Author, validate, calibrate and deliver through the pipeline. Use the
  credentials provided in this workspace's `keys.env`; never substitute a
  local CLI login or Keychain credential. Anthropic authoring uses the
  configured `ANTHROPIC_AUTH_TOKEN`. Preserve drafts and spent budgets when
  authentication fails, and resume with the corrected token from the same file.
- **Verify by execution.** Claims about what an environment does or refuses are
  worth nothing without the command and its output. The sandbox exists so the
  designer, the gates and you can all run the shipped grader. After touching
  sandbox.py, controls.py, build.py or grade.py run BOTH `sandbox_smoke` and
  `sandbox_smoke --two-steps`: the single-step smoke hid a history-upload bug
  that cost the first real build its second step (2026-09-27).
- **Never claim a task is complete, flawless or fully hardened.** State what is
  verified and what is untested.
- **`SKIPPED` and `ERROR` are never green.**
- **`gated_out` is not `failed`.** Compute rates over reachable attempts only.
- **Every zero is classified** as model failure or void before it reaches a
  table.
- **Freeze fixture, instruction, tests and solution together.** The spec is
  the source of truth; `assurance.generated_problems` rebuilds the package
  from it and any byte that differs blocks.
- **Interlocks, both families, every step** (user, 2026-10-03). Single levers
  and noticing traps measured dead (sibling lab: 280/280 solved). Each graded
  decision interlocks two or more catalogued levers on one hidden item, every
  step aims at both Fable's misses (scope, long tail, one branch, interaction,
  state) and Astra's (unseen shape or identity, exact predicate, a convention),
  proved by an `m_baseline_` mutant and an `m_partial_` per lever, recorded in
  `quality.lever_plan`. Advisory everywhere; the screen measures.
- **Diverse failure modes, read from the grading surface** (user, 2026-10-27:
  "a lot of diverse trap modes that defeats both the models"; many traps per
  family are fine). The cricket build screened 0.0/0.0 while every lost point
  of both models came from ONE mode: four refusal checks quoting one sentence,
  typed absence/temporal/numeric. `author/failure_modes.py` therefore joins
  objective checks sharing a lever_plan entry, a quality.decisions entry or the
  same rule_quote clause (generic sentences ignored) into one mode, classed by
  its levers' catalogued family. The hardness gate (creation and hidden-only)
  asks for `policy.MIN_PLANTED_FAILURE_MODES` planted modes over
  `MIN_PLANTED_MODE_CLASSES` classes, `MIN_SHARED_FAILURE_MODES` modes
  defeating both families over `MIN_SHARED_MODE_CLASSES` classes, and
  `MIN_FAILURE_MODES_PER_FAMILY` per family; it waives with the gate. blind_solve,
  regrade_episodes, check_draft, the hardening brief and the screen report
  (`failure_modes`, loss concentration) show it. Labels alone never count.
  A hardening lineage meets its target only with those shared modes too
  (`policy.HARDENING_TARGET_NEEDS_DIVERSE_MODES`, `harden.lineage_target_met`);
  a target-met stop judged without them reopens (`harden.obsolete_target_stop`).
  A designer that resubmits one measured draft `HARDNESS_GATE_IDLE_SUBMITS`
  times in a row, held only by measured hardness gaps, concedes and the gate
  waives with that reason recorded (`agent_designer.idle_concession`).
- **Two origins of new trap modes** (user, 2026-10-28). A hardening round
  adds `policy.MIN_TRACE_TRAPS_HARDENING` NEW trace_derived traps refined from
  the session's own corrections, failures and retries, and
  `MIN_RESEARCHED_TRAPS_HARDENING` NEW researched traps (research_traps on the
  trace's domain, reproduced by sandbox_run); the traps the round started with
  never count (`trap_research.begin(baseline=...)`). Researched traps must drive
  `MIN_RESEARCHED_FAILURE_MODES` modes (fresh designs too). Every operator
  `--trap` idea is accounted for in `quality.requested_traps` (built with trap
  ids, or declined with a reason): a round once built four requested decisions
  in scratch and shipped none (`creation.requested_trap_gaps`).
- **Difficulty mechanisms are hypotheses until measured.** Coding opportunities
  include symptom versus cause, near-equivalent live code with different
  contracts, interacting exceptions, representation collisions, action history,
  cross-module invariants and architectural carry. Generate ordinary and
  divergence cases from published rules. Several checks can reveal one shared
  mistake; they do not establish independent failure probabilities. Decoy
  ablations and cited solver behavior distinguish causal evidence from labels.
- **The review is the last honest reader.** On the first real task it found two
  reference-correctness bugs, a fake mutant, an over-strict guardrail and a
  planted-module shortcut that 649 tests, the controls and 14 gates all passed.
  What it found generically is now enforced (tree-guard per step, mutants must
  change behaviour); what is task-specific goes back to the designer as a repair.
  A review discarded only for its OWN citations (a mis-typed quote, a passing
  objective quoting the hidden test) is continued, not re-bought: the same
  reviewer re-cites from its retained conversation (`review.recite_review`,
  `policy.REVIEW_RECITE_ATTEMPTS`, 2026-10-26) and only a continuation that
  still cites wrongly costs a fresh review.
  A finding that names one assertion is PATCHED, not redesigned: an operator stages
  the edit through the designer's own tools and re-runs the post-submit chain with
  `orchestration/patch_submit.py <bundle> <edits.json>` (spec stays the source of
  truth; full controls, gates, then the gate's review); a repair session gets
  `policy.REVISION_MAX_STEPS` (80) and a PATCH FIRST brief. The three shapes that
  cost the first bash RLE build six review rounds are `check_draft` advisories now
  (`validate._SUITE_SHAPES`): output bytes required to differ, a directory listing
  required to match an earlier one, a verdict gated on a regex of words, file
  timestamps as evidence (the build's own process can rewrite them).
- **One review round, proven patches** (user, 2026-10-27: "the reviewer should flag all the
  issues in a single round and the patch fixing tool should be able to fix it accurately").
  The reviewer gets `suite-inventory.json` (every function, module and script of the hidden
  suite, with the checks that reach it) and `reference/<step>/`; it must give every inventory
  entry a verdict (`helper_audit`; a gap is a receipt defect the re-cite repairs) and every
  finding an executable counterexample; a SWEEP continuation (`policy.REVIEW_SWEEP_PASSES`)
  looks only for what the first pass missed. The gate runs each counterexample through the
  shipped grader (`author/counterexamples.py`, `verification/review-counterexamples.json`)
  and attaches reproduced / not reproduced / not executable to its finding. The patch tool
  (`orchestration/patch_submit.py`) refuses a patch whose edits do not match once, that breaks
  a suite's syntax or adds an authoring advisory (plan); whose new probes do not reproduce on
  the current version (reproduce); or whose new and earlier probes do not all hold after it
  (confirm, `authoring/patches/probes.json`); then controls, gates and the review. Designer
  repairs have the same `run_probes` tool.
- **The author is strengthened, never blocked** (user, 2026-09-28). `check_draft` returns
  ADVISORIES for the shapes the reviewer rejects (a test using a name only the
  reference introduces, a tree-guard budget no sentence publishes, a test trusting a
  subprocess's stdout); `pre_review` runs the real reviewer over the staged draft
  (at most `config.MAX_PRE_REVIEWS_PER_SESSION`), and the submit preflight runs
  one itself, in a thread beside its full control run, when the submitted
  artifact has not been pre-reviewed and budget remains
  (`policy.SUBMIT_PREFLIGHT_REVIEW`, 2026-10-26; a revise verdict holds the
  submission once, like a control failure); a `shadow-guard` blocks only
  planted entries that shadow the modules the tests import; `blind_solve` has
  BOTH calibration families fix the task from the instruction and repository
  alone, at the same time in their own containers over one export
  (`model='astra'|'fable'` runs one), and grades each diff (a check it fails
  is a trap or an over-fit test). None of them refuses a draft; the gate's own
  review still decides, and a green preflight review is never reused as the gate's.
  Of the pre-review budget the last one is RESERVED for the submit preflight
  (`policy.SUBMIT_PREFLIGHT_RESERVED_REVIEWS`), so the gate reads an artifact
  reviewed exactly as submitted; when static problems kept it from running, the
  next complete submission runs it alone. A repair session calls one pre-review of
  its own (`policy.REVISION_PRE_REVIEWS`): it works from named findings, and each
  repair of the first bash RLE build spent all five. Long tools the designer asks for in ONE
  response run at the same time (`agent_designer.CONCURRENT_TOOLS`,
  `policy.DESIGNER_CONCURRENT_TOOLS`, 2026-10-27: controls, pre-reviews, blind
  solves and trap research were 60% of a 240-minute session, run in sequence).
  `submit_draft` saves incomplete candidates without validation or controls,
  then creation continues writing missing components within the existing budget.
  `creation_status` inventories fixture provenance, all step instructions, checks,
  suites, reference/alternative patches, traps, mutants and the self-audit. A
  complete component inventory is distinct from a valid or measured hard task.
  `verification/creation.json` and batch `creation_counts` report completion;
  exhausted or interrupted work remains explicitly unfinished.
  `difficulty_profile` and `probe_patch` are advisory too. Blind solving covers
  all steps in a persistent workspace; bounded cutoffs are not model failures.
  Scenario hardening may report F2 instead of F3 and has no arbitrary decoy
  count ceiling. Recompute provenance rather than inventing trace citations.
  `generate_cases`, `read_generated_cases`, `stage_research_variants` and
  `independent_oracle` are optional. The independent oracle sees public inputs
  and the toolchain before grading; its hashed provenance is checked during
  validation when supplied. Failed probes never prevent submission.
- **Freeze research before measuring.** `arena research prepare` retains even
  incomplete plans. `validate` checks variants and freezes protocol identities;
  `run` launches fresh native cohorts; `report` rechecks their evidence. Scored
  campaigns cannot be omitted or replaced. Confirmation has a fixed horizon
  and one registered plan per candidate/protocol. It supports a per-candidate
  claim, not a simultaneous guarantee over adaptive task-family searches.
- **The verifier isolates the hidden tests.** `/tests` is root-owned and read-only in
  the verifier image and every test process runs as the unprivileged `grader` user
  (`grade.py` drops privileges; the sandbox grants the verifier only CHOWN, SETUID,
  SETGID and DAC_READ_SEARCH). Code a hidden test imports cannot rewrite a later test.
- **Floors are floors.** The validator refuses fewer than 3 traps / 3 types / 2
  premise traps / 1 coupled trap and a difficulty case without the executed
  first-pass mutant (`policy.MIN_TRAPS_PER_TASK` ...). Clearing them proves
  nothing about difficulty; the screen does.
- **Instructions are the user's.** Their register, their words, vague about how
  and where, precise about what counts. Headings, bullet specs and assistant
  idioms are refused (`validate.voice_problems`); the reviewer rejects them.
- **A hard task is the goal, not a defect.** An all-fail check is admissible
  once an executed mutant fails it while the reference passes it, an
  independently written fix passes it, it was reached, and no near miss is
  unadjudicated (`policy.ALL_FAIL_PROOF_OBLIGATIONS`).
- **Graders are data, not code.** `tests/checks.json` names runner, selected
  tests, weight, band and the verbatim instruction sentence each check
  enforces; `tests/grade.py` is the one shipped runtime and is identical in
  every bundle. There is nowhere to type an expected value.
- **The verifier is built from `tests/` alone.** Hidden inputs live under
  `tests/suites/data/` and must not exist in the repository; tests import the
  repository through `ARENA_REPO`, never a hard-coded path.
- **Vendored material is never lifted.** `node_modules`, build output and
  virtualenvs are re-installed from pinned manifests at image build
  (`/opt/deps`, symlinked into the repo) and excluded from every fidelity and
  recoverability count.
- **Repair is bounded and monotone.** Two passes; the finding set must
  strictly shrink; a repeated finding is terminal.
- **Infra loss is re-run; results are not.**
- **A threshold with no reader is decoration.** `EveryPolicyConstantIsRead`
  fails the build on an orphaned `policy.py` constant.

## Gotchas

- A shell variant over `sandbox.SHELL_ARG_MAX` runs from a file in the container: as `docker exec sh -c` text, one
  argument past the kernel's 128 KiB MAX_ARG_STRLEN failed with E2BIG, so no counterexample of the 2026-10-30 bash
  test design ever ran. Locally on macOS, run `sandbox_smoke` / `./arena sandbox` with `TMPDIR` under `/Users`:
  Docker Desktop here does not share `/var/folders`, the verifier's bind mount silently lands inside the VM and
  every trial reads "verifier produced no reward" (the smoke failed this way before and after the change).
- **Designer cost controls (2026-10-26, ported from the large-dump checkout)**:
  `policy.DESIGNER_*` -- the designer's two cache breakpoints live 1 hour (the
  5-minute entry expired during every run_controls / pre_review / blind_solve
  pause: 48% of designer spend was re-writes), old thinking is cleared server-side
  (tool-result clearing is OFF: on the first bash RLE build each clear re-wrote
  100k-230k tokens into the 1-hour cache, $73 of a $91 session; the designer's own client-side
  shortening of old results fires only at `policy.DESIGNER_COMPACT_TOKENS`, newest first, never on
  evidence tools -- its old 1.5 MB byte trigger cut the first results at 180k tokens; beta `context-management-2025-06-27`, sent as a
  `betas` body key the transport lifts into the header), the evidence tools
  (`read_failures`, `read_trace`, the workspace and fixture readers) are never
  cleared, and `stage_spec` takes `edit={old,new}` for a staged string. The
  designer model is chosen per run with `ARENA_DESIGNER_MODEL`
  (claude-opus-5-5 for the 2026-10-26 measurement); config.py is untouched.
  transport.py is a harness-identity file: reviews recorded before this change
  report `harness_stable: false`, which is expected at this batch boundary.
- The grader's hidden-tree guard matches only ROOTED paths (`/tests/...`,
  `../tests`, `/./tests`); `pkg/tests/x.cjs` in a comment once zeroed two honest
  episodes (C-044). An agent-added `conftest.py` is deleted from every grading
  copy, never a zero: `--rootdir /tests/suites` cannot load it (C-015) --
  unless a shadow-guard lists `conftest` among its modules, which zeroed the
  reference of three shipped tasks; `grader.data` refuses it (2026-10-21).
- New toolchains set `login_shell_path` (agent_designer.stage_spec), which
  re-exports the image PATH in `/etc/profile.d`; without it Codex's `sh -lc`
  loses the venv, Go and Cargo. A preload `.pth` belongs in the venv only (C-033).
- The ledger declares extraction exclusions and host paths the fixture reads
  (`faithfulness.extraction_exclusions`, `host_paths`), so a faithful fixture
  naming an unshipped file is a stated property, not a broken one (C-028/C-030).
- `harden` baselines fall back per model to the version's screen when the
  qualify campaign for that model was held empty (10F+5A plan).
- The verifier installs `requirements.txt` and `requirements-test.txt` in ONE pip call;
  a repo-declared pin of a grader test package (`pytest==7.4.2` vs the grader's
  8.3.4) made c-043's verifier unbuildable. `build.verifier_requirements` leaves only
  that colliding pin out of the verifier copy (the deps-drift gate applies the same
  transform); the agent image keeps the declared version.
- `keys.env` is gitignored and lives per workspace. `./arena keys` writes it.
- Working names carry the seed's RANK (`c-007-slug`), and ranks move between
  classification runs. `_bundle_for` finds a seed's earlier bundle by its
  (session, seed) job record, so a re-ranked seed is never built twice.
- `arena deliver` copies every built non-F0 task by default and reports its
  acceptance; ship with `--accepted-only`.
- The dump path has no default. `./arena dump --set <path>`.
- Upstream PII redaction leaves placeholders (`[SECURITY_DATA]`,
  `[REDACTED_*]`, `[PATH_USERNAME_n]`) INSIDE source files. They are syntax
  errors the trace's own agent never saw; extraction counts them, validation
  refuses them in code files, and the designer must repair or route around
  them with authored bytes (declared, zero fidelity credit).
- Two trace paths can clean to one fixture path (`home/user/x` and `app/x`);
  extraction keeps both under their canonical paths and says so.
- The email redaction regex must not fire on decorators after an escaped
  newline (`\n@app.get`) or diff markers (`+@st.cache_data`).
- Harbor re-uploads collected artifacts to the verifier at their ORIGINAL
  paths (`/app/repo`, `/app/deliverables`); `grade.py` copies the tree fresh
  per check and relinks `/opt/deps`.
- Exit status is not evidence: `grade.py` counts the tests each runner reports
  (JUnit/JSON/TAP) and fails a check that completed none. The reviewer found the
  `os._exit(0)`-in-an-imported-module bypass on 2026-09-27; the `early_exit`
  control now executes it.
- `sh -c`, never `sh -lc`: a login shell resets PATH and hides the verifier
  venv. pytest runs as `python -I -m pytest --rootdir /tests/suites` with no
  PYTHONPATH; a `conftest.py` planted in the repo cannot reach it.
- Egress control needs `CONFIG_NFT_FIB_INET`, which Docker Desktop lacks;
  harness campaigns run on the Coder pool. `./arena sandbox` needs only Docker.
- `harbor check` is an LLM agent trial (~2.5 min, Docker + model auth), not a
  static lint.
