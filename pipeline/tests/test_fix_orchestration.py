"""Regression tests for orchestration defects that turned our failures into results.

Each class names the defect it pins. Every test here fails on the behaviour it
replaces: a session we could not read reported as "no seed extracted", office
and PDF fixtures passing the privacy scan unread, a finished task re-run into
an error, one-shot authoring in the category batch, a wall-clock deadline that
voided rollouts, and tables duplicated between `select/` and `policy.py`.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
import zlib
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from pipeline import accounting, checkpoint, orchestrate, policy, provider
from pipeline.artifacts import atomic_json, identity
from pipeline.dump import index


def _session(root: Path, session_id: str, *, outcomes: bool = True) -> Path:
    folder = root / session_id
    (folder / "workspaces").mkdir(parents=True)
    trace = {"session_id": session_id, "messages": [
        {"role": "user", "node_ids": ["n0"], "content": [{"type": "text", "text": "draft"}]},
        {"role": "assistant", "node_ids": ["n1"], "content": []}],
        "tasks": [{"task_index": 0, "task_key": "k", "n_turns": 2, "turn_ids": ["n0", "n1"]}]}
    (folder / "data.json").write_text(json.dumps({"traces": [trace], "metadata": {}}))
    if outcomes:
        (folder / "outcomes.json").write_text(json.dumps({"outcomes": []}))
    return folder


class UnreadableSessionsAreNamed(unittest.TestCase):
    """dump/index.py: a read failure is ours, never a finding about the trace."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "sessions"
        _session(self.root, "good")
        truncated = _session(self.root, "truncated") / "data.json"
        truncated.write_bytes(truncated.read_bytes()[:40])
        _session(self.root, "no-outcomes", outcomes=False)

    def test_index_session_raises_instead_of_returning_nothing(self):
        with self.assertRaises(index.UnreadableSession):
            index.index_session("truncated", self.root)
        with self.assertRaisesRegex(index.UnreadableSession, "outcomes.json"):
            index.index_session("no-outcomes", self.root)

    def test_build_records_every_unreadable_session(self):
        built = index.build(self.root, workers=1)
        self.assertEqual(built["sessions"], 3)
        self.assertEqual({e["session_id"] for e in built["index_errors"]},
                         {"truncated", "no-outcomes"})
        # The host path never reaches a report.
        self.assertNotIn(str(self.tmp), json.dumps(built["index_errors"]))

    def test_accounting_calls_them_unassessed_not_no_seed_extracted(self):
        artifacts = self.tmp / "artifacts"
        artifacts.mkdir()
        atomic_json(artifacts / "index.json", {"rows": [], "index_errors": [
            {"session_id": "truncated", "error": "JSONDecodeError: Unterminated string"}]})
        atomic_json(artifacts / "classified.json", {"rows": []})
        derived = accounting.exclusions(["truncated", "empty"], artifacts)
        self.assertEqual(derived["truncated"]["code"], accounting.UNASSESSED)
        self.assertIn("could not read", derived["truncated"]["reason"])
        # A session that was read and yielded nothing is still a real finding.
        self.assertEqual(derived["empty"]["code"], "no_seed_extracted")
        # Undispositioned in the report, never excluded.
        self.assertEqual(set(accounting.reasons(["truncated", "empty"], artifacts)), {"empty"})


def _office(path: Path, member: str, xml: str, extra: dict | None = None) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(member, xml)
        for name, body in (extra or {}).items():
            archive.writestr(name, body)


def _flate_pdf(text: str) -> bytes:
    content = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content)
               + content + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, xref))
    return out.getvalue()


