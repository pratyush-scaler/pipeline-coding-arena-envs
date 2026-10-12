# The coding pipeline -- design

Written 2026-09-27, before the code. This is the target the build is held to;
`pipeline/POLICY.md` carries the thresholds once they exist as constants, and
where the two disagree POLICY.md wins.

The non-coding pipeline (`../arena-envs-tasks-large-dump`) is the parent. It
is **forked, not imported**: this workspace copies the modules it keeps and
changes nothing in the parent. The parent's five phases, its validity
discipline and its calibration machinery are kept whole; what changes is what
a fixture is, what a grader is, and what difficulty looks like when the
deliverable is code.

## 1. What a coding task is

A coding task is a **repository in a defective or incomplete state**, an
instruction written the way the user wrote it, and a **hidden test suite**
that the untouched repository fails and the accepted fix passes. The agent
works in a container with the toolchain pre-installed and no network; the
verifier runs afterwards in a separate container, copies the hidden tests in,
executes them against the agent's working tree, and writes one reward with a
flag per check.

Three facts from the corpus analysis (`analysis/CODING-TRACES.md`) decide the
shape:

- **352 of 370 coding sessions have a fully recoverable source tree** at every
  assistant turn. The fixture is *lifted* -- a byte copy of a snapshot at a
  chosen message boundary -- and the ledger credits it file by file. F3 is the
  default target, not the exception.
- **Only 17 sessions ever ran a test runner.** Graders are authored, never
  lifted. The accepted snapshot is the oracle the tests are derived from.
- **The corrections are executable.** `persistent_defect` (274),
  `spec_correction` (193), `constraint_assertion` (29), `revert_request` (16)
  and `scope_correction` (10) each map to a trap type with a hidden test and a
  plausible wrong fix that fails it.

## 2. The bundle contract

The working tree is filed by target level, as in the parent:
`<tree>/target-F3-replay/c-001-<slug>/`. The prefix is `c-` so a coding bundle
can never collide with an `a-` bundle in a delivery.

```
task.toml                        Harbor 1.4; [[steps]]; [metadata] source_trace,
                                 target_level, source_tier, language, runtime,
                                 pipeline = "arena-coding"
instruction.md | steps/NN-<name>/instruction.md
environment/
  Dockerfile                     FROM a digest-pinned toolchain image; installs
                                 pinned dependencies at BUILD time (lockfile);
                                 COPY repo/ /app; never tests/, never solution/
  repo/                          THE FIXTURE: the repository state the agent
                                 starts from. Lifted bytes, minus vendored dirs
  toolchain.json                 {language, runtime, base_image@digest, install,
                                 build, run, test_runner, headless}
tests/
  test.sh                        verifier entry: installs test deps (pinned),
                                 runs grade.py, writes /logs/verifier/reward.json
  grade.py                       the generic grader runtime, byte-identical
                                 across every bundle (hash pinned by the gate)
  checks.json                    THE GRADER AS DATA (see 3)
  suites/                        the hidden tests: test_*.py, *.test.js,
                                 spec/, playwright/, plus data/ hidden inputs
  key/expected.json              expected values DERIVED by running the reference
                                 in the verifier image at build time, with a
                                 receipt {solution_sha256, fixture_sha256, image,
                                 run_id}. There is nowhere to type a value
  rubric.json                    human-readable: check -> what it measures,
                                 points, the instruction sentence it enforces
solution/
  solve.sh                       the reference: applies fix.patch / runs
                                 scripted edits, then builds. Derives the
                                 result by changing source, so Harbor's
                                 hardcoded_solution criterion passes
  fix.patch | fix/               the reference change and its provenance
  alt/                           an independently written second correct fix
                                 (different structure); must also score 1.0
  README.md                      trace-derived (accepted state at msg N) or
                                 authored, and why
authoring/
  spec.json                      the designer's emission
  traps.json                     traps with provenance and the mutant each one
                                 is proved by
  mutants/<id>.patch             plausible wrong fixes (counterexamples)
  extraction/                    moments.json, snapshots.json (before/after
                                 message ids + hashes), diffs/
  difficulty_case.json           the self-audit
provenance/
  ledger.json                    per file: lifted | anonymized | derived |
                                 authored, byte credit, level F0-F3
verification/                    receipts, outside artifact identity (parent
                                 layout: probes.json, controls.json,
                                 semantic-review.json, checkpoint.json,
                                 native-calibrations.json, screen*.json ...)
```

