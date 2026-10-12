"""What CI can enforce about the repository itself, with no private values.

The deployment check in test_coder_pool compares against the operator's own
gitignored workspace.toml, so on a clean CI checkout it has nothing to compare
and skips. These checks need no private input: they look for the SHAPE of a
leak -- a real key, a real Coder deployment URL, a credentials file -- in every
tracked file.
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Real credential shapes. Test fixtures build fake keys at runtime
#: ("sk-proj-" + "x" * 30), so a literal match here is a real key.
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{40,}"
                    r"|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}"
                    # a PEM with its body, not a bare header used as test input
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----\s*[A-Za-z0-9+/=\s]{64,}")
#: A Coder deployment named in the repository. Placeholders use .invalid.
DEPLOYMENT = re.compile(r"https?://coder\.(?![a-z0-9-]*\.invalid\b)[a-z0-9.-]+\.[a-z]{2,}", re.I)


def tracked() -> list[Path]:
    if not shutil.which("git") or not (ROOT / ".git").exists():
        raise unittest.SkipTest("not a git checkout")
    names = subprocess.run(["git", "ls-files", "-z"], capture_output=True, cwd=ROOT,
                           check=True).stdout.decode().split("\0")
    return [ROOT / n for n in names if n and (ROOT / n).is_file()]


class RepositoryHygiene(unittest.TestCase):
    def test_no_credentials_file_is_tracked(self):
        names = {p.name for p in tracked()}
        for private in ("keys.env", "workspace.toml", "secrets.env", ".env"):
            self.assertNotIn(private, names)

    def test_no_real_key_in_any_tracked_file(self):
        hits = []
        for path in tracked():
            text = path.read_text(errors="ignore")
            hits += [f"{path.relative_to(ROOT)}: {m.group()[:10]}..." for m in SECRET.finditer(text)]
        self.assertEqual(hits, [])

    def test_no_real_coder_deployment_in_any_tracked_file(self):
        hits = []
        for path in tracked():
            for m in DEPLOYMENT.finditer(path.read_text(errors="ignore")):
                hits.append(f"{path.relative_to(ROOT)}: {m.group()}")
        self.assertEqual(hits, [])

    def test_the_patterns_catch_what_they_are_for(self):
        self.assertTrue(SECRET.search("key=sk-ant-" + "a1B2" * 8))
        self.assertTrue(DEPLOYMENT.search("url = https://coder." + "scaler.com"))
        self.assertFalse(DEPLOYMENT.search('url = "https://coder.example.invalid"'))
        self.assertFalse(SECRET.search('"sk-proj-" + "x" * 30'))
        self.assertFalse(SECRET.search("'-----BEGIN PRIVATE KEY-----']"))
        self.assertTrue(SECRET.search("-----BEGIN PRIVATE KEY-----\n" + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 3))


class CommandLine(unittest.TestCase):
    def test_every_subcommand_starts(self):
        from pipeline import cli
        parser = cli.build_parser()
        commands = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction").choices
        failures = []
        for name in sorted(commands):
            proc = subprocess.run([sys.executable, "-m", "pipeline.cli", name, "--help"],
                                  capture_output=True, text=True, cwd=ROOT, timeout=120)
            if proc.returncode != 0:
                failures.append(f"{name}: {proc.stderr.strip()[-200:]}")
        self.assertGreaterEqual(len(commands), 20)
        self.assertEqual(failures, [])
