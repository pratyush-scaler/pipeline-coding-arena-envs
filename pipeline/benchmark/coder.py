"""Native calibration on one Coder workspace.

Calibration runs only here. `release` asks `native_runner("calibration")` for
its campaign function, and on a real run that is `run_campaign` below: a
drop-in for `harbor.run_campaign` that executes the same code on a remote
workspace and brings the evidence back.

The workspace, per the operator's rules:

    exactly one: the configured `workspace` (an existing one, never created,
    stopped or deleted) or else the pool's own `name`, created on first use
    and paused when idle -- never a second one
    at most `tasks_per_instance` campaigns on it at once (default 20); a
    campaign that does not fit waits for a slot instead of spilling over
    every campaign's output lands back in the local path it was asked for, and
    `release` pools the results exactly as it pools local ones

Evidence stays verifiable because nothing is translated. The workspace holds
the pipeline at the same absolute path as this checkout, the task snapshot and
the campaign output at their own absolute paths, and the pinned native runtime
(`requirements-native.lock`). `evidence.native_evidence` therefore reads a
remote campaign exactly as it reads a local one: same job config, same runner
hash, same pinned versions, same isolation proofs.

Configuration lives in the gitignored `workspace.toml` ([coder]); nothing about
a deployment, a user, a template or a workspace is stored in the repository.
See `workspace.example.toml` for the keys.

Credentials: model keys are passed to each remote campaign on stdin and set in
that process's environment only. They are never written to the workspace disk
or put on a command line.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "workspace.toml"

#: What a workspace needs of this checkout to run a campaign and to reproduce
#: every hash the evidence checks (runner, history adapter, pinned runtime).
#: Everything `artifacts.policy_identity` hashes must be here too, or the remote
#: run cannot compute the fingerprint its evidence is bound to.
SYNC_PATHS = ("pipeline", "arena", "pyproject.toml", "requirements-native.lock",
              "requirements-native.in", "requirements-evidence.lock", "uv.lock",
              "CLAUDE.md", "rulesets")

#: Environment passed to a model campaign: credentials and provider routing only.
FORWARD_SUFFIXES = ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "SECRET_KEY")
FORWARD_NAMES = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
                 "ARENA_HARBOR_MEM", "ARENA_HARBOR_CPUS")

#: Marks the end of the remote runner's log and the start of its report.
REPORT_MARKER = "=== arena-coder-report ==="

#: The CLI prints this when client and server versions differ; it is advice,
#: not output, and must not be parsed as either.
_NOISE = ("version mismatch", "download v2")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    template: str
    url: str | None = None
    copy_parameters_from: str | None = None
    parameters: dict = field(default_factory=dict)
    tasks_per_instance: int = 20
    trials_per_task: int = 1
    stop_after: str = "12h"
    #: The pool's own workspace: created on first use, paused when idle.
    name: str = "arena-calib-1"
    min_cpus: int = 32
    min_memory_gib: int = 120
    min_disk_gib: int = 190
    idle_stop_after: int = 300           # pause the workspace after this many idle seconds; 0 = never
    #: An existing workspace to run on instead, by name. The pool never creates,
    #: stops or deletes it: a named workspace can be shared with other work,
    #: and pausing it would kill whatever else runs there.
    workspace: str | None = None

    def problems(self) -> list[str]:
        out = []
        if self.workspace is not None and not (isinstance(self.workspace, str) and self.workspace.strip()):
            out.append("[coder] workspace must be a workspace name")
        elif self.workspace == self.name:
            out.append("[coder] workspace names an existing workspace to share; it cannot be "
                       "the pool's own `name`")
        if not (isinstance(self.name, str) and self.name.strip()):
            out.append("[coder] name must be a workspace name")
        if not self.template:
            out.append("[coder] template is required")
        if not (self.copy_parameters_from or self.parameters):
            out.append("[coder] needs copy_parameters_from (a workspace already built at the "
                       "specs to use) or a [coder.parameters] table")
        for key in ("tasks_per_instance", "trials_per_task"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                out.append(f"[coder] {key} must be a positive integer")
        if type(self.idle_stop_after) is not int or self.idle_stop_after < 0:
            out.append("[coder] idle_stop_after must be a non-negative integer (seconds)")
        if self.trials_per_task > self.tasks_per_instance:
            out.append("[coder] trials_per_task cannot exceed tasks_per_instance")
        return out


def settings(path: Path = CONFIG) -> Settings | None:
    """The [coder] section of workspace.toml, or None when it is absent."""
    try:
        with open(path, "rb") as fh:
            section = tomllib.load(fh).get("coder")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not section:
        return None
    known = set(Settings.__dataclass_fields__)
    unknown = sorted(set(section) - known)
    if unknown:
        raise ValueError(f"unknown [coder] keys in {path.name}: {unknown}")
    return Settings(**section)


# ---------------------------------------------------------------------------
# The Coder CLI
# ---------------------------------------------------------------------------

def _binary() -> str:
    found = os.environ.get("ARENA_CODER_BIN") or shutil.which("coder")
    if not found:
        raise ValueError("the Coder CLI is not installed; see https://coder.com/docs/install/cli")
    return found


def _clean(text: str) -> str:
    return "\n".join(l for l in (text or "").splitlines()
                     if not any(n in l.lower() for n in _NOISE))


def coder(*args: str, stdin: bytes | None = None, timeout: float | None = 600,
          check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([_binary(), *args], input=stdin, capture_output=True, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError(f"coder {args[0]} failed ({proc.returncode}): "
                           + _clean(proc.stderr.decode(errors="replace"))[-600:])
    return proc


def ssh(workspace: str, script: str, *, stdin: bytes | None = None,
        timeout: float | None = 600, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bash script on a workspace, after its startup script has finished."""
    return coder("ssh", workspace, "--wait=yes", "--", "bash", "-lc", shlex.quote(script),
                 stdin=stdin, timeout=timeout, check=check)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

