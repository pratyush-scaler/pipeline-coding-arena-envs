# Setup: from a fresh clone to shipped tasks

This repository builds Arena coding RL environments from agent traces, hardens them until both calibration
models (Fable 5.1 in Claude Code, GPT 6 Astra in Codex) fail them, releases them and zips them.
`DESIGN.md` is the bundle contract and `CLAUDE.md` holds the working rules; this file is only the setup.

One command runs the whole thing:

```
./start --dump /absolute/path/to/trace-dump --workspaces ws1,ws2,ws3 --batch first --seeds 40
```

Re-running `./start` (no flags) resumes: it re-checks, launches whatever is not launched yet, and keeps the
loop running. Zips land in `ship-zips/`.

## What you need first

| | |
|---|---|
| `uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (`./start` builds `.venv` with it) |
| API keys | an Anthropic credential and an OpenAI key, in `keys.env`: run `./arena keys` |
| Coder | the `coder` CLI, logged in (`coder login <your deployment url>`), and workspaces built from your template. Every heavy step runs there: authoring sandboxes, both models' episodes, release. Each workspace needs Docker; sizes the pipeline expects are in `workspace.example.toml` (32 CPU, 120 GiB RAM, 190 GiB disk) |
| `workspace.toml` | created by `./start` from `workspace.example.toml` on the first run: fill in `[coder] url` and `template` with your deployment's values |
| A trace dump | a folder with `sessions/` (from Arena). The same absolute path is used on every workspace; `./start` copies each batch's sessions there |

Nothing in the repository holds a credential: `keys.env`, `workspace.toml` and `orchestration/config.json`
are per person and gitignored.

## The steps `./start` runs

1. **Settings.** Flags are saved to `orchestration/config.json`; later runs reuse them.
2. **Prerequisites.** keys, the dump, `workspace.toml`'s `[coder]` section, the `coder` CLI, and each
   workspace (a stopped one is started). `./start --check` stops here.
3. **Seeds.** `arena select` indexes the dump, scores and classifies sessions (model calls; cached in
   `pipeline-artifacts/classify-cache/`) until `--seeds` survive. The survivors are frozen as the batch's
   wave, `pipeline-artifacts/waves/envs-<batch>.json`; a later batch never reuses an earlier batch's seeds.
4. **Workspaces.** Each workspace is bootstrapped once (`orchestration/prepare.py`): this checkout and the
   dump at the same absolute paths, Docker, the Harbor runtime, the authoring venv, and a disk janitor.
5. **Builds.** The wave's ranks are dealt round-robin to the workspaces and launched as staged builds
   (`orchestration/launch.py`). Each build designs its tasks, validates them, and screens each one on both
   models (5 + 5 episodes).
6. **The loop.** `orchestration/supervise.sh` keeps `orchestration/driver.py` running (restarting it if it
   goes silent) and `orchestration/keepalive.sh` keeps the workspaces up. Every 10 minutes the driver:
   - releases a version that is hard on both models (`shipfix --release`: re-validation, 10 + 5 qualification
     episodes, failure certification, hardness, the packaging sweep);
   - sends a version release stopped to one **release repair** round carrying every recorded blocker
     (ship-critic gaps with their edits, reviewer findings, scoring problems, failed attempts);
   - **triages reward-hacking flags**: exact false-positive rules, then both model families; genuine flags
     become repair blockers, unclear ones wait for you;
   - hardens a version both models solve (hidden-only rounds, then a full redesign);
   - packages released versions into `ship-zips/` and reproduces each zip on stock Harbor.

## Following and controlling it

```
./start --status            shipped zips, the last cycle's summary, whether the loop runs
tail -f logs/driver.log     every decision the driver makes
./arena coder runs          what is running on each workspace
./start --stop              stop the local loop (work already on Coder finishes; ./start resumes)
./start --batch next --seeds 40     a second batch on the same workspaces
```

The driver reports, never acts on, what only a person decides: a hack flag both judges could not settle, a
lineage whose hardening budget is spent, a hold that needs a human ruling. Those appear as `blocked` lines in
`logs/driver.log`.

## What to expect

From one 71-seed batch (2026-10-17/18): about 56 tasks screened, 32 reached pass@1 <= 0.5 on both models,
and shipping a hard task took 3-14 hours (median about 8) from its first screen, mostly in release-repair laps
of 3-5 hours each. Model time dominates; more workspaces and higher provider rate limits are what speed it up.

## When something goes wrong

- **Coder ssh is slow and occasionally hangs.** Every transfer is bounded and retried; the supervisor restarts
  a driver that goes silent for 25 minutes.
- **A step reports FAIL.** Fix what it names and run `./start` again: completed steps are skipped.
- **A truncated dump file on a workspace** (a copy cut off mid-transfer) fails a task's release preflight
  with a JSON decode error. `python orchestration/launch.py envs-<batch> --verify-dump` compares every
  launched session's files by size and re-copies the ones that differ; the driver re-runs a release whose
  step crashed once on its own.
- **Tests.** `.venv/bin/python -m pytest pipeline/tests` runs the suite. Tests that need a dump or keys fail
  until both are configured.
