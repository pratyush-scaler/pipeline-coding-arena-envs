"""Is every counted failure the model's? A certificate per failed check.

User, 2026-10-04: "ensure all the failures are genuine model incapability
failures. And there should not be any grader/env/infra issues."

`attribution.episode_verdict` calls a clean episode with a failed check
`capability`. That is an episode-level verdict: a check can still fail for a
reason that is ours -- the test errored before asserting anything, a helper or
dependency was missing in the verifier, the run timed out, the outcome changes
between runs, the test leaned on a name the instruction never published. This
module reads every failed check of every counted episode from the grader's own
retained output and classifies it:

    assertion    the test ran the submission and an assertion about its
                 behaviour failed (pytest `E   AssertionError`, node
                 ERR_ASSERTION, a go `--- FAIL` without a panic)
    repo_crash   the submission's own code raised: the innermost non-library
                 frame of the traceback is in the repository, not the suite
    output       the test raised while consuming the submission's output
                 (KeyError, IndexError, TypeError, ValueError, a JSON decode
                 error ...): malformed output, attributable only together with
                 the reference passing the same test
    interface    the test could not reach the published entry point (an import
                 or attribute error naming repository code): genuine only when
                 the instruction publishes that name -- always adjudicated
    suspect      anything else: a grader refusal, a timeout, no output, a
                 collection/setup error outside the repository, an exception
                 raised by the suite or the grader itself, a frame we cannot read

and, with `regrade=True`, re-grades the episode's captured tree with the same
verifier (`sandbox.grade_capture`) to show the failure REPRODUCES.

A task is `certified` only when every failed check of every counted episode is
assertion/repo_crash/output, reproduces, the reference and the independently
written alternative pass every check (executed controls), and the check
enforces published sentences (`rule_quote`). Anything else is listed with its
evidence; nothing uncertified may be reported as difficulty.

User, 2026-10-21, after the external audit: an assertion on a published
sentence is not yet the model's. c-game-bot run gMGGfLM asked "should I also
verify position and velocity against the harness's physics?", got no answer,
lost 41 points and was certified `capability`. `ambiguity` reads every failing
run's own words for the questions it put to the user and the readings it chose;
one that concerns the failed decision makes the failure `ambiguity_suspect`
until both calibration families answer that the instruction settles the point,
quoting the deciding sentence (receipt AMBIGUITY_RECEIPT).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .artifacts import atomic_json
from .author.contracts import strict_json

SCHEMA = "arena-pipeline/genuine-failures/1"
RECEIPT = "verification/genuine.json"
CERTIFIED_KINDS = ("assertion", "repo_crash", "output")
#: grade.py's verdict text for a failed Go check (not a grader fault).
GO_TEST_FAILED = "a failed or skipped private Go test is not a pass"

_LIBRARY = ("site-packages", "dist-packages", "/usr/lib/python", "/usr/local/lib/python", "<frozen",
            "node:internal", "node_modules", "/opt/deps", "/usr/local/go/", "/usr/lib/go",
            "_pytest", "pluggy", "unittest/", "importlib")
_OUTPUT_ERRORS = ("KeyError", "IndexError", "TypeError", "ValueError", "JSONDecodeError",
                  "StopIteration", "UnicodeDecodeError", "ZeroDivisionError", "RecursionError")
_INTERFACE_ERRORS = ("ImportError", "ModuleNotFoundError", "AttributeError", "NameError")
_ASSERTION = re.compile(r"^E\s+AssertionError\b|^E\s+assert\b|ERR_ASSERTION|AssertionError \[|"
                        r"JestAssertionError|^\s*AssertionError\b|^--- FAIL:", re.M)

#: Frames: python long/short tracebacks, pytest's `path:line: Exc`, node and go.
_FRAMES = (re.compile(r'File "([^"]+)", line \d+'),
           re.compile(r"^([^\s:][^:\n]*\.(?:py|js|mjs|cjs|ts|go)):\d+:", re.M),
           re.compile(r"at (?:[^\s(]+ )?\(?((?:/|\.)[^\s:()]+\.(?:js|mjs|cjs|ts)):\d+:\d+\)?"),
           re.compile(r"^\s+(/[^\s:]+\.go):\d+", re.M))
_EXCEPTION = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)?)\b(?::|$)|"
                        r"^([A-Za-z_][\w.]*(?:Error|Exception))(?::|$)", re.M)


def _frames(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for pattern in _FRAMES:
        found += [(m.start(), m.group(1)) for m in pattern.finditer(text)]
    return [path for _, path in sorted(found)]


def _where(path: str, suites: set[str]) -> str:
    name = Path(path).name
    if any(marker in path for marker in _LIBRARY):
        return "library"
    if path.startswith("/tests") or "/suites/" in path or name in suites or name == "grade.py":
        return "suite"
    return "repo"


def classify(record: dict, suites: set[str]) -> tuple[str, str]:
    """(kind, detail) for one failed check, from the grader's retained record."""
    from .evidence import grader_refusal
    if not isinstance(record, dict):
        return "suspect", "no check record"
    if grader_refusal(record):
        return "suspect", f"grader refused before any test ran: {str(record.get('error') or record.get('reason'))[:160]}"
    if record.get("timed_out"):
        return "suspect", "the check timed out (a slow verifier and a hanging submission look alike)"
    if str(record.get("error") or "").startswith(GO_TEST_FAILED):
        # grade.py's ordinary verdict on a failed Go test, not a grader error:
        # read go's own output for what failed.
        text = f"{record.get('stdout') or ''}\n{record.get('stderr') or ''}"
        if re.search(r"^\s*--- SKIP", text, re.M) and not re.search(r"^\s*--- FAIL", text, re.M):
            return "suspect", "a private Go test was skipped, not failed"
        if "panic:" in text:
            frames = [(path, _where(path, suites)) for path in _frames(text[text.index("panic:"):])]
            own = [entry for entry in frames if entry[1] != "library"]
            # A goroutine trace lists the innermost frame FIRST (Python's last).
            if own and own[0][1] == "repo":
                return "repo_crash", f"panic in the submission ({Path(own[0][0]).name}){_tail(text)}"
            return "suspect", f"panic outside the submission{_tail(text)}"
        if re.search(r"^\s*--- FAIL", text, re.M):
            return "assertion", f"a Go test's assertion on the submission failed{_tail(text)}"
        return "suspect", "a Go test failed without readable output"
    if str(record.get("error") or "").startswith(_NO_REPORT):
        # The grader counts no PASS without a trusted report; a failure the runner itself
        # reported is still a failure (c-003-firebase, 2026-10-04: Playwright's JSON report
        # recorded `expect(received).toBe(expected)` failing on the game order).
        failed = _reported_assertion(f"{record.get('stdout') or ''}\n{record.get('stderr') or ''}")
        if failed:
            return "assertion", f"the runner's own report records a failed assertion: {failed[:200]}"
    if record.get("error"):
        return "suspect", f"grader error: {str(record['error'])[:160]}"
    if record.get("exit_code") is None:
        return "suspect", f"no exit status: {str(record.get('reason'))[:160]}"
    text = f"{record.get('stdout') or ''}\n{record.get('stderr') or ''}"
    if not text.strip():
        return "suspect", "the test produced no output to read"
    if re.search(r"INTERNALERROR|no tests ran|collected 0 items|completed no tests", text):
        return "suspect", "the runner completed no test"
    frames = [(path, _where(path, suites)) for path in _frames(text)]
    own = [entry for entry in frames if entry[1] != "library"]
    innermost = own[-1] if own else None
    exceptions = [m.group(1) or m.group(2) for m in _EXCEPTION.finditer(text)]
    exception = exceptions[-1].split(".")[-1] if exceptions else ""
    if _ASSERTION.search(text) and (innermost is None or innermost[1] == "suite"
                                   or exception in ("AssertionError", "")):
        return "assertion", f"assertion on the submission's behaviour failed{_tail(text)}"
    if innermost and innermost[1] == "repo":
        return "repo_crash", f"{exception or 'exception'} raised in the submission ({Path(innermost[0]).name}){_tail(text)}"
    if exception in _INTERFACE_ERRORS:
        return "interface", f"{exception} reaching the submission{_tail(text)}"
    if innermost and innermost[1] == "suite" and exception in _OUTPUT_ERRORS:
        return "output", f"{exception} while the test read the submission's output{_tail(text)}"
    if re.search(r"^ERROR |errors during collection", text, re.M):
        return "suspect", f"collection or setup error{_tail(text)}"
    return "suspect", f"unrecognised failure ({exception or 'no exception named'}){_tail(text)}"


