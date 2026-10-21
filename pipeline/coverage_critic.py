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

The pass audit (user, 2026-10-21, after the external audit) turns the critic on
the calibration solutions that PASSED a check: the best counter-implementations
we have are the ones strong models actually wrote. It must DEMONSTRATE a
violation -- a new hidden test the reference passes and that solution fails,
both executed -- before anything is a finding.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
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

PROMISES THE TESTS MAY NOT ENFORCE (the clause map's text judgement -- target these FIRST: write the \
counter-implementation a solver could plausibly ship that breaks exactly that promise, and let execution decide)
-----------------------------------------------------------------------------------------------------------------
{promises}

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


def build_prompt(bundle: Path, step: str, tree: Path, limit: int = MAX_PROPOSALS) -> str:
    bundle = Path(bundle)
    instruction = (bundle / "steps" / step / "instruction.md").read_text()
    checks = json.loads((bundle / "tests/checks.json").read_text()).get("checks", [])
    # Shipped checks.json files carry no rule_quote: the sentences come from the spec / basis (2026-10-21).
    from .clause_map import rule_quotes
    quotes = rule_quotes(bundle)
    rows = [f"{c['id']} | {c.get('kind')} | {' / '.join(c.get('rule_quote') or quotes.get(c['id']) or [])[:400]}"
            for c in checks if c.get("step") in (None, step)]
    patch = (bundle / "steps" / step / "solution/fix.patch").read_text()
    touched = []
    for rel in _patch_paths(patch):
        path = tree / rel
        if path.is_file():
            text = path.read_text(errors="replace")
            touched.append(f"=== {rel}\n{text[:TOUCHED_FILE_CAP]}")
    return PROMPT.format(limit=limit, instruction=instruction.strip(), checks="\n".join(rows),
                         reference=patch.strip(), touched="\n\n".join(touched) or "(none)",
                         named=_named_files(instruction, tree, set(_patch_paths(patch))) or "(none)",
                         undetermined=_undetermined(bundle) or "(none)",
                         promises=_flagged_promises(bundle, step) or "(none)",
                         suites=_suites_text(bundle))


def _flagged_promises(bundle: Path, step: str) -> str:
    """Promises the clause map judged unenforced or enforced in name only (verification/clause-map.json), with the
    judges' counter-examples: the critic's first targets, since only an executed 1.0 counter-implementation blocks."""
    try:
        record = json.loads((Path(bundle) / "verification/clause-map.json").read_text())
    except (OSError, ValueError):
        return ""
    lines = []
    for clause in ((record.get("steps") or {}).get(step) or {}).get("clauses") or []:
        if clause.get("status") not in ("unenforced", "in_name_only"):
            continue
        said = [a.get("counter_example") for a in (clause.get("judgement") or {}).values()
                if isinstance(a, dict) and a.get("detects") == "NO" and a.get("counter_example")]
        lines.append(f"- [{clause['status']}] {' '.join(str(clause.get('quote', '')).split())[:300]}"
                     + (f" -- judge's counter-example: {said[0][:300]}" if said else ""))
    return "\n".join(lines[:40])


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


def parse_proposals(text: str, limit: int = MAX_PROPOSALS) -> list[dict]:
    blocks = re.findall(r"```json\s*(.*?)```", text or "", re.S) or [text or ""]
    for block in reversed(blocks):
        try:
            doc = json.loads(block)
        except ValueError:
            continue
        proposals = doc.get("proposals") if isinstance(doc, dict) else None
        if isinstance(proposals, list):
            return [p for p in proposals if isinstance(p, dict) and isinstance(p.get("edits"), list)][:limit]
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
           record_dir: Path | None = None, steps: list[str] | None = None, limit: int = MAX_PROPOSALS,
           receipt: bool = True) -> dict:
    """Propose, apply and grade counter-implementations for every step (or only `steps`, at most `limit` each).

    `receipt=False` records the raw answers but writes no receipt: the bounded fresh search of a changed final
    (`fresh_search`) is one part of the ship critic's run, and a receipt holding only that part would read as the
    version's whole critic run."""
    from .author import sandbox
    from .author.transport import MODEL
    bundle = Path(bundle)
    model = model or MODEL
    ask = ask or _ask
    every = _steps(bundle)
    from .artifacts import identity
    result = {"schema": SCHEMA, "artifact_sha256": identity(bundle), "model": model, "surface": surface(bundle),
              "steps": {}, "gaps": []}
    agent_tag = verifier_tag = None
    for step in [s for s in every if steps is None or s in steps]:
        with tempfile.TemporaryDirectory(prefix="critic-") as tmp:
            tree = reference_tree(bundle, step, Path(tmp) / "repo")
            prompt = build_prompt(bundle, step, tree, limit)
            text = ask(model, prompt)
            if not text.strip() and ask is _ask:
                # An empty answer (the whole budget spent thinking over a large suite) is not "no gaps": the
                # downloader and text-parsing packages shipped with raw_chars 0 and Arena found gaps in both
                # (feedback 2026-10-08). Ask once more with a larger budget at medium effort.
                text = ask(model, prompt, effort="medium", max_tokens=64000)
            proposals = parse_proposals(text, limit)
            if text.strip() and not proposals and ask is _ask:
                # A non-empty answer that yields no executable proposal (prose, a refusal, JSON whose proposals
                # carry no `edits` list) proves nothing either: a white-box critic hits on almost every proposal
                # (c-068: 6 of 6, 5 of 5). c-024-vanilla-js-webapp v15 recorded 2176 characters and zero
                # proposals as "0 gaps" (2026-10-09). Ask once more, then record what came back.
                retry = ask(model, prompt + "\n\nYour previous answer contained no proposal in the required "
                            "JSON shape. Answer again with the ```json block and its `edits` lists.",
                            effort="medium", max_tokens=64000)
                if parse_proposals(retry, limit) or not text.strip():
                    text = retry
                proposals = parse_proposals(text, limit)
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
            variants = {s: sandbox.solve_variant(bundle, s) for s in every}
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
    if record_dir is not None and receipt:
        from .artifacts import atomic_json
        atomic_json(Path(record_dir) / Path(RECEIPT).name, result)
    return result


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def surface(bundle: Path) -> dict:
    """What a critic run examined that decides its proposals: each step's instruction and the hidden suites.

    Recorded in every receipt (user, 2026-10-21), so a later final can tell whether the searching run it answers
    to saw the instruction and the suites it ships with."""
    bundle = Path(bundle)
    suites = bundle / "tests/suites"
    h = hashlib.sha256()
    for path in sorted(suites.rglob("*")) if suites.is_dir() else []:
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo"):
            h.update(f"{path.relative_to(suites).as_posix()}\0{_digest(path)}\n".encode())
    return {"instructions": {s: _digest(bundle / "steps" / s / "instruction.md") for s in _steps(bundle)},
            "suites_sha256": h.hexdigest()}


def changed_since(bundle: Path, source_dir: Path, source: dict) -> dict[str, list[str]]:
    """Steps whose INSTRUCTION differs from what the source searching run examined.

    Hidden-suite changes alone do not trigger a fresh search (2026-10-21, same day): every repair adds hidden cases, so
    a suites trigger re-searched every gap-closing round's result, found a few more gaps and asked for another round --
    three repaired tasks in a row stopped at the critic gate. What changes the task is its instruction; a hidden-only
    round is answered by re-executing the source run's proposals (closed-and-verified). A receipt written before
    2026-10-21 records no surface: the source version's own instruction files are read instead."""
    then = source.get("surface") if isinstance(source.get("surface"), dict) else None
    if then is None and Path(source_dir).is_dir():
        try:
            then = surface(Path(source_dir))
        except (OSError, ValueError, KeyError):
            then = None
    now = surface(bundle)
    out = {}
    for step, digest in now["instructions"].items():
        if then is None:
            out[step] = ["what the source run examined is not known"]
        elif (then.get("instructions") or {}).get(step) != digest:
            out[step] = ["instruction"]
    return out


