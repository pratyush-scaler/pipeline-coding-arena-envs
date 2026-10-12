"""The local execution engine: build a bundle's images and run its grader.

Emulates, in plain Docker, exactly what Harbor does for a separate-verifier
task -- an agent container from `environment/`, a working tree that gets
changed, a capture of `/app/repo` (minus dependency directories) and
`/app/deliverables`, a fresh offline verifier container from `tests/` with
the capture restored at the same paths, `/tests/test.sh` run with
`ARENA_STEP`, and `reward.json` read back. Every control, probe and designer
check runs through this, because a Harbor trial takes minutes and the
controls need dozens of variants per draft. The calibration of record still
runs through Harbor; this is the authoring-time engine, and `harbor run -a
oracle` / `-a nop` confirm the package end to end before anything ships.

Both containers run with `--network none`. Images are content-addressed by
the bytes of their build context, so an unchanged environment never rebuilds
and a changed one can never reuse a stale image.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from .contracts import strict_json

DOCKER = shutil.which("docker") or "docker"
LABEL = "arena-coding-sandbox"
#: What the verifier needs to drop privileges per test process (see grade.py).
#: DAC_READ_SEARCH: the root grader must still READ what the grader user wrote
#: (the private report directory, the per-check tree) once it is no longer the
#: owner; it never grants write access.
VERIFIER_CAPS = ("CHOWN", "SETUID", "SETGID", "DAC_READ_SEARCH")
BUILD_TIMEOUT_S = int(os.environ.get("ARENA_SANDBOX_BUILD_TIMEOUT_S", "1800"))
EXEC_TIMEOUT_S = int(os.environ.get("ARENA_SANDBOX_EXEC_TIMEOUT_S", "900"))


class SandboxError(RuntimeError):
    pass


def _run(argv: list[str], *, timeout: float | None = None, input_bytes: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, input=input_bytes, capture_output=True, timeout=timeout)
    if check and proc.returncode:
        raise SandboxError(f"{' '.join(argv[:4])}... failed ({proc.returncode}): "
                           f"{proc.stderr.decode('utf-8', 'replace')[-2000:]}")
    return proc


def available() -> tuple[bool, str]:
    try:
        proc = _run([DOCKER, "info", "--format", "{{.ServerVersion}}"], timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker is not reachable: {exc}"
    if proc.returncode:
        return False, "docker daemon is not running: " + proc.stderr.decode("utf-8", "replace")[-300:]
    reap_orphans()
    return True, proc.stdout.decode().strip()


#: A sandbox pair lives for one grading (minutes). A build killed mid-grade
#: leaves its pair idling on `sleep infinity`: four such pairs from two killed
#: builds were found on 2026-09-29, one of them 31 hours old.
ORPHAN_AGE_S = 3 * 3600


def _stale_container_ids(ps_output: str, now: float, max_age_s: int = ORPHAN_AGE_S) -> list[str]:
    """Container ids older than `max_age_s` from `docker ps --format "{{.ID}} {{.CreatedAt}}"`."""
    import calendar
    from datetime import datetime
    stale = []
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            created = calendar.timegm(datetime.strptime(parts[1] + " " + parts[2], "%Y-%m-%d %H:%M:%S").timetuple())
        except ValueError:
            continue
        if now - created > max_age_s:
            stale.append(parts[0])
    return stale


def reap_orphans(max_age_s: int = ORPHAN_AGE_S) -> list[str]:
    """Remove sandbox containers older than `max_age_s`; best effort, never raises."""
    import time
    try:
        proc = _run([DOCKER, "ps", "--filter", f"label={LABEL}", "--format", "{{.ID}} {{.CreatedAt}}"],
                    timeout=30, check=False)
        if proc.returncode:
            return []
        stale = _stale_container_ids(proc.stdout.decode("utf-8", "replace"), time.time(), max_age_s)
        if stale:
            _run([DOCKER, "rm", "-f", *stale], timeout=120, check=False)
        return stale
    except (OSError, subprocess.TimeoutExpired):
        return []


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def _context_files(context: Path) -> list[Path]:
    """Files docker will see, honouring the context's .dockerignore (allow-list style)."""
    ignore = context / ".dockerignore"
    patterns = [line.strip() for line in ignore.read_text().splitlines()
                if line.strip() and not line.startswith("#")] if ignore.is_file() else []
    import fnmatch

    def kept(rel: str) -> bool:
        keep = True
        for pattern in patterns:
            negate = pattern.startswith("!")
            body = pattern[1:] if negate else pattern
            body = body.rstrip("/")
            hit = fnmatch.fnmatch(rel, body) or fnmatch.fnmatch(rel, body + "/*") \
                or rel == body or rel.startswith(body + "/") \
                or (body.endswith("/**") and rel.startswith(body[:-3] + "/"))
            if hit:
                keep = negate
        return keep

    files = []
    for path in sorted(context.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(context).as_posix()
            if kept(rel) or rel == "Dockerfile":
                files.append(path)
    return files


def context_digest(context: Path) -> str:
    digest = hashlib.sha256()
    for path in _context_files(context):
        digest.update(path.relative_to(context).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def image_tag(context: Path, role: str) -> str:
    return f"{LABEL}-{role}:{context_digest(context)[:16]}"


def _image_exists(tag: str) -> bool:
    return _run([DOCKER, "image", "inspect", tag], check=False, timeout=60).returncode == 0


def present_image(tag: str, bundle: Path, role: str) -> str:
    """`tag`, rebuilt if it has gone since it was built.

    Designs reuse one content-addressed image for hours, and the workspace
    janitor prunes sandbox images by age when the disk fills: a control run
    on 2026-10-03 died at `docker create` with "Unable to find image" for a
    verifier built earlier in the same design. The check is made just before
    every container is created, so the window is seconds, not hours.
    """
    if _image_exists(tag):
        return tag
    return build_image(bundle, role)


def build_image(bundle: Path, role: str, *, log: Path | None = None) -> str:
    """Build (or reuse) the agent (`environment/`) or verifier (`tests/`) image."""
    context = Path(bundle) / ("environment" if role == "agent" else "tests")
    if not (context / "Dockerfile").is_file():
        raise SandboxError(f"{context}/Dockerfile is missing")
    tag = image_tag(context, role)
    if _image_exists(tag):
        return tag
    argv = [DOCKER, "build", "--label", LABEL, "-t", tag, "-f", str(context / "Dockerfile"), str(context)]
    started = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, timeout=BUILD_TIMEOUT_S)
    text = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text)
    if proc.returncode:
        raise SandboxError(f"docker build of {role} image failed after {time.monotonic() - started:.0f}s:\n"
                           + text[-4000:])
    return tag


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

