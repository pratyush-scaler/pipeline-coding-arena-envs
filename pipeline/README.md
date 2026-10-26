# Arena coding-environment pipeline

Turns Arena agent traces into Harbor coding tasks: the agent starts in a
repository lifted from a real session, the verifier runs hidden tests we
author. [POLICY.md](POLICY.md) holds every acceptance threshold;
[../DESIGN.md](../DESIGN.md) holds the bundle contract;
[../analysis/CODING-TRACES.md](../analysis/CODING-TRACES.md) holds what the
corpus actually carries.

## Run it

```
./arena dump --set /path/to/dump      # once per workspace
./arena keys                          # writes keys.env (mode 600)
./arena select --need 6               # index -> screen -> tier -> classify -> categorise
./arena build -n 3                    # author, grade, screen; stops there
./arena sandbox envs/<bundle>         # the executed controls, no model needed
./arena sandbox envs/<bundle> --control-workers 2  # run independent variants in pairs
./arena gate envs/<bundle>            # the 15-gate chain
./arena screen envs --report-only     # reprint the screen table
./arena harden envs/<bundle> --apply  # manual; recorded lifetime budget, default 3
./arena deliver envs delivered/ --accepted-only   # F3-replay/ F2-scenario/ F1-pattern/;
                                      # without the flag every built non-F0 task is
                                      # copied and its acceptance state reported
```

### The bash RLE track (user, 2026-10-26)

Coding tasks rich in bash failure outcomes (Arena's `bash_reliability` signal), built by
the same pipeline with two additions:

```
./arena bash-rle-wave -o pipeline-artifacts/bash-rle-wave.json   # surviving seeds with >= 2 failed
                                      # shell decisions in their span, best first, track=bash_rle
./arena build -c pipeline-artifacts/bash-rle-wave.json -o envs-bash-rle --ranks 1,2
```

- `pipeline/select/bash_rle.py` profiles each surviving seed's failed shell calls and
  stamps the wave's rows `track: "bash_rle"` with a `track_priority` (orchestrate orders
  by it inside a tier).
- `pipeline/author/bash_rle.py` holds the track's mandate: what the first wave measured
  (three tasks at 0-1/10 pass@1 per model on Harbor 0.24), which the designer reads first
  on a track row. Its levers (`prior_run_state`, `label_not_data`, `interrupted_write`,
  ...) are catalogued in `levers.BASH_LEVERS` for every task.
- Every design, on either track, opens with the TRACE FAILURE DIGEST
  (`pipeline/dump/failures.py`): every failed tool call of the session, its class, Arena's
  verdict where it maps to the call, repeats, and what the agent and the user did next.
  `read_failures` returns each in full, and `read_trace` shows tool input and output up
  to 6000 characters.

### On Coder (everything that takes time; user, 2026-10-03)

```
./arena coder prepare <ws> [--create]          # start, bind-mount persistent storage,
                                               # Docker + runtimes, sync code, dump, peers
./arena coder shard --workspaces ws1,ws2 --ranks 1-100 \
      -- build -o envs-batch3 --task-workers 10 --screen-workers 10
                                               # one detached orchestrator per workspace,
                                               # disjoint --ranks of the same ranking
./arena coder run <ws> -- build --ranks 5,9 -o envs-batch3   # one workspace
./arena coder runs <ws>                        # alive / exit code / last log lines
./arena coder watch <ws> <run>                 # keep it up, pull results every 10 min
./arena coder pull <ws> <run>                  # bring envs*/ and pipeline-artifacts/ home
./arena coder kill <ws> <run>
```

The remote process is the unchanged pipeline. It authors, repairs, runs the
controls, the gates and the semantic review, and calibrates on the workspace's
own Docker. `ARENA_NATIVE_BACKEND=local` is set explicitly, so the egress
control works. Credentials come from this checkout's `keys.env` and live in RAM
on the workspace. The run continues if this machine sleeps. A stop wipes the
workspace's root disk, so every synced path is bind-mounted from
`$HOME/arena-mirror`, and `watch` extends the stop deadline. Code is never
synced under a live run. The calibration pool never pauses a workspace that has
an open remote run. Creating a workspace (`--create`) is a cost for the operator
to decide.

Docker powers optional authoring probes and required executed validation;
draft creation and submission require no Docker or validation. Harbor
(`.venv-harbor`, pinned 0.24.0 since 2026-10-22; 0.22.0 before) is used for `benchmark`,
`screen` and `qualify`; the Coder pool for egress-controlled campaigns.

The [extreme-task workflow](../analysis/HARDENING-WORKFLOW-2026-09-30.md)
documents deterministic case generators, independent oracle provenance, four-way
decoy/interaction experiments, hypothesis adoption, appended checkpoint lineages,
explicit search budgets and fresh confirmation. `arena research` separates
prepare, validate, run and report. Difficulty remains advisory; a model failure
claim requires verified native evidence.

## Where the content is

