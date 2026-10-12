"""Coverage critic: wrong implementations written on purpose, graded by the shipped grader.

Arena's review (2026-10-05) found implementations that break a published rule
and still score full marks:

    C-006 shopify   exact `Metal` first, else any option containing "metal"   1.0
    C-013 glb       the old skip list kept for node_modules and .git           1.0
    C-024 date-form U+00AD stripped before logs are compared                  10/10 on the suite

Each clause was published. No hidden test exercised it, and neither our review
nor a blind solve found the gap, because nobody tried to write the wrong
implementation on purpose. The critic does that. A model reads the instruction,
the checks, the hidden suites and the reference. It proposes counter-
implementations: each one is an edit on top of the reference that violates
exactly one published clause. Every proposal is then EXECUTED -- applied to the
reference in the agent image and graded by the shipped grader. A proposal is a
gap only when its step scores 1.0. The model's opinion is never a finding.

A gap's fix is hidden-only: a case for the clause, the proposal kept as an
`m_partial_` mutant, and a determinacy probe on the exact input, so the new case
enforces what a careful reader of the instruction would do (`harden --fast
--fix`). Adding a case can only keep or lower pass rates.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCHEMA = "arena-coding/coverage-critic/1"
RECEIPT = "verification/coverage-critic.json"
MAX_PROPOSALS = 6
SUITE_FILE_CAP = 60_000
SUITE_TOTAL_CAP = 260_000
TOUCHED_FILE_CAP = 90_000

#: Characters of instruction-named, untouched repository files shown to the critic.
NAMED_TOTAL_CAP = 60_000

PROMPT = """\
You audit the hidden tests of a coding benchmark task for COVERAGE GAPS: a published rule \
whose violation no hidden test detects. Your job is to write wrong implementations on purpose.

You are given the instruction exactly as the solver sees it, the graded checks (each names \
the instruction sentence it enforces), the full hidden test suites, the reference fix with \
the reference version of every file it touches, and the reference version of every other \
repository file the instruction names.

Propose up to {limit} COUNTER-IMPLEMENTATIONS. Each must:
- start from the REFERENCE version of the files shown (touched by the fix OR named by the \
instruction) and change as little as possible;
- violate exactly ONE clause the instruction publishes (quote that clause verbatim); a careful \
human who read the instruction would agree the behaviour is wrong;
- plausibly pass every hidden test shown -- target clauses, enumerated items, boundaries, \
fallbacks, character classes, folder/file-name cases and option shapes that the suites never \
exercise. Read the suites carefully: a proposal an existing test obviously catches is wasted.
- be a realistic mistake a strong engineer might make (a fallback, a kept special case, an \
over-normalisation, a partial list), not sabotage.

Hunt these gap classes first -- each one was found in delivered tasks by the client's reviewers \
after our own checks passed:
- a published artifact the tests only check EXISTS or never RUN (a script, tool, command, \
exported or template-called function, formatter): break, stub or empty it;
- a rule stated for several cases or both directions but tested for one (effects on in one mode \
and off in another; hurt AND game over; positive and negative cases): violate the untested one;
- an alternate path the same rule governs (a backup service, a fallback branch, a second \
database or backend, an error/retry path): violate the rule only there;
- state that must stay current while the program keeps running (a rescan per request, \
persistence right after an action, a second request on the same server): cache or delay it;
- an all-or-nothing rule tested on a few fields ('nothing changes', atomic refusal): change a \
column, record or file the tests do not read;
- content checked only by structure, counts or presence (numbers present but values not \
compared; text, options or answers not compared): get the content wrong;
- a file or interface the instruction says to leave as it is: change it;
- functions that must each work on their own but are always called in one order: make one \
depend on another having run first;
- repeated actions (the same order twice, re-entry, a second click): double-count;
- inputs the instruction allows that the tests never use (zero, empty, missing optional parts, \
none of the extras present): mishandle them;
- a hidden harness that imports the code into a worker process and then serialises or writes \
the results there with library functions the code can replace at import time (json.dumps, \
print, sys.stdout.write, os.write): replace one at module scope so the worker reports correct \
results while the real function is wrong (a report forgery, not a behaviour change).

