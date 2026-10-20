"""A replacement package bumps [task].version, recorded in its delivery receipt (2026-10-21, the audit repairs).

The task bank (arena-task-bank CONTRIBUTING step 2) asks a replaced task to bump its version; the repaired tasks were
about to be re-delivered under the 1.0.0 of the packages they replace."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import naming
from pipeline.tests.test_fix_delivery import AUTHORED, POLICY, _accept, _bundle

TOML = 'schema_version = "1.4"\n\n[task]\nname = "arena/x"\nversion = "1.0.0"\n\n[[steps]]\nname = "01-task"\n'


class SetVersion(unittest.TestCase):

    def test_only_the_task_version_changes(self):
        out = naming.set_version(TOML, "1.1.0")
        self.assertEqual(out, TOML.replace('version = "1.0.0"', 'version = "1.1.0"'))
        self.assertEqual(naming.next_version("1.0.0"), "1.1.0")
        self.assertEqual(naming.next_version("1.4.2"), "1.5.0")

    def test_a_malformed_version_or_a_missing_key_is_refused(self):
        with self.assertRaises(ValueError):
            naming.set_version(TOML, "1.1")
        with self.assertRaises(ValueError):
            naming.set_version(TOML.replace('version = "1.0.0"\n', ""), "1.1.0")


class RecordedBumpIsAccepted(unittest.TestCase):

    def setUp(self):
        from pipeline import deliver, deliverable
        self.deliver, self.deliverable = deliver, deliverable
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch("pipeline.artifacts.policy_identity", return_value=POLICY)
        patcher.start()
        self.addCleanup(patcher.stop)
        source = _bundle(self.root / "work", "rich", "c-001-editorial-review", fidelity=0.95, traps=AUTHORED)
        toml = source / "task.toml"
        toml.write_text(toml.read_text().replace('name = "arena/c-001-editorial-review"\n',
                                                 'name = "arena/c-001-editorial-review"\nversion = "1.0.0"\n'))
        _accept(source)
        self.deliver.deliver(self.root / "work", self.root / "out", accepted_only=True)
        self.out = self.root / "out/F2-scenario/a_f2_editorial-review"

    def test_the_bump_is_written_recorded_and_accepted(self):
        self.assertEqual(self.deliver.bump_version(self.out), {"previous": "1.0.0", "assigned": "1.1.0"})
        self.assertIn('version = "1.1.0"', (self.out / "task.toml").read_text())
        self.assertEqual(self.deliverable.acceptance_problems(self.out), [])
        self.assertEqual(self.deliver.bump_version(self.out)["assigned"], "1.1.0")      # idempotent

    def test_an_unrecorded_bump_is_not_accepted(self):
        path = self.out / "task.toml"
        path.write_text(path.read_text().replace('version = "1.0.0"', 'version = "1.1.0"'))
        self.assertTrue(self.deliverable.acceptance_problems(self.out))

    def test_a_receipt_claiming_more_than_the_next_minor_is_not_accepted(self):
        self.deliver.bump_version(self.out)
        path = self.out / "task.toml"
        path.write_text(path.read_text().replace('version = "1.1.0"', 'version = "2.0.0"'))
        receipt = json.loads((self.out / "verification/delivery.json").read_text())
        receipt["version"] = {"previous": "1.0.0", "assigned": "2.0.0"}
        (self.out / "verification/delivery.json").write_text(json.dumps(receipt))
        self.assertTrue(any("next minor" in p for p in self.deliverable.acceptance_problems(self.out)))


if __name__ == "__main__":
    unittest.main()
