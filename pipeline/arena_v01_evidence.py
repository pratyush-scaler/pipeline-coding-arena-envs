"""Arena delivery format v0.1: the evidence around a converted package's runs.

`arena_v01` converts the runtime surface; this module builds what Arena's checker reads around it from
runs made on exactly that surface (Harbor 0.24.0, workspace captures from `workspace_capture`):
per-step `changes.json` / `changes.patch` (§12.4), run records, `calibration/metrics.json` and
`runs.csv` (§12), the Oracle evidence and receipt (§11.1, §15), and `PACKAGE-MANIFEST.json` (§16).

Canonical change lists and patches come from the format checker's own `workspace_changes` module, so a
recomputation by the checker reproduces them byte for byte. Statistics follow §3.6 exactly: pass rule
R = 1 within 1e-9, the unbiased pass@k estimator, sample std with n - 1, null where undefined.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tarfile
from pathlib import Path

WORKSPACE_SCHEMA = "arena-delivery/workspace-changes/1"
RUN_SCHEMA = "arena-delivery/run/1"
METRICS_SCHEMA = "arena-delivery/metrics/1"
RECEIPT_SCHEMA = "arena-delivery/receipt/1"
MANIFEST_SCHEMA = "arena-delivery/package-manifest/1"
PHASES = ("before-agent", "after-agent-before-verifier")
K = 10
PASS_TOL = 1e-9


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    fixed = text.replace("\\n@", "\\n\\u0040")  # see ESCAPED_NEWLINE_AT; kept only if it parses identically
    path.write_text((fixed if fixed != text and json.loads(fixed) == json.loads(text) else text) + "\n")


# ---------------------------------------------------------------- §12.4 workspace changes

def inventory(tar_path: Path, pkg: Path, contents_rel: str) -> list[dict]:
    """The complete inventory of one capture; regular-file bytes stored once under contents_rel/<sha256>."""
    entries = []
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            name = m.name.lstrip("./").rstrip("/")
            if not name:
                continue
            path = "/" + name
            mode = f"{m.mode & 0o7777:04o}"
            if m.isdir():
                entries.append({"path": path, "kind": "directory", "mode": mode})
            elif m.issym():
                entries.append({"path": path, "kind": "symlink", "mode": mode, "target": m.linkname})
            elif m.isfile() or m.islnk():
                data = tf.extractfile(m).read()
                digest = sha256(data)
                blob = pkg / contents_rel / digest
                if not blob.is_file():
                    blob.parent.mkdir(parents=True, exist_ok=True)
                    blob.write_bytes(data)
                entries.append({"path": path, "kind": "file", "mode": mode, "bytes": len(data), "sha256": digest,
                                "content": f"{contents_rel}/{digest}"})
            else:
                raise ValueError(f"{tar_path}: {path} is a special file, which v0.1 cannot represent")
    entries.sort(key=lambda e: e["path"])
    return entries


def capture_meta(capture_dir: Path, phase: str) -> dict:
    meta = json.loads((capture_dir / f"{phase}.json").read_text())
    if not meta.get("ok"):
        raise ValueError(f"{capture_dir}: {phase} capture failed: {meta.get('error')}")
    return meta


def workspace_changes(pkg: Path, capture_dir: Path, *, model_id: str, run_id: str, step: str, tree_sha: str,
                      contents_rel: str) -> tuple[dict, str]:
    """changes.json + canonical changes.patch for one step of one run, from its two captures."""
    from arena_rle_formatcheck.workspace_changes import derive_changes, render_patch

    metas = [capture_meta(capture_dir, p) for p in PHASES]
    if metas[0]["scope"] != metas[1]["scope"]:
        raise ValueError(f"{capture_dir}: before/after scopes differ")
    doc = {"schema": WORKSPACE_SCHEMA, "model_id": model_id, "run_id": run_id, "step": step,
           "task_tree_sha256": tree_sha, "scope": metas[0]["scope"],
           "before": {"phase": PHASES[0], "captured_at": metas[0]["captured_at"],
                      "entries": inventory(capture_dir / f"{PHASES[0]}.tar", pkg, contents_rel)},
           "after": {"phase": PHASES[1], "captured_at": metas[1]["captured_at"],
                     "entries": inventory(capture_dir / f"{PHASES[1]}.tar", pkg, contents_rel)},
           "changes": []}
    doc["changes"] = derive_changes(doc)
    return doc, render_patch(doc, pkg)


# ---------------------------------------------------------------- §3.6 statistics

def passed(reward) -> bool:
    return reward is not None and abs(float(reward) - 1.0) <= PASS_TOL


def pass_at_k(n: int, c: int, k: int = K):
    if n < k:
        return None
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def statistics(rewards: list[float], k: int = K) -> dict:
    n = len(rewards)
    c = sum(1 for r in rewards if passed(r))
    mean = sum(rewards) / n if n else None
    std = math.sqrt(sum((r - mean) ** 2 for r in rewards) / (n - 1)) if n >= 2 else None
    return {"n": n, "passes": c, "pass_at_1": (c / n) if n else None, "pass_at_k": pass_at_k(n, c, k),
            "mean_reward": mean, "std_reward": std}


# ---------------------------------------------------------------- §16 package manifest

def tree_sha256(pkg: Path) -> str:
    from .arena_v01 import task_tree_sha256
    return task_tree_sha256(pkg)


def package_manifest(pkg: Path, *, env_id: str, version: str) -> dict:
    files = []
    for root, dirs, names in os.walk(pkg):
        dirs.sort()
        for name in sorted(names):
            p = Path(root) / name
            rel = p.relative_to(pkg).as_posix()
            if rel == "PACKAGE-MANIFEST.json":
                continue
            if p.is_symlink() or not p.is_file():
                raise ValueError(f"{rel}: symlinks and special files are not allowed in a package")
            data = p.read_bytes()
            files.append({"path": rel, "bytes": len(data), "sha256": sha256(data),
                          "executable": bool(os.stat(p).st_mode & 0o111)})
    files.sort(key=lambda f: f["path"].encode())
    return {"schema": MANIFEST_SCHEMA, "env_id": env_id, "task_version": version,
            "task_tree_sha256": tree_sha256(pkg), "file_count": len(files),
            "total_bytes": sum(f["bytes"] for f in files), "files": files}


# ---------------------------------------------------------------- runs (§12, §11.1)

RUNS_CSV_HEADER = ["model_id", "run_id", "step", "outcome", "exclusion_code", "step_reward", "step_pass", "run_reward",
                   "run_pass", "checks_passed", "checks_total", "failed_checks", "agent_steps", "input_tokens",
                   "output_tokens", "wall_seconds", "task_tree_sha256"]
LIMIT_ERRORS = ("AgentTimeoutError", "MaxTurns", "TokenLimit", "ContextLength")
INFRA_CODES = (("Auth", "infra.provider_auth"), ("RateLimit", "infra.provider_transport"),
               ("APIConnection", "infra.provider_transport"), ("Transport", "infra.provider_transport"),
               ("Verifier", "infra.verifier_error"), ("Build", "infra.build"), ("Docker", "infra.sandbox"),
               ("Environment", "infra.sandbox"))


def _exception_name(info) -> str:
    return (info or {}).get("exception_type") or (info or {}).get("type") or ""


def step_dir(trial: Path, step: str) -> Path:
    """Harbor 0.24 relocates per-step logs under steps/<step>/; a single-step trial may keep them at the root."""
    nested = trial / "steps" / step
    for d in (nested, trial):
        if (d / "verifier" / "reward.json").is_file():
            return d
    return nested


def agent_steps(trajectory: Path) -> int | None:
    """ATIF steps with source = "agent" (§3.6)."""
    if not trajectory.is_file():
        return None
    doc = json.loads(trajectory.read_text())
    return sum(1 for s in doc.get("steps", []) if s.get("source") == "agent")


def classify(result: dict, rewards: dict, steps: list[str], refusal: bool = False) -> tuple[str, str | None]:
    """complete / model_limit / model_refusal / excluded (+ code). Never excluded for a low or wrong grade."""
    graded = all(rewards.get(s) is not None for s in steps)
    names = [_exception_name(result.get("exception_info"))] + \
            [_exception_name(sr.get("exception_info")) for sr in result.get("step_results") or []]
    if graded:
        if refusal:
            return "model_refusal", None
        if any(any(k in n for k in LIMIT_ERRORS) for n in names if n):
            return "model_limit", None
        return "complete", None
    code = next((c for n in names if n for key, c in INFRA_CODES if key in n), "infra.other")
    return "excluded", code


def run_record(trial: Path, *, model_id: str, harness: dict, effort: str, tree_sha: str, steps: list[str],
               strategy: str, refusal: bool = False) -> tuple[dict, dict]:
    result = json.loads((trial / "result.json").read_text())
    rewards = {}
    for s in steps:
        f = step_dir(trial, s) / "verifier" / "reward.json"
        rewards[s] = float(json.loads(f.read_text())["reward"]) if f.is_file() else None
    outcome, code = classify(result, rewards, steps, refusal)
    if outcome == "excluded":
        reward = None
    elif strategy == "final":
        reward = rewards[steps[-1]]
    else:
        reward = sum(rewards[s] for s in steps) / len(steps)
    rec = {"schema": RUN_SCHEMA, "run_id": trial.name, "model_id": model_id, "harness": harness,
           "reasoning_effort": effort, "task_tree_sha256": tree_sha, "outcome": outcome,
           "reward": reward, "per_step_reward": {s: rewards[s] for s in steps},
           "started_at": result["started_at"], "finished_at": result["finished_at"]}
    if code:
        rec["exclusion_code"] = code
    return rec, result


def runs_csv_rows(trial: Path, run: dict, result: dict, steps: list[str], tree_sha: str) -> list[dict]:
    rows = []
    by_step = {sr.get("step_name"): sr for sr in result.get("step_results") or []}
    for s in steps:
        d = step_dir(trial, s)
        grade_file = d / "verifier" / "arena-grade.json"
        grade = json.loads(grade_file.read_text()) if grade_file.is_file() else {}
        checks = grade.get("checks", {})
        failed = [c for c, rec in checks.items() if not rec.get("passed")]
        if grade.get("contract_errors"):
            failed.append("deliverable_contract")
        sr = by_step.get(s) or {}
        ar = sr.get("agent_result") or {}
        ex = sr.get("agent_execution") or {}
        wall = None
        if ex.get("started_at") and ex.get("finished_at"):
            from datetime import datetime
            wall = (datetime.fromisoformat(ex["finished_at"].replace("Z", "+00:00"))
                    - datetime.fromisoformat(ex["started_at"].replace("Z", "+00:00"))).total_seconds()
        r = run["per_step_reward"].get(s)
        rows.append({"model_id": run["model_id"], "run_id": run["run_id"], "step": s, "outcome": run["outcome"],
                     "exclusion_code": run.get("exclusion_code", ""),
                     "step_reward": "" if r is None else repr(r), "step_pass": "" if r is None else int(passed(r)),
                     "run_reward": "" if run["reward"] is None else repr(run["reward"]),
                     "run_pass": "" if run["reward"] is None else int(passed(run["reward"])),
                     "checks_passed": len(checks) + (1 if grade else 0) - len(failed) if grade else "",
                     "checks_total": len(checks) + 1 if grade else "",
                     "failed_checks": ";".join(sorted(failed)),
                     "agent_steps": agent_steps(d / "agent" / "trajectory.json") or "",
                     "input_tokens": "" if ar.get("n_input_tokens") is None else ar["n_input_tokens"],
                     "output_tokens": "" if ar.get("n_output_tokens") is None else ar["n_output_tokens"],
                     "wall_seconds": "" if wall is None else round(wall, 3), "task_tree_sha256": tree_sha})
    return rows


def model_metrics(runs: list[dict], csv_rows: list[dict], *, model_id: str, provider_model: str, harness: dict,
                  effort: str, steps: list[str], requested: int, settings: dict | None = None) -> dict:
    finalized = [r for r in runs if r["outcome"] != "excluded"]
    outcomes = {k: sum(1 for r in runs if r["outcome"] == k) for k in ("complete", "model_limit", "model_refusal", "excluded")}
    no_refusal = [r for r in finalized if r["outcome"] != "model_refusal"]
    mine = [row for row in csv_rows if row["model_id"] == model_id and row["outcome"] != "excluded"]

    def mean_of(key):
        per_run = {}
        for row in mine:
            if row[key] not in ("", None):
                per_run[row["run_id"]] = per_run.get(row["run_id"], 0) + float(row[key])
        return (sum(per_run.values()) / len(per_run)) if per_run else None

    return {"model_id": model_id, "provider_model": provider_model, "harness": harness, "reasoning_effort": effort,
            "settings": settings or {}, "runs_requested": requested, "runs_recorded": len(runs),
            "outcomes": outcomes,
            "exclusions": [{"run_id": r["run_id"], "code": r["exclusion_code"],
                            "detail": "The attempt produced no grade because of an infrastructure failure; see its run.json."}
                           for r in runs if r["outcome"] == "excluded"],
            "task": statistics([r["reward"] for r in finalized]),
            "task_excluding_refusals": statistics([r["reward"] for r in no_refusal]) if outcomes["model_refusal"] else None,
            "per_step": {s: statistics([r["per_step_reward"][s] for r in finalized]) for s in steps},
            "mean_agent_steps": mean_of("agent_steps"), "mean_input_tokens": mean_of("input_tokens"),
            "mean_output_tokens": mean_of("output_tokens")}


# ---------------------------------------------------------------- copying run evidence

#: The leak scan's e-mail pattern reads the JSON escape for a newline followed by a decorator
#: (`\n@pytest.fixture`) as the address `n@pytest.fixture`. Only that sequence is re-escaped (`@` -> `@`);
#: the parsed JSON is identical, and an `@` preceded by a letter or digit -- a real address -- is untouched.
ESCAPED_NEWLINE_AT = ("\\n@", "\\n\\u0040")


def copy_json_evidence(source: Path, target: Path) -> None:
    raw = source.read_text(encoding="utf-8")
    fixed = raw.replace(*ESCAPED_NEWLINE_AT)
    if fixed != raw and json.loads(raw) != json.loads(fixed):
        raise ValueError(f"{source}: re-escaping changed the parsed content")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fixed, encoding="utf-8")


def copy_text_evidence(source: Path, target: Path) -> None:
    """Harness logs are JSON lines; each line that parses is re-escaped like a JSON file, others are copied."""
    out = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
        fixed = line.replace(*ESCAPED_NEWLINE_AT)
        if fixed != line:
            try:
                if json.loads(line) != json.loads(fixed):
                    fixed = line
            except ValueError:
                fixed = line
        out.append(fixed)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(out), encoding="utf-8")


def finalize_details(source: Path, target: Path, *, tree_sha: str, snapshot_rel: str, snapshot_sha: str) -> dict:
    """The harness's handoff (verifier contract §8): the runtime tree and the captured input this grade read.
    Raw scores, values, statuses and reasons are preserved exactly."""
    doc = json.loads(source.read_text())
    doc["task_tree_sha256"] = tree_sha
    doc["input_snapshot"] = {"path": snapshot_rel, "sha256": snapshot_sha}
    write_json(target, doc)
    return doc


def archive_step(pkg: Path, trial: Path, step: str, dest_rel: str, *, model_id: str, run_id: str, tree_sha: str,
                 contents_rel: str, harness_log: str | None, deliverables: list[str] | None = None) -> dict:
    """One graded step of one run into dest_rel/: changes.json, changes.patch, verifier files, agent logs."""
    dest = pkg / dest_rel
    capture = trial / "arena-capture" / step
    doc, patch = workspace_changes(pkg, capture, model_id=model_id, run_id=run_id, step=step, tree_sha=tree_sha,
                                   contents_rel=contents_rel)
    if deliverables:
        # §11.1: the Oracle's deliverables are stored at artifacts/<container path> and its after inventory
        # references those exact bytes (shared storage keeps everything else).
        for e in doc["after"]["entries"]:
            if e["kind"] == "file" and any(e["path"] == d or e["path"].startswith(d.rstrip("/") + "/")
                                           for d in deliverables):
                rel = f"{dest_rel}/artifacts/{e['path'].lstrip('/')}"
                target = pkg / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((pkg / e["content"]).read_bytes())
                e["content"] = rel
        from arena_rle_formatcheck.workspace_changes import render_patch
        patch = render_patch(doc, pkg)
    write_json(dest / "changes.json", doc)
    (dest / "changes.patch").write_text(patch, encoding="utf-8")
    snap_sha = sha256((dest / "changes.json").read_bytes())
    src = step_dir(trial, step)
    (dest / "verifier").mkdir(parents=True, exist_ok=True)
    (dest / "verifier/reward.json").write_bytes((src / "verifier/reward.json").read_bytes())
    for name in ("arena-grade.json", "test-stdout.txt"):
        if (src / "verifier" / name).is_file():
            (dest / "verifier" / name).write_bytes((src / "verifier" / name).read_bytes())
    details = finalize_details(src / "verifier/reward-details.json", dest / "verifier/reward-details.json",
                               tree_sha=tree_sha, snapshot_rel=f"{dest_rel}/changes.json", snapshot_sha=snap_sha)
    if (src / "agent/trajectory.json").is_file():
        copy_json_evidence(src / "agent/trajectory.json", dest / "agent/trajectory.json")
    if harness_log and (src / "agent" / harness_log).is_file():
        copy_text_evidence(src / "agent" / harness_log, dest / "agent" / harness_log)
    return {"changes": doc, "details": details}