def fresh_search(bundle: Path, result: dict, changed: dict[str, list[str]], *, model: str | None = None,
                 ask=None, workers: int = 3, record_dir: Path | None = None) -> dict:
    """A bounded fresh search on the steps that changed, merged into a closed-and-verified result.

    User, 2026-10-21 (external audit, class 5): under closed_and_verified a final only re-executed an earlier run's
    proposals, so nothing searched the instruction and suites that actually shipped, and most audited tasks showed
    0 open gaps. The re-execution stays; the search is bounded by policy.CRITIC_FRESH_SEARCH_PROPOSALS per changed
    step, and its realistic gaps block like any other. The merged receipt is a searching run ("verify+search"):
    the next version answers to it, re-executing the carried and the fresh proposals alike."""
    from . import policy
    fresh = critic(bundle, model=model, ask=ask, workers=workers, record_dir=record_dir, steps=sorted(changed),
                   limit=policy.CRITIC_FRESH_SEARCH_PROPOSALS, receipt=False)
    result["mode"] = "verify+search"
    result["fresh_search"] = {"rule": "fresh bounded search where the instruction or suites changed (user, 2026-10-21)",
                              "changed": changed, "limit": policy.CRITIC_FRESH_SEARCH_PROPOSALS,
                              "model": fresh.get("model")}
    for step, entry in (fresh.get("steps") or {}).items():
        slot = result.setdefault("steps", {}).setdefault(step, {"proposals": []})
        rows = slot.setdefault("proposals", [])
        # Fresh indices follow the carried ones, so a subject (step#index) names one proposal in the lineage.
        base = 1 + max((r.get("index") for r in rows if isinstance(r.get("index"), int)), default=-1)
        added = [dict(r, index=base + int(r.get("index") or 0), fresh=True) for r in entry.get("proposals") or []]
        rows.extend(added)
        slot["raw_chars"] = int(slot.get("raw_chars") or 0) + int(entry.get("raw_chars") or 0)
        result.setdefault("gaps", []).extend({"step": step, **r} for r in added if r.get("gap"))
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
    EARLIER version of the lineage whose critic searched (verify runs are skipped; one that also searched --
    "verify+search", 2026-10-21 -- counts) -- or None when that run did not execute a proposal on every current
    step, so it proves nothing and a fresh search is due."""
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
    is a regression and counts as a gap too. Nothing new is searched for here (`fresh_search` does that where
    the instruction or suites changed). A former gap the source could not re-execute is tried again, never dropped."""
    from .artifacts import identity
    from .author import sandbox
    from .author.transport import MODEL
    bundle = Path(bundle)
    model = model or MODEL
    ask = ask or _ask
    steps = _steps(bundle)
    result = {"schema": SCHEMA, "artifact_sha256": identity(bundle), "model": model, "mode": "verify",
              "rule": "closed_and_verified (user, 2026-10-09)", "source": str(source_dir),
              "source_sha256": source.get("artifact_sha256"), "surface": surface(bundle), "steps": {}, "gaps": []}
    agent_tag = verifier_tag = None
    for step in steps:
        # A carried gap the source could not re-execute (applied False, gap True) is tried again: a "verify+search"
        # receipt is the next version's source, and skipping it would close a gap nobody re-ran (2026-10-21).
        source_rows = [r for r in ((source.get("steps") or {}).get(step) or {}).get("proposals") or []
                       if isinstance(r, dict) and (r.get("applied") or r.get("gap"))]
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
3. SENTENCE: Quote the one sentence of the instruction that decides question 1 -- or, when it does not settle it, \
the sentence that leaves it open -- verbatim.

Reply with your reasoning in at most five sentences, then exactly three final lines:
PUBLISHED: <YES|NO|UNDETERMINED>
REALISTIC: <YES|NO>
SENTENCE: <that sentence, quoted verbatim from the instruction>"""

#: The judges: the two calibration families, as in the determinacy probe (pipeline/author/determinacy.py).
JUDGES = ("claude-fable-5-1", "gpt-6-astra")


def _verdict(text: str, key: str) -> str | None:
    found = re.findall(rf"^\s*\**{key}\**\s*:\s*\**\s*([A-Za-z]+)", text or "", re.M)
    return found[-1].upper() if found else None


#: Quote marks and end punctuation stripped before two sentences are compared.
_QUOTES = "\"'`“”‘’«»「」『』.,;:!?؛،。、 "
#: Shorter quotes than this are never matched against an instruction or a check's sentence: a fragment of a few
#: characters occurs in almost any text.
QUOTE_MIN_CHARS = 10


def _norm(text) -> str:
    # Format characters (the Persian zero-width non-joiner, a BOM) are dropped: a judge quoting a sentence often
    # loses them, and a doubt is not unsupported for that.
    text = "".join(ch for ch in unicodedata.normalize("NFC", str(text or "")) if unicodedata.category(ch) != "Cf")
    return re.sub(r"\s+", " ", text).strip().strip(_QUOTES).strip().casefold()


def _sentence(text: str) -> str | None:
    found = re.findall(r"^\s*\**SENTENCE\**\s*:\s*\**\s*(.+?)\s*$", text or "", re.M)
    return found[-1].strip().strip("*").strip() if found else None


def in_instruction(sentence: str | None, instruction: str) -> bool:
    """`sentence` is quoted from `instruction` (whitespace, quote marks and case aside)."""
    wanted = _norm(sentence)
    return len(wanted) >= QUOTE_MIN_CHARS and wanted in _norm(instruction)


def _arbitrate(prompt: str, key: str, ask) -> dict:
    """The arbiter's answer to a judging prompt the families split on (policy.READING_ARBITER, 2026-10-21: on the
    repaired node-rest-dashboard and school-scheduling a gap Astra called published, quoting the deciding sentence,
    was demoted on Fable's UNDETERMINED alone and routed to an instruction rewrite)."""
    from . import policy
    try:
        text = ask(policy.READING_ARBITER, prompt)
    except Exception as exc:                              # noqa: BLE001 -- an arbiter that errs decides nothing
        return {"model": policy.READING_ARBITER, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"model": policy.READING_ARBITER, key.lower(): _verdict(text, key), "sentence": _sentence(text),
            "reasoning": (text or "")[-900:]}


def judge_gap(instruction: str, gap: dict, *, ask=None, models: tuple[str, ...] = JUDGES) -> dict:
    """Is the gap realistic and on a clearly published rule? (user, 2026-10-09: "Block only realistic gaps").

    It stays blocking unless the judges give a reason not to: ANY family answering that the instruction does not
    clearly settle the behaviour (the determinacy rule: a case every family does not settle is not admitted), or
    EVERY family calling the code contrived. A judge that errs or answers nothing never demotes a gap. The sentence
    a doubting family names is recorded (2026-10-21): whether a check grades that sentence decides whether the
    demotion holds (`adjudicate`)."""
    if ask is None:
        from .author.determinacy import _ask as ask
    prompt = JUDGE_PROMPT.format(instruction=instruction.strip(), clause=str(gap.get("clause") or "").strip(),
                                 violation=str(gap.get("violation") or "").strip(),
                                 edits=json.dumps(gap.get("edits"), indent=1)[:12000])
    answers = {}
    for model in models:
        try:
            text = ask(model, prompt)
            sentence = _sentence(text)
            answers[model] = {"published": _verdict(text, "PUBLISHED"), "realistic": _verdict(text, "REALISTIC"),
                              "sentence": sentence, "sentence_found": in_instruction(sentence, instruction),
                              "reasoning": (text or "")[-900:]}
        except Exception as exc:                          # noqa: BLE001 -- an unanswered judge demotes nothing
            answers[model] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    doubted = sorted(m for m, a in answers.items() if a.get("published") in ("NO", "UNDETERMINED"))
    arbiter = None
    if doubted and any(a.get("published") == "YES" for a in answers.values()):
        # The families split: the arbiter's PUBLISHED decides; an erring arbiter leaves the doubt standing.
        arbiter = _arbitrate(prompt, "PUBLISHED", ask)
        if arbiter.get("published") == "YES":
            doubted = []
    contrived = bool(answers) and all(a.get("realistic") == "NO" for a in answers.values())
    if doubted:
        why = "the instruction does not clearly settle it (" + ", ".join(doubted) + ")"
    elif contrived:
        why = "every judge calls it contrived, not a realistic mistake"
    else:
        why = "realistic and clearly published"
    sentence = next((answers[m]["sentence"] for m in doubted if answers[m].get("sentence_found")), None)
    return {"models": list(models), "answers": answers, "blocking": not doubted and not contrived, "why": why,
            "unsettled": bool(doubted), "doubted": doubted, "sentence": sentence,
            **({"arbiter": arbiter} if arbiter else {})}


def _unsettled(judgement: dict) -> bool:
    """The judgement demoted the gap because the instruction does not clearly settle it (not as contrived)."""
    if "unsettled" in judgement:
        return bool(judgement["unsettled"])
    return str(judgement.get("why") or "").startswith("the instruction does not clearly settle it")


def _reusable(judgement: dict, digest: str | None) -> bool:
    """A recorded judgement holds for its lineage while the instruction it judged is unchanged (2026-10-21: a
    round that states an unsettled rule must be judged on the new wording). One recorded before 2026-10-21 has no
    instruction hash: it still holds, unless it doubted the sentence -- a doubt now decides whether the gap blocks
    on the instruction, so it is asked once more, against the instruction as it stands, and then holds."""
    said = {a.get("published") for a in (judgement.get("answers") or {}).values() if isinstance(a, dict)}
    if "YES" in said and said & {"NO", "UNDETERMINED"} and not judgement.get("arbiter"):
        return False            # a split judged before the arbiter existed (2026-10-21) is judged again
    if judgement.get("instruction_sha256"):
        return judgement["instruction_sha256"] == digest
    return not _unsettled(judgement)


