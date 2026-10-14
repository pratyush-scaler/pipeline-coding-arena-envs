"""The coding designer: a bounded agent that builds a task and PROVES it by execution.

It inherits the parent's shape -- a tool-using loop with a draft protocol
(stage_spec / read_draft / check_draft / submit_draft / reject_seed), per-turn
checkpoint boundaries, resumable sessions and stall detection -- and replaces
what it can see and do:

  * it reads the trace and the WORKSPACE HISTORY: every assistant turn's tree,
    diffs between turns, the corrections the user made, and it chooses the
    message boundary the fixture is lifted from;
  * it has a SANDBOX: it builds the agent image from the current draft and
    runs commands against the fixture, so it can reproduce the defect before
    it writes a single test, and it grades any variant (untouched, reference,
    alternative, a mutant) through the exact shipped grader;
  * it can run advisory probes; submission retains the candidate unchanged,
    and downstream validation decides whether it is valid.

Two boundaries are load-bearing, as in the parent. The agent under test never
sees any of this. And the bundle's own answer stays out of the designer's
public reach except through the tools that record provenance: the accepted
later tree is readable through `workspace_at`/`diff_workspace` precisely so
the reference can cite it, but nothing copies it into `environment/repo`
without a provenance record.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import policy
from ..config import ARENA_EFFORT
from ..policy import (CALIBRATION_TARGET_PASS_AT_1, FAITHFULNESS_TRACE_DERIVED_FLOOR,
                      INSTRUCTION_ASSISTANT_IDIOMS, INSTRUCTION_MAX_BULLET_LINES, MIN_COUPLED_TRAPS,
                      MIN_OBJECTIVES, MIN_PREMISE_TRAPS, MIN_TRAP_TYPES, MIN_TRAPS_PER_TASK,
                      PREMISE_TRAP_TYPES, SCREEN_EPISODES_PER_MODEL, SPEC_HARD_MAX, WEIGHT_TOTAL)
from ..privacy import redact_excerpt, scan as privacy_scan, scan_text as privacy_scan_text
from . import build as build_mod
from . import checks as checks_mod
from . import controls as controls_mod
from . import extract as extract_mod
from . import creation, quality, sandbox, toolchain
from . import traps as traps_mod
from . import validate as validate_mod
from .contracts import ContractError, contained, strict_json
from .outcomes import AuthoringStalled, SeedRejected
from .quality import MIN_PITFALLS_CONSIDERED, QUALITY_PROMPT
from .difficulty import EXTREME_PROMPT, profile as difficulty_profile
from . import levers as levers_mod
from .transport import MODEL, _call
from .native_submission import call_with_retained_submit
from .validate import MAX_GATED_WEIGHT_SHARE, MAX_SINGLE_WEIGHT_SHARE

#: 140 until 2026-09-28. The strengthened workflow adds a pre-review (a step
#: that costs ten minutes) and advisory-driven rewrites; two first drafts in
#: the nine-seed run reached 139 steps before submitting, and a resumed repair
#: stalled at its 78 remaining steps. 200 until 2026-10-03: the census found
#: sessions stalled at 140 and 180, and the lever doctrine (baseline and
#: partial mutants per interlock) and the submit preflight add work.
MAX_STEPS = 240
MAX_CONSECUTIVE_TOOL_ERRORS = 6

#: The conversation is the designer's whole working memory and it only grows.
#: 2026-10-03: a design died at its 181st request with a 2.7 MB conversation
#: (HTTP 400) while 1.6-1.8 MB sessions ran on. Past CONTEXT_COMPACT_BYTES the
#: OLDEST tool results (never the designer's own turns, whose thinking is
#: signed) are cut to an excerpt until the conversation is back under
#: CONTEXT_TARGET_BYTES; the last CONTEXT_KEEP_RECENT messages stay whole.
CONTEXT_COMPACT_BYTES = 1_500_000
CONTEXT_TARGET_BYTES = 1_000_000
CONTEXT_KEEP_RECENT = 40
_ELIDED = ("[earlier tool output elided to keep the conversation inside the model's context window; "
           "re-run the tool if you need it again]\n")


def fit_context(messages: list[dict]) -> int:
    """Shorten the oldest tool results in place; return how many were cut."""
    size = len(json.dumps(messages, ensure_ascii=False))
    if size <= CONTEXT_COMPACT_BYTES:
        return 0
    cut = 0
    for message in messages[:max(0, len(messages) - CONTEXT_KEEP_RECENT)]:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if len(text) <= 1200 or text.startswith(_ELIDED):
                continue
            block["content"] = _ELIDED + text[:400]
            size -= len(text) - len(block["content"])
            cut += 1
            if size <= CONTEXT_TARGET_BYTES:
                return cut
    return cut


#: Transport failures a fresh request can outlive. `transport._call` already
#: retries five times inside one call; 2026-10-03 lost three designs of 170+
#: responses to a streak (two `_StreamInterrupted`, one unlisted mid-stream
#: error type) while sessions beside them ran on. The fix lives here, not in
#: transport.py: that file is part of `artifacts.harness_identity`, and
#: changing it would stale every recorded semantic review.
TRANSIENT_CALL_FAILURES = ("Anthropic stream reported", "Anthropic transport interrupted")
DESIGNER_CALL_ROUNDS = 4
DESIGNER_CALL_PAUSE_S = 90.0


def _designer_call(body: dict) -> dict:
    from ..provider import fatal_provider_error
    for round_ in range(DESIGNER_CALL_ROUNDS):
        try:
            return _call(body)
        except RuntimeError as exc:
            text = str(exc)
            if (round_ + 1 == DESIGNER_CALL_ROUNDS or fatal_provider_error(text)
                    or not any(marker in text for marker in TRANSIENT_CALL_FAILURES)
                    or "invalid_request_error" in text or "authentication_error" in text):
                raise
            time.sleep(DESIGNER_CALL_PAUSE_S * (round_ + 1))
    raise AssertionError("unreachable")

#: Tools an earlier session may have carried that no longer exist. A retained
#: conversation that used one is still resumable; the model is told it is gone.
RETIRED_TOOLS = ("review_design", "evaluate", "run_parser")
TRACE_MESSAGE_CHARS = 6000
TOOL_IO_CHARS = 900
REJECTING_FEEDBACK = ("edit", "disapprove", "escape")
MAX_FIXTURE_WRITE = 512 * 1024

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

#: What the independent reviewer rejected in every first draft of the pilot
#: (seven reviews, 2026-09-28): the recurring shapes, each a validity defect,
#: none of them a difficulty lever. They are read into both mandates so the
#: first draft avoids them instead of spending its repair budget on them.
REVIEWER_OBJECTIONS = (
    "WHAT THE INDEPENDENT REVIEWER REJECTS -- every first draft so far failed on these; write the "
    "tests so that none applies:\n"
    "  1. A test that encodes the reference implementation: an internal attribute, helper or module "
    "name the instruction never publishes; a stub whose conventions only the reference satisfies; an "
    "exact call sequence. Observe behaviour through the published interface, and make sure a third, "
    "differently written correct implementation passes (the alternative patch is the minimum).\n"
    "  2. An unpublished requirement: a tree or hash guard with a file budget, extension allowlist or "
    "location rule the instruction never states; index-by-index equality where the instruction says "
    "'the same result'; a tolerance tighter than the instruction implies; a guard that hashes five "
    "files of a rule about a whole folder. Every assertion traces to its rule_quote sentence, and the "
    "sentence must actually say what the assertion enforces.\n"
    "  3. A forgeable probe: a test that trusts a child process's stdout, an exit status, a marker "
    "file the agent's code can write, or a line printed by the code under test. Assert on returned "
    "values and on state the test process observes itself.\n"
    "  4. Inputs the test never creates: calling a function on a path that does not exist and relying "
    "on a double to ignore it. Create every input the interface needs.\n"
    "  5. A test double that encodes the reference: a fake tool that answers only one flag order, only "
    "on stdout, only for the reference's exact invocation. Doubles accept the real tool's interface.\n"
    "  6. Partial preservation: a later step whose checks drop the earlier step's behaviour; a guard "
    "that covers part of a 'leave X alone' rule. Grade the whole sentence.\n"
    "  7. Private data in the fixture: an account name, a device identifier, a personal path or an "
    "address inside an uploaded log or transcript. Exclude or replace such files before submitting.\n"
    "  8. A mutable oracle (census 2026-10-03: the reviewer's largest theme, 20 of 21 seeds): any "
    "comparison or evidence the agent's code can reach -- pytest.approx or math.isclose it can rebind, a "
    "prototype or global it can patch, a ledger, cache, tmp or marker file it can write, a hidden suite "
    "module it can import or replace, state that leaks between tests. Compute expected values inside the "
    "test from plain data, compare plain values, and give every test fresh inputs. A JS harness that loads "
    "the submission into its own realm is reachable whole: the submission can replace a CONSTRUCTOR "
    "(globalThis.Map, Set, WeakMap, Array, JSON, Reflect), not only a prototype method (three-js v4 WeakMap, "
    "v6 Map: both rejected). Capture every built-in the measurement uses before any submission code runs, "
    "call only the captured references, and attest the global bindings after; or measure from a realm the "
    "submission cannot reach (the test process, a fresh vm context).\n"
    "  9. Harness infidelity: a regex-rewritten ES-module loader, eval of transformed source, jsdom where "
    "the user runs a browser, synthetic Qt/Tk events, a stub runtime. Run the real program the way the "
    "user runs it (the real module system, headless Chromium, QTest/offscreen platform).\n"
    "  10. A mutant that does not do what it claims: it must implement exactly its trap's misreading, fail "
    "exactly the checks it declares (run_controls shows `failed` next to `declared`), pass at least one "
    "objective, and apply cleanly on top of the earlier steps' reference. Re-run run_controls after ANY "
    "test or patch change; a stale mismatch is the most common deterministic rejection.\n"
    "  11. An uncertifiable failure: a task ships only when the genuine-failure audit certifies every counted "
    "failure as the model's, and it recognises pytest, node and go assertions, a JSON report's matcher "
    "failure, or a crash inside the submission. A command-runner suite that prints its own messages must "
    "label an assertion failure, and only an assertion failure, as a line starting 'AssertionError: '; "
    "otherwise every failure stays 'suspect' and the task cannot ship (2026-10-14, wordpress v7: hard "
    "0.0/0.0, all failures uncertified).")

#: User, 2026-10-04: "aim that we meet the score criteria genuinely in the baseline
#: task creation itself ... target to achieve pass@1<=0.5 in the baseline task".
DIFFICULTY_LOOP = (
    "THE DIFFICULTY LOOP (the bar is met HERE, at creation, not by later hardening). Once check_draft is clean "
    "and the controls pass, solve your own draft with BOTH calibration families: blind_solve(model='astra') and "
    "blind_solve(model='fable'). Each sees only the instruction and the repository, exactly as a screen episode "
    "does, and the shipped grader scores what it leaves. GPT-6 Astra's five screen episodes converge on nearly "
    "the same code, so one Astra blind solve is a strong preview of its screen; Fable varies more. For EVERY "
    "step either solver fully passes, the step is too easy: add an interlocked hidden case where that family's "
    "solution is actually wrong -- read its patch, find an input the published instruction already settles on "
    "which its code gives the wrong answer (for Astra: a wider member of a stated class, the same field "
    "elsewhere in a record, the family of a reported case, a second entry point; for Fable: the narrower side "
    "of a rule, a long-tail branch, two rules meeting, state across reruns). Before writing a new case, call "
    "determinacy_probe with that exact input and the answer your reference computes: both families must give "
    "that answer when asked directly; if either answers differently or UNDETERMINED the instruction does not "
    "settle it -- drop the case (never reword the instruction to force it). Prove each case with the reference "
    "and the alternative passing and a mutant writing that family's answer failing; then blind_solve again. "
    "Stop when every step fails for both families, or when the budget is spent; record each solve in "
    "quality.difficulty_case. A failure a solver hits because the task is unclear, under-specified or graded "
    "on something the instruction never stated is a defect, not difficulty.")

AUTHORING_MANDATE = (
    "YOUR MANDATE: on this first attempt, design a coding task that is BOTH of EXCELLENT "
    "QUALITY and EXTREMELY HARD. Quality means it meets every line of the ARENA QUALITY BAR: "
    "an instruction written the way the user wrote it, a repository that starts in exactly "
    "the state the task needs, hidden tests that enforce only what the instruction states, a "
    "reference fix that scores full marks, an untouched repository that scores zero, and no "
    "route to credit that skips the work. Hard means the strict target of pass@1 below "
    f"{CALIBRATION_TARGET_PASS_AT_1} on BOTH calibration models -- aim for <= {policy.AUTHOR_TARGET_PASS_AT_1} or "
    "lower. Neither substitutes for the other: a task that is hard because it is unclear, "
    "under-specified or unfairly graded is broken, and a flawless task a frontier model solves "
    "misses the goal. Do not count on anything making this task harder after you submit: "
    "validity findings may come back for repair, difficulty feedback will not, and later "
    "hardening is manual, limited and not guaranteed. Reproduce the defect in the sandbox "
    "before you write a test; work THE TRAP HUNT and prove every trap with a mutant; run the controls until they are "
    "clean when possible; complete the difficulty self-audit. These checks guide creation and "
    "never prevent submission. Finish writing every task component before ending creation: fixture and "
    "provenance, toolchain and dependency manifests, all step instructions, checks and hidden suites, "
    "reference and alternative patches for every step, traps and their mutant patches, and the self-audit. "
    "Use creation_status to see missing components. Saving an unfinished submission retains the draft "
    "and continues this work within the remaining session budget; it is never a quality rejection. "
    "Retain the strongest candidate you can build; downstream validation "
    "decides validity. Difficulty feedback is advisory, including an easy draft. Reserve reject_seed "
    "for source evidence that no fair coding task exists, never merely a weak difficulty score.\n\n" + REVIEWER_OBJECTIONS
    + "\n\n" + EXTREME_PROMPT + "\n\n" + DIFFICULTY_LOOP)

REVISION_MANDATE = (
    "YOUR MANDATE FOR THIS SESSION: revise the staged draft exactly as the brief above directs "
    "-- the brief governs wherever it is more specific than your general instructions. Keep "
    "every line of the ARENA QUALITY BAR and every genuine trap the draft already has. Add or "
    "change traps only as the brief allows. Measured pass rates in the brief are real "
    "measurements of the previous version; use them. Re-run the controls and complete "
    "quality.difficulty_case for the revised draft. Call reject_seed only if the source cannot "
    "support the task at all -- never because a repair is difficult.\n\n" + REVIEWER_OBJECTIONS)

CATEGORY_GUIDANCE = {
    "F3": ("TARGET LEVEL F3 (Replay). The working tree survived at every turn and the user "
           "pushed back repeatedly. Lift the repository as it was (materialize_fixture at the "
           "turn before the correction you build on), keep every recovered byte you can, and "
           "build the task around the disputes the trace actually contains. Invent almost "
           "nothing in the fixture; plant additional traps on code that is already there."),
    "F2": ("TARGET LEVEL F2 (Scenario). The scenario, the failure mode and the grading criteria "
           "survive, but the fixture may need reconstruction around them. If the user pushed "
           "back while little of the tree survived, the DISPUTES are the asset. If the tree "
           "lifted but the trace carries little pushback, the CODE is the asset: let the task "
           "follow what it actually supports; never manufacture an argument the trace lacks."),
    "F1": ("TARGET LEVEL F1 (Pattern). Too little of the tree survived to rebuild the "
           "scenario, but the user's correction pattern did. Author the repository as a world "
           "that reproduces the mistake the user corrected -- same domain, same stack, same "
           "conventions. Invent the CONTENT, never the SITUATION."),
    "F0": ("TARGET LEVEL F0 (Inspired). Neither the tree nor a correction survived in usable "
           "form. Author the whole fixture inside the situation the trace describes and "
           "reproduce any failure its model actually made; that is what lifts the measured "
           "level to F1 or F2."),
}

TRAP_POLICY = {
    "F3": ("TRAPS -- target F3 Replay: FROM THE TRACE FIRST, THEN PLANT ADDITIONAL traps that "
           "align with it. Turn every usable correction into a trace_derived trap cited with the "
           "user's verbatim words (the defect that persisted, the rule the user restated, the "
           "file the user said to leave alone, the change the user reverted). Then plant "
           "authored traps on code the repository already contains: a second call site with "
           "the same bug, a sibling boundary, a stale comment that contradicts the tests. Keep "
           f"the trace-derived share at or above {FAITHFULNESS_TRACE_DERIVED_FLOOR['F3']:.0%}: "
           "for every 7 trace-derived traps you may add up to 3 authored ones. Never invent a "
           "dispute the trace does not contain. Provenance is never stretched to reach F3: a "
           "mislabelled trap is a quality defect."),
    "F2": ("TRAPS -- target F2 Scenario: TRACE CORRECTIONS FIRST, THEN INVENT INSIDE THE "
           "SCENARIO. Corrections are the strongest material; then invent traps freely, as many "
           "as the code genuinely supports, each marked authored, grounded in the file it lives "
           f"in and aligned to this task. Keep at least {FAITHFULNESS_TRACE_DERIVED_FLOOR['F2']:.0%} "
           "trace-derived when the fixture is mostly reconstructed."),
    "F1": ("TRAPS -- target F1 Pattern: a synthetic repository that reproduces the trace's "
           "failure mode. At least one trap must reproduce the mistake the user corrected and "
           "cite it -- calibration must show models failing that same way -- and the rest may "
           "be invented freely inside this domain and stack, aligned to this task."),
    "F0": ("TRAPS -- target F0 Inspired: everything authored. Invent traps freely, aligned to "
           "this task and grounded in a fixture file; reproduce any failure the trace's model "
           "made; never attribute a trap to the user."),
}

SYSTEM = f"""\
You design CODING benchmark environments from real agent-session material, and you \
use execution to strengthen your design when useful. Submission retains the
candidate; downstream validation decides validity.

