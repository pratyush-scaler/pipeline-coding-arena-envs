# Failure modes that genuinely bite frontier coding agents (evidence, 2026-09-30)

Question asked: which failure modes make Fable 5.1 (Claude Code 2.1.283) and GPT-6 Astra (Codex 0.157.1),
at high effort with no turn or time limits, fail a coding task for their own reasons, and can one of them
push pass@1 below 0.5 on both.

Every claim below names its evidence. "Measured here" means a 5x2 screen in this workspace on the
forgery-hardened runtime with valid episodes, attribution `capability`, no reward hacking. "Census" means the
parent programme's 2,557-episode near-miss census (`pipeline-artifacts/research/trajectory-nearmiss-report.md`);
"catalogue" its external-evidence survey (`frontier-agent-failure-modes.md`); "corpus" the 370 coding sessions of
the trace dump (`analysis/CODING-TRACES.md`).

## 1. What has been measured on coding tasks so far

| task | traps | Fable p@1 | Astra p@1 | what bit |
|---|---|---|---|---|
| c-001-arabic-assistant-memory (09-27) | 8 | 1.00 | 1.00 | nothing |
| c-001-turbomachinery-cad-tool | 21, three steps | 1.00 | 1.00 | nothing; two blind solves 1.0 |
| c-003-next-js-social-app | 10, two steps | 1.00 | 1.00 | nothing; four blind solves 1.0 |
| c-010-python-text-parsing | 12, two steps | **0.40** | 1.00 | two Fable modes, below |

The trap families that did nothing on coding tasks in 40 clean episodes: symptom-vs-cause with a mock that
agrees, chained defects, decoy fix location, restraint (a file or name that must not change), regression
bait, revert residue, a premise revised in a later step, hidden guard budgets. Both models reproduce the defect
with real code, run the visible inputs, and their fixes generalise. This matches the census: forbidden-edit
traps 0 failures in 5,210 attempts; authored trap labels ~1% failure regardless of type.

## 2. The two modes that bit, both on Fable, both genuine

Read from the episode artifacts (`build_set.py` each agent shipped), the hidden dumps and the transcripts.

**M1. Rule-scope over-generalisation.** The instruction names two placeholders to drop, "(Visual: ...)" and
"[Image ...]", and says "but only those, normal brackets inside a question stay". Two of five Fable episodes
wrote `(?:Visual|Image|Figure)` and `(?:Visual|Figure|Diagram|Image)`; the hidden s14 dump has a stem containing
"(Figure: the three circles are printed side by side)". One of the two restates the rule correctly in its own
docstring (Visual and Image only) and broadens the code anyway. Astra wrote `\(\s*Visual\s*:|\[\s*Image\b` five
times out of five. Census rank 1 ("population scope of a rule"): Fable 15 failed decisions, Astra 19 on
documents, but on code Astra encodes the literal rule.

**M2. Two published rules that interact on a rare input.** "Exactly four option lines a) b) c) d) in that order"
and "it keeps the letter the sheet puts in front of it". Hidden s12 question 8 prints A, C, B, D. One Fable
episode (and one in the earlier screen) kept sheet order; the transcript never considers the ordering sentence.
Astra validates the letter set and sorts by letter in every episode; one comments that labels stay attached to
their text through the sort.

What both modes share: a stated rule whose scope the model completes from what looks natural, visible inputs
that agree with the wrong completion, and a hidden input where it diverges. This is the coding form of the
census's rank-1 type, and of the corpus's 193 spec corrections ("the agent built the wrong thing; the user
restates the requirement").

## 3. Why Astra did not fall, and what bites Astra

Astra programs the answer: 98% of its steps assert invariants, it hashes sources, and it reads a stated rule
literally. It has no measured miss on rule scope or rule interaction in code. Its one blind-verified genuine
bite in the whole programme is a different mode:

**M3. The same entity or quantity under a surface variant the obvious predicate misses.** Census rank 3: Astra
8 of 10 failed decisions (m.facebook.com hosts under an exact-host predicate); a-005 v3 and v4: Astra 0/5 twice
when one set was named S08, 508, Model Set 508 and Loksewa S08 (blind adjudicator ~85% agreement that it is one
set). Fable passed all of these: it reads listing by listing and flags the variant. The coding form is a FORMAT
PREMISE: the same fact arriving in a second shape the visible samples never show, while the published rule
covers it. The corpus has it verbatim: the streamlit trace's timestamps arrived as `00:00:16,585` and the
parser expected seconds. On c-010 the answer already comes in two shapes (a letter, or the option's text) and
Astra handled both, so the variant must be one the visible dumps do not exhibit at all.

## 4. Modes with strong external evidence, not yet measured here

**M4. Nested exception chains in business rules** (catalogue rank 3): base rule, exemption, carve-back,
override. Opus-class models 0/70 on failing scenarios without tools; the family skew is two-sided (Claude denies
what only the override grants, both grant what the carve-back revokes). Authorization and eligibility logic in a
backend is the natural coding host (next.js's role grants already have a base rule and a self-escalation guard).
Design rule R4: one hidden case that qualifies only through the override, one revoked by the carve-back.

**M5. Verification that inherits the misreading** (catalogue rank 7, amplifier). The agent's own check agrees
with its plan: a mock, a fixture sample, or a script written from the same reading. Not a lever on its own; it is
what makes M1 to M4 silent. The design consequence is that the visible sample and the natural check must both
agree with the wrong reading (R2).

## 5. What does not bite, measured

Enumeration, breadth, volume, arithmetic, decoy values that can be compared side by side, restraint rules,
stated precedence, trap labels, "note that", worked examples on the trap item, non-English fixtures, stacked
traps of one type. On coding tasks additionally: symptom-vs-cause, chained defects, decoy locations, revert
residue, premise revisions announced in a later step. Reaching for the interpreter or the hidden suite is a
reward-hacking route, now refused by the runtime, not a failure mode.

## 6. Can one mode reach pass@1 < 0.5 on both models?

Not one mode. M1 and M2 have bitten Fable and never Astra; M3 has bitten Astra and never Fable. No task in the
programme has measured below 0.5 on both. The design that could: a task whose full success needs M1 or M2
(silent scope, hidden divergence) AND M3 (a format or identity variant the visible inputs never show), each
routed through at least three objectives, each proved by a mutant and a blind solve, with every deciding
sentence stated once and never announced. M4 is the untested candidate that the external evidence says is
two-sided.

## 7. The experiments now running or queued

- **c-010 attempt 1** (launched 14:55): deepen M1 and M2 within an F3 budget that allows no authored traps
  (tighten trace-derived traps, more divergent hidden inputs, at least three objectives per premise, step 2
  re-hits both). Measures whether coupling turns Fable's 0.40 into a lower number; Astra expected unchanged.
- **turbomachinery attempt 2** (queued behind its fresh screen): carries an M3 lever (the exporter's `m_pct`
  / `p_pct` names for the viewer's `camber_max_pct` / `camber_pos_pct`), M1 on the spanwise stations, and a
  stored derived figure. First reading of M3 on numerical code.
- **next.js attempt 1** (after its binding screen): M4 in the role-grant rules with two-sided hidden cases,
  plus an M3 variant in the seeded data. Needs the trace to ground the rules (F3 budget to be read).

Every screen is 5 x 2 at high effort with no limits; every failure is read from its trajectory before it
counts; identical wrong answers across one model's episodes are checked for ambiguity before they count.