class PrivacyScanReadsBinaries(unittest.TestCase):
    """privacy.scan: compressed documents are scanned, or reported unscannable."""

    EMAIL = "jane.roe@acme-corp.io"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def kinds(self):
        from pipeline.privacy import scan
        findings = scan(self.root)
        self.assertNotIn(self.EMAIL, json.dumps(findings))
        out = {}
        for finding in findings:
            out.setdefault(finding["file"], set()).add(finding["kind"])
        return out

    def test_office_documents_are_scanned_through_their_text(self):
        _office(self.root / "memo.docx", "word/document.xml",
                f'<w:document xmlns:w="w"><w:t>Contact {self.EMAIL}</w:t></w:document>')
        _office(self.root / "book.xlsx", "xl/sharedStrings.xml",
                f'<sst xmlns="s"><si><t>{self.EMAIL}</t></si></sst>')
        _office(self.root / "deck.pptx", "ppt/slides/slide1.xml",
                f'<p:sld xmlns:p="p" xmlns:a="a"><a:t>{self.EMAIL}</a:t></p:sld>')
        kinds = self.kinds()
        for name in ("memo.docx", "book.xlsx", "deck.pptx"):
            self.assertIn("email", kinds.get(name, set()), name)

    def test_parts_outside_the_body_text_are_scanned(self):
        _office(self.root / "commented.docx", "word/document.xml",
                '<w:document xmlns:w="w"><w:t>clean body</w:t></w:document>',
                {"word/comments.xml": f'<w:comments xmlns:w="w" a="{self.EMAIL}"/>'})
        self.assertIn("email", self.kinds()["commented.docx"])

    def test_flate_pdf_is_scanned_through_its_text(self):
        (self.root / "letter.pdf").write_bytes(_flate_pdf(self.EMAIL))
        self.assertIn(self.EMAIL.encode(), zlib.decompress(
            _flate_pdf(self.EMAIL).split(b"stream\n")[1].split(b"\nendstream")[0]))
        self.assertNotIn(self.EMAIL.encode(), (self.root / "letter.pdf").read_bytes())
        self.assertEqual(self.kinds()["letter.pdf"], {"email"})

    def test_archive_members_are_scanned(self):
        with zipfile.ZipFile(self.root / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("notes.txt", "sk-proj-" + "x" * 30)
        self.assertEqual(self.kinds()["bundle.zip"], {"credential"})

    def test_what_cannot_be_read_is_a_finding_not_silence(self):
        (self.root / "broken.pdf").write_bytes(b"%PDF-1.4\n" + zlib.compress(self.EMAIL.encode()))
        (self.root / "blob.bin").write_bytes(bytes(range(256)) * 4)
        (self.root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(40))
        _office(self.root / "media.docx", "word/document.xml",
                '<w:document xmlns:w="w"><w:t>clean</w:t></w:document>',
                {"word/media/image1.png": b"\x89PNG\r\n\x1a\n" + bytes(40)})
        kinds = self.kinds()
        for name in ("broken.pdf", "blob.bin", "shot.png", "media.docx"):
            self.assertEqual(kinds.get(name), {"unscannable"}, name)

    def test_plain_and_single_byte_text_stay_clean(self):
        (self.root / "plain.txt").write_text("nothing private here\n")
        (self.root / "latin1.csv").write_bytes("name,city\nJos\xe9,M\xe1laga\n".encode("latin-1"))
        self.assertEqual(self.kinds(), {})


class FinishedTasksResumeAsFinished(unittest.TestCase):
    """orchestrate.build_one: a terminal checkpoint returns the retained result."""

    ROW = {"session_id": "src", "seed_index": 0, "category": "A", "tier": 1}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = self.root / "rich/c-001-x"
        (self.bundle / "tests").mkdir(parents=True)
        (self.bundle / "task.toml").write_text("name = 'x'\n")
        (self.bundle / "tests/checks.json").write_text("{}")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(orchestrate, "_slug", return_value="c-001-x"))
        self.extract = stack.enter_context(patch.object(
            orchestrate.extract_stage, "extract", side_effect=AssertionError("work was redone")))

    def finish(self, status):
        result = orchestrate.TaskResult(slug="c-001-x", session_id="src", seed_index=0,
                                        tier=1, status=status, stage_reached="probe",
                                        blockers=["kept"] if status == "blocked" else [])
        checkpoint.update(self.bundle, stage=status)
        atomic_json(self.bundle / orchestrate.JOB_STATE, {
            "schema": "arena-pipeline/job/1", **asdict(result),
            "artifact_sha256": identity(self.bundle), "finished_at": 1.0,
            "release_ready": False})

    def build(self):
        return orchestrate.build_one(self.ROW, 1, self.root, Mock(), nullcontext())

    def test_each_terminal_stage_returns_its_retained_result(self):
        for status in sorted(orchestrate.TERMINAL_STAGES):
            with self.subTest(status=status):
                self.finish(status)
                result = self.build()
                self.assertEqual(result.status, status, result.error)
                self.assertEqual(result.stage_reached, "probe")
                self.extract.assert_not_called()
                self.assertFalse((self.bundle / "verification/orchestrator-error.txt").exists())
                state = json.loads((self.bundle / orchestrate.JOB_STATE).read_text())
                self.assertEqual(state["status"], status)

    def test_direct_build_returns_the_retained_result(self):
        self.finish("validated")
        result = orchestrate._build_one(self.ROW, 1, self.root, Mock(), nullcontext())
        self.assertEqual(result.status, "validated", result.error)

    def test_a_changed_artifact_is_not_reported_as_finished(self):
        self.finish("validated")
        (self.bundle / "tests/checks.json").write_text('{"changed": true}')
        result = self.build()
        self.assertEqual(result.status, "error")
        self.assertIn("no longer matches", result.error)
        self.extract.assert_not_called()
        # The retained record is kept for inspection, not overwritten.
        self.assertEqual(json.loads((self.bundle / orchestrate.JOB_STATE).read_text())["status"],
                         "validated")


