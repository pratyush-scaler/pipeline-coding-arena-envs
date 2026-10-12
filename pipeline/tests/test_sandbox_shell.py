"""Patched designer probes execute before a draft has task steps."""
import json
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.author import agent_designer, sandbox


class LocalContainer:
    """Execute the real Variant patch against a temporary fixture, without Docker."""
    def __init__(self, tag, **_):
        self.bundle = Path(tag)
        self.repo = self.bundle / "environment/repo"
        self.tmp = self.bundle / "shell-tmp"
        self.tmp.mkdir(exist_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def put_bytes(self, data, path):
        (self.tmp / Path(path).name).write_bytes(data)

    def exec(self, command, **_):
        command = command.replace("/app/repo", str(self.repo)).replace("/tmp/variant.patch", str(self.tmp / "variant.patch"))
        out = subprocess.run(command, shell=True, cwd=self.repo, capture_output=True, text=True, timeout=10)
        return {"exit_code": out.returncode, "stdout": out.stdout, "stderr": out.stderr,
                "timed_out": False, "seconds": 0}


class PatchedDesignerShell(unittest.TestCase):
    FIX = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-print('broken')\n+print('fixed')\n"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        repo = self.bundle / "environment/repo"
        repo.mkdir(parents=True)
        (repo / "app.py").write_text("print('broken')\n")
        (self.bundle / "environment/toolchain.json").write_text(json.dumps({"limits": {}}))
        self.container = patch.object(sandbox, "Container", LocalContainer)
        self.container.start()
        self.addCleanup(self.container.stop)
        self.image = patch.object(sandbox, "build_image", lambda bundle, target: str(bundle))
        self.image.start()
        self.addCleanup(self.image.stop)

    def test_designer_patch_runs_without_task_steps(self):
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle, moments=[])
        log = self.bundle / "authoring/log"
        handlers = agent_designer._handlers(self.bundle, extraction, {"messages": []},
                                             self.bundle / "draft.json", log=log)
        handlers["stage_spec"](path=["toolchain"], value={"name": "python"})
        with patch.object(sandbox, "available", return_value=(True, "ok")):
            out = handlers["sandbox_run"]("python3 app.py", patch=self.FIX)
        self.assertEqual(out["exit_code"], 0, out)
        self.assertEqual(out["stdout"], "fixed\n")
        self.assertFalse((log / "scratch/001/task.toml").exists())
        self.assertEqual((self.bundle / "environment/repo/app.py").read_text(), "print('broken')\n")

    def test_full_bundle_missing_task_config_still_fails(self):
        with self.assertRaises(FileNotFoundError):
            sandbox.shell(self.bundle, "python3 app.py", setup=sandbox.Variant("patch", patch=self.FIX))

    def test_full_bundle_malformed_task_config_still_fails(self):
        (self.bundle / "task.toml").write_text("[[steps]\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            sandbox.shell(self.bundle, "python3 app.py", setup=sandbox.Variant("patch", patch=self.FIX))

    def test_full_bundle_setup_receives_first_step(self):
        (self.bundle / "task.toml").write_text('[[steps]]\nname = "01-fix"\n[[steps]]\nname = "02-follow"\n')
        setup = sandbox.Variant("patch", patch=self.FIX)
        with patch.object(setup, "apply", wraps=setup.apply) as apply:
            out = sandbox.shell(self.bundle, "python3 app.py", setup=setup)
        self.assertEqual(out["stdout"], "fixed\n")
        self.assertEqual(apply.call_args.args[1], "01-fix")
