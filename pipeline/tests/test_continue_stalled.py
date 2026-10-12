"""A design that ran out of steps is continued only on request, from its draft."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import checkpoint


def stalled_bundle(root: Path, problems=("check 'c1': rule_quote is too long")) -> Path:
    bundle = root / "c-001-x"
    session = bundle / "verification/design-sessions/s1"
    session.mkdir(parents=True)
    draft = {"checks": [{"id": "c1"}], "traps": []}
    (session / "draft.json").write_text(json.dumps(draft))
    sha = hashlib.sha256((session / "draft.json").read_bytes()).hexdigest()
    (session / "stalled.json").write_text(json.dumps({"steps": 140, "draft_sha256": sha}))
    result = json.dumps({"clean": False, "problems": list(problems)})
    (session / "conversation.json").write_text(json.dumps([
        {"role": "user", "content": [{"type": "tool_result", "content": result}]}]))
    checkpoint.update(bundle, stage="stalled",
                      authoring_session={"session": "verification/design-sessions/s1"})
    return bundle


class ContinueStalled(unittest.TestCase):
    def test_the_draft_and_its_remaining_problems_are_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = checkpoint.stalled_design(stalled_bundle(Path(tmp), ["p1", "p2"]))
        self.assertEqual(found["draft"]["checks"], [{"id": "c1"}])
        self.assertEqual(found["problems"], ["p1", "p2"])
        self.assertEqual(found["steps"], 140)

    def test_a_changed_draft_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = stalled_bundle(Path(tmp))
            (bundle / "verification/design-sessions/s1/draft.json").write_text("{}")
            with self.assertRaises(ValueError):
                checkpoint.stalled_design(bundle)

    def test_only_a_stalled_checkpoint_qualifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = stalled_bundle(Path(tmp))
            checkpoint.update(bundle, stage="validated")
            self.assertIsNone(checkpoint.stalled_design(bundle))

    def test_it_never_happens_without_the_flag(self):
        from pipeline import orchestrate
        source = Path(orchestrate.__file__).read_text()
        self.assertIn('os.environ.get("ARENA_CONTINUE_STALLED") == "1"', source)
        self.assertNotEqual(os.environ.get("ARENA_CONTINUE_STALLED"), "1")


class BuildFlagsSpendNothingOnDifficulty(unittest.TestCase):
    """The presolve switch `--qualify` used to force is gone with presolve."""

    def test_build_flags_only_continue_a_stalled_design(self):
        import argparse
        from pipeline import orchestrate
        self.assertFalse(hasattr(orchestrate, "PRESOLVE_FORCED"))
        self.assertFalse(hasattr(orchestrate, "presolve_enabled"))
        previous = os.environ.pop("ARENA_CONTINUE_STALLED", None)
        try:
            orchestrate._apply_build_flags(argparse.Namespace(continue_stalled=False))
            self.assertIsNone(os.environ.get("ARENA_CONTINUE_STALLED"))
        finally:
            if previous is not None:
                os.environ["ARENA_CONTINUE_STALLED"] = previous


class ReviewValidityUnderParallelWork(unittest.TestCase):
    def test_only_same_source_peers_bind_a_review(self):
        import inspect
        from pipeline import review
        src = inspect.getsource(review)
        self.assertIn("same-source peers", src)
        self.assertNotIn("did not see the current retrieved corpus\")", src)
