# The coding traces, measured

Written 2026-09-27 over the 500-session `kaizen_dump` (cohort v2) before any
coding pipeline code was written. Every number below was computed by a script
over `sessions/*/data.json`, the workspace snapshots and `outcomes.json`; the
per-session rows are in `pipeline-artifacts/analysis/` (gitignored, they carry
user text). Nothing here is an estimate from reading a sample.

## 1. What is in the dump

| | sessions |
|---|---:|
| traces in the dump | 500 |
| **coding** (a `code_*` category or the `code` slice) | **370** |
| of which `code_writing` | 259 |
| of which `code_debugging` | 194 |
| non-coding only | 130 |

The non-coding pipeline's classifier inspected 468 seeds and marked **303 as
`deliverable_kind = code`, every one rejected as `requires_execution`** -- the
whole coding half of the corpus was refused by a rule that predates the scope
change (memory: `coding-now-in-scope`). Only 150 distinct coding sessions were
ever inspected; **220 have never been classified at all.**

Sessions are long: median 16 user turns, 90 tool calls; mean 27 turns, 168
tool calls. The tool surface is uniform across models -- `bash` (461
sessions), `write_file` (437), `read_file` (345), `edit_file` (328),
`present_file` (253), plus `ask_user`, `web_search`, `fetch_page`,
`start_process`. Every assistant turn points at a workspace snapshot
(`workspaces/<id>.json`), which is what makes the fixture liftable.

## 2. The fixture is recoverable from bytes, not reconstructed

A workspace snapshot lists every file with `path`, `hash`, `size`,
`mediaType`, `status` and either `inlineText` or a `files/<hash>` blob.
Measured over the **final** snapshot of each coding session:

| file status in final snapshots | files |
|---|---:|
| ok, blob present | 16,531 |
| ok, inline text | 3,188 |
| redacted, blob present | 356 |
| `dropped:unscannable`, no blob | 8,993 |
| `dropped:unattributed` / `pii` / `error` | 245 |

The 8,993 unscannable files are **entirely vendored or generated**
(`node_modules/`, `.next/`, `dist/`, build output, binaries). Excluding those
directories and non-source extensions:

> **352 of 370 coding sessions have a fully recoverable source tree** (every
> non-vendored source file has bytes); 0 are partial; 18 have no source files
> at all. Median 10 source files / 163 KB; p90 1.5 MB.

This inverts the non-coding pipeline's situation, where recovery was partial
and the ledger routinely under-credited. A coding fixture at F3 is a byte copy
of a snapshot, and the ledger can credit it as `lifted` file by file. The one
cost: dependencies were never captured, so every environment must re-install
them from a lockfile the pipeline pins at build time.

Snapshots exist at **every assistant turn** (median 13, mean 21 per session),
so the state *before* a correction and the state the user *accepted* after it
are both addressable by message index. That pair -- defective state, accepted
state -- is what a coding task is built from.

## 3. Languages and runtimes

Sessions containing each language (by extension, final snapshot + tool paths):

| language | sessions | runtime in a container |
|---|---:|---|
| HTML | 198 | headless Chromium (Playwright) for behaviour; static parse for structure |
| Python | 149 | native |
| JavaScript | 120 | Node 22 |
| CSS | 63 | with HTML |
| shell | 40 | native |
| TypeScript | 33 | Node + tsc |
| SQL | 16 | sqlite / postgres service |
| Java | 13 | JDK + gradle/maven |
| Lua | 12 | 7 are Roblox/Garry's Mod (engine-bound); plain Lua is native |
| PHP | 8 | php-cli + built-in server |
| C / C++ | 11 | gcc |
| Kotlin, Dart, C#, GDScript, Rust, Go, Swift, R | 1-5 each | mostly engine-bound (Android, Flutter, Unity, Godot) |

Manifests seen: `package.json` 68, `requirements.txt` 39, `tsconfig.json` 32,
`docker-compose.yml` 18, `Dockerfile` 17, `build.gradle` 12, `pyproject.toml`
8, `Cargo.toml` 3, `go.mod` 1.

