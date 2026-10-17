"""Trap research: the designer searches the web for failure modes of THIS trace's stack.

User, 2026-10-17: "give the ability to the authoring agent to search for relevant and
extremely extremely hard traps which are relevant to the source trace ... add as many
high quality, extremely hard traps as possible that would definitely defeat both the
models", with "a minimum goal of adding at least 6 extremely hard traps relevant to
source traces using web search and research + additional traps from the library", and
"diverse failure modes across tasks".

The lever library (`levers.py`) holds what measured or was published; it is the same
for every task, so every task built from it fails the models the same few ways. This
module adds the per-trace half: a `research_traps` tool that runs a separate Messages
call with Anthropic's server-side web search and fetch, asks for executable,
version-scoped semantics of the libraries, formats and protocols the fixture actually
uses, and records every call (question, sources, findings) in
`verification/trap-research.json`. A web claim is a hypothesis: the designer verifies
it by execution in the pinned toolchain before planting it, and a researched trap
still passes the lever admission test (default-wrong, unlabelled, determinate) and is
proved by its mutant like every other trap.

Nothing here blocks validation. A fresh design must plant `policy.MIN_RESEARCHED_TRAPS`
researched traps before creation counts as complete (`creation.inventory`), unless the
research tool is unavailable (provider failures), in which case the requirement is
waived and says so.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import policy
from ..artifacts import atomic_json

RECORD = "verification/trap-research.json"
SCHEMA = "arena-coding/trap-research/1"
RESEARCH_ID = re.compile(r"^r_\d{2,3}$")
URL = re.compile(r"^https?://[^\s]+$")
WEB_SEARCH = "web_search_20260209"
WEB_FETCH = "web_fetch_20260209"
QUESTION_MAX_CHARS = 2400
FINDINGS_MAX_CHARS = 9000
MAX_CONTINUATIONS = 4
MAX_PARALLEL = 4

#: Types worth steering an under-used batch toward: the semantic failure modes a
#: researched trap turns on, then the mechanisms that measured. Symptom-vs-cause,
#: chained defects, decoy locations, restraint and regression bait are DEAD as the
#: puzzle (levers.DEAD) and are never recommended for diversity's sake.
RECOMMENDED_TYPES = ("library_semantics", "protocol_semantics", "numeric_semantics", "text_semantics",
                     "temporal_semantics", "absence_semantics", "ordering_semantics", "resource_semantics",
                     "concurrency_semantics", "security_semantics", "error_semantics", "exception_interaction",
                     "representation_collision", "temporal_state", "distributed_invariant", "architectural_carry",
                     "semantic_decoy", "stale_authority")

TOOL = {
    "name": "research_traps",
    "description": (
        "WEB RESEARCH for extremely hard, trace-relevant traps. A research model with web search and fetch "
        "looks up documented, executable semantics of the stack THIS fixture uses (language and library "
        "versions it pins, file formats, protocols, the domain's rules) and evidence of where frontier coding "
        "agents go wrong, and returns candidate trap modes, each with an executable fact, a hidden case, the "
        "user sentence that settles it, validity risks and the source URLs. Ask SPECIFIC questions naming the "
        "stack and versions (e.g. 'pandas 1.5.3 read_csv: thousands separators, quoted newlines, dtype "
        "inference of zero-padded ids'), never the user's code, names, paths or data. Every call is recorded "
        f"with its sources as research_id r_NN; at most policy.TRAP_RESEARCH_MAX_CALLS calls per session. "
        "A finding is a HYPOTHESIS until sandbox_run reproduces it in the pinned toolchain."),
    "input_schema": {"type": "object", "properties": {
        "question": {"type": "string", "description": "one focused research question"},
        "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                      "description": "up to 4 focused questions, researched IN PARALLEL (each takes minutes); "
                                     "each gets its own research_id"},
        "stack": {"type": "string", "description": "language, framework and library versions as the fixture pins them"},
        "domain": {"type": "string", "description": "what the program does and the rules the user stated, generically"},
        "aim": {"type": "string", "enum": ["fable", "astra", "both"],
                "description": "which calibration family's habits the trap should defeat"}},
        "required": ["stack"]},
}

SYSTEM = """\
You are a research assistant for engineers who build HARD, FAIR coding benchmark tasks. \
A frontier coding agent (Claude-family in Claude Code, GPT-family in Codex; high effort, full \
shell, no network) will be given a real repository and a user's instruction, and graded \
afterwards by hidden executed tests. The task author needs TRAP MODES for the stack below: \
places where the natural, idiomatic implementation a strong engineer writes from memory is \
WRONG on a valid input that a plain sentence of the user's instruction settles.

