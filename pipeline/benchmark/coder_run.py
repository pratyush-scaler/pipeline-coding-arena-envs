"""Run the whole pipeline on Coder: one detached orchestrator per workspace.

User, 2026-10-03: "configure the pipeline to run everything concurrently on
Coder, the authoring, the repairs, the calibration whichever takes time and
needs to be run concurrently runs on Coder."

Native calibration already runs on Coder (`coder.run_campaign`). Authoring,
validity repair, controls, the semantic review and hardening ran on this
machine's Docker. This module moves ALL of it: a workspace receives this
checkout, the trace dump and the peer task trees at the SAME absolute paths,
and runs an unchanged `./arena ...` (or `category_batch.py ...`) command as a
detached process there. Calibration inside that process runs on the
workspace's own Docker (`ARENA_NATIVE_BACKEND=local`, set explicitly), which is
exactly what `coder_remote` runs for a pooled campaign. Several workspaces run
several shards at once; results come back to the same absolute paths.

    ./arena coder prepare <ws> [--create]   provision + sync code, dump, peers
    ./arena coder run <ws> [--create] [--env K=V ...] -- <arena args>
    ./arena coder shard --workspaces a,b,c --ranks 1-100 -- build -o envs-b3 ...
                                            disjoint --ranks per workspace, one tree
    ./arena coder push <ws>                 selection inputs + peers only (safe under a live run)
    ./arena coder runs [<ws>]               every run: alive / exit code / log tail
    ./arena coder pull <ws> <run>           fetch what the run wrote (envs*, artifacts)
    ./arena coder watch <ws> <run>          keep the workspace up, pull every interval
    ./arena coder kill <ws> <run>           stop a run's process group

Operator rules kept from `coder.py`:
  - a workspace is created only with an explicit --create; the pipeline never
    stops or deletes a workspace it is running on;
  - credentials come from THIS checkout's keys.env (never the inherited shell:
    an inherited ANTHROPIC_AUTH_TOKEN once reached calibration campaigns); on the
    workspace they live in RAM (/dev/shm, mode 600), never on its disk;
  - one code version per workspace while anything runs on it: a code sync is
    refused while a run is alive (lazy imports would mix module versions);
  - a stop wipes the workspace's root disk (memory: coder-autostop-wipes-runtime),
    so every synced path is bind-mounted from $HOME/arena-mirror when the
    template allows it, and `watch` extends the workspace's stop deadline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from . import coder as coder_mod

ROOT = coder_mod.ROOT
RUNS = "pipeline-artifacts/coder-runs"
#: What a remote orchestrator needs of this checkout beyond the campaign runtime.
CODE_PATHS = tuple(dict.fromkeys((*coder_mod.SYNC_PATHS, "category_batch.py", "workspace.example.toml",
                                  "DESIGN.md", "CATEGORY_BATCH.md")))
#: Peer task trees (novelty review, lineage, resumed bundles) and the selection
#: inputs a batch reads. `pipeline-artifacts/native` and other campaign trees stay
#: home: a remote run writes its own and `pull` brings them back.
PEER_GLOBS = ("envs*", "reference", "deliveries")
ARTIFACT_INCLUDES = ("*.json", "classify-cache/", "analysis/", "waves/")
#: Remote paths a pull may write into locally -- never code, keys or config.
PULL_ROOTS = ("envs", "pipeline-artifacts", "reference", "deliveries")
MIRROR = "$HOME/arena-mirror"
AUTHORING_PYTHON = "3.12"            # .python-version: the control fingerprints were taken on it
#: The remote runner reads this many native episodes at once by default; the
#: QC template is rated for about 20 concurrent trials on 32 vCPUs.
#: Control runs execute three trials at once (`controls.default_workers`).
#: Advisory pre-reviews ran on gpt-5.6 (2026-10-04, to leave gpt-6-astra's TPM to calibration). The
#: designer then tuned drafts against a different reviewer than the gate: in 21 of 23 cases a pre-review
#: pass was followed by a gate revise of the same version (2026-10-15). They now use the gate's own
#: reviewer (agent_designer.pre_review); set ARENA_PRE_REVIEW_MODEL again if Astra's TPM saturates.
DEFAULT_REMOTE_ENV = {"ARENA_NATIVE_BACKEND": "local", "ARENA_NATIVE_CONCURRENCY": "20",
                      "ARENA_LOCAL_WORKERS": "1", "ARENA_CONTROL_WORKERS": "3",
                      "ARENA_CODEX_SLOTS": "10", "ARENA_TRIAL_SLOTS": "24",
                      "PYTHONUNBUFFERED": "1"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def q(value) -> str:
    return shlex.quote(str(value))


#: Not forwarded to a workspace (user, 2026-10-03: "Drop the base URL on Coder").
#: keys.env's OPENAI_BASE_URL is an operator-network gateway that no workspace
#: can reach; Codex there voided every GPT-6 Astra episode. Without it, Codex
#: uses api.openai.com, which the same keys already serve for reviews.
REMOTE_DROPPED_KEYS = ("OPENAI_BASE_URL",)


def keys_values(path: Path | None = None) -> dict[str, str]:
    """keys.env as written -- the standing rule's only credential source --
    minus the settings a workspace must not inherit (REMOTE_DROPPED_KEYS)."""
    from ..openai_transport import read_key_file
    values = {k: v for k, v in read_key_file(path or ROOT / "keys.env").items()
              if v and k not in REMOTE_DROPPED_KEYS}
    if not (values.get("ANTHROPIC_AUTH_TOKEN") or values.get("ANTHROPIC_API_KEY")):
        raise ValueError("keys.env has no Anthropic credential; ./arena keys writes it")
    return values


def dump_path() -> Path:
    from ..config import dump_root
    return Path(dump_root()).resolve()


def remote_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(DEFAULT_REMOTE_ENV)
    env["ARENA_DUMP"] = str(dump_path())
    for key, value in (extra or {}).items():
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"bad environment name {key!r}")
        if key.endswith(coder_mod.FORWARD_SUFFIXES) or key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            raise ValueError(f"{key}: credentials come from keys.env only")
        env[key] = value
    return env


def run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------

def _rows() -> dict[str, dict]:
    proc = coder_mod.coder("list", "-o", "json", timeout=120)
    rows = json.loads(coder_mod._clean(proc.stdout.decode()) or "[]")
    return {row.get("name") or "": {"status": (row.get("latest_build") or {}).get("status")} for row in rows}


def ensure_running(ws: str, *, create: bool = False) -> dict:
    """Start (or, with --create, create) the named workspace; verify its specs."""
    cfg = coder_mod.settings()
    if cfg is None:
        raise ValueError("no [coder] section in workspace.toml (see workspace.example.toml)")
    coder_mod.ensure_login(cfg)
    row = _rows().get(ws)
    if row is None and not create:
        raise RuntimeError(f"workspace {ws} does not exist; pass --create to create it from template "
                           f"{cfg.template} (a new {cfg.min_cpus}-vCPU workspace is a cost the operator decides)")
    inst = coder_mod.Instance(ws, cfg, pinned=row is not None)
    if row is None:
        coder_mod.coder("create", ws, "-t", cfg.template, "--yes", "--stop-after", cfg.stop_after,
                        *(["--copy-parameters-from", cfg.copy_parameters_from] if cfg.copy_parameters_from else []),
                        *[a for k, v in sorted(cfg.parameters.items()) for a in ("--parameter", f"{k}={v}")],
                        timeout=1800)
    elif row.get("status") != "running":
        coder_mod.coder("start", ws, "--yes", timeout=1800)
    inst.verify_specs()
    return {"workspace": ws, "created": row is None, "was": (row or {}).get("status")}


def persist_script(paths: list[Path]) -> str:
    """Bind-mount each absolute path from $HOME/arena-mirror so a stop keeps it.

    A template whose home is not persistent, or a container that refuses mount,
    falls back to the root disk and says so: the caller then relies on `watch`
    keeping the workspace up and on frequent pulls.
    """
    lines = ["set -uo pipefail", "persistent=1"]
    for path in paths:
        lines += [
            f"P={q(path)}; M={MIRROR}{q(path)}",
            'mkdir -p "$M"',
            'sudo mkdir -p "$P"',
            'if ! mountpoint -q "$P"; then',
            # First use on a disk that already holds a runtime: keep it in the mirror.
            '  if [ -n "$(ls -A "$P" 2>/dev/null)" ] && [ -z "$(ls -A "$M" 2>/dev/null)" ]; then',
            '    sudo cp -a "$P"/. "$M"/ 2>/dev/null || true',
            '  fi',
            '  sudo mount --bind "$M" "$P" 2>/dev/null || persistent=0',
            'fi',
            'sudo chown "$(id -un):$(id -gn)" "$P" "$M" 2>/dev/null || true',
        ]
    lines.append('echo "persistent=$persistent"')
    return "\n".join(lines)


def bootstrap(ws: str) -> dict:
    """Docker + the campaign runtime (`coder.Instance.bootstrap`), rsync, and the
    authoring interpreter at this checkout's path, on persistent storage."""
    cfg = coder_mod.settings()
    out = coder_mod.ssh(ws, persist_script([ROOT, dump_path()]), timeout=900)
    persistent = "persistent=1" in out.stdout.decode(errors="replace")
    coder_mod.ssh(ws, "command -v rsync >/dev/null || (sudo apt-get update -qq && "
                      "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync >/dev/null)", timeout=900)
    if any_alive(ws):
        raise RuntimeError(f"a run is alive on {ws}; one code version per workspace -- wait for it or kill it")
    inst = coder_mod.Instance(ws, cfg, pinned=True)
    inst.bootstrap()                               # docker, buildx, .venv-harbor, code sync
    sync_code(ws)
    coder_mod.ssh(ws, "\n".join([
        "set -euo pipefail", 'export PATH="$HOME/.local/bin:$PATH"',
        f"uv python install {AUTHORING_PYTHON} >/dev/null",
        f"cd {q(ROOT)}",
        f"test -x .venv/bin/python || uv venv --python {AUTHORING_PYTHON} .venv",
        "uv pip install --quiet --python .venv/bin/python -r pyproject.toml",
        ".venv/bin/python -c 'import pypdf, sys; print(sys.version)'",
    ]), timeout=1800)
    return {"workspace": ws, "persistent": persistent}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _rsh_wrapper(directory: Path) -> Path:
    """rsync calls `<rsh> [-l user] host command...`; coder ssh wants `ws -- command`."""
    script = directory / "coder-rsh"
    script.write_text("#!/bin/sh\nif [ \"$1\" = \"-l\" ]; then shift 2; fi\nhost=\"$1\"; shift\n"
                      f"exec {q(coder_mod._binary())} ssh \"$host\" -- \"$@\"\n")
    script.chmod(0o700)
    return script