Do not propose anything that relies on reading the hidden tests or the grader.

Return ONLY one JSON object in a ```json fenced block:
{{"proposals": [{{"clause": "<verbatim quote from the instruction>",
  "violation": "<what this implementation does wrong>",
  "why_uncaught": "<which suite case would be needed and why none exists>",
  "edits": [{{"path": "<repo-relative path>", "old": "<exact text in the reference file, \
unique in that file>", "new": "<replacement>"}}]}}]}}

INSTRUCTION
-----------
{instruction}

CHECKS (id | kind | the sentence it enforces)
------
{checks}

REFERENCE FIX (unified diff over the starting repository)
-------------
{reference}

REFERENCE VERSIONS OF THE FILES THE FIX TOUCHES
-----------------------------------------------
{touched}

REFERENCE VERSIONS OF OTHER FILES THE INSTRUCTION NAMES
-------------------------------------------------------
{named}

DECLARED UNDETERMINED (a determinacy probe showed the instruction does not settle these -- do not \
propose them again)
---------------------
{undetermined}

HIDDEN TEST SUITES
------------------
{suites}
"""


def _steps(bundle: Path) -> list[str]:
    from .author import sandbox
    return sandbox._steps(bundle)


def _patch_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if target != "/dev/null":
                paths.append(target[2:] if target.startswith(("a/", "b/")) else target)
    return sorted(set(paths))


def reference_tree(bundle: Path, step: str, dest: Path) -> Path:
    """The repository after the reference patches of every step up to `step`."""
    bundle = Path(bundle)
    shutil.copytree(bundle / "environment/repo", dest, symlinks=True)
    for name in _steps(bundle):
        patch = bundle / "steps" / name / "solution/fix.patch"
        if patch.is_file() and patch.read_text().strip():
            run = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=dest,
                                 capture_output=True, text=True)
            if run.returncode:
                run = subprocess.run(["patch", "-p1", "--forward", "-i", str(patch)], cwd=dest,
                                     capture_output=True, text=True)
                if run.returncode:
                    raise RuntimeError(f"reference patch for {name} did not apply: {run.stderr[-400:]}")
        if name == step:
            break
    return dest


def _suites_text(bundle: Path) -> str:
    parts, total = [], 0
    for path in sorted((Path(bundle) / "tests/suites").rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(Path(bundle) / "tests").as_posix()
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            parts.append(f"=== {rel} (binary, {path.stat().st_size} bytes)")
            continue
        if len(text) > SUITE_FILE_CAP:
            text = text[:SUITE_FILE_CAP] + f"\n... [{len(text) - SUITE_FILE_CAP} more characters]"
        if total + len(text) > SUITE_TOTAL_CAP:
            parts.append(f"=== {rel} (omitted: suite budget spent)")
            continue
        total += len(text)
        parts.append(f"=== {rel}\n{text}")
    return "\n\n".join(parts)


def build_prompt(bundle: Path, step: str, tree: Path) -> str:
    bundle = Path(bundle)
    instruction = (bundle / "steps" / step / "instruction.md").read_text()
    checks = json.loads((bundle / "tests/checks.json").read_text()).get("checks", [])
    rows = [f"{c['id']} | {c.get('kind')} | {' / '.join(c.get('rule_quote') or [])[:400]}"
            for c in checks if c.get("step") in (None, step)]
    patch = (bundle / "steps" / step / "solution/fix.patch").read_text()
    touched = []
    for rel in _patch_paths(patch):
        path = tree / rel
        if path.is_file():
            text = path.read_text(errors="replace")
            touched.append(f"=== {rel}\n{text[:TOUCHED_FILE_CAP]}")
    return PROMPT.format(limit=MAX_PROPOSALS, instruction=instruction.strip(), checks="\n".join(rows),
                         reference=patch.strip(), touched="\n\n".join(touched) or "(none)",
                         named=_named_files(instruction, tree, set(_patch_paths(patch))) or "(none)",
                         undetermined=_undetermined(bundle) or "(none)",
                         suites=_suites_text(bundle))


def _undetermined(bundle: Path) -> str:
    """Declared untested surfaces carrying determinacy evidence: closing a gap means either a hidden case or
    a probe showing the instruction does not settle it, and the second must not be re-proposed every round."""
    try:
        record = json.loads((Path(bundle) / "verification/probes.json").read_text())
    except (OSError, ValueError):
        return ""
    from .shipcheck import _DETERMINACY_REASON
    return "\n".join(f"- {str(t)[:400]}" for t in record.get("untested_surfaces") or []
                     if _DETERMINACY_REASON.search(str(t)))


def _named_files(instruction: str, tree: Path, skip: set[str]) -> str:
    """Repository files the instruction names (full path or a distinctive file name), outside the fix:
    gaps in files the reference never touches -- a tool only checked to exist, a page the user said to
    leave alone -- are invisible to a critic shown only the touched files (Arena feedback 2026-10-08)."""
    import re
    low = instruction.lower()
    parts, total = [], 0
    for path in sorted(Path(tree).rglob("*")):
        if not path.is_file() or any(x in path.parts for x in (".git", "node_modules", "__pycache__", ".venv")):
            continue
        rel = path.relative_to(tree).as_posix()
        name = path.name
        if rel in skip or "." not in name or len(name) < 5:
            continue
        if rel.lower() not in low and not re.search(r"(?<![\w.-])" + re.escape(name.lower()) + r"(?![\w-])", low):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        text = text[:TOUCHED_FILE_CAP]
        if total + len(text) > NAMED_TOTAL_CAP:
            parts.append(f"=== {rel} (omitted: budget spent)")
            continue
        total += len(text)
        parts.append(f"=== {rel}\n{text}")
    return "\n\n".join(parts)


def parse_proposals(text: str) -> list[dict]:
    blocks = re.findall(r"```json\s*(.*?)```", text or "", re.S) or [text or ""]
    for block in reversed(blocks):
        try:
            doc = json.loads(block)
        except ValueError:
            continue
        proposals = doc.get("proposals") if isinstance(doc, dict) else None
        if isinstance(proposals, list):
            return [p for p in proposals if isinstance(p, dict) and isinstance(p.get("edits"), list)][:MAX_PROPOSALS]
    return []


def apply_edits(tree: Path, edits: list[dict]) -> dict[str, str]:
    """The edited files' full text, or ValueError when an edit is not exact and unique."""
    files: dict[str, str] = {}
    for edit in edits:
        rel = str(edit.get("path") or "").lstrip("/")
        old, new = edit.get("old"), edit.get("new")
        if not rel or ".." in Path(rel).parts or not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ValueError(f"malformed edit for {rel!r}")
        text = files.get(rel)
        if text is None:
            path = tree / rel
            if not path.is_file():
                raise ValueError(f"{rel} is not in the reference tree")
            text = path.read_text()
        if text.count(old) != 1:
            raise ValueError(f"{rel}: the old text occurs {text.count(old)} times")
        files[rel] = text.replace(old, new, 1)
    return files


class ReferenceThenFiles:
    """The step's reference (patch and solve commands), then the proposal's files copied in.

    Files go through `docker cp`, never the command line: a single-file web
    game overflowed ARG_MAX as a base64 argument (c-010-browser-canvas-game,
    2026-10-07). A reference that fails to apply raises, so a proposal can
    never be graded over the untouched fixture.
    """

    def __init__(self, golden, files: dict[str, str], label: str):
        self.golden, self.files, self.label, self.kind = golden, files, label, "reference+files"

    def apply(self, container, step: str) -> dict:
        from .author.sandbox import SandboxError
        applied = self.golden.apply(container, step)
        if isinstance(applied, dict) and applied.get("exit_code"):
            raise SandboxError(f"reference for {step} failed: {str(applied.get('stderr'))[-800:]}")
        for rel, text in sorted(self.files.items()):
            guest = f"/app/repo/{rel}"
            container.put_bytes(text.encode("utf-8"), guest)
            container.exec(f"chown agent:agent '{guest}' 2>/dev/null || true", user="root", timeout=60)
        return {"applied": f"reference + {len(self.files)} file(s)"}


def variant_for(bundle: Path, step: str, files: dict[str, str]):
    """The reference for `step`, then the proposal's files written over it."""
    from .author import sandbox
    return ReferenceThenFiles(sandbox.solve_variant(bundle, step), files, f"critic:{step}")


def _ask(model: str, prompt: str, effort: str = "high", max_tokens: int = 32000) -> str:
    from .author.transport import _call
    response = _call({"model": model, "max_tokens": max_tokens, "output_config": {"effort": effort},
                      "messages": [{"role": "user", "content": prompt}]})
    return "\n".join(b.get("text", "") for b in response.get("content") or [] if b.get("type") == "text")


def critic(bundle: Path, *, model: str | None = None, ask=None, workers: int = 3,
           record_dir: Path | None = None) -> dict:
    """Propose, apply and grade counter-implementations for every step."""
    from .author import sandbox
    from .author.transport import MODEL
    bundle = Path(bundle)
    model = model or MODEL
    ask = ask or _ask
    steps = _steps(bundle)
    from .artifacts import identity
    result = {"schema": SCHEMA, "artifact_sha256": identity(bundle), "model": model, "steps": {}, "gaps": []}
    agent_tag = verifier_tag = None
    for step in steps:
        with tempfile.TemporaryDirectory(prefix="critic-") as tmp:
            tree = reference_tree(bundle, step, Path(tmp) / "repo")
            prompt = build_prompt(bundle, step, tree)
            text = ask(model, prompt)
            if not text.strip() and ask is _ask:
                # An empty answer (the whole budget spent thinking over a large suite) is not "no gaps": the
                # downloader and text-parsing packages shipped with raw_chars 0 and Arena found gaps in both
                # (feedback 2026-10-08). Ask once more with a larger budget at medium effort.
                text = ask(model, prompt, effort="medium", max_tokens=64000)
            proposals = parse_proposals(text)
            if text.strip() and not proposals and ask is _ask:
                # A non-empty answer that yields no executable proposal (prose, a refusal, JSON whose proposals
                # carry no `edits` list) proves nothing either: a white-box critic hits on almost every proposal
                # (c-068: 6 of 6, 5 of 5). c-024-vanilla-js-webapp v15 recorded 2176 characters and zero
                # proposals as "0 gaps" (2026-10-09). Ask once more, then record what came back.
                retry = ask(model, prompt + "\n\nYour previous answer contained no proposal in the required "
                            "JSON shape. Answer again with the ```json block and its `edits` lists.",
                            effort="medium", max_tokens=64000)
                if parse_proposals(retry) or not text.strip():
                    text = retry
                proposals = parse_proposals(text)
            if record_dir is not None:
                raw_dir = Path(record_dir) / "coverage-critic-raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"{step}.txt").write_text(text)
            prepared = []
            for i, proposal in enumerate(proposals):
                try:
                    files = apply_edits(tree, proposal["edits"])
                    prepared.append((i, proposal, files, None))
                except ValueError as exc:
                    prepared.append((i, proposal, None, str(exc)))
        if any(files for _, _, files, _ in prepared):
            agent_tag = agent_tag or sandbox.build_image(bundle, "agent")
            verifier_tag = verifier_tag or sandbox.build_image(bundle, "verifier")

        def grade(item):
            i, proposal, files, error = item
            row = {"index": i, "clause": proposal.get("clause"), "violation": proposal.get("violation"),
                   "why_uncaught": proposal.get("why_uncaught"), "edits": proposal.get("edits")}
            if error:
                return {**row, "applied": False, "error": error}
            variants = {s: sandbox.solve_variant(bundle, s) for s in steps}
            variants[step] = variant_for(bundle, step, files)
            try:
                trial = sandbox.verify(bundle, variants, agent_tag=agent_tag, verifier_tag=verifier_tag)
            except Exception as exc:                       # noqa: BLE001 -- an ungraded proposal is no finding
                return {**row, "applied": False, "error": f"{type(exc).__name__}: {str(exc)[-400:]}"}
            entry = trial["steps"].get(step) or {}
            failed = sorted(cid for cid, c in ((entry.get("grade") or {}).get("checks") or {}).items()
                            if isinstance(c, dict) and not c.get("passed"))
            return {**row, "applied": True, "reward": entry.get("reward"), "valid": entry.get("valid"),
                    "failed_checks": failed, "gap": entry.get("valid") is True and entry.get("reward") == 1.0}

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            rows = list(pool.map(grade, prepared))
        result["steps"][step] = {"proposals": rows, "raw_chars": len(text)}
        result["gaps"] += [{"step": step, **r} for r in rows if r.get("gap")]
    if record_dir is not None:
        from .artifacts import atomic_json
        atomic_json(Path(record_dir) / Path(RECEIPT).name, result)
    return result


