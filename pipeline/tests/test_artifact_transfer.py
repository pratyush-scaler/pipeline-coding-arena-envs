"""Regression coverage for protected fixture transfer on non-root hosts."""
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from pipeline.benchmark.offline_docker import extract_artifact_archive


class ArtifactTransferTests(unittest.TestCase):
    def archive(self, root, entries):
        path = root / "input.tar"
        with tarfile.open(path, "w") as stream:
            for name, kind, mode, content in entries:
                item = tarfile.TarInfo(name)
                item.type, item.mode = kind, mode
                if kind == tarfile.REGTYPE:
                    item.size = len(content)
                    stream.addfile(item, io.BytesIO(content))
                else:
                    item.linkname = content.decode() if content else ""
                    stream.addfile(item)
        return path

    def test_nested_read_only_unicode_fixtures_copy_without_byte_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"k,dp\n1,0.004\n2,0.0007\n"
            archive = self.archive(root, [
                (".", tarfile.DIRTYPE, 0o555, b""),
                ("./上手教程", tarfile.DIRTYPE, 0o555, b""),
                ("./上手教程/code", tarfile.DIRTYPE, 0o555, b""),
                ("./上手教程/code/history.csv", tarfile.REGTYPE, 0o444, payload),
                ("./run.sh", tarfile.REGTYPE, 0o555, b"#!/bin/sh\ntrue\n"),
            ])
            target = root / "out"
            extract_artifact_archive(archive, target)
            self.assertEqual((target / "上手教程/code/history.csv").read_bytes(), payload)
            self.assertEqual((target / "上手教程/code").stat().st_mode & 0o777, 0o755)
            self.assertEqual((target / "run.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual(len([p for p in target.rglob("*") if p.is_file()]), 2)

    def test_unsafe_entries_are_rejected_before_any_member_is_written(self):
        cases = [
            ("../escape", tarfile.REGTYPE, b"bad"),
            ("/escape", tarfile.REGTYPE, b"bad"),
            ("link", tarfile.SYMTYPE, b"../escape"),
            ("link", tarfile.LNKTYPE, b"../escape"),
            ("pipe", tarfile.FIFOTYPE, b""),
        ]
        for name, kind, payload in cases:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = self.archive(root, [
                    ("good.txt", tarfile.REGTYPE, 0o444, b"good"),
                    (name, kind, 0o777, payload),
                ])
                with self.assertRaises(ValueError):
                    extract_artifact_archive(archive, root / "out")
                self.assertEqual(list((root / "out").iterdir()), [])

    def test_duplicate_names_do_not_replace_artifact_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self.archive(root, [
                ("answer.json", tarfile.REGTYPE, 0o444, b"first"),
                ("./answer.json", tarfile.REGTYPE, 0o444, b"second"),
            ])
            with self.assertRaises(ValueError):
                extract_artifact_archive(archive, root / "out")


if __name__ == "__main__":
    unittest.main()
