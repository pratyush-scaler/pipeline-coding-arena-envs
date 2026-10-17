# Why the traps in four 0/20 tasks bit both model families

## Summary

Each of the four tasks has **exactly one decision that both families got wrong in 10/10 runs**. That decision alone is enough to make pass@1 = 0. In all four, the rule is stated as a **general domain concept in the user's words**, while the repository, docs and seed data implement a **narrower proxy** of it. The hidden input sits exactly where the proxy and the concept part ways.

FAILURES.md overstates how many independent decisions bit, and it misattributes several checks. Many failing checks are cascades of one upstream miss.

| task | biting decisions (FAILURES.md claimed) | both-family 10/10 biter |
|---|---|---|
| ai-agent | **3** (8) | resolve annotations when each request goes out, not when the tool is registered |
| club | **4** (6) | count orders, not ledger rows |
| nfse | **~6** (8) | link each cancellation to its note by number, not by where it sits |
| forum | **1** (5) | strip full-width and typographic separators too |

Evidence quality:
- **Python tasks (ai-agent, nfse):** I read the pytest assertion text.
- **JS tasks (club, forum):** the verifier kept no stdout. I inferred causes from the 20 captured repos per task plus the hidden test inputs.
- **Masked traps:** some traps sit behind an earlier failure in the same test, so the runs never exercised them. I call these masked, not passed.

---

## 1. a_f2_python-ai-agent (zh)

Rewards: Fable 0.15–0.24, Astra 0.15–0.27.

The deciding fact: **every test built on the `LATE_TOOLS`/`LIVE_TOOLS` modules failed, and every test on `PLAIN_TOOLS` passed, in all 20 runs**, except one trim test (`test_a_round_whose_block_outgrows_the_cap`, last row of the table). Both families kept `register()` building the schema at decoration time, as the original code does. Both "fixed" the visible symptom (every type reported as "string", caused by PEP 563 string annotations) with `get_type_hints` at that old call site.

| check | FAILURES owner (type) | F/A | mechanism | wording that settles it | what the code did |
|---|---|---|---|---|---|
| o_schema_late_names | t_late_binding (temporal_state) | 10/10 | (c)+(a) | "**每次发请求时**报给模型的那份参数类型…"; "我回归时…自己 new 一个 ToolRegistry、拿 register 现写现挂" | Hidden modules define `Keywords = list[str]`, `Count`, `Meta` *below* the decorated function, and quote names inside `__future__` modules. At decoration time `get_type_hints` raises, so everything falls back to `string`. |
| o_schema_optional_generic | t_union_not_optional | 10/10 | cascade of the row above | — | The differing fields are only `top_n`/`meta`/`rows`, which are all late names. The union rule itself (`mode`, `flag`) was right. **Misattributed.** |
| o_args_typed, o_args_text_untouched, o_args_bool_strict, o_args_refuse, o_save_reload | t_pep563_schema, t_default_value_guess, t_bool_text_value, t_conversion_blows_up, t_late_binding | 10/10 each | cascade | — | `keywords` is advertised as `string`, so the list is refused (`[Error]… keywords 应为 string`); then `CALLS` is empty, a `KeyError`, or `"ok:str"`. The PLAIN_TOOLS coercion test passed. **The wild-number, bool and default-guess traps were never reached.** |
| o_schema_types | t_generic_element_missing / t_hints_all_or_nothing / t_outer_container_binding | 8/10 | (b)+(a), gated by technique | "带下标的泛型…按最外层的容器报" meets "其它的…报 string"; "名字指着什么，按它在那个模块里实际绑着的东西算" | `list[Source]`, `Rows[Source]` and `t.Union[list[Source], NoneType]`, where `Source` exists only under `TYPE_CHECKING`. The one unresolvable inner name erases the outer container. In 7 Fable and 6 Astra runs it also erased `depth: int` (all-or-nothing hints). The CI rule banning `__globals__`/`sys.modules` removes the easy per-name route. The 2 Fable passes used a NameError→placeholder `localns` loop. |
| o_trim_stops_at_cap | t_cap_counts_storage / t_live_block_atomic | **10/2** | (c)+(b) | "要丢一起丢"; "这道手续是发出去那一下做的，别回头去改存着的历史" | The cap is smaller than one round's block and `keep_recent=0`. Trimming after each append dropped the partial block, then `c4` landed in saved history as an orphan: `[('tool','hit','c4'),('assistant','收工')]`. |