_NO_REPORT = "runner produced no trustworthy report"
#: A failed test in a JSON test report (Playwright / Jest / Vitest) whose error message is an
#: assertion: `"status": "failed"` (or "unexpected") with `"message": "...expect(...)..."`.
_REPORTED_FAIL = re.compile(r'\bstatus"\s*:\s*"(?:failed|unexpected)"')   # retained output may be cut mid-key
_REPORTED_ASSERT = re.compile(
    r'"message"\s*:\s*"((?:[^"\\]|\\.){0,600}?(?:expect\(|toBe|toEqual|toHave|toContain|toMatch|AssertionError)(?:[^"\\]|\\.){0,200})')   # a diff may follow the matcher


#: The matcher's own failure form -- `expect(received).toEqual(expected)` -- is printed only
#: for an expectation that FAILED, so it stands even where the retained output was cut
#: before the result's `"status"` key (c-003-firebase o_different_links_kept, 2026-10-04).
_MATCHER_FAILED = re.compile(r"expect\((?:\\u001b\[[0-9;]*m|\x1b\[[0-9;]*m)*received(?:\\u001b\[[0-9;]*m|\x1b\[[0-9;]*m)*\)"
                             r"(?:\\u001b\[[0-9;]*m|\x1b\[[0-9;]*m|[.\w])*\((?:\\u001b\[[0-9;]*m|\x1b\[[0-9;]*m)*expected")