PORT_PROMPT = """\
A wrong implementation was written on purpose as exact edits over an EARLIER reference of this \
repository. The reference has changed since, so the edits no longer apply. Re-express the SAME wrong \
behaviour as exact edits over the CURRENT reference files below: violate the same clause in the same \
way, change as little as possible, and fix or add nothing else.

CLAUSE: {clause}
VIOLATION: {violation}

EARLIER EDITS (JSON)
{edits}

CURRENT REFERENCE FILES
{files}

Return ONLY one JSON object in a ```json fenced block:
{{"proposals": [{{"clause": "<the same clause>", "violation": "<the same violation>",
  "edits": [{{"path": "<repo-relative path>", "old": "<exact text, unique in the current file>", \
"new": "<replacement>"}}]}}]}}
"""


def _version_number(path: Path) -> int | None:
    m = re.fullmatch(r"v(\d+)", Path(path).name)
    return int(m.group(1)) if m else None


def source_record(bundle: Path) -> tuple[Path, dict] | None:
    """The searching critic run this version must answer to under the closed-and-verified rule: the newest
    EARLIER version of the lineage whose critic searched (verify runs are skipped) -- or None when that run did
    not execute a proposal on every current step, so it proves nothing and a fresh search is due."""
    bundle = Path(bundle)
    n = _version_number(bundle)
    if n is None or bundle.parent.name != "hardened" or not bundle.parent.is_dir():
        return None
    earlier = sorted((p for p in bundle.parent.iterdir()
                      if p.is_dir() and _version_number(p) is not None and _version_number(p) < n),
                     key=_version_number, reverse=True)
    for p in earlier:
        try:
            record = json.loads((p / RECEIPT).read_text())
        except (OSError, ValueError):
            continue
        if record.get("mode") == "verify":
            continue
        rows = record.get("steps") or {}
        for step in _steps(bundle):
            if not any(isinstance(r, dict) and r.get("applied") for r in (rows.get(step) or {}).get("proposals") or []):
                return None
        return p, record
    return None


