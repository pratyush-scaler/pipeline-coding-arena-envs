"""Clause coverage map: every promise the instruction makes, and the check that enforces it.

User, 2026-10-21, after the external audit of Batch 3 (class 4, "ungraded promises"): instruction
sentences promised things no hidden check enforced -- "python -m pytest tests has to stay green",
"the warnings that are already there stay unchanged", "вид слайдов не меняй — шрифты, цвета и
размеры как есть", "页面的结构、id 和 class ... 还有样式都别动" -- and designers even listed some of
them as untested surfaces. The detectors that should have caught them
(`shipcheck.untested_unjustified`, `shipcheck.preservation_unguarded`) were English regexes over
instructions written in Russian, Chinese, Persian and Japanese, so they could never match.

The map replaces them as the blocking check. Three layers, each recorded:

  (a) extract   both calibration families quote, verbatim and in the instruction's own language,
                every clause that promises something about the result (behaviour, formats, exact
                values, "keep / leave X as it is", "must stay green", "unchanged"). A quote that is
                not in the instruction is rejected; the clause set is the union of both families'
                verbatim quotes (and the English "leave <fixture file>" sentences the old
                pre-filter matches, so a family that misses one cannot drop it).
  (b) quoted    deterministic: each clause must be quoted by the rule_quote / sentence of at least
                one check (whitespace, punctuation and case normalised, any script), or by an
                untested surface (verification/probes.json or spec.quality) that quotes it AND
                carries a determinacy reason (shipcheck._DETERMINACY_REASON). Otherwise
                `coverage.clause_unenforced`.
  (c) whole     for a clause some check quotes, both families read the checks and the hidden
                suites and judge whether those tests can detect a plausible violation of the WHOLE
                clause (a "keep fonts, colours and sizes" guardrail that checks one colour cannot).
                "NO" from any family is `coverage.clause_in_name_only`, carrying its concrete
                counter-example.

A family that errs or answers nothing decides nothing: its clause stays unadjudicated and blocks
(`coverage.clause_map`) until it is asked again. The receipt (verification/clause-map.json) is
bound to the grading sha -- instructions, checks, their rule quotes and the hidden suites -- so a
stale map is rebuilt; an extraction is reused while its instruction is byte-identical, so a repair
round that changes only tests meets the same clauses again.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import policy

SCHEMA = "arena-coding/clause-map/1"
RECEIPT = "verification/clause-map.json"

#: Suite files a judge reads as text; anything else is named with its size.
SUITE_TEXT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".rs", ".php", ".sh", ".json", ".toml",
                       ".yaml", ".yml", ".txt", ".md", ".html", ".css", ".csv")

EXTRACT_PROMPT = """\
A user wrote the instruction below to a developer who will change a code repository. List every place where \
the instruction PROMISES something about the finished result: something the work must do, produce, show, \
accept, refuse, keep, or leave as it is. Include:
- behaviour, outputs, formats, exact values, names, orders and counts the user states;
- what must stay the same: "keep X", "leave X as it is", "don't touch X", "unchanged", "must stay green", \
"as they are", "the way it is now", "same as before";
- commands, files, interfaces, looks or wording the user says not to change.
Leave out background, complaints about the current state that promise nothing, and advice on how to work.

Quote each promise VERBATIM from the instruction, in the instruction's own language and script, exactly as \
written: do not translate, paraphrase, fix typos or join separate passages. Quote the shortest span that \
states one whole promise; a sentence that makes several promises may be quoted once per promise.

