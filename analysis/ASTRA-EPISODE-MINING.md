# Astra episode mining: latent defects in passing solutions (2026-10-04)

Question: where are GPT-6 Astra's (Codex CLI) **passing** final repositories still
wrong on inputs the published instruction settles but no hidden test exercises,
and would adding such cases (re-grading the captured outputs) push Astra below
0.5 pass@1? Fable 5.1 (Claude Code) is examined alongside so new cases keep it
failing too.

Scope: the 10 screened single-step tasks, 100 episodes (5 Astra + 5 Fable each;
for c-008-pyside6 and c-010-go one voided Astra episode is replaced by its
topup). Nothing under any bundle, test or pipeline directory was modified.

## Method

- **Trees.** Each episode's final tree is `steps/<s>/artifacts/app/repo`, used
  after the 2026-10-04 re-sync.
  - Every episode tree holds every file in `tests/fixture-manifest.json`.
  - `ref/` = fixture + `solution/fix.patch`.
  - `alt/` = fixture + `authoring/alt/<step>.patch`.
  - Patches were applied on the host. No repository code ran on the host.
- **Execution.** Everything ran only in `astra-mine-<id>` images built from
  `<bundle>/tests` (`docker build -q -t astra-mine-<id> <bundle>/tests`), run as
  `docker run --rm --network none -v /tmp/astra-mine/<id>:/w:ro ...`.
  - **Sanity check.** Re-running the hidden suite reproduces the screen
    grade for every tree on every task.
- **What counts as a defect.** A latent defect is counted only if all four
  hold:
  - (a) a published instruction sentence settles the input;
  - (b) reference and alternative agree on it, both by execution;
  - (c) no current hidden test exercises it;
  - (d) the input is natural for the user's scenario.
  - Reference/alternative disagreements are recorded as discarded.
- **Who ran what.** Per-task mining was fanned out to one agent per task. Every
  "verified" case below was re-executed by the coordinator with the command shown.
- **Probe locations.** Probes, candidate tests, episode maps (`map.txt`) and
  trees are under `/tmp/astra-mine/<id>/`. That is host `/tmp`, so it is
  ephemeral; re-create it from the commands if it is gone.

Task ids used below: c012 = `envs-s1/target-F3-replay/c-012-cli-tool`, c008q =
`envs-s1/target-F2-scenario/c-008-pyside6-desktop-app`, c010 =
`envs-s1/target-F3-replay/c-010-go-desktop-app`, c011 =
`envs-s2/target-F2-scenario/c-011-quant-analytics-pipeline`, c024 =
`envs-s2/target-F2-scenario/c-024-vanilla-js-webapp`, c009t =
`envs-s6/target-F2-scenario/c-009-trading-backtest-script`, c004 =
`envs-s4/target-F2-scenario/c-004-shopify-theme-vue`, c008l =
`envs-s6/target-F2-scenario/c-008-python-llm-client`, c001 =
`envs-s5/target-F0-inspired/c-001-minecraft-skin-generator`, c007 =
`envs-s6/target-F2-scenario/c-007-html-date-form`.

## Headline

| task | lang | screen F / A | verified Astra latent defect? | best case | Astra after re-grade | Fable after re-grade | fairness risk |
|---|---|---|---|---|---|---|---|
| c012 cli-tool | py | 0.2 / 1.0 | **yes** | D1 text after the `.class` type that is not a `#` comment | **0/5** | 0/5 | medium |
| c010 go battery | go | 1.0 / 1.0 | **yes** | L1 state word with no time field | **0/5** | 3/5 (0.6) | moderate |
| c024 vanilla-js | js | 0.4 / 1.0 | **yes** | D1 `total:null` in a pasted exam | **0/5** | 2/5 | low meaning / moderate naturalness |
| c007 date-form | py | 0.4 / 0.0 | yes (beyond U+2011) | C2 decimal point is not a full stop | 0/5 (stays 0/5 even if U+2011 handled) | 1/5 (0/5 with C3) | low |
| c001 minecraft | py | 1.0 / 1.0 | contested only | C1 white/grey eyes still get the look's eye colour | 0/5 | 5/5 | **high** (instruction readable both ways) |
| c008q pyside6 | py | 0.4 / 1.0 | no | - | 5/5 | 0/5 with L-F1 | - |
| c004 shopify-vue | js | 0.2 / 1.0 | no | - | 5/5 | 1/5 | - |
| c008l llm-client | py | 1.0 / 1.0 | no | - | 5/5 | 2/5 with D1+D2 | - |
| c011 quant | py | 0.6 / 1.0 | no | - | 5/5 | 3/5 | - |
| c009t backtest | py | 1.0 / 1.0 | no | - | 5/5 | 3/5 with LD1+LD2 | - |

**Where a clean case exists.** On 3 of the 9 tasks Astra currently passes
(c012, c010, c024), one clean-to-moderate hidden case flips **all 5** Astra
episodes. On c007 a new case keeps Astra at 0/5 even in the counterfactual where
Astra fixes U+2011.

**Where none exists.** On 5 tasks (c008q, c004, c008l, c011, c009t), Astra
matches the reference on every settled input. Those probes include roughly
3k–8.6k fuzz cases per task. No hidden case added under the current instruction
can move Astra there; it needs a new published rule (a premise change or new
step).

**Astra's five episodes are clones.** On every task the five Astra episodes
converged on near-identical code. A latent defect is therefore all-or-nothing
for Astra (0/5 or 5/5): the screen's n=5 is close to one effective draw per
task.

## Per task

### c012 cli-tool (`01-dump-every-smali`; smali_dump.py)

**Published rules (key sentences).**
- **Walk:**
  - "it goes in every folder inside the folder i give, any depth ... no folder gets skipped because of its name"
  - "a file whose name ends with .smali, capital or small letters don't matter"