def _rule_quotes(bundle: Path) -> dict[str, tuple[str | None, list[str]]]:
    """check id -> (step, the instruction sentences it enforces), for the checks the shipped grader runs."""
    bundle = Path(bundle)
    try:
        shipped = json.loads((bundle / "tests/checks.json").read_text()).get("checks") or []
    except (OSError, ValueError):
        return {}
    try:
        spec = json.loads((bundle / "authoring/spec.json").read_text()).get("checks") or []
    except (OSError, ValueError):
        spec = []
    out = {c["id"]: (c.get("step"), [q for q in c.get("rule_quote") or [] if isinstance(q, str)])
           for c in shipped if isinstance(c, dict) and c.get("id")}
    for c in spec:
        if isinstance(c, dict) and c.get("id") in out:
            step, quotes = out[c["id"]]
            out[c["id"]] = (step or c.get("step"), quotes + [q for q in c.get("rule_quote") or []
                                                             if isinstance(q, str) and q not in quotes])
    return out


def asserting_checks(bundle: Path, step: str | None, quotes: list) -> list[str]:
    """Checks of `step` that grade one of `quotes`: a sentence a check enforces (its rule_quote) contains the quote,
    or the quote contains it.

    User, 2026-10-21 (external audit, class 1): a gap the judges demote because the instruction does not settle it
    is harmless only while no check grades that sentence. When one does, the grader rewards one reading of an
    unsettled point (pdf-tooling: 45 points on a sentence that reads as a recognition rule or a renumbering order).
    Matching the sentence is the evidence available -- a counter-implementation that scores 1.0 shows by execution
    that no check fails it, never which sentence a check reads -- so it is a stated hypothesis, and a check that
    stops grading a sentence stops quoting it."""
    wanted = [n for n in (_norm(q) for q in quotes if q) if len(n) >= QUOTE_MIN_CHARS]
    out = []
    for cid, (s, enforced) in _rule_quotes(bundle).items():
        if s not in (None, step):
            continue
        for quote in enforced:
            n = _norm(quote)
            if len(n) >= QUOTE_MIN_CHARS and any(w in n or n in w for w in wanted):
                out.append(cid)
                break
    return sorted(out)


def readjudicate(bundle: Path, *, ask=None) -> dict | None:
    """Judge again, on the current rules, the gaps of the receipt a release reuses for this version: a judgement the
    rules no longer accept (a split recorded before the arbiter existed, 2026-10-21: node-rest-dashboard v11 kept
    stopping at the unsettled gate on its pre-arbiter receipt) is asked again; every other one holds."""
    from .artifacts import atomic_json
    path = Path(bundle) / RECEIPT
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or not receipt.get("gaps"):
        return receipt
    adjudicate(Path(bundle), receipt, prior={"gaps": [dict(g) for g in receipt["gaps"]]}, ask=ask)
    atomic_json(path, receipt)
    return receipt


def adjudicate(bundle: Path, result: dict, *, prior: dict | None = None, ask=None, workers: int = 4,
               asserted: bool | None = None) -> dict:
    """Mark every gap `blocking` or advisory. A gap the prior (source) run already judged keeps that judgement
    while the step's instruction is unchanged.

    Under policy.CRITIC_GAP_RULE "realistic_asserted" (2026-10-21) a gap demoted as unsettled whose sentence a
    hidden check grades keeps its demotion for grading -- no hidden case is owed -- and is routed to the
    instruction (`asserted_by`, route "instruction"): it blocks until the rule is stated or the assertion dropped."""
    from . import policy
    bundle = Path(bundle)
    asserted = policy.CRITIC_GAP_RULE == "realistic_asserted" if asserted is None else asserted
    digests: dict[str, str | None] = {}

    def digest(step) -> str | None:
        if step not in digests:
            digests[step] = _digest(bundle / "steps" / str(step) / "instruction.md")
        return digests[step]

    known = {}
    for g in (prior or {}).get("gaps") or []:
        if isinstance(g, dict) and isinstance(g.get("judgement"), dict) and _reusable(g["judgement"], digest(g.get("step"))):
            known.setdefault((g.get("step"), g.get("clause"), g.get("violation")), g["judgement"])
    gaps = result.get("gaps") or []

    def one(g):
        key = (g.get("step"), g.get("clause"), g.get("violation"))
        if key in known:
            return known[key]
        path = bundle / "steps" / str(g.get("step")) / "instruction.md"
        if not path.is_file():
            return {"blocking": True, "why": "no instruction to judge against", "answers": {}}
        return {**judge_gap(path.read_text(encoding="utf-8", errors="replace"), g, ask=ask),
                "instruction_sha256": digest(g.get("step"))}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        judgements = list(pool.map(one, gaps))
    for g, judgement in zip(gaps, judgements):
        g["judgement"], g["blocking"] = judgement, bool(judgement.get("blocking", True))
        g.pop("asserted_by", None)
        g.pop("route", None)
        if asserted and not g["blocking"] and _unsettled(judgement):
            by = asserting_checks(bundle, g.get("step"), [g.get("clause"), judgement.get("sentence")])
            if by:
                g["asserted_by"], g["route"] = by, "instruction"
        for row in ((result.get("steps") or {}).get(g.get("step")) or {}).get("proposals") or []:
            if row.get("index") == g.get("index") and row.get("gap"):
                row["judgement"], row["blocking"] = g["judgement"], g["blocking"]
                if g.get("asserted_by"):
                    row["asserted_by"], row["route"] = g["asserted_by"], g["route"]
    result["gap_rule"] = ("realistic_asserted (user, 2026-10-21)" if asserted
                          else "realistic_only (user, 2026-10-09)")
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
    """The critic a final pass runs, per `policy.CRITIC_SHIP_RULE` and `policy.CRITIC_GAP_RULE`.

    Closed-and-verified re-executes the source run's proposals. When this version's instruction or hidden suites
    differ from what that run examined (2026-10-21) a bounded fresh search runs too, and the receipt is written
    once, after both: a verify-only receipt for a changed version would read as its whole critic run."""
    from . import policy
    found = source_record(bundle) if policy.CRITIC_SHIP_RULE == "closed_and_verified" else None
    if found is not None:
        changed = changed_since(bundle, *found)
        result = verify(bundle, *found, workers=workers, record_dir=None if changed else record_dir)
        if changed:
            fresh_search(bundle, result, changed, workers=workers, record_dir=record_dir)
    else:
        result = critic(bundle, workers=workers, record_dir=record_dir)
    if policy.CRITIC_GAP_RULE in ("realistic_only", "realistic_asserted") and result.get("gaps"):
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


def unsettled_text(judgement: dict, asserted_by: list[str], clause) -> str:
    """The instruction-route finding's wording, shared by the critic and the pass audit."""
    sentence = judgement.get("sentence") or clause
    return (f"the grader rewards one reading of a sentence the instruction does not settle "
            f"({judgement.get('why')}): {', '.join(asserted_by)} grade(s) '{str(sentence)[:240]}'. State the rule "
            "in the user's voice so the sentence settles it, or drop that assertion (and the check's quote of it)")


def findings(result: dict) -> list[dict]:
    """The BLOCKING gaps (a gap without a judgement blocks), and the demoted ones a hidden check grades the
    sentence of (`coverage.unsettled`, route "instruction", 2026-10-21)."""
    from .shipcheck import finding
    from . import policy
    out = [finding("coverage.gap", f"{g['step']}#{g['index']}",
                   f"{_what(result, g)} while violating: {str(g.get('clause'))[:200]} -- {str(g.get('violation'))[:200]}",
                   route="hidden", measure="regrade", evidence=[json.dumps(g.get("edits"))[:1500]])
           for g in result.get("gaps") or [] if g.get("blocking", True) is not False
           and policy.LENIENCY_FINDINGS_BLOCK]           # a lenient grader is an advisory (policy, 2026-10-21)
    out += [finding("coverage.unsettled", f"{g['step']}#{g['index']}",
                    f"{unsettled_text(g.get('judgement') or {}, g['asserted_by'], g.get('clause'))} -- a wrong version "
                    f"{_what(result, g)} while violating it: {str(g.get('violation'))[:200]}",
                    route="instruction", measure="screen", evidence=[json.dumps(g.get("edits"))[:1500]])
            for g in result.get("gaps") or [] if g.get("blocking", True) is False and g.get("asserted_by")
            and policy.UNSETTLED_ASSERTION_BLOCK]       # a clarity advisory (policy, 2026-10-21)
    return out


def advisory(result: dict) -> list[dict]:
    """Gaps the judges demoted on a sentence no check grades: recorded and listed in the delivered package,
    never blocking."""
    from . import policy
    from .shipcheck import finding
    lenient = [] if policy.LENIENCY_FINDINGS_BLOCK else [
        finding("coverage.gap_advisory", f"{g['step']}#{g['index']}",
                f"advisory (a lenient grader, policy.LENIENCY_FINDINGS_BLOCK): {_what(result, g)} while violating: "
                f"{str(g.get('clause'))[:200]} -- {str(g.get('violation'))[:200]}", route="none", measure="none")
        for g in result.get("gaps") or [] if g.get("blocking", True) is not False]
    return lenient + [finding("coverage.gap_advisory", f"{g['step']}#{g['index']}",
                    f"advisory ({(g.get('judgement') or {}).get('why')}): {_what(result, g)} while violating: "
                    f"{str(g.get('clause'))[:200]} -- {str(g.get('violation'))[:200]}",
                    route="none", measure="none", evidence=[json.dumps(g.get("edits"))[:1500]])
            for g in result.get("gaps") or [] if g.get("blocking", True) is False and not g.get("asserted_by")]


