# Session selection and yield: what we learned

2026-10-05. Covers both pipelines across four checkouts:

| alias | checkout | dump |
|---|---|---|
| C1 | `arena-envs-codingtasks-large-dump` | kaizen |
| C2 | `arena-envs-coding-v2` | 9_29 accepted_500 |
| N2 | `arena-envs-non-coding-v2` | 9_29 accepted_500 |
| N1 | `arena-envs-tasks-large-dump` | kaizen |

Every number comes from receipts in those checkouts, read-only. Rates rest on 5 or
10 episodes per model per task, so small differences mean little.

"Hard" means every step has pass@1 <= 0.5 on both Fable 5.1 and GPT-6 Astra, and every
counted failure is certified genuine (not an ambiguity, void or grader defect).

---

## 1. Summary

1. **Coding yields. Non-coding does not yet.**
   - Coding: 28 tasks are READY. That is 5 of 17 validated in C1 and 23 of 58 in C2.
   - Non-coding: 2 tasks meet the bar genuinely, a-049 v4 and a-023 v2. No original
     non-coding design did (0 of about 55).
2. **In non-coding, the session does not decide hardness. The authored mechanism does.**
   - No session metadata separated hard tasks from easy ones.
   - 8 of 10 genuine bites sat in a document the designer wrote.
   - One session produced both a hard task (a-040) and an easy one (a-006).
3. **Session properties DO predict survival through classification,** in both pipelines.
   This is where better filtering upstream pays off measurably.
4. **In coding, some session properties also separate hard from stuck:**
   - short repos with a single deliverable;
   - input in a published external format with many surface variants;
   - single-step tasks;
   - no tests written by the trace's agent.
5. **Astra is the binding model in both pipelines.**
   - It fails only on unseen input shapes and identities, and on exact predicates.
   - Its 5 episodes are near-clones, so a task fails 5/5 or 0/5.

---

## 2. Where we stand

| | non-coding (N2) | coding C1 | coding C2 |
|---|---|---|---|
| Sessions in dump | 500 (282 non-coding) | 500 | 500 |
| Seeds surviving classification | 63 (v2) + 64 (batch 3) | 284 seeds / 220 sessions | 124 seeds / 91 sessions |
| Bundles built | 120 | 69 | 115 (some still running) |
| Validated | 59 | 16 | 56 |
| Hard and certified | **2** | **5** | **23** |
| Hard per bundle built | about 2% | about 7% | about 20% |

Sessions needed per day for 200 hard tasks, **at today's rates**:
- coding at C2's rate (23 per 500 sessions): about 4,000;
- non-coding (2 per 500 sessions): tens of thousands.

The dumps hold 500 sessions each. **Better filtering alone cannot close this gap. The
per-task hardness rate has to rise as well** (section 6).

---

## 3. Learnings

### 3.1 What made tasks hard

| model | what it misses | examples |
|---|---|---|
| Fable | It notices the right fact, then overrides the published rule as a "judgment call" | a-040 (22/26 failed), a-013 (15/15), a-063 (9/10), a-054 (8/10) |
| Fable (coding) | Scope over-reach, rule interactions, state | c-010, c-041, c-042 |
| Astra | An input shape it has not seen: a format variant, a typed dash (U+2011), `null` vs a missing key, a pmset line with no time | c-007-date, c-010-go, c-024, c-042 |
| Astra | Its literal key differs from the declared identity | v1 a-005 (10/10), c-024, c-031 v4 |
| Both | Exact money or rounding at an edge (decimal half-up, a band edge) | c-043, c-015-flask, a-037 (about 30-50%) |
| Both | A clue that can only be inferred across two documents | a-023 v2, a-049 |

Levers that bit Astra on READY coding tasks, by count:

| lever | count |
|---|---|
| format_variant_unseen | 57 |
| rule_scope_overreach | 45 |
| rule_interaction_rare_input | 44 |
| scope under-reach | 28 |
| sibling_path_omitted | 22 |
| identity_collision | 20 |

### 3.2 What did not work

- **Single levers and noticing traps.**
  - The lever lab solved 280 of 285 episodes, and the 5 misses were an ambiguity.
  - Wave 1 had 0 failures in 1,220 lever-episodes.
- **Breadth and data volume.** Checks that never fail carry about 60x more data. Bites
  came from 9-130 rows.
- **Rules stated outright.** Agents transcribe them.
- **Long instructions.**
  - In non-coding, 96 of 122 trap entries were neutralised by the instruction or fixture
    text itself.
  - Those instructions ran 294-668 words and gave the method away.
- **Coding levers that never bit:** symptom-vs-cause, chained defects, decoy location
  and restraint.