Return ONLY one JSON object in a ```json fenced block:
{{"clauses": [{{"quote": "<verbatim span of the instruction>", "promise": "<what it promises, in English, one line>"}}]}}

INSTRUCTION
-----------
{instruction}
"""

JUDGE_PROMPT = """\
A user gave a developer the instruction below. Hidden tests grade the developer's work; each graded check \
quotes the instruction sentences it enforces. For each CLAUSE listed at the end, decide whether the checks \
that quote it can DETECT a plausible violation of the WHOLE clause -- every promise in it, not one part of it.

Think of the realistic ways a developer could break the clause while doing the task in good faith: an old \
behaviour kept, a colour, font or size changed, a message reworded, a class renamed, a file edited, a test \
suite left red, one of several listed cases mishandled. If at least one such violation would pass every one \
of the listed checks' tests, answer NO and give that violation as a concrete counter-example (what the code \
does and why no listed test fails). Answer YES only when every plausible violation of the clause fails at \
least one listed check.

INSTRUCTION
-----------
{instruction}

CHECKS (id | kind | runner | the instruction sentences it quotes | what it says it enforces)
------
{checks}

HIDDEN TEST SUITES
------------------
{suites}

CLAUSES (id: the clause -- the checks that quote it)
-------
{clauses}

Return ONLY one JSON object in a ```json fenced block, one entry per clause id:
{{"clauses": [{{"id": "<clause id>", "detects": "<YES|NO>", "counter_example": "<for NO: a concrete \
violation the listed checks' tests would not catch, and why>"}}]}}
"""


# ---------------------------------------------------------------------------
# Normalisation and the bundle's published surface
# ---------------------------------------------------------------------------

def _keep(ch: str) -> bool:
    return not (ch.isspace() or unicodedata.category(ch)[0] in "PSZC")


def _norm_map(text: str) -> tuple[str, list[int]]:
    """The text with case, width, whitespace and punctuation normalised away (any script), and for every
    kept character the index of the original character it came from."""
    out, index = [], []
    for i, ch in enumerate(str(text)):
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if _keep(c):
                out.append(c)
                index.append(i)
    return "".join(out), index


def norm(text: str) -> str:
    return _norm_map(text)[0]


def locate(quote: str, instruction: str) -> str | None:
    """The instruction's own span a quote reproduces (normalised), or None when it is not verbatim."""
    target = norm(quote)
    if not target:
        return None
    flat, index = _norm_map(instruction)
    at = flat.find(target)
    if at < 0:
        return None
    start, end = index[at], index[at + len(target) - 1] + 1
    # The punctuation the quote itself opens or closes with, when the instruction has it there too.
    quote = str(quote).strip()
    i, j = 0, len(quote)
    while i < j and not _keep(quote[i]) and not quote[i].isspace():
        i += 1
    while j > i and not _keep(quote[j - 1]) and not quote[j - 1].isspace():
        j -= 1
    lead, tail = quote[:i], quote[j:]
    if lead and instruction[max(0, start - len(lead)):start] == lead:
        start -= len(lead)
    if tail and instruction[end:end + len(tail)] == tail:
        end += len(tail)
    return instruction[start:end]


def _json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def instructions(bundle: Path) -> dict[str, str]:
    return {p.parent.name: p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(Path(bundle).glob("steps/*/instruction.md"))}


def checks(bundle: Path) -> list[dict]:
    return [c for c in (_json(Path(bundle) / "tests/checks.json", {}) or {}).get("checks") or []
            if isinstance(c, dict) and c.get("id")]


