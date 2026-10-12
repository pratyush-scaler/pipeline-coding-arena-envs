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
no objective passing. `./arena sandbox <bundle>` runs them without a model.

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
- **The author is strengthened, never blocked** (user, 2026-09-28). `check_draft` returns
  ADVISORIES for the shapes the reviewer rejects (a test using a name only the
  reference introduces, a tree-guard budget no sentence publishes, a test trusting a
  subprocess's stdout); `pre_review` runs the real reviewer over the staged draft
  (at most `config.MAX_PRE_REVIEWS_PER_SESSION`); a `shadow-guard` blocks only
  planted entries that shadow the modules the tests import; `blind_solve` has a
  different vendor's model fix the task from the instruction and repository alone
  and grades its diff (a check it fails is a trap or an over-fit test). None of them
  refuses a draft; the gate's own review still decides.
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

- The grader's hidden-tree guard matches only ROOTED paths (`/tests/...`,
  `../tests`, `/./tests`); `pkg/tests/x.cjs` in a comment once zeroed two honest
  episodes (C-044). An agent-added `conftest.py` is deleted from every grading
  copy, never a zero: `--rootdir /tests/suites` cannot load it (C-015).
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
