# Verification, 2026-10-03 -- lever doctrine, per-step bar, provider/submit preflights, Coder runs

Every claim names the command and what it printed.

- `.venv/bin/python -m unittest discover -s pipeline/tests -t . -p 'test_*.py'`. Result: 1,023 tests,
  with 3 failures. All 3 failures need Docker Desktop, which is down on this Mac.
  - `test_authoring_progress`, `test_bounded_build_resume` and `test_fix_orchestration` end as
    `unverified` because sandbox availability is not mocked.
  - With only `sandbox.available` patched, all three pass (3 tests OK).
- **New tests:**
  - `test_lever_doctrine` (17)
  - `test_coder_run` (13)
  - `test_provider_fatal` (6)
  - `SubmitPreflight` in `test_creation_completion` (3)
- **First Coder run of the authoring controls (arena-calib-1):** every variant was invalid. The
  verifier failed with `/tests/test.sh: cannot create /logs/verifier/arena-grade.json: Permission denied`.
  - Cause: on a Linux host the bind mount keeps the workspace user as owner, and the verifier's root
    has no DAC_OVERRIDE.
  - Fix in `sandbox.verify`: the log directory is made writable before the run, and its contents are
    chowned back to the host user afterwards.
- **After the fix, on arena-calib-1 (`./arena coder run arena-calib-1 -- pipeline/tests/sandbox_smoke.py [--two-steps]`):**
  - Single step: m_symptom_only 0.52, m_month_sort 0.63, m_scope_creep 0.0 -> PASS.
  - Two steps: m_symptom_only 0.26, m_month_sort 0.32, m_scope_creep 0.0 -> PASS.
- `./arena coder run arena-calib-1 -- sandbox envs/target-F3-replay/c-010-python-text-parsing`
  (the real two-step task) exited 0. Every control was ok: report_forge 0.0, benign_test 1.0,
  alternative 1.0, and per_trap, answer_lookup and isolation all ok.
- **Coder transport, verified on arena-calib-1:**
  - start
  - bind-mounted persistence: the checkout and dump are mountpoints from `/home/coder`, a separate
    196 GB volume
  - rsync over `coder ssh` (openrsync client, GNU server)
  - detached launch
  - `runs`
  - `pull` (7 files written, 0 refused)
- **Untested:** a full remote `build` (authoring with model calls) and a remote calibration screen.
  The first is the coding-v2 launch.

# End-to-end verification, 2026-09-27

Every claim below names the command and what it printed. Nothing here is
inferred from reading code. "Untested" is stated where it applies.

## Phase 1 — select (real dump, 500 sessions)

```
./arena select --need 3 -j 4
  index      2045 seeds   runtime: native 854, browser 495, engine 223, none 473
  floors      969 past    dropped: kill_filter 362, no_runtime 197, no_source_fixture 517
  tiers       T1 41 (32 sessions) / T2 64 / T3 190 / T4 674
  targets     F3 191 / F2 727 / F1 14 / F0 37
  classifier  20 tier-1 verdicts cached before the run was stopped to free the API:
              14 survive; rejections are evidence-backed (e.g. an Elementor CSS session:
              runtime_feasible=false, 20 evidence items read, root causes located per correction)
```

The batch composer picked `019e95ed/2` (PHP restaurant app, persistent_defect slot) from the
provisional set; `./arena build` then ranked by (tier, dispute) and built `019f23e1/6`
(`c-001-arabic-assistant-memory`, a Python assistant whose planner mis-routed "المغرب" (Morocco) to
prayer times). Both selections are recorded in `pipeline-artifacts/`.

## Phase 2 — author

Synthetic task (`pipeline/tests/synthetic_code.py`, one-step and two-step):

```
python -m pipeline.tests.sandbox_smoke [--two-steps]     (Docker)
  nop 0.0 (guardrails hold, objectives fail) · golden 1.0 · alternative 1.0
  m_symptom_only 0.52 / m_month_sort 0.63 / m_scope_creep 0.0 (guardrail zeroes the step)
  two-step: golden 1.0 and alt 1.0 across both steps; m_step2_skip_refunds 0.69,
  m_step2_unordered 0.81, each failing exactly its declared checks           -> PASS
```

Real task: the designer (claude-opus-5, xhigh) ran 92 responses in its first session — read the
corrections and 9 trace windows, listed the fixture, staged toolchain + pinned deps, ran 31 sandbox
commands to reproduce the defect and develop the golden/alternative patches, re-materialized the fixture
at turn 44 excluding files that leaked the fix, and submitted a draft with 15 checks (12 objectives),
8 traps of 6 types, 8 mutants, 6 hidden-test files, instruction in the user's Arabic.
Its executed controls passed (`verification/controls.json`: ok). See "Real build" below.

## Phase 3 — grade

```
./arena sandbox <bundle>     controls: ok (27–91 s)   probes: ok
  seed_zero 0.0 · golden 1.0 · empty_deliverable 0.0 · destructive 0.0 · test_tamper 0.0 ·
  early_exit 0.0 (every source file exits 0 and forges every reachable report; nothing counts) ·
  per_trap ok · answer_lookup ok · isolation ok · alternative 1.0
./arena gate <bundle>        14/14 PASS -> GREEN   (one-step and two-step synthetic)
python -m pipeline.tests.review_smoke   (gpt-6-astra reviews a task whose hidden test contradicts
  its instruction)  -> passed: true, verdict revise, the contradicting check flagged with the right
  reason, evidence citations valid
```

The review smoke found and we closed: the grader trusted exit status (an `os._exit(0)` in an
imported module passed everything); then a report file whose path agent code could reach
(now: stdin-fed bootstrap, per-run nonce, selection cross-check, forging `early_exit` control,
planted-marker trajectory scan); the reviewer's `solution/` citations were rejected by the
validator; three synthetic-task weaknesses (memo-keyed refunds, an unpublished amount format, an
over-strict empty-input guard).

## Phase 4 — calibrate

```
./arena screen <synthetic bundle> --include-unvalidated     (Coder pool arena-calib-1)
  5 episodes x 2 models: claude-fable-5-1 in claude-code 2.1.283, gpt-6-astra in codex 0.157.1,
  effort high, no limits. Both p@1 1.00 (the smoke task is easy by design), all episodes valid,
  attribution clean, outlook "weak-candidate", ready for 10x2: yes
python -m pipeline.tests.native_smoke   oracle 1.0 / nop 0.0, one-step and two-step, through
  the real Harbor harness and separate verifier image (/arena-history upload verified)
```

Local Docker Desktop cannot run a harness episode: Harbor's egress control is not active, the
run is recorded as VOID ("an allowlist needs Docker egress control, which is not active"), never as
a measurement. Calibration runs on the Coder pool, as documented.

## Phase 5 — harden / deliver

Not exercised on a real task this session (needs a calibrated real bundle). Delivery layout,
naming, F0 hold and acceptance integrity are covered by the ported unit tests
(`test_fix_delivery*.py`, `DeliveryLayout`).

## Unit suite

`python -m unittest discover -s pipeline/tests -t .` — all green after every change
(34 new `test_coding_core.py` tests: weights, structural guards, authenticated reports,
early-exit and forged-report rejection, naming, trap floors, human voice, two-step build,
readable-fix threshold, planted-marker scan). `EveryPolicyConstantIsRead` passes.

## Defects found by running the pipeline, all fixed and pinned by a test

1. Dockerfile `ENV` order depended on JSON key order (toolchain.pinned gate).
2. Grader trusted exit status 0 (reviewer).
3. Grader trusted a report file reachable by agent code (reviewer, second pass).
4. Review validator rejected `solution/` and `authoring/` citations.
5. `sandbox.verify` deleted a step's capture before later steps uploaded it to `/arena-history`
   — every second step failed; the first real build lost its second step to it.
6. `answer_lookup` called one shared loop header "the reference readable in the fixture".
7. Redaction verification had been stubbed out (`_keys` raised), `audit()` looked for
   `environment/files`, the email regex missed addresses after a JSON `\n`, extraction silently
   overwrote colliding paths, `category_batch.py` imported the deleted designer.

## Real build — outcome

`./arena build -n 1 --no-calibrate` on `019f23e1/6` -> `envs/target-F3-replay/c-001-arabic-assistant-memory`
(the "Si Fahmi" Arabic assistant: its deterministic `RequestPlanner` routed any message naming Morocco
(المغرب) or dinner (العشاء) to prayer intents; 8 files / 129 KB lifted verbatim, fidelity 1.0).

- Designer session 1 (92 responses, 31 sandbox runs): reproduced the defect, materialized the fixture at
  turn 44 excluding files that leaked the fix, submitted 15 checks (12 objectives over 6 hidden pytest
  files), 8 traps of 6 types, 8 mutants, reference 79-line diff, independent alternative. It had drafted a
  second step and dropped it when `sandbox_grade` failed on the history upload (defect 5 above; fixed).
- Controls (run by the build): golden 1.0 x3 identical, alternative 1.0, nop/empty/destructive/tamper 0,
  mutants 0.55 / 0.83 / 0.81 / 0.90 / 0.91 and three guardrail-tripping 0.0, each failing exactly its
  declared checks.
- The build ended BLOCKED on `toolchain.pinned` because its process still held the pre-fix toolchain
  module, plus a false-positive "reference line readable" (defect 6). Re-exported from the spec in a fresh
  process: `./arena gate` 13/14 PASS; the two remaining blockers were the rules added mid-run at the
  user's request (a 9-bullet spec-style instruction; no `first_try_mutant`).
- Repair round (checkpoint stage `repair`, one round of budget): the designer rewrote the instruction as
  4,005 characters of Arabic prose in the user's register, set `first_try_mutant = m_guard_only`.
  Its gate then reported `export-drift: authoring/traps.json` -- a pipeline defect (the orchestrator
  patched `usable_corrections` into the exported file after the build; now written into the spec before
  export). Re-exported: validator clean, regeneration clean.
- Final: `./arena sandbox` controls ok (63 s, incl. `early_exit` against a real task), probes ok,
  `./arena gate` **GREEN 14/14**. Measured level F2 (2 of 8 traps trace-derived; the F3 floor is 70%).
- Semantic review (gpt-6-astra, after an OpenAI rate-limit retry): verdict **revise**. Substantive:
  two reference-correctness defects (the folding repair leaves a direct token comparison in
  `_is_family_update` unnormalized; `منذ` is read as the interrogative `من` by prefix), a fake mutant
  (`m_scope` is an inert comment, not the caller-side workaround its trap claims), an over-strict
  guardrail (`g_public_api` demands a receiver literally named `self`), and one shortcut: the authored
  test loader imports bare `planner` with the repository root first on `sys.path`, so a planted
  `/app/repo/planner.py` would be graded. The task is held; its repair budget is spent.
  Pipeline consequences: every step now needs a tree-guard guardrail (`checks.check_problems`), and the
  reviewer's completeness rule no longer demands naming the source file under repair.
