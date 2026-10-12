"""The pre-calibration gate chain for coding bundles.

Everything decidable by reading the package or its executed receipts. Each
gate is a function returning (findings, evidence); `run.py` orders them. No
gate here asks whether a task is hard -- that is the screen's question.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from .. import policy
from ..author import checks as checks_mod
from ..author import controls as controls_mod
from ..author import probes as probes_mod
from ..author import runtime, solution, toolchain
from ..author import traps as traps_mod
from ..author import validate as validate_mod
from ..author.build import fixture_manifest, load_spec, verifier_requirements
from ..author.contracts import strict_json
from .base import Finding, FunctionGate, GateContext, Severity

B, W, I = Severity.BLOCKER, Severity.WARN, Severity.INFO
REQUIRED_FILES = (
    "task.toml", ".dockerignore",
    "environment/Dockerfile", "environment/.dockerignore", "environment/toolchain.json",
    "tests/Dockerfile", "tests/test.sh", "tests/grade.py", "tests/checks.json", "tests/rubric.json",
    "tests/fixture-manifest.json", "tests/basis.toml", "tests/reward.toml",
    "solution/solve.sh", "solution/fix.patch", "solution/README.md",
    "authoring/spec.json", "authoring/traps.json", "provenance/ledger.json",
)
REQUIRED_DIRS = ("environment/repo", "environment/deps", "tests/suites", "tests/deps", "authoring/mutants")


def _config(ctx: GateContext) -> dict:
    return tomllib.loads(ctx.path("task.toml").read_text())


def _steps(ctx: GateContext) -> list[str]:
    return [s["name"] for s in _config(ctx).get("steps") or []]


# ---------------------------------------------------------------------------

def _structure(ctx: GateContext):
    findings = []
    for rel in REQUIRED_FILES:
        if not ctx.path(rel).is_file():
            findings.append(Finding(B, "missing-file", f"{rel} is missing", rel))
    for rel in REQUIRED_DIRS:
        if not ctx.path(rel).is_dir():
            findings.append(Finding(B, "missing-dir", f"{rel}/ is missing", rel))
    if findings:
        return findings, {}
    config = _config(ctx)
    if config.get("schema_version") != "1.4":
        findings.append(Finding(B, "schema-version", "task.toml must declare schema_version = '1.4'", "task.toml"))
    sources = {(a.get("source") if isinstance(a, dict) else a) for a in config.get("artifacts") or []}
    if "/app/repo" not in sources or "/app/deliverables" not in sources:
        findings.append(Finding(B, "artifacts", "artifacts must collect /app/repo and /app/deliverables", "task.toml"))
    repo_entry = next((a for a in config.get("artifacts") or [] if isinstance(a, dict) and a.get("source") == "/app/repo"), None)
    if repo_entry is None or not repo_entry.get("exclude"):
        findings.append(Finding(B, "artifacts", "the /app/repo artifact must exclude vendored directories", "task.toml"))
    env, agent, verifier = config.get("environment", {}), config.get("agent", {}), config.get("verifier", {})
    if env.get("network_mode") != "no-network":
        findings.append(Finding(B, "network", "[environment] network_mode must be 'no-network'", "task.toml"))
    if agent.get("network_mode") != "no-network" or agent.get("user") != "agent":
        findings.append(Finding(B, "agent", "[agent] must be no-network and run as user 'agent'", "task.toml"))
    if verifier.get("environment_mode") != "separate" or verifier.get("network_mode") != "no-network":
        findings.append(Finding(B, "verifier", "[verifier] must be separate and no-network", "task.toml"))
    if (verifier.get("environment") or {}).get("network_mode") != "no-network":
        findings.append(Finding(B, "verifier", "[verifier.environment] must restate no-network (Harbor defaults it to public)", "task.toml"))
    steps = config.get("steps") or []
    if not steps:
        findings.append(Finding(B, "steps", "task.toml declares no [[steps]]", "task.toml"))
    for step in steps:
        name = step.get("name", "")
        if (step.get("verifier") or {}).get("env", {}).get("ARENA_STEP") != name:
            findings.append(Finding(B, "step-env", f"step {name}: [steps.verifier] env must set ARENA_STEP = '{name}'", "task.toml"))
        for rel in (f"steps/{name}/instruction.md", f"steps/{name}/solution/solve.sh", f"steps/{name}/solution/fix.patch"):
            if not ctx.path(rel).is_file():
                findings.append(Finding(B, "missing-file", f"{rel} is missing", rel))
    if len(steps) > 1 and config.get("multi_step_reward_strategy") != "mean":
        findings.append(Finding(B, "steps", "multi-step tasks must declare multi_step_reward_strategy = 'mean'", "task.toml"))
    meta = config.get("metadata") or {}
    if meta.get("pipeline") != "arena-coding":
        findings.append(Finding(B, "metadata", "[metadata] pipeline must be 'arena-coding'", "task.toml"))
    for key in ("source_trace", "source_seeds", "target_level", "language", "toolchain"):
        if key not in meta:
            findings.append(Finding(B, "metadata", f"[metadata] {key} is missing", "task.toml"))
    ignore = ctx.path(".dockerignore").read_text()
    for rel in ("tests/", "solution/", "authoring/", "verification/", "provenance/"):
        if rel not in ignore:
            findings.append(Finding(B, "dockerignore", f"root .dockerignore must exclude {rel}", ".dockerignore"))
    return findings, {"steps": [s.get("name") for s in steps]}


def _grader_runtime(ctx: GateContext):
    return [Finding(B, "grader-runtime", p, "tests/grade.py") for p in runtime.problems(ctx.task_dir)], \
        {"sha256": runtime.sha256(ctx.task_dir)}


def _grader_data(ctx: GateContext):
    findings = []
    doc = checks_mod.load_shipped(ctx.path("tests/checks.json"))
    steps = _steps(ctx)
    checks = doc["checks"]
    for p in checks_mod.weight_problems(checks, steps):
        findings.append(Finding(B, "weight-budget", p, "tests/checks.json"))
    rubric = strict_json(ctx.path("tests/rubric.json").read_text())
    if rubric != checks_mod.rubric(checks, steps):
        findings.append(Finding(B, "rubric-drift", "tests/rubric.json does not match checks.json", "tests/rubric.json"))
    chain = toolchain.load(ctx.task_dir)
    manifest = strict_json(ctx.path("tests/fixture-manifest.json").read_text())
    if manifest != fixture_manifest(ctx.task_dir, chain.get("vendored", [])):
        findings.append(Finding(B, "fixture-manifest", "tests/fixture-manifest.json does not match environment/repo",
                                "tests/fixture-manifest.json"))
    suites = ctx.path("tests/suites")
    for c in checks:
        runner = c.get("runner") or {}
        if runner.get("type") in ("pytest", "node-test", "jest", "vitest", "mocha", "playwright", "go-test"):
            for item in runner.get("select") or []:
                if not (suites / str(item).split("::", 1)[0]).is_file():
                    findings.append(Finding(B, "missing-suite", f"check {c['id']} selects {item}, absent from tests/suites",
                                            "tests/suites"))
    spec = load_spec(ctx.task_dir)
    spec_checks = spec.get("checks") or []
    if ctx.path("tests/basis.toml").read_text() != checks_mod.basis_toml(spec_checks):
        findings.append(Finding(B, "basis-drift", "tests/basis.toml is not what the spec generates", "tests/basis.toml"))
    if ctx.path("tests/reward.toml").read_text() != checks_mod.reward_toml(spec_checks, steps, checks_mod.rubric(spec_checks, steps)):
        findings.append(Finding(B, "reward-drift", "tests/reward.toml is not what the spec generates", "tests/reward.toml"))
    if [checks_mod.runtime_check(c) for c in spec_checks] != checks:
        findings.append(Finding(B, "checks-drift", "tests/checks.json is not what authoring/spec.json generates", "tests/checks.json"))
    return findings, {"checks": len(checks), "objectives": sum(1 for c in checks if c["kind"] == "objective")}


def _toolchain_pinned(ctx: GateContext):
    findings = [Finding(B, "dockerfile", p, "environment/Dockerfile") for p in toolchain.dockerfile_problems(ctx.task_dir)]
    for rel in ("environment/Dockerfile", "tests/Dockerfile"):
        for line in ctx.path(rel).read_text().splitlines():
            if line.startswith("FROM ") and "@sha256:" not in line:
                findings.append(Finding(B, "unpinned-image", f"{rel}: {line.strip()} is not digest-pinned", rel))
    for rel in ("environment/deps/requirements.txt", "tests/deps/requirements.txt", "tests/deps/requirements-test.txt"):
        path = ctx.path(rel)
        if path.is_file():
            for line in path.read_text().splitlines():
                body = line.split("#", 1)[0].strip()
                if body and "==" not in body and not body.startswith("-"):
                    findings.append(Finding(B, "unpinned-dependency", f"{rel}: {body!r} is not pinned with ==", rel))
    env_deps, test_deps = ctx.path("environment/deps"), ctx.path("tests/deps")
    for path in env_deps.rglob("*"):
        if path.is_file():
            rel = path.relative_to(env_deps).as_posix()
            twin = test_deps / rel
            if not twin.is_file():
                findings.append(Finding(B, "deps-drift", f"tests/deps/{rel} is missing (the verifier needs the same manifests)", "tests/deps"))
            elif rel == "requirements.txt":
                # The verifier's copy leaves out only a pin that collides with the grader's own
                # test pins (build.verifier_requirements); anything else must be the same bytes.
                pins = test_deps / "requirements-test.txt"
                want = verifier_requirements(path.read_text(), pins.read_text().splitlines() if pins.is_file() else [])
                if twin.read_text() != want:
                    findings.append(Finding(B, "deps-drift", f"tests/deps/{rel} differs from environment/deps/{rel}", "tests/deps"))
            elif rel != "package.json" and twin.read_bytes() != path.read_bytes():
                findings.append(Finding(B, "deps-drift", f"tests/deps/{rel} differs from environment/deps/{rel}", "tests/deps"))
    return findings, {}


def _privacy_clean(ctx: GateContext):
    """No private data in the fixture the agent will read.

    The initial extraction is redacted before the design starts and delivery
    refuses a bundle with findings, but nothing between the two checked a
    fixture the designer re-lifted or authored: c-002 (2026-09-28) reached
    its repair round with an email address back in `uploads/index.html`. A
    finding here blocks; an `unscannable` file (a binary the scanner cannot
    read) blocks too, because "could not check" is not "clean".
    """
    from ..privacy import scan
    findings = []
    for row in scan(ctx.path("environment/repo")):
        findings.append(Finding(B, "private-data", f"{row.get('kind')} in fixture {row.get('file')}"
                                + (f" line {row['line']}" if row.get("line") else ""), row.get("file", "")))
    return findings, {"privacy_findings": len(findings)}


def _hidden_disjoint(ctx: GateContext):
    findings = []
    repo, suites = ctx.path("environment/repo"), ctx.path("tests/suites")
    repo_hashes = {}
    for p in repo.rglob("*"):
        if p.is_file():
            repo_hashes[hashlib.sha256(p.read_bytes()).hexdigest()] = p.relative_to(repo).as_posix()
    repo_text = {}
    for p in repo.rglob("*"):
        if p.is_file():
            try:
                repo_text[p.relative_to(repo).as_posix()] = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                pass
    for p in suites.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(suites).as_posix()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest in repo_hashes:
            findings.append(Finding(B, "hidden-in-fixture", f"tests/suites/{rel} is byte-identical to fixture {repo_hashes[digest]}", rel))
        try:
            body = p.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            continue
        if len(body) >= 40:
            for fixture_rel, text in repo_text.items():
                if body in text:
                    findings.append(Finding(B, "hidden-in-fixture", f"tests/suites/{rel} appears inside fixture {fixture_rel}", rel))
                    break
    spec = load_spec(ctx.task_dir)
    lookup = probes_mod.answer_lookup(ctx.task_dir, spec)
    for hit in lookup["reference_lines_found"]:
        findings.append(Finding(B, "reference-readable", f"a line of the reference patch is readable in fixture {hit['file']}", hit["file"]))
    return findings, {"leaks": lookup["leaks"]}


def _leak_sweep(ctx: GateContext):
    findings = []
    env = ctx.path("environment")
    for p in env.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ctx.task_dir).as_posix()
        if p.name.endswith((".orig", ".rej", ".bak", ".patch", ".diff")) or ".git" in p.parts:
            findings.append(Finding(B, "leftover", f"{rel}: patch, backup or git history in the agent's environment", rel))
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if rel.startswith("environment/repo/") and ("/tests/" in text or "reward.json" in text or "arena-grade" in text):
            findings.append(Finding(B, "grader-reference", f"{rel} mentions the grader's paths", rel))
    ignore = ctx.path("environment/.dockerignore").read_text().splitlines()
    if not ignore or ignore[0].strip() != "*":
        findings.append(Finding(B, "dockerignore", "environment/.dockerignore must start with '*' (allow-list)", "environment/.dockerignore"))
    lookup_leaks = probes_mod.answer_lookup(ctx.task_dir, load_spec(ctx.task_dir))["leaks"]
    for leak in lookup_leaks:
        findings.append(Finding(B, "answer-leak", leak, "environment/repo"))
    return findings, {}


def _solution_derived(ctx: GateContext):
    findings = [Finding(B, "solution", p, "solution/") for p in solution.problems(ctx.task_dir)]
    findings += [Finding(B, "alternative", p, "authoring/alt") for p in solution.alternative_problems(ctx.task_dir)]
    return findings, {}


def _trap_faithful(ctx: GateContext):
    findings = []
    doc = strict_json(ctx.path("authoring/traps.json").read_text())
    traps = doc.get("traps") or []
    checks = checks_mod.load_shipped(ctx.path("tests/checks.json"))["checks"]
    chain = toolchain.load(ctx.task_dir)
    fixture = set(fixture_manifest(ctx.task_dir, chain.get("vendored", [])))
    mutants = {p.stem for p in ctx.path("authoring/mutants").glob("*.patch")}
    for p in traps_mod.validate_all(traps, {c["id"] for c in checks}, fixture, mutants):
        findings.append(Finding(B, "trap-invalid", p, "authoring/traps.json"))
    if len(traps) < 2:
        findings.append(Finding(B, "too-few-traps", f"{len(traps)} traps; at least three genuine pitfalls are expected", "authoring/traps.json"))
    elif len(traps) == 2:
        findings.append(Finding(W, "few-traps", "only two traps; a third genuine pitfall usually exists", "authoring/traps.json"))
    usable = int(doc.get("usable_corrections") or 0)
    tier = doc.get("source_tier")
    exploitation = traps_mod.trace_exploitation(traps, usable)
    if usable and tier and exploitation + 1e-9 < traps_mod.floor_for(tier):
        findings.append(Finding(B, "trace-floor", f"traps cite {exploitation:.0%} of {usable} usable corrections; tier {tier} "
                                f"requires {traps_mod.floor_for(tier):.0%}", "authoring/traps.json"))
    if not usable:
        findings.append(Finding(W, "no-corrections", "no usable corrections; every trap is authored", "authoring/traps.json"))
    axes = traps_mod.axes_loaded(traps)
    if len(axes) < 2:
        findings.append(Finding(W, "one-axis", f"traps load only {sorted(axes)}; load at least two axes", "authoring/traps.json"))
    share = traps_mod.trace_derived_share(traps)
    level = (tomllib.loads(ctx.path("task.toml").read_text()).get("metadata") or {}).get("target_level")
    return findings, {"traps": len(traps), "trace_derived_share": round(share, 3), "exploitation": round(exploitation, 3),
                      "target_level": level, "level_floors": policy.FAITHFULNESS_TRACE_DERIVED_FLOOR, "axes": sorted(axes)}


def _trap_beatable(ctx: GateContext):
    findings = [Finding(B, "probes", p, "verification/probes.json") for p in probes_mod.problems(ctx.task_dir)]
    if findings:
        return findings, {}
    report = strict_json(ctx.path("verification/probes.json").read_text())
    results = report.get("results", {})
    if not results.get("golden", {}).get("ok"):
        findings.append(Finding(B, "golden", f"the reference scores {results.get('golden', {}).get('reward')}, not 1.0", "solution/"))
    if not results.get("seed_zero", {}).get("ok"):
        findings.append(Finding(B, "seed-zero", "the untouched fixture does not score 0 with every guardrail intact", "tests/"))
    for mid, m in (results.get("per_trap", {}).get("mutants") or {}).items():
        if not m.get("ok"):
            findings.append(Finding(B, "mutant", f"mutant {mid} (trap {m.get('trap')}) failed {m.get('failed')}, declared {m.get('declared')}",
                                    f"authoring/mutants/{mid}.patch"))
    return findings, {"mutants": len(results.get("per_trap", {}).get("mutants") or {})}


def _quality_controls(ctx: GateContext):
    findings = [Finding(B, "controls", p, "verification/controls.json") for p in controls_mod.problems(ctx.task_dir)]
    if findings:
        return findings, {}
    receipt = strict_json(ctx.path("verification/controls.json").read_text())
    for a in receipt.get("advisories") or []:
        findings.append(Finding(W, "controls-advisory", a, "verification/controls.json"))
    return findings, {"determinism": receipt["results"].get("determinism"), "coverage": receipt["results"].get("coverage")}


def _quality_spec(ctx: GateContext):
    return [Finding(B, "spec", p, "authoring/spec.json") for p in validate_mod.validate_bundle(ctx.task_dir)], {}


def _hacking_resistance(ctx: GateContext):
    findings = [Finding(B, "probes", p, "verification/probes.json") for p in probes_mod.problems(ctx.task_dir)]
    if findings:
        return findings, {}
    report = strict_json(ctx.path("verification/probes.json").read_text())
    for name in probes_mod.REQUIRED:
        if name not in report.get("results", {}):
            findings.append(Finding(B, "probe-missing", f"probe {name} was not run", "verification/probes.json"))
    if not report.get("results", {}).get("isolation", {}).get("ok"):
        findings.append(Finding(B, "isolation", "; ".join(report["results"]["isolation"].get("problems", [])), "environment/Dockerfile"))
    return findings, {"untested_surfaces": report.get("untested_surfaces")}


def _contract_published(ctx: GateContext):
    findings = []
    spec = load_spec(ctx.task_dir)
    instructions = {s["name"]: " ".join(s["instruction"].split()) for s in spec.get("steps") or []}
    for c in spec.get("checks") or []:
        quotes = c.get("rule_quote") or []
        if isinstance(quotes, str):
            quotes = [quotes]
        text = instructions.get(c.get("step"), "")
        if not quotes:
            findings.append(Finding(B, "unpublished-rule", f"check {c.get('id')} quotes no instruction sentence", "authoring/spec.json"))
        for q in quotes:
            if " ".join(str(q).split()) not in text:
                findings.append(Finding(B, "unpublished-rule", f"check {c.get('id')}: rule_quote is not verbatim in step {c.get('step')}",
                                        f"steps/{c.get('step')}/instruction.md"))
    return findings, {}


def _bundle_ledger(ctx: GateContext):
    from ..ledger import faithfulness
    findings = [Finding(B, "ledger", p, "provenance/ledger.json") for p in faithfulness.problems(ctx.task_dir)]
    if findings:
        return findings, {}
    ledger = strict_json(ctx.path("provenance/ledger.json").read_text())
    meta = tomllib.loads(ctx.path("task.toml").read_text()).get("metadata") or {}
    target = meta.get("target_level")
    floor = policy.CATEGORY_FIXTURE_FLOOR.get(target)
    fidelity = ledger.get("faithfulness_by_bytes", 0)
    if floor is not None and fidelity < floor:
        findings.append(Finding(W, "fidelity-below-target",
                                f"fixture fidelity {fidelity:.0%} is below the {target} target floor {floor:.0%}; "
                                f"the measured level is {ledger.get('faithfulness_level')}", "provenance/ledger.json"))
    return findings, {"fidelity": fidelity, "level": ledger.get("faithfulness_level"), "counts": ledger.get("counts")}


structure_gate = FunctionGate("bundle.structure", _structure)
ledger_gate = FunctionGate("bundle.ledger", _bundle_ledger, ("bundle.structure",))
grader_runtime_gate = FunctionGate("grader.runtime", _grader_runtime, ("bundle.structure",))
grader_data_gate = FunctionGate("grader.data", _grader_data, ("bundle.structure",))
toolchain_gate = FunctionGate("toolchain.pinned", _toolchain_pinned, ("bundle.structure",))
hidden_gate = FunctionGate("hidden.disjoint", _hidden_disjoint, ("bundle.structure",))
privacy_gate = FunctionGate("privacy.clean", _privacy_clean, ("bundle.structure",))
leak_gate = FunctionGate("leak.sweep", _leak_sweep, ("bundle.structure",))
solution_gate = FunctionGate("solution.derived", _solution_derived, ("bundle.structure",))
trap_faithful_gate = FunctionGate("trap.faithful", _trap_faithful, ("bundle.structure",))
trap_beatable_gate = FunctionGate("trap.beatable", _trap_beatable, ("trap.faithful",))
controls_gate = FunctionGate("quality.controls", _quality_controls, ("bundle.structure",))
spec_gate = FunctionGate("quality.spec", _quality_spec, ("bundle.structure",))
hack_gate = FunctionGate("hacking.resistance", _hacking_resistance, ("bundle.structure",))
published_gate = FunctionGate("contract.published", _contract_published, ("bundle.structure",))

CHAIN = (structure_gate, ledger_gate, grader_runtime_gate, grader_data_gate, toolchain_gate, hidden_gate,
         privacy_gate, leak_gate, solution_gate, trap_faithful_gate, trap_beatable_gate, controls_gate,
         spec_gate, hack_gate, published_gate)