- **Output format:** header/FILE/END/footer, relative `/` paths, the output
  name, and real counts. "if there is no smali file at all, don't make any txt".
- **Class:** "the class is the type written on the file's .class line (the
  Lsome/pkg/Name; part). if a file has no .class line, or more than one, put
  `(class ?)`".
- **Order:** "inside a folder first its own smali files, then its subfolders
  ... names go in plain character order with every letter taken as small,
  ... compared without its .smali ending ... (capital first)".
- **Encodings:** "utf-8, utf-8 with bom, utf-16 le or utf-16 be ..., or ansi,
  which is windows-1252 ... no bom mark, no junk letters ... no \r anywhere".

**Astra approach (5/5).**
- Small in-place edit using `os.walk` with the reference's sort keys and
  strict decoding.
- Class extraction is "cut the `#` comment, then the last word must look like
  `L...;`".
- One inline integration script (18–25 files), 5–9 commands (mean 7.2), and a
  stop at the first all-PASS.
- 3/5 dropped the unreadable-file handling.

**Fable.** F1, F3, F4 and F5 fail `o_class_line`: a comment glued to the type
(`Lcom/c/Hash;#patched by me`) is not cut.

**Verified latent defects.**

| id | exact input (.class line, ASCII, LF) | sentence | ref | alt | Astra | Fable |
|---|---|---|---|---|---|---|
| D1 | `.class public Lcom/c/Renamed; // was b.smali`; `.class public final Lcom/c/Pay; <-- real name`; `.class public Lcom/c/Cart; (was d.smali)` | "the class is the type written on the file's .class line (the Lsome/pkg/Name; part)" | `Lcom/c/Renamed;` etc. | same | A1–A5 give `(class ?)` | F2, F3 give `?`; F1, F4, F5 give the reference answer |
| D1b | `.class public Lcom/app/Main; // apktool named it Lcom/app/a;` | same | `Lcom/app/Main;` | same | A1–A5 give `Lcom/app/a;` (the user's original complaint) | F2, F3 give `Lcom/app/a;`; F5 gives `?`; F1, F4 match |
| D2 | `EF BB BF` + valid UTF-8 smali (CRLF) + `# note caf\xE9\r\n` | "no bom mark, no junk letters"; counts equal files written | dumped, BOM stripped, bad byte becomes U+FFFD | same | A1, A4: mojibake `ï»¿`… and class `?`; A2, A3, A5: crash, half-written txt | F5 like A1; F1–F4 match |

- **Command:**
  `docker run --rm --network none --user 2001:2001 -v /tmp/astra-mine/c012:/w:ro astra-mine-c012 sh -c 'cp -r /w /tmp/w && cd /tmp/w && for d in ref alt A1 A2 A3 A4 A5 F1 F2 F3 F4 F5; do ARENA_REPO=/tmp/w/$d python -I -m pytest -q -p no:cacheprovider --rootdir /tmp/w /tmp/w/test_latent.py -rA; done'`.
  - Coordinator re-run: ref, alt, F1 and F4 pass 3/3; A1–A5 fail 3/3; F2, F3 and
    F5 fail 2.
- **Fit:** D1 fits `o_class_line` as extra rows. D2 fits `o_text_bom` or a new
  check.
- **Fairness:**
  - `//` is not a smali comment, so a reviewer may call the line
    unspecified. Prefer the single-type D1 rows over D1b, which has two `L...;`
    words.
  - D2 is outside the five listed encodings.
- **Discarded:**
  - Undefined cp1252 byte `0x81`: ref decodes the whole file as latin-1, which
    is junk letters and itself a reference defect; alt gives U+FFFD. All Astra
    crash.
  - NBSP or form feed around `.class`, and `.class#note`: ref and alt
    disagree.
  - Unreadable file: not settled.
  - 500-tree random differential fuzz: Astra 500/500 equal to ref.
- **Projection:** D1 alone gives Astra 5/5 → 0/5 and Fable 1/5 → 0/5 (F2 flips).
  D2 alone gives Astra 0/5 and leaves Fable at 1/5.

### c010 go-desktop-app (`01-battery-state-and-alerts`; battery.ParsePMSet, alerts.Evaluator, alerts.Load)

**Published rules.**
- **Parser:**
  - R1 "charging has to mean the battery is really taking charge right now.
    pmset has more than one wording for that and every one of them counts, but
    discharging, charged, AC attached and not charging are never charging."
  - R2 "the percent, the time remaining and the state word ... always come from
    the mac's own battery line (the -InternalBattery one)".
  - R3: error when there is no own line.
  - R4: PowerSource / ExternalConnected.
  - R5 "anything about the battery state i didn't mention stays how it works
    now."
- **Alerts and persistence:** low/full thresholds, switched-off alerts, the
  first-reading seeding, quiet time and recovery margins of ±5, reading
  timestamps, and `Load(path)` persistence that starts fresh on a missing or
  broken file.

**Astra approach (5/5).**
- Own line found with `HasPrefix(TrimSpace(line), "-InternalBattery-")`.
- State = the raw second `;`-field compared **exactly** with
  `"charging"` / `"finishing charge"` / `"charged"`.
- Alerts and persistence match the reference on every probe; saves are
  atomic.
- Tests written first, then `go test`, `make test` and `-race`; 2–3 web lookups;
  11–17 commands.

**Fable.** All pass. F1 and F5 throw away the whole field when it contains
`present:`.

**Verified latent defect L1: own-battery state word with no time field after
it.**

| input | ref = alt | Astra A1–A5 | Fable |
|---|---|---|---|
| P1 `Now drawing from 'AC Power'\n -InternalBattery-0 (id=4653155)\t62%; charging present: true\n` | Charging=true, Percent=62, TimeRemainingMin=-1 | Charging=false | F2, F3, F4 correct; F1, F5 false |
| P2 UPS line with a time, then `-InternalBattery-0 ...\t98%; finishing charge present: true` | Charging=true | false | same split |
| P3 UPS `100%; charged present: true`, own `96%; charged present: true` | Charged=true | false | same split |

- **Sentences:** R1 ("every one of them counts"), R2, and R5. The old code
  used `Contains("charging")`, so it reported Charging=true here; Astra's exact
  equality on `"charging present: true"` changes that behaviour.
- **Coverage today:** the hidden suite uses the time-less shape only for UPS
  lines and for `AC attached; not charging present: true`. Astra survives
  that one because `ac attached` is field[1].
- **Command:**
  `docker run --rm --network none -v /tmp/astra-mine/c010:/w:ro astra-mine-c010 sh /w/run_newcheck.sh ref alt A1 A2 A3 A4 A5 F1 F2 F3 F4 F5`.
  - Coordinator re-run: ref, alt, F2, F3 and F4 PASS; A1–A5, F1 and F5 FAIL on
    all three rows.
- **Fit:** P1 and P2 fit `o_parse_charging_words`; P3 fits
  `o_parse_charged_kept`. Amend the bases to say "with or without a time
  field".
- **Fairness:** moderate. The shape is format-consistent and already used in
  the suite, but no known real internal-battery output prints `charging`
  without a time or `(no estimate)`. The three rows share one parsing seam, so
  they are not independent failures.
- **Discarded:**
  - `Currently drawing from`: ref and alt disagree.
  - Cooldown changed during a quiet time: ref and alt disagree.
  - Switched-off alert on the first reading: R7 vs R8 ambiguity. The reference
    also contradicts its own trap `t_disabled_state`; worth a look.
  - Many alert and persistence edges: all agree.
- **Projection:** Astra 5/5 → 0/5; Fable 5/5 → 3/5 (0.6, still above the step
  target).

### c024 vanilla-js-webapp (`01-template-blocks-add-up`; studymate/data.js)

**Published rules.**
- "work for every class 5 to 12 of PSEB and CBSE, also a class that has
  nothing yet".
- Merge and replace:
  - "Pasting never deletes anything"
  - "new chapters come after the old ... quiz questions come after"
  - "Exam and analysis in the block replace the old ones, but if the block dont
    have exam or analysis the old one stays"
- "**If the exam dont have total, total is all the section marks added.**"
- Chapter-name identity (trim and case only) and "later one wins, but it stays
  in the place where that name was first".
- Video id: "getContent must give only the id ... videoId """.
- Number coercion: "Where a number goes (section marks, total, internal, quiz
  answer, analysis weight) ... "10" ... the same number ... Only those five".
