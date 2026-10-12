"""Creation writes all task components while acceptance remains downstream."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline import checkpoint
from pipeline.artifacts import atomic_json
from pipeline.author import agent_designer as author, build, creation
from pipeline.author.contracts import ContractError
from pipeline.tests import synthetic_code


def response(name, data=None, tag="call"):
    return {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": tag, "name": name, "input": data or {}}]}


class CreationCompletion(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.extraction = SimpleNamespace(session_id="synthetic", seed_index=0,
                                         source_root=self.root, moments=[])
        provenance = {}
        for rel, text in synthetic_code.repo_files().items():
            path = self.root / "environment/repo" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            provenance[rel] = {"reason": "Synthetic source for transport-independent testing",
                               "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        atomic_json(self.root / "authoring/extraction/fixture-authored.json", {"files": provenance})
        self.spec = synthetic_code.spec(two_steps=True)

    def run_author(self, responses, *, initial=None, steps=None, resume=None):
        with ExitStack() as stack:
            stack.enter_context(patch("pipeline.dump.recover.load_trace", return_value={"messages": []}))
            stack.enter_context(patch("pipeline.privacy.scan", return_value=[]))
            stack.enter_context(patch.object(author, "_opening", return_value="Write every task component."))
            stack.enter_context(patch.object(author.extract_mod, "usable_correction_count", return_value=0))
            validator = stack.enter_context(patch.object(author.validate_mod, "validate",
                side_effect=AssertionError("creation cannot run acceptance validation")))
            controls = stack.enter_context(patch.object(author.controls_mod, "run",
                side_effect=AssertionError("creation cannot require controls")))
            docker = stack.enter_context(patch.object(author.sandbox, "available",
                side_effect=AssertionError("creation cannot require Docker")))
            call = stack.enter_context(patch.object(author, "_call", side_effect=responses))
            spec = author.design({}, self.extraction, self.root, initial_spec=initial,
                                 max_steps=steps or len(responses), resume_session=resume)
            validator.assert_not_called()
            controls.assert_not_called()
            docker.assert_not_called()
            return spec, call

    def test_early_submission_finishes_both_steps_and_exports_all_artifacts(self):
        partial = deepcopy(self.spec)
        del partial["alternative"]["02-counts"]
        partial["mutants"][0].pop("patch")
        spec, calls = self.run_author([
            response("submit_draft"),
            response("stage_spec", {"path": ["alternative", "02-counts"],
                                    "value": self.spec["alternative"]["02-counts"]}),
            response("stage_spec", {"path": ["mutants", 0, "patch"],
                                    "value": self.spec["mutants"][0]["patch"]}),
            response("submit_draft"),
        ], initial=partial)
        self.assertEqual(calls.call_count, 4)
        acknowledgement = next(m["content"][0] for m in reversed(calls.call_args_list[1].args[0]["messages"])
                               if m["role"] == "user" and isinstance(m["content"], list))
        self.assertNotIn("is_error", acknowledgement)
        saved = json.loads(acknowledgement["content"])
        self.assertTrue(saved["saved"])
        self.assertIn("alternative[02-counts].patch", saved["missing_components"])
        self.assertEqual(spec["_agent"]["creation_status"], "complete")
        self.assertEqual(spec["_agent"]["validation_status"], "deferred")
        report = json.loads((self.root / "verification/creation.json").read_text())
        self.assertTrue(report["components_present"])
        self.assertTrue(report["validation_deferred"])
        self.assertEqual(report["missing_components"], [])
        self.assertEqual(checkpoint.submitted_spec(self.root), spec)
        sessions = list((self.root / "verification/design-sessions").iterdir())
        self.assertEqual(json.loads((sessions[0] / "submissions/001.json").read_text())["name"], partial["name"])
        build.build(spec, self.root)
        for step in ("01-fix", "02-counts"):
            self.assertTrue((self.root / f"steps/{step}/instruction.md").is_file())
            self.assertTrue((self.root / f"steps/{step}/solution/fix.patch").is_file())
            self.assertTrue((self.root / f"authoring/alt/{step}.patch").is_file())
        self.assertTrue((self.root / "tests/suites/test_counts.py").is_file())

    def test_one_shot_partial_design_becomes_the_live_draft(self):
        partial = deepcopy(self.spec)
        partial.pop("suites")
        spec, _ = self.run_author([
            response("design", partial),
            response("stage_spec", {"path": ["suites"], "value": self.spec["suites"]}),
            response("submit_draft"),
        ])
        self.assertEqual(spec["_agent"]["creation_status"], "complete")
        self.assertEqual(spec["solution"], self.spec["solution"])

    def test_budget_end_keeps_latest_work_and_truthfully_marks_unfinished_creation(self):
        partial = {"name": "first version"}
        spec, calls = self.run_author([
            response("submit_draft"),
            response("stage_spec", {"path": ["name"], "value": "latest version"}),
        ], initial=partial)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(spec["name"], "latest version")
        self.assertEqual(spec["_agent"]["creation_status"], "unfinished")
        report = json.loads((self.root / "verification/creation.json").read_text())
        self.assertEqual(report["reason"], "session-budget-exhausted")
        self.assertIn("steps", report["missing_components"])
        self.assertTrue(report["validation_deferred"])
        self.assertEqual(checkpoint.submitted_spec(self.root)["name"], "latest version")

    def test_transport_interruption_resumes_component_completion_without_new_budget(self):
        partial = deepcopy(self.spec)
        partial.pop("alternative")
        with self.assertRaisesRegex(ConnectionError, "transport interrupted"):
            self.run_author([response("submit_draft"), ConnectionError("transport interrupted")],
                            initial=partial, steps=3)
        retained = checkpoint.read(self.root)["authoring_session"]
        self.assertEqual(retained["remaining_steps"], 2)
        spec, calls = self.run_author([
            response("stage_spec", {"path": ["alternative"], "value": self.spec["alternative"]}),
            response("submit_draft"),
        ], steps=2, resume=retained)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(spec["_agent"]["creation_status"], "complete")
        self.assertEqual(spec["solution"], partial["solution"])

    def test_quality_rejection_remains_in_validation_after_component_completion(self):
        invalid = deepcopy(self.spec)
        invalid["checks"][0]["weight"] = -100
        spec, _ = self.run_author([response("submit_draft")], initial=invalid)
        self.assertEqual(spec["_agent"]["creation_status"], "complete")
        self.assertTrue(creation.inventory(spec, self.root)["validation_deferred"])
        with self.assertRaises(ContractError):
            build.build(spec, self.root)

    def test_no_trap_floors_or_difficulty_thresholds_in_component_inventory(self):
        simple = deepcopy(self.spec)
        simple["traps"] = simple["traps"][:1]
        simple["mutants"] = [m for m in simple["mutants"] if m["id"] == simple["traps"][0]["mutant"]]
        simple["quality"]["difficulty_case"]["first_try_verdict"] = "The model may solve it."
        self.assertTrue(creation.inventory(simple, self.root)["components_present"])

    def test_provenance_presence_covers_every_fixture_file(self):
        path = self.root / "environment/repo/extra.py"
        path.write_text("pass\n")
        report = creation.inventory(self.spec, self.root)
        self.assertEqual(report["status"], "unfinished")
        self.assertIn("fixture.provenance[extra.py]", report["missing_components"])
        handlers = author._handlers(self.root, self.extraction, {}, self.root / "draft.json")
        handlers["write_fixture_file"]("extra.py", "pass\n", "An authored adjacent implementation for this source scenario.")
        self.assertTrue(creation.inventory(self.spec, self.root)["components_present"])

    def test_creation_status_lists_missing_second_step_components_without_validation(self):
        partial = deepcopy(self.spec)
        partial["solution"].pop("02-counts")
        partial["alternative"].pop("02-counts")
        atomic_json(self.root / "draft.json", partial)
        handlers = author._handlers(self.root, self.extraction, {}, self.root / "draft.json")
        with patch.object(author.validate_mod, "validate", side_effect=AssertionError("not a gate")):
            report = handlers["creation_status"]()
        self.assertIn("solution[02-counts].patch", report["missing_components"])
        self.assertIn("alternative[02-counts].patch", report["missing_components"])
        self.assertTrue(handlers["submit_check"](partial)["clean"])

    def test_generated_and_vendored_files_do_not_need_authored_provenance(self):
        path = self.root / "environment/repo/__pycache__/module.pyc"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"generated bytecode")
        self.assertTrue(creation.inventory(self.spec, self.root)["components_present"])

    def test_empty_self_audit_entries_do_not_count_as_written_components(self):
        partial = deepcopy(self.spec)
        partial["quality"]["decisions"] = [{}]
        report = creation.inventory(partial, self.root)
        self.assertEqual(report["status"], "unfinished")
        self.assertIn("quality.decisions[0].question", report["missing_components"])


if __name__ == "__main__":
    unittest.main()


class SubmitPreflight(unittest.TestCase):
    """Census 2026-10-03: deterministic defects come back once, inside the session."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.extraction = SimpleNamespace(session_id="synthetic", seed_index=0, source_root=self.root, moments=[])
        provenance = {}
        for rel, text in synthetic_code.repo_files().items():
            path = self.root / "environment/repo" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            provenance[rel] = {"reason": "Synthetic source for transport-independent testing",
                               "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        atomic_json(self.root / "authoring/extraction/fixture-authored.json", {"files": provenance})
        self.spec = synthetic_code.spec(two_steps=True)

    def run_author(self, responses, preflight_results, steps=20):
        real_handlers = author._handlers
        calls = []
        def handlers(*a, **k):
            h = real_handlers(*a, **k)
            def fake(spec):
                calls.append(spec)
                return preflight_results[len(calls) - 1]
            h["submit_preflight"] = fake
            return h
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"ARENA_TEST_SUBMIT_PREFLIGHT": "1"}))
            stack.enter_context(patch("pipeline.dump.recover.load_trace", return_value={"messages": []}))
            stack.enter_context(patch("pipeline.privacy.scan", return_value=[]))
            stack.enter_context(patch.object(author, "_opening", return_value="Write every task component."))
            stack.enter_context(patch.object(author.extract_mod, "usable_correction_count", return_value=0))
            stack.enter_context(patch.object(author, "_handlers", side_effect=handlers))
            call = stack.enter_context(patch.object(author, "_call", side_effect=responses))
            spec = author.design({}, self.extraction, self.root, initial_spec=deepcopy(self.spec), max_steps=steps)
        return spec, call, calls

    def test_findings_come_back_once_and_the_next_submit_finishes(self):
        spec, call, preflights = self.run_author(
            [response("submit_draft"), response("submit_draft")],
            [["control: mutant m_x: declares fails ['o_a'] but actually fails ['o_a', 'o_b']"]])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(preflights), 1)
        ack = json.loads(next(m["content"][0] for m in reversed(call.call_args_list[1].args[0]["messages"])
                              if m["role"] == "user" and isinstance(m["content"], list))["content"])
        self.assertTrue(ack["saved"])
        self.assertIn("declares fails", ack["preflight"][0])
        self.assertEqual(spec["_agent"]["creation_status"], "complete")

    def test_a_clean_preflight_finishes_at_once(self):
        spec, call, preflights = self.run_author([response("submit_draft")], [[]])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(preflights), 1)

    def test_too_few_steps_left_skips_it(self):
        spec, call, preflights = self.run_author([response("submit_draft")], [["x"]], steps=5)
        self.assertEqual(preflights, [])
        self.assertEqual(call.call_count, 1)


