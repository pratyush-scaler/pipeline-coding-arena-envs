"""Arena's package format (Scaler x Arena weekly sync, 2026-10-22, "Formatting Changes - P0").

A delivered package (pipeline.deliver plus the packaging step's runs/, FAILURES.md and REWARD-HACKING.md) is
exported into the layout Arena reads, without changing a byte the agent sees or the grader runs:

  <env-id>/task.toml                    Harbor 0.24 keys, plus top-level `type`, `deliverables`, `protected_paths`
  <env-id>/declarations.json            the unusual -- root, network, vendored, broken, large; [] when none
  <env-id>/README.md                    README_HEADINGS, in that order, in every package
  <env-id>/PACKAGE-MANIFEST.json        every file: path, size, sha256, executable
  <env-id>/environment/                 unchanged
  <env-id>/steps/<s>/instruction.md     unchanged
  <env-id>/steps/<s>/solution/solve.sh  executable; the root solution/ is dropped
  <env-id>/tests/                       the unchanged grader, plus rubrics/<s>.json and expected/<s>/
  <env-id>/calibration/                 metrics.json, runs.csv, trials/<model>/<run>/ with run.json and per-step diffs
  <env-id>/provenance/                  source-scope.json, ledger.json (Arena's shape; the pipeline's ledger nested)
  <env-id>/verification/                receipt.json, oracle/, quality-assessment.json, and the pipeline's receipts
  <env-id>/authoring/                   unchanged (traps.json, mutants, spec)

zipped as `<env-id>@<version>.zip` with the one folder `<env-id>/` inside.

Hidden data. tests/suites/data/ moves to tests/expected/<step>/data/. The shared grader (tests/grade.py, identical in
every bundle and never edited) protects only modules under /tests/suites, so the verifier image copies the data back
to /tests/suites/data when it is built (RESTORE_LINE): the tests read the same bytes at the same paths that the
controls and the calibration graded. Harbor builds a separate-mode verifier from tests/Dockerfile and does not upload
tests/ again (harbor 0.24 verifier._resolve_tests: "the verifier image already owns /tests").

Binding. The receipt (verification/receipt.json) is bound to `tree_sha256`: a digest of every file outside
verification/, calibration/ and PACKAGE-MANIFEST.json. Attaching proof to verification/ after the reproduction run therefore never
invalidates it, and `arena_repro` accepts a receipt whose tree digest matches the zip it is asked about.

No recalibration: the export moves files and adds derived records; calibration numbers, per-run diffs and the oracle
evidence are read from runs that already happened.

usage: python -m pipeline.arena_format convert <zip|package-dir> [...] --out <dir>
       python -m pipeline.arena_format attach <arena-zip> [...]     add the arena_repro receipt and oracle evidence
       python -m pipeline.arena_format check <arena-zip|package-dir> [...]
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

from . import metrics, policy

SCHEMA = "arena-coding/arena-format/1"
TASK_TYPE = "coding"
#: The fixed README template. Every package carries exactly these second-level headings, in this order.
README_HEADINGS = ("Overview", "Steps", "Environment", "Grading", "Reference solution", "Calibration", "Verification",
                   "Provenance", "Declarations", "Package layout")
#: What may sit at the top of a package. TURNS.md is the multi-turn record Spec Appendix A asks for.
TOP_LEVEL = {"task.toml", "declarations.json", "README.md", "PACKAGE-MANIFEST.json", "TURNS.md", ".dockerignore",
             "environment", "steps", "tests", "calibration", "provenance", "verification", "authoring"}
#: Top-level documents the delivery wrote, and where they live in Arena's layout.
RELOCATE = {"FAILURES.md": "calibration/FAILURES.md", "REWARD-HACKING.md": "verification/REWARD-HACKING.md",
            "FAITHFULNESS.md": "provenance/FAITHFULNESS.md", "REDACTIONS.md": "provenance/REDACTIONS.md"}
DECLARATION_KINDS = ("root", "network", "vendored", "broken", "large")
#: Excluded from tree_sha256: the manifest and the two evidence layers -- verification/ (written after the tree is
#: final) and calibration/ (measurements of the task, not part of what builds or grades it). The reproduction run
#: uploads a slim zip without them and its receipt still binds to the tree.
TREE_EXCLUDED_FILES = {"PACKAGE-MANIFEST.json"}
TREE_EXCLUDED_DIRS = ("verification/", "calibration/")
#: Directories a captured repository leaves out (task.toml artifacts, the packaging step); diffs skip them too.
DIFF_EXCLUDED = (".cache", ".pytest_cache", "__pycache__", ".next", "node_modules", ".git")
RESTORE_MARK = "# arena-format: hidden data lives in tests/expected/<step>/data; the grader reads it in /tests/suites/data"
RESTORE_LINE = ('RUN set -e; for d in /tests/expected/*/data; do [ -d "$d" ] || continue; '
                'mkdir -p /tests/suites/data; cp -a "$d/." /tests/suites/data/; done')
RUNS_COLUMNS = ("model", "role", "harness", "harness_version", "harbor_version", "campaign", "run_id", "step",
                "step_index", "step_reward", "step_passed", "run_reward", "run_passed", "exception", "gate_failed",
                "checks_passed", "checks_failed", "failed_checks", "agent_started_at", "agent_finished_at",
                "agent_seconds", "verifier_seconds", "input_tokens", "cache_tokens", "output_tokens", "cost_usd",
                "files_changed", "lines_added", "lines_removed", "trajectory", "patch")
PLACEHOLDER = re.compile(r"\[(?:REDACTED_[A-Z_]+|SECURITY_DATA|PATH_USERNAME_\d+)\]")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log", ".toml", ".yaml", ".yml", ".csv", ".py", ".js", ".mjs",
                 ".cjs", ".ts", ".tsx", ".html", ".css", ".sh", ".patch", ".diff", ".xml", ""}


# ----------------------------------------------------------------------------------------------- small helpers

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _files(root: Path):
    """Every regular file (and symlink) under root, as (relative posix path, path), sorted."""
    out = []
    for path in root.rglob("*"):
        if path.is_file():              # follows symlinks, as the zip does; a dangling link is no file
            out.append((path.relative_to(root).as_posix(), path))
    return sorted(out)


def _executable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR) if path.exists() else False


def task_config(pkg: Path) -> dict:
    return tomllib.loads((pkg / "task.toml").read_text(encoding="utf-8"))


def env_id(pkg: Path) -> str:
    name = (task_config(pkg).get("task") or {}).get("name") or pkg.name
    return name.split("/")[-1]


def version(pkg: Path) -> str:
    return str((task_config(pkg).get("task") or {}).get("version") or "1.0.0")


def zip_name(pkg: Path) -> str:
    return f"{env_id(pkg)}@{version(pkg)}.zip"


def steps_of(pkg: Path) -> list[str]:
    cfg = task_config(pkg)
    names = [s["name"] for s in cfg.get("steps") or []]
    if names:
        return names
    found = sorted(p.name for p in (pkg / "steps").iterdir() if p.is_dir()) if (pkg / "steps").is_dir() else []
    return found


def tree_entries(pkg: Path) -> list[tuple[str, Path]]:
    return [(rel, p) for rel, p in _files(pkg)
            if rel not in TREE_EXCLUDED_FILES and not rel.startswith(TREE_EXCLUDED_DIRS)]


def tree_sha256(pkg: Path) -> str:
    """Digest of every file outside the proof layer: one `path sha256 mode` line per file, sorted."""
    lines = [f"{rel}\0{_file_sha(p)}\0{'x' if _executable(p) else '-'}\n" for rel, p in tree_entries(pkg)]
    return _sha("".join(lines).encode())


def _zip_root(names: list[str]) -> str:
    tops = {n.split("/")[0] for n in names if n}
    return tops.pop() + "/" if len(tops) == 1 else ""


def zip_tree_sha256(zip_path: Path) -> str | None:
    """tree_sha256 of an Arena-format zip, computed from its members (None for any other layout)."""
    with zipfile.ZipFile(zip_path) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        root = _zip_root([i.filename for i in infos])
        if not root or f"{root}task.toml" not in {i.filename for i in infos}:
            return None
        lines = []
        for info in sorted(infos, key=lambda i: i.filename):
            rel = info.filename[len(root):]
            if rel in TREE_EXCLUDED_FILES or rel.startswith(TREE_EXCLUDED_DIRS):
                continue
            mode = (info.external_attr >> 16) & 0o777
            lines.append(f"{rel}\0{_sha(archive.read(info))}\0{'x' if mode & stat.S_IXUSR else '-'}\n")
    return _sha("".join(lines).encode())


# ----------------------------------------------------------------------------------------------- anonymity

def anonymity_tokens() -> list[tuple[str, str]]:
    """(needle, replacement) pairs, longest first: the pipeline checkout, then the operator's home and login."""
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    pairs = [(str(root), "<pipeline>"), (str(home), "<home>")]
    pairs += [(name, "<user>") for name in policy.ARENA_ANONYMIZE_NAMES]
    if home.name and len(home.name) >= 4 and home.name not in {"user", "agent", "coder", "root", "runner"}:
        pairs.append((home.name, "<user>"))
    seen, out = set(), []
    for needle, repl in sorted(pairs, key=lambda p: -len(p[0])):
        if needle and needle not in seen and needle not in {"/", "<home>"}:
            seen.add(needle)
            out.append((needle, repl))
    return out


def scrub_text(text: str, tokens=None) -> str:
    for needle, repl in tokens or anonymity_tokens():
        text = text.replace(needle, repl)
    return text