class Container:
    def __init__(self, tag: str, *, user: str | None = None, mounts: list[tuple[Path, str]] = (),
                 workdir: str | None = None, memory: str | None = None, cpus: str | None = None,
                 caps: tuple[str, ...] = ()):
        self.tag = tag
        self.id = None
        argv = [DOCKER, "create", "--network", "none", "--label", LABEL,
                "--name", f"arena-sb-{uuid.uuid4().hex[:12]}", "--cap-drop", "ALL",
                *[a for cap in caps for a in ("--cap-add", cap)],
                "--security-opt", "no-new-privileges"]
        if memory:
            argv += ["--memory", memory]
        if cpus:
            argv += ["--cpus", cpus]
        if user:
            argv += ["-u", user]
        if workdir:
            argv += ["-w", workdir]
        for host, guest in mounts:
            argv += ["-v", f"{host}:{guest}"]
        argv += [tag, "sleep", "infinity"]
        self.id = _run(argv, timeout=120).stdout.decode().strip()
        _run([DOCKER, "start", self.id], timeout=120)

    def exec(self, cmd: str, *, user: str | None = None, env: dict | None = None,
             timeout: float = EXEC_TIMEOUT_S, workdir: str | None = None) -> dict:
        argv = [DOCKER, "exec"]
        if user:
            argv += ["-u", user]
        if workdir:
            argv += ["-w", workdir]
        for key, value in (env or {}).items():
            argv += ["-e", f"{key}={value}"]
        # Not a login shell: `sh -l` re-sources /etc/profile, which resets PATH
        # and hides the image's ENV (the verifier venv). Harbor execs the same way.
        argv += [self.id, "sh", "-c", cmd]
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return {"exit_code": None, "timed_out": True, "seconds": round(time.monotonic() - started, 2),
                    "stdout": (exc.stdout or b"").decode("utf-8", "replace")[-4000:],
                    "stderr": (exc.stderr or b"").decode("utf-8", "replace")[-4000:]}
        return {"exit_code": proc.returncode, "timed_out": False,
                "seconds": round(time.monotonic() - started, 2),
                "stdout": proc.stdout.decode("utf-8", "replace")[-20000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-20000:]}

    def put(self, host: Path, guest: str) -> None:
        _run([DOCKER, "cp", f"{host}/." if Path(host).is_dir() else str(host), f"{self.id}:{guest}"], timeout=300)

    def put_bytes(self, data: bytes, guest: str, mode: int = 0o644) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            temp = Path(handle.name)
        try:
            temp.chmod(mode)
            _run([DOCKER, "cp", str(temp), f"{self.id}:{guest}"], timeout=120)
        finally:
            temp.unlink(missing_ok=True)

    def capture(self, guest_root: str, members: list[str], excludes: list[str], destination: Path) -> dict:
        """Tar `members` under guest_root (excluding names) into destination on the host."""
        argv = [DOCKER, "exec", self.id, "tar", "-cf", "-", "-C", guest_root]
        for name in excludes:
            argv += ["--exclude", name]
        argv += members
        proc = subprocess.run(argv, capture_output=True, timeout=600)
        if proc.returncode:
            raise SandboxError("capture failed: " + proc.stderr.decode("utf-8", "replace")[-1000:])
        destination.mkdir(parents=True, exist_ok=True)
        count = 0
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as stream:
            for member in stream.getmembers():
                parts = Path(member.name).parts
                if member.name.startswith("/") or ".." in parts:
                    raise SandboxError("capture contains an unsafe path")
                if member.issym() or member.islnk():
                    # Symlinks are recorded, not followed: a link is what the
                    # grader will see, and an escaping one is a finding there.
                    stream.extract(member, destination, filter="fully_trusted")
                    continue
                if member.isdir() or member.isfile():
                    stream.extract(member, destination, filter="data")
                    count += member.isfile()
        return {"files": count}

    def stop(self) -> None:
        if self.id:
            subprocess.run([DOCKER, "rm", "-f", self.id], capture_output=True, timeout=120)
            self.id = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def cleanup_stale(max_age_s: int = 6 * 3600) -> int:
    """Remove sandbox containers older than max_age_s (crash residue)."""
    proc = _run([DOCKER, "ps", "-aq", "--filter", f"label={LABEL}"], check=False, timeout=60)
    ids = proc.stdout.decode().split()
    removed = 0
    for cid in ids:
        info = _run([DOCKER, "inspect", "--format", "{{.Created}}", cid], check=False, timeout=60)
        created = info.stdout.decode().strip()
        try:
            from datetime import datetime, timezone
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00")[:26] + "+00:00") \
                if "+" not in created.replace("Z", "") else datetime.fromisoformat(created.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
        except ValueError:
            age = max_age_s + 1
        if age > max_age_s:
            subprocess.run([DOCKER, "rm", "-f", cid], capture_output=True, timeout=120)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# The verifier flow
