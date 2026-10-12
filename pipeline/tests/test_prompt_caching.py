"""The design loop caches its prefix without changing what the model reads.

The first real coding task sent 171 designer requests carrying 30.6M input
tokens, none served from cache (2026-09-27); a pilot turn the next day sent
217,337 input tokens, 0 cached. Cache markers cut that by roughly an order of
magnitude, and they must do so WITHOUT touching the prompt: the rendered
tools, system and messages have to stay byte-identical, the stored request
config has to keep deciding whether a stalled session may resume, and
`conversation.json` must never accumulate a marker per turn (the API allows
four per request; a fifth is a 400 that would end the design).
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.author.agent_designer import (
    CACHE_MARK, CACHEABLE_BLOCKS, design, with_cache_breakpoints)
from pipeline.author.outcomes import SeedRejected


def markers(node) -> int:
    """Count `cache_control` keys anywhere in a request body."""
    if isinstance(node, dict):
        return int("cache_control" in node) + sum(markers(v) for v in node.values())
    if isinstance(node, list):
        return sum(markers(v) for v in node)
    return 0


def rendered(body: dict) -> dict:
    """What the API renders: markers removed, shorthand strings expanded."""
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "cache_control"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    out = strip(body)
    system = out.get("system")
    if isinstance(system, str):
        out["system"] = [{"type": "text", "text": system}]
    return out


def a_body() -> dict:
    return {"model": "claude-opus-5", "max_tokens": 48000, "system": "You design tasks.",
            "output_config": {"effort": "xhigh"},
            "tools": [{"name": "read_fixture", "description": "Read.", "input_schema": {"type": "object"}}],
            "messages": [
                {"role": "user", "content": "Design from the source."},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": "Reading."},
                    {"type": "tool_use", "id": "t1", "name": "read_fixture", "input": {"path": "a.py"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "print(1)\n"}]}]}


class CacheBreakpoints(unittest.TestCase):
    def test_two_markers_on_system_and_the_last_block_only(self):
        body = a_body()
        cached = with_cache_breakpoints(body)
        self.assertEqual(markers(cached), 2)
        self.assertEqual(cached["system"], [{"type": "text", "text": body["system"], "cache_control": CACHE_MARK}])
        self.assertIs(cached["tools"], body["tools"], "tools are shared, not copied or marked")
        self.assertEqual(markers(cached["tools"]), 0)
        self.assertIs(cached["messages"][0], body["messages"][0])
        self.assertIs(cached["messages"][1], body["messages"][1])
        tail = cached["messages"][-1]["content"]
        self.assertEqual(tail[-1]["cache_control"], CACHE_MARK)
        self.assertEqual(tail[-1]["type"], "tool_result")

    def test_the_rendered_prompt_is_byte_identical(self):
        body = a_body()
        self.assertEqual(rendered(with_cache_breakpoints(body)), rendered(body))
        self.assertEqual(json.dumps(rendered(with_cache_breakpoints(body)), sort_keys=True),
                         json.dumps(rendered(body), sort_keys=True))

    def test_the_caller_s_body_is_never_mutated(self):
        body = a_body()
        before = copy.deepcopy(body)
        with_cache_breakpoints(body)
        self.assertEqual(body, before)
        self.assertEqual(markers(body), 0)

    def test_a_lone_string_opening_is_sent_exactly_as_written(self):
        body = {**a_body(), "messages": [{"role": "user", "content": "Design from the source."}]}
        cached = with_cache_breakpoints(body)
        self.assertIs(cached["messages"], body["messages"])
        self.assertEqual(markers(cached), 1)

    def test_a_string_tail_puts_the_marker_on_the_turn_before_it(self):
        body = {**a_body(), "messages": [*a_body()["messages"],
                                         {"role": "user", "content": "Continue."}]}
        cached = with_cache_breakpoints(body)
        self.assertEqual(markers(cached), 2)
        self.assertIs(cached["messages"][-1], body["messages"][-1], "the string is untouched")
        self.assertEqual(cached["messages"][-2]["content"][-1]["cache_control"], CACHE_MARK)
        self.assertEqual(cached["messages"][-2]["content"][-1]["type"], "tool_result")
        self.assertEqual(rendered(cached), rendered(body))

    def test_the_tail_marker_walks_back_past_a_thinking_block(self):
        body = {**a_body(), "messages": [{"role": "assistant", "content": [
            {"type": "text", "text": "done"}, {"type": "thinking", "thinking": "", "signature": "s"}]}]}
        cached = with_cache_breakpoints(body)
        blocks = cached["messages"][-1]["content"]
        self.assertEqual(blocks[0]["cache_control"], CACHE_MARK)
        self.assertNotIn("cache_control", blocks[1])
        self.assertEqual(markers(cached), 2)

    def test_no_cacheable_block_means_no_tail_marker(self):
        body = {**a_body(), "messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "s"}]}]}
        cached = with_cache_breakpoints(body)
        self.assertEqual(markers(cached), 1)
        self.assertEqual(cached["messages"], body["messages"])
        self.assertNotIn("thinking", CACHEABLE_BLOCKS)

    def test_a_system_block_list_keeps_its_blocks_and_marks_the_last(self):
        body = {**a_body(), "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]}
        cached = with_cache_breakpoints(body)
        self.assertEqual(cached["system"], [{"type": "text", "text": "A"},
                                            {"type": "text", "text": "B", "cache_control": CACHE_MARK}])
        self.assertEqual(rendered(cached), rendered(body))

    def test_an_unrecognised_system_shape_is_refused_not_guessed(self):
        for system in (None, "", [], [{"type": "image"}], 42):
            with self.subTest(system=system), self.assertRaises(ValueError):
                with_cache_breakpoints({**a_body(), "system": system})


class DesignLoopRequests(unittest.TestCase):
    """Every request the loop sends is marked; nothing it stores is."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = self.root / "candidate"
        fixture = self.bundle / "environment/repo"
        fixture.mkdir(parents=True)
        (fixture / "app.py").write_text("print('hello')\n")

    def test_every_request_is_cached_and_the_record_carries_no_marker(self):
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.root,
                                     stats={}, moments=[], before=[], after=[], changed_paths=[],
                                     evidence_summary={}, evidence_index=[])
        responses = [
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Let me look."}]},
            {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "reject", "name": "reject_seed",
                 "input": {"reason": "no executable truth source", "source_evidence": []}}]}]
        sent = []
        rejection = {"status": "rejected", "reason": "no executable truth source"}
        with patch("pipeline.dump.recover.load_trace", return_value={"messages": []}), \
             patch("pipeline.author.agent_designer._handlers", return_value={
                 "draft_spec": lambda: {}, "submit_check": lambda spec: {"clean": True}}), \
             patch("pipeline.author.agent_designer._opening", return_value="Read the source."), \
             patch("pipeline.author.agent_designer._rejection", return_value=dict(rejection)), \
             patch("pipeline.author.agent_designer._call",
                   side_effect=lambda body: sent.append(copy.deepcopy(body)) or responses[len(sent) - 1]):
            spec = design({}, extraction, self.bundle, max_steps=10)
            self.assertTrue(spec["_source_capacity_report"]["validation_deferred"])
        self.assertEqual(len(sent), 2)
        for body in sent:
            self.assertEqual(body["system"][-1]["cache_control"], CACHE_MARK)
            self.assertLessEqual(markers(body), 4)
        # Request 1 carries the opening alone, a string: system marker only,
        # and the opening goes out exactly as `_opening` composed it.
        self.assertEqual(markers(sent[0]), 1)
        self.assertEqual(len(sent[0]["messages"]), 1)
        self.assertIsInstance(sent[0]["messages"][0]["content"], str)
        self.assertIn("Read the source.", sent[0]["messages"][0]["content"])
        # Request 2 ends in the string "Continue." nudge, so the tail marker
        # sits on the assistant turn before it; the opening is re-sent as is.
        self.assertEqual(len(sent[1]["messages"]), 3)
        self.assertEqual(markers(sent[1]), 2)
        self.assertEqual(sent[1]["messages"][0], sent[0]["messages"][0])
        self.assertEqual(sent[1]["messages"][1]["content"][-1]["cache_control"], CACHE_MARK)
        self.assertIsInstance(sent[1]["messages"][2]["content"], str)
        # What the loop stores never carries a marker, so nothing accumulates
        # across turns and a retained session resumes with the same shape.
        sessions = list((self.bundle / "verification/design-sessions").glob("*"))
        self.assertEqual(len(sessions), 1, sessions)
        for name in ("request-config.json", "conversation.json"):
            record = json.loads((sessions[0] / name).read_text())
            self.assertEqual(markers(record), 0, name)
        config = json.loads((sessions[0] / "request-config.json").read_text())
        self.assertIsInstance(config["system"], str)


if __name__ == "__main__":
    unittest.main()