# --------------------------------------------------------------------------- the pass audit
#
# User, 2026-10-21 (external audit of Batch 3, class 3): every zero is classified and no full-credit run was ever
# read. pdf-tooling: 8 of 10 Astra runs overwrote the carried paper's numbers 51..54 with 1..4 and PASSED
# test_the_new_section_carries_on_from_the_last_question_of_the_paper (it read the expected value from the solver's
# own output). school-scheduling: Fable runs jRDmqrK, k9P57Dq and oem2t87 merged two teachers whose names differ
# only in a CJK Extension-A character and got full o_identity credit. A calibration solution that passed a check is
# a realistic counter-implementation by construction; the critic is asked to DEMONSTRATE a published clause it
# breaks, as one new hidden test, and the demonstration counts only when it is executed: the reference passes it
# and that solution fails it on an assertion.

PASS_SCHEMA = "arena-coding/pass-audit/1"
PASS_RECEIPT = "verification/pass-audit.json"
#: Characters of a solution's diff shown to the critic.
PASS_DIFF_CAP = 120_000
#: Runners whose selection names a file under tests/suites, so one new file is a check of its own. A check with
#: another runner (go-test, cargo, gradle, command) cannot carry a demonstration alone; it is recorded under the
#: step's `unaudited_checks` -- untested by the audit, never "clean".
DEMO_RUNNERS = ("pytest", "unittest", "node-test", "jest", "vitest", "mocha", "playwright")
#: Repository parts that are never a solution's own work.
_SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".cache"}

PASS_PROMPT = """\
You audit the hidden tests of a coding benchmark task by reading a REAL SOLUTION the hidden tests gave credit. A \
strong model wrote it during calibration; the checks marked PASSED below scored it as correct. Your job is to \
DEMONSTRATE, with one executable hidden test, a published rule this solution breaks while the reference fix keeps it.

You are given the instruction exactly as the solver saw it, the graded checks (each names its runner and the \
instruction sentence it enforces), the reference fix with the reference version of every file it touches, the \
solution's change to the starting repository, and the full hidden test suites.

Write up to {limit} DEMONSTRATIONS. Each must:
- concern a sentence enforced by a check marked PASSED: quote the clause verbatim and name that check;
- be ONE new test file for that check's runner, in the style of the suites shown (it may import their helpers and \
reaches the repository exactly as they do), feeding ONE concrete input the instruction allows and asserting what \
the clause requires for it;
- PASS on the reference fix and FAIL on this solution, on an assertion -- read both carefully, and reach the code \
through the interface the instruction or the suites already use, never a name only one of them has;
- assert only what the quoted clause settles for a careful reader of the instruction: no detail it leaves open, no \
formatting or wording it does not fix.

Do not demonstrate what the suites already catch (this solution passed them), and do not rely on reading the \
hidden tests or the grader. When this solution keeps every published rule, say so: an empty list is a result.

Return ONLY one JSON object in a ```json fenced block:
{{"demonstrations": [{{"check": "<id of a check marked PASSED>", "clause": "<verbatim quote from the instruction>",
  "violation": "<what this solution does wrong>", "input": "<the input, in one or two sentences>",
  "expected": "<what the reference produces for it>", "observed": "<what this solution produces>",
  "file": "<new file name directly under tests/suites, e.g. test_pass_audit_1.py>",
  "select": ["<the runner's selection of that file, in the form the check's own select entries use>"],
  "content": "<the whole test file>"}}]}}

INSTRUCTION
-----------
{instruction}

CHECKS (id | kind | this solution | runner | the sentence it enforces)
------
{checks}

REFERENCE FIX (unified diff over the starting repository)
-------------
{reference}

REFERENCE VERSIONS OF THE FILES THE FIX TOUCHES
-----------------------------------------------
{touched}

THE SOLUTION'S CHANGE (unified diff over the starting repository)
---------------------
{solution}

DECLARED UNDETERMINED (a determinacy probe showed the instruction does not settle these -- do not demonstrate them)
---------------------
{undetermined}

HIDDEN TEST SUITES
------------------
{suites}
"""

PASS_JUDGE_PROMPT = """\
A user gave a developer the instruction below. Two versions of the developer's program were run on the same input \
and gave different results. Judge, as a careful developer who reads only the instruction, which result the user's \
rules require.

INSTRUCTION
-----------
{instruction}

THE RULE IN QUESTION (quoted from the instruction)
--------------------------------------------------
{clause}

THE INPUT
---------
{case}

RESULT {first_label}
--------
{first}

RESULT {second_label}
--------
{second}

Does the instruction clearly settle which result is right, so that every careful developer would agree? Quote the \
one sentence of the instruction that decides it -- or, when it does not settle it, the sentence that leaves it open.

Reply with your reasoning in at most five sentences, then exactly two final lines:
SETTLES: <{first_label}|{second_label}|UNDETERMINED>
SENTENCE: <that sentence, quoted verbatim from the instruction>"""


def _json_file(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _checks(bundle: Path) -> list[dict]:
    doc = _json_file(Path(bundle) / "tests/checks.json")
    return [c for c in (doc or {}).get("checks") or [] if isinstance(c, dict) and c.get("id")] \
        if isinstance(doc, dict) else []


def _grading_sha(bundle: Path) -> str | None:
    """What a solver is graded against (difficulty_gate.grading_sha over the frozen spec), recorded beside the
    artifact identity so a release-repair round can tell its draft from the audited version."""
    spec = _json_file(Path(bundle) / "authoring/spec.json")
    if not isinstance(spec, dict):
        return None
    from .author import difficulty_gate
    return difficulty_gate.grading_sha(spec)


def _campaign_model(path: Path) -> str | None:
    report = _json_file(Path(path) / "report.json")
    if isinstance(report, dict) and (report.get("requested_model") or report.get("model")):
        return str(report.get("requested_model") or report.get("model"))
    return Path(path).parent.name if Path(path).parent.parent.name == "runs" else None


def audited_campaigns(bundle: Path) -> list[dict]:
    """The calibration of record (verification/native-calibrations.json: every primary campaign and its top-ups),
    each resolved to where its trials are -- the recorded path, or a delivered package's runs/<model>/<campaign>.
    A delivered package with no index is read from its runs/<model>/calibration-* folders."""
    bundle = Path(bundle)
    index = _json_file(bundle / "verification/native-calibrations.json")
    index = index if isinstance(index, dict) else {}
    if not index.get("campaigns") and (bundle / "runs").is_dir():
        return [{"campaign": str(p), "path": str(p), "model": _campaign_model(p)}
                for p in sorted((bundle / "runs").glob("*/calibration-*")) if p.is_dir()]
    out = []
    for primary in index.get("campaigns") or []:
        extra = (index.get("topups") or {}).get(str(primary)) or []
        for campaign in [primary, *(extra if isinstance(extra, list) else [])]:
            path = Path(str(campaign))
            if not path.is_dir() and (bundle / "runs").is_dir():
                copies = sorted((bundle / "runs").glob(f"*/{path.name}"))
                path = copies[0] if copies else path
            out.append({"campaign": str(campaign), "path": str(path), "model": _campaign_model(path)})
    return out


def _files(root: Path) -> dict[str, Path]:
    out = {}
    if not Path(root).is_dir():
        return out
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root)
        if set(rel.parts) & _SKIP_PARTS or path.is_symlink() or not path.is_file():
            continue
        out[rel.as_posix()] = path
    return out


def tree_key(repo: Path) -> str:
    """One solution, however many runs left it: the content of its final tree (vendored parts aside)."""
    h = hashlib.sha256()
    for rel, path in _files(Path(repo)).items():
        h.update(f"{rel}\0{_digest(path)}\n".encode())
    return h.hexdigest()


def solution_diff(base: Path, head: Path, cap: int = PASS_DIFF_CAP) -> str:
    """A unified diff from `base` (the fixture) to `head` (a solution's final tree): what the calibration run
    changed, in the form an m_partial_ mutant patch takes."""
    before, after = _files(Path(base)), _files(Path(head))
    parts, total, left = [], 0, 0
    for rel in sorted(set(before) | set(after)):
        a = before[rel].read_bytes() if rel in before else None
        b = after[rel].read_bytes() if rel in after else None
        if a == b:
            continue
        old, new = (f"a/{rel}" if a is not None else "/dev/null"), (f"b/{rel}" if b is not None else "/dev/null")
        try:
            lines = difflib.unified_diff((a or b"").decode("utf-8").splitlines(keepends=True),
                                         (b or b"").decode("utf-8").splitlines(keepends=True), old, new)
            text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
        except UnicodeDecodeError:
            text = f"Binary files {old} and {new} differ\n"
        if total + len(text) > cap:
            left += 1
            continue
        parts.append(text)
        total += len(text)
    if left:
        parts.append(f"... [{left} more changed file(s) not shown]\n")
    return "".join(parts)