THE FIRST PASS IS THE ONLY PASS -- THIS IS YOUR ONE CHANCE: ONE ATTEMPT, TWO \
NON-NEGOTIABLE REQUIREMENTS. This draft must be BOTH EXCELLENT QUALITY and EXTREMELY \
HARD. A hard task that breaks the quality bar is broken: its failures are not the \
model's. A high-quality task a frontier model solves misses the goal. Difficulty comes \
from genuine engineering judgment -- never from ambiguity, hidden requirements, missing \
information, busy work or grader quirks.

THE ARENA QUALITY BAR, clause by clause. (1) The instruction is written the way the \
user wrote it and is COMPLETE: every rule a hidden test enforces is a sentence of it. \
(2) The repository starts in exactly the state the task needs; nothing the agent must \
discover is missing from it, nothing it must not see is left in it. (3) Hidden tests \
enforce only what the instruction states, and grade behaviour, never wording or \
layout. (4) The reference fix scores 1.0 and the untouched repository scores 0. (5) No \
route to credit skips the work: no expected value the agent can read, no test it can \
edit or shadow, no file whose presence alone scores. (6) Dependencies are pinned and \
installed at image build; the verifier is offline. (7) Publish the RULE, never its \
RESULT: the instruction states the rule and the symptom, the tests hold the outcome, \
and nothing between them names the file, the line or the fix.

WHAT A TASK IS HERE. A repository in a defective or incomplete state (`/app/repo`, the \
fixture), an instruction written the way the user wrote it, and HIDDEN TESTS the \
untouched repository fails and the accepted fix passes, run in a separate offline \
verifier after the agent finishes. The reference solution is a PATCH that derives the \
fix by changing source; every expected value is computed by running code, never typed.

HOW HARD, EXACTLY. Strict target: pass@1 below {CALIBRATION_TARGET_PASS_AT_1} on BOTH \
calibration models (Fable 5.1 in Claude Code, GPT 6 Astra in Codex, high effort, no \
turn or time limits, a full shell, no network). Aim past it -- a frontier engineer \
working carefully completes the whole task with pass@1 <= \
{policy.AUTHOR_TARGET_PASS_AT_1} on EACH model -- an advisory design goal, measured later -- \
with the same model's runs DIFFERING (some decisions right, some wrong, partial scores varied; an \
occasional full pass is fine, identical failures every run are not), \
and fails because the JUDGMENT defeated it, \
never because it could not tell what was asked. The bar that keeps that honest -- NOT UNPASSABLE: an \
expert practitioner given ONLY the instruction and the repository must be able to get \
every hidden test right; a task such an expert gets right ZERO times in ten is a \
defect, not difficulty. No pre-judged evidence in the instruction: it names the rule \
and the symptom, never the file, the line or the fix. A task whose whole answer is one \
grep is a search query and called it a task; reject it.

WHAT DEFEATS A FRONTIER MODEL, measured (the CODING DIFFICULTY DOCTRINE at the top \
of this prompt carries the evidence and the lever catalogue; this is its summary):
  - SIZE ALONE is not demonstrated difficulty, and neither is NOTICING: both agents read \
everything they are pointed at, run every visible input and transcribe each rule they \
read. Symptom-vs-cause, chained defects, decoy locations, restraint and announced \
revisions scored 1.00/1.00 here when each was the puzzle.
  - What bit has one shape: a plainly published rule whose REALISATION in code goes \
wrong on a HIDDEN input the visible material never showed. Fable over-reaches a \
closed rule's scope and misses a rare interaction of two rules; Astra codes one exact \
predicate and misses a member in another shape. Each passed the other's steps.
  - So every graded decision is an INTERLOCK of two or more levers on one hidden item, \
aimed at both families, in EVERY step, each proved by an idiomatic-baseline mutant \
and a partial mutant per lever. Visible inputs are the ordinary cases; the hidden \
suite sits where the published rule and the natural code diverge.
  - Same-type traps are correlated: a model that sees the principle once applies it \
