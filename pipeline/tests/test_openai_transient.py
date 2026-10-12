"""An in-stream provider error that is infra loss is retried, not reported as
a result (build 2026-09-26: one rate-limit burst voided 13 reviews)."""
import io
import json
import unittest
from unittest.mock import patch

from pipeline.calibrate import agent_openai
from pipeline.tests.openai_transport_fixture import isolated_openai_transport


def stream(*events):
    lines = []
    for event in events:
        lines += [f"data: {json.dumps(event)}\n".encode(), b"\n"]
    return io.BytesIO(b"".join(lines))


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self.body

    def __exit__(self, *exc):
        return False


DONE = {"type": "response.completed",
        "response": {"status": "completed", "output": []}}


class TransientStreamErrors(unittest.TestCase):
    def call(self, *bodies):
        responses = iter([_Resp(b) for b in bodies])
        transport = isolated_openai_transport(self)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}), \
             patch.object(agent_openai.urllib.request, "urlopen", side_effect=lambda *a, **k: next(responses)):
            return agent_openai._call({"model": "m"}), transport.sleep

    def test_a_rate_limit_in_the_stream_is_retried_with_a_long_backoff(self):
        limited = stream({"type": "error", "error": {"code": "rate_limit_exceeded"}})
        response, sleep = self.call(limited, stream(DONE))
        self.assertEqual(response["status"], "completed")
        self.assertGreaterEqual(sleep.call_args[0][0], 15)

    def test_rate_limit_windows_do_not_spend_attempts(self):
        """2026-10-06: a throttled key killed 170-turn reviews on the fifth quick retry. Six
        rate-limit windows, then success, now completes within the default five attempts."""
        limited = [stream({"type": "error", "error": {"code": "rate_limit_exceeded"}}) for _ in range(6)]
        response, sleep = self.call(*limited, stream(DONE))
        self.assertEqual(response["status"], "completed")
        self.assertGreaterEqual(sum(c.args[0] for c in sleep.call_args_list), 15 * 6)

    def test_rate_limit_waits_stay_bounded_by_the_wait_budget(self):
        limited = [stream({"type": "error", "error": {"code": "rate_limit_exceeded"}}) for _ in range(40)]
        with self.assertRaisesRegex(RuntimeError, "waiting budget exhausted|rate-limit window"):
            self.call(*limited)

    def test_other_transient_errors_still_spend_attempts(self):
        failing = [stream({"type": "error", "error": {"code": "server_error"}}) for _ in range(5)]
        with self.assertRaisesRegex(RuntimeError, "failed after 5 attempts") as caught:
            self.call(*failing, stream(DONE))
        self.assertNotIn("rate-limit window", str(caught.exception))

    def test_a_quota_error_in_the_stream_is_not_retried(self):
        quota = stream({"type": "error", "error": {"code": "insufficient_quota"}})
        with self.assertRaisesRegex(RuntimeError, "insufficient_quota"):
            self.call(quota, stream(DONE))


if __name__ == "__main__":
    unittest.main()
