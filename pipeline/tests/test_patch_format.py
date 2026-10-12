import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from pipeline.author.patch_format import normalize_counts, normalize_staged_patches


PATCH = "--- a/example.py\n+++ b/example.py\n@@ -1,3 +1,3 @@ optional context\n old\n-old_value\n+new_value\n+extra_value\n"


class PatchFormattingTests(unittest.TestCase):
    def test_only_count_metadata_changes_and_strict_git_accepts(self):
        result, count = normalize_counts(PATCH)
        self.assertEqual(count, 1)
        self.assertIn("@@ -1,2 +1,3 @@ optional context", result)
        self.assertEqual([line for line in result.splitlines() if not line.startswith("@@")],
                         [line for line in PATCH.splitlines() if not line.startswith("@@")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.py").write_text("old\nold_value\n")
            patch = root / "candidate.patch"
            patch.write_text(result)
            checked = subprocess.run(["git", "apply", "--check", str(patch)], cwd=root,
                                     capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_correct_patch_is_byte_identical_and_idempotent(self):
        correct = PATCH.replace("-1,3", "-1,2")
        self.assertEqual(normalize_counts(correct), (correct, 0))
        first, _ = normalize_counts(PATCH)
        self.assertEqual(normalize_counts(first), (first, 0))

    def test_crlf_and_no_final_newline_are_preserved(self):
        for text in (PATCH.replace("\n", "\r\n"), PATCH.rstrip("\n")):
            result, count = normalize_counts(text)
            self.assertEqual(count, 1)
            self.assertEqual(text.endswith("\n"), result.endswith("\n"))
            if "\r\n" in text:
                self.assertEqual(result.count("\r\n"), text.count("\r\n"))

    def test_zero_sided_hunks_and_no_newline_marker(self):
        text = "--- /dev/null\n+++ b/example.py\n@@ -0,1 +1,2 @@\n+new\n\\ No newline at end of file\n"
        result, count = normalize_counts(text)
        self.assertEqual(count, 1)
        self.assertIn("@@ -0,0 +1,1 @@", result)
        self.assertTrue(result.endswith("\\ No newline at end of file\n"))

    def test_multiple_hunks_and_explicit_file_boundaries(self):
        text = PATCH + "@@ -10,2 +11,2 @@\n-one\n+two\ndiff --git a/second b/second\n--- a/second\n+++ b/second\n@@ -1,2 +1,2 @@\n-one\n+two\n"
        result, count = normalize_counts(text)
        self.assertEqual(count, 3)
        self.assertIn("@@ -10,1 +11,1 @@", result)
        self.assertIn("@@ -1,1 +1,1 @@", result)

    def test_unknown_body_or_ambiguous_plain_file_boundary_is_preserved(self):
        for text in (PATCH + "unrecognized trailer\n", PATCH + "--- a/second\n+++ b/second\n"):
            self.assertEqual(normalize_counts(text), (text, 0))

    def test_stale_context_is_not_repaired_or_credited(self):
        result, _ = normalize_counts(PATCH)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.py").write_text("different\nold_value\n")
            patch = root / "candidate.patch"
            patch.write_text(result)
            checked = subprocess.run(["git", "apply", "--check", str(patch)], cwd=root,
                                     capture_output=True, text=True)
            self.assertNotEqual(checked.returncode, 0)

    def test_all_declared_patch_sections_and_direct_entry_paths(self):
        full = {"solution": {"01-step": {"patch": PATCH}},
                "alternative": {"01-step": {"patch": PATCH}},
                "mutants": [{"id": "m_one", "patch": PATCH}],
                "quality": {"patch": PATCH}}
        result, updates = normalize_staged_patches([], full)
        self.assertEqual(len(updates), 3)
        self.assertEqual(result["quality"], full["quality"])
        self.assertEqual(full["solution"]["01-step"]["patch"], PATCH)
        for path, value in ((["solution", "01-step"], {"patch": PATCH}),
                            (["alternative", "01-step", "patch"], PATCH),
                            (["mutants", 0], {"patch": PATCH})):
            self.assertEqual(len(normalize_staged_patches(path, value)[1]), 1)

    def test_fixture_text_and_other_fields_are_not_formatted(self):
        for path in (["fixture", "patch"], ["quality"], ["instruction"]):
            self.assertEqual(normalize_staged_patches(path, {"patch": PATCH}), ({"patch": PATCH}, []))

    def test_actual_stage_handler_persists_the_normalized_draft_and_reports_it(self):
        from pipeline.author.agent_designer import _handlers
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = SimpleNamespace(session_id="s", seed_index=0, source_root=root, moments=[])
            draft_path = root / "draft.json"
            handlers = _handlers(root, extraction, {"messages": []}, draft_path)
            response = handlers["stage_spec"](["solution", "01-step"], {"patch": PATCH})
            persisted = json.loads(draft_path.read_text())
            self.assertEqual(persisted, handlers["draft_spec"]())
            self.assertEqual(persisted["solution"]["01-step"]["patch"], normalize_counts(PATCH)[0])
            self.assertEqual(response["patch_header_normalization"][0]["hunk_counts_corrected"], 1)


if __name__ == "__main__":
    unittest.main()