- Coder screen 5x2 (`./arena screen ... --label real-task-first-screen`): claude-fable-5-1 pass@1 1.00 /
  pass@4 1.00 / mean 1.00 (5 valid, 0 void, attribution: 5 pass); gpt-6-astra the same. Target NOT met;
  outlook `poor-candidate`: every one of the 12 objectives passed in every episode. The measurement is
  clean and the pipeline reports it as what it is -- an easy task -- rather than shipping it as hard.
- `./arena harden <bundle>` (report): 12 free checks = 100% of the score; at level F2 with 1 usable
  correction the faithfulness budget allows 0 authored traps ("make the trace-derived traps harder or
  cite real corrections"); 3 of 3 attempts open.
- `./arena harden ... --apply`: attempt 1 (61 designer responses) was REFUSED by the no-weakening rules --
  it removed three measured checks and changed the untargeted `g_bundle_untouched` guardrail. Attempt 2
  (repair, 35 responses) removed two other measured checks and changed the same guardrail: a repeated
  finding, so the loop STOPPED with the revision kept at `hardened/v2` for review and the original
  untouched; one attempt remains in the lineage. Nothing was screened, because nothing valid was produced.
  Read: with `g_bundle_untouched` hash-guarding every other file and the F2 budget allowing no authored
  trap, this seed (one usable correction) has no room to get harder within the faithfulness rules; the
  pipeline said so in the report before spending the attempts, and refused to weaken the task to fake it.

### What the easy result says (not a validity finding)

The instruction, even in the user's voice, states every distinction the tests grade (Morocco vs the
Maghrib prayer, dinner vs Isha, noun-form events, question particles, `event_action` values); once the
rules are published as criteria, both frontier models implement them in one pass. The designer's
`first_try_verdict` -- that a strong model would ship the guard-only patch -- was falsified 10/10. The
floors (traps, types, premise traps, executed first-pass mutant) were all cleared and did not produce
difficulty, exactly as the parent programme measured: difficulty is a premise that changes, and this
seed carries one usable correction and no second step to revise a premise in. Selection should weight
seeds with several distinct corrections and a late requirement change above single-defect seeds.

# Second pass, 2026-09-28

Credential: the coding workspace's `keys.env` held an expired OAuth token (HTTP 401); the fresh
`ANTHROPIC_AUTH_TOKEN` was copied from the parent workspace (mode 600 preserved, no value printed) ->
`anthropic OK 200`. Coder needs no separate push: `sync_code` syncs code only and every campaign
forwards the `*_AUTH_TOKEN` / `*_API_KEY` variables from the environment at run time.

Running: `./arena select --need 60 -j 6` (tier 1 fully classified, 41 seeds; tier 2 in progress, about
85 verdicts/hour) and `./arena build -n 3 -o envs --task-workers 3` (the pilot: c-001-turbomachinery-cad-tool
[F2], c-002-single-file-react-app [F3], c-003-pyqt6-desktop-app [F3]; all three designers past 30 responses
with a draft staged). Results are appended below when they land.

## Defects found by running the pipeline today, fixed and pinned by a test

8. **An upstream clone counted as fixture.** c-003's fixture was 128 files / 6,107,704 B, of which
   `whisper_cpp_repo/` (104 files, 6,030,076 B, 98.7% of bytes) is a partial clone of
   `https://github.com/ggerganov/whisper.cpp.git` the session made at message 1 -- credited as lifted
   fidelity for code the user never wrote, and unable to build. `extract.upstream_clones(trace, ws)` reads the
   trace's own `git clone <remote>` calls (shlex-tokenised; local paths ignored) and excludes a clone the
   session never wrote into; a clone the session worked inside stays (`Outlook_AnyFinder`: 395 edit/write
   revisions under the clone, nothing outside it -- the user's own repository). Measured over the 75 tier-1/2
   sessions: 5 carried a remote clone, 2 are upstream (whisper.cpp; `doc-approval-demo`, read only), 3 are
   the user's working repository. Re-extraction of c-003's seed under the fix: 24 files / 77,628 B, the
   clone recorded under `skipped.upstream_clones` with URL, message, files and bytes; the designer opening
   names it and forbids lifting it. `pipeline/tests/test_upstream_clones.py`.
9. **A re-ranked seed would be built twice.** Working names carry the seed's rank in the surviving-candidate
   list, and that list is re-ordered by every classification run and by the hardness prior, so a seed built
   as `c-003-x` and ranked 7th next time would have been built again as `c-007-x` beside its own bundle.
   `_bundle_for` now resolves a seed's earlier bundle by the (session, seed) pair in its job record and keeps
   that bundle's name; two bundles for one seed refuse a third. Pinned by `build_one` returning the retained
   result under the old name without calling the designer.
10. **No prompt caching.** The first real task's designer sent 171 requests carrying 30,648,088 input tokens,
    0 from cache; a pilot turn today carried 217,337 input tokens, 0 cached. Ported the parent's two-marker
    scheme into `author/prompt_cache.py`, used by the designer and by the classifier's evidence loop (27 turns
    on a typical seed); the stored `request-config.json`, `conversation.json` and classifier cache stay
    unmarked. Live check through the OAuth transport: request 1 `cache_creation_input_tokens 1741`,
    request 2 `cache_read_input_tokens 1741`. Classifier verdicts now record `usage`. `test_prompt_caching.py`.
11. `arena deliver` copies every built non-F0 task by default and reports its acceptance state (a dry run
    filed the held c-001-arabic task as F2). That default is the parent's deliberate batch-report shape and
    is kept; shipment is `--accepted-only`, now written in the README and CLAUDE.md.
12. **A later step could lean on the earlier prompt.** Every calibration step runs in a fresh session (Codex
    never sees step 1's instruction), and nothing refused a step-2 instruction saying "same rules as before"
    or "keep everything from step 1". `validate.history_reference_problems` (the coding counterpart of the
    parent's T5 rule, ported after a peer session landed it there) refuses those phrases in any step after
    the first; references to the repository ("the export you added in reports/export.py") stay allowed,
    because the code travels and the prompt does not. `test_step_self_contained.py` (5 tests, including the
    rule running inside `validate` over the two-step synthetic task).
13. **Classifier budget losses.** `--need 60` classified 105 seeds: 73 survived (tier 1: 31/41, tier 2: 42/64)
    and 7 ended as `Evidence classification budget exhausted; no verdict cached` at the hard-coded 32 turns
    -- three of them from one rich session (019ed766). A budget loss is not a verdict, so the budget is now
    `config.CLASSIFIER_MAX_TURNS = 64`. Re-run (`98 from cache, 0 errors`): all seven classified, five of
    them needing 36-47 turns; **79 survivors** (tier 1: 32/41, tier 2: 47/64). The seven new verdicts carry
    the classifier's first usage records and show the caching working in production: a 47-turn verdict
    read 3,981,334 tokens from cache against 16,205 uncached input tokens and 140,430 cache writes
    (about 0.4% of prefix tokens billed at the input rate).

14. **Re-lifted and authored fixture bytes escaped the privacy pass.** c-002's designer re-lifted the tree
    at message 33 (`materialize_fixture`), which skipped the redaction `minimize` the orchestrator applies to
    the initial extraction; an email address in `uploads/index.html` came back, nothing checked the fixture
    again until the repair session's opening scan, and that scan refused the very session that could fix
    it (`RuntimeError: design failed 1 times ... redact flagged source material`). Now `materialize_fixture`
    minimises and scans, `write_fixture_file` scans authored bytes (`privacy.scan_text`), `check_spec`
    refuses a draft while findings remain, the gate chain carries `privacy.clean` (15 gates), and a repair
    or revision session receives the findings as its first instruction instead of dying on them (a fresh
    design over a dirty fixture is still refused). `test_fixture_privacy.py`.
15. **The review's frozen copy failed its own provenance check.** c-003's repair session was the first to
    author fixture files; `freeze` copies `artifacts.ROOTS`, which left out
    `authoring/extraction/fixture-authored.json`, so the ledger rebuilt from the copy re-classified those
    files and the second review died with "source provenance is unverified" while the live bundle passed
    (`_provenance_problems`: live `[]`, frozen copy the error, frozen copy plus the file `[]`). The
    declaration is now a release surface. Pinned by a freeze-and-check test on a synthetic bundle.
16. **...which made the export-drift check charge a repair.** With the declaration in `ROOTS`, the
    regeneration in `assurance.generated_problems` reported it as "present in the package but not
    generated by the spec" and c-001-turbomachinery lost a repair round to it inside the relaunch (17:11-17:19).
    The build was stopped, the regeneration now carries the declaration, the charged pass is voided in
    `repair-log.json` (kept under `voided`), the read-only session it opened is set aside under
    `verification/interrupted-sessions/` with a note, and the checkpoint is back at `checking` on the
    first session's submission (`checkpoint.resumable` -> checking, 2 repairs). Test added.

Unit suite after all of the above: `Ran 723 tests in 22.5s  OK` at 17:20 (verdict line read, not the wrapper's status). Relaunch of ranks 1, 5, 6 at 17:21.

17. **A coding review could not be verified when it quoted the instruction.** c-001-turbomachinery's
    fresh review (17:29, harness stable) ended `unverified`: two passing objectives cited the instruction
    sentence (`steps/01-camber-and-angles/instruction.md`) and the hidden test, and `review_problems`
    accepted only a `fixture/` quote as "a valid source quotation" -- the document pipeline's rule, where
    the source is the fixture documents. For code the published contract is the instruction, so a passing
    objective may now quote `fixture/` or `steps/`; the hidden test alone is still not evidence
    (negative test). Under the new rule the 17:29 receipt is a plain "revise" and opens a repair round.
    Stopped the build at 17:5x (c-002's in-flight `run_controls` response set aside, boundary re-recorded
    at 51 responses), fixed, `Ran 725 tests  OK`, relaunched.

## Third relaunch (17:5x-), what the pipeline did on its own

- c-001-turbomachinery: the retained 17:29 review now reads as a plain "revise" and opened a repair round
  on the reviewer's 20 findings (pass 1 charged; the false pass stays voided); the harness was stable.
- c-006 (PyQt6 rebuild, clone excluded): first draft in 69 responses; controls ok; gates passed; the
  semantic review landed in five minutes with 23 findings and its repair round opened (1 left). Two of
  those findings are pipeline gaps, queued for after the run: (i) the novelty check offered the seed's own
  retired attempt (`envs-retired/c-003-...-attempt1`) as a competing peer and the reviewer called the task
  a "semantic duplicate" of it -- a rebuilt seed must declare the retired attempt as its predecessor
  (`provenance/replacement.json`, the lineage mechanism that already exists) and the orchestrator should
  write that automatically; (ii) the privacy scanner has no kind for a Windows account name, a device
  identifier (`LAPTOP-...`) or a `C:\Users\<name>` path, so a PowerShell transcript under `uploads/`
  reached the reviewer, who caught it. Known gap; the reviewer is the backstop until a typed transform
  exists.
- c-002: review "revise" with 18 findings, voided by one `checks.json` quote whose indentation differs
  from the file (content identical once whitespace runs collapse). Fix queued: `_quoted` accepts a
  whitespace-collapsed match (still exact on every non-space character), then c-002 resumes into its last
  repair.

## Pilot result (three seeds, 2026-09-28, final)

```
3 tasks in 6108.7s (third relaunch)   status: blocked 2, unverified 1   calibration screen: 0 tasks
  BLOK c-001-turbomachinery-cad-tool  3 sessions, 191 responses, 2 repairs spent; final review (19:34,
       harness stable) still requested revision, naming a hidden requirement (index-by-index outline
       equality where the instruction asks for identical camber)
  BLOK c-006-pyqt6-desktop-app        2 sessions, 151 responses; second review (19:37) repeated a round-1
       finding (o_clear_error), so the monotone repair rule ended it with one repair unspent
  UNVR c-002-single-file-react-app    review "revise" (18 findings) voided by an indentation-only quote;
       resumed at 20:0x under the corrected rule into its last repair
```

Yield on the first pass: 0 of 3. Every designer draft was rejected by the independent reviewer for the
same shapes -- tests that encode the reference implementation, unpublished requirements in guards, probes
that trust a child's stdout, inputs the test never creates, doubles that only the reference satisfies. The
report's `c-005-single-file-react-app` line names the rank-5 slug for the bundle on disk called c-002 (fixed
below).

## Fixes after the pilot (all pinned by a test; `Ran 733 tests in 23.8s  OK`)

18. `_quoted` accepts a whitespace-collapsed exact match (indentation is formatting, not content); reordered,
    dropped or changed characters still fail. `test_review_quotes.py`.
19. `_build_one` reports the bundle's name on disk, so the batch screen finds a re-ranked seed's task.
20. `_declare_lineage`: a seed rebuilt after a retired attempt declares that attempt in
    `provenance/replacement.json` (the existing lineage mechanism), so the novelty check presents it as a
    predecessor, not a competing peer.
21. `REVIEWER_OBJECTIONS`: the seven recurring rejection shapes, verbatim, read into both the authoring and
    the revision mandate. Validity guidance, not a difficulty lever; whether it raises first-pass validity is
    measured by the next seeds, not assumed.

Known gaps left open: the privacy scanner has no kind for Windows account names, device identifiers or
`C:\Users\<name>` paths (the reviewer caught one).

## Strengthening the author (user, 2026-09-28 evening: "strengthen the authoring phase, add no blockers")

Built and tested in an isolated copy of `pipeline/` while c-002's run was live (config.py and grade.py sit
in the review harness hash, so no edit could land in the workspace before that run ended). Three levers,
none a refusal or a gate:

- **`shadow-guard`**, a new structural runner in the shipped grader. It fails only on a planted entry that
  would shadow a module the hidden tests import (a new top-level `planner.py` or `planner/` when the tests
  import `planner`), a harness hook (`conftest.py`, `pytest.ini`, `sitecustomize.py`, `usercustomize.py`,
  `reward.json`, any `.pth`) or a hijacked standard-library name (`json/`, `pytest/`, `os.py`...). It never
  counts legitimate new files, so the reviewer cannot read it as an unpublished file budget. The per-step
  guard requirement now accepts a shadow-guard or a tree-guard (one blocker relaxed, none added). Docker
  smokes with the new guard in the synthetic task, one-step and two-step: PASS (nop 0, golden 1.0,
  alternative 1.0, mutants exact, `g_no_shadow`/`g2_no_shadow` P throughout).
- **Over-fit advisories** from `check_draft` (`validate.overfit_advisories`): a hidden test that uses a
  symbol only the reference patch introduces (absent from the instruction and the fixture); a tree-guard
  whose quoted sentences never speak of files or structure; a test that reads a subprocess's stdout.
  `ADVISORY ...` strings in the check_draft result; `clean` is unaffected.
- **`pre_review`**, a designer tool that runs the real independent reviewer (gpt-6-astra) over the staged
  draft exactly as the gate does, returns the verdict and every finding, records `pre-review-NNN.json`
  in the session, at most `config.MAX_PRE_REVIEWS_PER_SESSION = 3` per session. `submit_draft` never
  requires it. The system prompt tells the designer to run it once the controls are clean and again after
  addressing the findings, and the workflow gained the advisory-reading and pre-review steps.

`test_author_strengthening.py` (15 tests) covers the guard, the advisories (including that check_draft
stays clean while carrying them), the pre-review handler with a stubbed reviewer, the budget, and the
prompt wording. Whether these lift first-pass validity is measured by the next seeds, not assumed.

Synced into the workspace at 21:33 after stopping c-002's run at a clean boundary (21:24) (its final repair had
reached 221 responses with no submission; the round resumes from its staged draft, budget unchanged):
`Ran 748 tests in 66.8s  OK`. c-001-turbomachinery and c-006 moved to `envs-retired/` with notes.

