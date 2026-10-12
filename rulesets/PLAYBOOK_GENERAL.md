# Agent Benchmark Task Authoring — General Playbook

A benchmark-agnostic method for authoring hard, *valid* tasks for agentic evaluation.

This is the generalized form of `PLAYBOOK.md`, which applies the same method to one
specific programme (computer-use SaaS tasks, AutomationBench register). Nothing here
assumes a domain, an app stack, a harness, or a scoring format. Where a claim rests on
measured evidence, the source is named so you can decide whether it transfers.

Throughout: **environment** is whatever the agent acts on (apps, repos, APIs, a shell);
**surface** is one addressable part of it; **fixture** is the initial state you author.

---

## 1. What you are actually measuring

A benchmark task can load three largely independent difficulty axes. Most weak tasks
load exactly one.

| Axis | Question | Fails when |
|---|---|---|
| **Inference** | Can the agent *work out* the answer? | it picks a plausible wrong entity |
| **Execution** | Can it carry a long dependent chain? | the chain breaks early and everything after is unreachable |
| **Restraint** | Can it avoid changing what it shouldn't? | it "helpfully" edits something outside scope |

Restraint is the cheapest difficulty per unit of authoring effort, and the most
neglected. A restraint check costs one assertion, needs no instruction text — the agent
is measured on what it *doesn't* do — and it is usually the axis that separates strong
models, because capable agents over-act.

**Design intent:** load at least two axes per task. A task that loads only execution is
a long-form instruction-following test.

---

## 2. The five difficulty levers

Ordered by how much signal they produce per unit of authoring cost.

### Join — the answer exists only in an intersection
No single surface answers. The agent must build two sets and relate them.

> *Which customers appear in set A and not set B, where neither set is enumerated
> anywhere?*

Strongest inference lever, and it does not require a long action chain. In one corpus
the widest model gap observed on any task was a Join (0.00 vs 0.83 between two frontier
models).

### Arbitration — sources conflict and one is authoritative
Seed three places that state a value and make two of them stale or wrong.

**Non-negotiable:** the instruction must make the authoritative source unambiguous.
Ambiguity here yields defensible wrong answers, which is a broken task, not a hard one.

### Carry — a value read on surface A must appear on surface B
And **the grader recomputes the target live from A at grade time**, never hardcodes it.
This is the single most effective defence against both hardcoding and decoys, and it is
what makes a multi-surface task genuinely multi-surface.

### Restraint — negative space
"Leave everything before X as it was." "Don't notify anyone outside the named group."
"Records matching Y must remain absent." Cheap, high-signal, and it is where the
guardrails come from.

### Sequence — ordering enforced by creation time
Powerful but brittle. It is the lever most likely to become an unclearable gate.
**Never make a sequence check a prerequisite for anything.**

### The load-bearing test
For every surface in the task: **remove it and see whether the correct answer changes.**
If it doesn't, that surface is decoration.

This matters more than surface *count*. In one corpus, tasks touching ≤3 surfaces
produced a mean inter-model spread of 0.413, while 7-surface tasks produced 0.126 — one
of the 7-surface tasks scored identically for both models. A separate public corpus of
606 tasks has a median of 4 surfaces and a maximum of 8. Breadth is not difficulty;
coupling is.

### The sixth lever — measurement breadth (SUPERSEDED, see the seventh)

> Retained for the record. Its breadth claim rests on 24 tasks at one cold
> episode each and was falsified by a 369-check, two-model measurement; the
> seventh lever below replaces it. Exactness and cascade survive.

The five levers above order *authoring* effort. This one is different: it is the
only property that separated hard tasks from easy ones when 24 tasks were
screened against Fable 5.1 at high effort, one cold episode each.

    0.18  education-test-prep-qa
    0.37  content-writing
    0.38  creative-video-scripting
    1.00  the other twenty-one

Anatomy of the 94 objective checks on those tasks:

    property                             in FAILED checks   in PASSED checks
    threshold over a word count               4/11 (36%)         0/83  (0%)
    any numeric threshold                     7/11 (64%)         6/83  (7%)

