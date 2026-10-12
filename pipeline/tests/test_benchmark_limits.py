import json
import tempfile
import unittest
from pathlib import Path

from pipeline.benchmark.harbor import job_config, _step_result


class BenchmarkLimits(unittest.TestCase):
    def test_requested_provider_limits(self):
        for model, cap, key in (("claude-opus-5", 128000, "max_tokens"),
                                ("gpt-5.6-sol", 256000, "max_output_tokens")):
            config = job_config(Path("task"), Path("out"), model=model,
                agent="terminus-2", rollouts=10, workers=4, effort="xhigh",
                no_limits=True, max_output_tokens=cap, context_limit=1000000)
            agent = config["agents"][0]
            self.assertEqual(config["n_attempts"], 10)
            self.assertIn("UnlimitedTerminus2", agent["import_path"])
            self.assertIsNone(agent["kwargs"]["max_turns"])
            self.assertEqual(agent["kwargs"]["reasoning_effort"], "xhigh")
            self.assertEqual(agent["kwargs"]["llm_call_kwargs"][key], cap)
            self.assertEqual(agent["kwargs"]["model_info"]["max_input_tokens"], 1000000)

    def test_long_uncapped_trial_is_valid_but_missing_transcript_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "verifier").mkdir()
            (path / "verifier/reward-details.json").write_text(json.dumps({"reward": {
                "kind": "programmatic", "criteria": [{"name": "objective", "value": 1}]}}))
            record = {"agent_result": {"metadata": {"n_episodes": 75, "all_messages": ["transcript"]}},
                      "verifier_result": {"rewards": {"reward": 1}}}
            self.assertTrue(_step_result(record, path, "step", oracle=False, solver=True, max_turns=None)["valid"])
            self.assertFalse(_step_result(record, path, "step", oracle=False, solver=True)["valid"])
            record["agent_result"]["metadata"]["all_messages"] = []
            self.assertFalse(_step_result(record, path, "step", oracle=False, solver=True, max_turns=None)["valid"])


if __name__ == "__main__":
    unittest.main()