LOGIN_GUIDE = """\
Coder is not logged in, and calibration runs only on Coder.

  1. A browser tab is opening at your deployment's CLI sign-in page:
       {url}/cli-auth
  2. Sign in there the way you normally do (SSO or password).
  3. The page shows a session token. Copy it.
  4. Paste it at the prompt below and press Enter.

Nothing else is needed; the token is stored by the Coder CLI, not by this
pipeline.
"""


def logged_in() -> bool:
    proc = coder("whoami", check=False, timeout=60)
    text = (proc.stdout + proc.stderr).decode(errors="replace").lower()
    return proc.returncode == 0 and "authenticated" in text


def ensure_login(cfg: Settings, *, interactive: bool | None = None) -> None:
    """Refuse to calibrate on a logged-out CLI; walk the operator through login."""
    if logged_in():
        return
    url = (cfg.url or os.environ.get("CODER_URL") or "").rstrip("/")
    if not url:
        raise ValueError("Coder is not logged in and no deployment URL is configured. "
                         "Set [coder] url in workspace.toml, then run `./arena coder login`.")
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if not interactive:
        raise ValueError(f"Coder is not logged in. Run `./arena coder login` in a terminal "
                         f"(it opens {url}/cli-auth and asks for the session token).")
    print(LOGIN_GUIDE.format(url=url))
    webbrowser.open(f"{url}/cli-auth")
    code = subprocess.call([_binary(), "login", url])
    if code or not logged_in():
        raise ValueError("Coder login did not complete; run `./arena coder login` again")


# ---------------------------------------------------------------------------
# One workspace
# ---------------------------------------------------------------------------

def _tree_sha(paths) -> str:
    digest = hashlib.sha256()
    for base in paths:
        base = ROOT / base
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for f in files:
            if "__pycache__" in f.parts or f.suffix == ".pyc":
                continue
            digest.update(str(f.relative_to(ROOT)).encode() + b"\0" + f.read_bytes() + b"\0")
    return digest.hexdigest()


