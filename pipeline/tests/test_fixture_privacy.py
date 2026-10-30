"""Private data cannot reach a fixture through the designer's own tools.

The initial extraction is redacted before a design starts and delivery refuses
a bundle with findings, but on 2026-09-28 nothing in between checked a fixture
the designer re-lifted (`materialize_fixture` at a later message) or authored:
c-002 reached its repair round with an email address back in
`uploads/index.html`, and the repair session died at the next design's opening
scan. Now authored bytes are scanned before they land, a re-lifted tree gets
the same redaction pass as the initial extraction, a draft over a fixture with
findings is refused at check time, and the gate chain carries `privacy.clean`.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline import privacy
from pipeline.author import agent_designer
from pipeline.gates import code as gates
from pipeline.gates.base import GateContext, Severity


EMAIL = "reach me at first.last@corp-mail.example.org for the invoice\n"


class ScanText(unittest.TestCase):
    def test_an_email_is_a_finding_and_the_value_is_not_returned(self):
        findings = privacy.scan_text("uploads/index.html", "<p>" + EMAIL + "</p>")
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0]["kind"], "email")
        self.assertNotIn("first.last", str(findings))

    def test_clean_text_has_no_finding(self):
        self.assertEqual(privacy.scan_text("app.py", "def total(rows):\n    return sum(rows)\n"), [])


class AuthoredBytesAreScanned(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        (self.bundle / "environment/repo").mkdir(parents=True)
        (self.bundle / "environment/repo/app.py").write_text("print('ok')\n")
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle)
        self.handlers = agent_designer._handlers(self.bundle, extraction, {"messages": []},
                                                 self.bundle / "draft.json")

    def test_write_fixture_file_refuses_private_data_and_writes_nothing(self):
        with self.assertRaises(ValueError) as caught:
            self.handlers["write_fixture_file"](path="contact.txt", content=EMAIL,
                                               reason="a contact file the tests need to parse")
        self.assertIn("private data", str(caught.exception))
        self.assertNotIn("first.last", str(caught.exception))
        self.assertFalse((self.bundle / "environment/repo/contact.txt").exists())

    def test_write_fixture_file_accepts_placeholder_values(self):
        out = self.handlers["write_fixture_file"](path="contact.txt", content="support@example.com\n",
                                                  reason="a contact file the tests need to parse")
        self.assertEqual(out["written"], "contact.txt")
        self.assertTrue((self.bundle / "environment/repo/contact.txt").exists())


class TheGateBlocksADirtyFixture(unittest.TestCase):
    def _ctx(self, text: str) -> GateContext:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "environment/repo/uploads").mkdir(parents=True)
        (root / "environment/repo/uploads/index.html").write_text("<html>" + text + "</html>")
        return GateContext(task_dir=root, repo_root=root)

    def test_private_data_blocks(self):
        result = gates.privacy_gate.run(self._ctx(EMAIL))
        blockers = [f for f in result.findings if f.severity is Severity.BLOCKER]
        self.assertEqual(len(blockers), 1, result.findings)
        self.assertEqual(blockers[0].code, "private-data")
        self.assertIn("uploads/index.html", blockers[0].message)
        self.assertNotIn("first.last", blockers[0].message)

    def test_a_clean_fixture_passes(self):
        result = gates.privacy_gate.run(self._ctx("<p>hello</p>"))
        self.assertEqual(result.findings, [])

    def test_the_gate_is_in_the_chain(self):
        self.assertIn("privacy.clean", [g.name for g in gates.CHAIN])


class ARepairSessionCanRemoveWhatTheFirstSessionLeft(unittest.TestCase):
    """The opening scan refuses a fresh design over a dirty fixture (the source
    must be redacted before authoring). A repair or revision session is the
    one that can remove a finding its own earlier session left behind, so it
    receives the findings as its first instruction instead of dying on them;
    `check_spec` still refuses any draft while they remain."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = self.root / "candidate"
        fixture = self.bundle / "environment/repo/uploads"
        fixture.mkdir(parents=True)
        (fixture / "index.html").write_text("<p>" + EMAIL + "</p>")
        self.extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.root,
                                          stats={}, moments=[], before=[], after=[], changed_paths=[],
                                          evidence_summary={}, evidence_index=[])

    def _design(self, **kwargs):
        import copy
        from unittest.mock import patch
        sent = []
        responses = [{"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "reject", "name": "reject_seed",
             "input": {"reason": "no executable truth source", "source_evidence": []}}]}]
        with patch("pipeline.dump.recover.load_trace", return_value={"messages": []}), \
             patch("pipeline.author.agent_designer._handlers", return_value={
                 "draft_spec": lambda: {}, "submit_check": lambda spec: {"clean": True}}), \
             patch("pipeline.author.agent_designer._opening", return_value="Read the source."), \
             patch("pipeline.author.agent_designer._rejection",
                   return_value={"status": "rejected", "reason": "no executable truth source"}), \
             patch("pipeline.author.agent_designer._call",
                   side_effect=lambda body: sent.append(copy.deepcopy(body)) or responses[0]):
            candidate = agent_designer.design({}, self.extraction, self.bundle, max_steps=4, **kwargs)
            self.assertTrue(candidate["_source_capacity_report"]["validation_deferred"])
        return sent

    def test_a_fresh_design_can_repair_a_dirty_fixture_before_validation(self):
        sent = self._design()
        opening = sent[0]["messages"][0]["content"]
        self.assertIn("validation will reject these findings", opening)
        self.assertIn("uploads/index.html", opening)
        self.assertNotIn("first.last", opening)

    def test_a_repair_session_is_told_and_proceeds(self):
        sent = self._design(feedback=["hidden.disjoint: tests/suites/data/x.txt appears inside the fixture"])
        opening = sent[0]["messages"][0]["content"]
        self.assertIn("PRIVATE DATA IN THE FIXTURE", opening)
        self.assertIn("uploads/index.html", opening)
        self.assertIn("email", opening)
        self.assertNotIn("first.last", opening)