## Follow-up run (launched 21:56): nine seeds, four workers

Ranks 2, 3, 7, 9, 10, 11 (fresh: streamlit video montage, next.js social app, elementor html widget,
rtl html dashboard, python text parsing, browser visual novel -- three browser-graded), rank 1 and rank 6
rebuilt from scratch with their retired attempts declared as predecessors, rank 5 (c-002) resumed into its
last repair round. Everything under the strengthened author. The screen follows for whatever validates.

- 22:21 c-002's designer called `pre_review` unprompted: gpt-6-astra, "revise", 20 findings, no harness
  problems -- the first real use of the tool.
- 23:17 c-002 stalled: the resumed session inherited the 78 steps left from the interrupted one and ran
  out while re-staging its tests to those findings (`agent designer reached 78 steps without a validated
  submission`; its last fourteen calls were `stage_spec`). Status `unverified`, draft retained. The designed
  path is one fresh session seeded with the draft (`--continue-stalled`), after this batch.
- 23:19 the streamlit designer called `pre_review` twice; both failed: the reviewer over-escaped quotes
  inside a code quotation (`split(\\".\\")`), which ends the JSON string early. The gate's own review
  would have failed the same way on any code quotation containing quotes. Fix (round 2, below).
- 23:33 the streamlit draft was submitted; the gate found the reference readable in an uploaded later
  version of the script (`uploads/auto_montage_with_word_subtitles (3).txt`) and opened its repair round 1.
  The same rule now runs in `check_draft` before submission (round 2).
- 23:45 next.js stalled at exactly 140 steps without submitting; the turbomachinery rebuild stood at 135.
  The 140-step cap predates the pre-review and advisory workflow.

## Round 2 (23:46-23:47): stop, three fixes, relaunch

22. `review.load_review_deliverable`: strict parse first, then the single reversible repair of an
    over-escaped quote (`\\\\"` -> `\\"`), reported as repaired; anything else still refused. Used by the gate,
    the orchestrator and `pre_review`.
23. `MAX_STEPS` 140 -> 200. The four interrupted sessions had their request budgets raised to 200 and their
    boundaries re-recorded (turbomachinery: 134 done, 66 left instead of 6).
24. `validate._fixture_leak_problems`: the reference's added lines readable in a fixture file (the
    `answer_lookup` rule, same threshold) reported by `check_draft`, so the designer removes the file before
    submitting instead of spending a repair round.

`Ran 754 tests in 19.0s  OK` (23:47:02). Relaunched at 23:47:51 with `--continue-stalled`

## What the pre-review loop shows (00:09-00:44, turbomachinery rebuild)

Two pre-reviews of successive drafts: 16 findings, then 25. Not one of the first eight check-level
findings disappeared, yet for every one of them the reviewer's second reason acknowledges the first
objection as addressed and raises a deeper one (an unpublished 100-point minimum in a helper; expected
sections computed by another agent-editable module; the shadow-guard's top-level rule not covering a
subpackage the tests import from). The designer fixes what it is told; the reviewer peels the next layer.
The same shape ended the pilot's repair rounds (13 of 14 checks still flagged, new reasons). Whether a
layer is signal or sampling noise is being measured: the frozen `c-006-...-attempt2` bundle is under review
twice more (`consistency.log`), to be compared check by check with its 19:37 review. If check-level
findings do not reproduce across samples, the honest acceptance rule is "a finding counts when two
independent reviews agree", not "one review finds nothing".

Real gap the reviewer found in the new guard: `shadow-guard` watched only new top-level entries; a planted
`BladeViewer_v2.0/blade_viewer/__init__.py` beside the real module would have passed. Fixed in the
development copy (a planted module definition at any depth whose path or bare name is an import the tests
make), test added; syncs with the reviewer-prompt clause and the orphan reaper after this run.

## Built in the development copy while the run continues (01:05-01:17)

25. **Independent-solver control** (`author/blind_solve.py`, designer tool `blind_solve`): a different
    vendor's model (gpt-6-astra for a claude-authored task) fixes the first step from the instruction and
    `/app/repo` alone -- four tools over one live agent container (list, read, write, run; no network),
    never the tests, solution or traps -- and the shipped grader scores its diff. The designer gets the
    diff and the per-check results with one instruction: each failing check is a TRAP (record the
    misreading) or an OVER-FIT test (rewrite it), and a solver that passes everything means the task is
    not hard yet. At most `config.MAX_BLIND_SOLVES_PER_SESSION = 2`; advisory, never required. The latest
    record is copied to `verification/blind-solve.json` and rides into the reviewer's input with a prompt
    line telling the reviewer what it is. Tests: the OpenAI loop with a scripted transport and a fake
    container, path escape refused, grading wired through `sandbox.verify` with a patch variant, vendor
    choice, the designer tool's receipt and budget. Not yet exercised against Docker or a live model.
26. Reviewer prompt: `HARNESS GUARDS ARE NOT REQUIREMENTS` (the shadow-guard clause). 27. Shadow-guard
    catches a planted module definition at any depth. 28. `sandbox.reap_orphans` removes sandbox
    containers older than three hours at every availability check (four were found idling, one 31 hours
    old). 29. `pre_review` keeps the reviewed package's checks, suite hashes and deliverable under
    `pre-review-NNN/` so successive pre-reviews can be compared after the scratch export is pruned.
    Development-copy suite over the affected files: `Ran 94 tests  OK`. All of it syncs when this run ends.

## Caching, measured on real sessions (01:57)

822 designer requests since 21:56 carried 285,004,952 prefix tokens: 43,210 billed as uncached input
(0.0%), 266,107,208 served from cache (93.4%), 18,854,534 written to cache; 2,562,576 output tokens. At
assumed claude-opus-5 list rates (input $15/M, cache write $18.75/M, cache read $1.5/M, output $75/M -- the
receipts carry no cost field) that is about $946 of designer spend for this run so far against about
$4,467 had nothing been cached. The average request carries ~347k tokens of context, which is also why a
designer turn takes tens of seconds before any tool runs.