def _tar(paths: list[Path], root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in paths:
            tar.add(path, arcname=str(path.relative_to(root)),
                    filter=lambda i: None if "__pycache__" in i.name or i.name.endswith(".pyc") else i)
    return buf.getvalue()


def _with_docker(command: str) -> str:
    """Run with the docker group. Coder sessions inherit the workspace agent's
    groups, so a `usermod -aG docker` made after the agent started only takes
    effect through `sg`."""
    return f"sg docker -c {shlex.quote(command)}"


def _mkdir_script(path: Path) -> str:
    """Create `path` owned by the workspace user, parents as needed."""
    q = shlex.quote(str(path))
    return f'sudo mkdir -p {q} && sudo chown "$(id -un):$(id -gn)" {q}'


class Instance:
    def __init__(self, name: str, cfg: Settings, pinned: bool = False):
        self.name, self.cfg, self.pinned = name, cfg, pinned
        self.load = 0
        self.state = "provisioning"              # provisioning | ready | stopping | stopped | failed
        self.error: str | None = None
        self.synced: str | None = None
        self.pushed: dict[str, str] = {}         # bundle path -> tree sha
        self.lock = threading.Lock()             # serialises code/bundle pushes

    # -- lifecycle -------------------------------------------------------
    def provision(self, existing: dict | None) -> None:
        """Create or start the workspace, then check it and install the runtime."""
        if existing is None and self.pinned:
            raise RuntimeError(f"pinned workspace {self.name} does not exist; "
                               "the pool never creates a pinned workspace")
        if existing is None:
            args = ["create", self.name, "-t", self.cfg.template, "--yes",
                    "--stop-after", self.cfg.stop_after]
            if self.cfg.copy_parameters_from:
                args += ["--copy-parameters-from", self.cfg.copy_parameters_from]
            for key, value in sorted(self.cfg.parameters.items()):
                args += ["--parameter", f"{key}={value}"]
            coder(*args, timeout=1800)
        elif existing.get("status") != "running":
            coder("start", self.name, "--yes", timeout=1800)
        self.verify_specs()
        self.bootstrap()

    def verify_specs(self) -> None:
        out = ssh(self.name, "nproc; awk '/MemTotal/{print int($2/1048576)}' /proc/meminfo; "
                             "df -BG --output=size / | tail -1 | tr -dc 0-9", timeout=1800)
        cpus, mem, disk = (int(x) for x in _clean(out.stdout.decode()).split())
        short = [f"{label} {have} < {need}" for label, have, need in (
            ("vCPU", cpus, self.cfg.min_cpus), ("memory GiB", mem, self.cfg.min_memory_gib),
            ("disk GiB", disk, self.cfg.min_disk_gib)) if have < need]
        if short:
            raise RuntimeError(f"workspace {self.name} is below the required specs: "
                               + ", ".join(short))

    def bootstrap(self) -> None:
        """Docker access and the pinned native runtime, at this checkout's path."""
        root = shlex.quote(str(ROOT))
        # Fresh workspaces from the QC template carry no Docker. Install it
        # from Ubuntu's own archive (no curl-to-shell), with Compose v2, which
        # Harbor drives.
        ssh(self.name, "\n".join([
            "set -euo pipefail",
            "if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then",
            "  sudo apt-get update -qq",
            "  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null",
            "fi",
            # Harbor builds its egress-control sidecar with `docker buildx build`
            # (the calibration harnesses' model-API allowlist, user 2026-09-26);
            # workspaces bootstrapped before then have Docker without buildx.
            "if ! docker buildx version >/dev/null 2>&1; then",
            "  sudo apt-get update -qq",
            "  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-buildx >/dev/null",
            "fi",
            "sudo systemctl enable --now docker >/dev/null 2>&1 || "
            "(pgrep -x dockerd >/dev/null || (sudo nohup dockerd >/tmp/dockerd.log 2>&1 &); sleep 5)",
            'sudo usermod -aG docker "$(id -un)"',
            "sudo docker info >/dev/null",
            _mkdir_script(ROOT),
        ]), timeout=1800)
        self.sync_code()
        ssh(self.name, "\n".join([
            "set -euo pipefail",
            'export PATH="$HOME/.local/bin:$PATH"',
            "command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh",
            'export PATH="$HOME/.local/bin:$PATH"',
            "uv python install 3.13 >/dev/null",
            f"test -x {root}/.venv-harbor/bin/python || uv venv --python 3.13 {root}/.venv-harbor",
            f"uv pip install --quiet --python {root}/.venv-harbor/bin/python "
            f"--require-hashes -r {root}/requirements-native.lock",
            # A fresh session picks up the docker group; prove it and the runtime.
            f"cd {root} && .venv-harbor/bin/python -c 'import harbor, litellm'",
        ]), timeout=1800)
        check = ssh(self.name, _with_docker("docker info --format '{{.ServerVersion}}'"),
                    timeout=300, check=False)
        if check.returncode:
            raise RuntimeError(f"docker is not usable on {self.name}: "
                               + _clean(check.stderr.decode(errors="replace"))[-300:])

    def sync_code(self) -> None:
        """Install this checkout's pipeline once, at provisioning.

        Never re-synced under running campaigns: a mid-batch code change would
        mix runner versions in one measurement, so `run` refuses it instead.
        """
        sha = _tree_sha(SYNC_PATHS)
        with self.lock:
            if self.synced == sha:
                return
            blob = _tar([ROOT / p for p in SYNC_PATHS if (ROOT / p).exists()], ROOT)
            root = shlex.quote(str(ROOT))
            ssh(self.name, f"rm -rf {root}/pipeline && mkdir -p {root} && tar -xzf - -C {root}",
                stdin=blob, timeout=900)
            self.synced = sha

    def push_bundle(self, bundle: Path) -> None:
        """The task snapshot at its own absolute path; skipped when unchanged."""
        from .support import fingerprint
        sha = fingerprint(bundle, task_only=False)["sha256"]
        with self.lock:
            if self.pushed.get(str(bundle)) == sha:
                return
            q = shlex.quote(str(bundle))
            ssh(self.name, f"{_mkdir_script(bundle.parent)} && rm -rf {q} && "
                           f"tar -xpzf - -C {shlex.quote(str(bundle.parent))}",
                stdin=_tar([bundle], bundle.parent), timeout=900)
            self.pushed[str(bundle)] = sha

    # -- one campaign ------------------------------------------------------
    def run(self, bundle: Path, output: Path, kwargs: dict, slots: int = 1) -> dict:
        if _tree_sha(SYNC_PATHS) != self.synced:
            # Safe only when nothing else runs here: never swap the code under
            # a running campaign, or one measurement mixes runner versions.
            if self.load != slots:
                raise RuntimeError(f"the pipeline changed while campaigns run on {self.name}; "
                                   "one calibration must run on one code version. Re-run it.")
            self.sync_code()
        self.push_bundle(bundle)
        env = {k: v for k, v in os.environ.items()
               if k.endswith(FORWARD_SUFFIXES) or k in FORWARD_NAMES} \
            if kwargs.get("agent", "terminus-2") in {"terminus-2", "claude-code", "codex"} else {}
        from .openai_campaign_credentials import campaign_environment, write_selection
        env, credential_selection = campaign_environment(kwargs, env)
        selection_receipt = write_selection(output, credential_selection)
        request = json.dumps({"bundle": str(bundle), "output": str(output),
                              "kwargs": kwargs, "env": env}).encode()
        root = shlex.quote(str(ROOT))
        parent = shlex.quote(str(output.parent))
        proc = ssh(self.name, f"{_mkdir_script(output.parent)} && cd {root} && " + _with_docker(
                       ".venv-harbor/bin/python -m pipeline.benchmark.coder_remote"),
                   stdin=request, timeout=None, check=False)
        text = proc.stdout.decode(errors="replace")
        if REPORT_MARKER not in text:
            raise RuntimeError(f"remote campaign on {self.name} produced no report "
                               f"(exit {proc.returncode}): "
                               + _clean(proc.stderr.decode(errors="replace"))[-600:])
        report = json.loads(text.split(REPORT_MARKER, 1)[1].strip())
        # Bring the evidence home, byte for byte, then free the workspace disk.
        fetched = ssh(self.name, f"tar -C {parent} -czf - {shlex.quote(output.name)}",
                      timeout=1800)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(fetched.stdout), mode="r:gz") as tar:
            tar.extractall(output.parent, filter="tar")
        ssh(self.name, f"rm -rf {shlex.quote(str(output))}", timeout=300, check=False)
        receipt = {"schema": "arena-pipeline/coder-campaign/1", "workspace": self.name,
                   "template": self.cfg.template, "output": str(output),
                   "report_sha256": hashlib.sha256((output / "report.json").read_bytes()).hexdigest()
                   if (output / "report.json").is_file() else None}
        if credential_selection is not None:
            receipt["openai_credential_selection"] = credential_selection
            receipt["openai_credential_selection_receipt"] = selection_receipt
        (output / "coder-receipt.json").write_text(json.dumps(receipt, indent=2))
        return report


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------

