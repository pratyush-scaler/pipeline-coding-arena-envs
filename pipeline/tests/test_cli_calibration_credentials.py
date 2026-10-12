"""Calibration commands keep Fable on the API key when an OAuth token is also configured (2026-10-08)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import cli


class CalibrationCredentials(unittest.TestCase):
    def _load(self, argv):
        with tempfile.TemporaryDirectory() as tmp:
            keys = Path(tmp) / "keys.env"
            keys.write_text("ANTHROPIC_API_KEY=sk-ant-api03-k\nANTHROPIC_AUTH_TOKEN=sk-ant-oat01-t\nOPENAI_API_KEY=o\n")
            with patch.object(cli, "KEYS", keys), patch.object(sys, "argv", argv), patch.dict(os.environ, {}, clear=True):
                cli.load_keys(prompt_if_missing=False)
                return dict(os.environ)

    def test_screen_and_shipfix_keep_the_api_key_only(self):
        for command in cli.CALIBRATION_COMMANDS:
            env = self._load(["arena", command, "/x"])
            self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-api03-k", command)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env, command)

    def test_authoring_keeps_the_oauth_token(self):
        env = self._load(["arena", "harden", "/x"])
        self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), "sk-ant-oat01-t")


if __name__ == "__main__":
    unittest.main()