Host load at 01:57: load average 24.6 / 33.3 / 49.7 on 15 cores, ~4,000 free pages. Mine: two sandbox
containers grading at ~100% CPU each. The rest: the parent workspace's `pipeline.run`, `pipeline.release`
and a probe refresh at 75-90% CPU each, the Docker VM at 270%, editor and session processes. Each designer
turn and each control run is slowed by it; nothing is stuck. Left at four workers rather than paying a
fourth stop-and-recover.

## Verdicts and a systemic finding (02:24-02:36)

- 02:24 streamlit's second submission: gate review "revise", 22 findings, its last repair round opened.
- 02:29 c-002 React: final review (harness stable) "revise" -- 9 of 11 checks, 4 gaps, 3 risks; both
  repairs spent (passes: 1 finding, then 21). **Blocked.** Four pre-reviews across its sessions read
  20 -> 20 -> 19 -> final 9-of-11: the loop narrowed the check count but never reached zero.
- The reviewer raised the same shortcut risk on both tasks, and it is the pipeline's, not the designer's:
  "the delivered verifier does not isolate hidden suite files from imported submission code" -- the
  verifier image copies `/tests` as root, never switches user or makes the suites read-only, and
  `grade.py` starts test subprocesses without dropping privileges, so code a test imports could rewrite a
  later test or its data. No control plants that, and no designer repair can fix a shipped runtime, so the
  finding would persist on every task until fixed here.

30. **Verifier isolation** (development copy): the verifier image makes `/tests` root-owned and read-only and
    defines an unprivileged `grader` user (written to `/etc/passwd`; slim images ship no `useradd`);
    `grade.py` hands each check's tree, HOME and private report directory to that user and starts every
    runner process as it (`preexec_fn`), failing closed with a reason if it cannot; Playwright gets a
    writable `--output` under the report directory; the sandbox's verifier container is granted exactly
    `CAP_CHOWN`, `CAP_SETUID`, `CAP_SETGID` and `CAP_DAC_READ_SEARCH` (the first smoke under the change failed
    every check with `PermissionError`, because the sandbox drops all capabilities; the second ran the tests
    but root could not read back a report directory it no longer owned -- both fail-closed, as designed).
    Docker smokes with the hardened verifier, one-step and two-step: **PASS** (02:43) -- nop 0, golden 1.0,
    alternative 1.0, every mutant exact, every guardrail holding, the pytest guardrails counted from reports
    written by the grader user. Harbor's own verifier keeps Docker's default capabilities, so the drop works
    there; `native_smoke` runs after the sync to prove it through the real harness.

## Round 3 (02:44-02:47): stop, sync, relaunch

02:44 turbomachinery's first gate review (harness stable): 20 of 21 checks "revise", 2 gaps, 3 risks --
the verifier-isolation risk among them -- and its repair round 1 opened. That was the last in-flight
receipt the harness change would void, so the build was stopped at clean boundaries (c-002 React
blocked; next.js at 112 of 200; the rest early in their sessions), the development copy synced
(`Ran 765 tests in 37.4s  OK`, 02:46, CLAUDE.md and README updated), and the nine seeds relaunched with
`--continue-stalled`. Now in the workspace, all tested: verifier isolation (30), the independent-solver
control (25), pre-review budget 5, the reviewer-prompt shadow-guard clause (26), shadow-guard depth (27),
the orphan reaper (28), pre-review evidence retention (29). `native_smoke` (real Harbor, hardened verifier)
runs alongside the relaunch.

`native_smoke` (02:52, real Harbor 0.22, separate verifier image, hardened): oracle mean reward 1.0 and nop 0.0,
one valid trial each, zero void. The grader dropped to the `grader` user inside Harbor's verifier container
(default capabilities) exactly as in the sandbox.

## Round 4 (03:19-03:28): the first two independent solves were artefacts

03:19-03:20 the first two `blind_solve` calls completed in real sessions (gpt-6-astra: next.js 12 steps /
27 tool calls / 488 s, turbomachinery 19 steps / 584 s) and reported 2 of 10 and 9 of 15 checks passing.
Both patches were exactly ~20,000 bytes and began mid-hunk: `Container.exec` keeps only the LAST 20,000
characters of stdout, the diff came back headless, `patch` skipped the leading garbage and applied the rest
without error, and every "failing check" was an artefact. Both designers had already acted on it (11 and
10 responses; next.js rewrote fixture files). Fix (31): the diff is written inside the container and
captured as a file; a patch that does not start like a unified diff is refused before grading; test asserts
a diff beyond the exec cap survives whole. Recovery: the two records voided (`.void.json` + note), the
receipt copies removed, and each affected session told in its own conversation that the result was invalid
and to rerun the tool. Stop 03:28 at clean boundaries, sync 03:28, `Ran 765 tests  OK`, relaunched 03:29. A third artefact surfaced after the relaunch: streamlit's solve of 03:27 (pre-fix,
headless too). Its session was already live again, so only the record was voided and the receipt copy removed;
that designer's pre-review and gate review are the backstop for anything it loosened on the false result.

## The first valid independent solves (03:37, 03:39)

With whole diffs (36,272 B and 47,383 B, both starting `diff -ruN`), gpt-6-astra scored **1.0 on both
tasks**: next.js 10 of 10 checks in 16 steps (465 s), turbomachinery 15 of 15 in 16 steps (578 s). Read
two ways. Validity: an independently written fix passes every hidden test, so the tests are not shaped
around the reference -- the objection that blocked the pilot is answered by execution on these two drafts.
Difficulty: a frontier model solved each task completely on its first pass in under ten minutes, which is
what the 5x2 screen would have measured hours later at ten episodes' cost. The designers now hold that
evidence before submission ("a solver that passes everything means the task is not hard yet") and must
plant traps that the published rules settle and this solver's reading misses.

04:14 the same on a first draft built entirely under the strengthened author: the PyQt6 rebuild (c-006),
independent solver 10 of 10 in 13 steps (357 s), whole diff. Three for three: the drafts' tests pass an
independent fix, and a frontier model completes each draft on its first pass in six to ten minutes. The
designers' own reactions ("I must harden substantially"; "not hard enough yet") are the intended loop; the
open question is whether the second solve, after hardening, falls into the planted traps.

## Pre-review trends (04:46)

```
turbomachinery (rebuild)     16 -> 25 -> 14
c-002 React (blocked)        20 -> 20 -> 19 -> 20
streamlit (final repair)     27 -> 19 -> 18 -> 10   (check-level 12 -> 11 -> 12 -> 3)
next.js (continued)          25 -> 28 -> 18
pyqt6 (rebuild, first draft) 13                       (check-level 5; lowest first pass of the day)
```
The loop converges once a designer has three or more passes and the verifier no longer carries the
systemic isolation finding; the first pass of a draft built entirely under the strengthened author opened
at roughly half the findings of the earlier first passes. One seed is one seed; the batch decides.

## Reviewer consistency, measured (01:04)

`c-006-...-attempt2`, frozen, reviewed three times by gpt-6-astra (19:37 gate review, 00:58, 01:04;
511 s and 358 s for the two extra samples). Of its 18 checks, 17 receive the same verdict in all three
samples (15 revise, 2 pass); one (`o_widget_base`) flips pass/revise/pass. Substantive gaps 4/4/4, shortcut
risks 4/3/3, ambiguities 2/3/2, privacy findings 1/1/1. The reviewer is not noise, and its consistent
objections are validity defects by the project's own bar: a correct third implementation fails
`o_clear_error` (every OSError subclass banned), `o_path_fallback`/`o2_direct_wins` (validating both
candidates counts as selecting the wrong one), `g_modules_*` (an internal constant frozen), `g_tree_*` (a
30-file, extension-allowlisted budget no sentence publishes); execution evidence is an unsigned marker file
the code under test can write; the reference leaves transcribe startup broken. None of that is a tightness
nit a lower bar should wave through.

What this settles: the constraint is how the designer writes hidden tests -- around its own reference and
its own doubles -- and its repairs remove the named objection while leaving the shape. The mandate text,
the advisories and the pre-review make the bar visible to it (all four active designers now pre-review
before submitting); whether that changes the shape of the tests is what this run measures. If it does not,
the next structural step is an independent-solver control: a different model fixes the task from the
instruction and the repository alone, and its diff is evidence the reviewer reads -- a test that a
differently written correct fix fails is the defect, caught by execution rather than by reading.: c-002-react and next.js get one
fresh 200-step session each seeded from their drafts; the four interrupted sessions resume at their
boundaries; ranks 9, 10, 11 start fresh.

## Incident (parent workspace, reported by a peer session)

The earlier version of the early-exit/forged-report unit test globbed `/tmp/**/*.json` on the HOST;
`/private/tmp/verify-root-*` and `pm-verify-fork-*` hold 116 symlinks into the parent workspace's bundles,
`glob` followed them, and on 2026-09-27 14:21 58,930 JSON files under the parent's `envs-batch32/`,
`envs-rebuilt/`, `.delivery-src/`, `archive/` and `envs/` became `{"nonce": "x", "ran": ["test_forge"],
"testsRun": 1}` (9,236 outside `verification/`). `envs-final/` and the batch32 zips are intact. The test is
now scoped to its own `TemporaryDirectory` with `TMPDIR` patched; the full suite ran three times today with no
write outside it. Nothing in the parent was touched from here; restoration is with the peer session and the
user. This workspace holds 0 forged files.

## Unit suite

**Correction.** Earlier today this section (and my status reports) said "707 green after the caching port".
That was wrong: every full run from the caching port until 17:11 ended `FAILED (failures=1)` --
`test_parallel_tools_consume_one_response_and_interruption_preserves_history` compared the request the
loop SENT with the stored conversation, and the sent copy now carries cache markers by design. I had read
the exit status of a `unittest ... ; tail` chain (the tail's, always 0) instead of the suite's own verdict
line. The test now strips the markers before comparing (the stored history is asserted marker-free by
`test_prompt_caching`), and the verdict below is read from the `OK`/`FAILED` line:

```
python -m unittest discover -s pipeline/tests -t .    Ran 722 tests in 19.9s   OK     (2026-09-28 17:11)
```

Runs that were genuinely green: after the clone parser fix (696, OK) and after the seed-identity edits
(697, OK). Runs reported green that were not: after the caching port, the T5 rule, the classifier budget,
the privacy edits and the release-surface change (all `FAILED (failures=1)`, the one test above).

## Round 5 (2026-09-29 05:03-05:20): the reviewer could not see step 2, and saw the task itself as a rival

Read from streamlit's fifth pre-review (05:00): "The supplied runnable reference is only step 1" and
"Duplicate task under the supplied new-task metadata". Both traced to `pipeline/review.py` and
`pipeline/corpus.py`, not to the design:

- `review()` staged `steps/` with `ignore_patterns("solution")` and copied only the root `solution/` (the
  first step's patch, which is what Harbor's oracle runs for step 1), while `sandbox.verify` applies every
  step's own `steps/<step>/solution/fix.patch` in one persistent agent container. The prompt promised the critic
  "the reference fix per step"; the staging never delivered it. Twelve review deliverables over the two
  two-step tasks (streamlit 9, pyqt6 3) contain the resulting finding, each holding the step-2 checks.
- `review_context` scanned `envs*` for novelty peers and listed the candidate's OTHER exports as rivals: a
  pre-review runs on `scratch/NNN`, whose name is not the task's, so the live bundle of the same session and
  seed (artifact hash different, similarity 0.39) came back as a source-related peer and the reviewer called
  the draft a duplicate under new-task metadata. Reproduced on streamlit's scratch/020 with the old code (one
  same-name peer) and the new (`same_task_versions_skipped: 1`, no peers).

