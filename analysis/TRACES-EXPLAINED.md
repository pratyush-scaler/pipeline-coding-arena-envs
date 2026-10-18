# How we turned Arena traces into extremely hard coding tasks

A plain-language summary for the 29 shipped coding tasks (written 2026-10-06).

---

## The short answer

1. **We picked traces that left behind a real project we could run.** Difficulty played no part in the choice.
2. **The hardness was added by us afterwards**, then measured on both models (Fable 5.1 and GPT-6 Astra).
3. **What made tasks hard:** we stated the rule clearly, then hid tests where the "obvious" code gets it wrong. Each hidden test combines two or more tricks, aimed at both models' blind spots.
4. **Weaker-model traces can work too.** The source model doesn't decide difficulty: our hidden tests and the measurement do. Expect more hardening work per task.
5. **What we most need from Arena:** projects that actually run (pinned dependencies, environment info), redaction that doesn't break the code, and clear labels.

---

## 1. How we picked the traces

Think of it as a series of sieves. A trace had to pass every one.

| Sieve | What it means | Example of what got dropped |
|---|---|---|
| 1. A real conversation | At least 2 user turns | One-shot "write me X" requests |
| 2. Runs on a normal computer | Python, JavaScript, Node, Go, PHP or a browser app | Roblox, Unity, Flutter, trading-platform scripts |
| 3. The code is there | At least 2 source files and some real code in the trace | Sessions where the code was never saved |
| 4. It is a real coding problem | A coding request we can check by running tests, with a clear "this is wrong" moment or a clear missing feature | Writing documents, design taste, "make it prettier" |

---

## 2. What the winning traces looked like

The 29 shipped tasks came from traces that were mostly:

- **Short conversations:** about 9 user turns and 18 messages on average.
- **Small projects:** about 6 code files and 1,700 lines.
- **Everyday tech:** mostly Python (13) and JavaScript or Node (13), plus Go (2) and PHP (1).
- **One problem each:** all 29 are single-step tasks.
- **Not built around a dramatic failure:** in about half, the user never complained in the trace. What mattered was a real project plus a clear sense of what the user wanted.
- **Without tests:** only 3 of the 29 original sessions ever ran tests, so we wrote every test ourselves.
- **From many models:** 16 from top frontier models, 7 from frontier models, and 6 from cheaper models (Kimi K3, MiniMax M3, GPT-5.6 Luna low).

**Patterns we noticed** (small numbers, so these are hints, not proof):

| Helped | Hurt | Made no difference |
|---|---|---|
| Short sessions | Long sessions (over 40 messages) | How strong the original model was |
| One problem per task | Two-step tasks (0 of 4 worked) | Whether the trace had before/after file versions |
| Small projects | Big projects (over 20 code files) | |
| | Traces that already had tests | |

---

## 3. How we made the tasks extremely hard

### What did NOT work (we tested these)

- A single trick per task. Strong models solved 280 out of 280.
- "The visible error is not the real bug."
- A fake place to fix that looks right.
- "Don't touch this file." The models never broke this rule (0 violations in 5,210 tries).
- Telling the model the requirements changed.
- Simply making the task bigger.

Strong models read a clearly stated rule and follow it. A trick on its own gets noticed.

### What DID work

**Rule:** say the rule clearly, then test exactly where the "natural" code goes wrong.

1. **Combine two or more tricks on one hidden test**, so fixing one isn't enough.
2. **Aim at both models' different blind spots:**

| Fable 5.1 tends to miss | GPT-6 Astra tends to miss |
|---|---|
| A rule applied too broadly (over-reach) | The same thing in an unfamiliar format, e.g. `ß` vs `ss`, or a hidden byte-order mark |
| Two rules colliding on a rare input | Exact conditions; it grabs the "first match" |
| Something that must be remembered across runs | Conventions it forgets to follow |

3. **Prove every trick works before it counts:**
   - A "natural but wrong" solution must fail.
   - A "half-right" solution must fail.
   - A correct solution written separately must pass.
   - The untouched project must score 0.
   - Our reference fix must score 100%, three times in a row.
