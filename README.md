# pipeline-coding-arena-envs

A pipeline that turns real Arena agent sessions into hard coding RL environments.

Each environment drops an agent into a repository lifted from a real session, together with the user's request in
the user's own words. The agent has to fix, extend or finish that code. Afterwards an offline verifier runs **hidden
tests** against what the agent left behind. The only source of truth is an executed test: expected values are never
stored, every answer is derived by running code, and every trap in a task is proven with a mutant that falls for it.

Tasks ship as Harbor packages (schema 1.4, `[[steps]]`, offline
verifier) and are calibrated on two models, Claude Fable 5.1 (in Claude Code) and GPT 6 Astra (in Codex). A task
ships only when **pass@1 is at most 0.5 on every step, on both models**. Every counted failure must be certified as
the model's own, the grader must survive reward-hacking controls, and the zip must reproduce on stock Harbor.

## Quick start

```
./start --dump /absolute/path/to/trace-dump --workspaces ws1,ws2,ws3 --batch first --seeds 40
```

That one command:

1. installs the Python environment;
2. checks prerequisites;
3. selects and freezes the batch's seeds from the dump;
4. bootstraps the Coder workspaces;
5. launches the builds;
6. starts the loop that takes each task from build to a shipped zip in `ship-zips/`.

Every step can be repeated safely, so running `./start` again resumes.

```
./start --check      prerequisites only: nothing launched, no model calls
./start --status     shipped zips, the last cycle's summary, whether the loop runs
./start --stop       stop the local loop (runs already on Coder finish on their own)
tail -f logs/driver.log
```

You need:
- `uv`;
- an Anthropic credential and an OpenAI key in `keys.env` (run `./arena keys`);
- the `coder` CLI, logged in to your deployment, with workspaces that have Docker;
- your deployment's `[coder]` URL and template in `workspace.toml`;
- a trace dump: a folder with `sessions/`.

[SETUP.md](SETUP.md) walks through each of these and what to expect.

## How a task is made

```
1 select     index the dump, score and classify sessions; keep the ones with a recoverable source tree,
             a runnable toolchain, a real coding request and a reproducible defect
2 author     a designer agent with the whole trace, the working tree at every turn and a Docker sandbox
             reproduces the defect, writes the instruction, the hidden suites, a reference fix, an
             independent alternative fix, and traps each proven by a mutant
3 grade      validity gates (structure, ledger, grader runtime, pinned toolchain, hidden/visible
             disjointness, privacy, leak sweep, derived solution, trap faithfulness, controls,
             hacking resistance) and an independent semantic review
4 calibrate  5 + 5 screening episodes per task on both models through Harbor
5 harden     rounds that keep every spent attempt: hidden-only rounds first, then a full redesign
```

Once a task is hard on both models, release re-validates it, runs 10 + 5 qualification episodes, certifies every
failure, sweeps the package and zips it. Every zip is then reproduced on stock Harbor: the reference solution scores
1.0 and the untouched fixture scores 0.

The loop (`orchestration/driver.py`) also does the following:
- sends a version that release stopped to a **release repair** round, carrying every recorded blocker;
- **triages reward-hacking flags**: exact false-positive rules first, then judges from both model families. Genuine
  flags become repair blockers; unclear ones wait for a person.

The executed controls each task must hold:

| control | must score |
|---|---|
| untouched fixture (nop) | 0, with guardrails holding |
| reference fix | 1.0 on three identical runs |
| independently written alternative fix | 1.0 |
| every mutant | fails exactly the checks its trap declares, and never scores 1.0 |
| empty, destructive and test-tampering submissions | 0 |

## Repository layout

```
start                     the one-command entry point (bash -> orchestration/start.py)
arena                     the pipeline CLI: ./arena --help
orchestration/            the batch loop: settings, workspace bootstrap, launch, driver, supervisor, packaging
pipeline/                 the pipeline itself
  select/ dump/           seed selection from the trace dump
  author/                 the designer agent, sandbox, controls, traps, validation, the shipped grade.py
  gates/                  the validity gate chain
  benchmark/ calibrate/   Harbor campaigns, Coder transport, the calibration agents
  harden.py release.py    hardening rounds and the release pipeline
  hack_triage.py          automatic reward-hacking flag triage
  policy.py               every threshold (POLICY.md explains them)
  tests/                  the test suite
rulesets/                 anti-reward-hacking rules and the difficulty playbook
analysis/                 corpus analysis, difficulty levers, web research behind the trap catalogue
DESIGN.md                 the bundle contract: layout, grader as data, controls, traps, faithfulness
CLAUDE.md                 working rules and hard-won gotchas
SETUP.md                  from a fresh clone to shipped tasks
VERIFICATION.md           how claims about an environment are verified
```

A delivered task bundle:

```
environment/{Dockerfile,toolchain.json,deps/,repo/}     the agent image and the fixture
tests/{Dockerfile,test.sh,grade.py,checks.json,rubric.json,suites/}
                                                         the verifier, built from tests/ alone
steps/<step>/instruction.md   solution/   provenance/ledger.json   verification/   runs/
```

## Working by hand

Every phase is also a CLI command. The full list is in [pipeline/README.md](pipeline/README.md):

```
./arena select --need 6                 index -> score -> classify -> categorise
./arena build -n 3                      author, grade, screen; stops there
./arena sandbox <bundle>                run the executed controls, no model needed
./arena gate <bundle>                   the gate chain
./arena screen <bundle>                 5 + 5 calibration episodes
./arena harden <bundle> --apply         one hardening round
./arena shipfix <bundle> --release      re-validate, qualify, certify, sweep
./arena shipcheck <bundle> --image --repro
./arena coder run <ws> --staged -- ...  run any of these on a Coder workspace
```

## Credentials and local state

Nothing in the repository holds a credential. The following are per person and gitignored:
- `keys.env`, written by `./arena keys` with mode 600;
- `workspace.toml`, your Coder deployment;
- `orchestration/config.json`, written by `./start`.

Generated output also stays out of git: task trees (`envs-*`), `pipeline-artifacts/`, `ship-zips/` and `logs/`.

## Tests

```
.venv/bin/python -m pytest pipeline/tests
```

Tests that need a trace dump or keys fail until both are configured.