- "a chapter with no name is left out, and a quiz question whose answer number
  dont point to one of its options is left out".
- Extra subjects come in `subjectsFor` "once something of it really went in";
  "A subject with no content gives null"; "Every chapter is in buildSearchIndex
  one time"; "Everything that is already in data.js must stay exactly same."

**Astra approach (5/5).**
- A per-class Proxy for classes 5–12 installed with a defineProperty
  getter/setter; whole-class blocks are routed through the same merge.
- Reproduced the TypeError first, wrote `studymate/data.test.js`
  (12–28 node:test tests), 10–15 commands.

**Fable.** F1, F3 and F4 fail `o_no_content_null`: a subject emptied by
`analysis:[]` is still returned as content.

**Verified latent defects.**

| id | input | ref = alt | Astra | Fable |
|---|---|---|---|---|
| D1 | `DB.CBSE[10]["Mathematics"] = { exam:{ total:null, internal:20, duration:"3 hrs", sections:[ {name:"Section A — MCQ", q:"20 × 1", m:20}, {name:"Section B — Long", q:"12 × 5", m:"60"} ] } };` (also a new PSEB 7 Science block, and inside a whole-class `DB.CBSE[6] = {...}`) | total 80 (60 and 80 in the variants) | A1 gives 0; A2–A5 give `null` | F1–F5 give 80 |
| D2 | `DB.CBSE[9]["Computer Science"] = { analysis:[{unit:"Python",weight:40,color:"#4f46e5"}] };` then `= { analysis:[] };` | list ends with "Computer Science"; getContent gives null | A1, A4 correct; A2, A3, A5 drop the subject from the list | F2, F5 drop it; F1, F3, F4 list it but return content instead of null |

- **Cause of D1:** Astra tests key presence (`hasOwnProperty("total")`), not
  value. The same habit makes `exam: undefined` / `analysis: undefined` delete
  the old values (unnatural, not counted).
- **Coverage:** no hidden data has `total:null` (grep).
- **Command:**
  `docker run --rm --network none -v /tmp/astra-mine/c024:/w:ro astra-mine-c024 sh -c 'cp -r /w /tmp/w && cd /tmp/w && CASES=cases2.js ONLY=D1a,D1b,D1c,D1d,D2a,D2b NODE_PATH=/opt/deps/node_modules node probe.js'`.
  - Coordinator re-run of D1a and D2a: as in the table.
- **Fit:** D1 fits `o_exam_total` (add "a null total counts as no total"). D2
  fits `o_subject_list`, but the ten episodes split 5/5 on reading "once ...
  went in", so the fairness risk is medium-high; publish a clearer sentence
  before using it.
- **Strongest variant, blocked by alt:** `total:""`, the most natural blank
  total. ref gives 80, but **alt keeps `""`**, so it is discarded. Fable 5/5
  give 80 and Astra keeps `""` or 0. If alt were repaired to treat a blank
  total as missing, this becomes the best D1 row.
