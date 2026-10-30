"""Arena delivery format v0.1 (lmarena/arena-rle-formatcheck): the runtime surface of a coding package.

`arena_format.py` packages a task in our own `arena-coding/arena-format/1` layout. Arena's checker
(format v0.1) wants different shapes for the files that RUN the task -- `task.toml`, `declarations.json`,
`tests/rubrics/<step>.json` and the verifier's `reward-details.json` -- and ties calibration to the hash
of exactly those files (`task_tree_sha256`, §13.2). This module converts that runtime surface, so the
converted package can be calibrated as delivered. Evidence (calibration/, verification/, provenance/,
README, PACKAGE-MANIFEST.json) is built around the runs afterwards.

The grader is untouched: `tests/grade.py` still decides every check; `tests/arena_details.py` (run by
`test.sh` after it) restates that result in Arena's reward-details schema against the step rubric and
refuses to write a grade whose reward differs from grade.py's.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
import zipfile
from pathlib import Path

DELIVERY_FORMAT = "0.1"
RUBRIC_SCHEMA = "arena-delivery/rubric/1"
DECLARATIONS_SCHEMA = "arena-delivery/declarations/1"
CONTRACT_GATE = "deliverable_contract"
GRADE_IMPL = "grade.py"
ARTIFACT_EXCLUDES = [".cache", ".pytest_cache", "__pycache__"]
STORAGE_MB = 10240
LEVELS = {"F0", "F1", "F2", "F3"}


# ---------------------------------------------------------------- identity

def task_tree_sha256(root: Path) -> str:
    """§13.2: task.toml, declarations.json and every regular file under environment/, steps/ and tests/."""
    root = Path(root)
    lines = []
    for top in ("task.toml", "declarations.json", "environment", "steps", "tests"):
        p = root / top
        files = [p] if p.is_file() else [Path(d) / f for d, _, fs in os.walk(p) for f in fs] if p.is_dir() else []
        for f in files:
            rel = f.relative_to(root).as_posix()
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            x = "x" if os.stat(f).st_mode & 0o111 else "-"
            lines.append(f"{sha}  {x}  {rel}\n")
    lines.sort(key=lambda line: line.split("  ", 2)[2].encode())
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def bump_minor(version: str) -> str:
    major, minor, _ = (int(p) for p in version.split("."))
    return f"{major}.{minor + 1}.0"


def snake(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "_".join(words)[:64] or "software_engineering"


# ---------------------------------------------------------------- task.toml

def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(int(value)) if float(value).is_integer() else repr(float(value))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"no TOML form for {value!r}")


def _table(name: str, values: dict) -> list[str]:
    return [f"[{name}]", *(f"{k} = {_toml_value(v)}" for k, v in values.items() if v is not None), ""]


def task_toml(old: dict, *, env_id: str, version: str, domain: str, prompt_language: str,
              declared: list[str] | None = None) -> str:
    """declared: the repo-relative deliverable paths tests/checks.json makes the grader enforce."""
    meta_old = old.get("metadata", {})
    level = meta_old.get("faithfulness_level") or meta_old.get("faithfulness")
    if level not in LEVELS:
        raise ValueError(f"{env_id}: faithfulness level {level!r}")
    session = meta_old.get("source_trace") or meta_old.get("source_session")
    if not session:
        raise ValueError(f"{env_id}: no source session")
    workspace = "/app/repo"
    # The first deliveries name deliverables only in tests/checks.json (the grader's contract); a grader that
    # names none reads the whole working tree, and then the deliverable is the repo.
    deliverables = (old.get("deliverables") or meta_old.get("deliverables")
                    or sorted(f"{workspace}/{d}" for d in dict.fromkeys(declared or [])) or [workspace])
    outside = sorted({d for d in deliverables if not (d == workspace or d.startswith(workspace + "/"))})
    task = old["task"]
    env, agent, verifier = (dict(old.get(k, {})) for k in ("environment", "agent", "verifier"))
    venv = dict(verifier.pop("environment", {}) or {})
    env.setdefault("storage_mb", STORAGE_MB)
    env.setdefault("workdir", workspace)
    env["network_mode"] = "no-network"
    agent["network_mode"] = "no-network"
    verifier.update(network_mode="no-network", environment_mode="separate")
    if venv:
        venv.update(network_mode="no-network")
        venv.setdefault("cpus", env.get("cpus"))
        venv.setdefault("memory_mb", env.get("memory_mb"))
    metadata = {"delivery_format": DELIVERY_FORMAT, "type": "coding", "domain": domain,
                "faithfulness": level, "prompt_language": prompt_language,
                "programming_languages": [meta_old["language"]] if meta_old.get("language") else None,
                "agent_workspace": workspace, "deliverables": list(deliverables), "protected_paths": [],
                "source_session": session}
    # Arena's Q12 needs every captured descendant of a deliverable collected, so a whole-repo deliverable
    # takes no cache excludes (grade.py drops __pycache__ and .pytest_cache from its grading copy anyway).
    whole_repo = any(d == workspace or workspace.startswith(d.rstrip("/") + "/") for d in deliverables)
    excludes = [] if whole_repo else ARTIFACT_EXCLUDES
    lines = ['schema_version = "1.4"',
             f'multi_step_reward_strategy = {_toml_value(old.get("multi_step_reward_strategy", "mean"))}', "",
             "[[artifacts]]", f"source = {_toml_value(workspace)}",
             *([f"exclude = {_toml_value(excludes)}"] if excludes else []), ""]
    for path in outside:
        lines += ["[[artifacts]]", f"source = {_toml_value(path)}", ""]
    lines += _table("task", {"name": f"arena/{env_id}", "version": version, "description": task.get("description"),
                             "keywords": task.get("keywords")})
    lines += _table("metadata", metadata)
    lines += _table("metadata.x-pipeline", {"pipeline": "arena-coding", "toolchain": meta_old.get("toolchain")})
    lines += _table("environment", env)
    lines += _table("agent", agent)
    lines += _table("verifier", verifier)
    if venv:
        lines += _table("verifier.environment", venv)
    for step in old.get("steps", []):
        lines += ["[[steps]]", f"name = {_toml_value(step['name'])}"]
        sv = dict(step.get("verifier", {}))
        sv["env"] = {**(sv.get("env") or {}), "ARENA_STEP": step["name"]}
        lines += ["[steps.verifier]", *(f"{k} = {_toml_value(v)}" for k, v in sv.items()), ""]
        if step.get("agent"):
            lines += _table("steps.agent", step["agent"])
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- declarations.json

#: The archive suffixes Arena's checker declares-or-fails (Q16), by name alone.
ARCHIVE_EXT = {".tar", ".tgz", ".gz", ".xz", ".bz2", ".zst", ".zip", ".whl", ".jar", ".7z", ".deb", ".rpm", ".tbz2", ".txz"}
#: Leading bytes of the formats those suffixes name.
ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"\xfd7zXZ\x00", b"BZh", b"\x28\xb5\x2f\xfd",
                 b"7z\xbc\xaf\x27\x1c", b"!<arch>", b"\xed\xab\xee\xdb")


def archive_named(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() in ARCHIVE_EXT or name.endswith((".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst"))


def real_archive(path: Path) -> bool:
    head = path.read_bytes()[:512]
    return any(head.startswith(m) for m in ARCHIVE_MAGIC) or (len(head) >= 262 and head[257:262] == b"ustar")


def declarations(old_entries: list, pkg: Path | None = None) -> dict:
    """Arena kinds only. Ours: `large` -> large-data; `vendored` (dependencies installed from pinned
    manifests at image build, not archives in the package) and `broken` (source files the session named
    but never saved, so absent from the package) are not package declarations and move to provenance.

    Every archive-named file under environment/, steps/ or tests/ must be declared (Q16). A fixture that
    only carries an archive's name (a text placeholder or an empty file the task copies or checksums) is a
    `negative-fixture` of the wrong format; a real archive needs a vendored-dependency declaration with its
    install chain, which this converter does not invent."""
    out, n = [], 0
    if pkg is not None:
        stubs, real = [], []
        for top in ("environment", "steps", "tests"):
            for f in sorted((pkg / top).rglob("*")):
                if f.is_file() and not f.is_symlink() and archive_named(f):
                    (real if real_archive(f) else stubs).append(f.relative_to(pkg).as_posix())
        if real:
            raise ValueError("real archives need a vendored-dependency or opaque-data declaration: " + ", ".join(real[:5]))
        if stubs:
            out.append({"id": "archive-named-placeholders", "kind": "negative-fixture",
                        "reason": "The repository's mirror holds files named like archives whose bytes are short text "
                                  "placeholders or empty: the task only copies, lists and checksums them, never "
                                  "unpacks them, so they are fixtures of the wrong format by design.",
                        "details": {"paths": stubs, "defect": "wrong-format"}})
    for e in old_entries or []:
        # Arena's large-data covers task inputs; our old `large` also flagged authoring files (a big spec.json)
        # that are not in the runtime tree at all, and those are not declarations.
        if e.get("kind") == "large" and (pkg is None or (pkg / e["path"]).is_file()):
            n += 1
            out.append({"id": f"large-data-{n}", "kind": "large-data",
                        "reason": "A task input larger than 10 MB that the user's workflow reads as a whole.",
                        "details": {"paths": [e["path"]], "format": Path(e["path"]).suffix.lstrip(".") or "data"}})
    return {"schema": DECLARATIONS_SCHEMA, "declarations": out}


# ---------------------------------------------------------------- rubric

def rubric(step: str, checks: list[dict], spec_checks: dict) -> dict:
    mine = [c for c in checks if c.get("step") == step]
    scored = [c for c in mine if c["kind"] == "objective"]
    gates = [c for c in mine if c["kind"] == "guardrail"]
    if any(c.get("requires") for c in mine):
        raise ValueError(f"{step}: check prerequisites are not mapped yet")
    if not scored:
        raise ValueError(f"{step}: no scored criterion")

    def node(c, method):
        s = spec_checks.get(c["id"], {})
        quote = s.get("rule_quote") or c.get("rule_quote")
        quote = " ".join(quote) if isinstance(quote, list) else quote
        return {"id": c["id"], "kind": "criterion",
                "description": (s.get("basis") or c.get("basis") or c["id"]).strip(),
                "acceptance": (quote or s.get("basis") or c.get("basis") or c["id"]).strip(),
                "scoring": {"method": method},
                "verification": {"method": "deterministic_test", "implementation": GRADE_IMPL},
                "depends_on": []}

    nodes = [node(c, "binary") for c in scored] + [node(c, "hard_gate") for c in gates]
    nodes.append({"id": CONTRACT_GATE, "kind": "criterion",
                  "description": "The declared deliverables exist as regular files inside the working tree, and no "
                                 "symlink in the working tree points outside it.",
                  "acceptance": "Every declared deliverable is present as a regular file at its declared path and no "
                                "symlink escapes the working tree; otherwise the step scores 0.",
                  "scoring": {"method": "hard_gate"},
                  "verification": {"method": "deterministic_test", "implementation": GRADE_IMPL},
                  "depends_on": []})
    if len(scored) == 1:
        root = scored[0]["id"]
    else:
        root = "score"
        nodes.insert(0, {"id": root, "kind": "group",
                         "description": "Weighted mean of the step's scored criteria (weights are the published points).",
                         "aggregation": "weighted_mean",
                         "children": [{"node": c["id"], "weight": int(c["weight"])} for c in scored]})
    return {"schema": RUBRIC_SCHEMA, "step": step, "execution_profile": "local_deterministic", "root": root,
            "nodes": nodes, "gates": [c["id"] for c in gates] + [CONTRACT_GATE], "penalties": [],
            "repeatability": {"mode": "deterministic", "runs": 2}}


# ---------------------------------------------------------------- verifier wiring

ARENA_DETAILS = r'''#!/usr/bin/env python3
"""Restate this grader's result (arena-grade.json) as Arena v0.1 reward-details.json for the step rubric.