Every check applying a numeric threshold to a word count the model must compute
itself over prose has failed, every time it has been screened. None of the 83
passed checks uses the construct.

**The lever is a quantity the model must establish itself, once per item, across
many items, where no surface states it and it cannot check its own work.** Given
that, a frontier model satisfices. On the 0.18 task it returned an *empty* set
where the reference held nineteen entries, while getting the bounded parts of
the same submission — a malformed-record list, a foreign-key conflict count —
exactly right. It did the finite work and abandoned the exhaustive work.

Two constructs have been observed to fail:

- **Counted quantity.** A published threshold over words, sentences or
  paragraphs in a section body. Needs ≥15 sections.
- **Pairwise judgement.** A per-record comparison across two joined record sets
  — items where both sources state a value and they differ. Needs ≥50 records.

Three properties were proposed. **Breadth has since been falsified; the other
two survive.** See the next section for the measurement that settled it.

1. ~~**Breadth.**~~ Falsified. Measured over 369 checks with real calibration
   data, breadth is *anti*-correlated with difficulty: see below.
2. **Exactness.** `set_equals`/`equals` with no tolerance. A tolerance wide
   enough to absorb one mis-measured item makes the check unable to distinguish
   the work from a near miss. Still holds.
3. **Cascade.** Every other figure recomputes from the same measured quantity.
   One miscounted section failed four of six checks on the 0.37 task; one
   abandoned comparison failed five on the 0.18 task. A root check with no
   cascade caps the damage at its own weight. Still holds, and is now the
   *primary* lever rather than a supporting one.

**What does not work, measured.** Chained computation. The two hardest tasks in
the batch have the *least* derivation depth (8:19 and 9:21 derive:retrieve); two
1.00 tasks have the most (305:360, 221:276). A contested set whose boundary the
evidence only partly settles was built carefully into three tasks and every one
still scored 1.00 — because the census counted rows in a markdown table, and
reading a table is not measuring anything. Do not add derivation, date
arithmetic, output fields, files or instruction length. A longer task is not a
harder one and it costs more to measure.

### The seventh lever — a premise that changes (supersedes the sixth)

The breadth finding above came from 24 tasks at *one cold episode each*. Re-run
against every check in the batch that has real calibration data — 369 checks,
n≥4 episodes, two models at high effort — it inverts:

    measured fail rate   checks   members   source docs   rows scanned   named lookups
    always fails (100%)       7       5.0           2.1             41            0.6
    sometimes (30-99%)       34       6.5           2.6             22            1.3
    ~never fails (<30%)     331       6.2           2.7           2532            5.8

The checks that never fail have **more** members, **more** source documents and
**sixty times** more data than the ones that always fail. Counterexamples that
settle it: `c_rework_census` — 14 members over 14 separate documents — fails
0/4. `c_claims` (30 members) and `c_coupling` (24 members) fail 0/10. `a-197`
carries an 11-member record over 8 documents, a 20-member record and a 15-member
record, and the whole bundle sits at pass@1 1.00. Going the other way, three
checks with **two** members over **one** file fail 100%, one of them over a
nine-row fixture.

A large enumeration is mechanical, and a frontier model grinds it out correctly.
Adding members, documents or rows buys nothing and costs measurement time.

**What difficulty actually tracks: a premise that changes, or a conflict over
which document governs, that silently invalidates a result the model has already
computed.** The model is not defeated by size; it is defeated by having been
wrong earlier and not going back.

Difficulty therefore clusters by *bundle*, not by check. Four bundles own nearly
every failing check, and in each one a single premise trap poisons several
checks at once — which is what drives pass@1 to 0, since pass@1 counts full
success:

- `a-196` — step 1 measures sections thin at a 100-word line; the revision
  raises the line to 120. Every section must be re-examined; the model reuses
  its step-1 set. Three checks fail 64–93%.
- `technical-writing` — depth re-measured at a revised minimum. Four checks fail
  89–100%.
- `youtube-seo` — a rebrand is announced, and the rebrand guide still names the
  retired word in its own recommendations. A reviewer who reads the headline and
  stops reports the rebrand as done. Three checks fail 100%.
