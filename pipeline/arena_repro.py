"""Reproduce delivered zips on Arena's own harness before they ship.

User, 2026-10-12: "Let's ensure that we have a reproducibility check before we ship a task. It should be
reproducible at Arena's end." Arena builds and grades on Harbor 0.20.0 pinned to commit cfc1bcb, x86_64, at
2 vCPU / 4096 MB, and accepts a task only when the oracle scores 1.0 and a NOP agent scores 0.0. This module runs
exactly that harness -- installed on a Coder workspace from the commit's own lockfile -- on each delivered zip, with
a forced image build, and writes a receipt bound to the zip's sha256. `receipt_problems` is the ship gate: a missing
receipt, a receipt for other bytes, or anything but oracle 1.0 / NOP 0.0 without an exception on the pinned harness
and resources refuses the zip.

usage: python -m pipeline.arena_repro <zip> [<zip> ...]            reproduce zips without a passing receipt
       python -m pipeline.arena_repro --force <zip> [...]          reproduce again even with a passing receipt
       python -m pipeline.arena_repro --check <zip> [...]          only check receipts (exit 1 on any problem)
"""
from __future__ import annotations

import hashlib
import json
import shlex
import sys
import time
from pathlib import Path

from . import policy

SCHEMA = "arena-coding/arena-repro/1"
#: The persistent home of the pinned harness on the workspace (a workspace stop resets / and /tmp).
REMOTE_ROOT = "/home/coder/h020"
REMOTE_HARBOR = f"{REMOTE_ROOT}/harbor"
REPO_URL = "https://github.com/harbor-framework/harbor.git"
#: Concurrent trials per agent on the workspace (oracle and NOP run side by side).
CONCURRENCY = 4


def receipt_path(zip_path: Path) -> Path:
    """Receipts sit beside the zips, in their own folder. A legacy zip's receipt is bound to the zip's bytes; an
    Arena-format zip's (pipeline/arena_format.py) also to its tree digest, so attaching the receipt inside the zip's
    verification/ -- outside the digest -- keeps it valid."""
    zip_path = Path(zip_path)
    return zip_path.parent / "arena-repro" / f"{zip_path.name}.json"


