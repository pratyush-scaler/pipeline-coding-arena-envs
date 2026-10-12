"""A declared deliverable is read only as a regular file reached without links."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from pipeline.author.grader_runtime import grade


class DeliverableLinks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "out").mkdir(parents=True)
        self.secret = self.root / "hidden.json"
        self.secret.write_text(json.dumps({"expected": 42}))

    def errors(self, path, kind="json"):
        return grade.contract_errors([{"path": path, "kind": kind}], self.repo)

    def test_a_regular_json_deliverable_passes(self):
        (self.repo / "out/report.json").write_text('{"ok": true}')
        self.assertEqual(self.errors("out/report.json"), [])
        self.assertEqual(self.errors(str(self.repo / "out/report.json")), [])

    def test_missing_and_invalid_deliverables_are_contract_errors(self):
        self.assertEqual(len(self.errors("out/absent.json")), 1)
        (self.repo / "out/bad.json").write_text("{not json")
        self.assertIn("invalid", self.errors("out/bad.json")[0])

    def test_a_linked_deliverable_is_refused_not_followed(self):
        os.symlink(self.secret, self.repo / "out/report.json")
        self.assertIn("linked", self.errors("out/report.json")[0])

    def test_a_linked_parent_directory_is_refused(self):
        os.symlink(self.root, self.repo / "escape", target_is_directory=True)
        self.assertEqual(len(self.errors("escape/hidden.json")), 1)

    def test_parent_traversal_is_refused(self):
        self.assertIn("parent traversal", self.errors("../hidden.json")[0])


if __name__ == "__main__":
    unittest.main()
