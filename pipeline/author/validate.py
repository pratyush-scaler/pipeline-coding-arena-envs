"""Static validation of a coding spec against its fixture, before any container runs.

Everything here is decidable from the spec and the files on disk. What needs
execution (the fixture fails, the reference passes, mutants fail what they
declare, the grader is deterministic) is `controls.run`. Both run inside the
designer's `check_draft`, so a draft that reaches `submit` has already been
graded every way the gates will grade it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import policy
from . import checks as checks_mod
from . import quality, toolchain, traps as traps_mod
from .build import compile_steps, fixture_manifest
from .contracts import ContractError
from .solution import DIFF_HEADER, patch_touches

SLUG = re.compile(r"^[a-z][a-z0-9-]{4,60}$")
MIN_INSTRUCTION_CHARS = 200
MAX_SINGLE_WEIGHT_SHARE = 0.35
MAX_GATED_WEIGHT_SHARE = 0.30
MIN_RULE_QUOTE = 12
BACKTICK = re.compile(r"`([^`\n]{1,200})`")
PATHLIKE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _instruction_for(spec: dict, step: str) -> str:
    return next((s["instruction"] for s in spec.get("steps") or [] if s.get("name") == step), "")


def _scoring_shape(checks: list[dict], steps: list[str]) -> list[str]:
    issues = []
    objectives = [c for c in checks if c.get("kind") == "objective"]
    weights = [c.get("weight") for c in objectives]
    if objectives and len(set(weights)) == 1:
        issues.append("objective weights are uniform; weight the decisions that matter")
    if len(objectives) >= 6 and len(set(weights)) < 3:
        issues.append("with six or more objectives, use at least three distinct weights")
    for c in objectives:
        basis = c.get("weight_basis")
        if not isinstance(basis, str) or len(basis.strip()) < 25 or basis.strip().lower() == "important":
            issues.append(f"check {c.get('id')!r}: weight_basis must justify the weight in at least 25 characters")
    rubric = checks_mod.rubric(checks, steps)
    shares = checks_mod.effective_shares(checks, rubric)
    for cid, share in shares.items():
        if share > MAX_SINGLE_WEIGHT_SHARE + 1e-9:
            issues.append(f"check {cid!r} carries {share:.0%} of the task; at most {MAX_SINGLE_WEIGHT_SHARE:.0%}")
    by_id = {c["id"]: c for c in objectives}
    for c in objectives:
        downstream = _downstream(c["id"], objectives)
        gated = sum(shares.get(x, 0.0) for x in downstream) + shares.get(c["id"], 0.0)
        if downstream and gated > MAX_GATED_WEIGHT_SHARE + 1e-9:
            issues.append(f"check {c['id']!r} gates {gated:.0%} of the task through requires; at most "
                          f"{MAX_GATED_WEIGHT_SHARE:.0%} -- route a premise through several checks, never couple them")
        if c.get("requires") and by_id.get(c["requires"][0], {}).get("band") != "core":
            issues.append(f"check {c['id']!r}: only a core objective may be a prerequisite")
    return issues


def _downstream(cid: str, objectives: list[dict]) -> set[str]:
    out, frontier = set(), [cid]
    while frontier:
        current = frontier.pop()
        for c in objectives:
            if current in (c.get("requires") or []) and c["id"] not in out:
                out.add(c["id"])
                frontier.append(c["id"])
    return out


def _rule_quotes(checks: list[dict], spec: dict) -> list[str]:
    """Every check enforces a requirement the instruction states, verbatim."""
    issues = []
    for c in checks:
        quotes = c.get("rule_quote")
        if isinstance(quotes, str):
            quotes = [quotes]
        if not isinstance(quotes, list) or not quotes:
            issues.append(f"check {c.get('id')!r}: rule_quote must quote the instruction sentence(s) it enforces")
            continue
        text = _instruction_for(spec, c.get("step", ""))
        norm = " ".join(text.split())
        for q in quotes:
            if not isinstance(q, str) or len(q.strip()) < MIN_RULE_QUOTE:
                issues.append(f"check {c.get('id')!r}: a rule_quote must be at least {MIN_RULE_QUOTE} characters")
            elif " ".join(q.split()) not in norm:
                issues.append(f"check {c.get('id')!r}: rule_quote is not verbatim in step {c.get('step')}'s instruction: {q[:60]!r}")
    return issues


def _suite_problems(spec: dict, checks: list[dict], fixture: dict[str, dict]) -> list[str]:
    issues = []
    suites = spec.get("suites") or {}
    if not isinstance(suites, dict) or not suites:
        return ["suites: the hidden tests are missing"]
    for rel, content in suites.items():
        if not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/"):
            issues.append(f"suites: {rel!r} must be a relative path")
        if not isinstance(content, str):
            issues.append(f"suites: {rel} must be text")
    for c in checks:
        runner = c.get("runner") or {}
        for item in runner.get("select") or []:
            if runner.get("type") not in ("pytest", "node-test", "jest", "vitest", "mocha", "playwright", "go-test"):
                continue
            file_part, _, name = str(item).partition("::")
            if file_part not in suites:
                issues.append(f"check {c.get('id')!r}: selects {file_part}, which is not in suites")
            elif name and not re.search(r"\b" + re.escape(name.split("[")[0]) + r"\b", suites[file_part]):
                issues.append(f"check {c.get('id')!r}: {file_part} defines no {name.split('[')[0]!r}")
    # Hidden inputs must not already sit in the fixture, byte for byte.
    import hashlib
    fixture_hashes = {v["sha256"] for v in fixture.values()}
    for rel, content in suites.items():
        if rel.startswith("data/") and hashlib.sha256(content.encode("utf-8")).hexdigest() in fixture_hashes:
            issues.append(f"suites/{rel} is byte-identical to a fixture file; hidden inputs must be unseen")
    # The reference's added lines must not be quoted in a test (grades text, not behaviour).
    added = set()
    for entry in (spec.get("solution") or {}).values():
        for line in str(entry.get("patch", "")).splitlines():
            if line.startswith("+") and not line.startswith("+++") and len(line.strip("+ ")) >= 24:
                added.add(line[1:].strip())
    for rel, content in suites.items():
        for line in added:
            if line in content:
                issues.append(f"suites/{rel} quotes a line of the reference patch: {line[:50]!r}")
                break
    return issues


def _fixture_leak_problems(spec: dict, bundle: Path) -> list[str]:
    """The reference's added lines readable in a fixture file: the same rule the
    `answer_lookup` probe and the `hidden.disjoint` gate apply after submission,
    checked here so the designer sees it from check_draft instead of spending a
    repair round on it (streamlit seed, 2026-09-28: a later version of the
    script sat among the user's uploads as a .txt)."""
    from .probes import REFERENCE_LINES_READABLE
    added = set()
    for entry in (spec.get("solution") or {}).values() if isinstance(spec.get("solution"), dict) else []:
        for line in str((entry or {}).get("patch", "")).splitlines():
            if line.startswith("+") and not line.startswith("+++") and len(line.strip("+ ")) >= 24:
                added.add(line[1:].strip())
    if not added:
        return []
    repo = Path(bundle) / "environment/repo"
    issues = []
    for path in sorted(repo.rglob("*")) if repo.is_dir() else []:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        present = [line for line in added if line in text]
        if len(present) >= REFERENCE_LINES_READABLE or (len(added) < REFERENCE_LINES_READABLE
                                                        and len(present) == len(added)):
            rel = path.relative_to(repo).as_posix()
            issues.append(f"fixture {rel} carries {len(present)} line(s) of the reference patch (the answer is "
                          "readable there; the gate refuses it): exclude the file with materialize_fixture "
                          "exclude=[...] or replace it with write_fixture_file")
    return issues


def _instruction_references(spec: dict, fixture: dict[str, dict]) -> list[str]:
    """Backticked paths must exist in the fixture or be declared deliverables."""
    issues = []
    declared = {d.get("path") for d in spec.get("deliverables") or []}
    roots = ("/app/repo", "/app/deliverables")
    for step in spec.get("steps") or []:
        for token in BACKTICK.findall(step.get("instruction", "")):
            if "/" not in token and "." not in token:
                continue
            if not PATHLIKE.match(token) or token.endswith(("(", ")")):
                continue
            path = token
            for root in roots:
                if path.startswith(root + "/"):
                    path = path[len(root) + 1:]
            if path.startswith("/") or path.endswith("/"):
                continue
            if any(path.endswith(ext) for ext in (".py", ".js", ".ts", ".json", ".csv", ".txt", ".md", ".html",
                                                   ".css", ".php", ".go", ".rs", ".java", ".sql", ".yml", ".yaml",
                                                   ".toml", ".cfg", ".ini", ".sh")) or "/" in path:
                if path not in fixture and path not in declared and not any(k.startswith(path + "/") for k in fixture):
                    issues.append(f"step {step.get('name')}: instruction references `{token}`, which exists "
                                  "neither in the fixture nor as a declared deliverable")
    return issues


_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", "<!--", "'" * 3, '"' * 3)


def _changes_behaviour(patch: str) -> bool:
    """Does a unified diff add or remove at least one line that is not blank or a comment?"""
    for line in patch.splitlines():
        if not line or line[0] not in "+-" or line.startswith(("+++", "---")):
            continue
        body = line[1:].strip()
        if not body or body.startswith(_COMMENT_PREFIXES):
            continue
        return True
    return False


def trap_shape_problems(traps: list, checks: list[dict]) -> list[str]:
    """The difficulty floors (policy): enough traps, of enough types, changing a premise, coupled."""
    traps = [t for t in traps if isinstance(t, dict)]
    objectives = {c.get("id") for c in checks if c.get("kind") == "objective"}
    issues = []
    if len(traps) < policy.MIN_TRAPS_PER_TASK:
        issues.append(f"{len(traps)} trap(s); at least {policy.MIN_TRAPS_PER_TASK} genuine traps are required")
    types = {t.get("type") for t in traps}
    if len(types) < policy.MIN_TRAP_TYPES:
        issues.append(f"{len(types)} trap type(s) ({', '.join(sorted(str(x) for x in types))}); at least "
                      f"{policy.MIN_TRAP_TYPES} distinct types -- same-type traps are correlated")
    premise = [t for t in traps if t.get("type") in policy.PREMISE_TRAP_TYPES
               and any(cid in objectives for cid in (t.get("checks") or []))]
    if len(premise) < policy.MIN_PREMISE_TRAPS:
        issues.append(f"{len(premise)} objective-driving premise trap(s) ({', '.join(policy.PREMISE_TRAP_TYPES)}); "
                      f"at least {policy.MIN_PREMISE_TRAPS} -- a premise that changes is the only lever that measured")
    coupled = [t for t in traps if sum(1 for cid in (t.get("checks") or []) if cid in objectives) >= 2]
    if len(coupled) < policy.MIN_COUPLED_TRAPS:
        issues.append(f"{len(coupled)} trap(s) drive two or more objectives; at least {policy.MIN_COUPLED_TRAPS} -- "
                      "route several checks through one wrong premise, since pass@1 counts only full success")
    return issues


#: Lines that mark an assistant-written specification rather than a person
#: describing a problem: markdown headings, enumerated requirement lists.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.M)
_TABLE = re.compile(r"^\s*\|.*\|\s*$", re.M)


def voice_problems(instruction: str, where: str) -> list[str]:
    """The instruction reads as the user wrote it, never as an assistant would.

    Vague about how and where is fine and often right; the structure of a
    spec (headings, bullet-pointed requirements, acceptance criteria, "ensure
    that") is what gives an assistant away, and a task whose instruction reads
    that way is not the trace's.
    """
    if not policy.INSTRUCTION_VOICE_ENFORCED or not isinstance(instruction, str):
        return []
    issues = []
    if _HEADING.search(instruction):
        issues.append(f"{where}: markdown headings -- a person describing a problem does not write section titles")
    bullets = len(_BULLET.findall(instruction))
    if bullets > policy.INSTRUCTION_MAX_BULLET_LINES:
        issues.append(f"{where}: {bullets} bullet/numbered lines (at most {policy.INSTRUCTION_MAX_BULLET_LINES}); "
                      "state the ask in prose the way the user did, not as a requirements list")
    if _TABLE.search(instruction):
        issues.append(f"{where}: a markdown table; users describe, they do not tabulate")
    lowered = instruction.lower()
    idioms = [phrase for phrase in policy.INSTRUCTION_ASSISTANT_IDIOMS if phrase in lowered]
    if idioms:
        issues.append(f"{where}: assistant idioms {idioms} -- rewrite in the user's own register (their words from "
                      "the trace where they exist), vague about how and where, precise about what counts")
    return issues


#: Phrases by which a later step's instruction leans on an earlier step's
#: PROMPT. Every calibration step runs in a fresh session (Codex never sees
#: step 1's instruction; a-034 in the parent programme), so what governs a
#: later step must be stated in that step. The repository state carries the
#: earlier step's code; the earlier prompt is gone.
_HISTORY_REFERENCE = re.compile(
    r"\b(?:step[ -]?(?:[1-9]|one|two|three)\b|(?:the |your )?(?:previous|earlier|last|first|second|third) "
    r"(?:step|turn)\b|"
    r"(?:my|the|your) (?:previous|earlier|last|first|second|other) (?:message|request|prompt|instructions?)\b|"
    r"as (?:i|we) (?:said|asked|mentioned|described|explained) (?:before|earlier|above)|"
    r"like i (?:said|asked|told you)\b|"
    r"same (?:rules?|requirements?|constraints?|logic) as before|"
    r"the (?:rules?|requirements?|constraints?) (?:from )?(?:before|above|earlier))",
    re.IGNORECASE)


def history_reference_problems(instruction: str, where: str, position: int) -> list[str]:
    """A step after the first must not define what counts by pointing at the earlier prompt."""
    if not policy.INSTRUCTION_VOICE_ENFORCED or position <= 1 or not isinstance(instruction, str):
        return []
    hits = sorted({m.group(0).lower() for m in _HISTORY_REFERENCE.finditer(instruction)})
    if not hits:
        return []
    return [f"{where}: refers to an earlier step's instruction {hits} -- every step is graded in a fresh "
            "session that never saw the earlier prompt; restate what governs this step in its own words"]


#: Advisories, never refusals: the shapes the independent reviewer rejected in
#: every first draft of the pilot (2026-09-28), detected where a static reader
#: can. The designer sees them from check_draft and decides; the reviewer
#: still judges. Adding blockers to the pipeline is not the point; a first
#: draft that never carries these shapes is.
_DEFINED = re.compile(
    r"^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"               # def/class name
    r"|^\+\s*self\.([A-Za-z_]\w*)\s*=(?!=)"                              # self.attr =
    r"|^\+([A-Z_][A-Z0-9_]{2,})\s*(?::[^=\n]*)?=(?!=)"                   # MODULE_CONST =
    r"|^\+\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
    r"|^\+\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M)
_STRUCTURE_WORDS = re.compile(r"\b(?:files?|folders?|director(?:y|ies)|modules?|layout|structure|rename|move|"
                              r"split|add(?:ed|ing)?\s+(?:a\s+)?(?:new\s+)?files?|new\s+files?|create[sd]?\s+(?:a\s+)?"
                              r"(?:new\s+)?files?|restructur|monolith|single\s+file|one\s+file)\b", re.I)
_SUBPROCESS_STDOUT = re.compile(r"subprocess\.(?:run|check_output|Popen)[\s\S]{0,400}?\.stdout|check_output\(", re.S)
#: Census 2026-10-03 (167 reviews): the reviewer's largest theme was a route to
#: credit without the work -- agent-writable evidence and mutable oracles -- and
#: harness infidelity. Each pattern below is one recurring shape, any language.
_SUITE_SHAPES = (
    ("forgeable", re.compile(r"child_process|execSync|spawnSync|execFileSync|exec\.Command\([^)]*\)\s*\.(?:Output|"
                             r"CombinedOutput)|os\.popen|getoutput\(|##RESULT|RESULT_MARKER", re.S),
     "reads a child process's output or a printed marker. The code under test can print whatever the assertion "
     "expects; assert on files, return values or state the test process observes itself."),
    ("mutable-oracle", re.compile(r"pytest\.approx|math\.isclose|\.prototype\b|globalThis\.|window\.\w+\s*=|"
                                  r"sys\.modules\[|importlib\.reload|builtins\.", re.S),
     "compares through an object the code under test can rebind (pytest.approx, math.isclose, a prototype, a "
     "global, sys.modules). The reviewer rejects an oracle the agent's import can patch; compare plain values "
     "computed in the test, or serialise them in a separate process first."),
    ("unpublished-structure", re.compile(r"\.tagName\b|\.parentElement\b|\.children\[|\.childNodes\[|nth-child|"
                                         r"querySelector\(\s*['\"][^'\"]*>|findChild\(\s*Q\w+|findChildren\(\s*Q\w+|"
                                         r"winfo_children\(\)\[", re.S),
     "asserts element types, DOM ancestry or widget classes. A correct UI with different markup fails it; drive "
     "the page through roles, labels and visible text the instruction or the existing UI publishes."),
    ("harness-infidelity", re.compile(r"new\s+Function\(|vm\.runIn|\beval\(|replace\(\s*/\^?\s*import|"
                                      r"import\s*\\s|jsdom|synthetic event|event_generate\(", re.S),
     "imitates the runtime (a regex module loader, eval of rewritten source, jsdom or synthetic events). The "
     "reviewer reads it as testing a different program; load the real module the way the real runtime does."),
    ("text-bound", re.compile(r"read_text\(\)[\s\S]{0,80}re\.(?:search|findall|match)|readFileSync\([^)]*\)"
                              r"[\s\S]{0,80}\.(?:match|includes)\(|inspect\.getsource", re.S),
     "regex-scans source or output bytes for structure. Behaviour, not wording, is graded; parse the output "
     "format or observe the behaviour."),
)
_PY_STOPWORDS = {"self", "cls", "None", "True", "False", "test", "main", "setup", "init", "data", "path", "name",
                 "value", "result", "error", "config", "run", "get", "set", "list", "dict", "str", "int"}


def overfit_advisories(spec: dict, bundle: Path) -> list[str]:
    """Where a hidden test looks shaped by the reference rather than the rule.

    Three detectable shapes: a symbol the reference patch introduces that the
    tests use although neither the instruction nor the fixture names it (the
    test grades the reference's design); a tree-guard file budget with no
    instruction sentence about files or structure (an unpublished
    requirement); a test that reads a subprocess's stdout (forgeable by the
    code under test). Advisory text only; nothing here blocks.
    """
    out: list[str] = []
    suites = spec.get("suites") if isinstance(spec.get("suites"), dict) else {}
    instructions = " ".join(str(s.get("instruction", "")) for s in (spec.get("steps") or []) if isinstance(s, dict))
    repo = Path(bundle) / "environment/repo"
    fixture_text = []
    if repo.is_dir():
        for path in repo.rglob("*"):
            if path.is_file() and path.stat().st_size <= 2_000_000:
                try:
                    fixture_text.append(path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, OSError):
                    continue
    fixture_blob = "\n".join(fixture_text)
    introduced: set[str] = set()
    for entry in (spec.get("solution") or {}).values() if isinstance(spec.get("solution"), dict) else []:
        for match in _DEFINED.finditer(str((entry or {}).get("patch", ""))):
            name = next(g for g in match.groups() if g)
            if len(name) >= 4 and name not in _PY_STOPWORDS:
                introduced.add(name)
    for name in sorted(introduced):
        if re.search(r"\b" + re.escape(name) + r"\b", fixture_blob) or re.search(r"\b" + re.escape(name) + r"\b", instructions):
            continue
        users = [rel for rel, text in suites.items() if isinstance(text, str) and re.search(r"\b" + re.escape(name) + r"\b", text)]
        if users:
            out.append(f"ADVISORY overfit: {', '.join(sorted(users)[:3])} use `{name}`, which only the reference patch "
                       "introduces (the instruction and the fixture never name it): the test grades the reference's "
                       "design, and a differently written correct fix fails it. Observe behaviour through the published "
                       "interface, or publish the name in the instruction.")
    for c in spec.get("checks") or []:
        if not isinstance(c, dict):
            continue
        runner = c.get("runner") or {}
        if runner.get("type") == "tree-guard":
            quotes = " ".join(str(q) for q in (c.get("rule_quote") or []))
            if not _STRUCTURE_WORDS.search(quotes):
                out.append(f"ADVISORY unpublished: check {c.get('id')!r} is a tree-guard with a file budget, but none of its "
                           "quoted instruction sentences speaks of files or structure. The reviewer reads that as an "
                           "unpublished requirement; use a shadow-guard (no budget) unless the instruction restricts new files.")
    for rel, text in suites.items():
        if isinstance(text, str) and _SUBPROCESS_STDOUT.search(text):
            out.append(f"ADVISORY forgeable: suites/{rel} reads a subprocess's stdout. The code under test can print "
                       "whatever the assertion expects; assert on files, return values or state the test observes itself.")
    for rel, text in suites.items():
        if not isinstance(text, str) or rel.startswith("data/"):
            continue
        for kind, pattern, why in _SUITE_SHAPES:
            if kind == "forgeable" and _SUBPROCESS_STDOUT.search(text):
                continue
            if pattern.search(text):
                out.append(f"ADVISORY {kind}: suites/{rel} {why}")
    return out


#: A hidden suite must grade what is in the tree it is given. /arena-history is
#: provided only by this pipeline's own verifier (benchmark/history_verifier.py);
#: stock Harbor never uploads it, so a suite that reads it cannot reproduce at Arena.
_HISTORY_DIR = re.compile(r"arena-history|ARENA_HISTORY")


def multi_turn_problems(spec: dict, bundle: Path, checks: list[dict]) -> list[str]:
    """Spec Appendix A for a task with two or more steps: every turn is a real,
    later user message of the trace (`trace_turn`, checked against the
    extraction's user-turn index), every turn carries substantive graded work
    of its own (objective and trap floors per step), and no suite reads
    earlier captures the shipped verifier does not have."""
    steps = [s for s in (spec.get("steps") or []) if isinstance(s, dict)]
    issues: list[str] = []
    for name, text in (spec.get("suites") or {}).items():
        if isinstance(text, str) and _HISTORY_DIR.search(text):
            issues.append(f"suite {name}: reads /arena-history (earlier steps' captures); stock Harbor's verifier "
                          "never receives them, so the check cannot reproduce at Arena -- grade the tree the "
                          "step leaves behind")
    if len(steps) < 2:
        return issues
    index_path = Path(bundle) / "authoring/extraction/user-turns.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        user_msgs = {t["msg"] for t in index.get("turns") or [] if isinstance(t, dict) and type(t.get("msg")) is int}
        pivot = index.get("pivot")
    except (OSError, ValueError, KeyError, TypeError):
        index, user_msgs, pivot = None, set(), None
    if index is None:
        issues.append("multi-turn: authoring/extraction/user-turns.json is missing or unreadable; every turn must "
                      "cite the user message it reproduces (re-run extraction with the current pipeline)")
    previous = None
    for position, step in enumerate(steps, 1):
        turn = step.get("trace_turn")
        where = f"step {step.get('name')!r}"
        if type(turn) is not int:
            issues.append(f"{where}: trace_turn must be the msg index of the user message this turn reproduces "
                          "(Spec Appendix A: turns are the user's own sequential messages, never an artificial split)")
            continue
        if index is not None and turn not in user_msgs:
            issues.append(f"{where}: trace_turn {turn} is not a user message of the session")
        if previous is not None and turn <= previous:
            issues.append(f"{where}: trace_turn {turn} does not come after the previous turn's {previous}; turns "
                          "must be sequential user messages")
        if position == 1 and type(pivot) is int and pivot > turn + 1:
            issues.append(f"{where}: the fixture's pivot {pivot} is after turn 1's message {turn}, so environment/repo "
                          "already holds the trace assistant's answer to it -- re-lift the tree before that answer "
                          f"(materialize_fixture({turn}))")
        if position > 1 and type(pivot) is int and turn < pivot:
            issues.append(f"{where}: trace_turn {turn} precedes the fixture's pivot {pivot}; a later turn must come "
                          "after the state the task starts from")
        previous = turn
    objectives = [c for c in checks if isinstance(c, dict) and c.get("kind") == "objective"]
    step_of = {c.get("id"): c.get("step") for c in checks if isinstance(c, dict)}
    for step in steps:
        name = step.get("name")
        own = [c for c in objectives if c.get("step") == name]
        if len(own) < policy.MIN_OBJECTIVES_PER_STEP:
            issues.append(f"step {name!r}: {len(own)} objective(s); a multi-turn task needs at least "
                          f"{policy.MIN_OBJECTIVES_PER_STEP} per turn (Appendix A: every turn is substantive work)")
        traps = [t for t in (spec.get("traps") or []) if isinstance(t, dict)
                 and any(step_of.get(cid) == name for cid in (t.get("checks") or []))]
        if len(traps) < policy.MIN_TRAPS_PER_STEP:
            issues.append(f"step {name!r}: {len(traps)} trap(s) drive its checks; a multi-turn task needs at least "
                          f"{policy.MIN_TRAPS_PER_STEP} per turn, each proved by its own mutant at that turn")
    return issues


def validate(spec: dict, bundle: Path) -> list[str]:
    bundle = Path(bundle)
    issues: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be an object"]
    if not isinstance(spec.get("name"), str) or not SLUG.match(spec["name"]):
        issues.append("name must be a slug: lowercase letters, digits and hyphens, 5-61 characters")
    for key in ("description", "difficulty", "intent", "language"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            issues.append(f"{key} is required")
    chain = spec.get("toolchain") or {}
    if not isinstance(chain, dict) or chain.get("name") not in toolchain.TOOLCHAINS:
        issues.append("toolchain.name must be one of " + ", ".join(toolchain.TOOLCHAINS))
        runners = []
        vendored: list[str] = []
    else:
        record = toolchain.TOOLCHAINS[chain["name"]]
        runners, vendored = list(record.runners), list(record.vendored)
        for key in (spec.get("deps") or {}):
            if key not in record.manifests and key not in ("requirements.txt", "package.json", "package-lock.json"):
                issues.append(f"deps: {key} is not a manifest the {chain['name']} toolchain installs "
                              f"({', '.join(record.manifests) or 'none'})")
        for line in (spec.get("deps") or {}).get("requirements.txt", "").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and "==" not in line and not line.startswith("-"):
                issues.append(f"deps/requirements.txt: {line!r} is not pinned with ==")
    try:
        steps = [s["name"] for s in compile_steps(spec)]
    except ContractError as exc:
        return issues + [str(exc)]
    budget = spec.get("step_budget")
    if type(budget) is int and budget >= 1 and len(spec["steps"]) > budget:
        issues.append(f"steps: {len(spec['steps'])} steps exceed this task's step budget of {budget} (user, "
                      "2026-10-03: about 90% of tasks are single-step and only designated ones multi-step); "
                      f"fold the work into {budget} step(s), each extremely hard on its own")
    if (type(budget) is int and budget >= policy.MULTI_STEP_MIN_STEPS
            and len(spec["steps"]) < policy.MULTI_STEP_MIN_STEPS):
        issues.append(f"steps: this task is designated MULTI-TURN with a budget of {budget}; it needs at least "
                      f"{policy.MULTI_STEP_MIN_STEPS} turns (user, 2026-10-12: \"at least 3 turns\"), each a later "
                      "user message from the trace that builds on the state the previous turn left, each "
                      "extremely hard on its own (Spec Appendix A)")
    for step in spec["steps"]:
        if len(step["instruction"].strip()) < MIN_INSTRUCTION_CHARS:
            issues.append(f"step {step['name']}: instruction under {MIN_INSTRUCTION_CHARS} characters")
        if step is not spec["steps"][0]:
            if not isinstance(step.get("new_information"), str) or len(step["new_information"]) < 30:
                issues.append(f"step {step['name']}: a later step must state its new_information (>= 30 chars)")
    try:
        fixture = fixture_manifest(bundle, vendored)
    except ContractError as exc:
        return issues + [str(exc)]
    for rel in fixture:
        if any(part in ("node_modules", ".git", "__pycache__", ".venv", "venv") for part in rel.split("/")):
            issues.append(f"fixture contains a vendored or generated path: {rel}")
            break
    from ..config import BROWSER_LANGUAGES, LANGUAGE_BY_EXT, NATIVE_LANGUAGES
    from .extract import redaction_markers
    repo = bundle / "environment/repo"
    for rel in fixture:
        lang = LANGUAGE_BY_EXT.get(Path(rel).suffix.lower())
        if lang in NATIVE_LANGUAGES or lang in BROWSER_LANGUAGES:
            try:
                count = redaction_markers((repo / rel).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if count:
                issues.append(f"fixture {rel}: {count} upstream redaction placeholder(s) (e.g. [SECURITY_DATA]) "
                              "inside source -- the program cannot run as lifted; repair the file with "
                              "write_fixture_file (recorded as authored) or exclude it")
    checks = spec.get("checks") or []
    issues += checks_mod.check_problems(checks, steps, runners)
    if not any(i.startswith("check") or i.startswith("checks") for i in issues):
        issues += _scoring_shape(checks, steps)
        issues += _rule_quotes(checks, spec)
        issues += _suite_problems(spec, checks, fixture)
        issues += _fixture_leak_problems(spec, bundle)
    for c in checks:
        runner = c.get("runner") or {}
        if runner.get("type") in ("hash-guard",) and runner.get("mode", "unchanged") == "unchanged":
            for rel in runner.get("paths") or []:
                if rel not in fixture:
                    issues.append(f"check {c.get('id')!r}: hash-guard path {rel} is not in the fixture")
        if runner.get("type") == "size-guard" and runner.get("path") not in fixture:
            issues.append(f"check {c.get('id')!r}: size-guard path {runner.get('path')} is not in the fixture")
    issues += _instruction_references(spec, fixture)
    for _position, _step in enumerate(compile_steps(spec) if isinstance(spec.get('steps'), list) else [], 1):
        _where = f"step {_step.get('name')!r} instruction"
        issues += voice_problems(_step.get('instruction', ''), _where)
        issues += history_reference_problems(_step.get('instruction', ''), _where, _position)
    # deliverables
    for d in spec.get("deliverables") or []:
        if not isinstance(d, dict) or not isinstance(d.get("path"), str) or d.get("kind", "file") not in ("json", "file"):
            issues.append("deliverables entries must be {path, kind: json|file, step}")
        elif d.get("step") not in steps:
            issues.append(f"deliverable {d.get('path')}: step must be declared")
        elif (Path(bundle) / "environment/repo" / d["path"].lstrip("/")).is_dir():
            # grade.py accepts regular files only; two finished designs failed
            # their reference control on a declared directory (2026-10-03).
            issues.append(f"deliverable {d['path']}: is a directory in the fixture; the grader reads declared "
                          "deliverables as regular files -- declare the files themselves")
    # solution and alternative
    solution = spec.get("solution")
    if not isinstance(solution, dict):
        issues.append("solution must map each step to {patch, commands?, provenance}")
    else:
        for step in steps:
            entry = solution.get(step)
            if not isinstance(entry, dict) or not isinstance(entry.get("patch"), str):
                issues.append(f"solution[{step!r}] needs a unified-diff patch")
                continue
            if not DIFF_HEADER.search(entry["patch"]):
                issues.append(f"solution[{step!r}].patch is not a unified diff")
            for rel in patch_touches(entry["patch"]):
                if rel.startswith("tests/") or rel.startswith("/"):
                    issues.append(f"solution[{step!r}] touches {rel}: outside the working tree")
            prov = entry.get("provenance")
            if not isinstance(prov, dict) or prov.get("origin") not in ("trace_accepted", "trace_derived", "authored"):
                issues.append(f"solution[{step!r}].provenance.origin must be trace_accepted, trace_derived or authored")
    alternative = spec.get("alternative") or {}
    for step in steps:
        entry = alternative.get(step) if isinstance(alternative, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("patch"), str) or not DIFF_HEADER.search(entry["patch"]):
            issues.append(f"alternative[{step!r}]: an independently written second correct fix (unified diff) is required")
        elif isinstance(solution, dict) and isinstance(solution.get(step), dict) \
                and entry["patch"].strip() == str(solution[step].get("patch", "")).strip():
            issues.append(f"alternative[{step!r}] is identical to the reference")
    # traps and mutants
    mutants = spec.get("mutants") or []
    mutant_ids = set()
    for m in mutants:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not re.match(r"^m_[a-z0-9_]{2,40}$", m["id"]):
            issues.append("mutants need ids matching m_[a-z0-9_]+")
            continue
        if m["id"] in mutant_ids:
            issues.append(f"mutant {m['id']}: duplicate id")
        mutant_ids.add(m["id"])
        if not isinstance(m.get("patch"), str) or not DIFF_HEADER.search(m["patch"]):
            issues.append(f"mutant {m['id']}: patch must be a unified diff against the fixture")
        if not isinstance(m.get("reason"), str) or len(m["reason"]) < 20:
            issues.append(f"mutant {m['id']}: reason must say what plausible wrong fix this is")
        if isinstance(m.get("patch"), str) and not _changes_behaviour(m["patch"]):
            issues.append(f"mutant {m['id']}: the patch changes only comments or whitespace; a plausible wrong "
                          "fix changes what the program does (reviewer finding, 2026-09-27: an inert comment "
                          "edit was passed off as a scope-creep mutant)")
        fails = m.get("fails")
        if not isinstance(fails, list) or not fails or any(f not in {c.get("id") for c in checks} for f in fails):
            issues.append(f"mutant {m['id']}: fails must list declared check ids")
        if isinstance(solution, dict):
            for step in steps:
                ref = solution.get(step, {}).get("patch") if isinstance(solution.get(step), dict) else None
                if isinstance(ref, str) and ref.strip() == str(m.get("patch", "")).strip():
                    issues.append(f"mutant {m['id']}: identical to the reference patch")
    issues += traps_mod.validate_all(spec.get("traps") or [], {c.get("id") for c in checks}, set(fixture), mutant_ids)
    issues += trap_shape_problems(spec.get("traps") or [], checks)
    issues += multi_turn_problems(spec, bundle, checks)
    for t in spec.get("traps") or []:
        for c in checks:
            if c.get("trap") == t.get("id") and c.get("id") not in (t.get("checks") or []):
                issues.append(f"check {c.get('id')!r} names trap {t.get('id')!r} but the trap does not list it")
    # provenance floor for trace-derived traps
    tier = (spec.get("source") or {}).get("tier")
    usable = spec.get("usable_corrections", 0) or 0
    if usable and tier:
        exploitation = traps_mod.trace_exploitation(spec.get("traps") or [], usable)
        floor = traps_mod.floor_for(tier)
        if exploitation + 1e-9 < floor:
            issues.append(f"traps cite {exploitation:.0%} of the {usable} usable corrections; tier {tier} "
                          f"requires {floor:.0%} -- turn the corrections into trace_derived traps first")
    issues += quality.quality_problems(spec)
    from .oracle import problems as oracle_problems
    issues += oracle_problems(spec, bundle)
    from .difficulty import hypothesis_evidence_problems
    issues += hypothesis_evidence_problems(spec, bundle)
    return issues


def validate_bundle(bundle: Path) -> list[str]:
    from .build import load_spec
    return validate(load_spec(bundle), bundle)
