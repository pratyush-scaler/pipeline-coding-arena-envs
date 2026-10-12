"""Checks as data for coding tasks: the schema, weights and shipped documents.

The parent pipeline graded documents with an expression language evaluated
over the fixture. Coding tasks are graded by EXECUTING hidden tests, so the
declarative layer here is thinner: a check names a runner (which hidden tests
to run, or which structural guard to apply), a weight, a band and a step. The
shipped `tests/grade.py` executes exactly this document; nothing in a bundle's
tests/ is hand-written grader logic beyond the test files themselves.

What stays from the parent, unchanged in meaning: two kinds (objective /
guardrail), three bands, `requires` gating with `gated_out` semantics, weights
normalised so every task sums to `policy.WEIGHT_TOTAL`, equal step budgets,
and a documented `reward.toml` whose shares the gates recompute.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

from ..policy import MIN_OBJECTIVES, WEIGHT_TOTAL

SCHEMA = "arena-coding/checks/1"
KINDS = ("objective", "guardrail")
BANDS = ("floor", "core", "stretch")
STRUCTURAL = ("hash-guard", "size-guard", "tree-guard", "shadow-guard")
COMMAND_RUNNERS = ("command", "pytest", "unittest", "node-test", "jest", "vitest", "mocha",
                   "playwright", "gradle", "maven", "cargo", "go", "go-test")
RUNNER_TYPES = STRUCTURAL + COMMAND_RUNNERS
ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
#: Keys a spec check may carry that the shipped grader never reads.
AUTHORING_ONLY_CHECK_KEYS = ("rule_quote", "weight_basis", "trap", "dimension", "note", "mutants")
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 5, 1800


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def runner_problems(runner, toolchain_runners: list[str], where: str) -> list[str]:
    if not isinstance(runner, dict) or not isinstance(runner.get("type"), str):
        return [f"{where}: runner must be an object with a type"]
    kind = runner["type"]
    if kind not in RUNNER_TYPES:
        return [f"{where}: unknown runner type {kind!r}"]
    issues = []
    if kind == "go":
        issues.append(f"{where}: legacy go stdout reports are not trustworthy; use go-test with private suite selections")
    if kind in COMMAND_RUNNERS and toolchain_runners and kind not in toolchain_runners:
        issues.append(f"{where}: runner {kind!r} is not available in this toolchain "
                      f"(available: {', '.join(toolchain_runners)})")
    timeout = runner.get("timeout_s", 120)
    if kind in COMMAND_RUNNERS and (not isinstance(timeout, (int, float))
                                    or not MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S):
        issues.append(f"{where}: timeout_s must be between {MIN_TIMEOUT_S} and {MAX_TIMEOUT_S}")
    if kind == "command" and not (isinstance(runner.get("argv"), list) and runner["argv"]
                                  or isinstance(runner.get("shell"), str) and runner["shell"].strip()):
        issues.append(f"{where}: a command runner needs argv or shell")
    if kind in ("pytest", "node-test", "jest", "vitest", "mocha", "playwright", "go-test"):
        select = runner.get("select")
        if not isinstance(select, list) or not select or not all(isinstance(s, str) and s for s in select):
            issues.append(f"{where}: {kind} needs a non-empty select list of suite paths")
        else:
            for item in select:
                path = item.split("::", 1)[0]
                if path.startswith("/") or ".." in path.split("/"):
                    issues.append(f"{where}: suite path {item!r} must be relative to tests/suites")
                if kind == "go-test" and (not path.endswith("_test.go") or ("::" in item and not re.fullmatch(r"Test\w+", item.split("::", 1)[1]))):
                    issues.append(f"{where}: go-test selections must be file_test.go or file_test.go::TestName")
        if isinstance(select, list) and len(set(str(s) for s in select)) != len(select):
            issues.append(f"{where}: suite selections must be unique")
        if kind == "go-test":
            package = runner.get("package")
            if package is not None and (not isinstance(package, str) or not package or package.startswith("/") or ".." in package.split("/")):
                issues.append(f"{where}: go-test package must be relative to the module cwd")
            overlay = runner.get("overlay")
            if overlay is not None:
                selected_files = {str(item).split("::", 1)[0] for item in (select if isinstance(select, list) else [])}
                if package is None or not isinstance(overlay, dict) or any(not isinstance(key, str) or key not in selected_files
                    or not isinstance(value, str) or "/" in value or value in (".", "..") or not value.endswith("_test.go")
                    for key, value in (overlay.items() if isinstance(overlay, dict) else [])):
                    issues.append(f"{where}: go-test overlay needs a package and maps selected private suites to _test.go basenames")
                elif len(set(overlay.values())) != len(overlay):
                    issues.append(f"{where}: go-test overlay targets must be unique")
    if kind == "unittest":
        select = runner.get("select")
        if not isinstance(select, list) or not select:
            issues.append(f"{where}: unittest needs a select list of dotted test names")
    if kind == "hash-guard":
        paths = runner.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
            issues.append(f"{where}: hash-guard needs a non-empty paths list")
        if runner.get("mode", "unchanged") not in ("unchanged", "absent", "present"):
            issues.append(f"{where}: hash-guard mode must be unchanged, absent or present")
    if kind == "size-guard":
        if not isinstance(runner.get("path"), str) or not runner["path"]:
            issues.append(f"{where}: size-guard needs a path")
        if "min_bytes" not in runner and "max_bytes" not in runner:
            issues.append(f"{where}: size-guard needs min_bytes ('fixture' or an integer) or max_bytes")
    if kind == "tree-guard":
        allow = runner.get("allow", [])
        if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
            issues.append(f"{where}: tree-guard allow must be a list of glob patterns")
    if kind == "shadow-guard":
        modules = runner.get("modules", [])
        if not isinstance(modules, list) or not all(isinstance(m, str) and m for m in modules):
            issues.append(f"{where}: shadow-guard modules must be a list of import names or paths")
    for key in ("cwd",):
        value = runner.get(key)
        if value is not None and (not isinstance(value, str) or value.startswith("/") or ".." in value.split("/")):
            issues.append(f"{where}: {key} must be a relative path inside the working tree")
    if "env" in runner and not (isinstance(runner["env"], dict)
                                and all(isinstance(k, str) and isinstance(v, str) for k, v in runner["env"].items())):
        issues.append(f"{where}: env must map strings to strings")
    return issues


def check_problems(checks, steps: list[str], toolchain_runners: list[str] | None = None) -> list[str]:
    """Structural problems with a spec's checks. Empty when they are well-formed."""
    if not isinstance(checks, list) or not checks:
        return ["checks must be a non-empty list"]
    issues, ids = [], []
    for i, check in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(check, dict):
            issues.append(f"{where}: must be an object")
            continue
        cid = check.get("id")
        where = f"check {cid!r}" if isinstance(cid, str) else where
        if not isinstance(cid, str) or not ID_RE.match(cid):
            issues.append(f"{where}: id must match {ID_RE.pattern}")
        elif cid in ids:
            issues.append(f"{where}: duplicate id")
        else:
            ids.append(cid)
        kind = check.get("kind")
        if kind not in KINDS:
            issues.append(f"{where}: kind must be objective or guardrail")
        weight = check.get("weight")
        if kind == "objective" and (not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0):
            issues.append(f"{where}: an objective needs a positive weight")
        if kind == "guardrail" and weight not in (0, None):
            issues.append(f"{where}: a guardrail carries weight 0 (a violation zeroes the step)")
        if check.get("band") not in BANDS:
            issues.append(f"{where}: band must be floor, core or stretch")
        if check.get("step") not in steps:
            issues.append(f"{where}: step must name a declared step ({', '.join(steps)})")
        basis = check.get("basis")
        if not isinstance(basis, str) or len(basis.strip()) < 20:
            issues.append(f"{where}: basis (the public wording this check enforces) must be at least 20 characters")
        requires = check.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            issues.append(f"{where}: requires must be a list of check ids")
        elif requires and kind == "guardrail":
            issues.append(f"{where}: a guardrail cannot have prerequisites; it is never gated")
        issues.extend(runner_problems(check.get("runner"), toolchain_runners or [], where))
    by_id = {c.get("id"): c for c in checks if isinstance(c, dict)}
    for check in checks:
        if not isinstance(check, dict):
            continue
        for req in check.get("requires", []) or []:
            if req not in by_id:
                issues.append(f"check {check.get('id')!r}: requires undeclared check {req!r}")
            elif by_id[req].get("kind") != "objective":
                issues.append(f"check {check.get('id')!r}: may only require an objective")
            elif by_id[req].get("step") != check.get("step"):
                issues.append(f"check {check.get('id')!r}: may only require a check of the same step")
    if not issues:
        try:
            _topological(checks)
        except ValueError as exc:
            issues.append(str(exc))
    objectives = [c for c in checks if isinstance(c, dict) and c.get("kind") == "objective"]
    if len(objectives) < MIN_OBJECTIVES:
        issues.append(f"{len(objectives)} objectives; at least {MIN_OBJECTIVES} are required")
    if not any(isinstance(c, dict) and c.get("kind") == "guardrail" for c in checks):
        issues.append("at least one guardrail is required (a negative obligation the "
                      "instruction states: a protected file, a preserved behaviour)")
    for step in steps:
        if not any(c.get("kind") == "objective" and c.get("step") == step for c in objectives):
            issues.append(f"step {step!r} has no objective")
        # Reviewer finding (real task, 2026-09-27): a hidden suite imported a bare
        # module name with the repository root first on sys.path, so a planted
        # /app/repo/<name>.py would have been graded instead of the real one. A
        # tree-guard (with the allowances the task genuinely needs) makes every
        # planted file a violation, whatever the suite imports.
        # A shadow-guard (2026-09-28) does the same with no file budget: the
        # reviewer read every tree-guard allowance as an unpublished
        # requirement, and a fix that adds a helper module is legitimate work.
        if not any(isinstance(c, dict) and c.get("kind") == "guardrail" and c.get("step") == step
                   and isinstance(c.get("runner"), dict) and c["runner"].get("type") in ("tree-guard", "shadow-guard")
                   for c in checks):
            issues.append(f"step {step!r} has no shadow-guard or tree-guard guardrail: a planted file could shadow "
                          "the module under test; add a shadow-guard naming the modules the hidden tests import")
    return issues


