"""Executable counterexamples: findings the shipped grader can be made to show (2026-10-27).

The reviewer reads; it never runs code. On the first bash RLE build each of six review
rounds named one defect in prose ("a correct build that uses page_break_before is
rejected", "touching the old work directories forges the cleanup check"), and each
hand patch was judged only by the next review. A probe turns such a finding into a
run of the shipped grader:

    {"id": "S1-page-break-before", "finding": "S1",
     "kind": "false_negative" | "false_positive" | "forgery",
     "step": "01-build-script",                       # optional for a one-step task
     "base": "reference" | "fixture" | "alternative" | "mutant:<id>",
     "edits": [{"file": "make_doc.py", "old": "exact text", "new": "replacement"},
               {"file": "notes.txt", "content": "whole new file"}],
     "expect_fails": ["o_killed_complete"]}           # optional, wrong implementations only

The implementation is the step's base (the reference patch, the alternative, a mutant
or nothing) plus the edits, each of which must match exactly once in the file as the
base leaves it. A false_negative (a correct implementation) HOLDS when the step scores
1.0; a false_positive or forgery (a wrong implementation) holds when the step scores
below 1.0 and fails every check in expect_fails. A probe that does not hold on the
current version reproduces its finding; a patch is accurate only when every probe of
the findings it fixes holds afterwards, and every probe of earlier rounds still holds.

Earlier steps run their reference, as in the controls; later steps get nothing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import sandbox
from .contracts import contained

SCHEMA = "arena-coding/counterexample/1"
KINDS = ("false_negative", "false_positive", "forgery")
APPLIED = "ARENA_COUNTEREXAMPLE_APPLIED"


class ProbeError(ValueError):
    """A probe that cannot be built: malformed, an unknown base, or an edit that does not match."""


def normalize(probe: dict, steps: list[str]) -> dict:
    """A checked copy of `probe`; raises ProbeError with the reason."""
    if not isinstance(probe, dict):
        raise ProbeError("a probe is an object")
    kind = probe.get("kind")
    if kind not in KINDS:
        raise ProbeError(f"kind must be one of {KINDS}, not {kind!r}")
    step = probe.get("step") or (steps[0] if len(steps) == 1 else None)
    if step not in steps:
        raise ProbeError(f"step must be one of {steps}")
    base = str(probe.get("base") or "reference")
    if base not in ("reference", "fixture", "alternative") and not base.startswith("mutant:"):
        raise ProbeError(f"base must be reference, fixture, alternative or mutant:<id>, not {base!r}")
    edits = probe.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ProbeError("a probe needs at least one edit")
    clean = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("file"), str) or not edit["file"].strip():
            raise ProbeError("every edit names a repository-relative file")
        rel = edit["file"].strip()
        while rel.startswith("./"):
            rel = rel[2:]
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ProbeError(f"edit file {edit['file']!r} must stay inside the repository")
        if "content" in edit:
            if not isinstance(edit["content"], str):
                raise ProbeError(f"{rel}: content must be text")
            clean.append({"file": rel, "content": edit["content"]})
        elif isinstance(edit.get("old"), str) and edit["old"] and isinstance(edit.get("new"), str):
            clean.append({"file": rel, "old": edit["old"], "new": edit["new"]})
        else:
            raise ProbeError(f"{rel}: an edit is {{old, new}} (old not empty) or {{content}}")
    fails = probe.get("expect_fails") or []
    if not isinstance(fails, list) or not all(isinstance(c, str) for c in fails):
        raise ProbeError("expect_fails is a list of check ids")
    if kind == "false_negative" and fails:
        raise ProbeError("a false_negative is a correct implementation: it expects no failing check")
    pid = str(probe.get("id") or "").strip() or ("cx-" + hashlib.sha256(
        json.dumps([kind, step, base, clean], sort_keys=True).encode()).hexdigest()[:10])
    return {"schema": SCHEMA, "id": pid, "finding": str(probe.get("finding") or ""), "kind": kind, "step": step,
            "base": base, "edits": clean, "expect_fails": fails, "why": str(probe.get("why") or "")[:600]}


def _base_patch(dest: Path, step: str, base: str) -> str:
    if base == "fixture":
        return ""
    if base == "reference":
        rel = f"steps/{step}/solution/fix.patch"
    elif base == "alternative":
        rel = f"authoring/alt/{step}.patch"
    else:
        rel = f"authoring/mutants/{base.split(':', 1)[1]}.patch"
    path = contained(dest, rel)
    if not path.is_file():
        raise ProbeError(f"base {base!r} has no patch at {rel}")
    return path.read_text(encoding="utf-8")


def _reference_commands(dest: Path, step: str) -> str:
    """The reference's commands beyond its patch (as `sandbox.solve_variant` reads solve.sh)."""
    solve = Path(dest) / "steps" / step / "solution/solve.sh"
    if not solve.is_file():
        return ""
    lines = [line for line in solve.read_text().splitlines()
             if line and not line.startswith("#") and not line.startswith("set ")
             and line not in ("cd /app/repo",) and "git apply" not in line and "patch -p1" not in line
             and line.strip() not in ("fi", ":") and not line.startswith("if ")]
    return "\n".join(lines).strip()