def _port(tree: Path, row: dict, model: str, ask) -> tuple[dict[str, str], list[dict]] | None:
    """The same wrong behaviour as edits over this version's reference, or None."""
    paths = sorted({str(e.get("path") or "").lstrip("/") for e in row.get("edits") or [] if isinstance(e, dict)})
    shown = "\n\n".join(f"--- {rel}\n{(tree / rel).read_text(errors='replace')[:TOUCHED_FILE_CAP]}"
                        for rel in paths if rel and ".." not in Path(rel).parts and (tree / rel).is_file())
    if not shown:
        return None
    prompt = PORT_PROMPT.format(clause=row.get("clause"), violation=row.get("violation"),
                                edits=json.dumps(row.get("edits"), indent=1)[:TOUCHED_FILE_CAP], files=shown)
    for proposal in parse_proposals(ask(model, prompt, effort="medium", max_tokens=32000))[:1]:
        try:
            return apply_edits(tree, proposal["edits"]), proposal["edits"]
        except ValueError:
            return None
    return None


def verify(bundle: Path, source_dir: Path, source: dict, *, model: str | None = None, ask=None,
           workers: int = 3, record_dir: Path | None = None) -> dict:
    """Closed-and-verified (user, 2026-10-09): re-execute every proposal the source run executed, on THIS
    version. A former gap that still scores 1.0 stays a gap; a proposal that failed there and scores 1.0 here
    is a regression and counts as a gap too. Nothing new is searched for."""
    from .artifacts import identity
    from .author import sandbox
    from .author.transport import MODEL
    bundle = Path(bundle)
    model = model or MODEL
    ask = ask or _ask
    steps = _steps(bundle)
    result = {"schema": SCHEMA, "artifact_sha256": identity(bundle), "model": model, "mode": "verify",
              "rule": "closed_and_verified (user, 2026-10-09)", "source": str(source_dir),
              "source_sha256": source.get("artifact_sha256"), "steps": {}, "gaps": []}
    agent_tag = verifier_tag = None
    for step in steps:
        source_rows = [r for r in ((source.get("steps") or {}).get(step) or {}).get("proposals") or []
                       if isinstance(r, dict) and r.get("applied")]
        prepared = []
        with tempfile.TemporaryDirectory(prefix="critic-verify-") as tmp:
            tree = reference_tree(bundle, step, Path(tmp) / "repo")
            for r in source_rows:
                try:
                    prepared.append((r, apply_edits(tree, r.get("edits") or []), r.get("edits"), False))
                except ValueError:
                    ported = _port(tree, r, model, ask)
                    prepared.append((r, *(ported or (None, r.get("edits"))), True))
        if any(files for _, files, _, _ in prepared):
            agent_tag = agent_tag or sandbox.build_image(bundle, "agent")
            verifier_tag = verifier_tag or sandbox.build_image(bundle, "verifier")

        def grade(item):
            r, files, edits, ported = item
            was_gap = bool(r.get("gap"))
            row = {"index": r.get("index"), "clause": r.get("clause"), "violation": r.get("violation"),
                   "why_uncaught": r.get("why_uncaught"), "edits": edits, "source_gap": was_gap,
                   "source_reward": r.get("reward"), "ported": ported}
            if not files:
                # Not re-executed: a former gap stays open rather than counting as closed.
                return {**row, "applied": False, "gap": was_gap,
                        "error": "the source edits do not apply to this version's reference and could not be re-expressed"}
            variants = {s: sandbox.solve_variant(bundle, s) for s in steps}
            variants[step] = variant_for(bundle, step, files)
            try:
                trial = sandbox.verify(bundle, variants, agent_tag=agent_tag, verifier_tag=verifier_tag)
            except Exception as exc:                       # noqa: BLE001 -- unverified is not closed
                return {**row, "applied": False, "gap": was_gap, "error": f"{type(exc).__name__}: {str(exc)[-400:]}"}
            entry = trial["steps"].get(step) or {}
            failed = sorted(cid for cid, c in ((entry.get("grade") or {}).get("checks") or {}).items()
                            if isinstance(c, dict) and not c.get("passed"))
            full = entry.get("valid") is True and entry.get("reward") == 1.0
            return {**row, "applied": True, "reward": entry.get("reward"), "valid": entry.get("valid"),
                    "failed_checks": failed, "gap": full, "regression": full and not was_gap}

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            rows = list(pool.map(grade, prepared))
        result["steps"][step] = {"proposals": rows, "raw_chars": 0, "verified_from": str(source_dir)}
        result["gaps"] += [{"step": step, **r} for r in rows if r.get("gap")]
    if record_dir is not None:
        from .artifacts import atomic_json
        atomic_json(Path(record_dir) / Path(RECEIPT).name, result)
    return result