def rsync(ws: str, source: str, target: str, *, push: bool, delete: bool = False, update: bool = False,
          filters: list[str] | None = None, timeout: float | None = None, compress: bool = False) -> None:
    """Incremental mirror over `coder ssh`; absolute paths on both sides.

    `update` never overwrites a receiver file that is newer (a remote run's work). `compress` (-z) is for
    large, compressible pulls over the tunnel (2026-10-18: a 259 MB package directory whose zip is 57 MB
    took 12 minutes at about 0.4 MB/s and stalled the driver behind it).
    """
    with tempfile.TemporaryDirectory(prefix="arena-rsh-") as tmp:
        rsh = _rsh_wrapper(Path(tmp))
        args = ["rsync", "-a", "--partial", "-e", str(rsh)]
        if compress:
            args.append("-z")
        if delete:
            args.append("--delete")
        if update:
            args.append("--update")
        for rule in filters or []:
            args.append(f"--filter={rule}")
        if push:
            args += [source, f"{ws}:{target}"]
        else:
            args += [f"{ws}:{source}", target]
        env = dict(os.environ, COPYFILE_DISABLE="1")
        proc = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
        if proc.returncode not in (0, 24):          # 24: files vanished while sending
            raise RuntimeError(f"rsync {'to' if push else 'from'} {ws} failed ({proc.returncode}): "
                               + proc.stderr.decode(errors="replace")[-800:])