**Never failed: o_pairing_every_request, o_legacy_load, o_trim_protects_block.**
- Traps covered: t_two_live_paths, t_null_is_a_value, t_fill_missing, t_legacy_repair, t_orphan_inside_block, t_reply_order, t_block_spans_stray_reply, t_protect_boundary, t_keep_recent_floor and others.
- Why they passed:
  - They are written almost as an algorithm next to the visible 400-error symptom: "谁先谁后无所谓…tool_calls 摘掉、正文照原样留着；没有主的 tool 消息直接丢", "/load 进来…也不许", "整体是按位置算的", "哪怕因此还超着上限也认了".
  - Their tests run on PLAIN_TOOLS, so no cascade could reach them.
  - `test_the_tools_get_what_the_schema_promised` judges execution against the agent's *own* advertised schema, which kept it independent of the schema miss.

---

## 2. a_f0_next-js-club-management (fa)

Rewards: Fable 0.27–0.86, Astra 0.40 in all 10 runs.

| check | owner (type) | F/A | mechanism | wording | what the code did |
|---|---|---|---|---|---|
| o_reasons_all | t_rows_not_orders (distributed_invariant) | **10/10** | (e)+(a), second writer | "…از روی **سفارش‌هایی** سنجیده شود که واقعاً ثبت شده‌اند و لغو نشده‌اند"; "دفتر سفارش‌ها را پنل ادمین هم عوض می‌کند" | The hidden ledger contains `ord_8801` twice (the desk wrote it twice). Every run counts `live.length` (rows), not distinct orderIds. The failing test is `usage_one_order`, not `reasons_stack`. |
| o_code_variant | t_cancel_first_row (temporal_state) | 3/10 | (c)+(a) | "سفارشی که لغو می‌شود استفاده‌اش از کد آزاد می‌شود" | `cancelOrder` kept the original `rows.find`, so the duplicate row keeps spending the code. FAILURES quotes the digit sentence; the digits were fine. |
| o_usage_limit_ledger, o_user_once_cancel, o_paths_agree | t_committed_only / t_desk_statuses (semantic_decoy) | 3/10 each | (e) | "ثبت شده‌اند و **لغو نشده‌اند**" (a negative definition) | Whitelist `status === "committed"`. Hidden statuses: shipped, paid, delivered, processing, confirmed, "آماده ارسال". The 7 Fable runs that wrote `!== "cancelled"` passed all three. Route delegation (t_duplicate_route) was right in all runs. |
| o_round_down | t_cap_fraction (exception_interaction) | **7/0** | (b) | "…رو به پایین گرد می‌شود. تخفیف نه از سقف تخفیف خود کد بیشتر می‌شود…" | `Math.floor` applied before clamping to the cap. A hidden `maxDiscount: 100000.6` / `250000.75` leaks a fraction. Astra always clamps, then floors. |

**Never failed:** o_code_separator, o_discount_base, o_min_order_base, o_free_shipping, o_expiry_tehran, o_sale_scope; also t_stale_counter, t_live_ledger, t_duplicate_route, t_zero_width.
- Each is a **closed, enumerated rule**: "خط تیره و زیرخط و فاصلهٔ وسط کد جزو خود کد هستند"; sale = "فهرست محصولات… یا سبد خرید…"; "به وقت تهران"; usedCount is "قدیمی است و ملاک نیست".
- Or it is **named in the complaint**: Persian digits, shipping included in the discount base.

---

## 3. a_f2_python-desktop-nfse-client (pt)

Rewards: Fable 0.43–0.93, Astra 0.14–0.35. These are the main campaigns; there are 4 extra Astra episodes in two other campaigns, and I left them out of the counts.

The user's own layout notes (`docs/layout_abrasf.md`) say cancellation and substitution "ficam **fora** do Nfse, pendurados no mesmo CompNfse". That works as a proxy decoy.