- **Correction-rich traces, as such.**
  - In coding, persistent-defect or spec corrections did not predict READY: 9 of 33 with
    them, 19 of 42 without.
  - C2's index has almost no correction signal, yet C2 produced 23 READY tasks.
- **A high premise-change score at seed selection** (non-coding batch 3). All four seeds
  scored 3/3 and all four stayed at 1.0/1.0.
- **Two-step tasks.** All 4 two-step coding tasks are stuck.

### 3.3 Validity

| issue | evidence | what we do about it |
|---|---|---|
| Answers printed in fixtures | 8 non-coding tasks blocked at final calibration (10-02) | Sweep the fixture for reference values before review |
| An in-fixture README listing the traps | Read in 97% (Fable) and 88% (Astra) of v1 steps | Banned |
| Ambiguity passing as difficulty | Coding: determinacy probe rejected 863 of 2,415 (36%) | Keep the probe; add a third model family |
| Provisional re-grades vs a fresh screen | Coding: only 18 of 27 provisional target-mets held | Accept only on a fresh screen |
| Semantic review is the main blocker | Non-coding: 114 of 155 reviews "revise". C1: 42 of 44 blocks. C2: 37 blocks | Review earlier, and only drafts already measured hard |
| Hacking flags block screens | 75 coding flags, mostly agents killing their own dev server | An adjudication store (none exists today) |
| Missing pinned deps (coding) | c-002, c-009-go, c-011, c-043 | Dependency audit against the repo's manifests |
| Infra voids | Astra hit the 2M TPM org cap (14 of 14 wave-1 voids); a key out of credit (32 reviews lost); an OAuth expiry (8.5 h lost) | Key probes and Astra slot caps |

### 3.4 Speed

| stage | non-coding | coding |
|---|---|---|
| Design | about 1 h median | about 6.5 h |
| Screen, 5 episodes x 2 models | about 5 min wall | not summarised here |
| Fast hardening round | about 70 min between attempts | about 99 min |

Speed is not the constraint. Hardness yield and session supply are.

---

## 4. Where sessions are lost

### Non-coding

**Batch 3, manual screen:** 357 sessions in, 85 viable.

| reason | count |
|---|---|
| software request | 84 |
| media | 51 |
| too thin | 50 |
| non-professional | 45 |
| requires execution | 17 |
| other | 25 |

**Classifier:** 216 seeds in, 127 survive (59%).

| reason | count |
|---|---|
| not gradable (no truth source) | 59 |
| no route | 17 |
| software | 12 |
| requires execution | 1 |

"Too thin" means one of:
- no source files (generic MCQs, ideation);
- one-line edits;
- format conversion or layout work only;
- empty correction turns.

### Coding

| stage | C1 | C2 |
|---|---|---|
| Score floors | 2,045 seeds in, 969 pass | 1,260 seeds in, 592 pass |
| no source fixture | 517 | 319 |
| kill filter | 362 | 299 |
| no runtime (engine-bound) | 197 | 50 |
| Classifier | 424 seeds in, 284 pass | 570 seeds in, 124 pass |
| not gradable | 139 | 435 |
| no reproduction | 125 | 427 |
| not a coding request | 58 | 340 |
| document | 47 | 309 |
| human judgement | 27 | 295 |
| runtime infeasible | 95 | 152 |

One seed can carry several classifier kill reasons, so the classifier rows overlap.

---

## 5. What to ask for in the sessions shared with us

### 5.1 Raises survival (measured)

| | ask | evidence |
|---|---|---|
| Both | Recovered input and output files attached to the session | Non-coding: survivors had a median of 90 KB vs 26 KB; 64 of 99 with >= 5 file paths survived vs 15 of 35 with none |
| Both | Correctly tagged as coding vs non-coding | 84 software requests in the non-coding pool; 340 non-coding requests in the coding pool |
| Non-coding | Professional work with a checkable, document-based answer (not ideation or MCQs) | "Too thin" 50, non-professional 45, not gradable 59 |
| Non-coding | Text documents rather than media (images, video, audio) | 51 media rejections |
| Coding | A recoverable source tree of at least 20 KB, not vendored | Below 20 KB, survival was 38% (C1) and 5% (C2) |
| Coding | A runnable stack (Python, JS/TS, Go, C/C++), not engine-bound (Roblox, Unity, Flutter, MQL5, HDL) | 197 + 50 no_runtime |
| Coding | A reproducible defect or a concrete feature, not "explain this" | 552 "no reproduction" (C1 125, C2 427) |

### 5.2 May raise hardness (hypotheses, thin evidence)

**Coding** (n = 75 screened tasks):