4. **Measure, harden, measure again.** If one model finds a version easy, we add new hidden tests aimed at that model and measure again. One task went from Astra passing 5 of 5 runs to 0 of 5.
5. **Check the failures are real:**
   - Every failure is a genuine wrong answer, not a crash or a broken grader.
   - There are no stored answers.
   - Cheating attempts are blocked and checked.
   - An independent reviewer reads every task.

**The bar:** a model passes each task in at most half of its tries (pass@1 of 0.5 or less), on both models.

### Six techniques we use, with real examples

Each technique on its own is solvable. The difficulty comes from stacking two or more of them on the same hidden test.

**1. Same thing, different shape**
- **Idea:** the hidden input means the same as the visible examples, but arrives in a form the model hasn't seen.
- **Example (Anki flashcard deck):** duplicate cards must be removed, but `ß` and `ss` count as different words. One word list is saved by Notepad with an invisible marker at the start of the file. Code that works on the clean example files breaks on these.
- **Example (trading signal scanner):** saved price data arrives in two formats at once, newest-first CSV files and older pickled files, and the days overlap.
- **Catches:** mostly Astra.

**2. Two rules meeting on a rare input**
- **Idea:** each rule is easy alone. The hidden test picks the rare case where both apply at once and the instruction says which wins.
- **Example (Anki):** neighbouring cards must not share a preposition or repeat a verb. When a page is too full to keep both rules, the deck must break the preposition rule before the verb rule, counting the point where the page starts.
- **Example (browser ant-colony game):** a last-life nest breach happening on the exact tick a season ends.
- **Catches:** mostly Fable.

**3. Remember what happened before**
- **Idea:** the right answer depends on earlier runs or earlier actions, not just the current input.
- **Example (Anki):** when the user adds page 3, the deck must start with exactly the cards they already imported for pages 1 and 2, in the same order.
- **Example (browser strategy game):** a factory place is held from the moment a unit is sent in, so a second click can't take it twice.
- **Example (trading scanner):** a run early in the day must give the same rows as a run after the close.
- **Catches:** mostly Fable.

**4. The fix must reach every path**
- **Idea:** the same rule lives in several places. The natural fix repairs the one place the user pointed at and leaves its twin broken.
- **Example (sales dashboard):** the dashboard page was already patched, but the Excel export still calculates costs the old way. Both must follow one rule.
- **Example (Go WebSocket tool):** the command line, the web API and the AI-tool interface all read message history, and all three must return the newest messages.
- **Catches:** both models.

**5. The exact rule, not the habit**
- **Idea:** the instruction states a precise convention. The model's usual idiom gives a slightly different answer.
- **Example (sales dashboard):** amounts must be rounded "half up, the way a calculator does it", and totals added from the already-rounded lines. Python's normal `round()` turns 2.675 into 2.67, so the obvious code is off by a cent.
- **Example (gold-trading engine):** take the three closest price levels past the entry, merge levels closer than 0.15R outward, and print every level as a 2-decimal chart price.
- **Catches:** mostly Astra.

**6. Don't over-reach**
- **Idea:** the fix must touch only what the rule covers. The natural fix "cleans up" too much.
- **Example (trading scanner):** candles with zero volume must be treated as if never sent, but every other candle must stay. Dropping any other suspicious-looking row fails.
- **Example (gold-trading engine):** fix the stop and target levels, but "nothing else in the setup may change".
- **Catches:** mostly Fable.

**How they combine in practice.** The Anki task stacks techniques 1, 2 and 3 on the same hidden inputs: an unusual word form, an overfull page and a page added later.

---

## 4. Can traces from weaker models give us hard tasks?

**Probably yes, with two caveats.**

**Why yes:**
- Difficulty comes from the hidden tests we write and is measured against Fable 5.1 and Astra. The original model doesn't decide it.
- Cheaper models' traces already gave us 6 of the 29 shipped tasks.
- Kimi K3 traces had the best success rate in the 500-trace dump.

**Caveat 1: more work per task.**
- A weak model's own mistakes are usually easy for strong models.
- Tasks from weaker-model traces started easier: in the kaizen dump, none of 7 met the bar on the first try.
- We can still harden them; it just takes more rounds.

**Caveat 2: steerability signals don't tell us which traces will work.**
- A steerability signal means the model didn't follow the user.
- The kaizen dump has 881 of them, yet they did not reliably separate the traces that shipped from those that didn't.
- Their real value is the user's words. When a user repeats "it must do X", that's a precise rule we can test.

