"""Why the models failed: the failure-cause columns Arena asked for on the cover sheet.

Arena, after the Oct 12 review: "In the cover sheet, can you add columns for explaining why the
models failed? This will help both of us to keep a track on diversity around failure causes."

Derived, never typed: the genuine-failure audit (verification/genuine.json) names, per valid
episode, every check it failed, the failure kind (assertion, crash, contract ...) and the
instruction sentence(s) the check enforces; the spec links each check to its primary trap, whose
type and `beatable_by` say which reading the task rewards. The calibration's learning signal
(pipeline.learning_signal) adds the grader shape.

User, 2026-10-21, after the external audit of Batch 3: the write-ups blamed each failure on the
check's trap label, not on what failed. c-game-bot's one o_bogus failure was put on the thrust-step
trap while its failing assertion was fuel formatting (('BOGUS', 6, 887.0) == ('BOGUS', 6, 887.5));
school-scheduling's o_class_busy was put on period labels while every run's failing test was
test_class_busy_rows.js, not test_class_busy.js. Each failed check is now attributed, per run, to the
test and assertion that actually failed -- read from the run's own grade record
(steps/<step>/verifier/arena-grade.json under the campaign) or, where the grader kept no output
(node's JUnit-only runs), from the audit's re-run detail -- runs are grouped by that test, and the trap
label is kept as secondary context. When every failure of a check comes from one test the label is
confirmed, contradicted (the test belongs to another trap: another check selects it, or another trap's
record names it) or left unconfirmed (the check's other tests never failed), and the write-up says which.

    report(package) -> dict      write(package) -> FAILURES.md + verification/failure-causes.json
    cover_row(doc)  -> the cover-sheet columns for one task
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

#: /2 (2026-10-21): each check row adds failing_tests, cause and label; every /1 key is kept.
SCHEMA = "arena-coding/failure-causes/2"
MARKDOWN = "FAILURES.md"
RECEIPT = "verification/failure-causes.json"


def _read(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- the test and assertion that failed, from a runner's own output ---------------------------------------------

_PYTEST_SUMMARY = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - (.*))?$")
_PYTEST_SECTION = re.compile(r"^_{3,} (?:ERROR at \w+ of )?(\S.*?) _{3,}$")
_NODE_FAILED = re.compile(r"^(?:✖|✕|×)\s+(.+?)(?:\s+\(\d[\d.]*\s*m?s\))?$")
_TAP_FAILED = re.compile(r"^not ok \d+(?: -)?\s+(.+?)(?:\s+#.*)?$")
_GO_FAILED = re.compile(r"^--- FAIL: (\S+)")
_RAISED = re.compile(r"^(?:AssertionError|assert\b|[\w.]*(?:Error|Exception)\b)")
_NOISE = re.compile(r"^(?:Use -v to get more diff|At index \d+ diff|Full diff:|Omitting \d+ identical|\+|-|\?|$)")


def _assertion(lines: list[str]) -> str | None:
    """The first line of a failure that says what was asserted or raised."""
    kept = [line.strip() for line in lines if line.strip() and not _NOISE.match(line.strip())]
    pick = next((line for line in kept if _RAISED.match(line)), kept[0] if kept else None)
    return pick[:240] if pick else None


def failed_tests(text: str) -> tuple[list[dict], bool]:
    """([{"test", "assertion"}], complete) for one failed check's retained output. `test` is a pytest node id, a node
    or TAP title, a Go test name, or None (a command runner names no test). `complete` is True only when the runner's
    own summary lists every failure (pytest's short test summary); a tail or a re-run excerpt may omit some."""
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    out: list[dict] = []
    # pytest: the FAILED summary names every failed node; its section's `E` lines carry the assertion.
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        head = _PYTEST_SECTION.match(line.strip())
        if head:
            current = head.group(1)
            sections.setdefault(current, [])
        elif current is not None and line.startswith("E "):
            sections[current].append(line[1:])
    summary = [m for m in (_PYTEST_SUMMARY.match(line.strip()) for line in lines) if m]
    for found in summary:
        node = found.group(1)
        tail = node.split("::")[-1]
        section = sections.get(tail) or next((v for k, v in sections.items() if k.replace(".", "::").endswith(tail)), [])
        out.append({"test": node, "assertion": _assertion(section) or _assertion([found.group(2) or ""])})
    if not summary:
        out += [{"test": name, "assertion": _assertion(body)} for name, body in sections.items()]
    # node --test (spec reporter), TAP, Go: the title line, then the error lines under it.
    for n, line in enumerate(lines):
        stripped = line.strip()
        found = _NODE_FAILED.match(stripped) or _TAP_FAILED.match(stripped) or _GO_FAILED.match(stripped)
        if not found or found.group(1).rstrip(":") in ("failing tests", "failing test"):
            continue
        after = []
        for nxt in lines[n + 1:n + 12]:
            if _NODE_FAILED.match(nxt.strip()) or _TAP_FAILED.match(nxt.strip()) or _GO_FAILED.match(nxt.strip()):
                break
            after.append(re.sub(r"^\s*\S+_test\.go:\d+:\s*", "", nxt))
        out.append({"test": found.group(1), "assertion": _assertion(after)})
    if not out:
        raised = _assertion([line for line in lines if _RAISED.match(line.strip())])
        if raised:
            out.append({"test": None, "assertion": raised})
    unique, seen = [], set()
    for item in out:
        key = item["test"] or item["assertion"]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique, any("short test summary info" in line for line in lines)


def _audit_text(detail: str) -> str:
    """The runner excerpt inside a genuine.json failure detail ("<kind text>: line | line")."""
    _, sep, rest = str(detail or "").partition(": ")
    return "\n".join(part for part in (rest if sep else str(detail or "")).split(" | "))


# --- where the per-run grade records are ------------------------------------------------------------------------

def _campaign_dirs(roots) -> list[Path]:
    """The calibration campaigns a delivered package carries (runs/index.json) or a released version names
    (verification/native-calibrations.json, top-ups included); never the oracle's. One entry per campaign name."""
    found: dict[str, Path] = {}
    for root in roots:
        root = Path(root)
        index = _read(root / "runs/index.json") or {}
        paths = [root / c["path"] for c in index.get("campaigns") or []
                 if isinstance(c, dict) and c.get("path") and c.get("label") != "reference" and not c.get("missing")]
        native = _read(root / "verification/native-calibrations.json") or {}
        for primary in native.get("campaigns") or []:
            paths += [Path(p) for p in [primary, *((native.get("topups") or {}).get(str(primary)) or [])]]
        for path in paths:
            if path.name not in found and path.is_dir():
                found[path.name] = path
    return list(found.values())


def _trial_dirs(campaigns: list[Path]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for campaign in campaigns:
        for trial in sorted((campaign / "jobs").glob("*/*")):
            if (trial / "steps").is_dir():
                out.setdefault(trial.name, trial)
    return out


def _suite_text(roots, name: str) -> str | None:
    for root in roots:
        path = Path(root) / "tests/suites" / name
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _selected(check: dict) -> list[str]:
    runner = check.get("runner") if isinstance(check.get("runner"), dict) else {}
    return [str(s) for s in runner.get("select") or [] if isinstance(s, str)]


def _locate(test: str | None, check: dict, roots) -> tuple[str | None, str | None]:
    """(display id, suite file) of a failing test: a pytest node id carries its file; a node/TAP title is found in
    the one selected suite file whose source holds it."""
    if not test:
        return None, None
    if "::" in test:
        return test, test.split("::")[0].split("/")[-1]
    files = [s for s in _selected(check) if "::" not in s]
    holding = [f for f in files if (_suite_text(roots, f) or "").find(test) >= 0]
    if len(holding) == 1:
        return f"{holding[0]}::{test}", holding[0]
    return test, None


def _selects(check: dict, test_id: str, test_file: str | None) -> bool:
    """Does the check run this test (a node id, or a whole suite file when `test_id` is the file)?"""
    for item in _selected(check):
        same_file = bool(test_file) and item.split("::")[0].split("/")[-1] == test_file
        if item == test_id or (same_file and ("::" not in item or "::" not in test_id)):
            return True
    return False


_TRAP_TEXT = ("id", "beatable_by", "grounding", "surface", "misreading", "quote", "research", "grounded_in")


def _owners(test_id: str, test_file: str | None, cid: str, checks: dict, traps: dict) -> set[str]:
    """Traps a failing test demonstrably belongs to: the primary trap of another objective check that selects it,
    or a trap whose own record names the test function or its file."""
    owners = set()
    for other_id, other in checks.items():
        if other_id == cid or other.get("kind") != "objective" or not _selects(other, test_id, test_file):
            continue
        trap = _primary(other)
        if trap:
            owners.add(trap)
    names = {test_id.split("::")[-1].split("[")[0]} if "::" in test_id else set()
    if test_file:
        names.add(Path(test_file).stem)
    names = {n for n in names if re.fullmatch(r"test\w+|\w+_test|\w+\.test", n)}
    for tid, trap in traps.items():
        text = json.dumps({k: trap.get(k) for k in _TRAP_TEXT}, ensure_ascii=False)
        if any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", text) for n in names):
            owners.add(tid)
    return owners


def _primary(check: dict) -> str | None:
    trap = check.get("trap")
    return (trap[0] if trap else None) if isinstance(trap, list) else trap


def _runs_text(counts: dict) -> str:
    return ", ".join(f"{m.split('/')[-1]} {n}" for m, n in counts.items())


# --- the report ---------------------------------------------------------------------------------------------------

def report(package: Path, signal: dict | None = None, grades_from=None) -> dict:
    """`grades_from`: roots searched for per-run grade records and suite files (default: `package`)."""
    package = Path(package)
    roots = [Path(r) for r in (grades_from or [package])]
    if package not in roots:
        roots.append(package)
    spec = _read(package / "authoring/spec.json") or {}
    audit = _read(package / "verification/genuine.json") or {}
    checks = {c.get("id"): c for c in spec.get("checks") or [] if isinstance(c, dict)}
    traps = {t.get("id"): t for t in spec.get("traps") or [] if isinstance(t, dict)}
    trials = _trial_dirs(_campaign_dirs(roots))
    grades: dict[tuple, dict | None] = {}
    models: dict[str, dict] = defaultdict(lambda: {"episodes": 0, "failed_episodes": 0, "kinds": Counter()})
    per_check: dict[str, dict] = {}
    for ep in audit.get("episodes") or []:
        if not ep.get("valid") or ep.get("verdict") == "void":
            continue
        model = ep.get("model") or "?"
        m = models[model]
        m["episodes"] += 1
        fails = ep.get("failures") or []
        if fails:
            m["failed_episodes"] += 1
        for f in fails:
            cid = f.get("check")
            m["kinds"][f.get("kind") or "unknown"] += 1
            row = per_check.setdefault(cid, {"check": cid, "step": f.get("step"), "by_model": Counter(), "kinds": Counter(),
                                             "published": f.get("published") or [], "tests": {}, "unread": Counter(),
                                             "complete": True})
            row["by_model"][model] += 1
            row["kinds"][f.get("kind") or "unknown"] += 1
            # The test that failed in THIS run: the run's own grade record, else the audit's re-run excerpt.
            key = (ep.get("id"), f.get("step"))
            if key not in grades:
                trial = trials.get(str(ep.get("id")))
                grades[key] = _read(trial / "steps" / str(f.get("step")) / "verifier/arena-grade.json") if trial else None
            record = ((grades[key] if isinstance(grades[key], dict) else {}).get("checks") or {}).get(cid)
            record = record if isinstance(record, dict) else {}
            output = f"{record.get('stdout') or ''}\n{record.get('stderr') or ''}"
            source = "grade"
            found, complete = failed_tests(output) if output.strip() else ([], False)
            if not found:
                source = "audit"
                found, complete = failed_tests(_audit_text(f.get("detail")))
            row["complete"] = row["complete"] and complete
            if not found:
                row["unread"][model] += 1
                continue
            for item in found:
                test_id, test_file = _locate(item["test"], checks.get(cid) or {}, roots)
                sig = test_id or f"assertion:{item['assertion']}"
                cell = row["tests"].setdefault(sig, {"test": test_id, "file": test_file, "runs": Counter(), "trials": [],
                                                     "assertions": Counter(), "source": source})
                cell["runs"][model] += 1
                cell["trials"].append(str(ep.get("id")))
                if item["assertion"]:
                    cell["assertions"][item["assertion"]] += 1
    rows = []
    for cid, row in per_check.items():
        c = checks.get(cid) or {}
        trap_id = _primary(c)
        t = traps.get(trap_id) or {}
        tests = sorted(row["tests"].values(), key=lambda x: -sum(x["runs"].values()))
        failing = [{"test": x["test"], "file": x["file"], "runs": dict(x["runs"]), "trials": x["trials"],
                    "assertion": x["assertions"].most_common(1)[0][0] if x["assertions"] else None,
                    "assertions": [a for a, _ in x["assertions"].most_common(3)], "source": x["source"]} for x in tests]
        label = _label(cid, c, trap_id, t, failing, row, checks, traps)
        rows.append({"check": cid, "step": row["step"], "kind_of_check": c.get("kind"), "weight": c.get("weight"),
                     "failed_runs": dict(row["by_model"]), "failure_kinds": dict(row["kinds"]),
                     "trap": trap_id, "trap_type": t.get("type"),
                     "what_the_task_rewards": t.get("beatable_by"), "why_it_misleads": t.get("grounding"),
                     "instruction_sentences": row["published"][:3],
                     "failing_tests": failing, "unattributed_runs": dict(row["unread"]),
                     "records_complete": bool(row["complete"] and failing),
                     "cause": _cause(failing, dict(row["by_model"])), "label": label})
    rows.sort(key=lambda r: (-sum(r["failed_runs"].values()), -(r["weight"] or 0)))
    type_counts = Counter()
    for r in rows:
        owners = (r["label"] or {}).get("owner_traps") or []
        kind = (traps.get(owners[0]) or {}).get("type") if r["label"]["status"] == "different_trap" and len(owners) == 1 \
            else r["trap_type"]
        if kind:
            type_counts[kind] += sum(r["failed_runs"].values())
    return {"schema": SCHEMA, "task": package.name,
            "models": {k: {"valid_episodes": v["episodes"], "failed_episodes": v["failed_episodes"],
                           "failure_kinds": dict(v["kinds"])} for k, v in models.items()},
            "checks": rows, "failure_trap_types": dict(type_counts.most_common()),
            "signal": signal,
            "source": "verification/genuine.json + per-run verifier/arena-grade.json + authoring/spec.json traps"}


def _cause(failing: list[dict], failed_runs: dict) -> str | None:
    """What failed, grouped by test: the primary description of a failed check."""
    if not failing:
        return None
    total = sum(failed_runs.values())
    parts = []
    for x in failing[:3]:
        n = sum(x["runs"].values())
        what = f"`{x['test']}`" if x["test"] else "the check's run"
        share = "every failing run" if n == total else f"{n} of {total} failing runs"
        parts.append(f"{what} in {share} ({_runs_text(x['runs'])})" + (f": {x['assertion']}" if x["assertion"] else ""))
    return "; ".join(parts) + (f"; and {len(failing) - 3} more test(s)" if len(failing) > 3 else "")


def _label(cid: str, check: dict, trap_id, trap: dict, failing: list[dict], row: dict, checks: dict, traps: dict) -> dict:
    """The trap label against what failed, when every failing run of the check failed one test (or one suite file):
    confirmed, different_trap or unconfirmed; otherwise several_tests, single_test (the check is that one test) or
    unattributed (some failing run's record names no test)."""
    out = {"trap": trap_id, "type": trap.get("type"), "status": "unattributed", "owner_traps": [], "note": None,
           "short": None}
    total = sum(row["by_model"].values())
    if not failing or sum(row["unread"].values()) or len({t for x in failing for t in x["trials"]}) < total:
        return out
    files = {x["file"] for x in failing}
    if len(failing) == 1 and failing[0]["test"]:
        unit, unit_file, whole_file = failing[0]["test"], failing[0]["file"], False
    elif len(files) == 1 and None not in files:
        unit = unit_file = next(iter(files))
        whole_file = True
    else:
        out["status"] = "several_tests" if len(failing) > 1 else "single_test"
        return out
    owners = _owners(unit, unit_file, cid, checks, traps)

    def covers(item: str) -> bool:
        in_file = bool(unit_file) and item.split("::")[0].split("/")[-1] == unit_file
        return in_file if whole_file else (item == unit or (in_file and "::" not in item))

    others = [s for s in _selected(check) if not covers(s)]
    tag = f"{trap_id or 'none'} ({trap.get('type') or 'untyped'})"
    if trap_id and trap_id in owners:
        out.update(status="confirmed", owner_traps=[trap_id])
    elif owners:
        names = ", ".join(f"{o} ({(traps.get(o) or {}).get('type') or 'untyped'})" for o in sorted(owners))
        out.update(status="different_trap", owner_traps=sorted(owners),
                   note=f"every failure of {cid} comes from `{unit}`, which belongs to {names}, not to the check's "
                        f"label {tag}", short=f"the failing test belongs to {names}, not the label {tag}")
    elif others:
        listed = ", ".join(f"`{o}`" for o in others[:4])
        never = ("never failed" if row["complete"] else
                 f"{'was' if len(others) == 1 else 'were'} never named failing in any run's record")
        out.update(status="unconfirmed",
                   note=f"every failure of {cid} comes from `{unit}`; {listed} {never}, so the check's label {tag} is "
                        "not confirmed by what failed",
                   short=f"its other test(s) {'never failed' if row['complete'] else 'never named failing'}; label {tag} "
                         "not confirmed by what failed")
    else:
        out["status"] = "single_test"
    return out


def cover_row(doc: dict) -> dict:
    """The cover-sheet columns: decisive trap type(s), the leading failure causes, how they failed."""
    rows = doc.get("checks") or []
    lead = rows[:3]

    def cause(r):
        runs = ", ".join(f"{m.split('/')[-1]} {n}" for m, n in r["failed_runs"].items())
        why = (r.get("what_the_task_rewards") or r.get("why_it_misleads") or "").strip().rstrip(".")
        head = f"{r['check']} [{r.get('trap_type') or 'untyped'}; failed {runs}]"
        what = (r.get("cause") or "").strip()
        if not what:
            return head + (f": {why[:220]}" if why else "")
        note = (r.get("label") or {}).get("short")
        return head + f": {what[:260]}" + (f" -- {note[:160]}" if note else f" (trap label: {why[:120]})" if why else "")

    kinds = Counter()
    for m in (doc.get("models") or {}).values():
        kinds.update(m.get("failure_kinds") or {})
    signal = doc.get("signal") or {}
    return {"decisive_trap_types": list((doc.get("failure_trap_types") or {}).keys())[:3],
            "why_models_failed": [cause(r) for r in lead],
            "failure_kinds": dict(kinds.most_common()),
            "independent_hard_decisions": signal.get("independent_hard"),
            "free_weight_share": signal.get("free_weight_share"),
            "reward_spread": {m.split("/")[-1]: (s or {}).get("stdev") for m, s in (signal.get("spread") or {}).items()},
            "independent_hard_before_lockstep": signal.get("independent_hard_before_lockstep"),
            "lockstep_groups": signal.get("lockstep_groups"),
            "decisions_losing_reward": signal.get("decisions_losing_reward"),
            "single_decision": (signal.get("single_decision") or {}).get("checks"),
            "single_decision_share": signal.get("single_decision_share"),
            "label_notes": [f"{r['check']}: {r['label']['note']}" for r in rows if (r.get("label") or {}).get("note")]}


def markdown(doc: dict) -> str:
    row = cover_row(doc)
    signal = doc.get("signal") or {}
    lines = ["# Why the models failed", "",
             "Derived from the genuine-failure audit of the shipped calibration, each run's own grade record (the test "
             "and assertion that failed) and the spec's trap records; nothing here is typed by hand "
             "(`pipeline/failure_report.py`). The trap label is context: what failed is the test named.", ""]
    if row["decisive_trap_types"]:
        lines += [f"- **Decisive trap type(s):** {', '.join(row['decisive_trap_types'])}"]
    if row["independent_hard_decisions"] is not None:
        before = row["independent_hard_before_lockstep"]
        counted = (f" ({before} counted per trap, before checks that pass and fail together in every run were counted "
                   "once)") if before is not None and before != row["independent_hard_decisions"] else ""
        lines += [f"- **Independent hard decisions:** {row['independent_hard_decisions']}{counted}; "
                  f"**free weight:** {row['free_weight_share']}; **reward spread (stdev):** {row['reward_spread']}"]
    for group in row["lockstep_groups"] or []:
        lines += [f"- **Pass and fail together in every run (one decision):** "
                  f"{', '.join('`' + g.rpartition('/')[2] + '`' for g in group)}"]
    if row["single_decision_share"] is not None:
        cap = signal.get("single_decision_share_max")
        names = ", ".join(f"`{c.rpartition('/')[2]}`" for c in row["single_decision"] or [])
        lines += [f"- **Largest single decision:** {names} carries {row['single_decision_share']:.0%} of all the reward "
                  "the calibration lost" + (f" (at most {cap:.0%})" if isinstance(cap, (int, float)) else "")
                  + (f"; {row['decisions_losing_reward']} decision(s) lost any reward"
                     if row["decisions_losing_reward"] else "")]
    lines += [f"- **Failure kinds:** {row['failure_kinds']}", "", "| model | valid runs | runs with a failure |",
              "|---|---:|---:|"]
    for m, v in (doc.get("models") or {}).items():
        lines.append(f"| {m.split('/')[-1]} | {v['valid_episodes']} | {v['failed_episodes']} |")
    esc = lambda s, n=260: str(s or "").replace("|", "\\|").replace("\n", " ")[:n]
    lines += ["", "| check | failed runs | what failed (test: assertion) | trap label (context): what the task rewards | "
                  "instruction sentence |", "|---|---|---|---|---|"]
    for r in doc.get("checks") or []:
        runs = ", ".join(f"{m.split('/')[-1]} {n}" for m, n in r["failed_runs"].items())
        failed = "; ".join(f"`{x['test'] or r['check']}` ({_runs_text(x['runs'])})"
                           + (f": {x['assertion']}" if x["assertion"] else "")
                           for x in (r.get("failing_tests") or [])[:3]) or "not readable from the run records"
        label = f"{r.get('trap_type') or ''} {r.get('trap') or ''}: {r.get('what_the_task_rewards') or ''}".strip(" :")
        lines.append(f"| `{r['check']}` | {runs} | {esc(failed, 420)} | {esc(label)} | "
                     f"{esc((r.get('instruction_sentences') or [''])[0])} |")
    if row["label_notes"]:
        lines += ["", "The trap labels against what failed:", ""]
        lines += [f"- {esc(n, 600)}" for n in row["label_notes"]]
    return "\n".join(lines) + "\n"


def _signal_from_runs(package: Path, source: Path) -> dict | None:
    """The learning signal recomputed from the campaigns the package carries, for a release whose own signal predates
    lockstep collapse (user, 2026-10-21)."""
    from . import learning_signal
    spec = _read(source / "authoring/spec.json") or _read(package / "authoring/spec.json")
    campaigns = _campaign_dirs([package, source])
    if not spec or not campaigns:
        return None
    try:
        signal = learning_signal.from_campaigns(spec, campaigns)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    signal["problems"] = learning_signal.problems(signal)
    signal["advisories"] = learning_signal.advisories(signal)
    signal["recomputed_from"] = "the delivered calibration runs (the release signal predates lockstep collapse)"
    return signal


def write(package: Path, signal: dict | None = None, source: Path | None = None) -> dict:
    """Write into `package`, reading the audit, spec and release receipt from `source` (the released
    version) when the delivered copy does not carry them."""
    package = Path(package)
    source = Path(source) if source else package
    if signal is None:
        rel = _read(source / "verification/shipfix-release.json") or {}
        signal = (((rel.get("steps") or {}).get("hardness") or {}).get("result") or {}).get("signal")
        # A /1 release signal counts lockstep checks apart (c-game-bot: 5 for 2); recount from the runs shipped.
        if not (isinstance(signal, dict) and "single_decision_share" in signal):
            signal = _signal_from_runs(package, source) or signal
    doc = report(source if (source / "verification/genuine.json").is_file() else package, signal,
                 grades_from=[package, source])
    doc["task"] = package.name
    (package / "verification").mkdir(exist_ok=True)
    (package / RECEIPT).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (package / MARKDOWN).write_text(markdown(doc), encoding="utf-8")
    return doc


if __name__ == "__main__":
    import sys
    print(json.dumps(cover_row(write(Path(sys.argv[1]))), indent=1, ensure_ascii=False))