grade.py decides every check; this only reports it. It refuses to write a grade whose reward differs
from grade.py's. task_tree_sha256 and input_snapshot identify the package and the captured workspace,
which the verifier cannot see: they are written as all-zero placeholders here and set by the harness
when the run is archived (verifier contract §8).
"""
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path("/logs/verifier")
CONTRACT_GATE = "deliverable_contract"
ZERO = "0" * 64


def main() -> int:
    step = os.environ.get("ARENA_STEP", "").strip()
    rubrics = sorted((HERE / "rubrics").glob("*.json"))
    if not step and len(rubrics) == 1:
        step = rubrics[0].stem
    raw = (HERE / "rubrics" / f"{step}.json").read_bytes()
    rubric = json.loads(raw)
    result = json.loads((OUT / "arena-grade.json").read_text())
    checks = result.get("checks", {})
    nodes = {n["id"]: n for n in rubric["nodes"]}
    weights = {}
    for n in rubric["nodes"]:
        for child in n.get("children", []):
            weights[child["node"]] = child["weight"]
    criteria, values = [], {}
    for n in rubric["nodes"]:
        if n["kind"] != "criterion":
            continue
        gate = n["scoring"]["method"] == "hard_gate"
        if n["id"] == CONTRACT_GATE:
            errors = result.get("contract_errors") or []
            ok, why = not errors, ("; ".join(errors)[:500] if errors else "deliverables present; no escaping symlink")
        else:
            rec = checks[n["id"]]
            ok, why = bool(rec.get("passed")), rec.get("reason") or ("passed" if rec.get("passed") else "failed")
        value = 1.0 if ok else 0.0
        values[n["id"]] = value
        criteria.append({"name": n["id"], "kind": "gate" if gate else "scored",
                         "weight": 0 if gate else weights.get(n["id"], 1), "raw_value": value, "value": value,
                         "status": "evaluated", "reason": why or ("passed" if ok else "failed"),
                         "evidence": ["arena-grade.json"]})
    groups = []
    for n in rubric["nodes"]:
        if n["kind"] == "group":
            total = sum(c["weight"] for c in n["children"])
            values[n["id"]] = sum(c["weight"] * values[c["node"]] for c in n["children"]) / total
            groups.append({"name": n["id"], "value": values[n["id"]]})
    base = values[rubric["root"]]
    deduction = 0.0
    before_gates = min(1.0, max(0.0, base - deduction))
    failed = [g for g in rubric["gates"] if values.get(g) == 0.0]
    reward = 0.0 if failed else before_gates
    graded = float(result.get("reward"))
    if abs(reward - graded) > 1e-9:
        print(f"arena_details: rubric reward {reward} differs from grade.py reward {graded}", file=sys.stderr)
        return 1
    details = {"schema": "arena-delivery/reward-details/1", "step": step, "reward": reward,
               "task_tree_sha256": ZERO, "rubric_sha256": hashlib.sha256(raw).hexdigest(),
               "input_snapshot": {"path": "changes.json", "sha256": ZERO},
               "execution_profile": rubric["execution_profile"],
               "formula": "reward = 0 if any gate fails, else the weighted mean of the scored criteria (weights = points)",
               "gates_failed": failed, "criteria": criteria, "groups": groups,
               "aggregation": {"base": base, "deduction": deduction, "reward_before_gates": before_gates}}
    (OUT / "reward-details.json").write_text(json.dumps(details, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def patch_test_sh(text: str) -> str:
    """grade.py's own detail file moves aside; arena_details.py writes Arena's reward-details.json."""
    if "arena_details.py" in text:
        return text
    old = "export ARENA_DETAILS=/logs/verifier/reward-details.json"
    if old not in text or "python3 /tests/grade.py > /logs/verifier/arena-grade.json" not in text:
        raise ValueError("unexpected tests/test.sh; refusing to rewrite it blind")
    text = text.replace(old, "export ARENA_DETAILS=/logs/verifier/grade-details.json")
    return text.rstrip("\n") + "\npython3 /tests/arena_details.py\n"


# ---------------------------------------------------------------- conversion

def unpack(zip_path: Path, dest: Path) -> Path:
    with zipfile.ZipFile(zip_path) as z:
        roots = {n.split("/")[0] for n in z.namelist()}
        if len(roots) != 1:
            raise ValueError(f"{zip_path.name}: expected one top folder, found {sorted(roots)}")
        for info in z.infolist():
            target = dest / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode)
    top = dest / roots.pop()
    if not (top / "task.toml").is_file():
        # Older deliveries wrap the package in a faithfulness folder (F2-scenario/<env-id>/).
        inner = [d for d in top.iterdir() if d.is_dir() and (d / "task.toml").is_file()]
        if len(inner) != 1:
            raise ValueError(f"{zip_path.name}: no single package folder with a task.toml")
        top = inner[0]
    return top


def declared_deliverables(src: Path) -> list[str]:
    doc = json.loads((src / "tests/checks.json").read_text())
    return [d["path"] for d in doc.get("deliverables", []) if d.get("path")]


def convert_runtime(zip_path: Path, out_root: Path, *, domain: str, prompt_language: str) -> dict:
    """Convert one package's runtime surface; the rest of the old package is kept beside it for evidence."""
    work = out_root / ".source" / zip_path.stem
    shutil.rmtree(work, ignore_errors=True)
    src = unpack(zip_path, work)
    return convert_tree(src, out_root, domain=domain, prompt_language=prompt_language)