class Pool:
    """One calibration workspace; campaigns share its slots and wait for one."""

    def __init__(self, cfg: Settings):
        problems = cfg.problems()
        if problems:
            raise ValueError("; ".join(problems))
        self.cfg = cfg
        self.instances: list[Instance] = []      # at most one
        self.cond = threading.Condition()
        self.checked_login = False
        self.log: list[dict] = []
        self.idle_timer: threading.Timer | None = None
        self.exit_hook = False

    # -- provisioning ------------------------------------------------------
    def _rows(self) -> dict[str, dict]:
        proc = coder("list", "-o", "json", timeout=120)
        rows = json.loads(_clean(proc.stdout.decode()) or "[]")
        return {row.get("name") or "": {"status": (row.get("latest_build") or {}).get("status")}
                for row in rows}

    def _existing(self) -> dict[str, dict]:
        """The pool's own workspace -- the only one `stop` and `delete` touch."""
        return {n: r for n, r in self._rows().items() if n == self.cfg.name}

    def _lookup(self, inst: "Instance") -> dict | None:
        return self._rows().get(inst.name)

    def _start(self) -> Instance:
        """Bring the one workspace online (create, start or restart). Caller holds `self.cond`.

        The configured shared `workspace` if there is one, else the pool's own
        `name`: an existing workspace of that name (paused after an earlier
        calibration) is restarted, and one is created only when none exists.
        """
        if not self.instances:
            shared = self.cfg.workspace
            self.instances.append(Instance(shared or self.cfg.name, self.cfg, pinned=bool(shared)))
        inst = self.instances[0]
        inst.state = "provisioning"
        self.log.append({"event": "provision", "instance": inst.name, "pinned": inst.pinned,
                         "at": time.time()})
        threading.Thread(target=self._provision, args=(inst,), daemon=True).start()
        return inst

    def _provision(self, inst: Instance) -> None:
        self._hook_exit()
        try:
            existing = self._lookup(inst)
            self.log.append({"event": "reuse" if existing else "create", "instance": inst.name,
                             "at": time.time()})
            inst.provision(existing)
            state, error = "ready", None
        except Exception as exc:                          # noqa: BLE001
            state, error = "failed", f"{type(exc).__name__}: {exc}"
        with self.cond:
            inst.state, inst.error = state, error
            self.log.append({"event": state, "instance": inst.name, "at": time.time(),
                             **({"error": error} if error else {})})
            self.cond.notify_all()

    # -- slots ---------------------------------------------------------------
    def acquire(self, slots: int = 1) -> Instance:
        if slots > self.cfg.tasks_per_instance:
            raise ValueError("a campaign cannot need more slots than the workspace has")
        with self.cond:
            if not self.checked_login:
                ensure_login(self.cfg)
                self.checked_login = True
            self._cancel_idle()
            while True:
                inst = self.instances[0] if self.instances else None
                if inst is None or inst.state == "stopped":
                    self._start()
                elif inst.state == "failed":
                    raise RuntimeError(f"the Coder workspace {inst.name} cannot take this campaign: "
                                       f"{inst.error}")
                elif inst.state == "ready" and inst.load + slots <= self.cfg.tasks_per_instance:
                    inst.load += slots
                    self.log.append({"event": "assign", "instance": inst.name,
                                     "load": inst.load, "at": time.time()})
                    return inst
                # Starting, pausing or full: wait. There is never a second workspace.
                self.cond.wait(timeout=30)

    def release(self, inst: Instance, slots: int = 1) -> None:
        """Free a slot; when the workspace is idle, pause it after a grace period.

        Called only after the campaign's evidence has been copied back, so a
        paused workspace never holds scores that were not extracted.
        """
        with self.cond:
            inst.load -= slots
            self.cond.notify_all()
            if self.cfg.idle_stop_after and all(i.load == 0 for i in self.instances):
                self._cancel_idle()
                self.idle_timer = threading.Timer(self.cfg.idle_stop_after, self._pause_if_idle)
                self.idle_timer.daemon = True
                self.idle_timer.start()

    # -- pausing -------------------------------------------------------------
    def _cancel_idle(self) -> None:
        if self.idle_timer is not None:
            self.idle_timer.cancel()
            self.idle_timer = None

    def _pause_if_idle(self) -> None:
        with self.cond:
            if any(i.load for i in self.instances):
                return
            targets = [i for i in self.instances if i.state == "ready" and not i.pinned]
            for inst in targets:
                inst.state = "stopping"
        self.pause(targets)

    def pause(self, targets: list[Instance] | None = None) -> list[str]:
        """Stop the workspace (disk kept), so the next calibration reuses it.

        A workspace still being started is waited for and then stopped: left
        alone it would come up after the run ended and keep running.
        """
        with self.cond:
            if targets is None:
                deadline = time.time() + 1800
                while any(i.state == "provisioning" for i in self.instances) and time.time() < deadline:
                    self.cond.wait(timeout=10)
                targets = [i for i in self.instances
                           if i.state in ("ready", "stopping", "provisioning", "failed") and not i.load]
            # A shared workspace is never stopped: that would kill other work.
            targets = [i for i in targets if not i.pinned]
        # Nor is one a remote orchestrator is running on (`coder_run`): a stop
        # kills the run and wipes the workspace's root disk.
        targets = [i for i in targets if not _remote_run_alive(i.name)]
        paused = []
        for inst in targets:
            proc = coder("stop", inst.name, "--yes", check=False, timeout=1800)
            with self.cond:
                inst.state = "stopped" if proc.returncode == 0 else "failed"
                inst.synced, inst.pushed = None, {}
                self.log.append({"event": "paused" if proc.returncode == 0 else "pause-failed",
                                 "instance": inst.name, "at": time.time()})
                self.cond.notify_all()
            if proc.returncode == 0:
                paused.append(inst.name)
        return paused

    def _hook_exit(self) -> None:
        """Calibration is over when the process ends: pause what it used."""
        if not self.exit_hook:
            import atexit
            atexit.register(lambda: self.pause())
            self.exit_hook = True

    def status(self) -> list[dict]:
        with self.cond:
            return [{"name": i.name, "state": i.state, "load": i.load,
                     "capacity": self.cfg.tasks_per_instance, "error": i.error}
                    for i in self.instances]