class AFrozenCopyKeepsItsProvenance(unittest.TestCase):
    """The review's frozen copy must pass the same provenance check as the live
    bundle. c-003 (2026-09-28) was the first task whose repair session authored
    fixture files; its frozen copy lacked `authoring/extraction/fixture-authored.json`,
    the ledger rebuilt from the copy re-classified those files, and the second
    review died with "source provenance is unverified" while the live bundle
    passed."""

    def setUp(self):
        import json
        import os
        from unittest.mock import patch
        from pipeline.artifacts import atomic_json
        from pipeline.author.build import build as export
        from pipeline.tests.test_production import synthetic_bundle
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = synthetic_bundle(self.root)
        fixture = self.bundle / "environment/repo"
        originals = {str(p.relative_to(fixture)): p.read_text() for p in fixture.rglob("*") if p.is_file()}
        trace = {"session_id": "session", "messages": [{"role": "assistant", "content": [
            {"type": "tool-write_file", "input": {"path": name, "content": text}}
            for name, text in originals.items()]}]}
        atomic_json(self.root / "dump/sessions/session/data.json", {"traces": [trace]})
        self.enterContext(patch.dict(os.environ, ARENA_DUMP=str(self.root / "dump")))
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec.update(source={"session_id": "session", "seed_index": 0}, _producer={"model": "author"})
        # The designer authors one fixture file and declares it; the package is
        # exported afterwards, as the build does after a submission.
        (fixture / "notes/README.txt").parent.mkdir(parents=True, exist_ok=True)
        (fixture / "notes/README.txt").write_text("authored by the designer\n")
        import hashlib
        atomic_json(self.bundle / "authoring/extraction/fixture-authored.json", {
            "schema": "arena-coding/fixture-authored/1",
            "files": {"notes/README.txt": {"reason": "a stub the hidden tests import",
                                           "sha256": hashlib.sha256(b"authored by the designer\n").hexdigest()}}})
        export(spec, self.bundle)

    def _review_with(self, cite_for_pass):
        """Run the review with a stub critic that passes every check, citing
        `cite_for_pass(bundle_workspace)` for each; returns the report errors."""
        import json
        import shutil
        from unittest.mock import patch
        from pipeline.artifacts import atomic_json
        from pipeline.ledger.faithfulness import build
        from pipeline.review import review
        atomic_json(self.bundle / "provenance/ledger.json", build(self.bundle, "session", 0))
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())

        def critic(_prompt, public, work, model, _tag, max_steps):
            shutil.copytree(public, work / "workspace")
            atomic_json(work / "conversation.json", [])
            atomic_json(work / "request-config.json", {"model": model, "max_steps": max_steps})
            atomic_json(work / "deliverables/review.json", {"verdict": "pass", "checks": [
                {"id": c["id"], "verdict": "pass",
                 "reason": "The published sentence and the executed test agree; the check grades behaviour, not wording.",
                 "citations": cite_for_pass(work / "workspace")}
                for c in spec["checks"]], "ambiguities": [], "substantive_gaps": [], "shortcut_risks": [],
                "privacy_findings": [], "difficulty_assessment": "Synthetic verification only.",
                "novelty_assessment": "Synthetic verification only.", "untested_surfaces": ["No calibration."]})
            return SimpleNamespace(error=None, stop_reason="completed", tool_calls=1), None
        with patch("pipeline.calibrate.run._rollout_for", return_value=critic), \
             patch("pipeline.corpus.review_context", return_value={"peers": []}):
            return review(self.bundle, "critic")["errors"]

    def test_a_passing_objective_may_quote_the_instruction(self):
        # A coding task's published contract is the instruction; the reviewer
        # who quotes it (and the hidden test) has cited the source. The
        # document rule accepted `fixture/` only and voided the whole review.
        def cite(workspace):
            step = sorted((workspace / "steps").glob("*/instruction.md"))[0]
            line = next(l for l in step.read_text().splitlines() if len(l.strip()) >= 20)
            rel = step.relative_to(workspace).as_posix()
            grader = sorted((workspace / "private-grader/suites").rglob("*.py"))[0]
            gline = next(l for l in grader.read_text().splitlines() if len(l.strip()) >= 20)
            return [{"file": rel, "quote": line.strip()},
                    {"file": grader.relative_to(workspace).as_posix(), "quote": gline.strip()}]
        errors = self._review_with(cite)
        self.assertEqual([e for e in errors if "lacks a valid quotation" in e], [], errors)

    def test_a_passing_objective_quoting_only_the_hidden_test_is_not_justified(self):
        def cite(workspace):
            grader = sorted((workspace / "private-grader/suites").rglob("*.py"))[0]
            gline = next(l for l in grader.read_text().splitlines() if len(l.strip()) >= 20)
            return [{"file": grader.relative_to(workspace).as_posix(), "quote": gline.strip()}]
        errors = self._review_with(cite)
        self.assertTrue(any("lacks a valid quotation from the fixture or the instruction" in e for e in errors), errors)

    def test_the_declaration_is_not_export_drift(self):
        # c-001-turbomachinery (2026-09-28) lost a repair round to
        # "fixture-authored.json: present in the package but not generated by
        # the spec" the moment the file became a release surface.
        from pipeline import assurance
        self.assertEqual(assurance.generated_problems(self.bundle), [])

    def test_freeze_carries_the_declaration_and_the_check_passes_on_the_copy(self):
        from pipeline.artifacts import atomic_json, freeze
        from pipeline.ledger.faithfulness import build
        from pipeline.release import _provenance_problems
        ledger = build(self.bundle, "session", 0)
        self.assertEqual(ledger["counts"]["authored"], 1, ledger["counts"])
        atomic_json(self.bundle / "provenance/ledger.json", ledger)
        self.assertEqual(_provenance_problems(self.bundle), [])
        frozen = self.root / "frozen"
        freeze(self.bundle, frozen)
        self.assertTrue((frozen / "authoring/extraction/fixture-authored.json").is_file())
        self.assertFalse((frozen / "authoring/extraction/extraction.json").exists(),
                         "trace material stays out of the frozen copy")
        self.assertEqual(_provenance_problems(frozen), [])