| check | owner | F/A | mechanism | wording | what the code did |
|---|---|---|---|---|---|
| o_status_substituta | t_cancelamento_solto_na_resposta / t_dono_do_cancelamento | **10/10** | (a)+join by identifier | "Cancelada quando o cancelamento daquela nota **veio junto na resposta**" | The `NfseCancelamento` sits loose in `ListaNfse`, naming its note by `Numero` (9412, 9423, 8931). Nobody linked it by number. All runs searched only inside the note's CompNfse. |
| o_status_cancelada_substituida | t_cancelamento_pendurado_fundo | 4/10 | (a) | same sentence | The cancellation sits inside `Nfse` / `InformacoesComplementares`. Direct-child lookup (`_child(comp,…)`, fixed `find_path`) misses it. The 6 Fable runs using descendant search passed. |
| o_erro_empacotado | FAILURES says t_campo_pelo_nome; **actually t_cancelamento_no_corpo_da_nota** | 4/10 | (a) | same sentence | Its only failing test is `test_status_do_cancelamento_escrito_no_corpo_da_nota`: 9462 got Normal instead of Cancelada. **Misattributed.** |
| o_xml_da_nota | FAILURES says encoding; **actually cancellation deep in the note** | 4/10 | (a) | same sentence | It fails at the status assertion for 8951, before any file comparison. **Misattributed.** |
| o_payload_texto | t_liquido_vazio / t_forma_do_que_chega | 7/5 | (a) | "o cnpj ou cpf desse tomador do jeito que veio" | Note 9102 sits inside a text-wrapped full envelope. Fable took the first, empty `<Cnpj/>` before `<Cpf>` and returned ''. Astra did not strip indented text (`'\n 9102\n'`). |
| o_notas_v1 / o_notas_v2 / o_documento_prestador | t_tomador_do_prestador / t_tomador_sem_identificacao | **0/10** | (a)/(e) | "a data que o provedor mandou"; "o tomador" | Astra used fixed XPaths. The 1.00-layout document came back ''. The tomador `CpfCnpj` arrives without its `IdentificacaoTomador` wrapper and came back ''. A date already given as `19/01/2026` hit `fromisoformat` and raised a raw ValueError. |
| o_valor_decimal | t_valor_do_servico | 0/3 | (a) | "valor líquido; quando não mandar o líquido, valor dos serviços" | Returned 900.35 (services) instead of 882.34 (net). |

**Never failed:** o_endpoint_provedor, o_erro_provedor, o_erro_transporte, g_pasta_acumulada, g_recusa_documento_invalido, t_substituida_x_substituicao.
- Each is spelled out: "espaço…e maiúscula/minúscula é o mesmo provedor…qualquer outra diferença é outro nome"; "se vier nota e mensagem juntas…quero as notas"; "timeout, ssl…500, 503…NFSeConnectionError"; "a mesma nota não pode virar um segundo arquivo".
- The docs explicitly warn "Cuidado com os dois nomes parecidos".

---

## 4. a_f2_vanilla-js-forum-ui (zh)

Reward 0.50 in all 20 runs: zero spread.

| checks | FAILURES owners | F/A | real cause |
|---|---|---|---|
| o_login_resolve, o_login_ambiguous, o_login_dial_00, o_login_identity_kept, o_login_written_identity | t_separator_class, t_abstain_branch, t_dial_prefix, t_session_record, t_digits_merge | 10/10 each | **One miss: (e)+(g).** "中间的空格、横杠、括号都不算数". All 20 pages keep the file's `replace(/[\s\-()]/g,'')`. Every failing test includes at least one full-width or typographic writing: `（139）0000－1111`, `139–0000–1111`, `+1 390－000－1111`. The ASCII-only login guardrails (g1/g2/g3/g7/g11) all pass. The `00`→`+` folding and the ambiguity message are implemented in the code I read, but those traps are **masked**, not shown to pass. |

**Never failed:** every summary and count check, including o_other_page_excerpt (weight 16, the second page) and t_escape_then_clip, t_utf16_units, t_stale_summary, t_count_memo, t_store_rename and t_fix_on_load. All are **named in the complaint with concrete symptoms**: "&lt;&lt;&lt;…半个 &l", "一个表情…2 chars", "两个论坛页面都一样算数", "各处原来截多长就还截多长".

---

## What separates traps that bit from those that didn't

**The four both-family biters share one shape:** the rule is a concept in the user's words, and the code, docs or data hold a narrower proxy for it.

| concept in the instruction | proxy in the repo | hidden input |
|---|---|---|
| "every time a request is sent" | build the schema at registration | names defined later in the module |
| "orders placed and not cancelled" | one ledger row per order | the desk wrote one order twice |
| "came along in the response" | child of CompNfse (per the docs) | cancellation loose, referenced by number |
| "dashes and brackets don't count" | ASCII `[\s\-()]` | Chinese-keyboard full-width punctuation |

