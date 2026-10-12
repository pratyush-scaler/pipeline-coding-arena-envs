"""Trusted history must come from prior captures, never solver claims or logs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pipeline.benchmark.step_history import HistoryEvidenceError, build_history


class StepHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved: on macOS the temp root is under /var -> /private/var, and
        # the symlink guard then refuses every path beneath it, so these tests
        # would "pass" on refusals that have nothing to do with what they test.
        self.root = Path(temporary.name).resolve()
        self.trial = self.root / "trial"
        self.capture = self.trial / "steps/01-first/artifacts"
        self.capture.mkdir(parents=True)
        self.task = {"artifacts": [{"source": "/app/first.zip", "destination": "captured/release.zip"}],
                     "steps": [{"name": "01-first"}, {"name": "02-next"}, {"name": "03-future"}]}
        self.original = b"first submitted ZIP bytes"
        self.file = self.capture / "captured/release.zip"
        self.file.parent.mkdir()
        self.file.write_bytes(self.original)
        self.manifest = [
            {"source": "/logs/artifacts", "destination": "artifacts/logs/artifacts", "type": "directory", "status": "empty", "service": None},
            {"source": "/app/first.zip", "destination": "artifacts/captured/release.zip", "type": "file", "status": "ok", "service": None}]
        self.write_manifest()

    def write_manifest(self):
        (self.capture / "manifest.json").write_text(json.dumps(self.manifest))

    def build(self, task=None, step="02-next", name="history"):
        output = self.root / name
        result = build_history(task or self.task, step, self.trial, output)
        return result, output

    def test_only_prior_declared_bytes_with_destination_and_hash_proof_are_copied(self):
        for relative in ("logs/artifacts/solver-hash.txt", "unlisted.txt"):
            p = self.capture / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("DO NOT COPY")
        for relative in ("steps/01-first/verifier/private-grader.txt", "steps/02-next/artifacts/app/future.txt",
                         "steps/03-future/artifacts/app/future.txt"):
            p = self.trial / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("DO NOT COPY")
        before = {str(p.relative_to(self.trial)): p.read_bytes() for p in self.trial.rglob("*") if p.is_file()}
        result, output = self.build()
        self.assertEqual((output / "01-first/app/first.zip").read_bytes(), self.original)
        self.assertEqual(sorted(str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()),
                         ["01-first/app/first.zip", "manifest.json"])
        captured = result["steps"][0]
        self.assertEqual(captured["capture_manifest_sha256"], hashlib.sha256((self.capture / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(captured["artifacts"][0]["files"], [{"path": "01-first/app/first.zip", "bytes": len(self.original),
                           "sha256": hashlib.sha256(self.original).hexdigest()}])
        self.assertEqual(before, {str(p.relative_to(self.trial)): p.read_bytes() for p in self.trial.rglob("*") if p.is_file()})

    def test_first_and_single_step_have_explicit_no_prior_steps(self):
        result, output = self.build(step="01-first")
        self.assertEqual(result["steps"], [])
        self.assertEqual([p.name for p in output.iterdir()], ["manifest.json"])
        result, _ = self.build(task={"artifacts": []}, step=None, name="single")
        self.assertEqual(result["steps"], [])

    def test_failed_capture_stays_missing_not_an_empty_substitute(self):
        self.file.unlink()
        self.manifest[1]["status"] = "failed"
        self.write_manifest()
        result, output = self.build()
        entry = result["steps"][0]["artifacts"][0]
        self.assertEqual((entry["status"], entry["files"]), ("failed", []))
        self.assertFalse((output / "01-first/app/first.zip").exists())

    def test_missing_or_corrupt_capture_bookkeeping_is_error(self):
        original = (self.capture / "manifest.json").read_bytes()
        for index, raw in enumerate((None, b"{partial", b"[]", b'{"entries":[]}')):
            with self.subTest(raw=raw):
                path = self.capture / "manifest.json"
                if raw is None:
                    path.unlink()
                else:
                    path.write_bytes(raw)
                with self.assertRaises(HistoryEvidenceError):
                    self.build(name=f"bad-{index}")
                path.write_bytes(original)
        self.file.unlink()
        with self.assertRaisesRegex(HistoryEvidenceError, "advertised captured bytes"):
            self.build(name="claimed-but-missing")

    def test_source_destination_status_or_service_substitution_is_rejected(self):
        original = copy.deepcopy(self.manifest)
        for index, changes in enumerate(({"source": "/app/another.zip"}, {"destination": "artifacts/../../private"},
                {"status": "invented"}, {"service": "private-grader"}, {"type": "symlink"})):
            with self.subTest(changes=changes):
                self.manifest = copy.deepcopy(original)
                self.manifest[1].update(changes)
                self.write_manifest()
                with self.assertRaises(HistoryEvidenceError):
                    self.build(name=f"substituted-{index}")

    def test_symlinked_file_manifest_and_nested_directory_are_not_followed(self):
        secret = self.root / "private"
        secret.write_bytes(b"PRIVATE")
        self.file.unlink()
        self.file.symlink_to(secret)
        with self.assertRaisesRegex(HistoryEvidenceError, "symlink"):
            self.build()
        self.file.unlink()
        self.file.write_bytes(self.original)
        manifest = self.capture / "manifest.json"
        manifest.unlink()
        manifest.symlink_to(secret)
        with self.assertRaisesRegex(HistoryEvidenceError, "symlink"):
            self.build(name="manifest-link")
        manifest.unlink()
        self.task["artifacts"] = ["/app/folder"]
        self.manifest[1].update(source="/app/folder", destination="artifacts/app/folder", type="directory")
        self.write_manifest()
        folder = self.capture / "app/folder"
        folder.mkdir(parents=True)
        (folder / "escape").symlink_to(secret)
        with self.assertRaisesRegex(HistoryEvidenceError, "symlink"):
            self.build(name="directory-link")

    def test_parent_path_escape_and_non_app_declarations_fail(self):
        for index, declaration in enumerate(("/app/../tests/private", "/tests/private", {"source": "/app/a", "destination": "../private"},
                                             {"source": "/app/a", "destination": "/absolute"}, {"source": "/app/a", "service": "grader"})):
            task = copy.deepcopy(self.task)
            task["artifacts"] = [declaration]
            with self.subTest(declaration=declaration), self.assertRaises(HistoryEvidenceError):
                self.build(task=task, name=f"escape-{index}")

    def test_directory_contents_and_step_only_artifacts_preserve_their_source_paths(self):
        self.task["artifacts"] = []
        self.task["steps"][0]["artifacts"] = ["/app/release"]
        self.manifest[1].update(source="/app/release", destination="artifacts/app/release", type="directory")
        self.write_manifest()
        folder = self.capture / "app/release"
        (folder / "nested").mkdir(parents=True)
        (folder / "nested/out.bin").write_bytes(b"submitted")
        result, output = self.build()
        self.assertEqual((output / "01-first/app/release/nested/out.bin").read_bytes(), b"submitted")
        self.assertEqual(len(result["steps"][0]["artifacts"][0]["files"]), 1)

    def test_unknown_current_step_and_duplicate_names_fail(self):
        with self.assertRaises(HistoryEvidenceError):
            self.build(step="unknown")
        self.task["steps"][1]["name"] = "01-first"
        with self.assertRaises(HistoryEvidenceError):
            self.build(name="duplicate")


if __name__ == "__main__":
    unittest.main()