def _quotes(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [q for q in value or [] if isinstance(q, str) and q.strip()]


def rule_quotes(bundle: Path) -> dict[str, list[str]]:
    """check id -> the instruction sentences it says it enforces: spec.json's rule_quote (the source the
    package is generated from), else tests/basis.toml's, plus any `rule_quote` / `sentence` a shipped
    checks.json carries itself."""
    import tomllib
    bundle = Path(bundle)
    out: dict[str, list[str]] = {}
    spec = _json(bundle / "authoring/spec.json", {}) or {}
    for c in spec.get("checks") or []:
        if isinstance(c, dict) and c.get("id"):
            out.setdefault(c["id"], []).extend(_quotes(c.get("rule_quote")))
    if not out and (bundle / "tests/basis.toml").is_file():
        try:
            basis = tomllib.loads((bundle / "tests/basis.toml").read_text(encoding="utf-8"))
        except ValueError:
            basis = {}
        for cid, row in basis.items():
            if isinstance(row, dict):
                out.setdefault(cid, []).extend(_quotes(row.get("rule_quote")))
    for c in checks(bundle):
        own = out.setdefault(c["id"], [])
        for key in ("rule_quote", "sentence"):
            for q in _quotes(c.get(key)):
                if q not in own:
                    own.append(q)
    return out


def untested_surfaces(bundle: Path) -> list[str]:
    bundle = Path(bundle)
    probes = _json(bundle / "verification/probes.json", {}) or {}
    spec = _json(bundle / "authoring/spec.json", {}) or {}
    rows = list(probes.get("untested_surfaces") or []) + list((spec.get("quality") or {}).get("untested_surfaces") or [])
    seen, out = set(), []
    for row in rows:
        text = row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _suite_files(bundle: Path) -> list[Path]:
    suites = Path(bundle) / "tests/suites"
    return sorted(p for p in suites.rglob("*") if p.is_file()) if suites.is_dir() else []


def grading_sha(bundle: Path) -> str:
    """What the map judged: the instructions, the checks with the sentences they quote, the hidden suites.
    A change to any of them makes the map stale."""
    bundle = Path(bundle)
    suites = bundle / "tests/suites"
    surface = {"instructions": instructions(bundle), "checks": checks(bundle), "quotes": rule_quotes(bundle),
               "suites": {p.relative_to(suites).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in _suite_files(bundle)}}
    return hashlib.sha256(json.dumps(surface, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Two-family judgement (the machinery of hack_triage.tier2)
# ---------------------------------------------------------------------------

def _ask(model: str, prompt: str, effort: str = "medium", max_tokens: int = 64000) -> str:
    from .hack_triage import _ask as ask
    return ask(model, prompt, effort=effort, max_tokens=max_tokens)


def judges() -> tuple[str, ...]:
    from .hack_triage import JUDGES
    return tuple(JUDGES)


def _judge_one(model: str, prompt: str, parse, ask) -> dict:
    """One family's parsed answer, asked at medium then low effort; a family whose model refuses answers
    through its next model (hack_triage.FALLBACK). Nothing usable is {"error": ...}, which decides nothing."""
    from .hack_triage import FALLBACK, JUDGE_MAX_TOKENS, JudgeRefused
    problems, judge = [], model
    for effort in ("medium", "low"):
        try:
            reply = ask(judge, prompt, effort=effort, max_tokens=JUDGE_MAX_TOKENS) or ""
        except JudgeRefused as exc:
            problems.append(f"{effort}: {exc}")
            if judge != model or model not in FALLBACK:
                break
            judge = FALLBACK[model]
            try:
                reply = ask(judge, prompt, effort=effort, max_tokens=JUDGE_MAX_TOKENS) or ""
            except Exception as exc2:                  # noqa: BLE001
                problems.append(f"{effort}: {judge}: {type(exc2).__name__}: {str(exc2)[:160]}")
                continue
        except Exception as exc:                       # noqa: BLE001 -- a judge that errs decides nothing
            problems.append(f"{effort}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        parsed = parse(reply)
        if parsed is not None:
            out = {"answer": parsed, "reply": reply.strip()[-1200:]}
            if judge != model:
                out.update(judge=judge, because=f"{model} refused")
            return out
        problems.append(f"{effort}: " + ("empty reply" if not reply.strip() else f"unparsable: {reply.strip()[-80:]!r}"))
    return {"error": "; ".join(problems)[:400] or "no answer"}


def ask_families(prompt: str, parse, *, models: tuple[str, ...] | None = None, ask=None) -> dict[str, dict]:
    """Every family's answer to one prompt: {model: {"answer", "reply"} | {"error"}}."""
    ask = ask or _ask
    models = judges() if models is None else tuple(models)
    if not models:
        return {}
    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        return dict(zip(models, pool.map(lambda m: _judge_one(m, prompt, parse, ask), models)))


def _json_rows(reply: str, key: str) -> list[dict] | None:
    blocks = re.findall(r"```json\s*(.*?)```", reply or "", re.S) or [reply or ""]
    for block in reversed(blocks):
        try:
            doc = json.loads(block)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get(key), list):
            return [r for r in doc[key] if isinstance(r, dict)]
    return None


# ---------------------------------------------------------------------------
# (a) extraction
# ---------------------------------------------------------------------------

def _extraction_parser(instruction: str):
    def parse(reply: str) -> dict | None:
        rows = _json_rows(reply, "clauses")
        if rows is None:
            return None
        kept, rejected = [], []
        for row in rows:
            quote = str(row.get("quote") or "")
            span = locate(quote, instruction)
            if span is None:
                rejected.append(quote[:300])
            else:
                kept.append({"quote": span, "promise": str(row.get("promise") or "")[:300]})
        if rows and not kept:
            return None                    # nothing verbatim: asked again, never an empty map
        return {"clauses": kept, "rejected": rejected}
    return parse


def _seeds(bundle: Path) -> dict[str, list[str]]:
    """The old English pre-filter's 'leave <fixture file>' sentences, always in the map."""
    from .shipcheck import _preservation_sentences
    out: dict[str, list[str]] = {}
    for step, sentence, _ in _preservation_sentences(bundle):
        if sentence not in out.setdefault(step, []):
            out[step].append(sentence)
    return out


# ---------------------------------------------------------------------------
# (b) quoted, (c) whole
# ---------------------------------------------------------------------------

def _quoted_by(clause: str, step: str, all_checks: list[dict], quotes: dict[str, list[str]]) -> list[str]:
    cn = norm(clause)
    out = []
    for c in all_checks:
        if c.get("step") not in (None, "", step):
            continue
        for q in quotes.get(c["id"]) or []:
            qn = norm(q)
            if qn and cn and (cn in qn or qn in cn):
                out.append(c["id"])
                break
    return out


def _untested_by(clause: str, surfaces: list[str]) -> list[int]:
    from .shipcheck import _DETERMINACY_REASON
    cn = norm(clause)
    return [i for i, text in enumerate(surfaces) if cn and cn in norm(text) and _DETERMINACY_REASON.search(text)]


def _runner_text(check: dict) -> str:
    runner = check.get("runner") or {}
    kind = runner.get("type")
    detail = {k: runner[k] for k in ("select", "shell", "paths", "modules", "allow", "max_new_files", "root",
                                     "files", "cases") if k in runner}
    return f"{kind} {json.dumps(detail, ensure_ascii=False)}"[:1500]


def _referenced(check: dict) -> set[str]:
    runner = check.get("runner") or {}
    names = {str(s).split("::", 1)[0] for s in runner.get("select") or []}
    names |= set(re.findall(r"suites/([\w./-]+)", str(runner.get("shell") or "")))
    return names


def _suites_text(bundle: Path, first: set[str]) -> str:
    """The step's hidden suites, the files the quoting checks run first, within policy.CLAUSE_MAP_SUITE_CHARS."""
    suites = Path(bundle) / "tests/suites"
    files = _suite_files(bundle)

    def rank(p: Path):
        rel = p.relative_to(suites).as_posix()
        return (0 if rel in first or p.name in first else 1 if not rel.startswith("data/") else 2, rel)

    parts, budget = [], policy.CLAUSE_MAP_SUITE_CHARS
    for p in sorted(files, key=rank):
        rel = p.relative_to(suites).as_posix()
        if p.suffix.lower() not in SUITE_TEXT_SUFFIXES:
            parts.append(f"=== {rel} ({p.stat().st_size} bytes, not text)")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if budget <= 0:
            parts.append(f"=== {rel} (omitted: budget spent)")
            continue
        if len(text) > budget:
            text = text[:budget] + f"\n... [{len(text) - budget} more characters]"
        budget -= len(text)
        parts.append(f"=== {rel}\n{text}")
    return "\n\n".join(parts) or "(none)"


def _judgement_parser(ids: set[str]):
    def parse(reply: str) -> dict | None:
        rows = _json_rows(reply, "clauses")
        got = {}
        for row in rows or []:
            verdict = str(row.get("detects") or "").strip().upper()
            if str(row.get("id")) in ids and verdict in ("YES", "NO"):
                got[str(row["id"])] = {"detects": verdict,
                                       "counter_example": str(row.get("counter_example") or "").strip()[:1500]}
        return got or None
    return parse


def _judge_step(bundle: Path, instruction: str, todo: dict[str, list[dict]], all_checks: list[dict],
                quotes: dict[str, list[str]], ask) -> None:
    """Ask each family about the clauses it has not judged yet: one batched prompt per step, the families
    that miss the same clauses asked together."""
    by_id = {c["id"]: c for c in all_checks}
    groups: dict[tuple[str, ...], list[str]] = {}
    for model, clauses in todo.items():
        if clauses:
            groups.setdefault(tuple(c["id"] for c in clauses), []).append(model)
    for clause_ids, models in groups.items():
        clauses = todo[models[0]]
        ids = {f"c{i + 1}": clause for i, clause in enumerate(clauses)}
        used = sorted({cid for clause in clauses for cid in clause["quoted_by"] if cid in by_id})
        rows = [f"{cid} | {by_id[cid].get('kind')} | {_runner_text(by_id[cid])} | "
                + " / ".join(quotes.get(cid) or [])[:1200] + f" | {str(by_id[cid].get('basis') or '')[:600]}"
                for cid in used]
        listed = "\n".join(f"{k}: {json.dumps(c['quote'], ensure_ascii=False)} -- {', '.join(c['quoted_by'])}"
                           for k, c in ids.items())
        first = set().union(*(_referenced(by_id[cid]) for cid in used))
        prompt = JUDGE_PROMPT.format(instruction=instruction.strip(), checks="\n".join(rows),
                                     suites=_suites_text(bundle, first), clauses=listed)
        answers = ask_families(prompt, _judgement_parser(set(ids)), models=tuple(models), ask=ask)
        for model, answer in answers.items():
            for k, clause in ids.items():
                if "error" in answer:
                    clause["judgement"][model] = {"error": answer["error"]}
                elif k in answer["answer"]:
                    clause["judgement"][model] = {**answer["answer"][k],
                                                  **({"judge": answer["judge"]} if answer.get("judge") else {})}
                else:
                    clause["judgement"][model] = {"error": "no verdict for this clause in the reply"}


def _status(clause: dict, models: tuple[str, ...]) -> str:
    if clause["quoted_by"]:
        answers = [clause["judgement"].get(m) or {"error": "not asked"} for m in models]
        if any(a.get("detects") == "NO" for a in answers):
            return "in_name_only"
        if all(a.get("detects") == "YES" for a in answers):
            return "enforced"
        return "unadjudicated"
    return "untested" if clause["untested_by"] else "unenforced"


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def ensure(bundle: Path, *, ask=None, write: bool = True) -> dict:
    """The current map: reused where the receipt still holds, asked again where it is stale or a family
    erred, and its coverage recomputed against the bundle as it is now."""
    bundle = Path(bundle)
    ask = ask or _ask
    models = judges()
    current = grading_sha(bundle)
    old = _json(bundle / RECEIPT, {}) or {}
    same = old.get("grading_sha256") == current and old.get("judges") == list(models)
    all_checks, quotes, surfaces, seeds = checks(bundle), rule_quotes(bundle), untested_surfaces(bundle), _seeds(bundle)
    record = {"schema": SCHEMA, "grading_sha256": current, "judges": list(models),
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": {}}
    for step, instruction in instructions(bundle).items():
        before = (old.get("steps") or {}).get(step) or {}
        inst_sha = hashlib.sha256(instruction.encode()).hexdigest()
        extraction = {}
        if before.get("instruction_sha256") == inst_sha:
            extraction = {m: e for m, e in (before.get("extraction") or {}).items()
                          if m in models and isinstance(e, dict) and "error" not in e}
        missing = tuple(m for m in models if m not in extraction)
        if missing:
            answers = ask_families(EXTRACT_PROMPT.format(instruction=instruction.strip()),
                                   _extraction_parser(instruction), models=missing, ask=ask)
            for m, a in answers.items():
                extraction[m] = ({"error": a["error"]} if "error" in a else
                                 {**a["answer"], **({"judge": a["judge"]} if a.get("judge") else {})})
        merged: dict[str, dict] = {}
        for m in models:
            for row in (extraction.get(m) or {}).get("clauses") or []:
                entry = merged.setdefault(norm(row["quote"]), {"quote": row["quote"], "by": []})
                entry["by"].append(m)
        for sentence in seeds.get(step) or []:
            merged.setdefault(norm(sentence), {"quote": sentence.strip(), "by": []})["by"].append("seed:preservation")
        judged = {norm(c.get("quote", "")): c.get("judgement") or {}
                  for c in before.get("clauses") or []} if same else {}
        clauses, todo = [], {m: [] for m in models}
        for i, (key, entry) in enumerate(merged.items(), 1):
            clause = {"id": f"c{i}", "quote": entry["quote"], "by": entry["by"],
                      "quoted_by": _quoted_by(entry["quote"], step, all_checks, quotes),
                      "untested_by": _untested_by(entry["quote"], surfaces),
                      "judgement": {m: a for m, a in (judged.get(key) or {}).items()
                                    if m in models and isinstance(a, dict) and "error" not in a}}
            if clause["quoted_by"]:
                for m in models:
                    if m not in clause["judgement"]:
                        todo[m].append(clause)
            clauses.append(clause)
        _judge_step(bundle, instruction, todo, all_checks, quotes, ask)
        for clause in clauses:
            clause["status"] = _status(clause, models)
        record["steps"][step] = {"instruction_sha256": inst_sha, "extraction": extraction, "clauses": clauses}
    blocking = findings(record)
    record["complete"] = not any(f["check"] == "coverage.clause_map" for f in blocking)
    record["open"] = len(blocking)
    if write and record["steps"]:
        from .artifacts import atomic_json
        atomic_json(bundle / RECEIPT, record)
    return record


def findings(record: dict) -> list[dict]:
    """The blocking findings a map records."""
    from .shipcheck import finding
    out = []
    models = tuple(record.get("judges") or ())
    for step, row in sorted((record.get("steps") or {}).items()):
        where = f"steps/{step}/instruction.md"
        for model, ext in sorted((row.get("extraction") or {}).items()):
            if "error" in ext:
                out.append(finding("coverage.clause_map", where,
                                   f"{model} did not extract the instruction's promises ({ext['error'][:200]}); the "
                                   "clause map is incomplete until both families answer -- re-run "
                                   "`python -m pipeline.clause_map <bundle>`", route="regenerate", measure="none"))
        for clause in row.get("clauses") or []:
            quote, status = " ".join(clause["quote"].split()), clause.get("status")
            subject = f"{where}: {quote[:80]}"
            if status == "unenforced":
                out.append(finding(
                    "coverage.clause_unenforced", subject,
                    f"the instruction promises {quote.strip()[:300]!r} but no check's rule_quote quotes it and no "
                    "untested surface quotes it with a determinacy reason: add a hidden case to a check that quotes "
                    "it (with its m_partial_ mutant) when the determinacy probe settles it, else declare it under "
                    "quality.untested_surfaces quoting the clause and the probe's result",
                    route="harden", measure="regrade", evidence=clause.get("by") or []))
            elif status == "in_name_only":
                said = {m: a for m, a in (clause.get("judgement") or {}).items() if a.get("detects") == "NO"}
                example = " | ".join(f"{m}: {a.get('counter_example') or '(no counter-example given)'}"
                                     for m, a in sorted(said.items()))
                out.append(finding(
                    "coverage.clause_in_name_only", subject,
                    f"{', '.join(clause['quoted_by'])} quote(s) {quote.strip()[:200]!r} but cannot detect a violation of "
                    f"the whole clause ({', '.join(sorted(said))}): {example[:900]} -- extend the tests so this "
                    "counter-example fails, or narrow the rule_quote to what they enforce and declare the rest "
                    "untested with determinacy evidence",
                    route="harden", measure="regrade",
                    evidence=[f"{m}: {a.get('counter_example', '')}"[:1500] for m, a in sorted(said.items())]))
            elif status == "unadjudicated":
                why = "; ".join(f"{m}: {(clause.get('judgement') or {}).get(m, {}).get('error', 'not asked')}"
                                for m in models if (clause.get("judgement") or {}).get(m, {}).get("detects") is None)
                out.append(finding(
                    "coverage.clause_map", subject,
                    f"whether {', '.join(clause['quoted_by'])} enforce(s) {quote.strip()[:200]!r} as a whole is not "
                    f"adjudicated by every family ({why[:300]}); an unanswered judgement blocks until it is asked again",
                    route="regenerate", measure="none"))
    return out


def clause_findings(bundle: Path, *, ask=None) -> list[dict]:
    """The map's blocking findings for the bundle as it is now (the map rebuilt when stale)."""
    if not instructions(bundle):
        return []
    return findings(ensure(bundle, ask=ask))


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundles", nargs="+", type=Path)
    args = ap.parse_args(argv)
    from .cli import load_keys
    load_keys(prompt_if_missing=False)
    failed = 0
    for bundle in args.bundles:
        record = ensure(bundle)
        found = findings(record)
        counts: dict[str, int] = {}
        for row in record["steps"].values():
            for clause in row["clauses"]:
                counts[clause["status"]] = counts.get(clause["status"], 0) + 1
        print(f"{bundle}: {sum(counts.values())} clause(s) {json.dumps(counts, sort_keys=True)}; {len(found)} open")
        for f in found:
            print(f"  [{f['route']:10s}] {f['check']:28s} {f['subject']}: {f['detail'][:200]}")
        failed += bool(found)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
