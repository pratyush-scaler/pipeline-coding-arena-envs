"""User, 2026-09-26: Fable 5.1 calibrates in Claude Code and GPT 6 Astra in Codex,
both at high effort, the CLI baked into the task image at a pinned version, the
agent phase allowed only its own model API host and the verifier offline."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import policy
from pipeline.benchmark import harbor


class HarnessAssignment(unittest.TestCase):
    def test_each_model_runs_in_its_own_harness(self):
        self.assertEqual(policy.harness_for("claude-fable-5-1"), "claude-code")
        self.assertEqual(policy.harness_for("anthropic/claude-fable-5-1"), "claude-code")
        self.assertEqual(policy.harness_for("gpt-6-astra"), "codex")
        self.assertEqual(policy.harness_for("openai/gpt-6-astra"), "codex")
        # A documented substitute runs in its provider's harness.
        self.assertEqual(policy.harness_for("anthropic/claude-other"), "claude-code")
        self.assertEqual(policy.harness_for("gpt-other"), "codex")
        with self.assertRaises(ValueError):
            policy.harness_for("kimi-k3")

    def test_effort_stays_high_for_both(self):
        self.assertEqual(policy.SPEC_CALIBRATION_EFFORT, "high")

    def test_job_config_per_harness(self):
        for model, agent, host in (("claude-fable-5-1", "claude-code", "api.anthropic.com"),
                                   ("gpt-6-astra", "codex", "api.openai.com")):
            with self.subTest(agent=agent), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANTHROPIC_BASE_URL", None)
                os.environ.pop("OPENAI_BASE_URL", None)
                config = harbor.job_config(Path("/p"), Path("/o"), model=model, agent=agent,
                                           rollouts=5, workers=5, effort="high", no_limits=True)
                settings = config["agents"][0]
                self.assertEqual(settings["name"], agent)
                self.assertEqual(settings["model_name"], harbor.resolve_model(model))
                self.assertEqual(settings["kwargs"], {"reasoning_effort": "high",
                                                      "version": policy.HARNESS_VERSIONS[agent]})
                self.assertNotIn("max_turns", settings["kwargs"], "no turn limit")
                self.assertEqual(settings["extra_allowed_hosts"], [host])
                self.assertIs(settings["resume_trajectory"], False)
                self.assertEqual(config["environment"]["import_path"], harbor.HARNESS_PROVIDER)

    def test_a_model_in_the_wrong_harness_is_refused(self):
        with self.assertRaisesRegex(ValueError, "calibrated in codex"):
            harbor.job_config(Path("/p"), Path("/o"), model="gpt-6-astra", agent="claude-code",
                              rollouts=1, workers=1, effort="high")
        with self.assertRaisesRegex(ValueError, "calibrated in claude-code"):
            harbor.run_campaign(Path("/nonexistent"), Path("/nonexistent-out"),
                                model="claude-fable-5-1", agent="codex")

    def test_offline_agents_keep_the_offline_provider(self):
        config = harbor.job_config(Path("/p"), Path("/o"), model=None, agent="oracle",
                                   rollouts=1, workers=1, effort="high")
        self.assertEqual(config["environment"]["import_path"], harbor.OFFLINE_PROVIDER)


class Credentials(unittest.TestCase):
    def _env(self, agent, model, **env):
        with patch.dict(os.environ, env, clear=True):
            return harbor._child_environment(agent, model)

    def test_claude_code_takes_an_oauth_token_on_its_oauth_path(self):
        env = self._env("claude-code", "anthropic/claude-fable-5-1",
                        ANTHROPIC_AUTH_TOKEN="sk-ant-oat01-x", OPENAI_API_KEY="o")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-x")
        self.assertEqual(env["CLAUDE_FORCE_OAUTH"], "1")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_claude_code_takes_an_api_key_as_one(self):
        env = self._env("claude-code", "anthropic/claude-fable-5-1", ANTHROPIC_AUTH_TOKEN="sk-ant-api03-x")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-api03-x")
        self.assertNotIn("CLAUDE_FORCE_OAUTH", env)

    def test_codex_keeps_its_key_and_offline_agents_get_none(self):
        self.assertEqual(self._env("codex", "openai/gpt-6-astra", OPENAI_API_KEY="o")["OPENAI_API_KEY"], "o")
        env = self._env("oracle", None, OPENAI_API_KEY="o", ANTHROPIC_AUTH_TOKEN="a")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)


class IsolationProofs(unittest.TestCase):
    AGENT = {"schema": "arena-pipeline/harness-network/1", "privileged": False, "port_bindings": None,
             "cap_drop": ["NET_ADMIN", "NET_RAW"], "baseline": "no-network", "egress_control": True,
             "network_mode": "harbor_default", "agent_phase_allowlist": ["api.anthropic.com"]}

    def ok(self, proof, agent="claude-code"):
        from pipeline.evidence import _isolation_ok
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            return _isolation_ok(proof, agent)

    def test_the_agent_container_may_reach_only_its_model_api(self):
        self.assertTrue(self.ok(self.AGENT))
        self.assertFalse(self.ok(dict(self.AGENT, agent_phase_allowlist=["example.com"])))
        self.assertFalse(self.ok(dict(self.AGENT, agent_phase_allowlist=["api.openai.com"])),
                         "Claude Code may not reach OpenAI")

    def test_a_verifier_container_has_no_allowlist(self):
        verifier = dict(self.AGENT, agent_phase_allowlist=[])
        self.assertTrue(self.ok(verifier))
        offline = dict(self.AGENT, egress_control=False, network_mode="none", agent_phase_allowlist=[])
        self.assertTrue(self.ok(offline))
        self.assertFalse(self.ok(dict(offline, network_mode="bridge")),
                         "no egress control means no network at all")

    def test_hardening_is_required(self):
        for broken in ({"privileged": True}, {"port_bindings": {"80/tcp": []}}, {"cap_drop": []},
                       {"baseline": "public"}, {"schema": "arena-pipeline/offline-network/1"}):
            with self.subTest(broken=broken):
                self.assertFalse(self.ok(dict(self.AGENT, **broken)))

    def test_offline_agents_still_need_no_network(self):
        from pipeline.evidence import _isolation_ok
        self.assertTrue(_isolation_ok({"privileged": False, "network_mode": "none", "networks": ["none"]}, "oracle"))
        self.assertFalse(_isolation_ok(self.AGENT, "oracle"))


class HarnessTranscripts(unittest.TestCase):
    def _step(self, root, steps):
        folder = Path(root) / "steps/01-task"
        (folder / "agent").mkdir(parents=True)
        if steps is not None:
            (folder / "agent/trajectory.json").write_text(json.dumps({"steps": steps}))
        return folder

    def test_a_step_without_a_transcript_is_void_never_a_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._step(tmp, None)
            result = harbor._step_result({"verifier_result": {"rewards": {"reward": 0}}}, folder,
                                         "01-task", oracle=False, solver=True, harness=True)
            self.assertFalse(result["valid"])
            self.assertIn("Missing or empty native harness transcript", result["errors"])

    def test_a_transcript_with_agent_turns_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._step(tmp, [{"source": "user"}, {"source": "agent", "tool_calls": []}])
            self.assertEqual(harbor.harness_transcript_steps(folder), 1)

    def test_a_passing_harness_episode_is_a_pass_not_a_void(self):
        # Screen 2026-09-26: harness steps carry no `n_episodes`, so every
        # episode read as "no tool calls" and attribution voided all of them.
        from pipeline.attribution import attribute
        from pipeline.evidence import rollouts
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._step(tmp, [{"source": "user"}] + [{"source": "agent"}] * 3)
            (folder / "verifier").mkdir()
            (folder / "verifier/reward-details.json").write_text(json.dumps({"reward": {
                "kind": "programmatic", "score": 1.0, "criteria": [
                    {"name": "obj_a", "value": 1.0, "status": "evaluated", "gated_out": False,
                     "reason": ""}]}}))
            step = harbor._step_result({"agent_result": {"metadata": None},
                                        "verifier_result": {"rewards": {"reward": 1}}},
                                       folder, "01-task", oracle=False, solver=True, harness=True)
            self.assertEqual(step["agent_turns"], 3)
            rows = rollouts([{"id": "t", "valid": True, "steps": [step]}], ["01-task"])
            self.assertEqual(rows[0]["tool_calls"], 3)
            bundle = Path(tmp) / "bundle"
            (bundle / "tests").mkdir(parents=True)
            (bundle / "tests/checks.json").write_text(json.dumps({"checks": []}))
            counts = attribute(bundle, {"rollouts": rows, "step_names": ["01-task"]}, 1)["counts"]
            self.assertEqual(dict(counts), {"pass": 1})


class HarnessToolCallsAreScanned(unittest.TestCase):
    def cmds(self, call):
        from pipeline.attribution import _tool_call_commands
        return _tool_call_commands(call)

    def test_file_tools_and_argv_commands_become_scannable_commands(self):
        self.assertEqual(self.cmds({"function_name": "Read", "arguments": {"file_path": "/tests/checks.json"}}),
                         ["cat /tests/checks.json"])
        self.assertEqual(self.cmds({"function_name": "Write", "arguments": {"file_path": "/logs/verifier/reward.txt"}}),
                         ["tee /logs/verifier/reward.txt"])
        self.assertEqual(self.cmds({"function_name": "exec_command", "arguments": {"cmd": ["bash", "-lc", "ls /tests"]}}),
                         ["ls /tests"])
        self.assertEqual(self.cmds({"function_name": "shell", "arguments": {"command": ["cat", "/tests/test.sh"]}}),
                         ["cat /tests/test.sh"])
        self.assertEqual(self.cmds({"function_name": "apply_patch", "arguments": {
            "input": "*** Begin Patch\n*** Update File: /tests/test.sh\n*** End Patch"}}), ["tee /tests/test.sh"])

    def test_a_graded_write_through_a_file_tool_is_flagged(self):
        from pipeline.attribution import _graded_write, _GRADED_RE
        write = self.cmds({"function_name": "Write", "arguments": {"file_path": "/logs/verifier/reward.txt"}})[0]
        self.assertTrue(_graded_write(write))
        read = self.cmds({"function_name": "Read", "arguments": {"file_path": "/tests/checks.json"}})[0]
        self.assertTrue(_GRADED_RE.search(read))
        edit = self.cmds({"function_name": "Edit", "arguments": {"file_path": "/app/deliverables/a.json"}})[0]
        self.assertFalse(_graded_write(edit) or _GRADED_RE.search(edit))


class CodexUnifiedExec(unittest.TestCase):
    """Smoke run, 2026-09-26: Codex 0.157 (unified_exec) sends one `exec` tool
    whose input is JavaScript calling tools.exec_command({cmd, workdir}); the
    scanner read 0 commands from a transcript that ran 16."""

    def cmds(self, call):
        from pipeline.attribution import _tool_call_commands
        return _tool_call_commands(call)

    def test_each_exec_command_is_read_with_its_workdir(self):
        script = ('const r = await Promise.allSettled([\n'
                  'tools.exec_command({cmd:"cat review.md",workdir:"/app/environment/repo",max_output_tokens:5000}),\n'
                  'tools.exec_command({cmd:"ls -la \\"/app/deliverables\\""})]);\n')
        self.assertEqual(self.cmds({"function_name": "exec", "arguments": {"input": script}}),
                         ["cd /app/environment/repo && cat review.md", 'ls -la "/app/deliverables"'])

    def test_a_python_repr_argument_is_parsed(self):
        call = {"function_name": "exec",
                "arguments": "{'input': 'text(await tools.exec_command({cmd:\"cat /tests/checks.json\"}));'}"}
        self.assertEqual(self.cmds(call), ["cat /tests/checks.json"])

    def test_writes_and_patches_through_exec_are_seen(self):
        from pipeline.attribution import _graded_write
        patch_call = {"function_name": "exec", "arguments": {"input":
            'await tools.apply_patch("*** Begin Patch\\n*** Update File: /tests/test.sh\\n*** End Patch");'}}
        self.assertEqual(self.cmds(patch_call), ["tee /tests/test.sh"])
        self.assertTrue(_graded_write(self.cmds(patch_call)[0]))
        stdin = {"function_name": "exec", "arguments": {"input": 'tools.write_stdin({chars:"echo 1 > /logs/verifier/reward.txt\\n"})'}}
        self.assertTrue(_graded_write(self.cmds(stdin)[0]))


class UnreadableTranscripts(unittest.TestCase):
    def test_tool_calls_none_readable_is_uninspectable_not_clean(self):
        from pipeline import attribution
        with tempfile.TemporaryDirectory() as tmp:
            step_dir = Path(tmp) / "trial/steps/01-task"
            (step_dir / "agent").mkdir(parents=True)
            (step_dir / "agent/trajectory.json").write_text(json.dumps({"steps": [
                {"source": "agent", "step_id": 1, "tool_calls": [
                    {"function_name": "mystery_tool", "arguments": {"blob": "x"}}]}]}))
            step = {"name": "01-task", "evidence_dir": str(step_dir)}
            self.assertEqual(attribution._unread_tool_calls(step), 1)
            (Path(tmp) / "trial/result.json").write_text("{}")
            bundle = Path(tmp) / "bundle"
            (bundle / "verification").mkdir(parents=True)
            hits = attribution.trajectory_hacks(bundle, {"trials": [{
                "id": "t", "valid": True, "result_file": str(Path(tmp) / "trial/result.json"),
                "steps": [step]}]})
            self.assertTrue(any(h["kind"] == "uninspectable" and "none readable" in h["evidence"]
                                for h in hits), hits)


class HarnessImage(unittest.TestCase):
    def test_node_agent_reuses_its_existing_uid_1000(self):
        from pipeline.author import toolchain as n
        for name in ("node", "browser"):
            text = n.agent_dockerfile({"name": name})
            self.assertIn("usermod --login agent --home /home/agent --move-home node", text)
            self.assertIn("groupmod --new-name agent node", text)
            self.assertNotIn("useradd --create-home --uid 1000 agent", text)
        self.assertIn("useradd --create-home --uid 1000 agent", n.agent_dockerfile({"name": "python"}))

    def test_node_images_have_certificates_before_https_snapshot_apt(self):
        from pipeline.author import toolchain as n
        for make, name in ((n.agent_dockerfile, "node"),
                           (n.verifier_dockerfile, "node"),
                           (n.agent_dockerfile, "browser")):
            text = make({"name": name})
            self.assertIn(f"FROM {n.IMAGES['python']} AS certificates", text)
            copy = "COPY --from=certificates /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt"
            self.assertIn(copy, text)
            self.assertLess(text.index(f"FROM {n.IMAGES['node']}\n"), text.index(copy))
            self.assertLess(text.index(copy), text.index("apt-get"))
        # The Python base already carries the trust store; its recipe stays unchanged.
        for make in (n.agent_dockerfile, n.verifier_dockerfile):
            self.assertNotIn("AS certificates", make({"name": "python"}))
        self.assertNotIn("AS certificates", n.verifier_dockerfile({"name": "browser"}))

    def test_the_agent_image_bakes_in_the_pinned_harnesses(self):
        from pipeline.author import toolchain as n
        for name in ("python", "node", "go"):
            text = n.agent_dockerfile({"name": name})
            self.assertIn(f"@anthropic-ai/claude-code@{policy.HARNESS_VERSIONS['claude-code']}", text)
            self.assertIn(f"@openai/codex@{policy.HARNESS_VERSIONS['codex']}", text)
            self.assertIn("claude --version && codex --version", text)
            # The shared layers come before the repository, so builds share them.
            self.assertLess(text.index("claude --version"), text.index("COPY repo/"))
        # A non-node base gets node from the pinned node image, never from apt.
        self.assertIn(f"FROM {n.IMAGES['node']} AS node", n.agent_dockerfile({"name": "python"}))


if __name__ == "__main__":
    unittest.main()


class ValidityBlocksDifficultyAdvises(unittest.TestCase):
    """User, 2026-09-26: the semantic review blocks on validity and novelty only;
    its difficulty judgments are advisory -- the screen measures difficulty, and
    hardening reads the advisories."""

    def test_the_reviewer_is_told_which_concerns_decide_the_verdict(self):
        from pipeline.review import PROMPT
        self.assertIn("VALIDITY DECIDES THE VERDICT; DIFFICULTY NEVER DOES", PROMPT)
        self.assertIn("difficulty_advisories", PROMPT)
        self.assertNotIn("Continue to reject\nconcrete design shortcuts", PROMPT)

    def test_advisories_never_enter_the_blocking_fields(self):
        import inspect
        from pipeline import review
        source = inspect.getsource(review.review_problems)
        self.assertIn('("ambiguities", "substantive_gaps", "shortcut_risks", "privacy_findings")', source)
        self.assertNotIn('"difficulty_advisories", "', source.split("for field in")[1].split(":")[0])

    def _reviewed_bundle(self, tmp, advisories):
        from pipeline.artifacts import atomic_json
        bundle = Path(tmp) / "b"
        work = "verification/reviews/r/attempt"
        atomic_json(bundle / work / "deliverables/review.json",
                    {"verdict": "pass", "difficulty_advisories": advisories})
        atomic_json(bundle / "verification/semantic-review.json",
                    {"artifact_sha256": "current", "work_dir": work})
        return bundle

    def test_advisories_are_read_from_the_current_review(self):
        from pipeline.review import review_advisories
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._reviewed_bundle(tmp, ["a short script follows the published rule",
                                                 {"id": "o3", "reason": "monotone maximum"}])
            with patch("pipeline.review.identity", return_value="current"):
                self.assertEqual(review_advisories(bundle),
                                 ["a short script follows the published rule", "o3: monotone maximum"])
            with patch("pipeline.review.identity", return_value="changed"):
                self.assertEqual(review_advisories(bundle), [], "a stale review advises nothing")

    def test_the_screen_report_and_the_hardening_brief_carry_them(self):
        import inspect
        from pipeline import harden, screen
        doc = screen.report([{"bundle": "t", "outlook": {"verdict": "strong-candidate", "reasons": []},
                              "review_advisories": ["recipe-driven timing chain"], "per_model": {}}])
        self.assertIn("reviewer difficulty advisory: recipe-driven timing chain", screen.markdown(doc))
        self.assertIn("THE INDEPENDENT REVIEWER'S DIFFICULTY ADVISORIES", inspect.getsource(harden.propose))