JUDGE_PROMPT = """\
A user gave a developer the instruction below. A reviewer then wrote, on purpose, a WRONG version of the \
developer's fix and claims it breaks one rule the user published. Judge that claim as a careful developer who \
reads only the instruction would.

INSTRUCTION
-----------
{instruction}

THE RULE IT IS SAID TO BREAK (quoted from the instruction)
----------------------------------------------------------
{clause}

WHAT THE WRONG VERSION DOES
---------------------------
{violation}

THE CODE CHANGE (exact edits over a correct fix)
------------------------------------------------
{edits}

Answer two questions.
1. PUBLISHED: Does the instruction clearly settle that this behaviour is wrong, so that every careful developer \
would agree it breaks what the user asked? Answer YES, NO (the instruction allows it) or UNDETERMINED (reasonable \
readings differ).
2. REALISTIC: Could a competent developer plausibly write this while doing the task in good faith -- an honest \
mistake such as a kept special case, a common idiom misapplied, a missed branch or boundary -- rather than code \
contrived only to slip past particular tests? Answer YES or NO.

Reply with your reasoning in at most five sentences, then exactly two final lines:
PUBLISHED: <YES|NO|UNDETERMINED>
REALISTIC: <YES|NO>"""

#: The judges: the two calibration families, as in the determinacy probe (pipeline/author/determinacy.py).
JUDGES = ("claude-fable-5-1", "gpt-6-astra")