def _reported_assertion(text: str) -> str | None:
    found = _REPORTED_ASSERT.search(text)
    if not found or not (_REPORTED_FAIL.search(text) or _MATCHER_FAILED.search(found.group(1))):
        return None
    return re.sub(r"\\u001b\[[0-9;]*m|\x1b\[[0-9;]*m", "", found.group(1)) if found else None


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    keep = [line for line in lines if line.startswith(("E ", "FAILED", "not ok", "--- FAIL", "panic:", "AssertionError", "✖"))
            or "_test.go:" in line]
    snippet = " | ".join((keep or lines)[-2:])
    return f": {snippet[:220]}" if snippet else ""


#: Runners whose grader record keeps no readable output (node writes only a
#: JUnit report the grader consumes). `explain` re-runs the selected tests with
#: a human reporter -- diagnosis only; the verdict stays the grader's.
_EXPLAIN = {"node-test": "node --test --test-reporter=spec {select}"}


def explain(bundle: Path, step: str, capture: Path, check_id: str, *, verifier_tag: str | None = None) -> str | None:
    """Re-run one failed check's selected tests over a captured tree, with readable output."""
    import shlex
    from .author import sandbox
    try:
        checks = strict_json((Path(bundle) / "tests/checks.json").read_text())["checks"]
        runner = next(c["runner"] for c in checks if c["id"] == check_id)
    except (OSError, ValueError, KeyError, StopIteration):
        return None
    template = _EXPLAIN.get(runner.get("type"))
    if not template or not (Path(capture) / "repo").is_dir():
        return None
    tag = sandbox.present_image(verifier_tag, Path(bundle), "verifier") if verifier_tag else \
        sandbox.build_image(Path(bundle), "verifier")
    select = " ".join(shlex.quote(item) for item in runner.get("select") or [])
    # The grader's own environment, not the image's: grade.py starts every check from a
    # clean env, so an image default (the Playwright image's PLAYWRIGHT_BROWSERS_PATH)
    # never reaches a test. Inheriting it made c-012-visual-novel's explanations a
    # browser-launch error the graded run never had (2026-10-04).
    try:
        toolchain_env = strict_json((Path(bundle) / "environment/toolchain.json").read_text()).get("env") or {}
    except (OSError, ValueError):
        toolchain_env = {}
    env = {"HOME": "/tmp/explain/home", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1",
           "TZ": "UTC", "NODE_OPTIONS": "", "NPM_CONFIG_UPDATE_NOTIFIER": "false",
           "ARENA_REPO": "/tmp/explain/repo", "ARENA_TESTS": "/tests", "ARENA_STEP": step,
           **toolchain_env, **(runner.get("env") or {})}
    assignments = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
    command = ("rm -rf /tmp/explain && mkdir -p /tmp/explain/home && cp -R /app/repo /tmp/explain/repo && "
               "for d in /opt/deps/*; do [ -e \"$d\" ] && [ ! -e \"/tmp/explain/repo/$(basename \"$d\")\" ] "
               "&& ln -s \"$d\" /tmp/explain/repo/; done; "
               f"cd /tests/suites && env -i PATH=\"$PATH\" {assignments} "
               + template.format(select=select) + " 2>&1")
    with sandbox.Container(tag, caps=sandbox.VERIFIER_CAPS) as container:
        container.exec("rm -rf /app/repo && mkdir -p /app", timeout=60)
        container.put(Path(capture) / "repo", "/app/repo")
        result = container.exec(command, timeout=900)
    return (result.get("stdout") or "") + (result.get("stderr") or "")


def _suites(bundle: Path) -> set[str]:
    folder = Path(bundle) / "tests/suites"
    return {p.name for p in folder.rglob("*") if p.is_file()} if folder.is_dir() else set()


def episodes(bundle: Path, record: dict | None = None) -> list[dict]:
    """Every episode of the bundle's current screen, with its evidence dirs.

    Read through `evidence.native_evidence` -- the same parse the screen's
    numbers came from -- so validity (void or not) is the screen's own.
    """
    from . import screen
    from .attribution import contract_check_ids, episode_verdict
    from .evidence import native_evidence
    bundle = Path(bundle)
    record = record or screen.current(bundle)
    if not record:
        raise ValueError(f"{bundle.name}: no current screen to audit")
    contracts = contract_check_ids(bundle)
    out = []
    for model, row in sorted((record.get("per_model") or {}).items()):
        for campaign in [row.get("campaign"), *(row.get("topups") or [])]:
            if not campaign:
                continue
            problems, measured = native_evidence(bundle, Path(campaign), "solver")
            names = measured.get("step_names") or []
            for trial in measured.get("trials") or []:
                rows = [r for r in measured.get("rollouts") or [] if r["id"].startswith(f"{trial.get('id')}/")]
                verdict = episode_verdict(rows, contracts) if rows else {"verdict": "void", "reasons": ["no rows"]}
                steps = {s.get("name"): s for s in trial.get("steps") or []}
                out.append({"model": model, "campaign": str(campaign), "id": trial.get("id"),
                            "valid": bool(trial.get("valid")), "verdict": verdict["verdict"],
                            "reasons": verdict.get("reasons", []),
                            "evidence_problems": problems,
                            "steps": [{"name": n, "evidence_dir": (steps.get(n) or {}).get("evidence_dir"),
                                       "reward": (steps.get(n) or {}).get("reward")} for n in names]})
    return out


