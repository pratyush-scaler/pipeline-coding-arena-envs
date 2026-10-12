"""Run complete task environments through pinned Harbor, without changing their graders.

This is a new, stock-Terminus comparison protocol. The archived custom agent and
Docker adapter are unavailable, so historical scores are not interchangeable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import tomllib
from pathlib import Path, PurePosixPath

from .. import policy
from ..author.cli_process import _run_process
from .support import copy_tree, digest, fingerprint, redact, write_json, task_paths

EXPECTED_VERSIONS = {"harbor": "0.22.0", "litellm": "1.99.0"}
PROTOCOL = "arena-harbor-harness/4-step-history"
PROTOCOL_NOTE = ("Calibration models in their assigned in-container harnesses (user, 2026-09-26): "
                 "Fable 5.1 in Claude Code, GPT 6 Astra in Codex, pinned CLI versions baked into "
                 "the task image, reasoning effort high, no turn or time limits. The agent phase "
                 "may reach only its model API host (Harbor egress-control allowlist); setup and "
                 "the separate verifiers stay offline, with controller-owned prior-step artifact "
                 "history. Terminus-2 measurements (protocol 3) are not comparable.")

#: The in-container calibration harnesses, and every agent that is a model solver.
HARNESS_AGENTS = frozenset({"claude-code", "codex"})
SOLVER_AGENTS = HARNESS_AGENTS | {"terminus-2"}
HARNESS_PROVIDER = "pipeline.benchmark.harness_docker:HarnessDockerEnvironment"
OFFLINE_PROVIDER = "pipeline.benchmark.offline_docker:OfflineDockerEnvironment"


def provider_for(agent: str) -> str:
    """The Docker provider an agent's campaign must run under."""
    return HARNESS_PROVIDER if agent in HARNESS_AGENTS else OFFLINE_PROVIDER
LIFECYCLE_FILENAME = "native-lifecycle.compose.yaml"
#: Bound the trial container. PLAYBOOK §9: size resources to the measured peak,
#: not the average -- one verifier parsing a large payload peaked at ~5x the
#: document size and was OOM-killed with no exception, no output and no score
#: file, AFTER the agent had run. Unbounded, that failure is a silent void; the
#: run summary counts it correctly but the cause is never bounded.
#:
#: Set from the upstream fork's own sampling -- 371.91 MiB working set and 1.762
#: cores across owned containers -- with headroom, and overridable because a
#: ceiling fitted to one corpus is a guess about the next.
MEM_LIMIT = os.environ.get("ARENA_HARBOR_MEM", "4g")
CPU_LIMIT = os.environ.get("ARENA_HARBOR_CPUS", "4")

_LIFECYCLE_BYTES = (b"services:\n  main:\n    init: true\n"
                    b'    command: ["sh", "-c", "exec sleep infinity"]\n'
                    + f"    mem_limit: {MEM_LIMIT}\n".encode()
                    + f"    cpus: {CPU_LIMIT}\n".encode())
HISTORY_VERIFIER = "pipeline.benchmark.history_verifier:HistoryVerifier"
#: The image a task package builds on. Recorded per run so two runs can
#: be compared, and overridable for a pinned digest reference.
from ..author.toolchain import IMAGES as _IMAGES
DEFAULT_BASE_IMAGE = _IMAGES['python']