_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", re.M)


def _within(name: str, entry: str) -> bool:
    return name == entry or name.startswith(entry + "::") or name.startswith(entry + "[")


def _failing_tests(check: dict, record: dict) -> list[str] | None:
    """The selected tests a failed check's runner names as failing, or None when its output does not say (only
    pytest's summary names them; node-test, for one, reports nothing the grade record keeps)."""
    runner = check.get("runner") or {}
    if runner.get("type") != "pytest" or record.get("timed_out") or record.get("error"):
        return None
    names = _PYTEST_FAILED.findall(f"{record.get('stdout') or ''}\n{record.get('stderr') or ''}")
    select = [s for s in runner.get("select") or [] if isinstance(s, str)]
    if not names or not select or not all(any(_within(n, s) for s in select) for n in names):
        return None
    return [s for s in select if any(_within(n, s) for n in names)]


def credit(checks: list[dict], records: dict) -> dict:
    """What a grade gave credit for, as units (check, selected test) -- (check, "*") for the check as a whole.

    A check fails when any of its tests does, so a run can pass the one test that holds a published rule and still
    lose the check on another (pdf-tooling, 2026-10-21: 8 of 10 Astra runs passed
    test_the_new_section_carries_on_from_the_last_question_of_the_paper while o_labels_digits failed on its decoy
    test). The pass audit reads test-level credit wherever the runner names its failures."""
    passed, failed = set(), set()
    for c in checks:
        record = records.get(c.get("id")) if isinstance(records, dict) else None
        if c.get("kind") != "objective" or not isinstance(record, dict) or record.get("gated_out"):
            continue
        select = [s for s in (c.get("runner") or {}).get("select") or [] if isinstance(s, str)]
        if record.get("passed") is True:
            passed |= {(c["id"], "*")} | {(c["id"], s) for s in select}
            continue
        failed.add((c["id"], "*"))
        failing = _failing_tests(c, record)
        if failing is not None:
            failed |= {(c["id"], s) for s in failing}
            passed |= {(c["id"], s) for s in select if s not in failing}
    return {"passed": sorted(list(u) for u in passed), "failed": sorted(list(u) for u in failed)}


def newly_failed(before: dict, now: dict) -> list[list[str]]:
    """Units `now` fails that `before` gave credit for, or that did not exist then (a new check or test)."""
    then = {tuple(u) for u in before.get("passed") or []}
    known = then | {tuple(u) for u in before.get("failed") or []}
    return sorted(list(u) for u in {tuple(u) for u in now.get("failed") or []} if u in then or u not in known)


def passing_solutions(bundle: Path, campaigns: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    """Per step, the distinct final trees the calibration runs left behind, each with the objective checks the
    shipped grader gave it credit for -- `passed` whole, `partial` test by test (`credit`) -- identical trees once,
    every run that left one listed; and what could not be read -- an unreadable campaign is unverified, never
    clean."""
    bundle = Path(bundle)
    steps = _steps(bundle)
    defined = _checks(bundle)
    found: dict[str, dict[str, dict]] = {s: {} for s in steps}
    problems = []
    for campaign in campaigns:
        runs = Path(campaign["path"]) / "jobs/run"
        trials = sorted(p for p in runs.iterdir() if p.is_dir() and (p / "steps").is_dir()) if runs.is_dir() else []
        if not trials:
            problems.append(f"{campaign['campaign']}: no calibration trial is readable at {runs}")
            continue
        readable = 0
        for trial in trials:
            history: dict[str, str] = {}
            for step in steps:
                folder = trial / "steps" / step
                capture = folder / "artifacts/app"
                grade = _json_file(folder / "verifier/arena-grade.json")
                checks = grade.get("checks") if isinstance(grade, dict) else None
                if isinstance(checks, dict) and (capture / "repo").is_dir():
                    readable += 1
                    units = credit([c for c in defined if c.get("step") in (None, step)], checks)
                    passed = sorted({cid for cid, test in units["passed"] if test == "*"})
                    partial = {}
                    for cid, test in units["passed"]:
                        if test != "*" and cid not in passed:
                            partial.setdefault(cid, []).append(test)
                    if passed or partial:
                        run, key = {"model": campaign["model"], "trial": trial.name}, tree_key(capture / "repo")
                        entry = found[step].setdefault(key, {
                            "key": key, **run, "campaign": campaign["campaign"],
                            "capture": str(capture), "history": dict(history), "passed": passed,
                            "partial": partial, "credited": sorted(set(passed) | set(partial)),
                            "credit_before": units, "runs": []})
                        entry["runs"].append(run)
                if capture.is_dir():
                    history[step] = str(capture)
        if not readable:
            problems.append(f"{campaign['campaign']}: {len(trials)} trial(s), none with a readable grade record "
                            "and captured tree")
    return {s: list(v.values()) for s, v in found.items()}, problems


def select_solutions(solutions: list[dict], cap: int, audited: set[str]) -> list[dict]:
    """Up to `cap` distinct solutions per audited objective check from EACH model family; one pick counts for every
    check it was given credit for. Within a family a solution whose credit differs from the ones already read comes
    first: the same credit is likely the same behaviour.

    Per family, not per check: the two families miss different things (the lever doctrine), and a cap shared by both
    read only Astra's o_identity solutions of school-scheduling (real campaigns, 2026-10-21), never the three Fable
    runs that merged two teachers."""
    chosen, keys = [], set()
    signature = {s["key"]: tuple(map(tuple, (s.get("credit_before") or {}).get("passed") or [])) for s in solutions}
    for cid in sorted(audited):
        for model in sorted({str(s.get("model")) for s in solutions}):
            mine = [s for s in solutions if str(s.get("model")) == model and cid in s["credited"]]
            have = sum(1 for s in mine if s["key"] in keys)
            seen = {signature[s["key"]] for s in chosen if str(s.get("model")) == model}
            left = [s for s in mine if s["key"] not in keys]
            while have < cap and left:
                pick = min(left, key=lambda s: (signature[s["key"]] in seen, str(s.get("trial"))))
                left.remove(pick)
                chosen.append(pick)
                keys.add(pick["key"])
                seen.add(signature[pick["key"]])
                have += 1
    return chosen


def build_pass_prompt(bundle: Path, step: str, tree: Path, solution: dict, audited: set[str],
                      limit: int) -> str:
    bundle = Path(bundle)
    instruction = (bundle / "steps" / step / "instruction.md").read_text()
    quotes = _rule_quotes(bundle)
    rows = []
    for c in _checks(bundle):
        if c.get("step") not in (None, step):
            continue
        cid = c["id"]
        partial = (solution.get("partial") or {}).get(cid)
        state = ("guardrail" if c.get("kind") == "guardrail"
                 else "failed" if cid not in solution["credited"]
                 else "passed (its runner cannot carry a demonstration)" if cid not in audited
                 else "PASSED" if not partial
                 else f"PASSED these of its tests, failed the rest: {', '.join(partial)}")
        rows.append(f"{cid} | {c.get('kind')} | {state} | {json.dumps(c.get('runner'), ensure_ascii=False)[:300]} | "
                    f"{' / '.join(quotes.get(cid, (None, []))[1])[:400]}")
    patch = (bundle / "steps" / step / "solution/fix.patch").read_text()
    touched = []
    for rel in _patch_paths(patch):
        path = tree / rel
        if path.is_file():
            touched.append(f"=== {rel}\n{path.read_text(errors='replace')[:TOUCHED_FILE_CAP]}")
    diff = solution_diff(bundle / "environment/repo", Path(solution["capture"]) / "repo")
    return PASS_PROMPT.format(limit=limit, instruction=instruction.strip(), checks="\n".join(rows),
                              reference=patch.strip(), touched="\n\n".join(touched) or "(none)",
                              solution=diff.strip() or "(no change)", undetermined=_undetermined(bundle) or "(none)",
                              suites=_suites_text(bundle))


def parse_demonstrations(text: str, limit: int) -> list[dict] | None:
    """The demonstrations of an answer, or None when it holds no JSON object in the required shape."""
    blocks = re.findall(r"```json\s*(.*?)```", text or "", re.S) or [text or ""]
    for block in reversed(blocks):
        try:
            doc = json.loads(block)
        except ValueError:
            continue
        found = doc.get("demonstrations") if isinstance(doc, dict) else None
        if isinstance(found, list):
            return [d for d in found if isinstance(d, dict)][:limit]
    return None


def _demo_problem(demo: dict, solution: dict, audited: set[str], taken: set[str], selected: set[str]) -> str | None:
    """Why a demonstration cannot be executed as one check of its own, or None."""
    if demo.get("check") not in solution["credited"] or demo.get("check") not in audited:
        return "it names no check this solution passed whose runner can carry a demonstration"
    name = str(demo.get("file") or "")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) or ".." in name:
        return "its file must be a plain new name directly under tests/suites"
    if name in taken:
        return "its file already exists in the suites"
    if not isinstance(demo.get("content"), str) or not demo["content"].strip():
        return "its test file is empty"
    stem = name.rsplit(".", 1)[0]
    select = demo.get("select")
    if not isinstance(select, list) or not select or not all(isinstance(s, str) and stem in s for s in select):
        return "every entry of its selection must name its own file"
    if set(select) & selected:
        return "its selection is an existing check's selection"
    return None