def _controls_pass(bundle: Path) -> dict:
    try:
        results = strict_json((Path(bundle) / "verification/controls.json").read_text())["results"]
    except (OSError, ValueError, KeyError):
        return {"golden": False, "alternative": False, "why": "no readable controls receipt"}
    out = {}
    for label in ("golden", "alternative"):
        entry = results.get(label) or {}
        out[label] = (entry.get("reward") == 1.0 and entry.get("valid") is True
                      and all(all(flags.values()) for flags in (entry.get("flags") or {}).values()))
    determinism = results.get("determinism")
    out["determinism"] = determinism if isinstance(determinism, (bool, dict, list)) else None
    return out


def _published(bundle: Path) -> dict[str, list[str]]:
    """check id -> the instruction sentences it enforces, only those actually in the step instruction."""
    try:
        spec = strict_json((Path(bundle) / "authoring/spec.json").read_text())
    except (OSError, ValueError):
        return {}
    texts = {s["name"]: re.sub(r"\s+", " ", s.get("instruction", "")) for s in spec.get("steps") or []}
    out = {}
    for check in spec.get("checks") or []:
        quotes = [q for q in check.get("rule_quote") or [] if isinstance(q, str)]
        text = texts.get(check.get("step"), "")
        out[check["id"]] = [q for q in quotes if re.sub(r"\s+", " ", q).strip() in text]
    return out


# ---------------------------------------------------------------------------
# Ambiguity-suspect failures
# ---------------------------------------------------------------------------

AMBIGUITY_RECEIPT = "verification/genuine-ambiguity.json"
AMBIGUITY_SCHEMA = "arena-pipeline/genuine-ambiguity/1"

#: Sentences that say a run chose between readings, in English and the languages the instructions are written in
#: (Russian, Chinese, Persian, Japanese). A detector's net only: the extracting judges read every language, and both
#: families judge relevance before anything counts.
_CHOICE = re.compile(
    r"\bI(?:'|’)?(?:ll| will| am going to|'m going to) (?:assume|interpret|read|treat|take|go with)\b|"
    r"\bassum(?:e|ing) (?:that|the user|you)\b|\b(?:two|both|several|multiple) (?:ways|readings|interpretations)\b|"
    r"\binterpret(?:ing|ed|ation)\b|\bambigu|\bunless you (?:want|request|prefer|ask)\b|\bif you (?:want|prefer|meant)\b|"
    r"\bread (?:this|it|that) as\b|\b(?:stated|listed) (?:rules|checks|limits) only\b|"
    r"предполага|предположу|будем считать|трактов|假设|假定|理解为|解读为|فرض (?:می|کرد)|تفسیر|仮定|と解釈|解釈し", re.I)
_SENTENCES = re.compile(r"(?<=[.!?。！？؟])\s+|\n+")

EXTRACT_PROMPT = """\
Below are the words a coding agent wrote while it worked on a user's task: its messages and its reasoning, not its \
commands or their output. List every place where it
(a) ASKED the user a question about what they want, or
(b) CHOSE one reading of the user's instruction over another: an assumption about what the user meant, an \
interpretation of an unclear rule, a scope it settled on because the instruction could be read two ways, or a \
behaviour it deliberately left out or put in pending the user's word.
The words may be in any language. Progress notes, questions the agent asks itself while debugging, and plans that \
do not choose between readings of the instruction are not listed.

THE AGENT'S WORDS
-----------------
{notes}

Reply with one line per item, exactly
ITEM: <ASKED|CHOSE> | <the agent's own words, copied verbatim: one or two sentences>
and then a final line exactly
ITEMS: <how many items you listed, 0 when there are none>"""

AMBIGUITY_PROMPT = """\
A user gave a developer the instruction below. A hidden test then graded the work and one check FAILED. While \
working, the developer asked the user the question(s) below or wrote down the reading it chose; nobody answered. \
Judge as a careful developer who reads only the instruction would.

INSTRUCTION
-----------
{instruction}

THE FAILED CHECK
----------------
id: {check}
what it grades (its author's description): {basis}
the instruction sentences it enforces:
{sentences}
the failing assertion (grader output, cut):
{assertion}

WHAT THE RUN ASKED OR CHOSE (its own words)
-------------------------------------------
{items}

Answer two questions. They are about WHAT the instruction asks for, not about how the developer went about it.
1. RELEVANT: Does any of these questions or choices concern a reading of the instruction that decides this check -- \
what the correct behaviour is in the case it grades, so that acting on the other answer would change whether it \
passes? A plan for how to implement the work (which files or functions to change, how to structure or validate the \
code) is not a reading and is NOT relevant. YES or NO.
2. SETTLED: Do the instruction's words decide that point -- the correct behaviour in the case the check grades? \
YES, NO (nothing in the instruction decides it), or UNDETERMINED (its words support two different correct \
outcomes). A precision or parsing slip, or a habit of the existing code that a stated rule overrides, is not a \
reading. If YES, quote the deciding sentence(s) verbatim from the instruction.

Reply with your reasoning in at most five sentences, then exactly these final lines:
RELEVANT: <YES|NO>
SETTLED: <YES|NO|UNDETERMINED>
QUOTE: <the deciding sentence(s), verbatim, or NONE>"""


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def _notes_text(notes: list[dict]) -> str:
    from . import policy
    from .attribution import _cut
    return _cut("\n".join(f"[turn {n.get('turn')}, {n['kind']}] {n['text']}" for n in notes),
                policy.AMBIGUITY_NOTES_CHARS)