Fix (built and tested in the scratchpad devroot, then synced): `stage_review_input` now stages each step's
solution under `solution/<step>/`, the receipt in `review_input_problems` binds those files, the prompt says
how a later step's reference composes; `review_context` skips a peer with the candidate's name or its
(session_id, seed_index) unless it is a declared predecessor and reports the count. Six new tests in
`pipeline/tests/test_review_step_solutions.py`; the blind-solve budget test now reads the constant instead of
hardcoding 2 (the queued `MAX_BLIND_SOLVES_PER_SESSION = 3` shipped with this sync).

Suites, verdict lines read from the runner: devroot `Ran 771 tests ... OK (skipped=3)` (05:12, after
mirroring the static root files and workspace.toml into the devroot -- the first devroot run's 20 errors
were all missing-root-file/dump-config errors, not code); workspace `Ran 771 tests in 40.504s  OK` (05:19).

Stop -> recover -> resume, per the recorded procedure: SIGTERM to the orchestrate child and the cli wrapper
by PID at 05:17 (the other orchestrate process on the machine, PID 5167, is the parent workspace's
`envs-final-stronger-models` build from another session and was left alone). All four `authoring-tools`
sessions had exactly one response past their boundary (turbomachinery 068 `run_controls`, streamlit 070
`submit_draft`, next.js 054 `run_controls`, pyqt6 077 `run_controls`) with conversation.json, draft.json and
the workspace unchanged since the boundary and no bundle-root write (streamlit's package is from 02:07); each
response was moved to `verification/interrupted-responses/` with a NOTE, `checkpoint.author_boundary`
re-recorded, and `restore_design_session` dry-ran OK on all five authoring bundles (remaining steps 105 /
89 / 123 / 93 / 192). Relaunched at 05:20 as launch 5 (`build-batch9e`), same ranks,
`--continue-stalled`.

Untested: whether the critic stops raising the step-2 finding on a real two-step review -- the first
post-fix pre-review or gate review of streamlit/turbomachinery/pyqt6 measures it. No harness file changed
except `config.py` (one constant), with no review or screen in flight at the time.

## Round 6 (2026-09-29 11:05-12:22): a citation of the control receipts voided reviews

Launch 5 (05:20) produced the programme's first validated coding task: c-001-turbomachinery-cad-tool,
gate review 12:14 `pass` on all 26 checks, controls untouched 0 / golden 1.0 / alternative 1.0 / tamper 0 /
early-exit 0, faithfulness 0.802 -> F2, two gate rounds. Its two independent solves both scored 1.0, and the
reviewer's difficulty advisory reads "moderate ... the 'hard' label is not established": the task is valid
and, on the available evidence, easy. Streamlit (05:45) and pyqt6 (11:12) were blocked on genuine
test-double and forgeable-observer findings with their repair budgets already spent.

The 33 reviews written after round 5's staging fix contain no "reference is only step 1" and no "duplicate
task" finding; the reviewer now cites `solution/01-…` and `solution/02-…` and reasons about the cumulative
reference. Both fixes are verified on real reviews.

A third pipeline defect: elementor's gate review (09:28) and c-009's (12:14) both came back `unverified`
instead of `revise`, each with two repairs unspent, because the reviewer cited `controls.json` for the
shadow-guard rows (the prompt tells it to read that receipt) and the citation validator rewrote any unknown
root path to `fixture/controls.json`. The same thing had voided pyqt6-attempt2 on 09-28. Three of 61
deliverables on disk carry it. Fix: `review.ROOT_INPUTS` accepts `controls.json`, `probes.json` and
`blind-solve.json` at the workspace root; two regression tests (`ControlReceiptsMayBeCited`, one positive,
one negative). Devroot suite `Ran 773 tests ... OK (skipped=3)` (11:11).

Installed by stop -> recover -> sync -> resume: SIGTERM by PID at 12:21; next.js had one response past its
boundary (`run_controls` in flight, nothing durable) and was set aside with a note; c-010 and c-011 were at
their boundaries; all restores dry-ran OK; c-009 and elementor (`checking`, submitted spec intact) are
resumable and will be re-reviewed from their submissions with two repairs each. Workspace suite
`Ran 773 tests in 24.273s  OK` (12:22). Relaunch time is in `build-batch9f.started`.

Scores: the calibration screen (5 x 2, Coder pool) has not run for any task in this batch -- it runs once
the whole batch is built. The only measured numbers so far are the controls above and the independent
solves (every task 1.0 except elementor 0.66).

## Early screen of the first validated task (2026-09-29 13:31-14:08)

`./arena screen envs/target-F2-scenario/c-001-turbomachinery-cad-tool` on arena-calib-1 (idle; no other
calibration process on the machine), 5 episodes x 2 models, Fable 5.1 in claude-code 2.1.283 and GPT-6 Astra in
codex 0.157.1, effort high, no turn or time limits. Result: pass@1 1.00, pass@4 1.00, mean 1.00 on BOTH models;
outlook `weak-candidate`; all 12 objective checks never failed on either model; no unused trace corrections,
authored-trap room 19. Report: `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/screen-turbo-early.md`, receipt in the bundle's `verification/screen.json`.
The second real coding task to screen, and the second at 1.00/1.00: the gate now produces valid tasks, and the
first-pass authoring does not produce hard ones. Difficulty is the next problem, and it is a premise problem
(see the standing rule), not a coverage problem.

## Launch 6 ended on a dead credential (2026-09-29 14:09-14:24)

All five in-flight designs (next.js, elementor, c-009, c-010, c-011) failed between 14:09 and 14:24 with
`HTTP 401: provider request failed` from the Anthropic transport; the last successful designer response was
13:54. A live probe through the pipeline's own transports at 14:25 (no secret printed): Anthropic 401, OpenAI
OK. `keys.env` holds an `sk-ant-oat…` OAuth token written 2026-09-28 14:11, i.e. it died about 24 hours after
it was issued. This is infra loss, not a result: every one of the five checkpoints is at an `authoring`
boundary and `restore_design_session` dry-runs OK on all of them (remaining steps 175 / 76 / 121 / 57 / 180).
The batch then ran its phase-4 screen, which REUSED the 13:31 early campaign for turbomachinery (no second
spend) and reported 1 validated / 3 blocked / 5 error. Final table of launch 6:

```
9 tasks in 7284.4s (4.4/h)   status: {'validated': 1, 'blocked': 3, 'error': 5}
VAL  c-001-turbomachinery-cad-tool   p@1 1.00 / 1.00   weak-candidate
BLOK c-002-single-file-react-app, c-002-streamlit-video-montage, c-006-pyqt6-desktop-app  (semantic review, budgets spent)
ERR  c-003, c-007, c-009, c-010, c-011  (HTTP 401 mid-design; resumable)
```

Needs a fresh Anthropic token from the user (`./arena keys`), then `./arena build --ranks 1,2,3,5,6,7,9,10,11
-o envs --task-workers 4 --continue-stalled` resumes the five.

## Launch 7 (2026-09-29 22:21): resumed after the credential refresh

The user refreshed the OAuth token in the parent workspace's `keys.env` (14:33); it was copied into the
coding workspace's `keys.env` at 22:21 without being read (`cp`, mode 600, the dead file kept as
`keys.env.bak-20260928`). Probe through the pipeline's transports at 22:21: Anthropic OK, OpenAI OK.
Relaunched with the same ranks and `--continue-stalled`; the five 401-interrupted designs resume at their
recorded boundaries.

## Hardening lineage: c-001-turbomachinery-cad-tool (started 2026-09-29 23:24)

