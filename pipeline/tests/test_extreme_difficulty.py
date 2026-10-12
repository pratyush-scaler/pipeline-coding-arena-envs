"""Creation stays open; semantic difficulty and its measurement stay honest."""
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pipeline import checkpoint, harden, metrics, orchestrate, policy
from pipeline.author import agent_designer as author, blind_solve, difficulty
from pipeline.measurement import extreme_confirmation, success_observation
from pipeline.tests.test_harden_traps import bundle_with, trap


class CreationAndValidation(unittest.TestCase):
    def test_deadline_message_still_allows_submission_without_checks(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            (root / "environment/repo").mkdir(parents=True)
            extraction = SimpleNamespace(session_id="s", seed_index=0)
            stack.enter_context(patch("pipeline.dump.recover.load_trace", return_value={"messages": []}))
            stack.enter_context(patch("pipeline.privacy.scan", return_value=[]))
            stack.enter_context(patch.object(author, "_opening", return_value="Create a candidate."))
            stack.enter_context(patch.object(author.extract_mod, "usable_correction_count", return_value=0))
            call = stack.enter_context(patch.object(author, "_call", return_value={
                "stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "submit",
                "name": "submit_draft", "input": {}}]}))
            spec = author.design({}, extraction, root, max_steps=3, initial_spec={"name": "Deadline candidate"})
            request = repr(call.call_args.args[0]["messages"])
            self.assertIn("even if checks or controls have not passed", request)
            self.assertNotIn("Complete validation", request)
            self.assertEqual(spec["_agent"]["validation_status"], "deferred")

    def test_incomplete_candidate_submits_even_when_validators_and_docker_are_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            (root / "environment/repo").mkdir(parents=True)
            (root / "environment/repo/core.py").write_text("old\n")
            extraction = SimpleNamespace(session_id="s", seed_index=0, moments=[])
            stack.enter_context(patch("pipeline.dump.recover.load_trace", return_value={"messages": []}))
            stack.enter_context(patch("pipeline.privacy.scan", return_value=[]))
            stack.enter_context(patch.object(author, "_opening", return_value="Create the strongest candidate."))
            stack.enter_context(patch.object(author.extract_mod, "usable_correction_count", return_value=0))
            validator = stack.enter_context(patch.object(author.validate_mod, "validate", side_effect=AssertionError("creation must not validate")))
            controls = stack.enter_context(patch.object(author.controls_mod, "run", side_effect=AssertionError("creation must not run controls")))
            docker = stack.enter_context(patch.object(author.sandbox, "available", side_effect=AssertionError("creation must not need Docker")))
            stack.enter_context(patch.object(author, "_call", side_effect=[
                {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "stage", "name": "stage_spec",
                  "input": {"path": ["name"], "value": "Incomplete candidate"}}]},
                {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "submit", "name": "submit_draft", "input": {}}]},
            ]))
            spec = author.design({}, extraction, root, max_steps=2)
            self.assertEqual(spec["_agent"]["validation_status"], "deferred")
            self.assertEqual(checkpoint.submitted_spec(root)["name"], "Incomplete candidate")
            self.assertNotIn("checks", spec)
            validator.assert_not_called()
            controls.assert_not_called()
            docker.assert_not_called()

    def test_incomplete_candidate_is_retained_and_blocked_in_export_validation(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            extraction = SimpleNamespace(session_id="s", seed_index=0)
            stack.enter_context(patch.object(orchestrate, "_slug", return_value="candidate"))
            stack.enter_context(patch.object(orchestrate.extract_stage, "extract", return_value=extraction))
            stack.enter_context(patch.object(orchestrate.extract_stage, "write"))
            stack.enter_context(patch.object(orchestrate.extract_stage, "usable_correction_count", return_value=0))
            stack.enter_context(patch("pipeline.ledger.redaction.minimize"))
            controls = stack.enter_context(patch.object(orchestrate.controls_stage, "run"))
            designer = Mock(return_value={"name": "Retain even incomplete"})
            result = orchestrate._build_one({"session_id": "s", "seed_index": 0}, 1,
                                           root, designer, orchestrate.Budget(1), gate_repairs=0)
            self.assertEqual(result.status, "blocked", result.error)
            self.assertEqual(result.stage_reached, "export-validation")
            self.assertEqual(result.creation_status, "unfinished")
            creation_receipt = json.loads((root / "candidate/verification/creation.json").read_text())
            self.assertEqual(creation_receipt["status"], "unfinished")
            self.assertIn("steps", creation_receipt["missing_components"])
            candidate = root / "candidate"
            self.assertEqual(json.loads((candidate / "authoring/spec.json").read_text())["name"], "Retain even incomplete")
            receipt = json.loads((candidate / "verification/submission-validation.json").read_text())
            self.assertEqual(receipt["phase"], "validation")
            self.assertEqual(receipt["verdict"], "blocked")
            designer.assert_called_once()
            controls.assert_not_called()

    def test_source_capacity_rejection_happens_only_after_retaining_the_candidate(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            extraction = SimpleNamespace(session_id="s", seed_index=0, moments=[])
            stack.enter_context(patch.object(orchestrate, "_slug", return_value="candidate"))
            stack.enter_context(patch.object(orchestrate.extract_stage, "extract", return_value=extraction))
            def extracted(extraction, bundle):
                (bundle / "environment/repo").mkdir(parents=True)
                (bundle / "environment/repo/brief.md").write_text("component records are absent.")
            stack.enter_context(patch.object(orchestrate.extract_stage, "write", side_effect=extracted))
            stack.enter_context(patch("pipeline.ledger.redaction.minimize"))
            exported = stack.enter_context(patch.object(orchestrate.build_stage, "build"))
            controls = stack.enter_context(patch.object(orchestrate.controls_stage, "run"))
            spec = {"name": "Retained weak-source candidate", "_source_capacity_report": {
                "reason": "The fixture has only an unsupported final total and no component records. " * 4,
                "source_evidence": [{"file": "brief.md", "quote": "component records are absent."}],
                "validation_deferred": True}}
            result = orchestrate._build_one({"session_id": "s", "seed_index": 0}, 1,
                                           root, Mock(return_value=spec), orchestrate.Budget(1), gate_repairs=0)
            self.assertEqual(result.status, "reserve", result.error)
            self.assertEqual(result.stage_reached, "source-capacity-validation")
            candidate = root / "candidate"
            self.assertEqual(json.loads((candidate / "authoring/spec.json").read_text())["name"], spec["name"])
            self.assertEqual(json.loads((candidate / "verification/submission-validation.json").read_text())["phase"], "validation")
            rejection = json.loads((candidate / "verification/source-rejection.json").read_text())
            self.assertFalse(rejection["validation_deferred"])
            exported.assert_not_called()
            controls.assert_not_called()

    def test_source_capacity_evidence_is_rechecked_against_the_retained_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment/repo").mkdir(parents=True)
            (root / "environment/repo/brief.md").write_text("the source now contains component records")
            spec = {"_source_capacity_report": {
                "reason": "The source cannot support this coding task without inventing component records. " * 4,
                "source_evidence": [{"file": "brief.md", "quote": "component records are absent."}]}}
            with self.assertRaisesRegex(ValueError, "quote is absent"):
                author.validate_source_capacity(spec, root)

    def test_profile_can_inspect_an_incomplete_draft_and_metadata_is_optional(self):
        for draft in ({}, {"quality": []}, {"traps": None, "mutants": "unfinished"},
                      {"quality": {"difficulty_case": {"hypotheses": [{"mechanism": []}]}}}):
            self.assertTrue(difficulty.profile(draft)["advisory_only"])
        self.assertEqual(difficulty.hypothesis_problems({}), [])

    def test_supplied_hypotheses_have_validated_causal_references(self):
        def hypothesis(hid, mechanism, peer):
            return {"id": hid, "mechanism": mechanism, "traps": ["t_identity"], "checks": ["o_result"],
                    "tempting_rule": "normalize every lookalike", "correct_rule": "merge only declared aliases",
                    "distinguishing_case": "a lookalike with a different owner", "interaction_with": [peer]}
        draft = {"traps": [{"id": "t_identity"}], "checks": [{"id": "o_result", "kind": "objective"}],
                 "quality": {"difficulty_case": {"hypotheses": [
                     hypothesis("h_identity", "representation_collision", "h_state"),
                     hypothesis("h_state", "temporal_state", "h_identity")]}}}
        self.assertEqual(difficulty.hypothesis_problems(draft), [])
        self.assertEqual(difficulty.profile(draft)["hypothesis_count"], 2)
        draft["quality"]["difficulty_case"]["hypotheses"][1]["interaction_with"] = ["missing"]
        self.assertTrue(any("interaction_with" in p for p in difficulty.hypothesis_problems(draft)))

    def test_unregistered_shortcut_probe_carries_earlier_code_and_records_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment/repo").mkdir(parents=True)
            (root / "environment/repo/core.py").write_text("old\n")
            handlers = author._handlers(root, SimpleNamespace(session_id="s", seed_index=0, moments=[]), {}, root / "draft.json",
                                        log=root / "verification/probes")
            handlers["stage_spec"]([], {"checks": [{"id": "o_result", "kind": "objective"},
                                                    {"id": "g_guard", "kind": "guardrail"}]})
            def export(spec, dest):
                (dest / "task.toml").write_text("[[steps]]\nname='01-fix'\n[[steps]]\nname='02-revise'\n[[steps]]\nname='03-carry'\n")
            grading = {"valid": True, "reward": .5, "steps": {"02-revise": {"grade": {"checks": {
                "o_result": {"passed": False}, "g_guard": {"passed": True}}}}}}
            with patch.object(author.sandbox, "available", return_value=(True, "")), \
                 patch.object(author.build_mod, "build", side_effect=export), \
                 patch.object(author.sandbox, "solve_variant", return_value=author.sandbox.Variant("nop")) as reference, \
                 patch.object(author.sandbox, "verify", return_value=grading) as verify:
                result = handlers["probe_patch"]("02-revise", "candidate diff", hypothesis="h_identity", expected_fails=["o_result"])
            self.assertEqual(list(verify.call_args.args[1]), ["01-fix", "02-revise"])
            self.assertEqual(reference.call_args.args[1], "01-fix")
            self.assertEqual(result["failing_objectives"], ["o_result"])
            self.assertEqual(result["unexpected_failures"], [])
            self.assertTrue(result["advisory_only"])
            self.assertNotIn("mutants", handlers["draft_spec"]())
            receipts = list(root.glob("verification/**/hypothesis-probe-*.json"))
            self.assertEqual(len(receipts), 1)


class ScenarioHardening(unittest.TestCase):
    def test_scenario_additions_have_no_arbitrary_ceiling_but_cannot_relabel_real_corrections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = bundle_with(root, 10, 1)
            budget = harden.faithfulness_budget(root, ledger, preserve_level=False)
        self.assertEqual(budget["minimum_level"], "F2")
        self.assertIsNone(budget["authored_traps_left"])
        before = [trap(0, "trace_derived")]
        after = before + [trap(i, "authored") for i in range(1, 1001)]
        self.assertEqual(harden.budget_problems(budget, before, after, 10**9), [])
        self.assertTrue(harden.budget_problems(budget, before, after[1:], 0))

    def test_general_target_does_not_stop_extreme_hardening(self):
        record = {"measurement_complete": True, "meets_target": True, "models": ["a", "b"],
                  "per_model": {"a": {"pass_at_1": .4}, "b": {"pass_at_1": .2}}}
        self.assertFalse(harden.extreme_target_met(record))
        for row in record["per_model"].values():
            row["pass_at_1"] = 0.
        self.assertTrue(harden.extreme_target_met(record))
        record["measurement_complete"] = False
        self.assertFalse(harden.extreme_target_met(record))
        record["measurement_complete"] = True
        record["models"] = ["a"]
        self.assertFalse(harden.extreme_target_met(record))


class HonestConfirmation(unittest.TestCase):
    def test_zero_of_five_does_not_establish_five_percent(self):
        measured = success_observation(0, 5)
        self.assertAlmostEqual(measured["one_sided_upper"], 1 - .05**.2)
        cohorts = {m: {"passes": 0, "episodes_valid": 5} for m in ("a", "b")}
        self.assertFalse(extreme_confirmation(cohorts, .05, held_out=True)["confirmed"])

    def test_confirmation_requires_both_models_and_fresh_fixed_cohorts(self):
        cohorts = {m: {"passes": 0, "episodes_valid": 72} for m in ("a", "b")}
        self.assertTrue(extreme_confirmation(cohorts, .05)["bounds_below_target"])
        self.assertFalse(extreme_confirmation(cohorts, .05)["confirmed"])
        self.assertTrue(extreme_confirmation(cohorts, .05, held_out=True)["confirmed"])
        self.assertFalse(extreme_confirmation({"a": cohorts["a"]}, .05, held_out=True)["confirmed"])
        self.assertFalse(extreme_confirmation({"a": cohorts["a"], "b": {}}, .05, held_out=True)["confirmed"])

    def test_nonzero_success_bound_and_void_exclusion(self):
        self.assertAlmostEqual(success_observation(1, 10)["one_sided_upper"], .3941633024365)
        report = {"step_names": ["01"], "rollouts": [
            {"episode": 1, "step": "01", "reward": 0.},
            {"episode": 2, "step": "01", "reward": 0., "zero_class": "void"}]}
        measured = metrics.task_metrics(report)
        self.assertEqual(measured["pass_at_1_uncertainty"]["attempted"], 1)
        self.assertEqual(measured["episodes_void"], 1)


class LocalContainer:
    """Exercise real diff capture and shell quoting without Docker or providers."""
    def __init__(self, root):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "core.py").write_text("old\n")
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def exec(self, command, **kwargs):
        if command.startswith("chown "):
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        command = command.replace("/app/repo", str(self.repo)).replace("/app/deliverables", str(self.root / "deliverables"))
        command = command.replace("/tmp/", str(self.root) + "/")
        proc = subprocess.run(["bash", "-c", command], cwd=self.repo, capture_output=True, text=True)
        return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    def put_bytes(self, data, guest):
        target = (self.root / "deliverables" / guest.removeprefix("/app/deliverables/")) if guest.startswith("/app/deliverables/") else self.repo / guest.removeprefix("/app/repo/")
        target.write_bytes(data)
    def capture(self, guest, members, excludes, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for member in members:
            if guest == "/app":
                source = self.root / member
                if source.is_dir():
                    shutil.copytree(source, destination / member)
            else:
                shutil.copyfile(self.root / guest.removeprefix("/tmp/") / member, destination / member)


class SequentialBlindSolve(unittest.TestCase):
    def package(self, root):
        package = root / "package"
        for step in ("01-fix", "02-revise"):
            path = package / "steps" / step
            path.mkdir(parents=True)
            (path / "instruction.md").write_text(step)
        (package / "task.toml").write_text("multi_step_reward_strategy='min'\n[[steps]]\nname='01-fix'\n[[steps]]\nname='02-revise'\n")
        return package

    def test_failed_initial_snapshot_never_reaches_the_solver_or_grader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = Mock()
            container.__enter__ = Mock(return_value=container)
            container.__exit__ = Mock(return_value=False)
            container.exec.return_value = {"exit_code": 1, "stderr": "snapshot copy failed"}
            call = Mock()
            with patch.object(blind_solve.sandbox, "verify") as verify:
                with self.assertRaises(blind_solve.sandbox.SandboxError):
                    blind_solve.solve(self.package(root), "gpt-6-astra", root / "work",
                                      call=call, container_factory=lambda: container)
            call.assert_not_called()
            verify.assert_not_called()

    def test_missing_incremental_baseline_is_not_scored_as_an_unchanged_solution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = LocalContainer(root)
            def respond(body):
                shutil.rmtree(root / "pristine")
                return {"status": "completed", "output": []}
            with patch.object(blind_solve.sandbox, "verify") as verify:
                with self.assertRaises(blind_solve.sandbox.SandboxError):
                    blind_solve.solve(self.package(root), "gpt-6-astra", root / "work",
                                      call=respond, container_factory=lambda: container)
            verify.assert_not_called()

    def test_diff_error_is_distinct_from_diff_status_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = LocalContainer(Path(tmp))
            tools = blind_solve.SandboxTools(container)
            execute = container.exec
            def unavailable_diff(command, **kwargs):
                return execute(command.replace("diff -ruN", "missing_capture_diff -ruN"), **kwargs)
            with patch.object(container, "exec", side_effect=unavailable_diff):
                with self.assertRaises(blind_solve.sandbox.SandboxError):
                    tools.diff(Path(tmp) / "capture")
            self.assertFalse((Path(tmp) / "capture/blind.patch").exists())

    def test_persistent_workspace_incremental_patches_and_all_step_grades(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = LocalContainer(root)
            turns = iter([
                {"status": "completed", "output": [{"type": "function_call", "call_id": "1", "name": "write_file",
                 "arguments": json.dumps({"path": "core.py", "content": "first\n"})}]},
                {"status": "completed", "output": []},
                {"status": "completed", "output": [{"type": "function_call", "call_id": "2", "name": "read_file",
                 "arguments": json.dumps({"path": "core.py"})}]},
                {"status": "completed", "output": [{"type": "function_call", "call_id": "3", "name": "write_file",
                 "arguments": json.dumps({"path": "core.py", "content": "second\n"})}]},
                {"status": "completed", "output": []}])
            def verify(package, variants, **kwargs):
                # Replay deltas exactly as the shipped sandbox does. A
                # cumulative second patch would fail to apply to 'first'.
                replay = root / "replay"
                replay.mkdir()
                (replay / "core.py").write_text("old\n")
                snapshots = []
                for variant in variants.values():
                    proc = subprocess.run(["patch", "-p1"], cwd=replay, input=variant.patch, capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    snapshots.append((replay / "core.py").read_text())
                self.assertEqual(snapshots, ["first\n", "second\n"])
                return {"valid": True, "steps": {
                    "01-fix": {"reward": 1., "grade": {"checks": {"o_same": {"passed": True}}}},
                    "02-revise": {"reward": .5, "grade": {"checks": {"o_same": {"passed": False}}}}}}
            with patch.object(blind_solve.sandbox, "verify", side_effect=verify):
                record = blind_solve.solve(self.package(root), "gpt-6-astra", root / "work", max_steps=5,
                                           call=lambda body: next(turns), container_factory=lambda: container)
            self.assertEqual(record["reward"], .5, "use the published min strategy over all steps")
            self.assertTrue(record["complete"])
            self.assertEqual(record["checks"], {"01-fix/o_same": True, "02-revise/o_same": False})
            history = json.loads((root / "work/steps/02-revise/conversation.json").read_text())
            self.assertIn("first", next(r["output"] for r in history if r.get("call_id") == "2" and r.get("type") == "function_call_output"))
            self.assertIn("-old\n+second", (root / "work/blind.patch").read_text())

    def test_bounded_cutoff_is_diagnostic_and_unchanged_steps_are_still_graded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = LocalContainer(root)
            response = {"status": "completed", "output": [{"type": "function_call", "call_id": "r", "name": "read_file",
                        "arguments": json.dumps({"path": "core.py"})}]}
            graded = {"valid": True, "steps": {s: {"reward": 0., "grade": {"checks": {}}}
                       for s in ("01-fix", "02-revise")}}
            with patch.object(blind_solve.sandbox, "verify", return_value=graded) as verify:
                record = blind_solve.solve(self.package(root), "gpt-6-astra", root / "work", max_steps=1,
                                           call=lambda body: response, container_factory=lambda: container)
            self.assertFalse(record["complete"])
            self.assertIsNone(record["reward"])
            self.assertEqual(record["diagnostic_reward"], 0.)
            self.assertTrue(all(v.kind == "nop" for v in verify.call_args.args[1].values()))
            self.assertTrue(all(r["stop_reason"] == "max_steps" for r in record["by_step"].values()))

    def test_provider_cutoff_cannot_look_like_completion(self):
        tools = Mock()
        session = blind_solve._anthropic_loop("fix", "claude-fable-5-1", tools, 3, "high",
                                             lambda body: {"content": [], "stop_reason": "max_tokens"})
        self.assertFalse(session["complete"])
        tools.call.assert_not_called()

    def test_deliverable_snapshots_are_kept_for_their_own_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = LocalContainer(root)
            (root / "deliverables").mkdir()
            def respond(body):
                instruction = body["input"][1]["content"]
                (root / "deliverables/result.txt").write_text(instruction)
                return {"status": "completed", "output": []}
            graded = {"valid": True, "steps": {s: {"reward": 1., "grade": {"checks": {}}}
                       for s in ("01-fix", "02-revise")}}
            with patch.object(blind_solve.sandbox, "verify", return_value=graded) as verify:
                record = blind_solve.solve(self.package(root), "gpt-6-astra", root / "work",
                                           call=respond, container_factory=lambda: container)
            variants = verify.call_args.args[1]
            self.assertEqual(variants["01-fix"].deliverables, {"result.txt": b"01-fix"})
            self.assertEqual(variants["02-revise"].deliverables, {"result.txt": b"02-revise"})
            variants["01-fix"].apply(container, "01-fix")
            self.assertEqual((root / "deliverables/result.txt").read_text(), "01-fix")
            (root / "deliverables/stale.txt").write_text("remove at next checkpoint")
            variants["02-revise"].apply(container, "02-revise")
            self.assertEqual((root / "deliverables/result.txt").read_text(), "02-revise")
            self.assertFalse((root / "deliverables/stale.txt").exists())
            self.assertEqual(record["reward"], 1.)


if __name__ == "__main__":
    unittest.main()
