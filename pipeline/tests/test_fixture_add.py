"""shipfix `fixture_add` (Arena 2026-10-11 R07): a referenced-but-missing fixture file is added from a pinned source."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import shipfix


class FixtureAdd(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "environment/repo/public").mkdir(parents=True)
        (self.root / "environment/repo/public/index.html").write_text('<script src="/vendor/k.js"></script>\n')
        self.src = self.root / "k.js"
        self.src.write_bytes(b"window.k=1;\n")
        self.sha = hashlib.sha256(self.src.read_bytes()).hexdigest()

    def edit(self, **over):
        item = {"path": "public/vendor/k.js", "from_file": str(self.src), "sha256": self.sha, "reason": "bundled"}
        item.update(over)
        return shipfix.edit_fixture(self.root, {"fixture_add": [item]})

    def test_adds_declares_and_pins(self):
        self.assertEqual(self.edit(), {"public/vendor/k.js": "bundled"})
        self.assertEqual((self.root / "environment/repo/public/vendor/k.js").read_bytes(), b"window.k=1;\n")
        doc = json.loads((self.root / "authoring/extraction/fixture-authored.json").read_text())
        self.assertEqual(doc["files"]["public/vendor/k.js"]["sha256"], self.sha)

    def test_refuses_wrong_hash_escape_and_overwrite(self):
        with self.assertRaises(ValueError):
            self.edit(sha256="0" * 64)
        with self.assertRaises(ValueError):
            self.edit(path="../outside.js")
        with self.assertRaises(ValueError):
            self.edit(path="public/index.html")


if __name__ == "__main__":
    unittest.main()
