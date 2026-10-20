"""Control concurrency preserves evidence, isolation and failure reporting."""
import json
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pipeline.artifacts import identity
from pipeline.author import controls, sandbox
from pipeline.tests.synthetic_code import write_bundle


class ParallelControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = write_bundle(Path(self.tmp.name) / "bundle", two_steps=True)
        self.spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        self.steps = [s["name"] for s in self.spec["steps"]]
        self.mutants = {m["id"]: m for m in self.spec["mutants"]}

    def record(self, bundle, variants, *, work, **kwargs):
        label = work.name
        mutant = self.mutants.get(label.removeprefix("mutant-")) if label.startswith("mutant-") else None
        record = {"valid": True, "steps": {}}
        for step in self.steps:
            correct = label.startswith("golden-") or label in ("alternative", "benign_test", "neutral_additions")
            if mutant:
                mutant_step = mutant.get("step") or next(
                    c["step"] for c in self.spec["checks"] if c["id"] in mutant["fails"])
                correct = self.steps.index(step) <= self.steps.index(mutant_step)
            flags = {c["id"]: {"passed": c["kind"] == "guardrail" or correct}
                     for c in self.spec["checks"] if c["step"] == step}
            if mutant:
                for cid in mutant["fails"]:
                    if cid in flags:
                        flags[cid]["passed"] = False
            objectives = [c for c in self.spec["checks"] if c["step"] == step and c["kind"] == "objective"]
            reward = sum(c["weight"] for c in objectives if flags[c["id"]]["passed"]) / sum(c["weight"] for c in objectives)
            record["steps"][step] = {"reward": reward, "grade": {"checks": flags, "contract_errors": []}}
        record["reward"] = round(sum(s["reward"] for s in record["steps"].values()) / len(self.steps), 4)
        return record

    def run_controls(self, workers, verify):
        with patch.object(sandbox, "build_image", side_effect=lambda b, role, **kw: role), \
                patch.object(sandbox, "verify", side_effect=verify):
            return controls.run(self.bundle, work=Path(self.tmp.name) / f"work-{workers}", workers=workers)

    def test_two_workers_preserve_every_trial_and_error_in_policy_order(self):
        seen = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active = peak = 0
        def verify(bundle, variants, *, work, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                seen.append(work)
            try:
                if work.name in ("nop", "golden-1"):
                    barrier.wait(timeout=5)
                record = self.record(bundle, variants, work=work, **kwargs)
                # A real verifier failure must survive collection and block.
                if work.name == "test_tamper":
                    record["valid"] = False
                    record["steps"][self.steps[0]]["error"] = "verifier produced no reward"
                return record
            finally:
                with lock:
                    active -= 1
        def serial_verify(bundle, variants, *, work, **kwargs):
            record = self.record(bundle, variants, work=work, **kwargs)
            if work.name == "test_tamper":
                record["valid"] = False
                record["steps"][self.steps[0]]["error"] = "verifier produced no reward"
            return record
        serial = self.run_controls(1, serial_verify)
        parallel = self.run_controls(2, verify)
        for receipt in (serial, parallel):
            receipt.pop("seconds")
            receipt.pop("execution")
        self.assertEqual(serial, parallel)
        self.assertFalse(parallel["ok"])
        self.assertTrue(any("test_tamper" in e and "no reward" in e for e in parallel["errors"]))
        self.assertEqual(peak, 2)
        self.assertEqual(len(seen), len(set(seen)))
        # + prefix_only and skip_prior for every step after the first (Spec Appendix A)
        # + neutral_additions (2026-10-21 audit)
        self.assertEqual(len(seen), 3 + len(self.mutants) + 9 + 2 * (len(self.steps) - 1))
        self.assertEqual(parallel["artifact_sha256"], identity(self.bundle))

    def test_second_checkpoint_mutants_keep_only_prior_reference_steps(self):
        doc = json.loads((self.bundle / "tests/checks.json").read_text())
        plan = controls._trial_plan(self.bundle, self.spec, doc, self.steps, {"name": "python"}, 3)
        for mid, mutant in self.mutants.items():
            step = mutant.get("step") or next(c["step"] for c in doc["checks"] if c["id"] in mutant["fails"])
            variants = plan[f"mutant:{mid}"]
            self.assertEqual(list(variants), self.steps[:self.steps.index(step) + 1])
            self.assertEqual(variants[step].label, f"mutant:{mid}")
            for earlier in self.steps[:self.steps.index(step)]:
                self.assertEqual(variants[earlier].patch, sandbox.solve_variant(self.bundle, earlier).patch)

    def test_bundle_mutation_cannot_receive_a_newly_bound_receipt(self):
        def verify(bundle, variants, *, work, **kwargs):
            if work.name == "nop":
                instruction = self.bundle / "steps" / self.steps[0] / "instruction.md"
                instruction.write_text(instruction.read_text() + "\nchanged during execution\n")
            return self.record(bundle, variants, work=work, **kwargs)
        with self.assertRaisesRegex(sandbox.SandboxError, "bundle changed"):
            self.run_controls(2, verify)
        self.assertFalse((self.bundle / controls.RECEIPT).exists())

    def test_worker_exception_is_not_a_partial_green_receipt(self):
        def verify(bundle, variants, *, work, **kwargs):
            if work.name == "golden-2":
                raise sandbox.SandboxError("container failed")
            return self.record(bundle, variants, work=work, **kwargs)
        with self.assertRaisesRegex(sandbox.SandboxError, "container failed"):
            self.run_controls(2, verify)
        self.assertFalse((self.bundle / controls.RECEIPT).exists())

    def test_unbounded_or_invalid_concurrency_is_refused_before_execution(self):
        for workers in (0, 5, True, "2"):
            with self.subTest(workers=workers), patch.object(sandbox, "verify") as verify:
                with self.assertRaisesRegex(ValueError, "workers must be 1..4"):
                    controls.run(self.bundle, workers=workers)
                verify.assert_not_called()

    def test_sandbox_command_passes_requested_workers_and_reuses_controls_for_probes(self):
        from pipeline.cli import build_parser, cmd_sandbox
        receipt = {"ok": True, "seconds": 1.0, "errors": []}
        for workers in (1, 2):
            args = build_parser().parse_args(["sandbox", str(self.bundle), "--control-workers", str(workers)])
            with self.subTest(workers=workers), patch.object(sandbox, "available", return_value=(True, "")), \
                    patch.object(controls, "run", return_value=receipt) as run, \
                    patch("pipeline.author.probes.run", return_value={"ok": True}) as probes, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_sandbox(args), 0)
                run.assert_called_once_with(self.bundle.resolve(), workers=workers)
                probes.assert_called_once_with(self.bundle.resolve(), controls_receipt=receipt)


if __name__ == "__main__":
    unittest.main()
