"""Incremental in-session controls (2026-10-26): only what an edit touches re-executes.

run_controls was 26% of a design session's wall clock (8.6 min a run, 3.4 runs
a session) while most runs re-executed trials whose inputs had not changed.
Every executed trial is now recorded under the digest of its inputs; an
incremental run serves unchanged trials from their records. The gate never
accepts such a receipt: validation's matrix is a full run.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.artifacts import policy_identity
from pipeline.author import control_cache, controls, sandbox
from pipeline.tests import test_parallel_controls
from pipeline.tests.synthetic_code import write_bundle


def content_tag(bundle, role, **_):
    """Content-addressed image tags without Docker: what the real builder names them."""
    context = Path(bundle) / ("environment" if role == "agent" else "tests")
    return sandbox.image_tag(context, role)


class IncrementalControls(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = write_bundle(self.root / "bundle", two_steps=True)
        self.spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        self.steps = [s["name"] for s in self.spec["steps"]]
        self.mutants = {m["id"]: m for m in self.spec["mutants"]}
        self.cache = self.root / "cache"

    def record(self, bundle, variants, *, work, **kwargs):
        record = test_parallel_controls.ParallelControls.record(self, bundle, variants, work=work, **kwargs)
        record["schema"] = "arena-coding/sandbox-trial/1"
        record.update(agent_image=kwargs.get("agent_tag"), verifier_image=kwargs.get("verifier_tag"))
        for entry in record["steps"].values():
            entry.update(valid=True, timed_out=False)
        return record

    def execute(self, *, incremental, work):
        with patch.object(sandbox, "build_image", side_effect=content_tag), \
                patch.object(sandbox, "verify", side_effect=self.record) as verify:
            receipt = controls.run(self.bundle, work=self.root / work, cache_root=self.cache, incremental=incremental)
        self.assertTrue(receipt["ok"], receipt["errors"])
        return receipt, sorted(c.kwargs["work"].name for c in verify.call_args_list)

    def test_a_first_run_executes_everything_and_records_every_trial(self):
        receipt, executed = self.execute(incremental=True, work="w1")
        self.assertEqual(receipt["execution"]["mode"], "full")
        # nop, golden x3, alt, empty, destructive, tamper, exit, benign, forge, plus this
        # branch's neutral_additions trials and every mutant: each executed exactly once.
        self.assertGreaterEqual(len(executed), len(receipt["results"]["mutants"]) + 11)
        self.assertEqual(len(executed), len(set(executed)))
        self.assertEqual(len(list((self.cache / controls.TRIALS).glob("*.json"))), len(executed))
        self.assertIn("evidence_path", receipt["execution"], "a full run is whole-bundle evidence")

    def test_editing_one_mutant_re_executes_only_that_mutant(self):
        _, first = self.execute(incremental=True, work="w1")
        mid = next(iter(self.mutants))
        path = self.bundle / f"authoring/mutants/{mid}.patch"
        path.write_text(path.read_text() + "\n")
        receipt, executed = self.execute(incremental=True, work="w2")
        self.assertEqual(executed, [f"mutant-{mid}"])
        self.assertEqual(receipt["execution"]["mode"], "incremental")
        self.assertEqual(receipt["execution"]["trials_executed"], 1)
        self.assertEqual(len(receipt["execution"]["trials_reused"]), len(first) - 1)
        self.assertNotIn("evidence_path", receipt["execution"], "never whole-bundle evidence")
        self.assertIn("incremental", controls.binding_problems(self.bundle, receipt)[0])

    def test_editing_a_hidden_suite_or_the_fixture_re_executes_everything(self):
        _, first = self.execute(incremental=True, work="w1")
        suite = next((self.bundle / "tests/suites").iterdir())
        suite.write_text(suite.read_text() + "\n# touched\n")
        receipt, executed = self.execute(incremental=True, work="w2")
        self.assertEqual(receipt["execution"]["mode"], "full")
        self.assertEqual(len(executed), len(first))
        fixture = next(p for p in (self.bundle / "environment/repo").rglob("*.py"))
        fixture.write_text(fixture.read_text() + "\n")
        receipt, executed = self.execute(incremental=True, work="w3")
        self.assertEqual(receipt["execution"]["mode"], "full")

    def test_editing_the_first_reference_re_executes_its_dependants_and_keeps_the_rest(self):
        self.execute(incremental=True, work="w1")
        first = self.bundle / "steps" / self.steps[0] / "solution/fix.patch"
        first.write_text(first.read_text().rstrip("\n") + "\n-- touched reference\n")
        receipt, executed = self.execute(incremental=True, work="w2")
        self.assertEqual(receipt["execution"]["mode"], "incremental")
        dependants = ["golden-1", "golden-2", "golden-3"]
        if (receipt["results"].get("benign_test") or {}).get("valid"):
            dependants.append("benign_test")          # the reference plus a regression test file
        for label in dependants:
            self.assertIn(label, executed, label)
        for label in ("nop", "empty", "destructive", "test_tamper", "early_exit", "report_forge", "alternative"):
            self.assertNotIn(label, executed, label)
        second_step_mutants = [f"mutant-{mid}" for mid, m in self.mutants.items()
                               if (m.get("step") or next(c["step"] for c in self.spec["checks"] if c["id"] in m["fails"]))
                               == self.steps[1]]
        for label in second_step_mutants:
            self.assertIn(label, executed, "a later-step mutant carries the earlier reference")

    def test_a_failed_or_partial_record_is_never_reused(self):
        self.execute(incremental=True, work="w1")
        store = self.cache / controls.TRIALS
        for path in store.glob("*.json"):
            evidence = json.loads(path.read_text())
            if evidence["label"] == "nop":
                evidence["record"]["valid"] = False
                path.write_text(json.dumps(evidence))
            elif evidence["label"] == "empty":
                evidence["record"]["steps"][self.steps[0]]["timed_out"] = True
                path.write_text(json.dumps(evidence))
        mid = next(iter(self.mutants))
        patch_path = self.bundle / f"authoring/mutants/{mid}.patch"
        patch_path.write_text(patch_path.read_text() + "\n")
        _, executed = self.execute(incremental=True, work="w2")
        self.assertEqual(executed, ["empty", f"mutant-{mid}", "nop"])

    def test_a_full_run_ignores_the_records_and_binds_the_artifact(self):
        _, first = self.execute(incremental=True, work="w1")
        mid = next(iter(self.mutants))
        patch_path = self.bundle / f"authoring/mutants/{mid}.patch"
        patch_path.write_text(patch_path.read_text() + "\n")
        self.execute(incremental=True, work="w2")
        receipt, executed = self.execute(incremental=False, work="w3")
        self.assertEqual(receipt["execution"]["mode"], "full")
        self.assertEqual(len(executed), len(first))
        self.assertEqual(controls.binding_problems(self.bundle, receipt), [])
        # And the whole-bundle cache now serves the gate without any execution.
        with patch.object(sandbox, "build_image", side_effect=AssertionError("unnecessary build")), \
                patch.object(sandbox, "verify", side_effect=AssertionError("duplicate execution")):
            again = controls.run(self.bundle, cache_root=self.cache)
        self.assertTrue(again["execution"]["reused"])

    def test_an_incremental_receipt_is_not_whole_bundle_evidence(self):
        receipt, _ = self.execute(incremental=True, work="w1")
        path = self.bundle / receipt["execution"]["evidence_path"]
        evidence = json.loads(path.read_text())
        evidence["receipt"]["execution"]["mode"] = "incremental"
        path.unlink()
        forged, _ = control_cache.retain(self.bundle / control_cache.ROOT, evidence["inputs"],
                                         evidence["receipt"], evidence["records"])
        with self.assertRaisesRegex(ValueError, "no complete successful executed controls receipt"):
            control_cache.read(forged, control_cache.snapshot(self.bundle, receipt["policy"]["determinism_runs"]),
                               list(evidence["records"]), self.steps,
                               controls._cache_shape(json.loads((self.bundle / "tests/checks.json").read_text()), self.steps))


class TrialKey(unittest.TestCase):
    def test_every_input_class_changes_the_key(self):
        base = dict(label="mutant:m", variants={"s1": sandbox.Variant("patch", patch="p"), "s2": sandbox.Variant("shell", shell="sh")},
                    agent_tag="a:1", verifier_tag="v:1", steps=["s1", "s2"], policy_sha256="pol")
        key = controls.trial_key(**base)
        self.assertEqual(key, controls.trial_key(**base))
        changes = [dict(label="golden-2"), dict(agent_tag="a:2"), dict(verifier_tag="v:2"), dict(steps=["s1"]),
                   dict(variants={"s1": sandbox.Variant("patch", patch="q"), "s2": sandbox.Variant("shell", shell="sh")}),
                   dict(variants={"s1": sandbox.Variant("patch", patch="p"), "s2": sandbox.Variant("shell", shell="sh2")}),
                   dict(variants={"s1": sandbox.Variant("files", files={"a": b"1"}), "s2": sandbox.Variant("shell", shell="sh")}),
                   dict(policy_sha256="pol2")]
        for change in changes:
            with self.subTest(change=list(change)):
                self.assertNotEqual(key, controls.trial_key(**{**base, **change}))

    def test_the_policy_digest_covers_the_planting_shells(self):
        self.assertTrue(policy_identity())


if __name__ == "__main__":
    unittest.main()