def detected(notes: list[dict]) -> list[dict]:
    """The deterministic half of the detector: every question put through an ask-the-user tool, every sentence of a
    message that ends in a question mark, and every sentence matching a reading-choice cue."""
    out = []
    for note in notes:
        if note["kind"] == "asked":
            out.append({"kind": "asked", "text": note["text"], "turn": note.get("turn"), "found_by": "ask-the-user tool"})
            continue
        for sentence in (s.strip() for s in _SENTENCES.split(note["text"]) if s.strip()):
            if note["kind"] == "message" and sentence.endswith(("?", "？", "؟")):
                out.append({"kind": "asked", "text": sentence, "turn": note.get("turn"), "found_by": "question in a message"})
            elif _CHOICE.search(sentence):
                out.append({"kind": "chose", "text": sentence, "turn": note.get("turn"), "found_by": "cue"})
    return out


def _extracted(text: str) -> bool:
    return re.search(r"^\s*\**ITEMS\**\s*:\s*\d+", text or "", re.M) is not None


def extract(notes: list[dict], *, ask=None) -> dict:
    """The model half: both families list what the run asked or chose, in any language. An item counts only when its
    words are found verbatim in the run's notes; the notes count as read when at least one family answered."""
    from .attribution import ask_families, quote_found
    text = _notes_text(notes)
    answers = ask_families(EXTRACT_PROMPT.format(notes=text), _extracted, ask=ask)
    record = {"answered": sorted(m for m, a in answers.items() if "text" in a),
              "errors": {m: a["error"] for m, a in answers.items() if "error" in a}, "items": [], "unverified": []}
    for model, answer in answers.items():
        for kind, words in re.findall(r"^\s*ITEM:\s*(ASKED|CHOSE)\s*\|\s*(.+?)\s*$", answer.get("text") or "", re.M | re.I):
            words = words.strip().strip('"“”«»')
            row = {"kind": "asked" if kind.upper() == "ASKED" else "chose", "text": words, "found_by": model}
            (record["items"] if quote_found(words, text) else record["unverified"]).append(row)
    return record


def _judged(text: str) -> bool:
    from .attribution import final_line, verdict_word
    return (verdict_word(final_line(text, "RELEVANT")) in ("YES", "NO")
            and verdict_word(final_line(text, "SETTLED")) in ("YES", "NO", "UNDETERMINED"))


def judge_ambiguity(instruction: str, answers: dict[str, dict]) -> dict:
    """{"relevant", "settled", "judges"} from both families' answers.

    Relevant when ANY family says the question or choice concerns the graded decision, or gives no usable answer (an
    erring judge never clears a failure). Settled only when EVERY family answers YES with a deciding sentence found
    verbatim in the instruction."""
    from .attribution import final_line, quote_found, verdict_word
    per = {}
    for model, answer in answers.items():
        if "error" in answer:
            per[model] = {"error": answer["error"]}
            continue
        text = answer["text"]
        quote = final_line(text, "QUOTE") or ""
        per[model] = {"relevant": verdict_word(final_line(text, "RELEVANT")),
                      "settled": verdict_word(final_line(text, "SETTLED")), "quote": quote[:800],
                      "quote_found": quote_found(quote, instruction), "reasoning": text.strip()[-900:],
                      **({"judge": answer["judge"]} if answer.get("judge") else {})}
    relevant = any(a.get("relevant") != "NO" for a in per.values())
    settled = bool(per) and all(a.get("settled") == "YES" and a.get("quote_found") for a in per.values())
    return {"relevant": relevant, "settled": settled, "judges": per}


def _flags(answer: dict) -> bool | None:
    """One family's answer: True when the point is relevant and not settled, False when it clears, None when it erred."""
    if "error" in answer:
        return None
    return answer.get("relevant") != "NO" and not (answer.get("settled") == "YES" and answer.get("quote_found"))


def arbitrated(instruction: str, prompt: str, judged: dict, *, ask=None) -> dict:
    """`judged` (judge_ambiguity's result), decided by policy.READING_ARBITER when the families split -- one flags the
    failure, the other clears it (2026-10-21: Astra's plan narration "I'll route both scripts through the shared
    writer" flagged on one family's word held slide-pptx v10 back). An erring arbiter decides nothing."""
    from . import hack_triage, policy
    flags = {_flags(a) for a in judged["judges"].values()}
    if flags != {True, False}:
        return judged
    ask = ask or hack_triage._ask
    for effort in ("high", "medium"):
        try:
            reply = ask(policy.READING_ARBITER, prompt, effort=effort, max_tokens=hack_triage.JUDGE_MAX_TOKENS) or ""
        except Exception as exc:                           # noqa: BLE001 -- an arbiter that errs decides nothing
            judged = {**judged, "arbiter": {"model": policy.READING_ARBITER, "error": f"{type(exc).__name__}: {exc}"[:300]}}
            continue
        if _judged(reply):
            row = judge_ambiguity(instruction, {policy.READING_ARBITER: {"text": reply}})
            return {**judged, "relevant": row["relevant"], "settled": row["settled"],
                    "arbiter": {"model": policy.READING_ARBITER, **row["judges"][policy.READING_ARBITER]}}
    return judged


