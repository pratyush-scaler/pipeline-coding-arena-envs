"""Materialise a coding spec into a Harbor bundle the gate chain can read.

The spec (`authoring/spec.json`) is the single source of truth: every
generated file is derived from it and from the fixture already materialised
under `environment/repo/` (see `fixture.py`), so instruction, fixture, tests
and reference cannot drift apart.

    <bundle>/
      task.toml                       Harbor 1.4; [[steps]]; [[artifacts]]
      steps/<step>/instruction.md
      steps/<step>/solution/          solve.sh + fix.patch for Harbor's oracle
      environment/toolchain.json      the container recipe
      environment/Dockerfile          generated from toolchain.json (agent)
      environment/deps/               dependency manifests (pinned)
      environment/repo/               THE FIXTURE (materialised earlier)
      tests/Dockerfile                generated from toolchain.json (verifier)
      tests/test.sh, tests/grade.py   the pinned runtime
      tests/checks.json               the grader as data
      tests/rubric.json               step -> check ids
      tests/fixture-manifest.json     sha256/bytes of every fixture file
      tests/suites/                   the hidden tests and hidden inputs
      tests/deps/                     environment/deps + test-only deps
      tests/basis.toml, reward.toml   human-readable rubric
      solution/                       README, fix.patch (single-step)
      authoring/spec.json, traps.json, mutants/, alt/
      provenance/ledger.json          written by the ledger stage
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path

from . import checks as checks_mod
from . import runtime, toolchain
from .contracts import ContractError, contained, strict_json

STEP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TASK_TOML = '''schema_version = "1.4"
multi_step_reward_strategy = "mean"

[[artifacts]]
source = "/app/repo"
exclude = {exclude}

[[artifacts]]
source = "/app/deliverables"

[task]
name = {name}
version = "1.0.0"
description = {description}
keywords = {keywords}

[metadata]
pipeline = "arena-coding"
source_trace = {source_trace}
source_seeds = {source_seeds}
source_tier = {source_tier}
target_level = {target_level}
language = {language}
toolchain = {toolchain_name}
difficulty_explanation = """
{difficulty}
"""

[environment]
os = "linux"
network_mode = "no-network"
build_timeout_sec = 1800.0
cpus = {cpus}
memory_mb = {memory_mb}

[agent]
timeout_sec = {agent_timeout}
network_mode = "no-network"
user = "agent"

[verifier]
timeout_sec = {verifier_timeout}
network_mode = "no-network"
environment_mode = "separate"

[verifier.environment]
network_mode = "no-network"

{steps}'''

SOLVE_SH = '''#!/bin/sh
# Harbor's OracleAgent copies this directory to /solution and runs it inside
# the agent container. The reference DERIVES the fixed repository by applying
# a patch to the source and then running the build the instruction names;
# nothing here writes an expected output directly.
set -eu
cd /app/repo
if git apply --whitespace=nowarn /solution/fix.patch 2>/dev/null; then :; else
  patch -p1 --forward < /solution/fix.patch
fi
{commands}'''


def _multiline(text: str) -> str:
    return str(text).strip().replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def step_verifier_timeout(step: str, checks: list[dict], floor: float) -> float:
    """Seconds one step's grading may take: every check of the step timing out (grade.py's
    per-check `timeout_s`, default 120) plus `policy.STEP_VERIFIER_MARGIN_S`, never under `floor`."""
    from .. import policy
    from .checks import STRUCTURAL
    total = sum(float((c.get("runner") or {}).get("timeout_s", 120)) for c in checks or []
                if c.get("step") == step and (c.get("runner") or {}).get("type") not in STRUCTURAL)
    return float(max(floor, total + policy.STEP_VERIFIER_MARGIN_S))


def _steps_toml(steps: list[dict], checks: list[dict] | None = None, floor: float | None = None) -> str:
    out = []
    for step in steps:
        name = json.dumps(str(step["name"]), ensure_ascii=False)
        timeout = ("" if checks is None else
                   f"timeout_sec = {step_verifier_timeout(step['name'], checks, floor or 0.0)}\n")
        out.append(f"[[steps]]\nname = {name}\n[steps.verifier]\n{timeout}env = {{ ARENA_STEP = {name} }}\n")
    return "\n".join(out)


def compile_steps(spec: dict) -> list[dict]:
    steps = deepcopy(spec.get("steps") or [])
    if not steps or any(not isinstance(s, dict) or not isinstance(s.get("name"), str)
                        or not STEP_RE.match(s["name"]) or not isinstance(s.get("instruction"), str)
                        or not s["instruction"].strip() for s in steps):
        raise ContractError("steps must be a non-empty list of {name, instruction}")
    names = [s["name"] for s in steps]
    if len(set(names)) != len(names) or names != sorted(names):
        raise ContractError("step names must be unique and sorted (01-..., 02-...)")
    return steps


def fixture_manifest(bundle: Path, vendored: list[str]) -> dict[str, dict]:
    """sha256 and bytes of every fixture file, keyed by repo-relative path."""
    repo = Path(bundle) / "environment/repo"
    if not repo.is_dir():
        raise ContractError("environment/repo is missing: materialise the fixture first")
    skip = {Path(v).name for v in vendored} | {"__pycache__", ".pytest_cache"}
    manifest = {}
    for path in sorted(repo.rglob("*")):
        if any(part in skip for part in path.relative_to(repo).parts):
            continue
        if path.is_symlink():
            raise ContractError(f"symlink in fixture: {path.relative_to(repo)}")
        if path.is_file():
            data = path.read_bytes()
            manifest[path.relative_to(repo).as_posix()] = {
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            if path.suffix == ".py":
                from .grader_runtime.grade import fixture_benign_syntax
                baseline = fixture_benign_syntax(data.decode("utf-8", "replace"))
                if baseline:
                    manifest[path.relative_to(repo).as_posix()]["benign_syntax"] = baseline
    if not manifest:
        raise ContractError("environment/repo holds no files")
    return manifest


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _write_tree(root: Path, files: dict, label: str) -> list[str]:
    """Write {relative path: text} under root, refusing escapes. Returns paths."""
    written = []
    for rel, content in sorted((files or {}).items()):
        if not isinstance(content, str):
            raise ContractError(f"{label}: {rel} must be text")
        target = contained(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def _solution_for(spec: dict, step: str) -> dict:
    solution = spec.get("solution")
    if not isinstance(solution, dict):
        raise ContractError("solution must map each step to {patch, commands?, notes?}")
    entry = solution.get(step)
    if not isinstance(entry, dict) or not isinstance(entry.get("patch"), str) or not entry["patch"].strip():
        raise ContractError(f"solution[{step!r}] needs a unified-diff patch against the fixture")
    return entry


_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def verifier_requirements(text: str, test_pins: list[str]) -> str:
    """The verifier's copy of requirements.txt: a task pin that names a test-only package at a
    DIFFERENT exact version is left out there, because the verifier installs both files in one
    pip call and the grader's pin must win. The agent image keeps the declared version
    (c-043, 2026-10-07: the repo's declared pytest==7.4.2 against the grader's pytest==8.3.4
    made the verifier image unbuildable). Anything that already resolves is returned unchanged."""
    def key(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()
    pins = {key(m.group(1)): m.group(3) for m in map(_PIN.match, test_pins) if m}
    out, dropped = [], []
    for line in text.splitlines():
        m = _PIN.match(line)
        if m and key(m.group(1)) in pins and m.group(3) != pins[key(m.group(1))]:
            dropped.append(line.strip())
            continue
        out.append(line)
    if not dropped:
        return text
    note = [f"# verifier: {d} left out; requirements-test.txt pins the grader's version" for d in dropped]
    return "\n".join(note + out) + "\n"


def _test_deps(spec: dict, chain: dict) -> dict[str, str]:
    """tests/deps: the environment manifests plus the pinned test-only deps."""
    deps = dict(spec.get("deps") or {})
    name = chain["name"]
    test = toolchain.TEST_DEPS.get(name)
    if name in ("python", "php", "c", "shell"):
        lines = list(test or toolchain.TEST_DEPS["python"])
        deps["requirements-test.txt"] = "\n".join(lines) + "\n"
        deps["requirements.txt"] = verifier_requirements(deps.get("requirements.txt") or "", lines)
    elif name in ("node", "browser"):
        package = json.loads(deps["package.json"]) if deps.get("package.json") else {"name": "fixture", "private": True}
        dev = dict(package.get("devDependencies") or {})
        for key, version in (test or {}).items():
            dev.setdefault(key, version)
        package["devDependencies"] = dev
        deps["package.json"] = json.dumps(package, indent=2) + "\n"
    return deps


def build(spec: dict, bundle: Path) -> dict:
    """Write every generated file. Never touches environment/repo."""
    bundle = Path(bundle)
    steps = compile_steps(spec)
    names = [s["name"] for s in steps]
    chain_spec = spec.get("toolchain") or {}
    if not isinstance(chain_spec, dict) or chain_spec.get("name") not in toolchain.TOOLCHAINS:
        raise ContractError("toolchain.name must be one of " + ", ".join(toolchain.TOOLCHAINS))
    limits = chain_spec.get("limits") or {}
    chain = toolchain.record(chain_spec["name"], language=spec.get("language") or chain_spec["name"],
                             extra_apt=chain_spec.get("extra_apt"), post_install=chain_spec.get("post_install"),
                             verifier_timeout_s=int(limits.get("verifier_timeout_s", 1800)),
                             agent_timeout_s=int(limits.get("agent_timeout_s", 10800)),
                             cpus=int(limits.get("cpus", 2)), memory_mb=int(limits.get("memory_mb", 4096)),
                             login_shell_path=bool(chain_spec.get("login_shell_path")))
    checks = deepcopy(spec.get("checks") or [])
    problems = checks_mod.check_problems(checks, names, chain["runners"])
    if problems:
        raise ContractError("checks: " + "; ".join(problems[:6]))
    checks_mod.normalise_weights(checks, steps=names)
    rubric = checks_mod.rubric(checks, names)
    manifest = fixture_manifest(bundle, chain["vendored"])

    # -- task.toml and steps -------------------------------------------------
    source = spec.get("source") or {}
    _write(bundle / "task.toml", TASK_TOML.format(
        exclude=json.dumps(sorted({Path(v).name for v in chain["vendored"]} | {"__pycache__", ".pytest_cache", ".cache"})),
        name=json.dumps(f"arena/{spec['name']}"),
        description=json.dumps(spec.get("description", ""), ensure_ascii=False),
        keywords=json.dumps(spec.get("keywords", []), ensure_ascii=False),
        source_trace=json.dumps(source.get("session_id", "")),
        source_seeds=json.dumps([source.get("seed_index", 0)]),
        source_tier=json.dumps(source.get("tier", 0)),
        target_level=json.dumps(spec.get("target_level", "")),
        language=json.dumps(chain["language"]), toolchain_name=json.dumps(chain["name"]),
        difficulty=_multiline(spec.get("difficulty", "")),
        cpus=chain["limits"]["cpus"], memory_mb=chain["limits"]["memory_mb"],
        agent_timeout=float(chain["limits"]["agent_timeout_s"]),
        verifier_timeout=float(chain["limits"]["verifier_timeout_s"]),
        steps=_steps_toml(steps, checks, float(chain["limits"]["verifier_timeout_s"]))))
    for step in steps:
        _write(bundle / "steps" / step["name"] / "instruction.md", step["instruction"].strip() + "\n")

    # -- environment ---------------------------------------------------------
    env = bundle / "environment"
    _write(env / "toolchain.json", json.dumps(chain, indent=2, sort_keys=True) + "\n")
    deps_dir = env / "deps"
    if deps_dir.exists():
        shutil.rmtree(deps_dir)
    deps_dir.mkdir(parents=True)
    _write_tree(deps_dir, spec.get("deps") or {}, "deps")
    for required in chain["manifests"]:
        if required in ("package-lock.json", "go.sum", "Cargo.lock"):
            continue  # a lockfile may be absent for a repo with no dependencies
        if not (deps_dir / required).is_file():
            _write(deps_dir / required, "" if required.endswith(".txt") else
                   json.dumps({"name": "fixture", "private": True, "dependencies": {}}, indent=2) + "\n"
                   if required == "package.json" else "")
    _write(env / "Dockerfile", toolchain.agent_dockerfile(chain))
    _write(env / ".dockerignore", "*\n!Dockerfile\n!repo/\n!repo/**\n!deps/\n!deps/**\n")

    # -- tests ---------------------------------------------------------------
    tests = bundle / "tests"
    for stale in ("suites", "deps", "key"):
        if (tests / stale).exists():
            shutil.rmtree(tests / stale)
    tests.mkdir(parents=True, exist_ok=True)
    runtime.stage(bundle)
    _write(tests / "test.sh", "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\n"
           "export ARENA_REWARD=/logs/verifier/reward.json\n"
           "export ARENA_DETAILS=/logs/verifier/reward-details.json\n"
           "python3 /tests/grade.py > /logs/verifier/arena-grade.json\n", 0o755)
    shipped = checks_mod.shipped_document(checks, toolchain.runtime_view(chain), spec.get("deliverables") or [])
    _write(tests / "checks.json", json.dumps(shipped, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    _write(tests / "rubric.json", json.dumps(rubric, indent=2) + "\n")
    _write(tests / "fixture-manifest.json", json.dumps(manifest, indent=2) + "\n")
    suites = spec.get("suites") or {}
    if not suites:
        raise ContractError("suites: the hidden tests are missing")
    _write_tree(tests / "suites", suites, "suites")
    _write_tree(tests / "deps", _test_deps(spec, chain), "test deps")
    _write(tests / "Dockerfile", toolchain.verifier_dockerfile(chain))
    _write(tests / "basis.toml", checks_mod.basis_toml(checks))
    _write(tests / "reward.toml", checks_mod.reward_toml(checks, names, rubric))
    _write(tests / "README.md", "# Hidden tests\n\n`grade.py` executes `checks.json`; `basis.toml` "
           "quotes the public requirement each check enforces; `suites/` holds the tests and "
           "hidden inputs; `fixture-manifest.json` pins every fixture byte for the structural guards.\n")

    # -- solution ------------------------------------------------------------
    for step in steps:
        entry = _solution_for(spec, step["name"])
        commands = "\n".join(str(c) for c in entry.get("commands") or [])
        dest = bundle / "steps" / step["name"] / "solution"
        if dest.exists():
            shutil.rmtree(dest)
        _write(dest / "fix.patch", entry["patch"] if entry["patch"].endswith("\n") else entry["patch"] + "\n")
        _write(dest / "solve.sh", SOLVE_SH.format(commands=commands + ("\n" if commands else "")), 0o755)
    top = bundle / "solution"
    if top.exists():
        shutil.rmtree(top)
    first = _solution_for(spec, names[0])
    _write(top / "fix.patch", first["patch"] if first["patch"].endswith("\n") else first["patch"] + "\n")
    _write(top / "solve.sh", SOLVE_SH.format(
        commands="\n".join(str(c) for c in first.get("commands") or []) + ("\n" if first.get("commands") else "")), 0o755)
    _write(top / "README.md", "# Reference solution\n\n" + str(spec.get("solution_notes") or
           "The reference applies `fix.patch` to the fixture and runs the build the instruction names.\n")
           + "\n\nProvenance: " + json.dumps({k: v.get("provenance") for k, v in (spec.get("solution") or {}).items()},
                                              ensure_ascii=False, sort_keys=True) + "\n")

    # -- authoring -----------------------------------------------------------
    authoring = bundle / "authoring"
    authoring.mkdir(parents=True, exist_ok=True)
    stored = deepcopy(spec)
    stored["checks"] = checks
    _write(authoring / "spec.json", json.dumps(stored, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    traps = {"source_tier": source.get("tier"), "usable_corrections": spec.get("usable_corrections", 0),
             "traps": spec.get("traps") or []}
    _write(authoring / "traps.json", json.dumps(traps, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    for folder in ("mutants", "alt"):
        if (authoring / folder).exists():
            shutil.rmtree(authoring / folder)
    for mutant in spec.get("mutants") or []:
        if not isinstance(mutant, dict) or not isinstance(mutant.get("id"), str):
            raise ContractError("mutants must be objects with an id")
        _write(contained(authoring / "mutants", mutant["id"] + ".patch"),
               str(mutant.get("patch", "")).rstrip("\n") + "\n")
    for step_name, entry in (spec.get("alternative") or {}).items():
        if step_name in names and isinstance(entry, dict) and isinstance(entry.get("patch"), str):
            _write(contained(authoring / "alt", step_name + ".patch"), entry["patch"].rstrip("\n") + "\n")
    _write(bundle / ".dockerignore", "tests/\nsolution/\nauthoring/\nverification/\nprovenance/\n")
    return {"steps": names, "checks": len(checks), "fixture_files": len(manifest),
            "objectives": sum(1 for c in checks if c["kind"] == "objective"),
            "guardrails": sum(1 for c in checks if c["kind"] == "guardrail")}


def environment_only(spec: dict, bundle: Path) -> dict:
    """Write just environment/ (toolchain, deps, Dockerfile) for a draft whose
    checks and tests may not exist yet, so the designer can build the agent
    image and run commands against the fixture early in a design."""
    bundle = Path(bundle)
    chain_spec = spec.get("toolchain") or {}
    if not isinstance(chain_spec, dict) or chain_spec.get("name") not in toolchain.TOOLCHAINS:
        raise ContractError("stage `toolchain` first: toolchain.name must be one of "
                            + ", ".join(toolchain.TOOLCHAINS))
    limits = chain_spec.get("limits") or {}
    chain = toolchain.record(chain_spec["name"], language=spec.get("language") or chain_spec["name"],
                             extra_apt=chain_spec.get("extra_apt"), post_install=chain_spec.get("post_install"),
                             verifier_timeout_s=int(limits.get("verifier_timeout_s", 1800)),
                             agent_timeout_s=int(limits.get("agent_timeout_s", 10800)),
                             cpus=int(limits.get("cpus", 2)), memory_mb=int(limits.get("memory_mb", 4096)),
                             login_shell_path=bool(chain_spec.get("login_shell_path")))
    if not (bundle / "environment/repo").is_dir():
        raise ContractError("environment/repo is missing: materialise the fixture first")
    env = bundle / "environment"
    _write(env / "toolchain.json", json.dumps(chain, indent=2, sort_keys=True) + "\n")
    deps_dir = env / "deps"
    if deps_dir.exists():
        shutil.rmtree(deps_dir)
    deps_dir.mkdir(parents=True)
    _write_tree(deps_dir, spec.get("deps") or {}, "deps")
    for required in chain["manifests"]:
        if required in ("package-lock.json", "go.sum", "Cargo.lock"):
            continue
        if not (deps_dir / required).is_file():
            _write(deps_dir / required, "" if required.endswith(".txt") else
                   json.dumps({"name": "fixture", "private": True, "dependencies": {}}, indent=2) + "\n"
                   if required == "package.json" else "")
    _write(env / "Dockerfile", toolchain.agent_dockerfile(chain))
    _write(env / ".dockerignore", "*\n!Dockerfile\n!repo/\n!repo/**\n!deps/\n!deps/**\n")
    return chain


def load_spec(bundle: Path) -> dict:
    return strict_json((Path(bundle) / "authoring/spec.json").read_text(encoding="utf-8"))