# ---------------------------------------------------------------------------

def _toolchain(bundle: Path) -> dict:
    return strict_json((Path(bundle) / "environment/toolchain.json").read_text())


def _steps(bundle: Path) -> list[str]:
    import tomllib
    config = tomllib.loads((Path(bundle) / "task.toml").read_text())
    return [s["name"] for s in config.get("steps") or []]


def _excludes(bundle: Path) -> list[str]:
    import tomllib
    config = tomllib.loads((Path(bundle) / "task.toml").read_text())
    names = []
    for entry in config.get("artifacts") or []:
        if isinstance(entry, dict) and entry.get("source") == "/app/repo":
            names = list(entry.get("exclude") or [])
    return names


class VariantApplyError(SandboxError):
    """A patch variant (alternative, mutant) does not apply to the tree its step starts from: a defect
    of the design the designer can repair, never infrastructure (ship15, 2026-10-12: a multi-turn
    alternative written against the reference chain instead of the alternative chain ended a build)."""


class Variant:
    """What to do to the agent container before a step is captured.

    kind: nop | patch | shell | empty | files
    """

    def __init__(self, kind: str = "nop", *, patch: str | None = None, shell: str | None = None,
                 files: dict[str, bytes | None] | None = None, label: str = ""):
        self.kind, self.patch, self.shell, self.files, self.label = kind, patch, shell, files or {}, label

    def apply(self, container: Container, step: str) -> dict:
        if self.kind == "nop":
            return {"applied": "nothing"}
        if self.kind == "patch":
            container.put_bytes((self.patch or "").encode("utf-8"), "/tmp/variant.patch")
            result = container.exec("cd /app/repo && (git apply --whitespace=nowarn /tmp/variant.patch "
                                    "|| patch -p1 --forward < /tmp/variant.patch)", user="agent", timeout=300)
            if result["exit_code"]:
                raise VariantApplyError(f"variant patch {self.label!r} did not apply: {result['stderr'][-1500:]}")
            return {"applied": "patch"}
        if self.kind == "shell":
            result = container.exec(self.shell or "true", user="agent", timeout=EXEC_TIMEOUT_S, workdir="/app/repo")
            return {"applied": "shell", "exit_code": result["exit_code"], "stdout": result["stdout"][-2000:],
                    "stderr": result["stderr"][-2000:]}
        if self.kind == "empty":
            container.exec("find /app/repo -mindepth 1 -maxdepth 1 -exec rm -rf {} + ; "
                           "find /app/deliverables -mindepth 1 -exec rm -rf {} + || true", user="agent", timeout=120)
            return {"applied": "empty"}
        if self.kind == "files":
            for rel, data in self.files.items():
                guest = rel if rel.startswith("/") else f"/app/repo/{rel}"
                if data is None:
                    container.exec(f"rm -rf '{guest}'", user="agent", timeout=60)
                else:
                    container.exec(f"mkdir -p \"$(dirname '{guest}')\"", user="agent", timeout=60)
                    container.put_bytes(data, guest)
                    container.exec(f"chown agent:agent '{guest}' 2>/dev/null || true", user="root", timeout=60)
            return {"applied": f"{len(self.files)} file(s)"}
        raise SandboxError(f"unknown variant kind {self.kind!r}")