User request: harden to pass@1 < 0.2 with near-miss / semantically similar decoys that make a model fail
genuinely. Launched `./arena harden <bundle> --apply --traps /private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/turbo-harden-traps.json` (attempt 1 of 3,
the full extremely-hard redesign; each valid revision is screened 5 x 2 on arena-calib-1, which was idle;
the parent workspace's campaign runs on arena-calib-2). The build (launch 7) keeps running; no pipeline file
is edited while either runs.

Operator trap ideas handed to the designer (held to every legitimacy rule, dropped if the source cannot
support them): (1) silent surface-variant identity -- the same quantity under two spellings or forms, one
carried only by older saved params dicts, stated once in the user's words; (2) population scope of a later
revision that re-governs an earlier step's path the visible repro never exercises (interpolated stations,
the chart's second dispatch); (3) a derived figure stored per section under the old premise and reused after
the revision; (4) a silent unit or frame boundary where the code already mixes two readings (degrees vs
radians at the pinned ends, chord fraction vs body fraction, percent vs fraction); (5) silence and coupling
throughout -- each definition once, away from the change, at least three objectives routed per premise,
every trap proved by a mutant and by blind_solve.

Evidence going in, stated so the outcome can be read honestly: in this harness (Claude Code 2.1.283 /
Codex 0.157.1, high, no limits) 0 of 27 document-pipeline hardening lineages produced legitimate difficulty;
8 hand-hardened bundles with near-miss decoys screened 8/8 at 1.00/1.00; the single blind-verified bite is a
silent surface-variant identity (a-005 v3, Astra 0/5). Both real coding tasks screened 1.00/1.00. The
tooling's stop rule is pass@1 < 0.5 on both models (policy), not the user's 0.2; a revision that lands
between them ends the lineage as target-met and further attempts would be a separate decision. Known gap
carried from the parent: a hardened revision comes out with no faithfulness level and the name arena/vN and
needs finalizing before release.

## Launch 7 ended (2026-09-30 06:52): 3 validated, 5 blocked, 1 stalled; batch screen of three

`9 tasks in 28999.0s   status: {'validated': 3, 'blocked': 5, 'unverified': 1}`. Validated: c-001-turbomachinery
(F2), c-003-next-js-social-app (F3, 19 checks, gate pass 23:50), c-010-python-text-parsing (F3, 19 checks, 2
steps). Blocked on genuine review findings with budgets spent: react, streamlit, pyqt6, elementor (18 findings),
c-009 (17). c-011-browser-visual-novel stalled at 180 steps without a submission (resumable once more with
`--continue-stalled`). Batch screen, 5 x 2, Fable 5.1 in claude-code 2.1.283 / GPT-6 Astra in codex 0.157.1,
high effort, no limits, report `pipeline-artifacts/screen-report-envs.md` (06:52):

```
c-001-turbomachinery-cad-tool   Fable 1.00/1.00/1.00   Astra 1.00/1.00/1.00   weak-candidate   (campaign reused)
c-010-python-text-parsing       Fable 0.80/1.00/0.97   Astra 1.00/1.00/1.00   poor-candidate
c-003-next-js-social-app        Fable 1.00/1.00/1.00   Astra 1.00/1.00/1.00   weak-candidate
```
c-010: 5/5 valid episodes per model, no voids, no reward hacking; the one Fable miss failed
`o_new_lettered` and `o_new_joined` in step 1, attributed `capability` (a model failure, not a void). Whether
it is a fair error or a defensible alternative reading is not yet read from the trajectory.

Hardening lineage, turbomachinery: attempt 1 (the full redesign) SUBMITTED at 04:59 after three controls runs
and was judged INVALID by the routing rule -- four of the six new traps (`t_record_closure`, `t_width_blend`,
`t_stale_mca_record`, `t_record_unscaled`) drive only one graded objective each, and a new trap must drive at
least two (`harden.MIN_CHECKS_PER_NEW_TRAP`). Attempt 2 (repair only) is running in a fresh session; its
first pre-review (06:52) returned 22 findings, read below. One attempt remains after it.

## CRITICAL: the shipped grader's report is forgeable from inside the test process (2026-09-30 07:01)

Found by the independent reviewer in the hardening revision's first pre-review (06:52, finding S1) and
CONFIRMED BY EXECUTION at 06:59: a 20-line block prepended to
`BladeViewer_v2.0/blade_viewer.py` (the module the hidden tests import) that reads `__main__.cfg` -- the JSON
the grader's in-process bootstrap keeps in the test process's globals: report path, nonce, selected nodes --
writes a JUnit report whose suite name is the nonce with one passing testcase per selected node, then
`os._exit(0)`, scored `reward 1.0`, 26/26 checks over all three steps of the VALIDATED
c-001-turbomachinery-cad-tool with no fix applied (`sandbox.verify`, 5 s; record
`/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/forge-check/forge-verify.json`, probe `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/forge-check/forger.py`).

Why the existing controls miss it: `early_exit` forges reports with suite name "forged" (no nonce) and
`test_tamper` plants conftest/ini/sitecustomize files; neither reads the bootstrap's globals. The
`_pytest_report_counts` nonce check protects against a PRE-PLANTED report, not against code that runs inside
the grader's interpreter. The same exposure exists in `UNITTEST_BOOTSTRAP` (`cfg` global).

Scope: every pytest- or unittest-graded task built so far (all three validated tasks, every screen). The
hardening lineage was stopped at 06:5x before attempt 2 was spent (ledger: attempt 1 invalid only); no
revision can pass review until the runtime is fixed. Fix, tests, smokes and re-export follow below.

### The fix (2026-09-30 07:00-07:50), verified so far by unit tests

`grade.py`: both in-process bootstraps now consume their stdin configuration inside a function and delete
it, close stdin (replaced by /dev/null), reset `sys.argv`, and keep the report path and nonce only in the
closure of a reporter built by a helper whose frame is gone before any test module is imported. pytest's
junitxml plugin is disabled (`-p no:junitxml`; `--junitxml` and `junit_suite_name` no longer exist in the
process, so pytest's Config holds no nonce) and the closure writes the JUnit report itself at session finish
(hook signature `pytest_runtest_logreport(self, report)` -- pluggy validates argument names, which the first
draft got wrong and every real run refused). `_pytest_report_counts` refuses a document with more than one
testsuite (a shotgun at the nonce). `controls.py`: a new required control `report_forge` prepends to every
source file a hunt of `__main__`, the environment, argv, stdin and every live frame's locals that forges a
report and exits 0 with whatever it finds; on the untouched fixture every objective must still fail.
`probes.py`: `report_forge` is REQUIRED and a HACK_PROBE; a controls receipt from before it is not ok; every
receipt's untested_surfaces now declares the residual (a walk of the garbage collector or the plugin manager
still reaches the reporter's closure; in-process execution cannot close that).

Tests: `pipeline/tests/test_report_forgery.py` (7): the control's own planting ahead of a genuinely failing
unittest cannot forge (counts 0/1/0); an honest module is counted; a scan run inside the bootstrap finds the
identity in none of `__main__`, argv, environ, stdin or live frames; the planting DOES forge and exit 0 when
`cfg` sits in `__main__` (the old exposure), and that report would have been counted; a two-suite report is
refused; the control is required and an old receipt is not ok. The old pin `junit_suite_name in
PYTEST_BOOTSTRAP` in test_coding_core was replaced by the new invariants. Unit suite: `Ran 780 tests ... OK`
(07:42, before the hook-name fix; the fixed bootstrap re-ran test_report_forgery + test_coding_core: 43 OK).

Execution evidence pending in this log's next entry: forger 0.0 and reference 1.0 on a copy of turbomachinery
carrying the fixed runtime, both sandbox smokes, the native Harbor smoke.

### Fixed runtime verified by execution (2026-09-30 08:01-08:38)

On a copy of c-001-turbomachinery-cad-tool carrying the fixed `tests/grade.py` (`runtime.problems` empty):
the forger probe that scored 1.0 this morning now scores `reward 0.0` on every step (every pytest objective:
"runner produced no trustworthy report of completed tests"; only the structural guards pass, as on the
untouched fixture), and the reference solution scores `reward 1.0`, 26/26. `sandbox_smoke` PASS (golden
1.0, alt 1.0, nop guardrails, mutants exact), `sandbox_smoke --two-steps` PASS, `native_smoke` (real Harbor
0.22) oracle 1.0 / nop 0.0. Unit suite 780 OK. Records: `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/tasks/by593nrnz.output`,
`/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/smoke-forge-1b`, `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/smoke-forge-2b`, `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/native-forge-b`.

Consequences still to execute: every built bundle ships the old runtime (`runtime.problems` will flag it at
release), so the three validated tasks are re-staged, re-gated (the new `report_forge` control), re-reviewed
(harness hash changed) and re-screened before anything is qualified or delivered. The hardening lineage
restarts from the original with the six v2 trap ideas carried as operator ideas, since an invalid revision
cannot be resumed and the interrupted repair session spent nothing.

### Fresh review of the fixed runtime: REVISE on the declared residual (2026-09-30 10:33)

turbomachinery's fresh gpt-6-astra review (harness stable) revised every pytest check for one reason, S1:
"a frame walk can reach _boot's still-live plugin and args locals ... the bound pytest_sessionfinish method
exposes closure cells named nonce, report_path and outcomes. Candidate code can ... invoke that reporter to
write a correctly named, nonce-bearing report, and exit zero ... Alternatively the reporter is reachable
through pytest's plugin manager or the garbage collector. ... Move trusted assertions/reporting across a
process boundary from the candidate and add an existing-module closure/plugin tamper control." Plus two
design gaps (G1 order-insensitive section comparisons for a published order rule; G2 cap preservation not
tested). The reviewer is right that a declared residual is not a closed route. Next: an introspection
detector in the runtime over files the agent changed, the bootstrap frame emptied of references, the
control extended with the closure-traversal forger the reviewer described, all proved by execution.

### Round 2 of the runtime fix (2026-09-30 10:35-11:10): refuse the route instead of declaring it

`grade.py`: `INTROSPECTION_TOKENS` / `introspection_problems(tree, manifest, vendored)` scan every `.py` file
the agent wrote or changed (hash absent from or different to the fixture manifest; vendored trees and
unchanged fixture files never scanned) for the primitives a forgery needs -- frames (`_getframe`,
`currentframe`, `f_back`, `f_locals`), the collector (`gc.get_objects` ...), closures (`__closure__`,
`cell_contents`, `co_freevars`), the plugin manager and the reporter's hook names, `__main__`, `settrace`,
ctypes, `os._exit`, `/proc/self`, `/dev/fd`, dynamic attribute access on sys/gc/inspect, `__import__`,
`import_module`, bare `exec`/`eval`/`compile` (not `re.compile`, not `literal_eval`). For pytest and unittest
checks the runtime refuses to run the suite over such a tree and the check fails closed with the file and
token named. The pytest bootstrap now holds no reference in any live frame (`sys.exit(pytest.main(*_prepare()))`).
`controls.py`: the `report_forge` planting adds the second review's route -- walk gc for the reporter,
fill its `outcomes`, call `pytest_sessionfinish`, exit 0; and any closure carrying a nonce. `probes.py`:
the untested surface now says what the scan covers and that an obfuscated primitive it does not name is not
covered. Tests: 45 OK across test_report_forgery (10) and test_coding_core; the early-exit test's planted
modules are marked fixture files so the refusal does not pre-empt what it measures. The never-recorded
`hardened/v3` copy of the stopped attempt-2 session was removed (no attempt spent; ledger unchanged).
Execution evidence follows.

### Detector runtime verified by execution (2026-09-30 10:40-10:45)

Copy of turbomachinery with the final runtime: the `__main__` forger 0.0 and the gc/closure reacher (the
second review's route) 0.0 -- every pytest check "grader: code the hidden tests would import reaches into the
test process, so the suite was not run over it: BladeViewer_v2.0/blade_viewer.py: ..."; the reference 1.0,
26/26 (54 s). `sandbox_smoke` PASS, `sandbox_smoke --two-steps` PASS, `native_smoke` oracle 1.0 / nop 0.0.
Unit suite `Ran 782 tests ... OK` (10:41). The reviewer prompt now describes the mechanism (in-process
execution, the refusal rule, the control) so the next review judges it rather than re-declaring the class.
Records: `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/tasks/bcp6g27pn.output`, `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/ws-suite-forge3.log`.

### Order of operations (2026-09-30 10:46-10:48)

`arena harden` relaunched at 10:46 was REFUSED: "no calibration on record. Measure a frozen baseline first" --
the original's screen binds to the pre-fix artifact and the runtime was re-staged into the original at 08:41.
Nothing was spent (ledger: attempt 1 invalid only). Order flipped: a sequential chain re-stages, re-runs
controls and probes (`arena sandbox`), gates, reviews (gpt-6-astra) and re-screens turbomachinery first, then
next.js, then c-010, one screen at a time on arena-calib-1; hardening attempt 2 is launched from inside the
chain the moment turbomachinery's fresh screen exists, so its baseline binds. Started 10:48.

### Round 3 of the runtime fix (2026-09-30 11:36-12:00): the framework itself, and the environment

The re-verification chain (10:47-11:36) took all three validated bundles through controls and gates GREEN
under the round-2 runtime (`report_forge` 0.0 on each), and every fresh review then found something new:
- turbomachinery S1: agent code imported during the test call can replace `_pytest.reports.TestReport.__init__`
  so every report says passed; the genuine reporter then writes an honest-looking report under the real
  nonce. No frame, gc or closure access needed.
- next.js S1: the backend is told which check is running -- the working tree was `work/<check id>` and
  exported as ARENA_REPO, HOME was `/tmp/arena-grade-home/<check id>`, and pytest's PYTEST_CURRENT_TEST
  reached the Node child through the suite's driver; an authorization gate can branch on scoring identity.
- c-010: the reviewer's own deliverable was invalid JSON (an unescaped quote inside a string at char 22021);
  the review crashed and left no verdict. To be re-run.

Runtime, round 3 (`grade.py`): both bootstraps take a SNAPSHOT of every function and method identity in the
test framework's outcome path before any test module is imported (pytest: _pytest.runner/reports/python/
outcomes/skipping/main/nodes/fixtures/unittest/assertion/config, pluggy, unittest.case/result; unittest:
case/result/runner/suite/loader) and compare at session end; a mutation writes the report under the name
`tampered` (no nonce -> no trustworthy report) and says which names changed on stderr. The pytest reporter
pops PYTEST_CURRENT_TEST at the start of every setup/call/teardown (tryfirst); the runner's environment drops
PYTEST_CURRENT_TEST/PYTEST_ADDOPTS; the working tree is `work/t-<random>` and HOME `/tmp/arena-grade-home/
<random>`, so no path names the check. Tokens added for changed files: `_pytest`, `import/from pytest`,
`import builtins`, `__builtins__`, `sys.meta_path`, `sys.path_hooks`, `sys.excepthook`,
`unittest.(case|result|runner)`, `TestCase.<attr> =`. `controls.py`: the planting adds the rewrite route
(TestReport.__init__ and TestCase.run). Tests: 49 OK (test_report_forgery 14: a token-free rewrite of
unittest yields no trustworthy report with "mutated" on stderr; the running test is absent from the
environment; the working tree and HOME carry no check id; the new tokens). Execution evidence follows.

### Round 3, first execution (2026-09-30 11:41-11:44): a false positive in the snapshot, narrowed

The first execution of round 3 caught the obfuscated rewirer (0.0, "the test framework was mutated") but
ALSO the reference (0.0) and every smoke golden: pytest rebinds its own setup machinery during a session
(`_pytest.config.Config._getini_unknown_type`), so a snapshot that includes `_pytest.config` flags every
honest run. The snapshot is now the outcome path only: `_pytest.runner`, `reports`, `python`, `outcomes`,
`skipping`, `nodes`, `unittest`, `pluggy._hooks`, `pluggy._callers`, `unittest.case`, `unittest.result`;
`_pytest.config`, `main`, `fixtures`, `assertion` and `pluggy._manager` are out. The rewirer's target
(`TestReport.__init__`) stays inside the set. Also in this round: `review.review_problems` treats a
deliverable that does not parse as a review problem ("review deliverable is not valid JSON: ...") instead
of crashing (c-010's review at 11:36), and the reviewer prompt describes the identity snapshot and the
environment scrubbing. Re-executing: rewirer, reference, the other forgers, both sandbox smokes, native.

### Round 3 verified by execution (2026-09-30 11:45-11:48)

Copy of turbomachinery with the round-3 runtime: the obfuscated rewirer (rebinds `TestReport.__init__` via
`sys.modules.get('_py' + 'test.reports')`, names no scanned token) scores 0.0 on every step with "the test
framework was mutated before the session ended: _pytest.reports.TestReport.__init__" on stderr; the
reference scores 1.0, 26/26; the `__main__` forger and the gc reacher 0.0 (refused before the suite runs).
`sandbox_smoke` PASS, `sandbox_smoke --two-steps` PASS, `native_smoke` oracle 1.0 / nop 0.0. Unit suite
`Ran 786 tests ... OK`. Records: `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/tasks/boerddlaj.output`, `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/ws-suite-forge5.log`. The re-verification
chain over the three validated bundles restarted at 11:49 (stage, controls and probes, gate,
review, screen; hardening attempt 2 launched behind turbomachinery's fresh screen).

### Round 3 in the field (2026-09-30 11:49-12:31), and round 4

Chain 2 under the round-3 runtime: turbomachinery gate GREEN (12:05) but its fresh review revised again with a
fourth route -- "ordinary imports can mutate the hidden suite itself": the tests import blade_viewer inside
their bodies, the suite directory is on sys.path, so the fixture module can `import test_naca_angles` and
empty its case lists or replace its assertion helpers; no token, no framework mutation, no environment
read. next.js gate GREEN (12:21) and its fresh review PASSED (12:27, 0 gaps, 0 risks) -- the first review
pass under a runtime the reviewer has examined for forgery; its 5x2 re-screen started 12:27 on arena-calib-1.

Round 4 (`grade.py`): after collection (pytest `pytest_collection_finish`, trylast; unittest after
loading) the hidden suite is DETACHED from the import system -- every module whose file lies under the
suites directory leaves `sys.modules`, the directory leaves `sys.path`, importer caches are invalidated --
while the runner keeps its own references so the collected tests run; the suite modules' namespaces are
fingerprinted (data structurally, functions and classes by identity) and compared at session end, a
difference yielding the report without its nonce. Tokens added for changed files: `sys.modules`, `/tests`,
`ARENA_`, `import test_*`/`conftest`, `from test_* import`. The planting adds the suite-gutting route; the
reviewer prompt now states the bounded threat model (interpreter, framework, run identity, hidden suite)
and asks for routes outside it with exact code, and says that code merely behaving differently under test
is what calibration attribution reads trajectories for. Tests: 52 OK (three through the unittest bootstrap
itself: a plain import of a loaded suite module fails after detachment while the victim still passes; a
reference bound before detachment that empties a case list is caught by the fingerprint, nonce `tampered`;
an honest suite keeps its nonce). Execution evidence follows.

### Round 4, first execution (2026-09-30 12:31-12:34): the suite fingerprint read a cache as tampering

The gutting probes (plain and obfuscated imports of the hidden suite) scored 0.0 -- the plain one refused
by the new tokens, the obfuscated one defeated by the detachment (the import fails, the tests run against the
unfixed code) -- and the rewirer 0.0, smokes PASS, native PASS; but the reference scored 0.6667: step 3's
suite fills two module-level caches (`_CACHE`, `_MULT` in test_ctrl_multipliers.py) while it runs, and a
fingerprint that covered data read that as `suite:test_ctrl_multipliers` mutated. The suite fingerprint now
covers functions and classes only (replacing a helper is the tampering; the data is protected by the
detachment); the unit test replaces a helper through a pre-detachment reference and is still caught. 52 OK.
Re-executing with the reference first.

### Round 4 verified by execution (2026-09-30 12:35-12:39)

Copy of turbomachinery with the round-4 runtime: reference 1.0, 26/26 (43 s); obfuscated suite gutting
(`__import__` reached through `__builtins__` under a split name, no scanned token) 0.0 -- the import fails
after detachment and the tests run against the unfixed code; plain gutting 0.0 (refused by the tokens);
rewirer 0.0 (framework snapshot); `__main__` forger 0.0 (refused). `sandbox_smoke` PASS, `--two-steps`
PASS, `native_smoke` oracle 1.0 / nop 0.0. Unit suite `Ran 789 tests ... OK`. Records:
`/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/tasks/bl9owdg8w.output`, `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/ws-suite-forge7.log`. Chain 2 (round 3) is left to finish its next.js and
c-010 screens for early difficulty numbers; the round-4 chain runs controls, gates and reviews now and
re-screens all three, one at a time, after chain 2 releases the pool.

### Chain 2 results and the misfired round-4 chain (2026-09-30 12:40-13:15)

Chain 2: next.js re-screened at 12:27-12:47 under the round-3 runtime: Fable 1.00/1.00/1.00, Astra
1.00/1.00/1.00, weak-candidate. c-010 was re-staged at 12:47 -- by then grade.py on disk was ROUND 4, so its
controls (report_forge 0.0), gate GREEN, fresh review PASS (13:00, 0 gaps, 0 risks) and screen (13:12) ran
under round 4: Fable p@1 0.40 / p@4 1.00 / mean 0.90, Astra 1.00/1.00/1.00, poor-candidate. Turbomachinery's
review under round 3 revised (the suite-import route), no screen.

The first round-4 chain launched at 12:40 misfired: zsh does not word-split a `$BUNDLES` variable in `for`,
so the loop ran once over the whole string as a path (every file "missing"), touched nothing, and screened
nothing. Relaunched 13:15 with literal paths for turbomachinery and next.js; c-010 needs no re-run if its
receipts bind to the current artifact (checked below).

### c-010 is complete under the round-4 runtime (checked 2026-09-30 13:14)

`tests/grade.py` current; controls, fresh review (pass) and screen all bind to the current artifact. The
screen: Fable 5.1 p@1 0.40 / p@4 1.00 / mean 0.90; GPT-6 Astra 1.00 / 1.00 / 1.00; 5 valid episodes per
model. The Fable misses are on the same premise as the earlier 0.80 run: the dump's option letters must keep
their own letter but come out a) b) c) d) in that order. The first coding task with a complete binding
measurement under a runtime the reviewer examined for forgery, and the first with a below-0.5 pass@1 on one
model. next.js (review pass, screen 1.00/1.00 under round 3) and turbomachinery are in the relaunched chain.