def _history_adapter() -> dict:
    root = Path(__file__).resolve().parents[2]
    paths = [root / "pipeline/benchmark" / name for name in ("history_verifier.py", "step_history.py", "offline_docker.py",
                                                           "harness_docker.py",
                                                           "unlimited_runtime.py", "unlimited_terminus.py",
                                                           "support.py", "preflight.py")]
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Prior-step verifier adapter must use its pinned controller modules")
    return {"schema": "arena-factory/harbor-step-history-adapter/1", "import_path": HISTORY_VERIFIER,
            "pythonpath": str(root), "history_root": "/arena-history",
            "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                      for path in paths]}


def _lifecycle_bytes(ownership: dict | None = None) -> bytes:
    if ownership is None:
        return _LIFECYCLE_BYTES
    from ..author.native_ownership import resource_labels
    labels = resource_labels(ownership)
    # The admitted native Linux/no-network protocol always uses this sidecar.
    # Labels do not exempt a service from Harbor's later egress routing overlay.
    return (json.dumps({"services": {
        "main": {"init": True, "command": ["sh", "-c", "exec sleep infinity"],
                 "labels": labels, "mem_limit": MEM_LIMIT, "cpus": CPU_LIMIT}},
        "networks": {"default": {"labels": labels}}}, sort_keys=True, indent=2) + "\n").encode()


def _lifecycle_record(output: Path, ownership: dict | None = None) -> dict:
    """Only this fixed controller file may alter native idle-process handling."""
    path = output / LIFECYCLE_FILENAME
    expected = _lifecycle_bytes(ownership)
    if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
        raise ValueError("Native lifecycle overlay changed or is not a regular controller file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise ValueError("Native lifecycle overlay permissions changed")
    return {"schema": "arena-factory/harbor-lifecycle/1", "path": str(path),
            "sha256": hashlib.sha256(expected).hexdigest(),
            "bytes": len(expected), "mode": mode}


def _stage_lifecycle(output: Path, ownership: dict | None = None) -> dict:
    path = output / LIFECYCLE_FILENAME
    with path.open("xb") as stream:
        stream.write(_lifecycle_bytes(ownership))
    path.chmod(0o600)
    return _lifecycle_record(output, ownership)


def _validate_runtime_controls(output: Path, lifecycle: dict, config: dict, history: dict,
                               ownership: dict | None = None) -> None:
    if _lifecycle_record(output, ownership) != lifecycle:
        raise ValueError("Native lifecycle overlay identity changed")
    path = output / "harbor-job.json"
    if path.is_symlink() or json.loads(path.read_text()) != config:
        raise ValueError("Native Harbor job configuration changed")
    if _history_adapter() != history:
        raise ValueError("Prior-step verifier adapter changed during the native run")


def harbor_binary() -> Path:
    return Path(os.environ.get("ARENA_HARBOR_BIN", str(
        Path(__file__).resolve().parents[2] / ".venv-harbor/bin/harbor"))).absolute()


class _RuntimeUnavailable(Exception):
    """Harbor is absent, so there is no runtime to hash -- not itself an error."""


def doctor(*, check_docker: bool = True) -> dict:
    """Read runtime versions and source hashes. Does not start tasks or model calls."""
    binary = harbor_binary()
    errors = []
    result = {"binary": str(binary), "expected_versions": EXPECTED_VERSIONS,
              "protocol": PROTOCOL, "protocol_note": PROTOCOL_NOTE}
    # A missing Harbor used to return here, which skipped the Docker probes
    # below -- so on a box with Docker but no Harbor the report said nothing
    # about Docker, and a second run was needed to learn the second problem.
    # Record the fault, skip only the runtime inspection it blocks, and let the
    # rest of the checks run so one report lists everything that is wrong.
    python = binary.parent / "python"
    runtime_ready = True
    if not binary.is_file():
        errors.append(f"Harbor executable missing: {binary}")
        runtime_ready = False
    elif not python.is_file():
        errors.append(f"Harbor Python missing: {python}")
        runtime_ready = False
    script = """
import hashlib, importlib.metadata as m, json, pathlib, sys
import harbor
root=pathlib.Path(next(iter(harbor.__path__)))
files=[]
for name, package_root in [('harbor',root),('litellm',pathlib.Path(m.distribution('litellm').locate_file('litellm')))]:
 for p in sorted(package_root.rglob('*')):
  if p.is_file() and '__pycache__' not in p.parts:
   files.append([name+'/'+str(p.relative_to(package_root)),hashlib.sha256(p.read_bytes()).hexdigest()])
versions={name:m.version(name) for name in ('harbor','litellm')}
dependencies={d.metadata['Name']:d.version for d in m.distributions() if d.metadata['Name']}
print(json.dumps({'versions':versions,'python':sys.version,'package_root':str(root),
 'dependency_versions':dependencies,
 'runtime_sha256':hashlib.sha256(json.dumps([files,dependencies,sys.version],sort_keys=True).encode()).hexdigest(),
 'runtime_file_count':len(files)}))
"""
    try:
        if not runtime_ready:
            raise _RuntimeUnavailable
        proc = subprocess.run([str(python), "-c", script], text=True, capture_output=True,
                              timeout=45, check=True)
        result.update(json.loads(proc.stdout))
        result["version"] = result["versions"].get("harbor")
        for name, expected in EXPECTED_VERSIONS.items():
            if result["versions"].get(name) != expected:
                errors.append(f"Expected {name}=={expected}; found {result['versions'].get(name)}")
        result["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    except _RuntimeUnavailable:
        pass  # already reported above; nothing to hash
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        errors.append(f"Cannot inspect Harbor runtime: {redact(str(exc))}")
    if check_docker:
        for label, command in [("docker", ["docker", "version", "--format", "{{.Server.Version}}"]),
                               ("compose", ["docker", "compose", "version", "--short"])]:
            try:
                probe = subprocess.run(command, capture_output=True, text=True,
                                       timeout=15, check=True)
                result[label + "_version"] = probe.stdout.strip()
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{label} unavailable: {redact(str(exc))}")
        # Image provenance. PLAYBOOK §9: pin images by digest, never a mutable
        # tag, or runs are not comparable across time. Harbor builds the task
        # image from the package, so there is no tag for us to pin at config
        # time -- what we can do is RECORD what was actually used, so a later
        # run that silently sat on a rebuilt base is visible rather than
        # invisible. Everything else here is hashed; the image was not.
        try:
            probe = subprocess.run(
                ["docker", "image", "inspect", "--format",
                 "{{.Id}} {{index .RepoDigests 0}}", DEFAULT_BASE_IMAGE],
                capture_output=True, text=True, timeout=15)
            if probe.returncode == 0 and probe.stdout.strip():
                image_id, _, repo_digest = probe.stdout.strip().partition(" ")
                result["base_image"] = {"reference": DEFAULT_BASE_IMAGE,
                                        "id": image_id,
                                        "repo_digest": repo_digest or None}
            else:
                result["base_image"] = {"reference": DEFAULT_BASE_IMAGE,
                                        "note": "not present locally; pulled at "
                                                "build time and therefore unpinned"}
        except (OSError, subprocess.SubprocessError) as exc:
            result["base_image"] = {"reference": DEFAULT_BASE_IMAGE,
                                    "note": f"uninspectable: {redact(str(exc))}"}
    result.update(ok=not errors, errors=errors)
    return result


def inspect_package(bundle: Path) -> dict:
    """Require the original offline/separate-verifier task contract before execution."""
    bundle = Path(bundle)
    fingerprint(bundle, task_only=True)  # Validate the published Docker/task surfaces.
    config = tomllib.loads((bundle / "task.toml").read_text())
    if config.get("schema_version") != "1.4":
        raise ValueError("Harbor benchmark packages must declare schema_version = '1.4'")
    env, agent, verifier = (config.get(k, {}) for k in ("environment", "agent", "verifier"))
    _environment_policy(env, "Task environment")
    if agent.get("network_mode", "no-network") != "no-network":
        raise ValueError("Agent network must remain no-network")
    if verifier.get("environment_mode") != "separate":
        raise ValueError("Task must use a separate verifier")
    if verifier.get("network_mode", "no-network") != "no-network":
        raise ValueError("Verifier network must remain no-network")
    ve = verifier.get("environment", {})
    if "environment" in verifier:
        # An explicit environment REPLACES the inherited one. In Harbor an
        # omitted network_mode here defaults to PUBLIC, not its parent's mode.
        _environment_policy(ve, "Verifier environment")
    for folder in ("environment", "tests"):
        if not (bundle / folder / "Dockerfile").is_file():
            raise ValueError(f"Missing {folder}/Dockerfile")
    # Native Harbor respects task-authored Compose networking. An explicit task
    # override can bypass its egress sidecar even if task.toml says no-network.
    # This protocol only admits the one-environment Dockerfile packages we audit.
    # Source files under environment/repo are inert fixture bytes, even
    # when a recovered repository contains Compose files. Harbor only loads its
    # environment-level configuration; keep rejecting overrides elsewhere in
    # the task surfaces. fingerprint above still checks every fixture for links.
    fixture = bundle / "environment/repo"
    if (any(bundle.glob("docker-compose*.y*ml"))
            or any(path.match("docker-compose*.y*ml") and not path.is_relative_to(fixture)
                   for path in task_paths(bundle))):
        raise ValueError("Task-supplied Docker Compose overrides are outside this offline protocol")
    if not (bundle / "tests/test.sh").is_file():
        raise ValueError("Missing tests/test.sh")
    steps = config.get("steps", [])
    names = []
    for step in steps:
        name = step.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in names:
            raise ValueError(f"Invalid or repeated step name: {name!r}")
        if not (bundle / "steps" / name / "instruction.md").is_file():
            raise ValueError(f"Missing instruction for step {name}")
        if "min_reward" in step:
            raise ValueError("Do not skip later steps through min_reward thresholds")
        for phase in ("agent", "verifier"):
            if step.get(phase, {}).get("network_mode", "no-network") != "no-network":
                raise ValueError(f"Step {name} changes the offline network policy")
        sv = step.get("verifier", {})
        if sv.get("environment_mode", "separate") != "separate":
            raise ValueError(f"Step {name} changes the separate verifier policy")
        if "environment" in sv:
            _environment_policy(sv["environment"], f"Step {name} verifier environment")
        _artifact_policy(step.get("artifacts", []), f"Step {name} artifacts")
        names.append(name)
    _artifact_policy(config.get("artifacts", []), "Task artifacts")
    if not names and not (bundle / "instruction.md").is_file():
        raise ValueError("Missing instruction.md")
    if not config.get("artifacts") and (not steps or not all(step.get("artifacts") for step in steps)):
        raise ValueError("Declare deliverable artifacts for the separate verifier")
    if len(steps) > 1 and config.get("multi_step_reward_strategy") not in ("mean", "final"):
        raise ValueError("Declare multi_step_reward_strategy explicitly")
    return {"step_names": names, "multi_step_reward_strategy": config.get("multi_step_reward_strategy"),
            "task_name": config.get("task", {}).get("name", bundle.name)}


def _environment_policy(environment: dict, label: str) -> None:
    if not isinstance(environment, dict):
        raise ValueError(f"{label} must be a configuration table")
    if environment.get("os", "linux") != "linux":
        raise ValueError(f"{label} must use Linux")
    if environment.get("network_mode") != "no-network":
        raise ValueError(f"{label} must explicitly declare network_mode='no-network'")
    if environment.get("docker_image") is not None:
        # Even step-level image overrides bypass that step's published build
        # context. Preserve the exact checked grader and task Dockerfiles.
        raise ValueError(f"{label} must use its package Dockerfile, not a prebuilt image override")
    if environment.get("allowed_hosts"):
        raise ValueError(f"{label} cannot add network hosts to this offline protocol")


def _artifact_policy(artifacts: list, label: str) -> None:
    if not isinstance(artifacts, list):
        raise ValueError(f"{label} must be a list")
    for artifact in artifacts:
        source = artifact.get("source") if isinstance(artifact, dict) else artifact
        if isinstance(artifact, dict) and artifact.get("service", "main") != "main":
            raise ValueError(f"{label} may collect only the task's main service")
        path = PurePosixPath(source) if isinstance(source, str) else None
        # Harbor restores artifacts at their source paths in the verifier. A
        # transfer to /tests, /logs, /tmp, /usr, etc. could replace grader code,
        # rewards or its runtime. This protocol publishes work only under /app;
        # broad root/directory artifacts and traversal must not cross that bound.
        if (path is None or not path.is_absolute() or ".." in path.parts
                or len(path.parts) < 2 or path.parts[1] != "app" or "\\" in source):
            raise ValueError(f"{label} source {source!r} must stay inside /app; verifier/runtime paths are protected")


def job_config(snapshot: Path, output: Path, *, model: str | None, agent: str,
               rollouts: int, workers: int, effort: str, no_limits: bool = False,
               max_output_tokens: int | None = None, context_limit: int | None = None) -> dict:
    settings = {"name": "oracle" if agent == "fixture" else agent}
    if agent in HARNESS_AGENTS:
        if not model:
            raise ValueError(f"A benchmark model is required for {agent}")
        if policy.harness_for(model) != agent:
            raise ValueError(f"{model} is calibrated in {policy.harness_for(model)}, not {agent}")
        from .harness_docker import harness_hosts
        # No max_turns: the harness CLIs run until the model stops (SPEC_NO_LIMITS).
        # `version` makes Harbor verify the baked-in CLI and skip its run-time
        # installer, which would need internet the agent phase does not have.
        settings.update(model_name=resolve_model(model),
                        kwargs={"reasoning_effort": effort,
                                "version": policy.HARNESS_VERSIONS[agent]},
                        resume_trajectory=False,
                        extra_allowed_hosts=harness_hosts(agent))
    elif agent == "terminus-2":
        if not model:
            raise ValueError("A benchmark model is required for Terminus-2")
        settings.update(model_name=resolve_model(model), kwargs={"reasoning_effort": effort,
                        "max_turns": None if no_limits else 40, "store_all_messages": True,
                        # Token-ID/logprob collection sends vLLM-only parameters
                        # that Anthropic rejects. Full messages are retained above.
                        "collect_rollout_details": False,
                        "use_responses_api": resolve_model(model).startswith("openai/"),
                        "record_terminal_session": False}, resume_trajectory=False)
        if no_limits:
            settings.update(name=None, import_path="pipeline.benchmark.unlimited_terminus:UnlimitedTerminus2")
            settings["kwargs"]["llm_kwargs"] = {"timeout": None}
        model_info = {}
        if context_limit is not None:
            model_info["max_input_tokens"] = context_limit
        if max_output_tokens is not None:
            model_info["max_output_tokens"] = max_output_tokens
            token_key = "max_output_tokens" if resolve_model(model).startswith("openai/") else "max_tokens"
            settings["kwargs"]["llm_call_kwargs"] = {token_key: max_output_tokens}
        if model_info:
            settings["kwargs"]["model_info"] = model_info
    return {"job_name": "run", "jobs_dir": str(output / "jobs"),
            "n_attempts": rollouts, "n_concurrent_trials": workers, "quiet": True,
            "retry": {"max_retries": 0}, "environment": {"type": "docker", "delete": True,
                "import_path": provider_for(agent),
                # Harbor 0.22 clears the typed overlay field for separate verifiers,
                # but preserves these documented environment constructor kwargs.
                "kwargs": {"extra_docker_compose": [str(output / LIFECYCLE_FILENAME)]}},
            "verifier": {"import_path": HISTORY_VERIFIER, "kwargs": {"skip_tests_upload": True}},
            "agents": [settings], "tasks": [{"path": str(snapshot)}]}


def resolve_model(model: str) -> str:
    """Make provider routing explicit while preserving already-qualified IDs."""
    if not isinstance(model, str) or not model or any(char.isspace() or ord(char) < 32 for char in model):
        raise ValueError("Model must be a nonempty ID without whitespace")
    if "/" in model:
        provider, name = model.split("/", 1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", provider) or not name or any(
                part in {"", ".", ".."} for part in name.split("/")):
            raise ValueError("Expected an explicit provider/model ID")
        return model
    if model.startswith("claude-"):
        return "anthropic/" + model
    if model.startswith("gpt-") or re.match(r"o[134](?:-|$)", model):
        return "openai/" + model
    if model.startswith("kimi-"):
        return "moonshot/" + model
    raise ValueError(f"Unknown bare model ID {model!r}; specify its provider/model routing explicitly")


def _fixture_overlay(snapshot: Path, fixture: Path, steps: list[str]) -> None:
    fingerprint(fixture)
    for path in [snapshot / "solution", *[snapshot / "steps" / name / "solution" for name in steps]]:
        if path.exists():
            shutil.rmtree(path)
    if steps:
        for name in steps:
            source = fixture / "steps" / name / "solution"
            if not (source / "solve.sh").is_file():
                raise ValueError(f"Fixture missing steps/{name}/solution/solve.sh")
            copy_tree(source, snapshot / "steps" / name / "solution")
    else:
        source = fixture / "solution"
        if not (source / "solve.sh").is_file():
            raise ValueError("Fixture missing solution/solve.sh")
        copy_tree(source, snapshot / "solution")


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def file_modes(root: Path) -> dict:
    """Record execution-relevant file and directory permissions separately from bytes."""
    entries = [{"path": str(path.relative_to(root)), "mode": stat.S_IMODE(path.stat().st_mode)}
               for path in task_paths(root)]
    return {"sha256": digest(entries), "entries": entries}


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _evidence_metadata(record: dict, label: str) -> list[str]:
    errors = []
    for field in ("error", "exception"):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or value.strip()):
            errors.append(f"{label} has an error or malformed {field} field: {value!r}")
    skipped = record.get("skipped", False)
    if not isinstance(skipped, bool) or skipped:
        errors.append(f"{label} was skipped or has malformed skipped metadata")
    status = record.get("status")
    if status is not None and (not isinstance(status, str) or status.strip().lower() not in {
            "ok", "success", "passed", "failed", "pass", "fail", "complete", "completed", "evaluated"}):
        errors.append(f"{label} is skipped, missing, erroneous or has an unknown status: {status!r}")
    return errors


def _details(path: Path) -> tuple[dict, list[str]]:
    """Read RewardKit 0.2 leaves and groups without losing criterion identity.

    A group's ``$checks`` component is its local programmatic bucket, so it
    keeps the flat ``dimension/criterion`` IDs. Named children add their path.
    RewardKit can also emit an anonymous list of leaf results for a dimension;
    those share a namespace and duplicate criterion names remain invalid.
    """
    if not path.is_file():
        return {}, [f"Missing per-check details: {path.name}"]
    checks, errors = {}, []

    def name_ok(value: object) -> bool:
        return (isinstance(value, str) and bool(value.strip()) and value not in {".", ".."}
                and not any(char in value for char in "/\\")
                and not any(ord(char) < 32 for char in value))

    def metadata(record: dict, label: str, *, require_score: bool = False) -> None:
        errors.extend(_evidence_metadata(record, label))
        if (require_score or "score" in record) and (
                not _number(record.get("score")) or not 0 <= record["score"] <= 1):
            errors.append(f"Invalid dimension score: {label}")

    def visit(block: object, dimension: str, location: str) -> None:
        if isinstance(block, list):
            if not block:
                errors.append(f"Empty criterion result list: {location}")
            for index, leaf in enumerate(block):
                if (not isinstance(leaf, dict) or leaf.get("kind") == "group"
                        or "components" in leaf):
                    errors.append(f"Invalid criterion result list entry: {location}[{index}]")
                    continue
                visit(leaf, dimension, f"{location}[{index}]")
            return
        if not isinstance(block, dict):
            errors.append(f"Invalid criterion block: {location}")
            return
        group = block.get("kind") == "group" or "components" in block
        metadata(block, f"Dimension {location}", require_score=group)
        if group:
            components = block.get("components")
            if (block.get("kind") != "group" or "criteria" in block
                    or not isinstance(components, list) or not components):
                errors.append(f"Invalid criterion group: {location}")
                return
            if not isinstance(block.get("aggregation"), str) or block["aggregation"] not in {
                    "weighted_mean", "all_pass", "any_pass", "threshold", "required_pass"}:
                errors.append(f"Invalid group aggregation: {location}")
            if "threshold" in block and not _number(block["threshold"]):
                errors.append(f"Invalid group threshold: {location}")
            names = set()
            for index, component in enumerate(components):
                label = f"{location}.components[{index}]"
                if (not isinstance(component, dict) or not name_ok(component.get("name"))
                        or "detail" not in component or "criteria" in component or "components" in component):
                    errors.append(f"Invalid criterion component: {label}")
                    continue
                metadata(component, f"Component {label}")
                name = component["name"]
                if name in names:
                    errors.append(f"Duplicate criterion component: {location}/{name}")
                names.add(name)
                if not _number(component.get("weight")) or component["weight"] < 0:
                    errors.append(f"Invalid component weight: {location}/{name}")
                child = component["detail"]
                if name == "$checks" and (not isinstance(child, dict)
                        or child.get("kind") == "group" or "components" in child):
                    errors.append(f"Invalid local criterion bucket: {location}/{name}")
                    continue
                child_dimension = dimension if name == "$checks" else f"{dimension}/{name}"
                visit(child, child_dimension, f"{location}/{name}")
            return
        criteria = block.get("criteria")
        if (not isinstance(criteria, list) or not criteria
                or ("kind" in block and (not isinstance(block["kind"], str)
                    or block["kind"] not in {"programmatic", "llm", "agent"}))):
            errors.append(f"Invalid criterion block: {location}")
            return
        for criterion in criteria:
            if not isinstance(criterion, dict) or not name_ok(criterion.get("name")):
                errors.append(f"Invalid criterion entry: {location}")
                continue
            key = f"{dimension}/{criterion['name']}"
            value = criterion.get("value")
            if key in checks:
                errors.append(f"Duplicate criterion: {key}")
            if not _number(value) or not 0 <= value <= 1:
                errors.append(f"Invalid criterion value: {key}")
            errors.extend(_evidence_metadata(criterion, f"Criterion {key}"))
            checks.setdefault(key, {"value": value if _number(value) else None,
                                    "gated_out": criterion.get("gated_out", False),
                                    "error": criterion.get("error") or criterion.get("exception"),
                                    "skipped": criterion.get("skipped", False),
                                    "status": criterion.get("status")})

    try:
        data = json.loads(path.read_text(), object_pairs_hook=_unique_json_keys)
        if not isinstance(data, dict) or not data:
            raise ValueError("expected nonempty dimension mapping")
        for dimension, block in data.items():
            if not name_ok(dimension):
                errors.append(f"Invalid dimension name: {dimension!r}")
                continue
            visit(block, dimension, dimension)
        if not checks:
            errors.append("No per-check results")
    except (ValueError, OSError, RecursionError) as exc:
        errors.append(f"Unreadable per-check details: {exc}")
    return checks, errors


def harness_transcript_steps(folder: Path) -> int:
    """Agent turns in a harness step's ATIF transcript (0 when absent or unreadable)."""
    path = Path(folder) / "agent/trajectory.json"
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    return sum(1 for turn in data.get("steps") or []
               if isinstance(turn, dict) and turn.get("source") == "agent")


def _step_result(data: dict, folder: Path, name: str, *, oracle: bool, solver: bool = False,
                 max_turns: int | None = 40, harness: bool = False) -> dict:
    errors = []
    if not isinstance(data, dict):
        return {"name": name, "valid": False, "reward": None, "checks": {},
                "agent_exit_code": None, "errors": ["Invalid step record"],
                "agent_result": None, "evidence_dir": str(folder)}
    if data.get("exception_info"):
        errors.append(f"Step exception: {data['exception_info']}")
    if solver and harness:
        # An in-container harness leaves an ATIF transcript. A step without
        # one measured nothing: void, never a zero.
        if harness_transcript_steps(folder) <= 0:
            errors.append("Missing or empty native harness transcript")
    elif solver:
        metadata = (data.get("agent_result") or {}).get("metadata") or {}
        turns = metadata.get("n_episodes")
        if (type(turns) is not int or turns <= 0 or
                (max_turns is not None and turns >= max_turns) or not metadata.get("all_messages")):
            errors.append("Stalled, capped or missing native solver transcript")
    verifier = data.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    if not _number(reward) or not 0 <= reward <= 1:
        errors.append("Missing, nonfinite or out-of-range step reward")
    checks, detail_errors = _details(folder / "verifier/reward-details.json")
    errors.extend(detail_errors)
    code_path = folder / "agent/exit-code.txt"
    code = None
    if oracle and code_path.exists():
        try:
            code = int(code_path.read_text().strip())
            if code != 0:
                errors.append(f"Oracle script exited with code {code}")
        except (ValueError, OSError):
            errors.append("Invalid oracle exit-code record")
    # An in-container harness writes no `n_episodes`; its turn count is the
    # ATIF transcript's. Without it every harness episode read as "no tool
    # calls" and attribution voided all of them (screen 2026-09-26).
    turns = harness_transcript_steps(folder) if solver and harness else None
    return {"name": name, "valid": not errors, "reward": reward if _number(reward) else None,
            "checks": checks, "agent_exit_code": code, "errors": errors,
            "agent_result": data.get("agent_result"), "evidence_dir": str(folder),
            "agent_turns": turns}


def parse_trial(path: Path, step_names: list[str], *, oracle: bool = False,
                strategy: str = "mean", solver: bool = False, max_turns: int | None = 40,
                harness: bool = False) -> dict:
    """Missing or aborted steps invalidate a trial, never silently become zeros."""
    folder = path.parent
    result = {"id": folder.name, "valid": False, "reward": None, "steps": [],
              "result_file": str(path), "errors": []}
    try:
        data = json.loads(path.read_text(), object_pairs_hook=_unique_json_keys)
        if not isinstance(data, dict):
            raise ValueError("expected trial object")
        result["id"] = data.get("trial_name", folder.name)
        if data.get("exception_info"):
            result["errors"].append(f"Trial exception: {data['exception_info']}")
        raw_steps = data.get("step_results")
        if step_names:
            if not isinstance(raw_steps, list):
                raw_steps = []
            observed = [s.get("step_name") for s in raw_steps if isinstance(s, dict)]
            if observed != step_names:
                result["errors"].append(f"Expected steps {step_names}; got {observed}")
            by_name = {s.get("step_name"): s for s in raw_steps if isinstance(s, dict)}
            for name in step_names:
                result["steps"].append(_step_result(by_name.get(name, {}), folder / "steps" / name,
                                                     name, oracle=oracle, solver=solver, max_turns=max_turns,
                                                     harness=harness))
        else:
            result["steps"] = [_step_result(data, folder, "single", oracle=oracle, solver=solver,
                                            max_turns=max_turns, harness=harness)]
        reward = (data.get("verifier_result") or {}).get("rewards", {}).get("reward")
        if not _number(reward) or not 0 <= reward <= 1:
            result["errors"].append("Missing, nonfinite or out-of-range aggregate reward")
        result["reward"] = reward if _number(reward) else None
        if step_names and all(s["valid"] for s in result["steps"]) and _number(reward):
            values = [s["reward"] for s in result["steps"]]
            expected = values[-1] if strategy == "final" else sum(values) / len(values)
            if not math.isclose(reward, expected, abs_tol=1e-8):
                result["errors"].append("Aggregate reward disagrees with complete step rewards")
        result["valid"] = not result["errors"] and all(s["valid"] for s in result["steps"])
    except (ValueError, OSError, AttributeError, TypeError) as exc:
        result["errors"].append(f"Cannot read trial result: {exc}")
    result["zero_class"] = "void" if not result["valid"] else "model_failure" if solver and result["reward"] == 0 else None
    return result


def summarize(trials: list[dict], requested: int) -> dict:
    valid = [t for t in trials if t["valid"]]
    steps = {}
    for trial in valid:
        for step in trial["steps"]:
            steps.setdefault(step["name"], []).append(step["reward"])
    return {"requested": requested, "recorded": len(trials), "valid": len(valid),
            "void": max(0, requested - len(valid)),
            "mean_reward": sum(t["reward"] for t in valid) / len(valid) if valid else None,
            "step_means_from_complete_trials": {name: {"n": len(values),
                 "mean_reward": sum(values) / len(values)} for name, values in steps.items()}}


def _child_environment(agent: str, model: str | None) -> dict[str, str]:
    """Adapt credentials for the pinned native runtime without changing the host."""
    from ..author.native_ownership import ENV as native_resource_env
    env = dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True")
    # Registration belongs to the host wrapper, not to the native solver.
    env.pop(native_resource_env, None)
    if agent not in SOLVER_AGENTS:
        for key in list(env):
            if key.endswith(("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "SECRET_KEY")):
                env.pop(key)
    elif agent == "claude-code":
        token = env.get("ANTHROPIC_AUTH_TOKEN") or ""
        if token.startswith("sk-ant-oat"):
            # An OAuth token is Claude Code's subscription credential: Harbor
            # passes CLAUDE_CODE_OAUTH_TOKEN to the CLI when CLAUDE_FORCE_OAUTH
            # is set, and drops the API key it would otherwise prefer.
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            env["CLAUDE_FORCE_OAUTH"] = "1"
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)
        elif token and not env.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_API_KEY"] = token
    elif agent == "terminus-2" and model and model.startswith("anthropic/"):
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        if token and token.startswith("sk-ant-oat"):
            # LiteLLM 1.99.0 recognizes this token in its API-key input and
            # adds the OAuth beta headers. Its AUTH_TOKEN-only path omits them.
            # Claude Code's host-side credential routing must stay unchanged.
            env["ANTHROPIC_API_KEY"] = token
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


async def _execute(command: list[str], output: Path, timeout_s: float | None,
                   stop_event: threading.Event | None, env: dict[str, str]) -> tuple | None:
    if stop_event and stop_event.is_set():
        return None
    task = asyncio.create_task(_run_process(command, "", cwd=output, env=env,
                                            timeout=timeout_s, log_dir=output / "logs"))
    try:
        while not task.done():
            if stop_event and stop_event.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return None
            await asyncio.sleep(.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _project(name: str) -> str:
    name = name.lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return re.sub(r"[^a-z0-9_-]", "-", name)


def _owned_projects(job_dir: Path, snapshot: Path, steps: list[str]) -> set[str]:
    projects = set()
    for config_path in job_dir.glob("*/config.json"):
        try:
            config = json.loads(config_path.read_text())
            if Path(config.get("task", {}).get("path", "")).resolve() != snapshot.resolve():
                continue
            name = config["trial_name"]
            if name != config_path.parent.name:
                continue
            projects.add(_project(name + "__env"))
            for key in steps or ["trial"]:
                raw = f"{name}__verifier__{key}"
                safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in raw)
                if len(safe) > 63:
                    suffix = "__" + hashlib.sha1(safe.encode()).hexdigest()[:8]
                    safe = safe[:63 - len(suffix)].rstrip("-._") + suffix
                projects.add(_project(safe))
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return projects


def _cleanup_owned(job_dir: Path, snapshot: Path, steps: list[str]) -> dict:
    """Remove only exact project labels derived from this job's owned trial configs."""
    report = {"projects": sorted(_owned_projects(job_dir, snapshot, steps)), "removed": [], "errors": []}
    for project in report["projects"]:
        for kind, query, remove in [
            ("containers", ["docker", "ps", "-aq"], ["docker", "rm", "-f"]),
            ("networks", ["docker", "network", "ls", "-q"], ["docker", "network", "rm"]),
        ]:
            try:
                found = subprocess.run([*query, "--filter", f"label=com.docker.compose.project={project}"],
                                       text=True, capture_output=True, timeout=15, check=True)
                ids = found.stdout.split()
                if ids:
                    subprocess.run([*remove, *ids], capture_output=True, text=True, timeout=30, check=True)
                    report["removed"].append({"project": project, "kind": kind, "ids": ids})
            except (OSError, subprocess.SubprocessError) as exc:
                report["errors"].append(redact(str(exc)))
    report["scope"] = "Exact owned trial projects only; no global prune. Images and volumes retained."
    return report


#: Retained campaign evidence, hashed into `report["evidence_files"]` and
#: re-hashed by `evidence.native_evidence`.
EVIDENCE_PATHS = ("jobs", "harbor-job.json", LIFECYCLE_FILENAME,
                  "source-build.json", "execution-build.json", "file-modes.json")


def evidence_record(output: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(file hashes, symlinks) of a campaign's retained evidence.

    `artifacts.evidence_inventory` raises on the first symlink, which is right
    for a package and wrong for a campaign: an agent that plants `ln -s` in its
    deliverables would make the whole campaign unreadable -- every other trial's
    measurement lost, and the trial that did it never named. Here a symlink is
    recorded (path -> link text, never followed) so the campaign stays readable
    and `attribution.trajectory_hacks` names the trial that planted it.
    """
    root = Path(output).resolve()
    files: dict[str, str] = {}
    links: dict[str, str] = {}

    def visit(path: Path) -> None:
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            links[rel] = os.readlink(path)
        elif path.is_file():
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()

    for rel in EVIDENCE_PATHS:
        source = root / rel
        if source.is_symlink() or not source.is_dir():
            if source.is_symlink() or source.exists():
                visit(source)
            continue
        for folder, dirs, names in os.walk(source, followlinks=False):
            for name in sorted(dirs + names):
                visit(Path(folder) / name)
    return files, links


def run_campaign(bundle: Path, output: Path, model: str | None = None, *,
                 agent: str = "terminus-2", fixture_case: Path | None = None,
                 rollouts: int = 1, workers: int = 1,
                 effort: str = policy.SPEC_CALIBRATION_EFFORT,
                 timeout_s: float | None = 14400, stop_event: threading.Event | None = None,
                 no_limits: bool = policy.SPEC_NO_LIMITS, max_output_tokens: int | None = None,
                 context_limit: int | None = None,
                 legacy_diagnostic: bool = False,
                 dry_run: bool = False) -> dict:
    """Synchronous entrypoint, suitable for the factory's interruptible QA thread."""
    if agent not in SOLVER_AGENTS | {"oracle", "nop", "fixture"}:
        raise ValueError(f"Unknown Harbor agent: {agent}")
    if isinstance(rollouts, bool) or not isinstance(rollouts, int) or rollouts < 1:
        raise ValueError("rollouts must be a positive integer")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if no_limits:
        timeout_s = None
    if timeout_s is not None and (not _number(timeout_s) or timeout_s <= 0):
        raise ValueError("timeout_s must be None or positive and finite")
    for name, value in (("max_output_tokens", max_output_tokens), ("context_limit", context_limit)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError(f"{name} must be a positive integer")
    if max_output_tokens and context_limit and max_output_tokens >= context_limit:
        raise ValueError("Output allowance must be smaller than the context window")
    if agent in SOLVER_AGENTS and not model:
        raise ValueError(f"{agent} requires an explicit model")
    if agent == "terminus-2" and max_output_tokens is None:
        # The spec allowance by default, so no caller has to remember it.
        max_output_tokens = policy.output_tokens_for(model)
    if agent in SOLVER_AGENTS:
        # Refused before anything is staged or spent. The benchmark CLI,
        # the screen and category_batch all reach a solver through here.
        policy.refuse_retired(model)
    if agent in HARNESS_AGENTS and policy.harness_for(model) != agent:
        raise ValueError(f"{model} is calibrated in {policy.harness_for(model)}, not {agent}")
    resolved_model = resolve_model(model) if agent in SOLVER_AGENTS else None
    if agent == "fixture" and fixture_case is None:
        raise ValueError("Fixture replay requires fixture_case")
    bundle, output = Path(bundle).absolute(), Path(output).absolute()
    if output.resolve().is_relative_to(bundle.resolve()):
        raise ValueError("Benchmark output must be outside the source package")
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("Benchmark output must not use symlinks")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Benchmark output must be a fresh directory: {output}")
    if stop_event and stop_event.is_set():
        return {"status": "cancelled", "cancelled": True, "ok": False,
                "errors": [], "trials": [], "summary": summarize([], rollouts),
                "note": "Cancelled before package inspection or process launch."}
    info = inspect_package(bundle)
    source_hash = fingerprint(bundle, task_only=True)
    source_modes = file_modes(bundle)
    from .preflight import validate_task
    task_validation = validate_task(bundle, legacy_diagnostic=legacy_diagnostic)
    if fingerprint(bundle, task_only=True) != source_hash or file_modes(bundle) != source_modes:
        raise ValueError("Package changed during benchmark preflight")
    output.mkdir(parents=True, mode=0o700)
    snapshot = output / "package"
    copy_tree(bundle, snapshot, task_only=True)
    snapshot_hash = fingerprint(snapshot, task_only=True)
    if source_hash != snapshot_hash:
        raise ValueError("Package changed while taking benchmark snapshot")
    if source_modes != file_modes(snapshot):
        raise ValueError("Package permissions changed while taking benchmark snapshot")
    if agent == "fixture":
        _fixture_overlay(snapshot, Path(fixture_case), info["step_names"])
    run_hash = fingerprint(snapshot, task_only=True)
    run_modes = file_modes(snapshot)
    write_json(output / "source-build.json", source_hash)
    write_json(output / "execution-build.json", run_hash)
    write_json(output / "file-modes.json", {"source": source_modes, "execution": run_modes})
    from ..author.native_ownership import register_native_run
    ownership = register_native_run(output, snapshot, info["step_names"]) if not dry_run else None
    lifecycle = _stage_lifecycle(output, ownership)
    history = _history_adapter()
    config = job_config(snapshot, output, model=model, agent=agent, rollouts=rollouts,
                        workers=workers, effort=effort, no_limits=no_limits,
                        max_output_tokens=max_output_tokens, context_limit=context_limit)
    write_json(output / "harbor-job.json", config)
    report = {"schema": "arena-factory/harbor-benchmark/1", "protocol": PROTOCOL,
              "protocol_note": PROTOCOL_NOTE, "source_build_sha256": source_hash["sha256"],
              "task_validation": task_validation,
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "execution_build_sha256": run_hash["sha256"], "agent": agent, "model": resolved_model,
              "native_lifecycle": lifecycle,
              "native_ownership": ownership,
              "step_history_adapter": history,
              "requested_model": model, "resolved_model": resolved_model,
              "source_file_modes_sha256": source_modes["sha256"],
              "execution_file_modes_sha256": run_modes["sha256"],
              "effort": effort if agent in SOLVER_AGENTS else None, "status": "preflight",
              "harness": ({"agent": agent, "version": policy.HARNESS_VERSIONS[agent]}
                          if agent in HARNESS_AGENTS else None),
              "limits": {"no_limits": no_limits, "campaign_timeout_sec": timeout_s,
                         "max_output_tokens": max_output_tokens, "context_limit": context_limit},
              "ok": False, "errors": [], "trials": [], "step_names": info["step_names"],
              "summary": summarize([], rollouts), "started_at": time.time()}
    if dry_run:
        report.update(status="planned", dry_run=True, finished_at=time.time())
        write_json(output / "report.json", report)
        return report
    write_json(output / "report.json", report)
    job_dir = output / "jobs/run"
    try:
        runtime = doctor()
        report["runtime"] = runtime
        write_json(output / "runtime.json", runtime)
        if not runtime["ok"]:
            raise RuntimeError("; ".join(runtime["errors"]))
        _validate_runtime_controls(output, lifecycle, config, history, ownership)
        report["status"] = "running"
        write_json(output / "report.json", report)
        env = _child_environment(agent, resolved_model)
        # Harbor 0.22 accepts module:Class, not filesystem import paths. The
        # console script otherwise cannot resolve this controller-owned hook.
        env["PYTHONPATH"] = history["pythonpath"]
        command = [runtime["binary"], "run", "--config", str(output / "harbor-job.json")]
        if no_limits:
            command = [str(Path(runtime["binary"]).parent / "python"), "-m",
                       "pipeline.benchmark.unlimited_runtime", *command[1:]]
        result = asyncio.run(_execute(command, output, timeout_s, stop_event, env))
        if result is None:
            report.update(status="cancelled", cancelled=True)
            report["errors"].append("Benchmark cancelled")
        else:
            code, _, _, seconds, timed_out = result
            report.update(exit_code=code, elapsed_seconds=seconds, timed_out=timed_out)
            if timed_out or code:
                report["errors"].append("Harbor timed out" if timed_out else f"Harbor exited with code {code}")
            report["status"] = "finished"
        trial_dirs = sorted(p for p in job_dir.iterdir() if p.is_dir()) if job_dir.exists() else []
        # Only trial directories carry config.json; never ingest nested verifier result files.
        report["trials"] = [parse_trial(p / "result.json", info["step_names"],
                                        oracle=agent in {"oracle", "fixture"},
                                        solver=agent in SOLVER_AGENTS,
                                        harness=agent in HARNESS_AGENTS,
                                        max_turns=None if no_limits else 40,
                                        strategy=info["multi_step_reward_strategy"] or "mean")
                            for p in trial_dirs if (p / "config.json").is_file()]
        report["summary"] = summarize(report["trials"], rollouts)
        if len(report["trials"]) != rollouts:
            report["errors"].append(f"Expected {rollouts} trial records; found {len(report['trials'])}")
        if report["summary"]["valid"] != rollouts:
            report["errors"].append("One or more trials were incomplete or invalid")
        if fingerprint(snapshot, task_only=True) != run_hash or fingerprint(bundle, task_only=True) != source_hash:
            report["errors"].append("Task package changed during benchmark execution")
        if file_modes(snapshot) != run_modes or file_modes(bundle) != source_modes:
            report["errors"].append("Task package permissions changed during benchmark execution")
        _validate_runtime_controls(output, lifecycle, config, history, ownership)
    except (Exception, KeyboardInterrupt) as exc:
        report["errors"].append(redact(f"{type(exc).__name__}: {exc}"))
        report["status"] = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
    finally:
        report["cleanup"] = _cleanup_owned(job_dir, snapshot, info["step_names"])
        if report["cleanup"]["errors"]:
            report["errors"].append("Owned Docker resources may remain; inspect cleanup errors")
        report["ok"] = not report["errors"] and report["summary"]["valid"] == rollouts
        report["finished_at"] = time.time()
        report["evidence_files"], report["evidence_symlinks"] = evidence_record(output)
        write_json(output / "report.json", report)
    return report
