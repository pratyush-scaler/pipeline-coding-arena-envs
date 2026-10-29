"""Long tools asked for in one designer turn run at the same time, and the last pre-review
is reserved for the submit preflight (2026-10-27).

Measured on the first bash RLE build: run_controls 44 min, pre_review 41, blind_solve 39,
research_traps 19 of a 240-minute session, one after another even when the designer asked
for two in one turn. The gate then found a problem the five designer pre-reviews had not
seen, because none of them read the submitted bytes.
"""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import policy
from pipeline.author import agent_designer as author


def turn(*names):
    return {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": f"call-{i}", "name": name, "input": {}} for i, name in enumerate(names)]}


class ConcurrentTools(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = self.root / "candidate"
        (self.bundle / "environment/repo").mkdir(parents=True)
        (self.bundle / "environment/repo/app.py").write_text("print('hello')\n")
        self.extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.root,
                                          stats={}, moments=[], before=[], after=[], changed_paths=[],
                                          evidence_summary={}, evidence_index=[])

    def run_turns(self, responses, handlers):
        rejection = {"status": "rejected", "reason": "no executable truth source"}
        sent = []
        with ExitStack() as stack:
            stack.enter_context(patch("pipeline.dump.recover.load_trace", return_value={"messages": []}))
            stack.enter_context(patch("pipeline.author.agent_designer._handlers", return_value={
                "draft_spec": lambda: {}, "submit_check": lambda spec: {"clean": True}, **handlers}))
            stack.enter_context(patch("pipeline.author.agent_designer._opening", return_value="Read the source."))
            stack.enter_context(patch("pipeline.author.agent_designer._rejection", return_value=dict(rejection)))
            stack.enter_context(patch("pipeline.author.agent_designer._call",
                                      side_effect=lambda body: sent.append(body) or responses[len(sent) - 1]))
            author.design({}, self.extraction, self.bundle, max_steps=10)
        return sent

    def test_two_long_tools_in_one_turn_overlap_and_results_keep_their_order(self):
        gate = threading.Barrier(2, timeout=10)
        seen = {}

        def long_tool(name):
            def run(**_):
                seen[name] = threading.current_thread().name
                gate.wait()          # both must be inside at the same time
                return {"tool": name}
            return run

        responses = [turn("run_controls", "blind_solve"),
                     {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "reject", "name": "reject_seed",
                                                              "input": {"reason": "done", "source_evidence": []}}]}]
        sent = self.run_turns(responses, {"run_controls": long_tool("run_controls"), "blind_solve": long_tool("blind_solve")})
        self.assertTrue(all(name.startswith("designer-tool") for name in seen.values()), seen)
        results = sent[1]["messages"][-2]["content"] if isinstance(sent[1]["messages"][-2]["content"], list) else None
        results = [m for m in sent[1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["call-0", "call-1"])
        self.assertEqual([json.loads(r["content"])["tool"] for r in results], ["run_controls", "blind_solve"])

    def test_a_turn_that_stages_runs_in_sequence(self):
        order = []

        def tool(name):
            def run(**_):
                order.append((name, threading.current_thread().name))
                return {"tool": name}
            return run

        responses = [turn("stage_spec", "run_controls"),
                     {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "reject", "name": "reject_seed",
                                                              "input": {"reason": "done", "source_evidence": []}}]}]
        self.run_turns(responses, {"stage_spec": tool("stage_spec"), "run_controls": tool("run_controls")})
        self.assertEqual([name for name, _ in order], ["stage_spec", "run_controls"])
        self.assertTrue(all(not thread.startswith("designer-tool") for _, thread in order), order)

    def test_one_failing_tool_does_not_hide_the_other(self):
        def boom(**_):
            raise RuntimeError("controls exploded")

        responses = [turn("run_controls", "blind_solve"),
                     {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "reject", "name": "reject_seed",
                                                              "input": {"reason": "done", "source_evidence": []}}]}]
        sent = self.run_turns(responses, {"run_controls": boom, "blind_solve": lambda **_: {"tool": "blind_solve"}})
        results = [m for m in sent[1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)][-1]["content"]
        self.assertIn("controls exploded", results[0]["content"])
        self.assertTrue(results[0].get("is_error"))
        self.assertEqual(json.loads(results[1]["content"])["tool"], "blind_solve")

    def test_the_switch_and_the_brief(self):
        self.assertTrue(policy.DESIGNER_CONCURRENT_TOOLS)
        self.assertIn("AT THE SAME TIME", author.SYSTEM)
        self.assertNotIn("stage_spec", author.CONCURRENT_TOOLS)
        self.assertNotIn("submit_draft", author.CONCURRENT_TOOLS)
        for name in ("run_controls", "pre_review", "blind_solve", "research_traps"):
            self.assertIn(name, author.CONCURRENT_TOOLS)


