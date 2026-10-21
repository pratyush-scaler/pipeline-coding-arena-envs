"""Whole-pipeline runs on Coder: launch, status, pull and the pause guard, offline.

User, 2026-10-03: "run everything concurrently on Coder, the authoring, the
repairs, the calibration". Nothing here reaches a deployment: the launcher and
status scripts are executed locally against a temporary root.
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.benchmark import coder, coder_run


class Arguments(unittest.TestCase):
    def test_arena_args_and_checkout_scripts_become_remote_argv(self):
        argv = coder_run.command_argv(["build", "--need", "10"])
        self.assertEqual(argv[1:4], ["-m", "pipeline.cli", "build"])
        self.assertTrue(argv[0].endswith(".venv/bin/python"))
        script = coder_run.command_argv(["category_batch.py", "--out", "envs-x"])
        self.assertTrue(script[1].endswith("category_batch.py"))
        with self.assertRaises(ValueError):
            coder_run.command_argv(["../elsewhere.py"])
        with self.assertRaises(ValueError):
            coder_run.command_argv([])

    def test_remote_env_runs_calibration_locally_and_refuses_credentials(self):
        with patch.object(coder_run, "dump_path", return_value=Path("/data/dump")):
            env = coder_run.remote_env({"ARENA_TASK_WORKERS": "12"})
            self.assertEqual(env["ARENA_NATIVE_BACKEND"], "local")
            self.assertEqual(env["ARENA_DUMP"], "/data/dump")
            for name in ("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "X_SECRET_KEY"):
                with self.assertRaises(ValueError):
                    coder_run.remote_env({name: "x"})

    def test_credentials_come_from_keys_env_never_the_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = Path(tmp) / "keys.env"
            keys.write_text("ANTHROPIC_AUTH_TOKEN=from-file\nOPENAI_API_KEY=\n")
            with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "inherited", "OPENAI_API_KEY": "inherited"}):
                values = coder_run.keys_values(keys)
        self.assertEqual(values, {"ANTHROPIC_AUTH_TOKEN": "from-file"})
        with tempfile.TemporaryDirectory() as tmp:
            keys = Path(tmp) / "keys.env"
            keys.write_text("ANTHROPIC_API_KEY=k\nOPENAI_BASE_URL=http://198.18.240.3:18443/v1\nOPEN_AI_API_KEY1=o\n")
            self.assertEqual(coder_run.keys_values(keys), {"ANTHROPIC_API_KEY": "k", "OPEN_AI_API_KEY1": "o"})
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "keys.env"
            empty.write_text("OPENAI_API_KEY=x\n")
            with self.assertRaises(ValueError):
                coder_run.keys_values(empty)

    def test_persistence_bind_mounts_every_synced_path(self):
        script = coder_run.persist_script([Path("/Users/u/repo"), Path("/Users/u/dump")])
        self.assertEqual(script.count("mount --bind"), 2)
        self.assertIn("arena-mirror", script)
        self.assertIn("persistent=", script)


class DataSync(unittest.TestCase):
    def test_peer_and_artifact_pushes_never_delete_or_overwrite_newer_remote_work(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ("envs-a/t", "envs-b", "reference", "pipeline-artifacts/classify-cache"):
                (root / d).mkdir(parents=True)
            with patch.object(coder_run, "ROOT", root), \
                    patch.object(coder_run, "dump_path", return_value=root / "dump"), \
                    patch.object(coder_run, "remote_trees", return_value=set()), \
                    patch.object(coder_run, "rsync", side_effect=lambda *a, **k: calls.append((a, k))):
                coder_run.sync_data("ws")
        targets = {a[1]: k for a, k in calls}
        for name in ("envs-a", "envs-b", "reference", "pipeline-artifacts"):
            kwargs = next(k for t, k in targets.items() if t.rstrip("/").endswith(name))
            self.assertFalse(kwargs.get("delete"), name)
            self.assertTrue(kwargs.get("update"), name)

    def test_a_tree_a_live_run_is_writing_is_never_pushed(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ("envs-s1/t", "envs-s2/t", "reference"):
                (root / d).mkdir(parents=True)
            run = root / coder_run.RUNS / "r1"
            run.mkdir(parents=True)
            (run / "launch.json").write_text(json.dumps({"workspace": "ws", "argv": ["py", "-m", "pipeline.cli",
                                                                                      "build", "-o", "envs-s1"]}))
            with patch.object(coder_run, "ROOT", root), \
                    patch.object(coder_run, "dump_path", return_value=root / "dump"), \
                    patch.object(coder_run, "remote_trees", return_value=set()), \
                    patch.object(coder_run, "rsync", side_effect=lambda *a, **k: calls.append(a[1])):
                done = coder_run.sync_data("ws", dump=False, artifacts=False)
            self.assertEqual(done["skipped_live_trees"], ["envs-s1"])
            self.assertFalse(any("envs-s1" in c for c in calls))
            self.assertTrue(any("envs-s2" in c for c in calls))
            (run / "finished.json").write_text("{}")
            with patch.object(coder_run, "ROOT", root):
                self.assertEqual(coder_run.live_trees(), set())

    def test_a_tree_the_workspace_already_has_is_never_pushed_again(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ("envs-s1/t", "envs-s9/t"):
                (root / d).mkdir(parents=True)
            with patch.object(coder_run, "ROOT", root), \
                    patch.object(coder_run, "remote_trees", return_value={"envs-s1"}), \
                    patch.object(coder_run, "rsync", side_effect=lambda *a, **k: calls.append(a[1])):
                coder_run.sync_data("ws", dump=False, artifacts=False)
        self.assertFalse(any("envs-s1" in c for c in calls))
        self.assertTrue(any("envs-s9" in c for c in calls))

    def test_the_rsync_command_carries_update_and_the_coder_transport(self):
        seen = {}
        def fake(args, **kwargs):
            seen["args"] = args
            return subprocess.CompletedProcess(args, 0, b"", b"")
        with patch.object(coder_run.coder_mod, "_binary", return_value="/usr/bin/coder"), \
                patch.object(coder_run.subprocess, "run", side_effect=fake):
            coder_run.rsync("ws", "/a/", "/a/", push=True, update=True)
        self.assertIn("--update", seen["args"])
        self.assertNotIn("--delete", seen["args"])
        self.assertEqual(seen["args"][-1], "ws:/a/")


class Sharding(unittest.TestCase):
    def test_ranges_expand_and_split_round_robin_without_overlap(self):
        ranks = coder_run.expand_ranks("1-7,10")
        self.assertEqual(ranks, [1, 2, 3, 4, 5, 6, 7, 10])
        plan = coder_run.shard_plan(["a", "b", "c"], ranks)
        self.assertEqual(plan, {"a": [1, 4, 7], "b": [2, 5, 10], "c": [3, 6]})
        self.assertEqual(sorted(r for rs in plan.values() for r in rs), ranks)
        for bad in ("0-2", "1,1", "x", ""):
            with self.assertRaises(ValueError):
                coder_run.expand_ranks(bad)
        with self.assertRaises(ValueError):
            coder_run.shard_plan(["a", "a"], [1])

    def test_shard_owns_the_ranks_flag_and_launches_each_workspace(self):
        with patch.object(coder_run, "launch", side_effect=lambda ws, args, **k: {"workspace": ws, "run": "r", "args": args}):
            rows = coder_run.shard(["a", "b"], [1, 2, 3], ["build", "-o", "envs-x"])
        self.assertEqual({r["workspace"]: r["args"][-1] for r in rows}, {"a": "1,3", "b": "2"})
        with self.assertRaises(ValueError):
            coder_run.shard(["a"], [1], ["build", "--ranks", "1"])


class LauncherAndStatus(unittest.TestCase):
    def test_a_launched_run_is_detached_records_its_exit_and_keeps_keys_off_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, shm = Path(tmp) / "root", Path(tmp) / "shm"
            root.mkdir(); shm.mkdir()
            run_dir = root / coder_run.RUNS / "r1"
            request = {"root": str(root), "run_dir": str(run_dir), "keys_dir": str(shm),
                       "argv": [sys.executable, "-c", "import os; print('hello', os.environ['ARENA_X'])"],
                       "env": {"ARENA_X": "1"}, "keys": {"ANTHROPIC_AUTH_TOKEN": "tok"}, "code_sha": "abc"}
            out = subprocess.run([sys.executable, "-c", coder_run.LAUNCHER], input=json.dumps(request),
                                 capture_output=True, text=True, check=True)
            started = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertTrue(started["pid"])
            deadline = time.time() + 20
            while not (run_dir / "exit_code").is_file() and time.time() < deadline:
                time.sleep(0.1)
            self.assertEqual((run_dir / "exit_code").read_text().strip(), "0")
            self.assertIn("hello 1", (run_dir / "run.log").read_text())
            link = root / "keys.env"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (shm / "arena-keys.env").resolve())
            self.assertEqual(oct((shm / "arena-keys.env").stat().st_mode & 0o777), "0o600")
            cmd = json.loads((run_dir / "cmd.json").read_text())
            self.assertNotIn("tok", json.dumps(cmd))
            status = subprocess.run([sys.executable, "-c", coder_run.STATUS, str(root / coder_run.RUNS)],
                                    capture_output=True, text=True, check=True)
            rows = json.loads(status.stdout)
            self.assertEqual(rows[0]["run"], "r1")
            self.assertEqual(rows[0]["exit_code"], "0")
            self.assertFalse(rows[0]["alive"])


class Staged(unittest.TestCase):
    def test_the_link_farm_links_everything_but_code_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dest = Path(tmp) / "root", Path(tmp) / "root/.staged/abc"
            for name in ("pipeline", "rulesets", "envs-s1", "pipeline-artifacts", "datasets", ".venv"):
                (root / name).mkdir(parents=True)
            for name in ("CLAUDE.md", "workspace.toml", "keys.env", "arena"):
                (root / name).write_text("x")
            dest.mkdir(parents=True)
            subprocess.run([sys.executable, "-c", coder_run.LINK_FARM, str(root), str(dest),
                            "pipeline,rulesets,CLAUDE.md,arena"], check=True, capture_output=True)
            links = sorted(p.name for p in dest.iterdir())
            self.assertEqual(links, sorted(["envs-s1", "pipeline-artifacts", "datasets", ".venv", "workspace.toml"]))
            self.assertTrue(all((dest / name).is_symlink() for name in links))
            self.assertEqual((dest / "envs-s1").resolve(), (root / "envs-s1").resolve())
            # idempotent: a second pass adds nothing
            again = subprocess.run([sys.executable, "-c", coder_run.LINK_FARM, str(root), str(dest),
                                    "pipeline,rulesets,CLAUDE.md,arena"], check=True, capture_output=True, text=True)
            self.assertEqual(again.stdout.strip(), "0")

    def _native_venv(self, root, dest, uv):
        return subprocess.run([sys.executable, "-c", coder_run.NATIVE_VENV, str(root), str(dest), str(uv)],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_a_snapshot_pinning_another_harbor_gets_its_own_native_venv(self):
        """2026-10-22 (Harbor 0.22.0 -> 0.24.0): the link farm pointed every snapshot's
        .venv-harbor at ROOT's, which live runs hold at the old pin."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            dest = root / coder_run.STAGED / "abc"
            dest.mkdir(parents=True)
            (root / ".venv-harbor").mkdir()
            (root / "requirements-native.lock").write_text("harbor==0.22.0\n")
            calls = Path(tmp) / "calls.txt"
            uv = Path(tmp) / "uv"
            uv.write_text("#!" + sys.executable + "\n"
                          "import os, sys\n"
                          f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:3]) + '\\n')\n"
                          "if sys.argv[1] == 'venv':\n"
                          "    os.makedirs(os.path.join(sys.argv[-1], 'bin'))\n"
                          "    py = os.path.join(sys.argv[-1], 'bin', 'python')\n"
                          "    open(py, 'w').write('#!/bin/sh\\nexit 0\\n'); os.chmod(py, 0o755)\n")
            uv.chmod(0o755)
            # Same pin as the live checkout: nothing installed, the link farm links ROOT's.
            (dest / "requirements-native.lock").write_text("harbor==0.22.0\n")
            self.assertEqual(self._native_venv(root, dest, uv), "shared")
            self.assertFalse(calls.exists())
            self.assertFalse((dest / ".venv-harbor").is_symlink())
            # Another pin: its own venv, keyed by the lock, never ROOT's.
            (dest / "requirements-native.lock").write_text("harbor==0.24.0\n")
            (dest / ".venv-harbor").symlink_to(root / ".venv-harbor")   # an older staging's link
            venv = Path(self._native_venv(root, dest, uv))
            self.assertEqual(venv.parent, root / coder_run.STAGED)
            self.assertTrue(venv.name.startswith("venv-harbor-"))
            self.assertEqual(os.readlink(dest / ".venv-harbor"), str(venv))
            self.assertEqual(calls.read_text().split("\n")[:2], ["venv --python", "pip install"])
            self.assertTrue((venv / ".arena-lock-sha256").is_file())
            self.assertEqual(sorted(p.name for p in (root / ".venv-harbor").iterdir()), [])
            # A second snapshot with the same pin reuses it: no second install.
            other = root / coder_run.STAGED / "def"
            other.mkdir()
            (other / "requirements-native.lock").write_text("harbor==0.24.0\n")
            self.assertEqual(Path(self._native_venv(root, other, uv)), venv)
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            # The link farm then leaves the snapshot's own link alone.
            subprocess.run([sys.executable, "-c", coder_run.LINK_FARM, str(root), str(other), "pipeline"],
                           check=True, capture_output=True)
            self.assertEqual(os.readlink(other / ".venv-harbor"), str(venv))

    def test_a_staged_launch_runs_from_the_snapshot_and_writes_to_root_artifacts(self):
        sent = {}

        def ssh(ws, command, stdin=None, timeout=None, check=True):
            if stdin:
                sent["request"] = json.loads(stdin.decode())
            out = json.dumps({"pid": 7, "run_dir": "x"}).encode()
            return subprocess.CompletedProcess([], 0, out, b"")
        snapshot = coder_run.ROOT / coder_run.STAGED / "abc"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(coder_run, "ensure_running"), patch.object(coder_run, "sync_data"), \
                patch.object(coder_run, "prepare") as prepare, patch.object(coder_run, "ensure_dump") as dump, \
                patch.object(coder_run, "stage_code", return_value=snapshot), \
                patch.object(coder_run, "keys_values", return_value={}), \
                patch.object(coder_run, "dump_path", return_value=Path("/data/dump")), \
                patch.object(coder_run, "RUNS", Path(tmp).relative_to(coder_run.ROOT)
                             if str(tmp).startswith(str(coder_run.ROOT)) else coder_run.RUNS), \
                patch.object(coder_run.coder_mod, "ssh", ssh):
            record = coder_run.launch("ws", ["harden", "/abs/bundle", "--apply"], staged=True)
        prepare.assert_not_called()                      # no code sync under live runs
        dump.assert_called_once_with("ws", "/data/dump")  # the dump the run reads is mounted first
        self.assertEqual(sent["request"]["root"], str(snapshot))
        self.assertEqual(sent["request"]["env"]["ARENA_PIPELINE_ARTIFACTS"],
                         str(coder_run.ROOT / "pipeline-artifacts"))
        self.assertEqual(record["code_root"], str(snapshot))
        import shutil
        shutil.rmtree(coder_run.ROOT / coder_run.RUNS / record["run"], ignore_errors=True)