- **Other discards:**
  - `exam:null`, `analysis:null`, hex or exponent strings, `"10 marks"`: ref and
    alt disagree.
- **Reference defects to repair before any video-id check:**
  - videoseries / live_stream embeds return `"videoseries"` / `"live_stream"`.
  - `music.youtube.com` and `youtube-nocookie.com/embed/ID` return `""`.
  - An answer pointing at an array hole is kept.
- **Projection:** D1 only: Astra 5/5 → 0/5, Fable unchanged at 2/5. D1 + D2:
  Astra 0/5, Fable 0/5.

### c007 html-date-form (`01-logcheck`; logcheck.py `check(text)`)

**Published rules.**
- Report every rule a log breaks, once each; fine logs are absent.
- Logs are separated by blank lines; PDF wrapping does not split a log.
- A paragraph without `. ! ?` is a heading and is not numbered.
- "no long dashes: en dash, em dash or any other dash thats longer than a
  hyphen".
- "**a hyphen typed in place of a dash counts too, so a hyphen with a gap on
  both sides of it (a space or the end of a line) or two or more hyphens
  together.** hyphens inside words like single-leg ... are fine".
- "every log needs at least 8 sentences, **a sentence ends with a full stop,
  question mark or exclamation mark**".
- US spellings from `us_spellings.txt`, whole word, any case.
- Repeats are "the same log if the only differences between them are spaces,
  line breaks or capital letters ... anything else thats different makes them
  different logs. the first one isnt a repeat".

**Astra approach (5/5).**
- A hand-picked long-dash list, and an **ASCII-only** typed-dash regex
  `-{2,}|(?<!\S)-(?!\S)`.
- All five printed or dumped the Unicode hyphen characters and still coded
  ASCII only. A1 commented that U+2010/U+2011 "are allowed"; its test is named
  `test_ascii_dash_substitutes`.
- `[.!?]+` sentence count; `utf-8-sig` word list.
- 12–20 unittests, 8–12 commands, stopped once its own tests passed and the
  sample matched. None opened the generators.

**Fable.**
- F1 and F5 fail `o_sentences`: the lookahead after a sentence ending needs a
  capital or a space, so `!2 more laps` is missed.
- F2 fails `o_dash_typed` and `o_mixed` (ASCII-only, like Astra).
- F3 and F4 pass.

**Verified latent defects.**

| id | input | sentence | ref = alt | Astra | Fable |
|---|---|---|---|---|---|
| C1 (tested today, confirmed) | `" ‑ "` (U+0020 U+2011 U+0020); `"‐‐"` (U+2010 x2); `"\u00A0‑ "` | typed-dash rule | dash | A1–A5 miss | F2 misses |
| C1 untested variants | `" ‐ "`, `"‑‑"`, `"-‑"`, `"\n‑ "`, `" ‑\n"` | same | dash | A1–A5 miss | F2 misses |
| **C2** | a log of 6 sentences + `" I ran 2.5 km at the end."` (also `7.30`, `12.5 seconds`) | "a sentence ends with a full stop, question mark or exclamation mark" (7 sentences → short) | `{2:["short"]}` | A1–A5 give `{}` | F4 gives `{}`; F1, F2, F3, F5 correct |
| C3 | a log with "finally" and a later copy with "ﬁnally" (U+FB01) | "anything else thats different makes them different logs" | not a repeat | A1–A5 correct | F3 (`casefold`) flags a repeat |

- **Counterfactual:** the Astra trees with only the typed regex changed to
  `[-‐‑]` (A1h–A5h) pass the current hidden suite 10/10, so U+2011 is the sole
  cause of Astra's 0/5. They **still fail C2**.
- **Command:**
  `docker run --rm --network none -v /tmp/astra-mine/c007:/w:ro astra-mine-c007 sh -c 'cp -r /w /tmp/w && cd /tmp/w && for t in ref alt A1 A2 A3 A4 A5 F1 F2 F3 F4 F5 A1h A2h A3h A4h A5h; do ARENA_REPO=/tmp/w/$t python -I -m pytest -q -p no:cacheprovider --rootdir /tmp/w/extra /tmp/w/extra/test_extra.py; done'`.
  - Coordinator re-run: ref, alt, F1, F2 and F5 pass 2/2; A1–A5, A1h, F3 and F4
    fail 1.
- **Fit:**
  - C2 fits `o_sentences`; add "a decimal point inside a number is not a full
    stop" to the basis.
  - The reference's `is_heading` (in both ref and alt) still treats a decimal
    `.` as an ending, so keep decimals out of heading lines or align both.
  - C3 fits `o_repeat_distinct`.
- **Discarded:**
  - U+2212 minus and U+2053 swung dash (contestable).
  - Fullwidth and vertical forms (unnatural).
  - Form feed and U+2029 (ref and alt disagree).
  - A BOM at the text start (ambiguous).
- **Projection:** C2 keeps Astra at 0/5, including the U+2011-fixed
  counterfactual, and takes Fable to 1/5 (F4 flips). C2 + C3 gives Fable 0/5.

### c001 minecraft-skin-generator (`01-army-roster`; army_factory.py)

**Published rules.**
- Rank marks on the shoulder top of the second layer, ranks 1–8.
- `ordu.csv` utf-8 with a `name,rank` header; one PNG per guy, named exactly
  as written.
- "the 15 looks in the script stay exactly as they are"; look tenure and
  first-free allocation; leavers deleted and returners are new.
- Turkish case-insensitive identity; "only his first line counts".
- Deletes only its own PNGs; state kept in the run folder; deterministic
  output.
- "a spot only counts as empty if it's fully see-through".
- "**the recolour is fine but black, grey and white stay exactly as they are,
  like now**".