def _verdict(text: str, key: str) -> str | None:
    found = re.findall(rf"^\s*\**{key}\**\s*:\s*\**\s*([A-Za-z]+)", text or "", re.M)
    return found[-1].upper() if found else None


def judge_gap(instruction: str, gap: dict, *, ask=None, models: tuple[str, ...] = JUDGES) -> dict:
    """Is the gap realistic and on a clearly published rule? (user, 2026-10-09: "Block only realistic gaps").

    It stays blocking unless the judges give a reason not to: ANY family answering that the instruction does not
    clearly settle the behaviour (the determinacy rule: a case every family does not settle is not admitted), or
    EVERY family calling the code contrived. A judge that errs or answers nothing never demotes a gap."""
    if ask is None:
        from .author.determinacy import _ask as ask
    prompt = JUDGE_PROMPT.format(instruction=instruction.strip(), clause=str(gap.get("clause") or "").strip(),
                                 violation=str(gap.get("violation") or "").strip(),
                                 edits=json.dumps(gap.get("edits"), indent=1)[:12000])
    answers = {}
    for model in models:
        try:
            text = ask(model, prompt)
            answers[model] = {"published": _verdict(text, "PUBLISHED"), "realistic": _verdict(text, "REALISTIC"),
                              "reasoning": (text or "")[-900:]}
        except Exception as exc:                          # noqa: BLE001 -- an unanswered judge demotes nothing
            answers[model] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    doubted = sorted(m for m, a in answers.items() if a.get("published") in ("NO", "UNDETERMINED"))
    contrived = bool(answers) and all(a.get("realistic") == "NO" for a in answers.values())
    if doubted:
        why = "the instruction does not clearly settle it (" + ", ".join(doubted) + ")"
    elif contrived:
        why = "every judge calls it contrived, not a realistic mistake"
    else:
        why = "realistic and clearly published"
    return {"models": list(models), "answers": answers, "blocking": not doubted and not contrived, "why": why}