class CategoryBatchRepairsByPolicy(unittest.TestCase):
    """category_batch: repair budget is policy.MAX_REPAIR_PASSES, not zero."""

    def test_default_budget_is_the_policy_budget_and_zero_stays_explicit(self):
        from category_batch import gate_repair_budget
        self.assertEqual(gate_repair_budget({}), policy.MAX_REPAIR_PASSES)
        self.assertGreater(gate_repair_budget({}), 0)
        self.assertEqual(gate_repair_budget({"repair_budget": {"targeted_gate_repairs": 0}}), 0)
        with self.assertRaises(ValueError):
            gate_repair_budget({"repair_budget": {"targeted_gate_repairs": -1}})

    def test_a_fresh_build_receives_the_policy_budget(self):
        from category_batch import Controller, source_key
        from pipeline.orchestrate import TaskResult
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work = Path(tmp)
            controller.budget = None
            controller.request = {}
            source = {"session_id": "new", "seed_index": 0, "category": "F3", "_rank": 1}
            controller.records = {}
            controller.update = lambda *args, **kwargs: None
            result = TaskResult(slug="d", session_id="new", seed_index=0, tier=1,
                                status="validated")
            with patch("pipeline.orchestrate.build_one", return_value=result) as build:
                controller.build(source)
            self.assertEqual(build.call_args.kwargs["gate_repairs"], policy.MAX_REPAIR_PASSES)
            self.assertIsInstance(source_key(source), str)

    def test_the_worker_and_the_brief_do_not_force_one_shot(self):
        import category_batch
        source = Path(category_batch.__file__).read_text()
        self.assertNotIn('ARENA_GATE_REPAIRS="0"', source)
        self.assertNotIn("gate_repairs=0", source)
        brief = " ".join(category_batch.DESIGN_BRIEF)
        self.assertNotIn("No automatic repair round", brief)
        self.assertIn("targeted repair passes", brief)


