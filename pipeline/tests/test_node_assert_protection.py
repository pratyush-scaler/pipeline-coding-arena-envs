"""Execute native Node workers to verify that repository code cannot disable assertions."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pipeline.author.grader_runtime.node_test import BOOTSTRAP


@unittest.skipUnless(shutil.which("node"), "requires the native Node executable")
class NativeAssertionsRemainEffective(unittest.TestCase):
    def run_native(self, root, source):
        selected = root / "private.test.cjs"
        selected.write_text(source)
        cfg = {"nodes": [str(selected)], "nonce": "isolated-regression",
               "report": str(root / "junit.xml"), "proof": str(root / "selection.json")}
        return subprocess.run(["node", "--input-type=module", "-e", BOOTSTRAP],
                              input=json.dumps(cfg), text=True, capture_output=True, timeout=30)

    def test_mutating_host_assertions_leaves_an_actual_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.cjs").write_text(
                "const assert = require('node:assert');\n"
                "const strictAssert = require('node:assert/strict');\n"
                "assert.equal = () => {}; strictAssert.equal = () => {};\n")
            proc = self.run_native(root,
                "const test = require('node:test');\n"
                "const assert = require('node:assert/strict');\n"
                "require('./agent.cjs');\n"
                "test('private equality', () => assert.equal(1, 2));\n")
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("<failure", (root / "junit.xml").read_text())

    def test_ordinary_assertion_imports_and_passing_cases_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = self.run_native(root,
                "const test = require('node:test');\n"
                "const assert = require('node:assert/strict');\n"
                "test('private equality', () => assert.equal(1, 1));\n")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("<failure", (root / "junit.xml").read_text())