def adjudicate(bundle: Path, result: dict, *, prior: dict | None = None, ask=None, workers: int = 4) -> dict:
    """Mark every gap `blocking` or advisory. A gap the prior (source) run already judged keeps that judgement."""
    bundle = Path(bundle)
    known = {}
    for g in (prior or {}).get("gaps") or []:
        if isinstance(g, dict) and g.get("judgement"):
            known.setdefault((g.get("step"), g.get("clause"), g.get("violation")), g["judgement"])
    gaps = result.get("gaps") or []

    def one(g):
        key = (g.get("step"), g.get("clause"), g.get("violation"))
        if key in known:
            return known[key]
        path = bundle / "steps" / str(g.get("step")) / "instruction.md"
        if not path.is_file():
            return {"blocking": True, "why": "no instruction to judge against", "answers": {}}
        return judge_gap(path.read_text(encoding="utf-8", errors="replace"), g, ask=ask)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        judgements = list(pool.map(one, gaps))
    for g, judgement in zip(gaps, judgements):
        g["judgement"], g["blocking"] = judgement, bool(judgement.get("blocking", True))
        for row in ((result.get("steps") or {}).get(g.get("step")) or {}).get("proposals") or []:
            if row.get("index") == g.get("index") and row.get("gap"):
                row["judgement"], row["blocking"] = g["judgement"], g["blocking"]
    result["gap_rule"] = "realistic_only (user, 2026-10-09)"
    return result