def ambiguity(bundle: Path, rows: list[dict], steps: dict[tuple, dict], *, ask=None, write: bool = True) -> list[str]:
    """Mark every failure whose run asked or chose on the graded decision `ambiguity_suspect` unless both families
    say the instruction settles it; the problems that keep the certificate from being certified.

    `steps` maps (model, episode id, step) to {"evidence_dir", "records": {check id: grader record}}. The extraction
    and every judgement are recorded in AMBIGUITY_RECEIPT, bound to the grading sha: an answered one holds for the
    version, an erring one is asked again by the next audit and blocks until then.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor
    from . import policy
    from .artifacts import identity
    from .attribution import ask_families, grading_sha, instruction_text, run_notes
    bundle = Path(bundle)
    sha = grading_sha(bundle)
    try:
        doc = strict_json((bundle / AMBIGUITY_RECEIPT).read_text())
    except (OSError, ValueError):
        doc = {}
    if doc.get("schema") != AMBIGUITY_SCHEMA or doc.get("grading_sha256") != sha:
        doc = {"schema": AMBIGUITY_SCHEMA, "bundle": bundle.name, "grading_sha256": sha,
               "extractions": {}, "judgements": {}}
    try:
        basis = {c["id"]: c.get("basis") for c in strict_json((bundle / "tests/checks.json").read_text())["checks"]}
    except (OSError, ValueError, KeyError):
        basis = {}
    published = _published(bundle)
    failing = [(entry, step) for entry in rows if entry["verdict"] not in ("void", "pass")
               for step in sorted({i["step"] for i in entry["failures"] if i.get("check")})]

    def read(pair):
        entry, step = pair
        notes = run_notes((steps.get((entry["model"], entry["id"], step)) or {}).get("evidence_dir"))
        if notes is None:
            return pair, None, {"unread": "no retained trajectory or transcript"}
        key = _sha(_notes_text(notes))
        known = doc["extractions"].get(key)
        record = known if known and not known.get("errors") else extract(notes, ask=ask)
        doc["extractions"][key] = record
        # Neither family read the notes: what the tools and cues found is still judged, and the step stays unread.
        trouble = {} if record["answered"] else {
            "unread": "; ".join(f"{m.split('/')[-1]}: {e}" for m, e in record["errors"].items())}
        items, seen = [], []
        for item in detected(notes) + record["items"]:
            words = re.sub(r"\s+", " ", item["text"]).casefold()
            if not any(words in other or other in words for other in seen):     # the same words found twice
                seen.append(words)
                items.append(item)
        items.sort(key=lambda i: i["kind"] != "asked")
        return pair, items[:policy.AMBIGUITY_MAX_ITEMS], trouble

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(read, failing))

    prompts: dict[str, tuple[str, str]] = {}          # key -> (prompt, the instruction its quotes are found in)
    plan = []
    for (entry, step), items, trouble in found:
        failures = [i for i in entry["failures"] if i["step"] == step and i.get("check")]
        if not items:
            for item in failures:
                item["ambiguity"] = {"status": "unread", "why": trouble["unread"]} if trouble else {"status": "none"}
            continue
        instruction = instruction_text(bundle, step)
        records = (steps.get((entry["model"], entry["id"], step)) or {}).get("records") or {}
        for item in failures:
            record = records.get(item["check"]) or {}
            prompt = AMBIGUITY_PROMPT.format(
                instruction=instruction, check=item["check"], basis=basis.get(item["check"]) or "(none given)",
                sentences="\n".join(f"- {q}" for q in published.get(item["check"]) or []) or "(none published)",
                assertion=str(record.get("stdout") or record.get("stderr") or item.get("detail") or "")[-1500:],
                items="\n".join(f"{n}. [{i['kind']}] {i['text']}" for n, i in enumerate(items, 1)))
            key = _sha(prompt)
            prompts[key] = (prompt, instruction)
            plan.append((entry, step, item, items, key))

    def judge(key):
        known = doc["judgements"].get(key)
        if known and not any("error" in a for a in (known.get("judges") or {}).values()):
            return key, known
        prompt, instruction = prompts[key]
        judged = judge_ambiguity(instruction, ask_families(prompt, _judged, ask=ask))
        return key, {**arbitrated(instruction, prompt, judged, ask=ask), "at": time.time()}

    with ThreadPoolExecutor(max_workers=4) as pool:
        doc["judgements"].update(dict(pool.map(judge, sorted(prompts))))

    problems = []
    for (entry, step), _, trouble in found:
        if trouble:
            problems.append(f"{entry['model'].split('/')[-1]} {entry['id']} {step}: the run's own words could not be "
                            f"read for questions to the user or reading choices ({trouble['unread']}) -- could-not-check "
                            "is not capability")
    for entry, step, item, items, key in plan:
        judged = doc["judgements"][key]
        label = f"{entry['model'].split('/')[-1]} {entry['id']} {step}/{item['check']}"
        if not judged["relevant"]:
            item["ambiguity"] = {"status": "unrelated", "judgement": key}
            continue
        if judged["settled"]:
            item["ambiguity"] = {"status": "settled", "judgement": key,
                                 "quotes": sorted({a.get("quote") for a in judged["judges"].values()})}
            continue
        item["ambiguity"] = {"status": "unsettled", "judgement": key, "was": item["kind"]}
        item["kind"] = "ambiguity_suspect"
        why = "; ".join(f"{m.split('/')[-1]}: " + (a["error"] if "error" in a else a.get("settled") + (
            "" if a.get("settled") != "YES" or a.get("quote_found") else " without a deciding sentence found in the instruction"))
            for m, a in judged["judges"].items())
        sentence = (published.get(item["check"]) or ["(no published sentence)"])[0]
        problems.append(f"{label}: ambiguity_suspect -- the run {items[0]['kind']} \"{items[0]['text'][:240]}\" and the "
                        f"instruction does not settle the decision this check grades ({why}); the check enforces "
                        f"\"{sentence[:240]}\". Route to the instruction: state the rule in the user's voice (or drop "
                        "the assertion), then recalibrate")
    doc["episodes"] = [{"model": entry["model"], "id": entry["id"], "step": step, "items": items,
                        "unread": trouble.get("unread")} for (entry, step), items, trouble in found]
    doc["artifact_sha256"], doc["at"] = identity(bundle), time.time()
    if write:
        atomic_json(bundle / AMBIGUITY_RECEIPT, doc)
    return problems


def audit(bundle: Path, *, regrade: bool = False, record: dict | None = None, write: bool = True,
          ask=None) -> dict:
    """The certificate for every counted failure of the bundle's current screen."""
    from .author import sandbox
    bundle = Path(bundle).resolve()
    suites = _suites(bundle)
    published = _published(bundle)
    controls = _controls_pass(bundle)
    verifier_tag = None
    rows = []
    step_records: dict[tuple, dict] = {}
    for episode in episodes(bundle, record):
        entry = {k: episode[k] for k in ("model", "id", "verdict", "valid")}
        entry["failures"] = []
        if episode["verdict"] in ("void", "pass"):
            rows.append(entry)
            continue
        history: dict[str, Path] = {}
        for step in episode["steps"]:
            folder = Path(step["evidence_dir"]) if step.get("evidence_dir") else None
            try:
                grade = json.loads((folder / "verifier/arena-grade.json").read_text()) if folder else None
            except (OSError, ValueError):
                grade = None
            if not isinstance(grade, dict):
                entry["failures"].append({"step": step["name"], "check": None, "kind": "suspect",
                                          "detail": "the step's grade record is missing or unreadable"})
                continue
            failed = {cid: rec for cid, rec in (grade.get("checks") or {}).items()
                      if isinstance(rec, dict) and not rec.get("passed") and not rec.get("gated_out")}
            step_records[(entry["model"], entry["id"], step["name"])] = {"evidence_dir": step.get("evidence_dir"),
                                                                         "records": failed}
            regraded = None
            capture = folder / "artifacts/app" if folder else None
            if regrade and failed and capture is not None and capture.is_dir():
                try:
                    result = sandbox.grade_capture(bundle, step["name"], capture, history=dict(history),
                                                   verifier_tag=verifier_tag)
                    verifier_tag = result.get("verifier_image") or verifier_tag
                    regraded = {cid for cid, rec in ((result.get("grade") or {}).get("checks") or {}).items()
                                if not rec.get("passed") and not rec.get("gated_out")} if result.get("valid") else None
                except Exception as exc:                  # noqa: BLE001 -- recorded as unreproduced
                    regraded = None
                    entry.setdefault("regrade_errors", []).append(f"{step['name']}: {type(exc).__name__}: {exc}"[:300])
            if capture is not None:
                history[step["name"]] = capture
            for cid, rec in sorted(failed.items()):
                kind, detail = classify(rec, suites)
                explained = False
                if (kind == "suspect" and detail.startswith("the test produced no output")
                        and capture is not None and capture.is_dir()):
                    try:
                        text = explain(bundle, step["name"], capture, cid, verifier_tag=verifier_tag)
                    except Exception:                     # noqa: BLE001 -- stays suspect
                        text = None
                    if text:
                        kind, detail = classify({**rec, "stdout": text, "stderr": ""}, suites)
                        explained = True
                item = {"step": step["name"], "check": cid, "kind": kind, "detail": detail,
                        "published": published.get(cid, []), "explained": explained}
                if regrade:
                    item["reproduced"] = bool(regraded is not None and cid in regraded)
                entry["failures"].append(item)
            if regrade and regraded is not None:
                extra = sorted(regraded - set(failed))
                if extra:
                    entry.setdefault("regrade_disagrees", []).append(
                        f"{step['name']}: re-grade also failed {extra} that the episode passed")
        rows.append(entry)
    problems = []
    for label in ("golden", "alternative"):
        if not controls.get(label):
            problems.append(f"the {label} control does not pass every check")
    # Before the per-failure verdicts: a failure the run asked or chose on is not yet the model's (user, 2026-10-21).
    problems += ambiguity(bundle, rows, step_records, ask=ask, write=write)
    for entry in rows:
        if entry["verdict"] in ("void", "pass"):
            continue
        if entry["verdict"] != "capability":
            problems.append(f"{entry['model']} {entry['id']}: episode verdict {entry['verdict']}")
        for item in entry["failures"]:
            label = f"{entry['model'].split('/')[-1]} {entry['id']} {item['step']}/{item['check']}"
            if item["kind"] not in CERTIFIED_KINDS and item["kind"] != "ambiguity_suspect":   # stated by `ambiguity`
                problems.append(f"{label}: {item['kind']} -- {item['detail']}")
            if regrade and not item.get("reproduced"):
                problems.append(f"{label}: did not reproduce on re-grade")
            if item.get("check") and not item.get("published"):
                problems.append(f"{label}: the check names no published sentence found in the instruction")
        for note in entry.get("regrade_disagrees", []) + entry.get("regrade_errors", []):
            problems.append(f"{entry['model'].split('/')[-1]} {entry['id']}: {note}")
    problems += guardrail_share_problems(bundle, rows)
    from .artifacts import identity
    from .attribution import grading_sha
    doc = {"schema": SCHEMA, "bundle": bundle.name, "artifact_sha256": identity(bundle),
           "grading_sha256": grading_sha(bundle),
           "regraded": regrade, "controls": controls, "episodes": rows,
           "counted_failures": sum(len(e["failures"]) for e in rows),
           "kinds": _kinds(rows), "ambiguity": {"receipt": AMBIGUITY_RECEIPT, **_statuses(rows)},
           "problems": problems, "certified": not problems}
    if write:
        atomic_json(bundle / RECEIPT, doc)
    return doc


