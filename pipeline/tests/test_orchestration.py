import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from orchestration import config


class Config(unittest.TestCase):
    def test_batch_names_become_task_trees(self):
        self.assertEqual(config.tree_name("Oct 20 batch!"), "envs-oct-20-batch")
        self.assertEqual(config.tree_name("envs-x1"), "envs-x1")
        with self.assertRaises(ValueError):
            config.tree_name("!!!")

    def test_waves_dumps_and_workspaces_come_from_the_file(self):
        cfg = dict(config.DEFAULTS, workspaces=["a", "b"], dump="/d",
                   batches={"envs-b1": {"wave": "envs-b1.json"}, "envs-b2": {"wave": "w2.json", "dump": "/e"}})
        self.assertEqual(config.waves(cfg), {"envs-b1": "envs-b1.json", "envs-b2": "w2.json"})
        self.assertEqual(config.dumps(cfg), {"envs-b1": "/d", "envs-b2": "/e"})
        self.assertEqual(config.repro_workspace(cfg), "a")


class Driver(unittest.TestCase):
    def test_full_routing_applies_to_configured_batches_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"workspaces": ["w"], "batches": {"envs-b1": {"wave": "envs-b1.json",
                                                                                   "trap_ideas": tmp}}}))
            with mock.patch.object(config, "PATH", path):
                sys.modules.pop("orchestration.driver", None)
                driver = importlib.import_module("orchestration.driver")
            self.assertEqual(driver.WORKSPACES, ["w"])
            self.assertTrue(driver._in_batch("/r/envs-b1/target-F2-scenario/c-001-x"))
            self.assertFalse(driver._in_batch("/r/envs-other/target-F2-scenario/c-001-x"))
            row = {"lineage": "/r/envs-b1/target-F2-scenario/c-001-x"}
            self.assertEqual(driver._ideas_file(row, "c-001-x"), Path(tmp) / "c-001-x.json")
            sys.modules.pop("orchestration.driver", None)


class VerifyDump(unittest.TestCase):
    def test_only_differing_or_missing_session_files_are_copied_again(self):
        from orchestration import launch
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp)
            for name, body in (("data.json", "x" * 100), ("manifest.json", "{}")):
                p = dump / "sessions/s1" / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)
            ssh = types.SimpleNamespace(ssh=lambda ws, cmd, **k: types.SimpleNamespace(
                stdout=b"60 sessions/s1/data.json\n2 sessions/s1/manifest.json\n"))
            copied = []
            run = types.SimpleNamespace(rsync=lambda ws, a, b, **k: copied.append(a))
            launch.verify_dump({"launched": {"w": [1]}}, [{"session_id": "s1"}], dump, ssh, run)
        self.assertEqual([Path(c).name for c in copied], ["data.json"])


class Check(unittest.TestCase):
    def test_missing_prerequisites_are_named_with_their_fix(self):
        from orchestration import start
        cfg = dict(config.DEFAULTS, dump="/no/such/dump", workspaces=[])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(start, "ROOT", Path(tmp)), \
                mock.patch("pipeline.cli.load_keys", side_effect=SystemExit("no keys")), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch("builtins.print") as out:
            (Path(tmp) / "workspace.example.toml").write_text('[workspace]\ndump = "/absolute/path/to/arena-dump"\n')
            self.assertFalse(start.check(cfg))
            text = "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)
        for needle in ("./arena keys", "--dump", "[coder] url", "coder login", "--workspaces"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
