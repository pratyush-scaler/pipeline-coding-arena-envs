# Why the planted traps did not bite: five solved tasks compared with c-032

## Scope and limits
I classified all 56 objective-owning traps in the five solved tasks: c-039 has 12, c-028 has 11, c-026 has 12, c-025 has 10 and c-017 has 11. I classified the 7 in c-032 separately.

Only one blind patch exists per task, and only for four tasks:
- **Astra's patch** for c-039, c-026 and c-017. The c-017 run was graded void, but the code is readable.
- **Fable's patch** for c-028.
- **c-025 and c-032 have no patch.** For c-025 I relied on the authors' line-by-line reads of both blind patches in `quality.difficulty_case.calibration_runs`.

Every model/task pair not covered by a patch is backed only by the screen, where every check passed in all 5 episodes for both models.

Several blind-solve JSONs were graded on earlier drafts with fewer checks:
- c-039 Fable is missing o_marc_digito, o_marc_repetido and o_parrafos_pegados.
- c-026 Fable is missing o_menu_fit, o_counts_live and o_agent_unknown_zone.
- c-025 Fable is missing o_pk_width and o_whole_table_scan.

Three blind zeros are voids, not bites:
- **c-017 Astra:** the grader refused to run the suite because of `gui.py: getattr(sys,`.
- **c-032 Astra:** the grader refused because of `ra_sheet.py: import_module(`.
- **c-017 Fable:** `provider_incomplete`, with 0 tool calls.

## Per-trap table
Columns: Res = the trap carries `research`. Cause letters are the brief's A–G.