def _apply(tree: Path, patch: str, label: str) -> None:
    if not patch.strip():
        return
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as fh:
        fh.write(patch if patch.endswith("\n") else patch + "\n")
        name = fh.name
    try:
        done = subprocess.run(["git", "apply", "--whitespace=nowarn", name], cwd=tree, capture_output=True, timeout=120)
        if done.returncode:
            done = subprocess.run(["patch", "-p1", "--forward", "-s", "-i", name], cwd=tree,
                                  capture_output=True, timeout=120)
        if done.returncode:
            raise ProbeError(f"{label} does not apply: {done.stderr.decode(errors='replace')[-600:]}")
    finally:
        Path(name).unlink(missing_ok=True)


def edited_files(dest: Path, probe: dict, steps: list[str]) -> dict[str, bytes]:
    """The final bytes of every file the probe edits, computed on the host: the fixture,
    the earlier steps' references, the step's base, then the edits (each exactly once)."""
    dest = Path(dest)
    with tempfile.TemporaryDirectory(prefix="arena-cx-") as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(dest / "environment/repo", tree, symlinks=True)
        for earlier in steps[:steps.index(probe["step"])]:
            _apply(tree, _base_patch(dest, earlier, "reference"), f"the reference of {earlier}")
        _apply(tree, _base_patch(dest, probe["step"], probe["base"]), f"base {probe['base']}")
        out: dict[str, bytes] = {}
        for edit in probe["edits"]:
            path = contained(tree, edit["file"])
            if "content" in edit:
                data = edit["content"]
            else:
                if edit["file"] in out:
                    text = out[edit["file"]].decode("utf-8")
                elif path.is_file():
                    text = path.read_text(encoding="utf-8")
                else:
                    raise ProbeError(f"{edit['file']}: no such file after base {probe['base']}")
                count = text.count(edit["old"])
                if count != 1:
                    raise ProbeError(f"{edit['file']}: `old` occurs {count} times after base {probe['base']}; "
                                     "it must occur exactly once")
                data = text.replace(edit["old"], edit["new"], 1)
            out[edit["file"]] = data.encode("utf-8")
        return out


def variant(dest: Path, probe: dict, steps: list[str], *, root: str = "/app/repo") -> sandbox.Variant:
    """The probe as one shell variant: apply the base patch, write the edited files, then the
    reference's own commands when the base is the reference, and print a marker last."""
    files = edited_files(dest, probe, steps)
    patch = _base_patch(dest, probe["step"], probe["base"])
    parts = ["set -e", f"cd {shlex.quote(root)}"]
    if patch.strip():
        parts.append("cat > /tmp/arena-cx.patch <<'ARENA_CX_PATCH_EOF'\n" + patch.rstrip("\n") + "\nARENA_CX_PATCH_EOF")
        parts.append("git apply --whitespace=nowarn /tmp/arena-cx.patch 2>/dev/null || patch -p1 --forward -s < /tmp/arena-cx.patch")
    for rel, data in files.items():
        quoted = shlex.quote(rel)
        parts.append(f"mkdir -p \"$(dirname {quoted})\"")
        parts.append(f"base64 -d > {quoted} <<'ARENA_CX_B64_EOF'\n" + base64.b64encode(data).decode("ascii")
                     + "\nARENA_CX_B64_EOF")
    if probe["base"] == "reference":
        commands = _reference_commands(dest, probe["step"])
        if commands:
            parts.append(commands)
    parts.append(f"echo {APPLIED}")
    return sandbox.Variant("shell", shell="\n".join(parts), label=f"counterexample:{probe['id']}")