`artifacts.ROOTS` (what the artifact hash covers) becomes `task.toml, steps/,
environment/, tests/, solution/, provenance/, authoring/spec.json,
authoring/traps.json, authoring/mutants/, .dockerignore`.

## 3. The grader: still data

`tests/checks.json` declares every check; `tests/grade.py` executes it. A
check is:

```json
{"id": "c_stall_hidden_roster", "kind": "objective", "weight": 14,
 "band": "core", "step": "01-fix",
 "runner": {"type": "pytest", "select": "suites/test_engine.py::test_bracket_advances",
            "timeout_s": 60},
 "trap": "t_symptom_patch", "rule_quote": ["the tournament just stops ..."],
 "requires": []}
```

Runner types: `pytest` (node ids), `unittest`, `node-test` / `jest` / `vitest`
(test name pattern), `junit` (class#method), `cargo-test`, `go-test`,
`playwright` (a spec file run headless), `command` (an executable whose exit
code is the verdict, for shells, PHP, C), and `hash-guard` (a path whose bytes
must equal the fixture's -- the restraint guardrail that needs no test).

Two kinds, as in the parent:

| kind | untouched fixture | after the reference | violation |
|---|---|---|---|
| **objective** | fails | passes | loses its weight |
| **guardrail** | passes | passes | the step scores 0 |

Reward per step = sum of passed objective weights / 100 if every guardrail
holds, else 0. Objective weights sum to `WEIGHT_TOTAL = 100` over the whole
task; at least `MIN_OBJECTIVES = 8` objectives. `reward.json` carries
`reward`, `check_<id>: 0|1`, `passed_checks`, `total_checks` per step -- the
same shape the parent's screen, attribution, bands and batch report read.

**Expected values are derived, never stored.** Any literal an assertion
compares against must either be a property (no exception, invariant holds,
file unchanged) or come from `tests/key/expected.json`, which the build
produces by running the reference solution in the verifier image over the
hidden inputs and freezes with a receipt. The `key.derived` gate re-derives it
and fails on any difference; changing the fixture or the solution invalidates
the key. Hidden inputs (`tests/suites/data/`) must be absent from
`environment/repo/` (`hidden.disjoint` gate).

## 4. Difficulty: the corpus' failure modes as traps

| trap | corpus class | catches | proved by |
|---|---|---|---|
| `symptom_patch` | persistent_defect | fixing the visible repro while the root cause remains | a mutant that passes the visible case and fails the hidden mirror case |
| `chained_defect` | persistent_defect | defect B is only reachable after A is fixed | a mutant fixing A alone |
| `spec_misread` | spec_correction | a plausible narrower/wider reading of a stated requirement | a mutant implementing the misreading; hidden cases sit on the divergence |
| `constraint` | constraint_assertion | "keep the file", "don't rename events", "no new deps" | `hash-guard` / structural guardrails; a mutant that fixes by violating them |
| `revert_residue` | revert_request | something reverted still leaves residue that must go | a mutant that removes the headline and leaves the residue |
| `scope_creep_bait` | scope_correction | the adjacent broken thing must be left alone or reported, not fixed | a guardrail on the adjacent file; a mutant that "helpfully" fixes it |
| `regression_bait` | -- | the obvious fix for A breaks B | a regression guardrail; a mutant with the obvious fix |
| `decoy_location` | -- | the tempting wrong place to fix (the caller, not the callee) | a mutant patching the decoy |
| `stale_authority` | -- | README/comments/docstring state a contract the tests contradict; the instruction says which governs | a mutant following the stale document |
| `premise_revision` | (parent's seventh lever) | step 2 revises a threshold/contract that step-1 code already satisfies; earlier work must be re-derived | step-2 checks routed through the revision; a mutant that keeps the step-1 value |
| `unsatisfiable` | -- | part of the request cannot be met offline; the answer reports it in a status file and completes the rest | a mutant that fabricates |
| `environment` | infrastructure | the fixture assumes a toolchain that is not the container's | a mutant that hard-codes the wrong path |

Provenance rules are the parent's: a trap is `trace_derived` (cites the moment
and quotes the user) or `authored` (names the fixture surface and why a
practitioner would meet it). Floors are the parent's per target level.

**Every trap is proved by an executed mutant.** A mutant is a patch in
`authoring/mutants/`; the controls apply it to the fixture in the sandbox and
require that it fails exactly the checks the trap drives, passes at least one
objective, and loses at most `MISREADING_MAX_OBJECTIVE_SHARE` (50%) of the
objectives. A trap whose mutant does not exist, or whose mutant fails
everything, is dormant and blocks the build.

Difficulty is authored on the first pass and measured once by the screen. The
designer is told what the parent's calibration data says: size does not bite;
a plausible wrong premise that agrees with the right answer almost everywhere
does; route several checks through one premise; keep the population small.

## 5. Validity controls, all executed

Every control runs the exported grader in the verifier image against a
prepared working tree and writes `verification/controls.json`.

| control | prepared tree | required result |
|---|---|---|
| `nop` | the fixture untouched | every objective 0, every guardrail 1, reward 0 |
| `golden` | the reference applied | reward 1.0 |
| `determinism` | golden, 3 runs | identical per-check flags each run |
| `alt_solution` | `solution/alt/` applied | reward 1.0 (proves tests grade behaviour, not text) |
| `mutant:<id>` | each authored mutant | fails its trap's checks and nothing else; ≥1 objective passes; ≤50% lost |
| `empty` | `/app` emptied | guardrails fail, reward 0 |
| `destructive` | tests and features deleted | regression guardrails fail, reward 0 |
| `test_tamper` | repo tests edited, conftest/jest config set to skip, `reward.json` pre-written, exit codes forced | reward equals `nop`: the verifier ignores agent-side test state |
| `forged_output` | expected output files written without the fix | fails the checks that compute |
| `resource` | golden with `/usr/bin/time -v` | verifier timeout ≥ 3x measured, memory ≥ 2x |

Reward-hacking probes (`verification/probes.json`) are `nop`, `golden`,
`empty`, `destructive`, `test_tamper`, `forged_output`, `answer_lookup` (a
scan of `environment/` for the fix: `.orig` files, a `.git` history holding
the post-fix state, TODO comments naming the fix, the patch text itself) and
`surface_leverage` (which repo files move a scored check). The parent's
`MANDATORY_PROBES`, `HACK_PROBES`, `EXPECT_FULL_CREDIT` sets are re-keyed.

All-fail proof obligations (a check no episode passed) become:
`mutant_counterfactual` (a plausible wrong fix fails it and the reference
passes), `alternative_implementation` (the alt solution passes it),
`reachable` (attempted in ≥1 episode), `near_miss_adjudicated` (a submission
whose diff overlaps the reference ≥80% and still scores 0 needs a verdict).

## 6. The phases

```
1 select    code-aware: the index (index/6) carries the recoverable source
            tree (vendored excluded), languages by bytes, runtime class,
            test runners the agent ran, tree completeness at the seed
            boundary; kill filters no_runtime (engine-bound) and
            no_source_fixture; the classifier survives a seed only with
            truth_source executable_tests, coding_request, runtime_feasible,
            a named reproduction, and a route (two usable corrections, one
            with a located root cause, or a repo_task). Non-survivals carry
            structural_assertion / requires_human_judgement / none.
            Target levels F3/F2/F1/F0 on the parent's rule
2 author    the coding designer: full evidence tools + workspace tools
            (workspace_at, read_workspace_file, diff_workspace,
            materialize_fixture) + a SANDBOX (build the environment image,
            run commands, run the grader over golden / nop / a mutant / alt).
            It must reproduce the defect on the fixture and its absence on
            the reference before it may submit. Emits spec.json; the build
            exports the Harbor package
3 grade     gates (structure, ledger, key.derived, hidden.disjoint,
            weight-budget, objective-count, trap.faithful, trap.beatable,
            hacking.resistance, leak.sweep, isolation: Dockerfile copies no
            tests/solution, pinned deps, grader.runtime hash), controls,
            probes, harbor check, semantic review (code-aware prompt)
   -- barrier --
4 calibrate the parent's screen: 5 x 2, Fable 5.1 in Claude Code / GPT 6
            Astra in Codex, high effort, no limits, Coder pool, void top-up
5 harden    manual, 3 attempts per lineage, the parent's rules
```

The reward-hacking scan over trajectories adds coding patterns: editing or
deleting repository tests, adding `conftest.py`/jest config that skips or
mocks, monkeypatching the runner, writing under `/logs/verifier`, catching all
exceptions to pass, special-casing the visible repro's inputs, `git` history
tricks, symlinks into `/tests`, and network probes.

## 7. Faithfulness

`environment/repo/` files are credited by the parent's ledger: `lifted` if
byte-identical to a snapshot file (the citation is the snapshot id and path),
`anonymized` under a verified transformation, `derived` when edited (half),
`authored` (none). F3 needs ≥90% lifted-or-anonymized bytes and ≥70%
trace-derived traps; F2 ≥20% or ≥30%; F1 synthetic with the failure mode
reproduced; F0 rejected at delivery.

The fixture may be taken at any assistant turn. The natural choice is the
snapshot *before the first correction the task is built on*; the accepted
snapshot after the last correction is the reference. When the accepted state
is itself defective (common: the user gave up), the reference is authored and
the ledger says so.

## 8. Toolchains

`environment/toolchain.json` selects a digest-pinned base image and the
install/build/test commands. Supported at launch: Python 3.12 (pip lockfile
via `pip-compile` at build), Node 22 (npm lockfile, `npm ci`), TypeScript,
Java 21 (Gradle/Maven, offline cache baked), PHP 8 (php-cli, built-in
server), C/C++ (gcc, CMake), Rust (cargo, vendored), Go (modules vendored),
shell, SQL (sqlite3), and **browser** (single-file HTML apps graded through
Playwright + Chromium in the verifier image only). Engine-bound code
(Roblox/GMod Lua, Unity C#, Android, Flutter, MQL5, HDL) is `no_runtime`
unless the task can be cut to a pure-logic module.

Dependencies are installed at image build, when network exists, from a
lockfile the pipeline pins; the agent phase has network only to its own model
API host, the verifier none. Calibration CLIs are baked in as in the parent.

## 9. What is kept from the parent, what is replaced

Kept whole (copied, package renamed where paths change): `policy.py`
skeleton and every calibration/screen/attribution/top-up/hardening/delivery
constant; `phases.py`; `cli.py` shape; `orchestrate.py` skeleton (budget,
locks, checkpoint, repair loop, batch record); `checkpoint.py`; `repair.py`;
`review.py` mechanism; `privacy.py`; `provider.py`, `credential.py`,
`config.py` (re-keyed); `artifacts.py`; `benchmark/` (harbor, coder,
offline_docker, tasks_check, step_history, support); `calibrate/`;
`screen.py`; `measurement.py`; `metrics.py`; `release.py`; `harden.py`
mechanism; `deliver.py`; `deliverable.py`; `accounting.py`; `ledger/`
mechanism; `naming.py`; `dump/` whole (index re-keyed).

Replaced: `select/` (index fields, score, classify prompt and schema,
shortlist slots); `author/` (designer, tools, spec schema, build/export,
checks, validate, probes, controls, solution, traps); `gates/` (the
checks.json-bound gates); `evaluate.py`; `assurance.py`, `row_controls.py`,
`source_controls.py` (replaced by mutants + alt solutions); the review
prompt; the hardening brief; attribution's reward-hacking scan and proof
obligations.

Removed: document parsers, calendar anonymization, scenario authoring,
breadth screens, `check_difficulty.py`.

## 10. What this design does not claim

- It cannot prove a hidden test suite is *complete*. `behavior_in_tests` and
  `behavior_in_task_description` (Harbor's rubric) are checked by a model and
  by the semantic reviewer; both are advisory on coverage.
- Browser grading through Playwright measures DOM state and behaviour, not
  appearance. Taste corrections stay `taste` and are never graded.
- A mutant proves a trap can bite; it does not prove a frontier model bites.
  The screen measures that, once, for the batch.
- Flakiness is bounded by three golden runs, not proved absent.