def _remote_run_alive(name: str) -> bool:
    """A remote orchestrator launched on `name` and not yet seen to finish.

    Local launch records first (no remote call when none exist); when one is
    open, the workspace is asked, and an unreadable answer counts as alive --
    stopping a workspace under a run kills it and wipes its root disk.
    """
    from .coder_run import any_alive, open_launches
    if not open_launches(name):
        return False
    try:
        return any_alive(name, strict=True)
    except Exception:                                    # noqa: BLE001 -- unreadable: assume busy
        return True


_POOL: Pool | None = None
_POOL_LOCK = threading.Lock()


def pool() -> Pool:
    global _POOL
    if "unittest" in sys.modules and os.environ.get("ARENA_NATIVE_BACKEND") != "coder":
        raise RuntimeError("refusing to create Coder workspaces from a test process; set "
                           "ARENA_NATIVE_BACKEND=coder explicitly for a live test")
    with _POOL_LOCK:
        if _POOL is None:
            cfg = settings()
            if cfg is None:
                raise ValueError("calibration runs only on Coder: add a [coder] section to "
                                 "workspace.toml (see workspace.example.toml)")
            _POOL = Pool(cfg)
        return _POOL


# ---------------------------------------------------------------------------
# The campaign function release uses
# ---------------------------------------------------------------------------