def _judged_before(bundle: Path) -> list[dict]:
    """Judged gaps of EARLIER versions of this lineage, oldest first (adjudicate keeps the first key it meets)."""
    bundle = Path(bundle)
    n = _version_number(bundle)
    if n is None or bundle.parent.name != "hardened":
        return []
    out = []
    for p in sorted((p for p in bundle.parent.iterdir() if p.is_dir() and _version_number(p) is not None and _version_number(p) < n),
                    key=_version_number):
        try:
            record = json.loads((p / RECEIPT).read_text())
        except (OSError, ValueError):
            continue
        out += [g for g in record.get("gaps") or [] if isinstance(g, dict) and g.get("judgement")]
    return out


def ship_critic(bundle: Path, *, workers: int = 3, record_dir: Path | None = None) -> dict:
    """The critic a final pass runs, per `policy.CRITIC_SHIP_RULE` and `policy.CRITIC_GAP_RULE`."""
    from . import policy
    found = source_record(bundle) if policy.CRITIC_SHIP_RULE == "closed_and_verified" else None
    if found is not None:
        result = verify(bundle, *found, workers=workers, record_dir=record_dir)
    else:
        result = critic(bundle, workers=workers, record_dir=record_dir)
    if policy.CRITIC_GAP_RULE == "realistic_only" and result.get("gaps"):
        # A gap's FIRST recorded judgement holds for its lineage: model judges are not repeatable (c-014's Polya gap,
        # 2026-10-09: advisory at 19:15, blocking at 23:3x), and re-judging every final would let it flip either way.
        adjudicate(bundle, result, prior={"gaps": _judged_before(bundle) + list((found[1] if found else {}).get("gaps") or [])})
        if record_dir is not None:
            from .artifacts import atomic_json
            atomic_json(Path(record_dir) / Path(RECEIPT).name, result)
    return result


def _what(result: dict, g: dict) -> str:
    if g.get("error"):
        return f"a former gap not re-executed on this version ({str(g['error'])[:160]})"
    if g.get("regression"):
        return f"scores 1.0 here although it failed on {Path(str(result.get('source'))).name}"
    return "scores 1.0"


def findings(result: dict) -> list[dict]:
    """The BLOCKING gaps (a gap without a judgement blocks)."""
    from .shipcheck import finding
    return [finding("coverage.gap", f"{g['step']}#{g['index']}",
                    f"{_what(result, g)} while violating: {str(g.get('clause'))[:200]} -- {str(g.get('violation'))[:200]}",
                    route="hidden", measure="regrade", evidence=[json.dumps(g.get("edits"))[:1500]])
            for g in result.get("gaps") or [] if g.get("blocking", True) is not False]


def advisory(result: dict) -> list[dict]:
    """Gaps the judges demoted: recorded and listed in the delivered package, never blocking."""
    from .shipcheck import finding
    return [finding("coverage.gap_advisory", f"{g['step']}#{g['index']}",
                    f"advisory ({(g.get('judgement') or {}).get('why')}): {_what(result, g)} while violating: "
                    f"{str(g.get('clause'))[:200]} -- {str(g.get('violation'))[:200]}",
                    route="none", measure="none", evidence=[json.dumps(g.get("edits"))[:1500]])
            for g in result.get("gaps") or [] if g.get("blocking", True) is False]


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundles", nargs="+", type=Path)
    ap.add_argument("--model", default=None)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args(argv)
    gaps = 0
    for bundle in args.bundles:
        result = critic(bundle, model=args.model, workers=args.workers, record_dir=bundle / "verification")
        rows = [r for s in result["steps"].values() for r in s["proposals"]]
        print(f"{bundle}: {len(result['gaps'])} gap(s) from {len(rows)} proposal(s) "
              f"({sum(1 for r in rows if r.get('applied'))} graded)")
        for g in result["gaps"]:
            print(f"  GAP {g['step']}#{g['index']}: {str(g.get('clause'))[:160]} -- {str(g.get('violation'))[:160]}")
        gaps += len(result["gaps"])
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