**Check first:** Arena's spec says traces come "from frontier-capable models". Confirm weaker-model traces are allowed.

**Suggestion:** start with a pilot of about 100 weaker-model traces, measure how many become shipped tasks, then scale.

---

## 5. What to ask Arena for

### Make the projects runnable

- [ ] **Dependency files with exact versions** (`requirements.txt` with pins, `package-lock.json`, etc.). Most kaizen sessions had none.
- [ ] **Environment info:** operating system, language version, tool versions. Today there is none.
- [ ] **Redaction that doesn't break code.** A placeholder like `[SECURITY_DATA]` inside a code file is a syntax error. It happened in 197 of 370 kaizen coding sessions.
  - Ask for realistic fake values instead (a fake key, a fake name).
  - Ask for a flag on files that were redacted.
- [ ] **Keep images and other asset files** (about 48,000 were dropped in kaizen). Leave out downloaded libraries and big copied projects.
- [ ] **Fill the empty "where this repo came from" fields** (base commit, changes).

### Make the intent clear

- [ ] **Accurate tags:** coding or not, language, runtime. In the 500-trace dump, 340 seeds that reached our coding filter turned out not to be coding requests at all; accurate tags would have screened them out up front.
- [ ] **How the session ended:** did the user accept the result, give up, or work around it?
- [ ] **Fill the signal fields that exist but are empty** (steerability fields, per-turn ratings), and explain what `session_scores`, "relaxed" and "accepted" mean.
- [ ] **Model name and reasoning effort on every message.**
- [ ] **Don't remove the failures.** The 500-trace dump looks filtered toward "accepted" sessions, but the failure moments are useful to us.

---

## 6. Filters for weaker-model traces

| # | Filter | Keep traces that... | Why |
|---|---|---|---|
| 1 | Task type | Change code in a real project | Documents and taste requests can't be tested |
| 2 | Technology | Use Python, JavaScript/TypeScript, Node, Go or PHP, or run in a browser, with no special hardware, paid APIs or cloud logins | We must run them in a container |
| 3 | Length | Have about 3 to 15 user turns (6 to 40 messages), or mark where each sub-task starts and ends | Long sessions rarely became tasks |
| 4 | Project size | Have 2 to 20 code files, about 500 to 10,000 lines | Small-to-medium projects worked best |
| 5 | Code present | Contain the final code, which differs from the starting code | We need a real project to start from |
| 6 | Dependencies | Include a dependency file, ideally with exact versions | Unpinned dependencies broke tasks |
| 7 | Clear correction | Have a user saying what went wrong: what they did, what happened, what should have happened | "No clear reproduction" was the top reason traces were dropped |
| 8 | Domain | Involve rule-heavy logic: parsing, dates and timezones, money and rounding, text and encodings, state over time, permissions, sort order | This is where our hardest tests live |
| 9 | Scope | Cover one feature or bug | Two-step tasks never met the bar |
| 10 | Clean | Have little personal data in the code and no pasted passwords or keys | Redaction placeholders broke code |
| 11 | Variety | Give one task per session, with no near-duplicate projects | Avoids repeats |
| 12 | Don't require | Existing tests, or a top frontier source model | Neither helped |

---

## Words used in this document

| Term | Meaning |
|---|---|
| **Trace** | A recorded Arena session: the conversation, the tool calls and the files at every step |
| **Seed** | One piece of a trace that could become a task |
| **Hidden tests** | Tests the solving model never sees; they decide the score |
| **pass@1 of 0.5 or less** | The model solves the task in at most half of its attempts |
| **Hardening** | Adding new hidden tests to a task that a model found too easy, then measuring again |
| **Steerability failure** | A moment where the model did not do what the user asked |
| **Reward hacking** | A model cheating the grader instead of solving the task; we block and check for this |

---

## How sure are we?

- **Solid:** the selection steps, the counts, and that every shipped task met the bar on both models.
- **Hints only:** the "helped / hurt" patterns and the weaker-model findings. They rest on small numbers (tens of tasks).
- **Corrections to earlier figures:**
  - Recoverable code: a fresh count gives 316 of 370 coding sessions, not the 352 quoted before.
  - Sessions that ran tests: 21 to 23, not 17.
  - Neither changes the conclusions.