Runtime feasibility of the 150 classified coding sessions: **110 native**
(python/js/ts/java/php/c), **20 browser-only** (single-file HTML apps), 7
engine-bound Lua, 13 no runtime (Unity C#, Jenkins Groovy, FPGA HDL, MQL5).

**Tests were almost never part of the original work.** Only 65 sessions have a
file that looks like a test, and only **17 ever ran a test runner** (pytest 5,
jest 5, `npm test` 5, unittest 3, vitest 1). The agents ran their programs
(`python x.py` 138 sessions, `node x.js` 48, `npm run build` 22, `tsc` 11) and
declared success. Graders therefore cannot be lifted from the trace; they must
be authored, and the accepted state is the oracle they are derived from.

## 4. What the users corrected

Over the 303 classified code seeds, correction moments by class:

| class | moments | what it is |
|---|---:|---|
| `persistent_defect` | 274 | "still broken" -- the agent claimed a fix and the symptom persisted |
| `other` | 274 | new asks, follow-ups (not model errors) |
| `spec_correction` | 193 | the agent built the wrong thing; the user restates the requirement |
| `constraint_assertion` | 29 | "don't change X", "keep the file", "no new files" |
| `taste` | 24 | look and feel |
| `impatience` | 19 | |
| `infrastructure` | 18 | the user's machine differs (Windows paths, missing tools) |
| `revert_request` | 16 | "put it back the way it was" |
| `scope_correction` | 10 | "I only asked for X" |

Signals over the 370 coding sessions: `steerability` failure in 216 sessions
(743 moments), `task_outcome_explicit` failure in 89, `praise_complaint`
failure in 216, `tool_hallucination` 1, `bash_recovery_steps` never fails.

The classifier extracted **148 checkable rules** from 95 seeds. They are
executable assertions, e.g.:

- *"In the forest-environment code path, every reference to `dt` must occur
  inside a scope where `dt` is declared -- switching to the forest environment
  throws no ReferenceError."*
- *"No `next` entry in package.json or package-lock.json may resolve below
  14.2.35."*
- *"The 'View Menu' control must be a plain anchor to menu.php and must not
  use a header() redirect."*
- *"After the layout overhaul every UI element present in the previous
  index.html must still be present."*
- *"The project directory must contain no stray file whose name is a run of
  '=' characters; only the six intended files may exist."*

Every one of these is a hidden test, and the last three are **guardrails** --
negative obligations the trace states verbatim.

## 5. The shape of a persistent defect (four traces read in full)

`019ef635` (Python/Streamlit, 6 files): the user says *"ничего не меняй ... только
добавь веб интерфейс"*; the agent rewrites `core.py` and drops the Deepgram
timecode output; the user objects; the agent restores it but breaks the
import (`ImportError: parse_deepgram_result`); later the LLM-segment parser
silently returns nothing because the model's timestamps arrive as
`00:00:16,585` and the parser expects seconds. Three genuine defects, two of
them **restraint** failures (changing what was declared off-limits) and one a
**format premise** the agent never re-checked.

`019fb770` (single-file HTML booking dashboard): *"Il resto del file deve
restare assolutamente intatto"* -- the agent then breaks edit mode twice;
the calendar check-out boundary is fixed with a blunt `+1 day` that inverts
the error (suites now show occupied on the departure day). A classic
**symptom patch**: the visible repro passes, the mirror case fails.

`019eb783` (Python CLI, one file): five rounds of "the AI path fails" -- a
dead model id swallowed by a bare `except`, a fallback array of 4 where the
API allows 3, a 404 read as a bug. Each fix is real and each leaves the next
defect in place: **chained defects** where the agent verifies against a mock
that does not enforce the constraint.

`019e95ed` (PHP/MySQL, 45 files): "View Menu" is fixed three times by
changing link paths; the real cause is a `header()` redirect in `menu.php`
guarding on session state that the customer flow never sets. **Wrong fix
location** with a plausible decoy (the paths *were* also wrong).

None of these needs a GUI to grade. Each has a defective snapshot, an
accepted snapshot, and a behaviour that a hidden test can assert on inputs the
agent never saw.

## 6. What this decides about the pipeline

1. **Truth source is execution.** The grader is a hidden test suite run in a
   separate verifier container; the untouched fixture scores 0, the reference
   fix scores 1, and every expected value is *derived by running the reference
   at build time* with a receipt -- never typed in.
2. **The fixture is lifted.** `environment/repo/` is a byte copy of a snapshot
   at a chosen message boundary, minus vendored directories; dependencies are
   re-pinned. The ledger credits it file by file, so F3 is the default target
   for 352 sessions and reconstruction (F2) is the exception.
3. **Difficulty comes from the corrections, not from size.** The corpus'
   failure modes are symptom-vs-cause, chained defects, spec misreadings,
   restraint violations, revert residue and wrong fix location. Each becomes a
   trap type with a mutant (a plausible wrong fix) that must fail exactly the
   checks the trap drives. The non-coding finding that difficulty is *a
   premise that changes* transfers directly as a multi-step lever: step 2
   revises a contract that step-1 code already satisfies.
4. **Restraint is free difficulty here.** 29 explicit constraint assertions
   and every "keep the rest intact" instruction become guardrails: file hashes
   that must not change, event names, dependency sets, file counts.
5. **Selection must be code-aware.** Feasibility (native / browser /
   engine-bound / none), recoverable source bytes, correction classes and the
   before/after snapshot pair are the signals; `requires_execution` is a
   *reason to build*, not to reject. 220 coding sessions have never been
   inspected.
6. **Graders are still data.** `tests/checks.json` declares every check (id,
   kind, weight, band, runner, trap, rule quote); a generic shipped grader
   executes it and writes per-check flags into `reward.json`, so the screen,
   attribution, bands and batch report of the non-coding pipeline read the
   same shape.

## 7. Candidate ranking

`pipeline-artifacts/analysis/code_candidates_ranked.json` ranks the 150
classified coding sessions by usable corrections, checkable rules, runtime
feasibility and recoverable files. 74 sessions have ≥2 usable corrections; 53
of those run natively. The ranking is provisional: it uses the non-coding
classifier's view of a code trace, and a code-aware classification (phase 1
of the new pipeline) will re-inspect all 370.