def guardrail_share_problems(bundle: Path, rows: list[dict]) -> list[str]:
    """A guardrail most of one model's episodes fail is presumed over-strict.

    verification-library (Arena batch, 2026-10-07): g_existing_behaviour zeroed
    10/10 Fable episodes over a zero-argument `int()` that only the catch-all
    "everything else keeps working" published, while every objective scored
    0.68-1.0. Each failure was an assertion with a published sentence, so the
    per-failure certificate passed it. Above policy.SHIP_GUARDRAIL_FAIL_SHARE_MAX
    the guardrail needs a human waiver (shipcheck) or a repair.
    """
    from collections import Counter
    from . import policy
    from .shipcheck import _waived, waivers
    try:
        kinds = {c["id"]: c.get("kind") for c in
                 strict_json((Path(bundle) / "tests/checks.json").read_text()).get("checks", [])}
    except (OSError, ValueError):
        return []
    granted = waivers(bundle)
    seen: dict[str, int] = {}
    failed: dict[str, Counter] = {}
    for entry in rows:
        if entry.get("verdict") == "void":
            continue
        model = str(entry.get("model", "")).split("/")[-1]
        seen[model] = seen.get(model, 0) + 1
        for cid in {i.get("check") for i in entry.get("failures") or [] if kinds.get(i.get("check")) == "guardrail"}:
            failed.setdefault(model, Counter())[cid] += 1
    out = []
    for model, counts in sorted(failed.items()):
        for cid, n in sorted(counts.items()):
            subject = f"{model}/{cid}"
            if n / seen[model] > policy.SHIP_GUARDRAIL_FAIL_SHARE_MAX and not _waived(
                    {"check": "runs.guardrail_share", "subject": subject}, granted):
                out.append(f"{subject}: guardrail failed {n}/{seen[model]} episodes -- presumed over-strict; "
                           "repair it, or a human waives it after confirming a careful reader keeps that behaviour")
    return out