def scrub_tree(root: Path, rels: list[str] | None = None) -> dict:
    """Replace the anonymity tokens in every text file under root; binary files that carry one are reported."""
    tokens = anonymity_tokens()
    needles = [n.encode() for n, _ in tokens]
    changed, unscrubbable = 0, []
    for rel, path in _files(root):
        if rels is not None and not any(rel == r or rel.startswith(r.rstrip("/") + "/") for r in rels):
            continue
        if path.is_symlink():
            continue
        raw = path.read_bytes()
        if not any(n in raw for n in needles):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            unscrubbable.append(rel)
            continue
        path.write_text(scrub_text(text, tokens), encoding="utf-8")
        changed += 1
    return {"changed": changed, "unscrubbable": unscrubbable}


def anonymity_problems(pkg: Path) -> list[str]:
    needles = [n.encode() for n, _ in anonymity_tokens()]
    hits = []
    for rel, path in _files(pkg):
        if path.is_symlink():
            continue
        raw = path.read_bytes()
        found = [n.decode() for n in needles if n in raw]
        if found:
            hits.append(f"{rel} ({', '.join(sorted(set(found)))[:80]})")
    return [f"anonymity: {len(hits)} file(s) name the operator or the pipeline checkout: {', '.join(hits[:6])}"] \
        if hits else []


# ----------------------------------------------------------------------------------------------- task.toml

_DEPRECATED = {"allow_internet", "memory", "storage"}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"no TOML form for {value!r}")


def _memory_mb(value) -> int:
    text = str(value).strip().upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)I?B?", text)
    if not match:
        raise ValueError(f"unreadable memory value {value!r}")
    number, unit = float(match.group(1)), match.group(2)
    return int(number * {"": 1 / (1024 * 1024), "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit])


def rename_deprecated(text: str) -> str:
    """Harbor 0.24 key names: allow_internet -> network_mode, memory -> memory_mb, storage -> storage_mb, a
    top-level `version` -> schema_version, MCP transport "http" -> "streamable-http"."""
    out, table = [], ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            table = stripped.strip("[]").strip()
        match = re.match(r"^(\s*)(allow_internet|memory|storage|version|transport)\s*=\s*(.+?)\s*$", line)
        if match:
            indent, key, raw = match.groups()
            if key == "allow_internet":
                line = f'{indent}network_mode = "{"public" if raw.lower() == "true" else "no-network"}"\n'
            elif key in ("memory", "storage") and table.split(".")[-1] == "environment":
                line = f"{indent}{key}_mb = {_memory_mb(tomllib.loads(f'v = {raw}')['v'])}\n"
            elif key == "version" and not table:
                line = f"{indent}schema_version = {raw}\n"
            elif key == "transport" and raw.strip("'\"") == "http":
                line = f'{indent}transport = "streamable-http"\n'
        out.append(line)
    return "".join(out)


def task_additions(pkg: Path) -> dict:
    checks = _read_json(pkg / "tests/checks.json", {}) or {}
    deliverables = []
    for item in checks.get("deliverables") or []:
        path = str(item.get("path") or "")
        if not path:
            continue
        if not path.startswith("/"):
            path = (f"/app/{path}" if path.startswith("deliverables/") else f"/app/repo/{path}")
        if path not in deliverables:
            deliverables.append(path)
    protected = ["/tests", "/logs/verifier"]
    for check in checks.get("checks") or []:
        runner = check.get("runner") or {}
        if runner.get("type") == "hash-guard":
            base = runner.get("root", ".")
            for rel in runner.get("paths") or []:
                full = "/app/repo/" + (rel if base in (".", "") else f"{base.rstrip('/')}/{rel}")
                if full not in protected:
                    protected.append(full)
    return {"type": TASK_TYPE, "deliverables": deliverables, "protected_paths": protected}


def write_task_toml(pkg: Path) -> None:
    text = rename_deprecated((pkg / "task.toml").read_text(encoding="utf-8"))
    lines = text.splitlines(keepends=True)
    head = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    lines = [l for l in lines[:head] if not re.match(r"^(type|deliverables|protected_paths)\s*=", l)] + lines[head:]
    additions = task_additions(pkg)
    block = "".join(f"{k} = {_toml_value(v)}\n" for k, v in additions.items())
    # top-level keys must precede the first table
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    insert_at = first_table
    while insert_at > 0 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new = "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])
    tomllib.loads(new)
    (pkg / "task.toml").write_text(new, encoding="utf-8")


def task_toml_problems(pkg: Path) -> list[str]:
    try:
        cfg = task_config(pkg)
    except (OSError, ValueError) as exc:
        return [f"task.toml: unreadable ({exc})"]
    problems = []
    if "schema_version" not in cfg:
        problems.append("task.toml: no schema_version")
    if "version" in cfg:
        problems.append("task.toml: top-level `version` (Harbor 0.24: schema_version)")
    if cfg.get("type") != TASK_TYPE:
        problems.append(f"task.toml: type must be {TASK_TYPE!r}")
    for key in ("deliverables", "protected_paths"):
        if not isinstance(cfg.get(key), list) or not all(isinstance(v, str) for v in cfg.get(key)):
            problems.append(f"task.toml: {key} must be a list of paths")

    def walk(node, where):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _DEPRECATED and where.split(".")[-1] in ("environment", "verifier", "agent", ""):
                    problems.append(f"task.toml: deprecated key {where + '.' if where else ''}{key}")
                if key == "transport" and value == "http":
                    problems.append(f"task.toml: {where}.transport = 'http' (Harbor 0.24: 'streamable-http')")
                walk(value, f"{where}.{key}" if where else key)
        elif isinstance(node, list):
            for item in node:
                walk(item, where)
    walk({k: v for k, v in cfg.items() if k != "metadata"}, "")
    return problems


def harbor_validate(pkg: Path, python: str) -> dict:
    """Validate task.toml with a Harbor's own TaskConfig (the interpreter `python` must have harbor installed)."""
    code = ("import json, sys, tomllib, warnings\n"
            "from importlib.metadata import version\n"
            "from harbor.models.task.config import TaskConfig\n"
            "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n"
            "    TaskConfig.model_validate(tomllib.loads(open(sys.argv[1]).read()))\n"
            "print(json.dumps({'harbor': version('harbor'), 'ok': True,"
            " 'warnings': [str(w.message) for w in caught if 'ResourceWarning' not in str(w.category)]}))\n")
    result = subprocess.run([python, "-I", "-c", code, str(pkg / "task.toml")], capture_output=True, text=True,
                            timeout=120)
    if result.returncode:
        return {"ok": False, "error": (result.stderr or result.stdout)[-600:]}
    return json.loads(result.stdout.strip().splitlines()[-1])


# ----------------------------------------------------------------------------------------------- tests/

def _step_test_files(checks: dict, step: str, suites: Path) -> list[Path]:
    files = []
    for check in checks.get("checks") or []:
        if check.get("step") != step:
            continue
        runner = check.get("runner") or {}
        names = [str(s).split("::")[0] for s in runner.get("select") or []]
        if runner.get("type") == "command":
            names += re.findall(r"[\w./-]+\.(?:py|js|mjs|cjs|ts|sh|awk)", runner.get("shell") or "")
        for name in names:
            path = suites / name
            if path.is_file() and path not in files:
                files.append(path)
    return files


def move_hidden_data(pkg: Path, steps: list[str]) -> dict[str, list[str]]:
    """tests/suites/data/* -> tests/expected/<step>/data/*, each entry under the first step whose tests name it."""
    suites, data = pkg / "tests/suites", pkg / "tests/suites/data"
    placed: dict[str, list[str]] = {s: [] for s in steps}
    if not data.is_dir():
        return placed
    checks = _read_json(pkg / "tests/checks.json", {}) or {}
    texts = {s: "\n".join(p.read_text(encoding="utf-8", errors="replace")
                          for p in _step_test_files(checks, s, suites)) for s in steps}
    for entry in sorted(data.iterdir()):
        owner = next((s for s in steps if entry.name in texts[s]), steps[0])
        target = pkg / "tests/expected" / owner / "data" / entry.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry), str(target))
        placed[owner] += [p.relative_to(pkg / "tests/expected" / owner).as_posix()
                          for p in sorted(target.rglob("*")) if p.is_file()] if target.is_dir() \
            else [target.relative_to(pkg / "tests/expected" / owner).as_posix()]
    shutil.rmtree(data)
    dockerfile = pkg / "tests/Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    if RESTORE_MARK not in text:
        lines = text.splitlines(keepends=True)
        at = next(i for i, l in enumerate(lines) if re.match(r"^COPY\s+\.\s+/tests/?\s*$", l.strip()))
        lines[at + 1:at + 1] = [RESTORE_MARK + "\n", RESTORE_LINE + "\n"]
        dockerfile.write_text("".join(lines), encoding="utf-8")
    return placed


def write_expected(pkg: Path, steps: list[str], placed: dict[str, list[str]]) -> None:
    checks = _read_json(pkg / "tests/checks.json", {}) or {}
    for step in steps:
        base = pkg / "tests/expected" / step
        files = []
        for rel in placed.get(step, []):
            path = base / rel
            files.append({"path": rel, "bytes": path.stat().st_size, "sha256": _file_sha(path),
                          "restored_to": "/tests/suites/" + rel})
        _json(base / "expected.json", {
            "schema": "arena-coding/expected/1", "step": step,
            "checks": [c["id"] for c in checks.get("checks") or [] if c.get("step") == step],
            "files": files,
            "note": ("Private inputs and helpers the hidden tests of this step read. The verifier image copies data/ "
                     "to /tests/suites/data when it is built, where the shared grader protects it; outcomes are "
                     "asserted by the tests tests/rubrics/<step>.json names, by running the agent's code.")
            if files else ("This step's hidden tests carry no separate data files; outcomes are asserted by the "
                           "tests tests/rubrics/<step>.json names, by running the agent's code.")})