- Limited room for marks; protected files.

**Astra approach (5/5).**
- Refactor into `main()` with atomic writes and a Turkish
  `translate+lower` key.
- All-or-nothing validation: exact header 5/5; 3/5 open the CSV as `utf-8`
  without BOM handling.
- All 5 applied the black/grey/white sentence to the eyes and unit-tested it.
- 9–11 unittests left in the repo, 16–20 commands.

**Fable.** All pass. All 5 validate every line before removing duplicates.

**Divergences (verified, none clean for Astra).**

| id | input | ref = alt | Astra | Fable | status |
|---|---|---|---|---|---|
| C1 | eye pixels (9,10,13,14 × 11,12) = (255,255,255,255) in row 11 and (200,200,200,255) in row 12 | today's `set_eyes` applies (look 0 → (255,255,180,255)) | A1–A5 leave them white or grey | F1–F5 correct | **contested**: "looks stay exactly" vs "black, grey and white stay exactly" |
| C2 | `b"\xef\xbb\xbf" + "name,rank\r\nRaufixYusuf,3\r\nRaufixVelr!th,2\r\nRaufixÖmer,1\r\n"` | 3 PNGs | A1, A3, A4 exit 1 with no PNGs | all correct | excluded by the author's spec (`pitfalls_considered[16]` calls the BOM unpublished) |
| C3 | `name,rank\nRaufixKurt,3\nRaufixYusuf,2\nraufixkurt,9\n` | 2 PNGs | all correct | F1–F5 abort | Fable-only; rank 9 is out of range (medium risk) |

- **Command:**
  `docker run --rm --network none -v /tmp/astra-mine/c001:/w:ro astra-mine-c001 sh -c 'cp -r /w /tmp/w && cp /tmp/w/proposed/*.py /tmp/ && cd /tmp && for e in ref alt A1 A2 A3 A4 A5 F1 F2 F3 F4 F5; do ARENA_REPO=/tmp/w/$e python -I -m pytest -q -p no:cacheprovider --rootdir /tmp /tmp/test_eyes_proposed.py /tmp/test_csv_proposed.py -rA; done'`.
  - Coordinator re-run matches the table.