def _topological(checks: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for c in checks}
    done, out, remaining = set(), [], list(checks)
    while remaining:
        progressed = False
        for check in list(remaining):
            if all(r in done for r in check.get("requires", []) or []):
                out.append(check)
                done.add(check["id"])
                remaining.remove(check)
                progressed = True
        if not progressed:
            raise ValueError("requires cycle among: " + ", ".join(c["id"] for c in remaining))
    return out


# ---------------------------------------------------------------------------
# Weights (ported from the parent, unchanged in arithmetic)
# ---------------------------------------------------------------------------

def step_budgets(steps: list[str], total: int = WEIGHT_TOTAL) -> dict[str, int]:
    if not steps:
        return {}
    base, extra = divmod(total, len(steps))
    return {name: base + (1 if i < extra else 0) for i, name in enumerate(steps)}


def normalise_weights(checks: list[dict], total: int = WEIGHT_TOTAL,
                      steps: list[str] | None = None) -> list[dict]:
    objectives = [c for c in checks if c.get("kind") == "objective"]
    if not objectives:
        return checks
    names = list(steps or [])
    for c in objectives:
        if c.get("step", "") not in names:
            names.append(c.get("step", ""))
    groups = {name: [c for c in objectives if c.get("step", "") == name] for name in names}
    groups = {name: members for name, members in groups.items() if members}
    budgets = step_budgets(list(groups), total)
    for name, members in groups.items():
        _normalise_group(members, budgets[name])
    for c in checks:
        if c.get("kind") == "guardrail":
            c["weight"] = 0
    return checks


