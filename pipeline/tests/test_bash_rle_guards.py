"""Guards for the failure classes of the 2026-10-24 bash_rle push: each one cost a release attempt."""
import json

import pytest

from pipeline import hack_triage, policy, release


def test_triage_finds_trials_under_any_artifacts_root(tmp_path):
    # a batch screened under ARENA_PIPELINE_ARTIFACTS=pipeline-artifacts-bash100 (every flag was "not retained")
    step = tmp_path / "pipeline-artifacts-bash100/native/screen-1/calibration-2/jobs/run/package__X/steps/01-s"
    step.mkdir(parents=True)
    version = tmp_path / "envs/lineage/hardened/v2"
    version.mkdir(parents=True)
    assert hack_triage.step_dir(version, {"trial": "package__X", "step": "01-s"}) == step


def _gate_bundle(tmp_path):
    bundle = tmp_path / "v6"
    (bundle / "authoring").mkdir(parents=True)
    (bundle / "verification").mkdir()
    (bundle / "authoring/spec.json").write_text(json.dumps({"_producer": {"model": "claude-opus-5-5"}}))
    return bundle


def _patch_gate(monkeypatch, refused):
    from pipeline import arena_v01_release, screen
    monkeypatch.setattr(release, "_calibrated", lambda *a, **k: False)
    monkeypatch.setattr(screen, "shippable_problems", lambda *a, **k: list(refused))
    monkeypatch.setattr(arena_v01_release, "target", lambda bundle: {"domain": "d", "prompt_language": "en"})
    monkeypatch.setattr(policy, "calibration_problems", lambda models: [])


def _past_gate(exc) -> bool:
    return "no clean screen of this version" not in str(exc)


def test_release_without_screen_is_opt_in_and_recorded(tmp_path, monkeypatch):
    # the opt-in path crashed on first real use (a local `time` binding): run it, not just read it
    bundle = _gate_bundle(tmp_path)
    _patch_gate(monkeypatch, ["no calibration screen of this task version (verification/screen.json); run ..."])
    monkeypatch.setenv("ARENA_V01_RELEASE_WITHOUT_SCREEN", "1")
    with pytest.raises(Exception) as exc:  # stops later (the reviewer check), never at the screen gate
        release._qualify(bundle, ["a", "b"], "claude-opus-5-5", "b", tmp_path / "native", n=10)
    assert _past_gate(exc.value), exc.value
    assert json.loads((bundle / "verification/screen-gate-waived.json").read_text())["refused"]


def test_without_the_opt_in_a_missing_screen_still_refuses(tmp_path, monkeypatch):
    bundle = _gate_bundle(tmp_path)
    _patch_gate(monkeypatch, ["no calibration screen of this task version (verification/screen.json); run ..."])
    monkeypatch.delenv("ARENA_V01_RELEASE_WITHOUT_SCREEN", raising=False)
    with pytest.raises(ValueError, match="no clean screen of this version"):
        release._qualify(bundle, ["a", "b"], "x", "b", tmp_path / "native", n=10)


def test_only_runner_transition_problems_are_forgiven(tmp_path, monkeypatch):
    bundle = _gate_bundle(tmp_path)
    _patch_gate(monkeypatch, ["anthropic/claude-fable-5-1: native runner changed; remeasure with the current protocol",
                              "anthropic/claude-fable-5-1: reward hacking unresolved in package__Y/01-s: symlink"])
    with pytest.raises(ValueError, match="reward hacking unresolved"):
        release._qualify(bundle, ["a", "b"], "x", "b", tmp_path / "native", n=10)