The designer can inspect the complete retained source inventory with
`list_evidence` and `read_evidence`, including raw `workspace_manifest` JSON,
all recovered file versions, uploads and archive members. Retained
`unmapped_blob` bodies are also readable and searchable, with their unverified
path/time association stated explicitly. Raw manifests and unanchored blobs
remain inspection-only and cannot be promoted into verified fixture provenance.
Text reads support pagination and in-body search; source bytes are hash-checked.

```
pipeline/
  dump/index.py          one row per seed: source tree, languages, runtime class,
                         test runners the agent ran, tree completeness, vendored share
  select/score.py        kill filters (no_runtime, no_source_fixture) and replay readiness
  select/priority.py     tiers 1-4 from attachments / corrections / signals
  select/classify.py     the LLM pass: correction classes, defect pairs, repo_task,
                         reproduction, truth source, runtime feasibility
  select/categories.py   target level F3/F2/F1/F0 (a target; delivery measures)
  select/shortlist.py    slots: persistent_defect / multi_step / restraint
  author/extract.py      the working tree at a message boundary -> environment/repo
  author/agent_designer.py  the designer: trace, workspace history, sandbox, draft protocol
  author/sandbox.py      Docker: agent + verifier images, variants, verify(), shell()
  author/controls.py     nop / golden x3 / alternative / mutants / empty / destructive / tamper
  author/probes.py       the receipt the release gate reads (seed_zero, golden, ...)
  author/checks.py       checks.json schema, runner types, weights, rubric, shipped documents
  author/build.py        spec -> bundle (task.toml, Dockerfiles, tests/, solution/, steps/)
  author/grader_runtime/grade.py   the ONE shipped grader (stdlib, offline)
  author/toolchain.py    digest-pinned bases, snapshot apt, harness layer, deps under /opt/deps
  author/validate.py     the static validator the designer and the gates share
  author/quality.py      bands, decisions, difficulty_case (misreading <-> trap <-> mutant)
  author/difficulty.py   causal hypotheses, public evidence and advisory profiles
  author/cases.py        deterministic intersections, state histories and checkpoints
  author/oracle.py       blind independent implementation and hashed provenance
  author/traps.py        trap types and their provenance rules
  research.py            retained factorial drafts, sealed campaigns and fresh confirmation
  gates/code.py          the 14 validity gates; gates/calibration.py the post-screen gates
  ledger/faithfulness.py lifted / anonymized / derived / authored, per file, by bytes
  attribution.py         episode verdicts, all-fail obligations, trajectory hack scan
  screen.py, release.py, harden.py, deliver.py, deliverable.py, accounting.py
```

## The funnel, measured (500-session dump, 2026-09-27)

```
2045 seeds indexed        runtime: native 854, browser 495, engine 223, none 473
 969 pass the hard floors  dropped: kill_filter 362, no_runtime 197, no_source_fixture 517
 885 replay-ready          tiers: T1 41 seeds / 32 sessions, T2 64, T3 190, T4 674
                           targets: F3 191, F2 727, F1 14, F0 37
```

The classifier runs top-down by tier and stops when `--need` seeds survive.

## What a task is

A repository in a defective or incomplete state, an instruction in the user's
own register, and hidden tests the untouched repository fails and the accepted
fix passes. The reference solution is a patch (`solution/fix.patch`) applied
by `solution/solve.sh`; an independently written alternative fix
(`authoring/alt/<step>.patch`) must also score 1.0; every trap is proved by a
mutant (`authoring/mutants/m_*.patch`) that fails exactly the checks the trap
drives and never scores 1.0.

The verifier image is built from `tests/` alone. Hidden inputs live under
`tests/suites/data/`; tests import the repository through `ARENA_REPO`.
`grade.py` copies the tree fresh per check, relinks `/opt/deps`, and runs
pytest as `python -I -m pytest --rootdir /tests/suites` with no PYTHONPATH, so
a `conftest.py` planted in the repository cannot reach it. Every test runner is
asked for a machine-readable result and the check passes only on counted,
completed tests: an `os._exit(0)` planted in a module the tests import ends the
process with status 0 and runs nothing, and that is a fail. The `early_exit`
control plants exactly that into every source file of the untouched fixture and
requires every objective to stay failed.

Verified by execution on the synthetic task (`pipeline/tests/synthetic_code.py`,
single-step and `two_steps=True` -- the second step's patches apply on top of
the first, earlier captures ride to `/arena-history`, budgets split per step):
`sandbox_smoke` (nop 0 / golden 1.0 / alt 1.0 / three mutants exact, a scope
guardrail zeroing the step) and `native_smoke` (oracle 1.0 / nop 0 through the
real Harbor harness with the separate verifier image).

## Difficulty

Authoring and hardening now aim for full-task pass@1 <= 0.05 on each calibration
model. Draft submission has no quality or difficulty blocker: candidates are
retained before validation, including incomplete ones. Controls and review
reject only in validation; optional creation probes remain advisory.