def verdict(probe: dict, entry: dict | None) -> dict:
    """Whether the probe holds on one graded step: a correct implementation scores 1.0, a
    wrong one or a forgery scores below it and fails every check it names."""
    entry = entry or {}
    applied = entry.get("applied") or {}
    if APPLIED not in str(applied.get("stdout") or "") or applied.get("exit_code"):
        return {"holds": None, "error": "the probe did not apply in the container: "
                + str(applied.get("stderr") or applied.get("stdout") or "")[-400:]}
    reward = entry.get("reward")
    if entry.get("valid") is not True or reward is None:
        return {"holds": None, "error": f"the grader produced no valid reward: {str(entry.get('error') or '')[:300]}"}
    checks = ((entry.get("grade") or {}).get("checks") or {})
    failing = sorted(cid for cid, r in checks.items() if not r.get("passed"))
    if probe["kind"] == "false_negative":
        holds = reward == 1.0
    else:
        holds = reward < 1.0 and set(probe["expect_fails"]) <= set(failing)
    return {"holds": holds, "reward": reward, "failing": failing}


def run(dest: Path, probes: list[dict], *, work: Path, workers: int | None = None) -> list[dict]:
    """Every probe through the shipped grader on the export `dest`; one record per probe.

    A probe that cannot be built is recorded with `holds: None` and its reason, never raised:
    the caller decides what an unbuildable probe means for it.
    """
    dest, work = Path(dest), Path(work)
    steps = sandbox._steps(dest)
    out, runnable = [], []
    for raw in probes:
        try:
            probe = normalize(raw, steps)
            runnable.append((probe, variant(dest, probe, steps)))
        except ProbeError as exc:
            out.append({"id": (raw or {}).get("id") if isinstance(raw, dict) else None, "holds": None,
                        "error": str(exc)[:600], "probe": raw})
    if not runnable:
        return out
    agent_tag = sandbox.build_image(dest, "agent")
    verifier_tag = sandbox.build_image(dest, "verifier")

    def one(item):
        probe, var = item
        variants = {s: sandbox.solve_variant(dest, s) for s in steps[:steps.index(probe["step"])]}
        variants[probe["step"]] = var
        try:
            record = sandbox.verify(dest, variants, work=work / probe["id"], agent_tag=agent_tag,
                                    verifier_tag=verifier_tag, keep_capture=False)
            result = verdict(probe, (record.get("steps") or {}).get(probe["step"]))
        except sandbox.SandboxError as exc:
            result = {"holds": None, "error": f"the sandbox could not run the probe: {exc}"[:600]}
        return {**result, "id": probe["id"], "finding": probe["finding"], "kind": probe["kind"],
                "step": probe["step"], "base": probe["base"], "probe": probe}

    if workers is None:
        from .controls import default_workers
        workers = default_workers()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(runnable))), thread_name_prefix="counterexample") as pool:
        out += list(pool.map(one, runnable))
    return out


def from_review(doc: dict) -> list[dict]:
    """The counterexamples a review deliverable carries, as raw probes tagged with their finding."""
    found = []

    def take(item, finding):
        cx = item.get("counterexample") if isinstance(item, dict) else None
        if isinstance(cx, dict):
            found.append({**cx, "finding": finding, "id": cx.get("id") or f"{finding}-cx"})

    for c in doc.get("checks") or []:
        if isinstance(c, dict) and c.get("verdict") != "pass":
            take(c, str(c.get("id")))
    for key in ("ambiguities", "substantive_gaps", "shortcut_risks"):
        for index, item in enumerate(doc.get(key) or [], 1):
            take(item, str((item.get("id") if isinstance(item, dict) else None) or f"{key}/{index}"))
    unique, seen = [], set()
    for probe in found:
        key = json.dumps({k: probe.get(k) for k in ("kind", "step", "base", "edits")}, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(probe)
    return unique