| task | trap | type | Res | cause | evidence (settling sentence → hidden input → what the solver's code did) |
|---|---|---|---|---|---|
| c-039 | t_bordes_marcador | text_sem | Y | A | "lo que tenga pegado justo delante y justo detrás no puede ser ni letra ni número" → `_De feito_`, `vinte`, `caeu` → Astra wrote `(?<![^\W_])…(?![^\W_])`, which is the author's own correct predicate |
| c-039 | t_espazo_non_separable | repr | Y | A | "con el espacio que sea entre ellas" plus "espacios que no son el espacio normal" → `por outra banda` → marker words joined with `\s+` |
| c-039 | t_bordes_numero | text_sem | Y | A | same "ni letra ni número" → `2024cae`, `cae3` → `[^\W_]`; Astra's own tests use "1", "９", "²" |
| c-039 | t_marcador_repetido | numeric | N | A | "si uno sale cinco veces cuentan las cinco" → each connector appears twice → `finditer` per entry; the original `tl.count` already counted occurrences |
| c-039 | t_acentos_descompostos | repr | Y | A | "acentos partidos en dos trozos, guiones de cortar palabra que no se ven" → NFD text with U+00AD and NBSP → `NFC(t.replace("­",""))` |
| c-039 | t_parrafos_sen_limpar | arch_carry | N | D | No sentence mentions the second argument; hidden test calls `analiza(T_PS_SUCIO, PS_SUCIOS)`. Normalising inside `palabras`/`oracions`/`unif_parrafos` covers both entry paths; `comproba.py` shows the two-argument call (B) |
| c-039 | t_parrafo_pecha_frase | repr | N | A | "cada párrafo acaba frase aunque no lleve punto final" → title without a period, CRLF → splits each line before the sentence regex |
| c-039 | t_frase_dunha_palabra | regr_bait | N | A | "lo de no contar las frases de una sola palabra déjalo como está" → `Ollo.`, `Si.` → keeps `len(p.split()) >= 2` |
| c-039 | t_xanela_derradeira | symptom | N | A | "contando también la última que quepa entera" → exactly 120 words → `range(0, len(w)-win+1, win//2)` |
| c-039 | t_xanela_ancorada_final | decoy | N | D | 150-, 180- and 200-word texts → the same `+1` fix produces exactly the step-60 window family; no solver anchors a window at the end |
| c-039 | t_sinais_sen_datos | absence | N | A | "salga vacía (None)… el valor y su sub-score… la diversidad léxica es la excepción" → short text → None from three measures; TTR left alone |
| c-039 | t_total_sen_repartir | numeric | N | A | "el total se reparta solo entre las señales que sí se hayan podido medir" → divides by the sum of available weights |
| c-028 | t_digit_class | text_sem | Y | A | "только … цифрами 0-9, с одним минусом в самом начале и максимум одной точкой" → `1_000`, `١٢`, `１２`, `+2`, `1e3` → Fable wrote an explicit `[0-9]` grammar, not `\d` |
| c-028 | t_padding_class | text_sem | Y | A | "обычные пробелы и табуляции — их отрезаем, а всё остальное оставляем" → ` 1.5`, `3\n` → `strip(" \t")` |
| c-028 | t_vector_hash | library | Y | A | "Vector(1, 2) и Vector(1.0, 2.0) — … один и тот же ключ"; the instruction also names NotImplemented → `hash((x, y))` plus `return NotImplemented` |
| c-028 | t_dataset_slice | protocol | Y | A | "мне нужны … срезы… уходит за конец — просто заканчивается" → `[::-1]`, `[-1:0:-2]` → `range(*idx.indices(len(self)))` |
| c-028 | t_scaler_zero_range | numeric | Y | A | "размах … считаем равным единице … (в настоящем sklearn именно так)": the researched fact verbatim → `scale = rng or 1`, no clipping |
| c-028 | t_lr_contract | error | Y | A | "не число или … не строго больше нуля — это ConfigError, а не TypeError" → `"0.1"`, None, NaN → `_is_number` (isfinite, not bool) then `not lr > 0` |
| c-028 | t_merge_alias | repr | Y | A | "вложенный блок … только те ключи… дефолты после любого числа загрузок…" → recursive `_deep_merge` over `deepcopy` |
| c-028 | t_bom_reading | text_sem | Y | A | "с BOM в начале файла … как обычный" plus duplicate-key sentence → `utf-8-sig` and `object_pairs_hook` |
| c-028 | t_shared_state | repr | N | A | "у двух разных объектов не должно оказаться одного и того же списка" → `_scores` moved to `__init__`, `deepcopy` in `from_dict` and `validate_config`. The repo notes also teach deepcopy (B) |
| c-028 | t_report_atomic | resource | Y | A | "ни обрывка, ни затёртого прошлого отчёта" → `json.dumps(allow_nan=False)` first, then `mkstemp` plus `os.replace` |
| c-028 | t_collect_files | library | N | A | "имена именно файлов, и каждое имя ровно один раз" → a directory named `arch.csv` → `{p.name … if p.is_file()}` |
| c-026 | t_reported_case_only | symptom | N | D | o_zone_readout only re-checks "name — one-liner" for '1' and 'annex'; `DASH` already exists, so any fix of the crash passes |
| c-026 | t_id_as_index | repr | Y | A | "the zone is the one whose id that is, not whatever happens to be sitting in that slot" → TOWER stored as 3, annex, 0, 2, 1 → `find(z => zoneKey(z.id) === id)` |
| c-026 | t_blank_key_is_zone_zero | absence | Y | A | "blank or junk or a floor we haven't got … idle" plus "zone 0 is a real zone" → `zoneKey` rejects blank and non-finite keys |
| c-026 | t_nan_comparator | ordering | Y | A | "only counts as part of the tower when it carries a floor level number" → level null or missing → `Number.isFinite` partition plus stable sort |
| c-026 | t_count_outside_budget | repr | Y | A | "count and brackets and all, never runs past 18 … that … is one of the 18" → `room = 18 - suffix.length` |
| c-026 | t_fit_boundary | text_sem | Y | A | "counting them the way a person reads them" → exactly-18 cards with emoji → `Intl.Segmenter` and `<= room` |
| c-026 | t_lift_counted_somewhere | absence | N | A | "anyone in the lift … not in any zone" → `if (a.state==='lift') continue` |
| c-026 | t_label_memo | temporal | N | E | No sentence mentions re-rendering. Astra recomputes counts on every `menu()` call and even self-tested for stale counts; no solver memoises |
| c-026 | t_sort_mutates | library | Y | A | "rendering the menu mustn't shuffle my list" → `zones.slice().sort` |
| c-026 | t_meeting_beats_state | exception | N | A | "In a meeting … in the lift … even when they're on their way to the meeting" → lift checked first; meeting only applies when seated |
| c-026 | t_sibling_path | arch_carry | N | A | "zones get looked up the same way as the cards" → `agentLine` reuses `zoneAt` |
| c-026 | t_agent_unknown_raw | absence | N | A | "the zone bit comes off the end" → zone 99 or 'basement' → `z ? DOT+z.name : ''`. The test is lenient: it also accepts a trailing `' · '` |
| c-025 | t_declared_size_not_enforced | library | Y | A | "SQLite never enforced that size… throw it away": the fact is the user's own reproduction (VARCHAR(10), 50 characters) |
| c-025 | t_pk_width_exemption | exception | Y | D | "EVERY column…" → a VARCHAR(10) primary key holding 50 characters → Astra's capacity pass after every mapping branch also covers primary keys |
| c-025 | t_varchar_counts_characters | repr | Y | A | "VARCHAR(n) in MySQL counts CHARACTERS" (fact verbatim) → both solvers kept separate character and byte maxima |
| c-025 | t_text_caps_are_bytes | numeric | Y | A | "limits are BYTES, 65,535 and 16,777,215 … UTF-8 size" (fact verbatim) → 30,000 Devanagari characters |
| c-025 | t_fit_rule_skips_sibling_paths | symptom | Y | A | "not only the ones declared VARCHAR or TEXT" → DATETIME, NUMERIC, untyped and dob columns all handled |
| c-025 | t_row_cap_scan | resource | Y | E | 12,000 rows with the long value last → Astra "streams every … value with no row bound"; the predicted inherited `LIMIT ?` never appeared |
| c-025 | t_text_cap_inclusive | numeric | Y | B | exactly 65,535 bytes → the fixture already compares the caps inclusively (per the reviewer); Astra self-tested 65,535 and 65,536 |
| c-025 | t_length_stops_at_nul | text_sem | Y | D | Not announced. `x*40+\0+NA*30000` → reading raw values in Python, which the announced invalid-UTF-8 rule forces, measures past the NUL |
| c-025 | t_cast_blob_db_encoding | library | Y | D (+C) | Not announced. A UTF-16le database → measured per value in Python; Fable noted that `sqlite3_column_text` returns UTF-8 even for UTF-16 databases |
| c-025 | t_undecodable_text_needs_binary | error | Y | A | "bytes that are not valid utf8mb4 … plan … binary … MUST NOT fall over" → both used a bytes `text_factory` |
| c-017 | t_encoding_bom | text_sem | Y | A | "в юникоде с меткой в начале — открывать надо любой" → UTF-16LE with BOM → `raw.decode(json.detect_encoding(raw))` |
| c-017 | t_bom_prefix_collision | library | Y | E | UTF-32 is never mentioned → hidden UTF-32LE and UTF-16BE files → `json.detect_encoding` orders the marks itself; no hand-written sniffer was ever written |
| c-017 | t_capture_shape | protocol | Y | A | "файл будет пустой или … одно слово null: это пустой инвентарь" (researched fact verbatim) → handles empty file and dict → [dict] |
| c-017 | t_windows_path | repr | Y | A | "имя inf-файла, а не путь к нему в хранилище" → `ntpath.basename` |
| c-017 | t_printed_list | distrib | N | D | `app.py` already prints `record.inf_name`, so fixing the record fixes the printed list |
| c-017 | t_cim_asterisk | repr | Y | A | "звёздочки … значат, что эта часть неизвестна" (fact verbatim) → `[0-9*]{14}` then `date.fromisoformat` |
| c-017 | t_critical_class_decoy | decoy | N | A | "даже если производитель не майкрософт… список критичных классов в программе уже есть" → `is_critical_driver_class` (the helper is visible: B) |
| c-017 | t_unknown_vs_empty | absence | Y | A | "поля с устройствами нет или там null … не трогаем; с загрузочностью ровно так же" → keeps None; `boot is not False` |
| c-017 | t_identity_case | repr | Y | A | "oem10.inf и OEM10.INF — это один и тот же пакет" → `casefold` key and union of devices |
| c-017 | t_oem_number_order | ordering | Y | A | "oem2 раньше oem10" → `int(name[3:-4])` |
| c-017 | t_guard_second_path | arch_carry | N | A | "программа должна отказаться удалять … ни папок, ни копий" → `validate_driver_removal` runs before `export_driver` |
| **c-032** | t_scale_order | ordering | N | **BIT** | Astra 5/5, Fable 3/5 (rows and columns), Fable blind run also failed it; detail below |
| c-032 | t_stale_rating / t_text_figures | numeric / repr | Y | partial | Fable 1/5 only, through text-typed figures; detail below |
| c-032 | t_hardwired_sibling | arch_carry | N | A | the exact CLI command is quoted in the instruction; never failed |
| c-032 | t_matrix_row_allocation | library | Y | A | "an empty row hanging at the bottom"; never failed |
| c-032 | t_control_line_breaks | text_sem | Y | E | Hidden `\x0b`, CRLF and U+2028 → `splitlines`, not the predicted `split('\n')`; never failed |
| c-032 | t_refusal_contract | error | Y | A | the full contract is in one sentence; never failed |

## Cause distribution (56 traps in the five solved tasks)

| cause | all | researched (37) | not researched (19) |
|---|---|---|---|
| A: announced in the instruction | 45 (80%) | 31 (84%) | 14 (74%) |
| D: covered by the natural fix | 7 (13%) | 3 (8%) | 4 (21%) |
| E: predicted wrong idiom never written | 3 (5%) | 2 (5%) | 1 (5%) |
| B: shown in the repository | 1 (2%) | 1 (3%) | 0 |
| C, F, G | 0 | 0 | 0 |

- **C is a frequent secondary cause.** The solvers reached for the robust idiom unprompted: `[^\W_]`, NFC, `utf-8-sig`, `object_pairs_hook`, `Intl.Segmenter`, `ntpath`, `idx.indices`, `json.detect_encoding`.
- **F did not occur.** No hidden test failed to exercise its divergence. The only weak test is the lenient trailing `' · '` in o_agent_unknown_zone, and it still fails the wrong reading.
- **Of the 31 researched traps that were announced:**
  - In about 15 the researched fact itself is written in the user's words. Examples: sklearn's zero-range rule, "SQLite never enforced", VARCHAR counts characters while TEXT caps are bytes, CIM asterisks, the single-object/empty/null capture shapes, case-insensitive file names, "acentos partidos".
  - In about 16 a stated rule makes the fact irrelevant. Examples: the "0-9" grammar, "ni letra ni número", "not whatever is in that slot".
  - So 0 of 37 researched traps bit in the solved tasks.

## c-032, the hard task
**Screen.**
- **Astra:** pass@1 0.0, all five episodes at 0.76, failing o_matrix_rows, o_matrix_cols and o_matrix_values every time.
- **Fable:** pass@1 0.2, with episodes at 0.74, 1.0, 0.84, 0.76 and 0.76. Fable's per-episode failures match the mutants exactly:
  - The two 0.76 episodes failed rows, columns and values, which matches the baseline that keeps the positional loops.
  - The 0.84 episode failed rows and columns only. That is the `m_partial_scale_order` signature: it took the numbers from the labels but laid them out in list order.
  - The 0.74 episode failed o_numbers_xlsx and both o_recompute checks. That is exactly `m_partial_str_product`: a figure typed as text gets string-multiplied (`'3'*4`).
- **The screen's own signal** found one independent hard decision: `t_scale_order`, with a fail rate of 0.6 for Fable and 1.0 for Astra.
- **Blind solves.** Fable scored 0.75, failing the same three matrix checks. Astra's 0.0 is a void (scanner refusal). The earlier-draft read says Astra "left both matrix loops exactly as they were".

**Why t_scale_order bit.**
1. The deciding sentence, "the matrix reads most likely at the top down to rare at the bottom and severity 1 on the left across to 5 on the right…", describes a result. It sits right after the empty-row complaint and reads like a description of the existing page.
2. Both visible sheets list SEVERITIES 1→5 and PROBABILITIES 5→1. The fixture's own `pnum = 5 - i` and `for snum in range(1,6)` therefore produce correct output on every input the solver can run.
3. The hidden `ra_data_tank.py` reverses both lists. Only the general sentence "i swap the sheets out… work off whatever is in them" licenses that.
4. The loops sit outside the lines the visible fixes touch (the CLI arguments and the `add_table` row count).
5. The trap is not researched and involves no library trivia.
6. The reviewer judged it settled by the instruction, so it is fair.

The text-figure partial bite has the same shape. "S, P and R have to be numbers excel can add up" is an output property the all-integer visible sheets already satisfy. The hidden shaft sheet has `("5",3,"15")` and `("3",4,9)`.

**What separates the bites from the non-bites:**
- **Status-quo code was the wrong answer.** Leaving existing code alone loses. In the non-bites, the wrong reading would have required writing new wrong code, and the models write robust code when they write new code.
- **Nothing named the variant.** In the non-bites, the variant or its mechanism was named in 45 of 56 traps.
- **Visible inputs already satisfied the rule, and there was no failing reproduction.** In the solved tasks, each rule cluster came with a narrated symptom or a "not X" contrast.
- **The divergence was a data-shape assumption baked into fixture code** (position means rank, one type per field), not a library semantic.
- **The two models fail differently.** Astra never questions a premise its own run confirms, so it fails deterministically. Fable misses one of two independent unflagged shapes per episode.

## Authoring rules, ranked
1. **The wrong reading must be the untouched fixture code.** Plant the trap as an existing positional or structural assumption that passes every visible run, so doing nothing loses. *Evidence:* c-032's `pnum = 5 - i` bit 1.0/0.6. Every trap whose wrong reading required new code was beaten.
2. **Every visible input must already satisfy the trapped rule, with no narrated failing reproduction.** Vary only a dimension the visible inputs hold constant: list order, value type, arity. *Evidence:* both c-032 sheets are canonical and all-integer. The solved tasks narrate a failure for each cluster (56%, VARCHAR(10), "Data Center throws").
3. **Never name the variant, the character or the case pair.** State the output invariant over an open input family ("captures come from whatever machine"; "work off whatever is in them"). It stays fair because the general clause entails the case. *Evidence:* 45 of 56 announced traps bit 0 times; the reviewer still called c-032's unnamed scale order settled.
4. **Use research to build hidden inputs and mutants, never sentences.** Ban parenthetical glosses that hand over the fact: "(в настоящем sklearn именно так)", "(кажется, это и есть то самое NotImplemented)", "VARCHAR(n) counts CHARACTERS", "звёздочки … неизвестна". *Evidence:* about 15 verbatim researched facts, all solved 10/10.
5. **Prefer fixture-structural divergences over library semantics.** *Evidence:* 0 of 37 researched library/text traps bit. In c-032, the researched line-break, row-allocation and refusal traps never failed, while the unresearched ordering trap was the only one that bit in every Astra episode and in 3 of 5 Fable episodes.
6. **Phrase the deciding sentence as a result description next to a different complaint, not as a correction.** Avoid "not X", "instead of", "even when", "а не", "aunque". *Evidence:* c-032's matrix sentence, placed after the empty-row complaint, bit. Its "work it out instead of copying it" never bit on its own.
7. **No enumerations of edge members.** Each listed item becomes a self-test the solver writes. *Evidence:* Astra's 256-line c-039 test file mirrors "paréntesis/comilla/final" plus NBSP and U+202F. Its c-026 tests iterate 24 "blank or junk" keys. Its c-025 tests cover 65,535/65,536, NUL and UTF-16 databases.
8. **Keep the trapped code out of every function another clause's natural fix rewrites.** Verify the item still fails after applying the natural fixes of all other clauses. *Evidence:* the 7 D cases: range `+1`, normalising inside `palabras()`, `app.py` already printing the record field, Python-side reads forced by the invalid-UTF-8 clause, a central capacity pass covering primary keys, an existing `DASH`.
9. **Count a new-code trap only if a blind patch actually wrote its predicted wrong idiom.** Otherwise retire it. *Evidence:* the lever_plan predicted `z.id === key`, `b.level - a.level`, a hand-written BOM sniffer, `split('\n')`, `LIMIT ?` and `CAST AS BLOB`; the patches show none of them.
10. **Fix the determinacy-probe selection bias.** Admit a difficulty case only if the models answer it correctly when asked directly (fair) and miss it in the blind solve (unnoticed). *Evidence:* in c-025, six cases were admitted after "both calibration families independently returned the reference's answer", and the blind solve still scored 1.0.
11. **Put at least two independent status-quo divergences in each step, in different files or output paths.** *Evidence:* Fable's 0.74 episode missed only text figures and its 0.84 episode only the layout. Fable's pass@1 of 0.2 needed both traps; either alone would give roughly 0.4–0.8.
12. **Scrub every visible demonstration of the hidden case,** or make it agree with the wrong reading. *Evidence:* the reviewer's B findings: `comproba.py` and `contido_a.py` in c-039, the deepcopy and slice notes in c-028, names already over budget in c-026, both date shapes in the c-017 samples, the `*_MAX_BYTES` names and inclusive caps in c-025.
13. **Deliver the variant through the channel the user says changes**, such as a swapped data or capture file run through the CLI, so the solver's own check on repository data passes. *Evidence:* c-032's tests copy `ra_data_tank.py` over the job sheet.

**Pipeline note.** Two blind "0.0" results came from scanner refusals (`getattr(sys,`, `import_module(`) and one came from a provider failure. Several blind runs were graded against earlier drafts. Both distort blind-solve difficulty evidence.