def _normalise_group(objectives: list[dict], total: int) -> None:
    current = sum(c.get("weight", 0) for c in objectives)
    if current <= 0:
        return
    scaled = [(c, c.get("weight", 0) * total / current) for c in objectives]
    floors = [(c, int(v), v - int(v)) for c, v in scaled]
    assigned = sum(max(f, 1) for _, f, _ in floors)
    for c, f, _ in floors:
        c["weight"] = max(f, 1)
    remainder = total - assigned
    for c, _, frac in sorted(floors, key=lambda t: -t[2]):
        if remainder <= 0:
            break
        c["weight"] += 1
        remainder -= 1
    for c in sorted(objectives, key=lambda c: -c["weight"]):
        if remainder >= 0:
            break
        if c["weight"] > 1:
            c["weight"] -= 1
            remainder += 1


def effective_shares(checks: list[dict], rubric: dict[str, list[str]],
                     strategy: str = "mean") -> dict[str, float]:
    if strategy != "mean":
        raise ValueError(f"unsupported multi_step_reward_strategy {strategy!r}")
    by_id = {c.get("id"): c for c in checks}
    shares: dict[str, float] = {}
    steps = list(rubric)
    for name in steps:
        owned = [by_id[cid] for cid in rubric[name]
                 if cid in by_id and by_id[cid].get("kind") == "objective"]
        total = sum(c.get("weight", 0) for c in owned)
        for c in owned:
            share = (c.get("weight", 0) / total / len(steps)) if total else 0.0
            shares[c["id"]] = shares.get(c["id"], 0.0) + share
    return shares