def write_rubrics(pkg: Path, steps: list[str]) -> None:
    checks = _read_json(pkg / "tests/checks.json", {}) or {}
    reward = {}
    try:
        reward = tomllib.loads((pkg / "tests/reward.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    strategy = task_config(pkg).get("multi_step_reward_strategy") or "mean"
    for step in steps:
        rows = [c for c in checks.get("checks") or [] if c.get("step") == step]
        objectives = [c for c in rows if c.get("kind") == "objective"]
        points = sum(int(c.get("weight") or 0) for c in objectives)
        criteria, gates = [], []
        for check in rows:
            runner = dict(check.get("runner") or {})
            enforced = {"runner": runner.pop("type", None), **runner}
            item = {"id": check["id"], "criterion": check.get("basis", ""), "band": check.get("band"),
                    "requires": check.get("requires") or [], "enforced_by": enforced}
            if check.get("kind") == "objective":
                weight = int(check.get("weight") or 0)
                criteria.append({**item, "weight": weight,
                                 "share_of_step": round(weight / points, 4) if points else 0.0,
                                 "share_of_task": round(weight / points / max(1, len(steps)), 4) if points else 0.0})
            else:
                gates.append({**item, "effect": "the step scores 0 when this gate fails"})
        _json(pkg / "tests/rubrics" / f"{step}.json", {
            "schema": "arena-coding/rubric/1", "step": step, "instruction": f"steps/{step}/instruction.md",
            "aggregation": "weighted_mean", "points": points,
            "step_reward": "earned weight / points, or 0 when any gate fails",
            "task_reward": f"{strategy} of the step rewards (task.toml multi_step_reward_strategy)",
            "pass": f"a run passes at reward >= {metrics.PASS_REWARD}",
            "bands": next((r.get("bands") for r in reward.get("reward") or [] if r.get("step") == step), None),
            "criteria": criteria, "gates": gates,
            "source": "tests/checks.json, which tests/grade.py reads; this file is derived from it and agrees with it"})


def rubric_problems(pkg: Path, steps: list[str]) -> list[str]:
    checks = _read_json(pkg / "tests/checks.json", {}) or {}
    problems = []
    for step in steps:
        doc = _read_json(pkg / "tests/rubrics" / f"{step}.json")
        if not isinstance(doc, dict):
            problems.append(f"tests/rubrics/{step}.json: missing")
            continue
        want = {c["id"]: int(c.get("weight") or 0) for c in checks.get("checks") or []
                if c.get("step") == step and c.get("kind") == "objective"}
        got = {c["id"]: c.get("weight") for c in doc.get("criteria") or []}
        gates_want = {c["id"] for c in checks.get("checks") or [] if c.get("step") == step and c.get("kind") != "objective"}
        if got != want or {g["id"] for g in doc.get("gates") or []} != gates_want:
            problems.append(f"tests/rubrics/{step}.json disagrees with tests/checks.json")
    return problems


# ----------------------------------------------------------------------------------------------- diffs

def _git(args, *, env=None, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false",
                           "-c", "core.fileMode=false", *args], capture_output=True, env=env, cwd=cwd, timeout=600)


def _diff_paths(work: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(work):
        dirnames[:] = sorted(d for d in dirnames if d not in DIFF_EXCLUDED)
        for name in filenames:
            if name in DIFF_EXCLUDED:
                continue
            full = Path(dirpath) / name
            if full.is_file() or full.is_symlink():
                out.append(full.relative_to(work).as_posix())
    return sorted(out)


def _tree_of(git_dir: Path, work: Path | None, index: Path) -> str:
    """The git tree of `work`'s files (every file outside DIFF_EXCLUDED; a repo .gitignore is not consulted)."""
    env = {**os.environ, "GIT_DIR": str(git_dir), "GIT_INDEX_FILE": str(index), "GIT_LITERAL_PATHSPECS": "1"}
    paths = _diff_paths(work) if work is not None else []
    if paths:
        env["GIT_WORK_TREE"] = str(work)
        listing = "\0".join(paths).encode() + b"\0"
        added = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.fileMode=false", "add", "--force",
                                "--pathspec-from-file=-", "--pathspec-file-nul"], input=listing, capture_output=True,
                               env=env, cwd=work, timeout=600)
        if added.returncode:
            raise RuntimeError(f"git add failed in {work}: {added.stderr.decode(errors='replace')[-300:]}")
    tree = _git(["write-tree"], env=env)
    return tree.stdout.decode().strip()


def diff_trees(before: Path | None, after: Path | None) -> dict:
    """The change from `before` to `after` (either may be absent): a binary-safe git patch plus counts."""
    with tempfile.TemporaryDirectory(prefix="arena-diff-") as tmp:
        git_dir = Path(tmp) / "g"
        init = _git(["init", "-q", "--bare", str(git_dir)])
        if init.returncode:
            raise RuntimeError(init.stderr.decode(errors="replace"))
        a = _tree_of(git_dir, before if before and before.is_dir() else None, Path(tmp) / "index-a")
        b = _tree_of(git_dir, after if after and after.is_dir() else None, Path(tmp) / "index-b")
        env = {**os.environ, "GIT_DIR": str(git_dir)}
        patch = _git(["diff-tree", "-r", "-p", "--binary", "--no-renames", a, b], env=env).stdout
        numstat = _git(["diff-tree", "-r", "--numstat", "--no-renames", a, b], env=env).stdout.decode(errors="replace")
        status = _git(["diff-tree", "-r", "--name-status", "--no-renames", a, b], env=env).stdout.decode(errors="replace")
    files, added, removed = [], 0, 0
    kinds = {line.split("\t", 1)[1]: line.split("\t", 1)[0] for line in status.splitlines() if "\t" in line}
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        plus, minus, path = parts
        binary = plus == "-"
        added += 0 if binary else int(plus)
        removed += 0 if binary else int(minus)
        files.append({"path": path, "status": {"A": "added", "D": "deleted", "M": "modified", "T": "type_changed"}
                      .get(kinds.get(path, "M"), kinds.get(path, "M")),
                      "added": None if binary else int(plus), "removed": None if binary else int(minus),
                      "binary": binary})
    return {"patch": patch, "files": files, "files_changed": len(files), "lines_added": added, "lines_removed": removed}


# ----------------------------------------------------------------------------------------------- calibration

def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    from datetime import datetime
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((b - a).total_seconds(), 1)


def _measured_package(campaign: Path) -> dict | None:
    package = campaign / "package"
    if not (package / "task.toml").is_file():
        return None
    try:
        cfg = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    except ValueError:
        cfg = {}
    inventory = {rel: _file_sha(p) for rel, p in _files(package) if not p.is_symlink()}
    return {"version": (cfg.get("task") or {}).get("version"), "files": len(inventory),
            "inventory_sha256": _sha(json.dumps(inventory, sort_keys=True).encode()),
            "graded_surfaces_sha256": _sha(json.dumps({k: v for k, v in inventory.items()
                                                       if k.startswith(("tests/", "environment/", "steps/"))
                                                       or k == "task.toml"}, sort_keys=True).encode())}


