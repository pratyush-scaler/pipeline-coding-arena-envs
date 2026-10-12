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


def audit(bundle: Path, *, regrade: bool = False, record: dict | None = None, write: bool = True) -> dict:
    """The certificate for every counted failure of the bundle's current screen."""
    from .author import sandbox
    bundle = Path(bundle).resolve()
    suites = _suites(bundle)
    published = _published(bundle)
    controls = _controls_pass(bundle)
    verifier_tag = None
    rows = []
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
    for entry in rows:
        if entry["verdict"] in ("void", "pass"):
            continue
        if entry["verdict"] != "capability":
            problems.append(f"{entry['model']} {entry['id']}: episode verdict {entry['verdict']}")
        for item in entry["failures"]:
            label = f"{entry['model'].split('/')[-1]} {entry['id']} {item['step']}/{item['check']}"
            if item["kind"] not in CERTIFIED_KINDS:
                problems.append(f"{label}: {item['kind']} -- {item['detail']}")
            if regrade and not item.get("reproduced"):
                problems.append(f"{label}: did not reproduce on re-grade")
            if item.get("check") and not item.get("published"):
                problems.append(f"{label}: the check names no published sentence found in the instruction")
        for note in entry.get("regrade_disagrees", []) + entry.get("regrade_errors", []):
            problems.append(f"{entry['model'].split('/')[-1]} {entry['id']}: {note}")
    problems += guardrail_share_problems(bundle, rows)
    from .artifacts import identity
    doc = {"schema": SCHEMA, "bundle": bundle.name, "artifact_sha256": identity(bundle),
           "regraded": regrade, "controls": controls, "episodes": rows,
           "counted_failures": sum(len(e["failures"]) for e in rows),
           "kinds": _kinds(rows), "problems": problems, "certified": not problems}
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