| ask | evidence |
|---|---|
| Single deliverable, 6-20 files, JS, Go or Python | JS 8 of 12 READY; Go 2 of 2; TS 0 of 4; one deliverable file 23 of 50 vs 2 of 14 with several |
| Input in a published external format with many surface variants: parsers, encodings, Unicode, money or dates, HTTP Range, paging, null vs absent | 12 of the 13 READY tasks in the C1/C2 comparison table bit on one of these; this is the class Astra misses (unseen input shapes) |
| Single-step | All 4 two-step tasks stuck |
| The trace's agent wrote no tests | With tests: 5 READY, 11 stuck. Without: 23 READY, 13 stuck |
| Under 10 turns | 1 of 9 tasks with 10 or more turns is READY |

Trace tests help a seed survive classification but go with Astra staying easy. **Do not ask
for them.**

**Non-coding** (n = 2 to 5):

| ask | evidence |
|---|---|
| Real artifacts with an internal inconsistency nobody flagged (the agent's deliverable has wrong arithmetic; uploads label one thing two ways) | The only 2 bites that came from the trace itself (a-040, v1 a-005) |
| Numeric work under an exact rule (budgets, marks, invoices, rounding) | 3 of 5 numeric-tolerance tasks became genuine |
| Several governing documents that bear on one answer (a policy plus data, versions V1 and V2, a contract plus a ledger) | a-023 and a-049, the only tasks that bit both models |
| A requirement revised mid-session, where the material still supports one exact answer | Only about 15 of 85 batch-3 seeds had this |

### 5.3 Not worth asking for

- Sessions chosen for many corrections. No effect on hardness in either pipeline.
- Long sessions, large data volumes, many deliverables.
- Sessions where every rule the work needs is spelled out in one document.
- Media-only, ideation or conversion-only sessions in the non-coding pool.
- Engine-bound coding sessions.

### 5.4 Metadata that would let us filter before we spend

Ask that each shared session carry:

1. A coding / non-coding tag and the deliverable type.
2. Whether input and output files are attached, with their sizes.
3. Language and runtime (coding).
4. Whether the agent ran code or tests.
5. Whether the user accepted the final deliverable, or kept pushing back.
6. Non-coding: whether governing documents are present (policy, standard, contract), and
   whether the answer is numeric.

---

## 6. What we change on our side

Whatever sessions arrive, non-coding hardness came from the authored mechanism. These
changes matter at least as much as filtering:

1. **Build in the mechanisms that bit.** Pair a Fable-override lever with an Astra
   unseen-shape or identity lever on the same hidden item, inside a premise that changes.
   Keep a catalogue with each template's measured bite rate.
2. **Short instructions, plus a label probe.** If a model reading only the instruction and
   fixture can name the special item, the trap is labelled.
3. **Test difficulty before validity.**
   - One Fable and one Astra blind solve per draft, and certify any failure.
   - Only then run controls, review and `harbor check`.
4. **Screen sequentially.** Stop a model's screen at its third pass. Accept a task only on
   a fresh cohort.
5. **Automate what still waits on a human:** hack-flag adjudication, a printed-answer
   sweep, a dependency audit, and key and quota preflight.

---

## 7. Not recorded / caveats

- Full session length: only the seed span's turn count is recorded.
- Source row volumes.
- Structured flags for published standards, conflicting sources or identity keys.
- The coding change type (bugfix, feature, finish) per session.
- Premise-change scores exist for non-coding batch 3 only.
- 30 batch-3 non-coding bundles are unscreened. Some C2 coding builds are still running.
- Runner use is recorded inconsistently: 17 of 370 coding sessions in `CODING-TRACES.md`
  vs 23 of 500 in C1's index.
- Every rate here rests on at most 20 episodes per model per task.

## Sources

- **Non-coding (N2):** `research/R6-wave1-trajectory-analysis.md`,
  `research/R7-measured-bites.md`, `research/seeds-batch3.{md,json}`,
  `research/batch3/audit-a-023-v2.md`, `research/scores/`, `research/lever-evidence.json`,
  `logs/classify-v2.log`, `logs/batch3-classify*.log`, `pipeline/select/classify.py`.
- **Non-coding (N1):** `pipeline-artifacts/research/trajectory-nearmiss-report.md`.
- **Coding (C1 / C2):** `pipeline-artifacts/waves/readiness.json`,
  `pipeline-artifacts/{index,candidates,tiered}.json`, `analysis/ASTRA-EPISODE-MINING.md`,
  `analysis/CODING-DIFFICULTY-LEVERS.md`, `analysis/CODING-TRACES.md`, each bundle's
  `verification/{screen,genuine}.json` and `hardened/hardening.json`.