everywhere. Vary the levers. Keep each trap's affected population small, and SPREAD the \
difficulty over several INDEPENDENT decisions (LEARNING SIGNAL, in your opening brief): \
full success needs every decision right, so pass@1 stays low, while each decision fails \
on its own and partial progress earns partial reward. Never hang several checks off one \
premise as the whole difficulty -- every run then scores the same.
  - WORKED EXAMPLE (an interlock, from the six-step parser). The user's rule names two \
placeholders to drop, "only those"; another rule says a fenced run is text wherever it \
sits. The visible dumps carry the two placeholders unfenced. A hidden question carries \
a "(Figure: ...)" that belongs to it INSIDE a fenced run that continues on the next \
line. The generic placeholder regex (Fable's habit) deletes the figure text; the exact \
fence predicate (Astra's habit) misses the continuation; fixing only one of them gives \
a different wrong question. m_baseline_ is the literal transcription with those idioms; \
m_partial_scope and m_partial_fence each resolve one lever; all three fail o_questions.

THE TRAP HUNT -- think deeply BEFORE writing a test. Work every family against THIS \
repository and record what you find in quality.difficulty_case.pitfalls_considered:
  symptom_vs_cause, chained_defect, spec_reading, constraint, revert_residue, scope, \
regression, decoy_location, stale_docs, premise_revision, unsatisfiable, environment, \
boundary, unit_or_format, identity, concurrency.
For each candidate: is it genuinely in THIS code and scenario (never a contrivance)? \
Does a published rule settle it? Would a strong engineer plausibly adopt the wrong \
reading? Does the wrong reading change a hidden test's verdict (PROVE it with a \
mutant)? Plant the ones that pass and make them interact; reject the rest with the \
reason. Then red-team your own draft: fix it the way a strong model would on a first quick \
pass, grade that patch in the sandbox, and if it scores full marks the task is not done.

YOU HAVE TOOLS. Use them; never design from assumption.
  - read_trace / list_corrections / read_signals: what the user disputed, with the \
per-turn feedback (edit / disapprove / escape) on every response. Rejected turns are \
the model's failures in this session and the richest trap material.
  - snapshot_boundaries / workspace_at / read_workspace_file / diff_workspace: the \
working tree at EVERY turn. Read the defective state and the state the user accepted. \
The diff between them is the candidate reference; the correction between them is the \
trap. materialize_fixture(msg_index) lifts the tree at a turn into environment/repo \
with byte provenance (the default fixture is the turn before the first correction).
  - list_fixture / read_fixture / write_fixture_file / delete_fixture_file: the fixture \
as the agent will see it. Written files are recorded as authored (zero fidelity \
credit); use them to remove a leaked fix, plant a decoy or add data, never to hide the \
answer somewhere the tests read.
  - sandbox_run(command, patch?): a shell in the agent image against the fixture (or \
the fixture plus a patch). REPRODUCE THE DEFECT HERE before you write a test, and run \
your hidden tests here against the reference and the mutants while you write them.
  - sandbox_grade(variant): the exact shipped grader over nop | golden | alt | \
mutant:<id> -- per-check pass/fail with each failing test's output.
  - run_controls: every executed control (nop 0, golden 1.0 x3 identical, alternative \
1.0, every mutant fails exactly what it declares, empty/destructive 0, tampering on the \
untouched fixture stays 0). Optional during creation; authoritative in validation.
  - pre_review: the INDEPENDENT REVIEWER (a different model) over your staged draft, exactly as the gate runs it after submission -- verdict and every finding. Run it once your controls are clean, address every finding, and run it again if you changed the tests. Advisory, never required; every finding you leave is a repair round you will spend.
  - blind_solve: a DIFFERENT vendor's model fixes every step in sequence from the instruction and repository alone. Inspect independent failures for genuine misreadings or over-fit tests. The bounded probe records cutoffs; one pass is not a success-rate estimate. Optional.
  - probe_patch: exercise an unregistered plausible shortcut or alternative at a named step before deciding how to declare it as a mutant. Earlier steps use the reference; later steps carry its code without repair. Optional; retains per-check diagnostics.
  - difficulty_profile: advisory causal diversity and interaction feedback over any draft.
  - stage_spec / read_draft / creation_status / check_draft / submit_draft / reject_seed: the draft \
protocol. check_draft is optional static feedback; submit_draft retains the candidate
without validation or controls. Complete every component listed by creation_status;
an unfinished submission is saved and creation continues within the remaining budget.
Downstream validation owns every quality rejection.

THE SPEC (stage it in sections; the schema is the `design` tool's):
  name, description, keywords, language, intent, difficulty, novelty, target_level
  toolchain {{name: python|node|browser|go|rust|php|c|shell, extra_apt?, post_install?, \
limits?}} -- deps {{requirements.txt | package.json (+ package-lock.json) | go.mod ... : \
content}}, every version PINNED (==); dependencies install at image build, never at run.
  steps [{{name: "01-...", instruction, new_information? (steps after the first), trace_turn? \
(multi-turn: msg index of the user message this turn reproduces)}}] -- \
the instruction is the user's ask in the user's register, complete: what is broken, \
what the rules are (as criteria, never as code locations), what must not change, where \
the repository is (/app/repo). Every rule a hidden test enforces is a sentence here.
  checks [{{id o_*/g_*, kind objective|guardrail, weight, weight_basis, band \
core|floor|stretch, step, basis, rule_quote [verbatim sentences of that step's \
instruction], trap?, requires?, runner}}] -- runner types: pytest {{select: \
["test_x.py::test_y"]}}, unittest, node-test, jest, vitest, mocha, playwright, gradle, \
maven, cargo, go-test {{cwd: "module-root", select: ["file_test.go::TestName"]}}, \
command {{argv|shell}}, hash-guard {{paths}} (bytes must not change), \
size-guard {{path, min_bytes: "fixture"}}, shadow-guard {{modules: [import names the hidden tests use]}} (fails only on a planted entry that would shadow one of them or hook the harness; never limits legitimate new files), tree-guard {{allow, max_new_files}} (a file budget -- only when the instruction itself restricts new files). A \
guardrail passes on the untouched fixture and must still pass after the fix; a \
violation zeroes the step. An objective fails on the untouched fixture and passes after \
the fix. Tests import the repository through the ARENA_REPO environment variable \
(`sys.path.insert(0, os.environ["ARENA_REPO"])`), never through a hard-coded path; \
hidden inputs live under suites/data/ and must not exist in the repository. \
For Go hidden checks use go-test: it compiles selected private suites in an isolated \
package inside the candidate module and verifies actual completion of every selected \
top-level Test function. Suites may import the module's internal packages. \
For same-package tests, add package: "internal/storage" and optionally overlay: \
{{"private_storage_test.go": "jsonl_test.go"}} to compile the trusted private suite \
in that existing package and replace the named test file only in the compiler's \
overlay; the agent's protected public file stays unchanged. Include helper-only \
private _test.go files in select when selected test functions depend on them. Never \
discover hidden test sources through agent-controlled directories or filesystem scans, \
and never use a command's zero exit status as evidence that Go tests completed.
  suites {{"test_x.py": content, "data/hidden.csv": content}} -- the hidden tests. They \
grade BEHAVIOUR: an independently written correct fix must pass every one of them.
  solution {{step: {{patch (unified diff against the fixture, a/ b/ prefixes), \
commands?, provenance {{origin: trace_accepted|trace_derived|authored, msg?, note}}}}}}
  alternative {{step: {{patch}}}} -- a SECOND correct fix, structurally different \
(different decomposition, different data structure), that must also score 1.0.
  traps [{{id t_*, type, surface, provenance, checks, mutant, beatable_by, \
derived_from/correction_class/quote | grounding/grounded_in}}] -- types: \
{', '.join(traps_mod.TYPES)}.
  mutants [{{id m_*, trap, reason, patch, fails: [check ids]}}] -- a PLAUSIBLE WRONG FIX \
per trap: it must fail exactly the checks it declares, pass at least one objective, and \
never score full credit. A mutant that fails everything is the untouched fixture wearing \
a trap's name.
  quality {{axes, levers, untested_surfaces, decisions, difficulty_case}} (see below).

EVERY TRAP GENUINE, NO CHECK DEAD OR VOID -- checked by executed controls and gates:
  - RESOLVABLE: the rule that settles each trap is stated in the instruction as a \
criterion. A failure caused by a hidden rule or an ambiguous spec is not the model's.
  - LIVE: the trap's mutant fails the checks the trap drives and nothing else.
  - FAILABLE: every core/stretch objective is failed by at least one mutant.
  - PASSABLE: the reference and the alternative both score 1.0; the grader never reads \
the reference's text (a test that quotes a line of the patch is refused).
  - REACHABLE: no `requires` chain hides a check behind something most attempts fail; \
no check may gate more than {MAX_GATED_WEIGHT_SHARE:.0%} of the task.
  - ZERO on the untouched fixture: every objective fails there, every guardrail holds.
  - DETERMINISTIC: no time, network, randomness or ordering dependence; the reference \
grades identically on every run.
  - ISOLATED: the agent image contains no tests and no solution; hidden inputs are \
absent from the repository; the reference's added lines are not readable anywhere the \
agent can look (no .orig/.rej/.git history, no TODO naming the fix).

RUBRIC SHAPE: at least {MIN_OBJECTIVES} objectives (core 4-8, floor 0-3, stretch \
0-4), weights summing to {WEIGHT_TOTAL} per task (the build normalises and splits \
equally across steps), no objective over {MAX_SINGLE_WEIGHT_SHARE:.0%} of the task, \
non-uniform weights with a `weight_basis` each, at least one guardrail that protects \
a real negative obligation the instruction states, and a shadow-guard guardrail in every step naming the modules the hidden tests import (a planted file must never shadow them; a shadow-guard never counts legitimate new files, so the reviewer cannot read it as an unpublished budget -- use a tree-guard only when the instruction itself restricts new files). TRAP FLOORS (refused below them): at \
least {MIN_TRAPS_PER_TASK} traps of at least {MIN_TRAP_TYPES} distinct types; at least \
{MIN_PREMISE_TRAPS} objective-driving PREMISE traps ({', '.join(PREMISE_TRAP_TYPES)}) -- the \
lever that measured; at least {MIN_COUPLED_TRAPS} trap routing two or more objectives \
through one wrong premise; every trap with a mutant and every objective-driving trap \
with a misreading. The floors are a floor: a task that merely clears them is not \
extremely hard, and you are judged by the screen, not by the validator.

WHAT THE INDEPENDENT REVIEWER REJECTS: a test asserting implementation details rather \
than stated behaviour; a requirement no instruction sentence states; a test that could \
pass or fail on timing, ordering or environment; an instruction with two defensible \
fixes that score differently; a reference that is itself wrong; a mutant that is just \
"did nothing"; a fixture that leaks the answer; a repeat of an existing task on new data.

CRITERIA, NOT RECIPES. The instruction states what correct behaviour IS in the domain's \
terms ("a refund reduces the total of the month it lands in") -- never where to change \
code or which test will run. Do not enumerate hidden cases. Publish deliverable paths.

HUMAN VOICE (refused otherwise). The instruction is what THIS user would have typed: \
their register from the trace -- terse or rambling, lowercase, a pasted error, mild \
frustration -- reusing their own sentences from list_corrections wherever they exist, \
with insults and personal data dropped. It reads like a person describing a problem \
they have, not an assistant writing a specification: no markdown headings, at most \
{INSTRUCTION_MAX_BULLET_LINES} bullet or numbered lines, no tables, no "acceptance \
criteria", none of {', '.join(repr(x) for x in INSTRUCTION_ASSISTANT_IDIOMS[:8])}... \
Be VAGUE ABOUT HOW AND WHERE -- no file names to change, no method, no checklist of \
cases, no hint at the cause -- and PRECISE ABOUT WHAT COUNTS: every rule a hidden test \
enforces is still one plain sentence of the instruction (rule_quote), in the user's \
words, so an expert can tell right from wrong while a hurried model can still misread \
it. Vagueness that leaves two defensible fixes scoring differently is a defect, not \
difficulty; vagueness about everything the tests do not grade is the point.

STEP BUDGET (user, 2026-10-03 and 2026-10-12). About 90% of tasks are SINGLE-STEP; the batch \
designates about 10% as MULTI-TURN ({policy.MULTI_STEP_MIN_STEPS} to {policy.MULTI_STEP_MAX_STEPS} steps, \
"at least 3 turns", "extremely dense"). Your opening message states THIS task's budget; validation \
refuses more steps than it and, for a multi-turn task, fewer than {policy.MULTI_STEP_MIN_STEPS}. EVERY \
step -- the only step of a single-step task, and each turn of a multi-turn one -- must be extremely \
hard ON ITS OWN: its own interlocks aimed at both families, pass@1 <= {policy.STEP_PASS_RATE_MAX} on each \
model. A single-step task concentrates the difficulty in one instruction: several interlocked hidden \
decisions, not a checklist. Each step has its own instruction, checks, reference patch (applied on top \
of the previous step's), alternative, traps and mutants.

MULTI-TURN TASKS (Arena spec, Appendix A, binding). "The agent does work, gets a new user message, and \
continues from the result. Do not split one difficult task into artificial turns." So:
  - every turn IS a real user message of the trace, in order: each step carries trace_turn = the msg \
index of the user message it reproduces (strictly increasing). Validation checks it against \
authoring/extraction/user-turns.json. The instruction is that message in the user's own register, made \
complete for this turn. The fixture is the tree BEFORE the trace's assistant answered turn 1 (pivot <= \
turn 1's msg + 1; re-lift it with materialize_fixture(<turn 1 msg>) if the extraction pivot is later), so \
no turn's answer is already in environment/repo.
  - each later turn BUILDS ON the state the previous turn created: it extends, revises or corrects what \
the earlier turn delivered. Executed control skip_prior:<step> applies the step's reference to the \
untouched fixture without the earlier turns; it must NOT score full credit.
  - each turn is SUBSTANTIVE: at least {policy.MIN_OBJECTIVES_PER_STEP} objectives and \
{policy.MIN_TRAPS_PER_STEP} traps of its own (mutant at that turn). Executed control \
prefix_only:<step> applies every earlier turn's reference and nothing new; no objective of the step \
may pass. Preserved behaviour from earlier turns belongs in that step's guardrails; a standing rule \
from an earlier turn is re-tested only on the NEW path this turn creates (standing_rule_carry).
  - every turn is graded on its own rubric before the next turn starts, in a FRESH agent session that \
never saw the earlier prompts: a later instruction never points at an earlier message ("as I said", \
"step 1", "my previous request"); what governs the turn is stated in it or is the behaviour already in \
the tree. Hidden suites grade the tree the step leaves; never read /arena-history (stock Harbor does \
not provide it, so validation refuses it).
  - patch chains: the alternative control applies authoring/alt/<step>.patch files IN ORDER, so a later \
step's alternative is a diff against the tree the EARLIER ALTERNATIVES leave; a later step's mutant is a \
diff against the tree the earlier steps' REFERENCES leave. run_controls before submitting shows a patch \
that does not apply.
  - density: interlock the carry levers (long_horizon_accumulation, standing_rule_carry, \
revision_reach, fresh_session_reconstruction, persistent_state_across_runs) with the measured \
single-step levers on every turn -- both families must be likely to miss something at EVERY turn, \
not only the last. Blind-solve the whole chain; a turn a solver fully passes needs another interlock.

BEFORE submit_draft: THE DIFFICULTY SELF-AUDIT (quality.difficulty_case, required):
  hardest_decision   the one judgment a frontier engineer gets wrong first, and why
  first_try_verdict  why a strong model will NOT score 1.0 on its first attempt; if \
you cannot write this honestly, make the task harder now
  shortcut_test      the smallest patch that scores full marks; if it is one obvious \
line a careful reader sees at once, redesign
  first_try_mutant   the id of the mutant that IS the strong model's first-pass patch \
(fix what the visible repro shows, run the visible case, ship). Write that patch as a \
mutant, sandbox_grade it, and if it scores 1.0 the task is not done; the controls \
execute it and refuse full credit
  misreadings        one per objective-driving trap: {{trap, misreading, why_plausible, \
diverges_on, mutant}} -- the mutant is that trap's own, executed by the controls
  pitfalls_considered  at least {MIN_PITFALLS_CONSIDERED} candidates from the trap \
hunt, each {{pitfall, family, where, decision: planted|rejected, trap|reason}}; every \
trap appears as planted.
  AND quality.lever_plan (the doctrine's interlocks, one entry per hidden decision, every \
step covered, both families aimed at; check_draft audits it, advisory).

WORKFLOW THAT WORKS: (1) read the corrections and the rejected turns; (2) find the \
defect pair in the workspace history and read both trees; (3) materialize_fixture at \
the right turn, remove anything that leaks the fix; (4) stage toolchain + deps, \
sandbox_run the program to REPRODUCE the defect; (5) write the instruction and the \
hidden tests, sandbox_run them against the fixture (they fail) and against the \
reference patch (they pass); (6) write the alternative and a mutant per trap and \
sandbox_grade each when useful; (7) check_draft and run_controls, and read every ADVISORY check_draft returns: each names a shape the independent reviewer rejects (a test using a name only the reference introduces, a file budget no sentence publishes, a test trusting a child process's output, a mutable oracle, asserted DOM structure, an imitated runtime); (8) optionally blind_solve across every step and classify failures; (9) pre_review once the controls are clean -- the same reviewer the gate runs; a batch that skipped it validated 0 of 10 tasks (census 2026-10-03) -- and fix every finding structurally, not locally; (10) self-audit, submit. submit_draft runs a one-time deterministic preflight (static validation and the executed controls) and hands back what validation would reject, so you can fix it within this session. No optional check is required for submission; downstream validation owns rejection.
{QUALITY_PROMPT}
{EXTREME_PROMPT}"""

# The coding difficulty doctrine leads the prompt (user, 2026-10-03): the lever
# evidence and the admission test are what the designer weighs first; the
# validity rules below it are never relaxed by it.
SYSTEM = levers_mod.PROMPT + "\n" + SYSTEM

SPEC_TOOL = {
    "name": "design",
    "description": "Emit the complete coding-task specification. Prefer stage_spec + submit_draft; "
                   "this tool takes the whole spec at once and must be called alone.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "description": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "language": {"type": "string"}, "intent": {"type": "string"},
            "difficulty": {"type": "string"}, "novelty": {"type": "string"},
            "target_level": {"type": "string"},
            "toolchain": {"type": "object", "properties": {
                "name": {"type": "string"},
                "extra_apt": {"type": "array", "items": {"type": "string"}},
                "post_install": {"type": "array", "items": {"type": "string"}},
                "login_shell_path": {"type": "boolean"},
                "limits": {"type": "object"}}},
            "deps": {"type": "object"},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "instruction": {"type": "string"},
                "new_information": {"type": "string"}, "trace_turn": {"type": "integer"}}}},
            "deliverables": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "kind": {"type": "string"}, "step": {"type": "string"}}}},
            "checks": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "kind": {"type": "string"}, "weight": {"type": "number"},
                "weight_basis": {"type": "string"}, "band": {"type": "string"}, "step": {"type": "string"},
                "basis": {"type": "string"}, "rule_quote": {"type": "array", "items": {"type": "string"}},
                "trap": {"type": "string"}, "requires": {"type": "array", "items": {"type": "string"}},
                "runner": {"type": "object"}}}},
            "suites": {"type": "object"},
            "solution": {"type": "object"},
            "alternative": {"type": "object"},
            "traps": {"type": "array", "items": {"type": "object"}},
            "mutants": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "trap": {"type": "string"}, "reason": {"type": "string"},
                "patch": {"type": "string"}, "fails": {"type": "array", "items": {"type": "string"}}}}},
            "quality": {"type": "object"},
            "usable_corrections": {"type": "integer"},
            "solution_notes": {"type": "string"},
        },
        "required": [],  # Partial submissions are retained and guide remaining creation work.
    },
}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _tools(n_msgs: int) -> list[dict]:
    from ..dump.evidence import TOOLS
    obj = {"type": "object", "properties": {}, "required": []}
    return TOOLS + [
        {"name": "list_corrections", "description": "The seed's correction moments verbatim, with citations for "
         "trap provenance, and every response the user EDITED, DISAPPROVED or ESCAPED with what the user said next.",
         "input_schema": obj},
        {"name": "read_trace", "description": f"Read the session transcript (0..{n_msgs - 1}), up to 3 messages per "
         "call, with per-turn feedback, undo/error markers and every tool call's input and output (clipped inline).",
         "input_schema": {"type": "object", "properties": {"start": {"type": "integer"}, "end": {"type": "integer"},
                                                           "char_offset": {"type": "integer"}}, "required": ["start", "end"]}},
        {"name": "read_signals", "description": "Arena's outcome signals mapped to message indices, feedback counts, "
         "session stats and the task split. Discovery aids, never the reward.", "input_schema": obj},
        {"name": "snapshot_boundaries", "description": "Every message boundary at which the working tree changed: "
         "{msg_index, role, files, bytes}. Use it to pick the turn the fixture is lifted from.", "input_schema": obj},
        {"name": "workspace_at", "description": "The working tree at a message boundary: paths, bytes, whether text.",
         "input_schema": {"type": "object", "properties": {"msg_index": {"type": "integer"}}, "required": ["msg_index"]}},
        {"name": "read_workspace_file", "description": "Read one file of the working tree at a message boundary.",
         "input_schema": {"type": "object", "properties": {"msg_index": {"type": "integer"}, "path": {"type": "string"},
                                                           "char_offset": {"type": "integer"}, "limit": {"type": "integer"}},
                          "required": ["msg_index", "path"]}},
        {"name": "diff_workspace", "description": "Paths added/removed/modified between two message boundaries, with "
         "unified diffs for text files (optionally one path).",
         "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"},
                                                           "path": {"type": "string"}}, "required": ["a", "b"]}},
        {"name": "materialize_fixture", "description": "Replace environment/repo with the working tree at a message "
         "boundary (the state BEFORE that message's changes are applied: pass the assistant turn whose defective "
         "state you want, and the tree as of that turn is lifted). Records byte provenance. Vendored directories are "
         "excluded; `exclude` adds glob patterns.",
         "input_schema": {"type": "object", "properties": {"msg_index": {"type": "integer"},
                                                           "exclude": {"type": "array", "items": {"type": "string"}}},
                          "required": ["msg_index"]}},
        {"name": "list_fixture", "description": "Every file in environment/repo with bytes and language.", "input_schema": obj},
        {"name": "read_fixture", "description": "Read a fixture file (line range or char_offset paging).",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"},
                                                           "limit": {"type": "integer"}, "char_offset": {"type": "integer"}},
                          "required": ["path"]}},
        {"name": "write_fixture_file", "description": "Create or overwrite a fixture file (recorded as AUTHORED, zero "
         "fidelity credit). Never write the fix, a hint to it, or test data the hidden tests use.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                                                           "reason": {"type": "string"}}, "required": ["path", "content", "reason"]}},
        {"name": "delete_fixture_file", "description": "Remove a fixture file (e.g. a leaked patch or a later artefact).",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                          "required": ["path", "reason"]}},
        {"name": "sandbox_run", "description": "Run a shell command in the agent image against the current fixture "
         "(cwd /app/repo, user agent, no network), optionally after applying a unified-diff patch. Needs `toolchain` "
         "(and `deps`) staged. Returns exit code, stdout, stderr.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "patch": {"type": "string"},
                                                           "timeout_s": {"type": "integer"}}, "required": ["command"]}},
        {"name": "sandbox_grade", "description": "Grade a variant of the current draft through the exact shipped "
         "grader: nop | golden | alt | mutant:<id>. Needs a complete draft (checks, suites, solution). Returns reward, "
         "per-check flags and each failing check's output tail.",
         "input_schema": {"type": "object", "properties": {"variant": {"type": "string"}}, "required": ["variant"]}},
        {"name": "probe_patch", "description": "Advisory execution of an unregistered plausible shortcut or correct "
         "alternative at one step. Applies earlier reference steps and carries this candidate through later steps "
         "without repairing it. Returns per-check failures and intended-failure differences. Never required to submit.",
         "input_schema": {"type": "object", "properties": {"step": {"type": "string"}, "patch": {"type": "string"},
                          "hypothesis": {"type": "string"}, "expected_fails": {"type": "array", "items": {"type": "string"}}},
                          "required": ["step", "patch"]}},
        {"name": "run_controls", "description": "Run every executed control over the current draft (nop, golden x3, "
         "alternative, every mutant, empty, destructive, test_tamper, coverage). Returns errors and advisories.",
         "input_schema": obj},
        {"name": "pre_review", "description": "Run the INDEPENDENT REVIEWER (a different model) over your staged draft, "
         "exactly as the gate will after submission: verdict, every finding, harness problems. Needs a draft that "
         "passes check_draft. Costs one review; at most MAX_PRE_REVIEWS_PER_SESSION per session. Advisory: submit_draft "
         "never requires it, but every finding you leave is a repair round you will spend.",
         "input_schema": obj},
        {"name": "blind_solve", "description": "A calibration model fixes the task from the instruction and the "
         "repository alone (it never sees tests, solution or traps); the shipped grader scores its diff. model: "
         "'astra' (GPT-6 Astra) or 'fable' (Fable 5.1); default the other vendor. Returns the diff and per-check "
         "results. Every check it fails is either a trap (record the misreading) or an over-fit test (rewrite "
         "it); every step it fully passes is too easy (THE DIFFICULTY LOOP). At most policy.BASELINE_BLIND_SOLVES "
         "per session; advisory, never required.",
         "input_schema": {"type": "object", "properties": {"model": {"type": "string", "enum": ["astra", "fable"]}},
                          "required": []}},
        {"name": "determinacy_probe", "description": "Ask BOTH calibration families one direct question about one "
         "concrete input, from the step's instruction alone. `expected` is what the reference computes. "
         "determinate=true only when every family answers exactly that; anything else means the instruction does "
         "not settle the case -- drop it. Recorded for the reviewer.",
         "input_schema": {"type": "object", "properties": {
             "case": {"type": "string", "description": "the concrete input, described exactly"},
             "question": {"type": "string", "description": "one question with a short answer"},
             "expected": {"type": "string", "description": "the reference's answer, in the answer form"},
             "answer_form": {"type": "string", "description": "e.g. 'yes or no', 'an integer', 'a JSON list'"},
             "context": {"type": "string", "description": "verbatim repository excerpt the agent would also see"},
             "step": {"type": "string"}},
             "required": ["case", "question", "expected", "answer_form"]}},
        {"name": "stage_spec", "description": "Assemble the spec incrementally: set a nested field by key/index path; "
         "append=true extends an array or updates an object. Empty path replaces the root.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "array", "items": {}}, "value": {},
                                                           "append": {"type": "boolean"}}, "required": ["path", "value"]}},
        {"name": "read_draft", "description": "Read a section of your staged draft (empty path lists sections).",
         "input_schema": {"type": "object", "properties": {"path": {"type": "array", "items": {}}}, "required": []}},
        {"name": "check_draft", "description": "Static validation of the complete draft against the fixture.", "input_schema": obj},
        {"name": "creation_status", "description": "List task components still to write. Checks presence only, "
         "without validity or difficulty gates, Docker or model calls. Partial work is retained.", "input_schema": obj},
        {"name": "difficulty_profile", "description": "Advisory causal mechanism and mutant diversity feedback over "
         "any staged draft, including incomplete drafts, plus the LEVER DOCTRINE audit of quality.lever_plan "
         "(interlocks per step, both families aimed at, baseline and partial mutants, announcing words, "
         "hidden data quoted in an instruction). Never blocks creation or submission.", "input_schema": obj},
        {"name": "generate_cases", "description": "Generate deterministic intersection inputs or action histories. "
         "No answers are generated. Returns a private case file and coverage gaps; optional even on incomplete drafts.",
         "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["intersections", "sequences", "checkpoints"]},
             "dimensions": {"type": "object"}, "actions": {"type": "array", "items": {"type": "object"}},
             "prefixes": {"type": "array", "items": {"type": "array", "items": {"type": "object"}}},
             "length": {"type": "integer"}, "count": {"type": "integer"}, "limit": {"type": ["integer", "null"]},
             "seed": {"type": "integer"}}, "required": ["mode"]}},
        {"name": "independent_oracle", "description": "A different vendor independently derives an implementation "
         "from public instructions and repository only, before any grading. Needs staged toolchain and steps, "
         "not tests or reference. Returns patches and provenance to use as the alternative. Optional; submission remains available.",
         "input_schema": obj},
        {"name": "read_generated_cases", "description": "Page through a private case-generator result without putting "
         "hidden inputs in the agent fixture.", "input_schema": {"type": "object", "properties": {
             "file": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["file"]}},
        {"name": "stage_research_variants", "description": "Retain four factorial experiment drafts and a deferred "
         "ablation plan outside the task image. Optional; never builds, validates or calls solvers. "
         "Design supplies decoy_files, interaction_suites, optional spec_overrides for 00/10/01/11, "
         "and treatment {decoy, interaction, public_disambiguation}.",
         "input_schema": {"type": "object", "properties": {"design": {"type": "object"}, "episodes": {"type": "integer"}},
                          "required": ["design"]}},
        {"name": "submit_draft", "description": "Save the draft. Finish creation when all task components have "
         "been written; otherwise continue writing the missing components within the remaining budget. "
         "Validation and controls run downstream; no passed check, probe or difficulty score is required. Call alone.", "input_schema": obj},
        {"name": "reject_seed", "description": "Submit the retained draft with a source-capacity finding when "
         "no fair coding task is possible. The first valid call ends creation; validation decides rejection. "
         "`reason` >= 200 characters; `source_evidence` "
         "lists {file, quote} passages copied verbatim from fixture files or trace messages (file = fixture path or "
         "'msg<N>'). Call alone.",
         "input_schema": {"type": "object", "properties": {"reason": {"type": "string"},
                                                           "source_evidence": {"type": "array", "minItems": 1, "items": {
                                                               "type": "object", "properties": {"file": {"type": "string"}, "quote": {"type": "string"}},
                                                               "required": ["file", "quote"], "additionalProperties": False}}},
                          "required": ["reason", "source_evidence"], "additionalProperties": False}},
        SPEC_TOOL,
    ]


def _compact(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())


def _clip(text: str, limit: int = TOOL_IO_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f" ...[{len(text) - limit} more chars]"


def _render_message(msg: dict, limit: int | None = TRACE_MESSAGE_CHARS) -> str:
    parts = []
    feedback = msg.get("feedback")
    if isinstance(feedback, dict) and feedback:
        parts.append("[USER FEEDBACK on this response: " + ", ".join(f"{k}={v}" for k, v in feedback.items()) + "]")
    if msg.get("undone") is True:
        parts.append("[UNDONE: the user undid this message]")
    if msg.get("error"):
        error = msg["error"]
        detail = (error.get("friendlyMessage") or error.get("errorCategory") if isinstance(error, dict) else error)
        parts.append(f"[ERROR on this message: {detail}]")
    content = msg.get("content")
    if isinstance(content, str):
        parts.append(content)
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind.startswith("tool-"):
            parts.append(f"[tool {kind[5:]} ({block.get('state') or 'no state'}) input: "
                         f"{_clip(_compact(block.get('input')))} | output: {_clip(_compact(block.get('output')))}]")
        elif kind in ("file", "image"):
            parts.append(f"[{kind} {block.get('filename') or block.get('mediaType') or ''}]")
    text = redact_excerpt(" ".join(p for p in parts if p))
    return text if limit is None else text[:limit]


def _require_shape(value, schema: dict, where: str) -> None:
    kind = schema.get("type")
    types = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
             "boolean": type(value) is bool, "integer": type(value) is int,
             "number": type(value) in (int, float) and type(value) is not bool, "null": value is None}
    if kind in types and not types[kind]:
        hint = " Pass the object or array directly, not a JSON-encoded string." \
            if isinstance(value, str) and kind in {"object", "array"} else ""
        raise ValueError(f"{where}: expected JSON {kind}, got {type(value).__name__}.{hint}")
    if isinstance(value, dict):
        for key, item_schema in schema.get("properties", {}).items():
            if key in value:
                _require_shape(value[key], item_schema, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_shape(item, schema.get("items", {}), f"{where}[{index}]")


def _draft_field_schema(path: list) -> dict:
    schema = SPEC_TOOL["input_schema"]
    for key in path:
        schema = (schema.get("items", {}) if type(key) is int else schema.get("properties", {}).get(key, {}))
    return schema


def _fixture_language(path: str) -> str | None:
    from ..config import LANGUAGE_BY_EXT
    return LANGUAGE_BY_EXT.get(Path(path).suffix.lower())


#: blind_solve's model choices: the two calibration families.
BLIND_FAMILIES = {"astra": "gpt-6-astra", "fable": "claude-fable-5-1"}


def _family_of(model: str | None) -> str:
    return next((family for family, name in BLIND_FAMILIES.items() if name == str(model).split("/")[-1]), "other")


def _blind_budget() -> int:
    from ..config import MAX_BLIND_SOLVES_PER_SESSION
    return max(MAX_BLIND_SOLVES_PER_SESSION, policy.BASELINE_BLIND_SOLVES)


#: Tools of a hidden-only hardening session (`fastharden`): only registered when
#: the row carries the screen's captured episodes, so ordinary designs keep
#: their tool list (a changed list invalidates a stalled session's continuation).
FAST_TOOLS = [
    {"name": "regrade_episodes", "description": "Grade EVERY captured screen episode (both models) with the current "
     "draft's hidden tests. Returns per model/step how many still fully pass (IN-SAMPLE, provisional), each failing "
     "check's count and how it failed (assertion/repo_crash/output = genuine; interface/suspect = not genuine). "
     "Needs a draft that passes check_draft. Takes minutes.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "read_episode", "description": "One captured episode: without path, the files it changed against the "
     "fixture with a diff; with path, that file of its final tree. model is 'fable' or 'astra'; index is 1-based.",
     "input_schema": {"type": "object", "properties": {"model": {"type": "string"}, "index": {"type": "integer"},
                                                       "path": {"type": "string"}, "offset": {"type": "integer"}},
                      "required": ["model", "index"]}},
    {"name": "coverage_critic", "description": "Run the SHIP-TIME coverage critic over your staged draft: a model "
     "writes up to six wrong implementations of published clauses (starting from the reference) and the shipped "
     "grader scores each. Every returned gap scores 1.0 while violating a published clause -- close it with a hidden "
     "case settled by determinacy_probe (and its m_partial_ mutant), or record the probe's undetermined evidence under "
     "untested_surfaces. The final version runs the same critic and its gaps block shipping, so run it before you "
     "submit. Needs a draft that passes check_draft; at most policy.DESIGNER_CRITIC_RUNS per session; takes minutes.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


def _handlers(bundle: Path, extraction, trace: dict, draft_path: Path | None = None, *,
              level: str | None = None, row: dict | None = None,
              log: Path | None = None) -> dict[str, Callable]:
    row = row or {}
    log = Path(log) if log is not None else Path(bundle) / "authoring" / "log"
    repo = bundle / "environment" / "repo"
    messages = trace.get("messages", [])
    session_id = extraction.session_id
    root = getattr(extraction, "source_root", None)
    if root is None:
        from ..config import sessions_root
        root = sessions_root()
    catalog = None

    def evidence():
        nonlocal catalog
        if catalog is None:
            from ..dump.evidence import Catalog
            from ..dump.recover import recover
            catalog = Catalog(trace, recover(trace, root), root, extraction.span, extraction.pivot)
        return catalog

    draft: dict = {}
    if draft_path and draft_path.is_file():
        draft = strict_json(draft_path.read_text())
    authored_path = bundle / "authoring/extraction/fixture-authored.json"

    def _authored() -> dict:
        return strict_json(authored_path.read_text()) if authored_path.is_file() else {"schema": "arena-coding/fixture-authored/1", "files": {}}

    def _record_authored(rel: str, reason: str, sha: str | None) -> None:
        from ..artifacts import atomic_json
        doc = _authored()
        if sha is None:
            doc["files"].pop(rel, None)
            doc.setdefault("deleted", {})[rel] = reason
        else:
            doc["files"][rel] = {"reason": reason, "sha256": sha}
        atomic_json(authored_path, doc)

    def _persist_draft():
        if draft_path:
            from ..artifacts import atomic_json
            atomic_json(draft_path, draft)

    def retain_draft(spec):
        nonlocal draft
        draft = copy.deepcopy(spec)
        _persist_draft()

    def creation_status(**_):
        spec = draft_spec()
        spec.setdefault("source", {"session_id": session_id, "seed_index": extraction.seed_index,
                                   "tier": row.get("tier", 4)})
        spec.setdefault("target_level", level or "")
        return creation.inventory(spec, bundle)

    # -- draft protocol ------------------------------------------------------
    def draft_spec():
        return copy.deepcopy(draft)

    def read_draft(path=None, **_):
        if not path:
            return {"sections": list(draft), "sizes": {k: len(json.dumps(v, default=str)) for k, v in draft.items()}}
        cur = draft
        for key in path:
            if type(key) not in (str, int):
                raise ValueError("draft paths contain only keys and integer indexes")
            cur = cur[key]
        return copy.deepcopy(cur)

    def stage_spec(path, value, append=False, **_):
        nonlocal draft
        if not isinstance(path, list) or any(type(k) not in (str, int) for k in path):
            raise ValueError("draft path must be a list of keys or integer indexes")
        _require_shape(value, _draft_field_schema(path), "draft" + "".join(f"[{k!r}]" for k in path))
        from .patch_format import normalize_staged_patches
        value, patch_format_updates = normalize_staged_patches(path, value)
        changed = copy.deepcopy(draft)
        if not path:
            if not isinstance(value, dict):
                raise ValueError("spec root must be an object")
            changed = copy.deepcopy(value)
        else:
            cur = changed
            for key in path[:-1]:
                if isinstance(cur, dict) and key not in cur and isinstance(key, str):
                    cur[key] = {}
                cur = cur[key]
            key = path[-1]
            existing = (cur.get(key) if isinstance(cur, dict)
                        else cur[key] if isinstance(key, int) and -len(cur) <= key < len(cur) else None)
            if isinstance(existing, (dict, list)) and isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except ValueError:
                    decoded = None
                if isinstance(decoded, (dict, list)):
                    raise ValueError(f"draft{''.join(f'[{k!r}]' for k in path)}: expected JSON object or array, "
                                     "not a JSON-encoded string -- pass the structure itself")
            if append:
                if isinstance(cur.get(key) if isinstance(cur, dict) else cur[key], list) and isinstance(value, list):
                    cur[key].extend(copy.deepcopy(value))
                elif isinstance(cur.get(key) if isinstance(cur, dict) else cur[key], dict) and isinstance(value, dict):
                    cur[key].update(copy.deepcopy(value))
                else:
                    raise ValueError("append requires matching arrays or objects")
            else:
                cur[key] = copy.deepcopy(value)
        # Every toolchain staged from now on re-exports its PATH for login
        # shells (toolchain.LOGIN_SHELL_PATH_LAYER: Codex's `sh -lc` lost the
        # venv and go in 5/5 Astra episodes); an explicit false is kept.
        if (not path or path[0] == "toolchain") and isinstance(changed.get("toolchain"), dict):
            changed["toolchain"].setdefault("login_shell_path", True)
        draft = changed
        _persist_draft()
        response = {"stored": path, "sections": list(draft)}
        if patch_format_updates:
            response["patch_header_normalization"] = patch_format_updates
        return response

    # -- trace ---------------------------------------------------------------
    def user_feedback():
        lo, hi = getattr(extraction, "span", (0, max(len(messages) - 1, 0)))
        rows = []
        for i, msg in enumerate(messages):
            verdict = (msg.get("feedback") or {}).get("check_in") if isinstance(msg.get("feedback"), dict) else None
            if verdict not in REJECTING_FEEDBACK:
                continue
            reply = next((m for m in messages[i + 1:] if m.get("role") == "user"), None)
            rows.append({"msg": i, "cite": f"{session_id}::msg{i}", "user_verdict": verdict,
                         "in_seed_span": lo <= i <= hi, "response_excerpt": _render_message(msg, limit=900),
                         "user_said_next": _render_message(reply, limit=2400) if reply else None})
        return rows

    def list_corrections(**_):
        return {"user_feedback": user_feedback(), "corrections": [
            {"cite": f"{session_id}::msg{m['msg']}", "signal": m.get("signal"),
             "user": redact_excerpt(m.get("user") or ""),
             "agent_had_just_said": " ".join(redact_excerpt(m.get("prior_assistant") or "").split())[:2400]}
            for m in extraction.moments],
            "note": "A trace_derived trap cites `derived_from` as '<session>::msgN' (the ASSISTANT turn the user "
                    "corrected) and quotes the user's own words from the NEXT user message verbatim. Signals are "
                    "discovery aids, not correction classes: classify the exchange yourself."}

    def read_signals(**_):
        from ..dump import signals as sig
        node_to_msg = {n: i for i, m in enumerate(messages) for n in (m.get("node_ids") or [])}
        try:
            outcomes = sig.load_outcomes(session_id, root)
        except (OSError, ValueError) as exc:
            outcomes = {"outcomes": [], "error": f"{type(exc).__name__}: {exc}"}
        grouped: dict = {}
        for r in outcomes.get("outcomes") or []:
            name = r.get("signal_name")
            entry = grouped.setdefault(name, {"counts": {}, "rows": []})
            entry["counts"][r.get("outcome")] = entry["counts"].get(r.get("outcome"), 0) + 1
            if name == "tool_hallucination" and r.get("outcome") == "success":
                continue
            node = str(r.get("observation_id") or r.get("annotating_node_id") or "").split("::")[0]
            if len(entry["rows"]) < 60:
                entry["rows"].append({"msg": node_to_msg.get(node), "outcome": r.get("outcome")})
        feedback: dict = {}
        for msg in messages:
            verdict = (msg.get("feedback") or {}).get("check_in") if isinstance(msg.get("feedback"), dict) else None
            if verdict:
                feedback[verdict] = feedback.get(verdict, 0) + 1
        tasks = [{"task_index": t.get("task_index"), "n_turns": t.get("n_turns"),
                  "messages": sorted({node_to_msg[n] for n in (t.get("turn_ids") or []) if n in node_to_msg})}
                 for t in trace.get("tasks") or []]
        return json.loads(redact_excerpt(json.dumps({
            "signals": grouped, "outcomes_error": outcomes.get("error"), "user_feedback_counts": feedback,
            "session_stats": trace.get("stats"), "tasks": tasks, "seed_span": list(extraction.span),
            "pivot": getattr(extraction, "pivot", None),
            "note": "Arena's per-turn signals and check-in verdicts are discovery material for traps: "
                    "never use it as the reward, a check or a claim about difficulty."}, ensure_ascii=False)))

    def read_trace(start: int, end: int, char_offset: int = 0, **_):
        lo = max(int(start), 0)
        hi = min(int(end), len(messages), lo + 3)
        offset = max(0, int(char_offset))

        def entry(i):
            text = _render_message(messages[i], limit=None)
            stop = min(len(text), offset + 14000)
            return {"i": i, "role": messages[i].get("role"), "text": text[offset:stop],
                    "next_char_offset": stop if stop < len(text) else None}
        return {"messages": [entry(i) for i in range(lo, hi)], "next_message": hi if hi < len(messages) else None,
                "total_messages": len(messages), "seed_span": list(extraction.span), "pivot": extraction.pivot}

    # -- workspace history ---------------------------------------------------
    def snapshot_boundaries(**_):
        return {"boundaries": extract_mod.snapshot_boundaries(session_id, root),
                "fixture_pivot": extraction.pivot,
                "note": "workspace_at(i) is the tree AFTER message i's changes. The default fixture is the tree at "
                        "pivot-1 (before the first correction). materialize_fixture(i) lifts the tree as of message i."}

    def workspace_at(msg_index: int, **_):
        docs = extract_mod.at_message(session_id, int(msg_index), root)
        return {"msg_index": int(msg_index), "files": [{"path": d.path, "bytes": d.bytes, "text": d.content is not None,
                                                        "trace_path": d.trace_path} for d in docs],
                "count": len(docs), "bytes": sum(d.bytes for d in docs)}

    def read_workspace_file(msg_index: int, path: str, char_offset: int = 0, limit: int = 40000, **_):
        docs = {d.path: d for d in extract_mod.at_message(session_id, int(msg_index), root)}
        doc = docs.get(path)
        if doc is None:
            raise FileNotFoundError(f"{path!r} is not in the tree at message {msg_index}")
        if doc.content is None:
            return {"path": path, "binary": True, "bytes": doc.bytes, "sha256": doc.sha256}
        text = redact_excerpt(doc.content)
        begin = max(0, int(char_offset))
        end = min(len(text), begin + max(1, min(int(limit), 40000)))
        return {"path": path, "msg_index": int(msg_index), "showing_characters": [begin, end],
                "next_char_offset": end if end < len(text) else None, "text": text[begin:end]}

    def diff_workspace(a: int, b: int, path: str | None = None, **_):
        out = extract_mod.diff(session_id, int(a), int(b), root)
        if path:
            return {"path": path, "diff": redact_excerpt(out["diffs"].get(path, "")) or None,
                    "status": "modified" if path in out["modified"] else "added" if path in out["added"]
                    else "removed" if path in out["removed"] else "unchanged"}
        diffs = {p: redact_excerpt(d)[:6000] for p, d in list(out["diffs"].items())[:12]}
        return {"added": out["added"], "removed": out["removed"], "modified": out["modified"], "diffs": diffs,
                "note": "diffs are clipped; ask for one path to read it whole"}

    def materialize_fixture(msg_index: int, exclude=None, **_):
        import fnmatch
        ex = extract_mod.extract(session_id, extraction.seed_index, root, pivot=int(msg_index) + 1)
        if exclude:
            ex.before = [d for d in ex.before if not any(fnmatch.fnmatch(d.path, pat) for pat in exclude)]
        if repo.exists():
            shutil.rmtree(repo)
        report = extract_mod.write(ex, bundle)
        if authored_path.is_file():
            authored_path.unlink()
        extraction.pivot = ex.pivot
        extraction.before = ex.before
        # The initial extraction is redacted by the orchestrator before the
        # design starts; a re-lifted tree must get the same pass, or private
        # data the trace carries comes straight back (c-002, 2026-09-28: an
        # email in uploads/index.html returned at message 33 and killed the
        # repair session at the next design's opening scan).
        privacy_note = None
        try:
            from ..ledger.redaction import minimize
            minimize(bundle, session_id, root)
        except ValueError as exc:
            privacy_note = f"redaction could not repair every finding: {exc}"[:600]
        findings = privacy_scan(repo)
        return {"fixture_files": len(report["fixture_files"]), "fixture_bytes": report["fixture_bytes"],
                "pivot": ex.pivot, "skipped": ex.stats.get("skipped_before"),
                "privacy_findings": findings, "privacy_note": privacy_note,
                "note": "environment/repo replaced; provenance recorded. Rebuild images with sandbox_run."
                        + (" PRIVATE DATA REMAINS in the files listed under privacy_findings: exclude them "
                           "(materialize_fixture exclude=[...]) or replace them with write_fixture_file. "
                           "Submission retains the candidate; validation rejects residual findings." if findings else "")}

    # -- fixture -------------------------------------------------------------
    def _fixture_path(rel: str) -> Path:
        try:
            return contained(repo, rel)
        except ContractError as exc:
            raise PermissionError(f"{rel!r}: {exc}") from None

    def list_fixture(**_):
        rows = []
        for path in sorted(repo.rglob("*")):
            if path.is_file():
                rel = path.relative_to(repo).as_posix()
                rows.append({"path": rel, "bytes": path.stat().st_size, "language": _fixture_language(rel)})
        authored = _authored()
        return {"files": rows, "count": len(rows), "authored": sorted(authored.get("files", {})),
                "note": "Lifted files carry byte provenance; authored files carry zero fidelity credit."}

    def read_fixture(path: str, offset: int = 1, limit: int = 4000, char_offset=None, **_):
        target = _fixture_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"{path!r} is not a fixture file")
        data = target.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"path": path, "binary": True, "bytes": len(data)}
        text = redact_excerpt(text)
        lines = text.splitlines(keepends=True)
        start = max(int(offset) - 1, 0)
        begin = max(0, int(char_offset)) if char_offset is not None else sum(map(len, lines[:start]))
        end = min(len(text), begin + 40000)
        if char_offset is None:
            end = min(end, begin + sum(map(len, lines[start:start + max(1, int(limit))])))
        return {"path": path, "total_lines": len(lines), "showing_characters": [begin, end],
                "truncated": end < len(text), "next_char_offset": end if end < len(text) else None,
                "text": text[begin:end]}

    def write_fixture_file(path: str, content: str, reason: str, **_):
        if not isinstance(reason, str) or len(reason) < 15:
            raise ValueError("reason must say why this authored file belongs in the fixture (>= 15 chars)")
        if extract_mod.vendored(path):
            raise ValueError("vendored directories are rebuilt from the dependency manifest, not written")
        if len(content.encode("utf-8")) > MAX_FIXTURE_WRITE:
            raise ValueError("fixture writes are capped at 512 KiB")
        target = _fixture_path(path)
        if findings := privacy_scan_text(path, content):
            raise ValueError(f"the content carries private data and was not written: {findings}; "
                             "use placeholder values (example.com addresses, 555 numbers)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        _record_authored(path, reason, sha)
        return {"written": path, "bytes": len(content.encode("utf-8")), "origin": "authored", "fidelity_credit": 0}

    def delete_fixture_file(path: str, reason: str, **_):
        target = _fixture_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"{path!r} is not a fixture file")
        target.unlink()
        _record_authored(path, reason, None)
        return {"deleted": path}

    # -- sandbox -------------------------------------------------------------
    scratch_root = log / "scratch"
    scratch_n = {"n": 0}

    def _scratch(complete: bool) -> Path:
        scratch_n["n"] += 1
        dest = scratch_root / f"{scratch_n['n']:03d}"
        if dest.exists():
            shutil.rmtree(dest)
        (dest / "environment").mkdir(parents=True)
        shutil.copytree(repo, dest / "environment/repo")
        spec = draft_spec()
        spec.setdefault("source", {"session_id": session_id, "seed_index": extraction.seed_index,
                                   "tier": row.get("tier", 4)})
        spec.setdefault("target_level", level or "")
        spec.setdefault("_producer", {"model": MODEL, "effort": ARENA_EFFORT})
        # Match submission defaults before running expensive controls. These
        # generated declarations must not drift solely because finish() ran.
        spec.setdefault("usable_corrections", extract_mod.usable_correction_count(row, extraction))
        spec.setdefault("solution_notes", "The reference applies a patch derived from the trace's accepted "
                        "state (or authored, as its provenance says) and the hidden tests compute their "
                        "verdict from the changed program.")
        if complete:
            build_mod.build(spec, dest)
        else:
            build_mod.environment_only(spec, dest)
        # Keep only the last three scratch exports.
        older = sorted(p for p in scratch_root.iterdir() if p.is_dir())[:-3]
        for p in older:
            shutil.rmtree(p, ignore_errors=True)
        return dest

    def sandbox_run(command: str, patch: str | None = None, timeout_s: int = 600, **_):
        ok, why = sandbox.available()
        if not ok:
            return {"error": why}
        try:
            dest = _scratch(complete=False)
        except (ContractError, ValueError) as exc:
            return {"error": str(exc)}
        setup = sandbox.Variant("patch", patch=patch, label="sandbox_run") if patch else None
        try:
            result = sandbox.shell(dest, command, timeout=float(min(int(timeout_s), 1800)), setup=setup,
                                   environment_only=True)
        except sandbox.SandboxError as exc:
            return {"error": str(exc)[-4000:]}
        return {"exit_code": result["exit_code"], "timed_out": result["timed_out"], "seconds": result["seconds"],
                "stdout": result["stdout"][-12000:], "stderr": result["stderr"][-12000:]}

    def _variants_for(dest: Path, variant: str) -> dict:
        steps = sandbox._steps(dest)
        spec = draft_spec()
        if variant == "nop":
            return {}
        if variant == "golden":
            return {s: sandbox.solve_variant(dest, s) for s in steps}
        if variant == "alt":
            return {s: sandbox.Variant("patch", patch=(dest / "authoring/alt" / f"{s}.patch").read_text(), label=f"alt:{s}")
                    for s in steps if (dest / "authoring/alt" / f"{s}.patch").is_file()}
        if variant.startswith("mutant:"):
            mid = variant.split(":", 1)[1]
            path = dest / "authoring/mutants" / f"{mid}.patch"
            if not path.is_file():
                raise ValueError(f"no mutant {mid!r} in the draft")
            mutant = next((m for m in spec.get("mutants") or [] if m.get("id") == mid), {})
            trap = next((t for t in spec.get("traps") or [] if t.get("id") == mutant.get("trap")), {})
            step_of = {c.get("id"): c.get("step") for c in spec.get("checks") or []}
            driven = trap.get("checks") or []
            step = mutant.get("step") or (step_of.get(driven[0]) if driven else None) or steps[0]
            variants = {s: sandbox.solve_variant(dest, s) for s in steps}
            variants[step] = sandbox.Variant("patch", patch=path.read_text(), label=f"mutant:{mid}")
            for later in steps[steps.index(step) + 1:]:
                variants.pop(later, None)
            return variants
        raise ValueError("variant must be nop, golden, alt or mutant:<id>")

    def sandbox_grade(variant: str, **_):
        ok, why = sandbox.available()
        if not ok:
            return {"error": why}
        try:
            dest = _scratch(complete=True)
            variants = _variants_for(dest, variant)
            record = sandbox.verify(dest, variants, work=dest / "verify", keep_capture=False)
        except (ContractError, ValueError, sandbox.SandboxError) as exc:
            return {"error": str(exc)[-4000:]}
        out = {"variant": variant, "reward": record["reward"], "valid": record["valid"], "steps": {}}
        for step, entry in record["steps"].items():
            grade = entry.get("grade") or {}
            flags = {cid: bool(r.get("passed")) for cid, r in (grade.get("checks") or {}).items()}
            failing = {cid: {"reason": r.get("reason"), "stderr": (r.get("stderr") or "")[-1500:],
                             "stdout": (r.get("stdout") or "")[-1500:]}
                       for cid, r in (grade.get("checks") or {}).items() if not r.get("passed")}
            out["steps"][step] = {"reward": entry.get("reward"), "flags": flags, "failing": failing,
                                  "contract_errors": grade.get("contract_errors"), "error": entry.get("error"),
                                  "verifier_stderr": (entry.get("verifier_stderr") or "")[-1500:]}
        return out

    def run_controls(**_):
        ok, why = sandbox.available()
        if not ok:
            return {"error": why}
        try:
            dest = _scratch(complete=True)
            receipt = controls_mod.run(dest, work=dest / "controls-work",
                                       cache_root=bundle / "verification/control-cache")
        except (ContractError, ValueError, sandbox.SandboxError) as exc:
            return {"error": str(exc)[-4000:]}
        summary = {k: {"reward": v.get("reward"), "valid": v.get("valid")} if isinstance(v, dict) and "reward" in v else v
                   for k, v in receipt["results"].items() if k not in ("mutants", "tamper_files")}
        summary["mutants"] = {mid: {"reward": m.get("reward"), "failed": m.get("failed"), "declared": m.get("declared")}
                              for mid, m in receipt["results"].get("mutants", {}).items()}
        return {"ok": receipt["ok"], "errors": receipt["errors"], "advisories": receipt["advisories"],
                "seconds": receipt["seconds"], "results": summary}

    def submit_preflight(spec: dict) -> list[str]:
        """What validation would reject, found deterministically before the session ends."""
        problems: list[str] = []
        checked = check_spec(spec)
        problems += [f"static: {p}" for p in checked.get("problems") or []]
        ok, why = sandbox.available()
        if ok:
            controls = run_controls()
            if controls.get("error"):
                problems.append(f"controls could not run: {str(controls['error'])[:800]}")
            else:
                problems += [f"control: {e}" for e in controls.get("errors") or []]
                for mid, row in ((controls.get("results") or {}).get("mutants") or {}).items():
                    failed, declared = set(row.get("failed") or []), set(row.get("declared") or [])
                    if declared and failed != declared:
                        problems.append(f"mutant {mid}: declares fails {sorted(declared)} but actually fails "
                                        f"{sorted(failed)}")
        problems += difficulty_advisories(spec)
        return problems

    def difficulty_advisories(spec: dict) -> list[str]:
        """THE DIFFICULTY LOOP's state at submission: which steps a family's blind solve still passes."""
        current = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        out = []
        steps = [s.get("name") for s in spec.get("steps") or []]
        for family in sorted(BLIND_FAMILIES):
            try:
                record = json.loads((bundle / f"verification/blind-solve-{family}.json").read_text())
            except (OSError, ValueError):
                out.append(f"difficulty (advisory): no {family} blind solve yet -- the bar is met at creation "
                           f"(THE DIFFICULTY LOOP); blind_solve(model='{family}')")
                continue
            stale = " (solved an earlier draft)" if record.get("draft_sha256") != current else ""
            for step in steps:
                row = (record.get("by_step") or {}).get(step) or {}
                if row.get("reward") == 1.0:
                    out.append(f"difficulty (advisory): {family}'s blind solve fully passed step {step}{stale} -- "
                               "expect its screen above the bar; add a determinate interlocked case it gets wrong")
        return out

    def probe_patch(step: str, patch: str, hypothesis: str = "", expected_fails: list | None = None, **_):
        ok, why = sandbox.available()
        if not ok:
            return {"error": why, "advisory_only": True}
        try:
            dest = _scratch(complete=True)
            names = sandbox._steps(dest)
            if step not in names:
                return {"error": f"unknown step {step!r}; choose from {names}", "advisory_only": True}
            variants = {s: sandbox.solve_variant(dest, s) for s in names[:names.index(step)]}
            variants[step] = sandbox.Variant("patch", patch=patch, label="hypothesis-probe")
            record = sandbox.verify(dest, variants, work=dest / "hypothesis-verify", keep_capture=False)
        except (ContractError, ValueError, sandbox.SandboxError) as exc:
            return {"error": str(exc)[-4000:], "advisory_only": True}
        from ..artifacts import atomic_json
        receipt = {"hypothesis": hypothesis, "step": step, "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                   "advisory_only": True, "grading": record}
        atomic_json(log / f"hypothesis-probe-{uuid.uuid4().hex}.json", receipt)
        check_kinds = {c.get("id"): c.get("kind") for c in draft_spec().get("checks") or []}
        failed = sorted(cid for cid, r in ((record.get("steps", {}).get(step, {}).get("grade") or {}).get("checks") or {}).items()
                        if not r.get("passed") and check_kinds.get(cid) == "objective")
        expected = set(expected_fails or [])
        return {"advisory_only": True, "valid": record.get("valid"), "reward": record.get("reward"),
                "failing_objectives": failed, "unexpected_failures": sorted(set(failed) - expected),
                "expected_but_passing": sorted(expected - set(failed)), "by_step": record.get("steps"),
                "note": "a shortcut probe demonstrates discrimination, not actual model adoption or measured hardness"}

    # -- independent solve ---------------------------------------------------
    blind_n = {"n": 0}

    def generate_cases(mode: str, **kw):
        from . import cases
        from ..artifacts import atomic_json
        if mode == "checkpoints":
            record = {"advisory_only": True, "checkpoints": cases.checkpoint_template()}
        elif mode == "intersections":
            record = cases.intersections(kw.get("dimensions") or {}, limit=kw.get("limit"), seed=kw.get("seed", 0))
        elif mode == "sequences":
            record = cases.sequences(kw.get("actions") or [], length=kw.get("length", 5), count=kw.get("count", 128),
                                     seed=kw.get("seed", 0), prefixes=kw.get("prefixes"))
        else:
            return {"advisory_only": True, "error": "unknown case mode"}
        path = log / f"generated-cases-{uuid.uuid4().hex}.json"
        atomic_json(path, record)
        rows = record.get("cases", [])
        return {**record, "cases": rows[:32], "total_cases": len(rows), "next_offset": 32 if len(rows) > 32 else None,
                "private_file": str(path), "file": path.name,
                "note": "page with read_generated_cases; keep hidden cases out of the fixture"}

    def read_generated_cases(file: str, offset: int = 0, limit: int = 32, **_):
        if Path(file).name != file or not file.startswith("generated-cases-") or not file.endswith(".json"):
            raise ValueError("choose a file returned by generate_cases")
        record = strict_json(contained(log, file).read_text())
        rows = record.get("cases", record.get("checkpoints", []))
        offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
        end = min(len(rows), offset + limit)
        return {"advisory_only": True, "file": file, "cases": rows[offset:end], "total_cases": len(rows),
                "next_offset": end if end < len(rows) else None}

    def independent_oracle(**_):
        from . import oracle, blind_solve as solver_mod
        if blind_n["n"] >= _blind_budget():
            return {"advisory_only": True, "error": "independent solve budget spent; submit_draft remains available"}
        ok, why = sandbox.available()
        if not ok:
            return {"advisory_only": True, "error": why}
        try:
            dest = _scratch(complete=False)
            spec = draft_spec()
            steps = build_mod.compile_steps(spec)
            (dest / "task.toml").write_text(build_mod._steps_toml(steps))
            for step in steps:
                path = contained(dest, f"steps/{step['name']}/instruction.md")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(step["instruction"].strip() + "\n")
            rel = f"authoring/oracles/{uuid.uuid4().hex}"
            blind_n["n"] += 1
            result = oracle.derive(dest, solver_mod.solver_model_for(MODEL), MODEL, bundle / rel)
            return {**result, "provenance_reference": {"path": rel},
                    "usage": "stage the independently derived patches as alternative and this reference as quality.independent_oracle; "
                             "controls and provenance are checked only during validation"}
        except Exception as exc:
            return {"advisory_only": True, "error": f"independent oracle unavailable: {type(exc).__name__}: {exc}"[:2000]}

    def stage_research_variants(design: dict, episodes: int = 5, **_):
        from .. import research
        from ..config import ARTIFACTS
        from ..artifacts import atomic_json
        root = ARTIFACTS / "research-drafts" / uuid.uuid4().hex
        source = root / "source"
        shutil.copytree(bundle, source, ignore=shutil.ignore_patterns("verification", "hardened", "__pycache__", "*.pyc"))
        spec = draft_spec()
        spec.setdefault("source", {"session_id": session_id, "seed_index": extraction.seed_index})
        atomic_json(source / "authoring/spec.json", spec)
        if not (source / "provenance/ledger.json").exists():
            atomic_json(source / "provenance/ledger.json", {"category": level or row.get("category", "F2")})
        drafts = research.create_variants(source, root / "candidates", design)
        plan = research.create_plan(root / "experiment", drafts["variants"], purpose="ablation", episodes=episodes,
                                    treatment=design.get("treatment"))
        return {"advisory_only": True, "validation_deferred": True, "variants": drafts["variants"],
                "plan": str(root / "experiment"), "plan_id": plan["id"],
                "note": "drafts retained; research validate and research run happen after creation"}

    def blind_solve(model: str | None = None, **_):
        """A calibration model fixes the task from the instruction and the repository alone;
        the shipped grader scores its diff. Every check it fails is a trap or an over-fit
        test, and the designer decides which with the diff in hand. Advisory."""
        from . import blind_solve as solver_mod
        from ..artifacts import atomic_json
        MAX_BLIND_SOLVES_PER_SESSION = _blind_budget()
        if blind_n["n"] >= MAX_BLIND_SOLVES_PER_SESSION:
            return {"error": f"blind_solve budget spent ({MAX_BLIND_SOLVES_PER_SESSION} per session)"}
        family = str(model or "").strip().casefold()
        if family and family not in BLIND_FAMILIES:
            return {"error": f"model must be one of {sorted(BLIND_FAMILIES)}"}
        ok, why = sandbox.available()
        if not ok:
            return {"error": why}
        verdict = check_spec(draft_spec())
        if not verdict["clean"]:
            return {"error": "the draft does not pass check_draft yet; the solver needs a complete package",
                    "problems": verdict["problems"][:12]}
        blind_n["n"] += 1
        try:
            dest = _scratch(complete=True)
            model = BLIND_FAMILIES[family] if family else solver_mod.solver_model_for(MODEL)
            record = solver_mod.solve(dest, model, log / f"blind-solve-{blind_n['n']:03d}")
        except (ContractError, ValueError, sandbox.SandboxError) as exc:
            return {"error": str(exc)[-2000:]}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"the independent solve did not complete: {type(exc).__name__}: {exc}"[:2000]}
        record["n"] = blind_n["n"]
        record["draft_sha256"] = hashlib.sha256(json.dumps(draft_spec(), sort_keys=True).encode()).hexdigest()
        atomic_json(log / f"blind-solve-{blind_n['n']:03d}.json", record)
        atomic_json(bundle / "verification/blind-solve.json", record)
        atomic_json(bundle / f"verification/blind-solve-{_family_of(record.get('model'))}.json", record)
        patch = (log / f"blind-solve-{blind_n['n']:03d}" / "blind.patch").read_text() if (log / f"blind-solve-{blind_n['n']:03d}" / "blind.patch").is_file() else ""
        return {"model": record["model"], "reward": record.get("reward"), "checks": record.get("checks"),
                "by_step": record.get("by_step"), "step_names": record.get("step_names"),
                "complete": record.get("complete"), "limits": record.get("limits"),
                "failing": record.get("failing"), "steps": record.get("steps"), "tool_calls": record.get("tool_calls"),
                "error": record.get("error"), "note": record.get("note"), "patch": patch[:20000],
                "remaining": MAX_BLIND_SOLVES_PER_SESSION - blind_n["n"],
                "too_easy_steps": [name for name, row in (record.get("by_step") or {}).items()
                                   if row.get("reward") == 1.0],
                "how_to_read": "For each failing check decide: TRAP (the solver adopted the misreading the task is "
                               "built on; record it in quality.difficulty_case) or OVER-FIT (the solver's fix is "
                               "correct under the instruction and the test asserts your reference's convention; "
                               "rewrite the test to observe behaviour). Inspect step diagnostics and completeness; "
                               "a truncated probe is not a capability failure, and one pass cannot establish an easy task."}

    # -- pre-review ----------------------------------------------------------
    pre_review_n = {"n": 0}

    def pre_review(**_):
        """The independent reviewer over the staged draft, exactly as the gate runs it.

        Strengthens authoring without adding a blocker: the designer sees the
        real bar before submitting, and every finding it addresses is a repair
        round it does not spend. Costs one review per call.
        """
        import os
        from contextlib import nullcontext
        from .. import review as critic
        from ..artifacts import atomic_json
        from ..config import MAX_PRE_REVIEWS_PER_SESSION
        from ..ledger import faithfulness
        if pre_review_n["n"] >= MAX_PRE_REVIEWS_PER_SESSION:
            return {"error": f"pre_review budget spent ({MAX_PRE_REVIEWS_PER_SESSION} per session); "
                             "submit_draft remains available without clean controls or a pre-review"}
        verdict = check_spec(draft_spec())
        if not verdict["clean"]:
            return {"error": "the draft does not pass check_draft yet; the reviewer reads a valid package only",
                    "problems": verdict["problems"][:12]}
        pre_review_n["n"] += 1
        try:
            dest = _scratch(complete=True)
            atomic_json(dest / "provenance/ledger.json", faithfulness.build(dest, session_id, extraction.seed_index, root))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"the draft could not be packaged for review: {type(exc).__name__}: {exc}"[:2000]}
        producer = MODEL
        # ARENA_PRE_REVIEW_MODEL moves only this advisory pass (about three in four
        # reviews) to another OpenAI model's rate bucket: on 2026-10-04 gpt-6-astra's
        # organisation TPM was saturated and Codex calibration episodes were voided.
        # The gate's own review is unchanged.
        model = (os.environ.get("ARENA_PRE_REVIEW_MODEL") or os.environ.get("ARENA_REVIEW_MODEL")
                 or ("claude-fable-5-1" if producer.startswith("gpt-") else "gpt-6-astra"))
        try:
            from ..orchestrate import _REVIEW_SLOTS as slot
        except Exception:  # noqa: BLE001
            slot = nullcontext()
        try:
            with slot:
                report = critic.review(dest, model)
            doc, _ = critic.load_review_deliverable(dest / report["work_dir"] / "deliverables/review.json")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"pre-review did not complete: {type(exc).__name__}: {exc}"[:2000]}
        findings = [{"id": c.get("id"), "verdict": c.get("verdict"), "reason": c.get("reason")}
                    for c in doc.get("checks", []) if isinstance(c, dict) and c.get("verdict") != "pass"]
        for key in ("ambiguities", "substantive_gaps", "shortcut_risks", "privacy_findings"):
            for index, item in enumerate(doc.get(key) or [], 1):
                findings.append({"id": f"{key}/{index}", "verdict": key,
                                 "reason": item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)})
        harness = [e for e in report.get("errors") or []
                   if not (e.startswith(("semantic reviewer requested revision", "unresolved review findings:"))
                           or ": check not independently justified" in e)]
        # The scratch export is pruned after three more exports; keep what a
        # later reader needs to compare pre-reviews: the checks, the suite
        # hashes, the instruction hashes and the reviewer's deliverable.
        keep = log / f"pre-review-{pre_review_n['n']:03d}"
        keep.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(dest / "tests/checks.json", keep / "checks.json")
            shutil.copyfile(dest / report["work_dir"] / "deliverables/review.json", keep / "review.json")
            hashes = {}
            for folder in ("tests/suites", "steps"):
                for path in sorted((dest / folder).rglob("*")):
                    if path.is_file():
                        hashes[path.relative_to(dest).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            atomic_json(keep / "package-hashes.json", hashes)
        except OSError:
            pass
        record = {"schema": "arena-coding/pre-review/1", "n": pre_review_n["n"], "model": model,
                  "verdict": doc.get("verdict"), "findings": findings, "harness_problems": harness,
                  "review_dir": str(dest.relative_to(bundle)), "kept": str(keep.relative_to(bundle))}
        atomic_json(log / f"pre-review-{pre_review_n['n']:03d}.json", record)
        return {"verdict": doc.get("verdict"), "findings": findings, "harness_problems": harness,
                "difficulty_assessment": doc.get("difficulty_assessment"),
                "novelty_assessment": doc.get("novelty_assessment"),
                "remaining": MAX_PRE_REVIEWS_PER_SESSION - pre_review_n["n"],
                "note": "Address every finding in the tests or the instruction, then pre_review again if the tests "
                        "changed. The gate runs its own review after submission; a finding you leave is a repair "
                        "round you will spend."}

    # -- validation ----------------------------------------------------------
    def check_spec(spec: dict, **_):
        from .. import local_work
        if local_work.enabled():
            # Validation in another process: a validator crash or a hung
            # sandbox cannot take the design session with it, and the receipt
            # under verification/local-workers names the worker that ran.
            portable = {k: v for k, v in vars(extraction).items()
                        if k in ("session_id", "seed_index", "source_root", "span", "pivot", "changed_paths")}
            if portable.get("source_root") is not None:
                portable["source_root"] = str(portable["source_root"])
            return local_work.run("check_spec", {"bundle": str(bundle), "extraction": portable,
                                                 "trace": trace, "level": level, "row": row,
                                                 "arguments": {"spec": spec}})
        spec = copy.deepcopy(spec)
        spec.setdefault("source", {"session_id": session_id, "seed_index": extraction.seed_index, "tier": row.get("tier", 4)})
        spec.setdefault("target_level", level or "")
        if type(row.get("step_budget")) is int:
            spec["step_budget"] = row["step_budget"]
        try:
            problems = validate_mod.validate(spec, bundle)
            from ..ledger.faithfulness import citation_problems
            problems += citation_problems(spec, trace, session_id)
            problems += [f"private data in the fixture: {f.get('file')} ({f.get('kind')}); exclude or replace "
                         "the file before submitting" for f in privacy_scan(repo)]
        except Exception as exc:  # noqa: BLE001
            return {"clean": False, "problems": [f"the spec could not be validated: {type(exc).__name__}: {exc}"]}
        try:
            advisories = validate_mod.overfit_advisories(spec, bundle)
        except Exception as exc:  # noqa: BLE001  advisories never block
            advisories = [f"ADVISORY unavailable: {type(exc).__name__}: {exc}"]
        try:
            from .. import learning_signal as _learning_signal
            advisories += _learning_signal.design_advisories(spec)
        except Exception as exc:  # noqa: BLE001  advisories never block
            advisories.append(f"ADVISORY learning signal unavailable: {type(exc).__name__}: {exc}")
        strength = difficulty_profile(spec)
        try:
            doctrine = levers_mod.diagnostics(spec, bundle)
        except Exception as exc:  # noqa: BLE001  advisories never block
            doctrine = {"advisories": [f"lever audit unavailable: {type(exc).__name__}: {exc}"]}
        return {"clean": not problems, "problems": problems,
                "advisories": advisories + strength["advisories"]
                + [f"LEVER DOCTRINE: {a}" for a in doctrine.get("advisories", [])],
                "difficulty_profile": strength, "lever_doctrine": doctrine}

    def submit_check(spec: dict) -> dict:
        # `clean` here is transport acceptance, never a validity verdict. Do not
        # call validators, Docker, controls or models while accepting a draft.
        return {"clean": True, "problems": [], "validation_deferred": True, "advisories": []}

    captured_episodes = list(row.get("_captured_episodes") or [])
    regrade_n = {"n": 0}

    def _episode(model: str, index: int) -> dict:
        family = str(model).casefold()
        chosen = [e for e in captured_episodes if family in e["model"].casefold()]
        if not chosen:
            raise ValueError(f"no captured episodes for {model!r}; models: "
                             f"{sorted({e['model'] for e in captured_episodes})}")
        if not 1 <= int(index) <= len(chosen):
            raise ValueError(f"index must be 1..{len(chosen)}")
        return chosen[int(index) - 1]

    def regrade_episodes(**_):
        from .. import fastharden
        from ..artifacts import atomic_json
        if not captured_episodes:
            return {"error": "this session has no captured episodes (only hidden-only hardening rounds do)"}
        ok, why = sandbox.available()
        if not ok:
            return {"error": why}
        verdict = check_spec(draft_spec())
        if not verdict["clean"]:
            return {"error": "the draft does not pass check_draft yet", "problems": verdict["problems"][:12]}
        try:
            summary = fastharden.regrade(_scratch(complete=True), captured_episodes)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"re-grade failed: {type(exc).__name__}: {exc}"[:2000]}
        regrade_n["n"] += 1
        atomic_json(log / f"regrade-{regrade_n['n']:03d}.json", summary)
        atomic_json(bundle / fastharden.RECEIPT, {**summary, "draft_sha256": hashlib.sha256(
            json.dumps(draft_spec(), sort_keys=True).encode()).hexdigest()})
        return {"in_sample": True, "provisional_meets_bar": summary["provisional_meets_bar"],
                "by_model": summary["by_model"], "problems": summary["problems"][:20],
                "note": "provisional: these trees were in view when the cases were chosen; only a fresh screen counts"}

    def read_episode(model: str, index: int, path: str | None = None, offset: int = 0, **_):
        import difflib
        try:
            episode = _episode(model, index)
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}
        capture = Path(episode["steps"][-1]["capture"]) / "repo"
        if path:
            target = contained(capture, path)
            if not target.is_file():
                return {"error": f"{path} is not a file of this episode's final tree"}
            text = target.read_text(encoding="utf-8", errors="replace")
            return {"episode": episode["id"], "path": path, "content": text[int(offset):int(offset) + 12000],
                    "total_chars": len(text)}
        changed, diffs = [], []
        skip = ("node_modules", ".venv", "__pycache__", ".git", "dist", "build")
        files = {p.relative_to(capture).as_posix() for p in capture.rglob("*")
                 if p.is_file() and not any(part in skip for part in p.parts)}
        original = {p.relative_to(repo).as_posix() for p in repo.rglob("*")
                    if p.is_file() and not any(part in skip for part in p.parts)}
        for rel in sorted(files | original):
            a, b = repo / rel, capture / rel
            old = a.read_bytes() if a.is_file() else b""
            new = b.read_bytes() if b.is_file() else b""
            if old == new:
                continue
            changed.append(rel + ("" if a.is_file() else " (new)") + ("" if b.is_file() else " (deleted)"))
            try:
                diffs.append("".join(difflib.unified_diff(old.decode().splitlines(True), new.decode().splitlines(True),
                                                          f"a/{rel}", f"b/{rel}", n=2)))
            except UnicodeDecodeError:
                diffs.append(f"(binary {rel})")
        text = "\n".join(diffs)
        return {"episode": episode["id"], "model": episode["model"], "changed": changed,
                "diff": text[int(offset):int(offset) + 15000], "total_chars": len(text)}

    def determinacy_probe(case: str, question: str, expected: str, answer_form: str = "a short answer",
                          context: str = "", step: str | None = None, **_):
        from .determinacy import probe
        steps = draft_spec().get("steps") or []
        chosen = next((s for s in steps if s.get("name") == step), steps[0] if steps else None)
        if not chosen:
            return {"error": "the draft has no step instruction yet"}
        result = probe(chosen.get("instruction", ""), case, question, expected, context=context,
                       answer_form=answer_form, record_dir=bundle / "verification/determinacy")
        return {"determinate": result["determinate"],
                "answers": {m: {k: a.get(k) for k in ("answer", "agrees", "undetermined", "error")}
                            for m, a in result["answers"].items()}}

    critic_n = {"n": 0}

    def coverage_critic(**_):
        # The final version's critic gaps now block shipping (Arena feedback 2026-10-08): let the round close them
        # before it submits instead of discovering a new batch at every final.
        from .. import coverage_critic as critic_mod
        if critic_n["n"] >= policy.DESIGNER_CRITIC_RUNS:
            return {"error": f"coverage_critic budget spent ({policy.DESIGNER_CRITIC_RUNS} per session)"}
        ok, why = sandbox.available()
        if not ok:
            return {"error": why, "advisory_only": True}
        try:
            dest = _scratch(complete=True)
            critic_n["n"] += 1
            result = critic_mod.critic(dest)
        except (ContractError, ValueError, sandbox.SandboxError) as exc:
            return {"error": str(exc)[-3000:], "advisory_only": True}
        proposals = sum(len(s.get("proposals") or []) for s in (result.get("steps") or {}).values())
        return {"advisory_only": True, "proposals": proposals, "remaining": policy.DESIGNER_CRITIC_RUNS - critic_n["n"],
                "gaps": [{k: g.get(k) for k in ("step", "clause", "violation", "why_uncaught", "edits")}
                         for g in result.get("gaps") or []]}

    fast_handlers = ({"regrade_episodes": regrade_episodes, "read_episode": read_episode,
                      "coverage_critic": coverage_critic} if captured_episodes else {})
    fast_handlers["determinacy_probe"] = determinacy_probe

    return {**fast_handlers, "list_evidence": lambda **kw: evidence().list(**kw),
            "read_evidence": lambda **kw: evidence().read(**kw),
            "view_evidence_image": lambda **kw: evidence().image(**kw),
            "list_corrections": list_corrections, "read_trace": read_trace, "read_signals": read_signals,
            "snapshot_boundaries": snapshot_boundaries, "workspace_at": workspace_at,
            "read_workspace_file": read_workspace_file, "diff_workspace": diff_workspace,
            "materialize_fixture": materialize_fixture,
            "list_fixture": list_fixture, "read_fixture": read_fixture,
            "write_fixture_file": write_fixture_file, "delete_fixture_file": delete_fixture_file,
            "sandbox_run": sandbox_run, "sandbox_grade": sandbox_grade, "run_controls": run_controls,
            "probe_patch": probe_patch,
            "pre_review": pre_review,
            "blind_solve": blind_solve,
            "independent_oracle": independent_oracle, "generate_cases": generate_cases,
            "read_generated_cases": read_generated_cases,
            "stage_research_variants": stage_research_variants,
            "difficulty_profile": lambda **_: {**difficulty_profile(draft_spec()),
                                               "lever_doctrine": levers_mod.diagnostics(draft_spec(), bundle)},
            "stage_spec": stage_spec, "read_draft": read_draft, "draft_spec": draft_spec,
            "retain_draft": retain_draft, "creation_status": creation_status,
            "check_spec": check_spec, "check_draft": lambda **_: check_spec(draft_spec()),
            "submit_check": submit_check, "submit_preflight": submit_preflight}


# ---------------------------------------------------------------------------
# Opening message
# ---------------------------------------------------------------------------

def _operator_brief_lines(row: dict) -> list[str]:
    """What an operator recorded after reading this trace before the build
    (frozen wave row: `operator_brief`, and for a multi-turn task
    `operator_turns` = [{msg, user_words, work, builds_on}]). Leads to check
    against the trace, never a substitute for reading it."""
    brief, turns = row.get("operator_brief"), row.get("operator_turns")
    lines: list[str] = []
    if isinstance(brief, str) and brief.strip():
        lines += ["", "OPERATOR TRACE ANALYSIS (a human-directed read of this trace before the build; verify every "
                      "claim against the trace and the fixture yourself -- where they disagree, the trace wins):",
                  brief.strip()[:4000]]
    if isinstance(turns, list) and turns:
        lines += ["", "OPERATOR TURN PLAN (candidate user messages for the turns; confirm each is a user message, in "
                      "order, that builds on the previous turn's result, and cite it as trace_turn):"]
        for t in turns[:6]:
            if isinstance(t, dict):
                lines.append(f"  - msg {t.get('msg')}: {str(t.get('user_words') or '')[:300]!r} -- work: "
                             f"{str(t.get('work') or '')[:300]}; builds on: {str(t.get('builds_on') or '')[:200]}")
    return lines


def _opening(row: dict, extraction, bundle: Path, level: str | None = None) -> str:
    from .. import learning_signal as _learning_signal
    repo = bundle / "environment" / "repo"
    files = [p for p in repo.rglob("*") if p.is_file()] if repo.is_dir() else []
    langs: dict[str, int] = {}
    for p in files:
        lang = _fixture_language(p.name)
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
    tier = row.get("tier", 4)
    pivot = getattr(extraction, "pivot", None)
    span = list(getattr(extraction, "span", (0, 0)))
    moments = getattr(extraction, "moments", []) or []
    changed = list(getattr(extraction, "changed_paths", []) or [])
    stats = getattr(extraction, "stats", {}) or {}
    usable = extract_mod.usable_correction_count(row, extraction)
    floor = traps_mod.floor_for(tier)
    need = -(-int(floor * 100) * usable // 100)
    cls = row.get("classification", {}) or {}
    lines = [
        f"SESSION {extraction.session_id} seed {extraction.seed_index} (tier {tier}, model under test {row.get('model')})",
        f"DOMAIN: {cls.get('domain')}   LANGUAGE (classifier): {cls.get('language')}   RUNTIME: {cls.get('runtime_class')}",
        f"FAILURE MODE the classifier saw: {cls.get('failure_mode')}",
        f"REPRODUCTION the classifier described: {cls.get('reproduction')}",
        "DEFECT PAIRS the classifier found (before_msg -> after_msg): " + json.dumps(cls.get("defect_pairs") or [], ensure_ascii=False),
        "CHECKABLE RULES the classifier extracted: " + json.dumps(cls.get("checkable_rules") or [], ensure_ascii=False)[:3000],
        "",
        f"The fixture (environment/repo) currently holds {len(files)} files, {sum(p.stat().st_size for p in files):,} bytes, "
        f"languages {json.dumps(langs)}, lifted at message boundary "
        f"{pivot - 1 if isinstance(pivot, int) else '?'} (pivot {pivot}). "
        f"The seed spans messages {span[0]}-{span[-1]}, carries {len(moments)} discovery "
        f"signals and {usable} usable corrections. {len(changed)} paths changed between the fixture "
        f"and the end of the seed: {json.dumps(changed[:20])}",
        ("UPSTREAM REDACTION PLACEHOLDERS inside fixture files (the dump masked secrets in place; these files "
         "cannot run as lifted -- repair them with write_fixture_file, which records the bytes as authored, or "
         "exclude them): " + json.dumps(stats.get("redaction_markers") or {}))
        if stats.get("redaction_markers") else "No upstream redaction placeholders in fixture source files.",
        (lambda ex: (f"BINARIES EXCLUDED from the fixture at extraction ({len(ex)}): "
                     + ", ".join(f"{e['path']} ({e['bytes']} B, {e['source']})" for e in ex[:12])
                     + ". A repository graded by tests is source; the user's screenshots and generated "
                       "images are evidence (read them with the evidence tools). Author an asset with "
                       "write_fixture_file only when a test genuinely needs one.")
         if ex else "No binaries were excluded from the fixture.")((stats.get("skipped_before") or {}).get("binaries_excluded") or []),
        (lambda cl: (f"UPSTREAM CLONES EXCLUDED from the fixture ({len(cl)}): "
                     + "; ".join(f"{c['dir']}/ ({c['files']} files, {c['bytes']} B) cloned from {c['url']} "
                                 f"at message {c['msg_index']}" for c in cl[:6])
                     + ". The session cloned a third-party repository into the tree; the dump kept only "
                       "the files its tools touched, so it is a partial clone that cannot build, and its "
                       "bytes are not the user's work. It is vendored material: never lift it or ask the "
                       "agent to build it. If the task genuinely needs that project, install a pinned "
                       "package (toolchain extra_apt or a pinned requirement) or author a declared stub "
                       "with write_fixture_file; otherwise route the tests around it.")
         if cl else "No upstream clones were excluded from the fixture.")((stats.get("skipped_before") or {}).get("upstream_clones") or []),
        (f"TRAP FLOOR for tier {tier}: cite at least {need} trap(s) from real corrections "
         f"({floor:.0%} of the {usable} usable corrections); the rest may be authored on code the fixture contains."
         if need else f"TRAP FLOOR for tier {tier}: cite at least {need} -- no trace quotation is required with "
                      f"{usable} usable corrections; every trap may be authored on code the fixture contains."),
        "",
        (f"STEP BUDGET FOR THIS TASK: {row.get('step_budget')} step(s). "
         + ("This task is SINGLE-STEP (as are about 90% of the batch): one instruction, one set of checks; put "
            "several interlocked hidden decisions aimed at BOTH model families into it."
            if row.get("step_budget") == 1 else
            f"This task is one of the ~10% designated MULTI-TURN tasks: {policy.MULTI_STEP_MIN_STEPS} to "
            f"{row.get('step_budget')} steps (Spec Appendix A, see MULTI-TURN TASKS): each step is a later real user "
            "message of the trace (trace_turn), builds on the state the previous turn left, and is extremely hard on "
            "its own -- its own interlocks aimed at both families, at least "
            f"{policy.MIN_OBJECTIVES_PER_STEP} objectives and {policy.MIN_TRAPS_PER_STEP} traps per turn.")
         + f" Validation refuses more steps than the budget; every step is held to pass@1 <= "
           f"{policy.STEP_PASS_RATE_MAX} on each model.")
        if type(row.get("step_budget")) is int else "STEP BUDGET: not assigned (legacy build).",
        *_operator_brief_lines(row),
        "",
        *_learning_signal.guidance().rstrip("\n").splitlines(),
        "",
        "BUDGET AND FLOORS, enforced during downstream validation:",
        f"  - at least {MIN_OBJECTIVES} objectives, at least one guardrail, at least three traps, a mutant per trap, an alternative fix per step",
        f"  - trace_derived traps are counted by DISTINCT citation; a tier-{tier} source must cite {int(floor * 100)}% of its "
        f"{usable} usable corrections: at least {need} different ones (none required when usable is zero)",
        "  - every requirements line pinned; the agent image never contains tests or the solution",
        "  - the untouched fixture scores 0, the reference and the alternative score 1.0, every mutant fails exactly what it declares",
        "",
        levers_mod.relevant_text(cls, repo, steps=max(1, len(cls.get("defect_pairs") or []))),
        "",
        "Start with list_corrections and snapshot_boundaries; read the defective and the accepted trees; reproduce "
        "the defect with sandbox_run before writing any test. Use stage_spec to build the draft in sections; check_draft "
        "for static problems; sandbox_grade and run_controls for executed ones; finish with submit_draft. Clean means "
        "VALID, not hard: before submitting, reread quality.difficulty_case and ask whether a strong engineer working "
        "carefully would get every hidden test right on the first try -- if yes, make it harder now.",
    ]
    return redact_excerpt("\n".join(lines))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def restore_design_session(bundle: Path, checkpoint: dict) -> dict:
    from ..artifacts import evidence_inventory
    from ..checkpoint import WORKSPACE
    session = contained(bundle, checkpoint["session"])
    if evidence_inventory(bundle, [checkpoint["session"]]) != checkpoint["session_files"]:
        raise ValueError("Retained authoring conversation changed after recovery was recorded")
    if evidence_inventory(bundle, list(WORKSPACE)) != checkpoint["workspace_files"]:
        raise ValueError("Retained authoring workspace changed after recovery was recorded")
    config = strict_json((session / "request-config.json").read_text())
    responses = sorted(session.glob("response-*.json"))
    if [p.name for p in responses] != [f"response-{i:03d}.json" for i in range(1, len(responses) + 1)]:
        raise ValueError("Retained authoring response sequence is incomplete")
    remaining = config["max_steps"] - len(responses)
    if not 0 < remaining <= MAX_STEPS or remaining != checkpoint["remaining_steps"]:
        raise ValueError("Retained authoring budget is invalid")
    messages = strict_json((session / "conversation.json").read_text())
    if not isinstance(messages, list) or not messages or messages[-1].get("role") != "user":
        raise ValueError("Retained authoring conversation has no complete request boundary")
    draft = session / "draft.json"
    return {"messages": messages, "config": config, "remaining_steps": remaining,
            "draft": strict_json(draft.read_text()) if draft.exists() else None}


REJECTION_REASON_MIN = 200


def _rejection(bundle: Path, data: dict, trace: dict | None = None) -> dict:
    from ..artifacts import evidence_inventory
    if not isinstance(data, dict) or set(data) != {"reason", "source_evidence"}:
        keys = set(data) if isinstance(data, dict) else set()
        detail = (f"missing {sorted({'reason', 'source_evidence'} - keys)}" if {'reason', 'source_evidence'} - keys
                  else f"unexpected {sorted(keys - {'reason', 'source_evidence'})}")
        raise ValueError(f'reject_seed takes exactly {{"reason": "<why this seed cannot carry a task>", '
                         f'"source_evidence": [{{"path": "<fixture file>" | "msg": <index>, "quote": "<verbatim>"}}]}}; '
                         f'{detail}. Nothing has been rejected yet')
    if not isinstance(data["reason"], str) or len(data["reason"].strip()) < REJECTION_REASON_MIN:
        raise ValueError(f"`reason` must argue the source limitation in at least {REJECTION_REASON_MIN} characters; "
                         "a rejection is final, so never send a trial call")
    evidence = data["source_evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("`source_evidence` must list at least one {file, quote}")
    repo = bundle / "environment/repo"
    for row in evidence:
        if not isinstance(row, dict) or set(row) != {"file", "quote"} or not str(row.get("quote", "")).strip():
            raise ValueError("source evidence requires a nonempty file and quote")
        source = str(row["file"])
        if source.startswith("msg") and source[3:].isdigit():
            index = int(source[3:])
            text = _render_message(trace["messages"][index], limit=None) if 0 <= index < len(trace.get("messages", [])) else ""
        else:
            try:
                text = contained(repo, source).read_text(encoding="utf-8", errors="replace")
            except (ContractError, OSError):
                raise ValueError(f"{source!r} is neither a fixture file nor a message index (msg<N>)") from None
        if row["quote"] not in text:
            raise ValueError(f"the quote is absent from {source}")
    return {"status": "source-rejected", "release_ready": False,
            "source_inventory": evidence_inventory(bundle, ("environment/repo",)), **data}


def validate_source_capacity(spec: dict, bundle: Path, *, source_root=None) -> dict | None:
    """Recheck a retained source-capacity finding during validation only."""
    if "_source_capacity_report" not in spec:
        return None
    report = spec["_source_capacity_report"]
    if not isinstance(report, dict):
        raise ValueError("_source_capacity_report must be an object")
    data = {key: report.get(key) for key in ("reason", "source_evidence")}
    trace = {"messages": []}
    if any(isinstance(row, dict) and str(row.get("file", "")).startswith("msg")
           for row in data.get("source_evidence") or []):
        from ..dump.recover import load_trace
        trace = load_trace((spec.get("source") or {}).get("session_id"), source_root)
    validated = _rejection(bundle, data, trace)
    return {**validated, "phase": "validation", "validation_deferred": False}


#: Every step re-sends the tool schemas, the system prompt and the whole
#: conversation; `prompt_cache.with_cache_breakpoints` marks a per-request
#: copy so the prefix is served from cache without changing a byte the
#: model reads. The names are re-exported here for the tests.
from .prompt_cache import (CACHE_MARK, CACHEABLE_BLOCKS,  # noqa: E402,F401
                           _marked_tail, with_cache_breakpoints)


def _with_revision_invariants(check: Callable, base: dict, draft_path: Path) -> Callable:
    """check_draft for a revision also reports the invariants validation enforces after the
    session (2026-10-14: 12 of 25 review repairs and the nfse round were refused only then, for
    removing a measured check): no check removed, no dependency chain rewired, the step inventory
    unchanged. Every starting check is passed as targeted, so a guardrail edit is left to the
    post-session judgement against the review's flagged checks. Advisory like check_draft:
    submit_draft is unaffected."""
    def wrapped(**kwargs):
        out = check(**kwargs)
        try:
            from ..harden import revision_problems, routing_problems
            draft = strict_json(Path(draft_path).read_text())
            found = revision_problems(base, draft, [{"id": c.get("id")} for c in base.get("checks") or []
                                                    if isinstance(c, dict)])
            # 2026-10-14 multiplayer: two new traps drove no graded objective; lightning: a
            # trace-derived trap was dropped. Both were refused only after the session.
            old, new = base.get("traps") or [], draft.get("traps") or []
            found += routing_problems(old, new, draft.get("checks") or [])
            derived = {t.get("id") for t in old if isinstance(t, dict) and t.get("provenance") == "trace_derived"}
            kept = {t.get("id") for t in new if isinstance(t, dict) and t.get("provenance") == "trace_derived"}
            if derived - kept:
                found.append(f"the revision removes or relabels trace-derived trap(s) {sorted(derived - kept)}; "
                             "a trap from a real correction is the task's faithfulness")
        except Exception as exc:  # noqa: BLE001  advisory, never blocks the session
            found = [f"revision invariants could not be checked: {type(exc).__name__}: {exc}"]
        if found and isinstance(out, dict):
            out["problems"] = list(out.get("problems") or []) + [f"REVISION: {p}" for p in found]
            out["clean"] = False
        return out
    return wrapped


def design(row: dict, extraction, bundle: Path, max_steps: int = MAX_STEPS, budget=None,
           feedback: list[str] | None = None, initial_spec: dict | None = None,
           resume_session: dict | None = None, revision: bool = False) -> dict:
    """Write all task components, retain submissions, and defer acceptance checks."""
    from .. import checkpoint as progress
    from ..artifacts import atomic_json
    from ..dump import recover as rec
    from ..policy import target_level
    from ..privacy import scan

    trace = rec.load_trace(extraction.session_id, getattr(extraction, "source_root", None))
    log = bundle / "verification/design-sessions" / uuid.uuid4().hex
    retained = restore_design_session(bundle, resume_session) if resume_session else None
    if retained:
        if initial_spec is not None or max_steps != retained["remaining_steps"]:
            raise ValueError("Continuation must use only the retained draft and remaining budget")
        initial_spec = retained["draft"]
    if initial_spec is not None:
        atomic_json(log / "draft.json", initial_spec)
    level = row.get("authoring_target_level") or target_level(row.get("category")) or row.get("target_level")
    if level == "F3" and not policy.HARDENING_PRESERVE_LEVEL:
        level = "F2"  # Source category stays intact; the ledger measures the final level.
    handlers = _handlers(bundle, extraction, trace, log / "draft.json", level=level, row=row, log=log)
    if revision and initial_spec is not None:
        handlers["check_draft"] = _with_revision_invariants(handlers["check_draft"], copy.deepcopy(initial_spec),
                                                            log / "draft.json")
    tools = _tools(len(trace.get("messages", [])))
    if row.get("_captured_episodes"):
        tools = tools + FAST_TOOLS
    opening = _opening(row, extraction, bundle, level)
    if level in TRAP_POLICY:
        opening += "\n\n" + TRAP_POLICY[level]
    if initial_spec is not None:
        opening += "\nAn earlier draft is staged. Use read_draft and check_draft to inspect and repair it."
    if level in CATEGORY_GUIDANCE:
        opening = CATEGORY_GUIDANCE[level] + "\n\n" + opening
    opening = (REVISION_MANDATE if revision else AUTHORING_MANDATE) + "\n\n" + opening
    if feedback:
        opening = ("Design or revise using the constraints and findings below. Confirm which findings apply "
                   "against the actual source. If the source is inadequate or cannot be used fairly, reject the "
                   "seed instead of disguising a source defect as difficulty.\n\n"
                   + "\n".join(f"  - {line}" for line in feedback) + "\n\n" + opening)
    if findings := scan(bundle / "environment/repo"):
        opening = ("PRIVATE DATA IN THE FIXTURE -- validation will reject these findings: "
                   + json.dumps(findings) + ". Exclude the file (materialize_fixture with exclude=[...]) "
                   "or replace it with write_fixture_file using placeholder values.\n\n" + opening)
    messages: list[dict[str, Any]] = [{"role": "user", "content": opening}]
    request_config = {"model": MODEL, "effort": ARENA_EFFORT, "max_steps": max_steps, "max_tokens": 48000,
                      "tools": tools, "system": SYSTEM}
    if retained:
        original = retained["config"]
        retired = [tool["name"] for tool in original.get("tools") or []
                   if isinstance(tool, dict) and tool.get("name") in RETIRED_TOOLS
                   and tool.get("name") not in {t.get("name") for t in tools}]
        comparable = dict(original, tools=[tool for tool in original.get("tools") or []
                                           if not (isinstance(tool, dict) and tool.get("name") in retired)])
        changed = sorted(key for key, value in request_config.items()
                         if key != "max_steps" and comparable.get(key) != value)
        if changed:
            atomic_json(log / "continuation-invalidated.json", {
                "from": resume_session, "changed": changed, "remaining_steps": max_steps,
                "draft_retained": retained.get("draft") is not None, "budget_reset": False})
        else:
            messages = retained["messages"]
            if retired:
                messages.append({"role": "user", "content": f"The retired tool(s) {', '.join(retired)} were "
                                 "removed from this session; continue with the tools available."})
        atomic_json(log / "continuation.json", resume_session)
    atomic_json(log / "request-config.json", request_config)
    atomic_json(log / "conversation.json", messages)
    checked_clean = False
    consecutive_errors = 0
    accepted_submissions = 0
    import sys as _sys
    preflight_pending = bool(policy.SUBMIT_PREFLIGHT and "submit_preflight" in handlers and (
        "unittest" not in _sys.modules or os.environ.get("ARENA_TEST_SUBMIT_PREFLIGHT") == "1"))

    def prepare(spec):
        spec = copy.deepcopy(spec)
        spec.setdefault("source", {"session_id": extraction.session_id, "seed_index": extraction.seed_index,
                                   "tier": row.get("tier", 4)})
        spec.setdefault("target_level", level or "")
        if type(row.get("step_budget")) is int:
            spec["step_budget"] = row["step_budget"]
        spec.setdefault("usable_corrections", extract_mod.usable_correction_count(row, extraction))
        return spec

    def finish(spec, steps, reason):
        spec = prepare(spec)
        status = creation.inventory(spec, bundle)["status"]
        spec["_agent"] = {"steps": steps, "self_checked": checked_clean,
                          "creation_status": status, "validation_status": "deferred",
                          "controls_advisories": []}
        spec["_producer"] = {"model": MODEL, "effort": ARENA_EFFORT}
        spec.setdefault("solution_notes", "The reference applies a patch derived from the trace's accepted "
                        "state (or authored, as its provenance says) and the hidden tests compute their "
                        "verdict from the changed program.")
        creation.record(spec, bundle, reason=reason)
        atomic_json(log / "conversation.json", messages)
        progress.submitted(bundle, log, spec)
        return spec

    def stop_stalled(reason: str, steps: int):
        draft = log / "draft.json"
        report = {"status": "authoring-stalled", "reason": reason, "steps": steps, "max_steps": max_steps,
                  "consecutive_tool_errors": consecutive_errors, "session": str(log.relative_to(bundle)),
                  "release_ready": False,
                  "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest() if draft.exists() else None}
        atomic_json(log / "conversation.json", messages)
        atomic_json(log / "stalled.json", report)
        creation.record(prepare(handlers["draft_spec"]()), bundle, reason=reason, finalized=False)
        progress.author_boundary(bundle, log, stage="stalled")
        raise AuthoringStalled(report)

    for step in range(max_steps):
        if step == max_steps - 3:
            messages.append({"role": "user", "content": "Three response turns remain. Prioritize writing the "
                             "remaining task components and submit your strongest candidate even if checks or "
                             "controls have not passed. Validation runs after submission. Preserve completed "
                             "work; unfinished work will be recorded as unfinished."})
        fit_context(messages)            # each shortened result carries its own note
        atomic_json(log / "conversation.json", messages)
        progress.author_boundary(bundle, log)
        body = {"model": MODEL, "max_tokens": 48000, "system": SYSTEM, "output_config": {"effort": ARENA_EFFORT},
                "tools": tools, "messages": messages}
        # The markers go on a per-request copy; `body`, `messages` and the
        # stored conversation stay unmarked (see `with_cache_breakpoints`).
        if budget is not None:
            with budget:
                response = call_with_retained_submit(_designer_call, with_cache_breakpoints(body), handlers, max_steps=max_steps)
        else:
            response = call_with_retained_submit(_designer_call, with_cache_breakpoints(body), handlers, max_steps=max_steps)
        progress.update(bundle, stage="authoring-tools")
        atomic_json(log / f"response-{step + 1:03d}.json", response)
        if response.get("stop_reason") == "max_tokens":
            messages.append({"role": "user", "content": "The response was truncated; no tools from it were executed. "
                             "Stage smaller sections with stage_spec, then continue."})
            continue
        blocks = response.get("content", [])
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        if not calls:
            messages.append({"role": "assistant", "content": blocks})
            messages.append({"role": "user", "content": "Continue. Use the tools; finish with submit_draft, or "
                             "reject_seed with grounded source-capacity reasons."})
            continue
        emit = next((b for b in calls if b["name"] in {"design", "submit_draft", "reject_seed"}), None)
        if emit is not None and len(calls) > 1:
            messages.append({"role": "assistant", "content": blocks})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                             "is_error": True, "content": "Call submit_draft / design / reject_seed alone."} for call in calls]})
            continue
        if emit is not None:
            if emit["name"] == "reject_seed":
                try:
                    report = _rejection(bundle, emit["input"], trace)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    messages.extend([{"role": "assistant", "content": blocks}, {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": emit["id"], "is_error": True,
                        "content": f"Invalid source-capacity receipt: {exc}"}]}])
                    continue
                report.update(model=MODEL, effort=ARENA_EFFORT, steps=step + 1,
                              status="source-capacity-finding", phase="creation-advisory", validation_deferred=True)
                atomic_json(log / "source-rejection.json", report)
                spec = handlers["draft_spec"]()
                spec["_source_capacity_report"] = report
            else:
                spec = handlers["draft_spec"]() if emit["name"] == "submit_draft" else emit["input"]
            spec = prepare(spec)
            verdict = handlers["submit_check"](spec)
            if verdict["clean"]:
                accepted_submissions += 1
                status = creation.inventory(spec, bundle)
                atomic_json(log / "submissions" / f"{step + 1:03d}.json", spec)
                atomic_json(log / "submissions" / f"{step + 1:03d}-creation.json", status)
                messages.append({"role": "assistant", "content": blocks})
                acknowledgement = {"saved": True, "validation_deferred": True,
                    "creation_status": status["status"], "missing_components": status["missing_components"],
                    "next": "Write the missing components with stage_spec and fixture tools, then submit again. "
                            "No quality check, control or difficulty result is required." if not status["components_present"]
                            else "All authored components are present; validation is next."}
                findings: list[str] = []
                if (preflight_pending and status["components_present"] and emit["name"] == "submit_draft"
                        and step < max_steps - policy.SUBMIT_PREFLIGHT_RESERVE_STEPS):
                    preflight_pending = False
                    try:
                        findings = handlers["submit_preflight"](spec)
                    except Exception as exc:  # noqa: BLE001  the preflight never blocks a submission
                        findings = []
                        acknowledgement["preflight_error"] = f"{type(exc).__name__}: {exc}"[:400]
                    atomic_json(log / "submissions" / f"{step + 1:03d}-preflight.json", {"problems": findings})
                    if findings:
                        acknowledgement.update({
                            "preflight": findings[:40],
                            "next": "SAVED -- and the one-time submit preflight found problems validation will "
                                    "reject (static validation and the executed controls). Fix them now with the "
                                    "same tools, re-run run_controls, then call submit_draft again; the next "
                                    "submission finishes this session whatever it shows."})
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": emit["id"],
                    "content": json.dumps(acknowledgement)}]})
                if not findings and (status["components_present"] or emit["name"] == "reject_seed"
                                     or step == max_steps - 1):
                    return finish(spec, step + 1, "components-written" if status["components_present"]
                                  else "source-capacity-finding" if emit["name"] == "reject_seed" else "session-budget-exhausted")
                # Acknowledgement is successful. Keep the candidate as the live
                # draft so one-shot `design` submissions can be completed too.
                handlers["retain_draft"](spec)
                consecutive_errors = 0
                creation.record(spec, bundle, reason="writing-missing-components", finalized=False)
                continue
            messages.append({"role": "assistant", "content": blocks})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": emit["id"],
                             "is_error": True, "content": "That draft does not pass. Fix exactly these and re-submit:\n\n"
                             + "\n".join(f"- {p}" for p in verdict["problems"])}]})
            continue
        results = []
        turn_failed = True
        for call in calls:
            fn = handlers.get(call["name"])
            try:
                out = fn(**call["input"]) if fn else {"error": "no such tool"}
            except Exception as exc:  # noqa: BLE001
                out = {"error": f"{type(exc).__name__}: {exc}"}
            failed = isinstance(out, dict) and bool(out.get("error"))
            turn_failed = turn_failed and failed
            if call["name"] in {"check_draft", "check_spec"} and isinstance(out, dict) and out.get("clean"):
                checked_clean = True
            from ..dump.evidence import tool_content
            encoded = tool_content(out)
            if isinstance(encoded, list):
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": encoded})
                continue
            if len(encoded) > 60000:
                encoded = json.dumps({"error": "Response exceeds the tool window. Request fewer rows, or page with "
                                      "char_offset.", "truncated": True})
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": encoded,
                            **({"is_error": True} if failed else {})})
        consecutive_errors = consecutive_errors + 1 if turn_failed else 0
        messages.append({"role": "assistant", "content": blocks})
        messages.append({"role": "user", "content": results})
        if consecutive_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
            stop_stalled(f"{consecutive_errors} consecutive turns in which every tool call failed", step + 1)
    if accepted_submissions:
        return finish(handlers["draft_spec"](), max_steps, "session-budget-exhausted")
    stop_stalled(f"agent designer reached {max_steps} steps without a submitted candidate", max_steps)