def sync_code(ws: str) -> None:
    """This checkout's code at its own path. Refused while a run is alive."""
    if any_alive(ws):
        raise RuntimeError(f"a run is alive on {ws}; syncing code under it would mix module versions")
    filters = ["- __pycache__/", "- *.pyc", "- keys.env*", "- workspace.toml"]
    for rel in CODE_PATHS:
        local = ROOT / rel
        if not local.exists():
            continue
        if local.is_dir():
            rsync(ws, f"{local}/", f"{local}/", push=True, delete=True, filters=filters, timeout=1800)
        else:
            rsync(ws, str(local), str(local), push=True, timeout=600)
    # The remote's own workspace.toml: the dump only. No [coder] section, so a
    # remote process can never reach Coder; calibration there is local.
    body = f'[workspace]\ndump = {json.dumps(str(dump_path()))}\n'
    coder_mod.ssh(ws, f"cat > {q(ROOT / 'workspace.toml')}", stdin=body.encode(), timeout=120)
    coder_mod.ssh(ws, f"echo {q(coder_mod._tree_sha(CODE_PATHS))} > {q(ROOT / '.arena-code-sha')}", timeout=120)


def sync_data(ws: str, *, dump: bool = True, peers: bool = True, artifacts: bool = True) -> dict:
    """The trace dump, peer task trees and selection inputs; incremental."""
    done = {}
    if dump:
        path = dump_path()
        rsync(ws, f"{path}/", f"{path}/", push=True, delete=False, timeout=None)
        done["dump"] = str(path)
    if peers:
        # Never push a tree a remote run is still writing (2026-10-03): the local
        # copy only accumulates (pulls never delete), so pushing it back
        # resurrected 112 files designers had deleted -- helper scripts that can
        # hold the reference fix among them -- and staled a finished review.
        # Only trees the workspace does not have yet. A tree that exists there is
        # its own (or an earlier copy): the local mirror never loses files, so
        # updating it would undo every deletion made on the workspace since.
        live = live_trees() | remote_trees(ws)
        roots = sorted({p for g in PEER_GLOBS for p in ROOT.glob(g) if p.is_dir() and p.name not in live})
        done["skipped_live_trees"] = sorted(live)
        # Never --delete and never overwrite newer: a shard's unpulled bundles
        # live only on the workspace.
        for local in roots:
            rsync(ws, f"{local}/", f"{local}/", push=True, update=True,
                  filters=["- __pycache__/", "- .build.lock", "- *.pyc"], timeout=None)
        done["peers"] = [p.name for p in roots]
    if artifacts:
        local = ROOT / "pipeline-artifacts"
        if local.is_dir():
            rules = ["+ /*.json", "+ /classify-cache/", "+ /classify-cache/**",
                     "+ /analysis/", "+ /analysis/**", "+ /waves/", "+ /waves/**", "- /*"]
            rsync(ws, f"{local}/", f"{local}/", push=True, update=True, filters=rules, timeout=None)
            done["artifacts"] = list(ARTIFACT_INCLUDES)
    return done