class ReservedPreReview(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        (self.bundle / "environment/repo").mkdir(parents=True)
        (self.bundle / "environment/repo/app.py").write_text("print(1)\n")
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle, moments=[])
        self.log = self.bundle / "authoring/log"
        self.handlers = author._handlers(self.bundle, extraction, {"messages": []}, self.bundle / "draft.json", log=self.log)
        self.handlers["stage_spec"](path=["name"], value="x-task")

    def test_the_designer_keeps_all_but_the_reserved_review(self):
        from pipeline.config import MAX_PRE_REVIEWS_PER_SESSION
        self.assertEqual(policy.SUBMIT_PREFLIGHT_RESERVED_REVIEWS, 1)
        yours = MAX_PRE_REVIEWS_PER_SESSION - policy.SUBMIT_PREFLIGHT_RESERVED_REVIEWS

        def fake_review(dest, model):
            work = dest / "verification/reviews/x/attempt"
            (work / "deliverables").mkdir(parents=True)
            (work / "deliverables/review.json").write_text(json.dumps({"verdict": "pass", "checks": []}))
            return {"work_dir": work.relative_to(dest).as_posix(), "artifact_sha256": "a" * 64, "errors": []}

        with patch.object(author.validate_mod, "validate", return_value=[]), \
             patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]), \
             patch("pipeline.ledger.faithfulness.build", return_value={"schema": "x"}), \
             patch.object(author.build_mod, "build", lambda spec, dest: (dest / "task.toml").write_text("x")), \
             patch.object(author, "MODEL", "claude-opus-5-5"), \
             patch("pipeline.review.review", side_effect=fake_review) as review:
            for n in range(yours):
                self.handlers["stage_spec"](path=["instruction"], value=f"draft {n}\n")
                self.assertNotIn("error", self.handlers["pre_review"]())
            refused = self.handlers["pre_review"]()
        self.assertIn("reserved for the submit preflight", refused["error"])
        self.assertEqual(review.call_count, yours)

    def test_a_repair_session_gets_one_of_its_own(self):
        """2026-10-27: each repair session of the first bash RLE build spent all five pre-reviews."""
        self.assertEqual(policy.REVISION_PRE_REVIEWS, 1)
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle, moments=[])
        handlers = author._handlers(self.bundle, extraction, {"messages": []}, self.bundle / "draft2.json",
                                    log=self.bundle / "authoring/log2", revision=True)
        handlers["stage_spec"](path=["name"], value="x-task")

        def fake_review(dest, model):
            work = dest / "verification/reviews/x/attempt"
            (work / "deliverables").mkdir(parents=True)
            (work / "deliverables/review.json").write_text(json.dumps({"verdict": "pass", "checks": []}))
            return {"work_dir": work.relative_to(dest).as_posix(), "artifact_sha256": "b" * 64, "errors": []}

        with patch.object(author.validate_mod, "validate", return_value=[]), \
             patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]), \
             patch("pipeline.ledger.faithfulness.build", return_value={"schema": "x"}), \
             patch.object(author.build_mod, "build", lambda spec, dest: (dest / "task.toml").write_text("x")), \
             patch.object(author, "MODEL", "claude-opus-5-5"), \
             patch("pipeline.review.review", side_effect=fake_review) as review:
            self.assertNotIn("error", handlers["pre_review"]())
            refused = handlers["pre_review"]()
        self.assertIn("repair session", refused["error"])
        self.assertIn("reserved for the submit preflight", refused["error"])
        self.assertEqual(review.call_count, 1)

    def test_without_the_preflight_review_the_whole_budget_is_the_designers(self):
        with patch.object(policy, "SUBMIT_PREFLIGHT_REVIEW", False), \
             patch.object(author.validate_mod, "validate", return_value=["not clean"]):
            out = self.handlers["pre_review"]()
        self.assertIn("check_draft", out["error"], "the budget is not the refusal here")


