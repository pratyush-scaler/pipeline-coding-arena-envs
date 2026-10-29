"""The designer's trap research: recorded web research, the researched-trap floor, diversity."""
import json
from pathlib import Path
import tempfile
import unittest

from pipeline import policy
from pipeline.author import creation, trap_research
from pipeline.tests import synthetic_code


def _search_response(text, urls, stop="end_turn"):
    return {"stop_reason": stop, "usage": {"server_tool_use": {"web_search_requests": 1}},
            "content": [{"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "q"}},
                        {"type": "web_search_tool_result", "tool_use_id": "srv_1",
                         "content": [{"type": "web_search_result", "url": u, "title": "t"} for u in urls]},
                        {"type": "text", "text": text}]}


FINDINGS = ('```json\n[{"mode": "splitlines_boundaries", "family": "text", "aims_at": "astra", '
            '"executable_fact": "\'a\\\\x0cb\'.splitlines() == [\'a\', \'b\']", "sources": []}]\n```')


def _trap(i, rid="r_01", url="https://docs.python.org/3/library/stdtypes.html", kind="text_semantics"):
    return {"id": f"t_{i:02d}", "type": kind, "surface": "app.py", "provenance": "authored",
            "checks": ["o_a"], "mutant": f"m_{i:02d}", "beatable_by": "read the documented line boundaries",
            "grounding": "the parser splits transcripts into lines before matching speakers",
            "grounded_in": "app.py",
            "research": {"research_id": rid, "sources": [url],
                         "finding": "str.splitlines also splits on \\x0c, \\x1c and \\u2028, unlike split('\\n')",
                         "executed": "sandbox_run python3 -c showed 3 lines where split gave 2"}}


class TrapResearch(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)

    def tool(self, responses, privacy=lambda rel, text: []):
        calls = []
        def call(body):
            calls.append(body)
            return responses[len(calls) - 1]
        return trap_research.make_tool(self.bundle, call=call, privacy=privacy), calls

    def test_a_call_is_recorded_with_its_sources_and_parsed_findings(self):
        trap_research.begin(self.bundle, 6)
        research, calls = self.tool([_search_response(FINDINGS, ["https://docs.python.org/3/library/stdtypes.html"])])
        out = research(question="splitlines semantics in CPython 3.12", stack="python 3.12")
        self.assertEqual(out["research_id"], "r_01")
        self.assertEqual(out["findings"][0]["mode"], "splitlines_boundaries")
        self.assertEqual(out["sources"], ["https://docs.python.org/3/library/stdtypes.html"])
        tools = {t["type"] for t in calls[0]["tools"]}
        self.assertEqual(tools, {trap_research.WEB_SEARCH, trap_research.WEB_FETCH})
        record = trap_research.load(self.bundle)
        self.assertTrue(record["calls"][0]["ok"])
        self.assertEqual(trap_research.requirement(self.bundle), (6, {"r_01"}))

    def test_several_questions_are_researched_in_parallel_and_recorded_separately(self):
        trap_research.begin(self.bundle, 6)
        research, calls = self.tool([_search_response(FINDINGS, [f"https://a.example/{i}"]) for i in range(3)])
        out = research(questions=["q one", "q two", "q three"], stack="python 3.12")
        self.assertEqual(sorted(r["research_id"] for r in out["results"]), ["r_01", "r_02", "r_03"])
        record = trap_research.load(self.bundle)
        self.assertEqual([c["ok"] for c in record["calls"]], [True, True, True])
        self.assertFalse(any(c.get("pending") for c in record["calls"]))
        self.assertEqual(out["remaining_calls"], policy.TRAP_RESEARCH_MAX_CALLS - 3)

    def test_pause_turn_is_continued_with_the_assistant_content(self):
        trap_research.begin(self.bundle, 6)
        paused = _search_response("", ["https://a.example/x"], stop="pause_turn")
        research, calls = self.tool([paused, _search_response(FINDINGS, ["https://b.example/y"])])
        out = research(question="q", stack="python")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["messages"][-1], {"role": "assistant", "content": paused["content"]})
        self.assertEqual(set(out["sources"]), {"https://a.example/x", "https://b.example/y"})

    def test_a_question_carrying_private_data_is_never_sent(self):
        trap_research.begin(self.bundle, 6)
        research, calls = self.tool([], privacy=lambda rel, text: [{"kind": "email"}])
        self.assertIn("private data", research(question="who is jane@example.com", stack="python")["error"])
        self.assertEqual(calls, [])

    def test_repeated_provider_failure_waives_the_floor(self):
        trap_research.begin(self.bundle, 6)
        def boom(body):
            raise RuntimeError("HTTP 401")
        research = trap_research.make_tool(self.bundle, call=boom, privacy=lambda rel, text: [])
        for _ in range(policy.TRAP_RESEARCH_UNAVAILABLE_AFTER):
            out = research(question="q", stack="python")
        self.assertTrue(out["unavailable"])
        self.assertEqual(trap_research.requirement(self.bundle)[0], 0)

    def test_the_call_budget_is_bounded(self):
        trap_research.begin(self.bundle, 6)
        research, _ = self.tool([_search_response(FINDINGS, ["https://a.example/x"])] * policy.TRAP_RESEARCH_MAX_CALLS)
        for _ in range(policy.TRAP_RESEARCH_MAX_CALLS):
            self.assertNotIn("error", research(question="q", stack="python"))
        self.assertIn("budget", research(question="q", stack="python")["error"])

    def test_creation_needs_the_floor_of_recorded_researched_traps(self):
        trap_research.begin(self.bundle, policy.MIN_RESEARCHED_TRAPS)
        research, _ = self.tool([_search_response(FINDINGS, ["https://docs.python.org/3/library/stdtypes.html"])])
        research(question="q", stack="python")
        spec = synthetic_code.spec()
        spec["traps"] = [_trap(i) for i in range(policy.MIN_RESEARCHED_TRAPS - 1)]
        missing = creation.inventory(spec, self.bundle)["missing_components"]
        self.assertTrue(any(m.startswith("researched traps (5/6)") for m in missing), missing)
        spec["traps"].append(_trap(9, rid="r_07"))  # not on record: does not count
        self.assertTrue(any("researched traps" in m for m in creation.inventory(spec, self.bundle)["missing_components"]))
        spec["traps"][-1] = _trap(9)
        missing = creation.inventory(spec, self.bundle)["missing_components"]
        self.assertFalse(any(m.startswith("researched traps (") for m in missing), missing)
        # 2026-10-28: the floor is met, and the researched traps must also spread over failure modes
        self.assertTrue(any(m.startswith("researched failure modes (") for m in missing), missing)

    def test_revisions_and_unrecorded_bundles_carry_no_floor(self):
        spec = synthetic_code.spec()
        self.assertFalse(any("researched" in m for m in creation.inventory(spec, self.bundle)["missing_components"]))
        trap_research.begin(self.bundle, 0)
        self.assertFalse(any("researched" in m for m in creation.inventory(spec, self.bundle)["missing_components"]))

    def test_advisories_name_stray_sources_malformed_research_and_one_dominant_type(self):
        trap_research.begin(self.bundle, policy.MIN_RESEARCHED_TRAPS)
        research, _ = self.tool([_search_response(FINDINGS, ["https://docs.python.org/3/library/stdtypes.html"])])
        research(question="q", stack="python")
        traps = [_trap(1), _trap(2, url="https://elsewhere.example/z"), _trap(3), _trap(4)]
        traps[2]["research"] = {"research_id": "r_01"}
        notes = trap_research.advisories({"traps": traps}, self.bundle)
        self.assertTrue(any("sources not returned by r_01" in n for n in notes))
        self.assertTrue(any("needs research_id" in n for n in notes))
        self.assertTrue(any("trap diversity" in n for n in notes))
        self.assertTrue(any("the floor for a fresh design" in n for n in notes))

    def test_a_resumed_session_sees_the_research_already_on_record(self):
        trap_research.begin(self.bundle, 6)
        research, _ = self.tool([_search_response(FINDINGS, ["https://docs.python.org/3/library/stdtypes.html"])])
        research(question="splitlines semantics", stack="python 3.12")
        text = trap_research.opening_text(self.bundle, None, 6)
        self.assertIn("RESEARCH ALREADY ON RECORD (1 call(s)", text)
        self.assertIn("r_01", text)
        self.assertIn("splitlines_boundaries", text)

    def test_batch_diversity_reads_the_ledger_and_sibling_specs(self):
        tree = self.bundle / "tree"
        bundle = tree / "target-F2-scenario" / "c-001-a"
        sibling = tree / "target-F2-scenario" / "c-002-b"
        (sibling / "authoring").mkdir(parents=True)
        bundle.mkdir(parents=True)
        (sibling / "authoring/spec.json").write_text(json.dumps({"traps": [{"type": "spec_misread"}] * 3}))
        ledger = self.bundle / "ledger.json"
        ledger.write_text(json.dumps({"tasks": 9, "decisive_type_share": {"spec_misread": 0.5, "semantic_decoy": 0.5}}))
        out = trap_research.batch_diversity(bundle, ledger)
        self.assertEqual(out["tasks"], 10)
        self.assertEqual(out["over_used"][0], "spec_misread")
        text = trap_research.opening_text(bundle, ledger, 6)
        self.assertIn("MINIMUM: 6 researched traps", text)
        self.assertIn("library_semantics", text)


if __name__ == "__main__":
    unittest.main()