- `brand-consulting-003` — the English and French charters disagree; a colour the
  English charter declares is never named by the French one, and the blueprint
  paints in three greys no charter declares. Its sibling `brand-consulting-002`
  has the same *shape* with 12 members over 3 documents and no disagreement, and
  fails 0/4.
- `a-416` — several documents state caps and only one governs each, plus a
  published strict-inequality convention ("under N words" → a maximum of N−1).

Three constructs, all evidenced above:

- **Revision.** A threshold, scope or definition moves *after* a first result is
  computed, so the earlier answer is no longer valid. Not a new item to count —
  a changed rule over items already counted.
- **Governing conflict.** Two documents state the number, name or status
  differently, and the instruction says which governs, so the model must notice
  the obvious document is not the governing one.
- **Residue.** Something declared done or retired is still present in the very
  document that declares it done.

Keep the affected population **small** — every measured killer lives between 9
and 130 rows. There must be no mechanical grind for the model to hide behind.
Then route several existing checks through the trap, so one wrong premise costs
several checks.

Fairness is unchanged: the rule must be published in the instruction — which
document governs, what the revised threshold is, and that earlier results must be
re-derived under it. The model must fail at *applying* a rule it was given.

### Limits of the expression language, found by measurement

These cost authors real designs in this batch. Check them before promising a check:

- **A field name containing a period is unresolvable.** `Time (approx.)` returns
  nothing for `present`, `matches` or any other assertion, while `Source` and
  `Event` on the same rows work.
- **`extract` requires exactly one match**, so a repeated pattern cannot be
  enumerated — and no parser yields one row per line of a fenced block or a grep
  listing. Build per-item fields with unique anchors instead.
- `min` and `max` DO exist (`checks.py:642`). An earlier note here said otherwise; it
  was wrong and was propagated to authors before anyone checked the source.
- **`matches` is CASE-INSENSITIVE** (`re.I`). `^[A-Z]` matches `docker`, so a pattern
  cannot be used to test capitalisation. Verified: it cost an author a design that
  silently admitted eight rows where seven were intended. Key on something else —
  "contains no digit", a structural marker — not on letter case.
- **A numeric comparison on an empty cell RAISES**: `gt`/`lt` over `''` gives
  `CheckError: expected a number, got ''`. The null-operand lesson is not limited to
  `equal` joins — guard every numeric filter with a shape test first.
- **`filter … matches` takes a fixed pattern, not a per-row value**, so there is no
  per-row cross-document lookup: you cannot ask "is this module named in §10?"
- **Object keys cannot be enumerated**, so a key-coverage audit over a JSON
  catalogue is unexpressible; `jsonl_records` returns the whole object as one row.
- **Deliverable object keys cannot contain dots**, so a filename makes a poor key.
- Values returned verbatim keep their markup: a table cell arrives with backticks,
  a bold heading with `**`, and `normalise` folds neither. Requiring a model to
  reproduce emphasis punishes formatting, not reasoning — grade a count instead.

**You cannot add this on top.** `author/quality.py` caps a task at ten
objectives (4-6 core, ≤2 floor, 1-2 stretch) and requires ≥4 *distinct* core
decisions. A root check bolted onto a full check set fails the gate however good
the design, so the construct is paid for by removing **transcription checks** —
the ones that only restate a figure the fixture already prints, which a frontier
model gets right every time and which therefore buy no discrimination.

Do not read that as "fewer checks is better". The measurement construct spans
only one or two decisions, so cutting to 4 core forces the transcription checks
to survive *as core* and pushes a root check out — backwards. Five or six core
is correct when the construct spans fewer than four decisions. **The target is
weight concentration, not check count:** root check plus cascade carrying ≥50%
of total weight.

Check a draft before paying for a screen:

    PYTHONPATH=. .venv/bin/python check_difficulty.py <bundle>

It reports the root check, its measured breadth, and the share of weight that
falls with it. Over the twelve tasks screened it flags exactly the three that
scored below 1.00 and none of the nine at 1.00. It is one-sided: it can say a
task is unlikely to measure hard, never that a task *is* hard. `pipeline/breadth.py`
carries the full measurement.

---

## 3. The check model

### Two kinds of check

