"""Research orchestration must never manufacture difficulty evidence."""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pipeline import artifacts, harden, policy, research
from pipeline.author import agent_designer, cases, oracle
from pipeline.measurement import compare_success, exact_interval
from pipeline.select import mechanisms, shortlist

MODELS = [policy.SPEC_PRIMARY_MODEL, policy.SPEC_SECONDARY_MODEL]
RESOLVED = ["anthropic/" + MODELS[0], "openai/" + MODELS[1]]


def package(root, cell="00"):
    root.mkdir(parents=True)
    files = {"task.toml": "[[steps]]\nname='initial'\n", "tests/checks.json": '{"checks":[]}',
             "steps/initial/instruction.md": "Preserve the published behavior.\n",
             "environment/repo/core.py": "old\n", "environment/Dockerfile": "FROM fixture\n",
             "tests/suites/test_cases.py": "base\n", "authoring/spec.json": json.dumps({"steps": [
                 {"name": "initial", "instruction": "Preserve the published behavior."}],
                 "quality": {"difficulty_case": {"hypotheses": [{"id": "h_identity"}]}}})}
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if cell[0] == "1":
        (root / "environment/repo/adjacent.py").write_text("adjacent contract\n")
    if cell[1] == "1":
        (root / "tests/suites/test_cases.py").write_text("interaction\n")
    return root


def measured(n=72, passes=0):
    return {"measurement_complete": True, "shippable_problems": [], "per_model": {
        m: {"passes": passes, "episodes_valid": n, "pass_at_1": passes / n} for m in RESOLVED}}


class FrozenResearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = patch("pipeline.config.ARTIFACTS", self.root / "artifacts")
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def plan(self, purpose="confirmation", n=72):
        cells = research.CELLS if purpose == "ablation" else ("final",)
        sources = {c: package(self.root / ("source-" + c), c if c != "final" else "00") for c in cells}
        root = self.root / "experiment"
        research.create_plan(root, sources, purpose=purpose, models=MODELS, episodes=n,
            treatment={"decoy": "adjacent serializer", "interaction": "nested exceptions", "public_disambiguation": "wire version"})
        with patch("pipeline.benchmark.preflight.validate_task"), patch("pipeline.run.run_bundle", return_value={"verdict": "green"}):
            manifest = research.validate_plan(root)
        return root, manifest

    def campaigns(self, root, manifest):
        state = {"jobs": {}}
        for job in manifest["jobs"]:
            path = root / "runs" / job["id"] / "calibration-fresh"
            artifacts.atomic_json(path / "report.json", {"started_at": manifest["frozen_at"] + .01,
                                                       "requested_model": job["model"]})
            state["jobs"][job["id"]] = {"parts": [str(path)], "complete": True}
        artifacts.atomic_json(root / "state.json", state)
        return state

    def test_plan_creation_retains_missing_and_invalid_candidates_without_validation(self):
        with patch("pipeline.run.run_bundle") as gates, patch("pipeline.artifacts.freeze") as freeze:
            plan = research.create_plan(self.root / "incomplete", {"draft": self.root / "missing"}, episodes=0)
        self.assertTrue(plan["validation_deferred"])
        self.assertTrue((self.root / "incomplete/plan.json").is_file())
        gates.assert_not_called()
        freeze.assert_not_called()
        with patch("pipeline.run.run_bundle") as gates:
            result = research.validate_plan(self.root / "incomplete")
        self.assertEqual(result["phase"], "validation")
        self.assertEqual(result["verdict"], "blocked")
        self.assertFalse((self.root / "incomplete/manifest.json").exists())

    def test_confirmation_needs_fresh_manifest_fixed_horizon_and_verified_measurements(self):
        root, manifest = self.plan()
        self.campaigns(root, manifest)
        with patch("pipeline.screen.summarize", return_value=measured()), patch.object(research, "failure_patterns", return_value={}):
            result = research.report(root)
        self.assertTrue(result["confirmed_extreme"])
        with patch("pipeline.screen.summarize", return_value=measured(n=71)), patch.object(research, "failure_patterns", return_value={}):
            result = research.report(root)
        self.assertFalse(result["confirmed_extreme"])
        self.assertIn("fixed horizon", str(result["problems"]))
        with patch("pipeline.screen.summarize", return_value={**measured(), "measurement_complete": False,
                  "shippable_problems": ["invalid grader evidence"]}), patch.object(research, "failure_patterns", return_value={}):
            self.assertFalse(research.report(root)["confirmed_extreme"])

    def test_screen_sized_confirmation_remains_unconfirmed(self):
        root, manifest = self.plan(n=5)
        self.campaigns(root, manifest)
        with patch("pipeline.screen.summarize", return_value=measured(n=5)), patch.object(research, "failure_patterns", return_value={}):
            result = research.report(root)
        self.assertTrue(result["measurement_complete"])
        self.assertFalse(result["confirmed_extreme"])

    def test_foreign_screen_campaign_cannot_be_imported_as_confirmation(self):
        root, manifest = self.plan()
        state = self.campaigns(root, manifest)
        job = manifest["jobs"][0]
        foreign = self.root / "old-screen"
        artifacts.atomic_json(foreign / "report.json", {"started_at": manifest["frozen_at"] + 1, "requested_model": job["model"]})
        state["jobs"][job["id"]]["parts"] = [str(foreign)]
        artifacts.atomic_json(root / "state.json", state)
        with patch("pipeline.screen.summarize", return_value=measured()), patch.object(research, "failure_patterns", return_value={}):
            result = research.report(root)
        self.assertFalse(result["confirmed_extreme"])
        self.assertIn("reused campaign", str(result["problems"]))

    def test_old_or_wrong_model_campaigns_are_refused_even_inside_reserved_directory(self):
        root, manifest = self.plan()
        state = self.campaigns(root, manifest)
        job = manifest["jobs"][0]
        path = Path(state["jobs"][job["id"]]["parts"][0])
        for data, expected in (({"started_at": 0, "requested_model": job["model"]}, "predates"),
                               ({"started_at": manifest["frozen_at"] + 1, "requested_model": MODELS[1] if job["model"] == MODELS[0] else MODELS[0]}, "different")):
            artifacts.atomic_json(path / "report.json", data)
            with self.assertRaisesRegex(ValueError, expected):
                research._fresh_parts(root, manifest, job, [str(path)])

    def test_frozen_artifact_and_protocol_cannot_drift(self):
        root, manifest = self.plan()
        research.sealed(root)
        variant = Path(manifest["variants"]["final"]["bundle"])
        (variant / "environment/repo/core.py").write_text("rewritten\n")
        with self.assertRaisesRegex(ValueError, "variant changed"):
            research.sealed(root)

    def test_manifest_edits_are_rejected(self):
        root, manifest = self.plan()
        manifest["episodes"] = 1
        artifacts.atomic_json(root / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            research.sealed(root)

    def test_unrun_plan_has_no_claim_of_confirmation(self):
        root, manifest = self.plan()
        missing = {"measurement_complete": False, "shippable_problems": ["not screened"], "per_model": {}}
        with patch("pipeline.screen.summarize", return_value=missing), patch.object(research, "failure_patterns", return_value={}):
            result = research.report(root)
        self.assertFalse(result["confirmed_extreme"])
        self.assertFalse(result["measurement_complete"])

    def test_resume_reuses_scored_campaign_and_passes_it_to_void_only_topup(self):
        root, manifest = self.plan()
        state = self.campaigns(root, manifest)
        first = manifest["jobs"][0]
        state["jobs"][first["id"]]["complete"] = False
        artifacts.atomic_json(root / "state.json", state)
        existing = state["jobs"][first["id"]]["parts"]
        with patch("pipeline.release._calibrate_model", return_value=(existing[0], [], True)) as run, \
             patch.object(research, "report", return_value={}):
            research.run(root)
        self.assertEqual(run.call_count, 1)
        self.assertEqual([Path(p).resolve() for p in run.call_args.kwargs["existing"]], [Path(p).resolve() for p in existing])

    def test_four_cells_validate_and_mismatched_contract_is_rejected(self):
        root, manifest = self.plan("ablation")
        self.assertEqual(set(manifest["variants"]), set(research.CELLS))
        paths = {c: Path(v["bundle"]) for c, v in manifest["variants"].items()}
        self.assertEqual(research.contrast_problems(paths), [])
        (paths["10"] / "steps/initial/instruction.md").write_text("Different required behavior.\n")
        self.assertIn("identical instructions", str(research.contrast_problems(paths)))

    def test_ablation_does_not_claim_extreme_confirmation(self):
        root, manifest = self.plan("ablation")
        self.campaigns(root, manifest)
        with patch("pipeline.screen.summarize", return_value=measured()), patch.object(research, "failure_patterns", return_value={}):
            result = research.report(root)
        self.assertFalse(result["confirmed_extreme"])
        self.assertEqual(result["factorial"]["by_model"][RESOLVED[0]]["decoy"]["status"], "inconclusive")

    def test_variant_factory_retains_incomplete_drafts_without_building_or_running_controls(self):
        source = package(self.root / "source")
        with patch("pipeline.author.build.build") as build, patch("pipeline.author.controls.run") as controls:
            drafts = research.create_variants(source, self.root / "drafts", {
                "decoy_files": {"adjacent.py": "adjacent semantics\n"}, "interaction_suites": {"data/cases.json": "[]"}})
        self.assertTrue(drafts["validation_deferred"])
        build.assert_not_called()
        controls.assert_not_called()
        for cell, path in drafts["variants"].items():
            spec = json.loads((Path(path) / "authoring/spec.json").read_text())
            self.assertEqual((Path(path) / "environment/repo/adjacent.py").exists(), cell[0] == "1")
            self.assertEqual("data/cases.json" in spec.get("suites", {}), cell[1] == "1")

    def test_second_confirmation_for_same_candidate_is_blocked_only_during_validation(self):
        root, manifest = self.plan()
        second = self.root / "second-confirmation"
        source = self.root / "source-final"
        research.create_plan(second, {"final": source}, models=MODELS, episodes=72)
        self.assertTrue((second / "plan.json").exists())
        with patch("pipeline.benchmark.preflight.validate_task"), patch("pipeline.run.run_bundle", return_value={"verdict": "green"}):
            receipt = research.validate_plan(second)
        self.assertEqual(receipt["phase"], "validation")
        self.assertIn("already has a confirmation plan", str(receipt["problems"]))

    def test_scored_reserved_campaign_cannot_be_omitted_from_report(self):
        root, manifest = self.plan()
        state = self.campaigns(root, manifest)
        job = manifest["jobs"][0]
        extra = root / "runs" / job["id"] / "calibration-unfavorable"
        artifacts.atomic_json(extra / "report.json", {"started_at": manifest["frozen_at"] + 1,
                                                     "requested_model": job["model"]})
        with self.assertRaisesRegex(ValueError, "cannot be discarded"):
            research._fresh_parts(root, manifest, job, state["jobs"][job["id"]]["parts"])

    def test_target_cannot_be_relaxed_to_get_a_confirmation_label(self):
        source = package(self.root / "source")
        root = self.root / "experiment"
        plan = research.create_plan(root, {"final": source}, models=MODELS)
        plan["target"] = .9
        artifacts.atomic_json(root / "plan.json", plan)
        with patch("pipeline.benchmark.preflight.validate_task"), patch("pipeline.run.run_bundle", return_value={"verdict": "green"}):
            receipt = research.validate_plan(root)
        self.assertEqual(receipt["verdict"], "blocked")
        self.assertIn("strict", str(receipt["problems"]))

    def test_joint_patterns_use_actual_attribution_and_exclude_voids(self):
        root, manifest = self.plan()
        bundle = Path(manifest["variants"]["final"]["bundle"])
        report = {"model": RESOLVED[0], "step_names": ["initial"], "rollouts": [
            {"episode": 1, "step": "initial", "id": "trial/initial", "reward": 0., "tool_calls": 2,
             "stop_reason": "completed", "checks": {"o_identity": "failed", "o_state": "failed"}},
            {"episode": 2, "step": "initial", "id": "void/initial", "reward": 0., "tool_calls": 2,
             "zero_class": "void", "stop_reason": "completed", "checks": {"o_identity": "failed"}}]}
        with patch("pipeline.release.calibration_measurement", return_value=([], report)):
            patterns = research.failure_patterns(bundle, {"campaigns": ["campaign"]})
        self.assertEqual(patterns["patterns"], [{"model": RESOLVED[0], "failed": ["initial/o_identity", "initial/o_state"], "episodes": 1}])

    def test_adoption_needs_this_trials_solver_evidence_and_rejects_forged_quotes(self):
        root, manifest = self.plan()
        state = self.campaigns(root, manifest)
        job = manifest["jobs"][0]
        campaign = state["jobs"][job["id"]]["parts"][0]
        rel = "jobs/run/trial-1/agent/trajectory.json"
        text = "I will normalize every lookalike into the same identity."
        file = Path(campaign) / rel
        file.parent.mkdir(parents=True)
        file.write_text(text)
        native = json.loads((Path(campaign) / "report.json").read_text())
        native["evidence_files"] = {rel: hashlib.sha256(file.read_bytes()).hexdigest()}
        artifacts.atomic_json(Path(campaign) / "report.json", native)
        claim = {"variant": "final", "campaign": campaign, "trial": "trial-1", "hypothesis": "h_identity", "verdict": "adopted",
                 "reason": "The cited algorithm conflates the published distinct identifiers.", "citations": [{"file": rel, "quote": text}]}
        artifacts.atomic_json(root / "adoption.json", [claim])
        measured = {"model": RESOLVED[0], "step_names": ["initial"], "trials": [{"id": "trial-1", "valid": True}],
            "rollouts": [{"episode": 1, "step": "initial", "id": "trial-1/initial", "reward": 0., "tool_calls": 2,
                          "stop_reason": "completed", "checks": {"o_identity": "failed"}}]}
        indices = {"final": {"campaigns": [campaign], "topups": {}}}
        with patch("pipeline.release.calibration_measurement", return_value=([], measured)):
            report = research.adoption_report(root, manifest, indices)
            self.assertEqual(report["counts"][0]["verdict"], "adopted")
            claim["citations"][0]["quote"] = "A quote the model never emitted."
            artifacts.atomic_json(root / "adoption.json", [claim])
            rejected = research.adoption_report(root, manifest, indices)
        self.assertEqual(rejected["counts"], [])
        self.assertIn("not verbatim", str(rejected["unresolved"]))


class InputsAndSelection(unittest.TestCase):
    def test_sequences_retain_retry_order_and_critical_histories_deterministically(self):
        actions = [{"name": "write"}, {"name": "restart"}, {"name": "read"}]
        prefix = [[{"name": "write"}, {"name": "failure"}, {"name": "retry"}]]
        one = cases.sequences(actions, prefixes=prefix, count=2, seed=7)
        self.assertEqual(one, cases.sequences(actions, prefixes=prefix, count=2, seed=7))
        self.assertIn(prefix[0], one["cases"])
        self.assertIn([actions[0], actions[1]], one["cases"])
        self.assertIn([actions[1], actions[0]], one["cases"])
        self.assertGreater(len(one["cases"]), 2, "coverage must not silently drop critical cases")

    def test_intersections_cover_pairs_and_report_sample_gaps(self):
        dimensions = {"identity": ["alias", "lookalike"], "exception": [False, True], "retry": [False, True]}
        self.assertEqual(cases.intersections(dimensions)["uncovered_pairs"], [])
        limited = cases.intersections(dimensions, limit=1)
        self.assertTrue(limited["uncovered_pairs"])
        self.assertEqual(len(limited["cases"]), 1)

    def test_ungrounded_mechanism_labels_do_not_improve_ranking(self):
        row = {"classification": {"truth_source": "executable_tests", "usable_ratio": 1., "evidence_inspected": ["file1"]},
               "replay_readiness": .8, "dispute": .8}
        base = shortlist.rank(row)
        row["classification"]["mechanism_opportunities"] = [{"mechanism": "temporal_state", "tempting_rule": "cached authorization",
            "divergence_case": "revoke and read", "evidence": ["unread-file"]}]
        self.assertEqual(shortlist.rank(row), base)
        row["classification"]["mechanism_opportunities"][0]["evidence"] = ["file1"]
        self.assertGreater(shortlist.rank(row), base)

    def test_case_tool_runs_on_an_incomplete_draft_without_docker(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment/repo").mkdir(parents=True)
            handlers = agent_designer._handlers(root, SimpleNamespace(session_id="s", seed_index=0), {}, root / "draft.json")
            with patch("pipeline.author.sandbox.available", side_effect=AssertionError("case creation must not need Docker")):
                record = handlers["generate_cases"]("intersections", dimensions={"scope": ["exact", "wider"], "empty": [False, True]})
            self.assertEqual(len(record["cases"]), 4)
            self.assertTrue(Path(record["private_file"]).is_file())
            self.assertTrue(handlers["submit_check"]({})["validation_deferred"])

    def test_case_paging_and_experiment_drafts_do_not_require_a_complete_task(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "task"
            (root / "environment/repo").mkdir(parents=True)
            (root / "environment/repo/core.py").write_text("source")
            handlers = agent_designer._handlers(root, SimpleNamespace(session_id="s", seed_index=0), {}, root / "draft.json")
            generated = handlers["generate_cases"]("sequences", actions=[{"name": "read"}], count=70)
            self.assertEqual(len(generated["cases"]), 32)
            page = handlers["read_generated_cases"](generated["file"], offset=32)
            self.assertEqual(page["total_cases"], 70)
            self.assertEqual(page["next_offset"], 64)
            with patch("pipeline.config.ARTIFACTS", Path(tmp) / "artifacts"), \
                 patch("pipeline.author.build.build", side_effect=AssertionError("creation must not export")), \
                 patch("pipeline.run.run_bundle", side_effect=AssertionError("creation must not validate")):
                staged = handlers["stage_research_variants"]({"decoy_files": {"adjacent.py": "adjacent rule"}})
            self.assertTrue(staged["validation_deferred"])
            self.assertTrue((Path(staged["plan"]) / "plan.json").exists())
            self.assertTrue(handlers["submit_check"]({})["validation_deferred"])

    def test_hypothesis_public_citations_are_checked_only_by_validation(self):
        from pipeline.author.difficulty import hypothesis_evidence_problems
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment/repo").mkdir(parents=True)
            (root / "environment/repo/schema.md").write_text("Wire version 2 owns this route.")
            spec = {"steps": [{"name": "initial", "instruction": "Only declared aliases merge."}], "quality": {"difficulty_case": {
                "hypotheses": [{"id": "identity", "supporting_evidence": [{"file": "schema.md", "quote": "Wire version 2"}],
                                "disambiguating_evidence": [{"file": "step:initial", "quote": "Only declared aliases merge."}]}]}}}
            self.assertEqual(hypothesis_evidence_problems(spec, root), [])
            spec["quality"]["difficulty_case"]["hypotheses"][0]["supporting_evidence"][0]["quote"] = "Invented authority"
            self.assertIn("absent", str(hypothesis_evidence_problems(spec, root)))


class IndependentOracle(unittest.TestCase):
    def test_derivation_never_grades_or_exposes_reference_and_receipt_detects_stale_alternative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from pipeline.tests.synthetic_code import write_bundle
            source = write_bundle(root / "package")
            step = "01-fix"
            work = source / "authoring/oracles/run"
            patch_text = "independently derived patch\n"
            def solve(package, model, destination, **kw):
                self.assertIs(kw["grade"], False)
                artifacts.atomic_json(destination / "conversation.json", {"public": "contract only"})
                path = destination / "steps" / step / "blind.patch"
                path.parent.mkdir(parents=True)
                path.write_text(patch_text)
                return {"step_names": [step], "complete": True, "limits": {"max_turns": 60}}
            with patch("pipeline.author.blind_solve.solve", side_effect=solve):
                result = oracle.derive(source, MODELS[1], MODELS[0], work)
            self.assertFalse(result["receipt"]["reference_exposed"])
            self.assertFalse(result["receipt"]["hidden_tests_exposed"])
            spec = json.loads((source / "authoring/spec.json").read_text())
            spec["quality"]["independent_oracle"] = {"path": "authoring/oracles/run"}
            spec["alternative"] = {step: {"patch": patch_text}}
            self.assertEqual(oracle.problems(spec, source), [])
            spec["alternative"][step]["patch"] += "edited"
            self.assertIn("alternative differs", str(oracle.problems(spec, source)))
            spec["alternative"][step]["patch"] = patch_text
            (source / "environment/repo/core.py").write_text("changed contract")
            self.assertIn("different public contract", str(oracle.problems(spec, source)))

    def test_same_vendor_is_rejected_only_by_optional_derivation_not_submission(self):
        with self.assertRaisesRegex(ValueError, "different vendor"):
            oracle.derive(Path("unused"), MODELS[0], MODELS[0], Path("unused"))
        self.assertEqual(oracle.problems({}, Path("unused")), [])


class UncertaintyAndLineage(unittest.TestCase):
    def test_equal_or_small_changed_screens_do_not_establish_futility(self):
        row = {"passes": 1, "episodes_valid": 5}
        self.assertEqual(compare_success(row, row)["status"], "inconclusive")
        self.assertIsNone(harden.measured_progress({m: row for m in MODELS}, {m: row for m in MODELS}))
        self.assertEqual(compare_success(row, {"passes": 0, "episodes_valid": 5})["status"], "inconclusive")

    def test_large_fixed_cohorts_distinguish_improvement_and_regression(self):
        old, new = {"passes": 180, "episodes_valid": 200}, {"passes": 0, "episodes_valid": 200}
        self.assertEqual(compare_success(old, new)["status"], "improved")
        self.assertEqual(compare_success(new, old)["status"], "regressed")
        self.assertTrue(harden.measured_progress({m: old for m in MODELS}, {m: new for m in MODELS}))
        self.assertFalse(harden.measured_progress({m: new for m in MODELS}, {m: old for m in MODELS}))
        self.assertIsNone(exact_interval(0, 0))

    def test_new_checkpoint_lineage_can_append_but_cannot_weaken_old_work(self):
        before = {"steps": [{"name": "initial", "instruction": "Old contract."}],
                  "checks": [{"id": "old", "requires": []}], "suites": {"old.py": "old test"}}
        after = copy.deepcopy(before)
        after["steps"].append({"name": "restart", "instruction": "Preserve after restart."})
        after["checks"].append({"id": "new", "requires": ["old"]})
        self.assertEqual(harden.revision_problems(before, after, [], new_lineage=True), [])
        self.assertIn("step inventory", str(harden.revision_problems(before, after, [])))
        after["steps"][0]["instruction"] = "Weakened contract."
        self.assertIn("retain every prior instruction", str(harden.revision_problems(before, after, [], new_lineage=True)))

    def test_budget_increase_preserves_spent_attempts(self):
        ledger = {"max_attempts": 12, "attempts": [{"attempt": i} for i in range(1, 7)]}
        self.assertEqual(harden.attempts_left(ledger), 6)
        self.assertEqual(harden.attempts_left({"attempts": []}), policy.MAX_HARDENING_ATTEMPTS)

    def test_checkpoint_creation_retains_draft_before_any_validity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = package(root / "source")
            revised = {"name": "incomplete checkpoint candidate", "steps": []}
            with patch.object(harden, "propose", return_value=revised), \
                 patch("pipeline.author.validate.validate", side_effect=AssertionError("creation must defer validity")), \
                 patch("pipeline.author.controls.run", side_effect=AssertionError("creation must defer controls")):
                self.assertEqual(harden.extend_lineage(source, root / "extended", [], validate=False), 0)
            receipt = json.loads((root / "extended/verification/lineage-validation.json").read_text())
            self.assertTrue(receipt["validation_deferred"])
            self.assertEqual(json.loads((root / "extended/authoring/spec.json").read_text())["name"], revised["name"])
            self.assertEqual(artifacts.identity(source), receipt["origin"]["parent_artifact_sha256"])


if __name__ == "__main__":
    unittest.main()