def prepare(ws: str, *, create: bool = False, data: bool = True) -> dict:
    state = ensure_running(ws, create=create)
    # Bring home what earlier runs here wrote before anything is pushed over it.
    state["pulled_before_sync"] = []
    for rid in open_launches(ws):
        row = next((r for r in runs(ws) if r["run"] == rid), None)
        if row is not None and not row["alive"]:
            pull(ws, rid)
            mark_finished(rid, {"exit_code": row["exit_code"], "pulled_by": "prepare"})
            state["pulled_before_sync"].append(rid)
    state.update(bootstrap(ws))
    if data:
        state["synced"] = sync_data(ws)
    return state


#: Immutable code snapshots beside the live checkout, one per code version.
STAGED = ".staged"

LINK_FARM = r'''
import os, sys
root, dest, code = sys.argv[1], sys.argv[2], set(sys.argv[3].split(","))
made = 0
for name in sorted(os.listdir(root)):
    if name in code or name in (".staged", "keys.env", ".arena-code-sha") or name.startswith(".git"):
        continue
    link = os.path.join(dest, name)
    if not os.path.lexists(link):
        os.symlink(os.path.join(root, name), link)
        made += 1
print(made)
'''


def stage_code(ws: str) -> Path:
    """This checkout's code as a snapshot at ROOT/.staged/<sha>, beside live runs.

    `sync_code` is refused while a run is alive (one code version per process),
    and on 2026-10-04 every workspace always had one: fixes for losses already
    measured waited hours for builds to drain. A staged launch runs from its own
    immutable copy instead. Everything that is not code -- task trees, the
    dump, the interpreter, workspace.toml -- is a symlink back to ROOT, and
    `ARENA_PIPELINE_ARTIFACTS` points campaigns and receipts at ROOT's own
    pipeline-artifacts, so results land where `pull` collects them. Bundle
    paths resolve through the links to ROOT. The snapshot hashes to the same
    policy identity the live checkout will have once the same code is synced.
    Pass task-tree paths as ABSOLUTE paths: a tree created after staging has no
    link in the snapshot.
    """
    sha = coder_mod._tree_sha(CODE_PATHS)
    dest = ROOT / STAGED / sha[:16]
    filters = ["- __pycache__/", "- *.pyc", "- keys.env*", "- workspace.toml"]
    coder_mod.ssh(ws, f"mkdir -p {q(dest)}", timeout=120)
    for rel in CODE_PATHS:
        local = ROOT / rel
        if not local.exists():
            continue
        if local.is_dir():
            rsync(ws, f"{local}/", f"{dest / rel}/", push=True, delete=True, filters=filters, timeout=1800)
        else:
            rsync(ws, str(local), str(dest / rel), push=True, timeout=600)
    names = ",".join(Path(rel).parts[0] for rel in CODE_PATHS)
    coder_mod.ssh(ws, f"python3 -c {q(LINK_FARM)} {q(ROOT)} {q(dest)} {q(names)}", timeout=120)
    coder_mod.ssh(ws, f"echo {q(sha)} > {q(dest / '.arena-code-sha')}", timeout=120)
    return dest


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