def convert_tree(src: Path, out_root: Path, *, domain: str, prompt_language: str, version: str | None = None) -> dict:
    """convert_runtime on an unpacked supplier-layout package `src` (a delivered zip's folder, or the one
    pipeline.arena_v01_release builds from a bundle before release). `version` defaults to the next minor."""
    src = Path(src)
    env_id = src.name
    old = tomllib.loads((src / "task.toml").read_text())
    version = version or bump_minor(old["task"]["version"])
    pkg = Path(out_root) / env_id
    shutil.rmtree(pkg, ignore_errors=True)
    pkg.mkdir(parents=True)
    for top in ("environment", "steps", "tests"):
        shutil.copytree(src / top, pkg / top, symlinks=True)
    (pkg / "task.toml").write_text(task_toml(old, env_id=env_id, version=version, domain=domain,
                                             prompt_language=prompt_language, declared=declared_deliverables(src)))
    old_decl = json.loads((src / "declarations.json").read_text()) if (src / "declarations.json").is_file() else []
    (pkg / "declarations.json").write_text(json.dumps(declarations(old_decl, pkg), indent=1) + "\n")
    checks = json.loads((src / "tests/checks.json").read_text())["checks"]
    spec = json.loads((src / "authoring/spec.json").read_text())
    spec_checks = {c["id"]: c for c in spec.get("checks", [])}
    rubrics = pkg / "tests/rubrics"
    shutil.rmtree(rubrics, ignore_errors=True)
    rubrics.mkdir()
    for step in old.get("steps", []):
        (rubrics / f"{step['name']}.json").write_text(json.dumps(rubric(step["name"], checks, spec_checks),
                                                                indent=1, ensure_ascii=False) + "\n")
    details = pkg / "tests/arena_details.py"
    details.write_text(ARENA_DETAILS)
    details.chmod(0o755)
    test_sh = pkg / "tests/test.sh"
    test_sh.write_text(patch_test_sh(test_sh.read_text()))
    test_sh.chmod(0o755)
    tomllib.loads((pkg / "task.toml").read_text())  # parses
    return {"env_id": env_id, "from_version": old["task"]["version"], "version": version,
            "package": str(pkg), "source_package": str(src), "task_tree_sha256": task_tree_sha256(pkg)}