class DumpMountedBeforeLaunch(unittest.TestCase):
    """A restart drops the dump's bind mount; a launch re-mounts it or refuses (2026-10-21, three hard lineages)."""

    def _ssh(self, reply):
        calls = []

        def ssh(ws, command, stdin=None, timeout=None, check=True):
            calls.append(command)
            return subprocess.CompletedProcess([], 0, reply.encode(), b"")
        return ssh, calls

    def test_the_dump_is_remounted_from_the_mirror(self):
        ssh, calls = self._ssh("persistent=1\nDUMP_OK\n")
        with patch.object(coder_run.coder_mod, "ssh", ssh):
            coder_run.ensure_dump("ws", "/data/dump")
        self.assertIn('mount --bind "$M" "$P"', calls[0])
        self.assertIn("/data/dump/sessions", calls[0])

    def test_a_dump_the_workspace_does_not_hold_refuses_the_launch(self):
        ssh, _ = self._ssh("persistent=1\n")
        with patch.object(coder_run.coder_mod, "ssh", ssh), self.assertRaisesRegex(RuntimeError, "no trace dump"):
            coder_run.ensure_dump("ws", "/data/dump")


class Pull(unittest.TestCase):
    def _blob(self, tmp, members):
        blob = Path(tmp) / "pull.tgz"
        with tarfile.open(blob, "w:gz") as tar:
            for name, data, mtime in members:
                info = tarfile.TarInfo(name)
                info.size, info.mtime = len(data), mtime
                tar.addfile(info, io.BytesIO(data))
        return blob

    def test_only_result_trees_are_written_and_newer_local_files_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "envs/t").mkdir(parents=True)
            newer = root / "envs/t/kept.json"
            newer.write_text("local")
            blob = self._blob(tmp, [
                ("envs/t/new.json", b"remote", time.time()),
                ("envs/t/kept.json", b"remote-old", time.time() - 3600),
                ("pipeline-artifacts/native/x/report.json", b"{}", time.time()),
                ("pipeline/policy.py", b"evil", time.time()),
                ("keys.env", b"evil", time.time()),
                ("../outside.txt", b"evil", time.time())])
            counts = coder_run.extract_pull(blob, root)
            self.assertEqual((root / "envs/t/new.json").read_text(), "remote")
            self.assertEqual(newer.read_text(), "local")
            self.assertTrue((root / "pipeline-artifacts/native/x/report.json").is_file())
            self.assertFalse((root / "pipeline/policy.py").exists())
            self.assertFalse((root / "keys.env").exists())
            self.assertFalse((Path(tmp) / "outside.txt").exists())
            self.assertEqual(counts, {"written": 2, "kept_newer_local": 1, "refused": 3})