Research with web search and fetch. Prefer primary sources: official documentation of the \
exact versions given, language references, changelogs and migration guides, CPython / Node / \
library issue trackers, RFCs and format specifications, and published analyses of coding-agent \
failures (SWE-bench family, Terminal-Bench, agent trajectory studies). Search with generic \
technical terms only: never put people, organisations, file paths, credentials or user data \
into a query.

What makes a trap mode good here:
  - DETERMINATE: one plain user sentence makes the correct behaviour unambiguous to a careful \
expert; no "it depends" and no version-dependent claim unless the version is the pinned one.
  - DEFAULT-WRONG: the idiom (exact match after one normalisation, first match, round()/toFixed, \
naive datetime, str.lower, split on a literal, string sort, last-key-wins dict, a regex that \
generalises a named list, recompute-from-scratch, a library default) gives a different answer.
  - EXECUTABLE: a short program in the pinned toolchain shows the difference; give that program \
and its exact output.
  - REAL: it arises in this stack and domain in ordinary use, not as a contrivance.
  - NOT DEAD: "noticing" traps (a comment naming the case, an obviously broken visible example, \
symptom vs cause, a decoy file, "don't touch X") are solved by both agent families; skip them.
Claude-family agents tend to over-generalise a closed rule, miss long-tail/fallback branches, \
fix one of two code paths and miss two rules meeting on one input or state across reruns. \
GPT-family agents tend to code one exact predicate (ASCII-only class, line substring instead of \
a field, first match, the reported boundary instead of its family), stop at the first green \
test, and miss environment and error-path conventions.