def weight_problems(checks: list[dict], steps: list[str]) -> list[str]:
    """The built document must sum to WEIGHT_TOTAL with equal step budgets."""
    issues = []
    objectives = [c for c in checks if c.get("kind") == "objective"]
    total = sum(int(c.get("weight", 0)) for c in objectives)
    if total != WEIGHT_TOTAL:
        issues.append(f"objective weights sum to {total}, not {WEIGHT_TOTAL}")
    budgets = step_budgets([s for s in steps if any(c.get("step") == s for c in objectives)])
    for step, budget in budgets.items():
        got = sum(int(c.get("weight", 0)) for c in objectives if c.get("step") == step)
        if got != budget:
            issues.append(f"step {step} carries {got} points, expected {budget}")
    for c in checks:
        if c.get("kind") == "guardrail" and c.get("weight", 0) != 0:
            issues.append(f"guardrail {c.get('id')} carries weight {c.get('weight')}")
    return issues


# ---------------------------------------------------------------------------
# Shipped documents
# ---------------------------------------------------------------------------

def runtime_check(check: dict) -> dict:
    kept = {k: v for k, v in check.items() if k not in AUTHORING_ONLY_CHECK_KEYS}
    kept.setdefault("requires", [])
    return kept


def rubric(checks: list[dict], steps: list[str]) -> dict[str, list[str]]:
    return {step: [c["id"] for c in checks if c.get("step") == step] for step in steps}


def shipped_document(checks: list[dict], toolchain_view: dict, deliverables: list[dict]) -> dict:
    return {"schema": SCHEMA, "toolchain": toolchain_view,
            "deliverables": [deepcopy(d) for d in deliverables or []],
            "checks": [runtime_check(c) for c in checks]}


def basis_toml(checks: list[dict]) -> str:
    """Public wording per check, plus the instruction sentences a check rests on."""
    out = ["# The public basis of every check: what the instruction says, verbatim.",
           "# A hidden test enforces a stated requirement; this file is where a reviewer",
           "# finds that requirement without reading test code.", ""]
    for c in checks:
        out.append(f"[{c['id']}]")
        out.append(f"kind = {json.dumps(c['kind'])}")
        out.append(f"step = {json.dumps(c.get('step', ''))}")
        out.append(f"band = {json.dumps(c.get('band', ''))}")
        out.append(f"basis = {json.dumps(c.get('basis', ''), ensure_ascii=False)}")
        out.append(f"runner = {json.dumps(c['runner'].get('type', ''))}")
        quotes = c.get("rule_quote") or []
        if isinstance(quotes, str):
            quotes = [quotes]
        out.append("rule_quote = [" + ", ".join(json.dumps(q, ensure_ascii=False) for q in quotes) + "]")
        if c.get("trap"):
            out.append(f"trap = {json.dumps(c['trap'])}")
        out.append("")
    return "\n".join(out)


def reward_toml(checks: list[dict], steps: list[str], rubric_doc: dict[str, list[str]]) -> str:
    shares = effective_shares(checks, rubric_doc)
    by_id = {c["id"]: c for c in checks}
    out = ["# Scoring, documented exactly as the grader computes it.",
           "# A step's reward is earned points / that step's points, or 0 when a guardrail",
           "# is violated; the task reward is the mean of the step rewards",
           '# (task.toml multi_step_reward_strategy = "mean").',
           "# A check's task_share is points / step points / number of steps.",
           "# The weight-budget gate recomputes every share below from checks.json.", ""]
    for name in steps:
        owned = [by_id[cid] for cid in rubric_doc.get(name, []) if by_id.get(cid, {}).get("kind") == "objective"]
        bands: dict[str, int] = {}
        for c in owned:
            bands[c.get("band", "core")] = bands.get(c.get("band", "core"), 0) + int(c["weight"])
        points = sum(int(c["weight"]) for c in owned)
        guards = [cid for cid in rubric_doc.get(name, []) if by_id.get(cid, {}).get("kind") == "guardrail"]
        out += ["[[reward]]", f"name = {json.dumps(name)}", f"step = {json.dumps(name)}",
                'aggregation = "weighted_mean"', f"points = {points}",
                f"task_share = {round(1 / len(steps), 6) if steps else 0}",
                f"bands = {{ {', '.join(f'{k} = {v}' for k, v in sorted(bands.items()))} }}",
                f"guardrails = {json.dumps(guards)}", ""]
    out.append("[shares]")
    for cid in sorted(shares):
        c = by_id[cid]
        out.append(f"{json.dumps(cid)} = {{ step = {json.dumps(c.get('step', ''))}, "
                   f"points = {int(c['weight'])}, task_share = {round(shares[cid], 6)} }}")
    return "\n".join(out) + "\n"


def load_shipped(path) -> dict:
    """Read a bundle's tests/checks.json strictly."""
    from .contracts import strict_json
    doc = strict_json(open(path, encoding="utf-8").read())
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} document")
    if not isinstance(doc.get("checks"), list):
        raise ValueError(f"{path}: checks must be a list")
    return doc