def _rename(demo: dict, taken: set[str]) -> None:
    """Two demonstrations of one step that chose the same file name each keep a file of their own."""
    name = str(demo["file"])
    if name not in taken:
        return
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    new_stem = f"{stem}_{demo['n']}"
    demo["select"] = [s.replace(name, new_stem + dot + ext) if name in s else s.replace(stem, new_stem)
                      for s in demo["select"]]
    demo["file"] = new_stem + dot + ext


def _demo_bundle(bundle: Path, step: str, demos: list[dict], dest: Path) -> Path:
    """A copy of the bundle whose grader carries each demonstration as an objective check of its own (pa_<n>) in
    `step`. Only the copy changes; the grader runtime is the bundle's own, byte for byte."""
    bundle = Path(bundle)

    def ignore(folder, names):
        return {"runs", "verification", ".git"} & set(names) if Path(folder) == bundle else set()
    shutil.copytree(bundle, dest, symlinks=True, ignore=ignore)
    doc = json.loads((dest / "tests/checks.json").read_text())
    rubric_path = dest / "tests/rubric.json"
    rubric = _json_file(rubric_path)
    by_id = {c["id"]: c for c in doc.get("checks") or [] if isinstance(c, dict) and c.get("id")}
    for d in demos:
        target = by_id[d["check"]]
        path = dest / "tests/suites" / d["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(d["content"])
        doc["checks"].append({**target, "id": d["check_id"], "kind": "objective", "weight": 1, "requires": [],
                              "runner": {**(target.get("runner") or {}), "select": list(d["select"])}})
        if isinstance(rubric, dict):
            entry = rubric.setdefault(step, [])
            if isinstance(entry, list):
                entry.append(d["check_id"])
            elif isinstance(entry, dict):
                entry[d["check_id"]] = entry.get(d["check"], {})
    (dest / "tests/checks.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    if isinstance(rubric, dict):
        rubric_path.write_text(json.dumps(rubric, indent=2, ensure_ascii=False))
    return dest


def _failed(outcome: dict, cid: str) -> bool:
    record = ((outcome.get("grade") or {}).get("checks") or {}).get(cid) if outcome.get("valid") is True else None
    return isinstance(record, dict) and record.get("passed") is False and not record.get("gated_out")


def execute_demonstrations(bundle: Path, step: str, demos: list[dict], solutions: list[dict], *,
                           workers: int = 3) -> list[str]:
    """Grade every demonstration of `step` on the reference and on every distinct calibration solution of the step
    that passed a check they concern. Marks each demonstration; returns what leaves the step unverified.

    A demonstration holds only when the reference passes it and its own solution fails it on an assertion (or a
    crash, or an unreadable output) -- genuine.classify, the certificate's own reading: a solution that fails it by
    an import or a missing name was never shown to break the rule."""
    from . import genuine
    from .author import sandbox
    bundle = Path(bundle)
    targets = {d["check"] for d in demos}
    pool = [s for s in solutions if targets & set(s["credited"])]
    keyed = {s["key"]: s for s in pool}
    with tempfile.TemporaryDirectory(prefix="pass-audit-") as tmp:
        copy = _demo_bundle(bundle, step, demos, Path(tmp) / "bundle")
        try:
            agent_tag = sandbox.build_image(copy, "agent")
            verifier_tag = sandbox.build_image(copy, "verifier")
            trial = sandbox.verify(copy, {s: sandbox.solve_variant(copy, s) for s in _steps(copy)},
                                   agent_tag=agent_tag, verifier_tag=verifier_tag)
        except Exception as exc:                          # noqa: BLE001 -- unexecuted is unverified, never clean
            why = f"{type(exc).__name__}: {str(exc)[-300:]}"
            for d in demos:
                d.update(demonstrated=False, error=f"not executed: {why}")
            return [f"{step}: the demonstrations could not be graded on the reference ({why})"]
        reference = (trial.get("steps") or {}).get(step) or {}
        if reference.get("valid") is not True:
            why = str(reference.get("error") or "the reference's grade is not valid")[:300]
            for d in demos:
                d.update(demonstrated=False, error=f"not executed: {why}")
            return [f"{step}: the reference did not grade with the demonstrations ({why})"]
        passed_by_reference = {cid for cid, rec in ((reference.get("grade") or {}).get("checks") or {}).items()
                               if isinstance(rec, dict) and rec.get("passed") is True}

        def grade(solution):
            try:
                return solution["key"], sandbox.grade_capture(
                    copy, step, Path(solution["capture"]), verifier_tag=verifier_tag,
                    history={k: Path(v) for k, v in (solution.get("history") or {}).items()})
            except Exception as exc:                      # noqa: BLE001 -- recorded on the demonstration
                return solution["key"], {"valid": False, "error": f"{type(exc).__name__}: {str(exc)[-300:]}"}

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            graded = dict(ex.map(grade, pool))
        suites = {p.name for p in (copy / "tests/suites").rglob("*") if p.is_file()}
        problems = []
        for d in demos:
            cid = d["check_id"]
            d["reference_passed"] = cid in passed_by_reference
            outcome = graded.get(d["solution"]) or {}
            record = ((outcome.get("grade") or {}).get("checks") or {}).get(cid)
            if outcome.get("valid") is not True or not isinstance(record, dict):
                why = str(outcome.get("error") or "no grade record for the demonstration")[:300]
                d.update(demonstrated=False, error=f"not executed on its solution: {why}")
                problems.append(f"{step} {d.get('trial')}: demonstration {cid} not executed on its solution ({why})")
                continue
            d["solution_failed"] = _failed(outcome, cid)
            kind, detail = genuine.classify(record, suites) if d["solution_failed"] else (None, "")
            if kind == "suspect" and str(detail).startswith("the test produced no output"):
                try:
                    text = genuine.explain(copy, step, Path(keyed[d["solution"]]["capture"]), cid,
                                           verifier_tag=verifier_tag)
                except Exception:                         # noqa: BLE001 -- stays suspect
                    text = None
                if text:
                    kind, detail = genuine.classify({**record, "stdout": text, "stderr": ""}, suites)
            d["failure"] = {"kind": kind, "detail": str(detail)[:400]} if kind else None
            d["demonstrated"] = bool(d["reference_passed"] and kind in genuine.CERTIFIED_KINDS)
            if not d["reference_passed"]:
                d["error"] = "the reference fails it too: not a demonstration"
            elif not d["demonstrated"]:
                d["error"] = ("its solution passes it" if not d["solution_failed"]
                              else f"its solution fails it by {kind}, not by breaking the rule")
            d["violators"] = [run for s in pool if d["check"] in s["credited"]
                              and _failed(graded.get(s["key"]) or {}, cid) for run in s["runs"]]
    return problems


def judge_demonstration(instruction: str, demo: dict, *, ask=None, models: tuple[str, ...] = JUDGES) -> dict:
    """Does the instruction settle the demonstrated input the reference's way? (user, 2026-10-21)

    A calibration solution is a realistic mistake by construction, so the "contrived" demotion never applies. The
    "instruction does not clearly settle it" demotion does: a family answering UNDETERMINED, or choosing the
    solution's result, doubts the sentence -- and only a doubt that quotes the deciding sentence verbatim from the
    instruction demotes, so every demotion carries its sentence. A judge that errs or answers nothing demotes nothing.
    The two results are shown in an order fixed by the case, not reference-first."""
    if ask is None:
        from .author.determinacy import _ask as ask
    case = str(demo.get("input") or "").strip()
    reference_first = int(hashlib.sha256(f"{demo.get('clause')}\0{case}".encode()).hexdigest(), 16) % 2 == 0
    labels = {"reference": "X" if reference_first else "Y", "solution": "Y" if reference_first else "X"}
    shown = (demo.get("expected"), demo.get("observed")) if reference_first else (demo.get("observed"), demo.get("expected"))
    prompt = PASS_JUDGE_PROMPT.format(instruction=instruction.strip(), clause=str(demo.get("clause") or "").strip(),
                                      case=case[:6000], first_label="X", first=str(shown[0] or "")[:6000],
                                      second_label="Y", second=str(shown[1] or "")[:6000])
    answers = {}
    for model in models:
        try:
            text = ask(model, prompt)
            sentence = _sentence(text)
            answers[model] = {"settles": _verdict(text, "SETTLES"), "sentence": sentence,
                              "sentence_found": in_instruction(sentence, instruction), "reasoning": (text or "")[-900:]}
        except Exception as exc:                          # noqa: BLE001 -- an unanswered judge demotes nothing
            answers[model] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    doubting = sorted(m for m, a in answers.items() if a.get("settles") in ("UNDETERMINED", labels["solution"]))
    doubted = [m for m in doubting if answers[m].get("sentence_found")]
    arbiter = None
    if doubted and any(a.get("settles") == labels["reference"] for a in answers.values()):
        # The families split: the arbiter decides; an erring arbiter leaves the doubt standing.
        arbiter = _arbitrate(prompt, "SETTLES", ask)
        if arbiter.get("settles") == labels["reference"]:
            doubting, doubted = [], []
    if doubted:
        why = "the instruction does not clearly settle it (" + ", ".join(doubted) + ")"
    elif doubting:
        why = f"doubted without quoting the deciding sentence ({', '.join(doubting)}): not a demotion"
    else:
        why = "settled the reference's way, or no judge answered"
    return {"models": list(models), "labels": labels, "answers": answers, "blocking": not doubted,
            "unsettled": bool(doubted), "doubted": doubted,
            "sentence": next((answers[m]["sentence"] for m in doubted), None),
            "realistic": "by construction: a calibration solution", "why": why,
            **({"arbiter": arbiter} if arbiter else {})}


def _route(bundle: Path, gap: dict) -> bool:
    """Classify a demonstrated gap: settled -> blocking, route "hidden" (the demonstration becomes a hidden case);
    unsettled on a sentence a check grades -> route "instruction"; unsettled otherwise -> advisory. True when open."""
    judgement = gap.get("judgement") or {}
    gap.pop("asserted_by", None)
    if not judgement.get("unsettled"):
        gap.update(blocking=True, route="hidden")
        return True
    by = asserting_checks(bundle, gap.get("step"), [gap.get("clause"), judgement.get("sentence")])
    gap.update(blocking=False, route="instruction" if by else "none", asserted_by=by)
    return bool(by)


def _earlier_pass_record(bundle: Path) -> tuple[Path, dict] | None:
    """The newest EARLIER version of the lineage with a pass audit: its open gaps answer to this version."""
    bundle = Path(bundle)
    n = _version_number(bundle)
    if n is None or bundle.parent.name != "hardened" or not bundle.parent.is_dir():
        return None
    earlier = sorted((p for p in bundle.parent.iterdir()
                      if p.is_dir() and _version_number(p) is not None and _version_number(p) < n),
                     key=_version_number, reverse=True)
    for p in earlier:
        record = _json_file(p / PASS_RECEIPT)
        if isinstance(record, dict) and record.get("schema") == PASS_SCHEMA:
            return p, record
    return None


def recheck_carried(bundle: Path, source_dir: Path, source: dict, *, judge=None,
                    workers: int = 3) -> tuple[list[dict], list[dict], list[dict]]:
    """(open, advisory, closed): the open gaps of the lineage's newest earlier pass audit, on THIS version.

    Closed-and-verified, as for the critic: a gap closes only when this version's grader, re-grading the same
    captured run, fails a check or test it gave credit for there (or one that did not exist) -- the hidden case now
    bites. A run that cannot be re-graded stays open. A gap whose step instruction changed is judged again."""
    from .author import sandbox
    bundle = Path(bundle)
    gaps = [dict(g) for g in source.get("gaps") or [] if isinstance(g, dict)]
    if not gaps:
        return [], [], []
    defined = _checks(bundle)
    try:
        tag, unbuilt = sandbox.build_image(bundle, "verifier"), None
    except Exception as exc:                              # noqa: BLE001 -- every carried gap stays open
        tag, unbuilt = None, f"{type(exc).__name__}: {str(exc)[-300:]}"

    def regrade(g):
        capture = Path(str(g.get("capture")))
        if tag is None:
            return {"valid": False, "error": unbuilt}
        if not (capture / "repo").is_dir():
            return {"valid": False, "error": f"the captured run is gone ({capture})"}
        try:
            return sandbox.grade_capture(bundle, str(g.get("step")), capture, verifier_tag=tag,
                                         history={k: Path(v) for k, v in (g.get("history") or {}).items()})
        except Exception as exc:                          # noqa: BLE001 -- not re-graded is not closed
            return {"valid": False, "error": f"{type(exc).__name__}: {str(exc)[-300:]}"}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(regrade, gaps))
    still, demoted, closed = [], [], []
    for g, outcome in zip(gaps, results):
        g["carried_from"] = g.get("carried_from") or Path(source_dir).name
        if "." not in str(g.get("id")):
            g["id"] = f"{Path(source_dir).name}.{g.get('id')}"
        if outcome.get("valid") is not True:
            g["recheck"] = {"error": str(outcome.get("error") or "the re-grade is not valid")[:300]}
            still.append(g)
            continue
        now = credit([c for c in defined if c.get("step") in (None, g.get("step"))],
                     (outcome.get("grade") or {}).get("checks") or {})
        newly = newly_failed(g.get("credit_before") or {}, now)
        g["recheck"] = {"reward": outcome.get("reward"), "newly_failed": newly}
        if newly:
            closed.append(g)
            continue
        path = bundle / "steps" / str(g.get("step")) / "instruction.md"
        digest = _digest(path)
        if path.is_file() and (g.get("judgement") or {}).get("instruction_sha256") != digest:
            g["judgement"] = {**judge_demonstration(path.read_text(encoding="utf-8", errors="replace"), g, ask=judge),
                              "instruction_sha256": digest}
        (still if _route(bundle, g) else demoted).append(g)
    return still, demoted, closed


def _audit_step(bundle: Path, step: str, pool: list[dict], result: dict, *, model: str, ask, judge, workers: int,
                record_dir: Path | None) -> None:
    from . import policy
    by_id = {c["id"]: c for c in _checks(bundle)}
    credited = {cid for s in pool for cid in s["credited"]}
    audited = {cid for cid in credited if ((by_id.get(cid) or {}).get("runner") or {}).get("type") in DEMO_RUNNERS}
    chosen = select_solutions(pool, policy.PASS_AUDIT_SOLUTIONS_PER_CHECK, audited)
    picked = {s["key"] for s in chosen}
    entry = {"solutions": [{"key": s["key"], "model": s["model"], "trial": s["trial"], "passed": s["passed"],
                            "partial": s["partial"], "runs": s["runs"], "audited": s["key"] in picked} for s in pool],
             "unaudited_checks": sorted(credited - audited), "demonstrations": []}
    result["steps"][step] = entry
    if not chosen:
        return
    limit = policy.PASS_AUDIT_DEMONSTRATIONS
    with tempfile.TemporaryDirectory(prefix="pass-audit-ref-") as tmp:
        tree = reference_tree(bundle, step, Path(tmp) / "repo")

        def one(solution):
            text = ""
            try:
                prompt = build_pass_prompt(bundle, step, tree, solution, audited, limit)
                text = ask(model, prompt)
                if not str(text or "").strip() and ask is _ask:
                    text = ask(model, prompt, effort="medium", max_tokens=64000)
                found = parse_demonstrations(text, limit)
                if found is None and str(text or "").strip() and ask is _ask:
                    text = ask(model, prompt + "\n\nYour previous answer contained no JSON object in the required "
                               "shape. Answer again with the ```json block.", effort="medium", max_tokens=64000)
                    found = parse_demonstrations(text, limit)
            except Exception as exc:                      # noqa: BLE001 -- not audited, recorded as unverified
                return solution, None, text, f"{type(exc).__name__}: {str(exc)[:300]}"
            return solution, found, text, None if found is not None else "no answer in the required JSON shape"

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            answers = list(ex.map(one, chosen))
    taken = {p.relative_to(bundle / "tests/suites").as_posix() for p in (bundle / "tests/suites").rglob("*")} \
        if (bundle / "tests/suites").is_dir() else set()
    selected = {s for c in by_id.values() for s in ((c.get("runner") or {}).get("select") or []) if isinstance(s, str)}
    demos, mine = [], set()
    for solution, found, text, error in answers:
        row = next(r for r in entry["solutions"] if r["key"] == solution["key"])
        row["raw_chars"] = len(text or "")
        if record_dir is not None:
            raw = Path(record_dir) / "pass-audit-raw"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / f"{step}.{solution['trial']}.txt").write_text(str(text or ""))
        if error:
            row["error"] = error
            result["incomplete"].append(f"{step} {solution['model']} {solution['trial']}: not audited ({error})")
            continue
        for d in found:
            n = len(entry["demonstrations"]) + 1
            demo = {"n": n, "check_id": f"pa_{n}", "solution": solution["key"], "model": solution["model"],
                    "trial": solution["trial"],
                    **{k: d.get(k) for k in ("check", "clause", "violation", "input", "expected", "observed",
                                             "file", "select", "content")}}
            why = _demo_problem(demo, solution, audited, taken, selected)
            if why:
                demo.update(demonstrated=False, error=why)
            else:
                _rename(demo, mine)
                mine.add(demo["file"])
                demos.append(demo)
            entry["demonstrations"].append(demo)
    if demos:
        result["incomplete"] += execute_demonstrations(bundle, step, demos, pool, workers=workers)
    keyed = {s["key"]: s for s in pool}
    shown = [d for d in demos if d.get("demonstrated")]
    path = bundle / "steps" / step / "instruction.md"
    instruction, digest = path.read_text(encoding="utf-8", errors="replace"), _digest(path)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        judgements = list(ex.map(lambda d: {**judge_demonstration(instruction, d, ask=judge),
                                            "instruction_sha256": digest}, shown))
    for d, judgement in zip(shown, judgements):
        solution = keyed[d["solution"]]
        gap = {"step": step, "id": f"p{d['n']}",
               **{k: d.get(k) for k in ("check", "clause", "violation", "input", "expected", "observed", "file",
                                        "select", "content", "violators", "failure")},
               "credited_tests": (solution.get("partial") or {}).get(d.get("check")),
               "model": solution["model"], "trial": solution["trial"], "runs": solution["runs"],
               "capture": solution["capture"], "history": solution["history"],
               "credit_before": solution["credit_before"], "judgement": judgement}
        (result["gaps"] if _route(bundle, gap) else result["advisory"]).append(gap)