def solve_variant(bundle: Path, step: str) -> Variant:
    """The reference solution for a step, as a variant (patch + commands)."""
    solution = Path(bundle) / "steps" / step / "solution"
    patch = (solution / "fix.patch").read_text(encoding="utf-8")
    commands = [line for line in (solution / "solve.sh").read_text().splitlines()
                if line and not line.startswith("#") and not line.startswith("set ")
                and line not in ("cd /app/repo",) and "git apply" not in line and "patch -p1" not in line
                and line.strip() not in ("fi", ":") and not line.startswith("if ")]
    shell = "\n".join(commands).strip()
    if shell:
        return Variant("shell", shell=(
            "cd /app/repo && cat > /tmp/fix.patch <<'ARENA_PATCH_EOF'\n" + patch.rstrip("\n") +
            "\nARENA_PATCH_EOF\n(git apply --whitespace=nowarn /tmp/fix.patch || patch -p1 --forward < /tmp/fix.patch) && "
            + shell), label=f"solve:{step}")
    return Variant("patch", patch=patch, label=f"solve:{step}")


def _grade_capture(bundle: Path, step: str, capture: Path, history: dict[str, Path], verifier_tag: str,
                   limits: dict, step_dir: Path) -> tuple[dict, str]:
    """Grade one captured tree (`capture/repo`, `capture/deliverables`) for `step`.

    The verifier half of `verify`, shared with `grade_capture`: a fresh verifier
    container, the capture uploaded at its original paths, earlier steps'
    captures under /arena-history/<step>/app. Returns the step entry (without
    the variant fields) and the verifier tag actually used.
    """
    verifier_logs = step_dir / "verifier"
    verifier_logs.mkdir(parents=True, exist_ok=True)
    # On a Linux host a bind mount keeps the host owner, and the
    # verifier's root has no DAC_OVERRIDE (VERIFIER_CAPS): it could not
    # create reward.json in a directory the workspace user owns. Docker
    # Desktop's file sharing hid this; the first Coder run of the
    # controls did not (2026-10-03: every variant "verifier produced no
    # reward", `cannot create /logs/verifier/arena-grade.json`).
    verifier_logs.chmod(0o777)
    # The grader runs as root and hands each test process to the
    # unprivileged `grader` user (grade.py); that needs exactly these
    # three capabilities, and nothing else the default set carries.
    verifier_tag = present_image(verifier_tag, bundle, "verifier")
    with Container(verifier_tag, mounts=[(verifier_logs.resolve(), "/logs/verifier")],
                   memory=f"{limits.get('memory_mb', 4096)}m", cpus=str(limits.get("cpus", 2)),
                   caps=VERIFIER_CAPS) as verifier:
        verifier.exec("rm -rf /app/repo /app/deliverables && mkdir -p /app/repo /app/deliverables", timeout=60)
        if (capture / "repo").is_dir():
            verifier.put(capture / "repo", "/app/repo")
        if (capture / "deliverables").is_dir():
            verifier.put(capture / "deliverables", "/app/deliverables")
        if history:
            for earlier, path in history.items():
                if earlier == step:
                    continue
                verifier.exec(f"mkdir -p /arena-history/{earlier}/app", timeout=60)
                verifier.put(path, f"/arena-history/{earlier}/app")
        try:
            run = verifier.exec("sh /tests/test.sh", env={"ARENA_STEP": step},
                                timeout=float(limits.get("verifier_timeout_s", 1800)))
        finally:
            # Hand what the verifier's root wrote back to the host user
            # (CAP_CHOWN is granted), so the host can read, move and
            # delete it. A no-op where the host maps ownership itself.
            if hasattr(os, "getuid"):
                verifier.exec(f"chown -R {os.getuid()}:{os.getgid()} /logs/verifier", timeout=60)
    reward_file = verifier_logs / "reward.json"
    grade_file = verifier_logs / "arena-grade.json"
    entry = {"verifier_exit": run["exit_code"], "timed_out": run["timed_out"],
             "seconds": run["seconds"], "verifier_stderr": run["stderr"][-3000:]}
    if reward_file.is_file() and not run["timed_out"]:
        try:
            entry["reward"] = float(strict_json(reward_file.read_text())["reward"])
            entry["grade"] = strict_json(grade_file.read_text()) if grade_file.is_file() else None
            entry["valid"] = entry["grade"] is not None
        except (ValueError, KeyError, TypeError) as exc:
            entry.update(reward=None, grade=None, valid=False, error=f"unreadable reward: {exc}")
    else:
        entry.update(reward=None, grade=None, valid=False,
                     error="verifier produced no reward" + (" (timed out)" if run["timed_out"] else ""))
    return entry, verifier_tag


