"""Every failed tool call of a session, with what surrounded it, for the designer.

User, 2026-10-26: "while authoring and designing the task let's ensure the designer
has maximum amount of context on the failures in the traces and all the important
information from the traces should be sent to the designer. Not only for bash tasks
instead for both coding and bash_rle tasks."

Before this module the dump index only COUNTED failed shell calls (index.bash_failed)
and the designer saw each tool call clipped to 900 characters inside read_trace. The
bash RLE wave's trace leads (npm EACCES, a glob that matched nothing, pdfinfo exit
127) were found by hand and pasted into each row's operator_brief.

Arena's `bash_reliability` outcome names a node and an index that addresses Arena's
own representation, not the dump's content parts. Within one message the verdicts
follow the shell calls in order: on the 100-trace bash dump, of the 182 messages
whose verdict count equals their shell-call count, 159 agree call by call with the
exit codes (the rest are verdicts the metric judges on more than the exit code). So a
verdict is attached to a call only when the counts match; otherwise the message's
counts are reported as they are.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

SCHEMA = "arena-pipeline/trace-failures/1"
COMMAND_CHARS = 4000
STDERR_CHARS = 3000
STDOUT_CHARS = 1500
NEXT_CHARS = 400
USER_CHARS = 1200
BRIEF_ROWS = 14

#: First match wins. `prerequisite` classes are what Arena's bash_reliability_no_prereq
#: leaves out; they still make good setup material (a tool the trace's agent assumed).
CLASSES = (
    ("missing_tool", r"command not found|: not found\b|executable file not found"),
    ("missing_module", r"ModuleNotFoundError|No module named|Cannot find module|ERR_MODULE_NOT_FOUND|ImportError|"
                       r"cannot open shared object file|error while loading shared libraries"),
    ("permission", r"Permission denied|EACCES|Operation not permitted|EPERM\b|as root|Read-only file system|EROFS"),
    ("package_install", r"npm ERR|npm error|E: Unable to locate package|externally-managed-environment|"
                        r"Could not find a version|No matching distribution|Failed to install|E: Package"),
    ("network", r"Could not resolve|Temporary failure in name resolution|Connection refused|ECONNREFUSED|ENOTFOUND|"
                r"EAI_AGAIN|Network is unreachable|getaddrinfo|certificate verify failed|HTTP Error \d{3}|curl: \(\d+\)"),
    ("port_or_process", r"EADDRINUSE|address already in use|No such process|kill: \(\d+\)"),
    ("quoting_syntax", r"syntax error near|unexpected EOF|unexpected end of file|unterminated|bad substitution|"
                       r"unexpected token|here-document at line \d+ delimited by end-of-file|unmatched"),
    ("path_or_glob", r"No such file or directory|cannot access|cannot stat|cannot open|ENOENT|no matches found|"
                     r"FileNotFoundError|Not a directory|Is a directory"),
    ("test_failure", r"AssertionError|\bFAILED\b|tests? failed|# fail [1-9]|ERR_TEST_FAILURE|\b[1-9]\d* failed\b"),
    ("runtime_error", r"Traceback \(most recent call last\)|\w+Error:|Exception|panic:|Segmentation fault|core dumped"),
)
_CLASS_RE = [(name, re.compile(pattern)) for name, pattern in CLASSES]
PREREQUISITE = frozenset({"missing_tool", "missing_module", "package_install"})
KILLED_EXITS = frozenset({-1, 124, 137, 143})
#: The per-turn verdicts that reject a response (agent_designer.REJECTING_FEEDBACK); "approve" is not pushback.
REJECTING = ("edit", "disapprove", "escape")
_SEARCHERS = re.compile(r"(?:^|[|;&]\s*)(?:grep|egrep|fgrep|rg|ag|diff|cmp|test|\[)\b")
_PREFIX = re.compile(r"^(?:\s*(?:cd\s+\S+|set\s+-\S+|export\s+\S+|source\s+\S+)\s*(?:&&|;)\s*)+")
_ENV = re.compile(r"^(?:\s*(?:sudo|timeout\s+\S+|env|nohup|time|\w+=\S*)\s+)+")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"...[{len(text) - limit} chars before] " + text[-limit:]


def _head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f" ...[{len(text) - limit} more chars]"


def program(command: str) -> str:
    """The program a shell command runs first, past `cd X &&`, env assignments, sudo and timeout."""
    rest = _ENV.sub("", _PREFIX.sub("", command or "").lstrip("( "))
    word = rest.split(None, 1)[0] if rest.strip() else ""
    return word.rsplit("/", 1)[-1].strip("'\"();")


def classify(command: str, exit_code, stderr: str, stdout: str) -> str:
    text = f"{stderr}\n{stdout[-4000:]}"
    if exit_code == 127:
        return "missing_tool"
    if exit_code == 126:
        return "permission"
    for name, pattern in _CLASS_RE:
        if pattern.search(text):
            return name
    if exit_code in KILLED_EXITS or re.search(r"timed out|\bKilled\b", text):
        return "timeout_or_killed"
    if exit_code == 1 and not stderr.strip() and _SEARCHERS.search(command or ""):
        return "no_match"
    return "other"


def missing_program(command: str, stderr: str) -> str | None:
    found = re.search(r"(?:^|[\s:])([\w.+-]+): (?:command )?not found", stderr or "")
    return found.group(1) if found else (program(command) or None)


def _feedback(msg: dict) -> str | None:
    feedback = msg.get("feedback")
    verdict = feedback.get("check_in") if isinstance(feedback, dict) else None
    return verdict if verdict in REJECTING else None


def _message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return " ".join(_text(b.get("text")) for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _shell_calls(msg: dict) -> list[dict]:
    return [b for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool-bash"]


def _failed(block: dict) -> bool:
    output = block.get("output") if isinstance(block.get("output"), dict) else {}
    if block.get("type") == "tool-bash":
        return output.get("exit_code") not in (0, None) or block.get("state") == "output-error"
    return block.get("state") == "output-error" or bool(output.get("error")) or output.get("success") is False


def _arena_verdicts(messages: list[dict], outcomes: dict | None) -> tuple[dict, dict]:
    """(per-call verdicts {(msg, nth shell call): outcome}, per-message counts {msg: Counter})."""
    node_to_msg = {n: i for i, m in enumerate(messages) for n in (m.get("node_ids") or [])}
    per_msg: dict = defaultdict(list)
    for row in (outcomes or {}).get("outcomes") or []:
        if row.get("signal_name") != "bash_reliability":
            continue
        node, _, rest = str(row.get("observation_id") or "").partition("::")
        index = rest.split("::", 1)[0]
        if node in node_to_msg and index.lstrip("-").isdigit():
            per_msg[node_to_msg[node]].append((int(index), row.get("outcome")))
    calls, counts = {}, {}
    for i, rows in per_msg.items():
        rows.sort()
        counts[i] = Counter(outcome for _, outcome in rows)
        if len(rows) == len(_shell_calls(messages[i])):
            calls.update({(i, n): outcome for n, (_, outcome) in enumerate(rows)})
    return calls, counts


def _user_after(messages: list[dict], i: int, signals_at: dict) -> dict | None:
    for j in range(i + 1, min(len(messages), i + 4)):
        if messages[j].get("role") == "user":
            return {"msg": j, "text": _head(" ".join(_message_text(messages[j]).split()), USER_CHARS),
                    "signals": sorted(signals_at.get(j, ()))}
    return None


def _signals_at(messages: list[dict], outcomes: dict | None) -> dict:
    """{msg: {'steerability:failure', ...}} for the correction-bearing signals."""
    node_to_msg = {n: i for i, m in enumerate(messages) for n in (m.get("node_ids") or [])}
    found: dict = defaultdict(set)
    for row in (outcomes or {}).get("outcomes") or []:
        name, outcome = row.get("signal_name"), row.get("outcome")
        if name in ("steerability", "praise_complaint", "task_outcome_explicit") and outcome == "failure":
            node = str(row.get("observation_id") or row.get("annotating_node_id") or "").split("::")[0]
            if node in node_to_msg:
                found[node_to_msg[node]].add(f"{name}:{outcome}")
    return found


def digest(trace: dict, outcomes: dict | None = None, span: tuple[int, int] | list | None = None) -> dict:
    """Every failed tool call of the session, in order, with the seed span marked."""
    messages = trace.get("messages") or []
    lo, hi = (int(span[0]), int(span[-1])) if span else (0, max(len(messages) - 1, 0))
    arena_calls, arena_counts = _arena_verdicts(messages, outcomes)
    signals_at = _signals_at(messages, outcomes)
    failures, seen, streak, longest = [], Counter(), 0, 0
    shell_total = shell_failed = shell_failed_span = 0
    for i, msg in enumerate(messages):
        nth = -1
        blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)]
        for k, block in enumerate(blocks):
            kind = str(block.get("type") or "")
            if not kind.startswith("tool-"):
                continue
            is_shell = kind == "tool-bash"
            if is_shell:
                nth += 1
                shell_total += 1
            if not _failed(block):
                if is_shell:
                    streak = 0
                continue
            arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
            output = block.get("output") if isinstance(block.get("output"), dict) else {}
            command = _text(arguments.get("command")) if is_shell else repr(arguments)[:COMMAND_CHARS]
            stderr = _text(output.get("stderr")) or _text(output.get("error"))
            stdout = _text(output.get("stdout"))
            exit_code = output.get("exit_code") if is_shell else None
            cls = classify(command, exit_code, stderr, stdout) if is_shell else (
                "network" if kind == "tool-fetch_page" else "tool_error")
            key = " ".join(command.split())[:200]
            seen[key] += 1
            if is_shell:
                shell_failed += 1
                shell_failed_span += lo <= i <= hi
                streak += 1
                longest = max(longest, streak)
            following = next((b for b in blocks[k + 1:] if str(b.get("type") or "").startswith("tool-")), None)
            nxt = None
            if following is not None:
                nxt_in = following.get("input") if isinstance(following.get("input"), dict) else {}
                nxt = _head(_text(nxt_in.get("command")) or f"{following.get('type')} {nxt_in!r}", NEXT_CHARS)
            failures.append({
                "msg": i, "in_span": lo <= i <= hi, "tool": kind[5:], "class": cls,
                "prerequisite": cls in PREREQUISITE, "exit_code": exit_code,
                "program": (missing_program(command, stderr) if cls == "missing_tool" else program(command))
                           if is_shell else kind[5:],
                "command": _head(command, COMMAND_CHARS), "cwd": arguments.get("cwd"),
                "duration_ms": output.get("duration_ms"),
                "stderr": _tail(stderr, STDERR_CHARS), "stdout": _tail(stdout, STDOUT_CHARS),
                "truncated": bool(output.get("stdout_truncated") or output.get("stderr_truncated")),
                "arena_bash_reliability": arena_calls.get((i, nth)) if is_shell else None,
                "attempt": seen[key], "agent_next": nxt,
                "rejected_by_user": _feedback(msg),
                "user_next": _user_after(messages, i, signals_at),
            })
    arena = Counter()
    arena_span = Counter()
    for i, counts in arena_counts.items():
        arena.update(counts)
        if lo <= i <= hi:
            arena_span.update(counts)
    pushback = sum(1 for f in failures if f["rejected_by_user"] or (f["user_next"] and f["user_next"]["signals"]))
    return {
        "schema": SCHEMA, "span": [lo, hi],
        "summary": {
            "shell_calls": shell_total, "shell_failed": shell_failed, "shell_failed_in_span": shell_failed_span,
            "tool_errors": sum(1 for f in failures if f["tool"] != "bash"),
            "by_class": dict(Counter(f["class"] for f in failures).most_common()),
            "by_class_in_span": dict(Counter(f["class"] for f in failures if f["in_span"]).most_common()),
            "prerequisite_failures": sum(f["prerequisite"] for f in failures),
            "arena_bash_reliability": dict(arena), "arena_bash_reliability_in_span": dict(arena_span),
            "repeated": sum(1 for f in failures if f["attempt"] > 1),
            "followed_by_pushback": pushback, "longest_failed_streak": longest,
            "missing_programs": sorted({f["program"] for f in failures if f["class"] == "missing_tool" and f["program"]}),
        },
        "failures": failures,
    }


def _priority(row: dict, first_of_class: bool) -> float:
    return (3.0 * (row["arena_bash_reliability"] == "failure") + 2.0 * row["in_span"]
            + 2.0 * bool(row["rejected_by_user"] or (row["user_next"] and row["user_next"]["signals"]))
            + 1.5 * (row["attempt"] > 1)
            + 1.0 * first_of_class - 2.0 * (row["class"] == "no_match") - 0.5 * row["prerequisite"])


def ranked(result: dict) -> list[dict]:
    """Failures ordered by what they offer a designer: Arena-flagged, in the seed, pushed back on, repeated,
    and one of each class before a second of any."""
    seen, scored = set(), []
    for n, row in enumerate(result["failures"]):
        first = row["class"] not in seen
        seen.add(row["class"])
        scored.append((-_priority(row, first), n, row))
    return [row for *_, row in sorted(scored)]


def _one_line(row: dict) -> str:
    error = next((line.strip() for line in reversed((row["stderr"] or row["stdout"]).splitlines()) if line.strip()), "")
    tags = [row["class"], f"exit {row['exit_code']}" if row["exit_code"] is not None else row["tool"]]
    if row["arena_bash_reliability"]:
        tags.append(f"Arena {row['arena_bash_reliability']}")
    if row["attempt"] > 1:
        tags.append(f"attempt {row['attempt']}")
    if not row["in_span"]:
        tags.append("outside the seed")
    line = f"  - msg {row['msg']} [{', '.join(tags)}] $ {' '.join(row['command'].split())[:240]}"
    if error:
        line += f" -> {error[:200]}"
    if row["agent_next"]:
        line += f"; next: {' '.join(row['agent_next'].split())[:140]}"
    user = row["user_next"]
    if user and (user["signals"] or row["rejected_by_user"]):
        why = ", ".join(user["signals"] + ([f"check-in {row['rejected_by_user']}"] if row["rejected_by_user"] else []))
        line += f"; user next (msg {user['msg']}, {why}): {user['text'][:200]}"
    return line


def brief(result: dict, rows: int = BRIEF_ROWS) -> list[str]:
    """The opening-message digest: the counts, then the most informative failures, one line each."""
    s = result["summary"]
    if not result["failures"]:
        return [f"TRACE FAILURE DIGEST: {s['shell_calls']} shell call(s), none failed; no tool call returned an error."]
    lines = [
        "TRACE FAILURE DIGEST (every failed tool call of this session, computed from the trace; read_failures "
        "returns each in full -- command, stderr, stdout, what the agent did next, what the user said next):",
        f"  shell calls {s['shell_calls']}, failed {s['shell_failed']} ({s['shell_failed_in_span']} inside the seed "
        f"span {result['span'][0]}-{result['span'][1]}), other tool errors {s['tool_errors']}; by class "
        f"{s['by_class']}; Arena bash_reliability {s['arena_bash_reliability'] or 'not scored'}; prerequisite "
        f"failures (Arena's metric leaves these out) {s['prerequisite_failures']}; same command failed again "
        f"{s['repeated']}; failures the user then rejected or corrected {s['followed_by_pushback']}; longest run of failed "
        f"shell calls {s['longest_failed_streak']}.",
    ]
    if s["missing_programs"]:
        lines.append(f"  programs the trace's agent reached for that were not installed: {s['missing_programs']}")
    lines += [_one_line(row) for row in ranked(result)[:rows]]
    if len(result["failures"]) > rows:
        lines.append(f"  ... {len(result['failures']) - rows} more: read_failures(offset={rows})")
    lines.append("  A command the trace's agent got wrong is a decision the next model faces too: reproduce it in "
                 "the sandbox, then build hidden cases where the published rule meets that failure.")
    return lines


def session_lines(trace: dict, outcomes: dict | None) -> list[str]:
    """Arena's signals and the user's check-ins for the whole session, by message: pushed into the opening so the
    designer starts from them (read_signals and list_corrections still return the detail)."""
    from ..config import DEAD_SIGNALS
    messages = trace.get("messages") or []
    node_to_msg = {n: i for i, m in enumerate(messages) for n in (m.get("node_ids") or [])}
    counts: dict = defaultdict(Counter)
    where: dict = defaultdict(set)
    for row in (outcomes or {}).get("outcomes") or []:
        name, outcome = row.get("signal_name"), row.get("outcome")
        if not name or name in DEAD_SIGNALS:
            continue
        counts[name][outcome] += 1
        node = str(row.get("observation_id") or row.get("annotating_node_id") or "").split("::")[0]
        if outcome == "failure" and node in node_to_msg:
            where[name].add(node_to_msg[node])
    lines = []
    if counts:
        parts = []
        for name in sorted(counts):
            c = counts[name]
            msgs = sorted(where[name])
            parts.append(f"{name} " + ", ".join(f"{k} {v}" for k, v in c.most_common())
                         + (f" (failures at msgs {msgs[:12]}{' ...' if len(msgs) > 12 else ''})" if msgs else ""))
        lines.append("ARENA SIGNALS for this session (discovery aids, never the reward; read_signals has every row): "
                     + "; ".join(parts) + ".")
    verdicts: dict = defaultdict(list)
    for i, msg in enumerate(messages):
        feedback = msg.get("feedback")
        verdict = feedback.get("check_in") if isinstance(feedback, dict) else None
        if verdict:
            verdicts[verdict].append(i)
    undone = [i for i, m in enumerate(messages) if m.get("undone") is True]
    errored = [i for i, m in enumerate(messages) if m.get("error")]
    if verdicts or undone or errored:
        parts = [f"{v} at msgs {sorted(ix)[:12]}" for v, ix in sorted(verdicts.items())]
        if undone:
            parts.append(f"undone msgs {undone[:12]}")
        if errored:
            parts.append(f"errored msgs {errored[:12]}")
        lines.append("USER CHECK-INS on the agent's responses (list_corrections quotes the rejected ones with what the "
                     "user said next): " + "; ".join(parts) + ".")
    return lines
