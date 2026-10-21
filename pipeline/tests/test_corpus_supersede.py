"""A repaired version supersedes its own lineage's earlier release, explicitly; nothing else is retired (2026-10-21)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import corpus


def _entry(registry: Path, sha: str, bundle: str, session: str = "s1") -> None:
    (registry / f"{sha}.json").write_text(json.dumps(
        {"artifact_sha256": sha, "session_id": session, "fixture_hashes": {"h": 10}, "bundle": bundle}))


class Supersede(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = self.root / "releases"
        self.registry.mkdir()
        self.lineage = self.root / "envs/target-F1/c-006-pptx"
        self.new = self.lineage / "hardened/v8"
        self.new.mkdir(parents=True)
        candidate = {"artifact_sha256": "n" * 64, "session_id": "s1", "fixture_hashes": {"h": 10},
                     "bundle": str(self.new)}
        patcher = mock.patch.object(corpus, "signature", return_value=candidate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_lineages_own_earlier_release_is_retired_and_recorded(self):
        _entry(self.registry, "a" * 64, str(self.lineage / "hardened/v6"))
        _entry(self.registry, "b" * 64, str(self.root / "envs/target-F1/c-007-other"))          # same session
        self.assertEqual(len(corpus.problems(corpus.signature(self.new), self.registry)), 2)
        retired = corpus.supersede(self.new, "audit repair replaces the shipped v6", self.registry)
        self.assertEqual([r["artifact_sha256"] for r in retired], ["a" * 64])
        record = json.loads((self.registry / "retired" / ("a" * 64 + ".json")).read_text())["retired"]
        self.assertEqual(record["superseded_by"], str(self.new.resolve()))
        self.assertIn("audit repair", record["reason"])
        problems = corpus.problems(corpus.signature(self.new), self.registry)
        self.assertEqual(len(problems), 1)
        self.assertIn("b" * 64, problems[0])                                   # the other task still blocks

    def test_the_root_build_of_the_lineage_is_its_own_lineage_too(self):
        _entry(self.registry, "a" * 64, str(self.lineage))
        self.assertEqual(len(corpus.supersede(self.new, "repair", self.registry)), 1)

    def test_a_supersede_needs_a_reason(self):
        with self.assertRaises(ValueError):
            corpus.supersede(self.new, "  ", self.registry)


if __name__ == "__main__":
    unittest.main()