### Every screen failure classified (2026-09-30 13:40)

Screens so far: turbomachinery 10/10 pass, next.js 10/10 pass, c-010 Astra 5/5 pass and Fable 2/5 pass
(round-4 screen) plus Fable 4/5 (the 06:44 screen of the pre-fix artifact). All five Fable episodes are
valid, ran to a normal final agent message (8-19 steps, no timeout, no tool error), attributed `capability`,
no reward hacking, no evidence problems. The failures:
- 2 episodes (YCMpzVp, Yzcad5J): `o_s10_questions` + `o_carry` -- the agent's placeholder regex was
  `(?:Visual|Image|Figure)` / `(?:Visual|Figure|Diagram|Image)`, so it dropped "(Figure: the three circles
  are printed side by side)" from a stem in the hidden s14 dump. The instruction: "the bracketed (Visual: ...)
  and [Image ...] placeholders ... drop those wherever they sit, but only those, normal brackets inside a
  question stay." One agent's own docstring restates the rule as Visual/Image only and its regex broadens
  it anyway. Designed trap `t_overstrip` (mutant m_overstrip). The two passing episodes did not broaden it.
  Genuine spec-precision failure, not an ambiguity.
- 1 episode (v9o4JnH) and the 06:44 miss: `o_new_lettered` + `o_new_joined` -- hidden s12 question 8 prints
  its choices A, C, B, D; the agent kept sheet order (a, c, b, d) against "exactly four option lines a) b)
  c) d) in that order" while "keeps the letter the sheet puts in front of it". Its transcript never
  considers the ordering rule. Designed trap `t_letterpos` (mutant m_pos). Genuine.
