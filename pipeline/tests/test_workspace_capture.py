"""The per-step workspace capture Arena v0.1 §12.4 needs: before the agent starts, after it stops."""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from pipeline.benchmark import workspace_capture as wc


class Result:
    def __init__(self, code=0, stdout="", stderr=""):
        self.return_code, self.stdout, self.stderr = code, stdout, stderr


class FakeEnv:
    def __init__(self, code=0, payload=b"tar-bytes"):
        self.calls, self.code, self.payload = [], code, payload

    async def exec(self, command, timeout_sec=None, user=None, **_):
        self.calls.append(("exec", user, command))
        if command.startswith("rm -f"):
            return Result()
        if command.startswith("id -u"):
            return Result(0, "1000\n")
        return Result(self.code, "/app/repo\n", "boom" if self.code else "")

    async def download_file(self, source, target):
        self.calls.append(("download", source, str(target)))
        Path(target).write_bytes(self.payload)


def trial(tmp_path, env, metadata=None):
    return types.SimpleNamespace(
        paths=types.SimpleNamespace(trial_dir=tmp_path),
        task=types.SimpleNamespace(config=types.SimpleNamespace(
            agent=types.SimpleNamespace(user="agent"),
            metadata=metadata if metadata is not None else {"agent_workspace": "/app/repo",
                                                            "deliverables": ["/app/repo"], "protected_paths": []})),
        agent_environment=env)


def test_scope_is_sorted_unique_and_non_overlapping():
    meta = {"agent_workspace": "/app/repo/", "deliverables": ["/app/repo/out.json", "/app/deliverables"],
            "protected_paths": ["/app/data", "relative/ignored"]}
    assert wc.scope_of(meta) == ["/app/data", "/app/deliverables", "/app/repo"]


def test_capture_runs_as_root_keeps_the_tar_and_removes_the_remote_copy(tmp_path):
    env = FakeEnv()
    record = asyncio.run(wc.capture(trial(tmp_path, env), "01-step", "before-agent"))
    folder = tmp_path / wc.FOLDER / "01-step"
    assert record["ok"] and record["present"] == ["/app/repo"] and record["scope"] == ["/app/repo"]
    assert record["agent_user"] == "agent" and record["agent_uid"] == 1000 and isinstance(record["host_uid"], int)
    assert (folder / "before-agent.tar").read_bytes() == b"tar-bytes"
    assert json.loads((folder / "before-agent.json").read_text())["tar_sha256"] == record["tar_sha256"]
    assert all(user == "root" for kind, user, _ in env.calls if kind == "exec")
    assert env.calls[-1][2].startswith("rm -f " + wc.REMOTE)


def test_a_failed_capture_is_recorded_and_never_raised(tmp_path):
    env = FakeEnv(code=2)
    record = asyncio.run(wc.capture(trial(tmp_path, env), "01-step", "after-agent-before-verifier"))
    assert not record["ok"] and "exited 2" in record["error"] and record["stderr"] == "boom"
    assert not (tmp_path / wc.FOLDER / "01-step" / "after-agent-before-verifier.tar").exists()
    assert json.loads((tmp_path / wc.FOLDER / "01-step" / "after-agent-before-verifier.json").read_text())["ok"] is False


def test_no_scope_is_a_recorded_failure(tmp_path):
    record = asyncio.run(wc.capture(trial(tmp_path, FakeEnv(), metadata={}), None, "before-agent"))
    assert not record["ok"] and "agent_workspace" in record["error"]


@pytest.fixture
def fake_harbor(monkeypatch):
    order = []

    class Trial:
        async def _run_agent_phase(self, *, target=None, instruction="", timeout_sec=None, user=None,
                                   step_cfg=None, resume=False, load=False):
            order.append("agent")
            if instruction == "fail":
                raise RuntimeError("agent died")

    module = types.ModuleType("harbor.trial.trial")
    module.Trial = Trial
    for name, mod in (("harbor", types.ModuleType("harbor")), ("harbor.trial", types.ModuleType("harbor.trial")),
                      ("harbor.trial.trial", module)):
        monkeypatch.setitem(sys.modules, name, mod)

    async def fake_capture(trial, step, phase):
        order.append((phase, step))

    monkeypatch.setattr(wc, "capture", fake_capture)
    return Trial, order


def test_install_wraps_the_agent_phase_before_and_after_even_on_failure(fake_harbor):
    Trial, order = fake_harbor
    wc.install()
    wc.install()  # idempotent
    step = types.SimpleNamespace(name="01-step")
    asyncio.run(Trial()._run_agent_phase(target=None, instruction="go", timeout_sec=None, user="agent", step_cfg=step))
    assert order == [("before-agent", "01-step"), "agent", ("after-agent-before-verifier", "01-step")]
    order.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(Trial()._run_agent_phase(target=None, instruction="fail", timeout_sec=None, user="agent",
                                             step_cfg=step))
    assert order == [("before-agent", "01-step"), "agent", ("after-agent-before-verifier", "01-step")]