class PauseGuard(unittest.TestCase):
    def test_no_launch_record_means_no_remote_call(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(coder_run, "ROOT", Path(tmp)), \
                patch.object(coder_run, "runs", side_effect=AssertionError("no remote call")):
            self.assertFalse(coder._remote_run_alive("arena-calib-1"))

    def test_an_open_launch_blocks_the_pause_even_when_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(coder_run, "ROOT", Path(tmp)):
            run = Path(tmp) / coder_run.RUNS / "r1"
            run.mkdir(parents=True)
            (run / "launch.json").write_text(json.dumps({"workspace": "ws"}))
            with patch.object(coder_run, "runs", side_effect=RuntimeError("ssh dropped")):
                self.assertTrue(coder._remote_run_alive("ws"))
            with patch.object(coder_run, "runs", return_value=[{"run": "r1", "alive": False}]):
                self.assertFalse(coder._remote_run_alive("ws"))
            self.assertTrue((run / "finished.json").is_file())
            self.assertEqual(coder_run.open_launches("ws"), [])


class Cli(unittest.TestCase):
    def test_the_arena_cli_routes_remote_actions(self):
        from pipeline import cli
        with patch("pipeline.benchmark.coder_run.main", return_value=0) as main:
            self.assertEqual(cli.main(["coder", "runs", "ws"]), 0)
        main.assert_called_once_with(["runs", "ws"])
        with patch("pipeline.benchmark.coder_run.main", return_value=0) as main:
            cli.main(["coder", "run", "ws", "--", "build", "--need", "5"])
        main.assert_called_once_with(["run", "ws", "--", "build", "--need", "5"])


if __name__ == "__main__":
    unittest.main()