def pass_audit(bundle: Path, *, model: str | None = None, ask=None, judge=None, workers: int | None = None,
               record_dir: Path | None = None, campaigns: list[dict] | None = None) -> dict:
    """Audit the calibration solutions that passed each objective check (see the notes above `PASS_SCHEMA`).

    Per step: the distinct passing solutions, at most policy.PASS_AUDIT_SOLUTIONS_PER_CHECK per check, each read
    by the critic for at most policy.PASS_AUDIT_DEMONSTRATIONS demonstrations; every demonstration executed on the
    reference and on every passing solution; each demonstrated violation judged by both calibration families.
    The open gaps of the lineage's previous audit are re-checked on this version. The receipt is bound to the
    artifact identity and the grading sha, and records the campaigns it read; anything not read, asked or executed
    makes it incomplete -- unverified, never clean."""
    from . import policy
    from .artifacts import identity
    from .author.transport import MODEL
    workers = policy.PASS_AUDIT_WORKERS if workers is None else workers
    bundle = Path(bundle).resolve()
    model = model or MODEL
    ask = ask or _ask
    campaigns = audited_campaigns(bundle) if campaigns is None else campaigns
    result = {"schema": PASS_SCHEMA, "artifact_sha256": identity(bundle), "grading_sha256": _grading_sha(bundle),
              "rule": "pass audit (user, 2026-10-21)", "model": model,
              "campaigns": [c["campaign"] for c in campaigns],
              "limits": {"solutions_per_check": policy.PASS_AUDIT_SOLUTIONS_PER_CHECK,
                         "demonstrations": policy.PASS_AUDIT_DEMONSTRATIONS},
              "steps": {}, "gaps": [], "advisory": [], "closed": [], "incomplete": []}
    if not campaigns:
        result["incomplete"].append("no calibration campaign is on record to audit")
    solutions, unreadable = passing_solutions(bundle, campaigns)
    result["incomplete"] += unreadable
    for step in _steps(bundle):
        _audit_step(bundle, step, solutions.get(step) or [], result, model=model, ask=ask, judge=judge,
                    workers=workers, record_dir=record_dir)
    earlier = _earlier_pass_record(bundle)
    if earlier is not None:
        still, demoted, closed = recheck_carried(bundle, *earlier, judge=judge, workers=workers)
        result["carried_from"] = str(earlier[0])
        result["gaps"] += still
        result["advisory"] += demoted
        result["closed"] += closed
    result["complete"] = not result["incomplete"]
    if record_dir is not None:
        from .artifacts import atomic_json
        folder = Path(record_dir) / "pass-audit"
        for g in result["gaps"] + result["advisory"]:
            if g.get("patch") or not (Path(str(g.get("capture"))) / "repo").is_dir():
                continue
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{g['step']}.{g['id']}.patch"
            target.write_text(solution_diff(bundle / "environment/repo", Path(g["capture"]) / "repo", cap=10 ** 9))
            g["patch"] = str(target)
        atomic_json(Path(record_dir) / Path(PASS_RECEIPT).name, result)
    return result


