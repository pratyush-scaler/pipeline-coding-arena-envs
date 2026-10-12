"""Calibration runs on the spec settings with nobody passing a flag.

Fable 5.1 and GPT 6 Astra, high reasoning effort, 128k output tokens for Fable
and 256k for Astra, no turn limit and no time limit.
"""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import policy


class SpecDefaults(unittest.TestCase):
    def test_policy_states_the_operator_settings(self):
        self.assertEqual(policy.resolve_calibration_models(), ("claude-fable-5-1", "gpt-6-astra"))
        self.assertEqual(policy.SPEC_CALIBRATION_EFFORT, "high")
        self.assertIs(policy.SPEC_NO_LIMITS, True)
        self.assertEqual(policy.output_tokens_for("anthropic/claude-fable-5-1"), 128_000)
        self.assertEqual(policy.output_tokens_for("openai/gpt-6-astra"), 256_000)
        self.assertIsNone(policy.output_tokens_for("some-other-model"))

    def test_a_native_campaign_records_them_without_being_told(self):
        """Dry run: the job config and report carry the defaults."""
        from pipeline.benchmark.harbor import run_campaign
        from pipeline.tests.test_production import synthetic_bundle
        for model, tokens in (("claude-fable-5-1", 128_000), ("gpt-6-astra", 256_000)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()           # macOS /var is a symlink; Harbor refuses those
                bundle = synthetic_bundle(root / "src")
                report = run_campaign(bundle, root / "out", model=model, dry_run=True)
            self.assertEqual(report["effort"], "high", model)
            self.assertEqual(report["limits"]["max_output_tokens"], tokens, model)
            self.assertIs(report["limits"]["no_limits"], True, model)
            self.assertIsNone(report["limits"]["campaign_timeout_sec"], model)

    def test_the_cli_defaults_are_the_spec(self):
        from pipeline import cli
        parser = cli.build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        bench = sub.choices["benchmark"].parse_args(["b"])
        self.assertEqual((bench.model, bench.effort, bench.no_limits),
                         ("claude-fable-5-1", "high", True))
        qualify = sub.choices["qualify"].parse_args(["b"])
        self.assertEqual(qualify.models, ["claude-fable-5-1", "gpt-6-astra"])

    def test_the_file_workspace_agents_use_the_same_allowances_and_no_turn_cap(self):
        from pipeline.calibrate import agent, agent_openai, run
        self.assertEqual(agent.ROLLOUT_MAX_TOKENS, 128_000)
        self.assertEqual(agent_openai.OPENAI_MAX_TOKENS, 256_000)
        self.assertEqual(run._output_budget("claude-fable-5-1"), 128_000)
        self.assertEqual(run._output_budget("gpt-6-astra"), 256_000)


class ConcurrencyAtMaximum(unittest.TestCase):
    """Operator rule: rate limits and concurrent jobs at the maximum by default."""

    def test_defaults_fill_the_coder_pool(self):
        from pipeline import config, orchestrate, provider
        from pipeline.benchmark.coder import Settings
        pool = Settings(template="t").tasks_per_instance
        self.assertEqual(config.POOL_CAMPAIGNS, pool)                       # one workspace x 20
        self.assertEqual(config.MAX_QUALIFY_WORKERS * 2, pool)              # two models per task
        self.assertEqual(orchestrate.DEFAULT_ROLLOUT_BUDGET, pool)
        self.assertEqual(orchestrate.DEFAULT_TASK_WORKERS, config.MAX_TASK_WORKERS)
        self.assertEqual(provider.GLOBAL.max_requests, config.MAX_API_REQUESTS)

    def test_the_cli_uses_them_without_flags(self):
        from pipeline import cli, config
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        build = sub.choices["build"].parse_args([])
        self.assertEqual((build.task_workers, build.rollout_budget),
                         (config.MAX_TASK_WORKERS, config.MAX_ROLLOUT_BUDGET))