Creation continues writing missing components after an early submission, using
the original session budget. `creation_status` lists missing fixture provenance,
toolchain/dependency manifests, step instructions, checks/suites, per-step
reference and alternative patches, traps/mutants and self-audit entries. It checks
presence without running quality gates or Docker. `verification/creation.json`
records `complete` only when these authored components are present; this says
nothing about validity or measured difficulty. Budget exhaustion retains the
latest work as `unfinished`. Batch reports expose separate `creation_counts`.

`difficulty_profile` summarizes optional causal hypotheses and mutant diversity.
`probe_patch` executes an unregistered shortcut or alternative. `blind_solve`
solves all steps in one persistent workspace, grades incremental changes, and
marks bounded cutoffs as incomplete. Use interacting semantic decoys, exceptions,
identity collisions, state sequences, cross-module invariants and later demands
that invalidate an earlier architectural assumption. Add as many genuine decoys
as the scenario supports, with truthful provenance. Default scenario hardening
allows a measured F3-to-F2 transition rather than limiting additions to keep F3.

The screen reports the general target and extreme observed target separately,
with uncertainty. Five failed episodes cannot confirm a success rate below 5%.
Fresh fixed cohorts on the frozen final candidate are needed for confirmation;
no new model campaigns are launched automatically by these enhancements.
Research and source-code findings: [hardening audit](../analysis/HARDENING-AUDIT-2026-09-30.md).

**The lever doctrine** ([analysis/CODING-DIFFICULTY-LEVERS.md](../analysis/CODING-DIFFICULTY-LEVERS.md),
catalogue `author/levers.py`) supersedes the older list that stood here. The
levers it replaced are symptom vs cause, decoy location, chained defects, an
announced step-2 revision and restraint. Each of them measured 1.00/1.00 when it
was the whole puzzle.

What bit, per step and per model, is a plainly published rule whose realisation
goes wrong on a hidden input the visible material never showed:

- Fable over-reaches a closed rule's scope and misses rare rule interactions.
- Astra codes one exact predicate and misses a member in another shape.

Every graded decision is therefore an interlock: two or more levers on one hidden
item, aimed at both families, in every step. Each interlock is proved by an
`m_baseline_` mutant and one `m_partial_` mutant per lever, and recorded in
`quality.lever_plan`. `check_draft` audits the plan, and the designer's opening
lists the levers the trace's own code supports. The screen reports step-wise
pass@1, and hardening holds every step to `policy.STEP_PASS_RATE_MAX`.

One genuine objective failure already defeats full success; additional checks
should diagnose distinct consequences. The designer must record one
misreading per trap in `quality.difficulty_case`, each encoded by an executed
mutant, and name `first_try_mutant`: the patch a strong model ships on its
first pass, which the controls execute and must score below 1.0. The validator
refuses fewer than 3 traps, fewer than 3 trap types, fewer than 2 premise traps
or no trap that couples two objectives (`pipeline/policy.py`).

## Instructions in the user's voice

The instruction is what the trace's user would have typed: their register and
their sentences (from `list_corrections`), vague about how and where -- no file
to change, no method, no checklist of cases -- and precise about what counts:
every graded rule is one plain sentence (`rule_quote`). `validate.voice_problems`
refuses headings, bullet-pointed requirements, tables and assistant idioms; the
semantic reviewer rejects an instruction that reads like a specification.

Completed controls retain full trial records in `verification/control-cache/`
outside the designer's pruned scratch exports. Final export can reuse them when
only the published task name, `provenance/ledger.json`, the authored-fixture
declaration or known author-session bookkeeping changes. All fixture,
instruction, test, patch, trap and runtime bytes, checkpoint order, determinism
count and pipeline policy must match. Reuse re-evaluates the actual per-check
records and records both the executed artifact hash and the current export hash
in `verification/controls.json`; it executes zero new trials. Incomplete, failed,
stale or damaged evidence triggers fresh execution. Legacy receipts can certify
their exact original artifact but cannot be rebound from summaries. Final
provenance checks, generated-byte assurance, gates and semantic review still run.
Python callers can pass `reuse=False` to intentionally execute fresh controls.

## Reward hacking

The controls prove the grader cannot be moved from the agent side
(`test_tamper` on the untouched fixture stays 0); the trajectory scan
(`attribution.trajectory_hacks`) reads every retained episode for test access,
graded writes, symlinks, harness tampering and undeclared output. The agent's
working directory is `/app/repo`, so a relative `test.sh` is the repository's
own file; `/tests`, `/app/tests` and `/logs/verifier` are the harness.

## Known limits

- Only native (python, node, go, rust, php, c, shell) and browser (Chromium via
  Playwright) runtimes ship. Engine-bound languages are killed at selection.
- Browser grading is slower and flakier than native; `replay_readiness` scores
  it below native for that reason.
- The classifier's `repo_task` route admits a seed nobody complained about; it
  needs two verified evidence reads and an executed reproduction, but it is the
  route most likely to produce a task the trace never contained.
- No task is hard until the screen says so, and the screen never blocks.
- The end-to-end designer run on a real trace is the surface least exercised:
  its plumbing (extraction, sandbox, draft protocol) ran on session 019ef635,
  but no model-authored bundle has yet cleared the gate chain.