def run_campaign(bundle: Path, output: Path, **kwargs) -> dict:
    """`harbor.run_campaign`, on a pooled Coder workspace."""
    bundle, output = Path(bundle).absolute(), Path(output).absolute()
    if output.exists():
        raise FileExistsError(f"Benchmark output must be a fresh directory: {output}")
    p = pool()
    # One Harbor trial at a time per task by default: 20 tasks x 1 trial is
    # the workspace's rated concurrency. The job config records what ran.
    kwargs["workers"] = min(int(kwargs.get("workers") or 1), p.cfg.trials_per_task)
    slots = kwargs["workers"]
    inst = p.acquire(slots)
    try:
        return inst.run(bundle, output, kwargs, slots)
    finally:
        p.release(inst, slots)


class _Unbounded:
    """The pool bounds concurrency; the local episode cap does not apply."""
    limit = None

    @contextlib.contextmanager
    def reserve(self, requested):
        if requested < 1:
            raise ValueError("Requested episode slots must be positive")
        yield requested

    def stats(self):
        return {"backend": "coder"}


def _default_backend() -> str:
    """Coder, except inside a test process.

    A unit test that exercises qualification must never reach a real
    deployment: one did, and created a 32-vCPU workspace. Production code
    never imports unittest (a test asserts it), so its presence is a reliable
    sign of a test run however the suite was launched.
    """
    return "local" if "unittest" in sys.modules else "coder"