def _pass_text(g: dict) -> str:
    who = f"calibration run {str(g.get('model')).split('/')[-1]} {g.get('trial')}"
    runs = len(g.get("violators") or g.get("runs") or [])
    tests = list(g.get("credited_tests") or [])
    what = (f"{', '.join(tests[:3])}{f' (+{len(tests) - 3} more)' if len(tests) > 3 else ''} of {g.get('check')}"
            if tests else g.get("check"))
    text = (f"{who} passed {what} while violating: {str(g.get('clause'))[:200]} -- on "
            f"{str(g.get('input'))[:200]} it gives {str(g.get('observed'))[:160]}, the reference "
            f"{str(g.get('expected'))[:160]} ({runs} calibration run(s) in all)")
    recheck = g.get("recheck") or {}
    if recheck.get("error"):
        text += f"; not re-graded on this version ({str(recheck['error'])[:160]})"
    elif g.get("carried_from"):
        text += f"; still earns every check it passed on {g['carried_from']}"
    return text


def pass_findings(record: dict) -> list[dict]:
    """The pass audit's open gaps: `pass.gap` (route "hidden", only while policy.LENIENCY_FINDINGS_BLOCK) or
    `pass.unsettled` (route "instruction")."""
    from . import policy
    from .shipcheck import finding
    out = []
    for g in record.get("gaps") or []:
        subject = f"{g.get('step')}#{g.get('id')}"
        evidence = [str(g.get("content") or "")[:1500]] + ([str(g["patch"])] if g.get("patch") else [])
        if g.get("route") == "instruction" and not policy.UNSETTLED_ASSERTION_BLOCK:
            continue                                     # a clarity advisory (policy, 2026-10-21)
        if g.get("route") == "instruction":
            out.append(finding("pass.unsettled", subject,
                               f"{unsettled_text(g.get('judgement') or {}, g.get('asserted_by') or [], g.get('clause'))}"
                               f" -- {_pass_text(g)}", route="instruction", measure="screen", evidence=evidence))
        elif policy.LENIENCY_FINDINGS_BLOCK:            # a wrong solution passing is an advisory (policy, 2026-10-21)
            out.append(finding("pass.gap", subject, _pass_text(g), route="hidden", measure="regrade",
                               evidence=evidence))
    return out


def pass_advisory(record: dict) -> list[dict]:
    """Demonstrated violations on a sentence the instruction does not settle and no check grades: recorded with
    the deciding sentence, never blocking."""
    from .shipcheck import finding
    return [finding("pass.gap_advisory", f"{g.get('step')}#{g.get('id')}",
                    f"advisory ({(g.get('judgement') or {}).get('why')}; the sentence: "
                    f"'{str((g.get('judgement') or {}).get('sentence'))[:200]}'): {_pass_text(g)}",
                    route="none", measure="none", evidence=[str(g.get("content") or "")[:1500]])
            for g in record.get("advisory") or []]


def pass_problems(bundle: Path, sha: str | None = None) -> list[str]:
    """Release's reading of the pass audit of the calibration of record (release._evaluate). Missing, stale or
    incomplete is unverified, never clean; every open gap blocks."""
    from .artifacts import identity
    bundle = Path(bundle)
    sha = sha or identity(bundle)
    index = _json_file(bundle / "verification/native-calibrations.json")
    if not isinstance(index, dict) or index.get("artifact_sha256") != sha:
        return []                     # no calibration of record for this version: the calibration check blocks
    record = _json_file(bundle / PASS_RECEIPT)
    if not isinstance(record, dict) or record.get("artifact_sha256") != sha:
        return ["the pass audit has not run on this version's calibration of record"]
    if record.get("campaigns") != [c["campaign"] for c in audited_campaigns(bundle)]:
        return ["the pass audit read other campaigns than the calibration of record"]
    if not record.get("complete"):
        return ["unverified, run it again: " + "; ".join(map(str, record.get("incomplete") or ["incomplete"]))[:600]]
    return [f"{f['subject']}: {f['detail']}" for f in pass_findings(record)]


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundles", nargs="+", type=Path)
    ap.add_argument("--model", default=None)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pass-audit", action="store_true",
                    help="audit the calibration solutions that passed each check instead (verification/pass-audit.json)")
    args = ap.parse_args(argv)
    if args.pass_audit:
        worst = 0
        for bundle in args.bundles:
            record = pass_audit(bundle, model=args.model, workers=args.workers, record_dir=bundle / "verification")
            print(f"{bundle}: {len(record['gaps'])} open, {len(record['advisory'])} advisory, "
                  f"{len(record['closed'])} closed; complete {record['complete']}")
            for f in pass_findings(record):
                print(f"  {f['check']} {f['subject']}: {f['detail'][:240]}")
            for why in record["incomplete"][:6]:
                print(f"  INCOMPLETE {why[:240]}")
            worst = max(worst, 1 if record["gaps"] or not record["complete"] else 0)
        return worst
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