def export_calibration(src: Path, pkg: Path, steps: list[str]) -> dict:
    """runs/ -> calibration/{metrics.json, runs.csv, trials/<model>/<run>/, campaigns/<model>/<campaign>/}."""
    index = _read_json(src / "runs/index.json", {}) or {}
    out = pkg / "calibration"
    out.mkdir(parents=True, exist_ok=True)
    rows, per_model, campaigns_meta = [], {}, []
    used_ids: set[tuple[str, str]] = set()
    for campaign in index.get("campaigns") or []:
        label = campaign.get("label") or "unknown"
        cdir = src / campaign["path"]
        if not cdir.is_dir():
            continue
        role = "reference" if label == "reference" else "calibration"
        report = _read_json(cdir / "report.json", {}) or {}
        runtime = _read_json(cdir / "runtime.json", {}) or {}
        harbor_version = (runtime.get("versions") or {}).get("harbor")
        measured = _measured_package(cdir)
        fixture = cdir / "package/environment/repo"
        if not fixture.is_dir():
            fixture = src / "environment/repo"
        # campaign-level records, without the copy of the measured package (its inventory digest is kept)
        ckeep = out / "campaigns" / label / cdir.name
        for rel, path in _files(cdir):
            if rel.startswith("package/") or rel.startswith("jobs/run/") and rel.count("/") > 2:
                continue
            target = ckeep / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=True)
        if measured:
            _json(ckeep / "measured-package.json", measured)
        campaigns_meta.append({"model": label, "role": role, "campaign": cdir.name, "agent": report.get("agent"),
                               "model_id": report.get("model"), "harbor": harbor_version,
                               "protocol": report.get("protocol") or runtime.get("protocol"),
                               "measured_version": (measured or {}).get("version"),
                               "path": f"calibration/campaigns/{label}/{cdir.name}"})
        for trial in campaign.get("trials") or []:
            tid = trial.get("trial_id")
            tdir = cdir / "jobs/run" / str(tid)
            if not tid or not tdir.is_dir():
                continue
            run_id = tid if (label, tid) not in used_ids else f"{cdir.name[-8:]}-{tid}"
            used_ids.add((label, run_id))
            target = out / "trials" / label / run_id
            shutil.copytree(tdir, target, symlinks=False, dirs_exist_ok=True, ignore_dangling_symlinks=True)
            result = _read_json(tdir / "result.json", {}) or {}
            agent_info = result.get("agent_info") or {}
            step_results = {s.get("step_name"): s for s in result.get("step_results") or []}
            run_reward = trial.get("reward")
            exception = (result.get("exception_info") or {}).get("exception_type") if result.get("exception_info") \
                else (trial.get("exception") or None)
            run_doc_steps, previous = [], fixture
            for i, step in enumerate(steps, 1):
                sres = step_results.get(step) or {}
                sdir = tdir / "steps" / step
                final = sdir / "artifacts/app/repo"
                rubric = ((trial.get("per_step_rubric") or {}).get(step) or {})
                criteria = rubric.get("criteria") or {}
                step_reward = ((sres.get("verifier_result") or {}).get("rewards") or {}).get("reward",
                                                                                          rubric.get("score"))
                gate_failed = [c for c, v in criteria.items() if c.startswith("g_") and not v]
                change_doc = {"schema": "arena-coding/changes/1", "step": step, "status": "not_captured"}
                patch_rel = None
                if final.is_dir():
                    diff = diff_trees(previous, final)
                    base_name = "environment/repo (as measured)" if i == 1 else f"the repository after {steps[i - 2]}"
                    change_doc = {"schema": "arena-coding/changes/1", "step": step, "status": "captured",
                                  "base": base_name, "excluded": list(DIFF_EXCLUDED),
                                  "files_changed": diff["files_changed"], "lines_added": diff["lines_added"],
                                  "lines_removed": diff["lines_removed"], "files": diff["files"]}
                    (target / "steps" / step).mkdir(parents=True, exist_ok=True)
                    (target / "steps" / step / "changes.patch").write_bytes(diff["patch"])
                    patch_rel = f"calibration/trials/{label}/{run_id}/steps/{step}/changes.patch"
                    deliver_before = (previous.parent / "deliverables") if i > 1 else None
                    deliver_after = sdir / "artifacts/app/deliverables"
                    if deliver_after.is_dir() and any(deliver_after.rglob("*")):
                        ddiff = diff_trees(deliver_before, deliver_after)
                        (target / "steps" / step / "deliverables.patch").write_bytes(ddiff["patch"])
                        change_doc["deliverables"] = ddiff["files"]
                    previous = final
                _json(target / "steps" / step / "changes.json", change_doc)
                agent = sres.get("agent_result") or {}
                aexec, vexec = sres.get("agent_execution") or {}, sres.get("verifier") or {}
                trajectory = next((f"calibration/trials/{label}/{run_id}/steps/{step}/agent/{p.name}"
                                   for p in sorted((sdir / "agent").glob("trajectory.json"))), None) \
                    if (sdir / "agent").is_dir() else None
                row = {"model": label, "role": role, "harness": agent_info.get("name") or report.get("agent"),
                       "harness_version": agent_info.get("version"), "harbor_version": harbor_version,
                       "campaign": cdir.name, "run_id": run_id, "step": step, "step_index": i,
                       "step_reward": step_reward, "step_passed": metrics.passed(step_reward),
                       "run_reward": run_reward, "run_passed": metrics.passed(run_reward),
                       "exception": exception or "", "gate_failed": ";".join(gate_failed),
                       "checks_passed": sum(1 for v in criteria.values() if v),
                       "checks_failed": sum(1 for v in criteria.values() if not v),
                       "failed_checks": ";".join(c for c, v in criteria.items() if not v),
                       "agent_started_at": aexec.get("started_at"), "agent_finished_at": aexec.get("finished_at"),
                       "agent_seconds": _seconds(aexec.get("started_at"), aexec.get("finished_at")),
                       "verifier_seconds": _seconds(vexec.get("started_at"), vexec.get("finished_at")),
                       "input_tokens": agent.get("n_input_tokens"), "cache_tokens": agent.get("n_cache_tokens"),
                       "output_tokens": agent.get("n_output_tokens"), "cost_usd": agent.get("cost_usd"),
                       "files_changed": change_doc.get("files_changed"), "lines_added": change_doc.get("lines_added"),
                       "lines_removed": change_doc.get("lines_removed"), "trajectory": trajectory or "",
                       "patch": patch_rel or ""}
                rows.append(row)
                run_doc_steps.append({k: row[k] for k in ("step", "step_reward", "step_passed", "gate_failed",
                                                          "failed_checks", "agent_seconds", "verifier_seconds",
                                                          "input_tokens", "cache_tokens", "output_tokens",
                                                          "cost_usd", "files_changed", "lines_added",
                                                          "lines_removed", "trajectory", "patch")}
                                     | {"checks": criteria,
                                        "changes": f"calibration/trials/{label}/{run_id}/steps/{step}/changes.json"})
            _json(target / "run.json", {
                "schema": "arena-coding/run/1", "model": label, "model_id": report.get("model"), "role": role,
                "harness": agent_info.get("name") or report.get("agent"), "harness_version": agent_info.get("version"),
                "harbor_version": harbor_version, "campaign": cdir.name, "run_id": run_id,
                "measured_version": (measured or {}).get("version"), "reward": run_reward,
                "passed": metrics.passed(run_reward), "exception": exception,
                "started_at": result.get("started_at"), "finished_at": result.get("finished_at"),
                "steps": run_doc_steps})
            if role == "calibration":
                per_model.setdefault(label, []).append({"reward": run_reward, "exception": exception,
                                                         "steps": {s["step"]: s for s in run_doc_steps},
                                                         "harness": agent_info.get("name"),
                                                         "harness_version": agent_info.get("version"),
                                                         "harbor": harbor_version, "model_id": report.get("model")})
    with open(out / "runs.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RUNS_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in RUNS_COLUMNS})
    models = {}
    for label, runs in per_model.items():
        scored = [r for r in runs if r["reward"] is not None]
        passes = sum(1 for r in scored if metrics.passed(r["reward"]))
        per_step = {}
        for step in steps:
            vals = [r["steps"][step]["step_reward"] for r in scored if step in r["steps"]
                    and r["steps"][step]["step_reward"] is not None]
            check_rates = {}
            for r in scored:
                for cid, v in (r["steps"].get(step) or {}).get("checks", {}).items():
                    check_rates.setdefault(cid, []).append(1.0 if v else 0.0)
            per_step[step] = {"runs": len(vals), "pass_rate": round(sum(metrics.passed(v) for v in vals) / len(vals), 3)
                              if vals else None, "mean_reward": round(sum(vals) / len(vals), 3) if vals else None,
                              "check_pass_rates": {c: round(sum(v) / len(v), 3) for c, v in check_rates.items()},
                              "under_step_bar": (sum(metrics.passed(v) for v in vals) / len(vals)
                                                 < policy.STEP_PASS_RATE_MAX) if vals else None}
        models[label] = {"model_id": runs[0]["model_id"], "harness": runs[0]["harness"],
                         "harness_version": runs[0]["harness_version"], "harbor_version": runs[0]["harbor"],
                         "runs": len(runs), "scored": len(scored), "exceptions": sum(1 for r in runs if r["exception"]),
                         "passes": passes,
                         "pass_at_1": metrics.pass_at_k(len(scored), passes, 1) if scored else None,
                         "pass_at_4": metrics.pass_at_k(len(scored), passes, 4) if len(scored) >= 4 else None,
                         "mean_reward": round(sum(r["reward"] for r in scored) / len(scored), 3) if scored else None,
                         "per_step": per_step}
    shipped = version(pkg)
    measured_versions = sorted({c["measured_version"] for c in campaigns_meta
                                if c["role"] == "calibration" and c["measured_version"]})
    delivery = _read_json(src / "verification/delivery.json", {}) or {}
    enough = len(models) >= 2 and all(m["scored"] >= policy.SPEC_EPISODES_PER_MODEL // 2 for m in models.values())
    doc = {"schema": "arena-coding/calibration-metrics/1", "env_id": env_id(pkg), "shipped_version": shipped,
           "measured_versions": measured_versions,
           "status": ("calibrated" if enough else "partial" if models else "uncalibrated"),
           "measured_matches_shipped": measured_versions == [shipped] if measured_versions else None,
           "pass_rule": f"reward >= {metrics.PASS_REWARD} (within {metrics.EPSILON})",
           "targets": {"pass_at_1_strict": "< 0.5 on both models", "step_pass_rate_max": policy.STEP_PASS_RATE_MAX,
                       "extreme_target_pass_at_1": policy.EXTREME_TARGET_PASS_AT_1},
           "models": models,
           "run_environment": {"harness": "harbor", "harbor_versions": sorted({c["harbor"] for c in campaigns_meta
                                                                             if c["harbor"]}),
                               "protocol": next((c["protocol"] for c in campaigns_meta if c["protocol"]), None),
                               "agents": {c["model"]: {"agent": c["agent"], "model_id": c["model_id"]}
                                          for c in campaigns_meta},
                               "reasoning_effort": "high", "turn_or_time_limits": None,
                               "network": "agent phase: its model API host only; setup and verifier offline",
                               "resources": {k: (task_config(pkg).get("environment") or {}).get(k)
                                             for k in ("cpus", "memory_mb")}},
           "campaigns": campaigns_meta, "runs_csv": "calibration/runs.csv", "trials": "calibration/trials/"}
    if measured_versions and measured_versions != [shipped]:
        doc["version_note"] = (f"These runs measured version {', '.join(measured_versions)}; this package is "
                               f"{shipped}. The change between them is recorded in verification/delivery.json "
                               "(\"patch\").")
        if delivery.get("patch"):
            doc["patch"] = delivery["patch"]
    _json(out / "metrics.json", doc)
    return doc


# ----------------------------------------------------------------------------------------------- provenance

_CITATION = re.compile(r"^(?P<session>[^:]+)::msg(?P<msg>\d+)(?:::(?P<path>.*))?$")


def export_provenance(src: Path, pkg: Path, steps: list[str]) -> None:
    ledger = _read_json(src / "provenance/ledger.json", {}) or {}
    spec = _read_json(src / "authoring/spec.json", {}) or {}
    source = ledger.get("source") or {}
    files, messages = [], set()
    for entry in ledger.get("entries") or []:
        cite = _CITATION.match(str(entry.get("citation") or ""))
        ref = None
        if cite:
            ref = {"session_id": cite.group("session"), "message": int(cite.group("msg")),
                   "path": cite.group("path")}
            messages.add(int(cite.group("msg")))
        files.append({"path": entry.get("path"), "sha256": entry.get("sha256"), "bytes": entry.get("bytes"),
                      "origin": entry.get("origin"), "source": ref,
                      **({"note": entry["note"]} if entry.get("note") else {})})
    repo = src / "environment/repo"
    mapped = {f["path"] for f in files}
    unmapped = [rel for rel, _ in _files(repo) if rel not in mapped] if repo.is_dir() else []
    _json(pkg / "provenance/ledger.json", {
        "schema": "arena-coding/ledger/1", "workspace": "environment/repo",
        "source": {"session_id": source.get("session_id"), "seed_index": source.get("seed_index"),
                   "trace_sha256": source.get("trace_sha256")},
        "files": files, "unmapped": unmapped,
        "summary": {"files": len(files), "by_origin": ledger.get("counts"), "bytes_by_origin": ledger.get("bytes"),
                    "faithfulness_level": ledger.get("faithfulness_level"),
                    "faithfulness_by_file": ledger.get("faithfulness_by_file"),
                    "faithfulness_by_bytes": ledger.get("faithfulness_by_bytes")},
        "pipeline": ledger})
    spec_steps = {s.get("name"): s for s in spec.get("steps") or []}
    turns = _read_json(src / "authoring/extraction/user-turns.json")
    step_rows = []
    for step in steps:
        turn = (spec_steps.get(step) or {}).get("trace_turn")
        if isinstance(turn, int):
            messages.add(turn)
        step_rows.append({"step": step, "instruction": f"steps/{step}/instruction.md", "user_message": turn})
    corrections = []
    usable = spec.get("usable_corrections")
    for c in usable if isinstance(usable, list) else []:
        if isinstance(c, dict):
            idx = c.get("message") if isinstance(c.get("message"), int) else c.get("message_index")
            if isinstance(idx, int):
                messages.add(idx)
            corrections.append({k: c.get(k) for k in ("message", "message_index", "kind", "summary") if k in c})
    _json(pkg / "provenance/source-scope.json", {
        "schema": "arena-coding/source-scope/1",
        "source_session": source.get("session_id") or (spec.get("source") or {}).get("session_id"),
        "seed_index": source.get("seed_index"), "trace_sha256": source.get("trace_sha256"),
        "source_tier": (spec.get("source") or {}).get("tier"),
        "fixture_snapshot": {"policy": ledger.get("snapshot_policy"),
                             "messages": sorted({f["source"]["message"] for f in files if f["source"]})},
        "steps": step_rows, "corrections": corrections,
        "usable_corrections": usable if isinstance(usable, int) else len(corrections),
        "messages_used": sorted(messages),
        "recovery_omissions": ledger.get("recovery_omissions") or [],
        **({"user_turns": "authoring/extraction/user-turns.json"} if turns is not None else {})})


# ----------------------------------------------------------------------------------------------- declarations

#: Vendored or generated trees are never lifted (they are reinstalled at image build); a session naming them is
#: not a missing file.
NEVER_LIFTED = {"node_modules", ".next", ".git", "__pycache__", ".cache", ".pytest_cache", ".venv", "venv"}


def _grouped(items, why: str, full_list: str) -> list[dict]:
    """One `broken` declaration per top-level folder (a single file keeps its own path), with a count and examples."""
    groups: dict[str, set] = {}
    for item in items:
        path = str(item.get("path") if isinstance(item, dict) else item).strip().lstrip("/")
        if not path or path.split("/")[0] in NEVER_LIFTED:
            continue
        groups.setdefault(path.split("/")[0] if "/" in path else path, set()).add(path)
    out = []
    for head, paths in sorted(groups.items()):
        if len(paths) == 1:
            out.append({"kind": "broken", "path": next(iter(paths)), "reason": why})
        else:
            examples = ", ".join(sorted(paths)[:3])
            out.append({"kind": "broken", "path": f"{head}/", "count": len(paths),
                        "reason": f"{len(paths)} files {why} (e.g. {examples}); every path is listed in {full_list}"})
    return out


def declarations(pkg: Path) -> list[dict]:
    out = []
    cfg = task_config(pkg)
    agent_user = (cfg.get("agent") or {}).get("user")
    dockerfile = (pkg / "environment/Dockerfile").read_text(encoding="utf-8", errors="replace") \
        if (pkg / "environment/Dockerfile").is_file() else ""
    users = re.findall(r"^\s*USER\s+(\S+)", dockerfile, re.M)
    if agent_user in (None, "root", 0, "0") or (users and users[-1] in ("root", "0")):
        out.append({"kind": "root", "path": "task.toml", "reason": f"the agent runs as {agent_user or 'root'}"})
    for step in steps_of(pkg):
        solve = pkg / "steps" / step / "solution/solve.sh"
        if solve.is_file() and re.search(r"\bsudo\b", solve.read_text(encoding="utf-8", errors="replace")):
            out.append({"kind": "root", "path": f"steps/{step}/solution/solve.sh", "reason": "the reference uses sudo"})

    def phases(node, where):
        if isinstance(node, dict):
            mode = node.get("network_mode")
            if mode and mode != "no-network":
                out.append({"kind": "network", "path": "task.toml", "reason": f"{where or 'task'} network_mode = {mode}"})
            if node.get("allowed_hosts"):
                out.append({"kind": "network", "path": "task.toml",
                            "reason": f"{where} allows hosts: {', '.join(node['allowed_hosts'])}"})
            for key, value in node.items():
                if key != "metadata":
                    phases(value, f"{where}.{key}" if where else key)
        elif isinstance(node, list):
            for item in node:
                phases(item, where)
    phases({k: v for k, v in cfg.items() if k != "metadata"}, "")
    checks = _read_json(pkg / "tests/checks.json", {}) or {}
    for item in (checks.get("toolchain") or {}).get("vendored") or []:
        out.append({"kind": "vendored", "path": str(item if isinstance(item, str) else item.get("path", item)),
                    "reason": "third-party dependencies installed at image build from the pinned manifests in "
                              "environment/deps and linked into the repository; not shipped in environment/repo"})
    repo = pkg / "environment/repo"
    vendored_dirs = set()
    for rel, path in _files(repo) if repo.is_dir() else []:
        parts = rel.split("/")
        hit = next((i for i, part in enumerate(parts[:-1]) if part in ("vendor", "vendors", "third_party",
                                                                       "third-party", "node_modules")), None)
        if hit is not None:
            vendored_dirs.add("/".join(parts[:hit + 1]))
        elif re.search(r"\.min\.(js|css)$", parts[-1]):
            out.append({"kind": "vendored", "path": f"environment/repo/{rel}",
                        "reason": "a minified third-party file shipped in the fixture"})
        if path.suffix.lower() in TEXT_SUFFIXES - {".patch", ".diff"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            found = sorted(set(PLACEHOLDER.findall(text)))
            if found:
                out.append({"kind": "broken", "path": f"environment/repo/{rel}",
                            "reason": "upstream redaction placeholders remain in the source: " + ", ".join(found[:4])})
    for path in sorted(repo.rglob("*")) if repo.is_dir() else []:
        if path.is_symlink() and not path.exists():
            out.append({"kind": "broken", "path": f"environment/repo/{path.relative_to(repo).as_posix()}",
                        "reason": "a symlink with no target"})
    for d in sorted(vendored_dirs):
        out.append({"kind": "vendored", "path": f"environment/repo/{d}", "reason": "third-party code shipped in the fixture"})
    ledger = (_read_json(pkg / "provenance/ledger.json", {}) or {}).get("pipeline") \
        or _read_json(pkg / "provenance/ledger.json", {}) or {}
    out += _grouped(ledger.get("recovery_omissions") or [], "named by the source session but not recoverable, so "
                    "absent from the fixture", "provenance/source-scope.json (recovery_omissions)")
    faith = ledger.get("faithfulness") or {}
    for key, why in (("extraction_exclusions", "excluded from the fixture at extraction"),
                     ("host_paths", "a host path the fixture reads that is not shipped")):
        out += _grouped(faith.get(key) or [], why, f"provenance/ledger.json (pipeline.faithfulness.{key})")
    for rel, path in _files(pkg):
        if rel.startswith(("calibration/", "verification/")) or path.is_symlink():
            continue
        if path.stat().st_size >= policy.ARENA_LARGE_FILE_BYTES:
            out.append({"kind": "large", "path": rel, "reason": f"{path.stat().st_size} bytes"})
    return out


# ----------------------------------------------------------------------------------------------- README

def write_readme(pkg: Path, steps: list[str], calibration: dict, decls: list[dict]) -> None:
    cfg = task_config(pkg)
    task, meta, env = cfg.get("task") or {}, cfg.get("metadata") or {}, cfg.get("environment") or {}
    checks = _read_json(pkg / "tests/checks.json", {}) or {}
    toolchain = checks.get("toolchain") or {}
    dockerfile = (pkg / "environment/Dockerfile").read_text(encoding="utf-8", errors="replace")
    bases = [b.split("@")[0] for b in re.findall(r"^FROM\s+(\S+)", dockerfile, re.M)]
    lines = [f"# {env_id(pkg)}", "", "## Overview", "", task.get("description") or "", "",
             f"Version {version(pkg)}. Faithfulness level {meta.get('faithfulness_level', '?')}; language "
             f"{meta.get('language', '?')}. Built from Arena trace `{meta.get('source_trace', '?')}`.", "",
             "## Steps", "", "| step | instruction | objectives | gates | points |", "|---|---|---:|---:|---:|"]
    for step in steps:
        rubric = _read_json(pkg / "tests/rubrics" / f"{step}.json", {}) or {}
        lines.append(f"| {step} | steps/{step}/instruction.md | {len(rubric.get('criteria') or [])} | "
                     f"{len(rubric.get('gates') or [])} | {rubric.get('points', '')} |")
    lines += ["", "## Environment", "",
              f"The agent works in `/app/repo` as the non-root user `{(cfg.get('agent') or {}).get('user', '?')}`, "
              f"built from `environment/Dockerfile` (base: {', '.join(dict.fromkeys(bases)) or '?'}). Toolchain "
              f"`{toolchain.get('name', '?')}`; dependencies are installed from the pinned manifests in "
              f"`environment/deps/`. Network: {env.get('network_mode', '?')}. Resources: {env.get('cpus', '?')} CPU, "
              f"{env.get('memory_mb', '?')} MB.", "",
              "## Grading", "",
              "The verifier image is built from `tests/` alone (`tests/Dockerfile`); `tests/test.sh` runs "
              "`tests/grade.py`, which executes the hidden suites in `tests/suites/` as an unprivileged user and "
              "scores the checks in `tests/checks.json`. `tests/rubrics/<step>.json` states each step's criteria, "
              "weights and gates; `tests/expected/<step>/` holds the private data the hidden tests read. A failed "
              "gate scores its step 0; the task reward is the "
              f"{cfg.get('multi_step_reward_strategy') or 'mean'} of the step rewards.", "",
              "## Reference solution", "",
              "Each step's reference is `steps/<step>/solution/solve.sh` (Harbor's oracle copies the folder to "
              "`/solution` and runs it). It derives the fixed repository by applying `fix.patch`; nothing writes an "
              "expected output directly.", "",
              "## Calibration", ""]
    models = calibration.get("models") or {}
    if models:
        lines += ["| model | harness | runs | passes | pass@1 | mean reward |", "|---|---|---:|---:|---:|---:|"]
        for label, m in models.items():
            p1 = "" if m.get("pass_at_1") is None else f"{m['pass_at_1']:.2f}"
            lines.append(f"| {label} | {m.get('harness')} {m.get('harness_version') or ''} | {m.get('runs')} | "
                         f"{m.get('passes')} | {p1} | {m.get('mean_reward')} |")
        lines += ["", f"Harbor {', '.join(calibration['run_environment']['harbor_versions']) or '?'}; "
                      f"status {calibration.get('status')}. Per-run records and per-step diffs are in "
                      "`calibration/trials/`, one row per run and step in `calibration/runs.csv`."]
        if calibration.get("version_note"):
            lines += ["", calibration["version_note"]]
    else:
        lines.append("Not calibrated.")
    lines += ["", "## Verification", "",
              "`verification/receipt.json` records the oracle (1.0) and no-op (0.0) runs on Arena's harness, bound "
              "to the package tree's digest; `verification/oracle/` keeps that run's evidence before and after the "
              "reference. `verification/quality-assessment.json` lists every self-check with its status and reason.",
              "", "## Provenance", "",
              "`provenance/source-scope.json` maps the source session to the messages each step and the fixture "
              "come from; `provenance/ledger.json` maps every workspace file to its source.", "",
              "## Declarations", ""]
    if decls:
        lines += [f"- {d['kind']}: `{d['path']}` -- {d['reason']}" for d in decls]
    else:
        lines.append("None (`declarations.json` is an empty list).")
    lines += ["", "## Package layout", "", "```",
              "task.toml  declarations.json  README.md  PACKAGE-MANIFEST.json",
              "environment/   Dockerfile, deps/, repo/ (the agent's workspace)",
              "steps/<step>/  instruction.md, solution/solve.sh",
              "tests/         Dockerfile, test.sh, grade.py, checks.json, suites/, rubrics/<step>.json, expected/<step>/",
              "calibration/   metrics.json, runs.csv, trials/<model>/<run>/, campaigns/",
              "provenance/    source-scope.json, ledger.json",
              "verification/  receipt.json, oracle/, quality-assessment.json",
              "authoring/     spec.json, traps.json, mutants/", "```", ""]
    (pkg / "README.md").write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------------------------------- verification

def _control_status(name: str, value) -> tuple[str, str]:
    expected = {"nop": 0.0, "golden": 1.0, "alternative": 1.0, "empty": 0.0, "destructive": 0.0, "test_tamper": 0.0}
    if isinstance(value, dict):
        if "ok" in value:
            return ("pass" if value["ok"] else "fail"), json.dumps({k: v for k, v in value.items()
                                                                    if k in ("ok", "reward", "problems")})[:300]
        if name in expected and "reward" in value:
            ok = value.get("reward") == expected[name]
            return ("pass" if ok else "fail"), f"reward {value.get('reward')} (expected {expected[name]})"
        if name == "determinism":
            return ("pass" if value.get("identical") else "fail"), f"{value.get('runs')} runs, rewards {value.get('rewards')}"
    if isinstance(value, list):
        bad = [v for v in value if isinstance(v, dict) and v.get("ok") is False]
        return ("fail" if bad else "pass"), f"{len(value)} entries, {len(bad)} failing"
    return "recorded", "see verification/controls.json"


def quality_assessment(pkg: Path, extra: list[dict] | None = None) -> dict:
    v = pkg / "verification"
    checks = []
    controls = _read_json(v / "controls.json")
    if isinstance(controls, dict):
        for name, value in (controls.get("results") or {}).items():
            status, reason = _control_status(name, value)
            checks.append({"id": f"control.{name}", "status": status, "reason": reason,
                           "evidence": "verification/controls.json"})
        checks.append({"id": "controls", "status": "pass" if controls.get("ok") else "fail",
                       "reason": "; ".join(map(str, controls.get("errors") or [])) or "every executed control held",
                       "evidence": "verification/controls.json"})
    release = _read_json(v / "release.json")
    if isinstance(release, dict):
        checks.append({"id": "release", "status": "pass" if release.get("release_ready") else "fail",
                       "reason": "; ".join(map(str, release.get("errors") or [])) or f"status {release.get('status')}",
                       "evidence": "verification/release.json"})
    ship = _read_json(v / "shipcheck.json")
    if isinstance(ship, dict):
        checks.append({"id": "shipcheck", "status": "pass" if ship.get("ok") else "fail",
                       "reason": f"{len(ship.get('open') or [])} open finding(s), {len(ship.get('advisory') or [])} advisory",
                       "evidence": "verification/shipcheck.json"})
    probes = _read_json(v / "probes.json")
    if isinstance(probes, dict):
        checks.append({"id": "probes", "status": "pass" if probes.get("ok") else "fail",
                       "reason": "; ".join(map(str, probes.get("problems") or []))[:400] or "every probe held",
                       "evidence": "verification/probes.json"})
        if probes.get("untested_surfaces"):
            checks.append({"id": "untested_surfaces", "status": "advisory",
                           "reason": f"{len(probes['untested_surfaces'])} surface(s) the hidden tests do not settle",
                           "evidence": "verification/probes.json"})
    critic = _read_json(v / "coverage-critic.json")
    if isinstance(critic, dict):
        gaps = critic.get("gaps") or []
        blocking = [g for g in gaps if isinstance(g, dict) and g.get("blocking")]
        checks.append({"id": "coverage_critic", "status": "fail" if blocking else ("advisory" if gaps else "pass"),
                       "reason": f"{len(gaps)} gap(s) recorded, {len(blocking)} blocking",
                       "evidence": "verification/coverage-critic.json"})
    genuine = _read_json(v / "genuine.json")
    if isinstance(genuine, dict):
        checks.append({"id": "genuine_failures", "status": "pass" if genuine.get("certified") else "advisory",
                       "reason": "; ".join(map(str, genuine.get("problems") or []))[:400]
                       or "every counted model failure was adjudicated genuine",
                       "evidence": "verification/genuine.json"})
    harbor = _read_json(v / "harbor-tasks-check.json")
    if isinstance(harbor, dict):
        checks.append({"id": "harbor_tasks_check", "status": "pass" if harbor.get("status") == "pass" else "fail",
                       "reason": f"status {harbor.get('status')}", "evidence": "verification/harbor-tasks-check.json"})
    delivery = _read_json(v / "delivery.json")
    if isinstance(delivery, dict):
        accepted = delivery.get("acceptance_problems") or []
        checks.append({"id": "delivery_acceptance", "status": "advisory" if accepted else "pass",
                       "reason": "; ".join(map(str, accepted))[:400] or "accepted at delivery",
                       "evidence": "verification/delivery.json"})
    if (v / "REWARD-HACKING.md").is_file():
        checks.append({"id": "reward_hacking", "status": "recorded",
                       "reason": "executed hack probes and their outcomes", "evidence": "verification/REWARD-HACKING.md"})
    receipt = _read_json(v / "receipt.json")
    checks.append({"id": "arena_reproduction",
                   "status": ("pass" if isinstance(receipt, dict) and receipt.get("ok") else
                              "fail" if isinstance(receipt, dict) else "pending"),
                   "reason": (f"oracle {receipt['oracle'].get('reward')}, nop {receipt['nop'].get('reward')} on "
                              f"Harbor {receipt['harness'].get('version')}" if isinstance(receipt, dict)
                              else "the Arena reproduction run attaches verification/receipt.json"),
                   "evidence": "verification/receipt.json"})
    for item in extra or []:
        checks.append(item)
    format_problems = [p for p in problems(pkg, require_receipt=False, require_manifest=False)
                       if not p.startswith("verification/quality-assessment.json")]
    checks.append({"id": "arena_format", "status": "fail" if format_problems else "pass",
                   "reason": "; ".join(format_problems)[:600] or "the package has Arena's layout",
                   "evidence": "pipeline/arena_format.py"})
    return {"schema": "arena-coding/quality-assessment/1", "env_id": env_id(pkg), "version": version(pkg),
            "summary": {s: sum(1 for c in checks if c["status"] == s) for s in
                        ("pass", "fail", "advisory", "recorded", "pending")},
            "checks": checks}


def write_receipt(pkg: Path, repro: dict, evidence: Path | None) -> dict:
    """verification/receipt.json and verification/oracle/ from an arena_repro receipt and its pulled trials."""
    controls = _read_json(pkg / "verification/controls.json", {}) or {}
    determinism = (controls.get("results") or {}).get("determinism") or {}
    cfg = task_config(pkg)
    dockerfile = (pkg / "environment/Dockerfile").read_text(encoding="utf-8", errors="replace")
    uid = re.search(r"useradd\b[^\n]*?(?:--uid|-u)\s+(\d+)", dockerfile)
    oracle, nop = repro.get("oracle") or {}, repro.get("nop") or {}
    attempts = oracle.get("rewards") or ([oracle.get("reward")] if oracle.get("reward") is not None else [])
    out = pkg / "verification/oracle"
    if out.exists():
        shutil.rmtree(out)
    saved = {"after": [], "before": []}
    if evidence and evidence.is_dir():
        for agent, side in (("oracle", "after"), ("nop", "before")):
            for n, trial in enumerate(sorted((evidence / agent).glob("*")), 1):
                for step in steps_of(pkg):
                    sdir = trial / "steps" / step
                    target = out / side / (f"run-{n}" if side == "after" else "") / step
                    target.mkdir(parents=True, exist_ok=True)
                    for name in ("reward.json", "reward-details.json", "arena-grade.json", "test-stdout.txt"):
                        if (sdir / "verifier" / name).is_file():
                            shutil.copy2(sdir / "verifier" / name, target / name)
                    if side == "after" and (sdir / "artifacts/app/repo").is_dir():
                        diff = diff_trees(pkg / "environment/repo", sdir / "artifacts/app/repo")
                        (target / "changes.patch").write_bytes(diff["patch"])
                        _json(target / "changes.json", {k: diff[k] for k in ("files_changed", "lines_added",
                                                                              "lines_removed", "files")})
                    saved[side].append(target.relative_to(pkg).as_posix())
                if (trial / "result.json").is_file():
                    res = _read_json(trial / "result.json", {}) or {}
                    _json(out / side / (f"run-{n}" if side == "after" else "") / "result.json",
                          {k: res.get(k) for k in ("task_name", "trial_name", "agent_info", "verifier_result",
                                                   "exception_info", "started_at", "finished_at", "step_results")})
        scrub_tree(out)
    rewards_ok = attempts and all(r == 1.0 for r in attempts) and nop.get("reward") == 0.0 \
        and not oracle.get("exception") and not nop.get("exception")
    doc = {"schema": "arena-coding/receipt/1", "env_id": env_id(pkg), "version": version(pkg),
           "tree_sha256": tree_sha256(pkg),
           "tree_rule": "sha256 over one `path NUL sha256 NUL x|-` line per file, sorted, for every file except "
                        "PACKAGE-MANIFEST.json, calibration/ and verification/",
           "harness": dict(repro.get("harness") or {}), "resources": repro.get("resources"),
           "oracle": {"reward": oracle.get("reward"), "attempts": attempts, "exception": oracle.get("exception")},
           "nop": {"reward": nop.get("reward"), "exception": nop.get("exception")},
           "non_root": {"agent_user": (cfg.get("agent") or {}).get("user"),
                        "agent_uid": int(uid.group(1)) if uid else (
                            1000 if re.search(r"usermod\b[^\n]*--login\s+agent\b[^\n]*\bnode\b", dockerfile)
                            else None),       # the node images' `node` user is uid 1000, renamed to agent
                        "verifier_user": "grader",
                        "note": "the agent image ends as its non-root user; tests/grade.py drops to the unprivileged "
                                "grader user before any hidden test runs"},
           "repeatable": {"oracle_rewards": attempts, "identical": len(set(attempts)) == 1 and len(attempts) > 1,
                          "sandbox_golden_runs": determinism.get("rewards"),
                          "sandbox_identical": determinism.get("identical")},
           "at": repro.get("at"), "evidence": saved, "ok": bool(rewards_ok)}
    _json(pkg / "verification/receipt.json", scrub_json(doc))
    return doc


def scrub_json(doc):
    return json.loads(scrub_text(json.dumps(doc, ensure_ascii=False)))


def write_manifest(pkg: Path) -> dict:
    files = [{"path": rel, "size": p.stat().st_size, "sha256": _file_sha(p), "executable": _executable(p)}
             for rel, p in _files(pkg) if rel != "PACKAGE-MANIFEST.json"]
    doc = {"schema": "arena-coding/package-manifest/1", "env_id": env_id(pkg), "version": version(pkg),
           "tree_sha256": tree_sha256(pkg), "count": len(files), "total_bytes": sum(f["size"] for f in files),
           "files": files}
    _json(pkg / "PACKAGE-MANIFEST.json", doc)
    return doc


# ----------------------------------------------------------------------------------------------- export

def _locate(src: Path) -> Path:
    if (src / "task.toml").is_file():
        return src
    found = sorted(p.parent for p in src.glob("*/task.toml")) or sorted(p.parent for p in src.glob("*/*/task.toml"))
    if len(found) != 1:
        raise ValueError(f"{src}: expected one task package, found {len(found)}")
    return found[0]


def export(src: Path, out_root: Path) -> Path:
    """Write Arena's layout for the delivered package `src` to out_root/<env-id>/ (proof layer left pending)."""
    src = _locate(Path(src))
    name = env_id(src)
    pkg = Path(out_root) / name
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)
    for entry in sorted(src.iterdir()):
        if entry.name in ("solution", "runs", "__pycache__"):
            continue
        target = pkg / RELOCATE.get(entry.name, entry.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=True, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__") if entry.name != "environment" else None)
        else:
            shutil.copy2(entry, target)
    if (src / "solution/README.md").is_file():
        shutil.copy2(src / "solution/README.md", pkg / "authoring/solution-notes.md")
    steps = steps_of(pkg)
    for step in steps:
        solve = pkg / "steps" / step / "solution/solve.sh"
        if solve.is_file():
            solve.chmod(0o755)
    if (pkg / "tests/test.sh").is_file():
        text = (pkg / "tests/test.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
        if not text.startswith("#!"):
            text = "#!/bin/sh\n" + text
        (pkg / "tests/test.sh").write_text(text, encoding="utf-8")
        (pkg / "tests/test.sh").chmod(0o755)
    if (pkg / ".dockerignore").is_file():
        text = (pkg / ".dockerignore").read_text(encoding="utf-8")
        if "calibration/" not in text.split():
            (pkg / ".dockerignore").write_text(text.rstrip("\n") + "\ncalibration/\n", encoding="utf-8")
    write_task_toml(pkg)
    placed = move_hidden_data(pkg, steps)
    write_expected(pkg, steps, placed)
    write_rubrics(pkg, steps)
    calibration = export_calibration(src, pkg, steps)
    export_provenance(src, pkg, steps)
    scrubbed = scrub_tree(pkg, ["calibration", "verification", "provenance", "authoring", "README.md",
                                "declarations.json", "TURNS.md"])
    decls = declarations(pkg)
    _json(pkg / "declarations.json", decls)
    write_readme(pkg, steps, calibration, decls)
    _json(pkg / "verification/quality-assessment.json", quality_assessment(pkg, [
        {"id": "anonymity", "status": "fail" if scrubbed["unscrubbable"] else "pass",
         "reason": (f"{scrubbed['changed']} file(s) scrubbed of the operator's paths; unscrubbable binary file(s): "
                    + ", ".join(scrubbed["unscrubbable"][:6])) if scrubbed["unscrubbable"]
         else f"{scrubbed['changed']} file(s) scrubbed of the operator's paths", "evidence": "pipeline/arena_format.py"}]))
    write_manifest(pkg)
    return pkg


def write_zip(pkg: Path, out_dir: Path) -> Path:
    """`<env-id>@<version>.zip`, one folder `<env-id>/` inside, unix modes kept, members in sorted order."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / zip_name(pkg)
    tmp = target.with_suffix(".zip.tmp")
    root = env_id(pkg)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(pkg.rglob("*")):
            rel = path.relative_to(pkg).as_posix()
            if path.is_dir():
                info = zipfile.ZipInfo(f"{root}/{rel}/", date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(info, b"")
                continue
            if not path.is_file():
                continue                # a dangling symlink (declared broken) has no bytes to ship
            info = zipfile.ZipInfo(f"{root}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = ((0o100755 if _executable(path) else 0o100644) << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    tmp.replace(target)
    return target


def package(src: Path, out_dir: Path, work: Path | None = None) -> Path:
    """Export a delivered package and zip it in Arena's format (the receipt is attached by arena_repro)."""
    with tempfile.TemporaryDirectory(prefix="arena-format-", dir=work) as tmp:
        pkg = export(src, Path(tmp))
        found = problems(pkg, require_receipt=False)
        if found:
            raise ValueError(f"{pkg.name}: not in Arena's format: " + "; ".join(found))
        return write_zip(pkg, out_dir)


def attach(zip_path: Path, repro: dict, evidence: Path | None) -> Path:
    """Add the reproduction receipt and its oracle evidence to an Arena-format zip (the tree digest is unchanged)."""
    zip_path = Path(zip_path)
    with tempfile.TemporaryDirectory(prefix="arena-attach-") as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            _extract(archive, Path(tmp))
        pkg = _locate(Path(tmp))
        before = tree_sha256(pkg)
        if repro.get("tree_sha256") and repro["tree_sha256"] != before:
            raise ValueError(f"{zip_path.name}: the reproduction ran on another tree ({repro['tree_sha256'][:12]} != "
                             f"{before[:12]})")
        write_receipt(pkg, repro, evidence)
        qa = _read_json(pkg / "verification/quality-assessment.json", {}) or {}
        keep = [c for c in qa.get("checks") or [] if c.get("id") in ("anonymity",)]
        _json(pkg / "verification/quality-assessment.json", quality_assessment(pkg, keep))
        write_manifest(pkg)
        assert tree_sha256(pkg) == before
        return write_zip(pkg, zip_path.parent)


def slim_zip(zip_path: Path, out_dir: Path) -> Path:
    """The same zip, same name, without calibration/, verification/ and the manifest: what the reproduction run
    needs to build and grade the task, with the same tree_sha256."""
    target = Path(out_dir) / Path(zip_path).name
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        root = _zip_root(src.namelist())
        for info in src.infolist():
            rel = info.filename[len(root):]
            if rel in TREE_EXCLUDED_FILES or rel.startswith(TREE_EXCLUDED_DIRS):
                continue
            dst.writestr(info, src.read(info))
    return target


def _extract(archive: zipfile.ZipFile, dest: Path) -> None:
    for info in archive.infolist():
        target = dest / info.filename
        if not str(target.resolve()).startswith(str(dest.resolve())):
            raise ValueError(f"zip member escapes: {info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))
        mode = (info.external_attr >> 16) & 0o777
        if mode:
            target.chmod(mode)


# ----------------------------------------------------------------------------------------------- validation

def problems(pkg: Path, *, require_receipt: bool = True, require_manifest: bool = True) -> list[str]:
    """Why `pkg` is not a package in Arena's format ([] when it is)."""
    pkg = Path(pkg)
    found = task_toml_problems(pkg)
    if found and any("unreadable" in p for p in found):
        return found
    steps = steps_of(pkg)
    extra = sorted(e.name for e in pkg.iterdir() if e.name not in TOP_LEVEL)
    if extra:
        found.append(f"unexpected top-level entries: {', '.join(extra)}")
    if (pkg / "solution").exists():
        found.append("a root solution/ (Arena: per-step solutions only)")
    decl = _read_json(pkg / "declarations.json")
    if not isinstance(decl, list) or any(not isinstance(d, dict) or d.get("kind") not in DECLARATION_KINDS
                                         for d in decl):
        found.append("declarations.json must be a list of {kind, path, reason} with kind in " + "/".join(DECLARATION_KINDS))
    readme = (pkg / "README.md").read_text(encoding="utf-8") if (pkg / "README.md").is_file() else ""
    headings = re.findall(r"^## (.+?)\s*$", readme, re.M)
    if headings != list(README_HEADINGS):
        found.append(f"README.md headings {headings} are not the template {list(README_HEADINGS)}")
    if not steps:
        found.append("no steps")
    for step in steps:
        if not (pkg / "steps" / step / "instruction.md").is_file():
            found.append(f"steps/{step}/instruction.md missing")
        solve = pkg / "steps" / step / "solution/solve.sh"
        if not solve.is_file() or not _executable(solve):
            found.append(f"steps/{step}/solution/solve.sh missing or not executable")
        if not isinstance(_read_json(pkg / "tests/expected" / step / "expected.json"), dict):
            found.append(f"tests/expected/{step}/expected.json missing")
        else:
            for item in _read_json(pkg / "tests/expected" / step / "expected.json")["files"]:
                path = pkg / "tests/expected" / step / item["path"]
                if not path.is_file() or _file_sha(path) != item["sha256"]:
                    found.append(f"tests/expected/{step}/{item['path']} missing or changed")
    found += rubric_problems(pkg, steps)
    if not (pkg / "tests/test.sh").is_file() or not _executable(pkg / "tests/test.sh"):
        found.append("tests/test.sh missing or not executable")
    if (pkg / "tests/suites/data").exists():
        found.append("tests/suites/data still present (Arena: tests/expected/<step>/)")
    if any((pkg / "tests/expected").glob("*/data")):
        if RESTORE_MARK not in (pkg / "tests/Dockerfile").read_text(encoding="utf-8"):
            found.append("tests/Dockerfile does not restore tests/expected/<step>/data for the grader")
    metrics_doc = _read_json(pkg / "calibration/metrics.json")
    if not isinstance(metrics_doc, dict):
        found.append("calibration/metrics.json missing")
    else:
        try:
            with open(pkg / "calibration/runs.csv", newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
            if tuple(header) != RUNS_COLUMNS:
                found.append("calibration/runs.csv columns differ from RUNS_COLUMNS")
        except (OSError, StopIteration):
            found.append("calibration/runs.csv missing")
        if metrics_doc.get("models") and not any((pkg / "calibration/trials").glob("*/*/run.json")):
            found.append("calibration/trials/<model>/<run>/run.json missing")
    for rel in ("provenance/source-scope.json", "provenance/ledger.json", "verification/quality-assessment.json"):
        if not isinstance(_read_json(pkg / rel), dict):
            found.append(f"{rel} missing")
    if require_receipt:
        receipt = _read_json(pkg / "verification/receipt.json")
        if not isinstance(receipt, dict):
            found.append("verification/receipt.json missing (run the Arena reproduction: arena_repro attaches it)")
        else:
            if receipt.get("tree_sha256") != tree_sha256(pkg):
                found.append("verification/receipt.json is bound to another tree")
            if not receipt.get("ok"):
                found.append(f"verification/receipt.json: oracle {receipt.get('oracle', {}).get('reward')}, "
                             f"nop {receipt.get('nop', {}).get('reward')}")
            if not any((pkg / "verification/oracle").rglob("reward.json")):
                found.append("verification/oracle/ holds no oracle-run evidence")
    if require_manifest:
        manifest = _read_json(pkg / "PACKAGE-MANIFEST.json")
        if not isinstance(manifest, dict):
            found.append("PACKAGE-MANIFEST.json missing")
        else:
            want = {f["path"]: (f["size"], f["sha256"], f["executable"]) for f in manifest.get("files") or []}
            have = {rel: (p.stat().st_size, _file_sha(p), _executable(p)) for rel, p in _files(pkg)
                    if rel != "PACKAGE-MANIFEST.json"}
            if want != have:
                diff = sorted(set(want.items()) ^ set(have.items()))
                found.append(f"PACKAGE-MANIFEST.json disagrees with the files ({len(diff)} difference(s), "
                             f"first {diff[0][0] if diff else ''})")
    found += anonymity_problems(pkg)
    return found


def zip_problems(zip_path: Path, *, require_receipt: bool = True) -> list[str]:
    zip_path = Path(zip_path)
    with tempfile.TemporaryDirectory(prefix="arena-check-") as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            names = [i.filename for i in archive.infolist()]
            root = _zip_root(names)
            if not root:
                return [f"{zip_path.name}: the zip must hold exactly one folder <env-id>/"]
            _extract(archive, Path(tmp))
        pkg = Path(tmp) / root.rstrip("/")
        if not (pkg / "task.toml").is_file():
            return [f"{zip_path.name}: no {root}task.toml"]
        found = problems(pkg, require_receipt=require_receipt)
        if zip_path.name != zip_name(pkg):
            found.insert(0, f"zip name {zip_path.name} is not {zip_name(pkg)}")
        if root.rstrip("/") != env_id(pkg):
            found.insert(0, f"zip folder {root} is not the env id {env_id(pkg)}")
        return [f"{zip_path.name}: {p}" for p in found]


# ----------------------------------------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("convert", "check", "attach"):
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    out = None
    if "--out" in rest:
        i = rest.index("--out")
        out = Path(rest[i + 1])
        rest = rest[:i] + rest[i + 2:]
    bad = 0
    if command == "convert":
        if out is None:
            print("convert needs --out <dir>")
            return 2
        for raw in rest:
            src = Path(raw)
            with tempfile.TemporaryDirectory(prefix="arena-src-") as tmp:
                if src.suffix == ".zip":
                    with zipfile.ZipFile(src) as archive:
                        _extract(archive, Path(tmp))
                    src = Path(tmp)
                try:
                    target = package(src, out)
                    print(f"OK   {target}")
                except Exception as exc:  # noqa: BLE001 -- report every package, then fail
                    bad += 1
                    print(f"FAIL {raw}: {exc}")
    elif command == "attach":
        from . import arena_repro
        for raw in rest:
            z = Path(raw)
            receipt = _read_json(arena_repro.receipt_path(z))
            if not isinstance(receipt, dict):
                print(f"FAIL {z.name}: no reproduction receipt")
                bad += 1
                continue
            evidence = arena_repro.evidence_path(z)
            attach(z, receipt, evidence if evidence.is_dir() else None)
            print(f"OK   {z.name}: receipt attached")
    else:
        for raw in rest:
            path = Path(raw)
            found = zip_problems(path) if path.suffix == ".zip" else problems(_locate(path))
            print(("OK   " if not found else "FAIL ") + path.name)
            for p in found:
                print(f"     - {p}")
            bad += bool(found)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