- **Projection:** C1 would take Astra to 0/5 and leave Fable at 5/5, but
  only with an instruction amendment (e.g. "eyes still get the look's eye
  colour even if they're white"). That amendment is itself a published-rule
  change, so a fresh screen would be needed. C3 would take Fable to 0/5.

### c008q pyside6-desktop-app (`01-sheet-columns`; ui/data_panel.py)

**Published rules.**
- Every column with a header or content comes through in order; blank means
  empty or spaces; drop blank-header empty columns and empty rows.
- An empty file leaves the grid untouched.
- Headers are trimmed and case-insensitive; never two columns of one name.
- Suffix rules: the first keeps the name, later ones get " (2)" … with the
  lowest free number; a blank header becomes "Column N" with the same suffix
  treatment.
- "free means not the same name as any header the sheet has and not a name
  already handed out".
- Values are unchanged; `set_rows` uses the same rules but keeps everything;
  `headers()` / `rows()` give the names.

**Astra (5/5).**
- Positional reader, then a reserve-every-header allocator, Unicode `strip`
  and `casefold`.
- Found the second call site (Add Column).
- 14–20 tests; first run green; 14–17 commands.

**Result.** **No Astra latent defect.**
- 3,275 executed inputs (125 hand probes + 3,150 seeded fuzz) plus a
  MainWindow palette sequence: all 5 Astra episodes are byte-identical to the
  reference.
- Exhausted dimensions: whitespace kinds, ß/Σ/İ/Kelvin/ligature case,
  encodings, xlsx value types, error paths and `set_rows` shapes.

**Fable-only verified defects** (ref and alt agree; command:
`docker run --rm --network none -e QT_QPA_PLATFORM=offscreen -v /tmp/astra-mine/c008q:/w:ro astra-mine-c008q sh -c 'cp -r /w /tmp/w; for t in ref alt A1 A2 A3 A4 A5 F1 F2 F3 F4 F5; do ARENA_REPO=/tmp/w/$t python -I -m pytest -q -p no:cacheprovider --rootdir /tmp/w/candidate_tests /tmp/w/candidate_tests; done'`;
coordinator re-run: ref, alt and A1–A5 pass 3/3; every Fable episode fails at
least 1):
- **L-F1.** csv `Ref,,Column 2,column 2\r\nR1,,a,b\r\n`.
  - ref gives `["Ref","Column 2","column 2 (2)"]`.
  - F1, F2 and F5 give `column 2 (3)`: the dropped leftover column consumed a
    name.
  - Rules: "drop it" plus "free means … not a name already handed out".
  - The input is synthetic.
- **L-F2.** `set_rows([{"Name":"Asha", None:"Puri"}])`.
  - ref gives `["Name","Column 2"]`; F3 and F4 give `"None"`.

**Projection.** Astra stays at 5/5. Fable 2/5 → 0/5 with L-F1.

### c004 shopify-theme-vue (`01-chain-split-picker`; assets/variant-picker.js)

**Published rules.**
- Labels show their own option; Chain Length / Chain Type are the halves
  around the slash ("spaces around the slash and capitals don't matter …
  written the way it first shows up").
- The same holds when the chain is in another position or absent.
- The selection is always a real variant; the clicked value stays.
- Resolution order: "go through the rest in option order … keep each one if
  there's still a variant with it … otherwise switch it to the first value that
  has one, in the order the buttons show them".
- A length/type click never changes metal or carat; greying rule.
- Saved metal `metal_name_nondyor` only on first load and only if it is this
  product's metal, case- and space-insensitive.
- Preselected-variant rule; nothing renamed.

**Astra (5/5).**
- Rebuilt the reference algorithm, with the chain as two levels and the
  clicked value fixed.
- Case folding only where published.
- 17–35 own tests; 4/5 looped over all option orderings; 9–14 commands.

**Result.** **No Astra latent defect.**
- 3,900 natural fuzz sessions plus 1,800 odd-input sessions, hand cases and a
  772-variant product: A1–A5 and F3 differ from ref in 0 sessions.
- The only Astra split is a chain value without a slash (`No Chain`), where
  **ref and alt disagree**.
  - ref ignores a non-greyed click, which breaks R6 (a reference defect).
  - A4 matches ref; A1–A3 and A5 match alt.

**Reference quirks worth repairing.**
- That `No Chain` click.
- `selectInitialVariant` re-applies the saved metal on a second call,
  contradicting R12.

**Projection.** Astra stays at 5/5; Fable stays at 1/5.

### c008l python-llm-client (`01-chat-memory-and-fallback`; orkit.py, 4_chat.py)

**Published rules.**
- `stream()` takes `ask()`'s prompt shapes and sends the same messages.
- The chat sends the whole conversation; `/reset` vs `/model`.
- "a model hasn't answered until it sends some actual text: empty or just
  whitespace doesn't count".
- "when a reply starts with <think>, the answer only begins after the
  </think> … a <think> anywhere else in a reply is just text".
- A blank model is a failure and fallback moves on, with no switch once
  answering has started (raise AllModelsFailed).
- The answer is exact, with nothing trimmed.
- An unfinished turn is forgotten; Ctrl-C semantics.
- One log line per answer; `ask()`'s output unchanged.

**Astra (5/5).**
- An `_AnswerText` prefix/thinking/answer state machine shared by `ask` and
  `stream`; history committed after success.
- `except Exception` (KeyboardInterrupt propagates).
- 16–22 tests left in the repo; 1–2 web searches; 11–14 commands.

**Result.** **No Astra latent defect.**
- About 8,600 fuzz scripts plus 26 probes.
- The only Astra divergence (P3: a model dies while its partial reply is a
  strict prefix of `<think>`, e.g. `"<thi"`) is shared by all 10 episodes.
  - The ref and alt "half answer is on screen" premise is false there, so it
    is discarded as unsettled.
- Reference gap: a `finish_reason="error"` chunk without a top-level `error`
  key is treated as a finished answer. It is moot with the real SDK.

**Fable-only verified defects** (command:
`docker run --rm --network none -v /tmp/astra-mine/c008l:/w:ro astra-mine-c008l sh -c 'cp -r /w /tmp/w && cd /tmp/w && python latent.py'`;
coordinator re-run matches):
- **D1.** A reply that is exactly `"<"` (ask, stream, chat).
  - ref, alt, A1–A5, F1–F3: the answer `"<"`.
  - F4 and F5 fall over to the next model and remember "less".
- **D2.** `"\n<think>plan</think>\nHello"`.
  - ref and alt return the whole text.
  - F2 strips the block (fairness moderate-high: intent arguably favours F2).

**Projection.** Astra stays at 5/5. Fable 5/5 → 3/5 (D1) or 2/5 (D1+D2).

### c011 quant-analytics-pipeline (`01-real-change-points`; egx_pipeline/regime_lab.py)

**Published rules.**
- `bocpd_report(name, df)` takes the frame and returns `expected_age`,
  `p_older_20` and `change_points`, oldest first, as plain fractions.
- The model is unchanged (hazard 1/60, prior, forward run, 250-day cap).
- Suspension days (high==low and close unchanged) are dropped first.
- "a day's probability is what the model says on the 5th trading day after it
  … the chance that the regime in force on that 5th day began on the day
  itself".
- No probability until 5 days after; the first return day is excluded.
- bocpd's second output is filled for every day, with NaN where there is no
  probability.
- The 3 strongest days, spaced more than 10 trading days apart.
- The age includes both ends; `p_older_20`.

**Astra (5/5).**
- Readout `R[s+6,6]/R[s+6,1:].sum()`, vectorised or looped.
- Tested both sides of every numeric boundary; 2/5 built exhaustive
  segmentation oracles.
- 13–15 commands.

**Result.** **No Astra latent defect.**
- Across 585 frames, 129 `bocpd()` arrays and 32 boundary cases: dates are
  identical, and values match within 6e-16. The float32 frame differs by
  1e-5, inside tolerance and not natural.
- Discarded:
  - A NaN close mid-frame (Astra and F3 use `.iloc[1:]` instead of `.dropna()`):
    no sentence covers it, alt sides with Astra, and `fetch_daily` already drops
    NaN closes.
  - A 1-row frame: ref and alt disagree.

**Fable.** F4 and F5 already fail: a joint readout with no renormalisation,
untested under the cap.

**Projection.** Astra stays at 5/5; Fable stays at 3/5.

### c009t trading-backtest-script (`01-ist-dates-and-holding`; backtest.py)

**Published rules.**
- tradeTimes are epoch milliseconds mapped to the India date, independent of
  the machine's timezone.
- Entry at the close of the first session after the signal day.
- r5/r10/r20 sessions after entry; no entry close means no trade.
- The date column is the entry date.
- No re-entry while holding the same scan's stock.
- Sell at the r20 close or the stop; re-entry only after the sell session.
- "my stop is 12% on closing basis: the first close after the entry session
  that is 12% or more below the entry close".
- Returns after the stop are the stop return.
- Signals and Avg/day count file trades over the scan history length.
- Monthly table by signal month; everything else unchanged.

**Astra (5/5).**
- In-place edit or refactor using Asia/Kolkata and `searchsorted(right)`.
- Stop as `entry*0.88`, which is float-identical to ref.
- Signals sorted chronologically; tz-aware price index normalised.
- 9–11 tests kept; checked under 3–4 host timezones; 10–18 commands.

**Result.** **No Astra latent defect.**
- In 1,500 random natural scenarios across 7 host timezones, A1–A5 are
  byte-identical to ref on trades, summary and monthly.
- Discarded:
  - Descending tradeTimes: ref processes file order, all episodes sort, and
    "earlier signal" favours the episodes. This is a fragility in the
    reference.
  - A tz-aware price index: ref crashes and alt runs.

**Fable-only verified defects** (command:
`docker run --rm --network none -v /tmp/astra-mine/c009t:/w:ro astra-mine-c009t sh -c 'cp -r /w /tmp/w && cd /tmp/w/extra && for t in ref alt A1 A2 A3 A4 A5 F1 F2 F3 F4 F5; do ARENA_REPO=/tmp/w/$t python -m pytest -q -p no:cacheprovider test_candidates.py; done'`;
coordinator re-run: everything passes 2/2 except F1 and F5, which fail 1 each):
- **LD1.** A close exactly 12% under entry (80.00 → 70.40; 160.00 → 140.80).
  F1 computes `(70.40/80-1)*100 = -11.99999999999999` and misses the stop.
  Write the oracle in integer paise.
- **LD2.** An entry 4 sessions before the end of the data has no r5, so no
  row. The stock fires again the next day. F5 opens a second trade because it
  ties holding to whether a row is written.

**Projection.** Astra stays at 5/5. Fable 5/5 → 3/5.

## Cross-task habit table

Counts are episodes out of 5 per task. A dash means the habit was not observed,
or the dimension was checked and Astra was correct.

| habit | c012 | c010 | c024 | c007 | c001 | c008q | c004 | c008l | c011 | c009t | defect-causing? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| five episodes converge on one design (all-or-nothing) | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 5 | amplifies every defect |
| stops at the first all-green own suite, with no adversarial pass | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 5 | yes, with the next row |
| own test inputs never contain the shape that later breaks it | 5 (no non-`#` note) | 5 (every pmset line had a time) | 5 (never a null total) | 5 (no decimals; Unicode hyphens tested only inside words) | 5 (eyes tested as neutral) | - | - | - | - | - | yes |
| exact-literal equality on a raw field without normalising it | - | 5 (`== "charging"` on `"charging present: true"`) | - | - | 3 (exact rank strings) | - | - | - | - | - | yes (c010) |
| key-present treated as value-present (`hasOwnProperty`, null and undefined count) | - | - | 5 | - | - | - | - | - | - | - | yes (c024 D1) |
| positional token heuristic ("last word") instead of the published pattern | 5 | - | - | - | - | - | - | - | - | - | yes (c012 D1) |
| ASCII-only character class where the rule covers Unicode | 2 (space/tab regex, harmless) | - | - | 5 (typed dash) | - | 0 | 0 | 0 | - | - | yes (c007) |
| "every `.` ends a sentence" (no decimal awareness) | - | - | - | 5 | - | - | - | - | - | - | yes (c007 C2) |
| strict decoding with no fallback (crash or mojibake on a bad byte or BOM) | 5 | - | - | 0 (`utf-8-sig`) | 3 (no BOM) | 0 | - | - | - | - | yes (c012 D2; c001 C2 excluded by spec) |
| abort the whole run on an unexpected line (all-or-nothing validation) | - | - | - | - | 5 | - | - | - | - | - | not counted (C2/C3 contexts) |
| removes existing error handling | 3 | - | - | - | - | 0 | - | - | - | - | no settled impact |
| over-applies one rule to a neighbouring feature | - | - | - | - | 5 (neutral colours to eyes) | - | - | - | - | - | contested (c001 C1) |
| filters a derived list by current state | - | - | 3 (subjectsFor) | - | - | - | - | - | - | - | contested (c024 D2) |
| Unicode-aware strip/casefold used correctly | - | - | 5 | - | 5 (Turkish) | 5 | 5 | - | - | - | no (protective) |
| misses a second call site | 0 | 0 | 0 | 0 | 0 | 0 (found Add Column) | 0 | 0 | 0 | 0 (From/To, monthly) | never |
| first-match regex / string sort / float-vs-decimal errors | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 (stop `entry*0.88` = ref) | never |
| off-by-one at a published boundary | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | never |
| wrote own tests (and left them in the repo) | 5 (inline, none left) | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 5 (heredoc, none left) | 5 | - |
| pip or network attempts before finding `/opt/deps` | - | - | - | - | 5 | 5 | - | 5 | 5 | 5 | - |
| commands per episode | 5–9 | 11–17 | 10–15 | 8–12 | 16–20 | 14–17 | 9–14 | 11–14 | 13–15 | 10–18 | - |

**Reading.** Astra's misses are not in boundaries, ordering, numerics or
coverage of call sites: it scored 0 on those across 50 episodes, and on 5 tasks
its code is behaviourally identical to the reference. Its misses come from one
pattern: **an input shape its own tests never contained, on a rule it encoded
narrowly**. The instances:
- a time-less pmset line (c010);
- a non-`#` note after the class (c012);
- `null` versus an absent key (c024);
- a decimal point and a Unicode hyphen with a gap on both sides (c007).

Every one is an input *representation* (Astra's "unseen shape" family), never
a scope or state error.

**Fable's misses** run in the other family:
- scope, missing the second call site: c008q Add Column 3/5;
- state and interaction:
  - c009t holding tied to row emission;
  - c024 emptied subject;
  - c004 chain click fixes only the clicked half, 3/5;
  - c011 conditioning inconsistency;
- float precision at an exact boundary: c009t F1 (12% stop);
- over-normalisation: c007 F3 `casefold`; c004 4/5 collapse whitespace.

Cases that keep Fable failing are therefore available on most tasks
independently of the Astra cases. The shared ones are c012 D1 (F2), c007 C2
(F4) and c010 L1 (F1, F5).

## Fable mistakes to preserve when adding cases

| task | Fable episodes already failing | added case that flips a Fable pass |
|---|---|---|
| c012 | F1, F3, F4, F5 (glued `#` comment) | D1 flips F2 |
| c008q | F1, F3, F4 (Add Column not deduplicated) | L-F1 flips F2, F5 |
| c024 | F1, F3, F4 (emptied subject) | D2 flips F2, F5 (contested) |
| c007 | F1, F5 (sentence lookahead), F2 (ASCII dash) | C2 flips F4; C3 flips F3 |
| c004 | F1, F4, F5 (chain click), F2 (fallback order) | none found |
| c011 | F4, F5 (joint readout) | none found |
| c010 | none | L1 flips F1, F5 (Fable 0.6) |
| c008l | none | D1 flips F4, F5; D2 flips F2 |
| c009t | none | LD1 flips F1; LD2 flips F5 |
| c001 | none | C3 flips F1–F5 (medium risk) |

## Assessment: does "add hidden cases within the published rules, then re-grade the captured outputs" push Astra below 0.5?

**Why re-grading is a fair estimate.** Hidden tests are invisible to the agent,
so with an unchanged instruction the captured repos are valid samples under a
new grader. Re-grading them is an unbiased (n=5) estimate of the new pass@1.
Two caveats:
- **Shared mistakes.** Several rows that reveal one shared mistake (c010 P1–P3;
  c012 D1 rows) are one decision, not independent probabilities.
- **Bundle work and the screen.** Every added case still has to go through the
  bundle protocol:
  - the spec,
  - an executed mutant per trap,
  - the alt agreeing,
  - controls,
  - review.

  The screen remains the measure. For tasks where the reference itself needs
  repair first (c024 video ids or the `total:""` alt split, c012 latin-1, c004
  `No Chain`), fix the reference and alt before writing the check.

| task | does it push Astra < 0.5? | Astra episodes failing | Fable episodes newly failing | note |
|---|---|---|---|---|
| c012 | **yes** (D1) | 5/5 | 1 (F2) → Fable 0/5 | both models under the target; the `//`-note naturalness will be questioned |
| c010 | **yes** (L1) | 5/5 | 2 (F1, F5) → Fable 0.6 | Fable stays above 0.5; needs a second, Fable-directed case to meet the per-step target |
| c024 | **yes** (D1) | 5/5 | 0 → Fable 0.4 | both under the target; repairing alt for `total:""` would give a more natural row |
| c007 | already 0.0; C2 keeps it there even if U+2011 is learned | 5/5 | 1 (F4) → Fable 0.2 | the most robust result in the set |
| c001 | only with an instruction amendment (C1) | 5/5 | 0 | that amendment is a premise change, so it needs a fresh screen |
| c008q | **no** | 0/5 | 2 (F2, F5) → Fable 0.0 | needs a new published rule or step for Astra |
| c004 | **no** | 0/5 | 0 | same |
| c008l | **no** | 0/5 | 3 → Fable 0.4 | same |
| c011 | **no** | 0/5 | 0 | same |
| c009t | **no** | 0/5 | 2 → Fable 0.6 | same |

**Totals.**
- **Astra:** of the 45 Astra episodes that currently pass (9 tasks × 5),
  re-grading with the clean cases fails **15** (c012, c010, c024), or 20 with
  the contested c001 C1.
- **Fable:** of the 31 Fable episodes that currently pass, the cases fail
  **12** (c012 1, c008q 2, c007 2, c008l 3, c010 2, c009t 2, c024 0). That rises
  to 17 with c001 C3 (5, medium risk) and 19 with c024 D2 (2, contested).

**Conclusion.** Within the current instructions, hidden-case hardening reaches
Astra on about a third of the tasks. Those are the ones whose published rule
names a pattern (`the L...; part`, a state wording, "dont have total", "a
sentence ends with …") that Astra encoded narrowly and never fed an
alternative shape. On the other five, Astra's code is behaviourally identical to
the reference across thousands of executed inputs. Only a premise that changes
(the seventh lever) or a new step can move it there.

## Bundle hygiene noticed during the mining (not acted on)

- **c007:** `environment/repo/_gen/ref.py` (a reference implementation) and
  `_gen/mut.py` sit in the local bundle's `environment/repo`.
  - They are not in `tests/fixture-manifest.json` and were not in the screened
    package.
  - `environment/.dockerignore` admits `repo/**`, so an agent image rebuilt
    from `environment/` would ship the reference.
- **Other stray files** not in the manifests:
  - c011: `environment/repo/_scratch/`. It briefly held `astra.patch` and a
    copy of the hidden test, then was emptied by a re-sync.
  - c024, c009t, c008l: `home/user/...` duplicates.
  - c004: `uploads/` and `section-product.patched.js`.

  All are absent from every episode tree.
- **Reference defects found:**
  - c024: video-id cases; an answer pointing at an array hole.
  - c012: whole-file latin-1 fallback for an undefined cp1252 byte.
  - c007: `is_heading` treats a decimal as a sentence end.
  - c004: a `No Chain` click is ignored; the saved metal is re-applied on a
    second init.
  - c010: a switched-off alert on the first reading contradicts trap
    `t_disabled_state`.
  - c009t: file-order dependence on tradeTimes; crash on a tz-aware index.
  - c008l: a `finish_reason="error"` chunk is treated as a finished answer.
