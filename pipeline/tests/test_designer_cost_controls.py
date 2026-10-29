"""The designer's cost controls (2026-10-26): what every request carries, and what it never loses.

Measured over 597 design sessions: 48% of designer spend was cache re-writes
after the 5-minute entry expired during long tool calls; a quarter of every
prompt was the designer's own replayed thinking and a third its own re-staged
files; the trace evidence was 2-4%. The controls cut the first three and pin
the fourth into the cached prefix.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import patch

from pipeline import policy
from pipeline.author import agent_designer as author
from pipeline.author import transport
from pipeline.author.extract import Extraction


def _stream(events):
    return io.BytesIO("".join("data: " + json.dumps(e) + "\n\n" for e in events).encode())


def _response():
    return _stream([
        {"type": "message_start", "message": {"id": "m", "model": "x", "content": [], "usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"}])


class BetaHeader(unittest.TestCase):
    """`betas` becomes the `anthropic-beta` header, merged with the credential's own, never a body field."""

    def _sent(self, body, auth):
        with patch.object(transport, "_auth_headers", return_value=auth), \
             patch.object(transport, "request_slot", side_effect=nullcontext), \
             patch.object(transport.urllib.request, "urlopen", side_effect=[_response()]) as urlopen:
            transport._call(body)
        request = urlopen.call_args.args[0]
        return request.headers, json.loads(request.data)

    def test_the_beta_rides_in_the_header_and_leaves_the_body(self):
        headers, sent = self._sent({"model": "m", "betas": [author.CONTEXT_EDITING_BETA]}, {"x-api-key": "k"})
        self.assertEqual(headers["Anthropic-beta"], author.CONTEXT_EDITING_BETA)
        self.assertNotIn("betas", sent)
        self.assertTrue(sent["stream"])

    def test_the_oauth_beta_is_kept_and_duplicates_collapse(self):
        headers, _ = self._sent({"model": "m", "betas": ["b2", "b2"]}, {"anthropic-beta": "oauth-x", "authorization": "Bearer t"})
        self.assertEqual(headers["Anthropic-beta"], "oauth-x,b2")
        headers, _ = self._sent({"model": "m"}, {"anthropic-beta": "oauth-x"})
        self.assertEqual(headers["Anthropic-beta"], "oauth-x")

    def test_a_malformed_beta_list_is_refused_before_any_request(self):
        for betas in ("b", [""], [1], {"b": 1}):
            with self.subTest(betas=betas), self.assertRaises(ValueError):
                transport._beta_header({}, betas)


class StageSpecEdits(unittest.TestCase):
    """A string field changes by its edited span; the result says what is stored without a read-back."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "environment/repo").mkdir(parents=True)
        extraction = SimpleNamespace(session_id="synthetic", seed_index=0, source_root=self.root, moments=[])
        self.handlers = author._handlers(self.root, extraction, {}, self.root / "draft.json")

    def test_edit_replaces_exactly_one_occurrence(self):
        first = self.handlers["stage_spec"](["description"], "Parse the ledger; keep the order; keep the totals.")
        self.assertEqual(set(first) >= {"stored", "sections", "size_bytes", "sha256"}, True)
        edited = self.handlers["stage_spec"](["description"], edit={"old": "keep the totals", "new": "sum the totals"})
        self.assertTrue(edited["edited"])
        self.assertNotEqual(edited["sha256"], first["sha256"])
        self.assertEqual(self.handlers["read_draft"](["description"]), "Parse the ledger; keep the order; sum the totals.")

    def test_ambiguous_missing_or_non_string_edits_are_refused(self):
        self.handlers["stage_spec"](["description"], "keep the order; keep the totals.")
        with self.assertRaisesRegex(ValueError, "occurs 2 times"):
            self.handlers["stage_spec"](["description"], edit={"old": "keep", "new": "hold"})
        with self.assertRaisesRegex(ValueError, "occurs 0 times"):
            self.handlers["stage_spec"](["description"], edit={"old": "absent", "new": "x"})
        with self.assertRaisesRegex(ValueError, "not staged yet"):
            self.handlers["stage_spec"](["name"], edit={"old": "a", "new": "b"})
        self.handlers["stage_spec"](["keywords"], ["a", "b"])
        with self.assertRaisesRegex(ValueError, "not a string"):
            self.handlers["stage_spec"](["keywords"], edit={"old": "a", "new": "b"})
        with self.assertRaisesRegex(ValueError, "no value or append"):
            self.handlers["stage_spec"](["description"], "x", edit={"old": "a", "new": "b"})
        with self.assertRaisesRegex(ValueError, "needs a value"):
            self.handlers["stage_spec"](["description"])
        self.assertEqual(self.handlers["read_draft"](["description"]), "keep the order; keep the totals.")

    def test_the_tool_contract_offers_edit_and_no_longer_requires_value(self):
        tool = next(t for t in author._tools(3) if t["name"] == "stage_spec")
        self.assertEqual(tool["input_schema"]["required"], ["path"])
        self.assertEqual(tool["input_schema"]["properties"]["edit"]["required"], ["old", "new"])


class ContextEditing(unittest.TestCase):
    def test_thinking_clears_and_tool_results_stay_cached(self):
        """2026-10-26: tool-result clearing re-wrote the cache 31 times in one session ($73 of $91)."""
        self.assertFalse(policy.DESIGNER_TOOL_CLEARING)
        edits = author._context_edits()
        self.assertEqual([e["type"] for e in edits], ["clear_thinking_20251015"])
        self.assertEqual(edits[0]["keep"], {"type": "thinking_turns", "value": policy.DESIGNER_THINKING_KEEP_TURNS})

    def test_tool_clearing_when_switched_on_excludes_every_evidence_tool(self):
        offered = {t["name"] for t in author._tools(3)}
        self.assertTrue(set(policy.DESIGNER_EVIDENCE_TOOLS) <= offered, set(policy.DESIGNER_EVIDENCE_TOOLS) - offered)
        with patch.object(policy, "DESIGNER_TOOL_CLEARING", True):
            edits = author._context_edits()
        self.assertEqual([e["type"] for e in edits], ["clear_thinking_20251015", "clear_tool_uses_20250919"])
        self.assertEqual(edits[1]["trigger"], {"type": "input_tokens", "value": policy.DESIGNER_TOOL_CLEAR_TRIGGER_TOKENS})
        self.assertEqual(edits[1]["keep"], {"type": "tool_uses", "value": policy.DESIGNER_TOOL_RESULTS_KEEP})
        self.assertEqual(edits[1]["clear_at_least"], {"type": "input_tokens", "value": policy.DESIGNER_TOOL_CLEAR_AT_LEAST_TOKENS})
        self.assertNotIn("stage_spec", edits[1]["exclude_tools"])
        self.assertNotIn("read_draft", edits[1]["exclude_tools"])
        self.assertIn("list_corrections", edits[1]["exclude_tools"])


if __name__ == "__main__":
    unittest.main()
