"""The Arena reproduction gate (user, 2026-10-12): a zip ships only with a passing receipt for exactly its bytes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import arena_repro, policy


class ReproGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.zip = self.tmp / "a_f2_example_20261012.zip"
        self.zip.write_bytes(b"PK\x05\x06" + b"\0" * 18)

    def receipt(self, **over):
        doc = {"schema": arena_repro.SCHEMA, "zip": self.zip.name, "zip_sha256": arena_repro.sha256(self.zip),
               "harness": {"version": policy.ARENA_HARBOR_VERSION, "commit": policy.ARENA_HARBOR_COMMIT},
               "resources": dict(policy.ARENA_REPRO_RESOURCES),
               "oracle": {"reward": 1.0, "exception": None}, "nop": {"reward": 0.0, "exception": None}}
        doc.update(over)
        path = arena_repro.receipt_path(self.zip)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def test_passing_receipt_ships(self):
        self.receipt()
        self.assertEqual(arena_repro.receipt_problems(self.zip), [])
        with mock.patch.object(policy, "ARENA_FORMAT_REQUIRED", False):
            self.assertEqual(arena_repro.gate([self.zip]), [])
        # Arena's format (2026-10-22) is a gate of its own: a legacy layout no longer ships
        self.assertIn("one folder <env-id>/", " ".join(arena_repro.gate([self.zip])))

    def test_missing_receipt_refuses(self):
        self.assertIn("no Arena reproduction receipt", " ".join(arena_repro.receipt_problems(self.zip)))

    def test_changed_bytes_refuse(self):
        self.receipt()
        self.zip.write_bytes(self.zip.read_bytes() + b"x")
        self.assertIn("other bytes", " ".join(arena_repro.receipt_problems(self.zip)))

    def test_oracle_below_one_refuses(self):
        self.receipt(oracle={"reward": 0.95, "exception": None})
        self.assertIn("oracle scored 0.95", " ".join(arena_repro.receipt_problems(self.zip)))

    def test_nop_above_zero_refuses(self):
        self.receipt(nop={"reward": 0.1, "exception": None})
        self.assertIn("nop scored 0.1", " ".join(arena_repro.receipt_problems(self.zip)))

    def test_exception_refuses_even_with_rewards(self):
        self.receipt(nop={"reward": 0.0, "exception": "EnvironmentStartTimeoutError"})
        self.assertIn("nop raised", " ".join(arena_repro.receipt_problems(self.zip)))

    def test_other_harness_or_resources_refuse(self):
        self.receipt(harness={"version": "0.22.0", "commit": "deadbeef"})
        self.assertIn("not 0.20.0", " ".join(arena_repro.receipt_problems(self.zip)))
        self.receipt(resources={"cpus": 4, "memory_mb": 8192})
        self.assertIn("reproduced at", " ".join(arena_repro.receipt_problems(self.zip)))

    def test_gate_switch(self):
        with mock.patch.object(policy, "ARENA_REPRO_REQUIRED", False), \
                mock.patch.object(policy, "ARENA_FORMAT_REQUIRED", False):
            self.assertEqual(arena_repro.gate([self.zip]), [])
        self.assertTrue(arena_repro.gate([self.zip]))


if __name__ == "__main__":
    unittest.main()