class NoWallClockDeadlineOnRollouts(unittest.TestCase):
    """provider: time is not a budget, and exhaustion stops rather than voids."""

    def test_admission_has_no_deadline(self):
        budget = provider.RequestBudget(concurrency=1, max_requests=10)
        with budget.slot():
            pass
        budget.started -= 10 ** 6            # far past the former 4-hour cutoff
        with budget.slot():
            pass
        self.assertEqual(budget.stats()["requests"], 2)
        self.assertNotIn("wall_seconds", budget.stats())

    def test_exhaustion_escapes_a_rollout_handler_instead_of_voiding(self):
        self.assertFalse(issubclass(provider.ProviderBudgetExhausted, Exception))
        budget = provider.RequestBudget(concurrency=1, max_requests=1)
        with budget.slot():
            pass
        voids = []
        with self.assertRaises(provider.ProviderBudgetExhausted):
            try:                                # the shape of calibrate.run's handler
                with budget.slot():
                    pass
            except Exception as exc:            # noqa: BLE001
                voids.append(exc)
        self.assertEqual(voids, [])

    def test_batch_admission_has_no_deadline_and_stops_loudly(self):
        from category_batch import ProviderAdmission, provider_failure
        admission = ProviderAdmission({"anthropic": 1, "openai": 1}, max_requests=2,
                                      elapsed_seconds=10 ** 6)
        with admission.slot():
            pass
        with admission.slot("openai"):
            pass
        with self.assertRaises(provider.ProviderBudgetExhausted) as caught:
            with admission.slot():
                pass
        self.assertTrue(provider_failure(caught.exception))


class SelectionReadsPolicy(unittest.TestCase):
    """select/ and config read policy.py; nothing retypes its tables."""

    def test_tables_are_derived_from_policy_with_identical_values(self):
        from pipeline import config
        from pipeline.select import priority
        self.assertIs(config.CATEGORY_DIRS, policy.TARGET_LEVEL_DIRS)
        self.assertEqual(priority.TIER_CUTS, {1: 0.53, 2: 0.40, 3: 0.28})
        self.assertEqual(tuple(priority.TIER_CUTS.values()), policy.CATEGORY_BOUNDARIES)
        self.assertEqual(priority.TIER_TRACE_FLOOR, {1: 0.70, 2: 0.55, 3: 0.40, 4: 0.30})
        self.assertEqual(priority.CATEGORY_FIXTURE_FLOOR, policy.CATEGORY_FIXTURE_FLOOR)
        self.assertEqual(priority.FIXTURE_FLOOR_MIN, policy.FIXTURE_FLOOR_MIN)
        source = (Path(priority.__file__)).read_text()
        for literal in ("{1: 0.53", "{1: 0.70", '{"A": 0.90', "= 0.80\n"):
            self.assertFalse(literal in source, f"retyped policy table: {literal!r}")

    def test_dead_helpers_are_gone(self):
        from pipeline import config
        from pipeline.dump import recover
        from pipeline.select import priority
        self.assertFalse(hasattr(priority, "take"))
        self.assertFalse(hasattr(recover, "materialise"))
        for name in ("multiversion", "manifests", "tests", "binary_paths"):
            self.assertFalse(hasattr(recover.Workspace, name), name)
        for name in ("GOOD_TURNS", "GOOD_CORRECTIONS", "GOOD_RECOVERED_PATHS", "LIVE_SIGNALS"):
            self.assertFalse(hasattr(config, name), name)

    def test_config_does_not_claim_its_effort_applies_to_calibration(self):
        from pipeline import config
        source = Path(config.__file__).read_text()
        self.assertNotIn("It applies to CALIBRATION too", source)
        self.assertIn("policy.SPEC_CALIBRATION_EFFORT", source)


class ShortlistWritesNothingUnread(unittest.TestCase):
    """select/shortlist: batch.json and briefs had no reader, so none are written."""

    def test_main_composes_without_writing_files(self):
        from pipeline.select import shortlist
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic_json(root / "classified.json", {"rows": []})
            with patch.object(shortlist, "ARTIFACTS", root), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(shortlist.main(["-i", str(root / "classified.json")]), 0)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["classified.json"])