LAUNCHER = r'''
import json, os, shlex, subprocess, sys, time
from pathlib import Path
request = json.loads(sys.stdin.read())
root, run = Path(request["root"]), Path(request["run_dir"])
run.mkdir(parents=True, exist_ok=False)
import tempfile
keys_dir = Path(request.get("keys_dir") or ("/dev/shm" if Path("/dev/shm").is_dir() else tempfile.gettempdir()))
keys = keys_dir / "arena-keys.env"
fd = os.open(keys, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write("".join(f"{k}={v}\n" for k, v in request["keys"].items()))
link = root / "keys.env"
if link.is_symlink() or not link.exists():
    if link.is_symlink():
        link.unlink()
    link.symlink_to(keys)
(run / "cmd.json").write_text(json.dumps({"argv": request["argv"], "env": sorted(request["env"]),
                                          "started_at": time.time(), "code_sha": request["code_sha"]}))
(run / "started").write_text(str(time.time()))
env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM") if k in os.environ}
env.update(request["env"])
env["PATH"] = f"{os.environ.get('HOME', '')}/.local/bin:" + env.get("PATH", "/usr/bin:/bin")
env["PYTHONPATH"] = str(root)
log = open(run / "run.log", "ab", buffering=0)
argv = [str(a) for a in request["argv"]]
shell = ("cd " + shlex.quote(str(root)) + " && " + " ".join(shlex.quote(a) for a in argv)
         + "; echo $? > " + shlex.quote(str(run / "exit_code")))
proc = subprocess.Popen(["bash", "-c", shell], cwd=root, env=env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
(run / "pid").write_text(str(proc.pid))
print(json.dumps({"pid": proc.pid, "run_dir": str(run)}))
'''


def command_argv(args: list[str], code_root: Path | None = None) -> list[str]:
    """`arena ...` args, or a script in the checkout (category_batch.py ...).

    A script runs from `code_root` (a staged snapshot) when given: the copy
    under ROOT may be older than the modules the snapshot imports.
    """
    if not args:
        raise ValueError("nothing to run: give the arena arguments after --")
    if args[0].endswith(".py"):
        script = ROOT / args[0]
        if not script.is_file() or ROOT not in script.resolve().parents:
            raise ValueError(f"{args[0]} is not a script of this checkout")
        return [str(ROOT / ".venv/bin/python"), str((code_root or ROOT) / args[0]), *args[1:]]
    return [str(ROOT / ".venv/bin/python"), "-m", "pipeline.cli", *args]


def launch(ws: str, args: list[str], *, env: dict[str, str] | None = None, create: bool = False,
           sync: bool = True, staged: bool = False) -> dict:
    """Prepare the workspace and start one detached orchestrator on it.

    `staged` runs this checkout's code from its own snapshot (`stage_code`),
    so it can start while other runs on the workspace hold older code.
    """
    root = ROOT
    extra = dict(env or {})
    if staged:
        ensure_running(ws, create=create)
        if sync:
            sync_data(ws)
        root = stage_code(ws)
        extra.setdefault("ARENA_PIPELINE_ARTIFACTS", str(ROOT / "pipeline-artifacts"))
    elif sync:
        prepare(ws, create=create)
    rid = run_id()
    run_dir = ROOT / RUNS / rid
    request = {"root": str(root), "run_dir": str(run_dir), "argv": command_argv(args, root),
               "env": remote_env(extra), "keys": keys_values(), "code_sha": coder_mod._tree_sha(CODE_PATHS)}
    proc = coder_mod.ssh(ws, coder_mod._with_docker(f"python3 -c {q(LAUNCHER)}"),
                         stdin=json.dumps(request).encode(), timeout=300)
    started = json.loads(coder_mod._clean(proc.stdout.decode()).strip().splitlines()[-1])
    record = {"schema": "arena-pipeline/coder-run/1", "workspace": ws, "run": rid, "argv": request["argv"],
              "env": sorted(request["env"]), "pid": started["pid"], "launched_at": time.time(),
              "code_sha": request["code_sha"], "code_root": str(root)}
    local = ROOT / RUNS / rid
    local.mkdir(parents=True, exist_ok=True)
    (local / "launch.json").write_text(json.dumps(record, indent=2))
    return record


STATUS = r'''
import json, os, sys
from pathlib import Path
runs = Path(sys.argv[1]); out = []
for run in sorted(runs.glob("*")) if runs.is_dir() else []:
    pid = int((run / "pid").read_text()) if (run / "pid").is_file() else None
    alive = False
    if pid:
        try:
            os.kill(pid, 0); alive = True
        except OSError:
            alive = False
    code = (run / "exit_code").read_text().strip() if (run / "exit_code").is_file() else None
    tail = []
    if (run / "run.log").is_file():
        with open(run / "run.log", "rb") as fh:
            fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 4000))
            tail = fh.read().decode("utf-8", "replace").splitlines()[-6:]
    cmd = json.loads((run / "cmd.json").read_text()) if (run / "cmd.json").is_file() else {}
    out.append({"run": run.name, "pid": pid, "alive": alive, "exit_code": code,
                "argv": cmd.get("argv"), "started_at": cmd.get("started_at"), "tail": tail})
print(json.dumps(out))
'''