| Kind | Initial state | Effect | Gated? |
|---|---|---|---|
| **Objective** | fails until the agent acts | weighted contribution to score | may be |
| **Guardrail** | **passes** until the agent breaks it | violation → **score 0** | **never** |

Guardrails are never gated: a rule the agent was told to respect is broken whether or not
the objective chain reached that far.

Express guardrails as **concrete negative assertions** — *this specific record must not
exist*, *this specific recipient must not be contacted* — not as conceptual statements.
In a 606-task public corpus, guardrails run at **65% of the objective count** and only 12
tasks have none at all. This is a descriptive observation, not an authoring quota.
Use only genuine negative obligations supported by the assignment and enforced by
the grader. Do not add copied registers or restated correct conclusions to meet a
count, and do not convert easy positive objectives into zeroing guardrails.

### Three difficulty bands

Declare intent up front, then confirm it by measurement:

| Band | Target pass rate | Count | May gate? |
|---|---|---|---|
| floor | ~100% | ≤2 | never |
| **core** | **30–70%** | 4–6 | **only these** |
| stretch | <30% | 1–2 | never |

**Only a check measured in the 30–70% band may gate anything.** This is the single most
important rule in this document.

### Grade by per-check rate, never by rubric mean

A rubric mean hides two opposite defects — a check nobody fails and a check nobody passes
both pull it toward the middle. Report per-check pass rates.

### `gated_out` is not `failed`

When a prerequisite fails, dependent checks did not *fail*; they were never attempted.
Recording them as failures is precisely what makes an over-gated task look uniformly hard
rather than broken. Compute pass rates over **reachable attempts only**.

---

## 4. Task validity: every failure must be the model's

**A score is meaningful only if every zero is attributable to model incapability.**

A zero caused by a grader defect, a flaky environment, an infrastructure limit, or an
ambiguous specification is a measurement error — and it is worse than a missing data
point, because it is indistinguishable from a real result. It inflates apparent
difficulty, survives into the results table, and gets read as evidence about a model.

Every task must be able to answer: *if a rollout scored 0, was that the model?*

### The five ways a zero can be invalid

| Class | Typical cause | Control |
|---|---|---|
| **Infrastructure** | verifier OOM, timeout, container death — often silent, and often *after* the agent has run | size resources to measured peak, not to the average; a missing score file is a **void trial**, never a 0 |
| **Environment** | a surface absent or unhealthy, so its state reads empty and every check on it fails | declare required surfaces; assert presence; surface a mismatch as an **evaluation error** |
| **Grader** | brittle comparison, a defect in a gating check, a check that reads the fixture rather than the agent's work | fixture-zero verification; normalize comparisons; never gate an exact-string match |
| **Specification** | a rule the instruction implies but the fixture never states, so two defensible answers exist | the careful-practitioner read-back; fix the **fixture**, not the instruction |
| **Non-determinism** | mutable image tags, drifting fixtures, wall-clock dependence | pin by digest; freeze fixture + instruction + grader as one unit |

### Classify every zero before it reaches a results table

A run that produced no score file, hit a state-fetch or state-shape error, or ran against
an incomplete environment **did not score 0** — it produced no score. Exclude it, re-run
it, record why. Keep "evaluation problem" and "incorrect answer" visibly distinct in
every table you publish.

### Two design consequences for the runner

- **A guardrail that raises should be treated as intact, not violated.** An evaluation
  fault must never manufacture a zero the agent did not earn.
- **Report `gated_out` separately from `failed`**, so an unreachable criterion is never
  counted as something the model got wrong.

### Report limitations honestly

State plainly what the runs do *not* establish. A correct final answer does not prove
every intermediate step was sound; a passing rubric does not prove the agent left
everything else untouched. Say which, rather than implying more than the grader checked.

---

## 5. Calibrating instruction specificity

The hardest authoring judgment is how much to say. There is one rule:

> **State the rules the agent could not reasonably discover. Push everything else into
> the fixture.**

Length is an *output* of that decision, not a target. In the 606-task public corpus,
prompts run 11–192 words (median 66). The 11-word one works because precedent carries the
entire specification ("…same as usual"); the 192-word one is long because its rules are
arbitrary policy existing nowhere else.