class GateRepairsPassTheGovernor(unittest.TestCase):
    """orchestrate: a gate repair is admitted by checkpoint.admit_repair, not charged blind."""

    def run_gate_blocked(self, message):
        from pipeline import orchestrate as m
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "rich/draft"
            (bundle / "environment/repo").mkdir(parents=True)
            (bundle / "environment/repo/source.md").write_text("Retained source")
            (bundle / "authoring/extraction").mkdir(parents=True)
            (bundle / "authoring/extraction/extraction.json").write_text("{}")
            (bundle / "authoring/traps.json").write_text("{}")
            designer = Mock(return_value={"source": {"session_id": "source", "seed_index": 0}})
            source = {"session_id": "source", "seed_index": 0, "category": "A"}
            blocked = {"verdict": "blocked", "results": [
                {"gate": "test", "blockers": [{"code": "unresolved", "message": message}]}]}
            with patch.object(m, "GATE_REPAIRS", 1), \
                    patch.object(m, "_slug", return_value="draft"), \
                    patch.object(m.extract_stage, "extract"), \
                    patch.object(m.extract_stage, "usable_correction_count", return_value=0), \
                    patch.object(m.build_stage, "build"), \
                    patch.object(m.faithfulness, "build", return_value={"faithfulness_by_bytes": 1}), \
                    patch.object(m.probe_stage, "run", return_value={"results": {}}), \
                    patch.object(m.controls_stage, "run", return_value={'ok': True, 'errors': [], 'results': {}, 'advisories': []}), \
                    patch.object(m, "run_bundle", return_value=blocked):
                result = m._build_one(source, 1, root, designer, nullcontext(),
                                      gate_repairs=1, preserve_workspace=True)
            return result, designer

    def test_an_uncited_finding_is_refused_and_kept(self):
        result, designer = self.run_gate_blocked("still invalid")
        self.assertEqual(result.status, "blocked", result.error)
        self.assertTrue(any(b.startswith("repair refused:") for b in result.blockers),
                        result.blockers)
        self.assertIn("test: unresolved: still invalid", result.blockers)
        designer.assert_called_once()

    def test_no_orchestrate_path_charges_a_repair_without_the_governor(self):
        source = Path(orchestrate.__file__).read_text()
        self.assertNotIn("consume_repair(", source)
        # Export validation, gate and semantic review. Difficulty is never a
        # repair finding in the build (the breadth screen never blocked).
        self.assertEqual(source.count("progress.admit_repair("), 3)


class RepairChoicesFollowPolicy(unittest.TestCase):
    def test_qualify_rejects_more_passes_than_policy_allows(self):
        from pipeline import cli
        with patch.object(cli, "cmd_release", return_value=0) as release:
            self.assertEqual(cli.main(["qualify", "b", "--repair-attempts",
                                       str(policy.MAX_REPAIR_PASSES)]), 0)
            release.assert_called_once()
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli.main(["qualify", "b", "--repair-attempts",
                          str(policy.MAX_REPAIR_PASSES + 1)])

    def test_orchestrate_rejects_more_qualification_repairs_than_policy_allows(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            orchestrate.main(["--qualification-repairs", str(policy.MAX_REPAIR_PASSES + 1)])


class CliUsesSharedBundleDiscovery(unittest.TestCase):
    def test_bundles_follow_bundle_paths_and_accept_a_bundle_root(self):
        from pipeline import cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            delivered = root / "F3-replay/a_f3_ledger-review"
            working = root / "rich/c-001-draft"
            for bundle in (delivered, working):
                bundle.mkdir(parents=True)
            (delivered / "task.toml").write_text("")
            (root / "notes").mkdir()
            (root / "notes/task.toml").write_text("")      # not a bundle name
            self.assertEqual(cli._bundles(root), sorted([delivered, working]))
            self.assertEqual(cli._bundles(delivered), [delivered])

    def test_deliverable_checks_accounting_against_the_dump_index(self):
        from pipeline import cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions.json").write_text('["s"]')
            report = {"acceptance": {"ok": True}}
            with patch("pipeline.deliverable.build", return_value=report) as build, \
                    patch("pipeline.deliverable.render_markdown", return_value=""), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["deliverable", str(root), "--sessions",
                                 str(root / "sessions.json"), "--artifacts", str(root / "art")])
            self.assertEqual(code, 0)
            self.assertEqual(build.call_args.kwargs["index"], root / "art/index.json")