class ReservedReviewAtTheNextSubmission(unittest.TestCase):
    """The first preflight's static problems kept the reserved review from reading the
    draft; the next complete submission gets the review alone, then the one after finishes."""

    def setUp(self):
        from pipeline.tests.test_creation_completion import SubmitPreflight
        self.base = SubmitPreflight("test_a_clean_preflight_finishes_at_once")
        self.base.setUp()
        self.root, self.extraction, self.spec = self.base.root, self.base.extraction, self.base.spec

    def run_author(self, responses, preflight_results, review_results, steps=20):
        from copy import deepcopy
        real_handlers = author._handlers
        preflights, reviews = [], []

        def handlers(*a, **k):
            h = real_handlers(*a, **k)
            h["submit_preflight"] = lambda spec: preflights.append(spec) or preflight_results[len(preflights) - 1]
            h["submit_preflight_review"] = lambda spec: reviews.append(spec) or review_results[len(reviews) - 1]
            return h

        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"ARENA_TEST_SUBMIT_PREFLIGHT": "1"}))
            stack.enter_context(patch("pipeline.dump.recover.load_trace", return_value={"messages": []}))
            stack.enter_context(patch("pipeline.privacy.scan", return_value=[]))
            stack.enter_context(patch.object(author, "_opening", return_value="Write every task component."))
            stack.enter_context(patch.object(author.extract_mod, "usable_correction_count", return_value=0))
            # The researched-trap floor and the hardness gate have their own tests; the
            # synthetic spec must count as complete here.
            stack.enter_context(patch.object(author.policy, "MIN_RESEARCHED_TRAPS", 0))
            stack.enter_context(patch.object(author.policy, "DIFFICULTY_GATE_AT_CREATION", False))
            stack.enter_context(patch.object(author, "_handlers", side_effect=handlers))
            call = stack.enter_context(patch.object(author, "_call", side_effect=responses))
            author.design({}, self.extraction, self.root, initial_spec=deepcopy(self.spec), max_steps=steps)
        return call, preflights, reviews

    def submit(self):
        from pipeline.tests.test_creation_completion import response
        return response("submit_draft")

    def test_static_problems_then_the_reserved_review_then_the_finish(self):
        call, preflights, reviews = self.run_author(
            [self.submit(), self.submit(), self.submit()],
            [["static: deliverables entries must be {path, kind: json|file, step}"]],
            [["review (revise): o_visible_fixes: an unpublished vocabulary gate"]])
        self.assertEqual((len(preflights), len(reviews), call.call_count), (1, 1, 3))
        ack = json.loads(next(m["content"][0] for m in reversed(call.call_args_list[2].args[0]["messages"])
                              if m["role"] == "user" and isinstance(m["content"], list))["content"])
        self.assertIn("reserved review", ack["next"])
        self.assertIn("o_visible_fixes", ack["preflight"][0])
        record = json.loads((self.root / "verification/design-sessions" / next(
            p.name for p in (self.root / "verification/design-sessions").iterdir()) / "submissions/002-preflight.json").read_text())
        self.assertTrue(record["review_only"])

    def test_a_clean_reserved_review_finishes_the_session(self):
        call, preflights, reviews = self.run_author([self.submit(), self.submit()],
                                                    [["static: one thing"]], [[]])
        self.assertEqual((len(preflights), len(reviews), call.call_count), (1, 1, 2))


if __name__ == "__main__":
    unittest.main()