if __name__ == "__main__":
    unittest.main()


class EditFixtureFile(AuthoredBytesAreScanned):
    """2026-10-30: a ~100 KB file rewritten whole through write_fixture_file outlasted the response stream."""

    def test_one_span_is_replaced_and_the_file_is_recorded_as_authored(self):
        (self.bundle / "environment/repo/page.html").write_text("<h1>Old</h1>\n<p>keep</p>\n")
        out = self.handlers["edit_fixture_file"](path="page.html", old="<h1>Old</h1>", new="<h1>New</h1>",
                                                 reason="retitle the page the decoy test reads")
        self.assertEqual((self.bundle / "environment/repo/page.html").read_text(), "<h1>New</h1>\n<p>keep</p>\n")
        self.assertEqual(out["origin"], "authored")
        self.assertEqual(out["edited"], "page.html")

    def test_ambiguous_missing_and_private_edits_change_nothing(self):
        page = self.bundle / "environment/repo/page.html"
        page.write_text("<p>a</p><p>a</p>\n")
        with self.assertRaises(ValueError):
            self.handlers["edit_fixture_file"](path="page.html", old="<p>a</p>", new="b", reason="an ambiguous span here")
        with self.assertRaises(FileNotFoundError):
            self.handlers["edit_fixture_file"](path="nope.html", old="a", new="b", reason="a file that does not exist")
        with self.assertRaises(ValueError):
            self.handlers["edit_fixture_file"](path="page.html", old="<p>a</p>\n", new=EMAIL, reason="adds private data")
        self.assertEqual(page.read_text(), "<p>a</p><p>a</p>\n")

    def test_the_tool_is_offered(self):
        self.assertIn("edit_fixture_file", [t["name"] for t in agent_designer._tools(10)])