# ---------------------------------------------------------------- provenance (outside the runtime tree)

SOURCE_SCOPE_SCHEMA = "arena-source-scope/v1"
LEDGER_SCHEMA = "arena-source-ledger/v1"
#: The ledger note on every generated (synthetic) data file; the README names them from it.
SYNTHETIC_NOTE = "Synthetic: data generated for this task by a seeded generator; it is not from the recorded session."


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_index(session_dir: Path) -> dict:
    """Every exported workspace snapshot of the session: node -> {path: sha256 of the blob's bytes}."""
    out = {}
    for manifest in sorted((session_dir / "workspaces").glob("*.json")):
        files = {}
        for entry in json.loads(manifest.read_text()).get("files", []):
            blob = session_dir / entry.get("file", "")
            if entry.get("status") == "ok" and blob.is_file():
                files[entry["path"]] = _sha(blob.read_bytes())
        out[manifest.stem] = {"files": files, "manifest_sha256": _sha(manifest.read_bytes())}
    return out


def provenance(pkg: Path, src: Path, *, session_dir: Path, archive: str, member: str, span: tuple[int, int],
               intent: str, workspace: str = "/app/repo") -> tuple[dict, dict]:
    """source-scope v1 and ledger v1 from the source session itself (Arena §14).

    The seed's message range is the one request segment. File citations resolve only through the
    session's exported workspace snapshots: identical bytes are `recorded`, the same path with other
    bytes is `recorded-modified`, anything else is `authored`. The start workspace is the snapshot most
    files were lifted from."""
    data_raw = (session_dir / "data.json").read_bytes()
    trace = json.loads(data_raw)["traces"][0]
    messages = trace["messages"]
    lo, hi = span
    hi = min(hi, len(messages) - 1)
    if not any(messages[i].get("role") == "user" for i in range(lo, hi + 1)):
        raise ValueError(f"{pkg.name}: seed range {lo}..{hi} holds no user message")
    node_of = lambda i, last=False: (messages[i].get("node_ids") or [None])[-1 if last else 0]
    msg_of_node = {n: i for i, m in enumerate(messages) for n in (m.get("node_ids") or [])}
    snaps = snapshot_index(session_dir)
    by_sha: dict[str, list[tuple[str, str]]] = {}
    for node, snap in snaps.items():
        for path, sha in snap["files"].items():
            by_sha.setdefault(sha, []).append((node, path))
    old_ledger = json.loads((src / "provenance/ledger.json").read_text())
    entries, from_node, cited, synthetic = [], {}, set(), []
    repo = pkg / "environment/repo"
    # Two ledger generations: `files` (current deliveries) and `entries` (the first deliveries); both list repo paths.
    for f in sorted(old_ledger.get("files") or old_ledger.get("entries") or [], key=lambda f: f["path"]):
        target = repo / f["path"]
        if not target.is_file():
            continue
        sha = _sha(target.read_bytes())
        container = f"{workspace}/{f['path']}"
        if f.get("origin") == "synthetic":
            # The generated world around the session's code (author/world.py). Arena: "Label invented data as
            # synthetic" and "Name every synthetic part".
            synthetic.append(container)
            entries.append({"path": container, "sha256": sha, "origin": "authored", "note": SYNTHETIC_NOTE})
            continue
        hits = by_sha.get(sha, [])
        same_path = [(n, p) for n, p in hits if p == f["path"]]
        # Arena refuses two ledger entries citing one source file (identical bytes, e.g. several empty
        # __init__.py, would otherwise all cite the first match): each snapshot file is cited at most once.
        free = [h for h in same_path + [h for h in hits if h not in same_path] if h not in cited]
        pick = free[0] if free else None
        if pick:
            cited.add(pick)
            node, spath = pick
            from_node[node] = from_node.get(node, 0) + 1
            cite = {"snapshot_node_id": node, "source_path": spath, "source_sha256": sha}
            if node in msg_of_node:
                cite["message_index"] = msg_of_node[node]
            entries.append({"path": container, "sha256": sha, "origin": "recorded", "citation": cite})
            continue
        prior = [(n, s) for n, snap in snaps.items() for p, s in snap["files"].items() if p == f["path"]]
        if prior:
            node, ssha = prior[-1]
            cite = {"snapshot_node_id": node, "source_path": f["path"], "source_sha256": ssha}
            if node in msg_of_node:
                cite["message_index"] = msg_of_node[node]
            entries.append({"path": container, "sha256": sha, "origin": "recorded-modified", "citation": cite,
                            "note": (f.get("note") or "Reconstructed and edited for the task.")[:1000]})
            continue
        entries.append({"path": container, "sha256": sha, "origin": "authored",
                        "note": (f.get("note") or "Reconstructed from the session's tool calls; no exported "
                                                   "snapshot holds these bytes.")[:1000]})
    authored_files = [e["path"] for e in entries if e["origin"] != "recorded"]
    if from_node:
        node = max(from_node, key=from_node.get)
        start = {"origin": "recorded-modified", "node_id": node, "snapshot_sha256": snaps[node]["manifest_sha256"],
                 "change_note": ("The working tree lifts the files cited as recorded in provenance/ledger.json from "
                                 "this snapshot or the session's other snapshots; "
                                 + (f"{len(authored_files)} file(s) are reconstructed or edited, listed with notes "
                                    "in the ledger." if authored_files else "no file is edited."))}
    else:
        start = {"origin": "synthetic"}
    step_names = [s["name"] for s in tomllib.loads((pkg / "task.toml").read_text()).get("steps", [])]
    scope = {"schema": SOURCE_SCOPE_SCHEMA, "session_id": trace["session_id"],
             "export": {"archive": archive, "member": member, "data_json_sha256": _sha(data_raw)},
             "segments": {"seed-request": {"start": {"node_id": node_of(lo), "message_index": lo},
                                           "end": {"node_id": node_of(hi, True), "message_index": hi}}},
             "steps": {}}
    for i, step in enumerate(step_names):
        scope["steps"][step] = {
            "start_workspace": start if i == 0 else {"origin": "synthetic"},
            "authored": ["Hidden test inputs and checks built from the rules the instruction states (tests/).",
                         "The instruction restates the session user's goal in their voice and states the rules "
                         "each criterion grades; the rubric's acceptance column quotes them."]
                        + ([f"Synthetic data generated for this task by a seeded generator, labelled in the ledger: "
                            f"{', '.join(synthetic[:40])}" + (f" and {len(synthetic) - 40} more" if len(synthetic) > 40
                                                              else "") + "."] if synthetic and i == 0 else []),
            "uses": [{"segment": "seed-request", "use": "request", "adoption": "partial",
                      "adopted": intent.strip()[:2000] or "The session user's goal, as the instruction restates it.",
                      "not_adopted": "Requests and assistant answers in this range that the instruction does not "
                                     "restate; the delivered task grades only the rules the instruction states."}]}
    ledger = {"schema": LEDGER_SCHEMA, "session_id": trace["session_id"],
              "export_data_json_sha256": _sha(data_raw), "entries": entries}
    return scope, ledger