| Put in the instruction | Push into the fixture |
|---|---|
| arbitrary policy that exists nowhere else | anything a real system would already record |
| the time anchor ("today is…") | thresholds, rates, rosters, org structure |
| the deliverable and its recipient | formats set by precedent ("the usual format") |
| a genuine trap a colleague would flag | which records are stale — say *that* some are, never *which* |
| **never:** coordinates, paths, or navigation steps | |

That last row is the real line. In the public corpus, even the 192-word instruction
specifies business rules and never a cell address or a menu path. Dictating coordinates
converts a judgment task into a transcription task — and makes the task voidable by a
typo in the instruction.

**The trade is not free.** Every rule removed from the instruction must exist,
discoverable, in the fixture. A terse instruction over a thin fixture is not a hard task;
it is an unanswerable one, and it calibrates at 0% exactly like a bad gate.

---

## 6. The nine stages

Stages 2 → 3 → 4 are authored in that order and never reversed.
Stages 6 → 7 → 8 loop until the bands are right.

| | Stage | Output |
|---|---|---|
| 0 | Scope: role, environment, surfaces | surface list |
| 1 | Scenario brief + **novelty line** | `brief.md` |
| 2 | **Fixture design — before any prose** | `fixture.md` |
| 3 | **Check list — before any prose** | criteria file, human-facing rubric |
| 4 | Write the instruction | `instruction.md` |
| 5 | Implement the grader | `grader.*` |
| 6 | Fixture-zero verification | pass/fail gate |
| 7 | Calibration | per-check rates |
| 8 | Rebalance bands and gates | revised rubric |
| 9 | Freeze and ship | versioned bundle + results row |

### Stage 1 — Novelty line
One sentence: what does this ask that no existing task asks? If the honest answer is
"the same question on different data", change the **lever**, not the numbers. A re-skin
adds no signal — a model that can do the original can do the copy. Reusing the *world*
is fine and cheap; reusing the *question* is not.

### Stage 2 — Fixture design
Fix four things before writing a word of prose:

1. **Ground truth** — the correct end state, computed by hand.
2. **Decoys** — plausible-but-wrong records. The best decoys are *structurally* similar
   to the right answer and differ on the one attribute the task turns on.
3. **Conflict sources** — which surfaces carry stale values; which is authoritative.
4. **Negative space** — what must survive untouched. This is where guardrails come from.

Then list every business rule the instruction won't state, and where it lives.

Make decoys *disagree with each other*. Two stale sources that contradict each other
cannot both be copied, which forces the agent to compute.

### Stage 3 — Check list
Enumerate the substantive criteria with id, kind, weight, prerequisites, intended band, and how each
target is computed at grade time (static vs. recomputed live). Sanity-check before
proceeding:

- ≤2 floor, 4–6 core, 1–2 stretch
- each guardrail protects a genuine negative obligation; no count or ratio quota
- every prerequisite edge points at a *core* check
- no exact-string check and no sequence check gates anything
- ≥1 check recomputes its target live from another surface

### Stage 4 — Instruction
Written to §5. Verify the omissions: for every rule left out, point at the fixture record
that carries it. If you can't, return to Stage 2 — do not put it back in the prose.