Answer with ONE JSON array inside a ```json fenced block, 4 to 8 objects, each:
  {"mode": snake_case name, "family": scope|interaction|representation|idiom|state|invariant|\
protocol|numeric|text|temporal|absence|resource|concurrency|security|error|library,
   "aims_at": "fable"|"astra"|"both",
   "executable_fact": exact program + exact output in the pinned version,
   "idiomatic_wrong": the code a strong engineer writes and why it is wrong,
   "hidden_case": concrete input -> correct output vs idiomatic output,
   "settling_sentence": the plain sentence a user would write that settles it,
   "fits_when": the code or data in a repository that makes this trap natural,
   "validity_risks": what could make it ambiguous, version-dependent or unfair,
   "sources": [{"url": "...", "title": "...", "finding": "one line"}]}
The author already has a library of common modes (float rounding ties, DST arithmetic, splitlines, \
NOT IN with NULL, RFC 9110 conditionals and ranges, CSV/BOM, SemVer, gitignore, XDG): go deeper than \
it -- the pinned versions' own behaviour, the formats this domain's real producers emit, the \
domain's conventions. Flag a mode as hazardous when it depends on locale, unpinned versions, wall \
clock timing or an unstated tie rule.
Use only URLs you actually saw. After the block, at most five lines of caveats."""


def record_path(bundle: Path) -> Path:
    return Path(bundle) / RECORD


def load(bundle: Path) -> dict | None:
    try:
        doc = json.loads(record_path(bundle).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def begin(bundle: Path, required: int) -> dict:
    """Open (or reopen) the record for a session; `required` is this session's floor."""
    doc = load(bundle) or {"schema": SCHEMA, "calls": []}
    doc["required"] = int(required)
    doc.setdefault("calls", [])
    doc.setdefault("unavailable", False)
    doc["opened_at"] = time.time()
    atomic_json(record_path(bundle), doc)
    return doc


def requirement(bundle: Path) -> tuple[int, set[str] | None]:
    """(researched traps creation still needs, research ids on record or None when unrecorded)."""
    doc = load(bundle)
    if doc is None:
        return 0, None
    ids = {c.get("id") for c in doc.get("calls") or [] if c.get("ok")}
    if doc.get("unavailable") and not ids:
        return 0, ids
    return int(doc.get("required") or 0), ids


def _sources(blocks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for block in blocks:
        rows = []
        if block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), list):
            rows = [{"url": r.get("url"), "title": r.get("title")} for r in block["content"] if isinstance(r, dict)]
        elif block.get("type") == "web_fetch_tool_result" and isinstance(block.get("content"), dict):
            rows = [{"url": block["content"].get("url"), "title": "fetched"}]
        for citation in block.get("citations") or []:
            if isinstance(citation, dict):
                rows.append({"url": citation.get("url"), "title": citation.get("title")})
        for row in rows:
            url = row.get("url")
            if isinstance(url, str) and URL.match(url) and url not in seen:
                seen.add(url)
                out.append({"url": url, "title": str(row.get("title") or "")[:160]})
    return out


def _parse(text: str) -> list[dict] | None:
    match = re.search(r"```json\s*(\[.*?\])\s*```", text, re.S) or re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
    if not match:
        return None
    try:
        rows = json.loads(match.group(1))
    except ValueError:
        return None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


def _call_with_search(question: str, call) -> tuple[str, list[dict], dict]:
    """One research conversation: web search/fetch server tools, pause_turn continued."""
    from .transport import MODEL
    messages = [{"role": "user", "content": question}]
    tools = [{"type": WEB_SEARCH, "name": "web_search", "max_uses": policy.TRAP_RESEARCH_MAX_SEARCHES},
             {"type": WEB_FETCH, "name": "web_fetch", "max_uses": policy.TRAP_RESEARCH_MAX_FETCHES}]
    blocks: list[dict] = []
    usage = collections.Counter()
    for _ in range(MAX_CONTINUATIONS + 1):
        response = call({"model": MODEL, "max_tokens": 32000, "system": SYSTEM, "tools": tools,
                         "output_config": {"effort": "high"}, "messages": messages})
        content = response.get("content") or []
        blocks.extend(content)
        for key, value in ((response.get("usage") or {}).get("server_tool_use") or {}).items():
            if isinstance(value, int):
                usage[key] += value
        if response.get("stop_reason") != "pause_turn":
            if response.get("stop_reason") == "refusal":
                raise RuntimeError("the research model declined this question; rephrase it in generic technical terms")
            break
        messages = messages + [{"role": "assistant", "content": content}]
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text, _sources(blocks), dict(usage)


def make_tool(bundle: Path, *, call=None, privacy=None):
    """The `research_traps` handler bound to one bundle's record.

    One call may carry up to MAX_PARALLEL questions; they are researched
    concurrently (a single researched question took 9 minutes live, 2026-10-17),
    and each is recorded as its own research id.
    """
    if call is None:
        from .agent_designer import _designer_call as call  # noqa: PLC0415  retried like every designer call
    if privacy is None:
        from ..privacy import scan_text as privacy  # noqa: PLC0415
    lock = threading.Lock()

    def _record(mutate):
        with lock:
            doc = load(bundle) or begin(bundle, 0)
            doc.setdefault("calls", [])
            result = mutate(doc)
            atomic_json(record_path(bundle), doc)
            return result

    def _reserve(doc):
        calls = doc["calls"]
        if len(calls) >= policy.TRAP_RESEARCH_MAX_CALLS:
            return None
        rid = f"r_{len(calls) + 1:02d}"
        calls.append({"id": rid, "at": time.time(), "ok": False, "pending": True})
        return rid

    def _one(question: str, stack: str, domain: str, aim: str) -> dict:
        prompt = (f"STACK (as the repository pins it): {stack.strip() or 'unspecified'}\n"
                  f"DOMAIN AND THE USER'S RULES (generic): {domain.strip() or 'unspecified'}\n"
                  f"AIM: traps that defeat {aim} agents\nQUESTION: {question.strip()}")[:QUESTION_MAX_CHARS]
        if findings := privacy("research-question", prompt):
            return {"error": "the question carries private data and was not sent: "
                             f"{[f.get('kind') for f in findings][:5]}; ask in generic technical terms"}
        rid = _record(_reserve)
        if rid is None:
            return {"error": f"research budget spent ({policy.TRAP_RESEARCH_MAX_CALLS} calls); plant from the "
                             "findings already returned (they are in your conversation and in "
                             f"{RECORD})"}
        entry = {"id": rid, "at": time.time(), "question": question[:800], "stack": stack[:400],
                 "domain": domain[:600], "aim": aim, "ok": False}
        try:
            text, sources, usage = _call_with_search(prompt, call)
        except Exception as exc:  # noqa: BLE001  research is advisory; record and hand back
            entry["error"] = f"{type(exc).__name__}: {exc}"[:400]

            def fail(doc):
                calls = doc["calls"]
                calls[:] = [entry if c.get("id") == rid else c for c in calls]
                failures = 0
                for row in reversed([c for c in calls if not c.get("pending")]):
                    if row.get("ok"):
                        break
                    failures += 1
                if failures >= policy.TRAP_RESEARCH_UNAVAILABLE_AFTER and not any(c.get("ok") for c in calls):
                    doc["unavailable"] = True
                return bool(doc.get("unavailable"))
            unavailable = _record(fail)
            return {"error": entry["error"], "research_id": rid, "unavailable": unavailable,
                    "note": "research is unavailable for this session; the researched-trap floor is waived"
                            if unavailable else "retry with a narrower question"}
        if privacy("research-findings", text):
            from ..privacy import redact_excerpt  # noqa: PLC0415
            text = redact_excerpt(text)
        parsed = _parse(text)
        entry.update(ok=True, sources=sources[:40], usage=usage, findings=parsed if parsed is not None else None,
                     text=text[:FINDINGS_MAX_CHARS])
        _record(lambda doc: doc["calls"].__setitem__(
            next(i for i, c in enumerate(doc["calls"]) if c.get("id") == rid), entry))
        return {"research_id": rid, "question": question[:200],
                "findings": parsed if parsed is not None else text[:FINDINGS_MAX_CHARS],
                "sources": [s["url"] for s in sources[:25]]}

    def research_traps(question: str = "", stack: str = "", domain: str = "", aim: str = "both",
                       questions: list | None = None, **_):
        asked = [q for q in ([question] if question else []) + list(questions or [])
                 if isinstance(q, str) and q.strip()][:MAX_PARALLEL]
        if not asked:
            return {"error": "give `question` or `questions` (up to %d, researched in parallel)" % MAX_PARALLEL}
        with ThreadPoolExecutor(max_workers=len(asked)) as pool:
            results = list(pool.map(lambda q: _one(q, stack, domain, aim), asked))
        ok = [r["research_id"] for r in results if "error" not in r]
        out = {"results": results,
               "remaining_calls": policy.TRAP_RESEARCH_MAX_CALLS - len((load(bundle) or {}).get("calls") or []),
               "next": "A finding is a hypothesis: reproduce its executable_fact with sandbox_run in the pinned "
                       "toolchain, then plant the ones that pass the admission test as traps carrying "
                       "research={research_id, sources: [urls that call returned], finding, executed} -- "
                       "interlocked with a library lever on ONE hidden item, settled by an instruction sentence, "
                       "proved by an m_baseline_ mutant writing the idiom and a partial mutant per lever."}
        if len(results) == 1:
            out = {**results[0], **{k: v for k, v in out.items() if k != "results"}}
        if not ok and all(r.get("unavailable") for r in results if "research_id" in r) and \
                any("research_id" in r for r in results):
            out["unavailable"] = True
        return out

    return research_traps


# ---------------------------------------------------------------------------
# Trap-level checks (advisory) and batch diversity
# ---------------------------------------------------------------------------

def researched(traps: list, record_ids: set[str] | None = None) -> list[str]:
    """Trap ids whose `research` is well formed (and on record, when a record exists)."""
    out = []
    for trap in traps or []:
        if not isinstance(trap, dict) or not research_shape_ok(trap.get("research")):
            continue
        if record_ids is not None and trap["research"].get("research_id") not in record_ids:
            continue
        out.append(trap.get("id"))
    return out


def research_shape_ok(research) -> bool:
    return (isinstance(research, dict) and isinstance(research.get("research_id"), str)
            and bool(RESEARCH_ID.match(research["research_id"]))
            and isinstance(research.get("sources"), list) and bool(research["sources"])
            and all(isinstance(u, str) and URL.match(u) for u in research["sources"])
            and isinstance(research.get("finding"), str) and len(research["finding"].strip()) >= 40
            and isinstance(research.get("executed"), str) and len(research["executed"].strip()) >= 20)


def advisories(spec: dict, bundle: Path) -> list[str]:
    """What check_draft tells the designer about researched traps and type diversity."""
    out: list[str] = []
    traps = [t for t in spec.get("traps") or [] if isinstance(t, dict)]
    doc = load(bundle)
    on_record = {c.get("id"): c for c in (doc or {}).get("calls") or [] if c.get("ok")}
    need, _ = requirement(bundle)
    for trap in traps:
        research = trap.get("research")
        if research is None:
            continue
        where = f"ADVISORY trap {trap.get('id')!r} research"
        if not research_shape_ok(research):
            out.append(f"{where}: needs research_id 'r_NN', sources [urls], finding (>= 40 chars) and executed "
                       "(what sandbox_run showed, >= 20 chars)")
            continue
        call = on_record.get(research["research_id"])
        if doc is not None and call is None:
            out.append(f"{where}: {research['research_id']} is not a successful research_traps call on record")
        elif call is not None:
            known = {s.get("url") for s in call.get("sources") or []}
            stray = [u for u in research["sources"] if u not in known]
            if stray:
                out.append(f"{where}: sources not returned by {research['research_id']}: {stray[:3]}")
    if need:
        have = researched(traps, set(on_record) if doc is not None else None)
        if len(have) < need:
            out.append(f"ADVISORY trap research: {len(have)} researched trap(s), the floor for a fresh design is "
                       f"{need} (creation stays unfinished until met). Use research_traps on THIS fixture's "
                       "stack, verify by sandbox_run, plant interlocked traps carrying `research`.")
    types = collections.Counter(t.get("type") for t in traps if t.get("type"))
    if traps:
        top, count = types.most_common(1)[0]
        if len(traps) >= 4 and count / len(traps) > policy.TRAP_TYPE_MAX_SHARE:
            out.append(f"ADVISORY trap diversity: {count}/{len(traps)} traps are {top!r}; same-type traps fail "
                       "together. Re-type each trap by its actual failure mode (the taxonomy has "
                       "library/protocol/numeric/text/temporal/absence/resource/concurrency/security/error "
                       "semantics) or vary the mechanism.")
    return out


def batch_diversity(bundle: Path, ledger: Path | None = None) -> dict:
    """Trap-type shares across the batch: the shipped ledger plus sibling bundles of this tree."""
    counts: collections.Counter = collections.Counter()
    tasks = 0
    if ledger is not None:
        try:
            doc = json.loads(Path(ledger).read_text())
            n = int(doc.get("tasks") or 0)
            for kind, share in (doc.get("decisive_type_share") or {}).items():
                counts[kind] += round(float(share) * n)
            tasks += n
        except (OSError, ValueError, TypeError):
            pass
    tree = Path(bundle).resolve().parent.parent
    for spec_path in sorted(tree.glob("*/*/authoring/spec.json"))[:400]:
        if spec_path.parent.parent.resolve() == Path(bundle).resolve():
            continue
        try:
            spec = json.loads(spec_path.read_text())
        except (OSError, ValueError):
            continue
        kinds = [t.get("type") for t in spec.get("traps") or [] if isinstance(t, dict) and t.get("type")]
        if kinds:
            tasks += 1
            for kind, n in collections.Counter(kinds).items():
                counts[kind] += n / len(kinds)
    total = sum(counts.values()) or 1.0
    shares = {k: round(v / total, 3) for k, v in counts.most_common()}
    return {"tasks": tasks, "shares": shares,
            "over_used": [k for k, s in shares.items() if s >= policy.DIVERSITY_OVERUSED_SHARE]}


def opening_text(bundle: Path, ledger: Path | None, required: int) -> str:
    from . import levers
    from . import traps as traps_mod
    lines = [
        "TRAP RESEARCH AND FAILURE-MODE DIVERSITY (user, 2026-10-17). The lever library is shared by every task, "
        "so on its own it makes every task fail the models the same few ways. THIS task must also carry traps "
        "researched for THIS trace: the libraries and versions its fixture pins, the formats and protocols its "
        "data uses, the domain rules its user stated.",
        f"  - MINIMUM: {required} researched traps (more is better when each is genuine). For each: research_traps "
        "with a specific question naming the stack -> reproduce the finding's executable fact with sandbox_run in "
        "the agent image -> plant it as a trap whose hidden case INTERLOCKS it with at least one library lever "
        "on ONE hidden item, aimed at the other family, settled by one instruction sentence (rule_quote), proved "
        "by an m_baseline_ mutant (the idiom) and an m_partial_ mutant per lever -> record "
        "research {research_id, sources, finding, executed} on the trap. A trace-derived trap may carry research "
        "too (it keeps its trace provenance); prefer that where the user's own correction is the seed.",
        "  - ADD AS MANY extremely hard traps as the code genuinely supports, researched and library alike, "
        "provided every one passes the admission test; a trap the instruction does not settle is a defect.",
        "  - Research questions that pay off: the pinned versions' documented semantics that differ from what "
        "an engineer writes from memory; format/protocol corner cases the data's real producers emit; the "
        "domain's own conventions (rounding stage, time zone, identity, ordering, absence); known issues and "
        "migration notes of the exact versions; published agent failure analyses for this kind of code. The "
        "library already holds the common modes (its research levers carry executed semantics and a trap_type); "
        "research what is SPECIFIC to this stack and domain, and use a library lever as the other half of each "
        "interlock.",
        "  - VALIDITY HAZARDS -- these look hard and are ambiguous unless a sentence of the instruction settles them "
        "(W11): " + "; ".join(levers.RESEARCH_HAZARDS) + ". Never plant one unsettled.",
    ]
    if required == 0:
        lines[1] = ("  - This is a revision session: research_traps is available, and any trap you ADD should be "
                    "researched and interlocked the same way; the researched-trap floor applies to fresh designs.")
    try:
        diversity = batch_diversity(bundle, ledger)
    except Exception:  # noqa: BLE001  advisory context only
        diversity = {"tasks": 0, "shares": {}, "over_used": []}
    if diversity["tasks"]:
        shares = ", ".join(f"{k} {v:.0%}" for k, v in list(diversity["shares"].items())[:8])
        unused = [t for t in RECOMMENDED_TYPES if t in traps_mod.TAXONOMY and t not in diversity["shares"]]
        # Rotated per task (stable): concurrent designs reading one list in one order would all
        # start from its first entry and make a new monoculture.
        if unused:
            turn = int(hashlib.sha256(Path(bundle).name.encode()).hexdigest(), 16) % len(unused)
            unused = unused[turn:] + unused[:turn]
        lines.append(f"  - BATCH DIVERSITY ({diversity['tasks']} tasks): trap-type shares so far {shares}. "
                     f"Over-used: {', '.join(diversity['over_used']) or 'none'}. Prefer failure modes the batch has "
                     f"not yet used where this code supports them: {', '.join(unused[:14]) or 'any'}. Within this "
                     f"task use at least four distinct types and no type above {policy.TRAP_TYPE_MAX_SHARE:.0%} "
                     "of the traps; type each trap by its ACTUAL failure mode, not the generic spec_misread.")
    return "\n".join(lines)
