"""A directory the session `git clone`d from a remote repository is vendored
material, not fixture: excluded from the lift, accounted, named to the
designer. And a seed keeps its bundle across batches whatever rank it holds.

Found on the second pilot (2026-09-28): `c-003-pyqt6-desktop-app` lifted 104
files / 6,030,076 B of a partial whisper.cpp clone -- 98.7% of its fixture
bytes -- as `lifted` fidelity for code the user never wrote.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.author import extract as ex


def _trace(*commands: str) -> dict:
    return {"messages": [{"role": "assistant",
                          "content": [{"type": "tool-bash", "input": {"command": c}}]}
                         for c in commands]}


class UpstreamClonesAreDetectedFromTheTrace(unittest.TestCase):

    def test_explicit_target_directory(self):
        found = ex.upstream_clones(_trace(
            "cd /home/user && git clone https://github.com/ggerganov/whisper.cpp.git whisper_cpp_repo"))
        self.assertEqual(list(found), ["whisper_cpp_repo"])
        self.assertEqual(found["whisper_cpp_repo"]["url"], "https://github.com/ggerganov/whisper.cpp.git")
        self.assertEqual(found["whisper_cpp_repo"]["msg_index"], 0)

    def test_target_defaults_to_the_repository_name(self):
        found = ex.upstream_clones(_trace("git clone --depth 1 https://github.com/org/proj.git && ls"))
        self.assertEqual(list(found), ["proj"])
        found = ex.upstream_clones(_trace("git clone git@github.com:org/other.git; cd other"))
        self.assertEqual(list(found), ["other"])

    def test_sandbox_roots_are_stripped_from_the_target(self):
        found = ex.upstream_clones(_trace("git clone https://x.example/a/b.git /home/user/vendor/b"))
        self.assertEqual(list(found), ["vendor/b"])

    def test_a_local_clone_is_the_users_own_material(self):
        self.assertEqual(ex.upstream_clones(_trace("git clone /srv/repos/mine.git mine")), {})
        self.assertEqual(ex.upstream_clones(_trace("git clone ../sibling copy")), {})

    def test_only_shell_calls_count(self):
        trace = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "please git clone https://github.com/org/proj"}]}]}
        self.assertEqual(ex.upstream_clones(trace), {})


class _Revision(SimpleNamespace):
    pass


def _workspace(files: dict[str, str]):
    revisions = {p: _Revision(path=p, msg_index=1, content=c, source="workspace-inline", origin="output")
                 for p, c in files.items()}
    return SimpleNamespace(session_id="s", raw_revisions=[], at=lambda at: dict(revisions),
                           _events=lambda: iter(()))


class ClonedDirectoriesLeaveTheFixture(unittest.TestCase):

    def test_clone_files_are_excluded_and_accounted(self):
        ws = _workspace({"/home/user/app/main.py": "print(1)\n",
                         "/home/user/whisper_cpp_repo/CMakeLists.txt": "x" * 100,
                         "/home/user/whisper_cpp_repo/src/whisper.cpp": "y" * 900})
        clones = {"whisper_cpp_repo": {"dir": "whisper_cpp_repo", "url": "https://u/w.git", "msg_index": 3}}
        with tempfile.TemporaryDirectory() as tmp:
            docs, skipped = ex.tree_at(ws, 5, Path(tmp), clones)
        self.assertEqual([d.path for d in docs], ["app/main.py"])
        self.assertEqual(skipped["upstream_clones"],
                         [{"dir": "whisper_cpp_repo", "url": "https://u/w.git", "msg_index": 3,
                           "files": 2, "bytes": 1000}])
        self.assertEqual(skipped["vendored"], 0)

    def test_without_clones_nothing_changes(self):
        ws = _workspace({"/home/user/app/main.py": "print(1)\n", "/home/user/node_modules/x/i.js": ""})
        with tempfile.TemporaryDirectory() as tmp:
            docs, skipped = ex.tree_at(ws, 5, Path(tmp))
        self.assertEqual([d.path for d in docs], ["app/main.py"])
        self.assertEqual(skipped["upstream_clones"], [])
        self.assertEqual(skipped["vendored"], 1)

    def test_a_directory_that_merely_shares_a_prefix_stays(self):
        ws = _workspace({"/home/user/proj/a.py": "a", "/home/user/proj2/b.py": "b"})
        clones = {"proj": {"dir": "proj", "url": "https://u/proj", "msg_index": 1}}
        with tempfile.TemporaryDirectory() as tmp:
            docs, _ = ex.tree_at(ws, 5, Path(tmp), clones)
        self.assertEqual([d.path for d in docs], ["proj2/b.py"])


class ASeedKeepsItsBundleAcrossBatches(unittest.TestCase):

    def _job(self, root: Path, folder: str, name: str, session: str, seed: int) -> Path:
        bundle = root / folder / name
        (bundle / "verification").mkdir(parents=True)
        (bundle / "verification/state.json").write_text(json.dumps(
            {"schema": "arena-pipeline/job/1", "status": "validated",
             "session_id": session, "seed_index": seed}))
        return bundle

    def test_a_reranked_seed_finds_its_earlier_bundle(self):
        from pipeline.orchestrate import _bundle_for
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            earlier = self._job(root, "target-F3-replay", "c-003-widget", "sess-a", 2)
            row = {"category": "F3", "session_id": "sess-a", "seed_index": 2}
            self.assertEqual(_bundle_for(root, row, "c-007-widget"), earlier)

    def test_another_seed_of_the_same_session_is_a_new_bundle(self):
        from pipeline.orchestrate import _bundle_for
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._job(root, "target-F3-replay", "c-003-widget", "sess-a", 2)
            row = {"category": "F3", "session_id": "sess-a", "seed_index": 3}
            self.assertEqual(_bundle_for(root, row, "c-007-widget"),
                             root / "target-F3-replay/c-007-widget")

    def test_a_reranked_seed_returns_its_retained_result_under_its_old_name(self):
        from contextlib import nullcontext
        from dataclasses import asdict
        from unittest.mock import Mock, patch
        from pipeline import checkpoint, orchestrate
        from pipeline.artifacts import atomic_json, identity
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "target-F3-replay/c-003-widget"
            (bundle / "tests").mkdir(parents=True)
            (bundle / "task.toml").write_text("name = 'x'\n")
            (bundle / "tests/checks.json").write_text("{}")
            retained = orchestrate.TaskResult(slug="c-003-widget", session_id="src", seed_index=0,
                                              tier=1, status="validated", stage_reached="probe")
            checkpoint.update(bundle, stage="validated")
            atomic_json(bundle / orchestrate.JOB_STATE, {
                "schema": "arena-pipeline/job/1", **asdict(retained),
                "artifact_sha256": identity(bundle), "finished_at": 1.0, "release_ready": False})
            row = {"session_id": "src", "seed_index": 0, "category": "F3", "tier": 1, "_rank": 7,
                   "classification": {"domain": "widget"}}
            with patch.object(orchestrate.extract_stage, "extract",
                              side_effect=AssertionError("work was redone")) as extract:
                result = orchestrate.build_one(row, 7, root, Mock(), nullcontext())
            self.assertEqual(result.status, "validated", result.error)
            self.assertEqual(result.slug, "c-003-widget")
            extract.assert_not_called()
            self.assertFalse((root / "target-F3-replay/c-007-widget").exists())

    def test_two_bundles_for_one_seed_refuse_to_build_a_third(self):
        from pipeline.orchestrate import _bundle_for
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._job(root, "target-F3-replay", "c-003-widget", "sess-a", 2)
            self._job(root, "target-F2-scenario", "c-009-widget", "sess-a", 2)
            row = {"category": "F3", "session_id": "sess-a", "seed_index": 2}
            with self.assertRaises(ValueError):
                _bundle_for(root, row, "c-011-widget")


class ARebuiltSeedDeclaresItsRetiredAttempt(unittest.TestCase):
    """The novelty check offered c-006's own retired attempt as a competing peer
    and the reviewer called the task a duplicate (2026-09-28)."""

    def _built(self, root: Path, folder: str, name: str, session: str, seed: int, note: str | None = None) -> Path:
        bundle = root / folder / name
        (bundle / "verification").mkdir(parents=True)
        (bundle / "tests").mkdir()
        (bundle / "task.toml").write_text("name = 'x'\n")
        (bundle / "tests/checks.json").write_text("{}")
        (bundle / "verification/state.json").write_text(json.dumps(
            {"schema": "arena-pipeline/job/1", "status": "blocked", "session_id": session, "seed_index": seed}))
        if note:
            (bundle / "RETIRED-NOTE.md").write_text(note)
        return bundle

    def test_a_retired_attempt_under_another_envs_tree_becomes_a_predecessor(self):
        from unittest.mock import patch
        from pipeline import orchestrate
        from pipeline.artifacts import identity
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            retired = self._built(workspace, "envs-retired", "c-003-widget-attempt1", "sess-a", 2,
                                  "# Retired 2026-09-28: first build attempt of seed sess-a/2\n\nbecause.\n")
            self._built(workspace, "envs-retired", "c-009-other", "sess-b", 0)
            out_root = workspace / "envs"
            new = out_root / "target-F3-replay/c-006-widget"
            (new / "provenance").mkdir(parents=True)
            row = {"session_id": "sess-a", "seed_index": 2}
            with patch.object(orchestrate, "__file__", str(workspace / "pipeline/orchestrate.py")):
                declared = orchestrate._declare_lineage(new, row, out_root)
            self.assertEqual(declared, [identity(retired)])
            doc = json.loads((new / "provenance/replacement.json").read_text())
            self.assertEqual(doc["schema"], "arena-pipeline/replacement/1")
            self.assertEqual(doc["predecessors"], [identity(retired)])
            self.assertIn("c-003-widget-attempt1: Retired 2026-09-28", doc["reason"])

    def test_no_retired_attempt_means_no_declaration(self):
        from unittest.mock import patch
        from pipeline import orchestrate
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            out_root = workspace / "envs"
            new = out_root / "target-F3-replay/c-006-widget"
            (new / "provenance").mkdir(parents=True)
            with patch.object(orchestrate, "__file__", str(workspace / "pipeline/orchestrate.py")):
                self.assertEqual(orchestrate._declare_lineage(new, {"session_id": "sess-a", "seed_index": 2}, out_root), [])
            self.assertFalse((new / "provenance/replacement.json").exists())


if __name__ == "__main__":
    unittest.main()