Read it back twice: *Could a careful practitioner produce a different defensible answer?*
(if yes, it's ambiguous, not hard — fix the fixture). *Does this tell the agent anything a
competent colleague would have had to work out?* (if yes, cut it).

### Stage 5 — Grader
One function per criterion. Cross-surface checks recompute their target live. Normalize
comparisons — case, whitespace, punctuation, markup. Filter soft-deleted records. Return
a numeric score; a string where a number is expected can void a trial silently.

### Stage 6 — Fixture-zero verification
Run the grader against the pristine fixture. **Every objective must score 0; every
guardrail must score 1.** An objective that passes on an untouched fixture is reading a
seeded record rather than the agent's work. A guardrail that fails there is
mis-specified. Either burns a whole calibration batch before anyone notices.

### Stage 7 — Calibration
At least 3 rollouts × 2 models. **Freeze fixture, instruction and grader for the
duration.** Record each check as passed / failed / gated_out, and classify every zero
per §4.

### Stage 8 — Rebalance

| Observed | Meaning | Action |
|---|---|---|
| 100% pass | floor | keep ≤2 useful floor objectives; redesign or remove the rest, never relabel them as guardrails |
| 30–70% | signal | keep; gates may sit **only** here |
| 0% pass | too hard, or broken | **ungate immediately**, then diagnose |
| never reachable | dead weight | ungate or delete |

Re-calibrate after changes. Ship when ≥4 checks sit in band and no gate rests on a dead
check.

### Stage 9 — Freeze
Version fixture + instruction + grader together. Changing one without the others is how a
task silently regresses.

---

## 7. Bundle layout

```
tasks/<slug>/
  brief.md          scenario + novelty line
  fixture.md        what the initial state must contain
  instruction.md    the only file the agent sees
  operating-notes.md harness rules, shown alongside
  rubric.md         human-facing; carries the fixture-zero attestation
  <config>          image/digest, timeouts, resources, taxonomy metadata
  tests/
    criteria.json   id, kind, weight, requires, band
    grader.*        one function per criterion
    run.*           entrypoint
```

Keep taxonomy metadata populated — environment, interface, grading mode, task category,
answer surface. It is what lets you slice results later, and it costs nothing at
authoring time.

---

## 8. Anti-patterns, with evidence

### Gating on an exact-string match
In one corpus, five tasks scored **exactly 0.125 with both models** — one of eight
checks, every time. The never-cleared first gate in each was a verbatim stamp, and the
instruction's own two revisions spelled it with different dash characters. Eight criteria
voided by one character.

If an exact string matters, make it an **ungated floor check** and normalize punctuation.

### Re-gating without re-calibrating
The same task, promoted to gate on that stamp, moved from **0.875 / 0.25 → 0.125 /
0.125**. Across all shared tasks, the revision cut mean inter-model spread **0.225 →
0.049** — a 4.6× loss of discriminative power, while making every task *look* harder.

### Treating surface count as difficulty
See §2. The 2-surface tasks out-discriminated every 7-surface task in the same corpus.

### Shipping without Stage 8
A task where both models score identically teaches nothing, however hard it is. Both
ends fail equally: one corpus contains a task both models scored 0.00 on and another both
scored 1.00 on.

### Instructions that dictate coordinates
See §5. It converts judgment into transcription and makes the task voidable by a typo.

### Proofreading gaps in load-bearing strings
One shipped instruction contains `link it as 'realted'`. Exact strings are load-bearing;
proofread them.

---

## 9. Operational gotchas

These are harness-specific in their details but general in shape.

- **Size resources to measured peak, not average.** One verifier parsing a large combined
  state payload peaked at ~5× the document size and was OOM-killed with no exception, no
  output and no score file — *after* the agent had run. Near a limit it is a coin toss,
  not a property of the task.
- **A score file must contain a number.** A string can void a trial silently.
- **Grade against live state**, in the environment the agent actually modified.
- **Assert the environment is complete** before trusting any check that reads it.
- **Pin images by digest, never a mutable tag** — otherwise runs are not comparable
  across time.
- **Keep the grader and the reference answer out of the agent's environment.**
- **Prefer explicit, fail-loud seeding.** If an orchestrator declares intent to seed, a
  broken seed should crash the container rather than silently fall back to a default —
  otherwise you get a valid-looking run against the wrong fixture, which is §4's worst
  case.

---

## 10. Definition of done

- [ ] novelty line written: what this asks that no existing task asks
- [ ] `fixture.md` written before `instruction.md`
- [ ] every surface passes the load-bearing test
- [ ] ≥2 difficulty axes loaded; ≥1 lever beyond Carry
- [ ] every guardrail is traceable to real negative space and actual enforcement; no padding
- [ ] ≤2 floor, 4–6 core, 1–2 stretch
- [ ] no gate on an exact-string or sequence check
- [ ] instruction states rules, not coordinates; every omitted rule is discoverable
- [ ] fixture-zero clean: all objectives 0, all guardrails 1
- [ ] calibrated ≥3×2; ≥4 checks in the 30–70% band
- [ ] no gate rests on a dead check
- [ ] every zero classified: model failure, or void trial
- [ ] fixture + instruction + grader frozen together; image pinned by digest
- [ ] limitations section written, stating what the runs do not establish

## 11. Four things this batch measured, three of them against expectation

### 11.1 Record volume is not difficulty

Measured over 350 objective checks with at least four reachable episodes, both
calibration models, `gated_out` excluded from every denominator, members counted
as leaves of the target's record tree:

| bucket | n | mean members | mean source files |
|---|---|---|---|
| always fails (rate 1.0) | 7 | 5.00 | 2.14 |
| sometimes fails (0.3–0.99) | 34 | 2.12 | 1.74 |
| never fails (<0.3) | 309 | 3.15 | 2.21 |

The never-fail population carries **more** members than the discriminating one.
Individual cases settle it more firmly than the means, which rest on n=7:
`c_claims` (30 members over 6 files) failed 0 of 13; `capability_namespaces`
(25 members) failed 0 of 18; `CHK-CLONE` and `CHK-DEPFIT` (20 members each)
failed 0 of 6 each. Against `packaged_policy_conflicts` — 2 members, 1 file —
which failed 17 of 17.

Large enumerations are mechanical and models grind them out correctly. Adding
members to a check buys nothing. The dataset is `.check-difficulty-dataset.json`
with its query recorded in the file.

### 11.2 Two weight caps, and why routing must not use `requires`

Both in `author/validate.py`, independent — clearing one exposes the other.

`MAX_SINGLE_WEIGHT_SHARE = 0.35` on a check's own effective share, which is
`weight / that step's objective total / number of steps`. For a two-step task:
**no objective may carry more than 70% of its own step's objective weight.** A
step with one objective is 50% and is always blocked; `build()` gives each step
an equal share, so the spec's stored weights do not save you.

`MAX_GATED_WEIGHT_SHARE = 0.30` on weight gated through `requires`, counting the
gating check's own weight plus everything downstream. In a two-step task **any
`requires` edge inside a two-objective step is 50% and always blocked.**

So: route a premise through several checks, but never couple them with
`requires`. Coupling trips the second cap, and worse, it converts discrimination
into voids — when the upstream check fails the downstream one is never
evaluated, so the trap costs one failure plus one void instead of two observable
failures. Since voids leave every denominator, a coupled trap hides the
difficulty it installs. Give the thin step a second genuinely independent
objective instead, one unanswerable from the earlier step's file.

### 11.3 A rate must say which agent produced it

`pipeline/calibrate` hands the model `list_files`, `read_file` and `write_file`.
The shipped package declares Terminus-2 driving a tmux session — a shell. Both
are legitimate; a screen should be cheaper than what it screens for. What is not
legitimate is a `pass@1` that does not record which one ran.

The cost of not recording it: a-554 was read as an infeasible task because six
episodes refused it — "I couldn't complete the validation reliably with the
available read/write-only tools. The extract requires aggregation across 6,879
outlets" — when a shell agent would have run one `awk`. The refusal measured the
screen, not the task, and a whole rework was briefed on the wrong premise.

`pipeline/tool_surface.py` now stamps every rollout record, and a screening rate
carries "must not be reported as a calibration rate for the released task".

### 11.4 A filter the instruction does not state is a hidden grading rule

`author/validate.py::_hidden_rules` (Arena's kw006). Any check whose target
filters rows or applies a regex is applying a rule; it must carry `rule_quote`,
sentences copied verbatim from that step's own instruction, with the clause's
field and every literal it tests appearing together in one sentence.

This batch shipped 236 such checks across 28 of 30 bundles with no quote at all.
That is not a formality: a failure caused by a rule the agent was never told is
not a model failure, and no amount of difficulty work is worth anything until it
is fixed.

Two things learned doing it. Extract the literals **programmatically from the
target tree** — these fixtures carry Urdu, Arabic, Swedish, Polish, Chinese and
French, and a hand-retyped codepoint fails the verbatim match silently. And step
two has its own instruction text, never concatenated with step one's, so a
step-two check whose prose says "as defined before" publishes nothing and must
restate the convention.