def native_runner(purpose: str):
    """(run_campaign, capacity) for a native campaign.

    Calibration runs only on Coder. `ARENA_NATIVE_BACKEND=local` exists for the
    test suite and offline diagnostics, and is refused for model campaigns
    unless it was set explicitly.
    """
    backend = os.environ.get("ARENA_NATIVE_BACKEND") or _default_backend()
    if backend == "local":
        from . import harbor
        from .capacity import NATIVE_CAPACITY
        return (lambda *a, **k: harbor.run_campaign(*a, **k)), NATIVE_CAPACITY
    if backend != "coder":
        raise ValueError(f"unknown ARENA_NATIVE_BACKEND {backend!r}")
    return run_campaign, _Unbounded()


# ---------------------------------------------------------------------------
# Operator commands: ./arena coder status|login|stop|delete
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="arena coder", description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=("status", "login", "stop", "delete"))
    ns = ap.parse_args(argv)
    cfg = settings()
    if cfg is None:
        print("no [coder] section in workspace.toml (see workspace.example.toml)")
        return 2
    if ns.action == "login":
        ensure_login(cfg, interactive=True)
        print("Coder CLI is logged in.")
        return 0
    print(f"logged in: {'yes' if logged_in() else 'NO -- run ./arena coder login'}")
    problems = cfg.problems()
    for problem in problems:
        print(f"  config: {problem}")
    names = Pool(cfg)._existing() if not problems and logged_in() else {}
    target = cfg.workspace or cfg.name
    print(f"calibration workspace: {target} "
          f"({'shared; never stopped by the pipeline' if cfg.workspace else 'own; paused when idle'}), "
          f"{cfg.tasks_per_instance} campaigns at once, template {cfg.template}")
    for name, row in sorted(names.items()):
        print(f"  {name:24s} {row.get('status')}")
    if ns.action in ("stop", "delete"):
        for name in sorted(names):
            if _remote_run_alive(name):
                print(f"  {ns.action} {name}: REFUSED -- a remote run is alive there (./arena coder runs {name})")
                continue
            args = ("stop", name, "--yes") if ns.action == "stop" else ("delete", name, "--yes")
            proc = coder(*args, check=False, timeout=1800)
            print(f"  {ns.action} {name}: {'ok' if proc.returncode == 0 else 'FAILED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