def runs(ws: str) -> list[dict]:
    proc = coder_mod.ssh(ws, f"python3 -c {q(STATUS)} {q(ROOT / RUNS)}", timeout=120)
    text = coder_mod._clean(proc.stdout.decode()).strip().splitlines()
    return json.loads(text[-1]) if text else []


def any_alive(ws: str, *, strict: bool = False) -> bool:
    """Is any run alive on `ws`? Unreadable is False unless `strict` (then it raises)."""
    try:
        alive = [r["run"] for r in runs(ws) if r["alive"]]
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        if strict:
            raise
        return False
    for rid in open_launches(ws):
        if rid not in alive:
            mark_finished(rid)
    return bool(alive)


def remote_trees(ws: str) -> set[str]:
    """Top-level peer trees that already exist on the workspace."""
    script = f"cd {q(ROOT)} 2>/dev/null && ls -d " + " ".join(PEER_GLOBS) + " 2>/dev/null"
    out = coder_mod.ssh(ws, script, timeout=120, check=False)
    return {line.strip().rstrip("/") for line in coder_mod._clean(out.stdout.decode()).splitlines() if line.strip()}


def live_trees() -> set[str]:
    """Output trees (`-o <tree>`) of every run this checkout launched and has not seen finish."""
    base, trees = ROOT / RUNS, set()
    for path in sorted(base.glob("*/launch.json")) if base.is_dir() else []:
        if (path.parent / "finished.json").is_file():
            continue
        try:
            argv = json.loads(path.read_text()).get("argv") or []
        except (OSError, ValueError):
            continue
        for flag in ("-o", "--out"):
            if flag in argv and argv.index(flag) + 1 < len(argv):
                trees.add(Path(argv[argv.index(flag) + 1]).name)
    return trees


def open_launches(ws: str) -> list[str]:
    """Runs this checkout launched on `ws` that it has not yet seen finish."""
    base = ROOT / RUNS
    out = []
    for path in sorted(base.glob("*/launch.json")) if base.is_dir() else []:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("workspace") == ws and not (path.parent / "finished.json").is_file():
            out.append(path.parent.name)
    return out


def mark_finished(rid: str, state: dict | None = None) -> None:
    local = ROOT / RUNS / rid
    local.mkdir(parents=True, exist_ok=True)
    (local / "finished.json").write_text(json.dumps({"at": time.time(), **(state or {})}, indent=2))


def kill(ws: str, rid: str) -> None:
    pid_file = ROOT / RUNS / rid / "pid"
    coder_mod.ssh(ws, f"kill -TERM -- -$(cat {q(pid_file)}) 2>/dev/null || true", timeout=120, check=False)


def _allowed(member: str) -> bool:
    rel = Path(member)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return False
    return any(rel.parts[0] == r or rel.parts[0].startswith("envs") for r in PULL_ROOTS)


def extract_pull(blob: Path, root: Path) -> dict:
    """Extract a pulled tar under `root`: only PULL_ROOTS, regular files and
    directories, never over a local file that is newer than the remote copy."""
    written = kept = refused = 0
    with tarfile.open(blob, "r:gz") as tar:
        for member in tar:
            if not _allowed(member.name) or not (member.isfile() or member.isdir()):
                refused += 1
                continue
            target = Path(root) / member.name
            if member.isfile() and target.is_file() and target.stat().st_mtime > member.mtime:
                kept += 1
                continue
            tar.extract(member, root, filter="tar")
            written += member.isfile()
    return {"written": written, "kept_newer_local": kept, "refused": refused}