class CategoryBatchCountsTopUps(unittest.TestCase):
    def test_completed_calibration_reads_through_calibration_measurement(self):
        import category_batch
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp)
            campaign = candidate / "campaign"
            atomic_json(campaign / "report.json", {"summary": {"n": 10}})
            index = {"artifact_sha256": "sha", "campaigns": [str(campaign)],
                     "topups": {str(campaign): [str(candidate / "topup")]}}
            atomic_json(candidate / "verification/native-calibrations.json", index)
            measured = {"model": "m", "by_step": {}}
            with patch("pipeline.artifacts.identity", return_value="sha"), \
                    patch("pipeline.release.calibration_measurement",
                          return_value=([], measured)) as measure, \
                    patch("pipeline.measurement.task_profile", return_value={
                        "status": "ok", "bands": {}, "uninformative_steps": []}), \
                    patch("pipeline.benchmark.harbor.resolve_model", side_effect=lambda m: m):
                done = category_batch.completed_calibration(candidate, ["m"])
            self.assertTrue(done and done["complete"])
            args = measure.call_args.args
            self.assertEqual(args[1]["topups"], index["topups"])
            self.assertEqual(args[2], str(campaign))


if __name__ == "__main__":
    unittest.main()


class CategoriesAreTargetLevels(unittest.TestCase):
    """User, 2026-09-26: the categories are the spec's F3/F2/F1/F0, classified
    before authoring, and none of them blocks authoring."""

    def test_a_new_bundle_is_filed_under_its_target_level(self):
        import tempfile
        from pipeline.orchestrate import _bundle_for
        with tempfile.TemporaryDirectory() as tmp:
            for level, folder in (("F3", "target-F3-replay"), ("F0", "target-F0-inspired")):
                self.assertEqual(_bundle_for(Path(tmp), {"category": level}, "c-001-x"),
                                 Path(tmp) / folder / "c-001-x")

    def test_a_bundle_built_under_a_retired_folder_is_still_found(self):
        import tempfile
        from pipeline.orchestrate import _bundle_for
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "rich/c-007-video").mkdir(parents=True)
            # A resumed build continues the bundle it started, never a second one.
            self.assertEqual(_bundle_for(Path(tmp), {"category": "F3"}, "c-007-video"),
                             Path(tmp) / "rich/c-007-video")
            self.assertEqual(_bundle_for(Path(tmp), {"category": "A"}, "c-007-video"),
                             Path(tmp) / "rich/c-007-video")

    def test_no_level_blocks_authoring(self):
        from pipeline.select.categories import CATEGORIES, categorise
        from pipeline.author.agent_designer import TRAP_POLICY
        from pipeline.author.agent_designer import CATEGORY_GUIDANCE
        self.assertEqual(list(CATEGORIES), ["F3", "F2", "F1", "F0"])
        # Every level, F0 included, reaches the designer with guidance.
        self.assertEqual(set(TRAP_POLICY), set(CATEGORIES))
        self.assertEqual(set(CATEGORY_GUIDANCE), set(CATEGORIES))
        self.assertEqual(categorise({"replay_readiness": 0.1, "corrections": 0}), "F0")

    def test_the_build_classifies_before_it_authors(self):
        import inspect
        from pipeline import orchestrate
        body = inspect.getsource(orchestrate.main)
        self.assertLess(body.index("annotate(rows)"), body.index("report = batch("))
        self.assertIn("classified before authoring", body)
        # --category takes levels, and still accepts a retired letter as its level.
        self.assertIn("wanted = {target_level(c) for c in named}", body)