def _kinds(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in rows:
        for item in entry["failures"]:
            out[item["kind"]] = out.get(item["kind"], 0) + 1
    return out


def _statuses(rows: list[dict]) -> dict[str, int]:
    """Failures by what the ambiguity read found: none, unrelated, settled, unsettled, unread."""
    out: dict[str, int] = {}
    for entry in rows:
        for item in entry["failures"]:
            status = (item.get("ambiguity") or {}).get("status")
            if status:
                out[status] = out.get(status, 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .config import bundle_paths
    ap = argparse.ArgumentParser(description="certify every counted failure of each bundle's current screen")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--regrade", action="store_true", help="re-grade each failing episode's captured tree")
    args = ap.parse_args(argv)
    bundles = []
    for raw in args.paths:
        path = Path(raw)
        bundles += [path] if (path / "task.toml").is_file() else list(bundle_paths(path))
    worst = 0
    for bundle in bundles:
        try:
            doc = audit(bundle, regrade=args.regrade)
        except Exception as exc:                          # noqa: BLE001 -- reported per bundle
            print(f"{Path(bundle).name}: not audited ({type(exc).__name__}: {exc})")
            worst = max(worst, 2)
            continue
        state = "CERTIFIED" if doc["certified"] else "NOT CERTIFIED"
        print(f"{doc['bundle']}: {state} -- {doc['counted_failures']} counted failure(s) {doc['kinds']}")
        for problem in doc["problems"][:12]:
            print(f"    {problem}")
        worst = max(worst, 0 if doc["certified"] else 1)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