def pull(ws: str, rid: str, *, since: str | None = None) -> dict:
    """Bring back every file the run wrote under envs*/pipeline-artifacts/reference.

    Files newer than the run's start (or than `since`, a remote marker file) are
    streamed as one tar and extracted under this checkout; a local file newer
    than the remote copy is kept, and nothing outside PULL_ROOTS is written.

    "Newer" is modification OR status change (-cnewer): a copy that preserves
    mtime -- Harbor's capture of an episode's final repository keeps every
    untouched fixture file's original mtime -- is still a new file. Until
    2026-10-04 those files were never pulled, and every captured episode tree
    on the operator's machine held only the files the agent edited.
    """
    run_dir = ROOT / RUNS / rid
    marker = since or str(run_dir / "started")
    stamp = run_dir / f"pulled-{int(time.time())}"
    roots = " ".join(q(r) for r in ("pipeline-artifacts", "reference", "deliveries"))
    script = (f"cd {q(ROOT)} && touch {q(stamp)} && "
              f"find envs* {roots} \\( -newer {q(marker)} -o -cnewer {q(marker)} \\) \\( -type f -o -type l \\) "
              "-not -path '*/__pycache__/*' -not -name '.build.lock' -print0 2>/dev/null "
              "| tar --null -czf - -T - 2>/dev/null")
    with tempfile.TemporaryDirectory(prefix="arena-pull-") as tmp:
        blob = Path(tmp) / "pull.tgz"
        with open(blob, "wb") as fh:
            proc = subprocess.run([coder_mod._binary(), "ssh", ws, "--wait=yes", "--", "bash", "-lc", q(script)],
                                  stdout=fh, stderr=subprocess.PIPE, timeout=None)
        if proc.returncode not in (0, 1, 2) or blob.stat().st_size == 0:
            raise RuntimeError(f"pull from {ws} failed ({proc.returncode}): "
                               + coder_mod._clean(proc.stderr.decode(errors="replace"))[-600:])
        counts = extract_pull(blob, ROOT)
    record = {"workspace": ws, "run": rid, **counts, "marker": str(stamp), "at": time.time()}
    local = ROOT / RUNS / rid
    local.mkdir(parents=True, exist_ok=True)
    with open(local / "pulls.jsonl", "a") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def mirror_bundle(ws: str, bundle: Path) -> None:
    """Make this checkout's copy of a FINISHED bundle identical to the workspace's.

    `pull` never deletes, so the local mirror only accumulates: on 2026-10-04
    the local c-007 still carried two designer helper scripts the designer had
    deleted on the workspace (`environment/repo/_gen/ref.py`), and its artifact
    identity no longer matched the screen that measured it. The release
    surfaces are mirrored with deletion; `verification/` and `hardened/` (which
    remote runs may still be writing) are only updated, never pruned.
    """
    bundle = Path(bundle).resolve()
    if ROOT not in bundle.parents:
        raise ValueError(f"{bundle} is not inside this checkout")
    rsync(ws, f"{bundle}/", f"{bundle}/", push=False, delete=True,
          filters=["- verification/", "- hardened/", "- __pycache__/"], timeout=1800)
    for sub in ("verification", "hardened"):
        try:
            rsync(ws, f"{bundle / sub}/", f"{bundle / sub}/", push=False, update=True, timeout=1800)
        except RuntimeError as exc:
            if "No such file or directory" not in str(exc):     # a bundle never hardened has no hardened/
                raise


def watch(ws: str, rid: str, *, interval: int = 600, extend: str = "4h") -> dict:
    """Keep the workspace from autostopping, pull incrementally, final pull at exit."""
    last, failures = None, 0
    while True:
        try:
            coder_mod.coder("schedule", "extend", ws, extend, check=False, timeout=120)
            state = next((r for r in runs(ws) if r["run"] == rid), None)
            failures = 0
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
            # A dropped SSH session must not end the watch: it is what keeps the
            # workspace's stop deadline moving.
            failures += 1
            print(f"  {time.strftime('%H:%M')} status unavailable ({failures}): {str(exc)[:160]}", flush=True)
            time.sleep(min(interval, 60 * failures))
            continue
        if state is None:
            raise RuntimeError(f"run {rid} is not on {ws}")
        try:
            got = pull(ws, rid, since=last)
            last = got["marker"]
            print(f"  {time.strftime('%H:%M')} {rid}: alive={state['alive']} exit={state['exit_code']} "
                  f"pulled {got['written']} file(s)", flush=True)
        except RuntimeError as exc:
            print(f"  pull failed (will retry): {exc}", flush=True)
        if not state["alive"]:
            final = {"run": rid, "exit_code": state["exit_code"], "tail": state["tail"]}
            mark_finished(rid, final)
            return final
        time.sleep(interval)


def expand_ranks(text: str) -> list[int]:
    """'1-5,9,12-13' -> [1, 2, 3, 4, 5, 9, 12, 13]."""
    out: list[int] = []
    for piece in text.replace(" ", "").split(","):
        if not piece:
            continue
        lo, _, hi = piece.partition("-")
        if not lo.isdigit() or (hi and not hi.isdigit()):
            raise ValueError(f"bad rank range {piece!r}")
        out += list(range(int(lo), int(hi or lo) + 1))
    if not out or min(out) < 1 or len(set(out)) != len(out):
        raise ValueError("ranks must be distinct positive integers")
    return out


