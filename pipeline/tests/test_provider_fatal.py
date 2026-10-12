"""A spent quota or a rejected credential stops a batch instead of erroring every task.

Census 2026-10-03: 19 of 44 batch runs errored at design on a weekly-limit 429
(retried as transient) or an expired OAuth token.
"""
import io
import json
import unittest
from unittest.mock import patch

from pipeline import orchestrate
from pipeline.provider import _http_failure, fatal_provider_error


class FakeHTTPError(Exception):
    def __init__(self, code, body):
        self.code, self.headers = code, {}
        self._body = json.dumps(body).encode()

    def read(self, n=-1):
        return self._body[:n]


class Classification(unittest.TestCase):
    def test_a_weekly_limit_429_is_fatal_and_says_so(self):
        exc, delay = _http_failure(FakeHTTPError(429, {"type": "error", "error": {
            "type": "rate_limit_error", "message": "You have reached your weekly usage limit."}}), 0)
        self.assertIsNone(delay)
        self.assertTrue(fatal_provider_error(exc))
        self.assertNotIn("weekly", str(exc))          # provider prose is never retained

    def test_a_per_minute_rate_limit_stays_retryable(self):
        exc, delay = _http_failure(FakeHTTPError(429, {"type": "error", "error": {
            "type": "rate_limit_error", "message": "This request would exceed the rate limit of 400,000 input tokens per minute."}}), 0)
        self.assertIsNotNone(delay)
        self.assertFalse(fatal_provider_error(exc))

    def test_a_rejected_credential_and_a_billing_error_are_fatal(self):
        for code, body in ((401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
                           (403, {"error": {"type": "permission_error", "message": "no access"}}),
                           (400, {"error": {"type": "billing_error", "message": "x"}}),
                           (400, {"error": {"type": "invalid_request_error",
                                            "message": "Your credit balance is too low"}})):
            exc, delay = _http_failure(FakeHTTPError(code, body), 0)
            self.assertIsNone(delay, code)
            self.assertTrue(fatal_provider_error(exc), code)

    def test_server_errors_stay_retryable(self):
        exc, delay = _http_failure(FakeHTTPError(529, {"error": {"type": "overloaded_error", "message": "x"}}), 0)
        self.assertIsNotNone(delay)
        self.assertFalse(fatal_provider_error(exc))


class BatchStops(unittest.TestCase):
    def tearDown(self):
        orchestrate.PROVIDER_STOP.clear()

    def test_after_a_fatal_failure_no_further_task_starts(self):
        import tempfile
        from pathlib import Path
        started = []
        def fake_build(row, index, out_root, designer, budget, **kw):
            if orchestrate.PROVIDER_STOP.is_set():
                return orchestrate.TaskResult(slug=f"s{index}", session_id=row["session_id"],
                                              seed_index=0, status="error", error=orchestrate.STOPPED)
            started.append(index)
            return orchestrate.TaskResult(slug=f"s{index}", session_id=row["session_id"], seed_index=0,
                                          status="error", error="design failed 1 times. HTTP 429: provider "
                                          "request failed (quota or spend limit; retry cannot restore access)")
        rows = [{"session_id": f"x{i}", "seed_index": 0} for i in range(6)]
        with tempfile.TemporaryDirectory() as tmp, patch.object(orchestrate, "build_one", side_effect=fake_build):
            report = orchestrate.batch(rows, Path(tmp), lambda *a, **k: None, task_workers=1)
        self.assertEqual(len(started), 1)
        self.assertTrue(orchestrate.PROVIDER_STOP.is_set())
        stopped = [r for r in report["results"] if r["error"] == orchestrate.STOPPED]
        self.assertEqual(len(stopped), 5)

    def test_the_preflight_stops_before_any_task_on_a_fatal_answer(self):
        with patch("pipeline.author.transport._call", side_effect=RuntimeError(
                "HTTP 401: provider request failed (credential rejected; check keys.env)")):
            with self.assertRaises(SystemExit):
                orchestrate.credential_preflight()
        with patch("pipeline.author.transport._call", side_effect=RuntimeError("Anthropic transport interrupted")):
            self.assertFalse(orchestrate.credential_preflight()["ok"])


if __name__ == "__main__":
    unittest.main()
