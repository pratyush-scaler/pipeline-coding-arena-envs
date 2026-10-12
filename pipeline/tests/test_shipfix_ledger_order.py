"""shipfix.update_ledger writes what faithfulness.build would: Path order and fresh declarations.

c-019 v6 (2026-10-08): the release check rebuilt the ledger and compared it list for list. Plain string
order put `values-night/` before `values/`, and a second file reading one host path was dropped, so
the semantic review refused the task as "source provenance is unverified".
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import shipfix
from pipeline.ledger import faithfulness


class LedgerOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name)
        repo = self.bundle / faithfulness.FIXTURE
        files = {"res/values/colors.xml": "<a/>", "res/values-night/colors.xml": "<b/>",
                 "tools/icons.py": 'BASE = "/home/user/wirutech-os"\n',
                 "tools/ultra_icons.py": 'BASE = "/home/user/wirutech-os"\n'}
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        entries = [{"path": rel, "origin": "lifted", "sha256": shipfix._sha(text.encode()), "bytes": len(text),
                    "citation": None, "similarity": None, "note": ""} for rel, text in sorted(files.items())]
        ledger = {"entries": entries, "recovery_omissions": [
            {"path": "big.mp4", "reason": "too_large"},
            {"path": "/home/user/wirutech-os", "read_by": "tools/icons.py", "reason": "host_path_never_in_trace"},
            {"path": "/home/user/gone", "read_by": "tools/old.py", "reason": "host_path_never_in_trace"}]}
        (self.bundle / "provenance").mkdir()
        (self.bundle / "provenance/ledger.json").write_text(json.dumps(ledger))
        self.repo = repo

    def tearDown(self):
        self.tmp.cleanup()

    def _update(self):
        original = faithfulness.with_level
        faithfulness.with_level = lambda bundle, ledger, *a: ledger
        try:
            return shipfix.update_ledger(self.bundle, {}, [])
        finally:
            faithfulness.with_level = original

    def test_entries_follow_the_order_build_writes(self):
        ledger = self._update()
        built = [p.relative_to(self.repo).as_posix() for p in sorted(self.repo.rglob("*")) if p.is_file()]
        self.assertEqual([e["path"] for e in ledger["entries"]], built)
        self.assertLess(built.index("res/values/colors.xml"), built.index("res/values-night/colors.xml"))

    def test_declarations_are_recomputed_like_build(self):
        ledger = self._update()
        want = [{"path": "big.mp4", "reason": "too_large"}] + faithfulness.extraction_exclusions(self.bundle) \
            + faithfulness.host_paths(self.bundle)
        self.assertEqual(ledger["recovery_omissions"], want)
        readers = sorted(o["read_by"] for o in ledger["recovery_omissions"] if o["reason"] == "host_path_never_in_trace")
        self.assertEqual(readers, ["tools/icons.py", "tools/ultra_icons.py"])


if __name__ == "__main__":
    unittest.main()
