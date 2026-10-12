"""Static offline Docker provider for Harbor 0.22, including Docker Desktop.

Only no-network policies are admitted. A final controller-owned Compose overlay
removes every network/port and Docker inspect verifies the effective namespace.
This does not emulate allowlists or silently weaken unsupported network policy.
"""
from __future__ import annotations
import asyncio
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:
    from harbor.environments.docker.docker import DockerEnvironment
except ImportError as exc:
    _IMPORT_ERROR = exc
    class DockerEnvironment:
        def __init__(self, *args, **kwargs):
            raise ImportError("the pinned Harbor runtime is required") from _IMPORT_ERROR

OVERLAY = """services:
  main:
    network_mode: none
    networks: !reset []
    ports: !reset []
    dns: !reset []
    privileged: false
    cap_add: !reset []
    cap_drop: [NET_ADMIN, NET_RAW]
"""


def extract_artifact_archive(archive: Path, destination: Path) -> None:
    """Copy artifact bytes without applying guest read-only modes on the host.

    Docker Desktop's cp extractor applies 0555 directory permissions before
    writing their children. A non-root host then receives an incomplete tree.
    Only the controller's temporary copy is writable; guest fixtures stay
    protected. Refuse links and traversal before writing any archive member.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:") as stream:
        members = stream.getmembers()
        paths = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise ValueError("Artifact archive contains an unsafe entry")
            if path in paths or (not path.parts and not member.isdir()):
                raise ValueError("Artifact archive contains a duplicate or invalid entry")
            paths.add(path)
        for member in members:
            path = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o755)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, path.open("xb") as target:
                    shutil.copyfileobj(source, target)
                path.chmod(0o644 | (member.mode & 0o111))


class OfflineDockerEnvironment(DockerEnvironment):
    def __init__(self, *args, **kwargs):
        self._offline_tmp = tempfile.TemporaryDirectory(prefix="arena-offline-")
        self._offline_path = Path(self._offline_tmp.name) / "offline.yaml"
        self._offline_path.write_text(OVERLAY)
        super().__init__(*args, **kwargs)
        if self._is_windows_container:
            raise ValueError("the static offline provider supports Linux containers only")

    @staticmethod
    def _requires_egress_control(**kwargs):
        return False

    @property
    def capabilities(self):
        return super().capabilities.model_copy(update={"disable_internet": True})

    def validate_network_policy_support(self, network_policy=None):
        policy = network_policy or self.network_policy
        if policy.network_mode.value != "no-network" or policy.allowed_hosts:
            raise ValueError("static offline provider only accepts no-network with no allowed hosts")

    @property
    def _docker_compose_paths(self):
        if self._offline_path.read_text() != OVERLAY:
            raise ValueError("offline control overlay changed")
        return [*super()._docker_compose_paths, self._offline_path]

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        # Stream Docker's archive rather than asking docker compose cp to
        # extract nested read-only directories directly into the host tree.
        result = await self._run_docker_compose_command(["ps", "-q", "main"], check=True)
        container = result.stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{12,64}", container):
            raise RuntimeError("Expected exactly one artifact source container")
        with tempfile.TemporaryDirectory(prefix="arena-artifact-transfer-") as temporary:
            root = Path(temporary)
            archive = root / "artifacts.tar"
            with archive.open("wb") as output:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "cp", f"{container}:{source_dir.rstrip('/')}/.", "-",
                    stdout=output, stderr=asyncio.subprocess.PIPE)
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
                except BaseException:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.communicate()
                    raise
            if proc.returncode:
                raise RuntimeError("Docker artifact download failed: " + stderr.decode(errors="replace")[-2000:])
            staged = root / "contents"
            extract_artifact_archive(archive, staged)
            target = Path(target_dir)
            if target.is_symlink() or (target.exists() and any(p.is_symlink() for p in target.rglob("*"))):
                raise ValueError("Artifact destination contains a symlink")
            shutil.copytree(staged, target, dirs_exist_ok=True)

    async def start(self, force_build):
        await super().start(force_build)
        result = await self._run_docker_compose_command(["ps", "-q", "main"])
        container = result.stdout.strip()
        if not container or "\n" in container:
            raise RuntimeError("expected exactly one offline task container")
        proc = await asyncio.create_subprocess_exec("docker", "inspect", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode:
            raise RuntimeError("could not inspect the effective network isolation")
        info = json.loads(stdout)[0]
        host = info["HostConfig"]
        if (host.get("NetworkMode") != "none" or host.get("Privileged")
                or host.get("PortBindings") or set(info["NetworkSettings"]["Networks"]) != {"none"}):
            raise RuntimeError("effective Docker isolation does not match the static offline protocol")
        # No shell output or solver-accessible files are used as proof.
        from .support import write_json
        write_json(self.trial_paths.trial_dir / "isolation" / (info["Id"] + ".json"), {
            "schema": "arena-pipeline/offline-network/1", "container_id": info["Id"],
            "network_mode": host["NetworkMode"], "privileged": host["Privileged"],
            "networks": sorted(info["NetworkSettings"]["Networks"]),
            "port_bindings": host.get("PortBindings")})