Verdict: every failure is the model's; none is a void, a grader defect or a defensible alternative reading.

### Failure-mode study and the next experiments (2026-09-30 14:45-14:57)

Chain 4 (relaunched 13:14) died silently at ~13:35 with its whole process tree and no traceback; cause
unknown. Chain 5 relaunched at 14:45 detached (`subprocess.Popen(start_new_session=True)`): turbomachinery
review -> screen -> hardening attempt 2; then next.js stage/sandbox/gate/review/screen. `analysis/FAILURE-
MODES-CODING.md` written: the measured record (four coding tasks, one genuine bite: c-010 Fable 0.40 on two
modes, rule-scope over-generalisation and two interacting rules on a rare input; Astra literal 5/5), the one
Astra lever (surface/format variant), the untested two-sided lever (nested exception chains), the dead levers,
and why no single mode reaches < 0.5 on both. c-010 hardening attempt 1 launched 14:55 detached with the
tighten-only brief (F3 budget: 0 authored traps).

### Round 5 of the runtime (2026-09-30 14:52-15:05): code identity, pluggy's manager, honest test files

Turbomachinery's round-4 review (14:52) revised on two findings. S1: the framework snapshot compared function
identity only, so `fn.__code__ = other.__code__` in place, or an in-place edit of `PluginManager._hookexec`
(pluggy._manager was outside the snapshot), makes every `pytest_runtest_makereport` result say passed while the
genuine reporter signs the report; `PluginManager` was not matched by the lowercase `pluginmanager` token and
`__code__`, `__globals__`, `__defaults__`, `FunctionType` were not banned. G1: the `import pytest` token
refused an honest fix that added an ordinary regression test file, which no instruction prohibits.

Fixes: the snapshot records, per function or method, the object, its `__func__`/`fget`, its code object, its
`__defaults__`, its `__kwdefaults__` and its closure cells (`_fsig`); `pluggy._manager` and `pluggy._result`
join the outcome path; tokens gain `[Pp]lugin[Mm]anager`, `_hookexec`, `__code__`, `__globals__`,
`__defaults__`, `__kwdefaults__`, `FunctionType`, `CodeType`, `pluggy` and drop `import pytest`, `from pytest`
and `unittest.(case|result|runner)` (an honest test file imports them; rewiring is what the snapshot catches;
`TestCase.<attr> =` stays). A new required control `benign_test` runs the reference plus an ordinary
`tests/test_arena_benign_regression.py` importing pytest and requires exactly the reference's score. The
reviewer prompt names both. Tests: 53 OK (an in-place `__code__` swap of `unittest.TestCase.run` through the
bootstrap yields no trustworthy report with "mutated"; pytest/unittest imports in a test file are not refused;
`PluginManager`, `__code__`, `pluggy` are). Execution evidence follows.

The Anthropic OAuth token expired again at ~14:33 (24 h after issue): c-010's hardening attempt 1 was
INTERRUPTED at launch with HTTP 401 (ledger: interruptions 1, attempts none; nothing spent). Reviews and
screens (OpenAI, Coder pool) continue. A fresh token is needed before any designer runs.

### Round 5 verified by execution (2026-09-30 14:57-15:02)

Copy of turbomachinery with the round-5 runtime: reference 1.0, 26/26; reference plus an ordinary
`tests/test_arena_benign_regression.py` importing pytest 1.0, 26/26 (the honest engineering the round-4
scan refused); the code-swap payload 0.0 (refused: `_pytest`, `__code__`); the earlier obfuscated rewirer
0.0 (framework snapshot). `sandbox_smoke` PASS, `--two-steps` PASS, `native_smoke` oracle 1.0 / nop 0.0.
Unit suite `Ran 790 tests ... OK`. Records: `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/tasks/bzgbtn0mg.output`, `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/ws-suite-forge8.log`.
Chain 5 (round 4) is stopped and the three bundles are re-verified under round 5; hardening launches are
left out of the chain until the Anthropic token is refreshed.

### Round 6 of the runtime (2026-09-30 15:33-15:40): the hidden tree in every language

next.js's review under round 5 (15:33, run by the old chain 4, which had not died after all -- its 13:32
review process was hung until the 15:03 kill, after which the chain resumed with next.js under whatever
runtime was on disk) found S1: the scan covered .py only, while the suite runs the agent's JavaScript in a
Node child that can READ /tests (unwritable, not unreadable); a changed roles.js reading the expected role
list from the hidden suite scores the objective. Plus M1, a design finding (m_oracle_tamper is a deception
probe listed among misreading mutants), which is the designer's to fix and needs a repair pass next.js no
longer has.

Fix: `HIDDEN_TREE_TOKENS` (/tests, /app/tests, /logs/verifier, ARENA_, /proc/self, /dev/fd) are refused in
a changed file of EVERY source language (`SOURCE_SUFFIXES`: py js mjs cjs ts tsx jsx php go rs rb java kt cs
sh bash zsh swift scala pl lua vue svelte) for EVERY runner type; the Python primitives stay .py-only. The
reviewer prompt says so. Tests: a changed roles.js naming /tests and an env.ts naming ARENA_REPO are
refused, a JS file containing Python primitive text is not, an unchanged roles.js is not scanned, a command
runner refuses a changed run.sh reading /tests. Chain 6 was stopped (its turbomachinery review had failed on
an OpenAI rate limit while chain 4 reviewed next.js concurrently). Execution evidence follows: the Python
reference, the next.js reference, the next.js reference plus a changed backend file reading /tests, smokes.

### Round 6 verified by execution (2026-09-30 15:36-15:47)

Turbomachinery copy: reference 1.0. next.js copy (pytest suite driving a Node child): reference 1.0, 19/19;
the reference plus one changed backend file (`nightgram-web/backend/src/routes/conversations.js`) prefixed
with a read of `/tests/suites/helpers/roles.json` scores 0.0 on both steps -- every pytest check "code the
hidden tests would import reaches into the test process ... nightgram-web/backend/src/routes/
conversations.js: /tests". (A first probe planted nothing because its file search only looked at *.js at
the top level; the record shows "planting into" an empty name and 1.0, which was the reference, not the
route.) `sandbox_smoke` PASS, `--two-steps` PASS, `native_smoke` oracle 1.0 / nop 0.0. Unit suite `Ran 810
tests ... OK`. Records: `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/tasks/bwei40nv7.output`, the re-run in this session's transcript at 15:47,
`/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/ws-suite-forge9.log`. Chain 7 (round 6) re-verifies the three bundles, one at a time, reviews retried on
an OpenAI rate limit, binding screens after passing reviews, no hardening (token expired).

### Round 7 of the runtime (2026-09-30 16:08-16:35): every imported module is in the snapshot

Chain 7 under round 6: turbomachinery controls GREEN (report_forge 0.0, benign_test 1.0), gate GREEN, fresh
review 16:08 REVISE on one shortcut risk -- `pytest.approx` is a public attribute the suites look up at call
time after importing the viewer; the agent's module can rebind it to accept any value; the snapshot covered
`_pytest.*` modules, not the `pytest` package namespace, and not the other libraries a suite calls
(`math.isclose`, numpy). next.js controls GREEN, gate GREEN, fresh review 16:24 PASS, binding screen running
since 16:24. c-010 queued.

Fix: both bootstraps snapshot EVERY module in `sys.modules` -- functions, builtins and class methods, with
`_fsig` identities -- at reporter creation (before any suite import) and again at collection finish for the
modules the suite imported, excluding the hidden suite (fingerprinted separately) and the agent's own tree
(`ARENA_REPO`); at session end every name still present must match, except `ALLOWED_REBINDS` (names the
framework rebinds during an honest session, found by running the reference; `_pytest.config.Config.
_getini_unknown_type` so far). Modules first imported after collection are not verifiable and are not
checked (declared). Tests: 21 in test_report_forgery incl. rebinding `math.isclose` through the unittest
bootstrap -> `tampered` naming it; a module first imported inside a test is no false positive. Execution:
the reference under the full snapshot first (to name the framework's own rebinds), then `pytest.approx` and
`math.isclose` rebind probes.

Token: the refreshed `keys.env` (15:39) authenticates but the Anthropic transport returns HTTP 429 after 5
retries (16:28, 16:31); c-010's hardening was INTERRUPTED without spending; a detached loop retries the launch
every 15 minutes.

### Round 7, first execution (2026-09-30 16:31-16:33): the reference named one legitimate rebind

Under the full-module snapshot the reference scored 0.0 with exactly one mutated name, `warnings.
_showwarnmsg_impl` (pytest's warnings capture patches it per test); the `pytest.approx` probe named
`pytest.approx` plus that one, the `math.isclose` probe named `math.isclose` plus that one. The name joins
`ALLOWED_REBINDS`; re-executing the reference (must be 1.0), both probes (must stay 0.0 on their own names),
the smokes and the native smoke. Unit suite `Ran 847 tests ... OK`.

### Round 7 verified by execution (2026-09-30 16:34-16:37)

With `warnings._showwarnmsg_impl` allowlisted: reference 1.0; the `pytest.approx` rebind 0.0 naming only
`pytest.approx`; the `math.isclose` rebind 0.0 naming only `math.isclose`; `sandbox_smoke` PASS,
`--two-steps` PASS, `native_smoke` oracle 1.0 / nop 0.0; unit suite 847 OK. Record: `/private/tmp/claude-501/-Users-pratyushmohanty-Downloads-arena-envs-tasks-large-dump/c34fba97-ea7e-47ec-b6e0-7c3ef4851470/scratchpad/tasks/bg44y09y0.output`.
Chain 7's next.js screen (round 6) continues on the pool; turbomachinery and c-010 are re-verified under
round 7 after it. The hardening retry loop saw HTTP 429 again at 16:32.

### Chain 7 results (2026-09-30 15:43-17:14)

next.js under round 6: controls GREEN (report_forge 0.0, benign_test 1.0), gate GREEN, review PASS 16:24,
screen 16:49 Fable 1.00/1.00/1.00, Astra 1.00/1.00/1.00, weak-candidate (superseded by round 7; chain 8
re-verifies it). c-010 under ROUND 7 (staged 16:49, after round 7 landed): controls GREEN (report_forge 0.0,
benign_test 1.0), gate GREEN, review PASS 16:57, screen 17:14 Fable p@1 0.40 / p@4 1.00 / mean 0.89, Astra
1.00/1.00/1.00 -- the same 0.40 as the 13:10 screen under round 4: the measurement is stable across the runtime
rounds, as it should be, since the runtime changes never touched the task. c-010 is COMPLETE AND BINDING under
the final runtime. The hardening retry loop's first successful launch hit "no calibration on record" because
chain 7 had just re-staged c-010; relaunched at 17:21 against the binding baseline.
