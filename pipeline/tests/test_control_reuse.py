"""Reuse genuine complete trials, with strict inputs and honest export lineage."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline.artifacts import atomic_json, identity
from pipeline.author import agent_designer, control_cache, controls, probes, sandbox
from pipeline.naming import rename_text
from pipeline.tests import test_parallel_controls
from pipeline.tests.synthetic_code import write_bundle


class ControlReuse(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = write_bundle(self.root / "bundle", two_steps=True)
        self.spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        self.steps = [s["name"] for s in self.spec["steps"]]
        self.mutants = {m["id"]: m for m in self.spec["mutants"]}

    def record(self, bundle, variants, *, work, **kwargs):
        record = test_parallel_controls.ParallelControls.record(self, bundle, variants, work=work, **kwargs)
        record["schema"] = "arena-coding/sandbox-trial/1"
        record.update(agent_image=kwargs.get("agent_tag"), verifier_image=kwargs.get("verifier_tag"))
        for entry in record["steps"].values():
            entry.update(valid=True, timed_out=False)
        return record

    def execute(self, **kwargs):
        with patch.object(sandbox, "build_image", side_effect=lambda b, role, **kw: role), \
                patch.object(sandbox, "verify", side_effect=self.record) as verify:
            receipt = controls.run(self.bundle, **kwargs)
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertGreater(verify.call_count, 0)
        return receipt

    def rename(self):
        path = self.bundle / "task.toml"
        path.write_text(rename_text(path.read_text(), "arena/a_f2_published"))
        atomic_json(self.bundle / "provenance/ledger.json", {"faithfulness_level": "F2"})
        atomic_json(self.bundle / "authoring/extraction/fixture-authored.json", {"files": {}})

    def no_execution(self):
        with patch.object(sandbox, "build_image", side_effect=AssertionError("unnecessary image build")), \
                patch.object(sandbox, "verify", side_effect=AssertionError("duplicate execution")):
            return controls.run(self.bundle)

    def evidence(self, receipt):
        path = self.bundle / receipt["execution"]["evidence_path"]
        return path, json.loads(path.read_text())

    def replace_evidence(self, path, evidence):
        inputs = evidence["inputs"]
        path.unlink()
        return control_cache.retain(self.bundle / control_cache.ROOT, inputs,
                                    evidence["receipt"], evidence["records"])[0]

    def test_export_metadata_reuses_full_trials_and_preserves_executed_hash(self):
        old = self.execute()
        source_hash = identity(self.bundle)
        path, evidence = self.evidence(old)
        self.rename()
        # Export also records author-session status, which no control consumes.
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec["_agent"] = {"steps": 42, "self_checked": True, "creation_status": "complete",
                          "validation_status": "deferred", "controls_advisories": []}
        atomic_json(self.bundle / "authoring/spec.json", spec)
        receipt = self.no_execution()
        self.assertNotEqual(receipt["artifact_sha256"], source_hash)
        self.assertEqual(receipt["artifact_sha256"], identity(self.bundle))
        self.assertTrue(receipt["execution"]["reused"])
        self.assertEqual(receipt["execution"]["source_artifact_sha256"], source_hash)
        self.assertEqual(receipt["execution"]["executed_new_trials"], 0)
        self.assertEqual(receipt["results"], old["results"])
        self.assertEqual(json.loads(path.read_text()), evidence)
        self.assertEqual(controls.problems(self.bundle), [])
        self.assertIn("provenance/ledger.json", receipt["execution"]["metadata_changes"])
        self.assertIn("flags", receipt["results"]["golden"])

    def test_fixture_instructions_tests_patches_traps_and_runtime_changes_force_execution(self):
        self.execute()
        baseline = self.root / "baseline"
        shutil.copytree(self.bundle, baseline)
        paths = ["environment/repo/app.py", "steps/01-fix/instruction.md", "tests/grade.py",
                 "tests/checks.json", "tests/rubric.json", "environment/toolchain.json",
                 "steps/01-fix/solution/fix.patch", "authoring/alt/01-fix.patch",
                 "authoring/mutants/" + next(iter(self.mutants)) + ".patch", "authoring/traps.json",
                 "authoring/spec.json", "task.toml"]
        # Discover concrete step names and fixture paths; mutate one surface
        # at a time while retaining the same previous complete evidence.
        paths = [p.replace("01-fix", self.steps[0]) for p in paths]
        paths[0] = next(p.relative_to(self.bundle).as_posix() for p in (self.bundle / "environment/repo").rglob("*.py"))
        for rel in paths:
            with self.subTest(surface=rel):
                shutil.rmtree(self.bundle)
                shutil.copytree(baseline, self.bundle)
                path = self.bundle / rel
                if path.suffix == ".json":
                    doc = json.loads(path.read_text())
                    if rel == "tests/rubric.json":
                        doc = dict(reversed(list(doc.items())))
                    elif isinstance(doc, dict):
                        doc["changed_control_input"] = True
                    atomic_json(path, doc)
                else:
                    path.write_text(path.read_text() + "\n# changed control input\n")
                # task.toml comments are conservatively bound too.
                with patch.object(sandbox, "build_image", side_effect=RuntimeError("fresh execution required")) as build:
                    with self.assertRaisesRegex(RuntimeError, "fresh execution required"):
                        controls.run(self.bundle)
                    build.assert_called_once()

    def test_policy_and_determinism_changes_force_fresh_execution(self):
        self.execute()
        for kwargs in ({"determinism_runs": 4}, {}):
            with self.subTest(kwargs=kwargs), \
                    patch.object(control_cache, "policy_identity", return_value="changed-policy"), \
                    patch.object(sandbox, "build_image", side_effect=RuntimeError("fresh execution required")):
                with self.assertRaisesRegex(RuntimeError, "fresh execution required"):
                    controls.run(self.bundle, **kwargs)

    def test_a_rewrite_during_reuse_cannot_receive_a_rebound_receipt(self):
        self.execute()
        old = (self.bundle / controls.RECEIPT).read_bytes()
        retain = control_cache.retain
        def mutate(*args, **kwargs):
            result = retain(*args, **kwargs)
            path = self.bundle / "steps" / self.steps[0] / "instruction.md"
            path.write_text(path.read_text() + "\nchanged during cache binding\n")
            return result
        with patch.object(control_cache, "retain", side_effect=mutate), \
                patch.object(sandbox, "verify", side_effect=AssertionError("no grader calls")):
            with self.assertRaisesRegex(sandbox.SandboxError, "bundle changed"):
                controls.run(self.bundle)
        self.assertEqual((self.bundle / controls.RECEIPT).read_bytes(), old)

    def test_probes_reuse_after_export_and_stale_supplied_receipt_is_blocked(self):
        receipt = self.execute()
        self.rename()
        with patch.object(sandbox, "build_image", side_effect=AssertionError("duplicate image build")), \
                patch.object(sandbox, "verify", side_effect=AssertionError("duplicate grader run")):
            report = probes.run(self.bundle)
        self.assertEqual(report["binding"]["controls_sha256"], identity(self.bundle))
        stale = probes.run(self.bundle, controls_receipt=receipt)
        self.assertFalse(stale["ok"])
        self.assertIn("controls: receipt is for another version of this bundle", stale["problems"])

    def test_a_check_killed_at_its_limit_is_a_failure_not_missing_evidence(self):
        path, original = self.evidence(self.execute())
        step = self.steps[0]
        label = next(l for l in original["records"] if l == "nop" or l.startswith("mutant:"))
        shape = {s: set(original["records"]["golden-1"]["steps"][s]["grade"]["checks"]) for s in self.steps}
        for passed, accepted in ((False, True), (True, False)):
            with self.subTest(passed=passed):
                evidence = deepcopy(original)
                item = next(iter(evidence["records"][label]["steps"][step]["grade"]["checks"].values()))
                item.update(timed_out=True, passed=passed)
                path.unlink(missing_ok=True)
                path = control_cache.retain(self.bundle / control_cache.ROOT, evidence["inputs"],
                                            evidence["receipt"], evidence["records"])[0]
                read = lambda: control_cache.read(path, evidence["inputs"], list(evidence["records"]), self.steps, shape)
                if accepted:
                    self.assertEqual(read()["records"][label], evidence["records"][label])
                else:
                    with self.assertRaisesRegex(ValueError, "per-check control evidence"):
                        read()

    def test_partial_invalid_failed_or_summary_evidence_is_never_reused(self):
        source = self.execute()
        path, original = self.evidence(source)
        for mode in ("partial", "invalid", "failed", "summary", "flags", "timed_out", "verdict"):
            with self.subTest(mode=mode):
                evidence = deepcopy(original)
                if mode == "partial":
                    evidence["records"].pop("golden-3")
                elif mode == "invalid":
                    evidence["records"]["golden-3"]["valid"] = False
                elif mode == "failed":
                    evidence["receipt"].update(ok=False, errors=["golden failed"])
                elif mode == "summary":
                    evidence["records"] = evidence["receipt"]["results"]
                elif mode == "flags":
                    evidence["records"]["golden-1"]["steps"][self.steps[0]]["grade"]["checks"].popitem()
                elif mode == "timed_out":
                    evidence["records"]["golden-1"]["steps"][self.steps[0]]["timed_out"] = True
                else:
                    evidence["receipt"]["results"]["golden"]["reward"] = 0.0
                if path.exists():
                    path.unlink()
                path = control_cache.retain(self.bundle / control_cache.ROOT, evidence["inputs"],
                                             evidence["receipt"], evidence["records"])[0]
                with patch.object(sandbox, "build_image", side_effect=RuntimeError("fresh execution required")):
                    with self.assertRaisesRegex(RuntimeError, "fresh execution required"):
                        controls.run(self.bundle)

    def test_tampered_cache_and_tampered_bound_receipt_are_rejected(self):
        receipt = self.execute()
        path, evidence = self.evidence(receipt)
        evidence["records"]["golden-1"]["reward"] = 0.0
        atomic_json(path, evidence)
        self.assertTrue(controls.problems(self.bundle))
        with patch.object(sandbox, "build_image", side_effect=RuntimeError("fresh execution required")):
            with self.assertRaisesRegex(RuntimeError, "fresh execution required"):
                controls.run(self.bundle)
        self.execute(reuse=False)
        receipt = self.no_execution()
        receipt["results"]["golden"]["reward"] = 0.0
        atomic_json(self.bundle / controls.RECEIPT, receipt)
        self.assertTrue(controls.problems(self.bundle))

    def test_durable_author_controls_survive_scratch_pruning_and_export(self):
        draft = self.root / "draft.json"
        atomic_json(draft, self.spec)
        extraction = SimpleNamespace(session_id="synthetic", seed_index=0, source_root=self.root, moments=[])
        log = self.bundle / "verification/design-sessions/test-session"
        handlers = agent_designer._handlers(self.bundle, extraction, {}, draft, log=log)
        with patch.object(sandbox, "available", return_value=(True, "")), \
                patch.object(agent_designer.extract_mod, "usable_correction_count", return_value=0), \
                patch.object(sandbox, "build_image", side_effect=lambda b, role, **kw: role), \
                patch.object(sandbox, "verify", side_effect=self.record), \
                patch.object(sandbox, "shell", return_value={"exit_code": 0, "timed_out": False,
                                                           "seconds": 0, "stdout": "", "stderr": ""}):
            response = handlers["run_controls"]()
            self.assertTrue(response["ok"], response)
            source = log / "scratch/001"
            source_receipt = json.loads((source / controls.RECEIPT).read_text())
            exported = self.root / "exported"
            shutil.copytree(source, exported)
            for _ in range(3):
                handlers["sandbox_run"](command="true")
            self.assertFalse(source.exists())
        self.bundle = exported
        # Mimic canonical export: generated surfaces retained, verification
        # remains on the owning bundle, never copied from the scratch export.
        shutil.rmtree(exported / "verification")
        shutil.copytree(self.root / "bundle" / control_cache.ROOT, exported / control_cache.ROOT)
        self.rename()
        receipt = self.no_execution()
        self.assertEqual(receipt["execution"]["source_artifact_sha256"], source_receipt["artifact_sha256"])
        self.assertEqual(controls.problems(exported), [])

    def test_legacy_summary_cannot_be_rebound_and_failed_final_provenance_still_blocks(self):
        self.execute()
        shutil.rmtree(self.bundle / control_cache.ROOT)
        receipt = json.loads((self.bundle / controls.RECEIPT).read_text())
        receipt.pop("input_binding")
        atomic_json(self.bundle / controls.RECEIPT, receipt)
        self.rename()
        with patch.object(sandbox, "build_image", side_effect=RuntimeError("fresh execution required")):
            with self.assertRaisesRegex(RuntimeError, "fresh execution required"):
                controls.run(self.bundle)
        # Reuse is deliberately not final acceptance. Ledger validation remains
        # an independent gate over the current, metadata-modified artifact.
        from pipeline.ledger import faithfulness
        self.assertTrue(faithfulness.problems(self.bundle))


if __name__ == "__main__":
    unittest.main()