Four properties held every time:
1. **Visible material is consistent with the proxy.** No counter-example appears in the repo, docs, seed data or visible tests.
2. **The symptom can be fixed without the general reading.** The trap is one step past the obvious fix, on the same subsystem.
3. **A second writer or source is mentioned in one clause** and never exemplified: the admin panel, the user's regression modules, each prefeitura, the user's keyboard.
4. **The settling sentence is general, not enumerated,** and sits inside the rules paragraph near its symptom.

**The traps that never bit were:**
- Closed enumerations or explicit procedures, implemented literally by all 40 runs.
- Behaviours named in the complaint.
- Common knowledge: Tehran time, Decimal, HTTP errors.
- Traps whose tests sat behind an upstream miss: these were masked, never measured.

**The families failed differently on the levers that bit only one of them:**
- **Astra:** exact predicates and fixed shapes. A `"committed"` whitelist, the original `rows.find` left in place, direct-child or fixed XPaths, `fromisoformat` on a date, no whitespace strip. Astra's results are near-deterministic.
- **Fable:** order of operations and state across a sequence. Flooring before the cap (7/10), the block growing during trimming (10/10), taking the first empty sibling (7/10).
- **Both** missed concept-versus-proxy and join-by-identifier.

---

## Rules for authoring traps that bite both families and stay fair (ranked)

1. **Make existing code a narrower proxy of a concept the instruction states in domain words.** Put hidden inputs exactly where proxy ≠ concept: rows vs orders, ASCII vs "dashes", containment vs "in the response", registration time vs "each request".
2. **Keep every visible artifact on the proxy.** Seed data, docs, fixtures, visible tests and the existing helper must never show a counter-example.
3. **Let the complaint's symptom be fixable without the general reading.** Place the trap one step past the obvious fix, in the same subsystem, so the settling sentence is plainly in scope.
4. **State the rule as a class or concept, never as a list.** A negative definition works well ("not cancelled"). Enumerated rules were solved 40/40.
5. **Name a second writer or source in one clause, and let only hidden data exercise it.** Models model the repo's own writer.
6. **Prefer identity by reference over structure.** For example, a block that names its owner by number, or one entity written twice. Both families default to containment and one-row-one-entity.
7. **Locale and input-method conventions are fair class members.** Use full-width and typographic separators, non-ASCII status words such as "آماده ارسال", or locale digits. The user's language makes them settled.
8. **For Astra, pair rule 1 with an unseen shape off the documented path.** Examples: a missing wrapper element, a value already formatted, an empty placeholder sibling, a status outside the visible set.
9. **For Fable, add an order-of-operations or mid-sequence invariant that only hidden values expose.** Examples: a fractional cap, a block outgrowing the cap mid-round. Put it on the same hidden item as an Astra lever. On its own it barely touches Astra (0–2/10).
10. **Isolate checks so one upstream miss can't cascade.** Give each check its own module or data, and judge downstream behaviour against the agent's own upstream output. Otherwise "8 decisions" is one, and reward spread collapses (forum: 0.50 in all 20 runs).
11. **Count biting decisions from the failing assertions, not from check names.** FAILURES.md misattributed nfse o_erro_empacotado and o_xml_da_nota, all five forum login checks, and ai-agent's o_args_*. t_conversion_blows_up was never reached.
12. **Don't spend objective weight on behaviours named in the complaint or spelled out as procedure.** Make them guardrails.
13. **Watch traps that need an obscure technique.** o_schema_types bit (Fable 8/10, Astra 10/10) partly because the CI rule removed the normal route. The reference needed a probe class with a `__module__` trick plus AST walking. Keep such a trap only if a standard-library route exists, and record it.
14. **Docs presented as the user's own notes may encode the proxy.** That is fair only if the instruction's general sentence clearly overrides them and the notes stay true for the canonical case, as in nfse.
15. **Plan two or three independent both-family biters per step.** One is enough for pass@1 = 0, but it gives zero spread and a single point of failure if a model generalises the class.

---

**Sources:** the four zips in `/Users/pratyushmohanty/Downloads/arena-envs-coding-v2/Arena-envs-coding-ship-15102026/ship-zips/`. I read only these members: `tests/checks.json`, `tests/suites/*`, `authoring/{traps,spec}.json`, `FAILURES.md`, `verification/failure-causes.json`, each episode's `verifier/arena-grade.json`, and the captured `artifacts/app/repo/*`. Nothing was modified.
