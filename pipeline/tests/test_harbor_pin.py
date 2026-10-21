"""The calibration harness pin (Arena weekly sync, 2026-10-22: "Calibrate on Harbor 0.24").

The pin lives in four places that must agree: the runner's EXPECTED_VERSIONS,
the no-deadline entrypoint's own gate, requirements-native.in and its hashed
lock. The native checks run the generated job configs and the subclassed hooks
against the INSTALLED runtime (`.venv-harbor`, or ARENA_HARBOR_BIN), so a Harbor
bump that renames a hook or tightens an options model fails here, not in a
calibration screen.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

from pipeline.benchmark import harbor, unlimited_runtime

ROOT = Path(__file__).resolve().parents[2]


def _pins(text: str) -> dict:
    return {name: version for name, version in
            re.findall(r"^(harbor|litellm)==([0-9][^\s\\]*)", text, re.MULTILINE)}


class PinAgreement(unittest.TestCase):
    def test_the_no_deadline_entrypoint_gates_on_the_runner_pin(self):
        self.assertEqual(unlimited_runtime.PINNED_HARBOR, harbor.EXPECTED_VERSIONS["harbor"])

    def test_requirements_pin_what_the_runner_expects(self):
        expected = dict(harbor.EXPECTED_VERSIONS)
        self.assertEqual(_pins((ROOT / "requirements-native.in").read_text()), expected)
        self.assertEqual(_pins((ROOT / "requirements-native.lock").read_text()), expected)

    def test_the_lock_hashes_every_pin(self):
        lock = (ROOT / "requirements-native.lock").read_text()
        for name, version in harbor.EXPECTED_VERSIONS.items():
            block = re.search(rf"^{name}=={re.escape(version)} \\\n((?:\s+--hash=sha256:[0-9a-f]{{64}}.*\n)+)",
                              lock, re.MULTILINE)
            self.assertIsNotNone(block, f"{name}=={version} carries no sha256 hashes")


#: Run inside the native interpreter: everything a campaign hands Harbor, checked
#: against Harbor's own models and classes.
NATIVE_CHECK = r'''
import inspect, json, sys, tempfile
from pathlib import Path
from importlib.metadata import version
configs = json.loads(sys.stdin.read())
from harbor.agents.factory import AgentFactory
from harbor.models.job.config import JobConfig
from harbor.utils.import_path import import_class
from harbor.environments.docker.docker import DockerEnvironment
from harbor.trial.trial import Trial
from harbor.verifier.verifier import Verifier
from harbor.agents.terminus_2.terminus_2 import Terminus2
from pipeline.benchmark.unlimited_terminus import UnlimitedTerminus2
out = {"versions": {n: version(n) for n in ("harbor", "litellm")}, "problems": []}
for key, raw in configs.items():
    try:
        cfg = JobConfig.model_validate(raw)
        for agent in cfg.agents:
            AgentFactory.run_preflight(agent)
            if agent.name not in ("oracle",):      # the oracle needs the trial's task paths
                with tempfile.TemporaryDirectory() as tmp:
                    AgentFactory.create_agent_from_config(agent, logs_dir=Path(tmp))
        import_class(cfg.environment.import_path, label="environment")
        import_class(cfg.verifier.import_path, label="verifier")
    except Exception as exc:
        out["problems"].append(f"{key}: {type(exc).__name__}: {exc}")
# Every DockerEnvironment member the two providers override or call.
for name in ("_docker_compose_paths", "_run_docker_compose_command", "_requires_egress_control",
             "validate_network_policy_support", "capabilities", "start", "download_dir"):
    if not hasattr(DockerEnvironment, name):
        out["problems"].append(f"DockerEnvironment.{name} is gone")
init = inspect.signature(DockerEnvironment.__init__).parameters
if list(init)[:9] != ["self", "environment_dir", "environment_name", "session_id", "trial_paths",
                      "task_env_config", "keep_containers", "network_policy", "phase_network_policies"]:
    out["problems"].append(f"DockerEnvironment.__init__ order changed: {list(init)[:9]}")
if set(inspect.signature(Trial._resolve_timeout_sec).parameters) != {"self", "base_sec", "max_sec", "multiplier"}:
    out["problems"].append("Trial._resolve_timeout_sec signature changed")
if not {"step_name", "skip_tests_upload"} <= set(inspect.signature(Verifier.__init__).parameters):
    out["problems"].append("Verifier no longer takes step_name/skip_tests_upload")
upstream = inspect.getsource(Terminus2._run_agent_loop).replace(
    "for episode in range(self._max_episodes):", "for episode in count():")
if inspect.getsource(UnlimitedTerminus2._run_agent_loop) != upstream:
    out["problems"].append("UnlimitedTerminus2._run_agent_loop drifted from the installed Terminus2")
print(json.dumps(out))
'''


class NativeRuntime(unittest.TestCase):
    """Executes only where the pinned runtime is installed (Coder, or ARENA_HARBOR_BIN)."""

    def setUp(self):
        self.python = harbor.harbor_binary().parent / "python"
        if not self.python.is_file():
            self.skipTest(f"no native Harbor interpreter at {self.python}")

    def test_job_configs_and_subclassed_hooks_fit_the_installed_harbor(self):
        configs = {}
        for agent, model, no_limits in (("oracle", None, True), ("nop", None, True),
                                        ("claude-code", "claude-fable-5-1", True),
                                        ("codex", "gpt-6-astra", True),
                                        ("terminus-2", "anthropic/claude-other", True),
                                        ("terminus-2", "openai/gpt-other", False)):
            configs[f"{agent}:{no_limits}"] = harbor.job_config(
                Path("/tmp/pkg"), Path("/tmp/out"), model=model, agent=agent, rollouts=2, workers=2,
                effort="high", no_limits=no_limits,
                max_output_tokens=128000 if agent == "terminus-2" else None,
                context_limit=1000000 if agent == "terminus-2" else None)
        env = {"PYTHONPATH": str(ROOT), "ANTHROPIC_API_KEY": "unused", "OPENAI_API_KEY": "unused",
               "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PATH": "/usr/bin:/bin"}
        proc = subprocess.run([str(self.python), "-c", NATIVE_CHECK], input=json.dumps(configs),
                              capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(report["versions"], harbor.EXPECTED_VERSIONS)
        self.assertEqual(report["problems"], [])


if __name__ == "__main__":
    unittest.main()