def shard_plan(workspaces: list[str], ranks: list[int]) -> dict[str, list[int]]:
    """Round-robin, so every shard gets a similar mix of the ranking."""
    if not workspaces or len(set(workspaces)) != len(workspaces):
        raise ValueError("name each workspace once")
    plan = {ws: [] for ws in workspaces}
    for i, rank in enumerate(ranks):
        plan[workspaces[i % len(workspaces)]].append(rank)
    return {ws: r for ws, r in plan.items() if r}


def shard(workspaces: list[str], ranks: list[int], args: list[str], *,
          env: dict[str, str] | None = None, create: bool = False) -> list[dict]:
    """One orchestrator per workspace over disjoint --ranks of the SAME ranking
    and the same output tree; prepared in parallel, launched as each is ready."""
    if "--ranks" in args or "-n" in args or "--count" in args:
        raise ValueError("shard sets --ranks itself; drop --ranks/-n/--count from the build arguments")
    plan = shard_plan(workspaces, ranks)
    from concurrent.futures import ThreadPoolExecutor
    def one(ws):
        return launch(ws, [*args, "--ranks", ",".join(map(str, plan[ws]))],
                      env={**(env or {}), "ARENA_SHARD": ws}, create=create)
    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        futures = {ws: pool.submit(one, ws) for ws in plan}
    out = []
    for ws, future in futures.items():
        try:
            out.append(future.result())
        except Exception as exc:                         # noqa: BLE001 -- report per shard
            out.append({"workspace": ws, "error": f"{type(exc).__name__}: {exc}", "ranks": plan[ws]})
    return out


# ---------------------------------------------------------------------------
# CLI: ./arena coder prepare|run|shard|runs|pull|watch|kill
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tail: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, tail = argv[:cut], argv[cut + 1:]
    ap = argparse.ArgumentParser(prog="arena coder", description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=("prepare", "run", "shard", "push", "runs", "pull", "watch", "kill"))
    ap.add_argument("workspace", nargs="?")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--create", action="store_true", help="create the workspace if it does not exist")
    ap.add_argument("--env", action="append", default=[], help="K=V for the remote process (no credentials)")
    ap.add_argument("--no-sync", action="store_true", help="launch on the code and data already there")
    ap.add_argument("--staged", action="store_true",
                    help="run: this checkout's code from its own snapshot beside live runs (stage_code)")
    ap.add_argument("--no-data", action="store_true", help="prepare: code and runtime only")
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--workspaces", help="shard: comma-separated workspace names")
    ap.add_argument("--ranks", help="shard: rank ranges to split, e.g. 1-100")
    ns = ap.parse_args(argv)
    if ns.action == "shard":
        if not (ns.workspaces and ns.ranks):
            print("shard needs --workspaces a,b,... and --ranks 1-100")
            return 2
        env = dict(item.split("=", 1) for item in ns.env)
        rows = shard([w.strip() for w in ns.workspaces.split(",") if w.strip()],
                     expand_ranks(ns.ranks), tail, env=env, create=ns.create)
        print(json.dumps(rows, indent=2))
        for row in rows:
            if "run" in row:
                print(f"  ./arena coder watch {row['workspace']} {row['run']}")
        return 0 if all("run" in r for r in rows) else 1
    cfg = coder_mod.settings()
    ws = ns.workspace or (cfg.workspace or cfg.name if cfg else None)
    if not ws:
        print("name a workspace (or configure [coder] in workspace.toml)")
        return 2
    if ns.action == "prepare":
        print(json.dumps(prepare(ws, create=ns.create, data=not ns.no_data), indent=2))
        return 0
    if ns.action == "push":
        # Selection inputs and peer trees only -- never code, so it is safe under
        # a live run (a second wave's seed file reaches the workspace this way).
        print(json.dumps(sync_data(ws, dump=False), indent=2))
        return 0
    if ns.action == "run":
        env = dict(item.split("=", 1) for item in ns.env)
        record = launch(ws, tail, env=env, create=ns.create, sync=not ns.no_sync, staged=ns.staged)
        print(json.dumps(record, indent=2))
        print(f"\n  follow it:   ./arena coder watch {ws} {record['run']}")
        return 0
    if ns.action == "runs":
        for row in runs(ws):
            state = "alive" if row["alive"] else f"exit {row['exit_code']}"
            print(f"{row['run']}  {state:10s} {' '.join(map(str, row['argv'] or []))[:120]}")
            for line in row["tail"]:
                print(f"    {line[:200]}")
        return 0
    if not ns.run:
        print("name the run (./arena coder runs <ws>)")
        return 2
    if ns.action == "pull":
        print(json.dumps(pull(ws, ns.run), indent=2))
        return 0
    if ns.action == "watch":
        print(json.dumps(watch(ws, ns.run, interval=ns.interval), indent=2))
        return 0
    if ns.action == "kill":
        kill(ws, ns.run)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