def evidence_path(zip_path: Path) -> Path:
    """The oracle and NOP trials pulled back from the workspace: <evidence>/<agent>/<trial>/."""
    zip_path = Path(zip_path)
    return zip_path.parent / "arena-repro" / "evidence" / zip_path.name


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def receipt_problems(zip_path: Path) -> list[str]:
    """Why `zip_path` may not ship on Arena's harness ([] when its receipt passes for exactly these bytes)."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return [f"{zip_path.name}: zip not found"]
    path = receipt_path(zip_path)
    if not path.is_file():
        return [f"{zip_path.name}: no Arena reproduction receipt (run python -m pipeline.arena_repro {zip_path})"]
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return [f"{zip_path.name}: unreadable Arena reproduction receipt ({exc})"]
    problems = []
    if doc.get("schema") != SCHEMA:
        problems.append(f"receipt schema {doc.get('schema')!r} is not {SCHEMA!r}")
    if doc.get("zip_sha256") != sha256(zip_path):
        from . import arena_format
        tree = arena_format.zip_tree_sha256(zip_path) if doc.get("tree_sha256") else None
        if not tree or tree != doc.get("tree_sha256"):
            problems.append("receipt is for other bytes: the zip changed after it was reproduced")
    harness = doc.get("harness") or {}
    if harness.get("version") != policy.ARENA_HARBOR_VERSION or harness.get("commit") != policy.ARENA_HARBOR_COMMIT:
        problems.append(f"reproduced on Harbor {harness.get('version')} @ {str(harness.get('commit'))[:12]}, "
                        f"not {policy.ARENA_HARBOR_VERSION} @ {policy.ARENA_HARBOR_COMMIT[:12]}")
    if doc.get("resources") != policy.ARENA_REPRO_RESOURCES:
        problems.append(f"reproduced at {doc.get('resources')}, not {policy.ARENA_REPRO_RESOURCES}")
    for agent, want in (("oracle", 1.0), ("nop", 0.0)):
        got = doc.get(agent) or {}
        if got.get("exception"):
            problems.append(f"{agent} raised {got['exception']}")
        if got.get("reward") != want:
            problems.append(f"{agent} scored {got.get('reward')}, not {want}")
    return [f"{zip_path.name}: {p}" for p in problems]


def gate(zips: list[Path]) -> list[str]:
    """The ship gate every publishing tool calls: every reason any of `zips` may not ship ([] = all may)."""
    found = []
    if policy.ARENA_REPRO_REQUIRED:
        found += [problem for z in zips for problem in receipt_problems(Path(z))]
    if policy.ARENA_FORMAT_REQUIRED:
        from . import arena_format
        found += [problem for z in zips for problem in arena_format.zip_problems(Path(z))]
    return found


# ---------------------------------------------------------------------------
# Running the harness on the workspace
# ---------------------------------------------------------------------------

def _ssh(command: str, *, timeout: float):
    from .benchmark import coder
    result = coder.ssh(policy.ARENA_REPRO_WORKSPACE, command, timeout=timeout, check=False)
    return result.returncode, coder._clean(result.stdout.decode())


def ensure_harness() -> dict:
    """Install the pinned harness in the workspace's persistent home if it is missing or not exactly the pin."""
    commit, version = policy.ARENA_HARBOR_COMMIT, policy.ARENA_HARBOR_VERSION
    # A workspace stop resets the root disk, Docker included (2026-10-12: arena-coding-3 came back without it).
    # Same install as coder.Instance.bootstrap: Ubuntu's own archive, Compose v2 and buildx, which Harbor drives.
    docker = ("set -e; if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1 "
              "|| ! docker buildx version >/dev/null 2>&1; then sudo apt-get update -qq && "
              "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 docker-buildx "
              ">/dev/null; fi; sudo systemctl enable --now docker >/dev/null 2>&1 || (pgrep -x dockerd >/dev/null || "
              "(sudo nohup dockerd >/tmp/dockerd.log 2>&1 &); sleep 5); sudo usermod -aG docker \"$(id -un)\"; "
              "sg docker -c 'docker info --format {{.ServerVersion}}'")
    code, out = _ssh(docker, timeout=1800)
    if code != 0:
        raise RuntimeError(f"no usable Docker on {policy.ARENA_REPRO_WORKSPACE}: {out[-400:]}")
    probe = (f"git -C {REMOTE_HARBOR} rev-parse HEAD 2>/dev/null; "
             f"{REMOTE_HARBOR}/.venv/bin/harbor --version 2>/dev/null | tail -1")
    _, out = _ssh(probe, timeout=300)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if lines[-2:] == [commit, version]:
        return {"version": version, "commit": commit, "installed": False}
    install = (f"set -e; mkdir -p {REMOTE_ROOT}; cd {REMOTE_ROOT}; "
               f"[ -d harbor/.git ] || git clone --quiet {REPO_URL} harbor; cd harbor; "
               f"git fetch --quiet origin {commit} || git fetch --quiet --all; git checkout --quiet {commit}; "
               f"export PATH=$HOME/.local/bin:$PATH; uv sync --frozen --python 3.12 >/dev/null; "
               f"git rev-parse HEAD; .venv/bin/harbor --version | tail -1")
    code, out = _ssh(install, timeout=2400)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if code != 0 or lines[-2:] != [commit, version]:
        raise RuntimeError(f"could not install Harbor {version} @ {commit[:12]} on {policy.ARENA_REPRO_WORKSPACE}: "
                           f"{out[-600:]}")
    return {"version": version, "commit": commit, "installed": True}