def grade_capture(bundle: Path, step: str, capture: Path, *, history: dict[str, Path] | None = None,
                  verifier_tag: str | None = None, work: Path | None = None) -> dict:
    """Re-grade a tree an episode left behind with `bundle`'s current verifier.

    `capture` holds `repo/` (and `deliverables/` when the task declares any) --
    a screen episode's `steps/<s>/artifacts/app`. Nothing runs in an agent
    container: the tree is graded exactly as Harbor's re-upload grades it.
    Used to show a counted failure reproduces (`genuine.audit`) and to measure
    hidden-only hardening against the episodes already run (`fastharden`).
    """
    bundle = Path(bundle)
    limits = _toolchain(bundle).get("limits", {})
    verifier_tag = verifier_tag or build_image(bundle, "verifier")
    step_dir = Path(work or tempfile.mkdtemp(prefix="arena-regrade-")) / step
    step_dir.mkdir(parents=True, exist_ok=True)
    entry, used = _grade_capture(bundle, step, Path(capture), dict(history or {}), verifier_tag, limits, step_dir)
    return {**entry, "verifier_image": used, "work": str(step_dir)}


def verify(bundle: Path, variants: dict[str, Variant] | None = None, *, work: Path | None = None,
           agent_tag: str | None = None, verifier_tag: str | None = None,
           history: bool = True, keep_capture: bool = False) -> dict:
    """Run every step: apply its variant, capture, grade. Returns the trial record.

    `variants` maps step name -> Variant; a missing step gets `nop`. The agent
    container persists across steps, as in Harbor; each step's verifier is a
    fresh container that receives the capture and, for later steps, the
    earlier captures under /arena-history/<step>/app/... (HistoryVerifier).
    """
    bundle = Path(bundle)
    variants = variants or {}
    steps = _steps(bundle)
    excludes = _excludes(bundle)
    chain = _toolchain(bundle)
    limits = chain.get("limits", {})
    agent_tag = agent_tag or build_image(bundle, "agent")
    verifier_tag = verifier_tag or build_image(bundle, "verifier")
    work = Path(work or tempfile.mkdtemp(prefix="arena-verify-"))
    work.mkdir(parents=True, exist_ok=True)
    record = {"schema": "arena-coding/sandbox-trial/1", "bundle": bundle.name, "steps": {},
              "agent_image": agent_tag, "verifier_image": verifier_tag, "work": str(work)}
    captures: dict[str, Path] = {}
    agent_tag = present_image(agent_tag, bundle, "agent")
    with Container(agent_tag, user="agent", workdir="/app/repo",
                   memory=f"{limits.get('memory_mb', 4096)}m", cpus=str(limits.get("cpus", 2))) as agent:
        for step in steps:
            variant = variants.get(step, Variant("nop"))
            step_dir = work / step
            step_dir.mkdir(parents=True, exist_ok=True)
            applied = variant.apply(agent, step)
            capture = step_dir / "artifacts"
            captured = agent.capture("/app", ["repo", "deliverables"], excludes, capture)
            captures[step] = capture
            graded, verifier_tag = _grade_capture(bundle, step, capture, captures if history else {},
                                                  verifier_tag, limits, step_dir)
            entry = {"variant": variant.label or variant.kind, "applied": applied, "captured": captured, **graded}
            record["steps"][step] = entry
    # Captures are the history later steps upload (/arena-history/<step>/app),
    # so they live until every step has been graded. Deleting each one right
    # after its own step broke every second step of a two-step task (real
    # build, 2026-09-27: `docker cp .../01-.../artifacts: no such file`).
    if not keep_capture:
        for capture in captures.values():
            shutil.rmtree(capture, ignore_errors=True)
    rewards = [s["reward"] for s in record["steps"].values() if s.get("valid")]
    record["valid"] = len(rewards) == len(steps)
    record["reward"] = round(sum(rewards) / len(rewards), 4) if record["valid"] and rewards else None
    return record


def shell(bundle: Path, command: str, *, agent_tag: str | None = None, timeout: float = 600,
          setup: Variant | None = None, environment_only: bool = False) -> dict:
    """Run one shell command in a fresh agent container (designer's sandbox_run).

    An explicitly environment-only designer export has no task steps yet.
    Full bundles keep their strict task.toml lookup when applying setup.
    """
    bundle = Path(bundle)
    tag = present_image(agent_tag, bundle, "agent") if agent_tag else build_image(bundle, "agent")
    chain = _toolchain(bundle)
    limits = chain.get("limits", {})
    with Container(tag, user="agent", workdir="/app/repo", memory=f"{limits.get('memory_mb', 4096)}m",
                   cpus=str(limits.get("cpus", 2))) as agent:
        if setup is not None:
            steps = [] if environment_only else _steps(bundle)
            setup.apply(agent, steps[0] if steps else "")
        return agent.exec(command, user="agent", timeout=timeout, workdir="/app/repo")
