"""Docker provider for the calibration harnesses (Claude Code, Codex) on Harbor 0.22.

User, 2026-09-26: Fable 5.1 runs in Claude Code and GPT 6 Astra in Codex. Both
run INSIDE the task container, so -- unlike Terminus-2, which drove an offline
container from outside -- the agent phase needs egress to its model API. This
provider admits exactly that and nothing more:

  * baseline (setup, verifier): no network, as in the static offline protocol;
  * agent phase: Harbor's egress-control allowlist, limited to the harness's
    own API host(s) (`policy.HARNESS_API_HOSTS`, plus a configured base-URL
    host), enforced by Harbor's nftables sidecar;
  * an allowlist is REFUSED when egress control is not active -- it is never
    silently widened to open network, which is what a missing kernel feature
    would otherwise do;
  * the same hardening as the offline provider: not privileged, no published
    ports, no NET_ADMIN/NET_RAW for the task container.

Artifact download keeps the offline provider's safe archive extraction.
"""
from __future__ import annotations
import asyncio
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .offline_docker import OfflineDockerEnvironment

try:
    from harbor.environments.docker.docker import DockerEnvironment
except ImportError:                                   # pragma: no cover -- pinned runtime absent
    DockerEnvironment = OfflineDockerEnvironment.__mro__[1]

#: Hardening without `network_mode: none`: the egress sidecar owns networking.
HARDENING = """services:
  main:
    ports: !reset []
    privileged: false
    cap_drop: [NET_ADMIN, NET_RAW]
"""


def allowed_hosts() -> set[str]:
    """Every host a harness agent phase may reach: pinned API hosts + base-URL hosts."""
    from .. import policy
    hosts = {h for group in policy.HARNESS_API_HOSTS.values() for h in group}
    for variable in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        host = urlparse(os.environ.get(variable) or "").hostname
        if host:
            hosts.add(host)
    return hosts


def harness_hosts(agent: str) -> list[str]:
    """The agent-phase allowlist for one harness (its API host, plus its base-URL host)."""
    from .. import policy
    hosts = list(policy.HARNESS_API_HOSTS[agent])
    variable = {"claude-code": "ANTHROPIC_BASE_URL", "codex": "OPENAI_BASE_URL"}[agent]
    host = urlparse(os.environ.get(variable) or "").hostname
    if host and host not in hosts:
        hosts.append(host)
    return hosts


class HarnessDockerEnvironment(OfflineDockerEnvironment):
    def __init__(self, *args, **kwargs):
        # Not the offline overlay (network_mode none) -- the hardening one.
        self._harness_tmp = tempfile.TemporaryDirectory(prefix="arena-harness-")
        self._harness_path = Path(self._harness_tmp.name) / "hardening.yaml"
        self._harness_path.write_text(HARDENING)
        # DockerEnvironment(environment_dir, environment_name, session_id,
        # trial_paths, task_env_config, keep_containers, network_policy,
        # phase_network_policies, ...): read it by keyword or by position.
        phases = kwargs.get("phase_network_policies")
        if phases is None and len(args) > 7:
            phases = args[7]
        self._phase_policies = list(phases or [])
        DockerEnvironment.__init__(self, *args, **kwargs)
        if self._is_windows_container:
            raise ValueError("the harness provider supports Linux containers only")
        if self._needs_allowlist() and not getattr(self, "_enable_egress_control", False):
            raise RuntimeError("the agent phase needs a model-API allowlist but Docker egress "
                               "control is not available on this host; refusing to run with "
                               "open network")

    # -- network policy ---------------------------------------------------
    @staticmethod
    def _requires_egress_control(**kwargs):
        return DockerEnvironment._requires_egress_control(**kwargs)

    @property
    def capabilities(self):
        return DockerEnvironment.capabilities.fget(self)

    def _needs_allowlist(self) -> bool:
        return any(p.network_mode.value == "allowlist"
                   for p in [self.network_policy, *self._phase_policies])

    def validate_network_policy_support(self, network_policy=None):
        policy = network_policy or self.network_policy
        mode = policy.network_mode.value
        if mode == "no-network" and not policy.allowed_hosts:
            return
        if mode == "allowlist":
            extra = set(policy.allowed_hosts) - allowed_hosts()
            if extra:
                raise ValueError(f"harness provider refuses hosts outside the model APIs: {sorted(extra)}")
            if not getattr(self, "_enable_egress_control", False):
                raise ValueError("an allowlist needs Docker egress control, which is not active")
            return
        raise ValueError(f"harness provider refuses network mode {mode!r}")

    @property
    def _docker_compose_paths(self):
        if self._harness_path.read_text() != HARDENING:
            raise ValueError("harness hardening overlay changed")
        return [*DockerEnvironment._docker_compose_paths.fget(self), self._harness_path]

    # -- isolation proof --------------------------------------------------
    async def start(self, force_build):
        await DockerEnvironment.start(self, force_build)
        result = await self._run_docker_compose_command(["ps", "-q", "main"])
        container = result.stdout.strip()
        if not container or "\n" in container:
            raise RuntimeError("expected exactly one harness task container")
        proc = await asyncio.create_subprocess_exec("docker", "inspect", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode:
            raise RuntimeError("could not inspect the effective harness isolation")
        info = json.loads(stdout)[0]
        host = info["HostConfig"]
        # Docker reports capabilities as NET_ADMIN or CAP_NET_ADMIN by version.
        caps = {str(c).upper().removeprefix("CAP_") for c in host.get("CapDrop") or []}
        problems = [name for name, bad in (
            ("privileged", bool(host.get("Privileged"))),
            (f"published ports {host.get('PortBindings')}", bool(host.get("PortBindings"))),
            (f"NET_ADMIN/NET_RAW not dropped (CapDrop={sorted(caps)})",
             not {"NET_ADMIN", "NET_RAW"} <= caps)) if bad]
        if problems:
            raise RuntimeError("effective Docker hardening does not match the harness protocol: "
                               + "; ".join(problems))
        egress = bool(getattr(self, "_enable_egress_control", False))
        allowlist = sorted({h for p in self._phase_policies for h in p.allowed_hosts})
        if not egress and host.get("NetworkMode") != "none":
            raise RuntimeError("a container without egress control must have no network")
        from .support import write_json
        write_json(self.trial_paths.trial_dir / "isolation" / (info["Id"] + ".json"), {
            "schema": "arena-pipeline/harness-network/1", "container_id": info["Id"],
            "network_mode": host["NetworkMode"], "privileged": host["Privileged"],
            "networks": sorted(info["NetworkSettings"]["Networks"]),
            "port_bindings": host.get("PortBindings"),
            "cap_drop": sorted(caps), "egress_control": egress,
            "baseline": self.network_policy.network_mode.value,
            "agent_phase_allowlist": allowlist})