class ContextFits(unittest.TestCase):
    """2026-10-03: a design died at its 181st request with a 2.7 MB conversation."""

    def conversation(self, turns, size):
        messages = [{"role": "user", "content": "open"}]
        for i in range(turns):
            messages.append({"role": "assistant", "content": [
                {"type": "thinking", "thinking": "t", "signature": "sig"},
                {"type": "tool_use", "id": f"c{i}", "name": "read_fixture", "input": {"path": "a"}}]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"c{i}", "content": "x" * size}]})
        return messages

    def test_small_conversations_are_untouched(self):
        messages = self.conversation(10, 1000)
        before = json.dumps(messages)
        self.assertEqual(author.fit_context(messages), 0)
        self.assertEqual(json.dumps(messages), before)

    def test_old_tool_results_shrink_and_signed_turns_and_recent_ones_stay(self):
        messages = self.conversation(120, 20_000)
        before = [json.dumps(m) for m in messages]
        cut = author.fit_context(messages)
        self.assertGreater(cut, 0)
        self.assertLessEqual(len(json.dumps(messages)), author.CONTEXT_TARGET_BYTES + 25_000)
        for old, new in zip(before, messages):
            if new["role"] == "assistant":
                self.assertEqual(json.dumps(new), old)          # signed thinking untouched
        for old, new in zip(before[-author.CONTEXT_KEEP_RECENT:], messages[-author.CONTEXT_KEEP_RECENT:]):
            self.assertEqual(json.dumps(new), old)              # recent turns whole
        ids = [b["tool_use_id"] for m in messages if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"]]
        self.assertEqual(len(ids), 120)                         # every tool_use keeps its result
        self.assertEqual(author.fit_context(messages), 0)       # idempotent below the threshold
