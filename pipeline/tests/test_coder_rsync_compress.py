import subprocess
import unittest
from unittest import mock

from pipeline.benchmark import coder_run


class RsyncCompression(unittest.TestCase):
    def _args(self, **kw):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            coder_run.rsync("ws", "/a/", "/b/", push=False, **kw)
        return run.call_args.args[0]

    def test_compression_is_opt_in(self):
        self.assertNotIn("-z", self._args())
        self.assertIn("-z", self._args(compress=True))

    def test_a_pull_keeps_partial_and_the_remote_source(self):
        args = self._args(compress=True, timeout=1800)
        self.assertIn("--partial", args)
        self.assertEqual(args[-2:], ["ws:/a/", "/b/"])


if __name__ == "__main__":
    unittest.main()