_SUMMARY = r'''
import json, glob, os, sys
work = sys.argv[1]
rows = {}
for agent in ("oracle", "nop"):
    for f in glob.glob(os.path.join(work, f"jobs-{agent}", "*", "*", "result.json")):
        try:
            d = json.load(open(f))
        except Exception as exc:
            continue
        name = (d.get("task_name") or "").split("/")[-1]
        vr = d.get("verifier_result") or {}
        reward = (vr.get("rewards") or {}).get("reward") if isinstance(vr, dict) else None
        exc = (d.get("exception_info") or {}).get("exception_type") if d.get("exception_info") else None
        rows.setdefault(name, {}).setdefault(agent, []).append({"reward": reward, "exception": exc,
                                                                  "trial": os.path.dirname(f)})
print("@@" + json.dumps(rows))
'''


def _task_names(zip_path: Path) -> set[str]:
    """The task folder name(s) inside a delivered zip: `<env-id>/task.toml` (Arena's format) or the legacy
    `<level-folder>/<task>/task.toml`."""
    import zipfile
    with zipfile.ZipFile(zip_path) as archive:
        return {Path(n).parent.name for n in archive.namelist() if n.endswith("/task.toml") and n.count("/") in (1, 2)}


def reproduce(zips: list[Path], *, timeout: float = 4 * 3600) -> dict[str, dict]:
    """Run oracle and NOP for every zip on the pinned harness; write and return one receipt per zip."""
    from .benchmark import coder_run
    zips = [Path(z).resolve() for z in zips]
    harness = ensure_harness()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = f"{REMOTE_ROOT}/runs/{stamp}"
    _ssh(f"find {REMOTE_ROOT}/runs -mindepth 1 -maxdepth 1 -mtime +3 -exec rm -rf {{}} + 2>/dev/null; "
         f"mkdir -p {work}/zips {work}/tasks", timeout=300)
    from . import arena_format
    import tempfile
    digests, names, uploaded = {}, {}, {}
    slim_dir = Path(tempfile.mkdtemp(prefix="arena-repro-slim-"))
    for z in zips:
        digests[z] = sha256(z)
        names[z] = _task_names(z)
        # An Arena-format zip goes up without its evidence layers (calibration/, verification/): they are outside
        # the tree digest the receipt binds, and they are most of the bytes.
        source = arena_format.slim_zip(z, slim_dir) if arena_format.zip_tree_sha256(z) else z
        uploaded[z] = sha256(source)
        coder_run.rsync(policy.ARENA_REPRO_WORKSPACE, str(source), f"{work}/zips/{z.name}", push=True, timeout=1800)
    py = f"{REMOTE_HARBOR}/.venv/bin/python"
    unpack = (f"cd {work} && for z in zips/*.zip; do n=$(basename \"$z\" .zip); "
              f"{py} -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \"$z\" x/$n; "
              f"for t in x/$n/*/task.toml x/$n/*/*/task.toml; do [ -f \"$t\" ] && mv \"$(dirname \"$t\")\" tasks/; "
              f"done; done; "
              f"sha256sum zips/*.zip")
    _, listing = _ssh(unpack, timeout=1800)
    remote_sha = {Path(line.split()[-1]).name: line.split()[0] for line in listing.splitlines()
                  if len(line.split()) == 2 and line.split()[-1].endswith(".zip")}
    cpus, memory = policy.ARENA_REPRO_RESOURCES["cpus"], policy.ARENA_REPRO_RESOURCES["memory_mb"]

    attempts = {"oracle": policy.ARENA_REPRO_ORACLE_ATTEMPTS, "nop": 1}

    def job(agent: str) -> str:
        return (f"cd {work} && {REMOTE_HARBOR}/.venv/bin/harbor run --path {work}/tasks --agent {agent} "
                f"--n-attempts {attempts[agent]} "
                f"--env docker --force-build --n-concurrent {CONCURRENCY} --jobs-dir {work}/jobs-{agent} "
                f"--override-cpus {cpus} --override-memory-mb {memory} > {work}/{agent}.log 2>&1 < /dev/null")

    _ssh(f"sg docker -c {shlex.quote(job('oracle') + ' & ' + job('nop') + ' & wait')}", timeout=timeout)
    _, out = _ssh(f"{py} - {work} <<'PYX'\n{_SUMMARY}PYX", timeout=600)
    line = next((l for l in out.splitlines() if l.startswith("@@")), "@@{}")
    rows = json.loads(line[2:])
    receipts = {}
    for z in zips:
        task_rows = [rows[n] for n in names[z] if n in rows]
        doc = {"schema": SCHEMA, "zip": z.name, "zip_sha256": digests[z], "tree_sha256": arena_format.zip_tree_sha256(z),
               "remote_zip_sha256": remote_sha.get(z.name), "uploaded_sha256": uploaded[z], "tasks": sorted(names[z]),
               "harness": {"name": "harbor", "version": harness["version"], "commit": harness["commit"],
                           "environment": "docker", "force_build": True},
               "resources": dict(policy.ARENA_REPRO_RESOURCES), "workspace": policy.ARENA_REPRO_WORKSPACE,
               "run_dir": work, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if remote_sha.get(z.name) != uploaded[z]:
            doc["transfer_problem"] = "the workspace copy's sha256 differs from the uploaded zip"
        for agent in ("oracle", "nop"):
            trials = [t for r in task_rows for t in r.get(agent, [])]
            if len(trials) != len(names[z]) * attempts[agent] or not trials:
                doc[agent] = {"reward": None, "exception": f"{len(trials)} trial(s) for {len(names[z])} task(s) "
                                                           f"x {attempts[agent]} attempt(s)"}
                continue
            rewards = {t["reward"] for t in trials}
            excs = [t["exception"] for t in trials if t["exception"]]
            doc[agent] = {"reward": rewards.pop() if len(rewards) == 1 else None,
                          "rewards": [t["reward"] for t in trials],
                          "exception": excs[0] if excs else None, "trials": [t["trial"] for t in trials]}
        if doc.get("transfer_problem"):
            doc["oracle"] = dict(doc.get("oracle") or {}, exception=doc["transfer_problem"])
        if doc["tree_sha256"]:
            # Arena's format carries its own proof: pull the trials back and attach receipt + evidence to the zip
            evidence = evidence_path(z)
            for agent in ("oracle", "nop"):
                for trial in (doc.get(agent) or {}).get("trials") or []:
                    target = evidence / agent / Path(trial).name
                    target.mkdir(parents=True, exist_ok=True)
                    try:
                        coder_run.rsync(policy.ARENA_REPRO_WORKSPACE, f"{trial}/", f"{target}/", push=False,
                                        timeout=1800, compress=True)
                    except Exception as exc:  # noqa: BLE001 -- the receipt still stands; the attach records the gap
                        doc.setdefault("evidence_problems", []).append(f"{agent} {Path(trial).name}: {exc}")
        path = receipt_path(z)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.replace(path)
        if doc["tree_sha256"] and not receipt_problems(z):
            arena_format.attach(z, doc, evidence_path(z))
            doc["attached_zip_sha256"] = sha256(z)
            tmp.write_text(json.dumps(doc, indent=1))
            tmp.replace(path)
        receipts[z.name] = doc
    return receipts


def main(argv: list[str]) -> int:
    check_only, force = "--check" in argv, "--force" in argv
    zips = [Path(a) for a in argv if not a.startswith("--")]
    if not zips:
        print(__doc__)
        return 2
    todo = [z for z in zips if force or receipt_problems(z)]
    if todo and not check_only:
        print(f"reproducing {len(todo)} zip(s) on Harbor {policy.ARENA_HARBOR_VERSION} @ "
              f"{policy.ARENA_HARBOR_COMMIT[:12]} ({policy.ARENA_REPRO_RESOURCES}) on {policy.ARENA_REPRO_WORKSPACE}")
        reproduce(todo)
    bad = 0
    for z in zips:
        problems = receipt_problems(z)
        doc = json.loads(receipt_path(z).read_text()) if receipt_path(z).is_file() else {}
        o, n = (doc.get("oracle") or {}).get("reward"), (doc.get("nop") or {}).get("reward")
        print(("OK   " if not problems else "FAIL ") + f"{z.name:52s} oracle={o} nop={n}")
        for p in problems:
            print(f"     - {p}")
        bad += bool(problems)
    print("ALL OK" if not bad else f"{bad} of {len(zips)} NOT reproducible on Arena's harness")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
